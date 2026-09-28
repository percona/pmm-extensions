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

"""Cover the Celery beat schedule-table bootstrap the side-car runs before its APIs."""

import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy_celery_beat.models import IntervalSchedule, Period, PeriodicTask
from sqlalchemy_celery_beat.session import SessionManager

from app import BASE_DIR
from app.core.celery import bootstrap
from app.core.celery.config import PoolEngineOptions
from app.core.config import settings
from tests.app.beat_autogenerate import BEAT_TABLES, table_names

MAKEFILE = BASE_DIR / "Makefile"
"""The developer entry point this module asserts drives the bootstrap."""

OVERRIDDEN_STORE = "postgresql+psycopg2://beat:{password}@beat-store.example:6543/beat"
"""A beat store deliberately unlike the PMM Extensions database, for the override cases."""

REFUSALS_BEFORE_THE_STORE_ANSWERS = 2
"""Connection attempts the flaky-store cases turn away before accepting one.

Bounded so the readiness loop is entered more than once — the wait itself has no
bound, so a stand-in that refused forever would hang the suite rather than fail.
"""


class RecordingSessionManager:
    """Record what the bootstrap hands the library, over the URL it was given.

    Substituted for ``SessionManager`` so the two calls the bootstrap makes can be
    asserted without a reachable store. ``create_engine`` opens no connection, so
    the engine handed back carries the resolved URL for the readiness probe to
    report while touching nothing.
    """

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.create_session_calls: list[tuple[str, str | None, dict[str, Any]]] = []
        self.prepare_models_calls: list[tuple[Engine, str | None]] = []

    def create_session(
        self, dburi: str, schema: str | None = None, **kwargs: Any
    ) -> tuple[Engine, None]:
        """Record the resolution arguments and return an engine for that URL.

        :param dburi: The store URL the bootstrap resolved.
        :param schema: The schema the bootstrap resolved.
        :param kwargs: Any engine options the bootstrap chose to forward.
        :return: An unconnected engine and a placeholder for the session maker.
        """
        self.create_session_calls.append((dburi, schema, kwargs))
        return create_engine(dburi), None

    def prepare_models(self, engine: Engine, schema: str | None = None) -> None:
        """Record the engine and schema the table creation was asked for.

        :param engine: The engine the bootstrap built.
        :param schema: The schema the bootstrap resolved.
        """
        self.prepare_models_calls.append((engine, schema))


@pytest.fixture
def sqlite_beat_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the beat store at an empty SQLite file for the length of one test.

    :param tmp_path: The per-test temporary directory.
    :param monkeypatch: The attribute patcher.
    :return: The store URL the bootstrap will resolve.
    """
    url = f"sqlite:///{tmp_path / 'schedule.db'}"
    monkeypatch.setattr(settings.CELERY, "beat_dburi", url)
    monkeypatch.setattr(settings.CELERY, "beat_schema", None)
    return url


@pytest.fixture
def recording_manager(monkeypatch: pytest.MonkeyPatch) -> RecordingSessionManager:
    """Swap the library's session manager for a recording stand-in.

    The stand-in hands back no session maker, and there is no store to read, so
    the schedule move that follows table creation is stubbed out as well.

    :param monkeypatch: The attribute patcher.
    :return: The instance the bootstrap will drive.
    """
    manager = RecordingSessionManager()
    monkeypatch.setattr(bootstrap, "SessionManager", lambda: manager)
    monkeypatch.setattr(bootstrap, "move_pre_rename_periodic_tasks", lambda _: 0)
    return manager


@pytest.fixture
def store_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let every connection attempt succeed without dialling anything.

    :param monkeypatch: The attribute patcher.
    """

    def connect(self: Engine, *args: Any, **kwargs: Any) -> Any:
        return nullcontext()

    monkeypatch.setattr(Engine, "connect", connect)


