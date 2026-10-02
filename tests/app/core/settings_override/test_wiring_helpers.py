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

"""Tests for the registry and settings-router ``__name__`` wiring helpers."""

from typing import ClassVar

import pytest

from app.core.config import BaseYamlSettings
from app.core.settings_override.api.routes import ClassEntry
from app.core.settings_override.lifecycle import ProxyEntry, ProxyRegistry
from app.core.settings_override.models import setting_class_token
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.extensions.config import ExtensionsSettings
from app.tasks.config import TasksSettings
from tests.app.core.settings_override.conftest import (
    assert_entries_keyed_by_class_name,
    assert_registry_keyed_by_class_name,
)


class _PinnedTokenSettings(BaseYamlSettings):
    __setting_class_token__: ClassVar[str] = "PINNED_TOKEN"


def _registry(*pairs: tuple[str, type[BaseYamlSettings]]) -> ProxyRegistry:
    return {key: ProxyEntry(OverridableSettingsProxy(cls), cls) for key, cls in pairs}


def _entries(*pairs: tuple[str, type[BaseYamlSettings]]) -> list[ClassEntry]:
    return [(key, cls, OverridableSettingsProxy(cls)) for key, cls in pairs]


class TestAssertRegistryKeyedByClassName:
    """Pin the refresher-registry wiring check to the class ``__name__``."""

    def test_accepts_registry_keyed_by_class_name(self) -> None:
        """Accept a registry whose every key is its class ``__name__``."""
        assert_registry_keyed_by_class_name(
            _registry(
                (ExtensionsSettings.__name__, ExtensionsSettings),
                (TasksSettings.__name__, TasksSettings),
            )
        )

    def test_rejects_registry_keyed_by_storage_token(self) -> None:
        """Reject a registry keyed by the storage token instead of ``__name__``."""
        registry = _registry(
            (setting_class_token(ExtensionsSettings), ExtensionsSettings)
        )
        with pytest.raises(AssertionError):
            assert_registry_keyed_by_class_name(registry)

    def test_rejects_registry_keyed_by_pinned_token(self) -> None:
        """Reject a registry keyed by a class's pinned storage token."""
        registry = _registry(
            (setting_class_token(_PinnedTokenSettings), _PinnedTokenSettings)
        )
        with pytest.raises(AssertionError):
            assert_registry_keyed_by_class_name(registry)

    def test_rejects_key_naming_another_class(self) -> None:
        """Reject a key that names a different settings class."""
        registry = _registry((TasksSettings.__name__, ExtensionsSettings))
        with pytest.raises(AssertionError):
            assert_registry_keyed_by_class_name(registry)

    def test_rejects_empty_registry(self) -> None:
        """Reject an empty registry, which would pass every per-key check."""
        with pytest.raises(AssertionError, match="wires no classes"):
            assert_registry_keyed_by_class_name({})


class TestAssertEntriesKeyedByClassName:
    """Pin the settings-router wiring check to the class ``__name__``."""

    def test_accepts_entries_keyed_by_class_name(self) -> None:
        """Accept entries whose every identifier is its class ``__name__``."""
        assert_entries_keyed_by_class_name(
            _entries(
                (ExtensionsSettings.__name__, ExtensionsSettings),
                (TasksSettings.__name__, TasksSettings),
            )
        )

    def test_rejects_entries_keyed_by_storage_token(self) -> None:
        """Reject entries keyed by the storage token instead of ``__name__``."""
        entries = _entries(
            (setting_class_token(ExtensionsSettings), ExtensionsSettings)
        )
        with pytest.raises(AssertionError):
            assert_entries_keyed_by_class_name(entries)

    def test_rejects_entries_keyed_by_pinned_token(self) -> None:
        """Reject entries keyed by a class's pinned storage token."""
        entries = _entries(
            (setting_class_token(_PinnedTokenSettings), _PinnedTokenSettings)
        )
        with pytest.raises(AssertionError):
            assert_entries_keyed_by_class_name(entries)

    def test_rejects_identifier_naming_another_class(self) -> None:
        """Reject an identifier that names a different settings class."""
        entries = _entries((TasksSettings.__name__, ExtensionsSettings))
        with pytest.raises(AssertionError):
            assert_entries_keyed_by_class_name(entries)

    def test_rejects_empty_entries(self) -> None:
        """Reject an empty entry list, which would pass every per-entry check."""
        with pytest.raises(AssertionError, match="serves no classes"):
            assert_entries_keyed_by_class_name([])
