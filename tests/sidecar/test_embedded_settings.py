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
"""Verify the baked PMM-embedded settings profile."""

import re
from pathlib import Path
from typing import Any

import pytest
from aioresponses import aioresponses
from fastapi import status
from sqlalchemy_celery_beat.models import Period

from app import BASE_DIR
from app.core.auth.config import AuthSettings
from app.core.config import Settings
from app.core.requests import RemoteAPI
from app.core.utils import import_var
from app.extensions.api.routes.settings import EXTENSIONS_ADMIN_SETTINGS_CLASSES
from app.extensions.apps.framework.registry import (
    build_app_registry,
    collect_app_owned_settings_classes,
)
from app.extensions.bundle_upload.plan import (
    ConnectionDetail,
    DeliveryPlanExecutor,
    SecretValue,
)
from app.extensions.config import ExtensionsSettings, SyncOptions
from app.extensions.routes.artifacts import collect_base_dirs
from app.extensions.snippets.constants import ARTIFACT_TYPE_SNIPPET
from app.extensions.sync.syncers.pmm import PMMSyncer
from app.extensions.sync.syncers.system_facts.syncer import (
    SystemFactsSyncer,
    UnmeasuredHostFactsSyncer,
)
from app.inventory.config import InventorySettings
from app.inventory.settings.routes import INVENTORY_ADMIN_SETTINGS_CLASSES
from app.tasks.config import TasksSettings
from app.tasks.settings.routes import TASKS_ADMIN_SETTINGS_CLASSES
from tests.app.extensions.conftest import REDUCED_ACTIVATION
from tests.sidecar.conftest import (
    EMBEDDED_PROFILE,
    read_allowlist,
    SETTINGS_ENV_HELPER,
    SIDECAR_DIR,
)

PASSWORD_BEARING_USERINFO = re.compile(r"://[^/@\s]+:[^/@\s]+@")
"""Match a URL whose authority carries both a user and a password.

Only an embedded credential is a finding, and a bare user in an authority is
legitimate, so an ``@``-rejecting pattern would be broader than the invariant
this file enforces.
"""

PMM_URL_PREFIX = "/extensions"
"""The mount prefix PMM hardcodes in the ``location`` block it ships for PMM Extensions.

Fixed topology rather than a per-deployment input, so the profile and the
healthcheck are both held against this one literal.
"""

PLACEHOLDER_MARKER = re.compile(r"glsa_|__[A-Z_]+__")
SECRET_KEYS = frozenset({"password", "service_account_token", "api_key"})

SECRET_MAP_KEYS = frozenset({"secrets"})
"""Keys whose whole sub-mapping is secret-valued, whatever its members are named.

``DIAGNOSTICS_DELIVERY.secrets`` names its credentials after the receiver's own
fields (``sn_api_key``, ``client_token``), which :data:`SECRET_KEYS` would walk
past, so the block is matched by its container instead.
"""

SHARED_DATABASE_NAME = "pmm_extensions"
"""The one database PMM's ``PMM_ENABLE_EXTENSIONS`` provisions for all three services."""

EMBEDDED_WORKER_CONCURRENCY = 4
"""Prefork children the baked profile pins the side-car's Celery worker to.

The profile's connection-budget arithmetic multiplies each child's engine
ceilings by this, so an unpinned worker (one child per host CPU) would void it.
"""

EMBEDDED_POOL_SIZING = {"POOL_SIZE": 3, "MAX_OVERFLOW": 2, "POOL_TIMEOUT": 10.0}
"""The pool keys the profile writes into its shared database block."""

ALLOWLIST_SIZE = 25
"""How many entries the embedded override allowlist ships.

``sidecar/settings.yaml``'s ``ALLOWED_KEYS`` is the list itself. A field missing
from it is not hot-reloadable through the profile, whether the caller is a UI
request or pmm-managed's own settings sync.

Pinned so a silently truncated list -- which the policy suite's negative
assertions would still accept -- fails here instead.
"""

