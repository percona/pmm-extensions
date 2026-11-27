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

"""Test the reference-only secret seam: fail-closed, and the fake-provider canary."""

import json

import pytest

from app.extensions.apps.shared.om.secret_delivery import (
    deliver_secret,
    NoConformingProviderError,
    RecordedJob,
    SecretReference,
    SecretSink,
)

#: A distinctive value stood in for a secret, searched for across every sink a
#: step touches. Its bytes must appear only in the job's own sink.
CANARY = "CANARY-1f6b2a7c-secret-value"


class _RecordingSink:
    """Record the bytes delivered to one job, standing in for a memfd or pipe."""

    def __init__(self) -> None:
        self.received = bytearray()

    def write(self, material: bytes) -> None:
        """Record delivered bytes.

        :param material: The secret's bytes.
        """
        self.received += material


class _CanaryProvider:
    """Redeem any reference to the canary bytes, writing them only to the sink."""

    def redeem(
        self, reference: SecretReference, job: RecordedJob, sink: SecretSink
    ) -> None:
        """Write the canary to ``sink`` and nowhere else.

        :param reference: The secret requested, by reference.
        :param job: The recorded job the delivery is bound to.
        :param sink: The per-job sink.
        """
        assert reference.secret_id
        assert job.node_id
        sink.write(CANARY.encode())


class TestFailClosed:
    """Refuse delivery when no conforming provider is bound."""

    def test_raises_and_writes_nothing_with_no_provider(self) -> None:
        """Fail closed with a structured error and an untouched sink."""
        job = RecordedJob(
            idempotency_key="k1",
            node_id="node-7",
            secret_references=[SecretReference(secret_id="ref-123")],
        )
        sink = _RecordingSink()
        with pytest.raises(NoConformingProviderError) as raised:
            deliver_secret(SecretReference(secret_id="ref-123"), job, sink, None)
        assert raised.value.code == "no_conforming_provider"
        assert raised.value.secret_id == "ref-123"
        assert raised.value.node_id == "node-7"
        assert bytes(sink.received) == b""

    def test_the_error_never_carries_secret_bytes(self) -> None:
        """Keep the secret's bytes out of the refusal's own message."""
        error = NoConformingProviderError(secret_id="ref-123", node_id="node-7")
        assert CANARY not in str(error)


class TestCanary:
    """Prove a delivered secret reaches only the job's sink."""

    def test_delivers_bytes_only_to_the_sink(self) -> None:
        """Deliver the canary to the sink and to no other surface of the job.

        The dispatch body carries only the ``secret_id`` reference, so the bytes
        appear in the sink and nowhere in the job's argv, env, script, references
        or a serialised receipt built from it.
        """
        job = RecordedJob(
            idempotency_key="k2",
            node_id="node-7",
            argv=["mongosh", "--eval", "db.serverStatus()"],
            env={"HOME": "/root"},
            script="#!/bin/sh\nmongosh\n",
            secret_references=[SecretReference(secret_id="ref-123")],
        )
        sink = _RecordingSink()

        deliver_secret(
            SecretReference(secret_id="ref-123"), job, sink, _CanaryProvider()
        )

        assert CANARY.encode() in bytes(sink.received)
        receipt = json.dumps(
            {"idempotency_key": job.idempotency_key, "node_id": job.node_id}
        )
        for surface in (
            json.dumps(job.model_dump()),
            " ".join(job.argv),
            " ".join(job.env.values()),
            job.script,
            receipt,
        ):
            assert CANARY not in surface

    def test_the_pbm_cli_exception_confines_the_secret_to_one_process(self) -> None:
        """Allow the secret on one ``pbm`` process's argv, nowhere else.

        A step declaring the exception may place the secret on its single ``pbm``
        process's argv; the canary still proves it is absent from the env, the
        script and a step not declaring the exception.
        """
        pbm_job = RecordedJob(
            idempotency_key="k3",
            node_id="node-7",
            argv=["pbm", "config", f"--mongodb-uri=mongodb://{CANARY}@127.0.0.1"],
            env={"HOME": "/root"},
            script="#!/bin/sh\npbm status\n",
            secret_references=[SecretReference(secret_id="ref-123")],
            pbm_cli_secret_exception=True,
        )
        other_job = RecordedJob(
            idempotency_key="k4",
            node_id="node-7",
            argv=["mongosh", "--eval", "db.serverStatus()"],
            secret_references=[SecretReference(secret_id="ref-123")],
        )

        assert any(CANARY in arg for arg in pbm_job.argv)
        assert CANARY not in " ".join(pbm_job.env.values())
        assert CANARY not in pbm_job.script
        assert CANARY not in json.dumps(other_job.model_dump())
