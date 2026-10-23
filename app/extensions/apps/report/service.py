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

"""Orchestrate report data collection from PMM.

This module replaces the legacy GAS ``report.py`` CLI by delegating all HTTP
calls to :class:`~app.extensions.clients.pmm.PMMRemoteAPI` and returning structured
:mod:`~app.extensions.apps.report.models` objects.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any, TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError
from weasyprint import CSS, HTML

from app.core.config import settings
from app.core.exceptions import HTTPServiceUnavailableException
from app.core.requests import RemoteAPI
from app.extensions.apps.report.config import (
    health_report_settings,
    HealthReportSettings,
)
from app.extensions.bundle_upload.factory import split_endpoint
from app.extensions.bundle_upload.plan import (
    DeliveryPlan,
    DeliveryPlanError,
    DeliveryPlanExecutor,
    LiteralValue,
    ManifestValue,
    SecretValue,
    UploadStep,
)
from app.extensions.bundle_upload.seam import BundleSource
from app.extensions.clients.pmm import PMMRemoteAPI
from app.extensions.utils.jinja import DEFAULT_FILTERS

if TYPE_CHECKING:
    from collections.abc import Mapping

from .models import (
    AdvisorCheck,
    AdvisorFamily,
    AdvisorSection,
    AlertEntry,
    AlertSection,
    BackupEntry,
    BackupSection,
    BackupStatus,
    DiskUsageEntry,
    FailedCheck,
    InventorySection,
    InventoryServiceEntry,
    MonitoredSummary,
    REPORT_SECTION_LABELS,
    REPORT_SECTIONS,
    ReportData,
    ReportMetadata,
    ServiceStatus,
    StorageSection,
    UptimeEntry,
    UptimeSection,
)

logger = logging.getLogger(__name__)

SERVICE_NAMES: dict[str, str] = {
    "agent": "Agent",
    "backup": "Backup",
    "external": "External",
    "generic": "Generic",
    "haproxy": "HAProxy",
    "mongodb": "MongoDB",
    "mysql": "MySQL",
    "remote": "Remote",
    "postgresql": "PostgreSQL",
    "proxysql": "ProxySQL",
}

_ALERT_LABEL_KEYS = frozenset(
    {
        "alertname",
        "environment",
        "grafana_folder",
        "node_id",
        "node_name",
        "service",
        "service_id",
        "service_type",
        "severity",
        "template_name",
    }
)
_ALERT_PATTERN = re.compile(r"\{(.+)\}")

_EXCLUDED_FS = "rootfs|selinuxfs|autofs|rpc_pipefs|tmpfs"
_PMM_SERVER_FILTER = {"pmm-server", "pmm-server-postgresql"}
_MIN_FRAME_FIELDS = 2
_OTHER_FAMILY_KEY = "other"
_OTHER_FAMILY_DISPLAY = "Other"


def _find_labels(schema_fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract PMM labels from Grafana frame schema fields."""
    for field in schema_fields:
        labels = {
            k: v
            for k, v in field.get("labels", {}).items()
            if field.get("name") != "Time"
        }
        if labels:
            return labels
    return {}


def _interval_ms(start: str, end: str) -> tuple[int, int]:
    """Convert relative time strings (``now-7d``, ``now``) to epoch milliseconds."""
    now_ms = int(time.time() * 1000)

    def _parse(value: str) -> int:
        if value == "now":
            return now_ms
        if value.startswith("now-"):
            offset_str = value[4:]
            if not offset_str or not offset_str[:-1].isdigit():
                raise ValueError(f"Unsupported time format: {value}")
            unit = offset_str[-1]
            multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
            if multiplier is None:
                raise ValueError(f"Unsupported time format: {value}")
            return now_ms - int(offset_str[:-1]) * multiplier * 1000
        raise ValueError(f"Unsupported time format: {value}")

    return _parse(start), _parse(end)


async def _get_metrics_datasource(
    pmm_api: PMMRemoteAPI,
) -> tuple[int, str]:
    """Return (datasource_id, datasource_uid) for the ``Metrics`` datasource."""
    datasources = await pmm_api.get_grafana_datasources()
    for ds in datasources:
        if ds.get("name") == "Metrics":
            return ds["id"], ds["uid"]
    raise LookupError("Metrics datasource not found in Grafana")