@pytest.fixture
def instant_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the readiness poll interval so the wait cases do not sleep.

    :param monkeypatch: The attribute patcher.
    """
    monkeypatch.setattr(bootstrap, "STORE_READINESS_POLL_INTERVAL", 0)


@pytest.fixture
def refuse_then_accept(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Refuse a fixed number of connections, then accept every later one.

    Later attempts are answered without dialling, for the cases whose store URL
    names a host that does not exist.

    :param monkeypatch: The attribute patcher.
    :return: A callable reporting how many attempts were made.
    """
    attempts = {"count": 0}

    def connect(self: Engine, *args: Any, **kwargs: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] <= REFUSALS_BEFORE_THE_STORE_ANSWERS:
            raise OperationalError("connect", {}, Exception("starting up"))
        return nullcontext()

    monkeypatch.setattr(Engine, "connect", connect)
    return lambda: attempts["count"]


@pytest.fixture
def refuse_always(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Refuse every connection with a persistent ``OperationalError``.

    Models a rejected password or a store that never comes up — both surface as
    ``OperationalError``, which is what the bounded readiness wait retries until
    its deadline.

    :param monkeypatch: The attribute patcher.
    :return: A callable reporting how many attempts were made.
    """
    attempts = {"count": 0}

    def connect(self: Engine, *args: Any, **kwargs: Any) -> None:
        attempts["count"] += 1
        raise OperationalError(
            "connect", {}, Exception("password authentication failed")
        )

    monkeypatch.setattr(Engine, "connect", connect)
    return lambda: attempts["count"]


@pytest.fixture
def refuse_then_really_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], int]:
    """Refuse a fixed number of connections, then let the real ones through.

    The table creation that follows the wait needs a genuine connection, so this
    delegates rather than standing in once the store has "come up".

    :param monkeypatch: The attribute patcher.
    :return: A callable reporting how many attempts were made.
    """
    attempts = {"count": 0}
    real_connect = Engine.connect

    def connect(self: Engine, *args: Any, **kwargs: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] <= REFUSALS_BEFORE_THE_STORE_ANSWERS:
            raise OperationalError("connect", {}, Exception("starting up"))
        return real_connect(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "connect", connect)
    return lambda: attempts["count"]


def test_the_bootstrap_creates_the_schedule_tables(sqlite_beat_store: str):
    """Create every table beat reads, against a store that carries no schema.

    ``celery_intervalschedule`` is the one the reported traceback names: no
    alembic track creates it, so nothing but this step puts it there before the
    APIs seed their periodic tasks.
    """
    bootstrap.bootstrap_beat_schema()

    assert table_names(sqlite_beat_store) >= BEAT_TABLES


def test_the_bootstrap_is_idempotent(sqlite_beat_store: str):
    """Leave an already-populated store alone, as a restart or upgrade finds it."""
    bootstrap.bootstrap_beat_schema()
    bootstrap.bootstrap_beat_schema()

    assert table_names(sqlite_beat_store) >= BEAT_TABLES


def test_the_bootstrap_resolves_the_store_like_the_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    store_accepts: None,
):
    """Pass the scheduler's own dburi and schema to both library calls.

    ``DatabaseScheduler.__init__`` resolves the pair once and hands it to
    ``create_session`` and ``prepare_models`` alike; a bootstrap that resolved
    either differently would create the tables somewhere beat never looks.
    """
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password="pw")
    )
    monkeypatch.setattr(settings.CELERY, "beat_schema", "beat")

    bootstrap.bootstrap_beat_schema()

    dburi, create_schema, _ = recording_manager.create_session_calls[0]
    assert dburi == settings.CELERY.beat_dburi
    assert create_schema == settings.CELERY.beat_schema
    assert recording_manager.prepare_models_calls[0][1] == settings.CELERY.beat_schema


def test_the_bootstrap_uses_the_synchronous_url(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    store_accepts: None,
):
    """Resolve the psycopg2 URL, not the asyncpg one the worker engine converts to.

    ``create_session`` calls ``create_engine``, which cannot drive an async
    driver.
    """
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password="pw")
    )

    bootstrap.bootstrap_beat_schema()

    dburi, _, _ = recording_manager.create_session_calls[0]
    assert dburi.startswith("postgresql+psycopg2://")
    assert "asyncpg" not in dburi


def test_the_bootstrap_forwards_no_engine_options(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    store_accepts: None,
):
    """Withhold the pool options, which this path can only ignore or choke on.

    The library pins ``NullPool`` here and drops every ``pool``-prefixed key, so
    such an option configures nothing. ``max_overflow`` carries no such prefix, is
    passed straight to ``create_engine``, and is rejected outright — which would
    fail this step on a documented, validated setting.
    """
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password="pw")
    )
    monkeypatch.setattr(
        settings.CELERY,
        "beat_engine_options",
        PoolEngineOptions(pool_size=20, max_overflow=5, pool_timeout=30),
    )

    bootstrap.bootstrap_beat_schema()

    _, _, options = recording_manager.create_session_calls[0]
    assert options == {}


def test_a_deadline_caps_each_postgres_connect_attempt(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    store_accepts: None,
):
    """Bound each dial when a deadline is set so a dropped-packet host cannot hang.

    The wall-clock deadline is only checked after ``connect`` returns; without a
    driver ``connect_timeout`` a firewalled store blocks on the OS TCP timeout
    (often minutes) and ``make migrate`` still outruns its 60s bound.
    """
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password="pw")
    )
    monkeypatch.setattr(settings.CELERY, "beat_schema", None)

    bootstrap.bootstrap_beat_schema(deadline_seconds=60)

    _, _, options = recording_manager.create_session_calls[0]
    assert options == {
        "connect_args": {"connect_timeout": bootstrap.STORE_CONNECT_TIMEOUT}
    }


def test_a_deadline_does_not_pass_connect_timeout_to_sqlite(
    sqlite_beat_store: str,
    recording_manager: RecordingSessionManager,
    store_accepts: None,
):
    """Omit ``connect_timeout`` for SQLite: the driver rejects the argument.

    Development and CI ``make migrate`` resolve the beat store to a local SQLite
    file; forwarding the Postgres-only kwarg would fail the step on the happy path.
    """
    bootstrap.bootstrap_beat_schema(deadline_seconds=60)

    _, _, options = recording_manager.create_session_calls[0]
    assert options == {}


def test_an_unbounded_wait_forwards_no_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    store_accepts: None,
):
    """Leave the side-car's engine creation unchanged when no deadline is set."""
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password="pw")
    )

    bootstrap.bootstrap_beat_schema()

    _, _, options = recording_manager.create_session_calls[0]
    assert options == {}


