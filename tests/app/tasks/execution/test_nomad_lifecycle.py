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

"""Tests for the NomadLifecycle holder and executor-resolution helpers."""

from types import SimpleNamespace

import pytest
from aiohttp import encode_basic_auth
from fastapi import FastAPI
from pydantic import ValidationError

from app.tasks.config import tasks_settings
from app.tasks.deps import (
    get_executor,
    get_request_executor,
    resolve_request_executor,
)
from app.tasks.execution.executors.celery.models import CeleryExecutor
from app.tasks.execution.executors.nomad import NomadExecutor
from app.tasks.execution.nomad_lifecycle import (
    NomadLifecycle,
    normalize_nomad_config_value,
)
from app.tasks.models import TaskBackendEnum

_NOMAD_A = {"endpoint": "https://nomad-a.example.org"}
_NOMAD_B = {"endpoint": "https://nomad-b.example.org"}
_NOMAD_WITH_CREDS = {
    "endpoint": "http://nomad-user:nomad-secret@nomad.internal:4646",
}
_NOMAD_WITH_KEY = {**_NOMAD_A, "api_key": "glsa_realtoken"}
_NOMAD_WITH_ROTATED_KEY = {**_NOMAD_A, "api_key": "glsa_rotated"}
_STREAM_LIMIT = 3


def _override_nomad(config: dict[str, object]) -> None:
    """Publish a merged ``NomadExecutor`` NOMAD override on the tasks proxy snapshot."""
    executor = NomadExecutor.model_validate(config)
    tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"NOMAD": executor}
    )


def test_normalize_passes_through_executor() -> None:
    """A live ``NomadExecutor`` is returned unchanged."""
    executor = NomadExecutor.model_validate(_NOMAD_A)
    assert normalize_nomad_config_value(executor) is executor


def test_normalize_reconstructs_executor_from_mapping() -> None:
    """A fingerprint mapping is reconstructed into a ``NomadExecutor``."""
    result = normalize_nomad_config_value(_NOMAD_A)
    assert isinstance(result, NomadExecutor)
    assert str(result.endpoint).startswith("https://nomad-a.example.org")


def test_normalize_reconstructs_executor_with_embedded_credentials() -> None:
    """A fingerprint with embedded URL credentials rebuilds a usable executor."""
    result = normalize_nomad_config_value(_NOMAD_WITH_CREDS)
    assert "nomad-secret" in str(result.endpoint)


def test_normalize_raises_on_invalid_mapping() -> None:
    """An invalid fingerprint mapping raises noisily instead of passing through."""
    with pytest.raises(ValidationError):
        normalize_nomad_config_value({"endpoint": "not-a-url"})


def test_normalize_raises_on_unsupported_type() -> None:
    """A value that is neither an executor nor a mapping raises ``TypeError``."""
    with pytest.raises(TypeError):
        normalize_nomad_config_value(123)


def test_current_raises_before_start() -> None:
    """Reading ``current`` before the holder is entered raises ``RuntimeError``."""
    holder = NomadLifecycle(FastAPI())
    with pytest.raises(RuntimeError, match="not started"):
        _ = holder.current


@pytest.mark.asyncio
async def test_aenter_enters_executor_with_embedded_credentials() -> None:
    """``__aenter__`` preserves embedded URL credentials from the override fingerprint."""
    app = FastAPI()
    _override_nomad(_NOMAD_WITH_CREDS)
    async with NomadLifecycle(app) as holder:
        assert "nomad-secret" in str(holder.current.endpoint)


@pytest.mark.asyncio
async def test_aenter_enters_effective_config_and_publishes_holder() -> None:
    """``__aenter__`` enters the override-config executor and exposes the holder."""
    app = FastAPI()
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(app) as holder:
        assert app.state.nomad_lifecycle is holder
        assert str(holder.current.endpoint).startswith("https://nomad-a.example.org")
        assert holder.current._session is not None
    assert holder._current is None


