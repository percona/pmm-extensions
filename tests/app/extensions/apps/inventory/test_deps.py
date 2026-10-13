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

"""Define tests for the app.extensions.apps.inventory.deps module."""

import logging
import re
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from pytest_mock import MockerFixture

from app.core.config import Settings
from app.core.exceptions import HTTPServiceUnavailableException
from app.core.requests import RemoteAPI
from app.core.utils.fields import UniqueList
from app.extensions.apps.inventory.deps import (
    _get_syncer_qualified_name,
    AvailableSyncer,
    build_available_syncers,
    filter_syncers_by_name,
    get_syncers,
    get_syncers_standalone,
)
from app.extensions.config import extensions_settings, SyncOptions
from app.extensions.sync.exceptions import SyncerConfigurationError
from app.extensions.sync.models import BaseSyncer
from app.extensions.sync.syncers.pmm import PMMSyncer
from app.tasks.execution_request_secrets import TASKS_CLIENT_LOG_POLICY

_EXPECTED_DISAMBIGUATED_DISPLAY_NAMES = 2


class _StubSyncer:
    """Minimal stand-in for ``BaseSyncer`` whose identity is its class name."""


class _OtherStubSyncer:
    """Second minimal syncer stand-in for multi-syncer scenarios."""


class FooSyncer:
    """Stub class with the conventional ``Syncer`` suffix."""


class Foo:
    """Stub class without the ``Syncer`` suffix."""


def _as_syncers(stubs: list[object]) -> list[BaseSyncer]:
    """Cast a list of stub instances to ``list[BaseSyncer]`` for the helpers.

    The helpers under test never invoke any method on the syncer instances
    themselves — they only consult ``type(syncer)`` and a caller-supplied
    capability check — so plain stub classes are sufficient and avoid pulling
    in the full ``BaseSyncer`` Pydantic surface.

    :param stubs: The stub instances to relabel.
    :type stubs: list[object]
    :return: The same list, typed as ``list[BaseSyncer]``.
    :rtype: list[BaseSyncer]
    """
    return cast("list[BaseSyncer]", stubs)


def _always(_syncer: BaseSyncer) -> bool:
    """Capability check that always returns ``True``."""
    return True


def _never(_syncer: BaseSyncer) -> bool:
    """Capability check that always returns ``False``."""
    return False


def test_get_syncer_qualified_name_returns_module_and_class_name():
    """Ensure the helper returns ``module.ClassName`` for an instance."""
    qualified = _get_syncer_qualified_name(cast("BaseSyncer", _StubSyncer()))
    assert qualified == f"{__name__}._StubSyncer"


def test_build_available_syncers_filters_out_non_matching():
    """Ensure syncers that fail the capability check are excluded."""
    available = build_available_syncers(
        _as_syncers([_StubSyncer(), _OtherStubSyncer()]),
        lambda syncer: type(syncer).__name__ == "_StubSyncer",
    )
    assert len(available) == 1
    assert available[0].name == f"{__name__}._StubSyncer"


def test_build_available_syncers_uses_fully_qualified_name_for_wire_identifier():
    """Ensure ``entry.name`` carries the fully qualified identifier."""
    available = build_available_syncers(_as_syncers([FooSyncer()]), _always)
    assert available[0].name == f"{__name__}.FooSyncer"


def test_build_available_syncers_strips_suffix_for_display_name():
    """Ensure the trailing ``Syncer`` suffix is stripped from display names."""
    available = build_available_syncers(_as_syncers([FooSyncer()]), _always)
    assert available[0].display_name == "Foo"


def test_build_available_syncers_class_without_suffix_falls_back_to_class_name():
    """Ensure classes without a ``Syncer`` suffix keep their full short name."""
    available = build_available_syncers(_as_syncers([Foo()]), _always)
    assert available[0].display_name == "Foo"


def test_build_available_syncers_returns_named_tuples():
    """Ensure entries are ``AvailableSyncer`` instances."""
    available = build_available_syncers(_as_syncers([FooSyncer()]), _always)
    assert isinstance(available[0], AvailableSyncer)


def test_build_available_syncers_preserves_declaration_order():
    """Ensure the returned list reflects the input order."""
    available = build_available_syncers(
        _as_syncers([_StubSyncer(), _OtherStubSyncer()]),
        _always,
    )
    assert [entry.display_name for entry in available] == ["_Stub", "_OtherStub"]


