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

"""Define constants shared by settings-override modules."""

#: Sentinel returned by
#: :func:`app.core.settings_override.resolution.resolve_nested_value` when a
#: segment along the chain is absent. Distinct from a present intermediate or
#: leaf whose value is ``None`` (an optional intermediate collapsing to
#: ``None``, or an unresolved secret leaf). The LIST response builder maps this
#: to a JSON ``null``. Lives in this leaf module so every reader gets the same
#: object from an import that cannot run mid-initialization: the identity check
#: callers rely on would silently fail against a second ``object()``.
NESTED_VALUE_MISSING = object()

# Stable, app-chosen advisory-lock key shared by every settingoverride migration
# so they serialize against each other on a shared PostgreSQL database. Any fixed
# bigint unused elsewhere works; no other advisory lock exists in the repo.
SETTINGOVERRIDE_MIGRATION_LOCK_KEY = 0x5E770438

#: Name of the ``settingoverride`` column recording which admin last saved an
#: override, shared by the migration helpers that add and drop it and by the
#: tests that assert on it, so the three cannot drift apart.
SETTINGOVERRIDE_UPDATED_BY_COLUMN = "updated_by"

#: Width of the ``settingoverride.setting_class`` column, shared by the ORM
#: type, the Pydantic constraint, and the migration that widens the column, so
#: the three cannot drift apart.
SETTING_CLASS_MAX_LENGTH = 255

# In-process identifiers of the core-wired settings classes, each the class
# ``__name__``. Plain strings rather than an enum because app-owned classes join
# the same identifier space at runtime, so no closed set exists to enumerate.
# They are never the ``settingoverride.setting_class`` storage token, which only
# ``setting_class_token`` derives.
EXTENSIONS_SETTINGS = "ExtensionsSettings"
TASKS_SETTINGS = "TasksSettings"
SNIPPETS_SETTINGS = "SnippetsSettings"
SETTINGS = "Settings"
ALERT_SETTINGS = "AlertSettings"
ANONYMIZER_SETTINGS = "AnonymizerSettings"
INVENTORY_SETTINGS = "InventorySettings"

#: Member list the ``settingoverride.setting_class`` CHECK enumerated immediately
#: before the drop migration widened the column. Downgrades re-add this exact
#: list and delete rows naming a class outside it.
SETTING_CLASS_CHECK_MEMBERS_LEGACY = (
    "SEP_SETTINGS",
    "TASKS_SETTINGS",
    "SNIPPETS_SETTINGS",
    "SETTINGS",
    "ALERT_SETTINGS",
    "ANONYMIZER_SETTINGS",
    "ALERTS_SETTINGS",
    "INVENTORY_SETTINGS",
    "HEALTH_REPORT_SETTINGS",
)
