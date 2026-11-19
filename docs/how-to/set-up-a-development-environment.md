# Set up a development environment

This page takes you from nothing to a checkout where the tests, the linters and the type check run. It does not start PMM Extensions. To run it, see [Run PMM Extensions in a local PMM](run-in-local-pmm.md).

## Prerequisites

- Git and `make`.
- Python 3.11.9 to 3.13. Versions 3.12.0 to 3.12.2 do not work.
- Pango, which WeasyPrint loads when the tests import the `report` app. [WeasyPrint's installation page](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html) names the package for each system.
- Linux or macOS. The steps were tested on Linux x86-64.

## Clone the repository

1. Clone the repository and enter it:

   ```bash
   git clone https://github.com/percona/pmm-extensions.git
   cd pmm-extensions
   ```

## Build the virtualenv

Start from a shell with no virtualenv active. When one is active, `make venv` installs the project into that virtualenv and creates no `venv/`.

1. Build the virtualenv with an interpreter from the supported range:

   ```bash
   make venv PYTHON=python3.11
   ```

   `PYTHON` is the interpreter that the virtualenv is built from. Give a full path when the interpreter is not on your `PATH`.

   The command creates two directories. `venv/` holds the project and its dependencies. `.poetry-venv/` holds Poetry, the tool that installs them. The last line of the output is:

   ```text
   Installing the current project: pmm-extensions (3.11.0.dev0)
   ```

   The version in that line changes with each release.

   If the interpreter is outside the supported range, the command stops before it creates anything:

   ```text
   venv: Python 3.14.10 is not supported. Use 3.11.9 to 3.13, except 3.12.0 to 3.12.2. Example: make venv PYTHON=python3.12
   ```

2. Activate the virtualenv:

   ```bash
   source venv/bin/activate
   ```

   Run this in every new shell. The commands below need it.

## Install the commit hooks

1. Install the hooks:

   ```bash
   pre-commit install
   ```

   The output is:

   ```text
   pre-commit installed at .git/hooks/pre-commit
   ```

   From now on, each commit runs the checks on the files you changed.

## Run the tests

1. Run one test file:

   ```bash
   pytest tests/scripts/test_check_python_version.py -q
   ```

   The output ends with a line such as:

   ```text
   8 passed in 0.05s
   ```

2. Run the whole suite:

   ```bash
   make test
   ```

   The suite runs in parallel, on one worker for each CPU. It fails if a test fails, or if coverage of `app/` is under 85%.

To run the tests of one app, see [Test an app](test-an-app.md).

## Run the linters

1. Check the Python code with Ruff:

   ```bash
   make lint
   ```

2. Run the type check:

   ```bash
   make typecheck
   ```

   The command ends with a count of diagnostics. [Run the type check](run-the-type-check.md) explains how to read the output, and what makes the command fail.

3. If you changed a page under `docs/`, check it:

   ```bash
   make docs-lint
   ```

   The first run downloads Vale into a directory that Git ignores. The output ends with a count, such as:

   ```text
   ✖ 0 errors, 12 warnings and 0 suggestions in 24 files.
   ```

   The command fails only on an error. The warnings today are headings that hold a product name or an app's name.

   The rules are in [Writing standard](../writing-standard.md).

## Verify

The environment is ready when these three commands succeed:

```bash
pytest tests/scripts/test_check_python_version.py -q
make typecheck
make docs-lint
```

## Next steps

- [Build your first app](../tutorials/first-app.md)
- [Run PMM Extensions in a local PMM](run-in-local-pmm.md)
- [Contributing](../../CONTRIBUTING.md), before you open a pull request
