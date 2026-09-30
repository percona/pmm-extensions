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

"""Build a settings REST API router parameterised by sub-app wiring."""

__all__ = [
    "AppOwnedClassEntry",
    "ClassEntry",
    "RemoteClassEntry",
    "apply_class_overrides",
    "build_settings_router",
    "clear_class_override",
    "collect_class_setting_responses",
]

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, params, Request, status
from pydantic import ValidationError
from pydantic.fields import FieldInfo
from sqlalchemy.exc import IntegrityError
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import BaseYamlSettings
from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPConflictException,
    HTTPNotFoundException,
    HTTPUnprocessableEntityException,
)
from app.core.requests.remote_api import RemoteAPI
from app.core.settings_override.api.models import (
    SettingClassAppMetadata,
    SettingClassGroup,
    SettingOption,
    SettingResponse,
    SettingsListResponse,
    SettingsPatch,
)
from app.core.settings_override.constants import NESTED_VALUE_MISSING
from app.core.settings_override.lifecycle import (
    fire_change_callbacks,
    publish_snapshot,
)
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token, SettingOverride
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    chain_has_explicit_not_overridable,
    chain_is_locked,
    coerce_field_value,
    computed_field_info,
    dump_field_value,
    field_materializer,
    FieldMetadata,
    is_hot_reloadable,
    is_nested_overridable_parent,
    iter_class_fields,
    materialize_override_value,
    ReloadClassification,
    rendered_leaf_keys,
    unwrap_secrets_for_storage,
)
from app.core.settings_override.resolution import (
    canonical_override_key,
    override_provenance_for_rows,
    override_rows_for_key,
    resolve_field_in_model,
    resolve_nested_field,
    resolve_nested_field_metadata,
    resolve_nested_value,
    SettingProvenance,
)
from app.core.settings_override.secret_preservation import (
    preserve_patch_credential_url_value,
)
from app.core.settings_override.secret_storage import encrypt_secret_leaves
from app.core.utils.date_time import utc_now

ClassEntry = tuple[str, type[BaseYamlSettings], OverridableSettingsProxy]


@dataclass(frozen=True, slots=True)
class AppOwnedClassEntry:
    """Expose one app-owned settings class on the PMM Extensions settings router.

    :param setting_class: The Pydantic class ``__name__`` of the settings class.
    :param settings_cls: The Pydantic settings model class.
    :param proxy: The live override proxy for the class.
    :param app_key: The owning app's registry key.
    :param reseed_keys: Field names on this class whose HOT override must
        re-seed the periodic-task schedule. Empty by default, so an app whose
        settings drive no beat entry declares nothing.
    """

    setting_class: str
    settings_cls: type[BaseYamlSettings]
    proxy: OverridableSettingsProxy
    app_key: str
    reseed_keys: frozenset[str] = frozenset()


#: One ``(class_name, remote_base_path)`` pair per settings class whose
#: storage lives in another sub-app and must be proxied server-side rather than
#: read from a local config singleton. ``remote_base_path`` is the path the
#: remote sub-app mounts its settings router at (e.g. ``"/admin/settings"``),
#: relative to the injected ``RemoteAPI`` client's base URL.
RemoteClassEntry = tuple[str, str]

#: Predicate deciding whether a field applies under current runtime state (e.g.
#: the active auth provider). ``None`` at a router or call site means every field
#: applies. Display-only: it drives ``SettingResponse.is_applicable`` for the UI
#: and never blocks PATCH/DELETE.
ApplicabilityPredicate = Callable[[str, FieldMetadata], bool]

#: Async callback resolving app identity and enabled state for one ``app_key``.
#: Injected by the PMM Extensions wiring so this factory stays free of ``app.extensions`` imports.
ResolveAppMetadata = Callable[[AsyncSession, str], Awaitable[SettingClassAppMetadata]]


async def _no_remote_api() -> None:
    """Return ``None`` as the remote-API dependency when no remote classes wire one.

    The settings router always declares a ``remote_api`` dependency so the LIST /
    DETAIL / PATCH / DELETE handlers share a single signature. Callers that pass
    no ``remote_classes`` (e.g. the Tasks sub-app's local-only router) get this
    no-op, keeping their behaviour identical to before remote support existed.

    :return: Always ``None``.
    """
    return


#: Default ``remote_api`` annotation when a router wires no remote classes:
#: resolves to ``None`` so the shared handler signature stays valid.
_no_remote_dep = Annotated[None, Depends(_no_remote_api)]


async def _proxy_settings_request(
    remote_api: RemoteAPI,
    method: str,
    path: str,
    **kwargs: Any,
) -> Any:
    """Dispatch one settings request to a remote sub-app, preserving client errors.

    Upstream **client** errors (HTTP < 500: ``400`` / ``404`` / ``409`` / ``422``)
    are re-raised unchanged so their status and ``detail`` survive the proxy --
    the React settings UI reads the upstream ``422`` ``detail`` to render inline
    per-field validation messages, which a blanket ``502`` would erase. Upstream
    **availability** failures (HTTP >= 500, or an ``OSError`` connection failure)
    become :class:`~app.core.exceptions.HTTPBadGatewayException` (``502``),
    matching the other PMM Extensions proxy routes.

    :param remote_api: The async client for the owning sub-app, already
        authenticated (the PMM Extensions wiring forwards the caller's Bearer token).
    :param method: The :class:`RemoteAPI` verb to call (``"get"`` / ``"patch"`` /
        ``"delete"``).
    :param path: The remote path to request, relative to the client's base URL.
    :param kwargs: Extra keyword arguments forwarded to the verb (e.g. ``json``).
    :return: The parsed JSON payload, or ``None`` on an upstream ``204``.
    :raises HTTPException: Re-raised unchanged for an upstream client error
        (status < 500).
    :raises HTTPBadGatewayException: For an upstream server error (status >= 500)
        or a connection-level ``OSError``.
    """
    try:
        return await getattr(remote_api, method)(path, **kwargs)
    except HTTPException as exc:
        if exc.status_code < status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        raise HTTPBadGatewayException(detail=str(exc.detail)) from exc
    except OSError as exc:
        raise HTTPBadGatewayException(detail=str(exc)) from exc


def _remote_wiring(
    remote_classes: list[RemoteClassEntry] | None,
    remote_api_dep: Any,
) -> tuple[dict[str, str], Any]:
    """Resolve the remote-class lookup and the handler's ``remote_api`` annotation.

    :param remote_classes: The configured ``(class_name, base_path)`` pairs, or ``None``.
    :param remote_api_dep: The ``Annotated[RemoteAPI, Depends(...)]`` alias, or
        ``None`` when no remote classes are wired.
    :return: A ``(setting_class -> base_path)`` map and the dependency annotation
        to use on the handlers (the no-op ``None`` dependency when unset).
    :raises ValueError: If ``remote_classes`` is non-empty but ``remote_api_dep``
        is ``None``, so the misconfiguration fails fast at router construction
        instead of as a runtime ``500`` when a handler calls ``None.get(...)``.
    """
    remote_lookup = {str(name): path for name, path in (remote_classes or [])}
    if remote_lookup and remote_api_dep is None:
        raise ValueError(
            "remote_api_dep is required when remote_classes is non-empty.",
        )
    remote_dep = remote_api_dep if remote_api_dep is not None else _no_remote_dep
    return remote_lookup, remote_dep


