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

"""Provide the settings every test module needs before it imports application code.

``ENCRYPTION_KEY`` is required at settings construction and has no default, so
it has to exist before the first module that touches ``settings`` is imported.
This runs at conftest import, which pytest performs ahead of collecting the
modules beneath it, and it is minted rather than pinned in
``[tool.pytest.ini_options] env`` so no working key is committed. Test data is
per-run and never leaves the process, so a key that differs between runs and
between parallel workers costs nothing.

It also hosts the opt-in per-process peak-RSS report (:mod:`tests.peak_rss`),
because this is the one conftest every xdist worker loads whichever test tree
it runs.

And it holds the probes that skip a case whose host tool or file system cannot
run it here. The scripts under test run on Linux: in the sidecar image, which
has bash 5, and on execution hosts, which have GNU tools. A macOS checkout has
bash 3.2, BSD ``xargs`` and ``df``, and APFS, so a case that runs one of those
scripts would fail there for a reason unrelated to the code. Each probe checks
the executable or file system the case actually uses. CI provides all of them,
so there a missing one fails the case instead of skipping it.
"""

import errno
import functools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from tests.app.host_payloads import missing_interpreter_is_fatal
from tests.peak_rss import peak_rss_summary, record_peak_rss

BASH_ASSOCIATIVE_ARRAYS_SINCE = 4
"""The first bash major version with ``declare -A`` and ``mapfile``."""

BASH_4_REASON = (
    "needs bash 4 or later as `bash` on PATH; macOS ships 3.2: brew install bash"
)

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Record this process's peak RSS when ``PYTEST_PEAK_RSS_FILE`` is set."""
    record_peak_rss(session.config)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Print every process's peak RSS and the worker median on the controller."""
    if hasattr(config, "workerinput"):
        return
    lines = peak_rss_summary()
    if not lines:
        return
    terminalreporter.section("peak RSS")
    for line in lines:
        terminalreporter.write_line(line)


def skip_unless_ci(reason: str) -> None:
    """Skip the running case with ``reason``, or fail it when running in CI.

    :param reason: What this host lacks.
    """
    if missing_interpreter_is_fatal():
        pytest.fail(f"CI must provide what this case needs: {reason}")
    pytest.skip(reason)


@functools.cache
def _bash_major_version(bash: str | None) -> int:
    """Return the major version of ``bash``, or 0 when it is missing or cannot run.

    :param bash: The resolved path of ``bash``, or ``None``.
    :return: The major version.
    """
    if bash is None:
        return 0
    try:
        probe = subprocess.run(
            [bash, "-c", "echo ${BASH_VERSINFO[0]}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    version = probe.stdout.strip()
    return int(version) if version.isdigit() else 0


def require_bash_4(path: str | None = None) -> None:
    """Skip the running case unless ``bash`` on ``path`` is version 4 or later.

    :param path: The ``PATH`` the case runs ``bash`` from; the process's own when
        ``None``.
    """
    bash = shutil.which("bash", path=path)
    if _bash_major_version(bash) < BASH_ASSOCIATIVE_ARRAYS_SINCE:
        skip_unless_ci(BASH_4_REASON)


@functools.cache
def _is_gnu(executable: str) -> bool:
    """Return whether ``executable`` is the GNU build of its tool.

    :param executable: The resolved path of the tool.
    :return: ``True`` when its ``--version`` names GNU; ``False`` when it cannot run.
    """
    try:
        probe = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    return probe.returncode == 0 and "GNU" in probe.stdout


def require_gnu(tool: str, path: str | None = None) -> None:
    """Skip the running case unless ``tool`` on ``path`` is the GNU one.

    :param tool: The tool's name, or the path of the executable the case runs.
    :param path: The ``PATH`` the case resolves ``tool`` on; the process's own
        when ``None``.
    """
    executable = shutil.which(tool, path=path)
    if executable is None or not _is_gnu(os.path.realpath(executable)):
        skip_unless_ci(f"needs GNU {Path(tool).name}, which this host does not run")


def require_file_name(directory: Path, name: str) -> None:
    """Skip the running case unless the file system under ``directory`` takes ``name``.

    APFS, for one, refuses a name that is not valid UTF-8.

    :param directory: An existing directory on the file system the case writes to.
    :param name: The file name the case creates.
    """
    with tempfile.TemporaryDirectory(dir=directory) as probe_dir:
        try:
            (Path(probe_dir) / name).touch()
        except OSError as error:
            if error.errno != errno.EILSEQ:
                raise
            skip_unless_ci(f"the file system under {directory} refuses {name!r}")