def test_a_rejected_engine_option_would_fail_the_step(sqlite_beat_store: str):
    """Pin why the options are withheld: the library forwards this one verbatim.

    Guards the reasoning behind the test above — if the library ever started
    filtering ``max_overflow`` too, withholding the options would stop being
    load-bearing and this test would say so.
    """
    with pytest.raises(TypeError, match="max_overflow"):
        bootstrap.SessionManager().create_session(sqlite_beat_store, max_overflow=5)


def test_a_failed_bootstrap_is_not_swallowed(
    sqlite_beat_store: str, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
):
    """Propagate a creation failure so the one-shot lands EXITED with no sentinel.

    The sentinel is what the API gate reads, so a bootstrap that exited 0 on a
    failed create would release the APIs against the schema it did not build.
    """

    def refuse_to_create(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("CREATE TABLE", {}, Exception("permission denied"))

    mocker.patch("logging.config.dictConfig")
    monkeypatch.setattr(bootstrap.SessionManager, "prepare_models", refuse_to_create)

    with pytest.raises(OperationalError):
        bootstrap.main()


def test_the_bootstrap_waits_for_a_store_that_is_not_up_yet(
    sqlite_beat_store: str,
    instant_polling: None,
    refuse_then_really_connect: Callable[[], int],
):
    """Retry a refused connection rather than failing on a store still starting.

    The library's own retry caps at ten attempts with sub-second sleeps, which
    covers its check-then-create race and not a database still booting.
    """
    bootstrap.bootstrap_beat_schema()

    assert refuse_then_really_connect() > REFUSALS_BEFORE_THE_STORE_ANSWERS
    assert table_names(sqlite_beat_store) >= BEAT_TABLES


def test_the_readiness_wait_is_not_bounded(
    sqlite_beat_store: str, instant_polling: None, monkeypatch: pytest.MonkeyPatch
):
    """Keep waiting past any fixed budget, so a slow store is not made terminal.

    ``migrate-beat`` is a one-shot supervisord never re-runs, so a wait that gave
    up would leave its sentinel unwritable for the life of the container and hold
    every gated API behind it. The three alembic siblings loop unbounded for the
    same reason.
    """
    attempts = {"count": 0}
    refusals = 250

    def connect(self: Engine, *args: Any, **kwargs: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] <= refusals:
            raise OperationalError("connect", {}, Exception("starting up"))
        return nullcontext()

    def skip_creation(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(Engine, "connect", connect)
    monkeypatch.setattr(bootstrap.SessionManager, "prepare_models", skip_creation)
    monkeypatch.setattr(bootstrap, "move_pre_rename_periodic_tasks", lambda _: 0)

    bootstrap.bootstrap_beat_schema()

    assert attempts["count"] == refusals + 1


def test_a_bounded_readiness_wait_gives_up_after_its_deadline(
    sqlite_beat_store: str,
    instant_polling: None,
    refuse_always: Callable[[], int],
):
    """Fail ``make migrate`` on a persistent OperationalError instead of hanging.

    A rejected password and a refused connection both surface as OperationalError;
    the wall-clock deadline covers both so CI and developers see the failure within
    the bound rather than waiting forever.
    """
    with pytest.raises(
        TimeoutError, match="did not become reachable within 0"
    ) as excinfo:
        bootstrap.bootstrap_beat_schema(deadline_seconds=0)

    assert isinstance(excinfo.value.__cause__, OperationalError)
    assert refuse_always() == 1


def test_a_bounded_readiness_wait_succeeds_before_its_deadline(
    sqlite_beat_store: str,
    instant_polling: None,
    refuse_then_really_connect: Callable[[], int],
):
    """Accept the store once it answers, without waiting out the full budget."""
    bootstrap.bootstrap_beat_schema(deadline_seconds=60)

    assert refuse_then_really_connect() > REFUSALS_BEFORE_THE_STORE_ANSWERS
    assert table_names(sqlite_beat_store) >= BEAT_TABLES


@pytest.mark.parametrize("deadline_seconds", [None, 60])
def test_a_non_transient_connection_failure_is_not_retried(
    sqlite_beat_store: str,
    instant_polling: None,
    monkeypatch: pytest.MonkeyPatch,
    deadline_seconds: float | None,
):
    """Surface anything that is not "not up yet" on the first attempt.

    Only ``OperationalError`` means a store that may still appear. Retrying every
    failure class would turn a misconfiguration into an unbounded wait; a deadline
    on the OperationalError path does not change that.
    """
    attempts = {"count": 0}

    def refuse(self: Engine, *args: Any, **kwargs: Any) -> None:
        attempts["count"] += 1
        raise InterfaceError("connect", {}, Exception("driver is broken"))

    monkeypatch.setattr(Engine, "connect", refuse)

    with pytest.raises(InterfaceError):
        bootstrap.bootstrap_beat_schema(deadline_seconds=deadline_seconds)

    assert attempts["count"] == 1


def test_main_forwards_a_cli_deadline(
    sqlite_beat_store: str,
    instant_polling: None,
    refuse_always: Callable[[], int],
    mocker: MockerFixture,
):
    """Thread ``--deadline-seconds`` from the CLI into the readiness wait."""
    mocker.patch("logging.config.dictConfig")

    with pytest.raises(TimeoutError, match="did not become reachable within 0"):
        bootstrap.main(["--deadline-seconds", "0"])

    assert refuse_always() == 1


def test_the_side_car_invocation_passes_no_deadline(mocker: MockerFixture):
    """Keep ``migrate-beat``'s flagless invocation unbounded."""
    mocker.patch("logging.config.dictConfig")
    run = mocker.patch.object(bootstrap, "bootstrap_beat_schema")

    bootstrap.main([])

    run.assert_called_once_with(deadline_seconds=None)


def test_readiness_follows_an_overridden_store(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    instant_polling: None,
    refuse_then_accept: Callable[[], int],
    caplog: pytest.LogCaptureFixture,
):
    """Wait on the store ``CELERY__BEAT_DBURI`` names, not the PMM Extensions database.

    Pointing beat at a separate store is a documented deployment input, so a
    readiness wait keyed on ``EXTENSIONS_DB_HOST`` would watch the wrong host.
    """
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password="pw")
    )
    monkeypatch.setattr(settings.CELERY, "beat_schema", None)

    with caplog.at_level(logging.INFO, logger=bootstrap.__name__):
        bootstrap.bootstrap_beat_schema()

    assert "beat-store.example" in caplog.text
    assert "6543" in caplog.text


