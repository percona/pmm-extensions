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

"""Define models for interacting with the Inventory API."""

from __future__ import annotations

from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.db import BaseSQLModel
from app.core.utils.fields import (
    ARBITRARY_ARGS_SCHEMA,
    ArbitraryMapping,
    EmptyStrToNone,
    NonEmptyStr,
    UTCDatetime,
)
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.models import ServiceTypeEnum, SourceEnum


class BaseInventoryModel(BaseModel):
    """Define the base structure for inventory-related data.

    This model serves as a foundation for all inventory-related operations,
    providing common configuration settings that ensure consistency across
    derived models.
    """

    model_config = ConfigDict(populate_by_name=True)


class CreatedEntityBase(BaseSQLModel):
    """Carry the fields every created inventory entity is persisted with.

    This model provides common functionality for entities that are persisted
    in the inventory database, including parent-child relationships.

    :cvar CHILDREN_FIELD: The field name representing child entities. Defaults to None.
    :cvar PARENT_FIELD: The field name representing the parent entity. Defaults to None.
    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record was last updated.
    :param retired_at: When the inventory stopped seeing the entity upstream, or
        None while it is active.
    """

    CHILDREN_FIELD: ClassVar[str | None] = None
    PARENT_FIELD: ClassVar[str | None] = None
    retired_at: UTCDatetime | None = None

    @property
    def is_retired(self) -> bool:
        """Return whether the entity is tombstoned.

        :return: ``True`` once ``retired_at`` is set.
        """
        return self.retired_at is not None

    @property
    def children(self) -> list[CreatedEntityBase]:
        """Retrieve the list of child entities associated with the entity.

        :return: The children associated with the entity.
        """
        if self.CHILDREN_FIELD is None:
            return []
        return dict(self).get(self.CHILDREN_FIELD, [])

    @property
    def parent(self) -> CreatedEntityBase | None:
        """Retrieve the parent entity associated with the entity.

        :return: The parent associated with the entity, or None if no parent is set.
        :rtype: CreatedEntityBase | None
        """
        return dict(self).get(self.PARENT_FIELD)

    @parent.setter
    def parent(self, entity: CreatedEntityBase) -> None:
        """Set the parent entity for the current entity.

        :param entity: The parent entity to associate with the current entity.
        :type entity: CreatedEntityBase
        :raises AttributeError: If the PARENT_FIELD is not defined for the entity.
        """
        if self.PARENT_FIELD is None:
            raise AttributeError(f"{self.__class__.__name__} has no PARENT_FIELD")
        setattr(self, self.PARENT_FIELD, entity)

    @model_validator(mode="after")
    def add_parent_to_children(self) -> Self:
        """Assign the current instance as the parent to each child entity.

        Iterates through the list of children and sets their `parent` attribute
        to reference this instance.

        :return: The current instance with updated children.
        :rtype: Self
        """
        for child in self.children:
            child.parent = self.model_copy(update={self.CHILDREN_FIELD: []})
        return self


class Node(BaseInventoryModel):
    """Represent an inventory node.

    This model represents a node within the Inventory API, including its network
    address, external identifier, name, and type.

    :param address: The network address of the node.
    :param name: The name of the node, aliased as "node_name".
    :param external_id: The external identifier for the node, aliased as "node_id".
    :param source: The source of the node information. Defaults to None. Left
        optional deliberately, unlike ``external_id``: both construction sites
        inject :attr:`SourceEnum.PMM` unconditionally, so narrowing it here
        would turn the syncer's ``source == SourceEnum.PMM`` check into
        provably dead code rather than a real boundary.
    :param type: The type of the node (e.g., "generic"), aliased as "node_type".
        Defaults to "generic".
    :param services: The services associated with the node.
    """

    address: NonEmptyStr
    name: NonEmptyStr = Field(validation_alias="node_name")
    external_id: NonEmptyStr = Field(validation_alias="node_id")
    source: SourceEnum | EmptyStrToNone = None
    type: NonEmptyStr = Field(default="generic", validation_alias="node_type")
    services: list[Service] = []

    def __repr__(self) -> str:
        return (
            f"Node("
            f"name={self.name!r}, address={self.address!r}"
            f", external_id={self.external_id!r}, type={self.type!r},"
            f" source='{self.source}')"
        )