# Section collectors
def _parse_failed_checks(
    raw_failed: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    services: dict[str, dict[str, Any]],
) -> dict[str, list[FailedCheck]]:
    """Parse raw failed check results into structured objects grouped by name."""
    failed_by_name: dict[str, list[FailedCheck]] = {}
    for result in raw_failed:
        labels = result.get("labels", {})
        node_info = nodes.get(labels.get("node_id", ""), {})
        svc_info = services.get(labels.get("service_id", ""), {})
        fc = FailedCheck(
            name=result["check_name"],
            description=result.get("description", ""),
            summary=result.get("summary", ""),
            severity=result.get("severity", "SEVERITY_WARNING"),
            node_name=node_info.get("name"),
            node_id=labels.get("node_id"),
            service_name=svc_info.get("name"),
            service_id=labels.get("service_id"),
            read_more_url=result.get("read_more_url", ""),
        )
        failed_by_name.setdefault(fc.name, []).append(fc)
    return failed_by_name


def _build_allowed_check_prefixes(active_service_types: set[str]) -> set[str]:
    """Build the set of allowed check name prefixes from active service types."""
    extra: set[str] = set()
    if "mysql" in active_service_types:
        extra |= {"innodb", "replica"}
    if "mongodb" in active_service_types:
        extra.add("mongo")
    return active_service_types | extra


async def _refresh_single_check(
    pmm_api: PMMRemoteAPI,
    name: str,
) -> str | None:
    """Refresh one check, returning its name on failure or ``None`` on success."""
    try:
        await pmm_api.start_advisor_checks(names=[name])
    except (KeyError, IndexError, OSError):
        logger.warning("Refresh timeout for check %s", name)
        return name
    return None


async def _refresh_checks(
    pmm_api: PMMRemoteAPI,
    raw_checks: list[dict[str, Any]],
) -> list[str]:
    """Trigger a refresh for each enabled check and return names that timed out."""
    enabled = [raw["name"] for raw in raw_checks if not raw.get("disabled")]
    if not enabled:
        return []
    results = await asyncio.gather(
        *(_refresh_single_check(pmm_api, name) for name in enabled)
    )
    return [name for name in results if name is not None]


async def collect_advisors(
    pmm_api: PMMRemoteAPI,
    active_service_types: set[str],
    nodes: dict[str, dict[str, Any]],
    services: dict[str, dict[str, Any]],
    *,
    refresh: bool = False,
) -> AdvisorSection:
    """Fetch advisor checks and failed results from PMM.

    :param pmm_api: PMM API client.
    :param active_service_types: Set of service type keys present in the inventory
        (e.g. ``{"mysql", "mongodb"}``).
    :param nodes: Mapping of ``node_id`` to node info dicts.
    :param services: Mapping of ``service_id`` to service info dicts.
    :param refresh: When ``True``, trigger a refresh of each check before
        fetching failed results.
    :return: Populated advisor section.
    """
    raw_checks = await pmm_api.get_advisor_checks()
    refresh_issues = await _refresh_checks(pmm_api, raw_checks) if refresh else []
    raw_failed = await pmm_api.get_failed_advisor_checks()
    allowed = _build_allowed_check_prefixes(active_service_types)

    families: dict[str, AdvisorFamily] = {}
    checks: list[AdvisorCheck] = []

    for raw in raw_checks:
        if raw.get("disabled"):
            continue
        family = raw.get("family")
        family_suffix = family.split("_")[-1] if family else ""
        if family:
            if family_suffix.lower() not in active_service_types:
                continue
        elif raw["name"].split("_")[0].lower() not in allowed:
            continue

        check = AdvisorCheck(
            name=raw["name"],
            description=raw.get("description", ""),
            summary=raw.get("summary", ""),
            family=family,
        )
        checks.append(check)

        if family:
            if family not in families:
                display = SERVICE_NAMES.get(
                    family_suffix.lower(),
                    family_suffix.capitalize(),
                )
                families[family] = AdvisorFamily(
                    family_key=family, display_name=display
                )
            families[family].checks.append(check)
        else:
            if _OTHER_FAMILY_KEY not in families:
                families[_OTHER_FAMILY_KEY] = AdvisorFamily(
                    family_key=_OTHER_FAMILY_KEY, display_name=_OTHER_FAMILY_DISPLAY
                )
            families[_OTHER_FAMILY_KEY].checks.append(check)

    checks.sort(key=lambda c: c.name)
    failed_by_name = _parse_failed_checks(raw_failed, nodes, services)

    for fam in families.values():
        check_names = {c.name for c in fam.checks}
        fam.failed = {k: v for k, v in failed_by_name.items() if k in check_names}

    return AdvisorSection(
        total_checks=len(checks),
        total_failed=len(failed_by_name),
        refresh_issues=refresh_issues,
        families=list(families.values()),
    )


