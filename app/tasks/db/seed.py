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

"""Define the database initial data for the Tasks app."""

import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy_celery_beat.models import Period, PeriodicTask
from sqlmodel import col

from app.core.celery.db import get_async_session_maker as get_celery_beat_session_maker
from app.core.celery.models import IntervalSchedule
from app.core.celery.utils import (
    init_periodic_tasks_db,
    SystemPeriodicTaskData,
    SystemPeriodicTaskSchedule,
)
from app.core.utils.date_time import utc_now
from app.core.utils.fields import DatabaseDialect
from app.tasks.config import tasks_settings
from app.tasks.crud import TaskManager
from app.tasks.db import get_async_session_maker
from app.tasks.db.engine import engine
from app.tasks.execution.executors.nomad.constants import (
    CHECK_NOMAD_CERT_EXPIRY_TASK_NAME,
    SYNC_FINISHING_TASKS_TASK_NAME,
)
from app.tasks.execution.executors.nomad.steps import (
    LAUNCH_CHECK_EXIT_CODE,
    LOG_CAPTURE_HOLD_DEFAULT_SECONDS,
    NomadStep,
    RUN_SCRIPT_OUTPUT_FILES_PATH,
)
from app.tasks.models import (
    EXECUTE_TASK_BY_NAME_TASK,
    INVENTORY_COLLECTION_TASK_NAME,
    INVENTORY_SYNC_FOLLOWERS_KEY,
    INVENTORY_SYNC_TASK_NAME,
    SYNC_RUNNING_TASKS_TASK_NAME,
    SYSTEM_USER,
    Task,
    TaskBackendEnum,
)
from app.tasks.periodic.crud import PeriodicTaskManager

logger = logging.getLogger(__name__)

# POSIX sh preamble that aborts a Nomad allocation when
# ``now - scheduled_at`` exceeds the configured staleness threshold. Missing
# or empty meta values short-circuit to a no-op for rollback safety.
#
# NOTE: shell variables are referenced with the bareword form (``$name``) and
# never the brace form (``${name}``). Nomad interpolates ``${...}`` in
# ``raw_exec`` args through its own variable table before spawning the shell,
# and fails config validation for references it does not know (e.g. the shell
# local ``${elapsed}``). The ``""s`` after each variable name disambiguates the
# variable name from the trailing literal ``s`` so ``$elapseds`` is not parsed
# as a single (unset) variable name.
STALENESS_PREAMBLE_SHELL = (
    'if [ -n "$NOMAD_META_scheduled_at" ] && '
    '[ -n "$NOMAD_META_staleness_threshold_seconds" ]; then '
    "now=$(date +%s); "
    "elapsed=$((now - NOMAD_META_scheduled_at)); "
    'if [ "$elapsed" -gt "$NOMAD_META_staleness_threshold_seconds" ]; then '
    'echo "EXTENSIONS_STALE_SKIP: elapsed=$elapsed""s '
    'threshold=$NOMAD_META_staleness_threshold_seconds""s"; '
    "exit 75; "
    "fi; "
    "fi"
)

_CHECK_STALENESS_TASK = {
    "Name": NomadStep.CHECK_STALENESS,
    "Lifecycle": {"hook": "prestart", "sidecar": False},
    "Driver": "raw_exec",
    "User": "",
    "Config": {
        "command": "sh",
        "args": ["-c", STALENESS_PREAMBLE_SHELL],
    },
    "Meta": {},
    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
}

_STALENESS_META_OPTIONAL = ["scheduled_at", "staleness_threshold_seconds"]

#: Allocation-shared path the ``check-launchable`` prestart step writes the
#: effective interpreter to, and the artifact specs' ``run-script`` steps launch
#: from. Mirrors the existing ``${NOMAD_ALLOC_DIR}/venv`` handoff between
#: ``prepare-env`` and ``run-script``.
EFFECTIVE_INTERPRETER_PATH = "${NOMAD_ALLOC_DIR}/pmm_extensions_interpreter"

#: ``sudo`` options that consume the following token as their value. Walking
#: past them is what keeps ``sudo -u postgres <cmd>`` resolving ``<cmd>`` rather
#: than the user name, which names no binary and would abort a run that works.
_SUDO_VALUE_OPTIONS = (
    "-C|-D|-c|-g|-h|-p|-R|-r|-T|-t|-U|-u"
    "|--close-from|--chdir|--login-class|--group|--host|--prompt|--chroot"
    "|--role|--command-timeout|--type|--other-user|--user"
)

#: ``sudo`` options that take no value, so stepping over one leaves the next
#: word still the command. Every other ``-*`` declines: an option outside both
#: tables may consume the token after it, and resolving that token as a command
#: would abort a run the node would have completed. ``-e``/``-i``/``-l``/``-s``
#: are deliberately absent — they change how the remaining words are read
#: rather than merely preceding them, so the check declines them too.
_SUDO_FLAG_OPTIONS = (
    "-A|-B|-b|-E|-H|-K|-k|-n|-P|-S|-V|-v"
    "|--askpass|--background|--bell|--help|--non-interactive|--preserve-env"
    "|--preserve-groups|--remove-timestamp|--reset-timestamp|--set-home"
    "|--stdin|--validate|--version"
)

#: The interpreter ``prepare-env`` builds its virtualenv with, and therefore the
#: one ``exec-python-artifact`` actually needs on the node. That spec's
#: ``run-script`` always execs the venv python, never the interpreter meta, so
#: this -- not the meta's own token -- is what its check resolves.
_VENV_BUILDER_COMMAND = "python3"

