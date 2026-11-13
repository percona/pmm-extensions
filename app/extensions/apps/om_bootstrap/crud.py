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

"""Define reads and writes for :class:`BootstrapRun`.

A plain :class:`~app.core.db.crud.BaseSQLModelManager`, unlike ``om_inventory``'s
own :class:`~app.extensions.apps.om_inventory.crud.ProbeRunManager` sibling
(``OmHost``/``OmService``), which hand-writes attribute-by-attribute upserts for
a documented reason — freshness columns a blanket update would silently wipe.
``BootstrapRun`` has no such column: every write here replaces the whole row
(the ``hosts`` document included, per ``models.py``'s "read and written whole"
design), so the manager's generic ``save``/``update`` need no override.
"""

from uuid import UUID

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db.crud import BaseSQLModelManager
from app.core.exceptions import HTTPNotFoundException
from app.extensions.apps.om_bootstrap.models import BootstrapRun, BootstrapRunStatus

__all__ = ["BootstrapRunManager"]

#: ``list_runs``' original ordering, kept explicit rather than relying on
#: :class:`~app.core.db.crud.BaseManager`'s ``created_at``-descending default —
#: PMM's stepper cares about ``started_at`` specifically (see :meth:`list`).
_NEWEST_STARTED_FIRST = [col(BootstrapRun.started_at).desc()]


class BootstrapRunManager(BaseSQLModelManager):
    """Manage :class:`BootstrapRun` CRUD operations.

    :cvar Model: The SQLModel class this manager is responsible for.
    """

    Model = BootstrapRun

    @classmethod
    async def get_run(
        cls, session: AsyncSession, run_id: UUID, *, for_update: bool = False
    ) -> BootstrapRun:
        """Return one run, or 404.

        With ``for_update`` the row is locked (``SELECT ... FOR UPDATE``) until
        the session's transaction ends — the caller's :meth:`save` commits it,
        and closing the session without saving rolls it back. Every route that
        writes a run back takes this lock, so two concurrent requests against
        the same run serialise instead of the later commit overwriting the
        earlier one's ``hosts``/``run_steps`` document. ``populate_existing``
        makes the locked read replace any copy of the row the session already
        holds, so the caller works from what the lock actually protects. On
        SQLite the clause is a no-op (writes already serialise at the database
        level).

        :param session: The database session.
        :param run_id: The run's id.
        :param for_update: Whether to lock the row for the rest of the
            transaction.
        :raises HTTPNotFoundException: When there is no such run.
        :return: The run.
        """
        query = select(BootstrapRun).where(col(BootstrapRun.id) == run_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        run = (await cls._exec(session, query)).first()
        if run is None:
            raise HTTPNotFoundException(detail=f"Run {run_id} not found")
        return run

    @classmethod
    async def list_runs(
        cls,
        session: AsyncSession,
        *,
        status: BootstrapRunStatus | None = None,
        replica_set_name: str | None = None,
        # pagination-ok: bounded by `limit` (capped at 100 by the route) and by
        # the number of concurrently in-flight bootstrap runs.
        limit: int = 100,
    ) -> list[BootstrapRun]:
        """Return runs, newest first, optionally narrowed to one status or name.

        The intended caller is PMM's HA-leader-only stepper: on every tick, and
        especially right after a leader failover, it needs to discover every
        run still in flight by reading this API rather than from any state of
        its own — ``status=RUNNING`` is exactly that query.

        :param session: The database session.
        :param status: Restrict to runs in this status. ``None`` for any status.
        :param replica_set_name: Restrict to runs creating this replica set.
            ``None`` for any name.
        :param limit: How many to return.
        :return: The runs.
        """
        clauses = []
        if status is not None:
            clauses.append(col(BootstrapRun.status) == status)
        if replica_set_name is not None:
            clauses.append(col(BootstrapRun.replica_set_name) == replica_set_name)
        return await cls.list(
            session, *clauses, order_by=_NEWEST_STARTED_FIRST, limit=limit
        )
