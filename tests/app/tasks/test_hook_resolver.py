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

"""Define tests for the shared ``app.tasks.hook_resolver`` colon-path resolver."""

import importlib
import logging
import re

import pytest
from pytest_mock import MockerFixture

from app.extensions.apps.archives import alerts as archive_alerts
from app.extensions.apps.archives.alerts import ALERT_DETAIL_BUILDER
from app.extensions.apps.mysql_backups.recorder import RUN_RESULT_RECORDER
from app.tasks import hook_resolver
from app.tasks.hook_resolver import (
    HookPathNotAllowedError,
    resolve_hook,
    validate_hook_path,
)

ALLOWED_PATH = ALERT_DETAIL_BUILDER


@pytest.fixture(autouse=True)
def _clear_cache(mocker):
    """Reset the resolver cache before each test."""
    mocker.patch.dict(hook_resolver._RESOLVED, {}, clear=True)


def test_resolves_module_function_path():
    """Return the callable named by a ``"module:function"`` path."""
    resolved = resolve_hook(ALLOWED_PATH)
    assert resolved is archive_alerts.build_owner_alert_details


def test_caches_resolved_callable(mocker):
    """Serve a repeated path from cache without re-importing the module."""
    spy = mocker.spy(importlib, "import_module")

    first = resolve_hook(ALLOWED_PATH)
    second = resolve_hook(ALLOWED_PATH)

    assert first is second
    spy.assert_called_once_with("app.extensions.apps.archives.alerts")


def test_raises_import_error_for_unknown_module():
    """Raise ``ImportError`` when the module cannot be imported."""
    with pytest.raises(ImportError):
        resolve_hook("app.extensions.apps.no_such_module:thing")


def test_raises_attribute_error_for_unknown_attribute():
    """Raise ``AttributeError`` when the module has no such attribute."""
    with pytest.raises(AttributeError):
        resolve_hook("app.extensions.apps.archives.alerts:does_not_exist")


class TestAllowList:
    """Cover the allow-list enforced by ``validate_hook_path`` and ``resolve_hook``."""

    @pytest.mark.parametrize(
        "path",
        [
            "os:system",
            "builtins:eval",
            "app.tasks.alert_hooks:build_owner_alert_details",
            "app.extensions.plugins.archives.alerts:build_owner_alert_details",
            "app.extensions.appsevil.mod:builder",
        ],
    )
    def test_rejects_module_outside_allow_listed_namespace(self, path: str) -> None:
        """Reject a path whose module is not under an allow-listed root."""
        with pytest.raises(
            HookPathNotAllowedError, match=re.escape("app.extensions.apps")
        ):
            validate_hook_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "no_colon_here",
            ":build_owner_alert_details",
            "app.extensions.apps.archives.alerts:",
            "app.extensions.apps.archives.alerts:not an identifier",
            "app.extensions.apps.archives.alerts:a:b",
            "app.extensions.apps..archives:builder",
            ".app.extensions.apps:builder",
            " app.extensions.apps.archives.alerts:builder",
            "app.extensions.apps.archives.alerts:__builtins__",
            "app.extensions.apps.archives.alerts:_private",
        ],
    )
    def test_rejects_malformed_path(self, path: str) -> None:
        """Reject a path that is not a well-formed ``"module:function"`` pair."""
        with pytest.raises(HookPathNotAllowedError):
            validate_hook_path(path)

    def test_accepts_allow_listed_path(self) -> None:
        """Return the path unchanged when it names an allow-listed module."""
        assert validate_hook_path(ALLOWED_PATH) == ALLOWED_PATH

    @pytest.mark.parametrize(
        "path",
        [
            ALERT_DETAIL_BUILDER,
            RUN_RESULT_RECORDER,
        ],
    )
    def test_accepts_every_in_tree_hook(self, path: str) -> None:
        """Admit both hook values the shipped task apps stamp onto their tasks."""
        assert validate_hook_path(path) == path

    def test_accepts_allow_listed_root_itself(self) -> None:
        """Admit a callable living in the allow-listed root module itself."""
        assert (
            validate_hook_path("app.extensions.apps:builder")
            == "app.extensions.apps:builder"
        )

    def test_rejection_names_the_offending_field(self) -> None:
        """Name the offending field in the rejection message."""
        with pytest.raises(HookPathNotAllowedError, match="alert_detail_builder"):
            validate_hook_path("os:system", field="alert_detail_builder")

    def test_rejection_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log the rejected path before raising."""
        # Capture on the emitting logger, not caplog's root handler: LOGGING_CONFIG
        # sets propagate=False on ``app`` and may disable existing loggers, so
        # whether root sees the record depends on what ran first.
        emitting_logger = logging.getLogger(hook_resolver.__name__)
        was_disabled = emitting_logger.disabled
        emitting_logger.disabled = False
        emitting_logger.addHandler(caplog.handler)
        try:
            with (
                caplog.at_level(logging.WARNING, logger=hook_resolver.__name__),
                pytest.raises(HookPathNotAllowedError),
            ):
                validate_hook_path("os:system", field="run_result_recorder")
        finally:
            emitting_logger.removeHandler(caplog.handler)
            emitting_logger.disabled = was_disabled

        assert "os:system" in caplog.text
        assert "run_result_recorder" in caplog.text

    def test_rejects_before_importing(self, mocker: MockerFixture) -> None:
        """Reject a denied module without importing it."""
        spy = mocker.spy(importlib, "import_module")

        with pytest.raises(HookPathNotAllowedError):
            resolve_hook("os:system")

        spy.assert_not_called()

    def test_rejects_denied_path_already_in_cache(self, mocker: MockerFixture) -> None:
        """Fail closed on a denied path even when the cache already holds it."""
        mocker.patch.dict(hook_resolver._RESOLVED, {"os:system": print}, clear=True)

        with pytest.raises(HookPathNotAllowedError):
            resolve_hook("os:system")

    def test_does_not_cache_a_denied_path(self) -> None:
        """Leave the cache untouched when a path is rejected."""
        with pytest.raises(HookPathNotAllowedError):
            resolve_hook("os:system")

        assert "os:system" not in hook_resolver._RESOLVED

    def test_honours_an_extended_allow_list(self, mocker: MockerFixture) -> None:
        """Admit a module under a root added to the configured allow-list."""
        mocker.patch(
            "app.tasks.config.tasks_settings.HOOK_MODULE_ALLOWLIST",
            ("app.extensions.apps", "app.tasks"),
        )

        resolved = validate_hook_path("app.tasks.alert_hooks:build_owner_alert_details")

        assert resolved == "app.tasks.alert_hooks:build_owner_alert_details"

    def test_is_a_value_error(self) -> None:
        """Subclass ``ValueError`` so existing hook call sites keep degrading."""
        assert issubclass(HookPathNotAllowedError, ValueError)