async def _remote_list_group(
    remote_api: RemoteAPI,
    setting_class: str,
    base_path: str,
) -> SettingClassGroup:
    """Fetch and validate one remote settings class's group for the LIST.

    The remote fetch is fail-closed: any upstream failure (HTTP error or
    connection ``OSError``) becomes a ``502`` so the LIST does not silently omit
    the remote group. The remote ``GET {base_path}/`` returns a full
    :class:`SettingsListResponse`; validation happens *outside* the
    ``502``-mapping ``try`` so an upstream shape mismatch surfaces as a ``500``
    (a genuine contract bug) rather than being masked as a ``502``.

    :param remote_api: The authenticated client for the owning sub-app.
    :param setting_class: The remote class whose group to extract.
    :param base_path: The remote settings router's mount path.
    :return: The validated group for ``setting_class``.
    :raises HTTPBadGatewayException: If the upstream call fails, or the upstream
        response omits ``setting_class``.
    """
    try:
        payload = await remote_api.get(f"{base_path}/")
    except (HTTPException, OSError) as exc:
        detail = getattr(exc, "detail", str(exc))
        raise HTTPBadGatewayException(detail=str(detail)) from exc
    remote = SettingsListResponse.model_validate(payload)
    group = next((g for g in remote.groups if g.setting_class == setting_class), None)
    if group is None:
        raise HTTPBadGatewayException(
            detail=f"Upstream did not return settings class {setting_class!r}.",
        )
    return group


async def _remote_detail(
    remote_api: RemoteAPI,
    base_path: str,
    setting_class: str,
    key: str,
) -> SettingResponse:
    """Proxy a DETAIL read for a remote settings class and validate the response.

    :param remote_api: The authenticated client for the owning sub-app.
    :param base_path: The remote settings router's mount path.
    :param setting_class: The remote class the field belongs to.
    :param key: The field name to read.
    :return: The validated response for the field.
    :raises HTTPException: Re-raised unchanged for an upstream client error
        (status < 500).
    :raises HTTPBadGatewayException: For an upstream server error (status >= 500)
        or a connection-level ``OSError``.
    :raises ValidationError: If the upstream response does not match
        :class:`SettingResponse`.
    """
    payload = await _proxy_settings_request(
        remote_api, "get", f"{base_path}/{setting_class}/{key}"
    )
    return SettingResponse.model_validate(payload)


async def _remote_patch(
    remote_api: RemoteAPI,
    base_path: str,
    setting_class: str,
    body: SettingsPatch,
) -> list[SettingResponse]:
    """Proxy a PATCH batch for a remote settings class and validate the response.

    :param remote_api: The authenticated client for the owning sub-app.
    :param base_path: The remote settings router's mount path.
    :param setting_class: The remote class the override targets.
    :param body: The batch of ``{key: value, ...}`` overrides to forward.
    :return: One validated :class:`SettingResponse` per applied key.
    :raises HTTPException: Re-raised unchanged for an upstream client error
        (status < 500), e.g. the upstream per-field ``422``.
    :raises HTTPBadGatewayException: For an upstream server error (status >= 500)
        or a connection-level ``OSError``.
    :raises ValidationError: If an upstream item does not match
        :class:`SettingResponse`.
    """
    payload = await _proxy_settings_request(
        remote_api,
        "patch",
        f"{base_path}/{setting_class}",
        json=body.model_dump(mode="json"),
    )
    return [SettingResponse.model_validate(item) for item in payload]


async def collect_class_setting_responses(
    *,
    session: AsyncSession,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    applicability: ApplicabilityPredicate | None = None,
) -> list[SettingResponse]:
    """Return every LIST-projection entry for one wired settings class.

    Loads active override rows, expands nested-overridable parents into their
    leaf keys via :func:`_field_responses`, and dumps each value through
    :func:`dump_field_value` so the key set matches ``GET /settings/``.

    :param session: The sub-app's database session.
    :param setting_class: The settings class identifier (Pydantic class ``__name__``).
    :param settings_cls: The Pydantic settings class to introspect.
    :param proxy: The proxy whose attribute access yields current values.
    :param applicability: Optional predicate deciding whether each field applies
        under current runtime state; ``None`` marks every field applicable.
    :return: One :class:`SettingResponse` per LIST row for the class.
    """
    rows = await SettingsOverrideManager.list(
        session, setting_class=setting_class_token(settings_cls), is_active=True
    )
    provenance_by_key = override_provenance_for_rows(settings_cls, rows)
    return [
        response
        for field_meta in iter_class_fields(settings_cls)
        for response in _field_responses(
            setting_class=setting_class,
            settings_cls=settings_cls,
            proxy=proxy,
            field_meta=field_meta,
            provenance_by_key=provenance_by_key,
            applicability=applicability,
        )
    ]


def _enum_options(field_info: FieldInfo) -> list[SettingOption] | None:
    """Return dropdown options for enum annotations; otherwise ``None``.

    :param field_info: The Pydantic field metadata for the target attribute.
    :return: One option per canonical enum member, or ``None`` when the
        annotation is not an ``Enum`` subclass.
    """
    annotation = field_info.annotation
    if not (isinstance(annotation, type) and issubclass(annotation, Enum)):
        return None
    return [
        SettingOption(
            label=member.name,
            value=dump_field_value(field_info, member),
        )
        for member in annotation  # iterating the enum skips aliases
    ]


def _settings_response_from_field(
    *,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    field_meta: FieldMetadata,
    provenance: SettingProvenance | None,
    applicability: ApplicabilityPredicate | None = None,
) -> SettingResponse:
    """Build a :class:`SettingResponse` for one field on a settings class.

    :param setting_class: The settings class identifier (Pydantic class ``__name__``).
    :param settings_cls: The Pydantic settings class declaring the field.
    :param proxy: The proxy whose attribute access yields the field's current
        value (snapshot if present, else the wrapped Pydantic instance).
    :param field_meta: The introspected metadata for the field.
    :param provenance: The last-written stamp for the active override applying
        to this ``(class, key)`` pair, or ``None`` when none applies. Its
        presence is what sets ``has_override``, so the flag and the stamps
        cannot disagree.
    :param applicability: Optional predicate deciding whether the field applies
        under current runtime state; ``None`` marks the field applicable.
    :return: The structured response for the field.
    """
    if "__" in field_meta.key:
        field_info, current_value = resolve_nested_value(
            settings_cls=settings_cls, proxy=proxy, key=field_meta.key
        )
        resolved = resolve_nested_field(settings_cls, field_meta.key)
        key_path = list(resolved[0]) if resolved else [field_meta.key]
        if current_value is NESTED_VALUE_MISSING or current_value is None:
            serialized_value = None
        else:
            serialized_value = dump_field_value(field_info, current_value)
    else:
        field_info = _top_level_field_info(settings_cls, field_meta.key)
        current_value = getattr(proxy, field_meta.key)
        key_path = [field_meta.key]
        serialized_value = dump_field_value(field_info, current_value)
    return SettingResponse(
        setting_class=setting_class,
        key=field_meta.key,
        key_path=key_path,
        value=serialized_value,
        default_value=dump_field_value(field_info, field_meta.default),
        type=_format_annotation(field_meta.annotation),
        reload=field_meta.reload,
        description=field_meta.description,
        is_secret=field_meta.is_secret,
        is_complex=field_meta.is_complex,
        has_override=provenance is not None,
        updated_at=provenance.updated_at if provenance is not None else None,
        updated_by=provenance.updated_by if provenance is not None else None,
        is_advanced=field_meta.is_advanced,
        is_applicable=(
            applicability(setting_class, field_meta)
            if applicability is not None
            else True
        ),
        options=_enum_options(field_info),
    )


