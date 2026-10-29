---
applyTo: "tests/**/*.py"
---

# Tests — Mocking, Factories, Patterns

## Directory layout

Tests mirror app structure exactly. `app/extensions/snippets/config.py` → `tests/app/extensions/snippets/test_config.py`. Flag tests at the wrong level.

**Don't duplicate fixtures across modules — promote them.** Before adding a fixture or module-level helper, grep the nearest `conftest.py` and sibling modules for an equivalent. Any bootstrap that two or more modules need (in-memory engine + `apply_schema`, seed-row builder, client factory) belongs in the nearest shared `conftest.py`, parametrized when call sites differ — not copied per module. The sibling may be in another module **or in the same one** — a conftest carrying two byte-identical fixtures drifts exactly as a cross-module pair does. The same rule covers module-level constants: an `UPPER_SNAKE` name bound to the same value in two files. A scope-bounded deferral carries `# scaffolding-dup-ok: <why, naming the sibling>`.

**Use `apply_schema`, not `metadata.create_all`, on an async-SQLite engine.** `tests.app.db_schema.apply_schema` replays the metadata's DDL through one `executescript` and still hands each test an empty database — roughly an order of magnitude faster than `create_all`. `create_all` stays required on the real-PostgreSQL and synchronous call sites, since `executescript` is aiosqlite-only.

## Factories — never manual dicts

Test data MUST come from a factory, never a hand-rolled `dict`. Core, cross-app factories live in `tests/app/factories.py` (`SchemaWriteFactory`, `TableWriteFactory`, `TaskFactory` / `GeneratedTaskFactory`, `PeriodicTaskFactory`, …). Build with `.build()`, customise inline: `CasdoorUserFactory.build(role=UserRole.ADMIN)`. Use mock ID constants from `tests/app/factories.py`.

**A factory for an app's model belongs in `tests/app/extensions/apps/<app>/factories.py`, not the shared file.** `tests/app/factories.py` sits at the root of the test tree, so every subtree imports it — an app-specific factory there couples the shared file to that app and has to be found and deleted by hand when the app goes. Adding an app means adding `tests/app/extensions/apps/<app>/factories.py`, never editing the shared module; the app test packages already have `__init__.py`, so `tests.app.extensions.apps.<app>.factories` resolves as-is. `tests/app/test_factories_boundary.py` enforces this: any module directly under `tests/app/` that imports `app.extensions.apps.*` (or re-exports from `tests.app.extensions.apps.*`) fails.

**Preference order: Factory > `spec=`'d Mock > bare Mock.** Inside the subject of a test, prefer `Factory.build(field=…)` over `MagicMock(spec=Model)` — the factory exercises field validation, where a spec'd mock returns an empty string for a `NonEmptyStr` field and masks the failure. Bare `MagicMock()` / `AsyncMock()` is a smell when the value has a model class to spec against.

**New/tightened constraints need a rejection test.** When a PR tightens a field (`str` → `NonEmptyStr`, adds `min_length` / `field_validator` / `Field(gt=…)`), require a `pytest.raises(ValidationError)` test for the now-invalid input — a happy-path-only test lets the constraint silently revert. **Reachability carve-out:** if moving from a spec'd mock to a factory closes off a branch because the constraint makes that state impossible (an empty-address test where `Node.address` is `NonEmptyStr`), drop the test — don't keep the spec'd mock alive to preserve it.

## Don't drop assertions for unchanged behavior

Editing an existing test MUST NOT remove an `assert` unless the asserted behavior itself changed in the same PR. If the mapping, field, or side effect under assertion is untouched by the diff, the assertion stays — dropping it silently narrows what the test proves and lets a later regression pass green. Sibling tests asserting the same property are the tell: if several parametrize cases assert `events[0].step == "step1"` and one edited case quietly drops it while the step mapping is unchanged, flag it as unintended coverage loss.

## Mock subjects vs boundaries — critical

Every test has a **subject** (SUT + its session, model instances, loop state) and **boundaries** (HTTP clients, subprocess/Nomad, file I/O, time, randomness). Mock only at the boundaries.

