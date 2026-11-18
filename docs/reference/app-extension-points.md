# App extension points

A `TaskExecutionApp` derives its routes from its arguments. This page lists the arguments that change or replace what is derived. The entries are ordered from the smallest change to the largest, and each one has an example from the repository.

For what "derived" means, see [The app framework](../explanation/app-framework.md). For the form itself, see [Form DSL](form-dsl.md).

A line that holds only `...` in an example stands for lines the example skips.

## `capabilities`

Turns derived routes on and off. The value is an `AppCapabilities` object.

| Field | Route | Default |
|---|---|---|
| `create` | `POST /` | on |
| `list` | `GET /` | on |
| `detail` | `GET /{task_name}` | on |
| `execute` | `POST /{task_name}/execute` | on |
| `update` | `PUT /{task_name}` | off |
| `delete` | `DELETE /{task_name}` | off |

`GET /schema` is always derived and has no flag.

A capability that is off removes the derived route, so a route from `extra_routes` can take its path.

<!-- src: app/extensions/apps/checksums/app.py :: app -->
```python
app = TaskExecutionApp(
    name="checksums",
    ...
    capabilities=AppCapabilities(update=True, delete=True),
```

## `extra_routes`

Adds routes that the framework does not derive, such as an approval route or a download. The value is a tuple of FastAPI `APIRouter` objects. They are mounted under the app's prefix, after the derived routes. A derived route therefore wins when two paths collide.

From this entry down, you write FastAPI route handlers, with its dependency injection and `async` handling.

