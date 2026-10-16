# Copyright (C) 2026 Percona LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Load the ``scripts/`` CLIs under test as importable modules."""

import importlib
from pathlib import Path
from types import ModuleType

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def write_file(tmp_path: Path, name: str, text: str) -> Path:
    """Write ``text`` to ``tmp_path/name`` and return the path.

    :param tmp_path: pytest's per-test temporary directory.
    :param name: The filename to create under ``tmp_path``.
    :param text: UTF-8 contents to write.
    :return: The newly-written path.
    """
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def load_script(name: str) -> ModuleType:
    """Return ``scripts/<name>.py`` as the one module object for that script.

    Delegates to :func:`importlib.import_module` under the package-qualified
    name, so this helper and ``from scripts.<name> import X`` resolve to one
    object. Do not reintroduce a by-path load: it registers a second module
    under the bare name, whose classes then fail ``except`` and
    ``pytest.raises`` against the package copy's.
    ``tests/scripts/test_loader_identity.py`` pins the invariant.

    ``scripts`` is a package whose ``__init__`` is a licence header, so the
    delegation adds no import side effects.

    :param name: The script's module name, without the ``.py`` suffix.
    :return: The loaded module.
    :raises RuntimeError: When ``scripts/<name>.py`` itself is absent. An
        ``ImportError`` raised by a module it imports propagates unchanged, so
        the traceback names the module that actually failed.
    """
    try:
        return importlib.import_module(f"scripts.{name}")
    except ImportError as exc:
        if exc.name != f"scripts.{name}":
            raise
        raise RuntimeError(f"cannot load {SCRIPTS_DIR / f'{name}.py'}") from exc


def ci_filter_globs(name: str) -> list[str]:
    """Return the globs of one ``dorny/paths-filter`` list in the CI workflow.

    :param name: Filter key, such as ``python``.
    :return: Glob entries declared under that key, in file order.
    :raises AssertionError: When ``ci.yml`` declares no paths-filter step.
    """
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["changes"]["steps"]:
        filters = step.get("with", {}).get("filters")
        if filters is not None:
            return yaml.safe_load(filters)[name]
    raise AssertionError("ci.yml declares no paths-filter step")