#: ``case`` pattern matching any meta value whose quoting, expansion or escaping
#: ``env -S`` parses differently from ``sh`` word-splitting. The check declines
#: those rather than guessing, because guessing wrong aborts an execution the
#: launcher would have run.
_META_METACHAR_PATTERN = r"""*\'*|*\"*|*\$*|*\`*|*\\*"""


def _launch_check_shell(
    meta_key: str, *, allow_strip: bool, launches: str | None = None
) -> str:
    """Build the POSIX sh preamble that resolves a spec's launch command chain.

    The preamble word-splits the spec's launch-command meta, resolves the
    commands the node would actually exec, and aborts with
    :data:`~app.tasks.execution.executors.nomad.steps.LAUNCH_CHECK_EXIT_CODE`
    when one of them is absent. It recognizes a deliberately small grammar —
    plain words, optionally behind a bare ``sudo`` — and declines anything
    else, leaving that invocation to behave exactly as it does today: ``sh``
    word-splitting and ``env -S`` (what the launcher tokenizes with) are
    different grammars, so aborting on a form only one of them understands would
    fail an execution that runs.

    Under ``allow_strip`` it also drops a redundant ``sudo`` prefix when the
    node's tasks already run as uid 0 and the prefix's *own* token does not
    resolve there — an operator-supplied ``/opt/x/sudo`` that exists is kept,
    since a binary the invocation names by path may be a wrapper that changes
    the target user rather than the stock no-op-as-root ``sudo``. It writes the
    effective interpreter to :data:`EFFECTIVE_INTERPRETER_PATH` for the spec's
    ``run-script`` step to launch from. Only a *bare* prefix is dropped:
    ``sudo -u <user>`` lowers privilege, so removing it would run the payload as
    root instead of as the named user.

    ``launches`` names the binary a spec's ``run-script`` execs when that is
    *not* the meta's own token. ``exec-python-artifact`` is the case: it always
    runs the venv python and consults the meta only for a ``"sudo "`` prefix
    test, so resolving the meta's interpreter there would abort a runnable
    execution whenever an operator maps ``.py`` to anything but
    :data:`_VENV_BUILDER_COMMAND` (``INTERPRETERS`` is settings-configurable).
    That variant therefore mirrors the launcher exactly: it resolves ``sudo``
    only when the effective interpreter carries the literal prefix the launcher
    tests for, then resolves ``launches``.

    A leading ``NAME=VALUE`` is declined rather than skipped: ``env -S`` applies
    it before locating the command, so an assignment that changes ``PATH``
    decides where the command resolves, and resolving against the step's own
    environment instead would report a runnable execution as unlaunchable.

    A first token beginning with ``-`` is declined for the same reason ``env``
    is: ``env -S`` parses leading options itself, so ``-u FOO bash`` unsets
    ``FOO`` and runs ``bash``, and resolving ``-u`` as a command name would
    abort an execution the node would have completed.

    A non-absolute path holding a slash is declined because it resolves against
    this step's cwd, which is not the launcher's — ``run-script`` pins a
    ``work_dir`` under the task dir and this step pins none. That answers about
    the wrong directory in both directions, and the direction that hurts is a
    payload-relative interpreter present only under that ``work_dir``, which
    resolving here would abort despite the launcher running it. The check
    cannot inspect that cwd, so the whole form is declined.

    ``run-command`` is checked against a single word-split of its meta, which
    its launcher passes to ``xargs`` as one argv element rather than splitting.
    Every producer emits a single token today, so the two agree; a multi-token
    command would be checked on its first token and fail in the launcher as it
    does now, which is the harmless direction.

    Shell locals use the bareword form (``$name``) throughout, for the reason
    given at :data:`STALENESS_PREAMBLE_SHELL`. ``${NOMAD_ALLOC_DIR}`` is in
    Nomad's own variable table and is the one brace form correct here.

    :param meta_key: The spec's launch-command meta key, without the
        ``NOMAD_META_`` prefix.
    :param allow_strip: Whether the spec's ``run-script`` step reads the
        effective interpreter back, which is what makes a strip observable.
    :param launches: The binary the spec's ``run-script`` execs, when the meta
        is not it. ``None`` resolves the meta's own command chain.
    :return: The POSIX sh script the step runs.
    """
    decline = (
        f"printf '%s' \"$m\" > {EFFECTIVE_INTERPRETER_PATH}; exit 0"
        if allow_strip
        else "exit 0"
    )

    def abort(command: str) -> str:
        return (
            f'echo "EXTENSIONS_UNLAUNCHABLE: command={command} node=$NOMAD_META_target"; '
            f"exit {LAUNCH_CHECK_EXIT_CODE}"
        )

    def resolve_or_abort(token: str, name: str) -> str:
        """Return the sh that resolves ``token`` or aborts naming ``name``.

        A token holding a slash is tested with ``[ -x ]`` and ``[ ! -d ]``
        rather than ``command -v``, which answers "would the shell accept this
        word" and not "can the node exec it": dash and busybox ash both report
        a bare-existing path as found whatever its mode, so a non-executable
        interpreter — or a directory — would pass and the run would land in
        ``FAILED``, the outcome this step exists to separate out. Only bash
        checks the mode. Bare names keep ``command -v``, which does search
        ``PATH`` for an executable and is right for them.

        :param token: The already-quoted shell word to resolve.
        :param name: The command to name in the abort diagnostic.
        :return: The POSIX sh fragment performing the resolution.
        """
        return (
            f"case {token} in "
            f"*/*) [ -x {token} ] && [ ! -d {token} ];; "
            f"*) command -v {token} > /dev/null 2>&1;; "
            f"esac || {{ {abort(name)}; }}; "
        )

    decline_assignment = f'case "$1" in *=*) {decline};; esac; '
    # Records the strip rather than announcing it, so the notice is emitted only
    # once the run is known to launch. Reporting it above an abort would head an
    # unlaunchable execution's only diagnostic with a success-shaped line.
    strip = (
        "if [ $# -ge 2 ]; then "
        'case "$1" in sudo|*/sudo) '
        'case "$2" in -*|*=*) ;; '
        '*) if [ "$(id -u)" = 0 ] && ! command -v "$1" > /dev/null 2>&1; then '
        "shift; eff=$*; stripped=1; "
        "fi;; esac;; esac; "
        "fi; "
    )
    sudo_walk = (
        'case "$1" in sudo|*/sudo) shift; '
        "while [ $# -gt 0 ]; do "
        'case "$1" in '
        "--) shift; break;; "
        "--*=*) shift;; "
        f"{_SUDO_VALUE_OPTIONS}) shift; if [ $# -gt 0 ]; then shift; fi;; "
        f"{_SUDO_FLAG_OPTIONS}) shift;; "
        f"-*) {decline};; "
        "*) break;; "
        "esac; done; "
        + decline_assignment
        + "if [ $# -gt 0 ]; then "
        + resolve_or_abort('"$1"', "$1")
        + "fi;; esac; "
    )
    if launches is None:
        resolution = (
            decline_assignment
            + "[ $# -gt 0 ] || exit 0; "
            + resolve_or_abort('"$1"', "$1")
            + sudo_walk
        )
    else:
        # Mirrors the launcher's own test byte for byte: it prefixes the venv
        # python with sudo for exactly this pattern and ignores the meta
        # otherwise, so anything else in the meta is not ours to resolve.
        resolution = (
            'case "$eff" in "sudo "*) '
            + resolve_or_abort("sudo", "sudo")
            + ";; esac; "
            + resolve_or_abort(launches, launches)
        )
    announce = (
        '[ -z "$stripped" ] || echo "EXTENSIONS_SUDO_STRIPPED: node=$NOMAD_META_target"; '
        if allow_strip
        else ""
    )
    return (
        f"m=$NOMAD_META_{meta_key}; "
        '[ -n "$m" ] || exit 0; '
        f'case "$m" in {_META_METACHAR_PATTERN}) {decline};; esac; '
        "set -f; "
        "set -- $m; "
        "[ $# -gt 0 ] || exit 0; "
        f'case "$1" in env|*/env|-*) {decline};; esac; '
        f'case "$1" in /*) ;; */*) {decline};; esac; '
        + ("eff=$m; " + strip if allow_strip else "")
        + resolution
        + announce
        + (
            f"printf '%s' \"$eff\" > {EFFECTIVE_INTERPRETER_PATH}; "
            if allow_strip
            else ""
        )
        + "exit 0"
    )