**Never mock `AsyncSession` on a session-touching SUT.** If the SUT receives `session: AsyncSession` and performs any session op directly or transitively (`add`, `commit`, `refresh`, `execute`, any `BaseSQLModelManager` method), use the real `session` fixture from `tests/app/tasks/conftest.py`. A mocked session silently accepts every attribute access — the test passes green while production fails on the first real call. Watch for a plain `session = AsyncMock()` / `_session=AsyncMock()` handed to the manager under test, and for `MagicMock(spec=<SQLModel>)` + `AsyncMock(spec=AsyncSession)` in one body — the latter mocks both halves of the SQLAlchemy lifecycle, so relationship traversals return `AsyncMock` instead of raising `MissingGreenlet`.

**Don't mock the subject.** `patch.object(<SUT class>, "<sibling>")` / `patch("app.<sut_module>.<sibling>")` turn the test into a self-assertion. Always use `spec=`; assert async with `.assert_awaited_once_with()`. ≥3 `patch(...)` against one SUT is **Mockery** — split the SUT.

**Prefer `dependency_overrides` over `patch()` for `Depends` callables.** When a route reaches a callable via `Annotated[T, Depends(fn)]`, override it with `app.dependency_overrides[fn] = lambda: fake`, not `patch("app.<module>.fn", …)` — the patch form forces a `# noqa: F401` re-export in the route module. A `noqa: F401` re-export whose only consumer is a `patch(...)` string in the sibling test is the tell; flag both and rewrite as an override.

**Hermetic handler tests: mock every fan-out point.** If any sibling test for a handler mocks dependency X (`SnippetManager.count`, an API client), every new test asserting on that handler's response body must also mock X — partial mocking diverges between CI (empty DB) and local (populated). Integration tests that explicitly wire a live DB are exempt.

**A negative assertion observes at the boundary the claim names.** "What does the new code call?" and "what does the criterion assert?" give the same answer for a positive assertion and different answers for a negative one — a positive assertion fails loudly when its observation point stops being reached, while a negative one starts **passing vacuously**, since observing nothing is exactly what it was written to see. So for any "X did not happen" assertion (`assert_not_called()`, "no additional query", "no second write"), put the observation point where the effect *would land*, not on the caller that would produce it:

```python
# Bad — spies a name in the module under test. A behaviour-preserving refactor
# (moving the import into the function, calling through another alias)
# silently detaches the spy and the assertion still passes.
spy = mocker.patch("app.extensions.apps.atw.send.republish_extensions_settings_snapshot")
spy.assert_not_called()

# Good — spies the boundary the criterion names; survives any refactor of how
# that boundary is reached.
overrides_read = mocker.spy(SettingsOverrideManager, "list")
overrides_read.assert_not_called()
```

This is the *reason* behind the "don't patch a sibling in the SUT module" rule above — the rule's letter is about module topology, its purpose is assertion durability, so a collaborator that merely happens to live in another module is no exemption. **Prove it is non-vacuous:** a negative assertion is worth its line only if some sibling test drives the same observation point positively — otherwise "never called" and "never wired up" are indistinguishable. Name that sibling when the pairing isn't obvious.

**Assert the log line, not just the skip.** A test that only checks a drop happened passes equally against a version that drops silently. When a refusal path is supposed to log, assert the log.

## Body-dep override masks form/JSON parsing — critical

When a route's body is `Annotated[<Model>, Form()]` / `Annotated[<Model>, Body()]`, the test MUST NOT override the dep that materialises that body model (`dependency_overrides[build_<app>_task_payload]`, `dependency_overrides[parse_<resource>_form]`, …). Such overrides remove the Pydantic model from FastAPI's body-field resolution, so body-parsing regressions (422 in production) pass green. At least one test per POST/PUT/PATCH route MUST issue a real `test_client.post(...)` with realistic data and no body-dep override. Overriding `get_current_user`, `get_session`, `get_inventory_api`, `get_tasks_api` is fine.

## Compile-only SQL ≠ engine coverage

