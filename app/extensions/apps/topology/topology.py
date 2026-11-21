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

"""Build the React-Flow topology graph from per-host MySQL collector results.

The collector payload (``payloads/topology.py``) emits one NDJSON event per
MySQL host. This module:

* parses those events into per-host records,
* builds the graph (nodes + edges) consumed by the React Flow front end,
* derives PXC cluster groups and unknown-source nodes for replication
  sources we never queried.

Pure functions only; the dispatch layer lives in ``api_routes.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from typing import Any, TYPE_CHECKING

from app.inventory.constants import DEFAULT_MYSQL_PORT

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, MutableMapping

logger = logging.getLogger(__name__)

NODE_TYPE_MYSQL = "mysql"
NODE_TYPE_CLUSTER = "cluster"
NODE_TYPE_UNKNOWN = "unknown_source"

EDGE_TYPE_REPLICATION = "replication"
EDGE_TYPE_DUAL_PRIMARY = "dual_primary"

TOPOLOGY_JOB_PREFIX = "topology"
TOPOLOGY_PAYLOAD_REQUIREMENTS = "PyMySQL[rsa,ed25519]\nmyloginpath"


def make_primary_hash(server_id: Any, server_uuid: Any, port: Any) -> str:
    """Compute the deterministic hash that lets a replica match a primary's identity.

    Stays byte-identical to the collector's server-identity hash,
    ``SHA2(CONCAT_WS('|', @@server_id, @@server_uuid, @@port), 256)`` (see
    ``payloads/topology.py``): a primary reports that value as its
    ``server_hash`` and a replica recomputes it here from its
    ``source_server_id``/``source_uuid``/``source_port``. The ``'|'`` separator
    and the ``NULL``-skipping mirror ``CONCAT_WS`` exactly -- a format drift
    silently disables hash-based correlation, and a falsy-but-real ``0`` must
    render as ``"0"`` rather than collapse away.
    MySQL 5.7 lacks ``server_uuid`` in replica status, so the collector
    synthesizes it from ``server_hash``; replica-to-5.7-primary correlation
    then matches on the address+port fallback rather than this hash.

    :param server_id: Source or server ``server_id`` value.
    :param server_uuid: Source or server ``server_uuid`` value.
    :param port: Source or server port value.
    :return: Hex SHA-256 hash matching the collector's ``server_hash`` format.
    """
    raw = "|".join(
        str(part) for part in (server_id, server_uuid, port) if part is not None
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_ndjson(stdout: str) -> list[dict[str, Any]]:
    """Parse the NDJSON stdout from one topology collector task.

    Skips blank lines and silently drops malformed JSON (logged at debug).

    :param stdout: Raw stdout content emitted by one collector shard.
    :return: Parsed JSON event objects in input order.
    """
    events: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON topology line: %s", line[:200])
    return events


def merge_host_records(
    *event_streams: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge ``host_done`` and ``host_error`` events from N shards into one map.

    Last-write-wins on duplicate ``host`` keys; ``host_error`` always replaces
    a prior ``host_done`` so a later shard's failure is visible.

    :param event_streams: Parsed event streams from one or more shards.
    :return: Host-entry map keyed by ``host`` with ``ok`` or ``error`` status.
    """
    out: dict[str, dict[str, Any]] = {}
    for stream in event_streams:
        for ev in stream:
            kind = ev.get("event")
            host = ev.get("host")
            if not host:
                logger.debug("Skipping topology event without host: %r", ev)
                continue
            if kind == "host_done":
                out[host] = {"status": "ok", "data": ev.get("data") or {}}
            elif kind == "host_error":
                out[host] = {
                    "status": "error",
                    "error": str(ev.get("error", "unknown error")),
                }
    return out


def _server_node_id(host_entry: str) -> str:
    return f"mysql:{host_entry}"


def _cluster_node_id(cluster_name: str) -> str:
    return f"cluster:{cluster_name}"


def _unknown_node_id(host: str | None, port: int | None) -> str:
    return f"unknown:{host or '?'}:{port or 0}"


def _build_repl_edge_id(source_id: str, target_id: str) -> str:
    return f"repl:{source_id}->{target_id}"


