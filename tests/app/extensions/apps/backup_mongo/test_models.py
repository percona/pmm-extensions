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

"""Tests for the backup_mongo response models rebased onto BaseTaskResponse."""

import pytest
from pydantic import ValidationError

from app.extensions.apps.backup_mongo.models import (
    BackupCreate,
    BackupTaskDetailResponse,
    BackupTaskResponse,
    BackupTaskWrite,
    BackupType,
    parse_backup_priority,
    validate_storage_config,
)
from app.extensions.apps.framework import BaseTaskResponse
from app.tasks.models import TaskBackendEnum

# A well-formed multi-line block mapping of node -> priority.
_VALID_PRIORITY_YAML = '"h1:27018": 2\n"h2:27018": 1'
# The reporter's repro: every mapping crammed onto a single line (invalid YAML).
_SINGLE_LINE_PRIORITY_YAML = '"h1:27018": 2 "h2:27018": 2'


def _backup_create(**overrides: object) -> BackupCreate:
    """Build a minimal valid BackupCreate with the given field overrides."""
    fields: dict[str, object] = {
        "task_name": "mongo-backup-task",
        "hostname": "mongo-host",
        "service_id": 1,
        "backup_type": BackupType.PBM_CONFIG,
        "storage_type": "filesystem",
        "storage_filesystem_path": "/var/backups/mongo",
        "pitr_compression": "snappy",
    }
    fields.update(overrides)
    return BackupCreate(**fields)


def _backup_write(**overrides: object) -> BackupTaskWrite:
    """Build a minimal valid BackupTaskWrite with the given field overrides."""
    fields: dict[str, object] = {
        "task_name": "mongo-backup-task",
        "hostname": "mongo-host",
        "service_id": 1,
        "storage_type": "filesystem",
        "storage_filesystem_path": "/var/backups/mongo",
        "pitr_compression": "snappy",
    }
    fields.update(overrides)
    return BackupTaskWrite(**fields)


class TestParseBackupPriority:
    """Cover the shared ``parse_backup_priority`` helper."""

    def test_parses_block_mapping_to_floats(self) -> None:
        """Return a node -> float mapping from a valid block mapping."""
        assert parse_backup_priority(_VALID_PRIORITY_YAML) == {
            "h1:27018": 2.0,
            "h2:27018": 1.0,
        }

    def test_parses_flow_mapping(self) -> None:
        """Accept a YAML flow mapping."""
        assert parse_backup_priority('{"h1:27018": 2}') == {"h1:27018": 2.0}

    def test_rejects_invalid_yaml(self) -> None:
        """Raise ValueError on the single-line repro (invalid YAML)."""
        with pytest.raises(ValueError, match="valid YAML"):
            parse_backup_priority(_SINGLE_LINE_PRIORITY_YAML)

    @pytest.mark.parametrize("value", ["just text", "42", "- a\n- b"])
    def test_rejects_non_mapping(self, value: str) -> None:
        """Raise ValueError when the YAML is not a mapping."""
        with pytest.raises(ValueError, match="mapping"):
            parse_backup_priority(value)

    def test_rejects_non_numeric_value(self) -> None:
        """Raise ValueError when a priority value is not numeric."""
        with pytest.raises(ValueError, match="number"):
            parse_backup_priority('"h1:27018": high')

    def test_accepts_float_value(self) -> None:
        """Preserve a fractional priority as a float."""
        assert parse_backup_priority('"h1:27018": 2.5') == {"h1:27018": 2.5}

    def test_rejects_null_value(self) -> None:
        """Raise ValueError on a YAML null priority value."""
        with pytest.raises(ValueError, match="number"):
            parse_backup_priority('"h1:27018": null')

    @pytest.mark.parametrize("value", ['"h1:27018": {"a": 1}', '"h1:27018": [1, 2]'])
    def test_rejects_collection_value(self, value: str) -> None:
        """Raise ValueError when a priority value is a mapping or list."""
        with pytest.raises(ValueError, match="number"):
            parse_backup_priority(value)

    @pytest.mark.parametrize("value", ["{}", "  "])
    def test_rejects_empty_mapping(self, value: str) -> None:
        """Raise ValueError on a present-but-empty mapping (would be silently dropped)."""
        with pytest.raises(ValueError, match=r"empty|mapping"):
            parse_backup_priority(value)

    def test_rejects_bool_value(self) -> None:
        """Raise ValueError on a boolean value (float(True) would be 1.0)."""
        with pytest.raises(ValueError, match="number"):
            parse_backup_priority('"h1:27018": true')


