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
# title: Collect PMM Valkey Graphs
# description: Collect PMM Valkey dashboard graphs and diagnostics from a PMM server.
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
#   - name: end
#     label: End time (UTC)
#     description: Ending timestamp for graph data (YYYY-MM-DDTHH:MM:SS). Defaults to 24h after start.
#     type: datetime
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
#   - name: skip-valkey
#     label: Skip Valkey graphs
#     description: Skip Valkey/Redis-related graphs.
#     type: bool
#   - name: skip-os
#     label: Skip OS graphs
#     description: Skip CPU/Memory/Disk-related graphs.
#     type: bool
#   - name: sentinel
#     label: Collect Sentinel graphs
#     description: Collect Sentinel-related graphs. Always collected; not user-controllable.
#     type: bool
#     hidden: true
#   - name: cluster
#     label: Collect Cluster graphs
#     description: Collect Cluster-related graphs.
#     type: bool
#     group: High Availability
#     arg_format: "--cluster"
# ---

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

# Args
parser = argparse.ArgumentParser(
  prog="pcs-collect-pmm.py",
  formatter_class=argparse.RawDescriptionHelpFormatter,
  description="Example: ./pcs-collect-pmm.py https://USER:PASS@localhost --node srq-db1 --service srq-db1-valkey",
  epilog="Percona Consulting Scripts - v1.3.0")

parser.add_argument("-v", "--verbose", default=False, help="increase output verbosity", action="store_true")
parser.add_argument("-d", dest="extra", help="Add additional string after hostname in collected filename")

parser.add_argument("--notar", default=False, help="Do not compress the exported graphs", action="store_true")
parser.add_argument("--insecure", default=False, help="Skip TLS certificate verification for the PMM server (use only for self-signed certs).", action="store_true")
parser.add_argument("pmmserver", help="Base URL of PMM server (ie: https://localhost/) (Required)")
parser.add_argument("--apikey", help="API Key from PMM server")
parser.add_argument("--node", help="Node name of audit target (Required)")
parser.add_argument("--service", help="Service name of audit target (ex: server1-valkey) (Required)")
parser.add_argument("--list", default=False, help="List services on PMM server", action="store_true")

parser.add_argument("--start", help="Starting date for graph data in YYYY-MM-DDTHH:MM:SS format (UTC). Defaults to -24h.")
parser.add_argument("--end", help="Ending date for graph data in YYYY-MM-DDTHH:MM:SS format (UTC). If not specified, will use 24hrs from start time.")
parser.add_argument("--width", default=1280, help="Width of image in pixels")
parser.add_argument("--height", default=720, help="Height of image in pixels")
parser.add_argument("--interval", default="5s", help="Change the interval resolution of data points. Uses Grafana time format (5s, 1h, 1d, etc)")

skipgroup = parser.add_argument_group()
skipgroup.add_argument("--skip-valkey", default=False, help="Skip Valkey/Redis-related graphs", action="store_true")
skipgroup.add_argument("--skip-os", default=False, help="Skip CPU/Memory/Disk-related graphs", action="store_true")

# valkey/redis cluster or sentinel graphs
hagroup = parser.add_argument_group()
hagroup.add_argument("--sentinel", default=True, help="Collect Sentinel-related graphs", action="store_true")
hagroup.add_argument("--cluster", default=False, help="Collect Cluster-related graphs", action="store_true")


args = parser.parse_args()

VERIFY_SSL = not args.insecure  # noqa: S501

PMMSERVER   = args.pmmserver
APIKEY      = args.apikey
NODE        = args.node
SERVICE     = args.service

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
  parser.error("Please provide the both node and service name for the graphs you wish to render. Use --list to view nodes and services on this PMM server.")

# Check for UN/PW in URL
if APIKEY is None and not re.match(r"https?://.*:.*@", PMMSERVER):
  parser.error(f"Error: PMM URL '{PMMSERVER}' does not contain username and/or password.")

# Strip trailing slash
if PMMSERVER[-1] == "/":
  PMMSERVER = PMMSERVER[0:-1]

# Sanity
if PMMSERVER[0:4] != "http":
  parser.error(f"Error: PMM URL '{PMMSERVER}' does not contain protocol (ie: http/https)")

class CollectPmmError(Exception):
  """Raise when a PMM collection step fails."""

#
# Base Functions
#
def get_graph_window(startstr: str | None, endstr: str | None) -> tuple[datetime.datetime, datetime.datetime]:

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

def render_dashboard(graphs: list, dashboard_uid: str, path_to_graphs: str, **kwargs: str) -> None:

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

