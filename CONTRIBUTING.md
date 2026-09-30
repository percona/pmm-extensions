# Contributing to PMM Extensions

- [Branching Strategy](#branching-strategy)
- [Pull Requests](#pull-requests)
- [Changelog Fragments](#changelog-fragments)
- [Setting Up Your Development Environment](#setting-up-your-development-environment)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
  * [Writing Tests](#writing-tests)
  * [Running Tests](#running-tests)
- [Getting Help](#getting-help)

## Branching Strategy

All development work should be done on feature branches derived from the main branch.
Branch names should follow the format `PMM-NNNNN`, taken from the Jira ticket key, with an optional `-suffix` (for example `PMM-12345-docs`). Existing `SEP-NNNN` keys stay valid.

Example:
```shell
git checkout main
git pull
git checkout -b PMM-12345
```

## Pull Requests

When your feature or fix is ready, open a Pull Request (PR) from your feature branch (`PMM-NNNNN`) to the main branch. Ensure the PR description includes a representative summary of your changes.

All PRs must be reviewed and approved by at least one of our [CODEOWNERS](https://github.com/percona/pmm-extensions/blob/main/.github/CODEOWNERS).

## Changelog Fragments

If your PR contains a **user-facing** change (a new feature, a bug fix, a behaviour change, a security fix, or a configuration change), add a **changelog fragment** so the entry is picked up in the next release notes. Fragments live under `changelog.d/` at the repo root — each PR writes its own file, which means parallel PRs never collide on `CHANGELOG.md`.

To create a fragment, use the Makefile helper:

```shell
make changelog-add TICKET=PMM-NNNNN SECTION=<section> MSG="Brief description"
```

`<section>` is one of `added`, `changed`, `breaking`, `config`, `fixed`, or `security`. If your change belongs in multiple sections (e.g. it is both a Change and a Breaking Change), run the command once per section.

**Skip this step when either applies:**

1. **Purely internal changes** — CI, refactoring, tooling, docs with no
   user-visible effect.
2. **Same-release-cycle fix** — the PR fixes a regression or behaviour
   change introduced by another ticket that shares this PR's Jira `Fix
   Version` and is itself still unreleased (no `[vX.Y.Z]` header for that
   Fix Version in `CHANGELOG.md`). Usually surfaces as a Jira `is caused
   by` link to a sibling ticket in the same version. The bug never
   shipped to users, so a fragment would add confusing "regression fixed"
   noise to release notes describing behaviour users never saw.

See [`changelog.d/README.md` § "TL;DR — adding an entry"](changelog.d/README.md#tldr--adding-an-entry) for the canonical rule.

See [`changelog.d/README.md`](changelog.d/README.md) for the full format and examples. A pre-commit hook validates any fragments you touch, and `make changelog-check` / `make changelog-list` are available to inspect or preview them locally.

## Setting Up Your Development Environment

1. Clone the repository

```shell
git clone https://github.com/percona/pmm-extensions.git
```
or
```shell
git clone git@github.com:percona/pmm-extensions.git
```

2. Install the dependencies

Navigate to the project directory, create a virtual environment and install the required packages:

```shell
cd pmm-extensions
make venv
```

3. Install pre-commit hooks

We use pre-commit hooks to maintain code quality. Install them by running:

```shell
pre-commit install
```

This ensures that your code adheres to our linting and formatting standards before each commit (see [Coding Standards](#coding-standards)).

## Coding Standards

- **Linting and Formatting**:

We enforce code style guidelines using [Ruff](https://docs.astral.sh/ruff/). The rules are defined in the [pyproject.toml](https://github.com/percona/pmm-extensions/blob/main/pyproject.toml) file.

- **Type checking**:

Because the project is fully type-annotated, you can check the annotations locally with [`ty`](https://github.com/astral-sh/ty) (Astral's type checker) by running `make typecheck`. It is not part of pre-commit, and a local run reports a backlog of existing warning-severity diagnostics that it exits 0 on. Two CI checks read it, and they behave differently:

- **`typecheck`** runs `make typecheck` over the whole tree and **blocks the merge**. It fails only on error-severity diagnostics, so keeping it green means not introducing one.
- **`typecheck_diff`** reports the diagnostics your branch adds against its merge-base, with every rule held at `warn` promoted to `error` for the comparison, over changed files outside `tests/`. It is **advisory**: a surplus turns that one check red and leaves the merge button enabled. So "all checks passed" is no longer the norm on every PR — a red `typecheck_diff` beside a green `ci-success` means read the job's summary, which lists each new diagnostic. Reproduce it locally with `make typecheck-diff BASE_SHA=origin/main`.

Which trees are checked, what severity each rule carries, and why enforcement is scoped this way are recorded in [docs/development/ty-policy.md](docs/development/ty-policy.md) under [Enforcement](docs/development/ty-policy.md#enforcement).

- **Docstrings**:

Use [reStructuredText (rST) docstrings](https://peps.python.org/pep-0287/) compatible with [Sphinx](https://www.sphinx-doc.org/en/master/usage/domains/python.html) for documentation.
All public modules, classes, methods, and functions should include docstrings.

Because the project is fully type-annotated, the function signature (or model field annotation) is the source of truth for types. Use `:param:` / `:return:` to describe a parameter's or return value's *meaning*, and document every parameter with a `:param:` entry. Omit `:type:` / `:rtype:` — they are optional; add one only when the documented type should intentionally differ from the annotation (for example, an `Any` whose real contract is narrower). When a type directive is needed, `:type:` is for `:param:` only and `:vartype:` for class/instance variables.

Example:

```python
class OAuthToken(BaseModel):
    """Represent an OAuth token.

    :param access_token: The token used to access protected resources.
    :param id_token: The token that contains identity information about the user.
    :param refresh_token: The token used to obtain new access tokens after the
        current one expires.
    :param token_type: The type of token, typically "bearer".
    :param expires_in: The time duration after which the token expires.
    :param scope: The scope of the access granted by the token.
    """

    access_token: str
    id_token: str
    refresh_token: str
    token_type: str
    expires_in: TimedeltaSeconds
    scope: str
```

- **Type Annotations**:

All functions and methods should include type annotations for parameters and return types.

Example:

```python
def deep_dict_update(main_dict: dict[Any, Any], update_dict: dict[Any, Any]) -> None:
    """Merge `update_dict` into `main_dict` recursively.

    Update `main_dict` with the contents of `update_dict` recursively. For each
    key in `update_dict`, if the key exists in `main_dict` and both values are
    dictionaries, merge them recursively. If the key exists in `main_dict` and
    both values are lists, prepend the non-empty list from `update_dict` to the
    list in `main_dict`, or replace with an empty list when the overlay is empty
    so profiles can clear inherited list settings. Otherwise, overwrite the value
    in `main_dict` with the value from `update_dict`.

    :param main_dict: The dictionary to be updated.
    :param update_dict: The dictionary containing updates to apply.
    """
    for key, value in update_dict.items():
        if (
            key in main_dict
            and isinstance(main_dict[key], dict)
            and isinstance(value, dict)
        ):
            deep_dict_update(main_dict[key], value)
        elif (
            key in main_dict
            and isinstance(main_dict[key], list)
            and isinstance(value, list)
        ):
            main_dict[key] = value + main_dict[key] if value else []
        else:
            main_dict[key] = value
```

## App development

PMM Extensions apps (checksums, backups, snippets, and the rest under `app/extensions/apps/`) are
built on the declarative app framework: you describe an app with a single
`TaskExecutionApp` object and the framework derives its whole HTTP surface. If
you are adding an app, start with the
[App Developer Guide](docs/development/app-developer-guide.md). It is the
zero-to-working-app path — mental model, the `make startapp` quickstart, the
form DSL, the escape hatches, and testing — with the examples sourced from real
apps.

## Testing

### Writing Tests

New code usually means new tests. Make sure new features and bug fixes include corresponding tests.
[PMM Extensions v0.1.0-alpha](https://github.com/percona/pmm-extensions/releases/tag/v0.1.0-alpha) has a test coverage of over 50%, and our goal is to continually increase this percentage.

Tests are located in the [tests/app/](https://github.com/percona/pmm-extensions/tree/main/tests/app) directory and mirror the structure of the [app/](https://github.com/percona/pmm-extensions/tree/main/app/) directory. For example:
- For [app/core/models.py](https://github.com/percona/pmm-extensions/blob/main/app/core/models.py), the tests are in [tests/app/core/test_models.py](https://github.com/percona/pmm-extensions/blob/main/tests/app/core/test_models.py).
- For [app/api/routes/oauth.py](https://github.com/percona/pmm-extensions/blob/main/app/api/routes/oauth.py), the tests are in [tests/app/api/routes/test_oauth.py](https://github.com/percona/pmm-extensions/blob/main/tests/app/api/routes/test_oauth.py).

As you can see, test filenames are prefixed with a `test_`.

### Running Tests

Ensure all tests pass before pushing your code:

```shell
make test
```

Mutation testing (`mutmut`) is an optional local spike tool, not part of
`make test`, pre-commit, or CI. Configuration for the pilot scoped to
`app/core/db/utils.py` lives under `[tool.mutmut]` in `pyproject.toml`. Run it
from the repo root; it writes a gitignored `mutants/` working copy:

```shell
poetry run mutmut run           # generate and test mutants
poetry run mutmut results       # list surviving mutants
poetry run mutmut show <mutant> # diff for a single mutant
```

On macOS, export the Homebrew library path first. `mutmut` invokes pytest
directly, so it bypasses the wrapper that `make test` uses to make WeasyPrint's
native libraries resolvable:

```shell
export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib:${DYLD_FALLBACK_LIBRARY_PATH}"
```

## Getting Help

If you have any questions or need assistance, feel free to reach out:
- Open an issue on GitHub or on Jira.
- Contact one of the [CODEOWNERS](https://github.com/percona/pmm-extensions/blob/main/.github/CODEOWNERS) or anyone from the GAS team.
