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

"""Define reusable Sync exceptions for the PMM Extensions app."""

from pydantic import ValidationError

from app.extensions.models import SyncInventoryEntityTypeEnum, SyncItem


class SyncError(Exception):
    """Define a base synchronization error.

    This is the base class for all synchronization-related exceptions within the PMM Extensions
    application. It serves as a common ancestor for more specific synchronization error
    types.
    """


class SyncFailError(SyncError):
    """Raise when a synchronization process fails.

    This exception is triggered when an error occurs during the synchronization of a
    specific entity, resulting in the associated `SyncItem` being marked as failed.

    :param entity_type: The type of the entity that failed to synchronize.
    :type entity_type: SyncInventoryEntityTypeEnum
    :param sync_item: The `SyncItem` instance that failed during synchronization.
    :type sync_item: SyncItem
    """

    def __init__(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        sync_item: SyncItem,
    ) -> None:
        self.entity_type = entity_type
        self.sync_item = sync_item
        message = f"Failed to sync {entity_type}: {sync_item}"
        super().__init__(message)


class SyncItemAlreadyInProgressError(SyncError):
    """Raise when a synchronization item is already in progress.

    This exception is triggered when an attempt is made to initiate a synchronization
    for an inventory item that is currently being synchronized.

    :param sync_in_progress: The synchronization item that is already in progress.
    :type sync_in_progress: SyncItem
    """

    def __init__(self, sync_in_progress: SyncItem) -> None:
        self.sync_in_progress = sync_in_progress
        message = (
            f"A sync item with this entity_id, entity_type, and "
            f"sync_instance is already in progress ({sync_in_progress})."
        )
        super().__init__(message)


class ExecutorHostNotFoundError(SyncError):
    """Raise when no executor host matches the node in strict matching mode.

    :param node_name: The name of the node that failed to match.
    :type node_name: str | None
    :param node_address: The address of the node that failed to match.
    :type node_address: str
    :param available_hosts: The available executor hosts that were checked.
    :type available_hosts: dict[str, str]
    """

    def __init__(
        self,
        node_name: str | None,
        node_address: str,
        available_hosts: dict[str, str],
    ) -> None:
        self.node_name = node_name
        self.node_address = node_address
        self.available_hosts = available_hosts
        message = (
            f"No executor host matches node {node_name!r} (address={node_address!r}). "
            f"Available hosts: {available_hosts}"
        )
        super().__init__(message)


class HostNotMeasuredError(SyncError):
    """Raise when a first-measurement attempt wrote no host observation.

    The attempt is recorded as failed, so it counts against the host's retry
    budget even though the probe itself may have run to completion.

    :param node_id: The node whose host observation was not written.
    """

    def __init__(self, node_id: int | None) -> None:
        self.node_id = node_id
        super().__init__(f"No host observation written for node {node_id}")


class SyncInstanceAlreadyInProgressError(SyncError):
    """Raise when a synchronization instance is already in progress.

    This exception is raised when an attempt is made to start a synchronization process
    that is already being handled by another synchronizer.

    :param sync_items: The synchronization items that are already in progress.
    :param detail: A message describing a conflict that no item evidences, such as
        a run that has not written its first item yet. Carries sync bookkeeping
        only: this type is allowlisted for verbatim storage on
        ``last_sync_error``, which the inventory read routes serve, so a message
        it carries must hold nothing a reader may not see — the allowlist is keyed
        on the type, not on which raising path recorded it.
    """

    def __init__(self, *sync_items: SyncItem, detail: str | None = None) -> None:
        self.sync_items = sync_items
        message = detail or (
            f"A sync instance with this syncer is already in progress for the "
            f"following sync items: {sync_items}."
        )
        super().__init__(message)


class SyncerConfigurationError(SyncError):
    """Raise when a configured syncer cannot be constructed from its settings.

    Wraps the ``pydantic.ValidationError`` a syncer raises for an unusable setting so
    callers can tell a misconfiguration apart from a runtime fault: the sync trigger
    answers it with a 503 instead of letting it surface as an unhandled 500, and a
    scheduled run fails with the offending field named. The message names the fields
    and not their values, since it reaches an API client; the wrapped error stays on
    ``__cause__`` for the log.

    :param syncer: Dotted path of the syncer that could not be constructed.
    :param error: The validation failure the syncer raised for its settings.
    """

    def __init__(self, syncer: str, error: ValidationError) -> None:
        self.syncer = syncer
        self.fields = [
            ".".join(str(part) for part in item["loc"])
            for item in error.errors()
            if item["loc"]
        ]
        named = f": {', '.join(self.fields)}" if self.fields else ""
        super().__init__(f"Syncer {syncer!r} rejected its configured settings{named}")