def _check_launchable_task(
    meta_key: str, *, allow_strip: bool, launches: str | None = None
) -> dict[str, Any]:
    """Build the prestart step guarding one spec's launch command.

    A factory rather than a module constant deep-copied per site: the rendered
    shell differs by meta key and strip policy, and building per call removes
    the shared-mutable hazard the constant shape carries.

    :param meta_key: The spec's launch-command meta key, without the
        ``NOMAD_META_`` prefix.
    :param allow_strip: Whether the spec's ``run-script`` step launches from
        :data:`EFFECTIVE_INTERPRETER_PATH`.
    :param launches: Forwarded to :func:`_launch_check_shell`.
    :return: The Nomad task definition for the check step.
    """
    return {
        "Name": NomadStep.CHECK_LAUNCHABLE,
        "Lifecycle": {"hook": "prestart", "sidecar": False},
        "Driver": "raw_exec",
        "User": "",
        "Config": {
            "command": "sh",
            "args": [
                "-c",
                _launch_check_shell(
                    meta_key, allow_strip=allow_strip, launches=launches
                ),
            ],
        },
        "Meta": {},
        "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
    }


#: POSIX sh body of the log-capture hold: keep the allocation non-terminal after
#: the payload exits so Nomad cannot garbage-collect logs PMM Extensions has not read yet,
#: until either PMM Extensions signals the step or the deadline elapses.
#:
#: ``sleep`` is backgrounded and waited on because a POSIX shell runs traps only
#: between foreground commands -- ``trap ...; sleep N`` would ignore the signal
#: for the full N seconds, which is the whole duration the trap exists to cut
#: short.
LOG_CAPTURE_HOLD_SHELL = (
    'trap "exit 0" TERM INT; '
    "hold=$NOMAD_META_log_capture_hold_seconds; "
    '[ -n "$hold" ] || hold=' + str(LOG_CAPTURE_HOLD_DEFAULT_SECONDS) + "; "
    'sleep "$hold" & '
    "wait $!; "
    "exit 0"
)

_LOG_CAPTURE_HOLD_TASK = {
    "Name": NomadStep.LOG_CAPTURE_HOLD,
    "Lifecycle": {"hook": "poststop", "sidecar": False},
    "Driver": "raw_exec",
    "User": "",
    "Config": {
        "command": "sh",
        "args": ["-c", LOG_CAPTURE_HOLD_SHELL],
    },
    "Meta": {},
    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
}

_LOG_CAPTURE_HOLD_META_OPTIONAL = ["log_capture_hold_seconds"]

