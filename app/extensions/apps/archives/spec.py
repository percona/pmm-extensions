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

"""Build the ``run-python`` pt-archiver spec for the Archives app.

:func:`build_archives_spec` is the pure ``(form, resolved) -> RunPythonSpec``
builder fed to the framework's three-phase create path (and reused by the legacy
Jinja form path via ``deps``), so an archive task's payload is byte-identical
regardless of the call origin. It serialises the kept
``PurgeConfig`` / ``PurgeConfigAll`` / ``PurgeConfigItem`` validators to the YAML
config, sourcing the schema / table / destination names from the entities
``resolve_refs`` already fetched (falling back to the free-typed value for an
``allow_custom`` selection). The framework's ``assemble_envelope`` supplies the
executor ``target``, ``_service_name``, and the connectivity meta around this
spec; ``extra_meta`` carries the source node name as ``_pmm_node_name``.
"""

from typing import Any

import yaml

from app.core.exceptions import HTTPUnprocessableEntityException
from app.core.utils.path import payload_uri
from app.extensions.apps.archives.models import (
    ArchivesCreate,
    DestByFile,
    DestByTable,
    HostByService,
    HostManual,
    PurgeConfig,
    PurgeConfigAll,
    PurgeConfigItem,
    SourceByQuery,
    SourceByTable,
)
from app.extensions.apps.framework.spec import ResolvedEntities, RunPythonSpec
from app.inventory.constants import DEFAULT_MYSQL_PORT

_REQUIREMENTS = "PyMySQL[rsa,ed25519]\nfilelock\nPyYAML"


def _flag_to_int(*, value: bool | None) -> int | None:
    """Map a tri-state flag to the integer the archiver config expects.

    :param value: ``True`` / ``False`` / ``None`` from a checkbox field.
    :return: ``1`` / ``0`` / ``None`` — ``None`` is dropped by ``exclude_none``.
    """
    return None if value is None else int(value)


def _resolved_name(
    resolved: ResolvedEntities, key: str, raw: int | str | None
) -> str | None:
    """Return the resolved entity name, falling back to a free-typed value.

    :param resolved: The entities ``resolve_refs`` fetched, keyed by dotted name.
    :param key: The nested reference field's dotted key (e.g. ``source.source_db``).
    :param raw: The submitted value — an inventory id (resolved) or free-typed name.
    :return: The resolved entity's name, the free-typed string, or ``None``.
    """
    entity = resolved.entities.get(key)
    if entity is not None:
        return entity.name
    return str(raw) if isinstance(raw, str) and raw else None


def _assert_not_self_archive(
    purge_item: dict[str, Any], source_host: str, source_port: int
) -> None:
    """Reject a destination that resolves to the same host, port, schema, and table.

    Skips the check when there is no destination table (file destination or a
    delete-only run) or no resolved source table (the source-query path); the
    destination host / schema fall back to the source at execution time, so a
    self-archive is only flagged when host, port, schema, and table all match.

    :param purge_item: The assembled ``PurgeConfigItem`` fields.
    :param source_host: The source service node address.
    :param source_port: The source service port.
    :raises HTTPUnprocessableEntityException: When source and destination are the
        same table.
    """
    if not purge_item.get("dest_table") or not purge_item.get("source_table"):
        return
    effective_dest_host = purge_item.get("dest_host") or source_host
    effective_dest_port = purge_item.get("dest_port") or source_port
    effective_dest_db = purge_item.get("dest_db") or purge_item.get("source_db")
    if (
        effective_dest_host == source_host
        and effective_dest_port == source_port
        and (effective_dest_db or "") == (purge_item.get("source_db") or "")
        and purge_item["dest_table"] == purge_item["source_table"]
    ):
        raise HTTPUnprocessableEntityException(
            detail="Source and Destination tables cannot be the same."
        )


def build_archives_spec(
    form: ArchivesCreate, resolved: ResolvedEntities
) -> RunPythonSpec:
    """Build the ``run-python`` pt-archiver spec from the validated form.

    Assemble a single ``PurgeConfigItem`` from the form's options plus the
    resolved source / destination names, guard against a self-archive, and
    serialise the ``PurgeConfig`` YAML. The source service (the
    ``check_connectivity`` primary) supplies ``SOURCE_HOST`` / ``SOURCE_PORT`` and
    the ``_pmm_node_name`` extra meta.

    :param form: The validated create form (an ``ArchivesCreate``).
    :param resolved: The entities resolved from the form's reference fields; the
        primary service is the source MySQL host.
    :return: The run-python spec consumed by ``assemble_envelope``.
    :raises ValueError: When no source service resolved (the source ``service_id``
        is required and ``check_connectivity``, so this only fires on misuse).
    :raises HTTPUnprocessableEntityException: When source and destination tables
        resolve to the same identity.
    """
    service = resolved.service
    if service is None:
        raise ValueError(
            "build_archives_spec requires a resolved source service; the source "
            "service_id is required and check_connectivity"
        )
    source_port = service.port or DEFAULT_MYSQL_PORT

    purge_item: dict[str, Any] = {
        "alias": form.task_name,
        "swap_drop": form.swap_drop,
        "where": form.where,
        "swp_table_suffix": form.swp_table_suffix,
        "use_index": form.use_index,
        "extra_args": form.extra_args,
        "limit": form.limit,
        "sleep": form.sleep,
        "disable_binlog": _flag_to_int(value=form.disable_binlog),
        "disable_bulk_insert": _flag_to_int(value=form.disable_bulk_insert),
        "delete_data": _flag_to_int(value=form.delete_data),
    }

    source = form.source
    if isinstance(source, SourceByQuery):
        purge_item["source_query"] = source.source_query
    elif isinstance(source, SourceByTable):
        purge_item["source_db"] = _resolved_name(
            resolved, "source.source_db", source.source_db
        )
        purge_item["source_table"] = _resolved_name(
            resolved, "source.source_table", source.source_table
        )

    destination = form.destination
    if isinstance(destination, DestByFile):
        purge_item["dest_file"] = destination.dest_file
    elif isinstance(destination, DestByTable):
        purge_item["dest_table"] = _resolved_name(
            resolved, "destination.dest_table", destination.dest_table
        )
        dest_db = _resolved_name(resolved, "destination.dest_db", destination.dest_db)
        if dest_db is not None:
            purge_item["dest_db"] = dest_db

    host = form.host
    if isinstance(host, HostByService):
        dest_service = resolved.entities.get("host.dest_service")
        if dest_service is not None:
            purge_item["dest_host"] = dest_service.node.address
            purge_item["dest_port"] = dest_service.port or DEFAULT_MYSQL_PORT
    elif isinstance(host, HostManual):
        purge_item["dest_host"] = host.dest_host
        purge_item["dest_port"] = host.dest_port or DEFAULT_MYSQL_PORT

    _assert_not_self_archive(purge_item, service.node.address, source_port)

    purge_config = PurgeConfig(
        all=PurgeConfigAll(source_host=service.node.address, source_port=source_port),
        purge_list=[PurgeConfigItem.model_validate(purge_item)],
    )
    return RunPythonSpec(
        config=yaml.dump(
            purge_config.model_dump(mode="json", by_alias=True, exclude_none=True)
        ),
        requirements=_REQUIREMENTS,
        payload=payload_uri(__file__, "payload"),
        extra_meta={"_pmm_node_name": service.node.name},
    )
