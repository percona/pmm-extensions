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

"""Pin what the Restores form says about an XtraBackup restore stopping MySQL.

An XtraBackup restore stops and starts MySQL on the execution host, and reads
the port to learn that it did. The form's schema snapshot pins the wording;
these tests pin the facts in it that come from somewhere else: the commands and
the error the restore script produces, and the labels and order of the fields
the help text points to.
"""

import re
import types

import pytest

from app.extensions.apps.framework.form_dsl.derivation import derive_form_sections
from app.extensions.apps.framework.schema import AnyField
from app.extensions.apps.mysql_backups.restore.models import RestoreCreate
from app.extensions.apps.mysql_backups.restore.views import restore_views
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_method,
    Recorder,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_PAYLOAD_PATH,
)


class _StopFailedError(Exception):
    """Stand in for the script's ``BackupError``, which a failed stop raises."""


def _fields(section_title: str) -> dict[str, AnyField]:
    """Return the fields the form schema serves in one section, by name.

    :param section_title: The title of the form section.
    :return: The section's fields, keyed by field name.
    """
    sections = derive_form_sections(RestoreCreate, restore_views.layout)
    return {
        field.name: field
        for section in sections
        if section.title == section_title
        for field in section.fields
    }


def _description(section_title: str, field_name: str) -> str:
    """Return the help text the form schema serves for one field.

    :param section_title: The title of the form section holding the field.
    :param field_name: The field to read.
    :return: The field's description, as the schema renders it.
    """
    description = _fields(section_title)[field_name].description
    assert description is not None
    return description


def _names_command(text: str, command: str) -> bool:
    """Report whether ``text`` names ``command`` whole, not as part of a longer one.

    :param text: The help text to search.
    :param command: The command line, as the script runs it.
    :return: Whether the command appears bounded by whitespace or punctuation.
    """
    return re.search(rf"(?<!\S){re.escape(command)}(?=[\s.,;]|$)", text) is not None


def _run_signal_mysqld(
    action: str, init_command: str | None, *, mysql_answers: bool
) -> list[str]:
    """Run the restore script's own ``signal_mysqld`` for one MySQL action.

    The commands are recorded instead of run, and the waits between probes are
    skipped.

    :param action: The action the script requests: ``stop`` or ``start``.
    :param init_command: The value of 'Custom MySQL init command', if any.
    :param mysql_answers: Whether MySQL answers every probe; when it does not,
        it refuses every probe.
    :return: Each command the script ran, as one line.
    :raises _StopFailedError: When MySQL still answers after the whole wait for
        a stop.
    """
    commands: list[str] = []

    def popen(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
        commands.append(" ".join(command))
        return types.SimpleNamespace(returncode=0, stderr="", wait=lambda: 0)

    def query(_sql: str) -> None:
        if not mysql_answers:
            raise ConnectionRefusedError

    signal_mysqld = payload_method(
        "Xtrabackup",
        "signal_mysqld",
        extra_namespace={
            "BackupError": _StopFailedError,
            "OperationalError": ConnectionRefusedError,
            "subprocess": types.SimpleNamespace(Popen=popen, PIPE=-1),
            "time": types.SimpleNamespace(sleep=lambda _seconds: None),
        },
        payload_path=RESTORE_PAYLOAD_PATH,
    )
    restore = types.SimpleNamespace(
        custom_mysql_init_command=init_command,
        port=3306,
        defaults_cnf_file="/home/backupuser/.my.cnf",
        logger=Recorder(),
        _query_mysql=query,
    )
    signal_mysqld(restore, action)
    return commands


def _commands_run_for(action: str, init_command: str | None) -> list[str]:
    """Return the commands the restore script runs for one MySQL action.

    MySQL refuses the probe after a stop and answers it after a start, so
    neither wait is entered.

    :param action: The action the script requests: ``stop`` or ``start``.
    :param init_command: The value of 'Custom MySQL init command', if any.
    :return: Each command the script ran, as one line.
    """
    return _run_signal_mysqld(action, init_command, mysql_answers=action == "start")


def test_data_directory_cites_the_labels_of_the_fields_that_choose_the_server() -> None:
    """Pin the two fields the data directory's help sends the operator to."""
    description = _description("XtraBackup", "datadir")
    general = _fields("General")

    assert f"'{general['custom_mysql_init_command'].label}'" in description
    assert f"'{general['port'].label}'" in description


@pytest.mark.parametrize("action", ["stop", "start"])
def test_init_command_names_the_default_command_for_each_action(action: str) -> None:
    """Pin each default command to the one the script runs when the field is empty."""
    (command,) = _commands_run_for(action, None)

    assert _names_command(_description("General", "custom_mysql_init_command"), command)


def test_init_command_shows_how_a_custom_command_is_run() -> None:
    """Pin the example of a custom command to what the script runs for it."""
    custom = "/etc/init.d/mysql-3307"
    (command,) = _commands_run_for("stop", custom)

    assert command == f"{custom} stop"
    assert _names_command(_description("General", "custom_mysql_init_command"), command)


def test_a_custom_systemctl_command_gets_the_mysql_unit() -> None:
    """Pin the unit name the script adds to the one custom command it extends."""
    systemctl = "/usr/bin/systemctl"

    assert _commands_run_for("stop", systemctl) == [f"{systemctl} stop mysql"]


def test_port_names_the_error_the_script_raises_when_mysql_keeps_answering() -> None:
    """Pin the error the port's help text names to the one a failed stop raises."""
    with pytest.raises(_StopFailedError) as failure:
        _run_signal_mysqld("stop", "/etc/init.d/mysql-3307", mysql_answers=True)
    error, _, attempts = str(failure.value).rpartition(" in ")

    assert attempts.isdigit()
    assert error in _description("General", "port")


def test_port_is_declared_above_the_init_command_it_points_to() -> None:
    """Pin the field order the port's help text relies on when it says 'below'."""
    names = list(_fields("General"))

    assert names.index("port") < names.index("custom_mysql_init_command")