NOMAD_RUN_COMMAND = {
    "ID": "run-command",
    "Name": "run-command",
    "Type": "batch",
    "Datacenters": ["*"],
    "Constraints": [
        {
            "LTarget": "${node.unique.name}",
            "RTarget": "${NOMAD_META_target}",
            "Operand": "=",
        },
    ],
    "ParameterizedJob": {
        "Payload": "forbidden",
        "MetaRequired": ["target", "command"],
        "MetaOptional": [
            "args",
            *_STALENESS_META_OPTIONAL,
            *_LOG_CAPTURE_HOLD_META_OPTIONAL,
        ],
    },
    "TaskGroups": [
        {
            "Name": "execution",
            "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
            "ReschedulePolicy": {"Attempts": 0},
            "Tasks": [
                deepcopy(_CHECK_STALENESS_TASK),
                _check_launchable_task("command", allow_strip=False),
                {
                    "Name": NomadStep.RUN_SCRIPT,
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "xargs",
                        "args": [
                            "--arg-file",
                            "args_file",
                            "${NOMAD_META_command}",
                        ],
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                    "Templates": [
                        {
                            "EmbeddedTmpl": '{{ env "NOMAD_META_args" }}',
                            "DestPath": "args_file",
                        },
                    ],
                },
                deepcopy(_LOG_CAPTURE_HOLD_TASK),
            ],
        },
    ],
}

NOMAD_RUN_PYTHON = {
    "ID": "run-python",
    "Name": "run-python",
    "Type": "batch",
    "Datacenters": ["*"],
    "Constraints": [
        {
            "LTarget": "${node.unique.name}",
            "RTarget": "${NOMAD_META_target}",
            "Operand": "=",
        },
    ],
    "ParameterizedJob": {
        "Payload": "required",
        "MetaRequired": ["target"],
        "MetaOptional": [
            "config",
            "requirements",
            *_STALENESS_META_OPTIONAL,
            *_LOG_CAPTURE_HOLD_META_OPTIONAL,
        ],
    },
    "TaskGroups": [
        {
            "Name": "execution",
            "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
            "PreventRescheduleOnLost": True,
            "ReschedulePolicy": {"Attempts": 0},
            "Tasks": [
                deepcopy(_CHECK_STALENESS_TASK),
                {
                    "Name": NomadStep.PREPARE_ENV,
                    "Lifecycle": {"hook": "prestart", "sidecar": False},
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "sh",
                        "args": [
                            "-c",
                            f"{STALENESS_PREAMBLE_SHELL}; "
                            "python3 -m venv --copies ${NOMAD_ALLOC_DIR}/venv;"
                            "${NOMAD_ALLOC_DIR}/venv/bin/pip install -r requirements.txt",
                        ],
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                    "Templates": [
                        {
                            "EmbeddedTmpl": '{{ env "NOMAD_META_requirements" }}',
                            "DestPath": "requirements.txt",
                        },
                    ],
                },
                {
                    "Name": NomadStep.RUN_SCRIPT,
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "sh",
                        "args": [
                            "-c",
                            "gzip -d ${NOMAD_TASK_DIR}/script.py.gz;"
                            "${NOMAD_ALLOC_DIR}/venv/bin/python3"
                            " -u ${NOMAD_TASK_DIR}/script.py --config ${NOMAD_TASK_DIR}/script_config",
                        ],
                        "work_dir": "${NOMAD_TASK_DIR}/output_files",
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                    "Templates": [
                        {
                            "EmbeddedTmpl": '{{ env "NOMAD_META_config" }}',
                            "DestPath": "local/script_config",
                        },
                        {
                            "EmbeddedTmpl": ".keep",
                            "DestPath": "local/output_files/.keep",
                        },
                    ],
                    "DispatchPayload": {"file": "script.py.gz"},
                },
                {
                    "Name": NomadStep.CLEAN_UP,
                    "Lifecycle": {"hook": "poststop", "sidecar": False},
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "rm",
                        "args": [
                            "-rf",
                            "${NOMAD_ALLOC_DIR}/venv",
                            "requirements.txt",
                            "script_config",
                        ],
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                },
                deepcopy(_LOG_CAPTURE_HOLD_TASK),
            ],
        },
    ],
}

NOMAD_EXEC_ARTIFACT = {
    "ID": "exec-artifact",
    "Name": "exec-artifact",
    "Type": "batch",
    "Datacenters": ["*"],
    "Constraints": [
        {
            "LTarget": "${node.unique.name}",
            "RTarget": "${NOMAD_META_target}",
            "Operand": "=",
        },
    ],
    "ParameterizedJob": {
        "Payload": "forbidden",
        "MetaRequired": [
            "target",
            "snippet_source",
            "interpreter",
            "md5_checksum",
        ],
        "MetaOptional": [
            "args",
            *_STALENESS_META_OPTIONAL,
            *_LOG_CAPTURE_HOLD_META_OPTIONAL,
        ],
    },
    "TaskGroups": [
        {
            "Name": "execution",
            "ReschedulePolicy": {"Attempts": 0},
            "Tasks": [
                deepcopy(_CHECK_STALENESS_TASK),
                _check_launchable_task("interpreter", allow_strip=True),
                {
                    "Name": NomadStep.RUN_SCRIPT,
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "sh",
                        "args": [
                            "-c",
                            f"i=$(cat {EFFECTIVE_INTERPRETER_PATH} 2>/dev/null); "
                            '[ -n "$i" ] || i=$NOMAD_META_interpreter; '
                            "xargs --arg-file ${NOMAD_TASK_DIR}/args_file "
                            'env -S "$i" ${NOMAD_TASK_DIR}/script',
                        ],
                        "work_dir": "${NOMAD_TASK_DIR}/output_files",
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                    "Templates": [
                        {
                            "EmbeddedTmpl": '{{ env "NOMAD_META_args" }}',
                            "DestPath": "local/args_file",
                        },
                        {
                            "EmbeddedTmpl": ".keep",
                            "DestPath": "local/output_files/.keep",
                        },
                    ],
                    "Artifacts": [
                        {
                            "GetterSource": "${NOMAD_META_snippet_source}",
                            "GetterMode": "file",
                            "RelativeDest": "local/script",
                            "GetterOptions": {
                                "checksum": "md5:${NOMAD_META_md5_checksum}",
                            },
                            "GetterInsecure": True,
                        }
                    ],
                },
                deepcopy(_LOG_CAPTURE_HOLD_TASK),
            ],
        },
    ],
}

