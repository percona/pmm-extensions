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

"""Test that a host is probed for its own sake, not only through its services.

The app used to dispatch strictly per resolved service, which meant the machines it
could say the least about were exactly the ones worth an install decision: a PMM
client with no database has no service to be reached through. So a dispatch now goes
to every executor host in the estate, and the payload prints a record for the host
whether or not it has any targets.

That record is also what stops host attributes being read off whichever service
happened to answer — they belong to the host, they are collected once, and now they
are reported once.
"""

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from python_minifier import minify

from app.extensions.apps.om_inventory.dispatch import (
    build_config,
    parse_ndjson,
    probe_all,
)
from app.extensions.apps.om_inventory.inventory import InventoryService
from app.extensions.apps.om_inventory.mapping import MappedService
from app.extensions.apps.om_inventory.models import NodeResolution
from app.extensions.apps.om_inventory.payload import probe
from app.tasks.execution.utils import minify_file_content

HOST_LINE = (
    '{"service": null, "collected_at": 1, "status": "ok", '
    '"system": {"os_name": "Ubuntu 24.04"}, "binary_version": null}'
)
SERVICE_LINE = (
    '{"service": "db00", "collected_at": 1, "status": "ok", '
    '"binary_version": "7.0.39-21"}'
)


def mapped(
    name: str, host: str | None, external_id: str | None = None
) -> MappedService:
    """Pair a service with an executor host.

    :param name: The service name.
    :param host: The executor host, or ``None`` for an orphan.
    :param external_id: PMM's service UUID. Defaults to a name-derived id, which is
        fine for every test except the one that needs two *different* ids behind one
        *shared* name.
    :return: The mapped service.
    """
    return MappedService(
        service=InventoryService(
            service_id=1,
            external_id=external_id if external_id is not None else f"svc-{name}",
            name=name,
            port=27017,
            node_name=name,
            node_address=None,
        ),
        executor_host=host,
        resolution=NodeResolution.NAME if host else NodeResolution.ORPHANED,
    )


class TestParseNdjson:
    """Assert the host record is told apart from the service records."""

    def test_splits_the_host_record_from_the_service_records(self) -> None:
        """Split the host record out by ``service: null``, the whole discriminator."""
        records, host_record = parse_ndjson(f"{HOST_LINE}\n{SERVICE_LINE}")

        assert list(records) == ["db00"]
        assert host_record is not None
        assert host_record["system"]["os_name"] == "Ubuntu 24.04"

    def test_a_host_with_no_database_still_reports(self) -> None:
        """Report an empty host's own line rather than dropping it.

        This is the case the app could not express at all before: no targets meant no
        output, and the payload refused to run.
        """
        records, host_record = parse_ndjson(HOST_LINE)

        assert records == {}
        assert host_record is not None

    def test_noise_around_the_records_is_ignored(self) -> None:
        """Ignore pip and job-template noise on the stream the payload writes to."""
        stdout = f"Collecting pymongo\n{HOST_LINE}\nnot json\n{SERVICE_LINE}\n"

        records, host_record = parse_ndjson(stdout)

        assert list(records) == ["db00"]
        assert host_record is not None

    def test_no_output_yields_no_host_record(self) -> None:
        """Distinguish "did not answer" from "has no database" by returning ``None``."""
        records, host_record = parse_ndjson("")

        assert records == {}
        assert host_record is None


class TestServiceIdPreventsANameCollision:
    """Keep two same-named services on one host from overwriting each other's record.

    ``build_config`` used to send only the service *name* as a target's identity, and
    ``parse_ndjson`` keyed the parsed records the same way — the payload never saw a
    service id at all ("the service id never reaches the node"). Names are not unique
    per node, so two same-named services dispatched to one executor host produced two
    NDJSON lines under one key, and the second silently replaced the first.
    """

    def test_two_same_named_services_are_recorded_distinctly(self) -> None:
        """Follow ``build_config`` -> ``probe`` -> ``parse_ndjson`` end to end.

        Two mapped services share a name (as two shards of the same replica set on
        one host well might) but carry different PMM service ids. Both must survive
        as two distinct entries once their NDJSON comes back.
        """
        entries = [
            mapped("db00", "node00", external_id="svc-id-1"),
            mapped("db00", "node00", external_id="svc-id-2"),
        ]

        config = json.loads(build_config(entries))
        targets = config["targets"]
        assert [target["service_id"] for target in targets] == [
            "svc-id-1",
            "svc-id-2",
        ]
        assert [target["service"] for target in targets] == ["db00", "db00"], (
            "the collision case only exists when both targets share a name"
        )

        stdout = "\n".join(
            json.dumps(probe.probe(target, {"probe_database": False}, {}))
            for target in targets
        )

        records, _host_record = parse_ndjson(stdout)

        assert set(records) == {"svc-id-1", "svc-id-2"}