class TestBackupPriorityValidation:
    """Validate that both create models enforce Node Priority YAML checks."""

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_accepts_valid_block_mapping(self, factory) -> None:
        """Construct with a valid mapping and preserve the raw string."""
        model = factory(backup_priority=_VALID_PRIORITY_YAML)
        assert model.backup_priority == _VALID_PRIORITY_YAML

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_accepts_flow_mapping(self, factory) -> None:
        """Accept a YAML flow mapping."""
        model = factory(backup_priority='{"h1:27018": 2}')
        assert model.backup_priority == '{"h1:27018": 2}'

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_single_line_repro(self, factory) -> None:
        """Reject the single-line repro with a validation error."""
        with pytest.raises(ValidationError):
            factory(backup_priority=_SINGLE_LINE_PRIORITY_YAML)

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    @pytest.mark.parametrize("value", ["just text", "- a\n- b"])
    def test_rejects_non_mapping(self, factory, value: str) -> None:
        """Reject non-mapping YAML with a validation error."""
        with pytest.raises(ValidationError):
            factory(backup_priority=value)

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_non_numeric_value(self, factory) -> None:
        """Reject a non-numeric priority value with a validation error."""
        with pytest.raises(ValidationError):
            factory(backup_priority='"h1:27018": high')

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_bool_value(self, factory) -> None:
        """Reject a boolean priority value with a validation error."""
        with pytest.raises(ValidationError):
            factory(backup_priority='"h1:27018": true')

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_empty_mapping(self, factory) -> None:
        """Reject a present-but-empty mapping so it is never silently dropped."""
        with pytest.raises(ValidationError):
            factory(backup_priority="{}")

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_empty_string_becomes_none(self, factory) -> None:
        """Treat an empty string as no priority (None), never validated."""
        model = factory(backup_priority="")
        assert model.backup_priority is None


def _call_validate(**overrides: object) -> None:
    """Call ``validate_storage_config`` starting from a valid S3 config.

    :param overrides: Field values overriding the well-formed S3 defaults.
    :return: The helper's ``None`` return, so callers can assert on it.
    """
    kwargs: dict[str, object] = {
        "storage_type": "s3",
        "s3_bucket": "backups",
        "s3_region": "eu-west-1",
        "s3_prefix": None,
        "s3_endpoint_url": None,
        "filesystem_path": None,
    }
    kwargs.update(overrides)
    storage_type = kwargs.pop("storage_type")
    return validate_storage_config(storage_type, **kwargs)


class TestValidateStorageConfig:
    """Cover the shared ``validate_storage_config`` helper."""

    def test_accepts_s3_with_bucket_and_region(self) -> None:
        """Return None for a well-formed S3 config."""
        assert _call_validate() is None

    def test_accepts_s3_with_prefix_and_endpoint(self) -> None:
        """Return None for an S3 config with a prefix and a valid endpoint URL."""
        assert (
            _call_validate(s3_prefix="mongo", s3_endpoint_url="https://s3.example.com")
            is None
        )

    def test_accepts_filesystem_with_path(self) -> None:
        """Return None for a well-formed filesystem config."""
        assert (
            _call_validate(
                storage_type="filesystem",
                s3_bucket=None,
                s3_region=None,
                filesystem_path="/var/backups/mongo",
            )
            is None
        )

    @pytest.mark.parametrize("storage_type", [None, "", "gcs", "azure"])
    def test_rejects_unsupported_type(self, storage_type: str | None) -> None:
        """Raise ValueError for an absent or unsupported storage type."""
        with pytest.raises(ValueError, match="storage_type"):
            _call_validate(storage_type=storage_type)

    @pytest.mark.parametrize("bucket", [None, "", "   "])
    def test_rejects_s3_without_bucket(self, bucket: str | None) -> None:
        """Raise ValueError when S3 bucket is missing or blank."""
        with pytest.raises(ValueError, match="bucket"):
            _call_validate(s3_bucket=bucket)

    @pytest.mark.parametrize("region", [None, "", "   "])
    def test_rejects_s3_without_region(self, region: str | None) -> None:
        """Raise ValueError when S3 region is missing or blank."""
        with pytest.raises(ValueError, match="region"):
            _call_validate(s3_region=region)

    @pytest.mark.parametrize("region", ["us-east2", "US-EAST-1", "not a region"])
    def test_rejects_malformed_aws_region_without_endpoint(self, region: str) -> None:
        """Raise ValueError for a malformed AWS region when no endpoint is set."""
        with pytest.raises(ValueError, match="valid AWS region"):
            _call_validate(s3_region=region)

    @pytest.mark.parametrize("region", ["us-east2", "minio", "anything"])
    def test_allows_provider_region_with_custom_endpoint(self, region: str) -> None:
        """Skip the AWS region format check when a custom endpoint is set."""
        assert (
            _call_validate(
                s3_region=region, s3_endpoint_url="https://minio.example.com"
            )
            is None
        )

    def test_rejects_filesystem_path_set_for_s3(self) -> None:
        """Raise ValueError when a filesystem path is set alongside S3 storage."""
        with pytest.raises(ValueError, match="Filesystem path must not be set"):
            _call_validate(filesystem_path="/var/backups/mongo")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("s3_bucket", "backups"),
            ("s3_region", "eu-west-1"),
            ("s3_prefix", "mongo"),
            ("s3_endpoint_url", "https://s3.example.com"),
        ],
    )
    def test_rejects_s3_fields_set_for_filesystem(self, field: str, value: str) -> None:
        """Raise ValueError when an S3 field is set alongside filesystem storage."""
        overrides = {
            "storage_type": "filesystem",
            "s3_bucket": None,
            "s3_region": None,
            "filesystem_path": "/var/backups/mongo",
            field: value,
        }
        with pytest.raises(ValueError, match="must not be set for filesystem"):
            _call_validate(**overrides)

    @pytest.mark.parametrize("path", [None, "", "   "])
    def test_rejects_filesystem_without_path(self, path: str | None) -> None:
        """Raise ValueError when the filesystem path is missing or blank."""
        with pytest.raises(ValueError, match="path"):
            _call_validate(
                storage_type="filesystem",
                s3_bucket=None,
                s3_region=None,
                filesystem_path=path,
            )


