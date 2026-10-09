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

"""Define tests for the app.core.utils.openapi module."""

import copy
from enum import Enum
from types import SimpleNamespace
from typing import Any

import fastapi._compat.v2 as fastapi_compat_v2
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, computed_field, create_model

from app.core.pagination.models import PaginatedResponse
from app.core.utils.openapi import (
    _app_namespaced_defs_name,
    _strip_core_ref_ids,
    generate_tag_prefixed_unique_id,
    merge_openapi_documents,
    namespace_app_schema_names,
    namespaced_openapi,
)


def _make_route(name, path, tags):
    """Build a minimal stand-in for a FastAPI ``APIRoute`` for ID generation.

    ``generate_unique_id`` only reads ``name``, ``path_format``, ``tags``, and
    ``methods`` — a ``SimpleNamespace`` with those attributes is enough and
    keeps the test free of a full FastAPI app setup.
    """
    return SimpleNamespace(
        name=name,
        path_format=path,
        tags=tags,
        methods={"GET"},
        operation_id=None,
    )


def _spec(
    *,
    paths=None,
    tags=None,
    schemas=None,
    security_schemes=None,
    info=None,
    openapi_version="3.1.0",
):
    """Build a minimal OpenAPI document dict for merge tests."""
    components = {}
    if schemas is not None:
        components["schemas"] = schemas
    if security_schemes is not None:
        components["securitySchemes"] = security_schemes
    doc = {
        "openapi": openapi_version,
        "info": info or {"title": "primary", "version": "1.0.0"},
        "paths": paths or {},
    }
    if tags is not None:
        doc["tags"] = tags
    if components:
        doc["components"] = components
    return doc


def _paginated_wrapper(inner_ref: str) -> dict[str, Any]:
    """Build a ``PaginatedResponse[...]`` schema body wrapping ``inner_ref``.

    Mirrors the shape Pydantic v2 emits for ``PaginatedResponse(BaseModel,
    Generic[T])`` (``items: list[T]``): the wrapped model sits at
    ``properties.items.items.$ref``.
    """
    return {
        "type": "object",
        "title": "PaginatedResponse[BackupTaskResponse]",
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": f"#/components/schemas/{inner_ref}"},
            },
            "total": {"type": "integer"},
        },
    }


class TestGenerateTagPrefixedUniqueId:
    """Exercise ``generate_tag_prefixed_unique_id`` operation-ID slug generation."""

    def test_untagged_route_falls_back_to_default_id(self) -> None:
        """Fall back to FastAPI's default operation ID for an untagged route."""
        route = _make_route("list_things", "/things", [])
        assert generate_tag_prefixed_unique_id(route) == "list_things_things_get"

    def test_string_tag_is_prefixed(self) -> None:
        """Slugify a plain string tag and prefix it to the base ID."""
        route = _make_route("list_things", "/things", ["inventory"])
        assert (
            generate_tag_prefixed_unique_id(route) == "inventory_list_things_things_get"
        )

    def test_enum_tag_uses_enum_value(self) -> None:
        """Use an ``Enum`` tag's ``.value``, not its ``repr``."""

        class TagEnum(Enum):
            EXTENSIONS = "extensions"

        route = _make_route("list_things", "/things", [TagEnum.EXTENSIONS])
        assert (
            generate_tag_prefixed_unique_id(route)
            == "extensions_list_things_things_get"
        )

    def test_non_alphanumeric_tag_collapses_to_underscores(self) -> None:
        """Collapse spaces, slashes, and punctuation in a tag to a single ``_``."""
        route = _make_route("list_things", "/things", ["Periodic Tasks / jobs"])
        assert (
            generate_tag_prefixed_unique_id(route)
            == "periodic_tasks_jobs_list_things_things_get"
        )

    def test_pure_punctuation_tag_falls_back_to_default(self) -> None:
        """Fall back to the default ID when a tag's slug is empty after sanitization."""
        route = _make_route("list_things", "/things", ["---"])
        assert generate_tag_prefixed_unique_id(route) == "list_things_things_get"


