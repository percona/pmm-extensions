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

"""Define OpenAPI helpers for predictable operation IDs, app schema-name namespacing, and spec merging."""

from __future__ import annotations

import copy
import re
import threading
from collections import Counter
from enum import Enum
from typing import Any, TYPE_CHECKING

import fastapi._compat.v2 as fastapi_compat_v2
from fastapi._compat.v2 import GenerateJsonSchema
from fastapi.utils import generate_unique_id as default_generate_unique_id
from pydantic.json_schema import DefsRef

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from fastapi.routing import APIRoute
    from pydantic.json_schema import CoreModeRef

_SCHEMA_REF_PREFIX = "#/components/schemas/"


def generate_tag_prefixed_unique_id(route: APIRoute) -> str:
    """Build a unique OpenAPI operation ID using the first tag plus FastAPI's default.

    Prefix FastAPI's generated ID with a slug from the first ``route.tags`` entry when
    tags exist to avoid collisions between similarly named handlers in different areas
    (for example ``restores_create`` under two plugins). Fall back to the stock
    FastAPI ID when the route has no tags.

    :param route: The FastAPI route being registered.
    :type route: fastapi.routing.APIRoute
    :return: A slug suitable for ``operationId`` in OpenAPI.
    :rtype: str
    """
    base = default_generate_unique_id(route)
    tags = route.tags
    if not tags:
        return base
    first = tags[0]
    label = first.value if isinstance(first, Enum) else str(first)
    prefix = re.sub(r"\W+", "_", label.strip()).strip("_").lower()
    if not prefix:
        return base
    return f"{prefix}_{base}"


