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

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import get_current_user as resolve_bearer
from app.api.deps import SERVICE_PRINCIPAL
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.config import settings
from app.core.settings_override.lifecycle import publish_snapshot
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token, SettingOverride
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field_names
from app.extensions.apps.framework.registry import collect_app_owned_settings_classes
from app.extensions.apps.om_inventory.app import app as om_inventory_app
from app.extensions.apps.om_inventory.config import (
    om_inventory_settings,
    OmInventorySettings,
)
from app.extensions.deps import get_session
from app.extensions.main import extensions_app, extensions_overrides_lifespan
from tests.app.extensions.apps.om_inventory.conftest import (
    BASE,
    BEYOND_TIMEDELTA_SECONDS,
)

#: The YAML-configured values these tests assert against, taken from the class so a
#: default change moves the assertions with it rather than silently passing.
DEFAULTS = OmInventorySettings()

#: The shared, admin-gated settings router's path for this class.
ADMIN_SETTINGS = f"/api/extensions/admin/settings/{OmInventorySettings.__name__}"

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


class TestSetEnabled:
    """Follow PMM's Operations for MongoDB switch through ``PUT /enabled``."""

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
        response = await gated_api.put(
            f"{BASE}/enabled",
            json={"enabled": not DEFAULTS.ENABLED},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )

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
            response = await gated_api.put(
                f"{BASE}/enabled",
                json={"enabled": not DEFAULTS.ENABLED},
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
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

        await gated_api.put(
            f"{BASE}/enabled",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )

        assert [task.name for task in schedules()] == ["extensions__run_om_probe"]

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


class TestConfigWritesAreNotServedHere:
    """Keep writes on the admin settings router, the one place that gates them."""

    @pytest.mark.asyncio
    async def test_patching_config_is_not_allowed(self, admin_api: AsyncClient) -> None:
        """Refuse ``PATCH /config``; only reads are served on that path.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(f"{BASE}/config", json={"RUN_RETENTION": 5})

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
    @pytest.mark.parametrize("key", ["SCHEDULE", "ENABLED"])
    async def test_a_change_re_seeds_the_beat_row(self, key: str) -> None:
        """Wire ``SCHEDULE`` and ``ENABLED`` to the beat re-seed callback.

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
        await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 5})

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
            ADMIN_SETTINGS, json={"SCHEDULE": {"every": 2, "period": "minutes"}}
        )

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.SCHEDULE.every == 2  # noqa: PLR2004

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
        await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 5})

        rows = await _active_rows(session)
        assert [(row.key, row.value) for row in rows] == [("RUN_RETENTION", 5)]

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

        response = await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 5})

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
    async def test_a_bad_value_is_refused_by_the_field_constraint(
        self, admin_api: AsyncClient
    ) -> None:
        """Enforce ``PositiveInt`` on the way in, not at the next sweep.

        A zero here would make ``MAX_CONCURRENT_PROBES`` a semaphore that never admits
        anyone, and the sweep would hang rather than fail.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"MAX_CONCURRENT_PROBES": 0}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
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
            ADMIN_SETTINGS, json={"RUN_RETENTION": 5, "POLL_INTERVAL": -1}
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
        await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 5})

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

    Each field already passes its own ``PositiveInt`` bound in these cases; only the
    combination is wrong, which is exactly what per-key validation cannot see.
    """

    @pytest.mark.asyncio
    async def test_lowering_the_timeout_below_a_stored_poll_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Judge a one-field PATCH against the other field's stored override.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(ADMIN_SETTINGS, json={"POLL_INTERVAL": 100})

        response = await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 90})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["POLL_INTERVAL", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {"POLL_INTERVAL": 100}
        assert om_inventory_settings.TASK_TIMEOUT == DEFAULTS.TASK_TIMEOUT

    @pytest.mark.asyncio
    async def test_a_timeout_below_the_deployment_poll_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Judge a one-field PATCH against the other field's YAML value too.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"TASK_TIMEOUT": DEFAULTS.POLL_INTERVAL - 1}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert await _stored_overrides(session) == {}

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
        response = await admin_api.patch(ADMIN_SETTINGS, json={"POLL_INTERVAL": 9999})

        assert {entry["msg"] for entry in response.json()["detail"]} == {
            "POLL_INTERVAL, TASK_TIMEOUT: POLL_INTERVAL must not exceed TASK_TIMEOUT"
        }
        assert "9999" not in response.text

    @pytest.mark.asyncio
    async def test_the_refusal_is_located_on_each_field_of_the_relationship(
        self, admin_api: AsyncClient
    ) -> None:
        """Point an entry at every field, so whichever one the operator edited shows it.

        The settings UI shows a 422 next to a field only when an entry's ``loc``
        names that field.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 1})

        assert [entry["loc"] for entry in response.json()["detail"]] == [
            ["body", "POLL_INTERVAL"],
            ["body", "TASK_TIMEOUT"],
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "batch",
        [
            {"POLL_INTERVAL": 300, "TASK_TIMEOUT": 600},
            {"TASK_TIMEOUT": 30, "STALE_RUN_AFTER": 60},
            {"POLL_INTERVAL": 20, "TASK_TIMEOUT": 30, "STALE_RUN_AFTER": 60},
        ],
        ids=["raise-poll-and-timeout", "lower-timeout-and-stale", "all-three"],
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
            json={"RUN_RETENTION": 5, "POLL_INTERVAL": 60, "TASK_TIMEOUT": 30},
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
            ADMIN_SETTINGS, json={"SCHEDULE__every": 5, "TASK_TIMEOUT": 1}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["POLL_INTERVAL", "TASK_TIMEOUT"]
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
        response = await admin_api.patch(ADMIN_SETTINGS, json={"CONNECT_TIMEOUT": 600})

        assert response.status_code == status.HTTP_200_OK


class TestDeleteCrossFieldRules:
    """Refuse a revert that would leave the relationships broken."""

    @pytest.mark.asyncio
    async def test_reverting_a_field_into_an_inconsistent_set_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Keep the override when its YAML value would undercut the other field.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(
            ADMIN_SETTINGS, json={"POLL_INTERVAL": 300, "TASK_TIMEOUT": 600}
        )

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["POLL_INTERVAL", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {
            "POLL_INTERVAL": 300,
            "TASK_TIMEOUT": 600,
        }
        assert om_inventory_settings.TASK_TIMEOUT == 600  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_the_other_order_reverts_both(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Leave a way back: reverting the dependent field first always works.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await admin_api.patch(
            ADMIN_SETTINGS, json={"POLL_INTERVAL": 300, "TASK_TIMEOUT": 600}
        )

        first = await admin_api.delete(f"{ADMIN_SETTINGS}/POLL_INTERVAL")
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
            ADMIN_SETTINGS, json={"TASK_TIMEOUT": 30, "STALE_RUN_AFTER": 60}
        )

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert timedelta(seconds=60) == om_inventory_settings.STALE_RUN_AFTER


async def _store(session: AsyncSession, **values: Any) -> None:
    """Write override rows straight to the table, as another worker would.

    The proxy's snapshot is left as it was, which is what this process sees until
    its next refresh.

    :param session: The database session.
    :param values: The overrides to store, by field name.
    """
    for key, value in values.items():
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=setting_class_token(OmInventorySettings),
                key=key,
                value=value,
            ),
        )


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
        """Refuse a timeout below a poll interval another worker just stored.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await _store(session, POLL_INTERVAL=100)
        assert om_inventory_settings.POLL_INTERVAL == DEFAULTS.POLL_INTERVAL

        response = await admin_api.patch(ADMIN_SETTINGS, json={"TASK_TIMEOUT": 90})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["POLL_INTERVAL", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {"POLL_INTERVAL": 100}

    @pytest.mark.asyncio
    async def test_a_revert_is_judged_against_a_row_this_process_has_not_seen(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a revert that would orphan a poll interval stored elsewhere.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        await _store(session, POLL_INTERVAL=300, TASK_TIMEOUT=600)

        response = await admin_api.delete(f"{ADMIN_SETTINGS}/TASK_TIMEOUT")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert await _stored_overrides(session) == {
            "POLL_INTERVAL": 300,
            "TASK_TIMEOUT": 600,
        }


class TestChangesWhileStoredRowsAreIgnored:
    """Keep the API usable, and honest, while a stored pair is being ignored."""

    @pytest_asyncio.fixture(autouse=True)
    async def _ignored_pair(self, session: AsyncSession) -> None:
        """Store a pair that breaks the poll rule and publish it, so both are ignored.

        :param session: The database session.
        """
        await _store(session, POLL_INTERVAL=90, TASK_TIMEOUT=30)
        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)
        assert om_inventory_settings.TASK_TIMEOUT == DEFAULTS.TASK_TIMEOUT

    @pytest.mark.asyncio
    async def test_get_reports_the_value_in_effect_for_an_ignored_override(
        self, api: AsyncClient
    ) -> None:
        """Show the base value in effect, while the row still counts as an override.

        Pins today's report: ``has_override`` reflects the stored row, not whether
        the snapshot honours it, so the value is the only sign it is ignored.

        :param api: The authenticated client.
        """
        rows = {row["key"]: row for row in (await api.get(f"{BASE}/config")).json()}

        assert rows["TASK_TIMEOUT"]["value"] == DEFAULTS.TASK_TIMEOUT
        assert rows["TASK_TIMEOUT"]["has_override"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "rule"),
        [
            ({"TASK_TIMEOUT": 60}, ["POLL_INTERVAL", "TASK_TIMEOUT"]),
            ({"STALE_RUN_AFTER": 100}, ["STALE_RUN_AFTER", "TASK_TIMEOUT"]),
        ],
        ids=["still-below-the-stored-poll", "inside-the-restored-timeout"],
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
            "POLL_INTERVAL": 90,
            "TASK_TIMEOUT": 30,
        }

    @pytest.mark.asyncio
    async def test_a_field_outside_the_broken_rule_can_still_be_set(
        self, admin_api: AsyncClient
    ) -> None:
        """Leave unrelated fields writable rather than blocking on the pair.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(ADMIN_SETTINGS, json={"RUN_RETENTION": 5})

        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_fixing_one_side_restores_the_other(
        self, admin_api: AsyncClient
    ) -> None:
        """Accept the repair, after which the stored timeout applies again.

        :param admin_api: The authenticated admin client.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"POLL_INTERVAL": DEFAULTS.POLL_INTERVAL}
        )

        assert response.status_code == status.HTTP_200_OK
        assert om_inventory_settings.TASK_TIMEOUT == 30  # noqa: PLR2004

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["POLL_INTERVAL", "TASK_TIMEOUT"])
    async def test_either_side_can_be_reverted(
        self, admin_api: AsyncClient, key: str
    ) -> None:
        """Let a revert clear the pair, since the result is consistent either way.

        :param admin_api: The authenticated admin client.
        :param key: The side being reverted.
        """
        response = await admin_api.delete(f"{ADMIN_SETTINGS}/{key}")

        assert response.status_code == status.HTTP_204_NO_CONTENT


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


class TestTimeoutBeyondTheDurationRange:
    """Answer a timeout too large for ``timedelta`` with a refusal, not a crash."""

    @pytest.mark.asyncio
    async def test_a_patch_is_refused(
        self, admin_api: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse it through the stale-run relationship and store nothing.

        :param admin_api: The authenticated admin client.
        :param session: The database session.
        """
        response = await admin_api.patch(
            ADMIN_SETTINGS, json={"TASK_TIMEOUT": BEYOND_TIMEDELTA_SECONDS}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _cross_field_fields(response.json()) == [
            ["STALE_RUN_AFTER", "TASK_TIMEOUT"]
        ]
        assert await _stored_overrides(session) == {}

    @pytest.mark.asyncio
    async def test_a_stored_row_is_ignored(self, session: AsyncSession) -> None:
        """Drop such a row from the snapshot so the YAML/env timeout applies.

        :param session: The database session.
        """
        await _store(session, TASK_TIMEOUT=BEYOND_TIMEDELTA_SECONDS)

        proxy = cast("OverridableSettingsProxy", om_inventory_settings)
        await publish_snapshot(proxy, session, OmInventorySettings)

        assert om_inventory_settings.TASK_TIMEOUT == DEFAULTS.TASK_TIMEOUT