#: The inventory-sync cadence the baked profile provisions.
EMBEDDED_INVENTORY_SYNC_MINUTES = 15

UNCOMPARABLE_FIELDS = frozenset({"FASTAPI_ENV"})
"""Fields a dump comparison cannot use.

``FASTAPI_ENV`` is what the comparison varies, so it can never match.
"""

DELIVERY_SECRET_NAME = "sn_api_key"
"""The credential every step of the baked plan carries to the receiver.

Declared empty, so the two read-only steps add no credential of their own and
delivery stays off until an operator supplies inputs.
"""

BAKED_CONNECTION_DETAIL_LABELS = [
    "Account name",
    "Account number",
    "Key",
    "ServiceNow user",
    "Active",
    "Expires",
]
"""The labels the baked connection-details step reports its pairs under.

Held in declaration order, which is the order the panel renders them in. A
label dropped from the block costs the panel a row and raises nothing, so the
whole list is pinned rather than counted.
"""

IDENTITY_SCOPED_QUERY = "user=javascript:gs.getUserID()"
"""The encoded query narrowing the ``api_key`` read to the caller's own rows.

Pinned byte-exact because a weakened predicate still answers with a row: the
read would widen to whatever the table's ACL exposes rather than fail.
"""

API_KEY_ROW = {
    "result": [
        {
            "expires": {
                "display_value": "2028-06-04 16:40:12",
                "value": "2028-06-04 16:40:12",
            },
            "name": {
                "display_value": "Percona GAS user",
                "value": "Percona GAS user",
            },
            "user.company.number": {
                "display_value": "ACCT0040479",
                "value": "ACCT0040479",
            },
            "active": {"display_value": "true", "value": "true"},
            "user.name": {
                "display_value": "Percona GAS User",
                "value": "Percona GAS User",
            },
            "user.company.name": {
                "display_value": "Contrativa",
                "value": "Contrativa",
            },
        }
    ]
}
"""One ``api_key`` row as the receiver answered the baked request on perconadev.

Inlined rather than read from the capture it was taken from, which lives under
the gitignored ``env/`` tree. Every field arrives wrapped as
``{display_value, value}`` because the request asks for
``sysparm_display_value=all``, and the row carries no ``token`` or
``token_hash`` because the projection never selects one. The keys are kept in
the receiver's own order, which is neither the projection's nor the panel's.
"""