def _top_level_field_info(settings_cls: type[BaseYamlSettings], key: str) -> FieldInfo:
    """Return the field metadata for a top-level key, declared or computed.

    :func:`iter_class_fields` enumerates computed fields alongside declared
    ones, and ``model_fields`` holds only the latter, so a plain subscript
    raises :class:`KeyError` for exactly the keys the listing added.

    :param settings_cls: The Pydantic settings class declaring the key.
    :param key: A top-level key :func:`iter_class_fields` yielded.
    :return: The declared field metadata, or the stand-in
        :func:`computed_field_info` synthesises for a computed field.
    :raises KeyError: If ``key`` is neither a declared nor a computed field.
    """
    field_info = settings_cls.model_fields.get(key) or computed_field_info(
        settings_cls, key
    )
    if field_info is None:
        raise KeyError(key)
    return field_info


def _field_responses(
    *,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    field_meta: FieldMetadata,
    provenance_by_key: dict[str, SettingProvenance],
    applicability: ApplicabilityPredicate | None = None,
) -> list[SettingResponse]:
    """Return one response for a plain field, or one per leaf for a nested parent.

    :func:`rendered_leaf_keys` decides which shape a field takes. When it names
    leaves, each becomes its own response (metadata resolved via
    :func:`resolve_nested_field_metadata`) in place of the parent's single
    summary entry; when it names none, the parent keeps that entry.

    :param setting_class: The settings class identifier (Pydantic class ``__name__``).
    :param settings_cls: The Pydantic settings class declaring ``field_meta``.
    :param proxy: The proxy whose attribute access yields current values.
    :param field_meta: The introspected metadata for the top-level field.
    :param provenance_by_key: The last-written stamp per canonical key (and
        prefix) carrying an override; an absent key carries no override.
    :param applicability: Optional predicate deciding whether each field applies
        under current runtime state; ``None`` marks every field applicable.
    :return: One or more responses for the field.
    """
    leaves = rendered_leaf_keys(settings_cls, field_meta.key)
    if not leaves:
        return [
            _settings_response_from_field(
                setting_class=setting_class,
                settings_cls=settings_cls,
                proxy=proxy,
                field_meta=field_meta,
                provenance=provenance_by_key.get(field_meta.key),
                applicability=applicability,
            )
        ]
    responses = []
    for leaf_key, _chain in leaves:
        leaf_meta = resolve_nested_field_metadata(settings_cls, leaf_key)
        if leaf_meta is None:
            continue
        responses.append(
            _settings_response_from_field(
                setting_class=setting_class,
                settings_cls=settings_cls,
                proxy=proxy,
                field_meta=leaf_meta,
                provenance=provenance_by_key.get(leaf_key),
                applicability=applicability,
            )
        )
    return responses


def _format_annotation(annotation: Any) -> str:
    """Return a human-readable string for a Pydantic field annotation.

    Strips the ``typing.`` prefix common to generic aliases and falls back to
    :func:`repr` when the annotation has no ``__name__``.

    :param annotation: The annotation to render.
    :return: A human-readable name for the annotation.
    """
    if annotation is None:
        return "None"
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return repr(annotation).removeprefix("typing.")


def _validate_patch_body(
    *,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    body: SettingsPatch,
) -> list[tuple[str, Any]]:
    """Validate every key/value in a PATCH body for one settings class.

    Performs Phase A of the PATCH handler: each key is checked for existence
    on ``settings_cls``, HOT classification, and type/constraint validation via
    :func:`materialize_override_value` (which routes materializer-backed fields
    -- ``PROVIDERS``, ``FOOTER_TEMPLATE`` -- through their declared
    materializer so the API accepts the same payloads the snapshot loader does).
    Errors are collected per-key; if any are present the entire batch is
    rejected with HTTP 422. Materializer-backed fields persist the raw JSON (the
    materialized value is not JSON-storable); plain fields persist the coerced
    value.

    :param settings_cls: The Pydantic settings class to validate against.
    :param proxy: The proxy whose attribute access yields current field values.
    :param body: The PATCH payload as a :class:`SettingsPatch` root model.
    :return: The list of ``(key, coerced_value)`` tuples ready to persist.
    :raises HTTPUnprocessableEntityException: If any key fails validation;
        the exception's ``detail`` is a structured list of Pydantic-style
        error entries.
    """
    errors = []
    to_apply = []
    for key, raw_value in body.root.items():
        if "__" in key:
            _validate_nested_key(
                settings_cls=settings_cls,
                proxy=proxy,
                key=key,
                raw_value=raw_value,
                errors=errors,
                to_apply=to_apply,
            )
            continue
        field_info = settings_cls.model_fields.get(key) or computed_field_info(
            settings_cls, key
        )
        if field_info is None:
            errors.append(
                {
                    "loc": ["body", key],
                    "msg": "Field does not exist on this settings class.",
                    "type": "unknown_key",
                }
            )
            continue
        if not is_hot_reloadable(settings_cls, key):
            errors.append(
                {
                    "loc": ["body", key],
                    "msg": "Setting cannot be overridden from the API.",
                    "type": "not_overridable",
                }
            )
            continue
        materializer = field_materializer(settings_cls, key)
        current_value = getattr(proxy, key, None)
        patch_value = preserve_patch_credential_url_value(
            field_info, current_value, raw_value
        )
        try:
            materialized = materialize_override_value(
                settings_cls, key, field_info, patch_value
            )
        except ValidationError as exc:
            errors.extend(
                {
                    "loc": ["body", key, *entry.get("loc", ())],
                    "msg": entry.get("msg", ""),
                    "type": entry.get("type", "value_error"),
                }
                for entry in exc.errors()
            )
            continue
        except ValueError as exc:
            errors.append(
                {"loc": ["body", key], "msg": str(exc), "type": "value_error"}
            )
            continue
        # Materializer-backed fields (PROVIDERS, FOOTER_TEMPLATE) produce values that are
        # not JSON-storable (a provider set, a Template); persist the raw JSON so
        # build_snapshot re-materializes on load.
        to_apply.append(
            (key, patch_value if materializer is not None else materialized)
        )

    if errors:
        raise HTTPUnprocessableEntityException(detail=errors)
    return to_apply