The framework does not build the response of an extra route. A handler that returns a task resolves the user names itself: see [`response_context_provider`](#response_context_provider).

<!-- src: app/extensions/apps/snippets/app.py :: app -->
```python
app = TaskExecutionApp(
    name="snippets",
    ...
    extra_routes=(approval_router, maintenance_router, artifact_router),
)
```

<!-- src: app/extensions/apps/snippets/extra_routes.py :: maintenance_router -->
```python
maintenance_router = APIRouter()


@maintenance_router.post(
    "/refresh", dependencies=[IsApiAuthenticated, IsManualSyncEnabled]
)
async def snippets_api_refresh(
    user: ApiAdminUser, session: SessionDep
) -> RefreshResponse:
```

## Response builders

Replace how a derived route builds its response body. The route itself stays derived.

| Argument | Replaces |
|---|---|
| `response_builder` | The body of each task in the list and create responses. |
| `response_model` | The model of that body. Set it to the class that `response_builder` returns. Construction rejects a mismatch. |
| `detail_response_builder` | The body of the detail response. |
| `detail_response_model` | The model of the detail body. |

Every builder must accept a `context` keyword. The framework rejects one that does not when the app is constructed.

<!-- src: app/extensions/apps/backup_pg/app.py :: app -->
```python
app = TaskExecutionApp(
    name="backup_pg",
    ...
    response_model=BackupTaskResponse,
    ...
    response_builder=build_backup_pg_api_task_response,
    detail_response_builder=build_backup_pg_api_detail_response,
    detail_response_model=BackupTaskDetailResponse,
```

## Handlers and guards

Replace the handler of a derived `PUT` or `DELETE`, or change the checks that run before it.

| Argument | Meaning |
|---|---|
| `update_handler`, `delete_handler` | A complete handler that replaces the derived one. |
| `update_guard`, `delete_guard` | The checks before the derived handler. The default, `()`, applies the two framework guards: the protected-task check and the running-conflict check. A tuple of dependencies replaces them. `UNGUARDED` removes them. |

No app in the repository replaces a handler. Prefer a response builder when only the body differs.

## `response_context_provider`

Supplies a value that every response builder receives as its `context` keyword. The framework awaits the provider once for each request.

The default provider returns a mapping from user identifier to user name. With it, the default response builder shows names in `created_by` and `last_updated_by`.

<!-- src: app/extensions/apps/framework/apps.py :: TaskExecutionApp -->
```python
    response_context_provider: SkipValidation[Callable[[], Awaitable[Any]] | None] = (
        get_username_mapping
    )
```

Three rules follow from the default:

- A custom response builder shows user names only if it adds `task_actor_fields(task, context or {})` to its extra fields. The function is in `app.extensions.api.task_history_actors`.
- A handler in `extra_routes` that returns a task awaits `get_username_mapping()` itself and passes the result as `context`.
- A replacement provider must still supply the mapping. `None` leaves the identifiers as they are, and framework conformance rejects that for an app whose responses show those fields.

## `cascade`

Makes one create request produce a group of related tasks. The parent is the task the user asked for. A predecessor is a task that must run before the parent. A satellite is any other task created with the parent.

An app that needs full control builds a `CascadeCreatePlan` in a function of its own:

<!-- src: app/extensions/apps/alters/deps.py :: build_alters_cascade_plan -->
```python
async def build_alters_cascade_plan(
    body: AltersCreate,
    inventory_api: InventoryAPI,
    tasks_api: TaskAPI,
) -> CascadeCreatePlan:
    ...
    parent_task = await build_alters_task(body, inventory_api)
    pre_checks_template = await build_pre_checks_task_payload(
        parent_task, task_api=tasks_api
    )
    return CascadeCreatePlan(
        parent_write=parent_task,
        form=body,
        cascade=lambda api: cascade_create_alters_group(
            api, parent_task, pre_checks_template, body
        ),
    )
```

The helpers that such a function calls are in `app/extensions/apps/framework/cascade.py`:

| Helpers | Behavior on failure |
|---|---|
| `cascade_create_*` | Stop at the first failure, delete the tasks already created in reverse order, and raise the original exception. They return `None`. |
| `cascade_update_*`, `cascade_delete_*` | Continue, and return a `CascadeResult`. Call `raise_if_failed(op="update")` or `raise_if_failed(op="delete")` on it to turn a partial failure into HTTP 500. |

## `script_source`

Backs an app whose form comes from each script and not from a form model. The value is a `ScriptSource` (`app/extensions/apps/framework/script_source.py`). The app passes it in place of `create_model`.

| Field | Meaning |
|---|---|
| `script_dir` | The directory that holds the scripts. |
| `load_script` | Returns one script by file name, or raises the not-found exception. |
| `list_scripts` | Returns a page of scripts and the total. |
| `load_scripts` | Optional. Returns several scripts in one call. |
| `build_form_schema` | Builds the form schema of one script from its header. |
| `build_execution_meta` | Builds what the framework sends to the Tasks API for one run. |
| `list_response` | Turns a script into its row in the list response. |
| `static_schema` | Optional. The schema served at `GET /schema`. Without it the route is not registered. |
| `list_response_model` | Optional. The model of a list row. |
| `list_query_dep` | Optional. A dependency that adds the source's own filters to sorting and search. |
| `in_memory_list_query` | Whether the source sorts, searches and pages in memory. |

<!-- src: app/extensions/snippets/script_source.py :: snippet_source -->
```python
snippet_source = ScriptSource(
    script_dir=snippets_settings.SNIPPETS_DIR,
    load_script=_load_script,
    list_scripts=_list_scripts,
    load_scripts=_load_scripts,
    build_form_schema=_build_form_schema,
    build_execution_meta=_build_execution_meta,
    list_response=_list_response,
    static_schema=SNIPPETS_PLUGIN_SCHEMA,
    list_response_model=SnippetResponse,
    list_query_dep=get_snippet_list_query,
)
```

## `BaseApp`

The last step: an app that does not derive task routes at all. It keeps discovery by the registry, activation and navigation. You write its `api_router`. The `base` flavor of the scaffolder generates one.

A `BaseApp` is outside the shared contract suite, because that suite checks derived routes.

## What each flavor's generated test covers

| Flavor | The generated `test_contract.py` |
|---|---|
| `task` | Subclasses `DerivedRouterContractTests` and binds the app to `app_def`. The suite then checks every derived route. |
| `script` | Holds its own tests of the script routes, with a client from `build_contract_client`. |
| `base` | Holds its own tests, with a client that the file itself builds in `_client`. |

A `task` test class can set `create_body_overrides`: a mapping of field values that the suite uses in place of generated ones when it builds a create body. Use it for a field that a rule or a validator constrains. Only top-level fields can be set.

<!-- src: tests/app/extensions/apps/checksums/test_contract.py :: TestChecksumsContract -->
```python
class TestChecksumsContract(DerivedRouterContractTests):
    """Assert the checksums app's full derived HTTP surface, knob by knob."""

    app_def = checksums_app
    create_body_overrides = {"defaults_file": ""}
```
