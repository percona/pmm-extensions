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

"""Guard the seed ↔ include ↔ registration invariant for PMM Extensions Celery tasks.

The relocation of the app-owned Celery tasks split one hazard three
ways: a task's registered name is its module path, the beat seed hard-codes that
path as ``task_name``, and two ``include`` lists drive which modules a worker
imports. If any of those drifts apart, a beat row points at a task no worker
registers — and every task-logic test stays green. These tests pin the three
together so a future move cannot silently break dispatch.
"""

import importlib

from app.celery import celery
from app.core.celery.config import STATIC_CELERY_INCLUDE
from app.core.config import settings
from app.extensions.apps.framework.registry import (
    app_celery_module_paths,
    build_celery_include,
)
from app.extensions.config import App, extensions_settings


def _seed_task_names() -> set[str]:
    """Return every ``task_name`` the PMM Extensions beat seed schedules."""
    from app.extensions.db.seed import get_system_periodic_tasks  # noqa: PLC0415

    return {
        task.task_name
        for schedule in get_system_periodic_tasks()
        for task in schedule.tasks
    }


class TestCeleryInclude:
    """Cover the two ``include`` lists that must move in lockstep."""

    def test_worker_include_matches_configured_include(self, mocker) -> None:
        """Assert ``start_celery_worker`` registers the configured module set.

        The worker's ``include`` list (``app/main.py``) and the Celery app's
        configured ``include`` (``settings.CELERY.include``) both compose
        ``build_celery_include()``; this pins them equal so beat cannot schedule
        against a module the worker never imports.
        """
        import app.main  # noqa: PLC0415

        worker_cls = mocker.patch.object(app.main.celery_app, "Worker")

        app.main.start_celery_worker()

        _, kwargs = worker_cls.call_args
        assert kwargs["include"] == settings.CELERY.include
        worker_cls.return_value.start.assert_called_once()

    def test_configured_include_is_the_derived_single_source(self) -> None:
        """Assert the configured include equals the registry-derived composition.

        Lockstep is now structural: both the Celery-app assembly and the worker
        bootstrap compose ``build_celery_include()``, so the configured value must
        equal a fresh derivation rather than a hand-maintained literal.
        """
        assert settings.CELERY.include == build_celery_include()


class TestSeedTaskRegistration:
    """Cover the seed ``task_name`` ↔ actual-registration invariant."""

    def test_seed_task_names_are_registered(self) -> None:
        """Assert every seeded ``task_name`` resolves to a registered Celery task.

        Importing the configured ``include`` modules is what a worker does at
        startup; the ``@owned_by`` app tasks and the statically-included library
        and drain modules register as a side effect. Any seeded ``task_name``
        absent afterwards is a beat row pointing at nothing.
        """
        for module in settings.CELERY.include:
            importlib.import_module(module)

        missing = _seed_task_names() - set(celery.tasks)
        assert not missing, f"seeded task_name(s) not registered: {sorted(missing)}"

    def test_relocated_tasks_register_under_new_names(self) -> None:
        """Assert the relocated tasks register under their current module paths."""
        importlib.import_module("app.extensions.snippets.celery")
        importlib.import_module("app.extensions.apps.alerts.celery")

        assert "app.extensions.snippets.celery.sync_snippets" in celery.tasks
        assert "app.extensions.apps.alerts.celery.backup_alert_config" in celery.tasks

    def test_snippet_sync_registers_without_the_snippets_app(self, mocker) -> None:
        """Assert snippet ingestion registers with the snippets app deactivated.

        The task moved into the library and is named in ``STATIC_CELERY_INCLUDE``,
        so an image that never mounts the snippets UI still runs the sync its beat
        row schedules unconditionally.
        """
        mocker.patch.object(
            extensions_settings, "APPS", [App(module_name="inventory", enabled=True)]
        )

        include = build_celery_include()
        assert "app.extensions.snippets.celery" in include
        for module in include:
            importlib.import_module(module)

        assert "app.extensions.snippets.celery.sync_snippets" in celery.tasks

    def test_drain_reconciler_registers_without_any_celery_bearing_app(
        self, mocker
    ) -> None:
        """Assert the drain reconciler registers from the static include alone.

        ``extensions__reconcile_disabling_apps`` is seeded unconditionally, so its module
        can no longer rely on a transitive import from an app-owned Celery module
        that a stripped image may not ship.
        """
        mocker.patch.object(
            extensions_settings, "APPS", [App(module_name="inventory", enabled=True)]
        )

        include = build_celery_include()
        assert not app_celery_module_paths()
        for module in include:
            importlib.import_module(module)

        assert "app.extensions.app_drain.reconcile_disabling_apps" in celery.tasks

    def test_no_task_registered_under_retired_shared_module(self) -> None:
        """Assert nothing still registers under the deleted ``app.extensions.celery``."""
        stale = [
            name for name in celery.tasks if name.startswith("app.extensions.celery.")
        ]
        assert not stale, f"tasks still under retired module: {stale}"

    def test_app_owned_seed_prefixes_track_registry_modules(self) -> None:
        """Assert every app-owned seed ``task_name`` prefix is registry-derived.

        A module rename now moves the seed prefix and the include entry together
        (both read ``App.celery_module_path``), so a stale hardcoded seed string
        would surface here rather than as a silent dead beat row. Tasks whose
        module is in ``STATIC_CELERY_INCLUDE`` are not ``EXTENSIONS.APPS`` apps and stay
        literals, so they are excluded.
        """
        app_modules = set(app_celery_module_paths())
        for name in _seed_task_names():
            if name.rsplit(".", 1)[0] in STATIC_CELERY_INCLUDE:
                continue
            prefix = name.rsplit(".", 1)[0]
            assert prefix in app_modules, (
                f"seed task_name {name!r} prefix {prefix!r} is not a "
                f"registry-derived app module {sorted(app_modules)}"
            )