@pytest.mark.asyncio
async def test_aenter_leaves_the_settings_executor_unentered() -> None:
    """``__aenter__`` enters a private executor, never the shared settings value."""
    _override_nomad(_NOMAD_A)
    settings_executor = tasks_settings.NOMAD
    async with NomadLifecycle(FastAPI()) as holder:
        assert holder.current is not settings_executor
        assert settings_executor._session is None


@pytest.mark.asyncio
async def test_reconcile_is_noop_when_config_unchanged() -> None:
    """``reconcile`` keeps the same executor when the config is unchanged."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        first = holder.current
        await holder.reconcile()
        assert holder.current is first


@pytest.mark.asyncio
async def test_reconcile_swaps_and_drains_on_change() -> None:
    """``reconcile`` opens the new executor, swaps it in, and drains the old."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        old = holder.current
        _override_nomad(_NOMAD_B)
        await holder.reconcile()
        assert holder.current is not old
        assert str(holder.current.endpoint).startswith("https://nomad-b.example.org")
        assert old._session is None


@pytest.mark.asyncio
async def test_reconcile_opens_a_fresh_session_for_a_copied_override() -> None:
    """Open a new session when the override is a ``model_copy`` of the entered executor.

    The snapshot builder merges nested leaves with ``model_copy``, which carries
    the source's private attributes over, its aiohttp session included, so the
    first override after startup arrives holding the very session the rebind is
    about to close.
    """
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        startup = holder.current
        tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"NOMAD": startup.model_copy(update={"log_socket_read_timeout": 13})}
        )

        await holder.reconcile()

        assert holder.current is not startup
        assert startup._session is None
        assert holder.current._session is not None
        assert not holder.current._session.closed


@pytest.mark.asyncio
async def test_reconcile_construction_failure_keeps_old_executor() -> None:
    """A failed rebuild propagates and leaves the previous executor live."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        old = holder.current
        tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"NOMAD": {"endpoint": "not-a-url"}}
        )
        with pytest.raises(ValidationError):
            await holder.reconcile()
        assert holder.current is old
        assert old._session is not None


def test_get_executor_returns_executor_unchanged_without_override() -> None:
    """With no override, request-less NOMAD resolution returns the live executor."""
    assert get_executor(TaskBackendEnum.NOMAD) is tasks_settings.NOMAD


def test_get_executor_returns_snapshot_executor_under_override() -> None:
    """Return the snapshot executor for request-less NOMAD resolution under an override."""
    _override_nomad(_NOMAD_A)
    result = get_executor(TaskBackendEnum.NOMAD)
    assert isinstance(result, NomadExecutor)
    assert str(result.endpoint).startswith("https://nomad-a.example.org")
    # The snapshot executor can build its sync sub-client without a session.
    assert result.backend is not None


@pytest.mark.asyncio
async def test_resolve_request_executor_returns_holder_current() -> None:
    """A request-scoped NOMAD read returns the holder's live entered executor."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(nomad_lifecycle=holder))
        )
        result = resolve_request_executor(request, TaskBackendEnum.NOMAD)
        assert result is holder.current


def test_resolve_request_executor_falls_back_without_holder() -> None:
    """Without a holder, the request-scoped read falls back to request-less."""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    result = resolve_request_executor(request, TaskBackendEnum.NOMAD)
    assert result is tasks_settings.NOMAD


def test_resolve_request_executor_falls_back_when_holder_not_started() -> None:
    """A holder present but never entered falls back to the request-less read."""
    holder = NomadLifecycle(FastAPI())  # never entered -> ``current`` raises
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(nomad_lifecycle=holder))
    )
    result = resolve_request_executor(request, TaskBackendEnum.NOMAD)
    assert result is tasks_settings.NOMAD


