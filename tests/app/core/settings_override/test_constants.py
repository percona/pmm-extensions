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

"""Pin the core settings-class identifier constants."""

import pytest

from app.core.config import BaseYamlSettings
from app.core.settings_override.constants import SETTING_CLASS_CHECK_MEMBERS_LEGACY
from app.core.settings_override.models import setting_class_token
from tests.app.core.settings_override.conftest import CORE_SETTINGS_CLASSES

_IDS = [cls.__name__ for _, cls in CORE_SETTINGS_CLASSES]


class TestSettingClassIdentifiers:
    """Pin each identifier constant to the class it names, never the storage token."""

    @pytest.mark.parametrize(
        ("identifier", "settings_cls"), CORE_SETTINGS_CLASSES, ids=_IDS
    )
    def test_matches_class_name(
        self, identifier: str, settings_cls: type[BaseYamlSettings]
    ) -> None:
        """Hold the class ``__name__``, since a typo is a silent lookup miss."""
        assert identifier == settings_cls.__name__

    @pytest.mark.parametrize(
        "identifier", [identifier for identifier, _ in CORE_SETTINGS_CLASSES], ids=_IDS
    )
    def test_is_plain_str(self, identifier: str) -> None:
        """Expose a plain ``str`` rather than a ``str`` subclass such as an enum."""
        assert type(identifier) is str

    @pytest.mark.parametrize(
        ("identifier", "settings_cls"), CORE_SETTINGS_CLASSES, ids=_IDS
    )
    def test_differs_from_storage_token(
        self, identifier: str, settings_cls: type[BaseYamlSettings]
    ) -> None:
        """Never coincide with the token existing ``settingoverride`` rows store."""
        assert identifier != setting_class_token(settings_cls)

    def test_legacy_check_members_stay_storage_tokens(self) -> None:
        """Keep the pre-drop CHECK fixture disjoint from the identifier constants."""
        assert SETTING_CLASS_CHECK_MEMBERS_LEGACY, "legacy CHECK member list is empty"
        identifiers = {identifier for identifier, _ in CORE_SETTINGS_CLASSES}
        assert identifiers.isdisjoint(SETTING_CLASS_CHECK_MEMBERS_LEGACY)