NOMAD_EXEC_PYTHON_ARTIFACT = {
    "ID": "exec-python-artifact",
    "Name": "exec-python-artifact",
    "Type": "batch",
    "Datacenters": ["*"],
    "Constraints": [
        {
            "LTarget": "${node.unique.name}",
            "RTarget": "${NOMAD_META_target}",
            "Operand": "=",
        },
    ],
    "ParameterizedJob": {
        "Payload": "forbidden",
        "MetaRequired": [
            "target",
            "snippet_source",
            "interpreter",
            "md5_checksum",
        ],
        "MetaOptional": [
            "args",
            "requirements",
            *_STALENESS_META_OPTIONAL,
            *_LOG_CAPTURE_HOLD_META_OPTIONAL,
        ],
    },
    "TaskGroups": [
        {
            "Name": "execution",
            "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
            "PreventRescheduleOnLost": True,
            "ReschedulePolicy": {"Attempts": 0},
            "Tasks": [
                deepcopy(_CHECK_STALENESS_TASK),
                _check_launchable_task(
                    "interpreter",
                    allow_strip=True,
                    launches=_VENV_BUILDER_COMMAND,
                ),
                {
                    "Name": NomadStep.PREPARE_ENV,
                    "Lifecycle": {"hook": "prestart", "sidecar": False},
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "sh",
                        "args": [
                            "-c",
                            f"{STALENESS_PREAMBLE_SHELL}; "
                            f"{_VENV_BUILDER_COMMAND} -m venv --copies "
                            "${NOMAD_ALLOC_DIR}/venv;"
                            "${NOMAD_ALLOC_DIR}/venv/bin/pip install -r requirements.txt",
                        ],
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                    "Templates": [
                        {
                            "EmbeddedTmpl": '{{ env "NOMAD_META_requirements" }}',
                            "DestPath": "requirements.txt",
                        },
                    ],
                },
                {
                    "Name": NomadStep.RUN_SCRIPT,
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "sh",
                        "args": [
                            "-c",
                            f"i=$(cat {EFFECTIVE_INTERPRETER_PATH} 2>/dev/null); "
                            '[ -n "$i" ] || i=$NOMAD_META_interpreter; '
                            "PYTHON_CMD=${NOMAD_ALLOC_DIR}/venv/bin/python3;"
                            'case "$i" in "sudo "*) '
                            'PYTHON_CMD="sudo ${NOMAD_ALLOC_DIR}/venv/bin/python3";; esac;'
                            "xargs --arg-file ${NOMAD_TASK_DIR}/args_file -- "
                            "$PYTHON_CMD -u ${NOMAD_TASK_DIR}/script",
                        ],
                        "work_dir": "${NOMAD_TASK_DIR}/output_files",
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                    "Templates": [
                        {
                            "EmbeddedTmpl": '{{ env "NOMAD_META_args" }}',
                            "DestPath": "local/args_file",
                        },
                        {
                            "EmbeddedTmpl": ".keep",
                            "DestPath": "local/output_files/.keep",
                        },
                    ],
                    "Artifacts": [
                        {
                            "GetterSource": "${NOMAD_META_snippet_source}",
                            "GetterMode": "file",
                            "RelativeDest": "local/script",
                            "GetterOptions": {
                                "checksum": "md5:${NOMAD_META_md5_checksum}",
                            },
                            "GetterInsecure": True,
                        }
                    ],
                },
                {
                    "Name": NomadStep.CLEAN_UP,
                    "Lifecycle": {"hook": "poststop", "sidecar": False},
                    "Driver": "raw_exec",
                    "User": "",
                    "Config": {
                        "command": "rm",
                        "args": [
                            "-rf",
                            "${NOMAD_ALLOC_DIR}/venv",
                            "requirements.txt",
                        ],
                    },
                    "Meta": {},
                    "RestartPolicy": {"Attempts": 0, "Mode": "fail"},
                },
                deepcopy(_LOG_CAPTURE_HOLD_TASK),
            ],
        },
    ],
}