def test_the_store_password_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch,
    recording_manager: RecordingSessionManager,
    instant_polling: None,
    refuse_then_accept: Callable[[], int],
    caplog: pytest.LogCaptureFixture,
):
    """Log the host and port only: the resolved store URL carries a credential."""
    password = "s3cr3t-beat-password"
    monkeypatch.setattr(
        settings.CELERY, "beat_dburi", OVERRIDDEN_STORE.format(password=password)
    )
    monkeypatch.setattr(settings.CELERY, "beat_schema", None)

    with caplog.at_level(logging.INFO, logger=bootstrap.__name__):
        bootstrap.bootstrap_beat_schema()

    assert password not in caplog.text


def makefile_recipe(target: str) -> list[str]:
    """Return the recipe lines ``make`` runs for ``target``.

    A recipe is the run of tab-indented lines below the rule, so a step read
    this way is read from where ``make`` reads it rather than from anywhere in
    the file.

    :param target: The target whose recipe to collect.
    :return: The recipe's lines, leading tabs stripped.
    """
    recipe: list[str] = []
    in_target = False
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{target}:"):
            in_target = True
        elif in_target:
            if line.startswith("\t"):
                recipe.append(line.lstrip("\t"))
            elif line.strip():
                break
    return recipe


def test_the_migrate_target_bootstraps_the_beat_tables():
    """Drive the library's bootstrap from ``make migrate``.

    Nothing outside a container creates the schedule tables, so a developer's
    first ``--start-celery`` against a freshly migrated store otherwise waits out
    the API readiness timeout on tables only beat itself would create.

    The recipe passes a 60s readiness deadline so a persistent OperationalError
    fails the command instead of hanging; the side-car one-shot omits that flag.

    This asserts the recipe's text; no test runs the target, so a shell-level
    fault in the line would still reach CI.
    """
    recipe = makefile_recipe("migrate")
    upgrades = [index for index, line in enumerate(recipe) if "alembic --name" in line]
    bootstraps = [
        index
        for index, line in enumerate(recipe)
        if f"-m {bootstrap.__name__}" in line and "--deadline-seconds 60" in line
    ]

    assert upgrades, recipe
    assert len(bootstraps) == 1, recipe
    assert bootstraps[0] > max(upgrades)


