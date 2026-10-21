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

"""Pin that the autouse snapshot reset covers every wired override proxy."""

import pytest

from app.core.settings_override.proxy import OverridableSettingsProxy
from app.extensions.settings_override import build_extensions_override_proxies
from app.tasks.celery import build_tasks_override_proxies
from tests.app.conftest import clear_wired_override_snapshots

_PROBE = {"_probe": True}


def _registry_proxy_params() -> list:
    """Parametrize over every proxy the production registries currently wire.

    Built from fresh registry calls (not the cached helper) so a proxy present
    in a registry but missing from the reset set fails the clear assertion.
    """
    return [
        pytest.param(entry.proxy, id=identifier)
        for registry in (
            build_extensions_override_proxies(),
            build_tasks_override_proxies(),
        )
        for identifier, entry in registry.items()
    ]


class TestOverrideSnapshotReset:
    """Pin the registry-derived autouse snapshot reset."""

    @pytest.mark.parametrize("proxy", _registry_proxy_params())
    def test_reset_clears_published_snapshot(
        self, proxy: OverridableSettingsProxy
    ) -> None:
        """Clear a published snapshot via the shared reset helper.

        Calls ``clear_wired_override_snapshots`` directly so the assertion does
        not depend on xdist scheduling: a broken reset fails here whether the
        suite runs serially or under ``worksteal``.
        """
        proxy._set_snapshot(_PROBE)
        clear_wired_override_snapshots()
        assert dict(proxy.get_snapshot()) == {}
