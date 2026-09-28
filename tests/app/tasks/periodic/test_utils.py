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

"""Cover the beat-row task-name resolution shared by the periodic-task helpers."""

import json
import re

import pytest
from sqlalchemy_celery_beat import PeriodicTask

from app.tasks.periodic.utils import (
    generate_periodic_task_name,
    resolve_schedule_task_name,
)


def _row(args: str | None = None, kwargs: str | None = None) -> PeriodicTask:
    """Build an unpersisted beat row carrying the given raw argument columns."""
    return PeriodicTask(
        name="row",
        task="app.tasks.celery.execute_task_by_name",
        args=args,
        kwargs=kwargs,
    )


class TestResolveScheduleTaskName:
    """Cover every shape of ``args``/``kwargs`` the beat store can hold."""

    def test_kwargs_task_name_is_resolved(self) -> None:
        """Return the name ``kwargs.task_name`` carries, the form every writer uses."""
        assert (
            resolve_schedule_task_name(_row(kwargs=json.dumps({"task_name": "r1"})))
            == "r1"
        )

    def test_positional_args_are_resolved(self) -> None:
        """Return ``args[0]`` for the positional encoding the model still reads."""
        assert resolve_schedule_task_name(_row(args=json.dumps(["r1"]))) == "r1"

    def test_kwargs_override_positional_args(self) -> None:
        """Prefer ``kwargs.task_name`` when a row carries both encodings."""
        row = _row(
            args=json.dumps(["positional"]), kwargs=json.dumps({"task_name": "r1"})
        )

        assert resolve_schedule_task_name(row) == "r1"

    @pytest.mark.parametrize(
        ("args", "kwargs"),
        [
            pytest.param("not json", None, id="args-not-json"),
            pytest.param(None, "not json", id="kwargs-not-json"),
            pytest.param(json.dumps({"a": 1}), None, id="args-not-a-list"),
            pytest.param(None, json.dumps(["a"]), id="kwargs-not-a-mapping"),
        ],
    )
    def test_unreadable_arguments_resolve_to_none(
        self, args: str | None, kwargs: str | None
    ) -> None:
        """Resolve an unreadable row to ``None`` rather than raising.

        A caller iterating the whole beat store must not fail on one row, so a
        hand-edited or foreign-written row is reported as nameless instead of
        raising :exc:`json.JSONDecodeError` or :exc:`AttributeError` out of the walk.
        """
        assert resolve_schedule_task_name(_row(args=args, kwargs=kwargs)) is None

    @pytest.mark.parametrize(
        "task_name", [None, 5, "", [], {}], ids=["none", "int", "empty", "list", "dict"]
    )
    def test_a_name_that_is_not_a_non_empty_string_resolves_to_none(
        self, task_name: object
    ) -> None:
        """Resolve to ``None`` when the derived name is not a usable task name."""
        row = _row(kwargs=json.dumps({"task_name": task_name}))

        assert resolve_schedule_task_name(row) is None

    def test_a_row_carrying_no_arguments_resolves_to_none(self) -> None:
        """Resolve to ``None`` when neither column names a task."""
        assert resolve_schedule_task_name(_row()) is None


#: ``blake2b`` digest-based name generated for ``("my-task", "every 10
#: minutes", '{"task_name": null}')``, pinned so a derivation that varies per
#: process cannot pass the stability test by agreeing with itself.
_STABLE_GENERATED_NAME = "run_my-task_every_10_minutes_01263f4315fc8f0f"


class TestGeneratedPeriodicTaskName:
    """Test the auto-generated name for an unnamed periodic task."""

    def test_name_is_stable_across_processes(self):
        """Pin the generated name so two processes agree on it.

        A ``hash()``-based derivation would vary with ``PYTHONHASHSEED`` and
        give each process its own name, letting the database's uniqueness
        check silently miss the duplicate instead of raising a conflict.
        """
        name = generate_periodic_task_name(
            "my-task", "every 10 minutes", '{"task_name": null}'
        )

        assert name == _STABLE_GENERATED_NAME

    def test_varying_task_name_changes_the_name(self):
        """Give distinct tasks distinct auto-generated names."""
        name = generate_periodic_task_name(
            "other-task", "every 10 minutes", '{"task_name": null}'
        )

        assert name != _STABLE_GENERATED_NAME

    def test_varying_period_changes_the_name(self):
        """Give distinct schedules on the same task distinct names."""
        name = generate_periodic_task_name(
            "my-task", "every 20 minutes", '{"task_name": null}'
        )

        assert name != _STABLE_GENERATED_NAME

    def test_varying_kwargs_changes_the_name(self):
        """Give distinct executions of the same task distinct names."""
        name = generate_periodic_task_name(
            "my-task", "every 10 minutes", '{"task_name": "x"}'
        )

        assert name != _STABLE_GENERATED_NAME

    def test_empty_and_empty_object_kwargs_do_not_collide(self):
        """Assert an empty string and an empty JSON object digest differently."""
        empty_string_name = generate_periodic_task_name("t", "every 10 minutes", "")
        empty_object_name = generate_periodic_task_name("t", "every 10 minutes", "{}")

        assert empty_string_name != empty_object_name

    def test_unicode_kwargs_produce_a_valid_digest_suffix(self):
        """Assert non-ASCII kwargs still digest to a fixed-width hex suffix."""
        name = generate_periodic_task_name(
            "t", "every 10 minutes", '{"note": "héllo wörld 世界"}'
        )

        assert re.search(r"_[0-9a-f]{16}$", name)

    def test_space_and_underscore_task_names_do_not_collide(self):
        """Give task names differing only by a space vs. an underscore distinct names.

        The returned name's visible prefix collapses every space to an
        underscore, so ``"foo bar"`` and ``"foo_bar"`` render identically
        there; only a digest computed over the raw, un-collapsed task name
        keeps their generated names apart.
        """
        space_name = generate_periodic_task_name(
            "foo bar", "every 10 minutes", '{"task_name": null}'
        )
        underscore_name = generate_periodic_task_name(
            "foo_bar", "every 10 minutes", '{"task_name": null}'
        )

        assert space_name != underscore_name