def uncommented(text: str) -> str:
    """Return ``text`` without its whole-line comments.

    The profile's comments name canonical environment variables such as
    ``TASKS__NOMAD__ENDPOINT``, which a placeholder scan over the raw text would
    read as a ``__MARKER__``.

    :param text: The profile source.
    :return: The source lines that carry configuration.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def secret_valued_leaves(data: Any) -> list[tuple[str, Any]]:
    """Collect every secret-typed key/value pair at any depth of ``data``.

    :param data: A node of the parsed profile.
    :return: The secret-typed keys paired with their configured values.
    """
    if isinstance(data, dict):
        return [
            *(
                (key, value)
                for key, value in data.items()
                if key.lower() in SECRET_KEYS
            ),
            *(
                pair
                for key, value in data.items()
                if key.lower() in SECRET_MAP_KEYS and isinstance(value, dict)
                for pair in value.items()
            ),
            *(pair for value in data.values() for pair in secret_valued_leaves(value)),
        ]
    if isinstance(data, list):
        return [pair for item in data for pair in secret_valued_leaves(item)]
    return []


def projected_field(pointer: str) -> str:
    """Return the response field one baked connection-details pointer addresses.

    :param pointer: A pointer from the baked ``details`` map, shaped
        ``/result/0/<field>/display_value``.
    :return: The field name the pointer walks into.
    :raises ValueError: When the pointer is not that shape. A pointer that stops
        at the field name lands on a container, and one ending at ``value``
        reads the unresolved half, so either drops or degrades its pair in
        silence. An off-shape pointer is a failure here rather than a skip.
    """
    _, root, index, field, terminus = pointer.split("/")
    if (root, index, terminus) != ("result", "0", "display_value"):
        raise ValueError(
            f"{pointer!r} does not address one projected field's display value."
        )
    return field


def resolved_profile() -> dict[str, dict[str, Any]]:
    """Return every prefixed settings class as resolved from the profile.

    :return: The resolved settings, keyed by service.
    """
    return {
        "global": Settings().model_dump(exclude=UNCOMPARABLE_FIELDS),
        "extensions": ExtensionsSettings().model_dump(exclude=UNCOMPARABLE_FIELDS),
        "inventory": InventorySettings().model_dump(exclude=UNCOMPARABLE_FIELDS),
        "tasks": TasksSettings().model_dump(exclude=UNCOMPARABLE_FIELDS),
    }


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_constructs_every_settings_class():
    """Assert every settings class the side-car builds resolves from the profile."""
    assert Settings().CELERY.broker_url
    assert AuthSettings().PROVIDER
    assert ExtensionsSettings().DATABASE.NAME == SHARED_DATABASE_NAME
    assert ExtensionsSettings().DIAGNOSTICS_DELIVERY is not None
    assert InventorySettings().DATABASE.NAME == SHARED_DATABASE_NAME
    assert TasksSettings().NOMAD.endpoint


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_seeds_a_pmm_pinned_inventory_sync_schedule():
    """Assert the profile resolves the values the inventory-sync seeder reads.

    A YAML indentation or key regression would otherwise leave both settings at
    their ``None`` defaults, silently dropping the seeded schedule — or worse,
    keeping the interval and losing the pin, which widens the 15-minute firing
    to every configured syncer.
    """
    settings = TasksSettings()

    assert settings.INVENTORY_SYNC_INTERVAL is not None
    assert (
        settings.INVENTORY_SYNC_INTERVAL.every,
        settings.INVENTORY_SYNC_INTERVAL.period,
    ) == (EMBEDDED_INVENTORY_SYNC_MINUTES, Period.MINUTES)
    assert PMMSyncer.get_name() == settings.INVENTORY_SYNC_SYNCER


def test_profile_writes_pool_sizing_into_the_shared_database(
    embedded_profile_data: dict[str, Any],
):
    """Set pool sizing in the profile, not by inheriting class defaults.

    Resolved settings cannot tell the two apart, so the parsed file is read: a
    later change to ``DatabaseOptions`` defaults must not move the side-car's
    budget silently.

    :param embedded_profile_data: The parsed baked profile.
    """
    shared = embedded_profile_data["default"]["DATABASE"]

    assert {
        key: shared.get(key) for key in EMBEDDED_POOL_SIZING
    } == EMBEDDED_POOL_SIZING


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_pins_worker_concurrency():
    """Pin the prefork concurrency the connection budget assumes."""
    assert Settings().CELERY.worker_concurrency == EMBEDDED_WORKER_CONCURRENCY


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_embedded_profile_sets_only_beat_pool_pre_ping():
    """Leave beat engine sizing unset, since the side-car's beat is not forked.

    One options dict feeds beat's scheduler engine as well as the celery-DB
    engines, and the scheduler is a ``NullPool`` here: ``max_overflow`` would
    crash beat at startup.
    """
    assert Settings().CELERY.beat_engine_options.model_dump(exclude_none=True) == {
        "pool_pre_ping": True
    }


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_pmm_annotations_stay_enabled_without_an_api_key():
    """Assert annotations are configured on, and inert, until a token arrives."""
    settings = Settings()

    assert settings.PMM.annotations_enabled is True
    assert settings.PMM.api_key is None


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_connectivity_check_defaults_to_unchecked():
    """Assert the connectivity checkbox resolves unchecked from the profile.

    The profile allowlists ``CONNECTIVITY_CHECK_DEFAULT`` without setting it, so
    the resolved value is the declared field default -- which a repository
    checkout masks, because its own ``settings.yaml`` supplies one.
    """
    assert ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT is False


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_grafana_provider_constructs_with_an_empty_token():
    """Assert the provider constructs, inert, until ``EXTENSIONS_GRAFANA_TOKEN`` arrives."""
    provider = AuthSettings().PROVIDER["grafana"]

    assert provider.service_account_token.get_secret_value() == ""


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_override_allowlist_resolves_from_the_profile(embedded_profile_data: dict):
    """Assert the profile's YAML list coerces into the field the policy reads."""
    declared = read_allowlist(embedded_profile_data)

    assert len(declared) == ALLOWLIST_SIZE
    assert set(declared) == Settings().SETTINGS_OVERRIDE.ALLOWED_KEYS