def _validate_nested_key(
    *,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    key: str,
    raw_value: Any,
    errors: list[dict[str, Any]],
    to_apply: list[tuple[str, Any]],
) -> None:
    """Validate one ``__``-delimited nested key, appending to ``errors``/``to_apply``.

    Gates the key in four steps, each mapping to a distinct 422 ``type``:
    the parent must exist (``unknown_key``) and be nested-overridable
    (``not_overridable``); the leaf must resolve (``unknown_nested_field``)
    and its chain must be open, neither explicitly not-overridable at any
    segment nor withheld by ``SETTINGS_OVERRIDE.ALLOWED_KEYS``
    (``not_overridable``); finally the value is coerced to the leaf type
    (structured Pydantic error on failure).

    :param settings_cls: The Pydantic settings class to validate against.
    :param proxy: The proxy whose attribute access yields current field values.
    :param key: The ``__``-delimited override key.
    :param raw_value: The raw value to coerce to the leaf type.
    :param errors: The running list of structured error entries, mutated in place.
    :param to_apply: The running list of ``(key, coerced_value)`` tuples,
        mutated in place.
    """
    top_resolved = resolve_field_in_model(settings_cls, key.split("__", 1)[0])
    if top_resolved is None:
        errors.append(
            {
                "loc": ["body", key],
                "msg": "Parent field does not exist on this settings class.",
                "type": "unknown_key",
            }
        )
        return
    canonical_top, _ = top_resolved
    if not is_nested_overridable_parent(settings_cls, canonical_top):
        errors.append(
            {
                "loc": ["body", key],
                "msg": "Setting cannot be overridden from the API.",
                "type": "not_overridable",
            }
        )
        return
    resolved = resolve_nested_field(settings_cls, key)
    if resolved is None:
        errors.append(
            {
                "loc": ["body", key],
                "msg": "Nested field does not exist on this settings class.",
                "type": "unknown_nested_field",
            }
        )
        return
    chain, leaf_info = resolved
    if chain_is_locked(settings_cls, key):
        errors.append(
            {
                "loc": ["body", key],
                "msg": "Setting cannot be overridden from the API.",
                "type": "not_overridable",
            }
        )
        return
    try:
        _, current_value = resolve_nested_value(
            settings_cls=settings_cls, proxy=proxy, key=key
        )
        patch_value = preserve_patch_credential_url_value(
            leaf_info, current_value, raw_value
        )
        validated = coerce_field_value(leaf_info, patch_value)
    except ValidationError as exc:
        errors.extend(
            {
                "loc": ["body", key, *entry.get("loc", ())],
                "msg": entry.get("msg", ""),
                "type": entry.get("type", "value_error"),
            }
            for entry in exc.errors()
        )
        return
    # Persist the canonical path so case-insensitive spellings collapse to one row.
    to_apply.append(("__".join(chain), validated))


async def _fire_inline_rebind_callbacks(
    request: Request,
    setting_class: str,
    proxy: OverridableSettingsProxy,
    previous: Mapping[str, object],
) -> None:
    """Fire rebind callbacks for keys this handler's inline publish just changed.

    The background refresher fires rebind callbacks only on a snapshot diff it
    computes itself (the snapshot before its ``publish_snapshot`` vs the one
    after). The PATCH/DELETE handlers publish the new snapshot *inline*, so by
    the refresher's next cycle the proxy already holds the new value and the
    cycle's diff is empty -- a hot-reload target (the live ``NomadExecutor``, a
    RemoteAPI client endpoint) would never rebind until restart. Firing here, off
    the same ``previous``-vs-published diff, closes that gap for the process that
    handled the request; the refresher still covers *other* processes.

    The per-sub-app callback registry is published by the lifespan on
    ``request.app.state.override_callbacks``. Requests routed to a mounted sub-app
    resolve ``request.app`` to that sub-app, so the lifespan anchors the registry
    on the sub-app's state. It is absent for unit tests that skip the lifespan, in
    which case this is a no-op.

    :param request: The incoming request; its ``app.state`` carries the sub-app's
        rebind-callback registry.
    :param setting_class: The settings class whose snapshot was just republished.
    :param proxy: The proxy holding the freshly-published snapshot.
    :param previous: The snapshot in effect immediately before the inline publish.
    """
    callbacks = getattr(request.app.state, "override_callbacks", None)
    if not callbacks:
        return
    await fire_change_callbacks(
        callbacks, setting_class, previous, proxy.get_snapshot()
    )


def _validate_app_owned_wiring(
    app_owned: list[AppOwnedClassEntry],
    resolve_app_metadata: ResolveAppMetadata | None,
) -> None:
    """Reject app-owned wiring that omits the metadata resolver.

    :param app_owned: The app-owned settings classes to expose.
    :param resolve_app_metadata: The callback that resolves app metadata.
    :raises ValueError: If ``app_owned`` is non-empty but ``resolve_app_metadata``
        is ``None``.
    """
    if app_owned and resolve_app_metadata is None:
        raise ValueError(
            "resolve_app_metadata is required when app_owned_classes is non-empty.",
        )


def _merge_app_owned_into_lookup(
    class_lookup: dict[str, tuple[type[BaseYamlSettings], OverridableSettingsProxy]],
    remote_lookup: dict[str, str],
    app_owned: list[AppOwnedClassEntry],
) -> None:
    """Register app-owned classes in the local lookup, rejecting duplicates.

    :param class_lookup: The core class lookup map to extend in place.
    :param remote_lookup: The remote class lookup map used for conflict checks.
    :param app_owned: The app-owned settings classes to merge.
    :raises ValueError: If a setting class is wired more than once.
    """
    for entry in app_owned:
        class_id = str(entry.setting_class)
        if class_id in class_lookup:
            raise ValueError(
                f"Settings class {class_id!r} is wired as both"
                " a core class and an app-owned class.",
            )
        if class_id in remote_lookup:
            raise ValueError(
                f"Settings class {class_id!r} is wired as both"
                " a remote class and an app-owned class.",
            )
        class_lookup[class_id] = (entry.settings_cls, entry.proxy)


async def _collect_app_owned_list_groups(
    session: AsyncSession,
    app_owned: list[AppOwnedClassEntry],
    resolve_app_metadata: ResolveAppMetadata,
) -> list[SettingClassGroup]:
    """Build LIST groups for app-owned settings classes with app metadata.

    :param session: The sub-app's database session.
    :param app_owned: The app-owned settings classes to list.
    :param resolve_app_metadata: The callback that resolves app metadata.
    :return: One :class:`SettingClassGroup` per app-owned class.
    """
    groups = []
    for entry in app_owned:
        class_id = str(entry.setting_class)
        settings_list = await collect_class_setting_responses(
            session=session,
            setting_class=class_id,
            settings_cls=entry.settings_cls,
            proxy=entry.proxy,
        )
        metadata = await resolve_app_metadata(session, entry.app_key)
        groups.append(
            SettingClassGroup(
                setting_class=class_id,
                settings=settings_list,
                is_app_owned=metadata.is_app_owned,
                app_id=metadata.app_id,
                app_display_name=metadata.app_display_name,
                app_enabled=metadata.app_enabled,
            )
        )
    return groups


async def _collect_settings_list_groups(
    session: AsyncSession,
    remote_api: RemoteAPI | None,
    classes: list[ClassEntry],
    remote_lookup: dict[str, str],
    app_owned: list[AppOwnedClassEntry],
    resolve_app_metadata: ResolveAppMetadata | None,
    applicability: ApplicabilityPredicate | None,
) -> list[SettingClassGroup]:
    """Collect every settings-class group for the LIST endpoint.

    :param session: The sub-app's database session.
    :param remote_api: The client for remote settings classes, or ``None``.
    :param classes: The core settings classes exposed locally.
    :param remote_lookup: Remote classes keyed by class name.
    :param app_owned: App-owned settings classes appended after remote groups.
    :param resolve_app_metadata: The callback that resolves app metadata.
    :param applicability: Optional predicate driving ``is_applicable`` on each
        core-class field; ``None`` marks every field applicable.
    :return: Groups in core, remote, then app-owned declaration order.
    """
    groups = []
    for setting_class, settings_cls, proxy in classes:
        class_id = str(setting_class)
        settings_list = await collect_class_setting_responses(
            session=session,
            setting_class=class_id,
            settings_cls=settings_cls,
            proxy=proxy,
            applicability=applicability,
        )
        groups.append(SettingClassGroup(setting_class=class_id, settings=settings_list))
    for setting_class, base_path in remote_lookup.items():
        groups.append(await _remote_list_group(remote_api, setting_class, base_path))
    if app_owned:
        if resolve_app_metadata is None:
            msg = "resolve_app_metadata is required when app_owned classes are listed."
            raise RuntimeError(msg)
        groups.extend(
            await _collect_app_owned_list_groups(
                session,
                app_owned,
                resolve_app_metadata,
            )
        )
    return groups