SYSTEM_TASKS = [
    Task(
        name="run-command",
        data=NOMAD_RUN_COMMAND,
        protected=True,
        anonymize_mask=None,
        created_by=SYSTEM_USER,
    ),
    Task(
        name="run-python",
        data=NOMAD_RUN_PYTHON,
        protected=True,
        anonymize_mask=None,
        output_files_path=RUN_SCRIPT_OUTPUT_FILES_PATH,
        created_by=SYSTEM_USER,
    ),
    Task(
        name="exec-artifact",
        data=NOMAD_EXEC_ARTIFACT,
        protected=True,
        anonymize_mask=None,
        output_files_path=RUN_SCRIPT_OUTPUT_FILES_PATH,
        created_by=SYSTEM_USER,
    ),
    Task(
        name="exec-python-artifact",
        data=NOMAD_EXEC_PYTHON_ARTIFACT,
        protected=True,
        anonymize_mask=None,
        output_files_path=RUN_SCRIPT_OUTPUT_FILES_PATH,
        created_by=SYSTEM_USER,
    ),
    Task(
        name=INVENTORY_SYNC_TASK_NAME,
        data={
            "callable": "app.extensions.apps.inventory.sync.run_scheduled_inventory_sync",
            "target": "local",
        },
        backend=TaskBackendEnum.CELERY,
        protected=True,
        created_by=SYSTEM_USER,
    ),
    Task(
        name=INVENTORY_COLLECTION_TASK_NAME,
        data={
            "callable": (
                "app.extensions.apps.inventory.collection.run_scheduled_inventory_collection"
            ),
            "target": "local",
        },
        backend=TaskBackendEnum.CELERY,
        protected=True,
        alert_on_fail=True,
        created_by=SYSTEM_USER,
    ),
]

SYSTEM_PERIODIC_TASKS = [
    SystemPeriodicTaskSchedule(
        schedule=IntervalSchedule(every=30, period=Period.SECONDS),
        tasks=[
            SystemPeriodicTaskData(
                name=SYNC_RUNNING_TASKS_TASK_NAME,
                task_name="app.tasks.celery.sync_running_tasks",
                extra_kwargs={"expire_seconds": 30},
            ),
        ],
    )
]

_nomad_cert_schedule = tasks_settings.NOMAD.check_cert_expiry_interval
if _nomad_cert_schedule is not None:
    SYSTEM_PERIODIC_TASKS.append(
        SystemPeriodicTaskSchedule(
            schedule=_nomad_cert_schedule,
            tasks=[
                SystemPeriodicTaskData(
                    name=CHECK_NOMAD_CERT_EXPIRY_TASK_NAME,
                    task_name="app.tasks.celery.check_nomad_cert_expiry",
                ),
            ],
        ),
    )

_finishing_sync_interval = tasks_settings.NOMAD.finishing_sync_interval_seconds
if _finishing_sync_interval is not None:
    SYSTEM_PERIODIC_TASKS.append(
        SystemPeriodicTaskSchedule(
            schedule=IntervalSchedule(
                every=_finishing_sync_interval, period=Period.SECONDS
            ),
            tasks=[
                SystemPeriodicTaskData(
                    name=SYNC_FINISHING_TASKS_TASK_NAME,
                    task_name="app.tasks.celery.sync_finishing_tasks",
                    extra_kwargs={"expire_seconds": _finishing_sync_interval},
                ),
            ],
        ),
    )

_log_purge_schedule = tasks_settings.LOG_PURGE_INTERVAL
if _log_purge_schedule is not None:
    SYSTEM_PERIODIC_TASKS.append(
        SystemPeriodicTaskSchedule(
            schedule=_log_purge_schedule,
            tasks=[
                SystemPeriodicTaskData(
                    name="tasks__purge_task_history_logs",
                    task_name="app.tasks.celery.purge_task_history_logs",
                ),
            ],
        ),
    )


SYSTEM_PERIODIC_TASK_PREFIX = "tasks__"
INVENTORY_SYNC_SCHEDULE_NAME = f"{SYSTEM_PERIODIC_TASK_PREFIX}inventory_sync"


def _inventory_sync_schedule_name(syncer: str) -> str:
    """Derive the seeded row name for a per-syncer inventory-sync schedule.

    Built from the **full** qualified syncer name rather than its bare class, which
    would collide for two syncers sharing a class name across modules — legal
    configuration that ``init_periodic_tasks_db`` resolves by silently updating one
    row rather than erroring, leaving fewer schedules than the configuration
    promises and letting order decide the winner.

    Retains ``SYSTEM_PERIODIC_TASK_PREFIX`` by construction, which is what scopes
    the orphan cleanup to rows this seeder owns.

    :param syncer: The fully qualified syncer the schedule targets.
    :return: A name that is deterministic, stable across boots, and unique per
        syncer identity.
    """
    return f"{INVENTORY_SYNC_SCHEDULE_NAME}__{syncer}"


def _inventory_sync_schedule(
    name: str,
    syncer: str | None,
    interval: IntervalSchedule | None,
    ordering: Mapping[str, str | list[str]] | None = None,
) -> SystemPeriodicTaskSchedule | None:
    """Build one inventory-sync schedule, or ``None`` when unconfigured.

    ``inventory-sync`` is a ``Task`` row rather than a Celery function, so the
    entry uses the same indirection an operator-created schedule uses: it points
    at ``execute_task_by_name`` and names the PMM Extensions task in ``kwargs``. Only that
    shape appears in the sync UI's schedule list and produces the ``TaskHistory``
    rows the sync-health rollup reads.

    The entry opts into ``due_on_first_seed`` because the beat store cannot tell
    a schedule that has never run apart from one that has just run: without the
    marker the first sync lands one interval after beat starts and the countdown
    restarts on every restart, so a deployment redeployed more often than the
    interval never syncs at all. It applies wherever an interval is configured,
    since a schedule switched on for the first time should collect inventory now
    rather than one interval from now.

    ``ordering`` names, on the pinned default, the per-syncer schedules it
    starts after its first completed pass. It is merged into the meta beside
    ``syncer``, where the executor forwards it to the callable, so which
    followers start is decided at run time and the due marker above stays
    unchanged.

    :param name: The seeded row name this schedule owns.
    :param syncer: The syncer to pin, or ``None`` to run every configured one.
    :param interval: How often the schedule fires, or ``None`` to seed nothing.
    :param ordering: Meta keys naming the per-syncer schedules the pinned
        default starts, or ``None`` for none. Ignored when ``syncer`` is unset.
    :return: The schedule to append to the seeded set, or ``None`` when
        ``interval`` is unset.
    """
    if interval is None:
        return None
    kwargs = {
        "task_name": INVENTORY_SYNC_TASK_NAME,
        "periodic_task_name": name,
        **(
            {"execution_data": {"meta": {"syncer": syncer, **(ordering or {})}}}
            if syncer
            else {}
        ),
    }
    return SystemPeriodicTaskSchedule(
        schedule=interval,
        tasks=[
            SystemPeriodicTaskData(
                name=name,
                task_name=EXECUTE_TASK_BY_NAME_TASK,
                extra_kwargs={"kwargs": json.dumps(kwargs)},
                due_on_first_seed=True,
            ),
        ],
    )


