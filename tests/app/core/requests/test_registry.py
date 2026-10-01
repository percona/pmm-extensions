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

"""Tests for :meth:`ClientRegistry.invalidate`."""

import asyncio

import pytest
from pytest_mock import MockerFixture

from app.core.requests.registry import ClientRegistry
from app.core.requests.remote_api import RemoteAPI
from app.core.utils.fields import CREDENTIAL_URL_MASK


@pytest.mark.asyncio
async def test_invalidate_evicts_matching_and_keeps_others() -> None:
    """``invalidate`` evicts and closes only clients on the matching endpoint."""
    registry = ClientRegistry()
    try:
        client_a = await registry.get(RemoteAPI, endpoint="https://a.example.org")
        client_b = await registry.get(RemoteAPI, endpoint="https://b.example.org")

        await registry.invalidate("https://a.example.org")

        assert client_a._session is None  # the evicted client was closed
        reborn_a = await registry.get(RemoteAPI, endpoint="https://a.example.org")
        survived_b = await registry.get(RemoteAPI, endpoint="https://b.example.org")
        assert reborn_a is not client_a  # a was reconstructed fresh
        assert survived_b is client_b  # b was left untouched
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_invalidate_is_trailing_slash_insensitive() -> None:
    """``invalidate`` matches regardless of a trailing slash on the endpoint."""
    registry = ClientRegistry()
    try:
        client = await registry.get(RemoteAPI, endpoint="https://a.example.org")
        await registry.invalidate("https://a.example.org/")
        assert client._session is None
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_invalidate_noop_when_closed() -> None:
    """``invalidate`` is a no-op on a closed registry instead of raising."""
    registry = ClientRegistry()
    await registry.close_all()
    await registry.invalidate("https://a.example.org")


@pytest.mark.asyncio
async def test_invalidate_noop_when_no_match() -> None:
    """``invalidate`` for an unknown endpoint leaves existing clients intact."""
    registry = ClientRegistry()
    try:
        client = await registry.get(RemoteAPI, endpoint="https://a.example.org")
        await registry.invalidate("https://unknown.example.org")
        assert client._session is not None
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_invalidate_defers_the_close_while_a_consumer_holds() -> None:
    """Evict the client at once and close it once the last holder releases."""
    registry = ClientRegistry()
    try:
        client = await registry.get(RemoteAPI, endpoint="https://a.example.org")

        async with client.hold():
            await registry.invalidate("https://a.example.org")

            assert client._session is not None
            reborn = await registry.get(RemoteAPI, endpoint="https://a.example.org")
            assert reborn is not client

        assert client._session is None
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_close_all_force_closes_a_deferred_retiree() -> None:
    """Force-close an invalidate()-deferred client when shutdown runs mid-hold."""
    registry = ClientRegistry()
    client = await registry.get(RemoteAPI, endpoint="https://a.example.org")

    async with client.hold():
        await registry.invalidate("https://a.example.org")
        assert client._session is not None

        await registry.close_all()

        assert client._session is None


