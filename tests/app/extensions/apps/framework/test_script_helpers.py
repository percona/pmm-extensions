# Copyright (C) 2026 Percona LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Cover the keying-agnostic script-app helpers directly.

Each helper takes the already-resolved script / meta, so the tests build a plain
in-memory ``Snippet`` (no DB row needed) and assert byte-identical output against
a hand-constructed expected value — the same guarantee the unchanged OpenAPI
snapshots prove end-to-end.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel
from starlette.datastructures import URL
from starlette.requests import Request

from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPUnprocessableEntityException,
)
from app.core.requests import RemoteAPI
from app.core.security import crypto_timestamp_serializer
from app.extensions.apps.dipper.constants import ARTIFACT_TYPE_DIPPER
from app.extensions.apps.framework.script_helpers import (
    build_artifact_download_url,
    build_execution_meta,
    build_script_preview,
    execute_script,
    post_task_execution,
)
from app.extensions.apps.framework.script_source import ScriptExecuteWrite, ScriptSource
from app.extensions.artifact_constants import ARTIFACT_DOWNLOAD_SALT
from app.extensions.config import extensions_settings
from app.extensions.snippets.config import snippets_settings
from app.extensions.snippets.constants import ARTIFACT_TYPE_SNIPPET
from app.extensions.snippets.models.snippet import (
    EXECUTOR_HOSTS_INPUT_NAME,
    FilePreview,
    Snippet,
    SnippetExecutionMeta,
    SUDO_INPUT_NAME,
)
from app.extensions.snippets.utils import (
    guess_mime_type,
    mime_type_to_highlighter_language,
)
from app.tasks.execution_request_secrets import MASKED_ARGS_META_KEY
from tests.app.extensions.path_unsafe_task_names import PATH_UNSAFE_TASKS

_MD5 = "a" * 32
_CREATED_TASK_ID = 42


def _snippet(*, filename: str = "script.sh", sudo: str | None = None) -> Snippet:
    """Return an unpersisted snippet, optionally carrying a ``sudo`` meta value."""
    snippet = Snippet(filename=filename, size=20, md5_digest=_MD5)
    if sudo is not None:
        snippet.meta = {**snippet.meta, "sudo": sudo}
    return snippet


def _execution_args(snippet: Snippet, extra: dict[str, object] | None = None):
    """Validate execution args for ``snippet`` with an executor host set."""
    return snippet.get_execution_model().model_validate(
        {EXECUTOR_HOSTS_INPUT_NAME: "host1", **(extra or {})}
    )


