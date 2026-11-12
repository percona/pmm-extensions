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

"""Test the app's configuration surface, and the decisions that shaped it.

Every role reads the configuration through the app's own ``GET /config``; an admin
changes it through ``/api/extensions/admin/settings`` like any other class. The one
write served here is ``PUT /enabled``, because pmm-managed sets ``ENABLED`` from PMM's
switch with no user behind the call, and **PMM's principal is not an admin**: the
token resolves to the synthetic ``extensions-service`` user, so the settings router
refuses it. That is asserted here rather than described, because a later change
making the principal an admin would silently remove the reason.

The other decision is ``CREDENTIALS_PATH`` staying cold. It names a file the payload
reads on every database *host* and hands to a driver as a URI, so an overridable one
would turn "configure this app" into "read a chosen file across the estate". A test
asserts it is refused, because the difference between "not hot" and "not yet hot" is
invisible in the field list.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient, Response
from kombu.exceptions import OperationalError as KombuOperationalError
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy import event
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import get_current_user as resolve_bearer
from app.api.deps import SERVICE_PRINCIPAL
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.celery.models import IntervalSchedule, Period
from app.core.config import settings
from app.core.exceptions import HTTPBadRequestException
from app.core.requests.remote_api import RemoteAPI
from app.core.settings_override import cache
from app.core.settings_override.lifecycle import publish_snapshot
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token, SettingOverride
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field_names
from app.core.utils.date_time import make_datetime_utc
from app.extensions.apps.framework.registry import collect_app_owned_settings_classes
from app.extensions.apps.om_inventory.api_routes import QUEUE_UNAVAILABLE_DETAIL
from app.extensions.apps.om_inventory.app import app as om_inventory_app
from app.extensions.apps.om_inventory.celery import run_om_probe
from app.extensions.apps.om_inventory.config import (
    om_inventory_settings,
    OmInventorySettings,
)
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRunStatus
from app.extensions.deps import get_session, get_tasks_api
from app.extensions.main import extensions_app, extensions_overrides_lifespan
from tests.app.extensions.apps.om_inventory.conftest import (
    BASE,
    start_run,
    store_overrides,
)

#: The YAML-configured values these tests assert against, taken from the class so a
#: default change moves the assertions with it rather than silently passing.
DEFAULTS = OmInventorySettings()

#: The shared, admin-gated settings router's path for this class.
ADMIN_SETTINGS = f"/api/extensions/admin/settings/{OmInventorySettings.__name__}"

#: The class's two relationships, in declaration order.
PROBE_RULE, STALE_RULE = OmInventorySettings.CROSS_FIELD_RULES

#: The ``EXTENSIONS_INTERNAL_TOKEN`` the real-gate tests present.
INTERNAL_TOKEN = "internal-test-token"


async def _active_rows(session: AsyncSession) -> list[SettingOverride]:
    """Return this class's active override rows.

    :param session: The database session.
    :return: The stored rows.
    """
    return await SettingsOverrideManager.list(
        session,
        setting_class=setting_class_token(OmInventorySettings),
        is_active=True,
    )


@pytest_asyncio.fixture
async def gated_api(
    session: AsyncSession, mocker: MockerFixture
) -> AsyncIterator[AsyncClient]:
    """Yield a client whose requests pass the real authentication and role gate.

    Only the database session is overridden, and ``EXTENSIONS_INTERNAL_TOKEN`` is set
    so that ``INTERNAL_TOKEN`` resolves to PMM Extensions' service principal.

    :param session: The database session the routes should use.
    :param mocker: Sets the internal token for the test's duration.
    :return: The client.
    """
    mocker.patch.object(
        settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(INTERNAL_TOKEN)
    )
    extensions_app.dependency_overrides[get_session] = lambda: session
    client = AsyncClient(
        transport=ASGITransport(app=extensions_app), base_url="http://test"
    )
    try:
        yield client
    finally:
        await client.aclose()
        extensions_app.dependency_overrides = {}


async def _switch(
    client: AsyncClient, *, enabled: bool = True, scan: bool = False
) -> Response:
    """Set PMM's switch as pmm-managed does.

    :param client: The client passing the real gate.
    :param enabled: The switch's new state.
    :param scan: Whether to ask for a sweep with it.
    :return: The response.
    """
    return await client.put(
        f"{BASE}/enabled",
        json={"enabled": enabled, "scan": scan},
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )


class TestSetEnabled:
    """Follow PMM's Operations for MongoDB switch through ``PUT /enabled``."""

    @pytest.fixture(autouse=True)
    def delay(self, mocker: MockerFixture) -> MagicMock:
        """Stand in for the broker, which switching on reaches.

        :param mocker: Patches the task's ``delay``.
        :return: The stand-in.
        """
        return mocker.patch.object(run_om_probe, "delay")

    def test_the_extensions_token_principal_is_not_an_admin(self) -> None:
        """Confirm PMM's principal is shut out of the admin settings router.

        Not a statement about the current deployment's configuration — the service
        principal is constructed in code with no ``is_admin`` argument, so this holds
        in every deployment. If it ever stops holding, the argument for a route of
        its own weakens and this test is where that gets noticed.
        """
        assert SERVICE_PRINCIPAL.is_admin is False

    @pytest.mark.asyncio
    async def test_the_service_token_flips_the_switch(
        self, gated_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Store and apply the new state, recording the principal as its author.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        """
        response = await _switch(gated_api, enabled=not DEFAULTS.ENABLED)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["value"] is not DEFAULTS.ENABLED
        assert om_inventory_settings.ENABLED is not DEFAULTS.ENABLED
        assert [(row.key, row.updated_by) for row in await _active_rows(session)] == [
            ("ENABLED", SERVICE_PRINCIPAL.username)
        ]

    @pytest.mark.asyncio
    async def test_flipping_the_switch_fires_its_rebind_callback(
        self, gated_api: AsyncClient, mocker: MockerFixture
    ) -> None:
        """Re-seed beat from the request, not only on the next refresher cycle.

        :param gated_api: The client passing the real gate.
        :param mocker: Supplies the spy callback.
        """
        reseed = mocker.AsyncMock()
        original = getattr(extensions_app.state, "override_callbacks", None)
        extensions_app.state.override_callbacks = {
            (OmInventorySettings.__name__, "ENABLED"): reseed
        }
        try:
            response = await _switch(gated_api, enabled=not DEFAULTS.ENABLED)
        finally:
            extensions_app.state.override_callbacks = original

        assert response.status_code == status.HTTP_200_OK
        reseed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_switching_on_registers_the_sweep(
        self, gated_api: AsyncClient
    ) -> None:
        """Register the periodic sweep once PMM turns the switch on.

        This is what the re-seed callback rebuilds beat from.

        :param gated_api: The client passing the real gate.
        """
        schedules = om_inventory_app.periodic_task_schedules
        assert schedules is not None
        assert not isinstance(schedules, list)
        assert DEFAULTS.SCHEDULE is not None
        assert schedules() == []

        await _switch(gated_api)

        assert [task.name for task in schedules()] == ["extensions__run_om_probe"]

    @pytest.mark.asyncio
    async def test_switching_on_sweeps_at_once(
        self, gated_api: AsyncClient, session: AsyncSession, delay: MagicMock
    ) -> None:
        """Queue a sweep of the whole estate when asked, rather than wait out ``SCHEDULE``.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        """
        response = await _switch(gated_api, scan=True)

        assert response.status_code == status.HTTP_200_OK
        (run,) = await ProbeRunManager.list(session)
        delay.assert_called_once_with(str(run.id), None, automatic=True)

    @pytest.mark.asyncio
    async def test_asking_with_the_switch_already_on_sweeps(
        self, gated_api: AsyncClient, session: AsyncSession, delay: MagicMock
    ) -> None:
        """Sweep when asked although ``ENABLED`` is already on, as after a restart.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        """
        await _switch(gated_api)

        response = await _switch(gated_api, scan=True)

        assert response.status_code == status.HTTP_200_OK
        (run,) = await ProbeRunManager.list(session)
        delay.assert_called_once_with(str(run.id), None, automatic=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{"enabled": True, "scan": False}, {"enabled": True}],
        ids=["scan-false", "scan-omitted"],
    )
    async def test_switching_on_unasked_does_not_sweep(
        self,
        gated_api: AsyncClient,
        session: AsyncSession,
        delay: MagicMock,
        body: dict[str, bool],
    ) -> None:
        """Start no sweep for a write that only corrects ``ENABLED``.

        pmm-managed rewrites the switch whenever it finds ``ENABLED`` differing from
        it, and asks for a sweep only at startup and when the switch turns on; an
        older PMM Server sends no ``scan`` at all.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        :param body: The write, with ``scan`` false or left out.
        """
        response = await gated_api.put(
            f"{BASE}/enabled",
            json=body,
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.ENABLED is True
        delay.assert_not_called()
        assert await ProbeRunManager.list(session) == []

    @pytest.mark.asyncio
    async def test_switching_on_with_auto_scan_off_does_not_sweep(
        self, gated_api: AsyncClient, session: AsyncSession, delay: MagicMock
    ) -> None:
        """Leave the estate alone when an admin stopped automatic scans.

        pmm-managed asks for a sweep at every start, so this is what keeps a restart
        from scanning.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        """
        await store_overrides(session, AUTO_SCAN=False)
        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)

        response = await _switch(gated_api, scan=True)

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.ENABLED is True
        delay.assert_not_called()
        assert await ProbeRunManager.list(session) == []

    @pytest.mark.asyncio
    async def test_switching_off_does_not_sweep(
        self, gated_api: AsyncClient, session: AsyncSession, delay: MagicMock
    ) -> None:
        """Start no sweep when PMM turns the switch off, even if asked for one.

        With ``AUTO_SCAN`` on and a sweep asked for, only ``enabled`` keeps this
        write from sweeping.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        """
        assert om_inventory_settings.AUTO_SCAN is True

        response = await _switch(gated_api, enabled=False, scan=True)

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.ENABLED is False
        delay.assert_not_called()
        assert await ProbeRunManager.list(session) == []

    @pytest.mark.asyncio
    async def test_switching_on_during_a_sweep_starts_no_other(
        self, gated_api: AsyncClient, session: AsyncSession, delay: MagicMock
    ) -> None:
        """Let the sweep in flight stand for the one switching on would start.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        """
        running = await start_run(session, None)

        response = await _switch(gated_api, scan=True)

        assert response.status_code == status.HTTP_200_OK
        delay.assert_not_called()
        assert [run.id for run in await ProbeRunManager.list(session)] == [running.id]

    @pytest.mark.asyncio
    async def test_a_sweep_that_cannot_be_queued_does_not_fail_the_switch(
        self, gated_api: AsyncClient, session: AsyncSession, delay: MagicMock
    ) -> None:
        """Keep the switch on and record the sweep failed when the broker is down.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        :param delay: The broker's stand-in.
        """
        delay.side_effect = KombuOperationalError("Connection refused")

        response = await _switch(gated_api, scan=True)

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.ENABLED is True
        assert [
            (run.status, run.error) for run in await ProbeRunManager.list(session)
        ] == [(ProbeRunStatus.FAILED, QUEUE_UNAVAILABLE_DETAIL)]

    @pytest.mark.asyncio
    async def test_a_sweep_that_cannot_be_recorded_does_not_fail_the_switch(
        self, gated_api: AsyncClient, mocker: MockerFixture, delay: MagicMock
    ) -> None:
        """Keep the switch on when the database refuses the sweep's run.

        :param gated_api: The client passing the real gate.
        :param mocker: Makes the run's save fail as a failed commit does.
        :param delay: The broker's stand-in.
        """
        mocker.patch.object(
            ProbeRunManager, "save", side_effect=HTTPBadRequestException()
        )

        response = await _switch(gated_api, scan=True)

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.ENABLED is True
        delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_switching_off_withdraws_the_sweep(
        self, gated_api: AsyncClient
    ) -> None:
        """Withdraw the periodic sweep once PMM turns the switch back off.

        Every other write sends ``true``, so this is the only case a handler
        ignoring or inverting ``enabled`` would fail.

        :param gated_api: The client passing the real gate.
        """
        schedules = om_inventory_app.periodic_task_schedules
        assert schedules is not None
        assert not isinstance(schedules, list)
        await gated_api.put(
            f"{BASE}/enabled",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )

        response = await gated_api.put(
            f"{BASE}/enabled",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["value"] is False
        assert om_inventory_settings.ENABLED is False
        assert schedules() == []

    @pytest.mark.asyncio
    async def test_a_body_without_the_switch_is_refused(
        self, gated_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a body that omits ``enabled`` rather than default the switch.

        :param gated_api: The client passing the real gate.
        :param session: The database session.
        """
        response = await gated_api.put(
            f"{BASE}/enabled",
            json={},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert om_inventory_settings.ENABLED is DEFAULTS.ENABLED
        assert await _active_rows(session) == []

    @pytest.mark.asyncio
    async def test_a_person_is_refused_even_as_an_admin(
        self, admin_api: AsyncClient, admin_user: CasdoorUser
    ) -> None:
        """Refuse every human caller; an admin uses the settings router instead.

        The shared client stubs the role gate out, so the refusal is the
        service-principal check's own.

        :param admin_api: The authenticated admin client.
        :param admin_user: The admin it is authenticated as.
        """
        extensions_app.dependency_overrides[resolve_bearer] = lambda: admin_user

        response = await admin_api.put(
            f"{BASE}/enabled", json={"enabled": not DEFAULTS.ENABLED}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert om_inventory_settings.ENABLED is DEFAULTS.ENABLED


class TestAutoScanStopsTheScheduleOnly:
    """Let an admin stop the schedule with ``AUTO_SCAN``, leaving PMM's ``ENABLED`` alone."""

    @pytest.mark.asyncio
    async def test_auto_scan_off_unregisters_the_sweep_only(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Drop the periodic sweep while PMM's switch and the schedule stay as they are.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await store_overrides(session, ENABLED=True)
        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)
        schedules = om_inventory_app.periodic_task_schedules
        assert schedules is not None
        assert not isinstance(schedules, list)
        assert [task.name for task in schedules()] == ["extensions__run_om_probe"]

        response = await admin_api.patch(ADMIN_SETTINGS, json={"AUTO_SCAN": False})

        assert response.status_code == status.HTTP_200_OK
        assert schedules() == []
        assert om_inventory_settings.ENABLED is True
        assert om_inventory_settings.SCHEDULE == DEFAULTS.SCHEDULE


class TestConfigWritesAreNotServedHere:
    """Keep writes on the admin settings router, the one place that gates them."""

    @pytest.mark.asyncio
    async def test_patching_config_is_not_allowed(self, admin_api: AsyncClient) -> None:
        """Refuse ``PATCH /config``; only reads are served on that path.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(f"{BASE}/config", json={"RUN_RETENTION": 20})

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        assert om_inventory_settings.RUN_RETENTION == DEFAULTS.RUN_RETENTION


class TestTheAppIsActuallyWiredIn:
    """Assert the registration a unit test of the endpoint cannot see.

    The settings API answers correctly with none of this in place: a read takes
    override rows straight from the database, and a write republishes the proxy
    snapshot inline, so a request appears to work end to end. What breaks is the *next
    process*: without the collection below the refresher never republishes, and an
    override silently reverts to YAML on restart. Measured happening in the sandbox —
    a 25-minute sweep that went back to 10 after ``./om restart extensions-backend``.
    """

    def test_the_declaration_is_reachable_from_the_package(self) -> None:
        """Require ``APP_OWNED_SETTINGS_CLASSES`` to be re-exported from ``__init__``.

        The collector does ``getattr(import_module(plugin.module_name), ...)`` against
        the app *package*, so a declaration that only exists in
        ``app_owned_settings.py`` is never found.
        """
        entries = collect_app_owned_settings_classes()

        assert OmInventorySettings.__name__ in {
            entry.setting_class for entry in entries
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["SCHEDULE", "ENABLED", "AUTO_SCAN"])
    async def test_a_change_re_seeds_the_beat_row(self, key: str) -> None:
        """Wire ``SCHEDULE``, ``ENABLED`` and ``AUTO_SCAN`` to the beat re-seed callback.

        The proxy holding a new interval is not the same thing as beat running on it:
        beat reads ``celery_periodictask``, which only changes when
        ``_reseed_system_periodic_tasks`` runs. Without this entry a schedule change
        is visible over the API and has no effect on when the sweep actually fires —
        the worst shape a configuration bug can take.

        :param key: The field whose change must re-seed beat.
        """
        original = getattr(extensions_app.state, "override_callbacks", None)
        try:
            async with extensions_overrides_lifespan(FastAPI()):
                callbacks = extensions_app.state.override_callbacks

            assert (OmInventorySettings.__name__, key) in callbacks
        finally:
            extensions_app.state.override_callbacks = original


class TestGetConfig:
    """Read the app's configuration."""

    @pytest.mark.asyncio
    async def test_lists_every_field_not_only_overridden_ones(
        self, api: AsyncClient
    ) -> None:
        """List every field, so "why is it sweeping every 10 minutes" is answerable.

        :param api: The authenticated client.
        """
        response = await api.get(f"{BASE}/config")

        assert response.status_code == status.HTTP_200_OK
        keys = {row["key"] for row in response.json()}
        scalars = set(OmInventorySettings.model_fields) - {"SCHEDULE"}
        assert scalars <= keys

    @pytest.mark.asyncio
    async def test_the_schedule_is_listed_as_leaves_not_as_one_object(
        self, api: AsyncClient
    ) -> None:
        """Split ``SCHEDULE`` into ``SCHEDULE__every`` and ``SCHEDULE__period`` leaves.

        The LIST projection expands any nested-model field into its leaves, so a
        caller reading ``/config`` never sees a key literally named ``SCHEDULE`` —
        the same shape the ``alerts`` app's ``BACKUP_INTERVAL`` produces. Asserted
        because ``PATCH`` accepts *both* spellings (see below) and a reader who
        assumes GET and PATCH share one key set will build a form against a key that
        is not there.

        :param api: The authenticated client.
        """
        rows = {row["key"]: row for row in (await api.get(f"{BASE}/config")).json()}

        assert "SCHEDULE" not in rows
        assert rows["SCHEDULE__every"]["value"] == DEFAULTS.SCHEDULE.every
        assert rows["SCHEDULE__period"]["value"] == "minutes"
        assert rows["SCHEDULE__every"]["has_override"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "bounds"),
        [
            ("TASK_TIMEOUT", (60, 300)),
            ("RUN_RETENTION", (10, 1000)),
            ("STALE_RUN_AFTER", (None, 86400)),
            ("SCHEDULE__every", (None, None)),
        ],
    )
    async def test_each_field_reports_its_inclusive_range(
        self, api: AsyncClient, key: str, bounds: tuple[int | None, int | None]
    ) -> None:
        """Report each field's range, in the unit its value is reported in.

        ``SCHEDULE__every`` has none of its own: its range depends on the period.

        :param api: The authenticated client.
        :param key: The field under test.
        :param bounds: The ``(minimum, maximum)`` expected for it.
        """
        rows = {row["key"]: row for row in (await api.get(f"{BASE}/config")).json()}

        assert (rows[key]["minimum"], rows[key]["maximum"]) == bounds

    @pytest.mark.asyncio
    async def test_credentials_path_is_redacted_for_this_routes_audience(
        self, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redact ``CREDENTIALS_PATH`` to ``None`` here no matter what is deployed.

        This route carries no ``@require_minimum_role``, so its only gate is
        ``IsApiAuthenticated`` — any signed-in PMM Extensions user, not only admins. The same
        settings class is also served at ``/api/extensions/admin/settings``, gated
        ``IsApiAdmin``, so a value that is admin-only there must not be
        viewer-readable here. Setting the field directly on the wrapped instance
        (rather than relying on the default already being ``None``) proves this is
        redaction, not coincidence.

        :param api: The authenticated client.
        :param monkeypatch: Restores the real value after the test.
        """
        monkeypatch.setattr(
            om_inventory_settings, "CREDENTIALS_PATH", "/etc/mongodb_uri_prod"
        )

        rows = {row["key"]: row for row in (await api.get(f"{BASE}/config")).json()}

        assert rows["CREDENTIALS_PATH"]["value"] is None


class TestChangeConfig:
    """Update the app's configuration at runtime, through the admin settings router."""

    @pytest.mark.asyncio
    async def test_a_change_records_the_admin_who_made_it(
        self, admin_api: AsyncClient, admin_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Stamp the row with the admin's own name, not PMM's service principal.

        :param admin_api: The authenticated admin client.
        :param admin_user: The admin it is authenticated as.
        :param session: The database session.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 20})

        rows = await _active_rows(session)
        assert [row.updated_by for row in rows] == [admin_user.username]

    @pytest.mark.asyncio
    async def test_a_schedule_change_takes_effect_on_the_proxy(
        self, admin_api: AsyncClient
    ) -> None:
        """Apply a PATCH to the settings proxy immediately, without a restart.

        The point of the whole registration: the running process reads the new value
        immediately, because the handler republishes the snapshot inline rather than
        waiting for the background refresher.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE": {"every": 20, "period": "minutes"}}
        )

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.SCHEDULE.every == 20  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_a_single_schedule_leaf_can_be_changed_on_its_own(
        self, admin_api: AsyncClient
    ) -> None:
        """Accept the leaf spelling GET returns as a PATCH key too.

        Both forms work, and the asymmetry runs the other way: only the whole-object
        form can express ``null``. A UI driven off the GET key set therefore needs the
        parent key too, which is the one thing it cannot discover from GET.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE__every": 30})

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.SCHEDULE.every == 30  # noqa: PLR2004
        assert om_inventory_settings.SCHEDULE.period == "minutes", (
            "changing one leaf must not reset the other to its default"
        )

    @pytest.mark.asyncio
    async def test_a_null_schedule_unregisters_the_sweep(
        self, admin_api: AsyncClient
    ) -> None:
        """Treat ``SCHEDULE: null`` as a legitimate value, not a missing one.

        It is how an operator says "trigger only", and a nullable union is exactly the
        shape a coercion layer is most likely to mishandle — so it is asserted rather
        than assumed to fall out of the type.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE": None})

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.SCHEDULE is None

    @pytest.mark.asyncio
    async def test_a_change_is_persisted_as_an_override_row(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Persist a change as an override row that survives the process.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 20})

        rows = await _active_rows(session)
        assert [(row.key, row.value) for row in rows] == [("RUN_RETENTION", 20)]

    @pytest.mark.asyncio
    async def test_the_wire_class_name_differs_from_the_stored_token(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Answer with the class ``__name__`` while storing ``setting_class_token``.

        A caller that queries ``settingoverride`` by the ``setting_class`` the
        response handed back finds nothing; the row is keyed by the token.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        token = setting_class_token(OmInventorySettings)
        assert token != OmInventorySettings.__name__

        response = await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 20})

        assert response.status_code == status.HTTP_200_OK
        assert [row["setting_class"] for row in response.json()] == [
            OmInventorySettings.__name__
        ]
        stored = await SettingsOverrideManager.list(
            session, setting_class=token, is_active=True
        )
        assert [row.setting_class for row in stored] == [token]
        assert not await SettingsOverrideManager.list(
            session, setting_class=OmInventorySettings.__name__
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "bound"),
        [(0, "greater than or equal to 1"), (9, "less than or equal to 8")],
    )
    async def test_a_bad_value_is_refused_by_the_field_constraint(
        self, admin_api: AsyncClient, value: int, bound: str
    ) -> None:
        """Enforce the field's range on the way in, not at the next sweep.

        A zero here would make ``MAX_CONCURRENT_PROBES`` a semaphore that never admits
        anyone, and the sweep would hang rather than fail.

        :param admin_api: The authenticated admin client.
        :param value: The value refused.
        :param bound: The part of the refusal naming the bound it breaks.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"MAX_CONCURRENT_PROBES": value}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert bound in response.json()["detail"][0]["msg"]
        assert (
            om_inventory_settings.MAX_CONCURRENT_PROBES
            == DEFAULTS.MAX_CONCURRENT_PROBES
        )

    @pytest.mark.asyncio
    async def test_repo_url_is_restricted_to_http_and_https(
        self, admin_api: AsyncClient
    ) -> None:
        """Refuse a ``file:`` URL rather than handing it to ``urllib.request`` as-is.

        ``REPO_URL`` is fetched by the payload to prove a host can reach Percona's
        repository. A plain ``str`` would accept any scheme ``urllib.request``
        understands, including ``file:`` and ``ftp:``, which is not a repository
        reachability check at all.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"REPO_URL": "file:///etc/passwd"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert om_inventory_settings.REPO_URL == DEFAULTS.REPO_URL

    @pytest.mark.asyncio
    async def test_one_bad_key_rejects_the_whole_batch(
        self, admin_api: AsyncClient
    ) -> None:
        """Write nothing when any key fails, so no partial apply is reachable.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"RUN_RETENTION": 20, "POLL_INTERVAL": -1}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert om_inventory_settings.RUN_RETENTION == DEFAULTS.RUN_RETENTION

    @pytest.mark.asyncio
    async def test_credentials_path_is_not_settable(
        self, admin_api: AsyncClient
    ) -> None:
        """Reject a write to a cold field rather than silently ignoring it.

        ``CREDENTIALS_PATH`` names a file read on every database host and handed to a
        driver as a URI. Overridable, it would widen "configure this app" into "read a
        chosen file across the estate", so it stays YAML/env only.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"CREDENTIALS_PATH": "/etc/passwd"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        hot = hot_field_names(OmInventorySettings)
        assert hot, "no hot fields at all would make the exclusion below vacuous"
        assert "CREDENTIALS_PATH" not in hot

    @pytest.mark.asyncio
    async def test_an_override_can_be_cleared_back_to_the_deployment_value(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """DELETE restores the YAML value, so "no override" stays reachable.

        Without it an operator who changed a value once can only ever change it to
        another one, and whatever the deployment ships becomes unrecoverable through
        the API.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 20})

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/RUN_RETENTION")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert om_inventory_settings.RUN_RETENTION == DEFAULTS.RUN_RETENTION
        assert not await _active_rows(session)

    @pytest.mark.asyncio
    async def test_clearing_a_field_that_was_never_set_is_not_an_error(
        self, admin_api: AsyncClient
    ) -> None:
        """DELETE is idempotent, so a revert-everything sweep needs no bookkeeping.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.delete(f"{ADMIN_SETTINGS}/RUN_RETENTION")

        assert response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused(self, admin_api: AsyncClient) -> None:
        """Refuse an unknown key rather than storing it as a row nobody reads.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDUEL": 1})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestScheduleBounds:
    """Hold the schedule to its range whichever spelling a PATCH uses.

    PMM writes the two leaves, and a leaf alone says nothing about the interval, so
    each is judged as the schedule it would leave.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("every", [9, 10**400])
    async def test_a_leaf_out_of_range_is_refused_at_that_leaf(
        self, admin_api: AsyncClient, session: AsyncSession, every: int
    ) -> None:
        """Refuse a schedule out of range, naming the range and the key that was sent.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        :param every: The minutes sent, below the floor or too large for a float.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE__every": every}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        (entry,) = response.json()["detail"]
        assert entry["loc"] == ["body", "SCHEDULE__every"]
        assert "from 10 minutes to 1 day" in entry["msg"]
        assert await _stored_overrides(session) == {}

    @pytest.mark.asyncio
    async def test_seconds_are_refused(self, admin_api: AsyncClient) -> None:
        """Refuse a schedule in seconds, which the periodic-task UI cannot edit.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE__period": "seconds"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "Invalid period 'seconds'" in response.json()["detail"][0]["msg"]

    @pytest.mark.asyncio
    async def test_both_leaves_are_judged_together(
        self, admin_api: AsyncClient
    ) -> None:
        """Accept 2 hours, though 2 minutes would be refused on the way there.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE__every": 2, "SCHEDULE__period": "hours"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert (
            IntervalSchedule(every=2, period=Period.HOURS)
            == om_inventory_settings.SCHEDULE
        )

    @pytest.mark.asyncio
    async def test_a_leaf_is_judged_against_a_stored_sibling(
        self, admin_api: AsyncClient
    ) -> None:
        """Refuse 30 once hours are stored, though 30 minutes is in range.

        :param admin_api: The authenticated admin client.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE__period": "hours"})

        response = await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE__every": 30})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert (
            IntervalSchedule(every=10, period=Period.HOURS)
            == om_inventory_settings.SCHEDULE
        )

    @pytest.mark.asyncio
    async def test_a_leaf_under_a_null_schedule_merges_onto_the_base(
        self, admin_api: AsyncClient
    ) -> None:
        """Merge a leaf onto the deployment's schedule, as the snapshot does.

        :param admin_api: The authenticated admin client.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE": None})

        response = await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE__every": 30})

        assert response.status_code == status.HTTP_200_OK
        assert (
            IntervalSchedule(every=30, period=Period.MINUTES)
            == om_inventory_settings.SCHEDULE
        )

    @pytest.mark.asyncio
    async def test_a_revert_leaving_the_schedule_out_of_range_is_refused(
        self, admin_api: AsyncClient
    ) -> None:
        """Refuse reverting the period of 2 hours, which would leave 2 minutes.

        :param admin_api: The authenticated admin client.
        """
        await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE__every": 2, "SCHEDULE__period": "hours"}
        )

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/SCHEDULE__period")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["detail"][0]["loc"] == ["body", "SCHEDULE__period"]
        assert (
            IntervalSchedule(every=2, period=Period.HOURS)
            == om_inventory_settings.SCHEDULE
        )

    @pytest.mark.asyncio
    async def test_a_whole_schedule_over_the_ceiling_is_refused(
        self, admin_api: AsyncClient
    ) -> None:
        """Refuse a two-day schedule written as one object.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE": {"every": 2, "period": "days"}}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


async def _stored_overrides(session: AsyncSession) -> dict[str, Any]:
    """Return this class's active override rows as ``{key: value}``.

    :param session: The database session.
    :return: The stored overrides.
    """
    return {row.key: row.value for row in await _active_rows(session)}


def _cross_field_fields(response_json: dict[str, Any]) -> list[list[str]]:
    """Return the field pairs of every cross-field relationship a 422 body reports.

    A broken relationship yields one entry per field it spans, so the pairs are
    deduplicated in order.

    :param response_json: The decoded 422 response.
    :return: One list of field names per violated relationship.
    """
    pairs: list[list[str]] = []
    for entry in response_json["detail"]:
        if entry["type"] == "cross_field" and entry["ctx"]["fields"] not in pairs:
            pairs.append(entry["ctx"]["fields"])
    return pairs


class TestPatchCrossFieldRules:
    """Judge a PATCH by the probe-timing relationships, not field by field.

    Each field already passes its own range in these cases; only the combination
    is wrong, which is exactly what per-key validation cannot see.
    """

    @pytest.mark.asyncio
    async def test_raising_the_timeout_to_a_stored_threshold_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Judge a one-field PATCH against the other field's stored override.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"STALE_RUN_AFTER": 200})

        response = await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 250})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["STALE_RUN_AFTER", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {"STALE_RUN_AFTER": 200}
        assert om_inventory_settings.TASK_TIMEOUT == DEFAULTS.TASK_TIMEOUT

    @pytest.mark.asyncio
    async def test_probe_timeouts_over_half_a_stored_timeout_are_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a repository timeout that leaves the probe no time to report.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 60})

        response = await admin_api.patch(ADMIN_SETTINGS, json={"REPO_TIMEOUT": 30})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["REPO_TIMEOUT", "CONNECT_TIMEOUT", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {"TASK_TIMEOUT": 60}

    @pytest.mark.asyncio
    async def test_a_stale_threshold_inside_the_timeout_is_refused(
        self, admin_api: AsyncClient
    ) -> None:
        """Refuse a threshold that could only ever reap a probe still in flight.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"STALE_RUN_AFTER": 1})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["STALE_RUN_AFTER", "TASK_TIMEOUT"]
        ]

    @pytest.mark.asyncio
    async def test_the_refusal_names_both_fields_and_the_relationship(
        self, admin_api: AsyncClient
    ) -> None:
        """Say which two fields disagree and how, without echoing the values.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"STALE_RUN_AFTER": 137})

        assert {entry["msg"] for entry in response.json()["detail"]} == {
            "STALE_RUN_AFTER, TASK_TIMEOUT: STALE_RUN_AFTER must exceed TASK_TIMEOUT"
        }
        assert "137" not in response.text

    @pytest.mark.asyncio
    async def test_the_refusal_is_located_on_each_field_of_the_relationship(
        self, admin_api: AsyncClient
    ) -> None:
        """Point an entry at every field, so whichever one the operator edited shows it.

        The settings UI shows a 422 next to a field only when an entry's ``loc``
        names that field.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"STALE_RUN_AFTER": 1})

        assert [entry["loc"] for entry in response.json()["detail"]] == [
            ["body", "STALE_RUN_AFTER"],
            ["body", "TASK_TIMEOUT"],
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "batch",
        [
            {"TASK_TIMEOUT": 60, "CONNECT_TIMEOUT": 28, "REPO_TIMEOUT": 2},
            {"STALE_RUN_AFTER": 120, "TASK_TIMEOUT": 60},
            {
                "STALE_RUN_AFTER": 120,
                "CONNECT_TIMEOUT": 28,
                "TASK_TIMEOUT": 60,
                "REPO_TIMEOUT": 2,
            },
        ],
        ids=["probe-timeouts-and-timeout", "lower-timeout-and-stale", "all-four"],
    )
    async def test_a_consistent_batch_is_accepted_whatever_the_key_order(
        self, admin_api: AsyncClient, batch: dict[str, int]
    ) -> None:
        """Judge a batch by its end state; some single-key orders would fail midway.

        :param admin_api: The authenticated admin client.
        :param batch: Fields moved together into a consistent combination.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json=batch)

        assert response.status_code == status.HTTP_200_OK
        assert batch["TASK_TIMEOUT"] == om_inventory_settings.TASK_TIMEOUT

    @pytest.mark.asyncio
    async def test_an_inconsistent_batch_writes_nothing(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse the whole batch, including its keys outside any relationship.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS,
            json={"RUN_RETENTION": 20, "STALE_RUN_AFTER": 100, "TASK_TIMEOUT": 200},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert await _stored_overrides(session) == {}
        assert om_inventory_settings.RUN_RETENTION == DEFAULTS.RUN_RETENTION

    @pytest.mark.asyncio
    async def test_a_nested_key_does_not_shield_a_broken_rule_in_its_batch(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Judge the rule fields of a batch that also sets a schedule leaf.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE__every": 15, "STALE_RUN_AFTER": 1}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["STALE_RUN_AFTER", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {}

    @pytest.mark.asyncio
    async def test_a_per_field_error_is_reported_without_cross_field_noise(
        self, admin_api: AsyncClient
    ) -> None:
        """Skip the relationship check when a value is not even valid on its own.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"POLL_INTERVAL": 9999, "TASK_TIMEOUT": -1}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == []

    @pytest.mark.asyncio
    async def test_a_field_outside_the_relationships_is_unaffected(
        self, admin_api: AsyncClient
    ) -> None:
        """Leave a PATCH that touches no rule field alone.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"POLL_INTERVAL": 30})

        assert response.status_code == status.HTTP_200_OK


class TestDeleteCrossFieldRules:
    """Refuse a revert that would leave the relationships broken."""

    @pytest.mark.asyncio
    async def test_reverting_a_field_into_an_inconsistent_set_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Keep the override when its YAML value would undercut the other fields.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        batch = {"TASK_TIMEOUT": 60, "CONNECT_TIMEOUT": 28, "REPO_TIMEOUT": 2}
        await admin_api.patch(ADMIN_SETTINGS, json=batch)

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/REPO_TIMEOUT")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["REPO_TIMEOUT", "CONNECT_TIMEOUT", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == batch
        assert om_inventory_settings.REPO_TIMEOUT == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_the_other_order_reverts_both(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Leave a way back: reverting the dependent field first always works.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(
            ADMIN_SETTINGS, json={"TASK_TIMEOUT": 60, "STALE_RUN_AFTER": 120}
        )

        first = await admin_api.delete(f"{ADMIN_SETTINGS}/STALE_RUN_AFTER")
        second = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert (first.status_code, second.status_code) == (
            status.HTTP_204_NO_CONTENT,
            status.HTTP_204_NO_CONTENT,
        )
        assert await _stored_overrides(session) == {}

    @pytest.mark.asyncio
    async def test_reverting_a_rule_field_that_has_no_override_is_still_idempotent(
        self, admin_api: AsyncClient
    ) -> None:
        """Answer 204 when the field already holds its consistent YAML value.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.asyncio
    async def test_reverting_a_nested_leaf_is_unaffected(
        self, admin_api: AsyncClient
    ) -> None:
        """Skip the check for a ``__``-delimited key no relationship names.

        :param admin_api: The authenticated admin client.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE__every": 30})

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/SCHEDULE__every")

        assert response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.asyncio
    async def test_a_stale_threshold_override_cannot_be_orphaned(
        self, admin_api: AsyncClient
    ) -> None:
        """Refuse reverting a lowered timeout under a stale threshold that needs it.

        :param admin_api: The authenticated admin client.
        """
        await admin_api.patch(
            ADMIN_SETTINGS, json={"TASK_TIMEOUT": 60, "STALE_RUN_AFTER": 120}
        )

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert timedelta(seconds=120) == om_inventory_settings.STALE_RUN_AFTER


class TestRulesJudgedAgainstStoredRows:
    """Judge a change against the stored overrides, not this process's snapshot.

    The snapshot can lag a write from another worker and omits stored rows it
    ignored, so judging against it accepts a change the next snapshot then drops,
    and the operator's 200 means nothing.
    """

    @pytest.mark.asyncio
    async def test_a_patch_is_judged_against_a_row_this_process_has_not_seen(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a timeout at a stale threshold another worker just stored.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await store_overrides(session, STALE_RUN_AFTER=200)
        assert om_inventory_settings.STALE_RUN_AFTER == DEFAULTS.STALE_RUN_AFTER

        response = await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 250})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["STALE_RUN_AFTER", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {"STALE_RUN_AFTER": 200}

    @pytest.mark.asyncio
    async def test_a_revert_is_judged_against_a_row_this_process_has_not_seen(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a revert that would orphan a stale threshold stored elsewhere.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await store_overrides(session, TASK_TIMEOUT=60, STALE_RUN_AFTER=120)

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert await _stored_overrides(session) == {
            "TASK_TIMEOUT": 60,
            "STALE_RUN_AFTER": 120,
        }


class TestChangesWhileStoredRowsAreIgnored:
    """Keep the API usable, and honest, while a stored pair is being ignored."""

    @pytest_asyncio.fixture(autouse=True)
    async def _ignored_pair(self, session: AsyncSession) -> None:
        """Store a pair that breaks the stale rule and publish it, so both are ignored.

        Either value is consistent with the other's YAML value, so either revert works.

        :param session: The database session.
        """
        await store_overrides(session, STALE_RUN_AFTER=200, TASK_TIMEOUT=250)
        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)
        assert om_inventory_settings.TASK_TIMEOUT == DEFAULTS.TASK_TIMEOUT

    @pytest.mark.asyncio
    async def test_get_reports_why_an_ignored_override_is_ignored(
        self, api: AsyncClient
    ) -> None:
        """Name the broken rule on both halves of the pair, beside the base value.

        ``has_override`` still reflects the stored row, so the reset stays offered;
        ``override_ignored_reason`` is what says the snapshot does not apply it.

        :param api: The authenticated client.
        """
        response = await api.get(f"{BASE}/config")

        assert response.status_code == status.HTTP_200_OK
        rows = {row["key"]: row for row in response.json()}
        assert rows["TASK_TIMEOUT"]["value"] == DEFAULTS.TASK_TIMEOUT
        for key in ("STALE_RUN_AFTER", "TASK_TIMEOUT"):
            assert rows[key]["has_override"] is True
            assert rows[key]["override_ignored_reason"] == STALE_RULE.message
        assert rows["POLL_INTERVAL"]["override_ignored_reason"] is None

    @pytest.mark.asyncio
    async def test_the_read_issues_one_override_query(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Judge the pair from the rows already listed for provenance, not a re-read.

        :param api: The authenticated client.
        :param session: The database session.
        """
        statements: list[str] = []

        def _record(
            conn: object, cursor: object, statement: str, *args: object
        ) -> None:
            statements.append(statement)

        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", _record)
        try:
            response = await api.get(f"{BASE}/config")
        finally:
            event.remove(bind, "before_cursor_execute", _record)

        assert response.status_code == status.HTTP_200_OK
        assert len([s for s in statements if "settingoverride" in s.lower()]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "rule"),
        [
            ({"TASK_TIMEOUT": 220}, ["STALE_RUN_AFTER", "TASK_TIMEOUT"]),
            ({"STALE_RUN_AFTER": 240}, ["STALE_RUN_AFTER", "TASK_TIMEOUT"]),
        ],
        ids=[
            "still-at-or-above-the-stored-threshold",
            "still-inside-the-stored-timeout",
        ],
    )
    async def test_a_value_the_next_snapshot_would_ignore_is_refused(
        self,
        admin_api: AsyncClient,
        session: AsyncSession,
        change: dict[str, int],
        rule: list[str],
    ) -> None:
        """Refuse a value that would be stored only to be ignored with the pair.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        :param change: The one-field PATCH.
        :param rule: The fields of the relationship it would break.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json=change)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [rule]
        assert await _stored_overrides(session) == {
            "STALE_RUN_AFTER": 200,
            "TASK_TIMEOUT": 250,
        }

    @pytest.mark.asyncio
    async def test_a_field_outside_the_broken_rule_can_still_be_set(
        self, admin_api: AsyncClient
    ) -> None:
        """Leave unrelated fields writable rather than blocking on the pair.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 20})

        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_fixing_one_side_restores_the_other(
        self, admin_api: AsyncClient
    ) -> None:
        """Accept the repair, after which the stored timeout applies again.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS,
            json={"STALE_RUN_AFTER": DEFAULTS.STALE_RUN_AFTER.total_seconds()},
        )

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.TASK_TIMEOUT == 250  # noqa: PLR2004

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["STALE_RUN_AFTER", "TASK_TIMEOUT"])
    async def test_either_side_can_be_reverted(
        self, admin_api: AsyncClient, key: str
    ) -> None:
        """Let a revert clear the pair, since the result is consistent either way.

        :param admin_api: The authenticated admin client.
        :param key: The side being reverted.
        """
        response = await admin_api.delete(f"{ADMIN_SETTINGS}/{key}")

        assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.fixture
def _mock_tasks_api() -> Iterator[None]:
    """Stub the Tasks client the shared LIST proxies ``TasksSettings`` through.

    Pops only its own key, since ``admin_api``'s teardown clears the rest.
    """
    mock = AsyncMock(spec=RemoteAPI)
    mock.get.return_value = {
        "groups": [{"setting_class": "TasksSettings", "settings": []}]
    }
    extensions_app.dependency_overrides[get_tasks_api] = lambda: mock
    yield
    extensions_app.dependency_overrides.pop(get_tasks_api, None)


class TestOverrideIgnoredAfterBaseChange:
    """Mark a lone override a redeployed base value leaves breaking a rule.

    The base timeout is raised past the stored stale threshold, so the snapshot
    ignores the threshold while the timeout, carrying no override, is not marked.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def stored_row(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> SettingOverride:
        """Raise the base timeout, then store and publish a threshold below it.

        The base is patched first: ``monkeypatch`` records the old value through
        the proxy, which would read a published override instead.

        :param session: The database session.
        :param monkeypatch: Restores the base timeout after the test.
        :return: The stored threshold row.
        """
        monkeypatch.setattr(om_inventory_settings, "TASK_TIMEOUT", 300)
        await store_overrides(session, actor="operator", STALE_RUN_AFTER=240)
        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)
        (row,) = await _active_rows(session)
        return row

    def _assert_ignored_threshold(
        self, entry: dict[str, Any], row: SettingOverride
    ) -> None:
        """Assert the threshold reads as an ignored override, stamps intact.

        :param entry: The ``STALE_RUN_AFTER`` response entry.
        :param row: The stored threshold row.
        """
        assert entry["has_override"] is True
        assert entry["updated_by"] == "operator"
        assert datetime.fromisoformat(entry["updated_at"]) == make_datetime_utc(
            row.updated_at
        )
        assert entry["value"] == DEFAULTS.STALE_RUN_AFTER.total_seconds()
        assert entry["override_ignored_reason"] == STALE_RULE.message

    @pytest.mark.asyncio
    async def test_the_app_config_marks_only_the_override(
        self, api: AsyncClient, stored_row: SettingOverride
    ) -> None:
        """Mark the threshold, and leave the un-overridden timeout unmarked.

        :param api: The authenticated client.
        :param stored_row: The stored threshold row.
        """
        response = await api.get(f"{BASE}/config")

        assert response.status_code == status.HTTP_200_OK
        rows = {row["key"]: row for row in response.json()}
        self._assert_ignored_threshold(rows["STALE_RUN_AFTER"], stored_row)
        assert rows["TASK_TIMEOUT"]["override_ignored_reason"] is None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_tasks_api")
    async def test_the_shared_list_marks_it(
        self, admin_api: AsyncClient, stored_row: SettingOverride
    ) -> None:
        """Carry the mark on the admin settings LIST.

        :param admin_api: The authenticated admin client.
        :param stored_row: The stored threshold row.
        """
        response = await admin_api.get("/api/extensions/admin/settings/")

        assert response.status_code == status.HTTP_200_OK
        (group,) = [
            group
            for group in response.json()["groups"]
            if group["setting_class"] == OmInventorySettings.__name__
        ]
        (entry,) = [e for e in group["settings"] if e["key"] == "STALE_RUN_AFTER"]
        self._assert_ignored_threshold(entry, stored_row)

    @pytest.mark.asyncio
    async def test_the_shared_detail_marks_it(
        self, admin_api: AsyncClient, stored_row: SettingOverride
    ) -> None:
        """Carry the mark on the admin settings detail read.

        :param admin_api: The authenticated admin client.
        :param stored_row: The stored threshold row.
        """
        response = await admin_api.get(f"{ADMIN_SETTINGS}/STALE_RUN_AFTER")

        assert response.status_code == status.HTTP_200_OK
        self._assert_ignored_threshold(response.json(), stored_row)


