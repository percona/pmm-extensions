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

"""Parse scan host output into classified facts over sample command output.

The parsers are pure, so they are exercised from captured text rather than a live
host: the systemd and PBM output a scan would collect, and the classes every fact is
given before ingestion.
"""

import json

import pytest

from app.extensions.apps.om_inventory.scan_facts import (
    collect_scan_facts,
    parse_systemd_show,
    pbm_facts,
    systemd_unit_facts,
)
from app.extensions.apps.shared.om.classification import FactClass

#: A ``pbm status --out json`` document for a configured, idle, two-agent cluster.
_PBM_CONFIGURED = json.dumps(
    {
        "backups": {"storage": {"type": "s3"}},
        "nodes": [{"host": "rs0/a:27017"}, {"host": "rs0/b:27017"}],
        "running": {},
    }
)


def test_parse_systemd_show_reads_key_value_lines() -> None:
    """Split each line on its first ``=``, keep empty values, skip a line with none."""
    output = (
        "ActiveState=active\nSubState=running\nUnitFileState=\nMainPID=1=2\ngarbage"
    )
    assert parse_systemd_show(output) == {
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "",
        "MainPID": "1=2",
    }


def test_systemd_unit_facts_key_under_the_unit() -> None:
    """Key a unit's state facts under the unit so two units stay apart."""
    output = (
        "ActiveState=active\nSubState=running\nUnitFileState=enabled\nLoadState=loaded"
    )
    assert systemd_unit_facts("mongod.service", output) == {
        "unit.mongod.service.active_state": "active",
        "unit.mongod.service.sub_state": "running",
        "unit.mongod.service.unit_file_state": "enabled",
        "unit.mongod.service.load_state": "loaded",
    }


def test_systemd_unit_facts_absent_property_is_none() -> None:
    """Report a property the output did not carry as ``None``, not a default."""
    facts = systemd_unit_facts("pbm-agent.service", "ActiveState=inactive")
    assert facts["unit.pbm-agent.service.active_state"] == "inactive"
    assert facts["unit.pbm-agent.service.sub_state"] is None


def test_pbm_facts_read_state_without_credentials() -> None:
    """Read storage kind and counts, never the storage connection details."""
    assert pbm_facts(_PBM_CONFIGURED) == {
        "pbm.configured": True,
        "pbm.storage_type": "s3",
        "pbm.agent_count": 2,
        "pbm.backup_running": False,
    }


def test_pbm_facts_unconfigured() -> None:
    """Report an unconfigured PBM as not configured, with no storage type."""
    facts = pbm_facts(json.dumps({"backups": {}, "nodes": []}))
    assert facts["pbm.configured"] is False
    assert facts["pbm.storage_type"] is None
    assert facts["pbm.agent_count"] == 0


def test_pbm_facts_backup_running() -> None:
    """Read a backup in progress as running."""
    output = json.dumps(
        {
            "backups": {"storage": {"type": "filesystem"}},
            "running": {"status": "running"},
        }
    )
    assert pbm_facts(output)["pbm.backup_running"] is True


def test_pbm_facts_rejects_non_json() -> None:
    """Raise on output that is not the JSON document the command promises."""
    with pytest.raises(json.JSONDecodeError):
        pbm_facts("pbm: not configured")


def test_collect_scan_facts_classes_cover_every_fact() -> None:
    """Collect systemd and PBM facts, classing each public so none is dropped."""
    facts, classes = collect_scan_facts(
        systemd_units={"mongod.service": "ActiveState=active"},
        pbm_status=_PBM_CONFIGURED,
    )
    assert facts["unit.mongod.service.active_state"] == "active"
    assert facts["pbm.storage_type"] == "s3"
    assert set(classes) == set(facts)
    assert all(cls is FactClass.PUBLIC for cls in classes.values())


def test_collect_scan_facts_without_pbm() -> None:
    """Skip PBM facts when the scan did not read PBM."""
    facts, _ = collect_scan_facts(
        systemd_units={"mongod.service": "ActiveState=active"}, pbm_status=None
    )
    assert not any(key.startswith("pbm.") for key in facts)


def test_collect_scan_facts_folds_probe_facts_and_classes() -> None:
    """Fold the estate payload's facts in, keeping each one's declared class."""
    facts, classes = collect_scan_facts(
        systemd_units={},
        pbm_status=None,
        probe_facts={
            "facts": {"installed_version": "7.0.14", "password": "secret"},
            "classes": {
                "installed_version": FactClass.PUBLIC,
                "password": FactClass.SECRET,
            },
        },
    )
    assert facts["installed_version"] == "7.0.14"
    assert classes["password"] is FactClass.SECRET