def _schedules(url: str) -> dict[str, str]:
    """Return every stored schedule's name mapped to the task path it fires.

    :param url: The beat store URL.
    :return: ``{name: task}`` for every ``PeriodicTask`` row.
    """
    engine, session_factory = SessionManager().create_session(url)
    try:
        with session_factory() as session:
            return {row.name: row.task for row in session.query(PeriodicTask)}
    finally:
        engine.dispose()


def _seed_schedules(url: str, schedules: dict[str, str]) -> None:
    """Store one interval schedule per ``{name: task}`` entry.

    :param url: The beat store URL.
    :param schedules: The schedules to store.
    """
    engine, session_factory = SessionManager().create_session(url)
    try:
        with session_factory() as session:
            interval = IntervalSchedule(every=1, period=Period.HOURS)
            session.add(interval)
            session.flush()
            session.add_all(
                PeriodicTask(name=name, task=task, schedule_model=interval)
                for name, task in schedules.items()
            )
            session.commit()
    finally:
        engine.dispose()


def test_the_bootstrap_moves_pre_rename_schedules_forward(sqlite_beat_store: str):
    """Rename old-prefixed schedules and their task paths over never-run seeds.

    A schedule already seeded under its new name but never fired gives way to
    the old row. A schedule of another service is left alone, and a second run
    finds nothing left to move.
    """
    old_name, old_task = (
        bootstrap.PRE_RENAME_NAME_PREFIX,
        bootstrap.PRE_RENAME_TASK_PREFIX,
    )
    new_name, new_task = bootstrap.NAME_PREFIX, bootstrap.TASK_PREFIX
    bootstrap.bootstrap_beat_schema()
    _seed_schedules(
        sqlite_beat_store,
        {
            f"{old_name}purge_atw_bundles": f"{old_task}apps.atw.celery.purge",
            f"{old_name}sync_snippets": f"{old_task}snippets.celery.sync",
            f"{new_name}sync_snippets": f"{new_task}snippets.celery.sync",
            "tasks__sync_running_tasks": "app.tasks.celery.sync_running_tasks",
        },
    )

    bootstrap.bootstrap_beat_schema()

    assert _schedules(sqlite_beat_store) == {
        f"{new_name}purge_atw_bundles": f"{new_task}apps.atw.celery.purge",
        f"{new_name}sync_snippets": f"{new_task}snippets.celery.sync",
        "tasks__sync_running_tasks": "app.tasks.celery.sync_running_tasks",
    }
    engine, session_factory = SessionManager().create_session(sqlite_beat_store)
    try:
        assert bootstrap.move_pre_rename_periodic_tasks(session_factory) == 0
    finally:
        engine.dispose()