def _make_request(host: str = "extensions.example", root_path: str = "") -> Request:
    """Return a minimal HTTPS request whose host and mount prefix derive the base URL."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": (host, 443),
            "root_path": root_path,
            "path": f"{root_path}/api/apps/snippets/snippet/download",
            "query_string": b"",
            "headers": [(b"host", host.encode())],
        }
    )


class TestBuildScriptPreview:
    """Cover the preview-response builder and its decode-error propagation."""

    @pytest.mark.asyncio
    async def test_maps_get_preview_to_response(self, mocker) -> None:
        """Map ``get_preview`` output to a response with a MIME-derived language."""
        snippet = _snippet()
        preview = FilePreview(
            preamble="#!/bin/sh\n",
            frontmatter="",
            content="echo hi\n",
            is_truncated=True,
        )
        mocker.patch.object(Snippet, "get_preview", AsyncMock(return_value=preview))

        result = await build_script_preview(snippet)

        assert result.content == preview.full_content
        assert result.is_truncated is True
        assert result.language == mime_type_to_highlighter_language(
            guess_mime_type(snippet.path)
        )

    @pytest.mark.asyncio
    async def test_propagates_unicode_decode_error(self, mocker) -> None:
        """Propagate ``UnicodeDecodeError`` rather than building a 422 itself."""
        snippet = _snippet()
        mocker.patch.object(
            Snippet,
            "get_preview",
            AsyncMock(
                side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
            ),
        )

        with pytest.raises(UnicodeDecodeError):
            await build_script_preview(snippet)


class TestBuildExecutionMeta:
    """Cover the sudo-resolving execution-meta assembly."""

    def test_no_sudo_keeps_interpreter(self) -> None:
        """Keep the interpreter untouched when neither script nor args opt into sudo."""
        snippet = _snippet()
        meta = build_execution_meta(
            snippet,
            _execution_args(snippet),
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="script.sh",
        )

        assert meta.interpreter == "bash"
        assert meta.target == "host1"
        assert meta.md5_checksum == _MD5

    def test_sudo_when_script_sudo_always(self) -> None:
        """Prepend ``sudo`` when the script's sudo option is ``ALWAYS``."""
        snippet = _snippet(sudo="always")
        meta = build_execution_meta(
            snippet,
            _execution_args(snippet),
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="script.sh",
        )

        assert meta.interpreter == "sudo bash"

    def test_sudo_when_args_field_truthy(self) -> None:
        """Prepend ``sudo`` when the validated args carry a truthy sudo field."""
        snippet = _snippet(sudo="optional")
        args = _execution_args(snippet, {SUDO_INPUT_NAME: True})
        meta = build_execution_meta(
            snippet,
            args,
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="script.sh",
        )

        assert meta.interpreter == "sudo bash"

    def test_sudo_default_applies_when_args_lack_field(self) -> None:
        """Use ``sudo_default`` when the args model has no sudo field."""
        snippet = _snippet()
        meta = build_execution_meta(
            snippet,
            _execution_args(snippet),
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="script.sh",
            sudo_default=True,
        )

        assert meta.interpreter == "sudo bash"

    def test_snippet_filename_is_caller_keyed(self) -> None:
        """Record the caller-supplied ``snippet_filename`` under its serialized alias."""
        snippet = _snippet()
        meta = build_execution_meta(
            snippet,
            _execution_args(snippet),
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="dipper/7/x.sh",
        )

        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert dumped["_snippet_filename"] == "dipper/7/x.sh"

    @staticmethod
    def _dumped_meta(parameters: list[dict[str, object]], **values: object):
        """Return the meta posted for a run of a snippet declaring ``parameters``.

        :param parameters: The snippet's frontmatter parameter definitions.
        :param values: The frontmatter-named parameter values of the run.
        :return: The meta as ``post_task_execution`` dumps it.
        """
        snippet = _snippet()
        snippet.meta = {**snippet.meta, "parameters": parameters}
        meta = build_execution_meta(
            snippet,
            _execution_args(snippet, values),
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="script.sh",
        )
        return meta.model_dump(by_alias=True, exclude_none=True)

    def test_records_a_masked_copy_beside_the_raw_arguments(self) -> None:
        """Send the executor the real credential and history a masked rendering."""
        dumped = self._dumped_meta(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "password", "type": "str"},
            ],
            dest="/tmp",
            password="hunter2",
        )

        assert dumped["args"] == "--dest /tmp --password hunter2"
        assert dumped[MASKED_ARGS_META_KEY] == "--dest /tmp --password ***"

    def test_masked_copy_equals_the_arguments_when_none_is_a_credential(self) -> None:
        """Keep non-credential arguments readable in history."""
        dumped = self._dumped_meta(
            [{"name": "dest", "type": "str", "required": True}], dest="/tmp"
        )

        assert dumped[MASKED_ARGS_META_KEY] == dumped["args"] == "--dest /tmp"

    def test_a_run_without_arguments_records_no_masked_copy(self) -> None:
        """Omit the masked copy, like the arguments, when the run renders none."""
        dumped = self._dumped_meta([])

        assert dumped["target"] == "host1"
        assert "args" not in dumped
        assert MASKED_ARGS_META_KEY not in dumped


