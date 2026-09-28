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

"""Cover the disk-backed ``ScriptSource`` factory against real script files.

The factory bridges a disk-backed :class:`~app.extensions.snippets.models.snippet.BaseSnippet`
subclass onto the framework ``ScriptSource`` seam. The suite writes real ``.sh`` /
``.py`` files with ``# ---`` frontmatter into a per-test directory, points a
throwaway ``BaseSnippet`` subclass' ``BASE_DIR`` at it, and exercises the public
source hooks (``load_script`` / ``list_scripts`` / ``build_form_schema`` /
``build_execution_meta``) — mirroring the snippets ``test_script_source.py`` but
without a database, since a scaffolded script app loads its catalogue from disk.
"""

from pathlib import Path

import pytest
from starlette.datastructures import URL

from app.core.db.in_memory_list_query import InMemoryListQuery
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPNotFoundException,
    HTTPUnprocessableEntityException,
)
from app.core.pagination import Pagination
from app.extensions.apps.field_names import (
    EXECUTOR_HOST_FIELD_NAME,
    RESERVED_EXECUTION_FIELD_NAMES,
    SUDO_FIELD_NAME,
)
from app.extensions.apps.framework.schema import (
    BoolField,
    ChoiceField,
    HostField,
    IntegerField,
    StringField,
)
from app.extensions.apps.framework.script_source import ScriptExecuteWrite, ScriptSource
from app.extensions.apps.shared.disk_script_source import (
    build_disk_script_source,
    DiskScriptListRow,
)
from app.extensions.snippets.config import snippets_settings
from app.extensions.snippets.models.snippet import BaseSnippet, SnippetExecutionMeta
from tests.app.extensions.form_schema_utils import (
    assert_only_synthesized_fields,
    form_field_names,
    form_field_types,
    form_fields_by_name,
)

pytestmark = pytest.mark.asyncio

#: The fields this app synthesizes for a sudo-optional script, and their widgets.
_SYNTHESIZED_FIELD_TYPES = {
    EXECUTOR_HOST_FIELD_NAME: HostField,
    SUDO_FIELD_NAME: BoolField,
}


_SHELL_NO_PARAMS = """#!/usr/bin/env bash
# ---
# title: Bare
# ---
echo "hi"
"""

_SHELL_MESSAGE_PARAM = """#!/usr/bin/env bash
# ---
# title: Greeter
# parameters:
#   - name: message
#     label: Message
#     positional: true
# ---
echo "${1:-}"
"""

_SHELL_TYPED_PARAMS = """#!/usr/bin/env bash
# ---
# title: Typed
# parameters:
#   - name: mode
#     label: Mode
#     choices:
#       - value: fast
#       - value: slow
#   - name: retries
#     label: Retries
#     type: int
#   - name: verbose
#     label: Verbose
#     type: bool
# ---
echo "run"
"""

_SHELL_OPTIONAL_SUDO = """#!/usr/bin/env bash
# ---
# title: Sudoable
# sudo: optional
# ---
echo "hi"
"""

_SHELL_REQUIRES_GATE = """#!/usr/bin/env bash
# ---
# title: Gated
# parameters:
#   - name: mode
#     label: Mode
#   - name: reason
#     label: Reason
#     requires_when:
#       parameter: mode
#       equals: write
# ---
echo "run"
"""

_SHELL_INVALID_PARAM = """#!/usr/bin/env bash
# ---
# title: Broken
# parameters:
#   - name: mode
#     label: Mode
#     visible_when:
#       parameter: nonexistent
#       equals: x
# ---
echo "run"
"""

_PYTHON_NO_REQS = """#!/usr/bin/env python3
# ---
# title: Plain Python
# ---
print("hi")
"""

_PYTHON_WITH_REQS = """#!/usr/bin/env python3
# ---
# title: Python With Reqs
# requires_packages:
#   - requests
# ---
print("hi")
"""