def _build_dual_edge_id(a: str, b: str) -> str:
    lo, hi = sorted((a, b))
    return f"dual:{lo}<->{hi}"


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _build_error_node(host_entry: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Build a MySQL node for a failed host collection.

    :param host_entry: Inventory host entry for the failed MySQL service.
    :param record: Merged ``host_error`` record.
    :return: React Flow node payload with error status.
    """
    return {
        "id": _server_node_id(host_entry),
        "type": NODE_TYPE_MYSQL,
        "data": {
            "host_entry": host_entry,
            "error": record.get("error", "unknown error"),
            "status": "error",
        },
    }


def _build_mysql_node(host_entry: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Build a MySQL node for a successfully collected host.

    :param host_entry: Inventory host entry for the MySQL service.
    :param data: Collector payload for the host.
    :return: React Flow node payload with server, replication, and cluster data.
    """
    repl = data.get("replication") or {}
    cluster = data.get("cluster") or {}
    return {
        "id": _server_node_id(host_entry),
        "type": NODE_TYPE_MYSQL,
        "data": {
            "host_entry": host_entry,
            "address": data.get("address"),
            "port": data.get("port"),
            "status": "ok",
            "server": data.get("server") or {},
            "replication": {k: v for k, v in repl.items() if k != "source_uuid"},
            "cluster": cluster or None,
            "gtid_mode": data.get("gtid_mode") or "",
        },
    }


def _index_ok_host(
    host_entry: str,
    record: Mapping[str, Any],
    nodes: list[dict[str, Any]],
    cluster_members: dict[str, list[str]],
    hash_to_node: dict[str, str],
    addr_to_node: dict[tuple[str, int], str],
) -> None:
    """Index one successfully collected host into graph lookup structures.

    :param host_entry: Inventory host entry for the MySQL service.
    :param record: Merged ``host_done`` record.
    :param nodes: Mutable React Flow node list.
    :param cluster_members: Mutable cluster key to member node ids map.
    :param hash_to_node: Mutable server hash to node id index.
    :param addr_to_node: Mutable address+port to node id index.
    """
    node_id = _server_node_id(host_entry)
    data = record.get("data") or {}
    server = data.get("server") or {}
    cluster = data.get("cluster") or {}

    nodes.append(_build_mysql_node(host_entry, data))
    if server_hash := server.get("server_hash"):
        hash_to_node[server_hash] = node_id
    port = _coerce_int(data.get("port"))
    if port is not None and (addr := data.get("address")):
        addr_to_node[(addr, port)] = node_id
    if cluster and cluster.get("cluster_name"):
        cluster_members[cluster["cluster_name"]].append(node_id)


def _build_mysql_nodes(
    host_records: Mapping[str, dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[str]],
    dict[str, str],
    dict[tuple[str, int], str],
]:
    """Build MySQL nodes and lookup indexes from merged host records.

    :param host_records: Merged topology host records.
    :return: Nodes, cluster membership index, server hash index, and address index.
    """
    nodes: list[dict[str, Any]] = []
    cluster_members: dict[str, list[str]] = defaultdict(list)
    hash_to_node: dict[str, str] = {}
    addr_to_node: dict[tuple[str, int], str] = {}

    for host_entry, record in host_records.items():
        if record.get("status") != "ok":
            nodes.append(_build_error_node(host_entry, record))
            continue
        _index_ok_host(
            host_entry,
            record,
            nodes,
            cluster_members,
            hash_to_node,
            addr_to_node,
        )
    return nodes, cluster_members, hash_to_node, addr_to_node


def _first_cluster_data(
    nodes: list[dict[str, Any]], members: list[str]
) -> dict[str, Any]:
    """Return cluster metadata from the first matching member node.

    :param nodes: React Flow node list.
    :param members: MySQL node ids in the cluster.
    :return: Cluster data from a member node, or an empty mapping.
    """
    return next(
        (
            node["data"]["cluster"]
            for node in nodes
            if node["id"] in members and (node.get("data") or {}).get("cluster")
        ),
        {},
    )


def _add_cluster_nodes(
    nodes: list[dict[str, Any]],
    cluster_members: Mapping[str, list[str]],
) -> None:
    """Add synthetic PXC cluster nodes to the graph.

    :param nodes: Mutable React Flow node list.
    :param cluster_members: Cluster key to member node ids map.
    """
    for cluster_name, members in cluster_members.items():
        first_data = _first_cluster_data(nodes, members)
        nodes.append(
            {
                "id": _cluster_node_id(cluster_name),
                "type": NODE_TYPE_CLUSTER,
                "data": {
                    "cluster_name": cluster_name,
                    "size": first_data.get("cluster_size"),
                    "status": first_data.get("cluster_status"),
                    "members": members,
                },
            }
        )


def _build_replication_edge(
    source_node_id: str,
    target_node_id: str,
    repl: Mapping[str, Any],
    gtid_mode: Any,
) -> dict[str, Any]:
    """Build a React Flow edge for one replication relationship.

    :param source_node_id: Source MySQL or unknown-source node id.
    :param target_node_id: Replica MySQL node id.
    :param repl: Replication metadata from the collector.
    :param gtid_mode: Target host GTID mode.
    :return: React Flow replication edge payload.
    """
    return {
        "id": _build_repl_edge_id(source_node_id, target_node_id),
        "source": source_node_id,
        "target": target_node_id,
        "type": EDGE_TYPE_REPLICATION,
        "data": {
            "status": repl.get("repl_status"),
            "io_running": repl.get("io_running"),
            "sql_running": repl.get("sql_running"),
            "seconds_behind": repl.get("seconds_behind"),
            "auto_position": repl.get("auto_position"),
            "filter": repl.get("repl_filter"),
            "gtid_mode": gtid_mode,
        },
    }


def _add_replication_edges(
    host_records: Mapping[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    hash_to_node: Mapping[str, str],
    addr_to_node: dict[tuple[str, int], str],
) -> dict[str, str]:
    """Add replication edges and collect primary hashes seen by replicas.

    :param host_records: Merged topology host records.
    :param nodes: Mutable React Flow node list.
    :param edges: Mutable React Flow edge list.
    :param hash_to_node: Server hash to node id index.
    :param addr_to_node: Mutable address+port to node id index.
    :return: Replica node id to computed primary hash map.
    """
    primary_hashes_seen: dict[str, str] = {}
    for host_entry, record in host_records.items():
        if record.get("status") != "ok":
            continue
        data = record.get("data") or {}
        repl = data.get("replication") or {}
        if not repl.get("source_host"):
            continue
        source_node_id = _resolve_or_add_replication_source(
            repl, hash_to_node, addr_to_node, nodes
        )
        target_node_id = _server_node_id(host_entry)
        edges.append(
            _build_replication_edge(
                source_node_id, target_node_id, repl, data.get("gtid_mode")
            )
        )
        primary_hashes_seen[target_node_id] = make_primary_hash(
            repl.get("source_server_id"),
            repl.get("source_uuid"),
            repl.get("source_port"),
        )
    return primary_hashes_seen


def _add_dual_primary_edges(
    edges: list[dict[str, Any]],
    primary_hashes_seen: Mapping[str, str],
    hash_to_node: Mapping[str, str],
) -> None:
    """Add dual-primary edges for mutually replicating MySQL nodes.

    :param edges: Mutable React Flow edge list.
    :param primary_hashes_seen: Replica node id to computed primary hash map.
    :param hash_to_node: Server hash to MySQL node id index.
    """
    node_to_hash = {
        node_id: server_hash for server_hash, node_id in hash_to_node.items()
    }
    pair_seen: set[str] = set()
    for replica_id, primary_hash in primary_hashes_seen.items():
        primary_id = hash_to_node.get(primary_hash)
        if not primary_id or primary_id == replica_id:
            continue
        reverse_primary_hash = primary_hashes_seen.get(primary_id)
        if reverse_primary_hash is None:
            continue
        if reverse_primary_hash != node_to_hash.get(replica_id):
            continue
        edge_id = _build_dual_edge_id(replica_id, primary_id)
        if edge_id in pair_seen:
            continue
        pair_seen.add(edge_id)
        edges.append(
            {
                "id": edge_id,
                "source": replica_id,
                "target": primary_id,
                "type": EDGE_TYPE_DUAL_PRIMARY,
                "data": {},
            }
        )


def _build_graph_summary(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    cluster_members: Mapping[str, list[str]],
) -> dict[str, int]:
    """Build aggregate graph counts for the topology response.

    :param nodes: React Flow node list.
    :param edges: React Flow edge list.
    :param cluster_members: Cluster key to member node ids map.
    :return: Host, status, cluster, and edge counts.
    """
    mysql_nodes = [node for node in nodes if node["type"] == NODE_TYPE_MYSQL]
    return {
        "host_count": len(mysql_nodes),
        "ok_count": sum(
            1 for node in mysql_nodes if (node.get("data") or {}).get("status") == "ok"
        ),
        "error_count": sum(
            1
            for node in mysql_nodes
            if (node.get("data") or {}).get("status") == "error"
        ),
        "cluster_count": len(cluster_members),
        "edge_count": len(edges),
    }


def build_topology_graph(
    host_records: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build the React Flow ``{nodes, edges, summary}`` graph from per-host records.

    :param host_records: Output of :func:`merge_host_records` - ``{host_entry:
        {"status": "ok"|"error", "data"|"error": ...}}``.
    :return: Graph dict with ``nodes`` (mysql + cluster + unknown_source) and
        ``edges`` (replication + dual_primary). The shape matches what the
        ``TopologyView`` React component expects.
    """
    nodes, cluster_members, hash_to_node, addr_to_node = _build_mysql_nodes(
        host_records
    )
    edges: list[dict[str, Any]] = []
    _add_cluster_nodes(nodes, cluster_members)
    primary_hashes_seen = _add_replication_edges(
        host_records, nodes, edges, hash_to_node, addr_to_node
    )
    _add_dual_primary_edges(edges, primary_hashes_seen, hash_to_node)
    return {
        "nodes": nodes,
        "edges": edges,
        "summary": _build_graph_summary(nodes, edges, cluster_members),
    }


def _resolve_or_add_replication_source(
    repl: Mapping[str, Any],
    hash_to_node: Mapping[str, str],
    addr_to_node: MutableMapping[tuple[str, int], str],
    nodes: list[dict[str, Any]],
) -> str:
    """Find or synthesize the node id for a replica's replication source.

    Prefer hash-based matching (same identity even when the source IP
    differs), fall back to address+port, and synthesize an
    ``unknown_source`` node when neither matches so the graph still shows
    the dependency.
    """
    if (
        repl.get("source_server_id") is not None
        and repl.get("source_uuid")
        and repl.get("source_port") is not None
    ):
        candidate = make_primary_hash(
            repl["source_server_id"], repl["source_uuid"], repl["source_port"]
        )
        if node_id := hash_to_node.get(candidate):
            return node_id
    source_host = repl.get("source_host")
    source_port = _coerce_int(repl.get("source_port")) or DEFAULT_MYSQL_PORT
    source_key = (source_host, source_port)
    if node_id := addr_to_node.get(source_key):
        return node_id
    unknown_id = _unknown_node_id(source_host, source_port)
    nodes.append(
        {
            "id": unknown_id,
            "type": NODE_TYPE_UNKNOWN,
            "data": {
                "address": source_host,
                "port": source_port,
                "reason": "Replication source not in inventory",
            },
        }
    )
    addr_to_node[source_key] = unknown_id
    return unknown_id


def build_graph_from_stdouts(stdout_blobs: list[str]) -> dict[str, Any]:
    """Build a graph from raw shard stdout without retaining collector output.

    :param stdout_blobs: Raw stdout blobs from completed topology shard tasks.
    :return: Merged topology graph ready for the API response.
    """
    streams = [parse_ndjson(blob) for blob in stdout_blobs]
    merged = merge_host_records(*streams)
    return build_topology_graph(merged)


def build_topology_meta(
    *, target: str, hosts: list[str], extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Assemble the ``meta`` dict for a topology ``run-python`` task dispatch.

    :param target: Executor target host name for the shard.
    :param hosts: Host entries assigned to the shard.
    :param extra: Optional additional collector config values.
    :return: Task meta payload including config JSON and shared requirements.
    """
    config: dict[str, Any] = {"hosts": hosts}
    if extra:
        config.update(extra)
    return {
        "config": json.dumps(config, separators=(",", ":")),
        "target": target,
        "requirements": TOPOLOGY_PAYLOAD_REQUIREMENTS,
        "_job_id_prefix": TOPOLOGY_JOB_PREFIX,
    }


def shard_hosts(hosts: list[str], shards: int) -> list[list[str]]:
    """Split ``hosts`` into ``shards`` round-robin chunks.

    Preserve host ordering inside each shard chunk.

    :param hosts: Host entries to distribute.
    :param shards: Requested shard count.
    :return: Non-empty shard chunks in dispatch order.
    :raises ValueError: When ``shards`` is less than 1.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")
    if shards == 1 or len(hosts) <= 1:
        return [list(hosts)]
    shards = min(shards, len(hosts))
    out: list[list[str]] = [[] for _ in range(shards)]
    for index, host in enumerate(hosts):
        out[index % shards].append(host)
    return [chunk for chunk in out if chunk]