class CreatedNode(CreatedEntityBase, Node):
    """Represent an existing node from the inventory database.

    This model extends `Node` and `BaseSQLModel` to integrate attributes from an
    existing database node.

    :cvar CHILDREN_FIELD: The field name representing child entities. Set to "services".
    :param id: The primary key of the node in the inventory database.
    :param created_at: The timestamp when the node was created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record was last updated.
    :param address: The network address of the node.
    :param external_id: The external identifier for the node, aliased as "node_id".
    :param name: The name of the node, aliased as "node_name".
    :param type: The type of the node (e.g., "generic"), aliased as "node_type".
        Defaults to "generic".
    :param source: The source of the node information. Defaults to None.
    :param services: A list of existing services associated with the node.
    """

    CHILDREN_FIELD: ClassVar[str] = "services"
    services: list[CreatedService] = []


class Service(BaseInventoryModel):
    """Represent an inventory service.

    This model represents a service within the Inventory API, including its environment,
    cluster, custom labels, external identifier, name, port, and type.

    :param environment: The environment in which the service is running (e.g.,
        "production", "staging"). Defaults to None.
    :param cluster: The cluster in which the service is running. Defaults to None.
    :param replication_set: The replication set in which the service is running. Defaults to None.
    :param custom_labels: Custom labels associated with the service. Defaults to None.
    :param external_id: The external identifier for the service, aliased as
        "service_id".
    :param name: The name of the service, aliased as "service_name".
    :param port: The port number on which the service is running. Defaults to None.
    :param type: The type of the service (e.g., "service_type"), aliased as
        "service_type".
    :param schemas: The schemas associated with the service.
    """

    environment: str | None = None
    cluster: str | None = None
    replication_set: str | None = None
    custom_labels: ArbitraryMapping | None = None
    external_id: NonEmptyStr = Field(validation_alias="service_id")
    name: NonEmptyStr = Field(validation_alias="service_name")
    port: int | EmptyStrToNone = None
    type: ServiceTypeEnum = Field(validation_alias="service_type")
    schemas: list[Schema] = []

    def __repr__(self) -> str:
        return (
            f"Service(name={self.name!r}, type={self.type!r},"
            f" external_id={self.external_id!r}, port={self.port!r})"
        )


class CreatedService(CreatedEntityBase, Service):
    """Represent an existent service from the inventory database.

    This model extends `Service` and `BaseSQLModel` to integrate attributes from an
    existent database service.

    :cvar CHILDREN_FIELD: The field name representing child entities. Set to "schemas".
    :cvar PARENT_FIELD: The field name representing the parent entity. Set to "node".
    :param id: The primary key of the service in the inventory database.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record was last updated.
    :param environment: The environment in which the service is running (e.g.,
        "production", "staging"). Defaults to None.
    :param cluster: The cluster in which the service is running. Defaults to None.
    :param replication_set: The replication set in which the service is running. Defaults to None.
    :param custom_labels: Custom labels associated with the service. Defaults to None.
    :param external_id: The external identifier for the service, aliased as
        "service_id".
    :param name: The name of the service, aliased as "service_name".
    :param port: The port number on which the service is running. Defaults to None.
    :param type: The type of the service (e.g., "service_type"), aliased as
        "service_type".
    :param node_id: The ID of the node to which the service belongs.
    :param node: The node to which the service is associated. Defaults to None.
    :param schemas: A list of existing schemas associated with the service.
    """

    CHILDREN_FIELD: ClassVar[str] = "schemas"
    PARENT_FIELD: ClassVar[str] = "node"
    node_id: int
    node: CreatedNode | None = None
    schemas: list[CreatedSchema] = []

    def __repr__(self) -> str:
        return (
            f"Service(name={self.name!r}, type={self.type!r},"
            f" external_id={self.external_id!r}, port={self.port!r},"
            f" node_id={self.node_id})"
        )

    @property
    def address(self) -> str | None:
        """Return the complete address for the service.

        Builds the complete address from the service's node address and the service's
        port.

        :return: The complete address for the service, or None if the service is not
            associated with a node
        :rtype: str | None
        """
        if self.node is None:
            return None
        host = self.node.address
        if self.port:
            host += f":{self.port}"
        return host


class Schema(BaseInventoryModel):
    """Represent an inventory schema.

    This model represents a schema within the Inventory API, including its name.

    :param name: The name of the schema.
    :type name: NonEmptyStr
    :param tables: The tables associated with the schema.
    :type tables: list[Table]
    """

    name: NonEmptyStr
    tables: list[Table] = []

    def __repr__(self) -> str:
        return f"Schema(name={self.name!r})"


