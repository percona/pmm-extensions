#!/usr/bin/env python3
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

# ---
# title: Collect PMM MySQL Graphs
# description: Collect PMM MySQL dashboard graphs and diagnostics from a PMM server.
# requires_packages:
#   - requests
#   - urllib3
# parameters:
#   - name: pmmserver
#     label: PMM server URL
#     description: Base URL of PMM server. Leave empty to use configured default (PMM.ENDPOINT).
#     positional: true
#     required: false
#   - name: apikey
#     label: API key
#     description: API key for PMM server. Leave empty to use configured default (PMM.API_KEY).
#     hidden: true
#   - name: node
#     label: Node name
#     description: Node name of audit target (required unless using --list).
#   - name: service
#     label: Service name
#     description: Service name of audit target (required unless using --list).
#   - name: list
#     label: List services
#     description: List nodes and services on the PMM server instead of collecting graphs.
#     type: bool
#   - name: verbose
#     label: Verbose output
#     description: Increase output verbosity.
#     type: bool
#   - name: extra
#     label: Output suffix
#     description: Additional string to append to the collected filename after the hostname.
#     arg_format: "-d ${value}"
#   - name: notar
#     label: Skip tar
#     description: Do not compress the exported graphs.
#     type: bool
#   - name: insecure
#     label: Insecure (skip TLS verification)
#     description: "Disable SSL certificate verification for the PMM server. Use only when PMM is deployed with a self-signed certificate."
#     type: bool
#   - name: start
#     label: Start time (UTC)
#     description: Starting timestamp for graph data (YYYY-MM-DDTHH:MM:SS). Defaults to 24h ago.
#     type: datetime
#     visible_when_not: list
#   - name: end
#     label: End time (UTC)
#     description: Ending timestamp for graph data (YYYY-MM-DDTHH:MM:SS). Defaults to 24h after start.
#     type: datetime
#     visible_when_not: list
#   - name: width
#     label: Image width
#     description: Width of images in pixels.
#     type: int
#     default: 1280
#     gt: 0
#   - name: height
#     label: Image height
#     description: Height of images in pixels.
#     type: int
#     default: 720
#     gt: 0
#   - name: interval
#     label: Interval
#     description: Interval resolution for data points (Grafana time format).
#     default: "5s"
#   - name: skip-mysql
#     label: Skip MySQL graphs
#     description: Skip MySQL-related graphs.
#     type: bool
#   - name: skip-os
#     label: Skip OS graphs
#     description: Skip CPU/Memory/Disk-related graphs.
#     type: bool
#   - name: skip-innodb
#     label: Skip InnoDB graphs
#     description: Skip InnoDB-related graphs.
#     type: bool
#   - name: skip-security
#     label: Skip security checks
#     description: Skip collection of security checks.
#     type: bool
#   - name: ha
#     label: High Availability mode
#     description: Collect HA-related graphs (mutually exclusive).
#     group: High Availability
#     choices:
#       - value: pxc
#         label: PXC
#       - value: gr
#         label: Group Replication
#       - value: async
#         label: Async Replication
#     arg_format: "--ha ${value}"
#     visible_when_not: list
#   - name: ha_name
#     label: Cluster / replica-set name
#     description: Required for PXC and Group Replication; ignored for Async or when no HA mode is selected.
#     group: High Availability
#     arg_format: "--ha-name ${value}"
#     visible_when_not: list
#   - name: dbaas
#     label: DBaaS mode
#     description: Collect DBaaS-related graphs (mutually exclusive).
#     group: DBaaS Options
#     choices:
#       - value: rds
#         label: Amazon RDS MySQL
#       - value: aurora
#         label: Amazon Aurora MySQL
#     arg_format: "--dbaas ${value}"
#     visible_when_not: list
# ---

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings()

# Args
parser = argparse.ArgumentParser(
    prog="pcs-collect-pmm.py",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Example: ./pcs-collect-pmm.py https://USER:PASS@localhost --node srq-db1 --service srq-db1-mysql",
    epilog="Percona Consulting Scripts - v1.3.0",
)

parser.add_argument(
    "-v",
    "--verbose",
    default=False,
    help="increase output verbosity",
    action="store_true",
)
parser.add_argument(
    "-d",
    dest="extra",
    help="Add additional string after hostname in collected filename",
)