def build_settings_router(
    *,
    classes: list[ClassEntry],
    session_dep: Any,
    admin_dep: params.Depends,
    actor_dep: Any,
    mutation_deps: list[params.Depends] | None = None,
    remote_classes: list[RemoteClassEntry] | None = None,
    remote_api_dep: Any = None,
    applicability: ApplicabilityPredicate | None = None,
    app_owned_classes: list[AppOwnedClassEntry] | None = None,
    resolve_app_metadata: ResolveAppMetadata | None = None,
) -> APIRouter:
    """Build an :class:`APIRouter` exposing the settings CRUD endpoints.

    The factory generates LIST / DETAIL / PATCH / DELETE routes parameterised
    by the settings classes the sub-app wants to expose, its session
    dependency, and its admin-auth dependency. ``admin_dep`` is applied at
    the router level so every endpoint inherits the admin gate. State-changing
    endpoints (PATCH / DELETE) additionally take ``mutation_deps``, which the
    PMM Extensions wiring uses to require Bearer authentication on mutations, so a
    cross-site JSON request carrying only ambient cookies cannot mutate
    settings.

    :param classes: One ``(class_name, settings_cls, proxy)`` triple per
        core settings class to expose on this router.
    :param session_dep: An ``Annotated[AsyncSession, Depends(...)]`` type alias
        for the sub-app's session dependency (e.g. ``app.extensions.deps.SessionDep``
        or ``app.tasks.deps.SessionDep``). Used as the parameter annotation on
        each generated handler so FastAPI resolves the session per-request.
    :param admin_dep: A FastAPI ``Depends(...)`` callable that gates access to
        admin users only (e.g. ``app.extensions.deps.IsApiAdmin`` or
        ``app.api.deps.IsAdminDep``). Applied at the router level so every
        endpoint inherits the admin gate.
    :param actor_dep: An ``Annotated[str, Depends(...)]`` type alias yielding the
        calling admin's username, recorded on every override row PATCH writes
        (e.g. ``app.extensions.deps.ApiAdminUsername`` or
        ``app.api.deps.AdminUsername``). Required rather than optional so a
        sub-app that forgets the wiring fails at import instead of silently
        recording no actor. Each in-tree alias resolves the same callable its
        sub-app passes as ``admin_dep``, and FastAPI reuses a dependency's
        result within a request, so binding the actor costs no second
        auth-provider round-trip.
    :param mutation_deps: Optional list of FastAPI ``Depends(...)`` callables
        applied only to the state-changing endpoints (PATCH / DELETE). The
        PMM Extensions wiring passes ``[RequireBearerForUnsafeMethods]`` so cookie sessions cannot
        mutate settings; the Tasks wiring leaves this empty because its
        admin dependency is bearer-only via ``OAuth2PasswordBearer``.
    :param remote_classes: Optional ``(class_name, remote_base_path)`` pairs
        for settings classes whose storage lives in another sub-app. Such a class
        has no local config singleton or override table; the LIST handler appends
        its group by proxying ``GET {remote_base_path}/`` server-side, and the
        DETAIL / PATCH / DELETE handlers dispatch on ``setting_class`` to the
        remote sub-app via ``remote_api_dep``. Defaults to no remote classes, in
        which case the router is purely local and behaves exactly as before.
    :param remote_api_dep: An ``Annotated[RemoteAPI, Depends(...)]`` type alias
        for the client used to reach the remote sub-app (e.g. ``app.extensions.deps.TaskAPI``,
        which forwards the caller's Bearer token). Required when ``remote_classes``
        is non-empty; ignored otherwise. Used as the parameter annotation on each
        handler so FastAPI resolves the client per-request.
    :param applicability: Optional predicate deciding whether each field applies
        under current runtime state (e.g. the active auth provider). It drives
        ``SettingResponse.is_applicable`` on every response the router returns
        (LIST, DETAIL, and PATCH); ``None`` (the default) marks every field
        applicable, so callers that omit it behave exactly as before. Display-only
        -- it never blocks PATCH/DELETE.
    :param app_owned_classes: Optional app-owned settings classes declared by
        PMM Extensions plugins. Appended after core and remote groups on LIST; merged into
        the local class lookup so GET / PATCH / DELETE work unchanged.
    :param resolve_app_metadata: Async callback that resolves app identity and
        enabled state for one ``app_key``. Required when ``app_owned_classes``
        is non-empty; ignored otherwise. Injected by the PMM Extensions wiring so this
        factory stays free of ``app.extensions`` imports.
    :return: A configured :class:`APIRouter` ready to mount under a sub-app's
        ``/settings`` prefix.
    :raises ValueError: If ``remote_classes`` is non-empty without
        ``remote_api_dep``, or ``app_owned_classes`` is non-empty without
        ``resolve_app_metadata``, or a setting class is wired more than once.
    """
    router = APIRouter(dependencies=[admin_dep])
    app_owned = list(app_owned_classes or [])
    _validate_app_owned_wiring(app_owned, resolve_app_metadata)
    class_lookup = {str(member): (cls, proxy) for member, cls, proxy in classes}
    remote_lookup, remote_dep = _remote_wiring(remote_classes, remote_api_dep)
    _merge_app_owned_into_lookup(class_lookup, remote_lookup, app_owned)

    def _resolve(
        setting_class: str,
    ) -> tuple[type[BaseYamlSettings], OverridableSettingsProxy]:
        """Return the settings class and proxy for ``setting_class`` or 404.

        :param setting_class: The class identifier requested by the client.
        :return: The settings class and its proxy.
        :raises HTTPNotFoundException: If ``setting_class`` is not configured
            on this router.
        """
        entry = class_lookup.get(setting_class)
        if entry is None:
            raise HTTPNotFoundException(
                f"Settings class {setting_class!r} is not exposed by this sub-app.",
            )
        return entry

    @router.get("/")
    async def list_settings(
        session: session_dep,  # type: ignore[valid-type]
        remote_api: remote_dep,  # type: ignore[valid-type]
    ) -> SettingsListResponse:
        """List every exposed settings class with current values and metadata.

        Local classes are read from their config singletons; remote classes
        (``remote_classes``) are fetched server-side from their owning sub-app
        and appended in declaration order; app-owned classes follow remote
        groups with per-group app metadata. A failed remote fetch fails the
        whole request with ``502`` -- the LIST never silently drops a remote
        group.

        :param session: The sub-app's database session.
        :param remote_api: The client for remote settings classes (``None`` when
            the router wires none).
        :return: Grouped responses, one group per configured settings class.
        """
        groups = await _collect_settings_list_groups(
            session=session,
            remote_api=remote_api,
            classes=classes,
            remote_lookup=remote_lookup,
            app_owned=app_owned,
            resolve_app_metadata=resolve_app_metadata,
            applicability=applicability,
        )
        return SettingsListResponse(groups=groups)

    @router.get("/{setting_class}/{key}")
    async def get_setting(
        setting_class: str,
        key: str,
        session: session_dep,  # type: ignore[valid-type]
        remote_api: remote_dep,  # type: ignore[valid-type]
    ) -> SettingResponse:
        """Return one field's metadata and current value.

        :param setting_class: The settings class the field belongs to.
        :param key: The field name on the settings class.
        :param session: The sub-app's database session.
        :param remote_api: The client for remote settings classes (``None`` when
            the router wires none).
        :return: The structured response for the field.
        :raises HTTPNotFoundException: If the class isn't exposed or the key
            doesn't exist on the class.
        """
        if setting_class in remote_lookup:
            return await _remote_detail(
                remote_api, remote_lookup[setting_class], setting_class, key
            )
        settings_cls, proxy = _resolve(setting_class)
        key = canonical_override_key(settings_cls, key)
        field_meta = _field_meta_or_404(settings_cls, key)
        rows = await SettingsOverrideManager.list(
            session, setting_class=setting_class_token(settings_cls), is_active=True
        )
        provenance_by_key = override_provenance_for_rows(settings_cls, rows)
        return _settings_response_from_field(
            setting_class=setting_class,
            settings_cls=settings_cls,
            proxy=proxy,
            field_meta=field_meta,
            provenance=provenance_by_key.get(key),
            applicability=applicability,
        )

    @router.patch("/{setting_class}", dependencies=mutation_deps or [])
    async def patch_settings(
        request: Request,
        setting_class: str,
        body: SettingsPatch,
        session: session_dep,  # type: ignore[valid-type]
        remote_api: remote_dep,  # type: ignore[valid-type]
        actor: actor_dep,  # type: ignore[valid-type]
    ) -> list[SettingResponse]:
        """Apply a batch of overrides for one settings class atomically.

        For a remote class the batch is forwarded to the owning sub-app, which
        owns the validation and persistence; the upstream's per-field ``422``
        (and its ``detail``) is preserved so the UI can render inline messages.

        For a local class: Phase A validates every key in ``body`` (existence on
        the class, HOT classification, type/constraint coercion) and collects
        per-key errors. If any key fails, the whole batch is rejected with a
        structured 422 and nothing is written. Phase B persists every valid entry
        in a single transaction, refreshes the proxy snapshot once, then fires
        the rebind callbacks for any changed keys so a HOT target rebinds without
        waiting for the next background refresh cycle.

        :param request: The incoming request; its ``app.state`` carries the
            sub-app's rebind-callback registry.
        :param setting_class: The settings class the override targets.
        :param body: The batch of ``{key: value, ...}`` overrides.
        :param session: The sub-app's database session.
        :param remote_api: The client for remote settings classes (``None`` when
            the router wires none).
        :param actor: The calling admin's username, recorded on every row the
            batch writes and reported back on each response.
        :return: One :class:`SettingResponse` per applied key, in input order.
        :raises HTTPNotFoundException: If the class isn't exposed.
        :raises HTTPUnprocessableEntityException: If any key fails validation;
            no rows are written.
        :raises HTTPBadGatewayException: For a remote class, when the owning
            sub-app returns a server error (status >= 500) or is unreachable.
        :raises IntegrityError: When the replay of a batch that lost the
            unique-index race conflicts again, which leaves nothing written.
        """
        if setting_class in remote_lookup:
            return await _remote_patch(
                remote_api, remote_lookup[setting_class], setting_class, body
            )
        settings_cls, proxy = _resolve(setting_class)
        return await apply_class_overrides(
            request=request,
            session=session,
            setting_class=setting_class,
            settings_cls=settings_cls,
            proxy=proxy,
            body=body,
            actor=actor,
            applicability=applicability,
        )

    @router.delete(
        "/{setting_class}/{key}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=mutation_deps or [],
    )
    async def delete_setting(
        request: Request,
        setting_class: str,
        key: str,
        session: session_dep,  # type: ignore[valid-type]
        remote_api: remote_dep,  # type: ignore[valid-type]
    ) -> None:
        """Revert override row(s) for one field to the field's declared default.

        For a remote class the DELETE is forwarded to the owning sub-app, which
        owns the idempotency and ``NOT_OVERRIDABLE`` semantics; its status and
        ``detail`` are preserved through the proxy.

        For a local class the DELETE is idempotent: deleting a (class, key) pair
        that has no override row succeeds with 204. Attempting to delete a field
        the code declares NOT_OVERRIDABLE responds 409, since it cannot have an
        override row in the first place and the operator's intent is
        unsatisfiable. A field only ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` withheld
        may still carry a row written before the restriction applied, so that
        row is deleted normally (found by canonicalizing the stored key, so a
        legacy non-canonical casing is still seen) and only the no-row case
        answers 409. When several rows canonicalize to the same key, all of
        them are removed.

        After republishing the snapshot, fires the rebind callbacks for the
        reverted key so a HOT target rebinds to its restored value without
        waiting for the next background refresh cycle.

        :param request: The incoming request; its ``app.state`` carries the
            sub-app's rebind-callback registry.
        :param setting_class: The settings class the field belongs to.
        :param key: The field name on the settings class.
        :param session: The sub-app's database session.
        :param remote_api: The client for remote settings classes (``None`` when
            the router wires none).
        :raises HTTPNotFoundException: If the class isn't exposed or the key
            doesn't exist on the class.
        :raises HTTPConflictException: If the field is NOT_OVERRIDABLE and has
            no row to delete.
        :raises HTTPUnprocessableEntityException: If ``key`` names a
            ``NESTED_ONLY`` parent (the whole parent cannot be overridden;
            target an individual ``parent__leaf`` instead).
        :raises HTTPBadGatewayException: For a remote class, when the owning
            sub-app returns a server error (status >= 500) or is unreachable.
        """
        if setting_class in remote_lookup:
            base_path = remote_lookup[setting_class]
            await _proxy_settings_request(
                remote_api, "delete", f"{base_path}/{setting_class}/{key}"
            )
            return
        settings_cls, proxy = _resolve(setting_class)
        await clear_class_override(
            request=request,
            session=session,
            setting_class=setting_class,
            settings_cls=settings_cls,
            proxy=proxy,
            key=key,
        )

    return router


