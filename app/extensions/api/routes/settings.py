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

"""Compose the PMM Extensions sub-app's settings REST API."""

__all__ = ["EXTENSIONS_ADMIN_SETTINGS_CLASSES", "router"]

from dataclasses import dataclass, field
from typing import Annotated, Any

import yaml
from fastapi import HTTPException, Query
from fastapi.responses import Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.alerts.config import alert_settings, AlertSettings
from app.core.auth import config as auth_config
from app.core.config import BaseYamlSettings, Settings, settings
from app.core.exceptions import HTTPBadGatewayException, HTTPBadRequestException
from app.core.settings_override.api import (
    build_settings_class_values,
    build_settings_router,
)
from app.core.settings_override.api.routes import ClassEntry
from app.core.settings_override.constants import (
    ALERT_SETTINGS,
    EXTENSIONS_SETTINGS,
    SETTINGS,
    SNIPPETS_SETTINGS,
    TASKS_SETTINGS,
)
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import FieldMetadata
from app.core.utils.date_time import utc_now
from app.extensions.api.openapi import UPSTREAM_TASKS_502_RESPONSE
from app.extensions.apps.framework.registry import (
    collect_app_owned_settings_classes,
    resolve_app_settings_metadata,
)
from app.extensions.config import extensions_settings, ExtensionsSettings
from app.extensions.deps import (
    ApiAdminUsername,
    IsApiAdmin,
    RequireBearerForUnsafeMethods,
    SessionDep,
    TaskAPI,
)
from app.extensions.snippets.config import snippets_settings, SnippetsSettings

# TasksSettings is owned by the Tasks sub-app, so PMM Extensions proxies it server-side
# through ``tasks_api`` (mounted at ``/admin/settings``) rather than registering
# it as a local class. The React Settings page reaches it via ``/api/extensions``
# only.
EXTENSIONS_ADMIN_SETTINGS_CLASSES: list[ClassEntry] = [
    (EXTENSIONS_SETTINGS, ExtensionsSettings, extensions_settings),
    (SNIPPETS_SETTINGS, SnippetsSettings, snippets_settings),
    (ALERT_SETTINGS, AlertSettings, alert_settings),
    # The global ``Settings`` class is refreshed only by the PMM Extensions web process, so
    # its override-eligible fields (e.g. ``PMM``, ``LOGGING``) are exposed here.
    (SETTINGS, Settings, settings),
]


def _extensions_setting_applicable(cls: str, field: FieldMetadata) -> bool:
    """Determine whether a PMM Extensions setting applies under the active runtime state.

    ``AMBIENT_SESSION_SSO_ENABLED`` applies only under an ambient-capable auth
    provider (Grafana); every other field is unconditionally applicable.

    :param cls: The settings class the field belongs to.
    :param field: The introspected field metadata.
    :return: Whether the field applies under the active auth provider.
    """
    if cls == EXTENSIONS_SETTINGS and field.key == "AMBIENT_SESSION_SSO_ENABLED":
        return auth_config.get_active_auth_provider().supports_ambient_session
    return True


EXTENSIONS_APP_OWNED_SETTINGS_CLASSES = collect_app_owned_settings_classes()

router = build_settings_router(
    classes=EXTENSIONS_ADMIN_SETTINGS_CLASSES,
    session_dep=SessionDep,
    admin_dep=IsApiAdmin,
    actor_dep=ApiAdminUsername,
    mutation_deps=[RequireBearerForUnsafeMethods],
    remote_classes=[(TASKS_SETTINGS, "/admin/settings")],
    remote_api_dep=TaskAPI,
    applicability=_extensions_setting_applicable,
    app_owned_classes=EXTENSIONS_APP_OWNED_SETTINGS_CLASSES,
    resolve_app_metadata=resolve_app_settings_metadata,
)