class TestMergeOpenapiDocuments:
    """Exercise ``merge_openapi_documents`` union and collision handling."""

    def test_merge_disjoint_paths(self) -> None:
        """Merge two specs with no path overlap to the union of paths."""
        primary = _spec(paths={"/a": {"get": {"summary": "a"}}})
        secondary = _spec(paths={"/b": {"get": {"summary": "b"}}})
        merged = merge_openapi_documents(primary, secondary)
        assert set(merged["paths"]) == {"/a", "/b"}

    def test_merge_tags_dedup_by_name(self) -> None:
        """Keep duplicate tag names once; the primary description wins."""
        primary = _spec(tags=[{"name": "shared", "description": "from primary"}])
        secondary = _spec(
            tags=[
                {"name": "shared", "description": "from secondary"},
                {"name": "extra"},
            ]
        )
        merged = merge_openapi_documents(primary, secondary)
        names = [t["name"] for t in merged["tags"]]
        assert names.count("shared") == 1
        assert "extra" in names
        shared = next(t for t in merged["tags"] if t["name"] == "shared")
        assert shared["description"] == "from primary"

    def test_merge_schemas_identical_dedup(self) -> None:
        """Keep identical schemas under the same name once, without renaming."""
        same = {"type": "object", "properties": {"x": {"type": "integer"}}}
        primary = _spec(schemas={"Item": same})
        secondary = _spec(
            paths={
                "/b": {
                    "get": {"responses": {"200": {"$ref": "#/components/schemas/Item"}}}
                }
            },
            schemas={"Item": same},
        )
        merged = merge_openapi_documents(primary, secondary)
        assert set(merged["components"]["schemas"]) == {"Item"}
        # secondary $ref untouched
        ref = merged["paths"]["/b"]["get"]["responses"]["200"]["$ref"]
        assert ref == "#/components/schemas/Item"

    def test_merge_schemas_collision_renamed_and_refs_rewritten(self) -> None:
        """Rename the secondary schema and rewrite its $refs when bodies differ."""
        primary = _spec(
            schemas={
                "Item": {"type": "object", "properties": {"x": {"type": "integer"}}}
            }
        )
        secondary = _spec(
            paths={
                "/b": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Item"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            schemas={
                "Item": {"type": "object", "properties": {"y": {"type": "string"}}},
                "Wrapper": {
                    "type": "object",
                    "properties": {"item": {"$ref": "#/components/schemas/Item"}},
                },
            },
        )
        merged = merge_openapi_documents(primary, secondary)
        schemas = merged["components"]["schemas"]
        assert "Item" in schemas
        assert "Item_extensions" in schemas
        # primary kept
        assert schemas["Item"] == {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        }
        # secondary path ref rewritten
        new_ref = merged["paths"]["/b"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert new_ref == "#/components/schemas/Item_extensions"
        # secondary component internal ref rewritten
        assert (
            schemas["Wrapper"]["properties"]["item"]["$ref"]
            == "#/components/schemas/Item_extensions"
        )

    def test_merge_security_schemes_union(self) -> None:
        """Union non-overlapping security schemes and dedup identical entries."""
        common = {"type": "http", "scheme": "bearer"}
        primary = _spec(
            security_schemes={
                "Bearer": common,
                "ApiKey": {"type": "apiKey", "in": "header", "name": "X-A"},
            }
        )
        secondary = _spec(
            security_schemes={
                "Bearer": common,
                "OAuth2": {"type": "oauth2", "flows": {}},
            }
        )
        merged = merge_openapi_documents(primary, secondary)
        assert set(merged["components"]["securitySchemes"]) == {
            "Bearer",
            "ApiKey",
            "OAuth2",
        }

    def test_merge_security_schemes_collision_renamed_and_refs_rewritten(self) -> None:
        """Rename the secondary scheme and rewrite refs on conflicting bodies."""
        primary = _spec(
            security_schemes={"Bearer": {"type": "http", "scheme": "bearer"}},
        )
        secondary_doc = _spec(
            paths={
                "/secured": {
                    "get": {
                        "security": [{"Bearer": []}],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
            security_schemes={
                "Bearer": {"type": "apiKey", "in": "header", "name": "X-A"},
            },
        )
        secondary_doc["security"] = [{"Bearer": []}]

        merged = merge_openapi_documents(primary, secondary_doc)

        schemes = merged["components"]["securitySchemes"]
        assert set(schemes) == {"Bearer", "Bearer_extensions"}
        assert schemes["Bearer"] == {"type": "http", "scheme": "bearer"}
        assert schemes["Bearer_extensions"] == {
            "type": "apiKey",
            "in": "header",
            "name": "X-A",
        }
        # Operation-level security on secondary path rewritten.
        op_sec = merged["paths"]["/secured"]["get"]["security"]
        assert op_sec == [{"Bearer_extensions": []}]
        # Document-level security from secondary rewritten and merged.
        assert {"Bearer_extensions": []} in merged.get("security", [])

    def test_merge_security_schemes_rename_target_in_secondary_raises(self) -> None:
        """Raise when the secondary holds both colliding ``Bearer`` and ``Bearer_extensions``."""
        primary = _spec(
            security_schemes={"Bearer": {"type": "http", "scheme": "bearer"}}
        )
        secondary = _spec(
            security_schemes={
                "Bearer": {"type": "apiKey", "in": "header", "name": "X-A"},
                "Bearer_extensions": {"type": "oauth2", "flows": {}},
            }
        )
        with pytest.raises(ValueError, match="Bearer_extensions"):
            merge_openapi_documents(primary, secondary)

    def test_merge_security_schemes_rename_target_in_primary_raises(self) -> None:
        """Raise when the primary holds ``Bearer_extensions`` and the secondary's ``Bearer`` collides."""
        primary = _spec(
            security_schemes={
                "Bearer": {"type": "http", "scheme": "bearer"},
                "Bearer_extensions": {"type": "oauth2", "flows": {}},
            }
        )
        secondary = _spec(
            security_schemes={
                "Bearer": {"type": "apiKey", "in": "header", "name": "X-A"}
            }
        )
        with pytest.raises(ValueError, match="Bearer_extensions"):
            merge_openapi_documents(primary, secondary)

    def test_merge_preserves_info_from_primary(self) -> None:
        """Take the top-level info and openapi version from the primary spec."""
        primary = _spec(info={"title": "P", "version": "9.9"}, openapi_version="3.1.0")
        secondary = _spec(
            info={"title": "S", "version": "1.0"}, openapi_version="3.0.3"
        )
        merged = merge_openapi_documents(primary, secondary)
        assert merged["info"] == {"title": "P", "version": "9.9"}
        assert merged["openapi"] == "3.1.0"

    def test_merge_path_collision_raises(self) -> None:
        """Raise on path collisions; renaming paths is out of scope."""
        primary = _spec(paths={"/x": {"get": {}}})
        secondary = _spec(paths={"/x": {"post": {}}})
        with pytest.raises(ValueError, match="/x"):
            merge_openapi_documents(primary, secondary)

    def test_merge_schema_rename_target_in_secondary_raises(self) -> None:
        """Raise when the secondary holds both colliding ``Item`` and ``Item_extensions``."""
        primary = _spec(schemas={"Item": {"type": "string"}})
        secondary = _spec(
            schemas={
                "Item": {"type": "integer"},
                "Item_extensions": {"type": "boolean"},
            }
        )
        with pytest.raises(ValueError, match="Item_extensions"):
            merge_openapi_documents(primary, secondary)

    def test_merge_schema_rename_target_in_primary_raises(self) -> None:
        """Raise when the primary holds ``Item_extensions`` and the secondary's ``Item`` needs a rename."""
        primary = _spec(
            schemas={
                "Item": {"type": "string"},
                "Item_extensions": {"type": "number"},
            }
        )
        secondary = _spec(schemas={"Item": {"type": "integer"}})
        with pytest.raises(ValueError, match="Item_extensions"):
            merge_openapi_documents(primary, secondary)

    def test_merge_does_not_mutate_inputs(self) -> None:
        """Deep-copy inputs so merging does not change them."""
        primary = _spec(paths={"/a": {"get": {}}}, schemas={"S": {"type": "string"}})
        secondary = _spec(paths={"/b": {"get": {}}}, schemas={"S": {"type": "integer"}})
        p_snapshot = {
            "paths": dict(primary["paths"]),
            "schemas": dict(primary["components"]["schemas"]),
        }
        s_snapshot = {
            "paths": dict(secondary["paths"]),
            "schemas": dict(secondary["components"]["schemas"]),
        }
        merge_openapi_documents(primary, secondary)
        assert primary["paths"] == p_snapshot["paths"]
        assert primary["components"]["schemas"] == p_snapshot["schemas"]
        assert secondary["paths"] == s_snapshot["paths"]
        assert secondary["components"]["schemas"] == s_snapshot["schemas"]


class TestNamespaceAppSchemaNames:
    """Exercise the ``namespace_app_schema_names`` post-processing pass."""

    def test_namespaces_colliding_app_model_schema(self) -> None:
        """Namespace module-path-qualified app-model keys to ``<app>__<Class>`` and rewrite refs."""
        doc = _spec(
            paths={
                "/backups": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/app__extensions__apps__backup_pg__models__BackupTaskResponse"
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            schemas={
                "app__extensions__apps__backup_pg__models__BackupTaskResponse": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                },
                "app__extensions__apps__backup_mongo__models__BackupTaskResponse": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            },
        )
        out = namespace_app_schema_names(doc)
        schemas = out["components"]["schemas"]
        assert "backup_pg__BackupTaskResponse" in schemas
        assert "backup_mongo__BackupTaskResponse" in schemas
        assert not [k for k in schemas if k.startswith("app__")]
        ref = out["paths"]["/backups"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert ref == "#/components/schemas/backup_pg__BackupTaskResponse"

    def test_namespaces_generic_wrapper_by_inner_app(self) -> None:
        """Disambiguate generic-wrapper collisions by the wrapped model's app.

        The two ``PaginatedResponse[BackupTaskResponse]`` variants (positional
        ``___1``/``___2`` suffixes today) get distinct, suffix-free names derived
        from the app owning the inner model.
        """
        doc = _spec(
            schemas={
                "app__extensions__apps__backup_mongo__models__BackupTaskResponse": {
                    "type": "object"
                },
                "app__extensions__apps__backup_pg__models__BackupTaskResponse": {
                    "type": "object"
                },
                "app__core__pagination__models__PaginatedResponse_BackupTaskResponse___1": _paginated_wrapper(
                    "app__extensions__apps__backup_mongo__models__BackupTaskResponse"
                ),
                "app__core__pagination__models__PaginatedResponse_BackupTaskResponse___2": _paginated_wrapper(
                    "app__extensions__apps__backup_pg__models__BackupTaskResponse"
                ),
            },
        )
        out = namespace_app_schema_names(doc)
        schemas = out["components"]["schemas"]
        assert "backup_mongo__PaginatedResponse_BackupTaskResponse_" in schemas
        assert "backup_pg__PaginatedResponse_BackupTaskResponse_" in schemas
        assert not [k for k in schemas if "___" in k]
        assert not [k for k in schemas if k.startswith("app__")]
        # The inner $ref inside a renamed wrapper is rewritten to the namespaced model.
        inner = schemas["backup_mongo__PaginatedResponse_BackupTaskResponse_"][
            "properties"
        ]["items"]["items"]["$ref"]
        assert inner == "#/components/schemas/backup_mongo__BackupTaskResponse"

    def test_namespace_is_idempotent(self) -> None:
        """Return the same document whether the pass runs once or twice."""
        doc = _spec(
            schemas={
                "app__extensions__apps__backup_pg__models__BackupTaskResponse": {
                    "type": "object"
                },
                "app__extensions__apps__backup_mongo__models__BackupTaskResponse": {
                    "type": "object"
                },
                "app__core__pagination__models__PaginatedResponse_BackupTaskResponse___1": _paginated_wrapper(
                    "app__extensions__apps__backup_pg__models__BackupTaskResponse"
                ),
            },
        )
        once = namespace_app_schema_names(doc)
        twice = namespace_app_schema_names(once)
        assert twice == once

    def test_non_colliding_schemas_untouched(self) -> None:
        """Leave bare schema names and their non-colliding paginated wrappers alone."""
        doc = _spec(
            schemas={
                "ServiceResponse": {"type": "object"},
                "PaginatedResponse_ServiceResponse_": _paginated_wrapper(
                    "ServiceResponse"
                ),
            },
        )
        out = namespace_app_schema_names(doc)
        assert set(out["components"]["schemas"]) == {
            "ServiceResponse",
            "PaginatedResponse_ServiceResponse_",
        }

    def test_app_model_named_like_wrapper_is_namespaced(self) -> None:
        """Namespace an app model whose class name merely contains ``PaginatedResponse``.

        Such a key (``…__models__PaginatedResponseMeta``) is a plain app model, not a
        ``PaginatedResponse[...]`` generic instantiation, so it must be namespaced by
        the app rule rather than routed through wrapper detection (which would fail
        to find an inner ``$ref`` and raise).
        """
        doc = _spec(
            schemas={
                "app__extensions__apps__example__models__PaginatedResponseMeta": {
                    "type": "object",
                    "properties": {"page": {"type": "integer"}},
                },
            },
        )
        out = namespace_app_schema_names(doc)
        schemas = out["components"]["schemas"]
        assert set(schemas) == {"example__PaginatedResponseMeta"}

    def test_app_model_with_exact_wrapper_prefix_is_namespaced(self) -> None:
        """Namespace an app model whose class name exactly starts with ``PaginatedResponse_``.

        ``PaginatedResponse_Metadata`` shares the wrapper class-name stem but is a
        real class, not a ``PaginatedResponse[Metadata]`` instantiation, so it has no
        inner ``items.items`` ref. It must fall through to the plain app-model rule
        instead of being classified as a wrapper and raising ``ValueError``.
        """
        doc = _spec(
            schemas={
                "app__extensions__apps__example__models__PaginatedResponse_Metadata": {
                    "type": "object",
                    "properties": {"page": {"type": "integer"}},
                },
            },
        )
        out = namespace_app_schema_names(doc)
        assert set(out["components"]["schemas"]) == {
            "example__PaginatedResponse_Metadata"
        }

    def test_bare_wrapper_prefixed_name_untouched(self) -> None:
        """Leave a bare (non-app) model whose name starts with ``PaginatedResponse_`` alone.

        Without an app prefix and without a wrapper body shape there is nothing to
        attribute, so the key must pass through unchanged rather than raise.
        """
        doc = _spec(schemas={"PaginatedResponse_Metadata": {"type": "object"}})
        out = namespace_app_schema_names(doc)
        assert set(out["components"]["schemas"]) == {"PaginatedResponse_Metadata"}

    def test_raises_on_target_name_collision(self) -> None:
        """Raise when two qualified keys collapse to the same ``<app>__<Class>`` target."""
        doc = _spec(
            schemas={
                "app__extensions__apps__backup_pg__models__Foo": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}},
                },
                "app__extensions__apps__backup_pg__restore__models__Foo": {
                    "type": "object",
                    "properties": {"b": {"type": "string"}},
                },
            },
        )
        with pytest.raises(ValueError, match="backup_pg__Foo"):
            namespace_app_schema_names(doc)

    def test_namespace_does_not_mutate_input(self) -> None:
        """Deep-copy the input document and leave the original unchanged."""
        doc = _spec(
            schemas={
                "app__extensions__apps__backup_pg__models__BackupTaskResponse": {
                    "type": "object"
                },
                "app__extensions__apps__backup_mongo__models__BackupTaskResponse": {
                    "type": "object"
                },
            },
        )
        snapshot = copy.deepcopy(doc)
        namespace_app_schema_names(doc)
        assert doc == snapshot

    def test_core_app_model_gets_core_token(self) -> None:
        """Namespace a residual ``app__core__…__models__<Class>`` key under ``core__``.

        No such key exists in the shipped spec today, but the fallback keeps the
        scheme total: any core-owned model that ever collides is namespaced
        rather than left with its leaked module path.
        """
        doc = _spec(
            schemas={
                "app__core__widgets__models__Widget": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                },
            },
        )
        out = namespace_app_schema_names(doc)
        assert set(out["components"]["schemas"]) == {"core__Widget"}

    def test_core_generic_wrapper_with_non_app_inner_raises(self) -> None:
        """Raise when a core-qualified generic wrapper wraps a non-app model.

        The wrapper has no app to attribute its name to, so leaving it with a
        positional ``___N`` suffix would silently reintroduce install-set-
        dependent names; fail loudly instead.
        """
        doc = _spec(
            schemas={
                "Thing": {"type": "object"},
                "app__core__pagination__models__PaginatedResponse_Thing___1": _paginated_wrapper(
                    "Thing"
                ),
            },
        )
        with pytest.raises(ValueError, match="cannot namespace generic wrapper"):
            namespace_app_schema_names(doc)

    def test_malformed_app_key_without_models_boundary_raises(self) -> None:
        """Raise on an ``app__extensions__apps__…`` key lacking the ``__models__`` boundary.

        A well-formed app-model key always carries ``__models__``; a key without
        it is malformed and must fail loudly rather than produce a mangled name.
        """
        doc = _spec(schemas={"app__extensions__apps__backup_pg": {"type": "object"}})
        with pytest.raises(ValueError, match="__models__"):
            namespace_app_schema_names(doc)

    def test_target_clobbering_existing_bare_name_raises(self) -> None:
        """Raise when an app model's target name collides with a pre-existing bare name."""
        doc = _spec(
            schemas={
                "app__extensions__apps__backup_pg__models__Foo": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}},
                },
                "backup_pg__Foo": {
                    "type": "object",
                    "properties": {"b": {"type": "string"}},
                },
            },
        )
        with pytest.raises(ValueError, match="backup_pg__Foo"):
            namespace_app_schema_names(doc)