def _field_meta_or_404(settings_cls: type[BaseYamlSettings], key: str) -> FieldMetadata:
    """Return the :class:`FieldMetadata` for ``key`` on ``settings_cls`` or raise 404.

    Accepts both top-level keys (matched against
    :func:`iter_class_fields`) and ``__``-delimited nested keys (resolved via
    :func:`resolve_nested_field_metadata` into a synthesised leaf metadata
    whose ``key`` is the full nested string).

    :param settings_cls: The Pydantic settings class to look up the field on.
    :param key: The field name (or ``__``-delimited nested key) to find.
    :return: The metadata for the field.
    :raises HTTPNotFoundException: If ``key`` is not a declared field of
        ``settings_cls`` (nor a resolvable nested key).
    """
    if "__" in key:
        nested_meta = resolve_nested_field_metadata(settings_cls, key)
        if nested_meta is not None:
            return nested_meta
        raise HTTPNotFoundException(
            f"Setting {settings_cls.__name__}.{key} does not exist.",
        )
    for field_meta in iter_class_fields(settings_cls):
        if field_meta.key == key:
            return field_meta
    raise HTTPNotFoundException(
        f"Setting {settings_cls.__name__}.{key} does not exist.",
    )


def _applied_field_meta(
    settings_cls: type[BaseYamlSettings],
    to_apply: list[tuple[str, Any]],
) -> dict[str, FieldMetadata]:
    """Return a ``key -> FieldMetadata`` map covering every applied override key.

    Seeds the map with all top-level fields and adds a synthesised entry for
    each ``__``-delimited key in ``to_apply`` (which ``iter_class_fields`` does
    not enumerate). Every key was already accepted by
    :func:`_validate_patch_body`, so the nested lookups cannot 404.

    :param settings_cls: The Pydantic settings class the keys belong to.
    :param to_apply: The list of ``(key, value)`` tuples being applied.
    :return: A mapping from override key to its field metadata.
    """
    field_meta_by_key = {f.key: f for f in iter_class_fields(settings_cls)}
    for key, _ in to_apply:
        if "__" in key and key not in field_meta_by_key:
            field_meta_by_key[key] = _field_meta_or_404(settings_cls, key)
    return field_meta_by_key