def test_build_available_syncers_disambiguates_colliding_short_names():
    """Render fully qualified names when two syncers share a short class name.

    The wire identifier already disambiguates these correctly, but the
    dropdown labels would otherwise collapse to identical strings, leaving
    the operator unable to tell which menu item targets which syncer.
    """
    legacy_cls = type("MySQLSyncer", (), {})
    legacy_cls.__module__ = "tests.synthetic.legacy"
    new_cls = type("MySQLSyncer", (), {})
    new_cls.__module__ = "tests.synthetic.new"
    available = build_available_syncers(
        _as_syncers([legacy_cls(), new_cls()]),
        _always,
    )
    display_names = [entry.display_name for entry in available]
    assert display_names == [
        "tests.synthetic.legacy.MySQLSyncer",
        "tests.synthetic.new.MySQLSyncer",
    ]
    assert len(set(display_names)) == _EXPECTED_DISAMBIGUATED_DISPLAY_NAMES


def test_build_available_syncers_does_not_disambiguate_unique_display_names():
    """Keep the short, stripped display name when no other entry collides."""
    available = build_available_syncers(
        _as_syncers([_StubSyncer(), _OtherStubSyncer()]),
        _always,
    )
    assert [entry.display_name for entry in available] == ["_Stub", "_OtherStub"]


def test_build_available_syncers_disambiguates_when_stripped_names_collide():
    """Disambiguate even when the collision only appears after suffix stripping."""
    available = build_available_syncers(
        _as_syncers([FooSyncer(), Foo()]),
        _always,
    )
    display_names = [entry.display_name for entry in available]
    assert len(set(display_names)) == _EXPECTED_DISAMBIGUATED_DISPLAY_NAMES
    assert all(name.endswith(("FooSyncer", "Foo")) for name in display_names)


def test_filter_syncers_by_name_returns_full_list_unchanged_when_name_is_none():
    """Ensure sync-all mode bypasses the capability check entirely."""
    syncers = _as_syncers([_StubSyncer(), _OtherStubSyncer()])
    assert filter_syncers_by_name(syncers, None, _never) == syncers


def test_filter_syncers_by_name_returns_full_list_unchanged_when_name_is_empty_string():
    """Ensure an empty ``syncer_name`` is treated as sync-all."""
    syncers = _as_syncers([_StubSyncer(), _OtherStubSyncer()])
    assert filter_syncers_by_name(syncers, "", _never) == syncers


def test_filter_syncers_by_name_matches_on_fully_qualified_name():
    """Ensure same-short-name classes in distinct modules are disambiguated."""
    legacy_cls = type("MySQLSyncer", (), {})
    legacy_cls.__module__ = "tests.synthetic.legacy"
    new_cls = type("MySQLSyncer", (), {})
    new_cls.__module__ = "tests.synthetic.new"
    legacy = legacy_cls()
    new = new_cls()
    result = filter_syncers_by_name(
        _as_syncers([legacy, new]),
        "tests.synthetic.new.MySQLSyncer",
        _always,
    )
    assert result == [new]


def test_filter_syncers_by_name_raises_value_error_for_unknown_name():
    """Ensure an unknown syncer name raises ``ValueError`` instead of silently no-op."""
    with pytest.raises(ValueError, match=r"not\.a\.real\.Name"):
        filter_syncers_by_name(
            _as_syncers([_StubSyncer()]),
            "not.a.real.Name",
            _always,
        )


def test_filter_syncers_by_name_raises_value_error_when_matched_syncer_cannot_sync_entity():
    """Ensure a name match that fails the capability check still raises ``ValueError``."""
    qualified = f"{__name__}._StubSyncer"
    with pytest.raises(ValueError, match=re.escape(qualified)):
        filter_syncers_by_name(
            _as_syncers([_StubSyncer()]),
            qualified,
            _never,
        )


def test_get_syncers_omits_unset_pmm_none() -> None:
    """Do not pass explicit ``pmm=None`` into ``PMMSyncer`` (breaks validation)."""
    inventory_api = AsyncMock(spec=RemoteAPI)
    tasks_api = AsyncMock(spec=RemoteAPI)
    syncers_list = UniqueList(
        [SyncOptions.model_validate({"syncer": "PMMSyncer"})],
    )
    with patch.object(extensions_settings, "SYNCERS", syncers_list):
        syncers = get_syncers(inventory_api, tasks_api)
    assert len(syncers) == 1
    assert isinstance(syncers[0], PMMSyncer)


