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

"""Define exceptions for the Nomad executor."""

from nomad.api.exceptions import BaseNomadException

from app.tasks.execution.exceptions import TaskDataNotFoundInExecutorError


class NomadRequestError(BaseNomadException):
    """Define exception for a Nomad API call that failed on the aiohttp client.

    The dispatch path's calls no longer go through python-nomad, so they no
    longer raise its exceptions - but two places outside this package catch
    ``BaseNomadException`` and depend on it:
    ``app.tasks.main.nomad_exception_handler`` answers a route with 502 and
    "make sure the agent is online", and ``app.tasks.celery`` raises the
    dispatch-failure alert for a periodic task on it. An ``aiohttp.ClientError``
    escaping instead would turn the first into a bare 500 and silence the
    second, so transport failures and error statuses alike arrive here.

    :ivar status_code: The HTTP status Nomad answered with, or ``None`` when the
        request never got an answer (connection refused, timeout). Callers that
        act on a specific status read this rather than parsing the message; see
        :meth:`~app.tasks.execution.executors.nomad.models.NomadExecutor.get_job`,
        which treats only a 404 as "the job is gone".
    """

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        """Initialise the error.

        :param detail: What failed, safe to log.
        :param status_code: The answering HTTP status, if there was one.
        """
        super().__init__(detail)
        self.status_code = status_code


class AllocationNotFoundError(TaskDataNotFoundInExecutorError, BaseNomadException):
    """Define exception for when an allocation is not found."""


class JobNotFoundError(TaskDataNotFoundInExecutorError, BaseNomadException):
    """Define exception for when a job is not found."""