def _schedule_covers_syncer(row: PeriodicTask, syncer: str | None) -> bool:
    """Return whether an existing beat row already syncs ``syncer``.

    Fail closed: a row whose ``kwargs`` cannot be decoded to the expected shape
    counts as covering, so an unreadable operator row can never result in a
    second schedule firing alongside it. Every level is ``isinstance``-guarded
    because ``kwargs`` is operator-populated free text and ``json.loads``
    returns ``Any``, which type-checks against anything.

    The ``syncer is None`` branch is deliberately asymmetric: when no syncer is
    configured the default would run every syncer, so any operator row at all
    covers it — including one pinned to a single syncer.

    :param row: An existing periodic-task row targeting ``inventory-sync``.
    :param syncer: The fully qualified syncer the default would target, or
        ``None`` for the sync-all default.
    :return: Whether the row makes seeding the default redundant.
    """
    try:
        decoded = json.loads(row.kwargs) if row.kwargs else None
    except json.JSONDecodeError:
        return True
    if not isinstance(decoded, dict):
        return True
    execution_data = decoded.get("execution_data")
    meta = execution_data.get("meta") if isinstance(execution_data, dict) else None
    existing = meta.get("syncer") if isinstance(meta, dict) else None
    if not isinstance(existing, str) or not existing.strip():
        return True
    return syncer is None or existing == syncer


async def _seeded_inventory_sync_schedule(
    name: str,
    syncer: str | None,
    interval: IntervalSchedule | None,
    ordering: Mapping[str, str | list[str]] | None = None,
) -> SystemPeriodicTaskSchedule | None:
    """Return the schedule to seed, or ``None`` when unset or already covered.

    An operator's manually attached interval stays authoritative, so the default
    is omitted when a schedule they own already covers the configured syncer.
    Rows this seeder owns are excluded: they carry the same ``task`` and
    ``kwargs.task_name`` the lookup filters on, so counting them would make the
    orphan cleanup drop the row on one boot and re-create it on the next.

    A lookup failure does not propagate: this runs at startup, and a malformed
    persisted ``kwargs`` can fail inside the JSON extraction the query performs.
    It also must not simply skip the default, because omitting an entry hands it
    to ``init_periodic_tasks_db``'s orphan cleanup — a data-level failure would
    delete a schedule seeded on an earlier boot and stop the sync entirely. So
    the failure path re-seeds a row this seeder already owns and withholds only
    a first-time one, which neither double-schedules nor un-schedules. That
    ownership question is answered by name alone, before the failing query runs,
    so it does not repeat the JSON extraction. A genuinely unreachable beat
    store fails both queries and then fails ``init_periodic_tasks_db`` too,
    which uses the same store, so nothing is deleted there either.

    Each schedule answers the ownership question for its own ``name``, so one
    schedule's first-time status never decides another's.

    :param name: The seeded row name this schedule owns.
    :param syncer: The syncer to pin, or ``None`` to run every configured one.
    :param interval: How often the schedule fires, or ``None`` to seed nothing.
    :param ordering: Meta keys naming the per-syncer schedules the pinned
        default starts, or ``None`` for none.
    :return: The schedule to seed, or ``None``.
    """
    if (schedule := _inventory_sync_schedule(name, syncer, interval, ordering)) is None:
        return None
    session_maker = get_celery_beat_session_maker()
    already_seeded = False
    try:
        async with session_maker() as session:
            already_seeded = (
                await PeriodicTaskManager.first(session, name=name) is not None
            )
            rows = await PeriodicTaskManager.list_by_task_names(
                session, INVENTORY_SYNC_TASK_NAME
            )
    except SQLAlchemyError:
        logger.exception(
            "Could not read existing %s schedules; keeping any default this "
            "seeder already owns rather than dropping it.",
            INVENTORY_SYNC_TASK_NAME,
        )
        return schedule if already_seeded else None
    covered = any(
        _schedule_covers_syncer(row, syncer)
        for row in rows
        if not row.name.startswith(SYSTEM_PERIODIC_TASK_PREFIX)
    )
    if covered:
        logger.info(
            "Skipping the default %s schedule: an operator-managed schedule "
            "already covers it.",
            INVENTORY_SYNC_TASK_NAME,
        )
        return None
    return schedule


