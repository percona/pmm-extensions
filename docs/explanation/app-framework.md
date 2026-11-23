# The app framework

This page explains how a PMM Extensions app is put together, and why. It has no steps. To build an app, follow [Build your first app](../tutorials/first-app.md).

## What an app is

An app is one tool that PMM shows under **Apps**: a form, the tasks the form creates, and the pages that list them. Each app is one Python package under `app/extensions/apps/`.

These terms appear on every page about apps.

| Term | Meaning |
|---|---|
| App | One tool under **Apps** in PMM. One package under `app/extensions/apps/`. |
| Task | One job that an app defines. It is created from the form and runs on a host. PMM Extensions stores its status and output. |
| Inventory | The services, schemas, tables and hosts that PMM Extensions knows. A form field can offer values from it. |
| Form model | The Python class that declares an app's form fields. It validates what a user submits, and the form is derived from it. |
| Form schema | The JSON description of the form that the UI renders. The app serves it at `GET /schema`. It is not a database schema. |
| Derived router | The HTTP routes that the framework builds from an app's definition. |
| Scaffolder | `make startapp`, which generates an app's files and its test. |
| Snippet | A script with parameters that the Snippet Manager app runs. |

## You describe the app, and the framework builds it

An app has a create form, a list of tasks and a detail page for each task. You write none of the routes behind them. You create one `TaskExecutionApp` object, and its arguments say what the app is. The framework derives the HTTP routes from that object: schema, list, detail, create, update, execute and delete.

Under the object sits a set of helpers that you can combine: schema derivation, response builders, cascade helpers, spec builders and the rules engine. When the arguments cannot express something, you use one of those helpers. You do not leave the framework. [App extension points](../reference/app-extension-points.md) lists them from the smallest change to the largest.

## `BaseApp` and `TaskExecutionApp`

`BaseApp` (`app/extensions/apps/framework/base.py`) is the smallest entry the registry accepts. It holds an identity, navigation metadata and an optional API router. Use it directly only for an app that does not create and run tasks.

`TaskExecutionApp` (`app/extensions/apps/framework/apps.py`) is for the common case: an app that creates, lists and runs tasks. It extends `BaseApp` with the form model, the views, the capabilities and the builder that turns a form into a task. Almost every app is a `TaskExecutionApp`.

One object defines a task app. This is the Checksums app:

<!-- src: app/extensions/apps/checksums/app.py :: app -->
```python
app = TaskExecutionApp(
    name="checksums",
    display_name="Checksums",
    item_display_name="checksum",
    uri_path="/checksums",
    css_class="checksums",
    nav_order=7,
    nav_icon=NavIcon.CHECK_CIRCLE,
    description="Run pt-table-checksum to verify MySQL replication consistency.",
    owner=OWNER,
    create_model=ChecksumsForm,
    views=checksums_views,
    payload_builder=build_checksums_payload,
    capabilities=AppCapabilities(update=True, delete=True),
    service_type=ServiceTypeEnum.MYSQL,
    list_filter=ListFilterConfig(status=True, service_type=True),
)
```

Four arguments do most of the work:

- `create_model` is the form model. [Form DSL](../reference/form-dsl.md) describes what it can hold.
- `views` holds the section titles, the list columns, the detail layout and the UI features.
- `capabilities` says which of the derived routes exist.
- `task_spec_builder` or `payload_builder` turns a validated form into a task.

The other arguments carry identity and navigation. `owner` decides which tasks the app lists: an app with its own owner lists only the tasks it created.

## How an app becomes active

No file wires an app into a router by hand. Activation is data. A settings file has one `MODULE_NAME` entry for each app, under `EXTENSIONS.APPS`:

<!-- src: settings.yaml -->
```yaml
  EXTENSIONS:
    ...
    APPS:
      ...
      - MODULE_NAME: checksums
```

Two settings files hold such a list, and they serve different readers:

- `settings.yaml` at the root of the repository is the list the test suite reads. The scaffolder adds a new app there.
- `sidecar/settings.yaml` is the list the released image ships. The image build removes every app package that this file does not name.

`ENABLED` defaults to `true`, and most entries omit it. An entry with `ENABLED: false` is discovered, and its routes answer HTTP 503 until an administrator enables the app.

At startup the registry reads the list in order. It imports each module and takes the `app` object that the module exports:

<!-- src: app/extensions/apps/framework/registry.py :: get_app_registry -->
```python
def get_app_registry() -> AppRegistry:
    ...
    return build_app_registry(extensions_settings.APPS)
```

The registry is built once for each process. The API router then mounts each app's derived router under `/api/apps/<key>`:

<!-- src: app/extensions/api/router.py :: build_apps_router -->
```python
    for app in registry:
        if app.api_router is None:
            continue
        ...
        apps_router.include_router(
            app.api_router,
            prefix=f"/{app.key}",
```

Inside PMM, the same routes are reached under `/extensions`, so an app's routes start with `/extensions/api/apps/<key>`.

## What "derived" means

You do not write `@router.get("/schema")` or `@router.post("/")`. You declare the app object, and the framework builds those routes from it:

- `GET /schema` comes from `create_model` and `views`.
- `POST /` validates the body against `create_model`, then calls the app's builder to make the task.
- `PUT` and `DELETE` exist when `capabilities` turns them on.

`PUT` and `DELETE` have two guards by default, and both answer HTTP 409. The first refuses to change a task whose `protected` flag is set. The second refuses to change a task that has a run in progress or waiting.

Because the routes are derived, one shared test suite can check them for every app. [Test an app](../how-to/test-an-app.md) covers that suite.

## How a form becomes a task

The derived `POST /` works in three phases:

1. It resolves the form's reference fields against the inventory. This is the only phase that calls the inventory API.
2. It builds a spec from the form and the resolved entities. This is your `task_spec_builder`, a function with no side effects.
3. It assembles the task that is submitted.

A spec says what runs on the host. `RunCommandSpec` runs a program that is already on the host, with arguments built from the form. `RunPythonSpec` sends a Python payload with the task and runs it.

`payload_builder` is the other route. It receives the form and the inventory API, and it returns the whole task. It replaces all three phases, so an app sets one builder or the other. Checksums uses `payload_builder` because two of its fields hold several inventory selections, and the resolve phase handles one identifier per field.

A task app needs no database migration. PMM Extensions stores every task in the same tables.

## Why you scaffold and do not copy

Some apps in the repository still carry wiring from before the framework. A copy of a whole app copies that wiring too. The scaffolder's templates under `app/extensions/apps/framework/templates/` do not have it.

So start every app with `make startapp`. To reuse a pattern from another app, such as a field or a rule, copy that one construct and not the module.

## A snippet or an app

PMM Extensions has two ways to offer a tool that runs on a host.

A script with parameters belongs in a snippet. The form is described in a YAML header inside the script, and the script goes into `snippets/` at the root of the repository. It needs no scaffolding. [Write a diagnostic script](../how-to/write-a-diagnostic-script.md) covers the header.

A tool belongs in an app when its form needs one of these:

- values from the inventory, such as a service, a schema, a table or a host
- a rule between fields, such as "this field is required when that one is set"
- validation of its own structure when the code loads

The line between the two is inventory and rules. A snippet is a script with parameters. An app is a typed model that the server validates and derives a form from.

## The three kinds of app

The scaffolder generates three kinds of app, which it calls flavors.

| Flavor | For | What you write |
|---|---|---|
| `task` | An app that runs a task built from a typed form. This is the common case. | A form model, the views and a builder. |
| `script` | A set of scripts that needs its own entry under **Apps**, its own routes and its own catalog. | A `ScriptSource`. Each script supplies its own form. |
| `base` | An app that does not create and run tasks. | A `BaseApp` with a router that you write. |

If you are not sure, start with `task`. [The scaffolder](../reference/scaffolder.md) lists what each flavor generates.