def rewrite_schema_refs(node: Any, rename_map: dict[str, str]) -> None:
    """Walk a JSON-like structure and rewrite ``$ref`` values per ``rename_map``.

    Mutates ``node`` in place. Only rewrites refs pointing at
    ``#/components/schemas/<name>`` where ``<name>`` is in ``rename_map``.

    :param node: Any nested dict/list found in an OpenAPI document.
    :param rename_map: Mapping of old schema name → new schema name.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX):
            old = ref[len(_SCHEMA_REF_PREFIX) :]
            if old in rename_map:
                node["$ref"] = _SCHEMA_REF_PREFIX + rename_map[old]
        for value in node.values():
            rewrite_schema_refs(value, rename_map)
    elif isinstance(node, list):
        for item in node:
            rewrite_schema_refs(item, rename_map)


_APP_MODEL_PREFIX = "app__extensions__apps__"
_MODELS_SEP = "__models__"


def _split_app_model(key: str) -> tuple[str, str]:
    """Split an ``app__extensions__apps__<app>[__<sub>]__models__<Class>`` schema key.

    :param key: A module-path-qualified app-model schema name.
    :return: The top-level app token and the class-name portion.
    :raises ValueError: If ``key`` lacks the ``__models__`` module boundary.
    """
    rest = key.removeprefix(_APP_MODEL_PREFIX)
    left, sep, cls = rest.partition(_MODELS_SEP)
    if not sep:
        raise ValueError(
            f"cannot derive an app token from schema name {key!r}: "
            f"missing {_MODELS_SEP!r} module boundary"
        )
    return left.split("__", 1)[0], cls


_GENERIC_WRAPPER_STEM = "PaginatedResponse_"


def _class_portion(key: str) -> str:
    """Return the class-name portion of a schema ``key``.

    Strip the module path up to and including the ``__models__`` boundary when
    present; a bare (unqualified) key has no boundary and is returned whole.

    :param key: A ``components.schemas`` key.
    :return: The trailing class-name portion.
    """
    before, sep, after = key.partition(_MODELS_SEP)
    return after if sep else before


def _wrapper_inner_ref(schema: Any) -> str | None:
    """Return the wrapped model's schema name for a ``PaginatedResponse[...]`` body.

    The generic parameter sits at ``properties.items.items.$ref`` (from
    ``PaginatedResponse`` declaring ``items: list[T]``).

    :param schema: The candidate wrapper schema body.
    :return: The referenced schema name, or ``None`` when the shape does not match.
    """
    try:
        ref = schema["properties"]["items"]["items"]["$ref"]
    except KeyError, TypeError:
        return None
    if isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX):
        return ref[len(_SCHEMA_REF_PREFIX) :]
    return None


def _wrapper_target_from_inner(inner: str) -> str | None:
    """Return a generic-wrapper's namespaced name from its wrapped model ref, or ``None``.

    Attributes the wrapper to the app that owns the wrapped model, so both
    colliding ``PaginatedResponse[...]`` variants get distinct, positional-
    suffix-free names. Handles both the ``<app>__<Class>`` form emitted by the
    :class:`_AppNamespacedJsonSchema` generator and the raw
    ``app__extensions__apps__…__models__<Class>`` module-path form (a generator
    fallback). A bare, non-app inner (e.g. a core model) yields ``None``.

    :param inner: The wrapped model's schema name (the wrapper's inner ref).
    :return: The wrapper's ``<app>__PaginatedResponse_<Class>_`` name, or ``None``.
    """
    if inner.startswith(_APP_MODEL_PREFIX):
        app, cls = _split_app_model(inner)
        return f"{app}__PaginatedResponse_{cls}_"
    app, sep, cls = inner.partition("__")
    if sep and cls and not _class_portion(inner).startswith(_GENERIC_WRAPPER_STEM):
        return f"{app}__PaginatedResponse_{cls}_"
    return None


def _namespaced_target(key: str, schemas: dict[str, Any]) -> str | None:
    """Return the stable app-namespaced name for a schema ``key``, or ``None``.

    ``None`` means the key needs no rename (a bare, non-colliding name). App
    models themselves are namespaced upstream by :class:`_AppNamespacedJsonSchema`;
    this pass primarily renames generic wrappers (whose names Pydantic still
    emits with positional ``___N`` suffixes) and defensively covers any raw
    ``app__extensions__apps__…`` key the generator left behind.

    :param key: A ``components.schemas`` key from the generated spec.
    :param schemas: The full schema map, used to resolve generic-wrapper inners.
    :return: The target name, or ``None`` to leave the key unchanged.
    :raises ValueError: If a module-path-qualified generic wrapper cannot be
        attributed to an app (its inner model is not an app model).
    """
    # Detect wrappers by the ``PaginatedResponse_`` class-name stem *and* the
    # actual generic-wrapper body shape, not a bare substring: an ordinary app
    # model whose class name merely starts with ``PaginatedResponse_`` (e.g.
    # ``PaginatedResponse_Metadata``, a real class name, not a
    # ``PaginatedResponse[Metadata]`` instantiation) has no inner ``items.items``
    # ref, so it falls through to the plain app-model rule below.
    if _class_portion(key).startswith(_GENERIC_WRAPPER_STEM):
        inner = _wrapper_inner_ref(schemas[key])
        if inner is not None:
            target = _wrapper_target_from_inner(inner)
            if target is not None:
                return target
            if key.startswith("app__") and not key.startswith(_APP_MODEL_PREFIX):
                raise ValueError(
                    f"cannot namespace generic wrapper {key!r}: its wrapped model "
                    "is not an app model, so it has no app to attribute the name to"
                )
    if key.startswith(_APP_MODEL_PREFIX):
        app, cls = _split_app_model(key)
        return f"{app}__{cls}"
    if key.startswith("app__core__"):
        _, _, cls = key.partition(_MODELS_SEP)
        return f"core__{cls or key}"
    return None


def namespace_app_schema_names(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``doc`` with app-model schema names app-namespaced.

    Pydantic v2 falls back to module-path-qualified ``$defs`` names when two app
    models share a class name (for example ``BackupTaskResponse`` in both the
    backup_mongo and backup_pg apps), producing
    ``app__extensions__apps__backup_pg__models__BackupTaskResponse``. Those names leak
    the internal module path and — because Pydantic qualifies every member of a
    collision group — are keyed off the module of each model, not off which app
    "won" a short name. This pass rewrites them to explicit ``<app>__<Class>``
    names that are readable and stable regardless of which apps are installed:
    adding a new colliding app only adds a new key, never shifting existing ones.

    Generic ``PaginatedResponse[...]`` wrapper collisions (emitted with
    positional ``___1``/``___2`` suffixes) are disambiguated by the app owning
    the wrapped model. Bare, non-colliding names are left untouched. The pass is
    idempotent: re-running it over its own output is a no-op.

    :param doc: A generated OpenAPI document. Not mutated.
    :return: A deep-copied document with namespaced schema names and rewritten
        ``$ref`` values.
    :raises ValueError: If two keys collapse to the same target name, if a target
        clobbers an existing name, or if a qualified generic wrapper cannot be
        attributed to an app.
    """
    result = copy.deepcopy(doc)
    schemas = result.get("components", {}).get("schemas")
    if not schemas:
        return result

    rename_map = {}
    for key in schemas:
        target = _namespaced_target(key, schemas)
        if target is not None and target != key:
            rename_map[key] = target

    if not rename_map:
        return result

    targets = list(rename_map.values())
    untouched = set(schemas) - set(rename_map)
    counts = Counter(targets)
    clashes = {t for t, n in counts.items() if n > 1} | (set(targets) & untouched)
    if clashes:
        raise ValueError(
            "app schema-name namespacing produced colliding target names: "
            + ", ".join(sorted(clashes))
        )

    result["components"]["schemas"] = {
        rename_map.get(name, name): body for name, body in schemas.items()
    }
    rewrite_schema_refs(result, rename_map)
    return result