def test_the_bootstrap_moves_pre_rename_paths_in_an_operator_schedule(
    sqlite_beat_store: str,
):
    """Rewrite old package paths in any row's task and kwargs, whatever its name.

    An operator names a schedule freely, so neither row carries the seeded
    prefix: one fires a task under the old package, the other pins an inventory
    sync to a syncer named by its old dotted path.
    """
    old_task, new_task = bootstrap.PRE_RENAME_TASK_PREFIX, bootstrap.TASK_PREFIX
    syncer = "sync.syncers.pmm.PMMSyncer"
    bootstrap.bootstrap_beat_schema()
    engine, session_factory = SessionManager().create_session(sqlite_beat_store)
    try:
        with session_factory() as session:
            interval = IntervalSchedule(every=1, period=Period.HOURS)
            session.add(interval)
            session.flush()
            session.add_all(
                [
                    PeriodicTask(
                        name="nightly purge",
                        task=f"{old_task}apps.atw.celery.purge",
                        schedule_model=interval,
                    ),
                    PeriodicTask(
                        name="pinned sync",
                        task="app.tasks.celery.execute_task_by_name",
                        kwargs=f'{{"meta": {{"syncer": "{old_task}{syncer}"}}}}',
                        schedule_model=interval,
                    ),
                ]
            )
            session.commit()

        bootstrap.bootstrap_beat_schema()

        with session_factory() as session:
            rows = {
                row.name: (row.task, row.kwargs) for row in session.query(PeriodicTask)
            }
    finally:
        engine.dispose()

    assert rows["nightly purge"][0] == f"{new_task}apps.atw.celery.purge"
    assert rows["pinned sync"] == (
        "app.tasks.celery.execute_task_by_name",
        f'{{"meta": {{"syncer": "{new_task}{syncer}"}}}}',
    )


