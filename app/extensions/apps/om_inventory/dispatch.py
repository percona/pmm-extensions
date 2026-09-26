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

"""Dispatch probe payloads to executor hosts and collect their NDJSON back.

Rides the pre-seeded system ``run-python`` task rather than creating one per app:
``POST /execute/run-python`` with ``meta.target`` naming the executor host,
``meta.config`` carrying the payload's JSON config, and ``payload`` a ``file://``
URI. This is how the ``topology`` app dispatches its collector shards.

Results come back over **stdout**, streamed from the task-log chunk store the way
:meth:`~app.extensions.sync.models.BaseTaskSyncer.wait_for_task_output` does it. That
channel has no total size cap, unlike the 16 KB ``.pmm-extensions-run-result.json`` file.

One dispatch per executor host, carrying every service that host serves — the
payload collects host-level facts once and reuses them across its targets, so
batching by host is both fewer Nomad jobs and less duplicated work.
"""

import asyncio
import json
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

from app.core.exceptions import HTTPConflictException, HTTPServiceUnavailableException
from app.core.requests import RemoteAPI
from app.extensions.apps.framework.spec import RUN_PYTHON_TASK
from app.extensions.apps.om_inventory import payload as payload_pkg
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.mapping import MappedService
from app.tasks.models import TaskHistoryStatusEnum, TaskLogType

logger = logging.getLogger(__name__)

#: The Nomad task inside the ``run-python`` job whose stdout carries the payload's
#: output. The job also runs prestart steps whose logs are filtered out by this.
STDOUT_STEP = "run-script"
#: The payload's only third-party import, installed into the executor's venv by the
#: run-python job template's prestart step.
REQUIREMENTS = "pymongo>=4.6,<5"
#: Prefix for this app's Nomad job ids, so a OM run is identifiable in Nomad.
JOB_ID_PREFIX = "om"
PROBE_PAYLOAD_PATH = Path(payload_pkg.__file__).parent / "probe.py"

# Bounds with_capacity_retry both ways, whichever runs out first.
#
# The wall-clock budget is the one that has to be sized deliberately: what a retry
# has to outlast here is the dispatch lock's stale-row sweep in
# `_dispatch_queue_item` (app/tasks/celery.py), which only clears a lock older
# than 30 seconds, so a shorter budget cannot clear an orphaned one at all. An
# attempt count on its own cannot express that, because attempt cost varies by
# four orders of magnitude: an attempt refused by an existing lock row returns a
# 409 in milliseconds, while one that ends in the pool's own connection timeout
# costs a full 10 seconds. The same three attempts therefore span anywhere from
# ~3s to ~33s -- the old budget of three attempts and 1+2s of backoff could not
# reach 30 seconds by any path.
CAPACITY_RETRY_BUDGET_SECONDS = 40.0
# The attempt cap is the secondary bound, and stops a refusal that returns
# instantly from spinning through the budget.
CAPACITY_RETRY_ATTEMPTS = 8
_CAPACITY_RETRY_BASE_DELAY_SECONDS = 1.0
# Caps the doubling, so the tail of the budget is spent on several late attempts
# rather than on one long sleep that ends just as the budget does.
_CAPACITY_RETRY_MAX_DELAY_SECONDS = 8.0

#: Substring `_dispatch_queue_item` (app/tasks/celery.py) puts in a 409's detail
#: when its own dispatch-lock row collided. Matched rather than the bare status
#: code so a 409 for an unrelated reason (e.g. "Queue item is not in a pending
#: state") still fails immediately instead of being retried into a longer hang.
_DISPATCH_LOCK_CONFLICT_MARKER = "DispatchLock"

#: Matches the 409 `_guard_identical_queue_item` (app/tasks/celery.py) raises when
#: an active queue item already carries this exact request, capturing that item's
#: id. Parsed out of the message because the id is not returned as a field; see
#: `_adopted_queue_item_id` for why it is worth parsing anyway.
_IDENTICAL_QUEUE_ITEM_RE = re.compile(r"Identical queue item already running \((\d+)\)")