@pytest.mark.asyncio
async def test_reconcile_defers_the_old_close_while_a_consumer_holds() -> None:
    """Keep a held executor alive through the reconcile, closing once released."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        old = holder.current

        async with old.hold():
            _override_nomad(_NOMAD_B)
            await holder.reconcile()

            assert holder.current is not old
            assert old._session is not None

        assert old._session is None


@pytest.mark.asyncio
async def test_reconcile_keeps_a_held_stream_pool_open_until_released() -> None:
    """Close the retired executor's long-lived pool only once its holder releases."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        old = holder.current

        async with old.hold():
            stream_session = old._long_lived_session()
            _override_nomad(_NOMAD_B)
            await holder.reconcile()

            assert not stream_session.closed

        assert stream_session.closed
        assert old._stream_session is None


@pytest.mark.asyncio
async def test_reconcile_applies_a_changed_stream_connection_limit() -> None:
    """Size the new executor's long-lived pool from the overridden limit."""
    _override_nomad(_NOMAD_A)
    async with NomadLifecycle(FastAPI()) as holder:
        _override_nomad({**_NOMAD_A, "log_stream_max_connections": _STREAM_LIMIT})
        await holder.reconcile()

        assert holder.current._long_lived_session().connector.limit == _STREAM_LIMIT


@pytest.mark.asyncio
async def test_get_request_executor_yields_a_celery_executor_unheld() -> None:
    """Yield a ``CeleryExecutor`` unheld, since it owns no session."""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    yielded = [
        executor
        async for executor in get_request_executor(request, TaskBackendEnum.CELERY)
    ]

    assert len(yielded) == 1
    assert isinstance(yielded[0], CeleryExecutor)
    assert not hasattr(yielded[0], "hold")  # the nullcontext branch, not a hold


@pytest.mark.asyncio
async def test_aenter_preserves_the_configured_api_key() -> None:
    """Rebuild the executor on entry with the real key, not the JSON mask."""
    _override_nomad(_NOMAD_WITH_KEY)
    async with NomadLifecycle(FastAPI()) as holder:
        assert holder.current.api_key is not None
        assert holder.current.api_key.get_secret_value() == "glsa_realtoken"


@pytest.mark.asyncio
async def test_reconcile_rebinds_when_only_the_api_key_rotates() -> None:
    """Swap the executor on reconcile when the key rotates and nothing else moves."""
    _override_nomad(_NOMAD_WITH_KEY)
    async with NomadLifecycle(FastAPI()) as holder:
        old = holder.current
        _override_nomad(_NOMAD_WITH_ROTATED_KEY)
        await holder.reconcile()
        assert holder.current is not old
        assert holder.current.api_key is not None
        assert holder.current.api_key.get_secret_value() == "glsa_rotated"


@pytest.mark.asyncio
async def test_the_config_fingerprint_keeps_the_endpoint_password() -> None:
    """Fingerprint the real credential, which the JSON dump otherwise masks."""
    _override_nomad(_NOMAD_WITH_CREDS)
    async with NomadLifecycle(FastAPI()) as holder:
        fingerprint = holder._current_config
        assert fingerprint is not None
        assert "nomad-secret" in fingerprint["base_url"]
        assert holder.current._endpoint_credential_header is not None


@pytest.mark.asyncio
async def test_reconcile_rebinds_when_only_the_endpoint_password_rotates() -> None:
    """Swap the executor when the password rotates and nothing else moves.

    A fingerprint taken after redaction would render both passwords as the
    same mask, so the rotation would read as no change and the executor would
    keep authenticating with the retired credential.
    """
    _override_nomad(_NOMAD_WITH_CREDS)
    async with NomadLifecycle(FastAPI()) as holder:
        old = holder.current
        _override_nomad(
            {"endpoint": "http://nomad-user:rotated-secret@nomad.internal:4646"}
        )
        await holder.reconcile()

        assert holder.current is not old
        assert "rotated-secret" in holder.current.base_url
        assert holder.current._endpoint_credential_header == encode_basic_auth(
            "nomad-user", "rotated-secret"
        )
