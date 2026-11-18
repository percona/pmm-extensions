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

"""Test Celery signal handlers for correlation ID propagation."""

from unittest.mock import MagicMock

import pytest

from app.celery import (
    clear_task_log_context,
    propagate_correlation_id,
    set_task_log_context,
)
from app.core.log import (
    correlation_id_var,
    set_log_context,
    task_id_var,
    task_name_var,
)

pytestmark = pytest.mark.usefixtures("reset_log_context")


class TestPropagateCorrelationId:
    """Test the before_task_publish signal handler."""

    def test_copies_correlation_id_to_headers(self) -> None:
        """Assert correlation_id is copied to task message headers."""
        set_log_context(correlation_id="corr-publish-123")
        headers = {}

        propagate_correlation_id(headers=headers)

        assert headers["correlation_id"] == "corr-publish-123"

    def test_skips_default_correlation_id(self) -> None:
        """Assert no header is added when correlation_id is default."""
        headers = {}

        propagate_correlation_id(headers=headers)

        assert "correlation_id" not in headers


class TestSetTaskLogContext:
    """Test the task_prerun signal handler."""

    def test_sets_context_from_task_request(self) -> None:
        """Assert correlation_id, task_id, task_name are set from task request."""
        mock_task = MagicMock()
        mock_task.name = "app.tasks.run_backup"
        mock_task.request = MagicMock()
        mock_task.request.correlation_id = "corr-task-456"

        set_task_log_context(task_id="task-id-789", task=mock_task)

        assert correlation_id_var.get() == "corr-task-456"
        assert task_id_var.get() == "task-id-789"
        assert task_name_var.get() == "app.tasks.run_backup"

    def test_defaults_correlation_id_when_missing(self) -> None:
        """Assert correlation_id defaults to ``"-"`` when not in request."""
        mock_task = MagicMock(spec=[])
        mock_task.name = "app.tasks.run_backup"
        mock_task.request = MagicMock(spec=[])

        set_task_log_context(task_id="task-id-abc", task=mock_task)

        assert correlation_id_var.get() == "-"
        assert task_id_var.get() == "task-id-abc"
        assert task_name_var.get() == "app.tasks.run_backup"


class TestClearTaskLogContext:
    """Test the task_postrun signal handler."""

    def test_clears_all_context_vars(self) -> None:
        """Assert all context vars are reset after task completes."""
        set_log_context(
            correlation_id="corr-1",
            task_id="task-1",
            task_name="my_task",
        )

        clear_task_log_context()

        assert correlation_id_var.get() == "-"
        assert task_id_var.get() == "-"
        assert task_name_var.get() == "-"