T = TypeVar("T")


def _is_pool_capacity_error(err: Exception) -> bool:
    """Return whether ``err`` is the Tasks API's own connection-pool refusal.

    :param err: The exception a retried call raised.
    :return: Whether it is worth retrying.
    """
    return isinstance(err, HTTPServiceUnavailableException)


def _is_dispatch_lock_race(err: Exception) -> bool:
    """Return whether ``err`` is a same-content dispatch racing its own lock row.

    ``_dispatch_queue_item`` computes its lock name as a hash of exactly
    ``{task_id, task, target, payload, meta}``, so a collision means a request
    with byte-identical content -- this same host, dispatched again -- is
    contending with this one, not that a real conflict is being hidden. Waiting
    and asking again is the correct response either way.

    What the wait has to cover is not obvious, and is why
    :data:`CAPACITY_RETRY_BUDGET_SECONDS` is sized the way it is. The lock is
    deleted in a ``finally``, but that ``finally`` guards only the dispatch
    itself: the lock row is committed one frame earlier, inside
    ``DispatchLockManager.create``, and ``CRUDBase.save`` then issues a
    post-commit ``session.refresh`` needing a *second* checkout from the same
    pool. When the pool is exhausted that refresh raises, the row is already
    committed, and the ``try`` that would have released it was never entered.
    The lock is then orphaned, and nothing clears it until the caller's own
    30-second stale-row sweep -- so the collision a retry meets here can just as
    easily be this probe's own failed first attempt as another dispatch, and it
    can outlive that attempt by the full 30 seconds.

    :param err: The exception a retried call raised.
    :return: Whether it is worth retrying.
    """
    return isinstance(
        err, HTTPConflictException
    ) and _DISPATCH_LOCK_CONFLICT_MARKER in str(err.detail)


def _adopted_queue_item_id(err: Exception) -> int | None:
    """Return the id of an in-flight queue item this dispatch should adopt.

    ``_guard_identical_queue_item`` refuses a request an active queue item
    already carries, naming that item. It is not a failure of this host's probe:
    it says the exact probe being asked for is already running, so the answer is
    on its way and this call need only wait for it.

    Adopting is sound because of what the guard compares before raising -- same
    ``task``, same ``target``, the same meta clauses, and
    ``_encrypted_leaves_match`` over the payload -- so the item it names produces
    the byte-identical result this dispatch would have produced itself.

    Worth handling rather than retrying: a retry cannot make the conflict go
    away, since the item stays active for as long as the probe takes, so the
    attempts are spent waiting for a result already being computed and the host
    is recorded failed anyway. This is reachable in ordinary operation, not only
    under a race: ``with_capacity_retry`` retries the pool's 503, that retry is
    not idempotent -- the enqueue can land while the client sees the refusal --
    and the next attempt then collides with its own earlier item. Observed
    2026-09-24: item 130 probed ``pmm-client-node02`` successfully in 5s while
    the run that asked for it recorded the host unanswered and came back
    PARTIAL.

    :param err: The exception the dispatch call raised.
    :return: The queue item id to adopt, or None if this is not that conflict.
    """
    if not isinstance(err, HTTPConflictException):
        return None
    match = _IDENTICAL_QUEUE_ITEM_RE.search(str(err.detail))
    return int(match.group(1)) if match else None


