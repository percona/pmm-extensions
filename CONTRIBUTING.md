# Contributing to PMM Extensions

This page says how a change gets from your checkout into this repository. The [README](README.md) links every other page.

## Set up

Follow [Set up a development environment](docs/how-to/set-up-a-development-environment.md). To run your change inside PMM, follow [Run PMM Extensions in a local PMM](docs/how-to/run-in-local-pmm.md).

To add an app, start with [Build your first app](docs/tutorials/first-app.md).

## Branches

Create your branch from `main`. Name it after the ticket key of the change, `PMM-NNNNN`, with an optional suffix: `PMM-NNNNN-docs`. Keys that start with `SEP-` stay valid.

If you have no ticket key, contribute from a fork:

1. Open an issue on GitHub that describes the change, before you start the work.
2. Fork the repository.
3. Create a branch in your fork, with any name. CI does not check the branch name.
4. Open the pull request from your fork against `main`, and name the issue in its description.

A maintainer creates the ticket key and writes it in the issue. Then put that key at the start of the pull request's title. A changelog fragment needs the same key in its name.

On a pull request from a fork, CI starts only after a maintainer approves the run. A maintainer also sets the QA label that the `label-gate` check requires.

## Code

- **Style.** The commit hooks format and lint the code with Ruff. The rules are in `pyproject.toml`.
- **Types.** The code is fully annotated, and CI runs a type check: see [Run the type check](docs/how-to/run-the-type-check.md).
- **Docstrings.** Every public module, class and function has one. `.github/instructions/python-docstrings.instructions.md` holds the rules.
- **Tests.** A change in behavior comes with a test. Tests mirror `app/` under `tests/app/`, and the full suite fails below 85% coverage. For an app, see [Test an app](docs/how-to/test-an-app.md).
- **Frontend.** CI fails on unused files, exports and dependencies under `frontend/`. Run `pnpm knip` there, and remove what it reports.

## Changelog fragments

A change that users can see needs a changelog fragment. That means a new feature, a fix, a change in behavior, a security fix or a configuration change.

1. Create the fragment:

   ```bash
   make changelog-add TICKET=PMM-NNNNN SECTION=added MSG="One sentence that describes the change."
   ```

   `SECTION` is one of `added`, `changed`, `breaking`, `config`, `fixed` and `security`. For a change that belongs in two sections, run the command once for each.

2. Commit the file that the command created under `changelog.d/`.

A change with no effect on users needs no fragment: CI, refactoring, tooling and documentation. [`changelog.d/README.md`](changelog.d/README.md) has the full rules and the format.

## Commits and pull requests

- Start each commit subject with its type: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci` or `build`.
- Open the pull request against `main`. Start its title with the ticket key, and say in the description what changed and how you tested it.
- CI runs the tests, the linters, the type check and the documentation lint. A pull request merges only when its required checks pass.

## Review

Every pull request needs an approval from an owner of the paths that it changes. `.github/CODEOWNERS` maps each path to its reviewers, and GitHub requests their review when the pull request opens.

[Ownership of the framework and the apps](docs/explanation/ownership.md) explains who decides what.

## Documentation

Every page follows [the writing standard](docs/writing-standard.md). Before you commit a page, run the documentation lint:

```bash
make docs-lint
```

The commit hooks also check the Markdown and the links between pages.

## Where to ask

- To ask a question or report a problem, open an issue on GitHub.
- For a question about one area of the code, ask its reviewers in `.github/CODEOWNERS`.
