# Update the API snapshots

The test suite keeps a snapshot of each app's public HTTP contract. A test fails when your change alters a contract, and when a new app has no snapshot yet. This page shows how to read that failure, accept a change that you intended, and create the snapshots of a new app.

## Prerequisites

- A development environment with the virtualenv active: see [Set up a development environment](set-up-a-development-environment.md).
- Node.js and `pnpm`, to accept a change. The command that accepts a change also regenerates the API client under `frontend/`.

## What is in a snapshot

Two test modules compare the running code with files under `tests/app/extensions/snapshots/`:

| Test module | Snapshot | One file for each |
|---|---|---|
| `tests/app/extensions/test_openapi_snapshot.py` | `openapi/<app>.json`: the app's part of the OpenAPI document | app in the root `settings.yaml` |
| `tests/app/extensions/test_schema_snapshot.py` | `schema/<app>.json`: the response of the app's `GET /schema` route | `GET /schema` route that takes no parameter |

A route nested under an app has a snapshot of its own, named with two underscores: `backup_mongo__restore.json`.

Both modules find the apps from the settings, so a new app needs no change to the tests. Each module also fails when a snapshot file is missing or left over.

## Run the snapshot tests

1. Run the two modules:

   ```bash
   pytest tests/app/extensions/test_openapi_snapshot.py tests/app/extensions/test_schema_snapshot.py -q
   ```

   When nothing changed, the output ends with a line such as:

   ```text
   36 passed, 21 warnings in 16.00s
   ```

## Read a failure

A failure names the snapshot file that no longer matches:

```text
AssertionError: OpenAPI/schema drift vs checksums.json; if intentional, regenerate with `make regen-specs` and review the diff in the PR
FAILED tests/app/extensions/test_schema_snapshot.py::test_schema_payload_matches_snapshot[checksums]
```

1. Decide whether you intended the change.

   A new route, a new form field or a new option is a change to the contract. If your pull request means to make it, accept it as described in the next section.

   If you did not intend the change, do not accept it. Find what altered the contract and fix that.

A new app fails four tests of these two modules, because its two snapshot files do not exist yet. For an app named `diskfree`, the output holds these lines:

```text
AssertionError: snapshot set drift in snapshots/schema: missing={'diskfree'}, orphaned=set(); regenerate with EXTENSIONS_UPDATE_SNAPSHOTS=1
FAILED tests/app/extensions/test_openapi_snapshot.py::test_openapi_subtree_matches_snapshot[diskfree]
FAILED tests/app/extensions/test_openapi_snapshot.py::test_openapi_golden_set_is_complete
FAILED tests/app/extensions/test_schema_snapshot.py::test_schema_payload_matches_snapshot[diskfree]
FAILED tests/app/extensions/test_schema_snapshot.py::test_schema_golden_set_is_complete
```

A new app also fails two tests in `tests/app/test_openapi_specs_fresh.py`, because it changes an OpenAPI file that the frontend reads:

```text
AssertionError: OpenAPI spec drift: ['extensions']; regenerate with `python scripts/dump_openapi.py`
FAILED tests/app/test_openapi_specs_fresh.py::test_committed_openapi_specs_are_fresh
FAILED tests/app/test_openapi_specs_fresh.py::test_dump_script_ignores_developer_auth_config
```

One command in the next section creates the two snapshot files and regenerates the OpenAPI file. `make regen-specs` does what both messages name.

A `GET /schema` route that stops answering 200 fails on its status before the comparison. That points at sign-in or at the app being disabled, not at the snapshot.

## Accept a change or create the snapshots of a new app

1. Install the frontend dependencies, once:

   ```bash
   (cd frontend && pnpm install)
   ```

2. Regenerate the snapshots and everything derived from them:

   ```bash
   make regen-specs
   ```

   The command rewrites the snapshot files that differ, the form snapshots, the OpenAPI files under `frontend/packages/api/specs/` and the generated API client. For a new app, it also creates `openapi/<app>.json` and `schema/<app>.json`. Its test run reports the snapshot cases as skipped, which is expected:

   ```text
   4 passed, 39 skipped, 21 warnings in 7.73s
   ```

3. Review the result:

   ```bash
   git status --short
   ```

   The command lists every file that changed. Read the diff of each one. Every changed line must follow from your change.

   For a new app, the list holds the two new snapshot files, and two files under `frontend/packages/api/`: the OpenAPI file and the generated client.

4. Commit the regenerated files with the change that caused them.

A reviewer reads a regenerated snapshot as carefully as source code. A snapshot that nobody reviewed is a contract change that nobody reviewed.

## Verify

Run the snapshot tests again. They pass.

For a new app, run the test of the OpenAPI files too:

```bash
pytest tests/app/test_openapi_specs_fresh.py -q
```

The output ends with a line such as `13 passed in 16.11s`.

## Next steps

- [Test an app](test-an-app.md)
