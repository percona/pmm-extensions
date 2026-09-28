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

"""Enforce the App framework's contract invariants as pure detector functions.

Each detector takes a single registry input plane — a schema wire payload, a
:class:`~app.extensions.apps.framework.apps.TaskExecutionApp`, the
:class:`~app.extensions.apps.framework.registry.AppRegistry`, or a merged OpenAPI
document — and returns a list of human-readable violation strings (empty when the
invariant holds), so a pytest suite can iterate ``get_app_registry()`` and assert
the framework's invariants mechanically instead of relying on reviewer memory.

The transitional :func:`~app.extensions.apps.framework.form_dsl.check_form_conformance`
drift check is re-exported here so the conformance suite imports every check from
one module; it lives in ``form_dsl`` because it cannot reference
``TaskExecutionApp`` (``apps`` imports ``form_dsl``, so the reverse import would
close an import cycle).
"""

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel

from app.extensions.api.task_history_actors import TASK_ACTOR_FIELDS
from app.extensions.apps.framework.form_dsl import (
    check_form_conformance,
    derive_form_sections,
)
from app.extensions.apps.framework.responses import root_segment, serialized_field_names
from app.extensions.apps.framework.schema import Capabilities, ITEM_DISPLAY_NAME_KEYS

if TYPE_CHECKING:
    from app.extensions.apps.framework.apps import TaskExecutionApp
    from app.extensions.apps.framework.base import BaseApp

__all__ = [
    "CAPABILITY_RENDERED_CONTROLS",
    "check_actor_fields_resolvable",
    "check_capability_route_consistency",
    "check_child_app_registration",
    "check_form_conformance",
    "check_item_display_names_declared",
    "check_no_duplicate_capability_control",
    "check_route_collisions",
    "check_routes_documented",
    "check_schema_derivation_succeeds",
    "check_view_fields_reference_real_fields",
]

CAPABILITY_RENDERED_CONTROLS: dict[str, str] = {"alert_on_fail": "alert_on_fail"}
"""Map a schema-side capability flag to the reserved form-field name the FE renders for it."""


def _validate_capability_controls() -> None:
    """Fail fast when a rendered-control key is not a real ``Capabilities`` field."""
    unknown = set(CAPABILITY_RENDERED_CONTROLS) - set(Capabilities.model_fields)
    if unknown:
        raise RuntimeError(
            f"CAPABILITY_RENDERED_CONTROLS contains unknown capability keys: {sorted(unknown)}"
        )


_validate_capability_controls()


