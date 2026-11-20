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
the port to learn that it did. The form is the only place that says so before
the restore runs, so each fact is pinned on the field that carries it, and the
command is read from the restore script rather than repeated here.
"""

import types

from app.extensions.apps.framework.form_dsl.derivation import derive_form_sections
from app.extensions.apps.mysql_backups.restore.models import RestoreCreate
from app.extensions.apps.mysql_backups.restore.views import restore_views
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_method,
    Recorder,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_PAYLOAD_PATH,
)


def _description(section_title: str, field_name: str) -> str:
    """Return the help text the form schema serves for one field.

    :param section_title: The title of the form section holding the field.
    :param field_name: The field to read.
    :return: The field's description, as the schema renders it.
    """
    sections = derive_form_sections(RestoreCreate, restore_views.layout)
    fields = {
        field.name: field
        for section in sections
        if section.title == section_title
        for field in section.fields
    }
    description = fields[field_name].description
    assert description is not None
    return description


def _commands_run_for(action: str, init_command: str | None) -> list[str]:
    """Return the commands the restore script runs for one MySQL action.

    The script's own ``signal_mysqld`` builds them. The commands are recorded
    instead of run, and MySQL refuses the probe after a stop and answers it
    after a start, so neither wait is entered.

    :param action: The action the script requests: ``stop`` or ``start``.
    :param init_command: The value of 'Custom MySQL init command', if any.
    :return: Each command the script ran, as one line.
    """
    commands: list[str] = []

    def popen(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
        commands.append(" ".join(command))
        return types.SimpleNamespace(returncode=0, stderr="", wait=lambda: 0)

    def query(_sql: str) -> None:
        if action == "stop":
            raise ConnectionRefusedError

    signal_mysqld = payload_method(
        "Xtrabackup",
        "signal_mysqld",
        extra_namespace={
            "OperationalError": ConnectionRefusedError,
            "subprocess": types.SimpleNamespace(Popen=popen, PIPE=-1),
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


def test_data_directory_says_the_restore_stops_mysql_on_the_execution_host() -> None:
    """Pin the stop on the one field every XtraBackup restore fills in."""
    description = _description("XtraBackup", "datadir")

    assert "stops MySQL on the execution host" in description
    assert "starts it again" in description


def test_init_command_names_the_commands_run_when_it_is_left_empty() -> None:
    """Pin the default stop and start commands to the ones the script runs."""
    description = _description("General", "custom_mysql_init_command")

    for action in ("stop", "start"):
        (command,) = _commands_run_for(action, None)
        assert command in description


def test_init_command_shows_how_a_custom_command_is_run() -> None:
    """Pin the example of a custom command to what the script runs for it."""
    description = _description("General", "custom_mysql_init_command")
    custom = "/etc/init.d/mysql-3307"

    assert _commands_run_for("stop", custom) == [f"{custom} stop"]
    assert f"{custom} runs as {custom} stop" in description


def test_init_command_says_systemctl_always_acts_on_the_mysql_unit() -> None:
    """Pin the one custom command the script adds a unit name to."""
    description = _description("General", "custom_mysql_init_command")
    systemctl = "/usr/bin/systemctl"

    assert _commands_run_for("stop", systemctl) == [f"{systemctl} stop mysql"]
    assert f"For {systemctl} it also adds the unit name mysql" in description


def test_port_says_it_must_be_the_port_of_the_server_the_init_command_controls() -> (
    None
):
    """Pin what the port is used for and which server it has to belong to."""
    description = _description("General", "port")

    assert "check that MySQL stopped and started" in description
    assert "replication statements" in description
    assert (
        "port of the server that the init command below stops and starts" in description
    )


def test_port_is_declared_above_the_init_command_it_points_to() -> None:
    """Pin the field order the port's help text relies on when it says 'below'."""
    sections = derive_form_sections(RestoreCreate, restore_views.layout)
    general = next(section for section in sections if section.title == "General")
    names = [field.name for field in general.fields]

    assert names.index("port") < names.index("custom_mysql_init_command")