async def with_capacity_retry(call: Callable[[], Awaitable[T]]) -> T:
    """Retry ``call`` a bounded number of times on a known-transient refusal.

    A sweep dispatches up to ``MAX_CONCURRENT_PROBES`` hosts at once, each making
    its own requests to the Tasks API — exactly the burst that can transiently
    exhaust that process's own database connection pool (sized at 5 connections,
    surfaced as a 503) or race two byte-identical dispatches against
    the same dispatch-lock row (surfaced as a 409, see
    :func:`_is_dispatch_lock_race`). Either is a queueing accident, not a fact
    about this host's probe, so neither belongs counted alongside a real dispatch
    or collection failure.

    Left uncapped this would be indistinguishable from a hang: retrying forever
    on a pool that stays saturated blocks the semaphore slot this host holds,
    which is exactly what starves the *other* queued hosts of a turn. So it is
    bounded two ways and stops at whichever runs out first — a wall-clock budget
    sized to outlast the dispatch lock's own stale-row sweep (see
    :data:`CAPACITY_RETRY_BUDGET_SECONDS`), and an attempt cap for a refusal that
    returns too fast for that budget to bound anything on its own.

    The budget is checked against the *next* sleep rather than after it, so this
    never sleeps past the deadline only to give up on waking.

    :param call: Zero-argument async callable to retry.
    :return: Whatever ``call`` returns, once it stops raising.
    :raises Exception: Whatever ``call`` last raised, once either bound is spent,
        or immediately for any exception neither predicate recognises.
    """
    deadline = monotonic() + CAPACITY_RETRY_BUDGET_SECONDS
    for attempt in range(CAPACITY_RETRY_ATTEMPTS):
        try:
            return await call()
        except Exception as err:
            transient = _is_pool_capacity_error(err) or _is_dispatch_lock_race(err)
            if not transient or attempt == CAPACITY_RETRY_ATTEMPTS - 1:
                raise
            delay = min(
                _CAPACITY_RETRY_BASE_DELAY_SECONDS * 2**attempt,
                _CAPACITY_RETRY_MAX_DELAY_SECONDS,
            )
            if monotonic() + delay >= deadline:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable: the loop above always returns or raises")


@dataclass
class HostProbeResult:
    """Carry the outcome of one executor host's probe dispatch.

    :param executor_host: The host the payload ran on.
    :param task_history_id: The dispatched run's history id, when dispatch succeeded.
    :param records: The parsed NDJSON records, keyed by service name.
    :param host_record: The host's own record — OS, kernel, the installed binary —
        collected once per dispatch. ``None`` when the payload never printed it,
        which is the only way to tell "the host did not answer" from "the host
        answered and has no database on it".
    :param duration_seconds: Wall-clock from dispatch to collected output, including
        the wait for Nomad to schedule the job. Measured here rather than read back
        from the task history because this is the number that explains a slow sweep:
        a host queued behind a busy client costs the sweep just as much as a slow
        payload, and the history's own timestamps would hide that wait.
    :param error: The dispatch or collection failure, when the whole host failed.
    """

    executor_host: str
    task_history_id: int | None = None
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    host_record: dict[str, Any] | None = None
    duration_seconds: float | None = None
    error: str | None = None


def group_by_executor(mapped: list[MappedService]) -> dict[str, list[MappedService]]:
    """Group resolved services by the executor host that serves them.

    :param mapped: Every mapped service, resolved or orphaned.
    :return: Resolved services keyed by executor host, in inventory order.
    """
    grouped: dict[str, list[MappedService]] = defaultdict(list)
    for entry in mapped:
        if entry.executor_host is not None:
            grouped[entry.executor_host].append(entry)
    return dict(grouped)


def record_key(service_id: str | None, service_name: str | None) -> str:
    """Return the key a target's parsed NDJSON record is stored and looked up under.

    PMM's service id, when the target carries one — the only thing two identically
    named services dispatched to the same host do not share. Keying on the name
    alone, which is what ``build_config`` used to send as a target's whole identity,
    let two same-named services on one host overwrite each other's record: the
    service id never reached the node, so nothing distinguished them once their
    NDJSON lines came back.

    Falls back to the name for the rare target inventory carries with no PMM id —
    the same key this dict used exclusively before ids were echoed back at all, so
    that case is no worse off than before. It is also harmless: an entity with no
    PMM id is never written to the estate (see ``_record_entity``), so a collision
    among such targets loses nothing that would have been kept anyway.

    :param service_id: The target's ``service_id``, or ``None`` when inventory
        carries none.
    :param service_name: The target's ``service`` name.
    :return: The key.
    """
    return service_id or service_name or ""


