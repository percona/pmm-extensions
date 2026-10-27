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

"""Keep the Tasks and Inventory services from importing ``app.extensions``.

The standalone Tasks and Inventory processes reach the PMM Extensions database
for the shared ``Settings`` and ``AlertSettings`` overrides through
``app.core`` alone. An import of ``app.extensions`` from either service would
pull the PMM Extensions app tree into processes that ship without it.
"""

import ast
from pathlib import Path

import pytest

from app import BASE_DIR
from tests.app.import_ast import absolute_base, package_of

#: The service packages the boundary covers.
_SERVICE_PACKAGES = ("app/tasks", "app/inventory")

#: The package the services must not import.
_FORBIDDEN = "app.extensions"


def _reaches_forbidden(dotted: str | None) -> bool:
    """Return whether ``dotted`` names ``app.extensions`` or a submodule.

    :param dotted: An absolute dotted module path, or ``None``.
    :return: Whether the path is inside the forbidden package.
    """
    return dotted is not None and (
        dotted == _FORBIDDEN or dotted.startswith(f"{_FORBIDDEN}.")
    )


def forbidden_imports(root: Path, base: Path) -> list[str]:
    """List every import under ``root`` that reaches ``app.extensions``.

    Walks the whole tree of each module, so imports inside functions and
    ``TYPE_CHECKING`` blocks count, and resolves relative imports against the
    module's package. A ``from app import extensions`` is caught through the
    imported name.

    :param root: The directory whose ``.py`` files are checked.
    :param base: The directory module paths are dotted relative to.
    :return: ``"<path>:<line>: <module>"`` for each offending import.
    """
    offences: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = package_of(path, base)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                reached = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = absolute_base(node, package)
                reached = [module or ""] + [
                    f"{module}.{alias.name}" for alias in node.names if module
                ]
            else:
                continue
            offending = next(
                (name for name in reached if _reaches_forbidden(name)), None
            )
            if offending is not None:
                offences.append(f"{path.relative_to(base)}:{node.lineno}: {offending}")
    return offences


@pytest.mark.parametrize("service", _SERVICE_PACKAGES)
def test_service_does_not_import_extensions(service: str) -> None:
    """Import nothing from ``app.extensions``, migrations included."""
    assert forbidden_imports(BASE_DIR / service, BASE_DIR) == []


class TestForbiddenImports:
    """Prove the walker catches each import shape it must."""

    @pytest.mark.parametrize(
        "source",
        [
            "def load():\n    import app.extensions.config\n",
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    from app.extensions.db import engine\n"
            ),
            "from app import extensions\n",
            "from ..extensions import config\n",
        ],
        ids=["function-level", "type-checking", "from-package", "relative"],
    )
    def test_reports_an_import_of_extensions(self, tmp_path: Path, source: str) -> None:
        """Report the import wherever and however it is spelled."""
        module = tmp_path / "app" / "tasks" / "module.py"
        module.parent.mkdir(parents=True)
        module.write_text(source, encoding="utf-8")

        assert len(forbidden_imports(tmp_path / "app" / "tasks", tmp_path)) == 1

    def test_ignores_a_sibling_whose_name_shares_the_prefix(
        self, tmp_path: Path
    ) -> None:
        """Leave ``app.extensions_owned``-style names alone."""
        module = tmp_path / "app" / "tasks" / "module.py"
        module.parent.mkdir(parents=True)
        module.write_text(
            "from app.core.settings_override.extensions_owned import x\n",
            encoding="utf-8",
        )

        assert forbidden_imports(tmp_path / "app" / "tasks", tmp_path) == []
