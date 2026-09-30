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

"""Test the App framework conformance detectors and the registry conformance suite.

Three layers:

* **Per-detector unit tests** drive each pure detector with small synthetic
  dicts / models, mirroring ``test_form_dsl_conformance.py``.
* **Synthetic ``TaskExecutionApp``** exercises the migrated-only and hard
  detectors against a real definition (clean plus deliberately-broken variants),
  so the logic is covered before any plugin is migrated.
* **Registry suite** iterates ``get_app_registry()`` and asserts the
  framework-contract checks hold over the live registry — the no-duplicate-control
  rule is what catches the duplicate ``alert_on_fail`` form field.
"""

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, computed_field, ConfigDict, Field

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.api.router import api_router
from app.extensions.apps.framework.apps import AppCapabilities, TaskExecutionApp, Views
from app.extensions.apps.framework.base import BaseApp
from app.extensions.apps.framework.conformance import (
    CAPABILITY_RENDERED_CONTROLS,
    check_actor_fields_resolvable,
    check_capability_route_consistency,
    check_child_app_registration,
    check_form_conformance,
    check_item_display_names_declared,
    check_no_duplicate_capability_control,
    check_route_collisions,
    check_routes_documented,
    check_schema_derivation_succeeds,
    check_view_fields_reference_real_fields,
)
from app.extensions.apps.framework.form_dsl import (
    AppFormModel,
    derive_app_schema,
    FormLayout,
    SectionLayout,
    Ui,
)
from app.extensions.apps.framework.form_dsl import (
    check_form_conformance as _form_dsl_check_form_conformance,
)
from app.extensions.apps.framework.registry import get_app_registry
from app.extensions.apps.framework.schema import (
    Capabilities,
    Column,
    DetailField,
    DetailSection,
    DetailView,
    ITEM_DISPLAY_NAME_KEYS,
    ListView,
)
from app.extensions.apps.framework.spec import ResolvedEntities, RunCommandSpec
from app.tasks.models import TaskHistoryStatusEnum
from tests.app.extensions import snapshot_utils as su
from tests.app.extensions.apps.framework.contract_suite import build_contract_client
from tests.app.extensions.apps.framework.kit import (
    MockInventoryAPI,
    MockTaskAPI,
    synth_app,
    synth_script_app,
)

_OWNER = "ARCHIVER"
_LAYOUT = FormLayout(sections=(SectionLayout(key="main", title="Main"),))
_LIST_VIEW = ListView(columns=[Column(key="name", label="Name")])
_DETAIL_VIEW = DetailView(
    sections=[
        DetailSection(title="Exec", fields=[DetailField(path="status", label="Status")])
    ]
)


class _CleanForm(AppFormModel):
    """Represent a synthetic create model with a task_name and an alert toggle."""

    task_name: Annotated[str, Ui(label="Name", section="main")]
    alert_on_fail: Annotated[bool, Ui(label="Alert", section="main")] = False


class _CleanResponse(BaseModel):
    """Represent the synthetic list/detail response model."""

    name: str
    status: TaskHistoryStatusEnum | None = None


class _DetailResponse(_CleanResponse):
    """Represent a detail response richer than the list response."""

    host: str | None = None


class _DivergenceResponse(_CleanResponse):
    """Carry an excluded field and a computed field on top of the clean list row."""

    secret: str = Field(exclude=True)

    @computed_field
    @property
    def label(self) -> str:
        """Return a derived label that serializes but is absent from ``model_fields``."""
        return self.name.upper()


class _AliasedResponse(_CleanResponse):
    """Carry a serialization-aliased field on top of the clean list row."""

    model_config = ConfigDict(populate_by_name=True)

    internal_host: str | None = Field(default=None, serialization_alias="wire_host")


def _detail_builder(
    task: object, *, status: object = None, context: object = None
) -> _DetailResponse:
    """Return a synthetic detail response; the detector never invokes it."""
    return _DetailResponse(name="x")


class _BadSectionForm(AppFormModel):
    """Represent a create model whose field names a section absent from the layout."""

    task_name: Annotated[str, Ui(label="Name", section="ghost")]


def _spec_builder(form: AppFormModel, resolved: ResolvedEntities) -> RunCommandSpec:
    """Return a trivial run-command spec; the detector tests never invoke it."""
    return RunCommandSpec(command="synth-cmd", args="")