parser.add_argument(
    "--notar",
    default=False,
    help="Do not compress the exported graphs",
    action="store_true",
)
parser.add_argument(
    "--insecure",
    default=False,
    help="Skip TLS certificate verification for the PMM server (use only for self-signed certs).",
    action="store_true",
)
parser.add_argument(
    "pmmserver", help="Base URL of PMM server (ie: https://localhost/) (Required)"
)
parser.add_argument("--apikey", help="API Key from PMM server")
parser.add_argument("--node", help="Node name of audit target (Required)")
parser.add_argument(
    "--service", help="Service name of audit target (ex: server1-mysql) (Required)"
)
parser.add_argument(
    "--list", default=False, help="List services on PMM server", action="store_true"
)

parser.add_argument(
    "--start",
    help="Starting date for graph data in YYYY-MM-DDTHH:MM:SS format (UTC). Defaults to -24h.",
)
parser.add_argument(
    "--end",
    help="Ending date for graph data in YYYY-MM-DDTHH:MM:SS format (UTC). If not specified, will use 24hrs from start time.",
)
parser.add_argument("--width", default=1280, help="Width of image in pixels")
parser.add_argument("--height", default=720, help="Height of image in pixels")
parser.add_argument(
    "--interval",
    default="5s",
    help="Change the interval resolution of data points. Uses Grafana time format (5s, 1h, 1d, etc)",
)

# MySQL skips
skipgroup = parser.add_argument_group()
skipgroup.add_argument(
    "--skip-mysql", default=False, help="Skip MySQL-related graphs", action="store_true"
)
skipgroup.add_argument(
    "--skip-os",
    default=False,
    help="Skip CPU/Memory/Disk-related graphs",
    action="store_true",
)
skipgroup.add_argument(
    "--skip-innodb",
    default=False,
    help="Skip InnoDB-related graphs",
    action="store_true",
)
skipgroup.add_argument(
    "--skip-security",
    default=False,
    help="Skip collection of security checks",
    action="store_true",
)

# HA options
hagroup = parser.add_argument_group("High Availability")
hagroup.add_argument(
    "--ha",
    choices=["pxc", "gr", "async"],
    help="Collect HA-related graphs (mutually exclusive)",
)
hagroup.add_argument(
    "--ha-name",
    dest="ha_name",
    metavar="NAME",
    help="Cluster or replica-set name (required for --ha pxc or gr)",
)

# DBaaS options
dbaasgroup = parser.add_argument_group("DBaaS Options")
dbaasgroup.add_argument(
    "--dbaas",
    choices=["rds", "aurora"],
    help="Collect DBaaS-related graphs (mutually exclusive)",
)

args = parser.parse_args()

# Derive legacy HA/DBaaS attrs for downstream collection blocks
args.pxc = args.gr = None
args.haasync = None
args.rds = args.aurora = False

if args.ha:
    if args.ha in ("pxc", "gr"):
        if not args.ha_name:
            parser.error("--ha-name is required when --ha is pxc or gr")
        if args.ha == "pxc":
            args.pxc = args.ha_name
        else:
            args.gr = args.ha_name
    elif args.ha == "async":
        args.haasync = True

if args.dbaas == "rds":
    args.rds = True
elif args.dbaas == "aurora":
    args.aurora = True

VERIFY_SSL = not args.insecure  # noqa: S501

PMMSERVER = args.pmmserver
APIKEY = args.apikey
NODE = args.node
SERVICE = args.service

API_ENDPOINTS = {
    "2": {
        "LIST_NODES": "v1/inventory/Nodes/List",
        "LIST_SERVICES": "v1/inventory/Services/List",
    },
    "3": {
        "LIST_NODES": "v1/inventory/nodes",
        "LIST_SERVICES": "v1/inventory/services",
    },
}

# If no service name provided, recommend --list
if (NODE is None or SERVICE is None) and not args.list:
    parser.error(
        "Please provide the both node and service name for the graphs you wish to render. Use --list to view nodes and services on this PMM server."
    )

# Check for UN/PW in URL
if APIKEY is None and not re.match(r"https?://.*:.*@", PMMSERVER):
    parser.error(
        f"Error: PMM URL '{PMMSERVER}' does not contain username and/or password."
    )

# Strip trailing slash
if PMMSERVER[-1] == "/":
    PMMSERVER = PMMSERVER[0:-1]

# Sanity
if PMMSERVER[0:4] != "http":
    parser.error(
        f"Error: PMM URL '{PMMSERVER}' does not contain protocol (ie: http/https)"
    )


class CollectPmmError(Exception):
    """Simple exception class."""