def build_config(entries: list[MappedService]) -> str:
    """Build the payload config for one executor host's targets.

    :param entries: The resolved services this host serves.
    :return: The JSON config placed in ``meta.config``.
    """
    return json.dumps(
        {
            "targets": [
                {
                    "service": entry.service.name,
                    # PMM's service id, echoed back by the payload on this target's
                    # record and what parse_ndjson / _record_for key on — see
                    # record_key. The name alone is not unique per node.
                    "service_id": entry.service.external_id,
                    # The node name is preferred over the address: in a sidecar
                    # deployment pmm-agent registers the node with its own container
                    # address while naming it after the database container, so the
                    # address reaches the agent where nothing listens on the DB port.
                    "host": entry.service.node_name or entry.service.node_address,
                    "port": entry.service.port,
                }
                for entry in entries
            ],
            "probe_database": om_inventory_settings.PROBE_DATABASE,
            "credentials_path": om_inventory_settings.CREDENTIALS_PATH,
            "connect_timeout_ms": om_inventory_settings.CONNECT_TIMEOUT * 1000,
            "repo_url": om_inventory_settings.REPO_URL,
            "repo_timeout": om_inventory_settings.REPO_TIMEOUT,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_ndjson(
    stdout: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Parse the payload's stdout into service records and the host's own record.

    The payload emits one line for the host and one per service. They are told apart
    by ``service``: the host's is null, and a service record always carries a name.
    That is the whole discriminator, and it is why the host line cannot be confused
    for a service even on a host running a service called nothing useful.

    Non-JSON lines are skipped rather than failing the host: the run-python job
    template's prestart step and pip itself write to the same stream, so the payload's
    records are interleaved with output it does not control.

    :param stdout: The dispatched run's captured stdout.
    :return: One record per service that reported, keyed by :func:`record_key`, and
        the host record if it came.
    """
    records: dict[str, dict[str, Any]] = {}
    host_record: dict[str, Any] | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("service"):
            records[record_key(record.get("service_id"), record.get("service"))] = (
                record
            )
        elif "service" in record:
            host_record = record
    return records, host_record


async def _wait_for_terminal(tasks_api: RemoteAPI, task_history_id: int) -> str:
    """Poll a dispatched run until it reaches a terminal status or times out.

    :param tasks_api: The tasks API client.
    :param task_history_id: The run to poll.
    :return: The terminal status value.
    :raises TimeoutError: When the run is still pending or running at the deadline.
    """
    waited = 0
    status = TaskHistoryStatusEnum.PENDING.value
    while waited < om_inventory_settings.TASK_TIMEOUT:
        await asyncio.sleep(om_inventory_settings.POLL_INTERVAL)
        waited += om_inventory_settings.POLL_INTERVAL
        history = await with_capacity_retry(
            lambda: tasks_api.get(f"/history/{task_history_id}")
        )
        if not isinstance(history, dict):
            continue
        status = history["status"]
        if status not in (
            TaskHistoryStatusEnum.PENDING.value,
            TaskHistoryStatusEnum.RUNNING.value,
        ):
            return status
    raise TimeoutError(
        f"probe task history {task_history_id} still {status} after "
        f"{om_inventory_settings.TASK_TIMEOUT}s"
    )


async def _release(tasks_api: RemoteAPI, task_history_id: int) -> str | None:
    """Drive an abandoned dispatch to a terminal status.

    Giving up on a probe ends *this sweep's* wait; it does nothing to the queue item,
    which stays ``RUNNING`` with nothing left to advance it. That matters more than it
    sounds: the tasks API refuses to dispatch a queue item identical to one already in
    flight, and every sweep dispatches the same ``run-python`` to the same host with
    the same config — so one abandoned run makes that host answer ``409`` forever, and
    its facts quietly stop refreshing while the sweep still reports itself partial.
    Measured on the sandbox: seven such rows blocked their hosts for over an hour, and
    the sweeps in between looked like unreachable nodes rather than a queue that needed
    clearing.

    Best-effort by design. The stop can legitimately fail — most sharply when the
    allocation is already gone, which is the case most likely to have caused the
    abandonment in the first place — and a sweep must not fail because its cleanup
    did. The reason comes back so the caller can say the dispatch was left in flight,
    because a queue item that could not be released is the one thing here that needs a
    human.

    :param tasks_api: The tasks API client.
    :param task_history_id: The run to release.
    :return: The failure detail, or ``None`` when there was nothing to release or the
        run was released.
    """
    try:
        history = await tasks_api.get(f"/history/{task_history_id}")
        # Not every failure after a dispatch leaves the queue item in flight — losing
        # the log stream of a run that already finished is a failure of collection,
        # not of the run. Stopping a terminal item answers 400, and reporting that as
        # "could not be released" would raise an alarm about a queue that is clean.
        if isinstance(history, dict) and history["status"] not in (
            TaskHistoryStatusEnum.PENDING.value,
            TaskHistoryStatusEnum.RUNNING.value,
        ):
            return None
    except Exception:
        # Unreadable status is not a reason to skip the stop: the whole point is to
        # not leave a queue item behind, and an unnecessary stop is harmless.
        logger.exception(
            "OM inventory: could not read probe task history %s before releasing it",
            task_history_id,
        )

    try:
        await tasks_api.post(f"/history/{task_history_id}/stop/")
    except Exception as err:
        logger.exception(
            "OM inventory: could not release probe task history %s", task_history_id
        )
        return f"{type(err).__name__}: {err}"
    logger.info(
        "OM inventory: released abandoned probe task history %s", task_history_id
    )
    return None


async def _read_stdout(tasks_api: RemoteAPI, task_history_id: int) -> tuple[str, str]:
    """Stream a finished run's logs and return its ``(stdout, stderr)``.

    :param tasks_api: The tasks API client.
    :param task_history_id: The run to read.
    :return: The concatenated stdout and stderr.
    """
    streams: dict[str, str] = defaultdict(str)
    async for entry in tasks_api.stream(
        f"/history/{task_history_id}/logs/", params={"step": STDOUT_STEP}
    ):
        if not entry:
            continue
        try:
            log = json.loads(entry)
        except ValueError:
            continue
        streams[log.get("type")] += log.get("msg") or ""
    return streams[TaskLogType.STDOUT], streams[TaskLogType.STDERR]


async def probe_host(
    tasks_api: RemoteAPI, executor_host: str, entries: list[MappedService]
) -> HostProbeResult:
    """Dispatch the probe to one executor host and collect its records.

    A failure here is scoped to this host: it is recorded on the result and the
    other hosts' probes are unaffected.

    :param tasks_api: The tasks API client.
    :param executor_host: The host to run on.
    :param entries: The resolved services this host serves.
    :return: The host's probe outcome.
    """
    result = HostProbeResult(executor_host=executor_host)
    # Monotonic: a wall clock stepped by NTP mid-sweep would report a negative
    # duration, and this number is only ever read as an interval.
    started = monotonic()
    try:
        try:
            created = await with_capacity_retry(
                lambda: tasks_api.post(
                    f"/execute/{RUN_PYTHON_TASK}",
                    json={
                        "meta": {
                            "target": executor_host,
                            "config": build_config(entries),
                            "requirements": REQUIREMENTS,
                            "_job_id_prefix": JOB_ID_PREFIX,
                        },
                        "payload": f"file://{PROBE_PAYLOAD_PATH}",
                        "anonymize_mask": 0,
                    },
                )
            )
        except Exception as err:
            adopted = _adopted_queue_item_id(err)
            if adopted is None:
                raise
            logger.info(
                "OM inventory: %s already has an identical probe in flight, "
                "adopting history %s instead of dispatching a second one",
                executor_host,
                adopted,
            )
            created = {"id": adopted}
        if not isinstance(created, dict) or "id" not in created:
            result.error = "Tasks API did not return a task history id"
            return result
        result.task_history_id = int(created["id"])
        logger.info(
            "OM inventory: dispatched probe to %s for %d service(s), history %s",
            executor_host,
            len(entries),
            result.task_history_id,
        )

        history_id = result.task_history_id
        status = await _wait_for_terminal(tasks_api, history_id)
        stdout, stderr = await with_capacity_retry(
            lambda: _read_stdout(tasks_api, history_id)
        )
        result.records, result.host_record = parse_ndjson(stdout)

        # A FAILED status with parsed records still yields those records: the payload
        # exits non-zero only when it could not start, but the job template's own
        # steps can fail after the payload printed. Report the status, keep the data.
        #
        # The host record counts as data: on a host with no database it is the *only*
        # thing the dispatch had to produce, so treating "no service records" as
        # failure would fail every empty host by construction.
        if (
            status != TaskHistoryStatusEnum.SUCCESS.value
            and not result.records
            and result.host_record is None
        ):
            result.error = f"probe run {status}: {stderr.strip()[:500] or 'no output'}"
    except Exception as err:
        logger.exception("OM inventory: probe of %s failed", executor_host)
        result.error = f"{type(err).__name__}: {err}"
        # Only a dispatch that reached the queue can be left in it. Anything that
        # failed before an id came back never became a queue item, and there is
        # nothing to release.
        if result.task_history_id is not None:
            release_error = await _release(tasks_api, result.task_history_id)
            if release_error:
                result.error = (
                    f"{result.error} -- and task history "
                    f"{result.task_history_id} could not be released, so it will "
                    f"block this host's next probe: {release_error}"
                )
    finally:
        # In a finally so a host that failed or returned early is still timed: how
        # long a broken host took before giving up is as diagnostic as how long a
        # working one took. Mutating the object the early return already yielded is
        # visible to the caller — the return value is this reference.
        result.duration_seconds = monotonic() - started
    return result


async def probe_all(
    tasks_api: RemoteAPI,
    mapped: list[MappedService],
    executor_hosts: Iterable[str] = (),
) -> dict[str, HostProbeResult]:
    """Probe every executor host in reach, concurrently.

    Hosts come from two places and the second is the point: the executor hosts of
    resolved services, *and* every executor host the estate knows about. A machine
    with a PMM client and no database has no service to be reached through, and it is
    exactly the machine an install decision is about — so dispatching only to hosts
    that serve a service would leave the interesting ones permanently undescribed.

    Concurrency is bounded by ``max_concurrent_probes``: each dispatch is a Nomad
    job, and a large estate would otherwise submit hundreds at once.

    :param tasks_api: The tasks API client.
    :param mapped: Every mapped service, resolved or orphaned.
    :param executor_hosts: Executor hosts to probe even if they serve no service.
    :return: One result per executor host, keyed by host.
    """
    grouped = group_by_executor(mapped)
    # ``setdefault`` rather than a union: a host that serves services must keep them
    # as its targets, and one that serves none is dispatched to with an empty list.
    for host in executor_hosts:
        grouped.setdefault(host, [])

    if not grouped:
        logger.warning("OM inventory: no executor host to probe")
        return {}

    semaphore = asyncio.Semaphore(om_inventory_settings.MAX_CONCURRENT_PROBES)

    async def guarded(host: str, entries: list[MappedService]) -> HostProbeResult:
        async with semaphore:
            return await probe_host(tasks_api, host, entries)

    results = await asyncio.gather(
        *(guarded(host, entries) for host, entries in grouped.items())
    )
    return {result.executor_host: result for result in results}