@pytest.mark.asyncio
async def test_close_all_force_closes_mid_invalidate_before_close_when_idle(
    mocker: MockerFixture,
) -> None:
    """Eviction registers on pending under the lock so close_all cannot miss it.

    Pause after ``invalidate`` has left ``close_when_idle``; the client must
    already be on pending from the locked eviction, so ``close_all`` force-closes
    it during the pause -- not only after invalidate resumes. Cancelling the
    invalidate task afterward must not reopen a leak.
    """
    registry = ClientRegistry()
    client = await registry.get(RemoteAPI, endpoint="https://a.example.org")
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = RemoteAPI.close_when_idle

    async def paused_close_when_idle(self: RemoteAPI, pending=None) -> None:
        entered.set()
        await resume.wait()
        await original(self, pending=pending)

    mocker.patch.object(RemoteAPI, "close_when_idle", paused_close_when_idle)

    async with client.hold():
        invalidate_task = asyncio.create_task(
            registry.invalidate("https://a.example.org")
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert client._session is not None
        assert id(client) in registry._pending_closes._clients

        await registry.close_all()
        assert client._session is None
        assert registry._pending_closes.sealed

        resume.set()
        invalidate_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invalidate_task

        assert client._session is None
        assert registry._pending_closes._clients == {}


@pytest.mark.asyncio
async def test_close_all_force_closes_idle_client_cancelled_mid_invalidate(
    mocker: MockerFixture,
) -> None:
    """An idle eviction still lands on pending so cancel mid-close cannot leak."""
    registry = ClientRegistry()
    client = await registry.get(RemoteAPI, endpoint="https://a.example.org")
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = RemoteAPI.close_when_idle

    async def paused_close_when_idle(self: RemoteAPI, pending=None) -> None:
        entered.set()
        await resume.wait()
        await original(self, pending=pending)

    mocker.patch.object(RemoteAPI, "close_when_idle", paused_close_when_idle)

    invalidate_task = asyncio.create_task(registry.invalidate("https://a.example.org"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert id(client) in registry._pending_closes._clients
    assert client._session is not None

    invalidate_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invalidate_task

    await registry.close_all()
    assert client._session is None


@pytest.mark.asyncio
async def test_close_all_still_force_closes_when_active_result_handling_raises(
    mocker: MockerFixture,
) -> None:
    """Pending sweep still runs when post-gather error handling raises.

    Nested ``finally`` keeps the deferred retiree from leaking when the try body
    fails after active closes are awaited.
    """
    registry = ClientRegistry()
    active = await registry.get(RemoteAPI, endpoint="https://active.example.org")
    deferred = await registry.get(RemoteAPI, endpoint="https://deferred.example.org")
    original_close = RemoteAPI.close
    try:
        async with deferred.hold():
            await registry.invalidate("https://deferred.example.org")
            assert id(deferred) in registry._pending_closes._clients
            assert deferred._session is not None

            async def close_active_fails(self: RemoteAPI) -> None:
                if self is active:
                    raise RuntimeError("close boom")
                await original_close(self)

            mocker.patch.object(RemoteAPI, "close", close_active_fails)
            mocker.patch(
                "app.core.requests.registry.logger.warning",
                side_effect=RuntimeError("log boom"),
            )

            with pytest.raises(RuntimeError, match="log boom"):
                await registry.close_all()

            assert deferred._session is None
            assert registry._pending_closes.sealed
    finally:
        mocker.stopall()
        if active._session is not None:
            await original_close(active)


@pytest.mark.asyncio
async def test_close_all_retries_failed_pending_force_close(
    mocker: MockerFixture,
) -> None:
    """A later ``close_all`` re-sweeps pending after a transient close failure."""
    registry = ClientRegistry()
    client = await registry.get(RemoteAPI, endpoint="https://deferred.example.org")

    async with client.hold():
        await registry.invalidate("https://deferred.example.org")
        session = client._session
        assert session is not None
        real_close = session.close
        fail_once = True

        async def flaky_close() -> None:
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("close boom")
            await real_close()

        mocker.patch.object(session, "close", flaky_close)

        await registry.close_all()
        assert registry.closed
        assert client._session is session
        assert id(client) in registry._pending_closes._clients

        await registry.close_all()
        assert client._session is None
        assert registry._pending_closes._clients == {}


_CREDENTIAL_ENDPOINT = "https://svcuser:svcpass@a.example.org"
_CREDENTIAL_SECRET = "svcpass"
_CLOSE_FAILURE = "close boom"
_REGISTRY_LOGGER = "app.core.requests.registry"
_FAILING_CLIENTS = 2


class _FailingCloseRemoteAPI(RemoteAPI):
    """Stand in for a client whose session refuses to close."""

    async def close(self) -> None:
        """Raise instead of closing, so the registry has a failure to report."""
        raise RuntimeError(_CLOSE_FAILURE)

    async def close_when_idle(self, pending=None) -> None:
        """Raise on the eviction path the same way."""
        raise RuntimeError(_CLOSE_FAILURE)


@pytest.mark.asyncio
async def test_invalidate_reports_a_close_failure_without_the_password(caplog) -> None:
    """Report an eviction close failure with the endpoint password masked."""
    registry = ClientRegistry()
    try:
        await registry.get(_FailingCloseRemoteAPI, endpoint=_CREDENTIAL_ENDPOINT)

        with caplog.at_level("WARNING", logger=_REGISTRY_LOGGER):
            await registry.invalidate(_CREDENTIAL_ENDPOINT)
    finally:
        await registry.close_all()

    assert _CREDENTIAL_SECRET not in caplog.text
    assert "svcuser:****@a.example.org" in caplog.text
    assert _CLOSE_FAILURE in caplog.text


@pytest.mark.asyncio
async def test_close_all_reports_a_close_failure_without_the_password(caplog) -> None:
    """Report a shutdown close failure with the endpoint password masked."""
    registry = ClientRegistry()
    await registry.get(_FailingCloseRemoteAPI, endpoint=_CREDENTIAL_ENDPOINT)

    with caplog.at_level("WARNING", logger=_REGISTRY_LOGGER):
        await registry.close_all()

    assert _CREDENTIAL_SECRET not in caplog.text
    assert "svcuser:****@a.example.org" in caplog.text
    assert _CLOSE_FAILURE in caplog.text


@pytest.mark.asyncio
async def test_close_all_reports_every_failing_client(caplog) -> None:
    """Keep reporting the remaining clients after the first failure."""
    registry = ClientRegistry()
    await registry.get(_FailingCloseRemoteAPI, endpoint=_CREDENTIAL_ENDPOINT)
    await registry.get(_FailingCloseRemoteAPI, endpoint="https://b.example.org")

    with caplog.at_level("WARNING", logger=_REGISTRY_LOGGER):
        await registry.close_all()

    reported = [r for r in caplog.records if _CLOSE_FAILURE in r.getMessage()]
    assert len(reported) == _FAILING_CLIENTS


@pytest.mark.asyncio
async def test_close_all_survives_a_base_url_it_cannot_redact(
    caplog, monkeypatch
) -> None:
    """Report the close failure even when the base URL defeats the redaction.

    Raising here would replace the failure being reported with a parse error
    and strand the clients still to report on, so the mask stands in for the
    whole URL instead. The broken value is introduced after the session is
    open, because a client cannot be built from it in the first place.
    """
    registry = ClientRegistry()
    await registry.get(_FailingCloseRemoteAPI, endpoint=_CREDENTIAL_ENDPOINT)
    monkeypatch.setattr(
        _FailingCloseRemoteAPI,
        "_compute_base_url",
        lambda _self: "http://[::1:4646/",
    )

    with caplog.at_level("WARNING", logger=_REGISTRY_LOGGER):
        await registry.close_all()

    assert _CREDENTIAL_SECRET not in caplog.text
    assert CREDENTIAL_URL_MASK in caplog.text
    assert _CLOSE_FAILURE in caplog.text
    assert registry._clients == {}


@pytest.mark.asyncio
async def test_invalidate_survives_a_base_url_it_cannot_redact(
    caplog, monkeypatch
) -> None:
    """Report an eviction close failure even when the base URL defeats the redaction."""
    registry = ClientRegistry()
    try:
        await registry.get(_FailingCloseRemoteAPI, endpoint=_CREDENTIAL_ENDPOINT)
        monkeypatch.setattr(
            _FailingCloseRemoteAPI,
            "_compute_base_url",
            lambda _self: "http://svcuser:svcpass@[::1:4646/",
        )

        with caplog.at_level("WARNING", logger=_REGISTRY_LOGGER):
            await registry.invalidate(_CREDENTIAL_ENDPOINT)
    finally:
        monkeypatch.undo()
        await registry.close_all()

    assert _CREDENTIAL_SECRET not in caplog.text
    assert CREDENTIAL_URL_MASK in caplog.text
    assert _CLOSE_FAILURE in caplog.text