_APP_CORE_REF_PREFIX = "app.extensions.apps."


def _strip_core_ref_ids(core_ref: str) -> str:
    """Return a Pydantic ``core_ref`` with the ``:id`` object suffixes removed.

    Mirrors the component-splitting Pydantic itself uses so generic arguments
    (bracketed) are handled: ``app.extensions.apps.backup_pg.models.Foo:140[Bar:99]``
    becomes ``app.extensions.apps.backup_pg.models.Foo[Bar]``.

    :param core_ref: A Pydantic core-schema reference string.
    :return: The reference with per-component object ids stripped.
    """
    components = re.split(r"([\][,])", core_ref)
    return "".join(part.rsplit(":", 1)[0] for part in components)


def _app_namespaced_defs_name(core_ref_no_id: str) -> str | None:
    """Return the ``<app>__<Class>`` defs name for an app-owned model, or ``None``.

    Derives the app token from the top package under ``app.extensions.apps.`` and the
    class from the trailing qualname segment, so the name depends only on the
    model's own module path — stable regardless of which other apps are
    installed. Generic instantiations (bracketed) return ``None``: they are
    namespaced by the wrapper post-processing pass, which can attribute them to
    the app owning the wrapped model.

    :param core_ref_no_id: A core reference with object ids already stripped.
    :return: The namespaced defs name, or ``None`` for non-app / generic refs.
    """
    if not core_ref_no_id.startswith(_APP_CORE_REF_PREFIX):
        return None
    rest = core_ref_no_id[len(_APP_CORE_REF_PREFIX) :]
    # Need at least an app segment and a class segment (a dotted path); a
    # bracketed ``[...]`` marks a generic instantiation handled by the wrapper pass.
    if "[" in rest or "." not in rest:
        return None
    parts = rest.split(".")
    return f"{parts[0]}__{parts[-1]}"


