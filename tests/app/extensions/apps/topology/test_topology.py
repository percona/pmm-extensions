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

"""Unit tests for the inventory topology graph builder."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from app.extensions.apps.topology.topology import (
    build_graph_from_stdouts,
    build_topology_graph,
    build_topology_meta,
    EDGE_TYPE_DUAL_PRIMARY,
    EDGE_TYPE_REPLICATION,
    make_primary_hash,
    merge_host_records,
    NODE_TYPE_CLUSTER,
    NODE_TYPE_MYSQL,
    NODE_TYPE_UNKNOWN,
    parse_ndjson,
    shard_hosts,
)


def _host_data(
    host: str,
    *,
    server_hash: str,
    read_only: str = "RW",
    cluster_name: str | None = None,
    repl: dict[str, Any] | None = None,
    server_id: int = 1,
    server_uuid: str = "uuid-1",
    port: int = 3306,
    address: str | None = None,
) -> dict[str, Any]:
    return {
        "event": "host_done",
        "host": host,
        "data": {
            "host_entry": host,
            "address": address or host.split(":", maxsplit=1)[0],
            "port": port,
            "server": {
                "server_hash": server_hash,
                "server_id": server_id,
                "server_uuid": server_uuid,
                "port": port,
                "version": "8.0.42",
                "log_bin": "ON",
                "read_only": read_only,
            },
            "replication": repl or {"source_host": None},
            "cluster": (
                {
                    "cluster_name": cluster_name,
                    "cluster_size": "3",
                    "cluster_status": "Primary",
                }
                if cluster_name
                else {}
            ),
            "gtid_mode": "ON",
        },
    }


class TestParseNdjson:
    """NDJSON parsing of one shard's stdout."""

    def test_parses_valid_lines_and_skips_garbage(self) -> None:
        """Blank and non-JSON lines are dropped without crashing the parse."""
        stdout = '{"event":"host_done","host":"a"}\n\nnot-json\n{"event":"complete","ok":1,"err":0}\n'
        events = parse_ndjson(stdout)
        assert [e["event"] for e in events] == ["host_done", "complete"]


class TestMergeHostRecords:
    """Per-host record merging across multiple shard streams."""

    def test_two_shards_merge(self) -> None:
        """Distinct-host shards combine into a single host->record map."""
        shard_a = [_host_data("h1:3306", server_hash="A")]
        shard_b = [_host_data("h2:3306", server_hash="B")]
        merged = merge_host_records(shard_a, shard_b)
        assert set(merged) == {"h1:3306", "h2:3306"}
        assert merged["h1:3306"]["status"] == "ok"

    def test_host_error_replaces_prior_ok(self) -> None:
        """A later ``host_error`` event clobbers an earlier ``host_done`` for the same host."""
        shard_a = [_host_data("h1:3306", server_hash="A")]
        shard_b = [{"event": "host_error", "host": "h1:3306", "error": "oops"}]
        merged = merge_host_records(shard_a, shard_b)
        assert merged["h1:3306"] == {"status": "error", "error": "oops"}