`.compile(dialect=postgresql.dialect(), ...)` + `assert "->>" in rendered` verifies the rendered shape, NOT that the engine accepts it. PostgreSQL's `->`/`->>` require `json`/`jsonb` — a compile-only test on a `Text` column passes while production rejects the SQL. Dialect-specific helpers MUST have a real-engine execution test per `(dialect × column type)`, and PMM Extensions' engine set is two arms: the shared in-memory-SQLite `session` fixture for SQLite, and `postgres_session` from `tests/app/conftest.py` (or `postgres_celery_beat_session` from `tests/app/tasks/periodic/conftest.py` for a beat-bound helper) for PostgreSQL. Mark the PG test `@pytest.mark.postgres` — the only registered marker; it selects on `$EXTENSIONS_TEST_POSTGRES_DSN`. **Reuse those fixtures; don't add a second PG engine fixture** — that is the duplicate DB bootstrap the scaffolding rule above forbids. SQLite is not a substitute for PostgreSQL. The same applies to any code path with Postgres-specific error or lifecycle semantics — aborted-transaction handling, `RETURNING`, relationship / lazy-load, deferred columns, NULL ordering — not just rendered SQL: cover it against a real Postgres engine.

## Don't ratify the implementation

`assert "<token>" in compiled_sql` with `<token>` copied verbatim from the SUT is a tautology. The same holds for any rendered output — a substring match against the report PDF template's HTML ratifies the template, not the code's contract. Drive the SUT with inputs and assert observable outputs (status, row count, parsed JSON fields).

## Loops, enums, settings, private state

- **Loops:** a polling/retry SUT MUST have a test that drives ≥2 iterations.
- **Recovery branches:** any `try`/`except` whose body promises non-trivial recovery (rollback-then-retry, default-fallback, replay) MUST have a test that forces the trigger condition. Pure log-and-reraise is exempt.
- **External-API edge cases:** a SUT that calls an external API needs coverage for empty response (`return_value=[]`), `None` optional fields, error status, connection failure, and partial data — not just the happy path.
- **Enums:** derive from `TaskHistoryStatusEnum.SUCCESS.value`, not `"success"`. When the test's point is that a value *validated into* the enum member (not merely matches the string), assert `field is Enum.MEMBER` (identity) alongside `field.value == "…"` — `StrEnum` equals its raw string, so `==` passes even for a string that bypassed validation.
- **Settings:** capture from the live settings object; don't hardcode the YAML default. The override value MUST differ from the resolved default (`override = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT`) — otherwise the test passes whether the override fired or not.
- **Private state:** `_CACHE`, any `_<priv>` module global — expose a public getter.
- **Test imports don't widen visibility:** don't drop a leading underscore or add to `__all__` just so a test can import a helper. Tests may import module-private names directly (`from app.<mod> import _resolve_field`; `SLF001` is relaxed in `tests/`) — keep intra-module helpers private. This carve-out is narrow: it covers a `test_*.py` importing a private name **from the very module it is the test for** (the `tests/<pkg>/…/test_<mod>.py` ↔ `<pkg>/…/<mod>.py` correspondence), where the symbol *is* the subject under test. A test importing a private name from an *unrelated* module is an ordinary cross-module-private violation and still blocks.
- **Vacuous assertions:** a set-membership or `any(...)` assertion that passes when the SUT never ran (`observed.issubset({a, b, c})` is trivially true when `observed == set()`) needs a precondition that fails fast — `assert observed, "<what should have happened>"`. A blocking check reads every form whose truth is unchanged on an empty subject — the set-algebra ones (`<=`, `issubset`, `isdisjoint`), `not in`, and `all(...)` — and clears each on a **positive control in the same function** that pins the subject non-empty; a genuinely-safe instance carries a trailing `# vacuous-ok: <why>`, so don't flag those. A second axis blocks an assertion no case in an enclosing literal `@pytest.mark.parametrize` table can falsify: it reads only the parametrized argnames and holds for every case, or it constrains only a value's *type* (`assert result is None or isinstance(result, str)`), restating the return annotation instead of the distinction the table draws — parametrize `(input, expected)` and assert equality per case.
- **Annotations:** `pyproject.toml` disables `ANN` for `test_*.py`, but that is a **lint-noise concession, not a relaxed convention** — the file's own prevailing style is the tiebreak, so an unannotated `monkeypatch` among siblings that all write `monkeypatch: pytest.MonkeyPatch` reads as an oversight and should be annotated. The exemption is also narrower than it looks: `conftest.py` carries `["S", "ARG001", "ARG002"]` and **not** `ANN`, so annotations there are mechanically required. A **shared-ancestor `conftest.py`** is where a loose annotation does most damage — promoting a fixture up from a leaf conftest makes it the copied exemplar for every test below it, so tighten its annotations *at the move*.
- **Match the dominant form in the file you're editing, not just in sibling files.** The file under edit is the strongest evidence of the local idiom, and a divergence inside it is the one the next reader hits first; a split inside one file is internal inconsistency, not two valid styles. Recurring shapes: a side-effect-only fixture requested as a parameter where siblings in the same module use `@pytest.mark.usefixtures`; two tests differing in one input where the module already parametrizes that exact shape with `ids=[...]`; an unguarded `re.search(...).group(1)` beside siblings that bind the match and assert it is not `None` first.