async def seed_system_periodic_tasks() -> None:
    """Seed the tasks-service periodic tasks, including the conditional defaults.

    Build a fresh list per call: ``init_tasks_db`` runs on every lifespan start,
    and appending to the module-level set would accumulate duplicates.

    The scalar pair keeps its historical row name unchanged, so an install
    upgrading into the per-syncer schedules recognises its existing row as owned
    rather than orphaning and re-creating it — which ``due_on_first_seed`` would
    turn into a sync on every boot.

    Each per-syncer schedule runs on its own interval from bring-up. When the
    pinned default is seeded, its meta names those schedules as followers, and
    its runs start a follower that has not run since its first completed sync.
    Nothing is written when the default is not seeded, which leaves a standalone
    or operator-scheduled install unchanged. A follower's meta carries no
    reference to the default, so the rewrite on each boot drops the
    ``after_syncer`` key an earlier build stored there.

    :raises SQLAlchemyError: When the celery-beat store cannot be written.
    """
    periodic_tasks = list(SYSTEM_PERIODIC_TASKS)
    leader = tasks_settings.INVENTORY_SYNC_SYNCER
    followers = [entry.syncer for entry in tasks_settings.INVENTORY_SYNC_SCHEDULES]
    primary = await _seeded_inventory_sync_schedule(
        INVENTORY_SYNC_SCHEDULE_NAME,
        leader,
        tasks_settings.INVENTORY_SYNC_INTERVAL,
        {INVENTORY_SYNC_FOLLOWERS_KEY: followers} if followers else None,
    )
    if primary is not None:
        periodic_tasks.append(primary)
    periodic_tasks.extend(
        [
            schedule
            for entry in tasks_settings.INVENTORY_SYNC_SCHEDULES
            if (
                schedule := await _seeded_inventory_sync_schedule(
                    _inventory_sync_schedule_name(entry.syncer),
                    entry.syncer,
                    entry.interval,
                )
            )
            is not None
        ]
    )
    await init_periodic_tasks_db(periodic_tasks, SYSTEM_PERIODIC_TASK_PREFIX)


async def init_tasks_db() -> None:
    """Initialize the Tasks database with system tasks and periodic tasks."""
    async_session = get_async_session_maker()
    async with async_session() as session:
        system_tasks_names = []
        for task in SYSTEM_TASKS:
            system_tasks_names.append(task.name)
            created_task, created = await TaskManager.get_or_create(
                session, task, {"name"}
            )
            if created:
                logger.info("Created system task %s", created_task.name)
            elif created_task.data != task.data:
                await TaskManager.update(
                    session,
                    created_task,
                    task,
                    flag_modified_fields=["data"],
                    last_updated_by=SYSTEM_USER,
                )
                logger.info(
                    "Updated system task %s with new data: %s",
                    created_task.name,
                    task.data,
                )
            elif created_task.model_dump(
                exclude={"id", "created_at", "updated_at", "deleted_at"}
            ) != task.model_dump(
                exclude={"id", "created_at", "updated_at", "deleted_at"}
            ):
                logger.debug("Created task: %s", created_task.model_dump())
                logger.debug("New task: %s", task.model_dump())
                await TaskManager.update(session, created_task, task)
                logger.info("Updated system task %s", created_task.name)
        await TaskManager.update_where(
            session,
            {"deleted_at": None},
            col(Task.name).in_(system_tasks_names),
            col(Task.deleted_at).is_not(None),
        )
        delete_result = await TaskManager.delete_unattached_system_tasks(
            session, exclude_task_names=system_tasks_names
        )
        if delete_result.rowcount:
            logger.info(
                "Deleted %s system tasks that are no longer needed.",
                delete_result.rowcount,
            )
        update_delete_result = await TaskManager.update_where(
            session,
            {"deleted_at": utc_now()},
            col(Task.name).not_in(system_tasks_names),
            col(Task.protected).is_(True),
            col(Task.deleted_at).is_(None),
        )
        if update_delete_result.rowcount:
            logger.info(
                "Marked %s unused system tasks with attached runs as deleted.",
                update_delete_result.rowcount,
            )
    await seed_system_periodic_tasks()


async def verify_taskhistory_execution_request_is_jsonb() -> None:
    """Fail fast if ``taskhistory.execution_request`` is not ``jsonb`` on PostgreSQL.

    Defend against a deploy that ships the ``@>`` dispatch dedup code
    without running the corresponding Alembic migration. ``compare_type``
    intentionally suppresses the ``json``/``jsonb`` diff during autogeneration,
    so ``make checkmigrations`` cannot detect this skew. Without this guard,
    the first dispatch hits ``operator does not exist: json @> jsonb`` at
    runtime; with it, the Tasks app refuses to start until the column is
    converted. The check is a no-op on SQLite and MySQL, where the corresponding
    migration is also a no-op.

    Use SQLAlchemy's reflection inspector rather than a raw
    ``information_schema`` query so the dialect-specific type mapping is what
    decides whether the column is ``JSONB`` or plain ``JSON``.

    :raises RuntimeError: If the PostgreSQL column type is not ``jsonb``.
    """
    if not engine.dialect.name.startswith(DatabaseDialect.POSTGRESQL):
        return
    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_columns("taskhistory")
        )
    for column in columns:
        if column["name"] != "execution_request":
            continue
        if isinstance(column["type"], JSONB):
            return
        raise RuntimeError(
            f"taskhistory.execution_request is {type(column['type']).__name__!r}, "
            "expected 'JSONB'. Run the Tasks API Alembic migrations (SEP-988) "
            "before starting the app."
        )
    raise RuntimeError(
        "taskhistory.execution_request column not found. Run the Tasks API "
        "Alembic migrations (SEP-988) before starting the app."
    )
