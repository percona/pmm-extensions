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

"""Define test fixtures for alters plugin route tests."""

from collections.abc import Callable, Iterator

import pytest
from pytest_mock import MockerFixture

from app.core.exceptions import HTTPConflictException
from app.extensions.deps import check_for_conflicted_running_tasks
from app.extensions.main import extensions_app
from tests.app.extensions.conftest import (  # noqa: F401
    mock_inventory_api_dep,
    mock_task_api_dep,
)


@pytest.fixture
def _mock_check_for_conflicted_running_tasks(mocker: MockerFixture) -> Iterator[None]:
    """Mock running-task guard for alters deps (JSON update/delete DI)."""

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    mocker.patch(
        "app.extensions.apps.alters.deps.check_for_conflicted_running_tasks",
        side_effect=_noop,
    )
    previous = extensions_app.dependency_overrides.copy()
    extensions_app.dependency_overrides[check_for_conflicted_running_tasks] = lambda: (
        None
    )
    yield
    extensions_app.dependency_overrides = previous


@pytest.fixture
def _mock_check_for_conflicted_running_tasks_raises(
    mocker: MockerFixture,
) -> Iterator[Callable[[], None]]:
    """Mock running-task guard to raise HTTPConflictException."""

    def raise_conflict() -> None:
        raise HTTPConflictException("Task is already running or pending.")

    async def _raise(*_args: object, **_kwargs: object) -> None:
        raise_conflict()

    mocker.patch(
        "app.extensions.apps.alters.deps.check_for_conflicted_running_tasks",
        side_effect=_raise,
    )
    previous = extensions_app.dependency_overrides.copy()
    extensions_app.dependency_overrides[check_for_conflicted_running_tasks] = (
        raise_conflict
    )
    yield raise_conflict
    extensions_app.dependency_overrides = previous