class TestGraphBuilder:
    """End-to-end graph construction from merged host records."""

    def test_single_replica_resolves_via_hash(self) -> None:
        """A replica's source resolves to its primary node via primary-hash matching."""
        primary = _host_data(
            "primary:3306",
            server_hash=make_primary_hash(10, "uuid-primary", 3306),
            server_id=10,
            server_uuid="uuid-primary",
        )
        replica = _host_data(
            "replica:3306",
            server_hash="replica-hash",
            read_only="RO",
            server_id=20,
            server_uuid="uuid-replica",
            repl={
                "source_host": "primary",
                "source_port": 3306,
                "source_server_id": 10,
                "source_uuid": "uuid-primary",
                "io_running": "Yes",
                "sql_running": "Yes",
                "seconds_behind": 0,
                "repl_status": "ok",
                "repl_filter": "none",
                "auto_position": 1,
            },
        )
        expected_host_count = 2
        graph = build_topology_graph(merge_host_records([primary, replica]))
        edges = [e for e in graph["edges"] if e["type"] == EDGE_TYPE_REPLICATION]
        assert len(edges) == 1
        assert edges[0]["source"] == "mysql:primary:3306"
        assert edges[0]["target"] == "mysql:replica:3306"
        assert graph["summary"]["host_count"] == expected_host_count

    def test_primary_hash_matches_collector_concat_ws_format(self) -> None:
        """Pin make_primary_hash to the collector's ``CONCAT_WS('|', ...)`` digest.

        The collector computes each host's ``server_hash`` in MySQL as
        ``SHA2(CONCAT_WS('|', @@server_id, @@server_uuid, @@port), 256)``; the
        replica side recomputes the expected primary hash here. The two must be
        byte-identical or hash-based correlation silently stops matching, so
        assert against an independently-built digest (not ``make_primary_hash``
        itself) and confirm a falsy-but-real ``0`` renders as ``"0"``.
        """
        assert (
            make_primary_hash(10, "uuid-primary", 3306)
            == hashlib.sha256(b"10|uuid-primary|3306").hexdigest()
        )
        assert (
            make_primary_hash(0, "uuid-zero", 3306)
            == hashlib.sha256(b"0|uuid-zero|3306").hexdigest()
        )

    def test_replica_resolves_when_primary_hash_uses_collector_format(self) -> None:
        """Correlate a replica to its primary purely by the collector-format hash.

        The replica's ``source_host`` differs from the primary's address, so
        address+port matching cannot resolve it -- only the primary-hash path
        can. The primary's ``server_hash`` is built the way the collector's
        ``CONCAT_WS('|', ...)`` query does (independently of
        ``make_primary_hash``), so the edge appears only when the two formats
        agree.
        """
        collector_hash = hashlib.sha256(b"10|uuid-primary|3306").hexdigest()
        primary = _host_data(
            "primary:3306",
            server_hash=collector_hash,
            server_id=10,
            server_uuid="uuid-primary",
        )
        replica = _host_data(
            "replica:3306",
            server_hash="replica-hash",
            read_only="RO",
            server_id=20,
            server_uuid="uuid-replica",
            repl={
                "source_host": "primary-vip",
                "source_port": 3306,
                "source_server_id": 10,
                "source_uuid": "uuid-primary",
                "repl_status": "ok",
            },
        )
        graph = build_topology_graph(merge_host_records([primary, replica]))
        edges = [e for e in graph["edges"] if e["type"] == EDGE_TYPE_REPLICATION]
        assert len(edges) == 1
        assert edges[0]["source"] == "mysql:primary:3306"
        assert edges[0]["target"] == "mysql:replica:3306"
        assert not [n for n in graph["nodes"] if n["type"] == NODE_TYPE_UNKNOWN]

    def test_unknown_source_node_synthesised(self) -> None:
        """A replica pointing at an out-of-inventory primary gets a synthetic ``unknown_source`` node."""
        replica = _host_data(
            "r:3306",
            server_hash="r-hash",
            read_only="RO",
            repl={
                "source_host": "external-primary",
                "source_port": 3306,
                "source_server_id": 999,
                "source_uuid": "external-uuid",
                "repl_status": "ok",
            },
        )
        graph = build_topology_graph(merge_host_records([replica]))
        unknown = [n for n in graph["nodes"] if n["type"] == NODE_TYPE_UNKNOWN]
        assert len(unknown) == 1
        assert unknown[0]["data"]["address"] == "external-primary"

    def test_dual_primary_emits_extra_edge(self) -> None:
        """Mutual replication between two hosts produces a dual-primary edge in addition to the two replication edges."""
        a_hash = make_primary_hash(1, "uuid-a", 3306)
        b_hash = make_primary_hash(2, "uuid-b", 3306)
        host_a = _host_data(
            "a:3306",
            server_hash=a_hash,
            server_id=1,
            server_uuid="uuid-a",
            repl={
                "source_host": "b",
                "source_port": 3306,
                "source_server_id": 2,
                "source_uuid": "uuid-b",
                "repl_status": "ok",
            },
        )
        host_b = _host_data(
            "b:3306",
            server_hash=b_hash,
            server_id=2,
            server_uuid="uuid-b",
            repl={
                "source_host": "a",
                "source_port": 3306,
                "source_server_id": 1,
                "source_uuid": "uuid-a",
                "repl_status": "ok",
            },
        )
        graph = build_topology_graph(merge_host_records([host_a, host_b]))
        dual = [e for e in graph["edges"] if e["type"] == EDGE_TYPE_DUAL_PRIMARY]
        assert len(dual) == 1

    def test_pxc_cluster_node_groups_members(self) -> None:
        """Hosts sharing a ``cluster_name`` are grouped under one synthetic cluster node listing them as members."""
        nodes = [
            _host_data(f"n{i}:3306", server_hash=f"h{i}", cluster_name="prod-cluster")
            for i in range(3)
        ]
        graph = build_topology_graph(merge_host_records(nodes))
        cluster_nodes = [n for n in graph["nodes"] if n["type"] == NODE_TYPE_CLUSTER]
        assert len(cluster_nodes) == 1
        assert sorted(cluster_nodes[0]["data"]["members"]) == sorted(
            f"mysql:n{i}:3306" for i in range(3)
        )

    def test_error_host_renders_with_error_status(self) -> None:
        """A ``host_error`` event still produces a MySQL node so the UI can show the failure."""
        events = [{"event": "host_error", "host": "down:3306", "error": "timed out"}]
        graph = build_topology_graph(merge_host_records(events))
        mysql_nodes = [n for n in graph["nodes"] if n["type"] == NODE_TYPE_MYSQL]
        assert mysql_nodes[0]["data"]["status"] == "error"
        assert graph["summary"]["error_count"] == 1