def _write_script(script_dir: Path, filename: str, content: str) -> str:
    """Write ``content`` to ``filename`` under ``script_dir`` and return the filename."""
    (script_dir / filename).write_text(content)
    return filename


def _framework_processed_body(
    source: ScriptSource, script: object, body: ScriptExecuteWrite
) -> ScriptExecuteWrite:
    """Reproduce the seam's pre-processing (validate ``args`` -> coerced dump).

    ``derive_script_routes`` validates ``body.args`` against the script's args-only
    model and replaces them with the coerced ``model_dump()`` before the
    ``build_execution_meta`` hook runs, so a direct hook call must reproduce it.
    """
    validated = script.get_execution_model().model_validate(body.args)
    return body.model_copy(update={"args": validated.model_dump()})


def _shell_with_parameter(name: str) -> str:
    """Return an optional-sudo shell script declaring exactly one parameter.

    :param name: The frontmatter parameter name the script declares.
    :return: The script body, frontmatter included.
    """
    return (
        "#!/usr/bin/env bash\n"
        "# ---\n"
        "# title: Reserved\n"
        "# sudo: optional\n"
        "# parameters:\n"
        f"#   - name: {name}\n"
        "#     label: Reserved\n"
        "# ---\n"
        'echo "run"\n'
    )


@pytest.fixture
def script_dir(tmp_path: Path) -> Path:
    """Return an empty per-test script directory."""
    directory = tmp_path / "snippets"
    directory.mkdir()
    return directory