def _tasks_settings_groups(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a Tasks ``SettingsListResponse`` JSON body to export class blocks.

    :param payload: Parsed JSON from ``GET /admin/settings/`` on the Tasks API.
    :type payload: dict[str, Any]
    :return: One ``{key: value}`` mapping per ``setting_class`` group in the
        payload.
    :rtype: dict[str, dict[str, Any]]
    :raises HTTPBadGatewayException: If ``payload`` is missing a ``groups`` list,
        any group is malformed, or any setting entry lacks a ``key`` or ``value``.
    """
    groups = payload.get("groups")
    if not isinstance(groups, list):
        msg = "Tasks settings LIST response missing 'groups'."
        raise HTTPBadGatewayException(detail=msg)

    export: dict[str, dict[str, Any]] = {}
    for group in groups:
        if not isinstance(group, dict):
            raise HTTPBadGatewayException(
                detail="Tasks settings LIST group is not an object.",
            )
        setting_class = group.get("setting_class")
        settings = group.get("settings")
        if not isinstance(setting_class, str):
            raise HTTPBadGatewayException(
                detail="Tasks settings LIST group missing 'setting_class'.",
            )
        if not isinstance(settings, list):
            raise HTTPBadGatewayException(
                detail=f"Tasks settings LIST group {setting_class!r} missing 'settings'.",
            )
        class_block: dict[str, Any] = {}
        for entry in settings:
            if not isinstance(entry, dict):
                raise HTTPBadGatewayException(
                    detail="Tasks settings LIST entry is not an object.",
                )
            key = entry.get("key")
            if not isinstance(key, str):
                raise HTTPBadGatewayException(
                    detail="Tasks settings LIST entry missing 'key'.",
                )
            if "value" not in entry:
                raise HTTPBadGatewayException(
                    detail=f"Tasks settings LIST entry missing 'value' for key {key!r}.",
                )
            class_block[key] = entry["value"]
        export[setting_class] = class_block
    return export


@dataclass
class _ClassRequest:
    """Hold the parsed export selectors targeting a single settings class.

    ``whole`` records that a bare ``Class`` selector was seen (keep every key in
    the output). ``keys`` collects every ``Class.KEY`` selector seen for the
    class. Both are tracked independently so a whole-class selector can dominate
    *output* (emit all keys) while each named key is *still validated* for
    existence -- a typo'd sibling key must fail even when its class is also
    requested whole (AC 6).

    :param whole: Whether a whole-class selector was requested.
    :param keys: The set of explicitly named keys requested for the class.
    """

    whole: bool = False
    keys: set[str] = field(default_factory=set)


def _parse_export_selectors(
    keys: list[str],
    allowed_classes: set[str],
) -> dict[str, _ClassRequest]:
    """Parse and validate export ``keys`` selectors into a per-class request map.

    Each selector is either ``Class.KEY`` (a single key within a settings class)
    or a bare ``Class`` (the whole class). Splitting on the first ``.`` is safe
    because nested leaves use the ``__`` delimiter, never ``.`` (so
    ``ExtensionsSettings.PMM__endpoint`` resolves to class ``ExtensionsSettings`` and key
    ``PMM__endpoint``). Class names are validated here against ``allowed_classes``
    so a typo fails before any value is collected or any upstream call is made;
    per-key existence is validated later against each class's built key set.

    A whole-class selector dominates the *output* for its class (every key is
    emitted) but does not suppress validation of any named sibling selector: a
    ``Class.KEY`` is always recorded so its existence is checked later, even when
    a bare ``Class`` selector is also present. Overlapping or duplicate
    selectors are otherwise benign and never error.

    :param keys: The raw, repeatable ``keys`` query values.
    :param allowed_classes: The set of wired settings-class names a selector may
        reference (core PMM Extensions classes, app-owned classes, and ``TasksSettings``).
    :return: A mapping from class name to its parsed :class:`_ClassRequest`.
    :raises HTTPBadRequestException: If a selector is blank, malformed (empty
        class or empty key), or names a class that is not wired -- the detail
        names the offending selector.
    """
    requested: dict[str, _ClassRequest] = {}
    for selector in keys:
        stripped = selector.strip()
        class_part, dot, key_part = stripped.partition(".")
        class_name = class_part.strip()
        key = key_part.strip()
        if not class_name or (dot and not key):
            raise HTTPBadRequestException(detail=f"Invalid selector: {selector!r}")
        if class_name not in allowed_classes:
            raise HTTPBadRequestException(detail=f"Unknown selector: {stripped}")
        req = requested.setdefault(class_name, _ClassRequest())
        if not dot:
            req.whole = True
        else:
            req.keys.add(key)
    return requested


def _filter_class_block(
    class_name: str,
    block: dict[str, Any],
    requested: dict[str, _ClassRequest],
) -> dict[str, Any]:
    """Narrow one class block to the requested keys, validating each named key.

    Every named key in the request is validated against the block's keys --
    raising on the first miss so a typo cannot silently drop a key from an
    otherwise complete-looking export -- *regardless of* whether a whole-class
    selector is also present. A whole-class request then returns ``block``
    unchanged; a key-only request keeps just the named keys in the block's own
    emitted order.

    :param class_name: The settings-class name this block belongs to.
    :type class_name: str
    :param block: The full ``{key: value}`` map built for the class.
    :type block: dict[str, Any]
    :param requested: The parsed selector map.
    :type requested: dict[str, _ClassRequest]
    :return: The block narrowed to the requested keys.
    :rtype: dict[str, Any]
    :raises HTTPBadRequestException: If a named key does not exist on the class
        -- the detail names the offending ``Class.KEY`` selector.
    """
    req = requested[class_name]
    for key in req.keys:
        if key not in block:
            raise HTTPBadRequestException(
                detail=f"Unknown selector: {class_name}.{key}"
            )
    if req.whole:
        return block
    return {key: value for key, value in block.items() if key in req.keys}


def _wired_export_class_names() -> set[str]:
    """Return every settings-class name the export endpoint may emit.

    :return: Core PMM Extensions, app-owned, and proxied Tasks class names.
    :rtype: set[str]
    """
    names = {name for name, _, _ in EXTENSIONS_ADMIN_SETTINGS_CLASSES}
    names.update(entry.setting_class for entry in EXTENSIONS_APP_OWNED_SETTINGS_CLASSES)
    names.add(TASKS_SETTINGS)
    return names


async def _append_local_class_export(
    payload: dict[str, dict[str, Any]],
    *,
    session: AsyncSession,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    requested: dict[str, _ClassRequest] | None,
) -> None:
    """Append one locally-wired settings class block to an export payload.

    :param payload: The export payload being assembled in canonical order.
    :param session: The active database session for PMM Extensions override queries.
    :param setting_class: The settings class identifier.
    :param settings_cls: The Pydantic settings model class.
    :param proxy: The live override proxy for the class.
    :param requested: Parsed export selectors, or ``None`` for a full export.
    """
    class_name = str(setting_class)
    if requested is not None and class_name not in requested:
        return
    block = await build_settings_class_values(
        session=session,
        setting_class=class_name,
        settings_cls=settings_cls,
        proxy=proxy,
    )
    if requested is not None:
        block = _filter_class_block(class_name, block, requested)
    payload[class_name] = block


async def _append_tasks_export_block(
    payload: dict[str, dict[str, Any]],
    *,
    tasks_api: TaskAPI,
    tasks_key: str,
    requested: dict[str, _ClassRequest] | None,
) -> None:
    """Append the proxied ``TasksSettings`` block to an export payload.

    :param payload: The export payload being assembled in canonical order.
    :param tasks_api: The Tasks API client used to fetch ``TasksSettings``.
    :param tasks_key: The ``TasksSettings`` class name.
    :param requested: Parsed export selectors, or ``None`` for a full export.
    :raises HTTPBadGatewayException: If the upstream Tasks LIST call fails or
        returns an unexpected payload shape.
    """
    if requested is not None and tasks_key not in requested:
        return
    try:
        tasks_payload = await tasks_api.get("/admin/settings/")
    except (HTTPException, OSError) as exc:
        detail = getattr(exc, "detail", str(exc))
        raise HTTPBadGatewayException(detail=str(detail)) from exc

    if not isinstance(tasks_payload, dict):
        raise HTTPBadGatewayException(
            detail="Tasks settings LIST returned an unexpected payload.",
        )

    tasks_groups = _tasks_settings_groups(tasks_payload)
    if tasks_key not in tasks_groups:
        raise HTTPBadGatewayException(
            detail=f"Tasks settings LIST response missing {tasks_key!r} group.",
        )
    tasks_block = tasks_groups[tasks_key]
    if requested is not None:
        tasks_block = _filter_class_block(tasks_key, tasks_block, requested)
    payload[tasks_key] = tasks_block


def _export_yaml_response(payload: dict[str, dict[str, Any]]) -> Response:
    """Serialize an export payload as a YAML download response.

    :param payload: The merged ``{class_name: {key: value}}`` export body.
    :return: YAML bytes with ``Content-Disposition`` set for download.
    :rtype: Response
    """
    yaml_body = yaml.safe_dump(
        payload,
        default_flow_style=False,
        sort_keys=False,
    )
    filename = f"pmm-extensions-config-{utc_now():%Y-%m-%d}.yaml"
    return Response(
        content=yaml_body,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/export",
    responses=UPSTREAM_TASKS_502_RESPONSE,
)
async def export_settings(
    session: SessionDep,
    tasks_api: TaskAPI,
    keys: Annotated[list[str] | None, Query()] = None,
) -> Response:
    """Return the merged effective configuration as a YAML attachment.

    Aggregates the PMM Extensions wired core settings classes and app-owned settings
    classes locally and fans out to ``GET /admin/settings/`` on the Tasks API
    for ``TasksSettings``. Values use the same dump path as the settings LIST
    endpoints. On upstream failure, re-raise as
    :class:`~app.core.exceptions.HTTPBadGatewayException` — no partial export.

    When ``keys`` is omitted the full merged export is returned exactly as
    before. When provided, each entry is a fully-qualified selector
    (``Class.KEY`` or a bare ``Class``); the response is narrowed to only the
    requested classes/keys. Class names and PMM Extensions keys are validated with no
    upstream call; the Tasks fan-out is skipped entirely unless a selector
    targets ``TasksSettings``, and Tasks keys are validated against the fetched
    block. Output blocks always follow the canonical declaration order
    (``EXTENSIONS_ADMIN_SETTINGS_CLASSES``, then app-owned classes, then
    ``TasksSettings``), independent of selector order.

    :param session: The active database session for PMM Extensions override queries.
    :param tasks_api: The Tasks API client used to fetch ``TasksSettings``.
    :param keys: Optional, repeatable selectors restricting the export to a
        subset of classes/keys. ``None`` (omitted) means the full export.
    :return: YAML bytes with ``Content-Disposition`` set for download.
    :raises HTTPBadRequestException: If a selector is blank, malformed, names an
        unwired class, or names a key that does not exist on its class.
    :raises HTTPBadGatewayException: If the Tasks settings LIST call fails
        with an ``HTTPException`` (e.g. an upstream non-2xx response), an
        ``OSError`` (e.g. a connection failure), an unexpected payload shape,
        or a missing ``TasksSettings`` group.
    """
    requested = (
        _parse_export_selectors(keys, _wired_export_class_names())
        if keys is not None
        else None
    )

    payload: dict[str, dict[str, Any]] = {}
    for setting_class, settings_cls, proxy in EXTENSIONS_ADMIN_SETTINGS_CLASSES:
        await _append_local_class_export(
            payload,
            session=session,
            setting_class=setting_class,
            settings_cls=settings_cls,
            proxy=proxy,
            requested=requested,
        )
    for entry in EXTENSIONS_APP_OWNED_SETTINGS_CLASSES:
        await _append_local_class_export(
            payload,
            session=session,
            setting_class=entry.setting_class,
            settings_cls=entry.settings_cls,
            proxy=entry.proxy,
            requested=requested,
        )
    await _append_tasks_export_block(
        payload,
        tasks_api=tasks_api,
        tasks_key=TASKS_SETTINGS,
        requested=requested,
    )
    return _export_yaml_response(payload)
