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

"""Define tests for Celery beat riding out an outage of its schedule database.

Celery's beat service exits on any exception from ``tick()``, and dev mode's
``--start-celery`` does not restart it. ``DatabaseScheduler`` reads the database
on every tick through its ``schedule`` property, so an outage of PMM's
PostgreSQL, such as a PMM Server restart, must not raise out of it.
"""

import shutil
from datetime import timedelta
from pathlib import Path

from celery import Celery
from pytest_mock import MockerFixture
from sqlalchemy_celery_beat.schedulers import DatabaseScheduler


def test_beat_keeps_sending_tasks_while_the_database_is_down(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Keep sending due tasks on the schedule already loaded while the database is down.

    sqlalchemy-celery-beat 0.8.4 raised ``OperationalError`` here, and beat exited.

    :param tmp_path: The per-test temporary directory.
    :param mocker: The pytest-mock fixture.
    """
    store = tmp_path / "store"
    store.mkdir()
    app = Celery("test", broker="memory://", set_as_current=False)
    app.conf.result_expires = None
    app.conf.beat_schedule = {"probe": {"task": "probe", "schedule": 60.0}}
    scheduler = DatabaseScheduler(app=app, dburi=f"sqlite:///{store / 'schedule.db'}")
    loaded = dict(scheduler.schedule)
    assert scheduler.tick() > 0
    loaded["probe"].last_run_at -= timedelta(minutes=2)

    scheduler.engine.dispose()
    shutil.rmtree(store)
    send_task = mocker.patch.object(app, "send_task")

    assert scheduler.tick() == 0
    assert scheduler.tick() > 0
    send_task.assert_called_once()
    assert send_task.call_args.args[0] == "probe"
    assert scheduler.schedule.keys() == loaded.keys() == {"probe"}