def _build_app(**overrides: object) -> TaskExecutionApp:
    """Build a minimal create-model ``TaskExecutionApp`` with sane defaults."""
    kwargs = {
        "name": "synthetic",
        "uri_path": "/synthetic",
        "owner": _OWNER,
        "create_model": _CleanForm,
        "response_model": _CleanResponse,
        "views": Views(layout=_LAYOUT, list_view=_LIST_VIEW, detail_view=_DETAIL_VIEW),
        "task_spec_builder": _spec_builder,
        "capabilities": AppCapabilities(execute=False),
    }
    kwargs.update(overrides)
    return TaskExecutionApp(**kwargs)


def _post_root_router() -> APIRouter:
    """Return an extra router that reintroduces a ``POST /`` create route."""
    router = APIRouter()

    @router.post("/")
    async def _create() -> dict:
        return {}

    return router


def _get_root_router() -> APIRouter:
    """Return an extra router that supplies a custom ``GET /`` list route."""
    router = APIRouter()

    @router.get("/")
    async def _list() -> dict:
        return {}

    return router


def _derived_payload(*, capabilities: Capabilities) -> dict:
    """Return the wire payload of a schema derived from ``_CleanForm``."""
    schema = derive_app_schema(
        _CleanForm,
        _LAYOUT,
        name="synthetic",
        display_name="Synthetic",
        capabilities=capabilities,
        list_view=_LIST_VIEW,
    )
    return schema.model_dump(mode="json", by_alias=True, exclude_none=True)


# --- check_no_duplicate_capability_control ------------------------------------


def test_no_duplicate_control_silent_when_capability_off():
    """Assert the rule stays silent when the rendered capability is disabled."""
    payload = {
        "capabilities": {"alert_on_fail": False},
        "forms": [{"fields": [{"name": "alert_on_fail"}]}],
    }
    assert check_no_duplicate_capability_control(payload) == []


def test_no_duplicate_control_silent_when_field_absent():
    """Assert the rule stays silent when no matching form field exists."""
    payload = {
        "capabilities": {"alert_on_fail": True},
        "forms": [{"fields": [{"name": "other"}]}],
    }
    assert check_no_duplicate_capability_control(payload) == []


def test_no_duplicate_control_fires_on_capability_plus_field():
    """Assert the rule fires when an enabled capability duplicates a form field."""
    payload = {
        "capabilities": {"alert_on_fail": True},
        "forms": [{"fields": [{"name": "alert_on_fail"}]}],
    }
    assert any(
        "alert_on_fail" in w for w in check_no_duplicate_capability_control(payload)
    )


def test_no_duplicate_control_treats_missing_capabilities_as_empty():
    """Assert an absent ``capabilities`` key yields no violations."""
    payload = {"forms": [{"fields": [{"name": "alert_on_fail"}]}]}
    assert check_no_duplicate_capability_control(payload) == []


def test_no_duplicate_control_traverses_entity_forms():
    """Assert the field scan spans ``entities[].forms[].fields[]``."""
    payload = {
        "capabilities": {"alert_on_fail": True},
        "entities": [{"forms": [{"fields": [{"name": "alert_on_fail"}]}]}],
    }
    assert any(
        "alert_on_fail" in w for w in check_no_duplicate_capability_control(payload)
    )


def test_no_duplicate_control_silent_for_unrelated_entity_field():
    """Assert an entity form without the reserved field yields no violations."""
    payload = {
        "capabilities": {"alert_on_fail": True},
        "entities": [{"forms": [{"fields": [{"name": "x"}]}]}],
    }
    assert check_no_duplicate_capability_control(payload) == []


def test_no_duplicate_control_fires_on_real_derived_schema():
    """Assert a real derived schema with the capability + field trips the rule."""
    payload = _derived_payload(capabilities=Capabilities(alert_on_fail=True))
    assert check_no_duplicate_capability_control(payload) != []


def test_no_duplicate_control_silent_on_real_derived_schema_without_capability():
    """Assert the same form is clean when the capability is off."""
    payload = _derived_payload(capabilities=Capabilities(alert_on_fail=False))
    assert check_no_duplicate_capability_control(payload) == []


class _HiddenControlForm(AppFormModel):
    """Represent a create model that inherits the excluded capability-rendered control."""

    task_name: Annotated[str, Ui(label="Name", section="main")]