@pytest.fixture
def source(script_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ScriptSource:
    """Build a disk-backed source over a throwaway ``BaseSnippet`` subclass."""
    monkeypatch.setattr(
        snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
    )

    class _KitScript(BaseSnippet):
        BASE_DIR = script_dir

    return build_disk_script_source(
        script_dir=script_dir,
        script_cls=_KitScript,
        artifact_type="testkit",
        name="testkit",
        display_name="Test Kit",
    )


class TestLoadAndList:
    """Cover disk resolution and listing."""

    async def test_missing_script_raises_404(self, source: ScriptSource) -> None:
        """Raise 404 when the requested script file is absent from the directory."""
        with pytest.raises(HTTPNotFoundException):
            await source.load_script("nope.sh")

    async def test_load_script_returns_usable_adapter(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Resolve a written script to an adapter exposing filename and task name."""
        _write_script(script_dir, "ok.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("ok.sh")
        assert script.filename == "ok.sh"
        assert script.execution_task_name == "exec-artifact"

    async def test_list_scripts_returns_every_file(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Return one list row per discovered file, projected to the row model."""
        _write_script(script_dir, "a.sh", _SHELL_NO_PARAMS)
        _write_script(script_dir, "b.sh", _SHELL_NO_PARAMS)
        scripts, total = await source.list_scripts(None, None)
        rows = [source.list_response(script) for script in scripts]
        assert all(isinstance(row, DiskScriptListRow) for row in rows)
        assert [row.filename for row in rows] == ["a.sh", "b.sh"]
        assert total == len(scripts)

    async def test_unqueried_list_returns_whole_set_past_a_default_page(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Return every script when the route derives neither query nor pagination.

        The framework calls this shape for a non-paginated list route, so it must not
        silently truncate at a default page size.
        """
        count = 60
        for index in range(count):
            _write_script(script_dir, f"s{index:02d}.sh", _SHELL_NO_PARAMS)

        scripts, total = await source.list_scripts(None, None)

        assert len(scripts) == count
        assert total == count

    async def test_unqueried_paginated_list_slices_in_spec_default_order(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Slice in the spec's default order when the route derives no query.

        There is no separate unsorted path any more: a missing query resolves to the
        spec default, so the ordering is the same one a queried call would produce.
        """
        names = ("c.sh", "a.sh", "b.sh")
        for name in names:
            _write_script(script_dir, name, _SHELL_NO_PARAMS)

        scripts, total = await source.list_scripts(None, Pagination(offset=0, limit=2))

        assert [script.filename for script in scripts] == ["a.sh", "b.sh"]
        assert total == len(names)

    async def test_in_memory_list_query_flag_is_set(self, source: ScriptSource) -> None:
        """Flag the disk source as in-memory so the framework builds that dep."""
        assert source.in_memory_list_query is True

    async def test_non_spec_list_query_spec_raises_at_wiring_time(
        self, script_dir: Path
    ) -> None:
        """Reject a bad spec when the source is built, not on the first list request."""

        class _KitScript(BaseSnippet):
            BASE_DIR = script_dir

        with pytest.raises(TypeError, match="must be a ListQuerySpec"):
            build_disk_script_source(
                script_dir=script_dir,
                script_cls=_KitScript,
                artifact_type="testkit",
                name="testkit",
                display_name="Test Kit",
                list_query_spec=None,
            )


class TestListQueryApplied:
    """Exercise the disk source's in-memory sort/search/paginate over real files.

    Runs against the same ``source`` fixture every other class uses: the builder now
    defaults to the shared spec, so there is no spec-less disk source to contrast with.
    """

    async def _seed(self, script_dir: Path, *names: str) -> None:
        for name in names:
            _write_script(script_dir, name, _SHELL_NO_PARAMS)

    async def test_sort_descending_by_filename(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Order by the filename attribute descending via the in-memory applier."""
        names = ("a.sh", "b.sh", "c.sh")
        await self._seed(script_dir, *names)
        query = InMemoryListQuery(sort_key="filename", descending=True, search=None)

        rows, total = await source.list_scripts(query, Pagination())

        assert [row.filename for row in rows] == ["c.sh", "b.sh", "a.sh"]
        assert total == len(names)

    async def test_search_narrows_rows_and_total(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Narrow the rows and the total by a case-insensitive filename substring."""
        await self._seed(script_dir, "mysql-dump.sh", "pg-vacuum.sh")
        query = InMemoryListQuery(sort_key="filename", descending=False, search="MYSQL")

        rows, total = await source.list_scripts(query, Pagination())

        assert [row.filename for row in rows] == ["mysql-dump.sh"]
        assert total == 1

    async def test_pagination_slices_with_full_total(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Return the pagination window while reporting the full filtered total."""
        names = ("a.sh", "b.sh", "c.sh", "d.sh")
        await self._seed(script_dir, *names)
        query = InMemoryListQuery(sort_key="filename", descending=False, search=None)

        rows, total = await source.list_scripts(query, Pagination(offset=1, limit=2))

        assert [row.filename for row in rows] == ["b.sh", "c.sh"]
        assert total == len(names)


class TestTaskAndInterpreterDerivation:
    """Cover runtime task/interpreter derivation from the script's extension/reqs."""

    async def test_shell_script_dispatches_exec_artifact_under_bash(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Derive ``exec-artifact`` + ``bash`` for a shell script with no requirements."""
        _write_script(script_dir, "ok.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("ok.sh")
        assert script.execution_task_name == "exec-artifact"
        body = ScriptExecuteWrite(executor_host="exec-1", args={})
        meta = source.build_execution_meta(
            script, _framework_processed_body(source, script, body)
        )
        assert meta.interpreter == "bash"

    async def test_plain_python_dispatches_exec_artifact_under_python3(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Derive ``exec-artifact`` + ``python3`` for a Python script with no reqs."""
        _write_script(script_dir, "ok.py", _PYTHON_NO_REQS)
        script = await source.load_script("ok.py")
        assert script.execution_task_name == "exec-artifact"
        body = ScriptExecuteWrite(executor_host="exec-1", args={})
        meta = source.build_execution_meta(
            script, _framework_processed_body(source, script, body)
        )
        assert meta.interpreter == "python3"

    async def test_python_with_requirements_dispatches_python_artifact(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Derive ``exec-python-artifact`` and carry the requirements into the meta."""
        _write_script(script_dir, "reqs.py", _PYTHON_WITH_REQS)
        script = await source.load_script("reqs.py")
        assert script.execution_task_name == "exec-python-artifact"
        body = ScriptExecuteWrite(executor_host="exec-1", args={})
        meta = source.build_execution_meta(
            script, _framework_processed_body(source, script, body)
        )
        assert meta.requirements == "requests"


class TestFormSchema:
    """Cover frontmatter-driven form synthesis reusing ``field_for``."""

    async def test_typed_parameters_reuse_field_for_field_types(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Map choice/int/bool parameters onto their framework field counterparts."""
        _write_script(script_dir, "typed.sh", _SHELL_TYPED_PARAMS)
        script = await source.load_script("typed.sh")
        fields = form_fields_by_name(source.build_form_schema(script))
        assert isinstance(fields["mode"], ChoiceField)
        assert isinstance(fields["retries"], IntegerField)
        assert isinstance(fields["verbose"], BoolField)

    async def test_execution_section_carries_host_and_optional_sudo(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Render the executor-host field plus a sudo toggle for an optional-sudo script."""
        _write_script(script_dir, "sudo.sh", _SHELL_OPTIONAL_SUDO)
        script = await source.load_script("sudo.sh")
        fields = form_fields_by_name(source.build_form_schema(script))
        assert isinstance(fields["executor_host"], HostField)
        assert isinstance(fields["sudo"], BoolField)

    async def test_script_without_parameters_has_only_execution_fields(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Render only the Execution section when the script declares no parameters."""
        _write_script(script_dir, "bare.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("bare.sh")
        fields = form_fields_by_name(source.build_form_schema(script))
        assert set(fields) == {"executor_host"}
        assert isinstance(fields["executor_host"], HostField)

    async def test_string_parameter_maps_to_string_field(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Map a plain string parameter onto a ``StringField``."""
        _write_script(script_dir, "msg.sh", _SHELL_MESSAGE_PARAM)
        script = await source.load_script("msg.sh")
        fields = form_fields_by_name(source.build_form_schema(script))
        assert isinstance(fields["message"], StringField)

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    async def test_reserved_named_parameter_is_dropped_not_raised(
        self, source: ScriptSource, script_dir: Path, reserved_name: str
    ) -> None:
        """Drop a reserved-name parameter instead of failing the schema build.

        Reservation is unconditional, so this holds for the two names this app
        never synthesizes (``script_preview``, ``extra_args``) as well as for
        the two it does: an author cannot tell from their frontmatter which app
        will render it.
        """
        _write_script(script_dir, "reserved.sh", _shell_with_parameter(reserved_name))
        script = await source.load_script("reserved.sh")

        schema = source.build_form_schema(script)

        assert_only_synthesized_fields(schema, _SYNTHESIZED_FIELD_TYPES)

    async def test_every_synthesized_field_name_is_reserved(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Keep every field this builder synthesizes covered by the reserved set."""
        _write_script(script_dir, "sudo.sh", _SHELL_OPTIONAL_SUDO)
        script = await source.load_script("sudo.sh")

        schema = source.build_form_schema(script)

        assert form_field_types(schema) == _SYNTHESIZED_FIELD_TYPES
        assert set(form_field_names(schema)) <= RESERVED_EXECUTION_FIELD_NAMES


class TestExecuteMeta:
    """Cover args validation, meta assembly, sudo, and gate enforcement."""

    async def test_args_only_validation_succeeds_without_hostname(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Validate an args body that omits the required ``-hostname-`` field."""
        _write_script(script_dir, "ok.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("ok.sh")
        validated = script.get_execution_model().model_validate({})
        assert validated.executor_host is None

    async def test_meta_assembly_signs_source_and_targets_host(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Assemble a meta with a signed source URL, 32-char checksum, and host target."""
        _write_script(script_dir, "ok.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("ok.sh")
        body = ScriptExecuteWrite(executor_host="exec-1", args={})
        meta = source.build_execution_meta(
            script, _framework_processed_body(source, script, body)
        )
        assert isinstance(meta, SnippetExecutionMeta)
        assert meta.target == "exec-1"
        assert "/artifacts/download/" in meta.snippet_source
        assert meta.md5_checksum == script.snippet.md5_digest

    @pytest.mark.parametrize("requested_sudo", [True, False])
    async def test_optional_sudo_toggle_is_honored(
        self, source: ScriptSource, script_dir: Path, *, requested_sudo: bool
    ) -> None:
        """Apply the ``sudo`` interpreter prefix only when the caller opts in."""
        _write_script(script_dir, "sudo.sh", _SHELL_OPTIONAL_SUDO)
        script = await source.load_script("sudo.sh")
        body = ScriptExecuteWrite(executor_host="exec-1", sudo=requested_sudo, args={})
        meta = source.build_execution_meta(
            script, _framework_processed_body(source, script, body)
        )
        assert meta.interpreter.startswith("sudo ") == requested_sudo

    async def test_requires_gate_violation_raises_422(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Reject a submission that violates a ``requires_when`` gate with 422."""
        _write_script(script_dir, "gated.sh", _SHELL_REQUIRES_GATE)
        script = await source.load_script("gated.sh")
        body = ScriptExecuteWrite(executor_host="exec-1", args={"mode": "write"})
        with pytest.raises(HTTPUnprocessableEntityException):
            source.build_execution_meta(
                script, _framework_processed_body(source, script, body)
            )

    async def test_invalid_frontmatter_parameters_raise_400(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Reject a script whose frontmatter declares an invalid parameter."""
        _write_script(script_dir, "broken.sh", _SHELL_INVALID_PARAM)
        script = await source.load_script("broken.sh")
        body = ScriptExecuteWrite(executor_host="exec-1", args={})
        with pytest.raises(HTTPBadRequestException):
            source.build_execution_meta(
                script, _framework_processed_body(source, script, body)
            )

    async def test_missing_base_url_raises_400(
        self, source: ScriptSource, script_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raise 400 when neither ``SNIPPETS_BASE_URL`` nor ``BASE_URL`` is configured."""
        monkeypatch.setattr(snippets_settings, "SNIPPETS_BASE_URL", None)
        monkeypatch.setattr(
            "app.extensions.apps.framework.script_helpers.settings.BASE_URL", None
        )
        _write_script(script_dir, "ok.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("ok.sh")
        body = ScriptExecuteWrite(executor_host="exec-1", args={})
        with pytest.raises(HTTPBadRequestException):
            source.build_execution_meta(
                script, _framework_processed_body(source, script, body)
            )


class TestRecordDisplayNames:
    """Cover the record nouns the disk-backed source puts on its two schemas."""

    async def test_per_script_schema_names_the_record_a_run(
        self, source: ScriptSource, script_dir: Path
    ) -> None:
        """Name the per-script record a run, never the script's own title."""
        _write_script(script_dir, "ok.sh", _SHELL_NO_PARAMS)
        script = await source.load_script("ok.sh")

        schema = source.build_form_schema(script)

        assert schema.item_display_name == "run"
        assert schema.item_display_name_plural == "runs"

    async def test_static_schema_carries_the_passed_record_names(
        self, script_dir: Path
    ) -> None:
        """Stamp the app-level record nouns the builder was given onto ``static_schema``."""

        class _KitScript(BaseSnippet):
            BASE_DIR = script_dir

        source = build_disk_script_source(
            script_dir=script_dir,
            script_cls=_KitScript,
            artifact_type="testkit",
            name="testkit",
            display_name="Test Kit",
            item_display_name="kit script",
            item_display_name_plural="kit scripts",
        )

        assert source.static_schema.item_display_name == "kit script"
        assert source.static_schema.item_display_name_plural == "kit scripts"

    async def test_static_schema_record_names_default_when_omitted(
        self, source: ScriptSource
    ) -> None:
        """Leave both app-level nouns as ``display_name`` when the caller passes neither."""
        assert source.static_schema.item_display_name == "Test Kit"
        assert source.static_schema.item_display_name_plural == "Test Kit"