class TestBuildArtifactDownloadUrl:
    """Cover the signed artifact-URL builder across request-backed and request-less paths."""

    @staticmethod
    def _decode(url: str) -> dict:
        token = url.rsplit("/artifacts/download/", 1)[1]
        return crypto_timestamp_serializer.loads(token, salt=ARTIFACT_DOWNLOAD_SALT)

    def test_request_backed_snippet_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Build a snippet-type token URL against the configured base URL."""
        monkeypatch.setattr(
            snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
        )
        url = build_artifact_download_url(
            _make_request(),
            artifact_type=ARTIFACT_TYPE_SNIPPET,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert url.startswith("https://extensions.example/artifacts/download/")
        assert self._decode(url) == {"type": "snippet", "filename": "x.sh", "md5": _MD5}

    def test_request_backed_dipper_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Build a dipper-type token URL, differing from the snippet URL only by type."""
        monkeypatch.setattr(
            snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
        )
        url = build_artifact_download_url(
            _make_request(),
            artifact_type=ARTIFACT_TYPE_DIPPER,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert self._decode(url)["type"] == "dipper"

    def test_request_backed_falls_back_to_request_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Derive the base URL from the request host when no base URL is configured."""
        monkeypatch.setattr(snippets_settings, "SNIPPETS_BASE_URL", None)
        monkeypatch.setattr("app.core.config.settings.BASE_URL", None)
        url = build_artifact_download_url(
            _make_request(host="host.internal"),
            artifact_type=ARTIFACT_TYPE_SNIPPET,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert url.startswith("https://host.internal/artifacts/download/")

    def test_request_less_uses_configured_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Use the configured base URL on the request-less path."""
        monkeypatch.setattr(
            snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
        )
        url = build_artifact_download_url(
            None,
            artifact_type=ARTIFACT_TYPE_SNIPPET,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert url.startswith("https://extensions.example/artifacts/download/")

    def test_request_less_without_base_url_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raise 400 with the exact message when no base URL is set request-less."""
        monkeypatch.setattr(snippets_settings, "SNIPPETS_BASE_URL", None)
        monkeypatch.setattr("app.core.config.settings.BASE_URL", None)

        with pytest.raises(HTTPBadRequestException) as exc_info:
            build_artifact_download_url(
                None,
                artifact_type=ARTIFACT_TYPE_SNIPPET,
                filename="x.sh",
                md5_digest=_MD5,
            )

        assert exc_info.value.detail == (
            "Snippet execution requires SNIPPETS_BASE_URL or BASE_URL to be set."
        )

    def test_request_backed_carries_the_mount_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the emitted URL inside the prefix the request arrived under.

        The request-backed path reads the prefix off the ASGI scope, not off
        ``ROOT_PATH``, so no setting is needed to exercise it.
        """
        monkeypatch.setattr(snippets_settings, "SNIPPETS_BASE_URL", None)
        monkeypatch.setattr("app.core.config.settings.BASE_URL", None)

        url = build_artifact_download_url(
            _make_request(host="host.internal", root_path="/extensions"),
            artifact_type=ARTIFACT_TYPE_SNIPPET,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert url.startswith("https://host.internal/extensions/artifacts/download/")

    def test_request_less_preserves_a_prefixed_configured_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Join onto the configured base's path rather than replacing it."""
        monkeypatch.setattr(snippets_settings, "SNIPPETS_BASE_URL", None)
        monkeypatch.setattr(
            "app.core.config.settings.BASE_URL", URL("https://pmm:8443/extensions")
        )
        monkeypatch.setattr(extensions_settings, "ROOT_PATH", "/extensions")

        url = build_artifact_download_url(
            None,
            artifact_type=ARTIFACT_TYPE_SNIPPET,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert url.startswith("https://pmm:8443/extensions/artifacts/download/")

    def test_configured_base_trailing_slash_does_not_double(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absorb a trailing slash on the configured base instead of doubling it."""
        monkeypatch.setattr(
            snippets_settings,
            "SNIPPETS_BASE_URL",
            URL("https://extensions.example/extensions/"),
        )
        monkeypatch.setattr(extensions_settings, "ROOT_PATH", "/extensions")

        url = build_artifact_download_url(
            None,
            artifact_type=ARTIFACT_TYPE_SNIPPET,
            filename="x.sh",
            md5_digest=_MD5,
        )

        assert url.startswith(
            "https://extensions.example/extensions/artifacts/download/"
        )

    def test_warns_when_a_hot_override_drops_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warn at the point of use, and still emit, when a live base loses the prefix."""
        monkeypatch.setattr(
            snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
        )
        monkeypatch.setattr(extensions_settings, "ROOT_PATH", "/extensions")

        with caplog.at_level(logging.WARNING):
            url = build_artifact_download_url(
                None,
                artifact_type=ARTIFACT_TYPE_SNIPPET,
                filename="x.sh",
                md5_digest=_MD5,
            )

        assert url.startswith("https://extensions.example/artifacts/download/")
        assert "SNIPPETS_BASE_URL" in caplog.text

    def test_stays_silent_when_no_prefix_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Leave the unprefixed deployment unwarned, which is the regression contract."""
        monkeypatch.setattr(
            snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
        )
        monkeypatch.setattr(extensions_settings, "ROOT_PATH", "")

        with caplog.at_level(logging.WARNING):
            build_artifact_download_url(
                None,
                artifact_type=ARTIFACT_TYPE_SNIPPET,
                filename="x.sh",
                md5_digest=_MD5,
            )

        assert "SNIPPETS_BASE_URL" not in caplog.text


class TestPostTaskExecution:
    """Cover the execute-POST tail and its soft id extraction."""

    @staticmethod
    def _meta() -> SnippetExecutionMeta:
        return SnippetExecutionMeta(
            target="host1",
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="x.sh",
            md5_checksum=_MD5,
        )

    @pytest.mark.asyncio
    async def test_returns_id_and_posts_meta_envelope(self) -> None:
        """Send the meta envelope and return the created task id."""
        expected_id = 42
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = {"id": expected_id, "status": "queued"}
        meta = self._meta()

        task_id = await post_task_execution(tasks_api, "run_snippet", meta)

        assert task_id == expected_id
        tasks_api.post.assert_awaited_once_with(
            "/execute/run_snippet",
            json={"meta": meta.model_dump(by_alias=True, exclude_none=True)},
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_id_absent(self) -> None:
        """Return ``None`` when the upstream dict carries no id."""
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = {}

        assert await post_task_execution(tasks_api, "run_snippet", self._meta()) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_response_not_dict(self) -> None:
        """Return ``None`` when the upstream response is not a dict."""
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = []

        assert await post_task_execution(tasks_api, "run_snippet", self._meta()) is None


class _StubArgs(BaseModel):
    """Validate a single integer argument, standing in for a script's exec model."""

    minutes: int = 1


@dataclass(frozen=True, slots=True)
class _StubScript:
    """Implement the framework's ``ScriptProtocol`` without a snippet row."""

    filename: str = "script.sh"

    @property
    def execution_task_name(self) -> str:
        """Return the task name the stub script dispatches under."""
        return "run_snippet"

    def get_execution_model(self) -> type[_StubArgs]:
        """Return the stub execution model the framework validates args against."""
        return _StubArgs


class TestExecuteScript:
    """Cover the shared validate/coerce/build-meta/dispatch execute sequence."""

    @staticmethod
    def _source(seen: list[ScriptExecuteWrite]) -> ScriptSource:
        """Return a script source recording the body its meta hook receives."""

        def _build_execution_meta(
            script: _StubScript, body: ScriptExecuteWrite
        ) -> SnippetExecutionMeta:
            seen.append(body)
            return SnippetExecutionMeta(
                target=body.executor_host,
                interpreter="bash",
                snippet_source="https://x/y",
                snippet_filename=script.filename,
                md5_checksum=_MD5,
            )

        return ScriptSource(
            script_dir=Path("/nonexistent"),
            load_script=AsyncMock(),
            list_scripts=AsyncMock(),
            build_form_schema=Mock(),
            build_execution_meta=_build_execution_meta,
            list_response=Mock(),
        )

    @pytest.mark.asyncio
    async def test_dispatches_and_returns_created_task(self) -> None:
        """Send the assembled meta and return the created task id and script name."""
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = {"id": _CREATED_TASK_ID}
        script = _StubScript()

        result = await execute_script(
            self._source([]),
            script,
            ScriptExecuteWrite(executor_host="host1", args={"minutes": 5}),
            tasks_api,
        )

        assert result.task_id == _CREATED_TASK_ID
        assert result.task_name == script.execution_task_name
        assert result.snippet_filename == script.filename

    @pytest.mark.asyncio
    async def test_default_dispatches_under_the_scripts_own_task(self) -> None:
        """Leave a caller that passes no override dispatching under the script's task.

        This is what keeps an app-owned proxy — and therefore that app's
        ``run_result_recorder`` — off every run another feature dispatches.
        """
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = {"id": _CREATED_TASK_ID}
        script = _StubScript()

        await execute_script(
            self._source([]),
            script,
            ScriptExecuteWrite(executor_host="host1", args={"minutes": 5}),
            tasks_api,
        )

        assert (
            tasks_api.post.await_args.args[0]
            == f"/execute/{script.execution_task_name}"
        )

    @pytest.mark.asyncio
    async def test_execution_task_name_override_redirects_the_dispatch(self) -> None:
        """Dispatch under an explicit override, reporting it as the task name.

        One dispatch implementation serves both callers: an app wrapping the
        interpreter root in its own proxy passes the proxy here rather than
        re-implementing the validate/build-meta/post sequence.
        """
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = {"id": _CREATED_TASK_ID}
        script = _StubScript()

        result = await execute_script(
            self._source([]),
            script,
            ScriptExecuteWrite(executor_host="host1", args={"minutes": 5}),
            tasks_api,
            execution_task_name="app__proxy-task",
        )

        assert tasks_api.post.await_args.args[0] == "/execute/app__proxy-task"
        assert result.task_name == "app__proxy-task"
        assert result.snippet_filename == script.filename

    @pytest.mark.asyncio
    async def test_meta_hook_receives_the_coerced_args(self) -> None:
        """Send the meta hook the execution model's coerced dump, not the raw args."""
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.post.return_value = {"id": 1}
        seen = []

        await execute_script(
            self._source(seen),
            _StubScript(),
            ScriptExecuteWrite(executor_host="host1", args={"minutes": "5"}),
            tasks_api,
        )

        assert seen[0].args == {"minutes": 5}

    @pytest.mark.asyncio
    async def test_invalid_args_raise_422(self) -> None:
        """Reject args the script's execution model cannot validate with a 422."""
        tasks_api = AsyncMock(spec=RemoteAPI)

        with pytest.raises(HTTPUnprocessableEntityException):
            await execute_script(
                self._source([]),
                _StubScript(),
                ScriptExecuteWrite(executor_host="host1", args={"minutes": "abc"}),
                tasks_api,
            )

        tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
class TestPostTaskExecutionPathGuard:
    """Test that a configured execution task name cannot reshape the POST."""

    @pytest.mark.parametrize("execution_task_name", PATH_UNSAFE_TASKS)
    async def test_refuses_an_unsafe_execution_task_name(
        self, execution_task_name: str
    ) -> None:
        """Refuse an unsafe name and issue no POST."""
        tasks_api = AsyncMock(spec=RemoteAPI)
        meta = SnippetExecutionMeta(
            target="host1",
            interpreter="bash",
            snippet_source="https://x/y",
            snippet_filename="x.sh",
            md5_checksum=_MD5,
        )

        with pytest.raises(HTTPUnprocessableEntityException):
            await post_task_execution(tasks_api, execution_task_name, meta)

        tasks_api.post.assert_not_awaited()