#
# Base Functions
#
def get_graph_window(startstr: str, endstr: str) -> tuple[datetime.datetime, datetime.datetime]:
    # Defaults to 24hrs ago
    end = datetime.datetime.now(tz=datetime.timezone.utc)
    start = end - datetime.timedelta(seconds=86400)

    # If provided, try to parse starting timestamp
    if startstr is not None:
        try:
            start = datetime.datetime.strptime(startstr, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc,
            )
        except ValueError:
            print(f"Unable to parse '{startstr}' starting timestamp")
            raise

    # If provided, try to parse ending timestamp
    if endstr is not None:
        try:
            end = datetime.datetime.strptime(endstr, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc,
            )
        except ValueError:
            print(f"Unable to parse '{endstr}' ending timestamp")
            raise

    return (start, end)


def get_valid_filename(s: str) -> str:
    s = str(s).strip().replace(" ", "_")
    return re.sub(r"(?u)[^-\w.]", "", s)


def build_header() -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if APIKEY is not None:
        headers["Authorization"] = f"Bearer {APIKEY}"

    return headers


def render_dashboard(
    graphs: list, dashboard_uid: str, path_to_graphs: str, **kwargs: str
) -> None:
    dashboard = get_dashboard(dashboard_uid)
    dashboard_params = {
        "panels": dashboard["dashboard"]["panels"],
        "slug": dashboard["meta"]["slug"],
        "uid": dashboard_uid,
    }

    if "cluster" in kwargs:
        dashboard_params.update({"var-cluster": kwargs["cluster"]})

    if "database" in kwargs:
        dashboard_params.update({"var-database": kwargs["database"]})

    if "time_from" in kwargs:
        dashboard_params.update({"time_from": kwargs["time_from"]})

    if "time_to" in kwargs:
        dashboard_params.update({"time_to": kwargs["time_to"]})

    render_graphs(graphs, dashboard_params, path_to_graphs)


def render_graphs(
    graphs: list[str], dashboard_params: dict[str, Any], path_to_graphs: str
) -> None:
    headers = build_header()

    # Must be in millisecond format
    time_from_ts = int(dashboard_params["time_from"].timestamp()) * 1000
    time_to_ts = int(dashboard_params["time_to"].timestamp()) * 1000

    p = None
    try:
        panels = dashboard_params["panels"]
        for p in panels:
            if "panels" in p:
                # "panel" is an expandable container of many graphs
                # recurse to dive into this nested panel of graphs
                _params = dict(dashboard_params)
                _params["panels"] = p["panels"]
                render_graphs(graphs, _params, path_to_graphs)

            else:
                # Some dashboards have colorful text-only panels that don't have a title
                if p["type"] == "text":
                    continue

                # Check if the title of this graph/panel is one we are interested in
                if p["title"].strip() not in graphs:
                    if args.verbose:
                        print("-- Ignoring graph {}".format(p["title"]))
                    continue

                print("-- Rendering graph '{}'...".format(p["title"]))

                # URL for PNG:
                # /graph/render/d-solo/mysql-innodb/mysql-innodb-details?from=1668010751705&to=1668032351705&var-interval=%24__auto_interval_interval&var-environment=All&var-node_name=srq-ngdb1&var-crop_host=srq-ngdb1&var-service_name=srq-ngdb1&var-region=&var-cluster=&var-node_id=%2Fnode_id%2F33c8c8be-d0fc-48bf-81f8-33ef3c01dc06&var-agent_id=%2Fagent_id%2Fe6c467b7-1a3f-4e25-97d9-a52fba32219d&var-service_id=%2Fservice_id%2F53e25f23-f037-4b36-a934-5c7c7d02d2af&var-az=&var-node_type=remote&var-node_model=&var-replication_set=SRQ-NGDB&var-version=5.7.15-log&var-service_type=All&var-database=All&var-username=All&var-schema=All&orgId=1&refresh=1m&panelId=23&width=1000&height=500&tz=America%2FChicago
                # /graph/render/d-solo/node-instance-summary/node-summary?from=1685935680566&to=1685978880566&var-interval=%24__auto_interval_interval&var-region=All&var-node_type=All&var-environment=All&var-node_name=db04&var-cpu=All&var-service_name=All&var-cluster=All&var-replication_set=All&var-database=All&var-service_type=All&var-username=All&var-schema=All&orgId=1&refresh=1m&var-node_id=%2Fnode_id%2F510634b7-93e5-4d94-ad89-6de3d8e8dec7&panelId=2&width=1000&height=500&tz=America%2FChicago
                url = "{}/graph/render/d-solo/{}/{}".format(
                    PMMSERVER, dashboard_params["uid"], dashboard_params["slug"]
                )
                params = {
                    "refresh": "1m",
                    "orgId": 1,
                    "panelId": str(p["id"]),
                    "from": time_from_ts,
                    "to": time_to_ts,
                    "var-service_name": SERVICE,
                    "var-node_name": NODE,
                    "var-interval": args.interval,
                    "width": args.width,
                    "height": args.height,
                    "theme": "light",
                    "scale": 1,
                    "__feature.dashboardSceneSolo": 1,
                }

                # Add cluster name if provided in parameters
                if "var-cluster" in dashboard_params:
                    params.update({"var-cluster": dashboard_params["var-cluster"]})

                if "var-database" in dashboard_params:
                    params.update({"var-database": dashboard_params["var-database"]})

                if args.verbose:
                    print(f"## DEBUG\n## {url}\n## {params}")

                # Send HTTP GET request to server and attempt to receive a response
                response = requests.get(
                    url, params=params, headers=headers, timeout=30, verify=VERIFY_SSL
                )

                # Good response for render
                if response.status_code == requests.codes.OK:
                    # Write the file contents in the response to a local file
                    filename = get_valid_filename(p["title"])
                    graph_image = Path(
                        "{}/{}_{}.png".format(path_to_graphs, dashboard_params["uid"], filename)
                    )
                    graph_image.write_bytes(response.content)

                else:
                    print("!! Non-200 Unable to render graph {}".format(p["title"]))

    except Exception:
        print("!! render_graph Error !!")
        print(dashboard_params)
        print(json.dumps(p))
        raise