class TestProbeAllTargets:
    """Assert which hosts a sweep dispatches to."""

    @pytest.mark.asyncio
    async def test_dispatches_to_a_host_that_serves_no_service(self) -> None:
        """Dispatch to the empty host too, or it can never be described.

        :return: Nothing.
        """
        seen: dict[str, list] = {}

        async def fake_probe_host(_api, host, entries):
            seen[host] = entries
            return type("R", (), {"executor_host": host})()

        with patch(
            "app.extensions.apps.om_inventory.dispatch.probe_host",
            AsyncMock(side_effect=fake_probe_host),
        ):
            await probe_all(
                AsyncMock(),
                [mapped("db00", "db00")],
                executor_hosts=["db00", "pmm-client-node00"],
            )

        assert set(seen) == {"db00", "pmm-client-node00"}
        # A host that serves services keeps them as targets...
        assert [entry.service.name for entry in seen["db00"]] == ["db00"]
        # ...and one that serves none is dispatched to with an empty target list.
        assert seen["pmm-client-node00"] == []

    @pytest.mark.asyncio
    async def test_reports_each_host_as_it_comes_back(self) -> None:
        """Hand every host's result to the caller's callback, for progress."""
        landed: list[str] = []

        async def fake_probe_host(_api, host, _entries):
            return type("R", (), {"executor_host": host})()

        async def on_host_done(result) -> None:
            landed.append(result.executor_host)

        with patch(
            "app.extensions.apps.om_inventory.dispatch.probe_host",
            AsyncMock(side_effect=fake_probe_host),
        ):
            await probe_all(
                AsyncMock(),
                [],
                executor_hosts=["db00", "db01"],
                on_host_done=on_host_done,
            )

        assert sorted(landed) == ["db00", "db01"]

    @pytest.mark.asyncio
    async def test_an_orphaned_service_adds_no_dispatch(self) -> None:
        """Skip an orphan, which has no executor and so nowhere to dispatch.

        :return: Nothing.
        """
        seen: dict[str, list] = {}

        async def fake_probe_host(_api, host, entries):
            seen[host] = entries
            return type("R", (), {"executor_host": host})()

        with patch(
            "app.extensions.apps.om_inventory.dispatch.probe_host",
            AsyncMock(side_effect=fake_probe_host),
        ):
            await probe_all(AsyncMock(), [mapped("db01", None)])

        assert seen == {}


class TestThePayloadRunsOnOldPython:
    """Assert the payload survives minification into something an old Python parses.

    The Tasks layer runs every payload through ``python-minifier`` before dispatch,
    and the minifier normalises inner string quotes to double. So
    ``f"...{target['host']}..."`` - legal on every Python there has ever been - is
    rewritten to ``f"...{target["host"]}..."``, which is PEP 701 and parses only on
    3.12 and later.

    The payload runs on whatever Python a monitored host happens to have, and that is
    not ours to choose. This workspace's own ``pmm-server`` carries 3.9: every probe
    of it failed with ``SyntaxError: f-string: expecting '}'`` while the database
    hosts, on 3.12, were fine. A syntax error that appears only after minification and
    only on some hosts is invisible in review and in local runs, which is why it is
    asserted rather than remembered.
    """

    def test_minification_produces_no_nested_quote_f_strings(self) -> None:
        """Refuse an f-string carrying its own quote character inside an expression."""
        source = Path(probe.__file__).read_text(encoding="utf-8")

        minified = minify(source, rename_locals=True, rename_globals=True)

        # An f-string opened with `"` that contains a `"` before its closing brace.
        offenders = re.findall(r'f"(?:[^"\\]|\\.)*?\{[^{}]*"', minified)
        assert offenders == [], (
            "these would be SyntaxErrors on Python < 3.12: bind the value to a name "
            f"before the f-string instead. {offenders}"
        )

    def test_find_unregistered_survives_minification(self) -> None:
        """Return the same answer from ``find_unregistered`` minified as unminified.

        Minifies the *whole file*, which is what dispatch does: ``hoist_literals``
        lifts every function's ``"pid"`` and ``"port"`` into shared module globals,
        and ``rename_globals`` then reuses those same short names as comprehension
        iteration variables. That reuse is not safe everywhere — on the 3.12 builds
        named in :func:`~app.extensions.apps.om_inventory.payload.probe.find_unregistered`'s
        own comment the collision raises ``UnboundLocalError``, which is why that
        function is written with loops.

        The minified function runs in *this* interpreter, so the coverage follows
        the test matrix: ``.github/workflows/python.yaml`` runs this suite on
        3.11.9, 3.12.3 and 3.13, and 3.12.3 is inside the affected range, so a
        revert to the comprehension form fails that leg rather than reaching a
        monitored host. That leg is load-bearing for this test — a bump past
        3.12.3 takes the coverage with it.

        The renamed top-level name is found by signature (``rename_globals``
        renames it away, unpredictably across minifier versions) rather than by
        executing the module, since ``main()`` runs for real under
        ``if __name__ == "__main__"`` and this only wants one function from it.
        """
        source = Path(probe.__file__).read_text(encoding="utf-8")
        minified = minify_file_content(source, "py")

        match = re.search(
            r"def (\w+)\(processes,targets,matched_pids=frozenset\(\)\)", minified
        )
        assert match is not None, (
            f"find_unregistered's signature is not recognisable post-minification: "
            f"{minified}"
        )

        namespace: dict[str, object] = {"__name__": "<minified probe.py>"}
        exec(compile(minified, "<minified probe.py>", "exec"), namespace)
        find_unregistered = namespace[match.group(1)]

        matched = {"pid": 1, "port": None}
        unmatched = {"pid": 2, "port": None}
        registered = {"pid": 3, "port": 27017}
        stranger = {"pid": 4, "port": 27018}
        targets = [{"port": 27017}]

        assert find_unregistered([matched], targets, frozenset({1})) == []
        assert find_unregistered([unmatched], targets, frozenset()) == [unmatched]
        assert find_unregistered([registered], targets, frozenset()) == []
        assert find_unregistered([stranger], targets, frozenset()) == [stranger]
        assert find_unregistered(
            [matched, unmatched, registered, stranger], targets, frozenset({1})
        ) == [unmatched, stranger]