def test_profile_carries_a_single_default_block(embedded_profile_data: dict):
    """Assert one block keeps the profile independent of ``FASTAPI_ENV``."""
    assert set(embedded_profile_data) == {"default"}


def test_profile_database_block_is_defined_once(embedded_profile_data: dict):
    """Assert the shared database is anchored once and aliased to every service."""
    default = embedded_profile_data["default"]
    shared = default["DATABASE"]
    assert default["EXTENSIONS"]["DATABASE"] is shared
    assert default["INVENTORY"]["DATABASE"] is shared
    assert default["TASKS"]["DATABASE"] is shared


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_all_services_resolve_the_same_database_connection():
    """Assert Extensions, Inventory, and Tasks read identical connection values from the profile."""
    databases = [
        settings_cls().DATABASE
        for settings_cls in (ExtensionsSettings, InventorySettings, TasksSettings)
    ]
    reference = databases[0]
    for database in databases[1:]:
        assert database.ENGINE == reference.ENGINE
        assert (database.HOST, database.NAME, database.PORT, database.USER) == (
            reference.HOST,
            reference.NAME,
            reference.PORT,
            reference.USER,
        )
    assert (reference.HOST, reference.NAME, reference.PORT, reference.USER) == (
        "pmm-server",
        SHARED_DATABASE_NAME,
        5432,
        SHARED_DATABASE_NAME,
    )


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_every_service_resolves_the_same_pool_sizing():
    """Assert all three services resolve one pool sizing, whatever supplies it.

    Resolved settings cannot tell a profile value from a class default, so the
    sibling test reading the raw profile is what pins where the values come
    from. This one asserts only that the three services agree and that the
    sizing reaches the engine kwargs.
    """
    expected = {key.lower(): value for key, value in EMBEDDED_POOL_SIZING.items()}

    for settings_cls in (ExtensionsSettings, InventorySettings, TasksSettings):
        database = settings_cls().DATABASE
        resolved = {
            "POOL_SIZE": database.POOL_SIZE,
            "MAX_OVERFLOW": database.MAX_OVERFLOW,
            "POOL_TIMEOUT": database.POOL_TIMEOUT,
        }

        assert resolved == EMBEDDED_POOL_SIZING
        assert database.pool_engine_kwargs == {"pool_pre_ping": True, **expected}


def test_global_database_password_reaches_every_service(embedded_profile_cwd: Path):
    """Assert one global password file supplies all three services from the profile."""
    secrets_dir = embedded_profile_cwd / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DATABASE__PASSWORD").write_text("shared-pw", encoding="utf-8")

    for settings_cls in (ExtensionsSettings, InventorySettings, TasksSettings):
        assert (
            settings_cls(_secrets_dir=secrets_dir).DATABASE.PASSWORD.get_secret_value()
            == "shared-pw"
        )


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_exactly_one_auth_provider_is_configured():
    """Reject a second provider copy, which trips the exactly-one-provider check."""
    assert list(AuthSettings().PROVIDER) == ["grafana"]


def test_no_url_carries_a_password():
    """Reject any URL embedding credentials in its authority."""
    profile = uncommented(EMBEDDED_PROFILE.read_text(encoding="utf-8"))

    assert PASSWORD_BEARING_USERINFO.search(profile) is None
    assert PASSWORD_BEARING_USERINFO.search("postgresql://u:p@h/db")