def get_dashboard(uid: str) -> dict:
    # https://10.0.1.38/graph/d/node-instance-summary/node-summary?from=now-12h&to=now...
    #                           ^------- uid -------^ ^-- slug --^
    headers = build_header()

    response = requests.get(
        f"{PMMSERVER}/graph/api/dashboards/uid/{uid}",
        headers=headers,
        timeout=30,
        verify=VERIFY_SSL,
    )
    dashboard = response.json()

    if "message" in dashboard:
        raise CollectPmmError(dashboard["message"])

    return dashboard


# Ping PMM server for health status, and get major version
# @return str PMM major version, or 0 on failure
def get_pmm_version() -> str:
    try:
        headers = build_header()

        # Get health
        response = requests.get(
            f"{PMMSERVER}/graph/api/health", headers=headers, timeout=30, verify=VERIFY_SSL
        )
        resp = response.json()

        if response.status_code != requests.codes.OK:
            msg = f"!! Server response: {response.status_code} / {response.content} !!"
            raise CollectPmmError(msg)

        if resp["database"] != "ok":
            msg = f"!! Connected to PMM at '{PMMSERVER}', but PMM is not healthy."
            raise CollectPmmError(msg)

        # Get version, PMMv2 and v3 both respond to /v1/version
        response = requests.get(
            f"{PMMSERVER}/v1/version", headers=headers, timeout=30, verify=VERIFY_SSL
        )
        resp = response.json()

        if response.status_code != requests.codes.OK:
            msg = f"!! Failed to fetch PMM version {response.content}"
            raise CollectPmmError(msg)

        return resp["version"][0]  # Just want the major number

    except requests.exceptions.RequestException:
        print(f"!! Unable to connect to PMM server at '{PMMSERVER}'. Check the URL.")
        raise
    except Exception:
        raise


def list_services(pmm_version: str) -> None:
    headers = build_header()

    print("\n-- List of Nodes and Services --\n")

    try:
        # Get list of nodes
        # This returns an array of dictionaries based on what 'type' (generic, container, rds, etc) of node is being monitored
        url = "{}/{}".format(PMMSERVER, API_ENDPOINTS[pmm_version]["LIST_NODES"])
        if pmm_version == "2":
            response = requests.post(url, headers=headers, timeout=30, verify=VERIFY_SSL)
        else:
            response = requests.get(url, headers=headers, timeout=30, verify=VERIFY_SSL)

        nodes = response.json()
        if "message" in nodes:
            raise CollectPmmError(nodes["message"])

        # Get list of services
        url = "{}/{}".format(PMMSERVER, API_ENDPOINTS[pmm_version]["LIST_SERVICES"])
        if pmm_version == "2":
            response = requests.post(url, headers=headers, timeout=30, verify=VERIFY_SSL)
        else:
            response = requests.get(url, headers=headers, timeout=30, verify=VERIFY_SSL)

        services = response.json()
        if "message" in services:
            raise CollectPmmError(services["message"])

        # Loop and print
        print("## Nodes")
        for nodetype in nodes:
            print(f"- {nodetype}")
            for node in nodes[nodetype]:
                print("-- {} ({})".format(node["node_name"], node["address"]))

        print("\n## Services")
        for servicetype in services:
            print(f"- {servicetype}")
            for service in services[servicetype]:
                print("-- Name: {}".format(service["service_name"]))

    except requests.exceptions.RequestException:
        print(f"!! Unable to connect to PMM server at '{PMMSERVER}'. Check the URL.")
        raise
    except Exception:
        print("!! List Hosts Error !!")
        raise


