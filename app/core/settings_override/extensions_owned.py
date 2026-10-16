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

"""Refresh the PMM Extensions-owned shared settings classes in any process.

``Settings`` and ``AlertSettings`` back the module-level ``settings`` and
``alert_settings`` proxies every service shares. Their override rows live in the
PMM Extensions database, whose settings API is their only writer, so every
process that honours them reads that database: the PMM Extensions web process
and worker children through their own refreshers, and the standalone Tasks and
Inventory processes through :func:`extensions_owned_overrides_refresher`. This
module holds the one definition of those classes and of the rebinds they need,
so the processes cannot drift apart, and lives under ``app/core`` so the
standalone services reach it without importing ``app/extensions``.

Not re-exported from the package ``__init__``: ``app.core.config`` imports
``app.core.settings_override.registry``, which runs that ``__init__``, and this
module imports ``app.core.config``.
"""

import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import cast

from app.core.alerts.config import alert_settings, AlertSettings
from app.core.config import ExtensionsDatabaseSettings, PMMSettings, Settings, settings
from app.core.db.config import DatabaseOptions
from app.core.db.utils import (
    create_app_async_engine,
    get_async_session_maker_from_engine,
)
from app.core.settings_override.constants import ALERT_SETTINGS, SETTINGS
from app.core.settings_override.lifecycle import (
    CallbackRegistry,
    fire_on_boot,
    previous_or_base,
    ProxyEntry,
    ProxyRegistry,
    settings_override_refresher,
    SnapshotChange,
)

logger = logging.getLogger(__name__)

#: Wall-clock budget in seconds for the boot seed against the PMM Extensions
#: database. The standalone services do not own that database, so an
#: unreachable one must not hold their startup for the driver's connect timeout.
EXTENSIONS_OWNED_SEED_TIMEOUT = 5.0


async def invalidate_pmm_clients(change: SnapshotChange) -> None:
    """Evict cached PMM clients on the previous and current endpoints after a ``PMM`` override.

    Evicts the ordered de-duplicated set of previous-and-current endpoints so a
    same-endpoint change (credentials, SSL) collapses to a single eviction, while
    an endpoint change also drops the client keyed by the endpoint no longer in
    use. The next :class:`PMMSyncer` key-misses to a fresh client via its
    ``default_factory`` PMM read. When ``PMM`` is absent from ``change.previous``
    (override created), :func:`previous_or_base` supplies the YAML/env value.

    :param change: The override snapshots on either side of the republish.
    """
    previous_pmm = cast("PMMSettings", previous_or_base(change, settings, "PMM"))
    for endpoint in dict.fromkeys(
        str(pmm.endpoint)
        for pmm in (previous_pmm, settings.PMM)
        if pmm.endpoint is not None
    ):
        await settings.invalidate_client(endpoint)


@fire_on_boot
async def apply_logging_dictconfig(_: SnapshotChange) -> None:
    """Re-apply ``logging.config.dictConfig`` after a global ``LOGGING`` override.

    ``LOGGING`` is a HOT field, but ``LOGGING_CONFIG`` (the dict handed to
    ``dictConfig``) is not: the override snapshot replaces only the ``LOGGING``
    key, so ``settings.LOGGING_CONFIG`` still carries the level baked in by the
    ``set_log_level`` model validator at construction time. This callback mirrors
    that validator: it injects the now-live ``settings.LOGGING`` into a copy of
    the config and re-applies it, so a log-level change takes effect without a
    restart in every process that registers it: the PMM Extensions web process,
    the Celery worker children, and the standalone Tasks and Inventory
    processes. Failures are logged and swallowed: a malformed config must not
    take the process down mid-request or mid-task. ``LOGGING_CONFIG`` sets
    ``disable_existing_loggers: False``, so re-entering ``dictConfig`` from a
    worker refresh cycle leaves the loggers Celery created at runtime enabled,
    and re-creates the ones the config names, ``celery`` among them, with
    their handlers.

    Marked ``fire_on_boot`` because the boot-time ``dictConfig`` call reads the
    process-wide ``LOGGING_CONFIG``, which no snapshot carries: a process that
    starts with a ``LOGGING`` override already stored would otherwise keep the
    YAML/env level until the override next changed.

    :param _: The snapshots on either side of the republish (unused; the
        level is re-read from the proxy).
    """
    try:
        config = deepcopy(settings.LOGGING_CONFIG)
        config["loggers"][""]["level"] = settings.LOGGING
        config["loggers"]["app"]["level"] = settings.LOGGING
        logging.config.dictConfig(config)
    except Exception:
        logger.exception("Failed to re-apply logging config after LOGGING override")


