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

"""Pin that a script under test resolves to exactly one module object.

``load_script`` used to register each CLI under its bare name while the same
file stayed importable as ``scripts.<name>``, which Python treats as two
unrelated modules. A test file reaching one script both ways then held two
copies of every class in it, and the failure surfaced as a ``pytest.raises``
reporting ``DID NOT RAISE`` against an exception the traceback showed being
raised. That misattributes to the code under test, so it is expensive to
diagnose and cheap to prevent.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.scripts import load_script


def test_both_routes_return_the_same_module() -> None:
    """Resolve the helper and a package import to one module object."""
    assert load_script("classify_ty_diagnostics") is importlib.import_module(
        "scripts.classify_ty_diagnostics"
    )


def test_a_class_is_the_same_object_across_routes() -> None:
    """Pin the class identity that ``except`` and ``pytest.raises`` compare.

    Asserting on the module alone would pass even if the helper re-executed
    the source, so this pins the class object an exception match resolves.
    """
    from scripts.classify_ty_diagnostics import ReconciliationError  # noqa: PLC0415

    assert load_script("classify_ty_diagnostics").ReconciliationError is (
        ReconciliationError
    )


def test_the_bare_name_is_not_registered() -> None:
    """Keep the bare name out of ``sys.modules``, so no second copy exists.

    This is the removal check: reinstate the by-path load and this fails,
    where the two assertions above could still pass by accident of import
    order.
    """
    load_script("classify_ty_diagnostics")

    assert "classify_ty_diagnostics" not in sys.modules


def test_an_unknown_script_raises_runtime_error() -> None:
    """Raise ``RuntimeError`` for a script that does not exist."""
    with pytest.raises(RuntimeError, match="cannot load"):
        load_script("no_such_script_exists")
