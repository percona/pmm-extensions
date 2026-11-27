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

"""Parse a scan's raw host output into classified observation facts.

A scan profile runs commands on a database host and captures their raw output; this
module turns that output into facts keyed and classified for ``ingest_observation``.
It follows the capability module's split: the host runs the command, and the parse
happens here from the captured text, so each parser is pure and tested over sample
output rather than a live host.

Two fact sources live here because the estate payload
(:mod:`~app.extensions.apps.om_inventory.payload.probe`) does not carry them: the
systemd unit state of a mongod or a PBM agent, and PBM's own status. The replica-set,
version and install-readiness facts a scan also needs are already collected by that
payload and are composed in when the scan dispatch runs them; see
:func:`collect_scan_facts`.

Every fact here is public: a unit's state and PBM's storage kind name what is running,
never a credential. The storage connection string PBM holds is never read, only the
storage type and the agent and backup counts.
"""

import json
from collections.abc import Mapping
from typing import Any

from app.extensions.apps.shared.om.classification import FactClass

__all__ = [
    "SYSTEMD_PROPERTIES",
    "collect_scan_facts",
    "parse_systemd_show",
    "pbm_facts",
    "systemd_unit_facts",
]

#: The ``systemctl show`` properties a scan reads for a unit, in one query.
SYSTEMD_PROPERTIES = ("ActiveState", "SubState", "UnitFileState", "LoadState")


def parse_systemd_show(output: str) -> dict[str, str]:
    """Read ``systemctl show`` ``key=value`` lines into a mapping.

    ``systemctl show`` prints one ``Property=value`` per line, a value may be empty,
    and a value may itself contain ``=``; only the first ``=`` splits. A line with no
    ``=`` is skipped.

    :param output: The command's captured standard output.
    :return: The properties by name, values verbatim.
    """
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            properties[key] = value
    return properties


def systemd_unit_facts(unit: str, output: str) -> dict[str, Any]:
    """Return one unit's state facts from its ``systemctl show`` output.

    The facts are keyed under the unit so a host carrying both a mongod unit and a
    PBM agent unit keeps them apart. A property the output did not carry is ``None``,
    which reads as "the host did not report it" rather than a guessed default.

    :param unit: The systemd unit the output describes, such as ``mongod.service``.
    :param output: The unit's captured ``systemctl show`` output.
    :return: The unit's ``active_state``, ``sub_state``, ``unit_file_state`` and
        ``load_state``, keyed ``unit.<unit>.<property>``.
    """
    properties = parse_systemd_show(output)
    prefix = f"unit.{unit}"
    return {
        f"{prefix}.active_state": properties.get("ActiveState"),
        f"{prefix}.sub_state": properties.get("SubState"),
        f"{prefix}.unit_file_state": properties.get("UnitFileState"),
        f"{prefix}.load_state": properties.get("LoadState"),
    }


def pbm_facts(output: str) -> dict[str, Any]:
    """Return PBM state facts from ``pbm status --out json`` output.

    Reads only what names what is running: whether a storage is configured and its
    kind, how many agents answered, and whether a backup is in progress. The storage
    connection details PBM holds are never read, so no credential is carried.

    :param output: The captured ``pbm status --out json`` standard output.
    :return: ``pbm.configured``, ``pbm.storage_type``, ``pbm.agent_count`` and
        ``pbm.backup_running``; a field PBM did not report is ``None`` or ``0``.
    :raises json.JSONDecodeError: When the output is not the JSON document
        ``pbm status --out json`` promises.
    """
    status = json.loads(output)
    backups = status.get("backups") or {}
    storage = backups.get("storage") or {}
    nodes = status.get("nodes") or []
    running = status.get("running") or {}
    return {
        "pbm.configured": bool(storage),
        "pbm.storage_type": storage.get("type"),
        "pbm.agent_count": len(nodes),
        "pbm.backup_running": bool(running.get("status")),
    }


def collect_scan_facts(
    *,
    systemd_units: Mapping[str, str],
    pbm_status: str | None,
    probe_facts: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, FactClass]]:
    """Compose a scan's facts and their classes from its captured host output.

    The systemd and PBM facts are parsed here; ``probe_facts`` carries the replica-set,
    version and install-readiness facts the estate payload already produced, folded in
    unchanged so a scan stores one document. Every fact collected here is public, so
    its class is :attr:`FactClass.PUBLIC`; a fact in ``probe_facts`` keeps whatever
    class its own source declared, and one with no class is dropped as secret by
    ``ingest_observation``.

    :param systemd_units: Each unit's captured ``systemctl show`` output, by unit name.
    :param pbm_status: The captured ``pbm status --out json`` output, or ``None`` when
        the scan did not read PBM.
    :param probe_facts: Facts and classes already produced by the estate payload, as a
        mapping carrying ``facts`` and ``classes``; omitted when the scan collected
        none.
    :return: The facts and the class of each, ready for ``ingest_observation``.
    :raises json.JSONDecodeError: When ``pbm_status`` is not valid JSON.
    """
    facts: dict[str, Any] = {}
    for unit, output in systemd_units.items():
        facts.update(systemd_unit_facts(unit, output))
    if pbm_status is not None:
        facts.update(pbm_facts(pbm_status))
    classes: dict[str, FactClass] = dict.fromkeys(facts, FactClass.PUBLIC)

    if probe_facts:
        facts.update(probe_facts.get("facts") or {})
        classes.update(probe_facts.get("classes") or {})
    return facts, classes
