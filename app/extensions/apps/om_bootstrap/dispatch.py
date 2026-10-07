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

"""Dispatch one bootstrap step's action to a host, as root, via Nomad.

Rides the pre-seeded system ``exec-artifact`` task rather than ``run-python``
(``om_inventory``'s own choice, ``om_inventory/dispatch.py``): ``run-python``'s
job template has no ``sudo`` branch at all, so a step needing root (installing a
package, writing ``/etc/mongod.conf``, managing a systemd unit — everything
:class:`~app.extensions.apps.om_bootstrap.strategies.packages.PackagesInstallStrategy`
does) would only work by accident, if the Nomad client agent itself happens to
run as root. ``exec-artifact`` has an explicit sudo path: its job template keys
off the ``interpreter`` meta literally starting with ``"sudo "``
(``app/tasks/db/seed.py``) and runs ``env -S "<interpreter>" <task dir>/script``,
so the script is invoked by its absolute path as ``sudo bash <path>``.

``exec-artifact`` downloads its script as a Nomad artifact rather than a
dispatch payload (unlike ``run-python``), which means the script has to exist as
a real file at a real URL *before* dispatch. Every existing consumer
(``dipper``, ``snippets``) serves a fixed, developer-authored file for this —
none of them write one on the fly, because none of them need to: their script
content never changes. A bootstrap step's command is different per run, host,
and step, so this module is the first to actually generate the file it serves,
which is the reason it needs a scratch directory (:func:`step_scripts_dir`) at
all. A script can carry a per-dispatch secret (a keyFile, a monitoring-user
password), so the directory is ``0700`` and every script ``0600``, and a script
is removed as soon as nothing will download it again: when its dispatch fails,
when reconciliation sees it finish, and when the run itself is finished
(:func:`cleanup_run_scripts`).

Deliberately not modeled as a :class:`~app.extensions.snippets.models.snippet.BaseSnippet`:
that abstraction exists for catalogued, user-parameterized scripts with a
validated execution model, and a bootstrap step is neither — its content is
fixed once :class:`~app.extensions.apps.om_bootstrap.strategy.StepAction` is built, with
nothing left for a user to supply. Reusing
:class:`~app.extensions.snippets.models.snippet.SnippetExecutionMeta` directly (the data
envelope ``exec-artifact`` actually reads) gets the same wire format without the
unneeded layer above it.

Dispatch here is fire-and-forget, matching the shape
``om_inventory/bootstrap.py``'s own PoC proved works for this kind of long-running
work: this returns as soon as the Tasks API accepts the dispatch, carrying no
opinion about whether the step has started, let alone finished. Polling
``TaskHistory`` to completion and writing the result back onto
:class:`~app.extensions.apps.om_bootstrap.strategy.StepRecord` is
:mod:`~app.extensions.apps.om_bootstrap.reconcile`'s job, not this module's.
"""

import asyncio
import hashlib
import os
import shlex
from pathlib import Path
from tempfile import gettempdir

from fastapi import Request

from app.core.requests import RemoteAPI
from app.extensions.apps.framework.script_helpers import (
    build_artifact_download_url,
    post_task_execution,
)
from app.extensions.apps.om_bootstrap.strategy import StepAction
from app.extensions.snippets.models.snippet import SnippetExecutionMeta

__all__ = [
    "ARTIFACT_TYPE",
    "EXEC_ARTIFACT_TASK",
    "ROOT_INTERPRETER",
    "cleanup_run_scripts",
    "cleanup_step_script",
    "dispatch_step",
    "step_scripts_dir",
]

#: The pre-seeded system task that runs an artifact-downloaded script as root.
EXEC_ARTIFACT_TASK = "exec-artifact"
#: ``exec-artifact``'s job template keys its sudo branch on the interpreter meta
#: literally starting with ``"sudo "`` (``app/tasks/db/seed.py``) — nothing else
#: triggers it.
ROOT_INTERPRETER = "sudo bash"
#: The ``artifact_base_dirs`` discriminator ``om_bootstrap`` registers
#: :func:`step_scripts_dir` under — see ``app.py``.
ARTIFACT_TYPE = "om_bootstrap_step"
#: Set by a step script on its first pass, before it re-executes itself under
#: ``timeout`` — see :func:`build_step_script`.
TIMEOUT_GUARD_VAR = "OM_BOOTSTRAP_STEP_TIMED"
#: Grace period between ``timeout``'s SIGTERM and its SIGKILL, in seconds.
TIMEOUT_KILL_AFTER_S = 10