def test_no_duplicate_control_silent_when_control_excluded_from_schema():
    """Assert excluding the capability-rendered field clears the duplicate violation."""
    schema = derive_app_schema(
        _HiddenControlForm,
        _LAYOUT,
        name="synthetic",
        display_name="Synthetic",
        capabilities=Capabilities(alert_on_fail=True),
        list_view=_LIST_VIEW,
    )
    payload = schema.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert check_no_duplicate_capability_control(payload) == []


def test_capability_rendered_controls_maps_alert_on_fail():
    """Assert the registry only reserves the ``alert_on_fail`` control today."""
    assert CAPABILITY_RENDERED_CONTROLS == {"alert_on_fail": "alert_on_fail"}
    assert set(CAPABILITY_RENDERED_CONTROLS) <= set(Capabilities.model_fields)


def test_no_duplicate_control_silent_with_response_extras_builder(
    regular_user: CasdoorUser,
) -> None:
    """Assert a response_builder injecting extras never reintroduces a form control.

    The synth app's response_builder injects response-plane fields (``service_type``,
    a remapped ``created_by``) — a plane independent of the schema's form fields.
    With the ``alert_on_fail`` capability enabled and its control excluded from the
    form, the served schema must still carry no duplicate control.
    """
    app_def = synth_app()
    client = build_contract_client(
        app_def,
        user=regular_user,
        tasks_api=MockTaskAPI(),
        inventory_api=MockInventoryAPI(),
    )

    payload = client.get(f"/api/apps{app_def.uri_path}/schema").json()

    assert app_def.response_builder is not None
    assert check_no_duplicate_capability_control(payload) == []


class _ExecuteWrite(BaseModel):
    """Represent a synthetic execute request body."""


class _ExecuteResponse(BaseModel):
    """Represent a synthetic execute response carrying identity and run state."""

    task_name: str
    task_id: int
    status: TaskHistoryStatusEnum
    created_at: datetime


async def _get_by_cluster(cluster_name: str) -> object:
    """Resolve a task by a non-default ``cluster_name`` detail path parameter."""
    raise NotImplementedError


# --- check_capability_route_consistency ---------------------------------------


def test_capability_route_consistency_clean_app():
    """Assert a well-formed app's routes match its capability flags."""
    assert check_capability_route_consistency(_build_app()) == []


def test_capability_route_consistency_allows_custom_extra_route():
    """Assert a custom ``extra_routes`` route for a disabled flag is exempt.

    A hybrid app keeps a verb custom (capability off, route mounted via
    ``extra_routes``); that explicit escape hatch is not a violation — only a
    leaked *derived* route would be.
    """
    app = _build_app(
        capabilities=AppCapabilities(create=False, execute=False),
        extra_routes=(_post_root_router(),),
    )
    assert check_capability_route_consistency(app) == []


def test_capability_route_consistency_flags_leak_alongside_custom():
    """Assert a leaked derived route is caught even beside a custom handler.

    A disabled verb with a legitimate custom ``extra_routes`` handler is exempt,
    but a derived route that leaks onto the same ``(method, path)`` must still be
    reported. The detector compares route counts, so the extra ``POST /``
    occurrence in ``api_router`` beyond the ``extra_routes`` count surfaces
    rather than being masked by set membership.
    """
    app = _build_app(
        capabilities=AppCapabilities(create=False, execute=False),
        extra_routes=(_post_root_router(),),
    )
    app.api_router.routes.extend(_post_root_router().routes)
    violations = check_capability_route_consistency(app)
    assert any("create" in v and "POST" in v for v in violations)


def test_capability_route_consistency_execute_ignores_custom_detail_path_param():
    """Assert execute matches /{task_name}/execute under a custom detail path param.

    The CRUD detail/update/delete routes adopt ``detail_path_param``, but the
    derived execute route is always ``POST /{task_name}/execute``; the detector
    must not report a false absence when the two path parameters diverge.
    """
    app = _build_app(
        detail_path_param="cluster_name",
        get_task=_get_by_cluster,
        capabilities=AppCapabilities(execute=True),
        execute_write_model=_ExecuteWrite,
        execute_response_model=_ExecuteResponse,
    )
    assert check_capability_route_consistency(app) == []


def test_capability_route_consistency_allows_list_suppress():
    """Assert a ``list=False`` app with a custom ``GET /`` reports no violation."""
    app = _build_app(
        capabilities=AppCapabilities(execute=False, list=False),
        extra_routes=(_get_root_router(),),
    )
    assert check_capability_route_consistency(app) == []


