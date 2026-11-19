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

The table below has one row for each rule that `pyproject.toml` names. The levels mean this:

- A rule at `warn` reports real defects together with reports caused by how a dependency declares its types.
- A rule named at `error` mostly reports defects in this repository's own annotations. Every unlisted rule is also at `error`.
- A rule at `ignore` checks a convention that the project has not adopted.

| Rule | Level | What it mostly reports here |
|---|---|---|
| `invalid-argument-type` | `warn` | The split between SQLModel's and SQLAlchemy's `AsyncSession`, and real `int` against `int \| None` mismatches |
| `unresolved-attribute` | `warn` | Real missing narrowing of an `Optional` value, test doubles, and gaps in third-party stubs |
| `unknown-argument` | `warn` | The private constructor arguments of pydantic-settings, such as `_env_file` and `_secrets_dir` |
| `invalid-assignment` | `warn` | The pydantic `Field(...)` idiom, where the call returns a `FieldInfo` and the annotation names another type |
| `missing-argument` | `warn` | `Settings()`, which fills its required fields from the environment |
| `call-non-callable` | `warn` | Subscripted generics, fixtures typed as `object`, and real calls of `None` |
| `no-matching-overload` | `warn` | Third-party overload sets: `AsyncSession.exec`, `select`, `create_model` |
| `unresolved-import` | `warn` | The golden app templates, which the test run generates |
| `invalid-parameter-default` | `warn` | The FastAPI `Query(...)` default idiom |
| `invalid-return-type` | `error` | Returns that can be `None`, and generator fixtures annotated with the yielded type |
| `not-subscriptable` | `error` | A subscript on a value that can be `None` |
| `invalid-type-form` | `error` | PMM Extensions' own annotations that name a class chosen at runtime, such as `type[BaseUser]` and `type[BaseModel]` |
| `unsupported-operator` | `error` | An operand of type `X \| None` that was not narrowed |
| `invalid-method-override` | `error` | Overrides of this repository's own base classes |
| `not-iterable` | `error` | Includes `async for` over a coroutine that was not awaited |
| `call-top-callable` | `error` | A call after `callable()` narrowing, which drops the signature |
| `unbound-type-variable` | `error` | The generics in `app/core` |
| `invalid-paramspec` | `error` | `*args: P.args` without the matching `**kwargs: P.kwargs` |
| `invalid-context-manager` | `error` | An `__aexit__` signature that prevents `async with` |
| `empty-body` | `error` | An implicit `None` return against another annotation |
| `redundant-cast` | `error` | A cast to the type the value already has |
| `possibly-missing-submodule` | `error` | A submodule that is used and not imported |
| `mismatched-type-name` | `error` | A `StrEnum` whose name differs from the variable it is assigned to |
| `invalid-yield` | `error` | An optional value yielded against an annotation that is not optional |
| `invalid-key` | `error` | A subscript with a key that the value does not have, and a `TypedDict` built with an unknown key |
| `invalid-base` | `error` | A class base that is not a class |
| `invalid-attribute-access` | `error` | An assignment to a `ClassVar` through an instance |
| `deprecated` | `error` | A use of something a library has deprecated |
| `possibly-unresolved-reference` | `error` | A name that is possibly not defined where it is read. `ty` ships this rule disabled, and `all = "error"` turns it on. |
| `possibly-missing-attribute` | `error` | An attribute read through a union that was narrowed in part. `ty` ships this rule disabled, and `all = "error"` turns it on. |
| `unsupported-dynamic-base` | `error` | A class created with `type()` whose base `ty` cannot follow, so the hierarchy is unknown. `ty` ships this rule disabled, and `all = "error"` turns it on. |
| `missing-type-argument` | `ignore` | Bare generics, which are common in the code and intended |
| `missing-override-decorator` | `ignore` | A method that overrides another without the `@override` decorator of PEP 698, which the project has not adopted |

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