_HTTP_METHODS = frozenset(
    {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
)


def _iter_form_fields(schema_payload: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield every create-form field dict across root and entity forms.

    :param schema_payload: A plugin schema's wire payload.
    :yield: Each field dict from ``forms[].fields[]`` and
        ``entities[].forms[].fields[]``.
    """
    for section in schema_payload.get("forms") or ():
        yield from section.get("fields") or ()
    for entity in schema_payload.get("entities") or ():
        for section in entity.get("forms") or ():
            yield from section.get("fields") or ()


def check_no_duplicate_capability_control(
    schema_payload: Mapping[str, Any],
) -> list[str]:
    """Return violations where a capability-rendered control is also a form field.

    For each ``(capability, field)`` in :data:`CAPABILITY_RENDERED_CONTROLS`, flag
    the schema when the capability is enabled and a form field of that name also
    appears — the framework already renders the control from the capability, so
    the explicit field is a duplicate.

    :param schema_payload: A plugin schema's wire payload (the ``GET /schema``
        body, or ``app_schema.model_dump(by_alias=True, exclude_none=True)``).
    :return: One message per duplicated control; empty when none are found.
    """
    capabilities = schema_payload.get("capabilities") or {}
    field_names = {
        name
        for field in _iter_form_fields(schema_payload)
        if (name := field.get("name")) is not None
    }
    return [
        f"capability {cap!r} renders the {field!r} control, but the schema also "
        f"declares an explicit {field!r} form field (duplicate control)"
        for cap, field in CAPABILITY_RENDERED_CONTROLS.items()
        if capabilities.get(cap) and field in field_names
    ]


def check_item_display_names_declared(
    schema_payload: Mapping[str, Any],
) -> list[str]:
    """Return violations where a create-form schema reuses its title as a record noun.

    A schema that never had a record name declared serves its ``display_name``
    under both record keys, so the renderer labels one record with the plural
    title of the whole app ("New MySQL Backups"). Equality with ``display_name``
    is the enforceable rule: Pydantic reports a validator-filled field as set, so
    "was it declared?" cannot be asked of the payload. A name that merely differs
    but is still not a mid-sentence record noun passes here and is left to review.

    Each scope is judged against its own ``display_name`` and gated on its own
    form sections, so an entities-mode schema — whose root ``forms`` are empty by
    construction — is checked per entity and never at the root, which names no
    record of its own.

    :param schema_payload: A plugin schema's wire payload (the ``GET /schema``
        body, or ``app_schema.model_dump(by_alias=True, exclude_none=True)``).
    :return: One message per record name left equal to its ``display_name``;
        empty when the schema declares no create form or names both records.
    """
    scopes = [(str(schema_payload.get("name")), schema_payload)]
    scopes.extend(
        (f"{schema_payload.get('name')}.{entity.get('name')}", entity)
        for entity in schema_payload.get("entities") or ()
    )
    return [
        f"schema {scope_label!r} leaves {key!r} equal to its display_name "
        f"({display_name!r}); declare the record noun this form creates one of, "
        f"in mid-sentence form"
        for scope_label, scope in scopes
        if any(section.get("fields") for section in scope.get("forms") or ())
        for key in ITEM_DISPLAY_NAME_KEYS
        if (display_name := scope.get("display_name")) is not None
        and scope.get(key) == display_name
    ]


def check_capability_route_consistency(app: "TaskExecutionApp") -> list[str]:
    """Return violations where derived routes disagree with the capability flags.

    Assert each verb toggle on ``app.capabilities`` matches the presence of its
    *derived* route: ``list`` ↔ ``GET /``, ``create`` ↔ ``POST /``, ``execute`` ↔
    ``POST /{task_name}/execute``, ``update`` ↔ ``PUT /{param}``, ``delete`` ↔
    ``DELETE /{param}`` (``param`` is ``app.detail_path_param``). The execute route
    always uses the literal ``task_name`` path parameter, independent of
    ``detail_path_param`` — only the CRUD detail/update/delete routes adopt
    ``param``. A route contributed by ``extra_routes`` is the app's explicit escape
    hatch (a hybrid app derives only its list/schema and keeps every mutation
    custom with the matching capability off), so it is exempt: only a *derived*
    route contradicting a flag is a violation. The exemption is compared by
    *count* — the disabled signature's occurrences across ``api_router`` (derived
    plus custom, since ``build_router`` folds ``extra_routes`` in) against its
    ``extra_routes`` count — so a leaked derived route registered *alongside* a
    legitimate custom handler for the same ``(method, path)`` still surfaces
    (``present`` exceeds ``custom``) instead of being masked by set membership.

    :param app: The migrated app whose derived router is inspected.
    :return: One message per flag/route disagreement; empty when consistent or when
        the app is a script-source app (its derived surface is script-centric, not
        the verb-toggled CRUD surface this check asserts).
    """
    if app.script_source is not None:
        return []
    detail = f"/{{{app.detail_path_param}}}"
    expected = {
        "list": ("GET", "/"),
        "create": ("POST", "/"),
        "execute": ("POST", "/{task_name}/execute"),
        "update": ("PUT", detail),
        "delete": ("DELETE", detail),
    }
    routes = app.api_router.routes if app.api_router is not None else []
    present = Counter(
        (method, getattr(route, "path", ""))
        for route in routes
        for method in getattr(route, "methods", None) or ()
    )
    custom = Counter(
        (method, getattr(route, "path", ""))
        for extra in app.extra_routes
        for route in extra.routes
        for method in getattr(route, "methods", None) or ()
    )
    violations = []
    for verb, signature in expected.items():
        enabled = getattr(app.capabilities, verb)
        if enabled and not present[signature]:
            violations.append(
                f"capability {verb!r} is enabled but route {signature[0]} "
                f"{signature[1]} is absent"
            )
        elif not enabled and present[signature] > custom[signature]:
            violations.append(
                f"capability {verb!r} is disabled but derived route {signature[0]} "
                f"{signature[1]} is present"
            )
    return violations


def _detail_response_model(app: "TaskExecutionApp") -> type[BaseModel]:
    """Return the model the detail view renders against.

    The detail route renders the explicit ``detail_response_model`` when set, else
    the model inferred from a ``detail_response_builder``'s return annotation, else
    the shared ``response_model`` — so a plugin whose detail response is richer than
    its list response validates its detail-view paths against the richer model.

    :param app: The migrated app whose detail surface is resolved.
    :return: The response model the detail view's field paths resolve against.
    """
    if app.detail_response_model is not None:
        return app.detail_response_model
    if app.detail_response_builder is not None:
        annotation = getattr(app.detail_response_builder, "__annotations__", {}).get(
            "return"
        )
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
    return app.response_model


def _rendered_names(model: type[BaseModel]) -> frozenset[str]:
    """Return every name a field on ``model`` could render under.

    Unions :func:`~app.extensions.apps.framework.responses.serialized_field_names` (the
    wire names a ``model_dump(by_alias=True)`` emits) with each non-excluded
    field's own attribute name, so a field declaring a ``serialization_alias``
    still matches by the name a builder or caller would look it up under.

    :param model: The Pydantic response model class to inspect.
    :return: The frozenset of attribute and serialized names ``model`` exposes.
    """
    attribute_names = {
        name for name, field in model.model_fields.items() if not field.exclude
    }
    attribute_names.update(model.model_computed_fields)
    return serialized_field_names(model) | attribute_names


def check_actor_fields_resolvable(app: "TaskExecutionApp") -> list[str]:
    """Return a violation when an app renders actor fields with no username map.

    A list or detail response model that renders any of
    :data:`~app.extensions.api.task_history_actors.TASK_ACTOR_FIELDS` — whether under
    its own name or a ``serialization_alias`` — shows the stored user identifier
    unless ``response_context_provider`` resolves it, so an app that sets the
    provider to ``None`` while rendering one is flagged.

    :param app: The migrated app whose response models and provider are checked.
    :return: A single message naming the unresolved actor fields; empty when the
        app binds a provider, renders no actor field, or is a script-source app
        (it derives no model-first CRUD responses).
    """
    if app.script_source is not None or app.response_context_provider is not None:
        return []
    rendered = _rendered_names(app.response_model) | _rendered_names(
        _detail_response_model(app)
    )
    unresolved = [field for field in TASK_ACTOR_FIELDS if field in rendered]
    if not unresolved:
        return []
    return [
        f"response_context_provider is None, so {unresolved} render the stored "
        "user id instead of a display name"
    ]


def check_view_fields_reference_real_fields(app: "TaskExecutionApp") -> list[str]:
    """Return violations where a detail-view path's root is not a serialized field.

    Validate the root segment (before the first ``.``, stripped of any ``[N]``) of
    every ``detail_view`` field path against the names
    ``detail_model.model_dump(by_alias=True)`` emits (see
    :func:`~app.extensions.apps.framework.responses.serialized_field_names`), not
    ``model_fields`` alone. ``by_alias=True`` matches the derived detail route,
    which pins ``response_model_by_alias=True``. The model is the explicit
    ``detail_response_model``, the detail builder's inferred model, or the
    shared ``response_model`` — see :func:`_detail_response_model`. Paths rooted
    at ``data`` are exempt (the opaque task-payload dict whose sub-paths are
    free-form). ``list_view`` column keys are enforced at
    ``TaskExecutionApp`` construction against the same serialized-row plane, so
    they are not checked here.

    :param app: The migrated app whose detail-view paths are checked against its
        detail response model.
    :return: One message per unknown root segment; empty when all resolve or when
        the app is a script-source app (it derives no model-first detail view).
    """
    if app.script_source is not None:
        return []
    if app.views.detail_view is None:
        return []
    detail_model = _detail_response_model(app)
    response_fields = serialized_field_names(detail_model)
    paths = [
        field.path
        for section in app.views.detail_view.sections
        for field in section.fields
    ]
    return [
        f"detail_view field {path!r} references {root_segment(path)!r}, absent from "
        f"{detail_model.__name__}"
        for path in paths
        if root_segment(path) != "data" and root_segment(path) not in response_fields
    ]


def check_schema_derivation_succeeds(app: "TaskExecutionApp") -> list[str]:
    """Return a violation when the app's create model fails to derive a schema.

    Skip ``schema=`` passthrough apps (no create model). Otherwise derive the form
    sections and flag the app when derivation raises or yields no sections; a raise
    is converted to a message rather than propagated, so one broken definition
    cannot abort the whole conformance run.

    :param app: The migrated app whose ``create_model`` derivation is exercised.
    :return: A single-element list on failure; empty on success or skip (a
        ``schema=`` passthrough or a script-source app, neither of which has a
        ``create_model`` — both exit on the ``create_model is None`` check below).
    """
    if app.create_model is None:
        return []
    try:
        sections = derive_form_sections(app.create_model, app.views.layout)
    except Exception as exc:  # noqa: BLE001 - conformance must report, never raise
        return [f"schema derivation raised: {exc!r}"]
    if not sections:
        return ["schema derivation yielded no sections"]
    return []


def _iter_route_signatures(registry: Iterable["BaseApp"]) -> Iterator[tuple[str, str]]:
    """Yield the ``(mounted_path, method)`` signature of every registry route.

    :param registry: The apps to walk (the ``AppRegistry`` is one such iterable).
    :yield: Each ``("/api/apps/{key}{path}", method)`` pair.
    """
    for app in registry:
        router = app.api_router
        if router is None:
            continue
        for route in router.routes:
            path = getattr(route, "path", None)
            if path is None:
                continue
            for method in getattr(route, "methods", None) or ():
                yield (f"/api/apps/{app.key}{path}", method)


def check_route_collisions(registry: Iterable["BaseApp"]) -> list[str]:
    """Return violations where two registry routes share a ``(path, method)``.

    :param registry: The apps whose mounted route signatures are tallied (the
        ``AppRegistry`` is one such iterable).
    :return: One message per duplicated signature; empty when all are unique.
    """
    counts = Counter(_iter_route_signatures(registry))
    return [
        f"route collision: {method} {path} appears {count} times across the registry"
        for (path, method), count in sorted(counts.items())
        if count > 1
    ]


def check_child_app_registration(registry: Iterable["BaseApp"]) -> list[str]:
    """Return violations where a parent/child app binding is inconsistent.

    Assert the structural invariants of the ``child_apps`` mechanism: every app a
    parent declares in ``child_apps`` is itself registered (so it mounts and
    snapshots), names that parent via ``parent_key``, and carries a key scoped
    under the parent's namespace; and every app carrying a ``parent_key`` resolves
    to a registered parent. That a child owns no ``AppState`` row is a seed-time
    invariant checked against the DB, not by this pure detector.

    :param registry: The apps to check (the ``AppRegistry`` is one such iterable).
    :return: One message per binding inconsistency; empty when all resolve.
    """
    apps = list(registry)
    by_key = {app.key: app for app in apps}
    violations = []
    for app in apps:
        for child in app.child_apps:
            if child.parent_key != app.key:
                violations.append(
                    f"child app {child.key!r} of parent {app.key!r} declares "
                    f"parent_key {child.parent_key!r}"
                )
            if child.key not in by_key:
                violations.append(
                    f"child app {child.key!r} of parent {app.key!r} is not registered"
                )
            elif not child.key.startswith(f"{app.key}/"):
                violations.append(
                    f"child app {child.key!r} is not scoped under parent {app.key!r}"
                )
        if app.parent_key is not None and app.parent_key not in by_key:
            violations.append(
                f"app {app.key!r} has parent_key {app.parent_key!r} but no such "
                "parent is registered"
            )
    return violations


def check_routes_documented(openapi: Mapping[str, Any]) -> list[str]:
    """Return violations for operations carrying neither a summary nor a description.

    A floor, not an authored-docs gate: FastAPI auto-derives a ``summary`` from the
    route name, so this only flags the pathological operation that has neither.

    :param openapi: The merged plugin OpenAPI document.
    :return: One message per undocumented operation; empty when all carry one.
    """
    return [
        f"operation {method.upper()} {path} carries neither summary nor description"
        for path, item in (openapi.get("paths") or {}).items()
        for method, operation in item.items()
        if method in _HTTP_METHODS
        and isinstance(operation, Mapping)
        and not (operation.get("summary") or operation.get("description"))
    ]