class TestNamespacedOpenapi:
    """Exercise the ``namespaced_openapi`` generator path and its helpers."""

    @pytest.mark.parametrize(
        ("core_ref", "expected"),
        [
            ("app.extensions.apps.backup_pg.models.BackupTaskResponse:140", None),
            (
                "app.core.pagination.models.PaginatedResponse:32[BackupTaskResponse:9]",
                "app.core.pagination.models.PaginatedResponse[BackupTaskResponse]",
            ),
        ],
    )
    def test_strip_core_ref_ids(self, core_ref: str, expected: str | None) -> None:
        """Strip per-component object ids, keeping generic brackets intact."""
        if expected is None:
            expected = "app.extensions.apps.backup_pg.models.BackupTaskResponse"
        assert _strip_core_ref_ids(core_ref) == expected

    @pytest.mark.parametrize(
        ("core_ref_no_id", "expected"),
        [
            (
                "app.extensions.apps.backup_pg.models.BackupTaskResponse",
                "backup_pg__BackupTaskResponse",
            ),
            (
                "app.extensions.apps.backup_mongo.restore.models.RestoreTaskResponse",
                "backup_mongo__RestoreTaskResponse",
            ),
            (
                "app.extensions.apps.framework.responses.BackupPgCreateResponse",
                "framework__BackupPgCreateResponse",
            ),
            ("app.core.pagination.models.PaginatedResponse", None),
            ("app.inventory.models.ServiceResponse", None),
            ("app.core.pagination.models.PaginatedResponse[BackupTaskResponse]", None),
            # Bare app segment with no dotted class part yields no namespaced name.
            ("app.extensions.apps.backup_pg", None),
        ],
    )
    def test_app_namespaced_defs_name(
        self, core_ref_no_id: str, expected: str | None
    ) -> None:
        """Derive ``<app>__<Class>`` only for non-generic ``app.extensions.apps`` models."""
        assert _app_namespaced_defs_name(core_ref_no_id) == expected

    def test_namespaces_every_app_model(self) -> None:
        """Namespace all app models — colliding or not — and wrappers by owning app.

        Uses ``create_model`` with an explicit ``__module__`` so the models carry an
        ``app.extensions.apps.<app>`` module path. ``SoloResponse`` has no cross-app
        collision yet is still namespaced (``backup_pg__SoloResponse``), proving the
        name does not depend on the installed app set.
        """
        pg = create_model(
            "BackupTaskResponse",
            __module__="app.extensions.apps.backup_pg.models",
            id=(int, ...),
        )
        mongo = create_model(
            "BackupTaskResponse",
            __module__="app.extensions.apps.backup_mongo.models",
            id=(str, ...),
        )
        solo = create_model(
            "SoloResponse",
            __module__="app.extensions.apps.backup_pg.models",
            x=(int, ...),
        )

        app = FastAPI()

        @app.get(
            "/pg",
            response_model=PaginatedResponse[pg],  # ty: ignore[invalid-type-form]
        )
        def _pg() -> Any: ...

        @app.get(
            "/mongo",
            response_model=PaginatedResponse[mongo],  # ty: ignore[invalid-type-form]
        )
        def _mongo() -> Any: ...

        @app.get("/solo", response_model=solo)
        def _solo() -> Any: ...

        schemas = namespaced_openapi(app)["components"]["schemas"]

        assert "backup_pg__BackupTaskResponse" in schemas
        assert "backup_mongo__BackupTaskResponse" in schemas
        # Non-colliding app model is namespaced too (stable regardless of install set).
        assert "backup_pg__SoloResponse" in schemas
        assert not [k for k in schemas if k.startswith("app__")]
        # Generic wrappers disambiguated by the wrapped model's app, no ``___N``.
        assert "backup_pg__PaginatedResponse_BackupTaskResponse_" in schemas
        assert "backup_mongo__PaginatedResponse_BackupTaskResponse_" in schemas
        assert not [k for k in schemas if "___" in k]
        inner = schemas["backup_pg__PaginatedResponse_BackupTaskResponse_"][
            "properties"
        ]["items"]["items"]["$ref"]
        assert inner == "#/components/schemas/backup_pg__BackupTaskResponse"

    def test_namespaces_dual_mode_app_model(self) -> None:
        """Namespace both ``-Input`` and ``-Output`` variants of a dual-mode app model.

        A model carrying a ``computed_field`` serializes with an extra property it
        does not validate, so its input and output JSON schemas differ and Pydantic
        splits it into ``-Input``/``-Output`` variants. Both variants must still be
        app-namespaced (``backup_pg__DualModel-Input``/``-Output``) — the reason
        :meth:`_AppNamespacedJsonSchema.get_defs_ref` injects a mode-suffixed
        preferred name alongside the bare one. ``__module__`` is set in the class
        body so Pydantic bakes the ``app.extensions.apps`` path into the core ref at
        schema-build time (assigning it after creation is too late).
        """

        class DualModel(BaseModel):
            __module__ = "app.extensions.apps.backup_pg.models"
            id: int

            @computed_field
            @property
            def doubled(self) -> int:
                return self.id * 2

        app = FastAPI()

        @app.post("/in")
        def _in(body: DualModel) -> dict[str, str]:
            return {}

        @app.get("/out", response_model=DualModel)
        def _out() -> Any: ...

        schemas = namespaced_openapi(app)["components"]["schemas"]

        assert "backup_pg__DualModel-Input" in schemas
        assert "backup_pg__DualModel-Output" in schemas
        assert not [k for k in schemas if k.startswith("app__")]
        assert "DualModel-Input" not in schemas
        assert "DualModel-Output" not in schemas

    def test_preserves_cached_spec(self) -> None:
        """Leave the app's served ``openapi_schema`` cache untouched."""
        app = FastAPI()

        @app.get("/ping")
        def _ping() -> dict[str, str]:
            return {}

        live = app.openapi()
        assert app.openapi_schema is live
        namespaced_openapi(app)
        assert app.openapi_schema is live

    def test_restores_globals_when_openapi_raises(self) -> None:
        """Restore the patched generator and spec cache when ``app.openapi()`` raises mid-patch.

        ``namespaced_openapi`` swaps a process-global ``GenerateJsonSchema`` and
        nulls the app's ``openapi_schema`` under a lock, guarded by ``try/finally``.
        A failure inside the guarded ``app.openapi()`` must not leak the patched
        class or the nulled cache to the rest of the process.
        """
        app = FastAPI()

        @app.get("/x")
        def _x() -> dict[str, str]:
            return {}

        original_generator = fastapi_compat_v2.GenerateJsonSchema
        sentinel = {"cached": "spec"}
        app.openapi_schema = sentinel

        def _boom() -> dict[str, Any]:
            raise RuntimeError("openapi blew up")

        app.openapi = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="openapi blew up"):
            namespaced_openapi(app)

        assert fastapi_compat_v2.GenerateJsonSchema is original_generator
        assert app.openapi_schema is sentinel