def _s3_overrides(**extra: object) -> dict[str, object]:
    """Return S3 storage field overrides for the create-model factories."""
    return {
        "storage_type": "s3",
        "storage_s3_bucket": "backups",
        "storage_s3_region": "eu-west-1",
        "storage_filesystem_path": None,
        **extra,
    }


class TestStorageValidation:
    """Validate that both create models enforce the S3-storage config checks."""

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_accepts_valid_s3(self, factory) -> None:
        """Construct with a well-formed S3 config."""
        model = factory(**_s3_overrides())
        assert model.storage_s3_bucket == "backups"
        assert model.storage_s3_region == "eu-west-1"

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_accepts_valid_filesystem(self, factory) -> None:
        """Construct with a well-formed filesystem config (factory default)."""
        model = factory()
        assert model.storage_type == "filesystem"

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_accepts_optional_prefix_and_endpoint_absent(self, factory) -> None:
        """Treat S3 prefix and endpoint URL as optional."""
        model = factory(**_s3_overrides())
        assert model.storage_s3_prefix is None
        assert model.storage_s3_endpoint_url is None

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_s3_without_bucket(self, factory) -> None:
        """Reject an S3 config missing a bucket with a validation error."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_s3_bucket=None))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_s3_without_region(self, factory) -> None:
        """Reject an S3 config missing a region with a validation error."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_s3_region=None))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_s3_with_blank_bucket(self, factory) -> None:
        """Reject a whitespace-only S3 bucket with a validation error."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_s3_bucket="   "))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_filesystem_without_path(self, factory) -> None:
        """Reject a filesystem config missing a path with a validation error."""
        with pytest.raises(ValidationError):
            factory(storage_type="filesystem", storage_filesystem_path=None)

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_unsupported_storage_type(self, factory) -> None:
        """Reject an unsupported storage type with a validation error."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_type="gcs"))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    @pytest.mark.parametrize(
        "bucket",
        [
            "@@",
            "UPPER",
            "a",
            "no_underscores",
            "-lead",
            "a..b",
            "a.-b",
            "a-.b",
            "192.168.5.4",
        ],
    )
    def test_rejects_malformed_bucket(self, factory, bucket: str) -> None:
        """Reject a present-but-non-DNS-compliant S3 bucket name at the field level."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_s3_bucket=bucket))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_strips_surrounding_whitespace_on_s3_fields(self, factory) -> None:
        """Trim padded S3 values so the untrimmed string never reaches the PBM config."""
        model = factory(
            **_s3_overrides(
                storage_s3_bucket="  backups  ", storage_s3_region="  eu-west-1  "
            )
        )
        assert model.storage_s3_bucket == "backups"
        assert model.storage_s3_region == "eu-west-1"

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_malformed_region_without_endpoint(self, factory) -> None:
        """Reject a malformed AWS region when no custom endpoint is set."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_s3_region="us-east2"))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_allows_provider_region_with_endpoint(self, factory) -> None:
        """Accept a non-AWS region when a custom endpoint marks S3-compatible storage."""
        model = factory(
            **_s3_overrides(
                storage_s3_region="minio",
                storage_s3_endpoint_url="https://minio.example.com",
            )
        )
        assert model.storage_s3_endpoint_url == "https://minio.example.com"

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_normalizes_endpoint_trailing_slash(self, factory) -> None:
        """Strip a trailing slash on the endpoint URL before it reaches the config builder."""
        model = factory(
            **_s3_overrides(
                storage_s3_region="minio",
                storage_s3_endpoint_url="https://minio.example.com/",
            )
        )
        assert model.storage_s3_endpoint_url == "https://minio.example.com"

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    @pytest.mark.parametrize(
        "url", ["not a url", "ftp://host", "example.com", "://x", "https://:443"]
    )
    def test_rejects_malformed_endpoint_url(self, factory, url: str) -> None:
        """Reject a malformed S3 endpoint URL at the field level."""
        with pytest.raises(ValidationError):
            factory(**_s3_overrides(storage_s3_endpoint_url=url))

    @pytest.mark.parametrize("factory", [_backup_create, _backup_write])
    def test_rejects_s3_field_set_for_filesystem(self, factory) -> None:
        """Reject an S3 field set alongside filesystem storage."""
        with pytest.raises(ValidationError):
            factory(
                storage_type="filesystem",
                storage_filesystem_path="/var/backups/mongo",
                storage_s3_bucket="backups",
            )

    def test_write_requires_storage_type(self) -> None:
        """Require ``storage_type`` on the JSON request model (contract matches runtime)."""
        fields = {
            "task_name": "mongo-backup-task",
            "hostname": "mongo-host",
            "service_id": 1,
        }
        with pytest.raises(ValidationError):
            BackupTaskWrite(**fields)