def test_capability_route_consistency_flags_list_leak_alongside_custom():
    """Assert a leaked derived ``GET /`` is caught even beside a custom list route."""
    app = _build_app(
        capabilities=AppCapabilities(execute=False, list=False),
        extra_routes=(_get_root_router(),),
    )
    app.api_router.routes.extend(_get_root_router().routes)
    violations = check_capability_route_consistency(app)
    assert any("list" in v and "GET" in v for v in violations)


# --- check_child_app_registration ---------------------------------------------


def _child(key: str, *, parent_key: str) -> BaseApp:
    """Build a minimal parent-bound child ``BaseApp`` for conformance tests."""
    return BaseApp(
        key=key,
        name=key.replace("/", "_"),
        display_name=key,
        uri_path=f"/{key}",
        parent_key=parent_key,
    )


def _parent(key: str, *children: BaseApp) -> BaseApp:
    """Build a minimal parent ``BaseApp`` carrying ``children`` as ``child_apps``."""
    return BaseApp(
        key=key,
        name=key,
        display_name=key,
        uri_path=f"/{key}",
        child_apps=children,
    )


def test_child_registration_clean_binding():
    """Assert a registered parent/child pair reports no violation."""
    child = _child("backups/restore", parent_key="backups")
    parent = _parent("backups", child)
    assert check_child_app_registration([parent, child]) == []


def test_child_registration_flags_unregistered_child():
    """Assert a ``child_apps`` entry absent from the registry is flagged."""
    child = _child("backups/restore", parent_key="backups")
    parent = _parent("backups", child)
    violations = check_child_app_registration([parent])
    assert any("backups/restore" in v for v in violations)


def test_child_registration_flags_parent_key_mismatch():
    """Assert a child whose ``parent_key`` does not name its declaring parent is flagged."""
    child = _child("backups/restore", parent_key="wrong_parent")
    parent = _parent("backups", child)
    violations = check_child_app_registration([parent, child])
    assert violations


def test_child_registration_flags_orphan_parent_key():
    """Assert an app whose ``parent_key`` resolves to no registered parent is flagged."""
    orphan = _child("ghost/restore", parent_key="ghost")
    violations = check_child_app_registration([orphan])
    assert any("ghost" in v for v in violations)


# --- check_view_fields_reference_real_fields ----------------------------------


def test_view_fields_clean_app():
    """Assert columns and detail fields that resolve to real fields pass."""
    assert check_view_fields_reference_real_fields(_build_app()) == []


def test_view_fields_exempts_data_detail_paths():
    """Assert a detail path rooted at the opaque ``data`` dict is exempt.

    List-column keys are enforced at ``TaskExecutionApp`` construction now; the
    detector only checks detail-view paths and leaves ``data.*`` sub-paths
    free-form because ``data`` is an opaque task-payload dict.
    """
    app = _build_app(
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X",
                        fields=[DetailField(path="data.meta.command", label="C")],
                    )
                ]
            ),
        )
    )
    assert check_view_fields_reference_real_fields(app) == []


def test_view_fields_flags_unknown_detail_path():
    """Assert a detail path whose root segment is not a response field fires."""
    app = _build_app(
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X", fields=[DetailField(path="ghost.sub", label="G")]
                    )
                ]
            ),
        )
    )
    assert any("ghost" in w for w in check_view_fields_reference_real_fields(app))


def test_view_fields_validates_root_segment_only():
    """Assert a dotted path resolves on its root segment, not the full path."""
    app = _build_app(
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X", fields=[DetailField(path="status.sub", label="S")]
                    )
                ]
            ),
        )
    )
    assert check_view_fields_reference_real_fields(app) == []


def test_view_fields_resolve_against_detail_response_model():
    """Resolve a detail-only field against the richer detail model."""
    app = _build_app(
        detail_response_builder=_detail_builder,
        detail_response_model=_DetailResponse,
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X", fields=[DetailField(path="host", label="H")]
                    )
                ]
            ),
        ),
    )

    assert check_view_fields_reference_real_fields(app) == []


def test_view_fields_flags_excluded_detail_path():
    """Report a detail path rooted at a ``Field(exclude=True)`` attribute."""
    app = _build_app(
        response_model=_DivergenceResponse,
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X", fields=[DetailField(path="secret", label="S")]
                    )
                ]
            ),
        ),
    )
    violations = check_view_fields_reference_real_fields(app)
    assert any("secret" in v and "_DivergenceResponse" in v for v in violations), (
        violations
    )


