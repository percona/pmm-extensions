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

"""Define models for the Inventory API.

The models are split by role into submodules: :mod:`.enums`, :mod:`.bases`,
:mod:`.tables`, :mod:`.writes` and :mod:`.responses`. Every public name is
re-exported here, so ``from app.inventory.models import ...`` keeps working and
the Alembic environment's star import still registers every table.
"""

from app.inventory.models.bases import (
    ExternalIdentityAliasBase,
    HOST_OBSERVATION_FIELD_NAMES,
    HostSystemObservationBase,
    NodeBase,
    RetirableSQLModel,
    RETIRED_ROWS_ONLY,
    RetiredAtBase,
    SchemaBase,
    ServiceBase,
    ServiceSystemObservationBase,
    SyncHealthBase,
    TableBase,
)
from app.inventory.models.enums import (
    IdentityLinkDecisionEnum,
    LinkageMethodEnum,
    ServiceTypeEnum,
    SourceEnum,
    SyncOutcomeEnum,
)
from app.inventory.models.responses import (
    ExternalIdentityAliasResponse,
    HostSystemObservationResponse,
    HostSystemObservationSummaryResponse,
    InventoryCollectResponse,
    NodeIdentityCandidateResponse,
    NodeResponse,
    SchemaCompactResponse,
    SchemaDetailResponse,
    SchemaResponse,
    ServiceDetailResponse,
    ServiceIdentityCandidateResponse,
    ServiceResponse,
    ServiceSystemObservationResponse,
    SyncHealthBulkResult,
    TableDetailResponse,
    TableResponse,
)
from app.inventory.models.tables import (
    ExternalIdentityAlias,
    host_observation_min_content_check,
    HOST_OBSERVATION_MIN_CONTENT_CONSTRAINT,
    HostSystemObservation,
    IdentityLinkDecision,
    Node,
    Schema,
    Service,
    ServiceSystemObservation,
    Table,
)
from app.inventory.models.writes import (
    HostSystemObservationWrite,
    IdentityLinkDecisionWrite,
    InventoryCollectWrite,
    NodeWrite,
    SchemaWrite,
    ServiceSystemObservationWrite,
    ServiceWrite,
    SyncHealthBulkWrite,
    SyncHealthEntityWrite,
    SyncHealthWrite,
    TableWrite,
)

__all__ = [
    "HOST_OBSERVATION_FIELD_NAMES",
    "HOST_OBSERVATION_MIN_CONTENT_CONSTRAINT",
    "RETIRED_ROWS_ONLY",
    "ExternalIdentityAlias",
    "ExternalIdentityAliasBase",
    "ExternalIdentityAliasResponse",
    "HostSystemObservation",
    "HostSystemObservationBase",
    "HostSystemObservationResponse",
    "HostSystemObservationSummaryResponse",
    "HostSystemObservationWrite",
    "IdentityLinkDecision",
    "IdentityLinkDecisionEnum",
    "IdentityLinkDecisionWrite",
    "InventoryCollectResponse",
    "InventoryCollectWrite",
    "LinkageMethodEnum",
    "Node",
    "NodeBase",
    "NodeIdentityCandidateResponse",
    "NodeResponse",
    "NodeWrite",
    "RetirableSQLModel",
    "RetiredAtBase",
    "Schema",
    "SchemaBase",
    "SchemaCompactResponse",
    "SchemaDetailResponse",
    "SchemaResponse",
    "SchemaWrite",
    "Service",
    "ServiceBase",
    "ServiceDetailResponse",
    "ServiceIdentityCandidateResponse",
    "ServiceResponse",
    "ServiceSystemObservation",
    "ServiceSystemObservationBase",
    "ServiceSystemObservationResponse",
    "ServiceSystemObservationWrite",
    "ServiceTypeEnum",
    "ServiceWrite",
    "SourceEnum",
    "SyncHealthBase",
    "SyncHealthBulkResult",
    "SyncHealthBulkWrite",
    "SyncHealthEntityWrite",
    "SyncHealthWrite",
    "SyncOutcomeEnum",
    "Table",
    "TableBase",
    "TableDetailResponse",
    "TableResponse",
    "TableWrite",
    "host_observation_min_content_check",
]
