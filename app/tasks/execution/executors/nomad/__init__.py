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

"""Nomad task executor package."""

__all__ = ["NomadExecutor"]


def __getattr__(name: str) -> object:
    """Resolve ``NomadExecutor`` on first attribute access.

    :param name: The attribute being read.
    :return: The resolved attribute.
    :raises AttributeError: If ``name`` is not exported by this package.
    """
    if name == "NomadExecutor":
        from app.tasks.execution.executors.nomad.models import (  # noqa: PLC0415 - tasks.config imports NomadExecutor from this package (cycle)
            NomadExecutor,
        )

        globals()[name] = NomadExecutor
        return NomadExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return attribute names for ``dir()``, including the lazy export."""
    return sorted({*globals(), *__all__})