class TestBackupMongoResponseModels:
    """Tests for the backup_mongo response models inheriting ``BaseTaskResponse``."""

    def test_response_exposes_inherited_task_response_surface(self) -> None:
        """Carry the shared anonymization and connectivity surface from the base."""
        response = BackupTaskResponse(
            name="mongo-backup",
            owner="BACKUP_MONGO",
            hostname="mongo-host",
            backend=TaskBackendEnum.PROXY,
            backup_type=BackupType.PBM_LOGICAL.value,
            data={"meta": {"target": "mongo-host"}},
            protected=False,
            alert_on_fail=False,
        )

        dumped = response.model_dump(mode="json")

        assert isinstance(response, BaseTaskResponse)
        assert dumped["backup_type"] == BackupType.PBM_LOGICAL.value
        assert dumped["hostname"] == "mongo-host"
        assert "service_type" not in dumped
        assert "owner" not in dumped
        assert "anonymize_mask" in dumped
        assert "anonymized_entities" in dumped
        assert "connectivity_warning" in dumped

    def test_detail_response_inherits_surface_and_keeps_extras(self) -> None:
        """Keep the derived-task extras while inheriting the base response surface."""
        detail = BackupTaskDetailResponse(
            name="mongo-backup",
            owner="BACKUP_MONGO",
            backend=TaskBackendEnum.PROXY,
            backup_type=BackupType.PBM_CONFIG.value,
            data={},
            protected=False,
            alert_on_fail=False,
        )

        dumped = detail.model_dump(mode="json")

        assert isinstance(detail, BaseTaskResponse)
        assert detail.derived_tasks == []
        assert detail.latest_pbm_status is None
        assert dumped["connectivity_warning"] is None
        assert "anonymize_mask" in dumped
        assert "anonymized_entities" in dumped


def test_backup_type_labels_cover_every_member():
    """Label every declared ``BackupType`` member, keyed by its stored value."""
    assert set(BackupType.LABELS) == {member.value for member in BackupType}


def test_backup_type_labels_are_the_declared_display_strings():
    """Pin the display text each stored backup-type value resolves to."""
    assert BackupType.LABELS == {
        "pbm_logical": "PBM Logical",
        "pbm_physical": "PBM Physical",
        "pbm_incremental": "PBM Incremental",
        "pbm_snapshot": "PBM Snapshot",
        "pbm_config": "PBM Config",
        "pbm_status": "PBM Status",
    }


def test_backup_type_labels_is_not_an_enum_member():
    """Keep ``LABELS`` off the enum's member list via ``enum.nonmember``."""
    assert "LABELS" not in {member.name for member in BackupType}
