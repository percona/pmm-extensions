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

"""Test the shared Celery application and its per-process event loop."""

import asyncio
import pickle
from collections.abc import Generator

import pytest
from celery import Celery

from app.celery import celery, ExtensionsCelery, init_child_event_loop

_ANSWER = 42


@pytest.fixture
def original_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Restore the app's loop and the current loop after the test."""
    original = celery.loop
    yield original
    replaced = celery.loop
    celery.loop = original
    asyncio.set_event_loop(original)
    if replaced is not original:
        replaced.close()


class TestCeleryApp:
    """Test how the shared Celery application is built."""

    def test_is_built_from_the_loop_declaring_subclass(self) -> None:
        """Build the app from the subclass, which is still a ``Celery``."""
        assert type(celery) is ExtensionsCelery
        assert isinstance(celery, Celery)

    def test_import_installs_an_open_loop(self) -> None:
        """Install an open event loop on the app at import."""
        assert isinstance(celery.loop, asyncio.AbstractEventLoop)
        assert not celery.loop.is_closed()

    def test_survives_a_pickle_round_trip(self) -> None:
        """Unpickle to the same subclass, as a spawned worker process does."""
        restored = pickle.loads(pickle.dumps(celery))

        assert type(restored) is ExtensionsCelery
        assert restored.main == celery.main


class TestInitChildEventLoop:
    """Test the worker_process_init handler."""

    def test_installs_a_fresh_current_loop(
        self, original_loop: asyncio.AbstractEventLoop
    ) -> None:
        """Replace the inherited loop with a new, open, current one."""
        init_child_event_loop()

        assert celery.loop is not original_loop
        assert not celery.loop.is_closed()
        assert asyncio.get_event_loop() is celery.loop

    @pytest.mark.usefixtures("original_loop")
    def test_runs_coroutines_on_the_new_loop(self) -> None:
        """Drive a coroutine to completion on the replacement loop."""

        async def _answer() -> int:
            return _ANSWER

        init_child_event_loop()

        assert celery.loop.run_until_complete(_answer()) == _ANSWER
