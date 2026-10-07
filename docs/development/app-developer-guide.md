# PMM Extensions App Developer Guide

PMM Extensions apps are the tools in the PMM Extensions sidebar — Checksums, MySQL Backups, Snippets,
and the rest. Each one presents a form, turns a submitted form into a task that
runs against your inventory, and shows the results. This guide takes you from
zero to a working app of your own, assuming no prior knowledge of the app
framework. You will write some Python, but most of it is filling in a scaffolded
skeleton by following the patterns shown here.

Before you start you need a running development setup — see
[Setting Up Your Development Environment](../../CONTRIBUTING.md#setting-up-your-development-environment).

> **In a hurry?** Jump to [§3 Quickstart](#3-quickstart) to scaffold a running app
> with `make startapp`, then come back for the concepts. Reading top to bottom is
> the recommended path: the Quickstart's very first choice — which scaffolder
> flavor to use — is the decision [§2 Snippet or framework app?](#2-snippet-or-framework-app)
> teaches.

The code examples are lifted from real, CI-exercised code in the PMM Extensions tree; each
carries an HTML comment naming its source file and symbol (e.g.
`<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->`) so you can
diff it against the source as the framework evolves. Long docstrings are
shortened to their summary line, and larger trims are noted inline. Examples
labelled `constructed` illustrate features no current app uses.

---

## Vocabulary

A few terms this guide uses throughout:

| Term | Meaning |
|---|---|
| **App** | One tool in the PMM Extensions sidebar: a form, the tasks it creates, and their list/detail pages. One package under `app/extensions/apps/`. |
| **Task** | One run of an app's job — created from the form, executed against a host or service, with stored status and output. |
| **Inventory** | PMM Extensions' registry of services, schemas, tables, and hosts. Form fields can offer dropdowns resolved from it. |
| **Form model** | The Python class declaring the app's form fields (the `create_model` knob) — both the validation of what users submit and the source of the rendered form. |
| **Form schema** | The JSON description of the form that the UI renders, served at `GET /schema`. Not a database schema. |
| **Knob** | A constructor argument on the app object (`create_model=...`, `capabilities=...`). You set knobs; the framework does the rest. |
| **Derived router** | The HTTP routes (list, create, execute, …) the framework generates from your knobs — you never write them by hand. |
| **Scaffolder** | `make startapp` — generates a complete app skeleton plus its test. |
| **Snippet** | A parameterized script run by the Snippet Manager — the lighter alternative when you don't need a full app ([§2](#2-snippet-or-framework-app)). |

---

## Table of contents

1. [Mental model](#1-mental-model)
   - [You describe the app; the framework builds it](#you-describe-the-app-the-framework-builds-it)
   - [`BaseApp` vs `TaskExecutionApp`](#baseapp-vs-taskexecutionapp)
   - [How the registry discovers and activates apps](#how-the-registry-discovers-and-activates-apps)
   - [What "derived router" means](#what-derived-router-means)
   - [The copy hazard](#the-copy-hazard--scaffold-never-copy-a-whole-app)
2. [Snippet or framework app?](#2-snippet-or-framework-app)
   - [The routing rule](#the-routing-rule) — including snippet frontmatter and
     [Diagnostic script authoring](diagnostic-script-authoring.md)
   - [Mapping the three scaffolder flavors](#mapping-the-three-scaffolder-flavors)
3. [Quickstart](#3-quickstart)
   - [What the scaffolder generates](#what-the-scaffolder-generates)
   - [What the scaffolder writes automatically](#what-the-scaffolder-writes-automatically)
   - [The manual steps that remain](#the-manual-steps-that-remain) — including
     [no database migration](#no-database-migration)
   - [Verify it end to end](#verify-it-end-to-end)
4. [Form DSL reference](#4-form-dsl-reference)
   - [`AppFormModel` / `TaskFormModel` and `Ui()`](#appformmodel--taskformmodel-and-ui)
   - [`ArgFormat`](#argformat)
   - [`Choices` and `Option`](#choices-and-option)
   - [`FieldWidget`](#fieldwidget)
   - [The four reference markers](#the-four-reference-markers)
   - [`FormLayout` and `SectionLayout`](#formlayout-and-sectionlayout)
   - [The predicate DSL (`rules.py`)](#the-predicate-dsl-rulespy)
   - [`Requires` / `Forbidden` — per-field gates](#requires--forbidden--per-field-gates)
5. [Escape-hatch ladder](#5-escape-hatch-ladder)
   - [Rung 1 — `AppCapabilities` flags](#rung-1--appcapabilities-flags) — which
     derived routes exist, and which are on by default
   - [Rung 2 — `extra_routes`](#rung-2--extra_routes)
   - [Rung 3 — handler overrides](#rung-3--handler-overrides)
   - [Rung 4 — `response_context_provider` and cascade hooks](#rung-4--response_context_provider-and-cascade-hooks)
   - [Rung 5 — fall back to `BaseApp`](#rung-5--fall-back-to-baseapp)
   - [Script-backed apps: `ScriptSource`](#script-backed-apps-scriptsource)
6. [Testing](#6-testing)
   - [The derived-router contract suite](#the-derived-router-contract-suite)
   - [The shared fixtures](#the-shared-fixtures)
   - [Testing a form rule](#testing-a-form-rule)
   - [Factory conventions](#factory-conventions)
   - [How conformance runs in CI](#how-conformance-runs-in-ci)

---

## 1. Mental model

### You describe the app; the framework builds it

Open the Checksums app in PMM Extensions: it has a create form, a list of runs, and a
detail page per run. None of that is hand-written. The framework *derives* the
entire HTTP surface — schema, list, detail, create, update, execute, delete —
from a single `TaskExecutionApp` object whose constructor arguments ("knobs")
describe *what* the app is.

Under that **declarative spine** sits a **toolbox** of composable helpers —
schema derivation, response builders, cascade helpers, spec builders, the rules
engine. When the knobs do not express something you need, you reach one rung
down the [escape-hatch ladder](#5-escape-hatch-ladder) into the toolbox, not
out of the framework entirely.

### `BaseApp` vs `TaskExecutionApp`

- **`BaseApp`** (`app/extensions/apps/framework/base.py`) is the minimal registry entry:
  identity, navigation metadata, and an optional API router. Reach for it
  directly only when your app is *not* a task-execution app (the bottom rung of the
  ladder).
- **`TaskExecutionApp`** (`app/extensions/apps/framework/apps.py`) is the declarative spine
  for the common case: an app that creates, lists, and runs **tasks**. It subclasses
  `BaseApp` and adds the create-model, views, capabilities, and spec-builder knobs
  from which the task router is derived. Almost every app you write is a
  `TaskExecutionApp`.

A minimal task app is one object. Here is the core of the Checksums app
definition (trimmed to the knobs this guide covers):

<!-- src: app/extensions/apps/checksums/app.py :: app -->
```python
app = TaskExecutionApp(
    name="checksums",
    display_name="Checksums",
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

Four knobs carry the weight: `create_model` (the form/body model, [§4](#4-form-dsl-reference)),
`views` (section titles, list columns, detail layout, and UI capability flags),
`capabilities` (which derived mutation routes to switch on, [§5](#5-escape-hatch-ladder)),
and the builder that turns a validated form into a task payload — `task_spec_builder`
or `payload_builder`, covered in [§3](#3-quickstart) below.

`capabilities` toggles six of the derived routes: **`create`, `list`, `detail`,
and `execute` default on; `update` and `delete` default off** — which is why a
fresh app has no PUT or DELETE until you ask for one. The seventh derived
route, `GET /schema`, is always on and not switchable.
[Rung 1](#rung-1--appcapabilities-flags) covers changing these.

The remaining knobs above carry identity and navigation — `uri_path`,
`css_class`, `nav_order`, `nav_icon`, `owner`, `service_type`, `list_filter`.
The scaffolded `app.py`'s docstring documents each one at the point you would
edit it; its defaults are fine until you care about sidebar placement and list
filtering.

### How the registry discovers and activates apps

Apps are **not** wired by hand into a router file. Activation is data, in
`settings.yaml`: one `MODULE_NAME` entry per app under `EXTENSIONS.APPS`. `ENABLED`
defaults to `true` and is normally omitted; an app opts out of shipping enabled
by setting it to `false`, as `topology` does:

<!-- src: settings.yaml -->
```yaml
default:
  EXTENSIONS:
    APPS:
      - MODULE_NAME: checksums
      ...
      - MODULE_NAME: topology
        ENABLED: false
```

At startup, `build_app_registry` walks the `EXTENSIONS.APPS` list in order, imports each
module, and uses its exported `app` object (a `BaseApp` / `TaskExecutionApp`):

<!-- src: app/extensions/apps/framework/registry.py :: get_app_registry -->
```python
@lru_cache(maxsize=1)
def get_app_registry() -> AppRegistry:
    """Return the process-wide registry built over ``extensions_settings.APPS``."""
    return build_app_registry(extensions_settings.APPS)
```

The registry is a lazy `@lru_cache` accessor, built once per process from
`extensions_settings.APPS`. `build_apps_router` (`app/extensions/api/router.py`) then iterates
that registry and mounts each app's derived router under `/api/apps/{key}`:

<!-- src: app/extensions/api/router.py :: build_apps_router -->
```python
    for app in registry:
        if app.api_router is None:
            continue
        apps_router.include_router(app.api_router, prefix=f"/{app.key}", ...)
```

An app registered with `ENABLED: false` is discovered but its routes are guarded by
a per-app enable check (a `require_app_enabled` dependency added in that loop) —
an admin turns it on from the sidebar's top-level **Apps** page (`/admin/apps`)
without a redeploy.

### What "derived router" means

"Derived router" is the framework's core move: instead of writing `@router.get("/schema")`,
`@router.post("/")`, `@router.put("/{task_name}")`, and so on by hand, you declare
the app object and the framework **derives** those routes from it. The
`GET /schema` form comes from the `create_model` and `views`; `POST /` validates the
body against `create_model` and turns it into a task through the app's
`task_spec_builder` or `payload_builder` ([§3](#3-quickstart) covers the two);
`PUT`/`DELETE` are switched on by `capabilities` and carry two default guards,
both answering HTTP 409: the **protected-task** guard refuses to mutate a task
whose `protected` flag is set (system-owned tasks set it so they cannot be
edited or deleted through the API), and the **running-conflict** guard refuses
to mutate a task that currently has a running or pending run. You do not see
the route functions — they are built for you from the knobs.

### The copy hazard — scaffold, never copy a whole app

> **⚠ Do not copy an existing app to start a new one.** The apps already in the
> tree carry historical wiring that is on its way out — copy a whole app and you
> inherit it. The only clean end-to-end starting point is the scaffolder
> (`make startapp`), whose templates under `framework/templates/` carry none of
> that baggage.

The rule: **scaffold with `make startapp`; never copy a whole app.** When you
want to *lift a pattern* from an existing app (a form field, a rule, a cascade
hook), copy only that **named construct** — never a whole module.

---

## 2. Snippet or framework app?

Before you scaffold, decide **what kind** of thing you are writing. PMM Extensions has two
ways to ship a runnable tool, and the scaffolder's three flavors map onto them.

### The routing rule

- **A flat, parameterized script** belongs in an **in-script YAML snippet** —
  the Snippet Manager runs it, and the form is described in a YAML header
  alongside the script. This needs no `make startapp` at all: drop the
  script, frontmatter included, into the existing Snippet Manager catalog at
  `snippets/` in the repo root. The frontmatter is a commented YAML block
  (each line prefixed `# `) opening and closing on `# ---`, carrying `title`,
  `description`, `sudo`, `allow_extra_args`, `diagnostic_categories`, and a
  `parameters` list whose entries give each form field's `name`, `type`,
  `label`, and `description`; the ~90 scripts already in that directory are the
  working reference — `snippets/disk_usage.sh` is a short one to copy the shape
  from. `diagnostic_categories` is required on every script and decides whether
  it appears in the Support diagnostics category browser; search reaches the
  script either way. [Diagnostic script authoring](diagnostic-script-authoring.md)
  gives the permitted values and the rule for choosing them. Choose this
  when the check is a single command whose only inputs are simple parameters.
- **Anything that needs inventory or rules between fields** belongs in a
  **framework app**. Choose this when the form must offer values resolved from
  inventory (service / schema / table / host), enforce cross-field rules ("this
  field is required only when that one is set"), or validate its shape when the
  code loads.

The dividing line is **inventory and invariants**. A snippet is
prose-and-parameters; a framework app is a typed model the server validates and
derives a form from.

### Mapping the three scaffolder flavors

`make startapp` offers three flavors that implement the rule:

| Flavor | Use when | What you write |
|---|---|---|
| `task` | The app runs a task built from a typed form (the common case) — inventory refs, cross-field rules, a derived schema. | Framework app: a form model (`create_model`) + `views` + a spec/payload builder. |
| `script` | The script-backed tool needs its own app identity — a sidebar entry, routes, and an isolated `ScriptSource` script catalog separate from the Snippet Manager's — not just another entry in the shared catalog. | Framework app wrapping a `ScriptSource`; per-script forms come from the script, not a `create_model`. |
| `base` | The app is not a task-execution app at all — it needs a custom router and does not fit the derived spine. | `BaseApp` with a hand-written `api_router` (the bottom rung of the ladder). |

If in doubt, start with `task`: it is the flavor the spine is built for, and you can
step down to `base` later if the derived router genuinely cannot express your app.

---

## 3. Quickstart

`make startapp` scaffolds a new app end to end. Three rules govern how it asks
you for things:

- **The wizard runs whenever standard input is a TTY** — with or without
  variables on the command line.
- **A variable you supply pre-fills and skips its own prompt**, and nothing
  else. Supplying variables does not turn the wizard off.
- **`NO_INPUT=1` (or piped/redirected stdin) skips the wizard entirely.** Every
  unset field then takes its default — except `NAME`, which has no default
  outside the wizard and must be supplied.

Either way a final confirmation gates the write.

```bash
make startapp NAME=myapp TYPE=task DISPLAY_NAME="My App" \
    DESCRIPTION="Run my check." NAV_ICON=CHECK_CIRCLE
```

Recognised variables:

| Variable | Meaning |
|---|---|
| `NAME` | The app's module name under `app/extensions/apps/`. No default — always required. |
| `TYPE` | Scaffolder flavor: `task` / `script` / `base` ([§2](#mapping-the-three-scaffolder-flavors)). Defaults to `task`. |
| `DISPLAY_NAME` | The name shown in the sidebar and page headings. |
| `DESCRIPTION` | One-line blurb for the app's landing card and `description` knob. |
| `GROUP` | Which sidebar group the app is listed under. |
| `SERVICE_TYPE` | The `ServiceTypeEnum` member the generated `ServiceRef` field filters inventory by (`MYSQL`, `MONGODB`, …). Defaults to `MYSQL`. |
| `NAV_ICON` | The `NavIcon` member for the sidebar entry. |
| `RUN_MODE` | `run-command` or `run-python`, task flavor only. See below. Defaults to `run-command`. |
| `COMMAND` | For `run-command`: the executable the generated spec builder invokes. |
| `PAYLOAD` | For `run-python`: a Python payload file to copy into the app. |
| `SCRIPT` | For the `script` flavor: a script file to seed the catalog with, replacing `snippets/sample.sh`. |
| `NO_INPUT` | Skip the wizard (see above). Triggered by *any* non-empty value — `NO_INPUT=0` still skips it; leave the variable unset to keep the wizard. |
| `ENABLE` | Register the app **enabled** in `settings.yaml` instead of disabled. Presence-triggered like `NO_INPUT`. |
| `DERIVE_UPDATE` / `DERIVE_DELETE` | Whether the generated `app.py` switches on the derived `update` / `delete` routes. Both default **true** here — note this is the opposite of `AppCapabilities`' own defaults ([Rung 1](#rung-1--appcapabilities-flags)), so a scaffolded app ships with PUT and DELETE derived; pass `0`, `false`, or `no` to leave them off. |

**`RUN_MODE` — the one choice the wizard makes you take early.** It selects
which kind of task envelope the generated `spec.py` builds:

- **`run-command`** (the default) runs an **executable on the target host**. The
  generated spec builder returns a `RunCommandSpec` — a command name plus an
  argument string assembled from your form's `ArgFormat` fields. Choose it when
  your app shells out to a binary (`pt-table-checksum`, `mysqldump`, a script
  on the host).
- **`run-python`** runs a **Python payload** shipped with the task. The spec
  builder returns a `RunPythonSpec` — a serialized config, a pip requirements
  string, and a `file://` payload URI. Choose it when the work is Python code
  you write rather than a command you invoke.

Keep `run-command` unless you are shipping a Python payload; it is the path the
rest of this guide's examples follow.

### What the scaffolder generates

For **`task`** (`RUN_MODE=run-command` shown; with `RUN_MODE=run-python` the
generated `spec.py` contains the run-python variant instead):

```text
app/extensions/apps/myapp/
  __init__.py
  app.py          # the TaskExecutionApp definition
  models.py       # the create-form model (marker DSL)
  spec.py         # the run-command spec builder
  views.py        # section layout, list/detail views, capability flags
tests/app/extensions/apps/myapp/
  __init__.py
  test_contract.py   # subclasses DerivedRouterContractTests
```

For **`script`**: `app.py`, `constants.py`, `__init__.py`, `source.py`,
`snippets/sample.sh` (replaced by the copied `--script` file when `SCRIPT=` is
supplied), plus `tests/__init__.py` and `tests/test_contract.py`.

For **`base`**: `api_routes.py`, `app.py`, `__init__.py`, `schema.py`, plus
`tests/__init__.py` and `tests/test_contract.py`.

### What the scaffolder writes automatically

The scaffolder registers the app in `settings.yaml` — and **only** `settings.yaml`.
It inserts a `EXTENSIONS.APPS` entry, **disabled** unless you pass `ENABLE`:

```text
Scaffolded 'task' app 'myapp':
  app:   <repo-root>/app/extensions/apps/myapp
  tests: <repo-root>/tests/app/extensions/apps/myapp

Registered 'myapp' DISABLED in settings.yaml. Manage it from the Apps page in
the sidebar (/admin/apps) once you have filled in the skeleton.
```

The scaffolder prints absolute paths (shown above with a `<repo-root>`
placeholder). In a dev venv that has `ruff` installed, a few auto-fix lines
(`Fixed 4 errors: ... I001 unsorted-imports`) precede this output — that is the
scaffolder tidying its own render, not errors in your app.

The resulting `settings.yaml` entry:

```yaml
      - MODULE_NAME: myapp
        ENABLED: false
```

### The manual steps that remain

The scaffolder does **not** finish the app for you. After scaffolding:

1. **Fill in the skeleton** — the generated files are stubs. For the `task`
   flavor: `models.py`, `spec.py`, and `views.py` — write your fields, spec
   builder, and views. For the `script` flavor: `source.py` plus the sample
   script under `snippets/` — replace the sample and curate the script
   catalog. For the `base` flavor: `api_routes.py` and `schema.py`, plus the
   bespoke React UI it requires (see the frontend paragraph below).
2. **Enable the app** — it is registered **disabled**. Turn it on from the
   sidebar's top-level **Apps** page (`/admin/apps`), or scaffold with `ENABLE`
   for a dev box.

**The scaffolded `spec.py` is right for almost every app — fill it in and move
on.** `payload_builder` is the exception, and only for multi-value reference
fields; the rest of this paragraph is why.

The derived `POST /` runs a **three-phase create path**: *resolve* the form's
reference fields against inventory (`resolve_refs`, the only step that touches
the inventory API), *build a spec* from the form plus those resolved entities
(your pure spec builder), then *assemble the envelope* into the `TaskWrite`
that gets submitted (`assemble_envelope`). The generated `spec.py` implements
the middle phase as a `task_spec_builder`: a pure
`(form, resolved) -> RunCommandSpec | RunPythonSpec` function, wired by the
`task` flavor's `app.py` as `task_spec_builder=build_<name>_spec`. Its stub
docstring documents that contract's default path — the `RunCommandSpec` /
`build_command_args` shape you fill in.

`payload_builder` is a different knob: a `(form, inventory_api) -> TaskWrite`
dependency used directly as the create payload, bypassing the three-phase path
entirely. The two are mutually exclusive.

> Checksums uses `payload_builder` rather than the scaffolded default because
> two of its fields (`databases`, `tables`) are multi-value `SchemaRef` /
> `TableRef` selections, and the resolve phase only resolves single-id
> reference fields — a list value comes back unresolved.
> `build_checksums_payload` runs its own multi-value resolution before
> assembling the task, which `task_spec_builder`'s fixed signature has no room
> for.

For the schema-driven `task` and `script` flavors, no frontend work is
needed: once enabled, the app appears in the sidebar and its pages render
from the derived schema — `GET /api/apps/` carries the route, display name,
and `nav_icon` the shell consumes. The `base` flavor is the bespoke-UI case:
it scaffolds with `custom_ui=True`, so it must register a component in
`frontend/packages/shell/src/appRegistry.tsx` and route metadata in
`appNavConfig.ts`; the schema-driven flavors need no such entry.

#### No database migration

Task apps need **no database migration** — tasks are stored generically, so
there is no per-app table to create and no Alembic revision to write.

### Verify it end to end

Run the generated contract test — it exercises the whole derived HTTP surface
against your definition:

```bash
pytest tests/app/extensions/apps/myapp/test_contract.py -v
```

Expect a green run with a substantial number of **skips** — that is normal, not
a misconfiguration. The suite is capability-gated: every case whose knob your
app does not switch on (an undeclared list filter, a disabled capability, a
form with no `HostRef`) skips with a reason rather than failing.

Then run PMM Extensions locally — see
[Setting Up Your Development Environment](../../CONTRIBUTING.md#setting-up-your-development-environment)
for the commands — and open the app from the sidebar. Remember it is registered
**disabled**, so enable it on the Apps page (`/admin/apps`) first. The form you
declared in `models.py` is what renders.

The CI `startapp-check` gate (`make startapp-check`, wired into
`.github/workflows/python.yaml`) scaffolds all three flavors non-interactively
and runs their generated test suites on every PR, protecting the default
scaffold-and-test path.

---

## 4. Form DSL reference

The form DSL is **model-first**: one model class is *both* the request body the
server validates *and* the source of the derived `GET /schema` form. You annotate
each field with markers — `Ui(...)` for presentation, reference markers for
inventory, `Choices(...)`/`ArgFormat(...)` for behaviour — and the framework derives
the form and the command from them.

**Where each name is imported from.** The split runs between *markers* (things
you attach to a field) and *rules* (things you build predicates out of):

| Import from | Names |
|---|---|
| `app/extensions/apps/framework/form_dsl` | `AppFormModel`, `TaskFormModel`, `Ui`, `Hidden`, `ArgFormat`, `Choices`, `Option`, `RemoteChoices`, `FieldWidget`, the four reference markers, `FormLayout` / `SectionLayout`, the rule **containers** `FormRules` / `SectionRules`, **and the per-field gate markers `Requires` / `Forbidden`** |
| `app/extensions/apps/framework/rules.py` | `F`, the truthiness predicates, the combinators, and the rule envelopes `FailRule` / `FieldGate` / `CardinalityRule` |
| `app/core/utils/fields.py` | The helper field types the examples below use: `NonEmptyStr`, `StrippedNonEmptyStr`, `EmptyStrToNone` |

`Requires` / `Forbidden` are the trap: this guide introduces them alongside the
predicate DSL because they take predicates, but they are form-DSL **markers**
— import them from `form_dsl` beside `Ui`, not from `rules.py`.

**How to read the examples.** Every field has the shape
`name: Annotated[type, markers…] = default`. `Annotated` is Python's way of
attaching extra information to a type: the first element is the field's real
type; everything after it is a marker the framework reads.

**How a field becomes required.** A field with **no default** — no `= ...` after
the annotation — is required, and the server rejects a body that omits it. A
field **with** a default is optional. That is plain Pydantic, and it is the
mechanism to reach for. `Ui(required=...)` is a *presentation* override: it
changes only the `required` flag the derived schema puts on the wire, for the
case where a field carries a default yet must still render as required. It
never changes what the server accepts — nothing on `Ui` does.

### `AppFormModel` / `TaskFormModel` and `Ui()`

`AppFormModel` is the base form model; it also carries the hidden `alert_on_fail`
capability control (a field marked `Hidden()` stays on the model but is omitted
from the form). `TaskFormModel` extends it for task apps, supplying the shared
`task_name` / `hostname` Task-section fields, so your model declares only its
own fields. **The `task` scaffold generates a `TaskFormModel` subclass**, so a
freshly scaffolded `models.py` already has those two fields and you add yours
below them. Subclass `AppFormModel` directly only for a form that needs no Task
section at all — a `TaskFormModel` subclass's layout must include one.

`Ui(...)` carries the per-field presentation the type cannot express — label,
section, description, display order, dependency:

<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->
```python
service_id: Annotated[
    int,
    ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
    Ui(label="Database Host", section="Task"),
]
```

Field **declaration order matters**: sections appear in the order of each
section's first field, and within a section fields sort by `Ui(order=...)`
first, with declaration order as the tiebreaker — `order` defaults to `0`, so
declaration order governs a section until a field pins an explicit `order`.
Checksums' Advanced section uses `Ui(order=...)` to reorder a few fields away
from their declaration order (see the model's docstring).

`Ui(destructive=...)` marks a field whose value irreversibly destroys
something the operator cannot get back — user data, or operator-managed state
such as a hand-tuned configuration file — and its value is the consequence
sentence a renderer shows in a confirmation. Presence is the mark — there is
no separate boolean — so a blank or whitespace-only string is rejected at
construction rather than published as a mark with nothing to display. Like
everything else on `Ui` it is presentation only: the API accepts exactly the
same bodies either way. Whether an unmarked field omits the key or publishes
`destructive: null` depends on the route: those serialising with
`response_model_exclude_none` drop it, while
`GET /api/apps/atw/execution-schema/` and `GET /api/apps/dipper/form-schema`
set no such posture and emit an explicit null on every field — the same way
they already do for `description`, `requires` and `forbidden`. Consumers must
treat an absent key and a null value identically and branch on the value,
never on key presence.

<!-- src: app/extensions/apps/archives/models.py :: ArchivesCreate -->
```python
delete_data: Annotated[
    bool | None,
    Ui(
        label="Delete Without Archiving",
        section="Advanced",
        destructive=(
            "The matched source rows are deleted without being archived "
            "anywhere. There is no copy to restore from."
        ),
    ),
] = None
```

Mark a field when **enabling or setting it** is what causes the irreversible
loss. Configuration state counts as much as table rows: a live `my.cnf`
overwritten with no on-disk copy is as unrecoverable as a dropped table, and
harder to spot. Where the loss is unconditional the mark goes on the field
naming what gets destroyed rather than on a toggle, which is why the restore
form marks `datadir`: every restore empties it. Do not mark one because the
surrounding operation is dangerous, because the flag is inverted (a
`no_drop_*` whose *enabled* state is the safe direction), or because what gets
destroyed is a tool's own bookkeeping — scratch state the tool wrote and
rebuilds — rather than something the operator authored.

A field whose annotation is a discriminated union cannot carry the marker: it
derives a one-of group rather than a field, and the group has nowhere to put
the text. Declaring one raises at class creation — mark the destructive leaf
inside each branch model instead, since the branches take their own `Ui`.

### `ArgFormat`

`ArgFormat` marks a field as a command-line argument and controls how it is
rendered into the command. It renders **two different kinds** of argument, and
the field's type picks which:

- A **`bool`** field is a **flag**: the template is emitted verbatim, and only
  when the value is `True`.
- **Any other** field is a **value arg**: the template must contain a
  `${value}` placeholder, and is emitted with the field's value shlex-quoted
  and substituted in — only when the value is truthy.

A bare `ArgFormat()` derives the template from the field's name and type:
`--kebab-field-name` for a `bool`, `--kebab-field-name=${value}` for anything
else. Pin an explicit template only when the CLI spelling diverges from the
field name — `ArgFormat("--explain")` for a flag on a field named
`explain_arg`, `ArgFormat("--databases=${value}")` for a value arg.

> **⚠ These rules are enforced when your app is imported, not at runtime.** A
> template with no `${value}` on a non-`bool` field is rejected ("a flag arg
> requires a bool field"), as is a `${value}` anywhere but the terminal
> `=${value}` position — the reverse parser splits a value arg on its first
> `=`. Writing `ArgFormat("--reason")` on a `str` field is the easy mistake:
> that spells a *flag*, and the app will not import.

The bool/flag case, from Checksums:

<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->
```python
explain_arg: Annotated[
    bool,
    ArgFormat("--explain"),
    Ui(
        label="Explain (dry run)",
        section="Flags",
        description="Show but do not execute checksum queries",
    ),
] = False
```

No app in the tree pins an explicit **value-arg** template today, so this
counterpart is **constructed**:

<!-- constructed -->
```python
databases: Annotated[
    str,
    ArgFormat("--databases=${value}"),
    Ui(label="Databases", section="Task"),
] = ""
```

With both fields set, `build_command_args` yields
`['--databases=sales,orders', '--explain']`; with `explain_arg=False` and
`databases="sales"`, just `['--databases=sales']`; with both left empty, `[]`.

### `Choices` and `Option`

`Choices` provides explicit options for a choice field, winning over any
type-derived options. The common form is a tuple of `(value, label)` pairs:

<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->
```python
recursion_method: Annotated[
    str,
    Choices(
        (
            ("default", "Default"),
            ("processlist", "Processlist"),
            ("hosts", "Hosts"),
            ("dsn", "DSN"),
            ("none", "None"),
        )
    ),
    Ui(section="Recursion", required=True),
] = "processlist"
```

When an option must render as **non-selectable** with an explanatory tooltip, use an
`Option(...)` instance instead of a bare tuple. No app in the tree currently needs
this, so the example below is **constructed** rather than lifted from a real app
(it derives a schema in which the `thorough` option is marked `disabled`):

<!-- constructed -->
```python
strategy: Annotated[
    str,
    Choices(
        (
            Option(value="fast", label="Fast"),
            Option(
                value="thorough",
                label="Thorough",
                disabled=True,
                disabled_reason="Not available on this service tier.",
            ),
        )
    ),
    Ui(label="Strategy", section="Task"),
] = "fast"
```

`Option.disabled` is a UI hint only — server-side rejection of a disabled value
remains the app's `FormRules` responsibility (see the predicate DSL below).

When the options cannot be listed statically at all, `RemoteChoices` marks a
field whose options the form fetches live from an endpoint the app serves (with
optional `depends_on` cascading). MySQL Restores uses it for `backup_source`
(see `app/extensions/apps/mysql_backups/restore/models.py`); the marker docstring in
`app/extensions/apps/framework/form_dsl/markers.py` covers the contract.

### `FieldWidget`

`FieldWidget` overrides the input widget the form renders for a field
(`TEXTAREA`, `YAML`, `CHOICE`, `MULTI_CHOICE`). For a multi-line value,
`FieldWidget.TEXTAREA`:

<!-- src: app/extensions/apps/backup_mongo/models.py :: BackupForm -->
```python
backup_priority: Annotated[
    str | None,
    Ui(
        label="Node Priority (YAML)",
        section="BackupOptions",
        widget=FieldWidget.TEXTAREA,
        description=(
            "YAML mapping of mongod addresses to backup priority (highest wins). "
            "One entry per line, e.g.:\n"
            '"host1:27018": 2\n'
            '"host2:27018": 1'
        ),
    ),
] = None
```

### The four reference markers

Four markers bind a field to **inventory**: `ServiceRef`, `SchemaRef`, `TableRef`,
and `HostRef`. They make the field a dropdown resolved from inventory —
each optionally takes `allow_custom=True` to also accept a typed name — and
they drive the connectivity and cascade wiring. The Alters app uses all four
together:

<!-- src: app/extensions/apps/alters/models.py :: AltersCreate -->
```python
hostname: Annotated[
    NonEmptyStr, HostRef(), Ui(label=EXECUTION_HOST_LABEL, section="Task")
]
service_id: Annotated[
    int,
    ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
    Ui(label="Database Host", section="Task"),
]
db_schema: Annotated[
    int | StrippedNonEmptyStr,
    SchemaRef(allow_custom=True),
    Ui(
        label="Schema",
        section="data",
        depends_on="service_id",
        description="Schema to alter; pick from inventory or type a name.",
    ),
]
db_table: Annotated[
    int | StrippedNonEmptyStr,
    TableRef(allow_custom=True),
    Ui(
        label="Table",
        section="data",
        depends_on="db_schema",
        description="Table to alter; pick from inventory or type a name.",
    ),
]
```

`depends_on` chains the dropdowns: choosing a service scopes the schema options,
choosing a schema scopes the table options.

The `int | StrippedNonEmptyStr` union on the two `allow_custom=True` fields is
doing real work: the field accepts **either** an inventory id (the `int`, when
the user picks from the dropdown) **or** a typed-in name (the string). A field
without `allow_custom` is typed plain `int` — inventory selections only.

### `FormLayout` and `SectionLayout`

Field **membership** and **order** in sections are declared on the model (via
`Ui(section=...)` and declaration order). The section **titles and metadata** — what
the model cannot express — live in `FormLayout` / `SectionLayout` inside the app's
`Views`.

A section **key** is an opaque string, matched **exactly**: every
`Ui(section="X")` must have a `SectionLayout(key="X")` in the layout, case
included, or the derivation rejects the model. Apps differ in convention —
Checksums capitalises (`section="Recursion"` / `key="Recursion"`), Alters does
not (`section="data"` / `key="data"`) — which is why the examples here and just
above disagree. Pick one convention per app and hold it.

<!-- src: app/extensions/apps/checksums/views.py :: checksums_views -->
```python
checksums_views = Views(
    layout=FormLayout(
        sections=(
            TASK_SECTION_LAYOUT,
            SectionLayout(key="Data", title="Data"),
            SectionLayout(key="Recursion", title="Recursion"),
            SectionLayout(key="Flags", title="Flags"),
            SectionLayout(key="Advanced", title="Advanced"),
        )
    ),
    list_view=ListView(
        columns=default_columns(
            SERVICE_TYPE_COLUMN,
        ),
    ),
    detail_view=DetailView(sections=[...]),
    capabilities=Capabilities(
        chaining=True,
        alert_on_fail=True,
        scheduling=True,
        stats=True,
        pii_anonymization=True,
    ),
)
```

> **Not to be confused with `AppCapabilities`**
> ([Rung 1](#rung-1--appcapabilities-flags)). Two different classes share the
> `capabilities` knob name: `AppCapabilities`, on the **app**, switches derived
> **routes** on and off; `Capabilities`, here inside `Views`, switches **UI
> features** of the rendered pages. Their flags do not overlap.

The five `Capabilities` flags, all defaulting to `False`:

| Flag | Effect when `True` |
|---|---|
| `chaining` | The app supports chaining tasks together. |
| `alert_on_fail` | The app supports configuring a PMM alert when a task fails, and renders the control (this is what the inherited hidden `alert_on_fail` field backs). |
| `scheduling` | The app supports scheduling tasks on a periodic interval. |
| `stats` | The detail page renders the aggregated execution-statistics card. |
| `pii_anonymization` | The detail view renders the "PII Anonymization" section. Purely a rendering gate — anonymization itself always happens when `anonymize_mask` is configured. |

`SectionLayout` also carries `collapsible` / `collapsed_by_default` and the
section-level `forbidden` gates shown in the predicate DSL below.

### The predicate DSL (`rules.py`)

For rules that span more than one field, the framework has a small predicate
DSL, in five parts:

- **Field reference** — `F("field_name")`, the handle on a field's submitted
  value. The comparison operators `==`, `!=`, `>`, `>=`, `<`, `<=` each turn it
  into a predicate: `F("severity") == "critical"`. (The boolean operators
  `&` `|` `^` `~` do *not* work on a bare `F` — they raise `TypeError`, since
  an `F` is a field reference, not yet a predicate.)
- **Truthiness predicates** — `truthy`, `present`, `falsy`, `absent`, plus
  multi-field siblings on the same pattern: `any_truthy` / `all_truthy`,
  `any_present` / `all_present`, `any_falsy` / `all_falsy`. The one irregular
  name is `absent`, whose multi-field counterpart is **`none_present`**.
- **Combinators** — `&`, `|`, `^`, `~`, or their `all_` / `any_` / `xor_` /
  `not_` function-call forms.
- **Per-field gate markers** — `Requires` / `Forbidden`, attached to a single
  field ([below](#requires--forbidden--per-field-gates)). Imported from
  `form_dsl`, not `rules.py`.
- **Rule envelopes** — `FailRule` (reject a form when a predicate holds),
  `FieldGate` (gate a field or section), `CardinalityRule` (bound the count of
  present fields). App-scoped rules collect in a `FormRules` object assigned to
  `__form_rules__` on the model.

The full set lives in `app/extensions/apps/framework/rules.py`.

**`FailRule` + `F`** — one rule rejects a form when its predicate holds. The
shape is a `fail_when` predicate, the `error_fields` the message attaches to,
and the `message` itself:

<!-- constructed -->
```python
__form_rules__: ClassVar[FormRules] = FormRules(
    fail_when=(
        FailRule(
            fail_when=truthy("compress") & (F("backup_type") != "M"),
            error_fields=["compress"],
            message="'compress' is only available for Mydumper backups.",
        ),
    )
)
```

Rules are often *generated* rather than written one by one. MySQL Backups
stamps out the same rule per mode-owned boolean, failing validation when one is
set outside its mode:

<!-- src: app/extensions/apps/mysql_backups/forms.py :: BackupCreate -->
```python
__form_rules__: ClassVar[FormRules] = FormRules(
    fail_when=tuple(
        FailRule(
            fail_when=truthy(name) & (F("backup_type") != owner_mode),
            error_fields=[name],
            message=(
                f"{name!r} must not be set when backup_type is not {owner_mode!r}."
            ),
        )
        for owner_mode, names in _MODE_BOOL_FIELDS.items()
        for name in names
    )
)
```

### `Requires` / `Forbidden` — per-field gates

The same predicates gate a single field directly, as markers on the field
itself: `Requires` makes the field mandatory when its predicate holds,
`Forbidden` rejects it. Import both from `form_dsl` alongside `Ui`. MySQL
Backups' encryption recipient is required exactly when encryption is on, and
rejected when it is off:

<!-- src: app/extensions/apps/mysql_backups/forms.py :: BackupCreate -->
```python
encryption_recipient: Annotated[
    NonEmptyStr | EmptyStrToNone,
    Requires(when=truthy("encrypt")),
    Forbidden(when=falsy("encrypt")),
    Ui(label="Encryption recipient", section="Encryption"),
] = None
```

A marker bound to a name can gate a whole family of fields at once — the same
model attaches `_MYDUMPER_ONLY = Forbidden(when=F("backup_type") != "M")` to
each of its non-boolean Mydumper-only fields.

> **⚠ A presence gate sees a `bool` field only when it is `True`.** The
> predicate engine treats `None`, `False`, and empty strings/bytes/collections
> as absent (numeric `0` still counts as present). A `Forbidden(when=...)`
> marker on a bool field therefore fires only on an explicit `True` toggle —
> never on the legitimate `False` default — and an explicitly submitted
> `False` is indistinguishable from an unset field. MySQL Backups covers its
> mode-owned bool fields (`mydumper_dump_triggers` and its siblings) with the
> generated `FailRule`s shown above rather than per-field `Forbidden` markers;
> both trigger on the same explicit `True`, and the model's own comment
> documents the convention.

**`FieldGate`** — the same predicate DSL gates a whole section. Here the Mydumper
section is forbidden unless `backup_type == "M"`:

<!-- src: app/extensions/apps/mysql_backups/views.py :: mysql_backups_views -->
```python
SectionLayout(
    key="Mydumper",
    title="Mydumper",
    collapsible=True,
    forbidden=(FieldGate(when=F("backup_type") != "M"),),
)
```

**`CardinalityRule`** — bound how many of a set of fields may be present. No app in
the tree uses it, so this example is **constructed** rather than lifted from a real
app (a form with neither host set is rejected with the custom message):

<!-- constructed -->
```python
__form_rules__: ClassVar[FormRules] = FormRules(
    cardinality_rules=(
        CardinalityRule(
            fields=["primary_host", "secondary_host"],
            min=1,
            message="Select at least one target host.",
        ),
    )
)
```

---

## 5. Escape-hatch ladder

When the declarative knobs do not express what you need, step **down** the ladder one
rung at a time. Each rung is more code than the last; stop at the first one that
covers your case, and prefer a knob over a custom route.

### Rung 1 — `AppCapabilities` flags

The cheapest step: switch derived routes on or off. `AppCapabilities` toggles which
of the six switchable derived routes (create, list, detail, execute, update, delete) the
spine builds — `create`, `list`, `detail`, and `execute` default on; `update` and
`delete` default off. (The seventh derived route, `GET /schema`, is always on
and has no flag.) Checksums turns on the derived update and delete:

<!-- src: app/extensions/apps/checksums/app.py :: app -->
```python
app = TaskExecutionApp(
    name="checksums",
    ...
    capabilities=AppCapabilities(update=True, delete=True),
)
```

Turning a capability **off** suppresses the derived route so a custom one can take
its place (Alters sets `create`, `detail`, `execute`, `update`, and `delete` all
to `False` — every mutation and its satellite-resolving detail route ride
`extra_routes` instead; only the derived `list` and schema routes remain). A
**satellite** here is a task the framework creates alongside a parent task as
part of one logical operation — see [Rung 4](#rung-4--response_context_provider-and-cascade-hooks).

### Rung 2 — `extra_routes`

When you need a route the spine does not derive (an approval endpoint, a manual
refresh, a download), hand the app a router via `extra_routes`. A handler that
returns a task response resolves its own actor fields — see
[Rung 4](#rung-4--response_context_provider-and-cascade-hooks).

> **This rung means writing FastAPI directly.** Everything above it is
> declarative — knobs and markers. From here down you are writing route
> handlers, with FastAPI's dependency injection (the `dependencies=[...]`
> list, the `user: ApiAdminUser` / `session: SessionDep` parameter aliases PMM Extensions
> defines in `app/extensions/deps.py`) and `async` request handling. If that is
> unfamiliar, FastAPI's own
> [dependency-injection tutorial](https://fastapi.tiangolo.com/tutorial/dependencies/)
> is the prerequisite; the example below assumes it.

Snippets carries three:

<!-- src: app/extensions/apps/snippets/app.py :: app -->
```python
app = TaskExecutionApp(
    name="snippets",
    ...
    script_source=snippet_source,
    capabilities_provider=_snippets_capabilities_provider,
    extra_routes=(approval_router, maintenance_router, artifact_router),
)
```

Each router is an ordinary FastAPI `APIRouter`, mounted under the app's prefix:

<!-- src: app/extensions/apps/snippets/extra_routes.py :: maintenance_router -->
```python
maintenance_router = APIRouter()


@maintenance_router.post(
    "/refresh", dependencies=[IsApiAuthenticated, IsManualSyncEnabled]
)
async def snippets_api_refresh(
    user: ApiAdminUser, session: SessionDep
) -> RefreshResponse:
    """Refresh the snippets cache from disk."""
    async with track_app_task(session, "snippets"):
        await update_snippets()
    logger.info("Snippets refreshed via JSON API by %s", user.username)
    return RefreshResponse(refreshed_at=utc_now())
```

### Rung 3 — handler overrides

When a derived route's *shape* is right but its response body needs custom
assembly, override the builder rather than replacing the route. PostgreSQL Backups
overrides the list and detail response builders (and their models) to keep its
response bodies in the exact shape its clients consume:

<!-- src: app/extensions/apps/backup_pg/app.py :: app -->
```python
app = TaskExecutionApp(
    name="backup_pg",
    ...
    response_builder=build_backup_pg_api_task_response,
    detail_response_builder=build_backup_pg_api_detail_response,
    detail_response_model=BackupTaskDetailResponse,
)
```

When you supply an explicit `response_builder`, set `response_model` to the same
class the builder's return annotation declares. The derived list route serializes
the builder's model, and every other `response_model` reader — including the
`list_view` column gate — measures that field; construction rejects a mismatch.

`TaskExecutionApp` also exposes `update_handler` / `delete_handler`
(`app/extensions/apps/framework/apps.py`) for fully replacing a mutation handler — no app
in the tree overrides those today; prefer the lighter response-builder override
above. The derived PUT/DELETE guards are knobs of their own: `update_guard` /
`delete_guard` default to the framework's protected-task and running-conflict
checks, can be replaced with your own dependency tuple, or removed with
`UNGUARDED`.

### Rung 4 — `response_context_provider` and cascade hooks

**`response_context_provider`** injects request-scoped context into every response
builder — the framework awaits the provider once per request and threads the
result through as each builder's `context` keyword. It defaults to the username
mapping, so an app on the framework's default response builder resolves
`created_by` / `last_updated_by` to display names without naming a provider at
all:

<!-- src: app/extensions/apps/framework/apps.py :: TaskExecutionApp -->
```python
class TaskExecutionApp(BaseApp):
    ...
    response_context_provider: SkipValidation[Callable[[], Awaitable[Any]] | None] = (
        get_username_mapping
    )
```

Because a provider is bound by default, every builder an app supplies must accept
a `context` keyword; the framework rejects one that does not when the app is
constructed. Accepting it is not enough to resolve anything, though: a custom
`response_builder` or `detail_response_builder` resolves the actor fields only if
it spreads `task_actor_fields(task, context or {})` (from
`app.extensions.api.task_history_actors`) into its extras. The framework never renders
an `extra_routes` handler's response, so a custom route that returns a task
awaits `get_username_mapping()` itself and passes the map as the builder's
`context`. A replacement provider replaces the username map, so it must still
supply it for the builders to resolve actors. Pass `None` to opt out and leave
actor ids raw; the registry conformance check (`check_actor_fields_resolvable`)
rejects a production app that opts out while its responses render actor fields,
and the contract suite fails its username assertions.

The default provider is an ordinary async callable:

<!-- src: app/extensions/deps.py :: get_username_mapping -->
```python
async def get_username_mapping() -> dict[str, str]:
    """Create a mapping from actor ID to username using the active auth provider."""
    users = await User.get_actors()
    return {str(user.id): user.username for user in users}
```

The body is trimmed to show the provider shape; the real source additionally wraps
the fetch in error handling that logs and returns an empty mapping when the auth
provider raises any `Exception`. A failed listing is remembered for
`USERNAME_MAPPING_FAILURE_WINDOW` (30 s): calls inside that window return the empty
mapping without contacting the provider, instead of retrying on every request. A
successful listing is not stored there; the provider SDK owns that cache.
Cancellation (`asyncio.CancelledError`) and other `BaseException` subclasses are not
caught, still propagate to the caller, and do not start the window.

**Cascade hooks** are the heaviest rung short of leaving the spine: when a single
create must fan out into a group of related tasks, build a `CascadeCreatePlan`.
The group has a shape and two names for it: the **parent** is the task the user
actually asked for; a **satellite** is a task created alongside it as part of
the same logical operation; a **predecessor** is a task that must run *before*
the parent (a pre-flight check, say). Alters assembles a parent execute task, an
imperative pre-checks predecessor, and a cascade closure:

<!-- src: app/extensions/apps/alters/deps.py :: build_alters_cascade_plan -->
```python
async def build_alters_cascade_plan(
    body: AltersCreate,
    inventory_api: InventoryAPI,
    tasks_api: TaskAPI,
) -> CascadeCreatePlan:
    """Build the cascade create plan for an alters task group."""
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

The `cascade_create_*` / `cascade_update_*` / `cascade_delete_*` helpers in
`app/extensions/apps/framework/cascade.py` are the toolbox the closure calls. The
create helpers (`cascade_create_tasks` and its siblings) are fail-fast and
return `None`: on any POST failure they delete the already-created tasks in
reverse creation order and re-raise the original exception. The update/delete
helpers are best-effort and return a `CascadeResult`; call its
`raise_if_failed(op="update")` / `raise_if_failed(op="delete")` (`op` is
keyword-only) to turn a partial cascade failure into an HTTP 500 instead of
hand-rolling the error block.

### Rung 5 — fall back to `BaseApp`

If the derived task router genuinely cannot express your app, drop to `BaseApp` with a
hand-written `api_router` (the scaffolder's `base` flavor). You lose the derived
surface but keep registry discovery, activation, and navigation. This is the last
rung — reach it only when every knob and hook above has failed, because a hand-written
router opts out of the contract suite's derived-surface guarantees.

### Script-backed apps: `ScriptSource`

Apps that execute **scripts** rather than a typed form use the `ScriptSource`
extension point (`app/extensions/apps/framework/script_source.py`): each script supplies
its own form schema, and listing/execute/history derive from the source. Snippets
wires one:

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
    list_page=_list_page,
)
```

`load_scripts` is the batch variant of `load_script`, for sources that can fetch
many scripts in one round trip; `list_query_dep` + `list_page` let the source
take over server-side sorting, searching, filtering, and paging of the list
route. The app then passes `script_source=snippet_source` instead of a
`create_model` (`ScriptProtocol` and the `script_helpers.py` helpers back this
extension point).

---

## 6. Testing

### The derived-router contract suite

Because the router is *derived*, the framework can test the whole HTTP surface
generically. `DerivedRouterContractTests`
(`tests/app/extensions/apps/framework/contract_suite.py`) exercises schema, list, detail,
create, update, execute, delete, auth, 404, the protected-task and
running-conflict guards, connectivity, and injected extras against the **real**
app definition. Your app's contract test is a subclass
that binds the definition to `app_def` — the scaffolder's `task` flavor emits
exactly this (the `script` and `base` flavors derive no model-first CRUD
contract to subclass, so their generated tests hand-write smoke tests against
`build_contract_client` instead):

<!-- src: tests/app/extensions/apps/checksums/test_contract.py :: TestChecksumsContract -->
```python
class TestChecksumsContract(DerivedRouterContractTests):
    """Assert the checksums app's full derived HTTP surface, knob by knob."""

    app_def = checksums_app
```

The suite stubs the auth provider's user listing, so it asserts the resolved
username on the seeded task for every app. An app left with no
`response_context_provider` fails `test_response_context_provider_bound` and the
injected-extras tests rather than skipping them.

The suite reads the contract from the app's own knobs, so switching a capability on
or changing a response model is covered automatically — no per-route test to write.
App-specific behaviour the generic suite does not seed (a custom guard, a bespoke
route, **a form rule**) gets a standalone test beside the contract subclass.

### The shared fixtures

The suite's fixtures live in `tests/app/extensions/apps/conftest.py`: `contract_client` (an
authenticated `TestClient` bound to the app definition under test), `mock_task_api`,
and `mock_inventory_api` (the two boundary APIs, seeded per test):

<!-- src: tests/app/extensions/apps/conftest.py :: contract_client -->
```python
@pytest.fixture
def contract_client(
    request: pytest.FixtureRequest,
    regular_user: CasdoorUser,
    mock_task_api: MockTaskAPI,
    mock_inventory_api: MockInventoryAPI,
) -> TestClient:
    """Return an authenticated contract client for the bound definition."""
    return build_contract_client(
        _bound_app_def(request),
        user=regular_user,
        tasks_api=mock_task_api,
        inventory_api=mock_inventory_api,
    )
```

### Testing a form rule

The contract suite exercises the derived HTTP surface, not your app's specific
rules — a form with a `Requires` gate or a `FailRule` will pass the whole suite
without that rule ever firing. Write a standalone test for it.

**`FailRule`, `Requires`, and `Forbidden` all enforce during model validation**,
so the test needs no HTTP client and no fixtures: construct the model directly
and assert `pydantic.ValidationError`.

<!-- constructed -->
```python
def test_reason_required_when_severity_critical() -> None:
    """Assert the Requires gate fires when severity is critical."""
    with pytest.raises(pydantic.ValidationError):
        LagcheckForm(task_name="t", hostname="h", severity="critical")

    LagcheckForm(
        task_name="t", hostname="h", severity="critical", reason="replica lag"
    )
```

The second construction is the other half of the assertion: the gate must *not*
fire once the required field is supplied. Match on the message
(`pytest.raises(ValidationError, match="is required")`) when a model has several
rules and you need to pin which one tripped.

### Factory conventions

Test data comes from `polyfactory` factories — build with `.build()` and customise
inline (`CasdoorUserFactory.build(role=UserRole.ADMIN)`). Never hand-roll a `dict` for a
model a factory already covers. The contract suite's own task/inventory seeding goes
through the `MockTaskAPI` / `MockInventoryAPI` helpers in
`tests/app/extensions/apps/framework/kit.py`, not raw dicts.

**Your app's factories live in your app's test package** —
`tests/app/extensions/apps/<app>/factories.py`, imported by your tests as
`from tests.app.extensions.apps.<app>.factories import <App>CreateFactory`. Only core,
cross-app factories (auth, tasks, inventory) belong in `tests/app/factories.py`; that
module is imported by the whole test tree, so a factory for your models there couples
the shared file to your app and outlives it. The directory already has an
`__init__.py`, so no packaging step is needed — just add the module.
`tests/app/test_factories_boundary.py` fails the build if a module at the root of the
test tree imports `app.extensions.apps.*`, or re-exports one of your factories from
`tests.app.extensions.apps.*`.

### How conformance runs in CI

Two CI gates keep apps honest beyond their own contract test:

- **Conformance** — `app/extensions/apps/framework/conformance.py` defines structural
  rules every app must satisfy (the required knobs wired, disallowed shapes
  absent), run per-app by `tests/app/extensions/apps/framework/test_conformance.py`.
  It is driven off the live registry and parametrized per app key, so **your app
  is picked up automatically the moment it is in `settings.yaml` — including
  while it is still `ENABLED: false`**. A half-finished scaffold is already
  covered; you do not wire anything up.
- **Scaffold check** — `make startapp-check` (`scripts/startapp_check.py`, wired into
  `.github/workflows/python.yaml`) scaffolds all three flavors
  non-interactively and runs their generated test suites on every PR,
  protecting the default scaffold-and-test path.

Conformance runs as part of the ordinary `make test` / CI `pytest` invocation,
alongside every other test; the scaffold check does not — `make startapp-check`
is its own separate CI step (`.github/workflows/python.yaml`), never invoked by
`make test`.

---

*For guide maintainers:* the `src:` markers on the examples are enforced by
`tests/docs/test_guide_references.py`, which fails CI when a cited file or
symbol is renamed, moved, or deleted. Keep the marker on any new example, and
execute a new `constructed` example against the framework before adding it.