def _is_statically_locked(settings_cls: type[BaseYamlSettings], key: str) -> bool:
    """Return whether the code, not the allowlist, refuses overrides of ``key``.

    Reads the classification with the policy gate switched off, so a key only
    ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` withholds reports ``False`` here. That
    distinction is what lets DELETE clear a row written before a restriction
    applied while keeping 409 for a field the code marks ``NOT_OVERRIDABLE``.

    :param settings_cls: The Pydantic settings class the key belongs to.
    :param key: The canonical key, top-level or ``__``-delimited.
    :return: ``True`` iff the static declaration alone refuses the override.
    """
    if "__" in key:
        return chain_has_explicit_not_overridable(settings_cls, key)
    return not is_nested_overridable_parent(
        settings_cls, key, include_policy_gate=False
    )


def _assert_key_deletable(
    settings_cls: type[BaseYamlSettings],
    field_meta: FieldMetadata,
    *,
    has_override_row: bool,
) -> None:
    """Raise the appropriate HTTP error when ``field_meta`` cannot be deleted.

    A ``__``-delimited key whose top-level parent is not *addressable* is
    rejected with 422 (``not_overridable``), mirroring the PATCH guard in
    :func:`_validate_nested_key` so DELETE and PATCH agree on which nested keys
    exist at all. The allowlist is deliberately excluded from that check: a
    parent every one of whose leaves ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` withheld
    must stay reachable, or any row accumulated beneath it before the
    restriction applied would be stuck. A ``NESTED_ONLY`` parent rejects
    whole-parent deletion with 422 (target a nested child instead). A
    ``NOT_OVERRIDABLE`` field rejects deletion with 409 when no row can or does
    exist; when only the allowlist withheld it and a row survives, deletion
    proceeds. Overridable keys pass through silently.

    :param settings_cls: The Pydantic settings class the field belongs to.
    :param field_meta: The resolved metadata for the key being deleted.
    :param has_override_row: Whether an override row currently exists for the
        key, which decides the not-overridable branch.
    :raises HTTPUnprocessableEntityException: If the key names a ``NESTED_ONLY``
        parent, or a nested key under an unaddressable parent.
    :raises HTTPConflictException: If the key names a ``NOT_OVERRIDABLE`` field
        with no row the operator could be asking to remove.
    """
    if "__" in field_meta.key:
        top = field_meta.key.split("__", 1)[0]
        if not is_nested_overridable_parent(
            settings_cls, top, include_policy_gate=False
        ):
            raise HTTPUnprocessableEntityException(
                detail=[
                    {
                        "loc": ["path", "key"],
                        "msg": "Setting cannot be overridden from the API.",
                        "type": "not_overridable",
                    }
                ]
            )
    if field_meta.reload == ReloadClassification.NESTED_ONLY:
        raise HTTPUnprocessableEntityException(
            detail=[
                {
                    "loc": ["path", "key"],
                    "msg": "Whole-parent override is not allowed; target a"
                    " nested field instead.",
                    "type": "not_overridable",
                }
            ]
        )
    if field_meta.reload == ReloadClassification.NOT_OVERRIDABLE and (
        _is_statically_locked(settings_cls, field_meta.key) or not has_override_row
    ):
        raise HTTPConflictException(
            f"Setting {settings_cls.__name__}.{field_meta.key} cannot be"
            " overridden; no row to delete.",
        )


async def _delete_override_rows(
    session: AsyncSession,
    setting_class: str,
    rows: list[SettingOverride],
) -> None:
    """Delete every resolved override row by its stored key.

    A no-op when ``rows`` is empty, so DELETE stays idempotent without emitting
    an invalid ``IN ()`` clause.

    :param session: The sub-app's database session.
    :param setting_class: The settings class the rows belong to.
    :param rows: The rows :func:`override_rows_for_key` resolved for this key.
    """
    if not rows:
        return
    await SettingsOverrideManager.delete_where(
        session,
        col(SettingOverride.key).in_({row.key for row in rows}),
        setting_class=setting_class,
    )


async def _persist_overrides(
    *,
    session: AsyncSession,
    settings_cls: type[BaseYamlSettings],
    to_apply: list[tuple[str, Any]],
    actor: str,
) -> dict[str, SettingProvenance]:
    """Insert or update each ``(setting_class, key)`` row in a single transaction.

    Existing rows (resolved by canonicalizing the stored key) have ``value``
    and ``is_active`` updated and their stored key healed to the canonical
    spelling; missing rows are inserted fresh. When several rows resolve to
    the same key, one survivor is kept (preferring an already-canonical row)
    and the rest are dropped so the unique index stays satisfied after the
    rename. Healing matters for top-level keys: the snapshot loader looks up
    ``model_fields`` by exact key, so leaving a mixed-case spelling would
    accept the PATCH while never applying the override. The transaction is
    committed once at the end so a failure on any single row rolls back the
    entire batch.

    Concurrent PATCHes against the same key would otherwise race: both
    requests can observe no matching row between their lookup and the
    unique-index commit, and the second commit would raise
    :class:`sqlalchemy.exc.IntegrityError`. The handler catches that case,
    rolls back the failed transaction, and replays the batch against the
    rows the winning writer left in place so the second PATCH still applies
    its values cleanly.

    :param session: The sub-app's database session.
    :param settings_cls: The Pydantic settings class whose storage token is
        written to ``settingoverride.setting_class``, and against which each
        stored key is canonicalized.
    :param to_apply: The list of ``(key, coerced_value)`` tuples to persist.
    :param actor: The username recorded on every row this call writes.
    :return: The stamp written for each applied key. On the replay path this is
        the second attempt's, since the first attempt's writes were rolled back.
    :raises IntegrityError: When the replay conflicts as well; the batch is
        rolled back once and retried exactly once, never further.
    """
    token = setting_class_token(settings_cls)
    try:
        return await _stage_and_commit_overrides(
            session=session,
            setting_class=token,
            settings_cls=settings_cls,
            to_apply=to_apply,
            actor=actor,
        )
    except IntegrityError:
        await session.rollback()
        return await _stage_and_commit_overrides(
            session=session,
            setting_class=token,
            settings_cls=settings_cls,
            to_apply=to_apply,
            actor=actor,
        )