#: The PMM Extensions-owned shared classes, keyed by class name, bound to the
#: module-level proxies every service reads.
EXTENSIONS_OWNED_PROXIES: ProxyRegistry = {
    SETTINGS: ProxyEntry(settings, Settings),
    ALERT_SETTINGS: ProxyEntry(alert_settings, AlertSettings),
}

#: The rebinds the shared classes need in any process that publishes them.
#: ``invalidate_pmm_clients`` is registered because ``ClientRegistry.IMMUTABLE_KEYS``
#: excludes ``api_key``: a same-endpoint credential override would otherwise
#: refresh the snapshot while cached clients keep the old key.
#: ``apply_logging_dictconfig`` re-enters ``dictConfig`` so a HOT ``LOGGING``
#: override replaces the boot-time levels.
EXTENSIONS_OWNED_CALLBACKS: CallbackRegistry = {
    (SETTINGS, "PMM"): invalidate_pmm_clients,
    (SETTINGS, "LOGGING"): apply_logging_dictconfig,
}


def extensions_override_database() -> DatabaseOptions:
    """Return the PMM Extensions database options, capped at one pooled connection.

    The refresher holds one session per cycle, so a single connection is all it
    needs, and the cap keeps the extra engine inside a shared connection budget
    whatever ``EXTENSIONS__DATABASE__POOL_SIZE`` sizes the service's own pool to.

    :return: The PMM Extensions database options with ``POOL_SIZE`` 1 and
        ``MAX_OVERFLOW`` 0.
    :raises ValidationError: When the resolved ``EXTENSIONS__DATABASE__*``
        values do not validate.
    """
    return ExtensionsDatabaseSettings().DATABASE.model_copy(
        update={"POOL_SIZE": 1, "MAX_OVERFLOW": 0}
    )


@asynccontextmanager
async def extensions_owned_overrides_refresher(
    *setting_classes: str,
) -> AsyncGenerator[None, None]:
    """Refresh the named PMM Extensions-owned classes from the PMM Extensions database.

    Builds an engine of its own for the lifespan and disposes it on exit, so a
    process that never enters this (the combined ``app.main:app``, whose PMM
    Extensions refresher already publishes these classes) never builds it. The
    seed is bounded by :data:`EXTENSIONS_OWNED_SEED_TIMEOUT`: on expiry the
    lifespan yields on whatever the seed published, YAML/env when the database
    is unreachable, and the periodic loop applies the overrides once it answers.

    :param setting_classes: Keys of :data:`EXTENSIONS_OWNED_PROXIES` to refresh.
    :return: An async context manager yielding ``None`` for the lifespan duration.
    :raises KeyError: When a name is not a PMM Extensions-owned class.
    :raises ValidationError: When the resolved ``EXTENSIONS__DATABASE__*``
        values do not validate.
    """
    proxies = {name: EXTENSIONS_OWNED_PROXIES[name] for name in setting_classes}
    engine = create_app_async_engine(extensions_override_database())
    try:
        session_maker = get_async_session_maker_from_engine(engine)
        async with settings_override_refresher(
            lambda: session_maker,
            proxies,
            callbacks=EXTENSIONS_OWNED_CALLBACKS,
            seed_timeout=EXTENSIONS_OWNED_SEED_TIMEOUT,
        ):
            yield
    finally:
        await engine.dispose()