def test_view_fields_accepts_computed_detail_path():
    """Accept a detail path rooted at a ``@computed_field`` that serializes."""
    app = _build_app(
        response_model=_DivergenceResponse,
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X", fields=[DetailField(path="label", label="L")]
                    )
                ]
            ),
        ),
    )
    assert check_view_fields_reference_real_fields(app) == []


def test_view_fields_aliased_root_uses_serialized_name():
    """Accept the serialized alias and report the attribute name."""
    accepted = _build_app(
        response_model=_AliasedResponse,
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X",
                        fields=[DetailField(path="wire_host", label="H")],
                    )
                ]
            ),
        ),
    )
    assert check_view_fields_reference_real_fields(accepted) == []

    flagged = _build_app(
        response_model=_AliasedResponse,
        views=Views(
            layout=_LAYOUT,
            list_view=_LIST_VIEW,
            detail_view=DetailView(
                sections=[
                    DetailSection(
                        title="X",
                        fields=[DetailField(path="internal_host", label="H")],
                    )
                ]
            ),
        ),
    )
    violations = check_view_fields_reference_real_fields(flagged)
    assert any("internal_host" in v for v in violations), violations


# --- check_schema_derivation_succeeds -----------------------------------------


def test_schema_derivation_succeeds_clean_app():
    """Assert a derivable create model yields no violation."""
    assert check_schema_derivation_succeeds(_build_app()) == []


def test_schema_derivation_flags_a_raise():
    """Assert a derivation error becomes a violation instead of propagating."""
    stub = SimpleNamespace(
        create_model=_BadSectionForm, views=SimpleNamespace(layout=_LAYOUT)
    )
    warnings = check_schema_derivation_succeeds(stub)
    assert warnings
    assert "deriv" in warnings[0].lower()


def test_schema_derivation_skips_passthrough_app():
    """Assert a ``schema=`` passthrough app (no create model) is skipped."""
    stub = SimpleNamespace(create_model=None, views=SimpleNamespace(layout=None))
    assert check_schema_derivation_succeeds(stub) == []


# --- check_actor_fields_resolvable --------------------------------------------


class _ActorResponse(_CleanResponse):
    """Represent a list/detail response that renders the task's creator."""

    created_by: str | None = None


class _ActorDetailResponse(_CleanResponse):
    """Represent a detail-only response that renders the task's last updater."""

    last_updated_by: str | None = None


class _AliasedActorResponse(_CleanResponse):
    """Represent a response that renders the task's creator under a wire alias."""

    model_config = ConfigDict(populate_by_name=True)

    created_by: str | None = Field(default=None, serialization_alias="creator")


def _actor_detail_builder(
    task: object, *, status: object = None
) -> _ActorDetailResponse:
    """Return a synthetic actor-bearing detail response; the detector never calls it."""
    return _ActorDetailResponse(name="x")


def test_actor_fields_resolvable_with_the_default_provider():
    """Assert an app rendering an actor field through the default provider passes."""
    assert (
        check_actor_fields_resolvable(_build_app(response_model=_ActorResponse)) == []
    )


def test_actor_fields_flags_an_opted_out_list_model():
    """Assert opting out of the provider while the list renders an actor is flagged."""
    app = _build_app(response_model=_ActorResponse, response_context_provider=None)

    violations = check_actor_fields_resolvable(app)

    assert len(violations) == 1
    assert "created_by" in violations[0]


def test_actor_fields_flags_an_opted_out_detail_model():
    """Assert an actor field rendered only on the detail model is flagged too."""
    app = _build_app(
        response_context_provider=None, detail_response_builder=_actor_detail_builder
    )

    violations = check_actor_fields_resolvable(app)

    assert len(violations) == 1
    assert "last_updated_by" in violations[0]


def test_actor_fields_flags_an_aliased_actor_field():
    """Assert an actor field renamed by a serialization alias is still flagged."""
    app = _build_app(
        response_model=_AliasedActorResponse, response_context_provider=None
    )

    violations = check_actor_fields_resolvable(app)

    assert len(violations) == 1
    assert "created_by" in violations[0]