async def _stage_and_commit_overrides(
    *,
    session: AsyncSession,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    to_apply: list[tuple[str, Any]],
    actor: str,
) -> dict[str, SettingProvenance]:
    """Stage every matching (setting_class, key) row and commit the batch.

    Resolves existing rows by canonicalizing each stored key. A missing key
    is inserted; a matching set collapses to one row under the canonical
    ``key`` with the new value.

    Every row the batch touches is stamped with ``actor`` and one shared
    ``utc_now()``, on the insert branch as well as the update branch, so
    ``updated_at`` means "when this override was last saved" on any row this
    code has written. The assignment is what makes an idempotent re-PATCH
    re-stamp: submitting the stored value leaves ``value`` and ``is_active``
    unchanged, so without it the row is not dirty, no UPDATE is emitted, and the
    column's ``onupdate`` never fires.

    :param session: The sub-app's database session.
    :param setting_class: The storage token written to
        ``settingoverride.setting_class``.
    :param settings_cls: The Pydantic settings class used to canonicalize keys.
    :param to_apply: The list of ``(key, coerced_value)`` tuples to persist.
    :param actor: The username recorded on every row this call writes.
    :return: The stamp written for each applied key.
    """
    stamp = utc_now()
    provenance = {
        key: SettingProvenance(updated_at=stamp, updated_by=actor)
        for key, _value in to_apply
    }
    for key, value in to_apply:
        stored_value = encrypt_secret_leaves(
            settings_cls, key, unwrap_secrets_for_storage(value)
        )
        existing_rows = await override_rows_for_key(
            session,
            settings_cls=settings_cls,
            setting_class=setting_class,
            key=key,
        )
        if not existing_rows:
            session.add(
                SettingOverride(
                    setting_class=setting_class,
                    key=key,
                    value=stored_value,
                    is_active=True,
                    updated_at=stamp,
                    updated_by=actor,
                )
            )
            continue
        # Prefer a row that already stores the canonical spelling so renaming
        # a legacy sibling does not collide with it on the unique index.
        keep = next((row for row in existing_rows if row.key == key), existing_rows[0])
        for row in existing_rows:
            if row is keep:
                continue
            # Delete in-session (do not use Manager.delete_where): that path
            # commits, and extras must share this batch's single transaction.
            await session.delete(row)
        keep.key = key
        keep.value = stored_value
        keep.is_active = True
        keep.updated_at = stamp
        keep.stamp(actor)
        session.add(keep)
    await session.commit()
    return provenance


async def apply_class_overrides(
    *,
    request: Request,
    session: AsyncSession,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    body: SettingsPatch,
    actor: str,
    applicability: ApplicabilityPredicate | None = None,
) -> list[SettingResponse]:
    """Validate, persist and publish a batch of overrides for one local class.

    The whole of ``PATCH /{setting_class}`` for a locally-wired class, minus the
    routing and the remote-class branch. It exists as a function — not only
    inlined in :func:`build_settings_router`'s ``patch_settings`` closure —
    because an app that owns its settings class (:class:`AppOwnedClassEntry`)
    may also need to serve its *own* narrower ``/config`` endpoint: the shared
    settings router is admin-gated, and not every caller of an app's
    configuration is a PMM Extensions admin (e.g. the principal behind the
    pmm-managed ``--extensions-token`` flag). A second implementation of
    "validate the batch, write it atomically, republish the snapshot, rebind"
    is exactly the kind of duplicate that drifts into two different validation
    rules.

    Phase A validates every key — existence on the class, HOT classification,
    type and constraint coercion — and rejects the whole batch on any failure,
    so a partial write is not a state this can reach. Phase B persists, publishes
    the new snapshot inline, and fires the rebind callbacks for the changed keys
    so a hot target rebinds without waiting for the background refresher.

    :param request: The incoming request; its ``app.state`` carries the sub-app's
        rebind-callback registry.
    :param session: The sub-app's database session.
    :param setting_class: The settings class the override targets.
    :param settings_cls: The Pydantic settings class being overridden.
    :param proxy: The proxy wrapping it.
    :param body: The batch of ``{key: value, ...}`` overrides.
    :param actor: The username recorded on every row this call writes, and
        reported back on each response.
    :param applicability: Optional predicate driving ``is_applicable`` on the
        returned rows; ``None`` marks every field applicable.
    :return: One :class:`SettingResponse` per applied key, in input order.
    :raises HTTPUnprocessableEntityException: If any key fails validation; no
        rows are written.
    :raises IntegrityError: When the replay of a batch that lost the
        unique-index race conflicts again, which leaves nothing written.
    """
    to_apply = _validate_patch_body(settings_cls=settings_cls, proxy=proxy, body=body)
    provenance_by_key = await _persist_overrides(
        session=session, settings_cls=settings_cls, to_apply=to_apply, actor=actor
    )
    previous = proxy.get_snapshot()
    await publish_snapshot(proxy, session, settings_cls)
    await _fire_inline_rebind_callbacks(request, setting_class, proxy, previous)
    field_meta_by_key = _applied_field_meta(settings_cls, to_apply)
    return [
        _settings_response_from_field(
            setting_class=setting_class,
            settings_cls=settings_cls,
            proxy=proxy,
            field_meta=field_meta_by_key[key],
            provenance=provenance_by_key[key],
            applicability=applicability,
        )
        for key, _ in to_apply
    ]


async def clear_class_override(
    *,
    request: Request,
    session: AsyncSession,
    setting_class: str,
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    key: str,
) -> None:
    """Revert one override row for a local class to its YAML/env value.

    The counterpart to :func:`apply_class_overrides`, extracted for the same
    reason: an app serving its own ``/config`` needs "put this back the way the
    deployment configured it", and without it an operator who once set a value can
    only ever set another one — "no override" stops being reachable.

    Idempotent: clearing a key with no row succeeds. A ``NOT_OVERRIDABLE`` field
    with no row answers 409, because the intent cannot be satisfied rather than
    having already been satisfied.

    :param request: The incoming request; its ``app.state`` carries the sub-app's
        rebind-callback registry.
    :param session: The sub-app's database session.
    :param setting_class: The settings class the field belongs to.
    :param settings_cls: The Pydantic settings class being reverted.
    :param proxy: The proxy wrapping it.
    :param key: The field name, or a ``__``-delimited nested key.
    :raises HTTPNotFoundException: If ``key`` is not a field on the class.
    :raises HTTPConflictException: If the field is NOT_OVERRIDABLE and has no row.
    :raises HTTPUnprocessableEntityException: If ``key`` names a NESTED_ONLY parent.
    """
    key = canonical_override_key(settings_cls, key)
    field_meta = _field_meta_or_404(settings_cls, key)
    token = setting_class_token(settings_cls)
    rows = await override_rows_for_key(
        session,
        settings_cls=settings_cls,
        setting_class=token,
        key=key,
    )
    _assert_key_deletable(settings_cls, field_meta, has_override_row=bool(rows))
    await _delete_override_rows(session, token, rows)
    previous = proxy.get_snapshot()
    await publish_snapshot(proxy, session, settings_cls)
    await _fire_inline_rebind_callbacks(request, setting_class, proxy, previous)