class _AppNamespacedJsonSchema(GenerateJsonSchema):
    """Emit ``<app>__<Class>`` ``$defs`` names for every ``app.extensions.apps`` model.

    Subclasses FastAPI's ``GenerateJsonSchema`` (not Pydantic's) so FastAPI's own
    overrides — notably ``bytes_schema`` emitting ``contentMediaType`` — are
    preserved; extending Pydantic's base directly would silently change unrelated
    field schemas across every spec.

    FastAPI/Pydantic v2 name a model's schema by its bare class name and only
    fall back to a module-path-qualified name when two models collide. That
    makes an app model's schema name depend on which *other* apps are installed.
    This generator instead makes each app-owned model's name derive solely from
    its own module path, by prepending the app-namespaced name (and its
    input/output mode variant) as the highest-priority choices Pydantic's
    definition-remapping considers. Non-app models keep Pydantic's default
    naming untouched.
    """

    def get_defs_ref(self, core_mode_ref: CoreModeRef) -> DefsRef:
        """Prepend the app-namespaced defs name for app-owned models.

        :param core_mode_ref: The ``(core_ref, mode)`` pair Pydantic is naming.
        :return: The internal defs-ref key (unchanged from the base class); the
            preferred human-facing name is injected into the remapping choices.
        """
        result = super().get_defs_ref(core_mode_ref)
        namespaced = _app_namespaced_defs_name(_strip_core_ref_ids(core_mode_ref[0]))
        if namespaced is None:
            return result
        choices = self._prioritized_defsref_choices[result]
        # ``choices`` is ``[name, name_mode, …]`` where ``name_mode`` is
        # ``name + "-Input"``/``"-Output"``; derive the mode suffix from it so
        # dual-mode models get a namespaced variant without importing Pydantic's
        # private mode-title mapping.
        name, name_mode = choices[0], choices[1]
        mode_suffix = name_mode[len(name) :]
        preferred = DefsRef(self.normalize_name(namespaced))
        self._prioritized_defsref_choices[result] = [
            preferred,
            DefsRef(preferred + mode_suffix),
            *choices,
        ]
        return result


# Serializes the global ``GenerateJsonSchema`` monkey-patch below so concurrent
# spec generations cannot observe each other's partially patched state.
_NAMESPACE_PATCH_LOCK = threading.Lock()


