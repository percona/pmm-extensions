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

"""Field-classification regression tests.

Assert the reclassifications (``APP_DRAIN`` -> NESTED_ONLY; ``SNIPPETS_BASE_URL``,
``SYNC_FILTER``, ``LOGGING`` -> HOT) and the per-leaf ``advanced`` markers on the
NOMAD, PMM, PMM Extensions, and Tasks settings landed with the intended reload classification
-- and, critically, that the excluded connection leaves (``endpoint``, ``api_key``)
stayed unmarked and their override eligibility is unchanged.
"""

import pytest

# ``app.tasks.config`` is imported first to resolve the pre-existing
# config <-> nomad-executor import cycle before the executor model is touched.
import app.tasks.config  # noqa: F401
from app.core.config import PMMSettings, Settings
from app.core.settings_override.registry import (
    field_reload_classification,
    is_advanced_field,
    is_explicit_not_overridable,
    is_nested_overridable_parent,
    ReloadClassification,
)
from app.extensions.config import ExtensionsSettings
from app.extensions.snippets.config import SnippetsSettings
from app.tasks.config import TasksSettings
from app.tasks.execution.executors.nomad.models import NomadExecutor


def _reload(cls: type, field: str) -> ReloadClassification:
    return field_reload_classification(
        cls.model_fields[field], owner_cls=cls, field_name=field
    )


def _advanced(cls: type, field: str) -> bool:
    return is_advanced_field(cls.model_fields[field], owner_cls=cls, field_name=field)


class TestReclassification:
    """Behavior-changing reclassifications."""

    def test_app_drain_is_nested_only(self) -> None:
        """Assert ``APP_DRAIN`` becomes a NESTED_ONLY overridable parent."""
        assert (
            _reload(ExtensionsSettings, "APP_DRAIN") is ReloadClassification.NESTED_ONLY
        )
        assert is_nested_overridable_parent(ExtensionsSettings, "APP_DRAIN") is True

    @pytest.mark.parametrize("field", ["SNIPPETS_BASE_URL", "SYNC_FILTER"])
    def test_snippets_fields_are_hot(self, field: str) -> None:
        """Assert ``SNIPPETS_BASE_URL`` and ``SYNC_FILTER`` become HOT fields."""
        assert _reload(SnippetsSettings, field) is ReloadClassification.HOT

    def test_logging_is_hot(self) -> None:
        """Assert the global ``Settings.LOGGING`` field becomes HOT."""
        assert _reload(Settings, "LOGGING") is ReloadClassification.HOT


class TestAdvancedMarkers:
    """Display-only ``advanced`` markers (must not touch eligibility)."""

    @pytest.mark.parametrize(
        "field",
        [
            "frontend",
            "verify_ssl",
            "execution_target",
            "annotations_enabled",
            "annotations_timeout",
        ],
    )
    def test_pmm_secondary_leaves_advanced(self, field: str) -> None:
        """Assert each PMM secondary leaf carries the display-only advanced marker."""
        assert _advanced(PMMSettings, field) is True

    @pytest.mark.parametrize("field", ["endpoint", "api_key"])
    def test_pmm_connection_leaves_not_advanced(self, field: str) -> None:
        """Verify PMM connection leaves stay unmarked and keep their eligibility."""
        assert _advanced(PMMSettings, field) is False
        # Connection leaves must not have been flipped to NOT_OVERRIDABLE.
        assert is_explicit_not_overridable(PMMSettings.model_fields[field]) is False

    @pytest.mark.parametrize(
        "field",
        [
            "verify_ssl",
            "ssl_cafile",
            "ssl_keyfile",
            "ssl_certfile",
            "secure",
            "timeout",
            "minify_payload",
            "log_socket_read_timeout",
            "log_stream_max_connections",
            "cert_expiry_warn_days",
            "check_cert_expiry_interval",
            "log_anonymization_max_withheld_bytes",
            "log_capture_hold_seconds",
            "auth_scheme",
        ],
    )
    def test_nomad_secondary_leaves_advanced(self, field: str) -> None:
        """Assert each NOMAD TLS/tuning leaf carries the advanced marker."""
        assert _advanced(NomadExecutor, field) is True

    @pytest.mark.parametrize("field", ["endpoint", "api_key"])
    def test_nomad_connection_leaves_not_advanced(self, field: str) -> None:
        """Assert the NOMAD connection leaves stay unmarked and overridable."""
        assert _advanced(NomadExecutor, field) is False
        assert is_explicit_not_overridable(NomadExecutor.model_fields[field]) is False

    def test_nomad_frozen_tls_leaves_preserved(self) -> None:
        """Assert the inherited TLS leaves keep ``frozen`` for hash stability."""
        # The TLS leaves are marked via ``INHERITED_MARKERS`` rather than
        # redeclared, so they keep ``BaseRemoteAPI``'s ``frozen=True`` and the
        # executor identity hash is unchanged.
        for field in ("verify_ssl", "ssl_cafile", "ssl_keyfile", "ssl_certfile"):
            assert NomadExecutor.model_fields[field].frozen is True

    def test_nomad_default_factory_leaf_constructs(self) -> None:
        """Verify the ``default_factory`` leaf constructs and stays HOT + advanced."""
        # ``check_cert_expiry_interval`` keeps its ``default_factory`` (no
        # default/default_factory conflict) and stays HOT + advanced.
        default = NomadExecutor.model_fields["check_cert_expiry_interval"].get_default(
            call_default_factory=True
        )
        assert default is not None
        assert _reload(NomadExecutor, "check_cert_expiry_interval") is (
            ReloadClassification.HOT
        )

    def test_extensions_artifact_download_ttl_advanced(self) -> None:
        """Assert ``ARTIFACT_DOWNLOAD_TTL`` is HOT and advanced."""
        assert _advanced(ExtensionsSettings, "ARTIFACT_DOWNLOAD_TTL") is True
        assert (
            _reload(ExtensionsSettings, "ARTIFACT_DOWNLOAD_TTL")
            is ReloadClassification.HOT
        )

    @pytest.mark.parametrize(
        "field",
        [
            "SYNC_LOCK_TTL",
            "STALENESS_THRESHOLD_SECONDS",
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            "LOG_RETENTION_DAYS",
            "LOG_PURGE_BATCH_SIZE",
            "LOG_STREAM_CAP_BYTES",
            "LOG_STREAM_EVICTION_MAX_ROWS",
        ],
    )
    def test_tasks_advanced_cluster(self, field: str) -> None:
        """Assert the Tasks log-retention / lock / staleness cluster is advanced."""
        assert _advanced(TasksSettings, field) is True
        assert _reload(TasksSettings, field) is ReloadClassification.HOT
