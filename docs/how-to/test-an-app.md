# Test an app

This page shows which tests cover an app, how to run them, and how to test a rule of your own. The examples use the Checksums app. Replace `checksums` with your app's name.

## Prerequisites

- A development environment with the virtualenv active: see [Set up a development environment](set-up-a-development-environment.md).
- An app that the scaffolder generated, with its test package under `tests/app/extensions/apps/<name>/`.

## Run the app's own tests

1. Run every test in the app's test package:

   ```bash
   pytest tests/app/extensions/apps/checksums -q
   ```

   The output ends with a line such as:

   ```text
   72 passed, 11 skipped, 37 warnings in 3.98s
   ```

   Skipped tests are expected. The shared suite skips each case for a feature that the app does not turn on. A list filter and a route that is off are examples.

## Run the contract suite

The framework derives an app's routes, so one shared suite can test them for every app. `DerivedRouterContractTests` covers the schema, list, detail, create, update, execute and delete routes. It also covers sign-in, the two guards and the connectivity check. It runs against your real app object.

A `task` app's `test_contract.py` is a subclass that names the app:

<!-- src: tests/app/extensions/apps/checksums/test_contract.py :: TestChecksumsContract -->
```python
class TestChecksumsContract(DerivedRouterContractTests):
    """Assert the checksums app's full derived HTTP surface, knob by knob."""

    app_def = checksums_app
    create_body_overrides = {"defaults_file": ""}
```

1. Run it alone:

   ```bash
   pytest tests/app/extensions/apps/checksums/test_contract.py -q
   ```

The suite reads what to test from the app object. When you turn on a capability or change a response model, the suite covers it with no new test.

The suite builds a create body from your form model. `create_body_overrides` sets the fields that a generated value cannot satisfy, such as a field that a rule constrains.

The suite's fixtures are in `tests/app/extensions/apps/conftest.py`. `contract_client` is a signed-in test client for the app under test:

<!-- src: tests/app/extensions/apps/conftest.py :: contract_client -->
```python
@pytest.fixture
def contract_client(
    request: pytest.FixtureRequest,
    regular_user: CasdoorUser,
    mock_task_api: MockTaskAPI,
    mock_inventory_api: MockInventoryAPI,
) -> Iterator[TestClient]:
    ...
    yield from shared_contract_client(
        _bound_app_def(request),
        user=regular_user,
        tasks_api=mock_task_api,
        inventory_api=mock_inventory_api,
    )
```

`mock_task_api` and `mock_inventory_api` stand in for the two services that an app calls.

## Test a form rule

The contract suite does not know your app's rules. A form with a `Requires` marker or a `FailRule` passes the whole suite without that rule ever being tested. Write a test for each rule.

`FailRule`, `Requires` and `Forbidden` are checked when the model is validated. The test needs no client and no fixture.

1. Build the model directly and assert the error:

   <!-- constructed -->
   ```python
   def test_reason_required_when_severity_critical() -> None:
       """Assert the Requires gate fires when severity is critical."""
       with pytest.raises(pydantic.ValidationError):
           LagcheckForm(task_name="t", hostname="h", severity="critical")

       LagcheckForm(task_name="t", hostname="h", severity="critical", reason="replica lag")
   ```

   The second call is the other half of the test: the rule must not fire once the field is supplied.

2. When a model has several rules, match the message to say which one fired: `pytest.raises(pydantic.ValidationError, match="is required")`.

Put the app's test data factories in `tests/app/extensions/apps/<name>/factories.py`. Only factories that several apps share belong in `tests/app/factories.py`.

## Run framework conformance

Conformance checks the rules that every app must follow, such as a record name that differs from the display name. It reads the apps from the root `settings.yaml`, so it covers a new app as soon as the scaffolder has registered it, enabled or not.

1. Run the two conformance modules:

   ```bash
   pytest tests/app/extensions/apps/framework/test_conformance.py tests/app/extensions/apps/framework/test_form_dsl_conformance.py -q
   ```

   The output ends with a line such as:

   ```text
   139 passed, 22 skipped, 21 warnings in 6.44s
   ```

## Run the scaffolder check

Do this only when you changed the scaffolder or its templates.

1. Run the check:

   ```bash
   make startapp-check
   ```

   The command scaffolds one app of each flavor, runs each generated test, runs conformance over all three, and removes them. It exits with status 0 when all of that passes.

## Create the snapshots of a new app

The test suite compares each app's routes and form schema with two snapshot files. A new app has none, so four tests of the whole suite fail until you create them. An app that this repository already holds has both files.

1. Create the two files and commit them: see [Update the API snapshots](update-api-snapshots.md).

## Run the app once in a local PMM

Tests do not run an app's task on a host. The PostgreSQL Backups app passed all its tests and failed on its first run on a real host. The cause was a path that only exists there.

1. Start the local PMM with your checkout's code, and run one task: see [Run PMM Extensions in a local PMM](run-in-local-pmm.md).

## Verify

The app's own tests, the two conformance modules and the two snapshot modules pass. One task of the app has finished with the status `success` in a local PMM.

## Next steps

- [Port an app](port-an-app.md)
- [Update the API snapshots](update-api-snapshots.md), when a later change alters a route or a form
- [App extension points](../reference/app-extension-points.md)