async def collect_alerts(
    pmm_api: PMMRemoteAPI,
    start_ts: int,
    stop_ts: int,
) -> AlertSection:
    """Fetch alert annotations from Grafana and aggregate them.

    :param pmm_api: PMM API client.
    :param start_ts: Start of the report period (epoch ms).
    :param stop_ts: End of the report period (epoch ms).
    :return: Populated alert section.
    """
    raw = await pmm_api.get_grafana_annotations(from_ts=start_ts, to_ts=stop_ts)

    alerts: list[AlertEntry] = []
    per_service: dict[str, int] = defaultdict(int)
    per_rule: dict[str, int] = defaultdict(int)
    per_host: dict[str, int] = defaultdict(int)
    daily: dict[str, int] = defaultdict(int)
    daily_per_host: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for annotation in raw:
        if annotation.get("newState") != "Alerting":
            continue
        matches = _ALERT_PATTERN.findall(annotation.get("text", ""))
        if not matches:
            continue
        data: dict[str, Any] = dict.fromkeys(_ALERT_LABEL_KEYS, "")
        data.update(
            {
                parts[0]: parts[1]
                for parts in (pair.split("=") for pair in matches[0].split(", "))
                if len(parts) == _MIN_FRAME_FIELDS and parts[0] in _ALERT_LABEL_KEYS
            }
        )
        data["id"] = annotation.get("id", 0)
        data["time"] = annotation.get("time", 0)
        entry = AlertEntry(**data)
        alerts.append(entry)

        if not entry.node_name:
            continue

        rule = entry.alertname.rsplit("_", 1)[-1]
        per_rule[rule] += 1
        per_host[entry.node_name] += 1

        svc_key = entry.service_type or entry.service
        if svc_key:
            per_service[svc_key] += 1

        try:
            day = time.strftime("%d %B %Y", time.gmtime(entry.time / 1000))
            daily[day] += 1
            daily_per_host[entry.node_name][day] += 1
        except (OverflowError, OSError):
            logger.warning("Failed to format alert time %s", entry.time)

    alerts.sort(key=lambda a: a.time, reverse=True)
    return AlertSection(
        total_alerts=len(alerts),
        alerts_per_service=dict(per_service),
        alerts_per_rule=dict(per_rule),
        alerts_per_host=dict(per_host),
        alerts_daily=dict(daily),
        alerts_daily_per_host={k: dict(v) for k, v in daily_per_host.items()},
        alert_history=alerts,
    )