class TestBuildGraphFromStdouts:
    """Public entry point that combines NDJSON parsing, merging, and graph build."""

    def test_round_trips_through_ndjson(self) -> None:
        """Serialised host events round-trip back into a graph."""
        expected_host_count = 2
        stdout = "\n".join(
            json.dumps(_host_data(f"h{i}:3306", server_hash=f"x{i}"))
            for i in range(expected_host_count)
        )
        graph = build_graph_from_stdouts([stdout])
        assert graph["summary"]["host_count"] == expected_host_count


class TestShardHosts:
    """Round-robin sharding of host lists across executor targets."""

    @pytest.mark.parametrize("shards", [1, 2, 3])
    def test_round_robin_split_preserves_all_hosts(self, shards: int) -> None:
        """Sharding never drops or duplicates a host across the resulting chunks."""
        hosts = [f"h{i}:3306" for i in range(7)]
        chunks = shard_hosts(hosts, shards)
        flattened = sorted(host for chunk in chunks for host in chunk)
        assert flattened == sorted(hosts)
        assert len(chunks) == min(shards, len(hosts))

    def test_one_shard_returns_single_chunk(self) -> None:
        """A single shard contains every host."""
        assert shard_hosts(["a", "b"], 1) == [["a", "b"]]

    @pytest.mark.parametrize("shards", [0, -1, -7])
    def test_invalid_shards_rejected(self, shards: int) -> None:
        """Shard count must match the API contract: at least one shard."""
        with pytest.raises(ValueError, match="shards must be >= 1"):
            shard_hosts(["a"], shards)


class TestBuildTopologyMeta:
    """``run-python`` task meta dict assembly."""

    def test_includes_required_keys_for_run_python(self) -> None:
        """Meta dict carries the JSON config, executor target, pip requirements, and job-id prefix."""
        meta = build_topology_meta(target="exec-1", hosts=["h1:3306", "h2:3306"])
        assert meta["target"] == "exec-1"
        config = json.loads(meta["config"])
        assert config["hosts"] == ["h1:3306", "h2:3306"]
        assert "PyMySQL" in meta["requirements"]
        assert meta["_job_id_prefix"] == "topology"