def get_security_checks(path_to_graphs: str) -> None:
    """Get security checks.

    PMMv2 API does not support any filtering, so we it manually.
    In the end, we want a single array containing each check, and whether
    or not the check passed for this host.
    """
    print("-- Begin security check list")

    # Fetch list of security checks
    response = requests.post(
        f"{PMMSERVER}/v1/management/SecurityChecks/List", timeout=30, verify=VERIFY_SSL
    )
    security_checks_list = response.json()

    if (
        response.status_code != requests.codes.OK
        or "checks" not in security_checks_list
    ):
        print(f"!! Failed to fetch security checklist {response.status_code} !!")
        print(f"!! {response.content} !!")
        return

    passing_checks_list = {}

    # The PMM API does not provide a 'passing' checks API, only a 'failing', so we assume
    # that any checks which did not explicitly fail, succeeded.
    for check in security_checks_list["checks"]:
        if check["family"] == "ADVISOR_CHECK_FAMILY_MYSQL":
            passing_checks_list[check["name"]] = {
                "name": check["name"],
                "description": check["description"],
                "summary": check["summary"],
                "result": {},
            }

    # Fetch list of failed checks
    response = requests.post(
        f"{PMMSERVER}/v1/management/SecurityChecks/FailedChecks",
        timeout=30,
        verify=VERIFY_SSL,
    )
    failed_checks = response.json()

    if response.status_code != requests.codes.OK:
        print(f"!! Failed to fetch failed security checks {response.status_code} !!")
        print(f"!! {response.content} !!")
        return

    if "results" not in failed_checks:
        print("-- No failed checks found")
    else:
        for failed_check in failed_checks["results"]:
            if failed_check["service_name"] != SERVICE:
                continue

            # Safety
            if failed_check["check_name"] not in passing_checks_list:
                print(
                    "!! Failed security check {} does not exist in main list !!".format(
                        failed_check["check_name"]
                    )
                )
                continue

            passing_checks_list[failed_check["check_name"]]["result"] = {
                "summary": failed_check["summary"],
                "description": failed_check["description"],
                "severity": failed_check["severity"],
            }

    with open(f"{path_to_graphs}/security_checks", "w") as f:
        f.write(json.dumps(passing_checks_list))

    print("-- Finish security check list")


