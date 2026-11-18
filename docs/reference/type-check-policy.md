# Type-check policy

PMM Extensions checks its Python code with `ty`. This page records what the check covers, what CI enforces, the severity of each rule, and the diagnostics that are suppressed on purpose.

To run the check, see [Run the type check](../how-to/run-the-type-check.md).

## Checked files

`[tool.ty.src]` in `pyproject.toml` defines which files are checked. `make typecheck` passes no path, so that table is the only definition.

| Setting | Value |
|---|---|
| Included | `app`, `tests`, `scripts`, `sidecar` |
| Excluded | every `migrations` directory, and `frontend` |
| Python version | 3.11, the oldest version the project supports |

## Enforcement

Two CI jobs run the check. They enforce different things.

| | `typecheck` | `typecheck_diff` |
|---|---|---|
| Workflow | `.github/workflows/python.yaml` | `.github/workflows/ci.yml` |
| Command | `make typecheck` | `make typecheck-diff` |
| Files | Every checked file | The Python files that the branch changed, outside `tests/` |
| Fails on | A diagnostic of a rule at `error` | A diagnostic that the branch adds, compared with its merge base. For the comparison, every rule at `warn` counts as `error`. |
| Blocks a merge | Yes. It is part of the `python` job, which `ci-success` needs. | No. `ci-success` does not need it. A failure turns this one check red. |

`ci.yml` starts on a `pull_request` event and on a manual `workflow_dispatch`. The `typecheck_diff` job runs only on `pull_request`, for a pull request that is not a draft and that changes Python files. It does not run on a pull request labeled `lint-fmt-only`.

The job takes the base commit from the output of the `label-gate` job, which reads the pull request's current base. It compares the branch with the merge base of that commit.

Before `make typecheck`, the `typecheck` job runs `poetry sync --no-root --all-extras --all-groups`. `ty` resolves imports against the packages that are installed, so the result depends on that set.

A rule at `warn` prints its diagnostics and does not fail `make typecheck`. The `typecheck_diff` job is what stops a branch from adding more of them without notice.

## Rule severities

`[tool.ty.rules]` in `pyproject.toml` sets the severities. A rule that the table does not list is at `error`, because the table starts with `all = "error"`.

### Rules at `warn`

Each of these rules reports real defects together with reports caused by how a dependency declares its types.

| Rule | What it mostly reports here |
|---|---|
| `invalid-argument-type` | The split between SQLModel's and SQLAlchemy's `AsyncSession`, and real `int` against `int \| None` mismatches |
| `unresolved-attribute` | Real missing narrowing of an optional value, test doubles, and gaps in third-party stubs |
| `unknown-argument` | The private constructor arguments of pydantic-settings, such as `_env_file` and `_secrets_dir` |
| `invalid-assignment` | The pydantic `Field(...)` idiom |
| `missing-argument` | `Settings()`, which fills its required fields from the environment |
| `call-non-callable` | Subscripted generics, fixtures typed as `object`, and real calls of `None` |
| `no-matching-overload` | Third-party overload sets: `AsyncSession.exec`, `select`, `create_model` |
| `unresolved-import` | The golden app templates, which the test run generates |
| `invalid-parameter-default` | The FastAPI `Query(...)` default idiom |

### Rules at `error`

These rules mostly report defects in this repository's own annotations. The table lists the ones that `pyproject.toml` names. Every unlisted rule is also at `error`.

| Rule | What it mostly reports here |
|---|---|
| `invalid-return-type` | Returns that can be `None`, and generator fixtures annotated with the yielded type |
| `not-subscriptable` | A subscript on a value that can be `None` |
| `invalid-type-form` | Annotations that name a class chosen at runtime |
| `unsupported-operator` | An operand of type `X \| None` that was not narrowed |
| `invalid-method-override` | Overrides of this repository's own base classes |
| `not-iterable` | Includes `async for` over a coroutine that was not awaited |
| `call-top-callable` | A call after `callable()` narrowing, which drops the signature |
| `unbound-type-variable` | The generics in `app/core` |
| `invalid-paramspec` | `*args: P.args` without the matching `**kwargs: P.kwargs` |
| `invalid-context-manager` | An `__aexit__` signature that prevents `async with` |
| `empty-body` | An implicit `None` return against another annotation |
| `redundant-cast` | A cast to the type the value already has |
| `possibly-missing-submodule` | A submodule that is used and not imported |
| `mismatched-type-name` | A `StrEnum` whose name differs from the variable it is assigned to |
| `invalid-yield` | An optional value yielded against an annotation that is not optional |
| `invalid-key`, `invalid-base` | As named |
| `invalid-attribute-access` | An assignment to a `ClassVar` through an instance |
| `deprecated` | A use of something a library has deprecated |
| `possibly-unresolved-reference`, `possibly-missing-attribute`, `unsupported-dynamic-base` | Three rules that `ty` ships disabled. `all = "error"` turns them on. |

### Rules at `ignore`

| Rule | Why |
|---|---|
| `missing-type-argument` | Bare generics are common in the code and intended. |
| `missing-override-decorator` | The project has not adopted the `@override` decorator. |

## Diagnostic groups

Some diagnostics describe how a dependency declares its types, not a defect in this repository. `scripts/classify_ty_diagnostics.py` defines them as groups. Each group names a rule and the message pattern that separates its diagnostics from real ones under the same rule.

A diagnostic in a group is suppressed in one of two ways. An entry under `[[tool.ty.overrides]]` in `pyproject.toml` turns the rule off for whole files where every report of it belongs to the group. In a file where the rule also reports real defects, each site has a `# ty: ignore[rule]` comment.

| Group | Rule | What identifies it |
|---|---|---|
| `settings-subclass-attributes` | `unresolved-attribute` | The attribute is one of three helpers that the settings-override proxy adds to a `*Settings` subclass. |
| `env-populated-required-params` | `missing-argument` | The required parameter is `CELERY` or `_session`, which the environment or dependency injection fills. |
| `pydantic-settings-private-kwargs` | `unknown-argument` | The argument is a private keyword of pydantic-settings, which the generated `__init__` does not declare. |
| `third-party-overload-sets` | `no-matching-overload` | The function called is `AsyncSession.exec`, `select` or `create_model`. |
| `sa-type-typedecorator` | `invalid-argument-type` | `sa_type=` receives a `TypeDecorator` instance where the stub expects a type. |
| `absent-modules` | `unresolved-import` | The module does not exist when the check runs: a golden app that the test run generates, or a payload that runs on a host. The group is limited to those paths. |
| `subscripted-generics-called` | `call-non-callable` | The object called is a subscripted generic, which can be called at runtime. |
| `fastapi-query-default` | `invalid-parameter-default` | The default is a FastAPI `Query(...)` marker. |
| `pygments-textlexer` | `unresolved-import` | `pygments.lexers` exports `TextLexer` lazily, so its stub does not list it. |
| `predicate-dsl-comparison-operators` | `invalid-method-override` | The override is `__eq__` or `__ne__` in the rules DSL, which return a predicate. The group is limited to `app/extensions/apps/framework/rules.py`. |
| `runtime-computed-model-in-type-position` | `invalid-type-form` | The annotation names a class that is chosen at runtime. |
| `factory-built-annotated-alias` | `invalid-type-form` | The annotation is a call to a function that returns an `Annotated` alias. The group is limited to `app/extensions/apps/mysql_backups/forms.py`. |

The script's `check` mode fails when a suppression hides a diagnostic that no group claims.