def test_actor_fields_ignores_an_opted_out_app_rendering_no_actor():
    """Assert opting out is allowed for an app whose responses carry no actor."""
    assert (
        check_actor_fields_resolvable(_build_app(response_context_provider=None)) == []
    )


def test_actor_fields_skips_a_script_source_app():
    """Assert a script-source app, which derives no CRUD responses, is skipped."""
    app = synth_script_app(response_context_provider=None)

    assert check_actor_fields_resolvable(app) == []


# --- check_route_collisions ---------------------------------------------------


def test_route_collisions_flags_same_key_and_path():
    """Assert two apps sharing a key collide on every shared route."""
    apps = [
        _build_app().model_copy(update={"key": "dup"}),
        _build_app().model_copy(update={"key": "dup"}),
    ]
    assert check_route_collisions(apps) != []


def test_route_collisions_silent_across_distinct_keys():
    """Assert apps mounted under distinct keys never collide."""
    apps = [
        _build_app().model_copy(update={"key": "a"}),
        _build_app().model_copy(update={"key": "b"}),
    ]
    assert check_route_collisions(apps) == []


# --- check_routes_documented --------------------------------------------------


def test_routes_documented_flags_bare_operation():
    """Assert an operation with neither summary nor description is flagged."""
    assert check_routes_documented({"paths": {"/x": {"get": {}}}}) != []


def test_routes_documented_silent_with_summary():
    """Assert a summary-only operation passes the floor."""
    assert (
        check_routes_documented({"paths": {"/x": {"get": {"summary": "Do X"}}}}) == []
    )


def test_routes_documented_silent_with_description():
    """Assert a description-only operation passes the floor."""
    assert (
        check_routes_documented({"paths": {"/x": {"get": {"description": "Does X"}}}})
        == []
    )


def test_routes_documented_ignores_non_operation_keys():
    """Assert path-level keys (``parameters``) are not treated as operations."""
    openapi = {"paths": {"/x": {"parameters": [], "get": {"summary": "ok"}}}}
    assert check_routes_documented(openapi) == []


# --- transitional re-export ---------------------------------------------------


def test_check_form_conformance_is_reexported():
    """Assert the transitional drift check is reused, not reimplemented."""
    assert check_form_conformance is _form_dsl_check_form_conformance


# --- registry suite -----------------------------------------------------------

OPENAPI = su.build_plugins_openapi()
_SCHEMA_PATHS = set(su.discover_schema_paths(OPENAPI, set(su.configured_plugin_keys())))
_REGISTRY = get_app_registry()
_APPS = list(_REGISTRY)


def _schema_payload(app, test_client) -> dict | None:
    """Return the app's schema wire payload, or ``None`` when it exposes none."""
    if app.app_schema is not None:
        return app.app_schema.model_dump(mode="json", by_alias=True, exclude_none=True)
    path = f"{su.PLUGIN_PREFIX}/{app.key}/schema"
    if path in _SCHEMA_PATHS:
        response = test_client.get(path)
        assert response.status_code == status.HTTP_200_OK
        return response.json()
    return None


@pytest.mark.parametrize("registry_app", _APPS, ids=lambda app: app.key)
def test_registry_app_has_no_duplicate_capability_control(registry_app, test_client):
    """Assert no registry app declares a form field a capability already renders."""
    payload = _schema_payload(registry_app, test_client)
    if payload is None:
        pytest.skip(f"{registry_app.key} exposes no schema payload")
    assert check_no_duplicate_capability_control(payload) == []


@pytest.mark.parametrize("registry_app", _APPS, ids=lambda app: app.key)
def test_registry_app_declares_item_display_names(registry_app, test_client):
    """Assert no registry app with a create form names its records after itself.

    An app declaring neither record name serves its ``display_name`` under both
    keys, which is the defect the pair exists to remove. Apps whose schema
    declares no create form name no record and are skipped by the detector.
    """
    payload = _schema_payload(registry_app, test_client)
    if payload is None:
        pytest.skip(f"{registry_app.key} exposes no schema payload")
    assert check_item_display_names_declared(payload) == []


@pytest.mark.parametrize("registry_app", _APPS, ids=lambda app: app.key)
def test_registry_migrated_app_structural_checks(registry_app):
    """Assert each migrated ``TaskExecutionApp`` satisfies the structural checks."""
    if not isinstance(registry_app, TaskExecutionApp):
        pytest.skip(f"{registry_app.key} is not a migrated TaskExecutionApp")
    assert check_capability_route_consistency(registry_app) == []
    assert check_view_fields_reference_real_fields(registry_app) == []
    assert check_schema_derivation_succeeds(registry_app) == []
    assert check_actor_fields_resolvable(registry_app) == []