def _store_rows(url: str, rows: list[PeriodicTask]) -> None:
    """Store the given schedules on one shared hourly interval.

    :param url: The beat store URL.
    :param rows: The unsaved schedules, without a schedule model.
    """
    engine, session_factory = SessionManager().create_session(url)
    try:
        with session_factory() as session:
            interval = IntervalSchedule(every=1, period=Period.HOURS)
            session.add(interval)
            session.flush()
            for row in rows:
                row.schedule_model = interval
            session.add_all(rows)
            session.commit()
    finally:
        engine.dispose()


def test_a_legacy_schedule_keeps_its_state_over_a_fresh_seed(sqlite_beat_store: str):
    """Carry the old row's ``enabled`` choice and run count past a never-run seed."""
    old_name, new_name = bootstrap.PRE_RENAME_NAME_PREFIX, bootstrap.NAME_PREFIX
    bootstrap.bootstrap_beat_schema()
    _store_rows(
        sqlite_beat_store,
        [
            PeriodicTask(
                name=f"{old_name}sync_snippets",
                task=f"{bootstrap.PRE_RENAME_TASK_PREFIX}snippets.celery.sync",
                enabled=False,
                total_run_count=42,
            ),
            PeriodicTask(
                name=f"{new_name}sync_snippets",
                task=f"{bootstrap.TASK_PREFIX}snippets.celery.sync",
            ),
        ],
    )

    bootstrap.bootstrap_beat_schema()

    engine, session_factory = SessionManager().create_session(sqlite_beat_store)
    try:
        with session_factory() as session:
            rows = [
                (row.name, row.task, row.enabled, row.total_run_count)
                for row in session.query(PeriodicTask)
            ]
    finally:
        engine.dispose()
    assert rows == [
        (
            f"{new_name}sync_snippets",
            f"{bootstrap.TASK_PREFIX}snippets.celery.sync",
            False,
            42,
        )
    ]


def test_a_schedule_that_fired_under_both_names_fails_the_step(
    sqlite_beat_store: str,
):
    """Refuse to pick between two schedules that each carry run state."""
    old_name, new_name = bootstrap.PRE_RENAME_NAME_PREFIX, bootstrap.NAME_PREFIX
    bootstrap.bootstrap_beat_schema()
    _store_rows(
        sqlite_beat_store,
        [
            PeriodicTask(
                name=f"{old_name}sync_snippets",
                task="app.tasks.celery.sync",
                total_run_count=42,
            ),
            PeriodicTask(
                name=f"{new_name}sync_snippets",
                task="app.tasks.celery.sync",
                total_run_count=1,
            ),
        ],
    )

    with pytest.raises(bootstrap.PreRenameScheduleConflictError):
        bootstrap.bootstrap_beat_schema()

    assert set(_schedules(sqlite_beat_store)) == {
        f"{old_name}sync_snippets",
        f"{new_name}sync_snippets",
    }


def test_kwargs_move_only_values_that_start_with_the_package(sqlite_beat_store: str):
    """Leave keys and strings that merely contain the package text as stored."""
    old_task, new_task = bootstrap.PRE_RENAME_TASK_PREFIX, bootstrap.TASK_PREFIX
    kwargs = {
        "syncers": [f"{old_task}sync.syncers.pmm.PMMSyncer"],
        f"{old_task}key": "https://myapp.sep.example/api",
        "note": f"see {old_task}sync",
    }
    bootstrap.bootstrap_beat_schema()
    _store_rows(
        sqlite_beat_store,
        [
            PeriodicTask(
                name="pinned sync",
                task="app.tasks.celery.execute_task_by_name",
                kwargs=json.dumps(kwargs),
            )
        ],
    )

    bootstrap.bootstrap_beat_schema()

    engine, session_factory = SessionManager().create_session(sqlite_beat_store)
    try:
        with session_factory() as session:
            stored = json.loads(session.query(PeriodicTask).one().kwargs)
        assert bootstrap.move_pre_rename_periodic_tasks(session_factory) == 0
    finally:
        engine.dispose()
    assert stored == {
        **kwargs,
        "syncers": [f"{new_task}sync.syncers.pmm.PMMSyncer"],
    }