def step_scripts_dir() -> Path:
    """Return the scratch directory step scripts are written to, creating it if needed.

    A fixed path under the system temp directory, not a per-call
    :func:`tempfile.mkdtemp`: :func:`~app.extensions.apps.framework.base.BaseApp`
    declares this directory once, at import time, as the thunk
    ``artifact_base_dirs`` calls on every download — it has to resolve to the
    same directory every time, not a fresh one per call.

    The directory is ``0700`` — re-applied on every call, so a directory left
    behind by an older build with looser permissions is tightened too — since
    the scripts in it can carry a per-dispatch secret.

    :return: The scratch directory, created if it did not already exist.
    """
    directory = Path(gettempdir()) / "extensions-om-bootstrap-steps"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def step_script_filename(run_id: str, host: str, step_name: str) -> str:
    """Name the script file for one run/host/step triple.

    Deterministic and reused on retry: a failed step retried under the
    stepper's retry-then-rollback policy dispatches again under the exact same
    name, so a retry simply overwrites the previous attempt's script rather
    than accumulating one file per attempt.

    :param run_id: The bootstrap run this step belongs to.
    :param host: The node name being bootstrapped.
    :param step_name: The step's name, from
        :meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.plan_steps`.
    :return: The script's filename, unique within :func:`step_scripts_dir`.
    """
    return f"{run_id}_{host}_{step_name}.sh"


def build_step_script(action: StepAction) -> str:
    """Render a step's action as a standalone POSIX shell script.

    An ``["sh", "-c", body]`` action is written with ``body`` as the script's
    own body rather than as a nested ``sh -c`` line: a nested shell would carry
    the whole body — and any secret embedded in it — in its argv, readable by
    any local user through ``ps`` for as long as the step runs. Any other argv
    (e.g. ``["systemctl", "enable", "--now", "mongod"]``) is ``shlex.join``-ed
    into one properly quoted line.

    The script enforces ``action.timeout_s`` itself: on its first pass it
    re-executes itself under coreutils ``timeout``, marking the re-execution
    with :data:`TIMEOUT_GUARD_VAR` so the second pass runs the body instead of
    wrapping again. ``$0`` is the script's own path because ``exec-artifact``
    invokes it by path (see the module docstring). A step that overruns exits
    124 (137 if it also ignores SIGTERM for :data:`TIMEOUT_KILL_AFTER_S`
    seconds), which the executor reports as a failed dispatch - with exit code
    123, like every other failing step: ``exec-artifact`` launches the script
    through GNU ``xargs``, which exits 123 for any command status from 1 to 125.
    The step's own code does not reach the tasks service, so a step has to say
    why it failed on stderr; see
    :mod:`~app.extensions.apps.om_bootstrap.reconcile`.

    :param action: The step's action.
    :return: The script's full text, including the shebang.
    """
    match action.command:
        case ["sh", "-c", str(shell_body)]:
            body = shell_body
        case command:
            body = shlex.join(command)
    if not body.endswith("\n"):
        body += "\n"
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f'if [ -z "${{{TIMEOUT_GUARD_VAR}:-}}" ]; then\n'
        f"  export {TIMEOUT_GUARD_VAR}=1\n"
        f"  exec timeout --kill-after={TIMEOUT_KILL_AFTER_S} {action.timeout_s} "
        'sh "$0"\n'
        "fi\n"
        f"{body}"
    )


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path``, readable and writable by this process's user only.

    ``os.open`` with mode ``0600`` rather than ``Path.write_text``, so a new
    file is never briefly world-readable under the process umask, and
    ``fchmod`` so a file left by an earlier attempt of the same step (see
    :func:`step_script_filename`) is tightened as well.

    :param path: The file to write.
    :param data: Its full content.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
    finally:
        if fd != -1:
            os.close(fd)