## Retiring a test is retiring a guard — name what it proved

A diff that drops, rewrites, or narrows an existing test is removing a guard, and the reviewable question is never "does this test still fit the new code" — it is **what invariant did this test prove, and what still proves it afterwards**. Answer it per test. A test-revision table prescribing an action per row (`drop`, `rewrite`, `re-anchor`) without that column is a list of edits, not an analysis.

The failure is invisible to every gate: deleting a test and deleting the behaviour it guarded leaves the suite **green**, with no red and no coverage drop naming the lost case, and the diff reads as consistent cleanup. Green after a deletion is evidence of nothing. Two shapes need different answers — **the behaviour moved** (name the test that now proves the invariant, by file and test name; if you can't, the invariant is unguarded and the edit is incomplete), or **the behaviour went away** (then the deletion is correct *and* the behaviour's removal is a change in its own right, belonging in the PR description and, if user-visible, a changelog fragment — a test deletion is not a silent carrier for a behaviour deletion).

**Read the assertion, not the test name.** A row's prescribed action is usually justified from what the test is *called* or appears to cover; the invariant lives in the assertion body, and the two diverge exactly where this matters.

## Cleanup & duplicates

`TestClient`/`AsyncClient` fixtures reset `dependency_overrides = {}` in teardown. An app-local `test_client` fixture MUST override **both** method gates, `require_bearer_for_unsafe_methods` and `require_minimum_role_for_unsafe_methods`, alongside `get_current_user`; the synchronous one also points `get_session` at the in-memory session so `require_app_enabled` reads an isolated `appstate` table. A fixture that drops a gate fails the mutation it was written to exercise with a **401 the route never raised**, which reads as an auth bug in the code under test rather than a fixture defect. Copy the override set from the `test_client` / `async_test_client` fixtures in `tests/app/conftest.py`, not from a paraphrase. Async: `@pytest.mark.asyncio`, `@pytest_asyncio.fixture`. Two tests differing only in one input → `@pytest.mark.parametrize`.

Never wrap `TestClient(extensions_app)` in `with …:` for a unit test — the context-manager form enters `extensions_lifespan` → `init_extensions_db()` and seeds the Celery beat DB, turning a unit test into a lifespan-dependent one with hidden cross-test coupling. If the lifespan path is genuinely needed, patch the startup chain to a no-op first.

## Test-shape smells

- **Eager** — multiple Acts (independent exercises of the SUT) in one test → `@pytest.mark.parametrize`, one Act per case.
- **Conditional logic** — `if`/`elif` over the SUT's branching state inside a test body → split into one test per branch.
- **Greedy catcher** — `try`/`except` to assert an expected exception → `pytest.raises`.
- **Sequencer / free ride** — a test that depends on execution order, or repeats teardown the fixture should own → move cleanup into a `@pytest.fixture` with `yield`.