@pytest.mark.parametrize("registry_app", _APPS, ids=lambda app: app.key)
def test_registry_app_requires_apps_resolve(registry_app):
    """Assert every app's ``requires_apps`` is a tuple of registered app keys."""
    assert isinstance(registry_app.requires_apps, tuple)
    for dep_key in registry_app.requires_apps:
        assert isinstance(dep_key, str)
        assert _REGISTRY.get(dep_key) is not None, (
            f"{registry_app.key} requires unregistered app {dep_key!r}"
        )


def test_registry_has_no_route_collisions():
    """Assert no two registry routes share a ``(path, method)`` signature."""
    assert check_route_collisions(_REGISTRY) == []


def test_registry_child_app_registration():
    """Assert every parent/child binding in the live registry is consistent."""
    assert check_child_app_registration(_REGISTRY) == []


def test_registry_openapi_builds():
    """Assert the merged plugin OpenAPI document builds with paths."""
    assert OPENAPI.get("paths")


def test_registry_routes_are_documented():
    """Assert every plugin operation carries a summary or description floor."""
    assert check_routes_documented(OPENAPI) == []


def test_registry_transitional_check_runs_at_warning_level(caplog):
    """Run the reused drift check over any app exposing both a model and a schema.

    Warning-level: drift is logged, never failed. Dormant over today's all-legacy
    registry (no app carries a discoverable create model alongside a hand-written
    schema), and auto-activates when a migrated app first exposes both — at which
    point the dormancy assertion trips so the wiring gets a deliberate review.
    """
    activated = []
    with caplog.at_level(logging.WARNING):
        for app in _APPS:
            model = getattr(app, "create_model", None)
            if model is None or app.app_schema is None:
                continue
            activated.append(app.key)
            for warning in check_form_conformance(model, app.app_schema):
                logging.getLogger(__name__).warning(
                    "transitional drift in %s: %s", app.key, warning
                )
    assert activated == []


# --- check_item_display_names_declared ----------------------------------------


def test_item_display_names_skipped_when_the_schema_declares_no_create_form():
    """Assert a schema with no create form names no record and is skipped."""
    payload = {
        "name": "tasks",
        "display_name": "Tasks",
        "item_display_name": "Tasks",
        "item_display_name_plural": "Tasks",
        "forms": [],
    }
    assert check_item_display_names_declared(payload) == []


def test_item_display_names_skipped_when_every_form_section_is_empty():
    """Assert a section with no fields creates no record, so the rule stays silent."""
    payload = {
        "name": "hollow",
        "display_name": "Hollow",
        "item_display_name": "Hollow",
        "item_display_name_plural": "Hollow",
        "forms": [{"fields": []}],
    }
    assert check_item_display_names_declared(payload) == []


def test_item_display_names_pass_when_both_names_differ_from_the_title():
    """Assert a schema naming its record distinctly yields no violations."""
    payload = {
        "name": "mysql_backups",
        "display_name": "MySQL Backups",
        "item_display_name": "backup",
        "item_display_name_plural": "backups",
        "forms": [{"fields": [{"name": "service_id"}]}],
    }
    assert check_item_display_names_declared(payload) == []


def test_item_display_names_fire_once_per_name_left_equal_to_the_title():
    """Assert both defaulted record names are reported, each naming its own key."""
    payload = {
        "name": "mysql_backups",
        "display_name": "MySQL Backups",
        "item_display_name": "MySQL Backups",
        "item_display_name_plural": "MySQL Backups",
        "forms": [{"fields": [{"name": "service_id"}]}],
    }

    violations = check_item_display_names_declared(payload)

    assert violations
    flagged = sorted(
        key
        for key in ITEM_DISPLAY_NAME_KEYS
        if any(repr(key) in message for message in violations)
    )
    assert flagged == sorted(ITEM_DISPLAY_NAME_KEYS)
    assert all("mysql_backups" in message for message in violations)