#
# Main
#
def main() -> int:
    # Make sure PMM is reachable, and get major version
    pmm_version = get_pmm_version()
    print(f"- Detected PMM version: {pmm_version}")

    # List all the nodes/services reporting to this PMM instance
    if args.list:
        list_services(pmm_version)
        return 0

    # Strip literal service name and create hostname, this should match pcs-collect-environment
    hostname = SERVICE
    if hostname[-6:] == "-mysql":
        hostname = hostname[0:-6]

    # Graph window / parse timestamp args
    (time_from, time_to) = get_graph_window(args.start, args.end)

    # Make local storage for graphs, presumably inside the same "environment" folder
    ts = time_from.strftime("%Y-%m-%d")
    extra = args.extra or ""
    path_to_graphs = f"{hostname}{extra}_pmm_{ts}"

    if not Path(path_to_graphs).exists():
        Path(path_to_graphs).mkdir(parents=True)

    ### Graphs

    if not args.skip_mysql:
        # MySQL Instance Summary
        print("- Collecting 'MySQL Instance Summary' graphs")

        dashboard_uid = "mysql-instance-summary"
        graphs = [
            "MySQL Connections",
            "MySQL Aborted Connections",
            "MySQL Client Thread Activity",
            "MySQL Temporary Objects",
            "MySQL Select Types",
            "MySQL Questions",
            "Top Command Counters",
            "MySQL Handlers",
            "MySQL Query Cache Activity",
            "MySQL Open Tables",
            "MySQL Sorts",
            "MySQL Table Open Cache Status",
            "MySQL Table Definition Cache",
        ]
        render_dashboard(
            graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to
        )

    if not args.skip_innodb:
        # MySQL InnoDB Details
        print("- Collecting 'MySQL InnoDB Details' graphs")

        dashboard_uid = "mysql-innodb"
        graphs = [
            "InnoDB Row Reads",
            "InnoDB Row Writes",
            "InnoDB Data I/O",
            "InnoDB FSyncs",
            "InnoDB Pending IO",
            "InnoDB IO Targets Bandwidth",
            "InnoDB Buffer Pool Data",
            "InnoDB Buffer Pool Requests",
            "InnoDB Buffer Pool Page Activity",
            "InnoDB Checkpoint Age",
            "InnoDB Log File Usage Hourly",
            "InnoDB Row Lock Wait Activity",
            "Innodb Row Lock Wait Time",
            "Transaction History",
            "InnoDB AHI Miss Ratio",
            "InnoDB AHI Usage",
            "InnoDB Contention - OS Waits",
            "InnoDB Contention - Spin Rounds",
            "Innodb Flushing by Type",
        ]
        render_dashboard(
            graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to
        )

    # ASync replication graphs
    if args.haasync:
        print("- Collecting 'MySQL High Availability' graphs")

        dashboard_uid = "mysql-replicaset-summary"
        graphs = [
            "MySQL Replication Lag",
        ]
        render_dashboard(
            graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to
        )

    # Percona XtraDB Cluster
    if args.pxc:
        print("- Collecting PXC graphs")

        dashboard_uid = "pxc-cluster-summary"
        graphs = [
            "Percona XtraDB / Galera Cluster Size",
            "Flow Control Paused Time",
            "Flow Control Messages Sent",
            "Average Galera Replication Latency",
        ]
        render_dashboard(
            graphs,
            dashboard_uid,
            path_to_graphs,
            time_from=time_from,
            time_to=time_to,
            cluster=args.pxc,
        )

        dashboard_uid = "pxc-node-summary"
        graphs = [
            "Galera Flow Control",
            "Available Downtime before SST Required",
            "Galera Writeset Traffic",
        ]
        render_dashboard(
            graphs,
            dashboard_uid,
            path_to_graphs,
            time_from=time_from,
            time_to=time_to,
            cluster=args.pxc,
        )

    # Group replication
    if args.gr:
        print("- Collecting Group Replication graphs")

        dashboard_uid = "mysql-group-replicaset-summary"
        graphs = [
            "Replication Lag",
            "Replication Delay",
            "Transactions in the Queue for Checking",
        ]
        render_dashboard(
            graphs,
            dashboard_uid,
            path_to_graphs,
            time_from=time_from,
            time_to=time_to,
            cluster=args.gr,
        )

    # Amazon Aurora
    if args.aurora:
        print("- Collecting Amazon RDS Aurora graphs")

        dashboard_uid = "mysql-amazonaurora"
        graphs = [
            "Amazon Aurora Transaction Commits",
            "Amazon Aurora Load",
            "Aurora Memory Used",
            "Amazon Aurora Statement Latency",
            "Amazon Aurora Special Command Counters",
            "Amazon Aurora Problems",
        ]
        render_dashboard(
            graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to
        )

    # If DBaaS
    if args.rds or args.aurora:
        args.skip_os = True
        print("- Not collecting OS/Memory/CPU related graphs on RDS/Aurora")

    # OS Node Summary
    if not args.skip_os:
        # Node Summary
        print("- Collecting 'Node Summary' graphs")

        dashboard_uid = "node-instance-summary"
        graphs = [
            "CPU Usage",
            "CPU Saturation and Max Core Usage",
            "Memory Utilization",
            "Swap Activity",
            "I/O Activity",
            "Disk IO Latency",
            "Network Traffic",
        ]
        render_dashboard(
            graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to
        )

    if not args.skip_security:
        get_security_checks(path_to_graphs)

    if not args.notar:
        print("- Compressing graphs into .tgz...")
        subprocess.run(
            ["/usr/bin/tar", "-czf", f"{path_to_graphs}.tgz", path_to_graphs],
            check=True,
        )

    print("== All Done! ==")

    return 0


#
# Script entry
#
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"Exception: {e}")
        sys.exit(1)
