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

"""Test inventory model validators and enums."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import utc_now
from app.inventory.constants import ACTIVE_RETIREMENT_KEY, SYNC_ATTEMPT_MAX_CLOCK_SKEW
from app.inventory.crud import RetiredInclusiveServiceManager, ServiceManager
from app.inventory.models import (
    ExternalIdentityAlias,
    HostSystemObservation,
    HostSystemObservationWrite,
    IdentityLinkDecision,
    Node,
    NodeWrite,
    Service,
    ServiceSystemObservationWrite,
    ServiceTypeEnum,
    ServiceWrite,
    SourceEnum,
    SyncHealthWrite,
    SyncOutcomeEnum,
)
from tests.app.factories import ServiceWriteFactory


class TestNodeWriteMandatoryOrigin:
    """Test that NodeWrite requires a PMM origin."""

    def test_missing_external_id_raises(self) -> None:
        """Raise ValidationError when external_id is absent."""
        with pytest.raises(ValidationError, match="external_id"):
            NodeWrite(address="10.0.0.1", name="node1", source=SourceEnum.PMM)

    def test_missing_source_raises(self) -> None:
        """Raise ValidationError when source is absent."""
        with pytest.raises(ValidationError, match="source"):
            NodeWrite(address="10.0.0.1", name="node1", external_id="abc")

    def test_null_external_id_raises(self) -> None:
        """Raise ValidationError when external_id is explicitly None."""
        with pytest.raises(ValidationError, match="external_id"):
            NodeWrite(
                address="10.0.0.1",
                name="node1",
                external_id=None,
                source=SourceEnum.PMM,
            )

    def test_null_source_raises(self) -> None:
        """Raise ValidationError when source is explicitly None."""
        with pytest.raises(ValidationError, match="source"):
            NodeWrite(
                address="10.0.0.1",
                name="node1",
                external_id="abc",
                source=None,
            )

    def test_full_origin_succeeds(self) -> None:
        """Accept a node carrying both external_id and source."""
        node = NodeWrite(
            address="10.0.0.1",
            name="node1",
            external_id="abc",
            source=SourceEnum.PMM,
        )
        assert node.external_id == "abc"
        assert node.source == SourceEnum.PMM


class TestServiceWriteMandatoryOrigin:
    """Test that ServiceWrite requires an external_id."""

    def test_missing_external_id_raises(self) -> None:
        """Raise ValidationError when external_id is absent."""
        with pytest.raises(ValidationError, match="external_id"):
            ServiceWrite(name="svc", type=ServiceTypeEnum.MYSQL, node_id=1)

    def test_null_external_id_raises(self) -> None:
        """Raise ValidationError when external_id is explicitly None."""
        with pytest.raises(ValidationError, match="external_id"):
            ServiceWrite(
                external_id=None,
                name="svc",
                type=ServiceTypeEnum.MYSQL,
                node_id=1,
            )

    def test_full_origin_succeeds(self) -> None:
        """Accept a service carrying an external_id."""
        service = ServiceWrite(
            external_id="svc-1",
            name="svc",
            type=ServiceTypeEnum.MYSQL,
            node_id=1,
        )
        assert service.external_id == "svc-1"


class TestHostSystemObservationBaseValidator:
    """Test HostSystemObservationBase.validate_at_least_one_observation_field."""

    def test_all_observation_fields_none_raises(self) -> None:
        """Raise ValidationError when os_version, packages, and config are all None."""
        with pytest.raises(ValidationError, match="os_version"):
            HostSystemObservationWrite(
                observed_at=utc_now(),
            )

    def test_os_version_only_succeeds(self) -> None:
        """Accept when only os_version is set."""
        observation = HostSystemObservationWrite(
            os_version="22.04",
            observed_at=utc_now(),
        )
        assert observation.os_version == "22.04"
        assert observation.installed_packages is None
        assert observation.config is None

    def test_installed_packages_only_succeeds(self) -> None:
        """Accept when only installed_packages is set."""
        observation = HostSystemObservationWrite(
            installed_packages=[{"name": "curl", "version": "7.81"}],
            observed_at=utc_now(),
        )
        assert observation.installed_packages == [{"name": "curl", "version": "7.81"}]

    def test_config_only_succeeds(self) -> None:
        """Accept when only config is set."""
        observation = HostSystemObservationWrite(
            config={"kernel": "5.15"},
            observed_at=utc_now(),
        )
        assert observation.config == {"kernel": "5.15"}


class TestHostSystemObservationTableGuard:
    """Test the at-least-one-fact CHECK on the ``hostsystemobservation`` table.

    ``HostSystemObservation`` is a table model, so SQLModel skips the validator
    ``HostSystemObservationWrite`` runs. Anything reaching the table through the
    ORM rather than through the write model — a fixture, a script, a future
    caller — meets the CHECK and nothing else.
    """

    @pytest.mark.asyncio
    async def test_fact_less_row_is_rejected(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse a row whose every observed fact is unset.

        The two JSON facts are what make this a real test rather than a
        tautology: they reach the database as SQL NULL only because they are
        declared ``JSON(none_as_null=True)``. Under SQLAlchemy's default they
        would arrive as the JSON text ``null``, and the CHECK would pass.
        """
        session.add(HostSystemObservation(node_id=node.id, observed_at=utc_now()))
        with pytest.raises(IntegrityError):
            await session.flush()

    @pytest.mark.asyncio
    async def test_row_with_one_fact_is_admitted(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Accept a row carrying a single fact, leaving the rest unset."""
        observation = HostSystemObservation(
            node_id=node.id,
            observed_at=utc_now(),
            os_version="Ubuntu 24.04",
        )
        session.add(observation)
        await session.commit()

        await session.refresh(observation)
        assert observation.id is not None
        assert observation.installed_packages is None
        assert observation.config is None
        assert observation.can_elevate is None


class TestServiceSystemObservationBaseValidator:
    """Test required fields on ServiceSystemObservationWrite."""

    def test_db_engine_version_required(self) -> None:
        """Raise ValidationError when db_engine_version is omitted."""
        with pytest.raises(ValidationError, match="db_engine_version"):
            ServiceSystemObservationWrite(observed_at=utc_now())


class TestSourceEnum:
    """Test SourceEnum values."""

    def test_values(self) -> None:
        """Assert SourceEnum has exactly the expected values."""
        assert {member.value for member in SourceEnum} == {"pmm"}


class TestServiceTypeEnum:
    """Test ServiceTypeEnum values."""

    def test_values(self) -> None:
        """Assert ServiceTypeEnum has exactly the expected values."""
        assert {member.value for member in ServiceTypeEnum} == {
            "mysql",
            "postgresql",
            "mongodb",
            "proxysql",
            "haproxy",
            "external",
            "valkey",
        }


class TestRetirementKeyUniqueness:
    """Test how ``retirement_key`` scopes the composite unique indexes.

    The database index is the only thing enforcing uniqueness; ``BaseManager.save``
    reports a breach of it as ``HTTPConflictException``, keeping
    ``HTTPBadRequestException`` for a violation that is not a duplicate. The
    discriminator only scopes the index while it stays non-NULL, since a NULL
    never compares equal to another.
    """

    @pytest.mark.asyncio
    async def test_active_service_carries_the_active_sentinel(
        self, service: Service
    ) -> None:
        """Keep the discriminator outside the autoincrement range on an active row."""
        assert service.retirement_key == ACTIVE_RETIREMENT_KEY
        assert ACTIVE_RETIREMENT_KEY < 1

    @pytest.mark.asyncio
    async def test_two_active_services_on_one_port_are_both_admitted(
        self, session: AsyncSession, service: Service
    ) -> None:
        """Admit a second active service sharing a node and port.

        Several databases behind one PostgreSQL or MySQL server legally share
        that server's port, and every service now carries its own external
        identity, so nothing about the pair collides.
        """
        second = await ServiceManager.create(
            session,
            ServiceWriteFactory.build(port=service.port),
            node_id=service.node_id,
        )
        assert second.id != service.id
        assert second.port == service.port
        assert second.external_id != service.external_id

    @pytest.mark.asyncio
    async def test_replacement_over_a_tombstone_is_admitted(
        self, session: AsyncSession, service: Service
    ) -> None:
        """Admit a replacement on the node and port a tombstone still holds."""
        await ServiceManager.retire(session, service)

        replacement = await ServiceManager.create(
            session,
            ServiceWriteFactory.build(port=service.port),
            node_id=service.node_id,
        )
        assert replacement.id != service.id
        assert replacement.retirement_key == ACTIVE_RETIREMENT_KEY

    @pytest.mark.asyncio
    async def test_successive_tombstones_share_one_key(
        self, session: AsyncSession, service: Service
    ) -> None:
        """Let any number of tombstones sit on the same node and port."""
        await ServiceManager.retire(session, service)
        replacement = await ServiceManager.create(
            session,
            ServiceWriteFactory.build(port=service.port),
            node_id=service.node_id,
        )
        await ServiceManager.retire(session, replacement)

        retired = await RetiredInclusiveServiceManager.list(
            session, node_id=service.node_id
        )
        assert {row.id for row in retired} == {service.id, replacement.id}
        assert {row.retirement_key for row in retired} == {service.id, replacement.id}


class TestIdentityTablesCarryNoUniqueIndex:
    """Test that the append-only identity tables declare no unique index.

    ``BaseSQLModelManager.save`` rebuilds equality filters from every unique
    index and refuses a row matching one, so a unique index here would reject
    the superseding record that closes a binding — which is how this design
    expresses closure.
    """

    def test_alias_table_has_no_unique_index(self) -> None:
        """Leave every external-identity alias index non-unique."""
        indexes = ExternalIdentityAlias.__table__.indexes
        assert indexes
        assert not any(index.unique for index in indexes)

    def test_decision_table_has_no_unique_index(self) -> None:
        """Leave every identity-link decision index non-unique."""
        indexes = IdentityLinkDecision.__table__.indexes
        assert indexes
        assert not any(index.unique for index in indexes)


class TestSyncHealthWriteAttemptClock:
    """Test the bound a reported attempt time is held to."""

    @pytest.mark.parametrize(
        "offset",
        [SYNC_ATTEMPT_MAX_CLOCK_SKEW - timedelta(minutes=1), -timedelta(days=30)],
        ids=["ahead_within_tolerance", "in_the_past"],
    )
    def test_an_attempt_not_beyond_the_tolerance_is_accepted(
        self, offset: timedelta
    ) -> None:
        """Admit both the ordinary late report and the drift two containers carry."""
        attempted_at = utc_now() + offset

        body = SyncHealthWrite(
            outcome=SyncOutcomeEnum.SUCCESS, attempted_at=attempted_at
        )

        assert body.attempted_at == attempted_at

    def test_an_attempt_beyond_the_tolerance_is_refused(self) -> None:
        """Refuse a freshness no later report could supersede.

        The ordering guards admit any attempt not older than the stored one, so
        a clock running fast would stamp a ``last_synced_at`` that locks the row
        until wall-clock time catches up — and locks out the same reporter once
        its clock is corrected.
        """
        attempted_at = utc_now() + SYNC_ATTEMPT_MAX_CLOCK_SKEW + timedelta(minutes=1)

        with pytest.raises(ValidationError, match="clock skew"):
            SyncHealthWrite(outcome=SyncOutcomeEnum.SUCCESS, attempted_at=attempted_at)
