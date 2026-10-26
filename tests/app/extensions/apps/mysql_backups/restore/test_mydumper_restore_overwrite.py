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

"""Pin the myloader option the Mydumper restore payload passes for Overwrite tables.

myloader renamed ``--overwrite-tables`` to ``--drop-table``, and a release that
knows only one spelling rejects the other, so the restore fails at option
parsing. ``-o`` is the short form both spellings share.
"""

import pathlib
import types

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_instance,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_MYDUMPER_PAYLOAD_PATH,
)


class _FinishedProc:
    """Stand in for a myloader process that printed nothing and exited 0."""

    stdout: tuple[bytes, ...] = ()
    returncode = 0

    def wait(self) -> int:
        """Return the exit code, as ``Popen.wait`` does.

        :return: The process's exit code.
        """
        return self.returncode


def _myloader_command(tmp_path: pathlib.Path, *, overwrite_tables: bool) -> list[str]:
    """Run the payload's ``_run_myloader`` and return the command it launched.

    :param tmp_path: Directory for the myloader log file the method writes.
    :param overwrite_tables: The restore's Overwrite tables setting.
    :return: The argv handed to ``subprocess.Popen``.
    """
    launched: list[list[str]] = []

    def popen(cmd: list[str], **_kwargs: object) -> _FinishedProc:
        launched.append(list(cmd))
        return _FinishedProc()

    fake_subprocess = types.SimpleNamespace(PIPE=-1, STDOUT=-2, Popen=popen)
    inst, _, _ = payload_instance(
        ("_run_myloader",),
        payload_path=RESTORE_MYDUMPER_PAYLOAD_PATH,
        extra_namespace={
            "subprocess": fake_subprocess,
            "MYLOADER_BIN": "/usr/bin/myloader",
        },
    )
    inst.defaults_cnf_file = "/root/.my.cnf"
    inst.dest_host = "localhost"
    inst.dest_port = 3306
    inst.local_path = str(tmp_path / "stage")
    inst.myloader_threads = 4
    inst.overwrite_tables = overwrite_tables
    inst.database = None
    inst.myloader_extra_args = None
    inst.ml_log_file = str(tmp_path / "myloader.log")

    inst._run_myloader()

    assert len(launched) == 1
    return launched[0]


@pytest.mark.parametrize("overwrite_tables", [True, False])
def test_overwrite_tables_never_passes_the_long_option(tmp_path, overwrite_tables):
    """Assert neither long spelling reaches myloader, whatever the setting."""
    cmd = _myloader_command(tmp_path, overwrite_tables=overwrite_tables)

    assert cmd[0] == "/usr/bin/myloader"
    assert "--overwrite-tables" not in cmd
    assert not any(arg.startswith("--drop-table") for arg in cmd)


def test_overwrite_tables_passes_the_short_option(tmp_path):
    """Assert Overwrite tables passes ``-o``, which every myloader accepts."""
    assert "-o" in _myloader_command(tmp_path, overwrite_tables=True)


def test_no_overwrite_passes_no_drop_option(tmp_path):
    """Assert a restore without Overwrite tables keeps myloader's fail-on-exists."""
    cmd = _myloader_command(tmp_path, overwrite_tables=False)

    assert cmd[0] == "/usr/bin/myloader"
    assert "-o" not in cmd