class CreatedSchema(CreatedEntityBase, Schema):
    """Represent an existent schema from the inventory database.

    This model extends `Schema` and `BaseSQLModel` to integrate attributes from an
    existent database schema.

    :cvar CHILDREN_FIELD: The field name representing child entities. Set to "tables".
    :vartype CHILDREN_FIELD: ClassVar[str]
    :cvar PARENT_FIELD: The field name representing the parent entity. Set to "service".
    :vartype PARENT_FIELD: ClassVar[str]
    :param id: The primary key of the schema in the inventory database.
    :type id: int | None
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :type created_at: UTCDatetime
    :param updated_at: The timestamp when the record was last updated.
    :type updated_at: UTCDatetime | None
    :param name: The name of the schema.
    :type name: NonEmptyStr
    :param service_id: The ID of the service to which the schema belongs.
    :type service_id: int
    :param service: The service to which the schema is associated. Defaults to None.
    :type service: CreatedService | None
    :param tables: A list of existing tables associated with the schema.
    :type tables: list[CreatedTable]
    """

    CHILDREN_FIELD: ClassVar[str] = "tables"
    PARENT_FIELD: ClassVar[str] = "service"
    service_id: int
    service: CreatedService | None = None
    tables: list[CreatedTable] = []

    def __repr__(self) -> str:
        return f"Schema(name={self.name!r}, service_id={self.service_id})"


class Table(BaseInventoryModel):
    """Represent an inventory table.

    This model represents a table within a schema in the Inventory API, including its
    name and the SQL statement used to create the table, and details about its keys.

    :param name: The name of the table.
    :type name: NonEmptyStr
    :param create: The SQL statement used to create the table.
    :type create: NonEmptyStr
    :param keys: A dictionary containing details about table keys (e.g., primary, unique).
    :type keys: dict[str, Any]
    """

    name: NonEmptyStr
    create: NonEmptyStr
    keys: dict[str, Any] = Field(
        default_factory=dict, json_schema_extra=ARBITRARY_ARGS_SCHEMA
    )

    def __repr__(self) -> str:
        return f"Table(name={self.name!r})"


class CreatedTable(CreatedEntityBase, Table):
    """Represent an existent table from the inventory database.

    This model extends `Table` and `BaseSQLModel` to integrate attributes from an
    existent database table.

    :cvar PARENT_FIELD: The field name representing the parent entity. Set to
        "database".
    :vartype PARENT_FIELD: ClassVar[str]
    :param id: The primary key of the service in the inventory database.
    :type id: int | None
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :type created_at: UTCDatetime
    :param updated_at: The timestamp when the record was last updated.
    :type updated_at: UTCDatetime | None
    :param name: The name of the table.
    :type name: NonEmptyStr
    :param create: The SQL statement used to create the table.
    :type create: NonEmptyStr
    :param schema_id: The ID of the schema to which the table belongs.
    :type schema_id: int
    :param database: The schema to which the table is associated. Defaults to None.
    :type database: CreatedSchema | None
    """

    PARENT_FIELD: ClassVar[str] = "database"
    schema_id: int
    database: CreatedSchema | None = Field(default=None, validation_alias="schema")

    def __repr__(self) -> str:
        return f"Table(name={self.name!r}, schema_id={self.schema_id})"


# ``Service``/``Schema`` reference each other's children before those classes
# exist, so Pydantic defers their build. Resolve it here, where every name is in
# scope, rather than leaving it to whichever consumer happens to build a model
# first -- a subclass declared in another module (``SystemFactsService``) cannot
# resolve ``Schema`` from its own namespace.
for _model in (Node, CreatedNode, Service, CreatedService, Schema, CreatedSchema):
    _model.model_rebuild()

CreatedEntity = CreatedNode | CreatedService | CreatedSchema | CreatedTable

ENTITY_MAPPING = {
    SyncInventoryEntityTypeEnum.NODE: ("/nodes", CreatedNode),
    SyncInventoryEntityTypeEnum.SERVICE: ("/services", CreatedService),
    SyncInventoryEntityTypeEnum.SCHEMA: ("/schemas", CreatedSchema),
    SyncInventoryEntityTypeEnum.TABLE: ("/tables", CreatedTable),
}
