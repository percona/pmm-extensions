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

"""Define the persistent ``SettingOverride`` model and class identifier enum."""

from __future__ import annotations

__all__ = [
    "SettingClassEnum",
    "SettingOverride",
    "StaleActorUpdateError",
    "setting_class_token",
]

import re
from enum import StrEnum
from typing import Any, cast, TYPE_CHECKING

from pydantic import BaseModel, field_validator, JsonValue
from sqlalchemy import Column, event, Index, inspect, String
from sqlalchemy.orm import InstanceState
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field as SQLField

from app.core.db.models import BaseSQLModel
from app.core.db.sql_types import AutoJSON
from app.core.settings_override.constants import SETTING_CLASS_MAX_LENGTH

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.engine.interfaces import Dialect
    from sqlalchemy.orm import Mapper

#: Columns whose change on an already-persisted row must be accompanied by a
#: matching ``updated_by`` restamp in the same flush. Excludes ``updated_at``:
#: it is the timestamp this guard exists to keep trustworthy, not tracked
#: content in its own right, so it carries no independent restamp
#: requirement.
_ACTOR_TRACKED_COLUMNS = ("value", "is_active", "key", "setting_class")

#: ``InstanceState.info`` key :meth:`SettingOverride.stamp` sets. Lives on the
#: instance state rather than the model so it never reaches ``model_dump`` and
#: is not carried over by ``session.merge()``.
_STAMPED_INFO_KEY = "settings_override_stamped"

#: Acronym-aware CamelCase split: ``PMMSettings`` -> ``PMM_Settings``,
#: ``HealthReportSettings`` -> ``Health_Report_Settings``.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def setting_class_token(settings_cls: type[BaseModel]) -> str:
    """Return the storage token written to ``settingoverride.setting_class``.

    The token is the SCREAMING_SNAKE form of the class ``__name__``, derived by
    an acronym-aware CamelCase split so ``ExtensionsSettings`` stores as
    ``EXTENSIONS_SETTINGS``. A class may pin a different token by declaring
    ``__setting_class_token__``, the same escape hatch shape as SQLAlchemy's
    ``__tablename__``.

    The bound is :class:`~pydantic.BaseModel` rather than ``BaseYamlSettings``
    because the re-encryption revisions pass frozen replica models that declare
    their token through that escape hatch instead of inheriting the settings
    base; those replicas and the live settings classes share no tighter base.

    :param settings_cls: The settings class or frozen replica whose override
        rows are stored.
    :return: The token written to ``settingoverride.setting_class``.
    """
    override = getattr(settings_cls, "__setting_class_token__", None)
    if isinstance(override, str) and override:
        return override
    return _CAMEL_SPLIT.sub("_", settings_cls.__name__).upper()


class SettingClassEnum(StrEnum):
    """Enumerate settings classes that may have HOT override rows.

    Members are the core-wired classes only. A settings class owned by an app
    declares itself under ``app/extensions/apps/<app>/`` and needs no member here.

    Members are in-process constants. The ``settingoverride.setting_class``
    column is a plain string whose stored token is derived by
    :func:`setting_class_token`; adding a member no longer requires a
    migration.

    To wire a new core settings class:

    1. Add a member here whose value matches the Pydantic class ``__name__``.
    2. Wire a ``ProxyEntry`` for the new class in the relevant service's
       lifespan (``app/extensions/main.py`` or ``app/tasks/main.py``).
    """

    EXTENSIONS_SETTINGS = "ExtensionsSettings"
    TASKS_SETTINGS = "TasksSettings"
    SNIPPETS_SETTINGS = "SnippetsSettings"
    SETTINGS = "Settings"
    ALERT_SETTINGS = "AlertSettings"
    ANONYMIZER_SETTINGS = "AnonymizerSettings"
    INVENTORY_SETTINGS = "InventorySettings"


class _SettingClassString(TypeDecorator):
    """Store the settings-class token as VARCHAR, coercing enum members by name.

    The ordinary persistence path passes a plain string token produced by
    :func:`setting_class_token`. :meth:`process_bind_param` also converts a
    directly supplied :class:`SettingClassEnum` member to its name, preserving
    the storage token for direct model construction.
    :meth:`SettingOverride._enum_member_to_token` applies the same coercion
    earlier on the ``model_validate`` path.

    """

    impl: String = String(SETTING_CLASS_MAX_LENGTH)
    cache_ok: bool = True

    def process_bind_param(
        self,
        value: Any,
        dialect: Dialect,  # noqa: ARG002
    ) -> str | None:
        """Persist enum members by name and every other value as a string.

        :param value: The Python value being bound.
        :param dialect: The active SQLAlchemy dialect (unused).
        :return: The storage token, or ``None``.
        """
        if value is None:
            return None
        if isinstance(value, SettingClassEnum):
            return value.name
        return str(value)


