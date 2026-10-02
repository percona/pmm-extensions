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

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import encode_basic_auth
from fastapi import FastAPI
from pydantic import ValidationError
from pytest_mock import MockerFixture

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
from tests.app.core.requests.pending_close_helpers import patch_paused_close_when_idle

_NOMAD_A = {"endpoint": "https://nomad-a.example.org"}
_NOMAD_B = {"endpoint": "https://nomad-b.example.org"}
_NOMAD_WITH_CREDS = {
    "endpoint": "http://nomad-user:nomad-secret@nomad.internal:4646",
}
_NOMAD_WITH_KEY = {**_NOMAD_A, "api_key": "glsa_realtoken"}
_NOMAD_WITH_ROTATED_KEY = {**_NOMAD_A, "api_key": "glsa_rotated"}


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
async def test_aexit_force_closes_a_deferred_retiree() -> None:
    """Force-close a mid-hold retiree when the lifecycle shuts down."""
    _override_nomad(_NOMAD_A)
    held = asyncio.Event()

    async def consumer(client: NomadExecutor) -> None:
        async with client.hold():
            held.set()
            await asyncio.Event().wait()

    try:
        async with NomadLifecycle(FastAPI()) as holder:
            old = holder.current
            task = asyncio.create_task(consumer(old))
            await asyncio.wait_for(held.wait(), timeout=5)
            _override_nomad(_NOMAD_B)
            await holder.reconcile()
            assert old._session is not None

        assert old._session is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        tasks_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_reconcile_refuses_to_publish_after_aexit() -> None:
    """Reject a reconcile queued past shutdown so it cannot open a fresh executor."""
    _override_nomad(_NOMAD_A)
    app = FastAPI()
    holder = NomadLifecycle(app)
    try:
        await holder.__aenter__()
        await holder.__aexit__(None, None, None)
        assert holder._current is None
        assert holder._closing is True

        _override_nomad(_NOMAD_B)
        await holder.reconcile()

        assert holder._current is None
    finally:
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_aexit_still_force_closes_when_active_close_fails(
    mocker: MockerFixture,
) -> None:
    """Keep pending sweep and unpublish running when the active executor close raises."""
    _override_nomad(_NOMAD_A)
    app = FastAPI()
    holder = NomadLifecycle(app)
    old = None
    try:
        await holder.__aenter__()
        old = holder.current
        retired = await NomadExecutor.model_validate(_NOMAD_B).open()

        async with retired.hold():
            await retired.close_when_idle(pending=holder._pending_closes)
            mocker.patch.object(
                old, "__aexit__", AsyncMock(side_effect=RuntimeError("active boom"))
            )

            with pytest.raises(RuntimeError, match="active boom"):
                await holder.__aexit__(None, None, None)

            assert holder._current is None
            assert retired._session is None
            assert app.state.nomad_lifecycle is None
    finally:
        if old is not None and old._session is not None:
            await old._session.close()
            old._session = None
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_aexit_force_closes_idle_retiree_cancelled_mid_reconcile(
    mocker: MockerFixture,
) -> None:
    """Register an idle reconcile under the lock so cancel mid-close cannot leak."""
    _override_nomad(_NOMAD_A)
    app = FastAPI()
    holder = NomadLifecycle(app)
    try:
        await holder.__aenter__()
        retired = holder.current
        entered, resume = patch_paused_close_when_idle(mocker, NomadExecutor)

        _override_nomad(_NOMAD_B)
        reconcile_task = asyncio.create_task(holder.reconcile())
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert id(retired) in holder._pending_closes._clients
        assert retired._session is not None

        reconcile_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reconcile_task

        await holder.__aexit__(None, None, None)
        assert retired._session is None
        assert holder._pending_closes._clients == {}
    finally:
        tasks_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_aexit_force_closes_mid_reconcile_before_close_when_idle(
    mocker: MockerFixture,
) -> None:
    """Register the swap on pending under the lock so ``__aexit__`` cannot miss it.

    Pause after reconcile has left ``close_when_idle``; the retired executor must
    already be on pending from the locked swap, so ``__aexit__`` force-closes it
    during the pause — not only after reconcile resumes.
    """
    _override_nomad(_NOMAD_A)
    app = FastAPI()
    holder = NomadLifecycle(app)
    try:
        await holder.__aenter__()
        retired = holder.current
        entered, resume = patch_paused_close_when_idle(mocker, NomadExecutor)

        async with retired.hold():
            _override_nomad(_NOMAD_B)
            reconcile_task = asyncio.create_task(holder.reconcile())
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert id(retired) in holder._pending_closes._clients
            assert retired._session is not None

            await holder.__aexit__(None, None, None)
            assert retired._session is None
            assert holder._pending_closes.sealed

            resume.set()
            reconcile_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reconcile_task

            assert retired._session is None
            assert holder._pending_closes._clients == {}
    finally:
        tasks_settings._set_snapshot({})


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