def test_item_display_names_fire_on_the_singular_alone():
    """Assert a declared plural does not excuse a defaulted singular."""
    payload = {
        "name": "mysql_backups",
        "display_name": "MySQL Backups",
        "item_display_name": "MySQL Backups",
        "item_display_name_plural": "backups",
        "forms": [{"fields": [{"name": "service_id"}]}],
    }

    violations = check_item_display_names_declared(payload)

    assert len(violations) == 1
    assert "item_display_name'" in violations[0]


def test_item_display_names_pass_when_singular_is_declared_and_plural_derived():
    """Pass a declared singular whose derived plural differs from the title."""
    payload = {
        "name": "mysql_backups",
        "display_name": "MySQL Backups",
        "item_display_name": "backup",
        "item_display_name_plural": "backups",
        "forms": [{"fields": [{"name": "service_id"}]}],
    }

    assert check_item_display_names_declared(payload) == []


def test_item_display_names_checked_per_entity_against_its_own_title():
    """Assert an entity's record names are judged against the entity's display name."""
    payload = {
        "name": "inventory",
        "display_name": "Inventory",
        "item_display_name": "Inventory",
        "item_display_name_plural": "Inventory",
        "entities": [
            {
                "name": "nodes",
                "display_name": "Nodes",
                "item_display_name": "Nodes",
                "item_display_name_plural": "nodes",
                "forms": [{"fields": [{"name": "address"}]}],
            }
        ],
    }

    violations = check_item_display_names_declared(payload)

    assert len(violations) == 1
    assert "nodes" in violations[0]
    assert "item_display_name'" in violations[0]


def test_item_display_names_pass_for_a_fully_declared_entity_schema():
    """Assert an entities-mode schema naming each entity's record yields no violations."""
    payload = {
        "name": "inventory",
        "display_name": "Inventory",
        "item_display_name": "Inventory",
        "item_display_name_plural": "Inventory",
        "entities": [
            {
                "name": "nodes",
                "display_name": "Nodes",
                "item_display_name": "node",
                "item_display_name_plural": "nodes",
                "forms": [{"fields": [{"name": "address"}]}],
            }
        ],
    }
    assert check_item_display_names_declared(payload) == []


def test_item_display_names_message_names_what_to_add():
    """Assert the message carries both the offending key and the reused value."""
    payload = {
        "name": "mysql_backups",
        "display_name": "MySQL Backups",
        "item_display_name": "MySQL Backups",
        "item_display_name_plural": "backups",
        "forms": [{"fields": [{"name": "service_id"}]}],
    }

    message = check_item_display_names_declared(payload)[0]

    assert "item_display_name" in message
    assert "MySQL Backups" in message


@pytest.mark.parametrize("registry_app", _APPS, ids=lambda app: app.key)
def test_registry_app_declares_its_record_display_names(registry_app, test_client):
    """Assert every registry app with a create form names its record distinctly."""
    payload = _schema_payload(registry_app, test_client)
    if payload is None:
        pytest.skip(f"{registry_app.key} exposes no schema payload")
    assert check_item_display_names_declared(payload) == []


def _derived_execute_routes() -> list[APIRoute]:
    """Return every registered ``POST /{task_name}/execute`` route.

    Walk the config-built ``api_router`` for the same reason
    :func:`snapshot_utils.build_plugins_openapi` does: sibling conftests mutate
    the process-global ``extensions_app`` at import time.

    :return: The derived execute routes across every configured app.
    """
    app = FastAPI()
    app.include_router(api_router)
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/{task_name}/execute")
        and "POST" in route.methods
    ]


def test_every_derived_execute_route_declares_run_state():
    """Assert every execute response model carries the run state a poller needs.

    Every derived route shares the default ``TaskExecutionResponse`` today, so
    what this can catch is a plugin passing an ``execute_response_model=`` that
    omits the fields — the one way a derived execute route can lose them without
    its own test noticing. It matches on the derived ``/{task_name}/execute``
    path shape, so a hand-written execute route mounted through ``extra_routes``
    under a different path is out of its reach.
    """
    required = {"status", "created_at"}
    offenders: dict[str, list[str]] = {}
    for route in _derived_execute_routes():
        model = route.response_model
        declared = set(model.model_fields) if model is not None else set()
        if not required <= declared:
            offenders[route.path] = sorted(required - declared)

    assert offenders == {}


def test_derived_execute_routes_are_discovered():
    """Assert the sweep above walks a non-empty route set.

    A walk that silently matched nothing would report the contract as satisfied
    for every plugin at once.
    """
    assert _derived_execute_routes() != []