def _generate_namespaced(
    app: FastAPI, generate: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Return the namespaced document produced by ``generate`` under the schema patch.

    Installs :class:`_AppNamespacedJsonSchema` for the duration of ``generate``
    (so every app model gets a stable ``<app>__<Class>`` name regardless of the
    installed app set), then runs :func:`namespace_app_schema_names` to rename
    generic wrappers by the app owning their wrapped model. ``app.openapi_schema``
    is nulled for the fresh computation and restored afterwards.

    The class swap mutates process-global FastAPI state, so the patch and the
    ``generate`` call it guards are held under a module-level lock; a concurrent
    generation would otherwise see the patched class or a half-restored global.
    The lock only serializes callers that route through here, which every
    ``app.openapi`` in this process does once :func:`install_namespaced_openapi`
    has run — so all live generation is cooperating and the swap is never observed
    outside a held lock. Callers are non-nested (the merged spec computes each
    sub-app's document sequentially, not one inside another), so the plain
    ``Lock`` needs no reentrancy.

    :param app: The FastAPI application whose cached schema is swapped out.
    :param generate: The zero-arg callable that produces the raw OpenAPI document.
    :return: A namespaced OpenAPI document.
    """
    with _NAMESPACE_PATCH_LOCK:
        original = fastapi_compat_v2.GenerateJsonSchema
        cached = app.openapi_schema
        fastapi_compat_v2.GenerateJsonSchema = _AppNamespacedJsonSchema
        app.openapi_schema = None
        try:
            doc = generate()
        finally:
            app.openapi_schema = cached
            fastapi_compat_v2.GenerateJsonSchema = original
    return namespace_app_schema_names(doc)


def namespaced_openapi(app: FastAPI) -> dict[str, Any]:
    """Return ``app``'s OpenAPI document with app schema names app-namespaced.

    Runs a fresh spec computation under :func:`_generate_namespaced`. The app's
    cached ``openapi_schema`` is preserved, so the live spec the app serves is
    unaffected.

    When :func:`install_namespaced_openapi` has run, ``app.openapi`` is the
    namespacing wrapper, which itself calls :func:`_generate_namespaced` under the
    non-reentrant patch lock. Driving that wrapper here would re-acquire the lock
    and deadlock, so the pre-install base generator stashed on ``app.state`` is
    used instead; a bare app that was never installed falls back to its own
    ``app.openapi``.

    :param app: The FastAPI application whose spec to namespace.
    :return: A namespaced OpenAPI document.
    """
    generate = getattr(app.state, "namespaced_base_openapi", app.openapi)
    return _generate_namespaced(app, generate)


def install_namespaced_openapi(app: FastAPI) -> None:
    """Override ``app.openapi`` so the live spec uses app-namespaced schema names.

    Makes the document ``app`` serves (and any spec merged from it) carry the
    same stable ``<app>__<Class>`` names as the committed fixtures, instead of
    the install-set-dependent module-path fallbacks the default generator emits
    on collision. The base ``app.openapi`` bound method is captured before the
    override and driven directly by :func:`_generate_namespaced`, so the
    computation never recurses through this override. The namespaced document is
    cached on ``app.openapi_schema`` on first access, exactly as FastAPI caches
    its own.

    :param app: The FastAPI application whose live OpenAPI endpoint to namespace.
    """
    base_openapi = app.openapi
    # Bridge the raw generator to namespaced_openapi so it can compute a fresh
    # spec without recursing through the wrapper installed below.
    app.state.namespaced_base_openapi = base_openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _generate_namespaced(app, base_openapi)
        return app.openapi_schema

    app.openapi = openapi


def _merge_schemas(
    p_schemas: dict[str, Any],
    s_schemas: dict[str, Any],
    rename_map: dict[str, str],
) -> None:
    """Fold ``s_schemas`` into ``p_schemas`` honouring ``rename_map`` collisions.

    :param p_schemas: The primary ``components.schemas`` mapping, mutated in place.
    :type p_schemas: dict[str, Any]
    :param s_schemas: The secondary ``components.schemas`` mapping to fold in.
    :type s_schemas: dict[str, Any]
    :param rename_map: Mapping of secondary schema name → renamed target.
    :type rename_map: dict[str, str]
    """
    for name, body in s_schemas.items():
        final_name = rename_map.get(name, name)
        if final_name in p_schemas and p_schemas[final_name] == body:
            continue
        p_schemas[final_name] = body


def _build_security_rename_map(
    p_security: dict[str, Any],
    s_security: dict[str, Any],
    secondary_suffix: str,
) -> dict[str, str]:
    """Return rename map for conflicting securityScheme names.

    Identical bodies dedup; differing bodies under the same name are renamed by
    appending ``secondary_suffix`` to the secondary entry. Raises if the renamed
    target collides with an existing name in either spec.

    :param p_security: The primary ``components.securitySchemes`` mapping.
    :type p_security: dict[str, Any]
    :param s_security: The secondary ``components.securitySchemes`` mapping.
    :type s_security: dict[str, Any]
    :param secondary_suffix: Suffix appended to a colliding secondary name.
    :type secondary_suffix: str
    :return: Mapping of original secondary name → renamed target. Empty when no
        collisions occur.
    :rtype: dict[str, str]
    :raises ValueError: If the renamed target already exists in either spec.
    """
    rename_map: dict[str, str] = {}
    for name, body in s_security.items():
        if name in p_security and p_security[name] != body:
            renamed = f"{name}{secondary_suffix}"
            if renamed in p_security or renamed in s_security:
                raise ValueError(
                    "OpenAPI securityScheme rename collision during merge: "
                    f"cannot rename {name!r} to {renamed!r}; target already exists"
                )
            rename_map[name] = renamed
    return rename_map


def _rewrite_security_refs(node: Any, rename_map: dict[str, str]) -> None:
    """Rewrite security scheme references in a JSON-like structure in place.

    Walks the structure looking for ``security`` keys whose value is a list of
    dicts mapping scheme name → scopes (per the OpenAPI security-requirement
    object). Each dict has its keys renamed per ``rename_map``.

    :param node: Any nested dict/list found in an OpenAPI document.
    :type node: Any
    :param rename_map: Mapping of old scheme name → new scheme name.
    :type rename_map: dict[str, str]
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "security" and isinstance(value, list):
                for req in value:
                    if not isinstance(req, dict):
                        continue
                    for old, new in rename_map.items():
                        if old in req:
                            req[new] = req.pop(old)
            else:
                _rewrite_security_refs(value, rename_map)
    elif isinstance(node, list):
        for item in node:
            _rewrite_security_refs(item, rename_map)


def _merge_security_schemes(
    p_components: dict[str, Any],
    s_security: dict[str, Any],
    rename_map: dict[str, str],
) -> None:
    """Fold ``s_security`` into ``p_components['securitySchemes']`` honouring renames.

    :param p_components: The primary ``components`` mapping, mutated in place.
    :type p_components: dict[str, Any]
    :param s_security: The secondary ``components.securitySchemes`` mapping.
    :type s_security: dict[str, Any]
    :param rename_map: Mapping of secondary scheme name → renamed target.
    :type rename_map: dict[str, str]
    """
    if not s_security:
        return
    p_security = p_components.setdefault("securitySchemes", {})
    for name, body in s_security.items():
        final_name = rename_map.get(name, name)
        if final_name in p_security and p_security[final_name] == body:
            continue
        p_security[final_name] = body


def _merge_tags(p_tags: list[Any], s_tags: list[Any]) -> list[Any]:
    """Return a deduped list of tags, primary entries winning on name conflicts.

    :param p_tags: The primary ``tags`` list.
    :type p_tags: list[Any]
    :param s_tags: The secondary ``tags`` list to fold in.
    :type s_tags: list[Any]
    :return: A new list with primary entries first followed by secondary entries
        whose ``name`` is not already present.
    :rtype: list[Any]
    """
    seen = {t["name"] for t in p_tags if isinstance(t, dict) and "name" in t}
    merged = list(p_tags)
    for tag in s_tags:
        if isinstance(tag, dict) and tag.get("name") in seen:
            continue
        if isinstance(tag, dict) and "name" in tag:
            seen.add(tag["name"])
        merged.append(tag)
    return merged


def _merge_top_level_security(p: dict[str, Any], s_security: Any) -> None:
    """Union the document-level ``security`` requirement list from secondary into primary.

    :param p: The primary OpenAPI document, mutated in place.
    :type p: dict[str, Any]
    :param s_security: The secondary document-level ``security`` list (or any
        falsy value, in which case this is a no-op).
    :type s_security: Any
    """
    if not s_security:
        return
    merged = list(p.get("security", []))
    for req in s_security:
        if req not in merged:
            merged.append(req)
    p["security"] = merged


def _prune_empty_components(p: dict[str, Any], p_components: dict[str, Any]) -> None:
    """Drop empty ``schemas`` / ``securitySchemes`` / ``components`` entries.

    :param p: The primary OpenAPI document, mutated in place when ``components``
        is emptied.
    :type p: dict[str, Any]
    :param p_components: The primary ``components`` mapping, mutated in place.
    :type p_components: dict[str, Any]
    """
    if not p_components.get("schemas"):
        p_components.pop("schemas", None)
    if "securitySchemes" in p_components and not p_components["securitySchemes"]:
        p_components.pop("securitySchemes")
    if not p_components:
        p.pop("components", None)


def merge_openapi_documents(
    primary: dict[str, Any],
    secondary: dict[str, Any],
    *,
    secondary_suffix: str = "_extensions",
) -> dict[str, Any]:
    """Merge two OpenAPI documents into one, primary winning on conflicts.

    The merge is deterministic and never silently drops content:

    * ``paths`` are unioned; a path key present in both raises ``ValueError``.
    * ``tags`` are unioned, deduped by ``name`` (primary entry wins).
    * ``components.schemas``: identical entries dedup; on name collision with a
      different body, the secondary entry is renamed by appending
      ``secondary_suffix`` and ``$ref`` values pointing at the renamed schema are
      rewritten throughout the secondary spec (including ``paths`` and
      ``components.schemas``; other ``components.*`` sections such as
      ``parameters``/``requestBodies``/``responses`` are not folded into the
      merged document and are dropped).
    * ``components.securitySchemes``: identical entries dedup; on name collision
      with a different body, the secondary entry is renamed by appending
      ``secondary_suffix`` and every security requirement in the secondary spec
      (operation-level and document-level ``security`` arrays) is rewritten to
      point at the renamed scheme.
    * Document-level ``security`` requirements from both specs are unioned
      (deduped by equality).
    * Top-level ``openapi`` version and ``info`` come from ``primary``.

    Inputs are deep-copied; neither argument is mutated.

    :param primary: The primary OpenAPI document (its ``info`` and ``openapi``
        version are preserved on the result).
    :type primary: dict[str, Any]
    :param secondary: The secondary OpenAPI document to fold into the primary.
    :type secondary: dict[str, Any]
    :param secondary_suffix: Suffix appended to a secondary schema name on
        collision with a different body. Defaults to ``"_extensions"``.
    :type secondary_suffix: str
    :return: A new merged OpenAPI document.
    :rtype: dict[str, Any]
    :raises ValueError: If two paths collide, or if a schema/securityScheme
        rename target already exists in either spec.
    """
    p = copy.deepcopy(primary)
    s = copy.deepcopy(secondary)

    p_paths = p.setdefault("paths", {})
    s_paths = s.get("paths", {})
    collisions = set(p_paths) & set(s_paths)
    if collisions:
        raise ValueError(
            "OpenAPI path collision during merge: " + ", ".join(sorted(collisions))
        )

    # Build schema rename map first so we can rewrite refs in secondary before
    # merging paths/components into primary.
    p_components = p.setdefault("components", {})
    p_schemas = p_components.setdefault("schemas", {})
    s_components = s.get("components", {})
    s_schemas = s_components.get("schemas", {})

    rename_map: dict[str, str] = {}
    for name, body in s_schemas.items():
        if name in p_schemas and p_schemas[name] != body:
            renamed = f"{name}{secondary_suffix}"
            if renamed in p_schemas or renamed in s_schemas:
                raise ValueError(
                    "OpenAPI schema rename collision during merge: "
                    f"cannot rename {name!r} to {renamed!r}; target already exists"
                )
            rename_map[name] = renamed

    if rename_map:
        rewrite_schema_refs(s, rename_map)

    s_security = s_components.get("securitySchemes", {})
    p_security = p_components.setdefault("securitySchemes", {}) if s_security else {}
    security_rename_map = _build_security_rename_map(
        p_security, s_security, secondary_suffix
    )
    if security_rename_map:
        _rewrite_security_refs(s, security_rename_map)

    p_paths.update(s_paths)
    _merge_schemas(p_schemas, s_schemas, rename_map)
    _merge_security_schemes(p_components, s_security, security_rename_map)
    _merge_top_level_security(p, s.get("security"))
    _prune_empty_components(p, p_components)

    p_tags = p.get("tags", [])
    s_tags = s.get("tags", [])
    if p_tags or s_tags:
        p["tags"] = _merge_tags(p_tags, s_tags)

    return p
