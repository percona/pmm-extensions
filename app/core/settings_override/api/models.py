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

"""Pydantic request/response models for the settings REST API."""

__all__ = [
    "SettingClassAppMetadata",
    "SettingClassGroup",
    "SettingOption",
    "SettingResponse",
    "SettingsListResponse",
    "SettingsPatch",
]

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from app.core.settings_override.registry import ReloadClassification
from app.core.utils.fields import UTCDatetime


class SettingOption(BaseModel):
    """Represent one selectable member for an enum-typed setting.

    :param label: The enum member name shown in the UI (e.g. ``WARNING``).
    :param value: The JSON-dumped member value the client must PATCH
        (e.g. ``30`` for an ``IntEnum``).
    """

    label: str
    value: JsonValue


class SettingResponse(BaseModel):
    """Represent a single setting's metadata and current value.

    :param setting_class: The in-process identifier of the class the field
        belongs to: the Pydantic class ``__name__`` (e.g. ``OmInventorySettings``),
        the same value the settings router takes as its ``{setting_class}`` path
        segment. It is not the value stored in ``settingoverride.setting_class``;
        that column holds the storage token ``setting_class_token`` derives from
        the class (e.g. ``OM_INVENTORY_SETTINGS``), so a query against the table
        must filter on the token, not on this value.
    :param key: The field name on the settings class.
    :param key_path: Carry the canonical key segments for ``key`` such that
        ``"__".join(key_path) == key``.
    :param value: The current value visible through the proxy, dumped to a
        JSON-safe shape via the field's annotation. ``SecretStr`` fields are
        redacted to ``"**********"``.
    :param default_value: The field's declared default value, dumped via the
        same JSON serialiser. ``None`` when no default exists.
    :param type: A human-readable representation of the field's declared
        annotation (for operator visibility; validation uses the actual
        ``FieldInfo``).
    :param reload: The reload classification (HOT or NOT_OVERRIDABLE).
    :param description: The field's free-text description, or ``None``.
    :param is_secret: Whether the field's annotation contains a Pydantic secret
        (``SecretStr`` / ``SecretBytes``) at any depth.
    :param is_complex: Whether the field's annotation is or contains a Pydantic
        ``BaseModel`` subclass (true for nested submodels).
    :param has_override: Whether an **active** row in the ``settingoverride``
        table applies to this ``(setting_class, key)`` pair. An inactive row is
        skipped by the cache loader, so the served value falls back to the
        declared default and reporting it as overridden would tell the UI a
        field is overridden while showing it that default. A nested row also
        marks every canonical prefix of its chain, so a parent reports ``True``
        when only a deeper leaf carries a row.
    :param is_advanced: Whether the setting is flagged ``advanced`` so the UI can
        present it separately from everyday settings. Display-only:
        it does not affect PATCH/DELETE eligibility.
    :param is_applicable: Whether the setting applies under current runtime state
        (e.g. the active auth provider). ``False`` lets the UI present the field
        as inert. Display-only, like ``is_advanced``: it does not block
        PATCH/DELETE server-side; the runtime gate is the real enforcement.
    :param options: Selectable enum members for dropdown UIs, or ``None`` when
        the field is not an ``Enum`` annotation. Aliased members are excluded.
    :param updated_at: When the override applying to this key was last saved,
        falling back to the row's creation time for a row written before the
        stamp was recorded. ``None`` when ``has_override`` is ``False``.
        Timestamps carry second granularity.
    :param updated_by: The username that last saved that override, or ``None``
        both when no override applies and when the row predates the actor
        column. A key can draw on several rows (a nested parent reporting on its
        leaves), in which case the pair comes from the row carrying the latest
        timestamp. Two writes landing within the same second are
        indistinguishable by timestamp, and the pair reported is then whichever
        contributing row was created later, which need not be the one written
        later.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    setting_class: str
    key: str
    key_path: list[str] = Field(default_factory=list)
    value: Any
    default_value: Any
    type: str
    reload: ReloadClassification
    description: str | None
    is_secret: bool
    is_complex: bool
    has_override: bool
    is_advanced: bool = False
    is_applicable: bool = True
    options: list[SettingOption] | None = None
    updated_at: UTCDatetime | None = None
    updated_by: str | None = None


class SettingsPatch(RootModel[dict[str, JsonValue]]):
    """Batch PATCH payload: a flat ``{field_name: new_value, ...}`` mapping.

    An empty body is rejected as 422 because a no-op PATCH is a client bug, not
    a valid request. The server coerces each value to the field's declared
    type via :func:`coerce_field_value`; validation is all-or-nothing -- if any
    key fails, nothing is written.
    """

    root: dict[str, JsonValue] = Field(min_length=1)


class SettingClassAppMetadata(BaseModel):
    """App-ownership metadata attached to a :class:`SettingClassGroup`.

    :param is_app_owned: Always ``True`` for app-owned groups.
    :type is_app_owned: bool
    :param app_id: The owning app's registry key.
    :type app_id: str
    :param app_display_name: The owning app's human-facing label.
    :type app_display_name: str
    :param app_enabled: Whether the owning app is currently enabled.
    :type app_enabled: bool
    """

    is_app_owned: bool = True
    app_id: str
    app_display_name: str
    app_enabled: bool


class SettingClassGroup(BaseModel):
    """Group one settings class's fields for the LIST response.

    :param setting_class: The Pydantic class ``__name__`` this group represents.
    :param settings: The fields declared on the settings class, with their
        current values and metadata.
    :param is_app_owned: Whether this group belongs to a PMM Extensions app under
        ``app/extensions/apps/`` rather than core PMM Extensions wiring.
    :param app_id: The owning app's registry key when ``is_app_owned`` is
        ``True``; ``None`` for core groups.
    :param app_display_name: The owning app's human-facing label when
        ``is_app_owned`` is ``True``; ``None`` for core groups.
    :param app_enabled: Whether the owning app is currently enabled when
        ``is_app_owned`` is ``True``; ``None`` for core groups. Disabled
        apps remain listed so the frontend can hide them without a second
        lookup.
    """

    setting_class: str
    settings: list[SettingResponse]
    is_app_owned: bool = False
    app_id: str | None = None
    app_display_name: str | None = None
    app_enabled: bool | None = None


class SettingsListResponse(BaseModel):
    """The LIST endpoint response, grouping settings by class.

    :param groups: One :class:`SettingClassGroup` per settings class the
        router was configured with.
    :type groups: list[SettingClassGroup]
    """

    groups: list[SettingClassGroup]
