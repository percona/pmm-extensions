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

"""DB-backed settings override layer (HOT-only)."""

__all__ = [
    "NESTED_VALUE_MISSING",
    "CallbackRegistry",
    "FieldMetadata",
    "Materializer",
    "MaterializerContext",
    "MaterializerPurpose",
    "OverridableSettingsProxy",
    "RefreshCallback",
    "ReloadClassification",
    "SettingOverride",
    "SettingsOverrideManager",
    "SnapshotChange",
    "build_snapshot",
    "coerce_field_value",
    "coerce_nested_field_value",
    "dump_field_value",
    "field_materializer",
    "fire_change_callbacks",
    "fire_on_boot",
    "hot_field",
    "hot_field_names",
    "is_hot_reloadable",
    "is_nested_overridable_parent",
    "iter_class_fields",
    "materialize_override_value",
    "materialize_template",
    "materialize_via_owning_model",
    "nested_overridable_field",
    "nested_overridable_field_names",
    "not_overridable_field",
    "publish_snapshot",
    "refresh_all",
    "resolve_nested_field",
    "setting_class_token",
    "settings_override_refresher",
    "start_refresh_task",
]

from app.core.settings_override.cache import build_snapshot
from app.core.settings_override.constants import NESTED_VALUE_MISSING
from app.core.settings_override.lifecycle import (
    CallbackRegistry,
    fire_change_callbacks,
    fire_on_boot,
    publish_snapshot,
    refresh_all,
    RefreshCallback,
    settings_override_refresher,
    SnapshotChange,
    start_refresh_task,
)
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import (
    setting_class_token,
    SettingOverride,
)
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    coerce_field_value,
    coerce_nested_field_value,
    dump_field_value,
    field_materializer,
    FieldMetadata,
    hot_field,
    hot_field_names,
    is_hot_reloadable,
    is_nested_overridable_parent,
    iter_class_fields,
    materialize_override_value,
    materialize_template,
    materialize_via_owning_model,
    Materializer,
    MaterializerContext,
    MaterializerPurpose,
    nested_overridable_field,
    nested_overridable_field_names,
    not_overridable_field,
    ReloadClassification,
)
from app.core.settings_override.resolution import resolve_nested_field