class TestIgnoredOverrideAttribution:
    """Name the rule that dropped each override, and nothing for an honoured one."""

    @pytest.mark.asyncio
    async def test_a_cascade_names_only_the_rule_that_dropped_each_field(
        self,
        api: AsyncClient,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Leave the stale rule off the timeout, whose override satisfies it.

        Against a base of repo and connect timeouts of 30 and a task timeout of
        300, the stored task timeout of 100 breaks the probe rule and goes;
        restoring 300 then breaks the stale rule for the stored threshold of 200.

        :param api: The authenticated client.
        :param session: The database session.
        :param monkeypatch: Restores the base values after the test.
        """
        monkeypatch.setattr(om_inventory_settings, "REPO_TIMEOUT", 30)
        monkeypatch.setattr(om_inventory_settings, "CONNECT_TIMEOUT", 30)
        monkeypatch.setattr(om_inventory_settings, "TASK_TIMEOUT", 300)
        await store_overrides(session, TASK_TIMEOUT=100, STALE_RUN_AFTER=200)
        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)

        response = await api.get(f"{BASE}/config")

        assert response.status_code == status.HTTP_200_OK
        rows = {row["key"]: row for row in response.json()}
        assert rows["TASK_TIMEOUT"]["override_ignored_reason"] == PROBE_RULE.message
        assert rows["STALE_RUN_AFTER"]["override_ignored_reason"] == (
            STALE_RULE.message
        )

    @pytest.mark.asyncio
    async def test_an_honoured_override_is_unmarked(
        self, api: AsyncClient, admin_api: AsyncClient
    ) -> None:
        """Report no reason for an override the snapshot applies.

        :param api: The authenticated client.
        :param admin_api: The authenticated admin client.
        """
        patched = await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 240})
        assert patched.status_code == status.HTTP_200_OK

        response = await api.get(f"{BASE}/config")

        assert response.status_code == status.HTTP_200_OK
        entry = {row["key"]: row for row in response.json()}["TASK_TIMEOUT"]
        assert entry["has_override"] is True
        assert entry["override_ignored_reason"] is None

    @pytest.mark.asyncio
    async def test_a_field_no_rule_names_is_not_decoded_on_read(
        self,
        api: AsyncClient,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Leave an unusable row outside every rule to the refresher's warning.

        Judging the rules reads only the fields they name, so a read does not
        repeat, once per request, the loader's warning for any other bad row.

        :param api: The authenticated client.
        :param session: The database session.
        :param caplog: Fixture capturing log records.
        """
        await store_overrides(session, RUN_RETENTION="not-a-number")

        with caplog.at_level(logging.WARNING, logger=cache.logger.name):
            response = await api.get(f"{BASE}/config")

        assert response.status_code == status.HTTP_200_OK
        assert "RUN_RETENTION" not in caplog.text


class TestStaleRunAfterRoundTrip:
    """Keep every accepted threshold re-submittable exactly as GET returns it.

    The field reads back in whole seconds, so a margin that rounding removes
    would come back as a value the same PATCH then refuses.
    """

    @pytest.mark.asyncio
    async def test_a_margin_the_api_rounds_away_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a threshold that would read back as the timeout itself.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"STALE_RUN_AFTER": DEFAULTS.TASK_TIMEOUT + 0.5}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["STALE_RUN_AFTER", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {}

    @pytest.mark.asyncio
    async def test_an_accepted_threshold_patches_back_as_read(
        self, admin_api: AsyncClient
    ) -> None:
        """Accept the value GET returns for a fractional threshold PATCH accepted.

        :param admin_api: The authenticated admin client.
        """
        accepted = await admin_api.patch(
            ADMIN_SETTINGS, json={"STALE_RUN_AFTER": DEFAULTS.TASK_TIMEOUT + 0.6}
        )
        rows = {
            row["key"]: row for row in (await admin_api.get(f"{BASE}/config")).json()
        }

        response = await admin_api.patch(
            ADMIN_SETTINGS,
            json={"STALE_RUN_AFTER": rows["STALE_RUN_AFTER"]["value"]},
        )

        assert (accepted.status_code, response.status_code) == (
            status.HTTP_200_OK,
            status.HTTP_200_OK,
        )


class TestStoredRowsOutsideTheBounds:
    """Ignore rows stored before the bounds, so the deployment's value applies."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rows", "field"),
        [
            ({"TASK_TIMEOUT": 1000}, "TASK_TIMEOUT"),
            ({"SCHEDULE__every": 10, "SCHEDULE__period": "seconds"}, "SCHEDULE"),
        ],
        ids=["timeout-over-the-ceiling", "schedule-in-seconds"],
    )
    async def test_a_stored_value_out_of_range_is_ignored(
        self, session: AsyncSession, rows: dict[str, Any], field: str
    ) -> None:
        """Drop a stored value outside its range so the YAML/env value applies.

        The honoured override beside it still applies, so the refresh did not fail.

        :param session: The database session.
        :param rows: The override rows stored before the bounds.
        :param field: The field they override.
        """
        await store_overrides(session, RUN_RETENTION=20, **rows)

        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)

        assert getattr(om_inventory_settings, field) == getattr(DEFAULTS, field)
        assert om_inventory_settings.RUN_RETENTION == 20  # noqa: PLR2004

    def test_a_stored_schedule_too_large_for_a_float_is_ignored(self) -> None:
        """Drop the schedule instead of failing the whole refresh.

        Built from a row in memory: SQLite's JSON round trip turns the value into
        ``inf``, which coercion refuses before the schedule's own check runs.
        """
        row = SettingOverride(
            setting_class=setting_class_token(OmInventorySettings),
            key="SCHEDULE__every",
            value=10**400,
        )

        stored = cache.stored_overrides_from_rows(OmInventorySettings, [row], DEFAULTS)

        assert "SCHEDULE" not in stored

    @pytest.mark.asyncio
    async def test_one_leaf_cannot_mend_a_schedule_stored_in_seconds(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a leaf that would leave the stored period in seconds.

        Accepted, it would store a schedule the snapshot keeps ignoring.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await store_overrides(session, SCHEDULE__every=10, SCHEDULE__period="seconds")

        alone = await admin_api.patch(ADMIN_SETTINGS, json={"SCHEDULE__every": 20})
        both = await admin_api.patch(
            ADMIN_SETTINGS, json={"SCHEDULE__every": 20, "SCHEDULE__period": "minutes"}
        )

        assert alone.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "Invalid period 'seconds'" in alone.json()["detail"][0]["msg"]
        assert both.status_code == status.HTTP_200_OK
        assert (
            IntervalSchedule(every=20, period=Period.MINUTES)
            == om_inventory_settings.SCHEDULE
        )