async def write_step_script(
    run_id: str, host: str, step_name: str, action: StepAction
) -> tuple[Path, str]:
    """Write a step's script into the scratch directory.

    The write itself runs off the event loop (:func:`asyncio.to_thread`): this
    is awaited directly from every ``:dispatch`` route handler, and a blocking
    disk write here would stall every other request that worker is serving for
    its duration.

    :param run_id: The bootstrap run this step belongs to.
    :param host: The node name being bootstrapped.
    :param step_name: The step's name.
    :param action: The step's action.
    :return: The written file's path, and its MD5 digest —
        :class:`~app.extensions.snippets.models.snippet.SnippetExecutionMeta` requires
        the digest to verify the download on the executor side.
    """
    data = build_step_script(action).encode("utf-8")
    path = step_scripts_dir() / step_script_filename(run_id, host, step_name)
    await asyncio.to_thread(_write_private, path, data)
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    return path, digest


def cleanup_step_script(run_id: str, host: str, step_name: str) -> None:
    """Remove a step's script once its dispatch has reached a terminal status.

    Best-effort by design, matching
    :func:`~app.extensions.apps.om_inventory.dispatch._release`'s own reasoning: a
    script that fails to delete is a few stray bytes in a scratch directory, not
    a run that needs to fail because its cleanup did.

    :param run_id: The bootstrap run this step belongs to.
    :param host: The node name being bootstrapped.
    :param step_name: The step's name.
    """
    path = step_scripts_dir() / step_script_filename(run_id, host, step_name)
    path.unlink(missing_ok=True)


def cleanup_run_scripts(run_id: str) -> None:
    """Remove every step script of one run, whatever state its steps are in.

    Called once a run is finished: no step of it will be dispatched again, so
    nothing will download any of its scripts again either — including one
    whose step was still ``running`` or never reconciled. Best-effort, like
    :func:`cleanup_step_script`.

    :param run_id: The finished run.
    """
    for path in step_scripts_dir().glob(f"{run_id}_*.sh"):
        path.unlink(missing_ok=True)


async def dispatch_step(
    tasks_api: RemoteAPI,
    request: Request | None,
    run_id: str,
    host: str,
    step_name: str,
    action: StepAction,
) -> int:
    """Dispatch one step's action to ``host``, as root, and return its task history id.

    Does **not** wait for the run to finish — see the module docstring. A caller
    polls ``GET /api/tasks/history/{id}`` for progress, the same generic endpoint
    ``om_inventory/bootstrap.py``'s PoC already established this pattern with.

    Any failure after the script is written removes the script before
    re-raising: no executor will download it, and it may carry a secret.

    :param tasks_api: The Tasks API client.
    :param request: The current request, whose host builds the artifact download
        URL — ``None`` falls back to the configured base URL, for callers
        outside a request context (e.g. a Celery reconciliation task retrying a
        step).
    :param run_id: The bootstrap run this step belongs to.
    :param host: The node name being bootstrapped.
    :param step_name: The step's name.
    :param action: The step's action.
    :return: The dispatched run's task history id.
    :raises RuntimeError: When the Tasks API accepts the dispatch but returns no
        history id.
    """
    filename = step_script_filename(run_id, host, step_name)
    _, digest = await write_step_script(run_id, host, step_name, action)
    try:
        return await _post_step_script(tasks_api, request, host, filename, digest)
    except Exception:
        cleanup_step_script(run_id, host, step_name)
        raise


async def _post_step_script(
    tasks_api: RemoteAPI,
    request: Request | None,
    host: str,
    filename: str,
    digest: str,
) -> int:
    """Ask the Tasks API to run an already-written step script on ``host``, as root.

    :param tasks_api: The Tasks API client.
    :param request: See :func:`dispatch_step`.
    :param host: The node name the script runs on.
    :param filename: The script's name in :func:`step_scripts_dir`.
    :param digest: The script's MD5 digest.
    :return: The dispatched run's task history id.
    :raises RuntimeError: When the Tasks API accepts the dispatch but returns no
        history id.
    """
    snippet_source = build_artifact_download_url(
        request, artifact_type=ARTIFACT_TYPE, filename=filename, md5_digest=digest
    )
    meta = SnippetExecutionMeta(
        target=host,
        interpreter=ROOT_INTERPRETER,
        snippet_source=snippet_source,
        snippet_filename=filename,
        md5_checksum=digest,
    )
    task_id = await post_task_execution(tasks_api, EXEC_ARTIFACT_TASK, meta)
    if task_id is None:
        raise RuntimeError("Tasks API did not return a task history id")
    return task_id
