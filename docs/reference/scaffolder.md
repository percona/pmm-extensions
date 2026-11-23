# The scaffolder

`make startapp` generates a new app: its package, its test package and its entry in the root `settings.yaml`. This page describes its inputs and its outputs. For a guided first run, see [Build your first app](../tutorials/first-app.md).

## Synopsis

```bash
make startapp NAME=<name> [VARIABLE=<value> ...]
```

The target runs `app/extensions/apps/framework/scaffold.py` with the project's Python interpreter.

## How it asks for values

| Situation | Behavior |
|---|---|
| Standard input is a terminal | A wizard prompts for each value that no variable supplied. It shows a preview and asks for a final confirmation before it writes. |
| A variable is supplied | The value is used, and the wizard does not prompt for it. The other prompts still appear. |
| `NO_INPUT` is set, or standard input is not a terminal | No wizard runs and no confirmation is asked. Every unset value takes its default, and the files are written at once. `NAME` has no default and is required. |

## Variables

| Variable | Flavors | Default | Meaning |
|---|---|---|---|
| `NAME` | all | none | The module name under `app/extensions/apps/`. A lowercase Python identifier: letters, digits and underscores. |
| `TYPE` | all | `task` | The flavor: `task`, `script` or `base`. |
| `DISPLAY_NAME` | all | `NAME` in title case | The name shown in the navigation and in page headings. |
| `ITEM_DISPLAY_NAME` | all | `<display name> task` for `task`. The display name for `script` and `base`. | What one record of the app is called, as it reads inside a sentence. |
| `ITEM_DISPLAY_NAME_PLURAL` | all | `<display name> tasks` for `task`. The display name for `script` and `base`. With `ITEM_DISPLAY_NAME` supplied, its plural. | What several records are called. |
| `DESCRIPTION` | `task`, `script` | A placeholder that starts with `TODO:` | The one-line description of the app. |
| `GROUP` | all | none | The navigation group that lists the app. |
| `SERVICE_TYPE` | `task` | `MYSQL` | The `ServiceTypeEnum` member that the generated service field filters the inventory by. |
| `NAV_ICON` | all | none | The `NavIcon` member for the navigation entry. |
| `RUN_MODE` | `task` | `run-command` | `run-command` or `run-python`. See [Run modes](#run-modes). |
| `COMMAND` | `task` | `echo` | For `run-command`: the program that the generated spec builder calls. |
| `PAYLOAD` | `task` | none | For `run-python`: a Python file to copy into the app as its payload. Supplying it selects `run-python`. |
| `SCRIPT` | `script` | none | A `.sh` or `.py` file to copy into the app's `snippets/` directory, in place of the sample script. |
| `NO_INPUT` | all | unset | Skips the wizard. Any non-empty value sets it, including `0`. |
| `ENABLE` | all | unset | Registers the app enabled in `settings.yaml`. Any non-empty value sets it. |
| `DERIVE_UPDATE`, `DERIVE_DELETE` | `task` | on | Whether the generated app turns on the derived `PUT` and `DELETE` routes. Pass `0`, `false` or `no` to leave one off. |

`DERIVE_UPDATE` and `DERIVE_DELETE` default to on. `AppCapabilities` itself defaults `update` and `delete` to off, so a scaffolded app has two routes that a hand-written one does not.

A default record name that differs from the display name is what lets a new `task` app pass framework conformance with no edit.

## Run modes

`RUN_MODE` selects what the generated `spec.py` builds.

| Mode | The spec builder returns | The task runs |
|---|---|---|
| `run-command` | A `RunCommandSpec`: a program name and an argument string built from the form's `ArgFormat` fields. | A program that is already on the host. |
| `run-python` | A `RunPythonSpec`: a serialized configuration, a list of pip requirements and a `file://` payload address. | A Python payload that is sent with the task. |

## Generated files

### `task`

```text
app/extensions/apps/<name>/
  __init__.py
  app.py          the TaskExecutionApp definition
  models.py       the form model and the app's OWNER constant
  spec.py         the spec builder
  views.py        section layout, list and detail views, UI features
tests/app/extensions/apps/<name>/
  __init__.py
  test_contract.py   a subclass of DerivedRouterContractTests
```

With `PAYLOAD` set, the payload file is copied beside `spec.py`.

The generated `app.py` sets `owner=OWNER`, and `models.py` defines `OWNER` as `NAME` in upper case. The app therefore lists only the tasks it created.

### `script`

```text
app/extensions/apps/<name>/
  __init__.py
  app.py
  constants.py
  source.py           the ScriptSource
  snippets/sample.sh  replaced by the SCRIPT file when one is supplied
tests/app/extensions/apps/<name>/
  __init__.py
  test_contract.py
```

### `base`

```text
app/extensions/apps/<name>/
  __init__.py
  api_routes.py   a router that you write
  app.py
  schema.py
tests/app/extensions/apps/<name>/
  __init__.py
  test_contract.py
```

The `base` flavor sets `custom_ui=True`. Its pages do not render from a schema, so it needs a page of its own in PMM's UI.

## Registration

The scaffolder adds one entry under `EXTENSIONS.APPS` in the root `settings.yaml`:

```yaml
      - MODULE_NAME: <name>
        ENABLED: false
```

With `ENABLE`, the entry says `ENABLED: true`. The test suite and framework conformance read this file.

The scaffolder does not change `sidecar/settings.yaml`. That file decides which apps the released image ships, and a test pins its list.

## Output

A run in a virtualenv that has `ruff` first prints the import fixes it applied to its own render. It then prints a summary:

```text
Scaffolded 'task' app 'diskfree':
  app:   <repository>/app/extensions/apps/diskfree
  tests: <repository>/tests/app/extensions/apps/diskfree

Registered 'diskfree' DISABLED in settings.yaml.

To run the app inside PMM, add this line under EXTENSIONS.APPS in sidecar/settings.yaml:
      - MODULE_NAME: diskfree
Then follow docs/how-to/run-in-local-pmm.md.
```

The paths are absolute.

## Failures

| Message | Cause |
|---|---|
| `--name is required in non-interactive mode (no TTY, or --no-input)` | `NAME` is missing and no wizard can ask for it. |
| `error: invalid app name ...` | `NAME` is not a lowercase Python identifier, or it is a Python keyword. |
| `error: <path> already exists; refusing to overwrite an existing plugin` | `app/extensions/apps/<name>/` or its test package already holds files. The scaffolder writes nothing. |
| `Aborted; nothing was written.` | The final confirmation was declined, or the wizard was interrupted. |

## The check that guards it

`make startapp-check` scaffolds one app of each flavor with no input and runs each generated test. It then runs framework conformance over all three and removes them. CI runs it on every pull request.