class SettingOverride(BaseSQLModel, table=True):
    """Represent an admin-managed runtime override of a single settings field.

    The same concrete class is shared across services. Each service's Alembic
    ``migrations/env.py`` imports the model directly (via
    ``from app.core.settings_override.models import *``) so it is registered
    on the shared metadata, and creates the same DDL in its own logical
    database. Rows live in whichever service writes them and are never
    accessed cross-service -- each service queries its own engine for its own
    snapshot.

    :param setting_class: The storage token of the wrapped settings class
        (the SCREAMING_SNAKE form derived by :func:`setting_class_token`).
    :param key: The field name on the target settings class to override.
    :param value: The JSON-encoded raw value to apply at runtime.
    :param is_active: Whether this override row should be considered by the
        cache loader. Inactive rows are skipped.
    :param updated_by: The username that last wrote this row, or ``None`` for a
        row written before the column existed. Deliberately unbounded: the value
        is copied from ``BaseUser.username``, which declares a minimum length and
        no maximum, so any width chosen here would be a bound the identity
        provider never agreed to.
    """

    __table_args__ = (
        Index(
            "ix_settingoverride_setting_class_key",
            "setting_class",
            "key",
            unique=True,
        ),
    )

    setting_class: str = SQLField(
        sa_column=Column(_SettingClassString(), nullable=False),
        max_length=SETTING_CLASS_MAX_LENGTH,
    )
    key: str = SQLField(index=True, nullable=False, max_length=255)
    value: JsonValue = SQLField(
        sa_column=Column(AutoJSON, nullable=False),
    )
    is_active: bool = SQLField(default=True, nullable=False, index=True)
    updated_by: str | None = SQLField(default=None, nullable=True)

    @field_validator("setting_class", mode="before")
    @classmethod
    def _enum_member_to_token(cls, value: Any) -> Any:
        """Persist ``SettingClassEnum`` members by name, not value.

        Runs on the ``model_validate`` path only, because SQLModel skips
        validation in ``__init__`` for ``table=True`` models. A constructed
        instance keeps the member and relies on
        :meth:`_SettingClassString.process_bind_param` for the same coercion
        at bind time.

        A ``StrEnum`` is a ``str`` whose content is the member *value*
        (``ExtensionsSettings``). Without this coercion, constructing
        ``SettingOverride(setting_class=SettingClassEnum.EXTENSIONS_SETTINGS)``
        would store that value and orphan every existing row, which stores
        the member *name* (``EXTENSIONS_SETTINGS``).

        :param value: The raw ``setting_class`` being assigned.
        :return: The storage token when ``value`` is an enum member, otherwise
            ``value`` unchanged.
        """
        if isinstance(value, SettingClassEnum):
            return value.name
        return value

    def stamp(self, actor: str) -> None:
        """Attribute this row's pending change to ``actor``.

        The only way to restamp the actor who already wrote the row's current
        value (an admin saving the same setting twice in a row), because a plain
        ``updated_by`` assignment of the stored value is indistinguishable from
        one that copies the stale actor back from the loaded row, and
        :func:`_reject_unstamped_update` rejects the latter.

        Also marks the row dirty, so a stamp with nothing else changed still
        emits an UPDATE and consumes the stamp instead of leaving it to vouch
        for a later, unrelated flush.

        :param actor: The username making the change.
        """
        state = _instance_state(self)
        self.updated_by = actor
        state.info[_STAMPED_INFO_KEY] = True
        # An unloaded attribute (after expiry) already records the assignment
        # as a change, and ``flag_modified`` raises on it.
        if "updated_by" in state.dict:
            flag_modified(self, "updated_by")


def _instance_state(instance: SettingOverride) -> InstanceState:
    """Return the SQLAlchemy instance state of ``instance``.

    ty doesn't run SQLAlchemy's mypy plugin, so it can't see mapped classes as
    ``Inspectable`` and types ``inspect()``'s return as ``Any | None``; the cast
    restores the runtime-guaranteed ``InstanceState``.

    :param instance: The ``SettingOverride`` to inspect.
    :return: Its instance state.
    """
    return cast(InstanceState, inspect(instance))


class StaleActorUpdateError(RuntimeError):
    """Raise when a ``SettingOverride`` update changes a tracked column without restamping ``updated_by``.

    Signals a write path that bypassed the ``updated_by``-alongside-every-change
    convention :func:`_reject_unstamped_update` enforces. Not an
    :class:`~sqlalchemy.exc.DatabaseError`, so it is never mistaken for one by
    :meth:`~app.core.db.crud.BaseSQLModelManager.save`, which only translates
    that family into an HTTP response. This error propagates unchanged
    through the manager instead.
    """


@event.listens_for(SettingOverride, "before_update")
def _reject_unstamped_update(
    mapper: Mapper,  # noqa: ARG001
    connection: Connection,  # noqa: ARG001
    target: SettingOverride,
) -> None:
    """Reject a flush that changes a tracked column on ``target`` without restamping ``updated_by``.

    Fires only for the UPDATE branch of a flush, never for a fresh insert, so
    a freshly-constructed row's documented ``updated_by = None`` is never
    rejected. Also fires for every dirty instance even when no mapped column
    actually changed, so a genuine change is confirmed per column via
    :class:`~sqlalchemy.orm.attributes.History` rather than inferred from the
    event alone.

    A restamp counts when :meth:`SettingOverride.stamp` was called since the
    last flush, or when ``updated_by`` changed to a different, non-``None``
    actor. Assigning the stored actor back does not count: it is what a patch
    built from the loaded row does, and it leaves the change attributed to
    whoever wrote the previous value.

    :param mapper: The mapper for ``target`` (unused).
    :param connection: The connection the flush runs on (unused).
    :param target: The ``SettingOverride`` instance being flushed.
    :raises StaleActorUpdateError: When a tracked column changed but
        ``updated_by`` was not restamped to an actor in the same flush.
    """
    state = _instance_state(target)
    # Consumed on read so one stamp vouches for exactly one flush, even if this
    # one is rejected or rolled back.
    stamped = state.info.pop(_STAMPED_INFO_KEY, False)
    tracked_changed = any(
        state.attrs[column].history.has_changes() for column in _ACTOR_TRACKED_COLUMNS
    )
    restamped = target.updated_by is not None and (
        stamped or state.attrs["updated_by"].history.has_changes()
    )
    if tracked_changed and not restamped:
        raise StaleActorUpdateError(
            f"SettingOverride {target.id} changed without restamping updated_by"
        )