class TestSyncerConstructionFailure:
    """Report an unusable syncer setting as a configuration error, never a 500.

    Settings load rejects a bad threshold, so construction only sees one when the
    settings object was assembled some other way — but the sync trigger must not
    answer such a state with a bare 500 either.
    """

    @pytest.fixture
    def unusable_syncers(self, mocker: MockerFixture) -> None:
        """Configure a single syncer whose stale-run threshold cannot be applied."""
        option = SyncOptions(
            syncer="app.extensions.sync.syncers.pmm.PMMSyncer", stale_run_after="60"
        )
        mocker.patch.object(extensions_settings, "SYNCERS", UniqueList([option]))

    def test_get_syncers_raises_service_unavailable(
        self, unusable_syncers: None
    ) -> None:
        """Answer a request with 503, naming the syncer and the offending field."""
        with pytest.raises(HTTPServiceUnavailableException) as excinfo:
            get_syncers(AsyncMock(spec=RemoteAPI), AsyncMock(spec=RemoteAPI))

        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "PMMSyncer" in excinfo.value.detail
        assert "STALE_RUN_AFTER" in excinfo.value.detail

    def test_service_unavailable_detail_withholds_the_raw_validation_error(
        self, unusable_syncers: None
    ) -> None:
        """Keep the value and pydantic's internals out of a client-facing body."""
        with pytest.raises(HTTPServiceUnavailableException) as excinfo:
            get_syncers(AsyncMock(spec=RemoteAPI), AsyncMock(spec=RemoteAPI))

        detail = excinfo.value.detail
        assert "input_value" not in detail
        assert "errors.pydantic.dev" not in detail

    def test_rejected_configuration_is_logged_with_the_syncer(
        self, unusable_syncers: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Make the log line greppable back to the entry that has to be fixed."""
        with (
            caplog.at_level(logging.ERROR, logger="app.extensions.apps.inventory.deps"),
            pytest.raises(HTTPServiceUnavailableException),
        ):
            get_syncers(AsyncMock(spec=RemoteAPI), AsyncMock(spec=RemoteAPI))

        assert len(caplog.records) == 1
        assert (
            "app.extensions.sync.syncers.pmm.PMMSyncer"
            in caplog.records[0].getMessage()
        )
        assert caplog.records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_get_syncers_standalone_raises_configuration_error(
        self, unusable_syncers: None, mocker: MockerFixture
    ) -> None:
        """Fail a scheduled run with a configuration error, not a pydantic error."""
        mocker.patch(
            "app.extensions.apps.inventory.deps.get_inventory_api_standalone",
            new=AsyncMock(return_value=AsyncMock(spec=RemoteAPI)),
        )
        mocker.patch.object(
            Settings,
            "get_remote_api",
            new=AsyncMock(return_value=AsyncMock(spec=RemoteAPI)),
        )

        with pytest.raises(SyncerConfigurationError, match="STALE_RUN_AFTER"):
            await get_syncers_standalone()


@pytest.mark.asyncio
async def test_get_syncers_standalone_requests_the_tasks_client_log_policy(
    mocker: MockerFixture,
) -> None:
    """Assert the Tasks client is requested from the registry with both log settings.

    The registry keys a client on its endpoint and SSL settings alone, so a site
    omitting the policy can hand every later site a client that logs in full.

    :param mocker: The patching fixture standing in for the client registry.
    """
    get_remote_api = mocker.patch.object(
        Settings,
        "get_remote_api",
        new=AsyncMock(return_value=MagicMock(spec=RemoteAPI)),
    )

    await get_syncers_standalone()

    tasks_calls = [
        awaited.kwargs
        for awaited in get_remote_api.await_args_list
        if awaited.kwargs["endpoint"] == extensions_settings.TASKS_ENDPOINT
    ]
    assert len(tasks_calls) == 1
    assert (
        tasks_calls[0]["sensitive_body_paths"]
        == TASKS_CLIENT_LOG_POLICY["sensitive_body_paths"]
    )
    assert tasks_calls[0]["withhold_response_log"] is True