def render_graphs(graphs: list[str], dashboard_params: dict[str, str], path_to_graphs: str) -> None:

  headers = build_header()

  # Must be in millisecond format
  time_from_ts = int(dashboard_params["time_from"].timestamp()) * 1000
  time_to_ts   = int(dashboard_params["time_to"].timestamp()) * 1000

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
        response = requests.get(url, params=params, headers=headers, timeout=30, verify=VERIFY_SSL)

        # Good response for render
        if response.status_code == requests.codes.OK:

          # Write the file contents in the response to a local file
          filename = get_valid_filename(p["title"])
          graph_image = Path("{}/{}_{}.png".format(path_to_graphs, dashboard_params["uid"], filename))
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

  response = requests.get(f"{PMMSERVER}/graph/api/dashboards/uid/{uid}", headers=headers, timeout=30, verify=VERIFY_SSL)
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
    response = requests.get(f"{PMMSERVER}/graph/api/health", headers=headers, timeout=30, verify=VERIFY_SSL)
    resp = response.json()

    if response.status_code != requests.codes.OK:
      msg = f"!! Server response: {response.status_code} / {response.content} !!"
      raise CollectPmmError(msg)

    if resp["database"] != "ok":
      msg = f"!! Connected to PMM at '{PMMSERVER}', but PMM is not healthy."
      raise CollectPmmError(msg)

    # Get version, PMMv2 and v3 both respond to /v1/version
    response = requests.get(f"{PMMSERVER}/v1/version", headers=headers, timeout=30, verify=VERIFY_SSL)
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
  if hostname[-7:] == "-valkey":
    hostname = hostname[0:-7]

  # Graph window / parse timestamp args
  (time_from, time_to) = get_graph_window(args.start, args.end)

  # Make local storage for graphs, presumably inside the same "environment" folder
  ts = time_from.strftime("%Y-%m-%d")
  extra = args.extra or ""
  path_to_graphs = f"{hostname}{extra}_pmm_{ts}"

  if not Path(path_to_graphs).exists():
    Path(path_to_graphs).mkdir(parents=True)

  ### Graphs

  if not args.skip_valkey:

    # Bound before the blocks that fill them: --sentinel defaults on, so one of
    # them always runs, but nothing local to this function says so.
    dashboard_uid = ""
    graphs: list[str] = []

    # Sentinel Summary graphs
    if args.sentinel:

      print("- Collecting Sentinel Summary graphs")

      dashboard_uid = "VCFX6PdHk"
      graphs = [
        "Max Uptime",
        "$node_name - Total Commands / sec",
        "$node_name - read and Write rate",
        "$node_name - command ops/sec",
        "Total Memory Usage",
        "Number of Keys",
        "$node_name - network I/O",
        "Connected/Blocked Clients",
        "$node_name - client Buffers",
        "Config Max Clients",
        "Evicted Clients",
        "Expired/Evicted Keys",
        "Expiring vs Not-Expiring Keys",
        "$node_name - top 10 total time by command",
        "$node_name - total Time Spent by Command / sec",
        "Commands Totals",
        "$node_name",
        "Replica vs Master offsets",
        "Replicas",
        "Backlog first byte offset",
        "Backlog History Bytes",
        "Backlog Size",
        "Connected Replicas",
        "Replica Resync Info",
        "Partial resyncs",
        "Full resyncs",
        "IO thread R/W per Sec",
        "IO threads active",
        "IO threads configured",
        "Base, current, last COW size",
        "Enabled",
        "Delayed fsyncs",
        "Appendfsync",
        "Last rewrite duration",
        "Loading Dump",
        "Last COW size",
        "Async Loading",
        "Last rewrite success",
        "Last bgsave timestamp",
        "Last bgsave success",
        "Changes since lastsave",
        "Save config",
        "RDB saves",
        "Last COW size",
        "$node_name - command percentiles",
        "get latency",
        "set latency",
        "hset latency",
        "rpop latency",
        "lpop latency",
        "lrange latency",
        "rpush latency",
        "lpush latency",
        "hset latency",
        "psync latency",
        "Slowlog length",
        "Slowlog",
        "Slowlog slower than (ms)",
        "Slowlog maxlength",
      ]

    # Valkey/Redis Cluster Summary graphs
    if args.cluster:

      print("- Collecting Cluster Summary graphs")

      dashboard_uid = "zddr6B2Hk"
      graphs = [
        "Max Uptime",
        "$node_name - Total Commands / sec",
        "$node_name - read and Write rate",
        "$node_name - command ops/sec",
        "Total Memory Usage",
        "Number of Keys",
        "$node_name - network I/O",
        "Connected/Blocked Clients",
        "$node_name - client Buffers",
        "Config Max Clients",
        "Evicted Clients",
        "Expired/Evicted Keys",
        "Expiring vs Not-Expiring Keys",
        "$node_name - top 10 total time by command",
        "$node_name - total Time Spent by Command / sec",
        "Commands Totals",
        "$node_name",
        "Replica vs Master offsets",
        "Replicas",
        "Backlog first byte offset",
        "Backlog History Bytes",
        "Backlog Size",
        "Connected Replicas",
        "Replica Resync Info",
        "Partial resyncs",
        "Full resyncs",
        "IO thread R/W per Sec",
        "IO threads active",
        "IO threads configured",
        "Base, current, last COW size",
        "Enabled",
        "Delayed fsyncs",
        "Appendfsync",
        "Last rewrite duration",
        "Loading Dump",
        "Last COW size",
        "Async Loading",
        "Last rewrite success",
        "Last bgsave timestamp",
        "Last bgsave success",
        "Changes since lastsave",
        "Save config",
        "RDB saves",
        "Last COW size",
        "$node_name - command percentiles",
        "get latency",
        "set latency",
        "hset latency",
        "rpop latency",
        "lpop latency",
        "lrange latency",
        "rpush latency",
        "lpush latency",
        "hset latency",
        "psync latency",
        "Slowlog length",
        "Slowlog",
        "Slowlog slower than (ms)",
        "Slowlog maxlength",
        "slots status",
        "cluster messages",
        "$node_name - cluster connections",
        "$node_name - cluster state",
        "$node_name - known nodes",
      ]

    if graphs:
      render_dashboard(graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to)


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
    render_dashboard(graphs, dashboard_uid, path_to_graphs, time_from=time_from, time_to=time_to)

  if not args.notar:
    print("- Compressing graphs into .tgz...")
    subprocess.run(["/usr/bin/tar", "-czf", f"{path_to_graphs}.tgz", path_to_graphs], check=True)  # noqa: S603

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