def test_the_profile_configures_no_beat_store():
    """Leave ``BEAT_DBURI`` unset, so the beat store follows the PMM Extensions database.

    A profile value is a configured value and would outrank the derived default,
    handing celery-beat a password-less URI. The assertion reads the uncommented
    text because the comment recording the omission names the key deliberately,
    and pairs with a sibling key so an empty read cannot pass as an absent one.
    """
    profile = uncommented(EMBEDDED_PROFILE.read_text(encoding="utf-8"))

    assert "BEAT_DBURI" not in profile
    assert "RESULT_EXPIRES" in profile


def test_every_secret_typed_field_is_empty(embedded_profile_data: dict):
    """Assert secret-typed keys are present only as empty values."""
    leaves = secret_valued_leaves(embedded_profile_data)

    assert leaves
    assert [(key, value) for key, value in leaves if value != ""] == []


def test_no_placeholder_markers_remain():
    """Reject placeholder markers: the profile is a default, not a template."""
    profile = uncommented(EMBEDDED_PROFILE.read_text(encoding="utf-8"))

    assert PLACEHOLDER_MARKER.search(profile) is None


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_database_password_merges_into_the_profile_block(
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert an environment password lands without displacing its YAML siblings."""
    monkeypatch.setenv("EXTENSIONS__DATABASE__PASSWORD", "pw")

    database = ExtensionsSettings().DATABASE

    assert database.PASSWORD.get_secret_value() == "pw"
    assert (database.HOST, database.NAME, database.USER) == (
        "pmm-server",
        SHARED_DATABASE_NAME,
        SHARED_DATABASE_NAME,
    )


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_grafana_token_merges_into_the_profile_block(monkeypatch: pytest.MonkeyPatch):
    """Assert an environment token lands without displacing its YAML siblings."""
    monkeypatch.setenv("AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN", "glsa_x")

    provider = AuthSettings().PROVIDER["grafana"]

    assert provider.service_account_token.get_secret_value() == "glsa_x"
    assert str(provider.endpoint) == "https://pmm-server:8443/graph"
    assert provider.session_cookie_name == "pmm_session"
    assert provider.verify_ssl is False


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_activation_list_builds_an_app_registry():
    """Assert the baked activation list satisfies every declared app dependency."""
    activated = set(build_app_registry(ExtensionsSettings().APPS).keys())

    assert {"inventory", "atw", "mysql_backups"} <= activated
    assert "snippets" not in activated


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_inventory_activates_without_a_sidebar_entry():
    """Assert the profile's ``SIDEBAR: false`` reaches the bound inventory app.

    PMM owns the embedded inventory UI, so inventory is activated for its
    operator API while ``GET /api/apps/`` advertises no page for it.
    """
    registry = build_app_registry(ExtensionsSettings().APPS)

    assert registry.get("inventory").sidebar is False


def test_profile_declares_the_system_facts_syncer(
    embedded_profile_data: dict[str, Any],
):
    """Assert the embedded profile makes the system-facts collector constructible.

    A syncer absent from ``SYNCERS`` cannot be constructed at all, so this entry is
    the precondition for every other route to the host-capability fact — the seeded
    schedule below included.
    """
    declared = [
        entry["SYNCER"]
        for entry in embedded_profile_data["default"]["EXTENSIONS"]["SYNCERS"]
    ]

    assert declared == [
        "PMMSyncer",
        "MySQLSyncer",
        "SystemFactsSyncer",
        "UnmeasuredHostFactsSyncer",
    ]


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_schedules_the_system_facts_syncer_daily():
    """Assert the collector carries a schedule, not merely a ``SYNCERS`` entry.

    A ``SYNCERS`` entry alone leaves it constructible and reachable by an explicit
    API-triggered sync while never firing on a timer, so verifying only the
    declaration above yields a green config that collects nothing.
    """
    settings = TasksSettings()

    (entry,) = [
        entry
        for entry in settings.INVENTORY_SYNC_SCHEDULES
        if entry.syncer == SystemFactsSyncer.get_name()
    ]
    assert (entry.interval.every, entry.interval.period) == (1, Period.DAYS)


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_measures_a_new_host_within_one_inventory_sync_interval():
    """Assert the first-measurement pass runs as often as the PMM inventory sync.

    A host PMM adds reaches the inventory within one inventory-sync interval, so a
    pass on the same interval starts its first probe within one more. A longer
    pass interval would let a new host wait past the bound the pass exists for.
    """
    settings = TasksSettings()

    (entry,) = [
        entry
        for entry in settings.INVENTORY_SYNC_SCHEDULES
        if entry.syncer == UnmeasuredHostFactsSyncer.get_name()
    ]
    assert settings.INVENTORY_SYNC_INTERVAL is not None
    assert (entry.interval.every, entry.interval.period) == (
        settings.INVENTORY_SYNC_INTERVAL.every,
        settings.INVENTORY_SYNC_INTERVAL.period,
    )


def test_the_short_syncer_name_resolves_to_the_collector():
    """Assert the profile's short syncer name resolves to the collector class.

    ``SyncOptions`` resolves a bare syncer name against ``app.extensions.sync.syncers``
    and ``get_syncers`` imports it from there, so this is what makes the entry
    reachable through a settings override as well as through the baked profile.
    """
    resolved = SyncOptions.model_validate({"syncer": "SystemFactsSyncer"})

    assert import_var(resolved.syncer) is SystemFactsSyncer


def test_the_short_name_resolves_to_the_first_measurement_pass():
    """Assert the profile's short name for the pass resolves through the package."""
    resolved = SyncOptions.model_validate({"syncer": "UnmeasuredHostFactsSyncer"})

    assert import_var(resolved.syncer) is UnmeasuredHostFactsSyncer


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_activation_list_resolves_the_snippet_artifact_type(mocker):
    """Resolve the snippet artifact type from the baked profile's activation list.

    The profile activates atw and no artifact-declaring app, so the type has to
    come from the static map rather than the registry; without it the signed URL
    ATW emits is rejected as an invalid artifact type.
    """
    registry = build_app_registry(ExtensionsSettings().APPS)
    mocker.patch(
        "app.extensions.routes.artifacts.get_app_registry", return_value=registry
    )

    assert ARTIFACT_TYPE_SNIPPET in collect_base_dirs()


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_reduced_activation_mirrors_the_baked_profile():
    """Pin the shared activation constant to the profile it claims to mirror.

    ``REDUCED_ACTIVATION`` stands in for this profile everywhere in the PMM Extensions
    subtree, so a divergence makes those tests assert against a deployment that
    does not exist — which is how an activation-gated artifact-download failure
    stayed invisible to the whole suite while carrying a ``snippets`` entry the
    profile never had.
    """
    assert [app.module_name for app in REDUCED_ACTIVATION] == [
        app.module_name for app in ExtensionsSettings().APPS
    ]


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_uvicorn_ports_match_the_healthcheck_probe():
    """Assert the profile follows the probe's hardcoded ports, which are contract."""
    healthcheck = (SIDECAR_DIR / "healthcheck.sh").read_text(encoding="utf-8")
    probed = re.search(r"for port in \(([^)]*)\)", healthcheck)

    assert probed is not None
    assert [int(port) for port in probed.group(1).split(",")] == [
        ExtensionsSettings().UVICORN_PORT,
        InventorySettings().UVICORN_PORT,
        TasksSettings().UVICORN_PORT,
    ]


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_serves_under_the_prefix_pmm_proxies():
    """Assert the profile mounts PMM Extensions where PMM's nginx drop-in forwards to it."""
    assert ExtensionsSettings().ROOT_PATH == PMM_URL_PREFIX


def test_healthcheck_probes_the_prefix_free_path():
    """Assert the loopback probe stays unprefixed even though the profile sets a prefix.

    Routing tolerates a request that arrives without the prefix, which is what
    lets the probe keep its short path; the HTTP-level proof lives beside the
    other prefixed-routing tests.
    """
    healthcheck = (SIDECAR_DIR / "healthcheck.sh").read_text(encoding="utf-8")
    probed = re.search(
        r"urlopen\(f\"http://127\.0\.0\.1:\{port\}([^\"]*)\"", healthcheck
    )

    assert probed is not None
    assert probed.group(1) == "/health"


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_database_host_and_port_match_the_expansion_defaults():
    """Assert the profile and the expansion state one truth about the server."""
    helper = SETTINGS_ENV_HELPER.read_text(encoding="utf-8")
    database = ExtensionsSettings().DATABASE
    host = re.search(r"EXTENSIONS_DB_HOST:-([^}\"]+)", helper)
    port = re.search(r"EXTENSIONS_DB_PORT:-([^}\"]+)", helper)

    assert host is not None
    assert port is not None
    assert host.group(1) == database.HOST
    assert port.group(1) == str(database.PORT)


def test_profile_is_not_shipped_in_the_shared_bundle():
    """Assert the profile stays a side-car-only copy of the shared bundle."""
    pack_recipe = re.search(
        r"^\s*@?git archive .*$",
        (BASE_DIR / "Makefile").read_text(encoding="utf-8"),
        re.MULTILINE,
    )

    assert pack_recipe is not None
    assert "settings.yaml" not in pack_recipe.group(0)


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_profile_resolves_identically_outside_production_docker(
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert the ``FASTAPI_ENV`` selection has nothing left to change."""
    baked = resolved_profile()
    monkeypatch.setenv("FASTAPI_ENV", "development")

    assert resolved_profile() == baked


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_every_allowlist_entry_names_a_reachable_class(embedded_profile_data: dict):
    """Assert every allowlist class token is reachable across all three services.

    The reachable set is the union of the Extensions, Inventory and Tasks wired classes
    plus the app-owned classes activated by the profile's own activation list.

    :param embedded_profile_data: The parsed baked profile.
    """
    reachable_tokens: set[str] = set()
    for member, _, _ in EXTENSIONS_ADMIN_SETTINGS_CLASSES:
        reachable_tokens.add(str(member))
    for member, _, _ in INVENTORY_ADMIN_SETTINGS_CLASSES:
        reachable_tokens.add(str(member))
    for member, _, _ in TASKS_ADMIN_SETTINGS_CLASSES:
        reachable_tokens.add(str(member))

    profile_apps = ExtensionsSettings().APPS
    for entry in collect_app_owned_settings_classes(profile_apps):
        reachable_tokens.add(str(entry.setting_class))

    allowlist = read_allowlist(embedded_profile_data)
    for key in allowlist:
        class_token = key.split(".")[0]
        assert class_token in reachable_tokens, (
            f"Allowlist entry {key!r} names class {class_token!r} which is not "
            f"reachable in any service under the embedded profile"
        )


@pytest.mark.usefixtures("embedded_profile_cwd")
class TestBakedDeliveryProbeAndConnectionDetails:
    """Cover the two read-only steps the baked delivery plan declares.

    Every assertion observes the plan ``ExtensionsSettings()`` returns rather than the
    profile's text: ``DeliveryPlan`` ignores keys it does not declare, so a
    misspelled block name is dropped in silence and a file-content check would
    pass on a plan carrying neither step.
    """

    def test_the_baked_plan_declares_a_probe(self):
        """Assert the connectivity check has a request to issue."""
        plan = ExtensionsSettings().DIAGNOSTICS_DELIVERY

        assert plan.probe is not None
        assert plan.probe.path == "api/now/table/sn_customerservice_case"

    def test_the_baked_probe_requests_one_identifier(self):
        """Assert the probe reads one row's identifier and nothing else."""
        probe = ExtensionsSettings().DIAGNOSTICS_DELIVERY.probe

        assert probe.query["sysparm_limit"].value == "1"
        assert probe.query["sysparm_fields"].value == "sys_id"

    def test_the_baked_plan_declares_connection_details(self):
        """Assert the connected-state panel has a request to issue."""
        plan = ExtensionsSettings().DIAGNOSTICS_DELIVERY

        assert plan.connection_details is not None
        assert plan.connection_details.path == "api/now/table/api_key"

    def test_the_baked_connection_details_declares_every_label(self):
        """Assert the panel's rows are declared, in the order they render."""
        step = ExtensionsSettings().DIAGNOSTICS_DELIVERY.connection_details

        assert list(step.details) == BAKED_CONNECTION_DETAIL_LABELS

    def test_every_projected_field_is_rendered(self):
        """Assert the projection and the pointer map name the same fields.

        A projected field no pointer addresses is read for nothing; a pointer
        addressing an unprojected field drops its own row in silence.
        """
        step = ExtensionsSettings().DIAGNOSTICS_DELIVERY.connection_details

        projected = step.query["sysparm_fields"].value.split(",")
        addressed = [projected_field(pointer) for pointer in step.details.values()]

        assert sorted(addressed) == sorted(projected)

    def test_the_baked_projection_selects_no_credential_field(self):
        """Assert the request never asks the receiver for the key material.

        The projection is the only thing deciding what the receiver sends, so
        it is the only place the key material can be kept out: a field selected
        here travels back over the wire whatever the pointers later discard.
        """
        step = ExtensionsSettings().DIAGNOSTICS_DELIVERY.connection_details

        projected = step.query["sysparm_fields"].value.split(",")

        assert "name" in projected
        assert "token" not in projected
        assert "token_hash" not in projected

    def test_the_baked_connection_details_scopes_to_the_calling_identity(self):
        """Assert the read is narrowed to the rows this identity owns."""
        step = ExtensionsSettings().DIAGNOSTICS_DELIVERY.connection_details

        assert step.query["sysparm_query"].value == IDENTITY_SCOPED_QUERY

    def test_the_baked_connection_details_requests_display_values(self):
        """Assert reference fields arrive resolved rather than as opaque ids."""
        step = ExtensionsSettings().DIAGNOSTICS_DELIVERY.connection_details

        assert step.query["sysparm_display_value"].value == "all"
        assert step.query["sysparm_limit"].value == "1"

    def test_the_baked_probe_and_details_use_the_declared_secret(self):
        """Assert both steps cite a credential the plan declares."""
        plan = ExtensionsSettings().DIAGNOSTICS_DELIVERY
        headers = [
            plan.probe.headers["x-sn-apikey"],
            plan.connection_details.headers["x-sn-apikey"],
        ]

        assert all(isinstance(header, SecretValue) for header in headers)
        assert {header.name for header in headers} == {DELIVERY_SECRET_NAME}
        assert DELIVERY_SECRET_NAME in plan.secrets

    @pytest.mark.asyncio
    async def test_the_baked_pointers_resolve_against_a_receiver_response(self):
        """Assert the declared pointers report six facts, not an empty panel.

        A step whose pointers all miss is answered as ``available`` carrying no
        pairs, which is the same blank panel an undeclared step leaves, so
        declaring the block is not on its own evidence that it reports anything.
        """
        plan = ExtensionsSettings().DIAGNOSTICS_DELIVERY
        api = RemoteAPI(endpoint=str(plan.endpoint))
        executor = DeliveryPlanExecutor(plan, api)

        with aioresponses() as mock:
            mock.get(
                re.compile(
                    rf"{re.escape(str(plan.endpoint) + plan.connection_details.path)}.*"
                ),
                status=status.HTTP_200_OK,
                payload=API_KEY_ROW,
            )
            async with api:
                details = await executor.read_connection_details()

        assert details == [
            ConnectionDetail(label="Account name", value="Contrativa"),
            ConnectionDetail(label="Account number", value="ACCT0040479"),
            ConnectionDetail(label="Key", value="Percona GAS user"),
            ConnectionDetail(label="ServiceNow user", value="Percona GAS User"),
            ConnectionDetail(label="Active", value="true"),
            ConnectionDetail(label="Expires", value="2028-06-04 16:40:12"),
        ]
