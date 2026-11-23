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

"""Define the only way a secret reaches a step: a reference-only, fail-closed seam.

The dispatch body carries a ``secret_id`` reference, never bytes. Bytes move only
on a conforming provider's redemption response, to a per-job sink the provider
owns (a pipe to stdin or a ``0700`` ``memfd``/``tmpfs``) - never argv, env, a
script, a URL, headers, the receipt, task history, the step result or any log.

Fail-closed is the target state: with no conforming provider bound the interface
refuses, writes no receipt and no status, and the step is not dispatched. The
token/grant redemption, deposit, revocation and the end-to-end canary are a named
follow-up and a blocking dependency; this module ships only the reference-only
interface and the fail-closed gate. A dev-preview deployment binds a conforming
provider that bridges to the existing delivery; its internals live with that
bridge, not here.

One narrow exception exists for the ``pbm`` CLI, which reads its URI only from
argv or environment: a step may set the secret on that one process, declared by
:attr:`RecordedJob.pbm_cli_secret_exception`. The secret still reaches no other
sink.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "NoConformingProviderError",
    "RecordedJob",
    "SecretProvider",
    "SecretReference",
    "SecretSink",
    "deliver_secret",
]


class SecretReference(BaseModel):
    """Name a secret by reference, the only form a dispatch body carries.

    :param secret_id: The opaque id of a secret held in encrypted ``om_secrets``.
        Never the secret's bytes.
    """

    model_config = ConfigDict(frozen=True)

    secret_id: str


class RecordedJob(BaseModel):
    """Describe the recorded executor job a secret is delivered to.

    The dispatch body carries ``secret_references`` - ids only - and never secret
    bytes. Delivery binds to this one job and its one node.

    :param idempotency_key: The dispatch's identity, which names the on-node artifact.
    :param node_id: The PMM node the job runs on.
    :param argv: The command line, carrying no secret bytes unless
        ``pbm_cli_secret_exception`` is set.
    :param env: The job environment, carrying no secret bytes unless
        ``pbm_cli_secret_exception`` is set.
    :param script: The job's script body, carrying no secret bytes.
    :param secret_references: The secrets the step needs, by reference.
    :param pbm_cli_secret_exception: Whether this step uses the one narrow exception
        that lets a secret reach a single ``pbm`` process's argv or environment.
    """

    model_config = ConfigDict(frozen=True)

    idempotency_key: str
    node_id: str
    argv: list[str] = []
    env: dict[str, str] = {}
    script: str = ""
    secret_references: list[SecretReference] = []
    pbm_cli_secret_exception: bool = False


class NoConformingProviderError(Exception):
    """Signal that no conforming secret provider is bound, so delivery is refused.

    Carries the refusal's structured identity and never the secret's bytes.

    :param secret_id: The secret that could not be delivered, by reference.
    :param node_id: The node the step would have run on.
    """

    code = "no_conforming_provider"

    def __init__(self, *, secret_id: str, node_id: str) -> None:
        self.secret_id = secret_id
        self.node_id = node_id
        super().__init__(
            f"no conforming secret provider for {secret_id} on node {node_id}"
        )


@runtime_checkable
class SecretSink(Protocol):
    """Receive secret bytes out of band, the only place they land on a node."""

    def write(self, material: bytes) -> None:
        """Write delivered secret bytes to the per-job sink.

        :param material: The secret's bytes.
        """


@runtime_checkable
class SecretProvider(Protocol):
    """Redeem a secret reference to bytes, writing them only to the job's sink."""

    def redeem(
        self, reference: SecretReference, job: RecordedJob, sink: SecretSink
    ) -> None:
        """Deliver the referenced secret's bytes to ``sink`` for this one job.

        A conforming provider binds the grant to exactly this ``job`` and its node
        and writes the bytes only to ``sink``; it returns nothing, so no caller
        holds the bytes.

        :param reference: The secret to deliver, by reference.
        :param job: The recorded job the delivery is bound to.
        :param sink: The per-job sink the bytes are written to.
        """


def deliver_secret(
    reference: SecretReference,
    job: RecordedJob,
    sink: SecretSink,
    provider: SecretProvider | None,
) -> None:
    """Deliver a referenced secret to a job's sink, or fail closed with no provider.

    The single delivery interface the dispatcher calls: it takes a reference and
    the recorded job, never secret bytes plus a destination. With no conforming
    provider bound it raises :class:`NoConformingProviderError` and writes nothing,
    so the credential-bearing step is not dispatched.

    :param reference: The secret to deliver, by reference.
    :param job: The recorded job the delivery is bound to.
    :param sink: The per-job sink the bytes are written to.
    :param provider: The conforming provider, or ``None`` to fail closed.
    :raises NoConformingProviderError: When no conforming provider is bound.
    """
    if provider is None:
        raise NoConformingProviderError(
            secret_id=reference.secret_id, node_id=job.node_id
        )
    provider.redeem(reference, job, sink)