async def collect_backups(
    pmm_api: PMMRemoteAPI,
    start_ts: int,
    stop_ts: int,
    ds_id: int,
    ds_uid: str,
) -> BackupSection:
    """Fetch backup metrics from Prometheus via Grafana.

    :param pmm_api: PMM API client.
    :param start_ts: Start of the report period (epoch ms).
    :param stop_ts: End of the report period (epoch ms).
    :param ds_id: Metrics datasource numeric ID.
    :param ds_uid: Metrics datasource UID.
    :return: Populated backup section.
    """
    results = await pmm_api.query_grafana_datasource(
        expressions=[
            '{__name__="msp_backup_status"}',
        ],
        datasource_uid=ds_uid,
        datasource_id=ds_id,
        from_ts=start_ts,
        to_ts=stop_ts,
        max_data_points=10000,
        instant=True,
        range_=True,
    )

    status_frames = results.get("A", {}).get("frames", [])
    by_host: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(
        int,
        {BackupStatus.PASS: 0, BackupStatus.FAIL: 0, BackupStatus.INACTIVE: 0},
    )
    by_type: dict[str, int] = defaultdict(int)
    failed: list[BackupEntry] = []
    all_backups: list[BackupEntry] = []

    status_map = {"-1": "inactive", "0": "pass", "1": "fail"}

    for frame in status_frames:
        labels = _find_labels(frame.get("schema", {}).get("fields", []))
        if not labels:
            continue
        values = frame.get("data", {}).get("values", [])
        if len(values) < _MIN_FRAME_FIELDS:
            continue

        bk_status_str = status_map.get(
            str(int(values[1][0])) if values[1] else "",
            "unknown",
        )
        bk_status = BackupStatus(bk_status_str)
        node_name = labels.get("node_name", "unknown")
        bk_type = labels.get("type", "unknown")
        bk_id = labels.get(
            "backup_id",
            f"gen-{labels.get('alias', '')}-{node_name}-{bk_type}-{values[0][0] if values[0] else 0}",
        )

        raw_start = values[0][0] if values[0] else 0
        raw_end = values[0][-1] if values[0] else 0
        estimated = True
        period: dict[str, Any] = {
            "start": raw_start,
            "end": raw_end,
            "duration": raw_end - raw_start if isinstance(raw_end, int) else 0,
        }

        if {"start_time", "end_time"} <= labels.keys():
            try:
                st = datetime.strptime(
                    labels["start_time"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=UTC)
                et = datetime.strptime(labels["end_time"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
                period.update(
                    start=st.strftime("%d %B %Y at %H:%M:%S UTC"),
                    end=et.strftime("%d %B %Y at %H:%M:%S UTC"),
                    duration=et - st,
                )
                estimated = False
            except ValueError:
                pass
        elif isinstance(raw_start, int) and raw_start > start_ts and raw_end < stop_ts:
            estimated = False

        bk_size = labels.get("backup_size")
        if bk_size is None:
            estimated = True
        elif bk_size:
            estimated = False

        entry = BackupEntry(
            id=bk_id,
            alias=labels.get("alias", ""),
            name=node_name,
            type=bk_type,
            status=bk_status,
            size=bk_size or "0",
            estimated_data=estimated,
            period=period,
        )
        all_backups.append(entry)

        by_host[node_name] += 1
        by_status[bk_status_str] += 1
        by_type[bk_type] += 1
        if bk_status == BackupStatus.FAIL and bk_type != "Binlogs":
            failed.append(entry)

    return BackupSection(
        total_backups=len(all_backups),
        backups_by_host=dict(by_host),
        backups_by_status=dict(by_status),
        backups_by_type=dict(by_type),
        failed_backups=failed,
        all_backups=all_backups,
    )


async def collect_storage(
    pmm_api: PMMRemoteAPI,
    nodes_raw: dict[str, dict[str, Any]],
    since: str,
    until: str,
    ds_id: int,
    ds_uid: str,
) -> StorageSection:
    """Fetch disk usage metrics from Prometheus via Grafana.

    :param pmm_api: PMM API client.
    :param nodes_raw: Mapping of node_id to node info dicts (must contain ``name``).
    :param since: Relative start time (e.g. ``now-7d``).
    :param until: Relative end time (e.g. ``now``).
    :param ds_id: Metrics datasource numeric ID.
    :param ds_uid: Metrics datasource UID.
    :return: Populated storage section.
    """
    if not nodes_raw:
        return StorageSection()

    node_id_to_name: dict[str, str] = {
        nid: info["name"] for nid, info in nodes_raw.items()
    }
    node_filter = "|".join(nodes_raw.keys())
    expr_used = (
        f"avg by (node_id, mountpoint) ("
        f" (max_over_time(node_filesystem_size_bytes{{"
        f'   node_id=~"{node_filter}", fstype!~"{_EXCLUDED_FS}"'
        f"}}[1h])"
        f"  or max_over_time(node_filesystem_size_bytes{{"
        f'   node_id=~"{node_filter}", fstype!~"{_EXCLUDED_FS}"'
        f"}}[5m])"
        f" ) - "
        f" (max_over_time(node_filesystem_free_bytes{{"
        f'   node_id=~"{node_filter}", fstype!~"{_EXCLUDED_FS}"'
        f"}}[1h])"
        f"  or max_over_time(node_filesystem_free_bytes{{"
        f'    node_id=~"{node_filter}", fstype!~"{_EXCLUDED_FS}"'
        f"}}[5m])"
        f" ))"
    )
    expr_total = (
        f"max by (node_id, mountpoint) ( "
        f"  node_filesystem_size_bytes{{"
        f'   node_id=~"{node_filter}",'
        f'    fstype!~"{_EXCLUDED_FS}"}}'
        f")"
    )

    results = await pmm_api.query_grafana_datasource(
        expressions=[expr_used, expr_total],
        datasource_uid=ds_uid,
        datasource_id=ds_id,
        from_ts=since,
        to_ts=until,
        instant=False,
        range_=True,
    )

    used_frames = results.get("A", {}).get("frames", [])
    total_frames = results.get("B", {}).get("frames", [])
    entries: list[DiskUsageEntry] = []

    # Build a lookup for total capacity keyed by (node_id, mountpoint) so that
    # frame ordering differences between refIds don't cause misaligned reads.
    total_lookup: dict[tuple[str, str], int] = {}
    for frame in total_frames:
        t_fields = frame.get("schema", {}).get("fields", [])
        if len(t_fields) < _MIN_FRAME_FIELDS:
            continue
        t_labels = t_fields[1].get("labels", {})
        t_node_id = t_labels.get("node_id", "")
        t_mountpoint = t_labels.get("mountpoint", "")
        if not t_node_id or not t_mountpoint:
            continue
        t_values = frame.get("data", {}).get("values", [])
        if len(t_values) >= _MIN_FRAME_FIELDS and t_values[1]:
            total_lookup[(t_node_id, t_mountpoint)] = t_values[1][0]

    for frame in used_frames:
        fields = frame.get("schema", {}).get("fields", [])
        if len(fields) < _MIN_FRAME_FIELDS:
            continue
        labels = fields[1].get("labels", {})
        node_id = labels.get("node_id", "")
        mountpoint = labels.get("mountpoint", "")
        if not node_id or not mountpoint:
            continue

        values = frame.get("data", {}).get("values", [])
        if len(values) < _MIN_FRAME_FIELDS:
            continue

        total_bytes = total_lookup.get((node_id, mountpoint), 0)

        used_values = values[1]
        pct = (
            round(used_values[-1] / total_bytes * 100)
            if total_bytes and used_values
            else 0
        )

        entries.append(
            DiskUsageEntry(
                node_name=node_id_to_name.get(node_id, node_id),
                mountpoint=mountpoint,
                capacity_bytes=int(total_bytes),
                used_start_bytes=used_values[0] if used_values else 0,
                used_end_bytes=used_values[-1] if used_values else 0,
                used_peak_bytes=max(used_values) if used_values else 0,
                usage_percentage=pct,
            )
        )

    entries.sort(key=lambda e: e.usage_percentage, reverse=True)
    return StorageSection(entries=entries)


async def collect_uptime(
    pmm_api: PMMRemoteAPI,
    since: str,
    until: str,
    ds_id: int,
    ds_uid: str,
) -> UptimeSection:
    """Fetch service uptime metrics from Prometheus via Grafana.

    :param pmm_api: PMM API client.
    :param since: Relative start time.
    :param until: Relative end time.
    :param ds_id: Metrics datasource numeric ID.
    :param ds_uid: Metrics datasource UID.
    :return: Populated uptime section.
    """
    expressions = [
        "avg by (service_name) (mongodb_instance_uptime_seconds)",
        "avg by (service_name) (pg_postmaster_uptime_seconds)",
        "avg by (service_name) (mysql_global_status_uptime)",
    ]
    results = await pmm_api.query_grafana_datasource(
        expressions=expressions,
        datasource_uid=ds_uid,
        datasource_id=ds_id,
        from_ts=since,
        to_ts=until,
        instant=True,
        range_=False,
    )

    entries: list[UptimeEntry] = []
    for ref_id in ("A", "B", "C"):
        for frame in results.get(ref_id, {}).get("frames", []):
            fields = frame.get("schema", {}).get("fields", [])
            if len(fields) < _MIN_FRAME_FIELDS:
                continue
            labels = fields[1].get("labels", {})
            svc_name = labels.get("service_name", "")
            if svc_name == "pmm-server-postgresql":
                continue
            values = frame.get("data", {}).get("values", [])
            if len(values) < _MIN_FRAME_FIELDS or not values[1]:
                continue
            seconds = int(values[1][0])
            uptime_td = timedelta(seconds=seconds)
            since_dt = datetime.now(UTC) - uptime_td
            entries.append(
                UptimeEntry(
                    service_name=svc_name,
                    uptime=uptime_td,
                    since=since_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                )
            )

    entries.sort(key=lambda e: e.uptime, reverse=True)
    return UptimeSection(entries=entries)


async def collect_inventory(
    pmm_api: PMMRemoteAPI,
) -> InventorySection:
    """Fetch the inventory service list with agent status from PMM.

    :param pmm_api: PMM API client.
    :return: Populated inventory section.
    """
    raw_services = await pmm_api.get_inventory_services_with_agents()

    entries: list[InventoryServiceEntry] = []
    sorted_services = sorted(
        raw_services,
        key=lambda s: (s.get("service_type", ""), s.get("service_name", "")),
    )

    for svc in sorted_services:
        if svc.get("service_name") in _PMM_SERVER_FILTER:
            continue
        if svc.get("node_id") == "pmm-server":
            continue

        status = ServiceStatus.OK
        for agent in svc.get("agents", []):
            if agent.get("agent_type") == "pmm-agent" and not agent.get("is_connected"):
                status = ServiceStatus.NOT_OK
                break
            if agent.get("agent_type") != "pmm-agent" and agent.get("status") not in (
                "RUNNING",
                "DONE",
                "AGENT_STATUS_RUNNING",
                "AGENT_STATUS_DONE",
            ):
                status = ServiceStatus.NOT_OK
                break

        entries.append(
            InventoryServiceEntry(
                service_name=svc.get("service_name", ""),
                service_type=svc.get("service_type", ""),
                node_name=svc.get("node_name", ""),
                status=status,
            )
        )

    return InventorySection(entries=entries)


# Top-level report generator
async def _fetch_base_inventory(
    pmm_api: PMMRemoteAPI,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    """Fetch nodes and services from PMM, filtering out internal entries.

    :return: Tuple of (nodes_raw, services_raw, active_service_types).
    """
    is_legacy = await pmm_api.is_older_than_v3()

    if is_legacy:
        nodes_data, svc_data = await asyncio.gather(
            pmm_api.post("/v1/inventory/Nodes/List", json={}),
            pmm_api.post("/v1/inventory/Services/List", json={}),
        )
    else:
        nodes_data, svc_data = await asyncio.gather(
            pmm_api.get("/v1/inventory/nodes"),
            pmm_api.get("/v1/inventory/services"),
        )

    nodes_raw: dict[str, dict[str, Any]] = {}
    for node_type, node_list in nodes_data.items():
        for node in node_list:
            nid = node.get("node_id", "")
            nname = node.get("node_name", "")
            if nid in _PMM_SERVER_FILTER or nname in _PMM_SERVER_FILTER:
                continue
            nodes_raw[nid] = {"name": nname, "type": node_type, "id": nid}

    services_raw: dict[str, dict[str, Any]] = {}
    active_types: set[str] = set()
    for svc_type, svc_list in svc_data.items():
        for svc in svc_list:
            sid = svc.get("service_id", "")
            sname = svc.get("service_name", "")
            if sname in _PMM_SERVER_FILTER or svc.get("node_id") == "pmm-server":
                continue
            services_raw[sid] = {
                "name": sname,
                "type": svc_type,
                "id": sid,
                "node_id": svc.get("node_id", ""),
            }
            active_types.add(svc_type)

    return nodes_raw, services_raw, active_types


async def _collect_section(
    section: str,
    report: ReportData,
    **kwargs: Any,
) -> None:
    """Collect a single report section, logging failures."""
    collectors: dict[str, Any] = {
        "advisors": lambda: collect_advisors(
            kwargs["pmm_api"],
            kwargs["active_types"],
            kwargs["nodes_raw"],
            kwargs["services_raw"],
            refresh=kwargs.get("refresh", False),
        ),
        "alerts": lambda: collect_alerts(
            kwargs["pmm_api"], kwargs["start_ts"], kwargs["stop_ts"]
        ),
        "backups": lambda: collect_backups(
            kwargs["pmm_api"],
            kwargs["start_ts"],
            kwargs["stop_ts"],
            kwargs["ds_id"],
            kwargs["ds_uid"],
        ),
        "storage": lambda: collect_storage(
            kwargs["pmm_api"],
            kwargs["nodes_raw"],
            kwargs["since"],
            kwargs["until"],
            kwargs["ds_id"],
            kwargs["ds_uid"],
        ),
        "uptime": lambda: collect_uptime(
            kwargs["pmm_api"],
            kwargs["since"],
            kwargs["until"],
            kwargs["ds_id"],
            kwargs["ds_uid"],
        ),
        "inventory": lambda: collect_inventory(kwargs["pmm_api"]),
    }
    if section not in collectors:
        return
    try:
        result = await collectors[section]()
        setattr(report, section, result)
    except (KeyError, IndexError, OSError, LookupError):
        logger.exception("Failed to collect %s data", section)


async def generate_report(
    pmm_api: PMMRemoteAPI,
    *,
    since: str = "now-7d",
    until: str = "now",
    full: bool = True,
    refresh: bool = False,
    sections: list[str] | None = None,
) -> ReportData:
    """Generate a full health report by collecting all enabled sections.

    :param pmm_api: PMM API client.
    :param since: Relative start time for the report period.
    :param until: Relative end time for the report period.
    :param full: When ``True``, include all check results and full backup
        history rather than just failures/summaries.
    :param refresh: When ``True``, force a refresh of advisor checks before
        fetching results.
    :param sections: List of section names to include. Defaults to all sections.
    :return: Complete report data.
    """
    if sections is None:
        sections = list(REPORT_SECTIONS)

    start_ts, stop_ts = _interval_ms(since, until)
    now = datetime.now(tz=UTC)
    year, week_number, _ = now.isocalendar()

    try:
        nodes_raw, services_raw, active_types = await _fetch_base_inventory(pmm_api)
    except (KeyError, IndexError, OSError):
        logger.exception("Failed to fetch base PMM inventory")
        nodes_raw, services_raw, active_types = {}, {}, set()

    ds_id, ds_uid = 0, ""
    try:
        ds_id, ds_uid = await _get_metrics_datasource(pmm_api)
    except LookupError:
        logger.warning(
            "Metrics datasource not found; storage/backup/uptime sections will be empty"
        )

    metadata = ReportMetadata(
        title="Health and Security Report",
        generated_at=now,
        report_week=f"Report {year} Week {week_number}",
        report_interval=(
            f"Report Period "
            f"{datetime.fromtimestamp(start_ts / 1000, UTC).strftime('%Y-%m-%d')} to "
            f"{datetime.fromtimestamp(stop_ts / 1000, UTC).strftime('%Y-%m-%d')}"
        ),
    )

    type_counts = Counter(s["type"] for s in services_raw.values())
    monitored = MonitoredSummary(
        total_nodes=len(nodes_raw),
        total_services=len(services_raw),
        services_by_type={k: type_counts[k] for k in sorted(type_counts)},
    )

    report = ReportData(
        full=full, refresh=refresh, metadata=metadata, monitored=monitored
    )

    requires_datasource = {"backups", "storage", "uptime"}
    collector_kwargs = {
        "pmm_api": pmm_api,
        "start_ts": start_ts,
        "stop_ts": stop_ts,
        "since": since,
        "until": until,
        "ds_id": ds_id,
        "ds_uid": ds_uid,
        "nodes_raw": nodes_raw,
        "services_raw": services_raw,
        "active_types": active_types,
        "refresh": refresh,
    }

    await asyncio.gather(
        *(
            _collect_section(section, report, **collector_kwargs)
            for section in sections
            if section not in requires_datasource or ds_id
        )
    )

    return report


# PDF generation helpers

_ASSETS_DIR = Path(__file__).parent / "assets"
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_LOGO_PNG_PATH = _ASSETS_DIR / "percona-logo.png"


@functools.lru_cache(maxsize=1)
def _get_pdf_environment() -> Environment:
    """Return the Jinja environment that renders the report PDF template.

    The report PDF is the only server-rendered surface left, so it owns its
    loader and filter set rather than sharing a PMM Extensions wide environment.

    :return: An environment loading from this app's ``templates`` directory.
    """
    env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)
    env.filters |= DEFAULT_FILTERS
    return env


@functools.lru_cache(maxsize=1)
def _get_logo_data_uri() -> str:
    """Return a ``data:`` URI for the pre-rendered Percona logo PNG."""
    png_bytes = _LOGO_PNG_PATH.read_bytes()
    b64 = base64.b64encode(png_bytes).decode()
    return f"data:image/png;base64,{b64}"


@functools.lru_cache(maxsize=1)
def _get_page_css() -> CSS:
    """Return the cached WeasyPrint CSS object for the PDF page layout."""
    return CSS(
        string="@page { size: 370mm 445.5mm; margin: 0; background: rgb(40, 39, 39); }"
    )


async def generate_pdf_report(report: ReportData) -> bytes:
    """Render a report to a self-contained HTML document and convert it to PDF.

    HTML template (``report/result_pdf.html.j2``) includes inline CSS and
    has no dependency on a Starlette ``Request`` object. weasyprint conversion
    is offloaded to a thread because it is synchronous and CPU-bound.

    :param report: The completed report data.
    :type report: ReportData
    :return: The PDF file contents.
    :rtype: bytes
    """
    template = _get_pdf_environment().get_template("result_pdf.html.j2")
    html = template.render(
        report=report,
        service_names=SERVICE_NAMES,
        sections=REPORT_SECTION_LABELS,
        logo_data_uri=_get_logo_data_uri(),
    )

    def _generate() -> bytes:
        return HTML(string=html).write_pdf(
            stylesheets=[_get_page_css()], presentational_hints=False
        )

    return await asyncio.to_thread(_generate)


# ServiceNow upload

_MAX_UPLOAD_SIZE_MB = 30


def _health_report_plan(
    upload: HealthReportSettings,
    *,
    origin: str,
    path: str,
    query: Mapping[str, str],
) -> DeliveryPlan:
    """Build the delivery plan describing one health-report upload.

    The plan is derived from the configured ``HEALTH_REPORT`` keys on every
    send, so the intake contract stays in code while deployments keep the
    settings block they already have. No ``Authorization`` header is sent: the
    intake authenticates on the ``api_key`` and ``client_identifier`` form
    fields alone.

    :param upload: The configured health-report upload settings.
    :param origin: The intake origin the transport client is pointed at.
    :param path: The request path the upload step targets.
    :param query: Query-string parameters carried by the configured endpoint.
    :return: The validated single-step delivery plan.
    """
    return DeliveryPlan(
        endpoint=origin,
        max_bundle_size_mb=_MAX_UPLOAD_SIZE_MB,
        secrets={"api_key": upload.api_key},
        upload=UploadStep(
            path=path,
            query={
                key: LiteralValue(source="literal", value=value)
                for key, value in query.items()
            },
            fields={
                "api_key": SecretValue(source="secret", name="api_key"),
                "client_identifier": LiteralValue(
                    source="literal", value=upload.client_id
                ),
                "report_type": LiteralValue(
                    source="literal", value="security_and_health_status"
                ),
                "report_week": ManifestValue(source="manifest_key", key="report_week"),
                "report_period": ManifestValue(
                    source="manifest_key", key="report_period"
                ),
                "report_generated_on": ManifestValue(
                    source="manifest_key", key="report_generated_on"
                ),
            },
            file_field="file",
            file_content_type="application/pdf",
        ),
    )


async def upload_pdf_report(
    report: ReportData, pdf_bytes: bytes
) -> dict[str, Any] | None:
    """Upload a PDF report to the ServiceNow intake via the shared delivery plan.

    :param report: The report metadata used to populate the upload fields.
    :param pdf_bytes: The rendered PDF file contents.
    :return: The intake's parsed JSON response, or ``None`` when it answers a
        success status with a body that is not a JSON object.
    :raises HTTPServiceUnavailableException: When upload is not configured.
    :raises DeliveryPlanError: When the PDF exceeds the size cap, or the endpoint
        is not a valid upload target, such as one whose path names another host.
    :raises HTTPException: Propagates the project exception the transport maps
        an intake error status to.
    :raises OSError: Propagates connection and timeout failures.
    """
    upload = health_report_settings
    if not upload.is_upload_configured:
        raise HTTPServiceUnavailableException(detail="Report upload is not configured")

    origin, path, query = split_endpoint(upload.endpoint)
    try:
        plan = _health_report_plan(upload, origin=origin, path=path, query=query)
    except ValidationError as err:
        raise DeliveryPlanError(
            "The configured health-report endpoint is not a valid upload target, "
            "so nothing was sent."
        ) from err
    api = await settings.get_remote_api(RemoteAPI, endpoint=origin)
    result = await DeliveryPlanExecutor(plan, api).upload_bundle(
        source_ref=f"health-report/{report.metadata.report_week}",
        bundle=BundleSource(
            filename=(
                "Health_and_Security_Report_"
                f"{report.metadata.generated_at:%Y-%m-%d}.pdf"
            ),
            content=pdf_bytes,
            size=len(pdf_bytes),
        ),
        case_ref=None,
        manifest={
            "report_week": report.metadata.report_week,
            "report_period": report.metadata.report_interval,
            "report_generated_on": report.metadata.generated_at.strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
        },
    )
    return None if result.detail is None else dict(result.detail)
