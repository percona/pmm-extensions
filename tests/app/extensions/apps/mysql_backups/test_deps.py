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

"""Define tests for the app.extensions.apps.mysql_backups.deps module."""

import sys
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.core.exceptions import HTTPNotFoundException
from app.extensions.apps.mysql_backups.deps import (
    _extract_backup_type_from_task,
    build_mysql_backups_api_task_response,
    parse_backup_task_data,
    resolve_optional_catalog_service_key,
)
from app.extensions.apps.mysql_backups.forms import EncryptionFormat
from app.extensions.apps.mysql_backups.models import (
    BackupType,
    CatalogServiceKey,
    extract_backup_type_marker,
    UNKNOWN_SERVICE_SENTINEL,
)
from app.tasks.models import (
    Task,
)
from tests.app.extensions.apps.mysql_backups.conftest import (
    inventory_mock,
    service_payload,
)


@pytest.mark.parametrize(
    ("all_servers", "expected_alt_host"),
    [
        (
            {
                "BINLOG_ALTERNATIVE_HOST": "10.0.0.5",
                "BINLOG_PREFIX": "binlog",
            },
            "10.0.0.5",
        ),
        (
            {"BINLOG_PREFIX": "binlog"},
            None,
        ),
    ],
)
def test_parse_backup_task_data(
    all_servers: dict[str, Any], expected_alt_host: str | None
):
    """Round-trip the binlog alt host from persisted YAML on the edit form path."""
    fake_task_dict = {
        "name": "test_task",
        "data": {
            "meta": {
                "target": "host.example.com",
                "config": yaml.dump(
                    {
                        "SERVER_LIST": [
                            {
                                "ALIAS": "db1-mysql",
                                "HOST": "10.0.0.5",
                                "PORT": 3306,
                                "BACKUP_TYPE": BackupType.BINLOG.value,
                                "UPLOAD": ["gsutil"],
                            }
                        ],
                        "ALL_SERVERS": all_servers,
                    }
                ),
            }
        },
    }

    result = parse_backup_task_data(fake_task_dict)

    assert result["name"] == "test_task"
    assert result["hostname"] == "host.example.com"
    assert result["backup_type"] == BackupType.BINLOG.value
    assert result["service_id"] is None
    assert result["host"] == "10.0.0.5"
    assert result["binlog_alternative_host"] == expected_alt_host


class TestParseBackupTaskDataXtrabackupQuiet:
    """Tests for XTRABACKUP_QUIET round-trip through ``parse_backup_task_data``."""

    def _make_task_dict(self, all_servers: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": "test_task",
            "data": {
                "meta": {
                    "target": "host.example.com",
                    "config": yaml.dump(
                        {
                            "SERVER_LIST": [
                                {
                                    "ALIAS": "db1-mysql",
                                    "HOST": "10.0.0.5",
                                    "PORT": 3306,
                                    "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                }
                            ],
                            "ALL_SERVERS": all_servers,
                        }
                    ),
                }
            },
        }

    @pytest.mark.parametrize(
        ("all_servers", "expected"),
        [
            ({"XTRABACKUP_QUIET": True}, True),
            ({"XTRABACKUP_QUIET": False}, False),
            # Absent key: legacy tasks pre-date the field; form must render unchecked.
            ({}, None),
            # YAML null value behaves identically to absent key.
            ({"XTRABACKUP_QUIET": None}, None),
        ],
    )
    def test_xtrabackup_quiet_round_trips(
        self, all_servers: dict[str, Any], *, expected: bool | None
    ):
        """XTRABACKUP_QUIET round-trips from persisted YAML to the edit-form dict."""
        result = parse_backup_task_data(self._make_task_dict(all_servers))
        assert result["xtrabackup_quiet"] == expected

    def test_missing_all_servers_section_returns_none(self):
        """Return ``None`` for ``xtrabackup_quiet`` when ``ALL_SERVERS`` is absent.

        Guards the legacy-task path where the config YAML pre-dates the
        ``ALL_SERVERS`` section entirely.
        """
        task_dict = {
            "name": "legacy_task",
            "data": {
                "meta": {
                    "target": "host.example.com",
                    "config": yaml.dump(
                        {
                            "SERVER_LIST": [
                                {
                                    "ALIAS": "db1",
                                    "HOST": "10.0.0.5",
                                    "PORT": 3306,
                                    "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                }
                            ]
                        }
                    ),
                }
            },
        }
        result = parse_backup_task_data(task_dict)
        assert result["xtrabackup_quiet"] is None

    def test_other_fields_are_unaffected(self):
        """Adding xtrabackup_quiet must not clobber adjacent fields in the result."""
        result = parse_backup_task_data(
            self._make_task_dict({"XTRABACKUP_QUIET": True, "BINLOG_PREFIX": "bp"})
        )
        assert result["xtrabackup_quiet"] is True
        assert result["binlog_prefix"] == "bp"


class TestParseBackupTaskDataUploadQuiet:
    """Tests for UPLOAD_QUIET round-trip through ``parse_backup_task_data``."""

    def _make_task_dict(self, all_servers: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": "test_task",
            "data": {
                "meta": {
                    "target": "host.example.com",
                    "config": yaml.dump(
                        {
                            "SERVER_LIST": [
                                {
                                    "ALIAS": "db1",
                                    "HOST": "10.0.0.1",
                                    "PORT": 3306,
                                    "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                    "UPLOAD": ["s3"],
                                }
                            ],
                            "ALL_SERVERS": all_servers,
                        }
                    ),
                }
            },
        }

    @pytest.mark.parametrize(
        ("all_servers", "expected"),
        [
            ({"UPLOAD_QUIET": True}, True),
            ({"UPLOAD_QUIET": False}, False),
            # Absent key: legacy tasks pre-date the field; form must render unchecked.
            ({}, None),
            # YAML null value behaves identically to absent key.
            ({"UPLOAD_QUIET": None}, None),
        ],
    )
    def test_upload_quiet_round_trips(
        self, all_servers: dict[str, Any], *, expected: bool | None
    ):
        """UPLOAD_QUIET round-trips from persisted YAML to the edit-form dict."""
        result = parse_backup_task_data(self._make_task_dict(all_servers))
        assert result["upload_quiet"] == expected

    def test_missing_all_servers_section_returns_none(self):
        """Return ``None`` for ``upload_quiet`` when ``ALL_SERVERS`` is absent.

        Guards the legacy-task path where the config YAML pre-dates the
        ``ALL_SERVERS`` section entirely.
        """
        task_dict = {
            "name": "legacy_task",
            "data": {
                "meta": {
                    "target": "host.example.com",
                    "config": yaml.dump(
                        {
                            "SERVER_LIST": [
                                {
                                    "ALIAS": "db1",
                                    "HOST": "10.0.0.1",
                                    "PORT": 3306,
                                    "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                    "UPLOAD": ["s3"],
                                }
                            ]
                        }
                    ),
                }
            },
        }
        result = parse_backup_task_data(task_dict)
        assert result["upload_quiet"] is None

    def test_other_fields_are_unaffected(self):
        """Adding upload_quiet must not clobber adjacent fields in the result."""
        result = parse_backup_task_data(
            self._make_task_dict({"UPLOAD_QUIET": True, "BINLOG_PREFIX": "bp"})
        )
        assert result["upload_quiet"] is True
        assert result["binlog_prefix"] == "bp"


@pytest.mark.parametrize(
    ("all_servers", "expected"),
    [
        ({"UPLOAD_QUIET": True}, True),
        ({"UPLOAD_QUIET": False}, False),
        ({}, None),
    ],
)
def test_parse_backup_task_data_upload_quiet(
    all_servers: dict[str, Any], *, expected: bool | None
) -> None:
    """Round-trip upload_quiet from persisted YAML on the edit form path."""
    fake_task_dict = {
        "name": "test_task",
        "data": {
            "meta": {
                "target": "host.example.com",
                "config": yaml.dump(
                    {
                        "SERVER_LIST": [
                            {
                                "ALIAS": "db1",
                                "HOST": "10.0.0.1",
                                "PORT": 3306,
                                "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                "UPLOAD": ["s3"],
                            }
                        ],
                        "ALL_SERVERS": all_servers,
                    }
                ),
            }
        },
    }

    result = parse_backup_task_data(fake_task_dict)

    assert result["upload_quiet"] == expected


@pytest.mark.parametrize(
    ("upload_providers", "all_servers", "expected_result"),
    [
        (
            ["s3"],
            {
                "S3_BUCKET": "my-bucket",
                "S3_STORAGE_CLASS": "STANDARD_IA",
                "SKIP_S3_SAFETY_CHECK": True,
            },
            {
                "s3_bucket": "my-bucket",
                "s3_storage_class": "STANDARD_IA",
                "skip_s3_safety_check": True,
            },
        ),
        (
            ["s3"],
            {},
            {
                "s3_bucket": None,
                "s3_storage_class": None,
                "skip_s3_safety_check": False,
            },
        ),
        (
            ["gsutil"],
            {"GS_BUCKET": "my-gs-bucket"},
            {"gs_bucket": "my-gs-bucket"},
        ),
        (
            ["gsutil"],
            {},
            {"gs_bucket": None},
        ),
        (
            ["rsync"],
            {"RSYNC_PATH": "/mnt/backups"},
            {"rsync_path": "/mnt/backups"},
        ),
        (
            ["rsync"],
            {},
            {"rsync_path": None},
        ),
        (
            ["S3"],
            {"S3_BUCKET": "case-insensitive"},
            {"s3_bucket": "case-insensitive"},
        ),
    ],
)
def test_parse_backup_task_data_storage_targets(
    upload_providers: list[str],
    all_servers: dict[str, Any],
    expected_result: dict[str, Any],
):
    """Round-trip S3/GSUTIL/RSYNC storage-target keys from persisted YAML on the edit form path."""
    fake_task_dict = {
        "name": "test_task",
        "data": {
            "meta": {
                "target": "host.example.com",
                "config": yaml.dump(
                    {
                        "SERVER_LIST": [
                            {
                                "ALIAS": "db1-mysql",
                                "HOST": "10.0.0.5",
                                "PORT": 3306,
                                "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                "UPLOAD": upload_providers,
                            }
                        ],
                        "ALL_SERVERS": all_servers,
                    }
                ),
            }
        },
    }

    result = parse_backup_task_data(fake_task_dict)

    for key, value in expected_result.items():
        assert result[key] == value


def test_parse_backup_task_data_without_all_servers():
    """parse_backup_task_data handles a YAML config with no ALL_SERVERS block."""
    fake_task_dict = {
        "name": "test_task",
        "data": {
            "meta": {
                "target": "host.example.com",
                "config": yaml.dump(
                    {
                        "SERVER_LIST": [
                            {
                                "ALIAS": "db1-mysql",
                                "HOST": "10.0.0.5",
                                "PORT": 3306,
                                "BACKUP_TYPE": BackupType.BINLOG.value,
                                "UPLOAD": ["gsutil"],
                            }
                        ],
                    }
                ),
            }
        },
    }

    result = parse_backup_task_data(fake_task_dict)

    assert result["name"] == "test_task"
    assert result["hostname"] == "host.example.com"
    assert result["host"] == "10.0.0.5"
    assert result["binlog_alternative_host"] is None


@pytest.mark.parametrize(
    ("all_servers", "expected_verbose"),
    [
        ({"MYDUMPER_VERBOSE": 1}, 1),
        ({"MYDUMPER_EXTRA_ARGS": "--foo"}, None),
        ({"MYDUMPER_VERBOSE": 0}, 0),
    ],
)
def test_parse_backup_task_data_mydumper_verbose(
    all_servers: dict[str, Any], expected_verbose: int | None
):
    """Round-trip the mydumper verbose level from persisted YAML on the edit form path.

    The ``0`` (silent) case guards against a falsy-value bug: a non-empty
    ``"0"`` must survive rather than collapse to "unset"/default. The
    no-key case asserts legacy tasks pre-populate the form empty (``None``)
    rather than omitting the field entirely.
    """
    fake_task_dict = {
        "name": "test_task",
        "data": {
            "meta": {
                "target": "host.example.com",
                "config": yaml.dump(
                    {
                        "SERVER_LIST": [
                            {
                                "ALIAS": "db1-mysql",
                                "HOST": "10.0.0.5",
                                "PORT": 3306,
                                "BACKUP_TYPE": BackupType.MYDUMPER.value,
                                "UPLOAD": ["gsutil"],
                            }
                        ],
                        "ALL_SERVERS": all_servers,
                    }
                ),
            }
        },
    }

    result = parse_backup_task_data(fake_task_dict)

    assert result["mydumper_verbose"] == expected_verbose


def _task_with_raw_config(raw_config: str) -> Task:
    """Build a minimal Task whose YAML config is the given raw string."""
    return Task(
        name="t",
        owner="BACKUPS",
        data={"meta": {"config": raw_config}},
    )


@pytest.mark.parametrize(
    "raw_config",
    [
        "- just\n- a\n- list\n",
        "scalar",
        "SERVER_LIST: not-a-list\n",
        "SERVER_LIST:\n  - just-a-string\n",
        "SERVER_LIST: []\n",
        ": invalid : yaml :",
    ],
)
def test_extract_backup_type_handles_non_dict_yaml(raw_config: str):
    """Malformed/non-dict YAML must return ``None`` instead of raising."""
    assert _extract_backup_type_from_task(_task_with_raw_config(raw_config)) is None


class TestExtractBackupTypeMarkerShapeGuards:
    """Guard ``extract_backup_type_marker`` against non-string/missing shapes.

    ``meta.config`` is expected to be a YAML string, but the value is untrusted
    task data. Before the structural-match rewrite, a non-string ``config``
    (e.g. a dict) reached ``yaml.safe_load`` directly and raised
    ``AttributeError`` uncaught by the ``except yaml.YAMLError`` guard. These
    lock in the ``None``-not-raise contract for every shape short of a string.
    """

    def test_none_task_data_returns_none(self):
        """Return ``None`` for a task with no ``data`` at all."""
        assert extract_backup_type_marker(None) is None

    def test_missing_meta_key_returns_none(self):
        """Return ``None`` when ``data`` carries no ``meta`` key."""
        assert extract_backup_type_marker({}) is None

    def test_non_dict_meta_returns_none(self):
        """Return ``None`` when ``meta`` itself is not a mapping."""
        assert extract_backup_type_marker({"meta": "not-a-dict"}) is None

    def test_dict_valued_config_returns_none_without_raising(self):
        """Return ``None``, not raise, when ``config`` is a dict instead of YAML text.

        This is the exact shape that used to reach ``yaml.safe_load`` and raise
        ``AttributeError: 'dict' object has no attribute 'read'``.
        """
        task_data = {"meta": {"config": {"SERVER_LIST": [{"BACKUP_TYPE": "M"}]}}}
        assert extract_backup_type_marker(task_data) is None

    def test_non_string_backup_type_marker_returns_none(self):
        """Return ``None`` when ``BACKUP_TYPE`` itself is not a string."""
        task_data = {
            "meta": {"config": yaml.dump({"SERVER_LIST": [{"BACKUP_TYPE": 1}]})}
        }
        assert extract_backup_type_marker(task_data) is None


def _make_backup_task(
    created_by: str | None = None, last_updated_by: str | None = None
) -> Task:
    """Build a minimal backups Task carrying the given user ids."""
    return Task(
        name="backup-task",
        owner="BACKUPS",
        data={"meta": {"target": "host1", "config": ""}},
        created_by=created_by,
        last_updated_by=last_updated_by,
    )


class TestBuildMysqlBackupsApiTaskResponse:
    """Tests for build_mysql_backups_api_task_response username mapping."""

    def test_created_by_resolved_to_username_when_mapping_provided(self):
        """created_by is resolved when the mapping contains the id."""
        task = _make_backup_task(created_by="uid-abc")

        result = build_mysql_backups_api_task_response(
            task, context={"uid-abc": "Alice"}
        )

        assert result.created_by == "Alice"

    def test_created_by_falls_back_to_raw_id_when_not_in_mapping(self):
        """created_by is preserved when the id is not in the mapping."""
        task = _make_backup_task(created_by="uid-unknown")

        result = build_mysql_backups_api_task_response(
            task, context={"uid-other": "Bob"}
        )

        assert result.created_by == "uid-unknown"

    def test_last_updated_by_resolved_via_mapping(self):
        """last_updated_by is also resolved via the mapping."""
        task = _make_backup_task(last_updated_by="uid-xyz")

        result = build_mysql_backups_api_task_response(
            task, context={"uid-xyz": "Carol"}
        )

        assert result.last_updated_by == "Carol"

    def test_context_none_preserves_raw_ids(self):
        """Raw ids are unchanged when no context is bound."""
        task = _make_backup_task(created_by="uid-123", last_updated_by="uid-456")

        result = build_mysql_backups_api_task_response(task)

        assert result.created_by == "uid-123"
        assert result.last_updated_by == "uid-456"


#: Digits ``str.isdigit`` accepts but ``int`` cannot parse, written as escapes so
#: the literals stay legible (and unambiguous) in source.
_UNPARSABLE_DIGITS = ["\N{SUPERSCRIPT TWO}", "\N{SUPERSCRIPT ONE}\N{SUPERSCRIPT TWO}"]

#: A decimal digit outside ASCII, which ``int`` does parse.
_NON_ASCII_DECIMAL_SEVEN = "\N{ARABIC-INDIC DIGIT SEVEN}"

_RESOLVED_SERVICE_ID = 7


class TestResolveOptionalCatalogServiceKey:
    """Cover every branch of the restore cascade parent's catalog-key resolution."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "submitted",
        [None, "", "   ", UNKNOWN_SERVICE_SENTINEL],
        ids=["omitted", "blank", "whitespace", "sentinel"],
    )
    async def test_unusable_parent_yields_none(self, submitted) -> None:
        """Return ``None`` for a parent the catalog cannot be keyed from.

        The route turns ``None`` into an empty option list rather than an error, so
        free-text entry is never blocked by a failed options fetch.
        """
        inventory = inventory_mock()

        assert await resolve_optional_catalog_service_key(inventory, submitted) is None
        inventory.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_numeric_parent_yields_both_keys(self) -> None:
        """Return the resolved service's name and id for a numeric parent."""
        inventory = inventory_mock(
            service_payload("svc-a", service_id=_RESOLVED_SERVICE_ID)
        )

        key = await resolve_optional_catalog_service_key(inventory, "7")

        assert key == CatalogServiceKey(
            service_name="svc-a", service_id=_RESOLVED_SERVICE_ID
        )

    @pytest.mark.asyncio
    async def test_numeric_parent_yields_the_resolved_name_not_the_submitted_one(
        self,
    ) -> None:
        """Take the name from inventory, so a rename resolves to the current one."""
        inventory = inventory_mock(
            service_payload("new-name", service_id=_RESOLVED_SERVICE_ID)
        )

        key = await resolve_optional_catalog_service_key(inventory, "7")

        assert key is not None
        assert key.service_name == "new-name"
        assert key.service_id == _RESOLVED_SERVICE_ID

    @pytest.mark.asyncio
    async def test_unknown_numeric_parent_yields_none(self) -> None:
        """Degrade an unresolvable inventory id to ``None``."""
        inventory = inventory_mock(raises=HTTPNotFoundException(detail="gone"))

        assert await resolve_optional_catalog_service_key(inventory, "7") is None

    @pytest.mark.asyncio
    async def test_custom_parent_yields_the_raw_name_and_no_id(self) -> None:
        """Pass a free-typed destination through unresolved, with no id.

        The ``ServiceRef(allow_custom=True)`` escape hatch: no Inventory call and
        no type check, so a name with no inventory row can still be queried.
        """
        inventory = inventory_mock()

        key = await resolve_optional_catalog_service_key(inventory, " custom-svc ")

        assert key == CatalogServiceKey(service_name="custom-svc", service_id=None)
        inventory.get.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("submitted", _UNPARSABLE_DIGITS, ids=["one", "two"])
    async def test_unicode_digit_parent_takes_the_custom_branch(
        self, submitted
    ) -> None:
        """Treat an ``int``-unparsable unicode digit as a free-typed name.

        ``str.isdigit`` accepts these but ``int`` rejects them, so gating on it
        would send them down the numeric branch and degrade them to ``None``
        instead of the name lookup the escape hatch exists to serve.
        """
        inventory = inventory_mock()

        key = await resolve_optional_catalog_service_key(inventory, submitted)

        assert key == CatalogServiceKey(service_name=submitted, service_id=None)
        inventory.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_decimal_parent_yields_none(self) -> None:
        """Degrade a decimal string longer than ``int`` will parse to ``None``.

        ``str.isdecimal`` is ``True`` for a decimal of any length, but ``int``
        refuses one over ``sys.get_int_max_str_digits()``. The parse guard turns
        that into the same ``None`` every other unusable parent yields, rather
        than raising out of the dependency as a 500.
        """
        inventory = inventory_mock()
        oversized = "1" * (sys.get_int_max_str_digits() + 1)

        assert await resolve_optional_catalog_service_key(inventory, oversized) is None
        inventory.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_inventory_payload_surfaces(self) -> None:
        """Let a malformed Inventory payload raise instead of degrading to ``None``.

        ``pydantic.ValidationError`` subclasses ``ValueError``, so guarding the
        whole resolve call against ``ValueError`` would serve an empty picker on
        exactly the upstream-data fault an operator needs to hear about.
        """
        inventory = inventory_mock({"id": _RESOLVED_SERVICE_ID})

        with pytest.raises(ValidationError):
            await resolve_optional_catalog_service_key(inventory, "7")

    @pytest.mark.asyncio
    async def test_non_ascii_decimal_parent_resolves_as_numeric(self) -> None:
        """Resolve a non-ASCII decimal digit, which ``int`` does parse.

        ``str.isdecimal`` must not be tightened to ASCII-only: these reach the
        numeric branch today and resolve to a real service.
        """
        inventory = inventory_mock(
            service_payload("svc-a", service_id=_RESOLVED_SERVICE_ID)
        )

        key = await resolve_optional_catalog_service_key(
            inventory, _NON_ASCII_DECIMAL_SEVEN
        )

        assert key == CatalogServiceKey(
            service_name="svc-a", service_id=_RESOLVED_SERVICE_ID
        )


class TestParseBackupTaskDataEncryptionFormat:
    """Reconstruct ``ENCRYPTION_FORMAT`` in ``parse_backup_task_data``.

    Tasks stored before the selector existed carry only the format-specific
    fields, so the edit form has to infer the format they were already running —
    an inference that must land on the same four states the backup script derives,
    or re-saving a task would change which encryption runs.
    """

    def _make_task_dict(
        self,
        all_servers: dict[str, Any],
        backup_type: BackupType = BackupType.XTRABACKUP,
    ) -> dict[str, Any]:
        return {
            "name": "test_task",
            "data": {
                "meta": {
                    "target": "host.example.com",
                    "config": yaml.dump(
                        {
                            "SERVER_LIST": [
                                {
                                    "ALIAS": "db1-mysql",
                                    "HOST": "10.0.0.5",
                                    "PORT": 3306,
                                    "BACKUP_TYPE": backup_type.value,
                                }
                            ],
                            "ALL_SERVERS": all_servers,
                        }
                    ),
                }
            },
        }

    @pytest.mark.parametrize(
        ("all_servers", "expected"),
        [
            ({"ENCRYPT": False}, EncryptionFormat.NONE),
            ({"ENCRYPT": True}, EncryptionFormat.GPG),
            ({"ENCRYPT": False, "POST_RUN_ENCRYPT": True}, EncryptionFormat.GPG),
            (
                {"ENCRYPT": False, "XTRABACKUP_AES256_KEYFILE": "/etc/keyfile"},
                EncryptionFormat.AES256,
            ),
            (
                {"ENCRYPT": True, "XTRABACKUP_AES256_KEYFILE": "/etc/keyfile"},
                EncryptionFormat.DUAL,
            ),
            (
                {
                    "ENCRYPT": False,
                    "POST_RUN_ENCRYPT": True,
                    "XTRABACKUP_AES256_KEYFILE": "/etc/keyfile",
                },
                EncryptionFormat.DUAL,
            ),
        ],
    )
    def test_infers_every_legacy_state(
        self, all_servers: dict[str, Any], expected: EncryptionFormat
    ):
        """Infer each of the four formats from the fields a legacy task carries."""
        result = parse_backup_task_data(self._make_task_dict(all_servers))
        assert result["encryption_format"] == expected

    def test_absent_encrypt_infers_none(self):
        """Read an absent ``ENCRYPT`` as disabled for a PMM Extensions stored task.

        The payload reads the same absence as *enabled*, but that fail-safe guards
        a standalone run against hand-authored config. Every config PMM Extensions writes names
        ``ENCRYPT`` explicitly — see
        ``test_build_backup_spec_always_emits_encrypt_key`` — so an absent key here
        is not an encrypted task whose flag went missing.
        """
        result = parse_backup_task_data(self._make_task_dict({}))
        assert result["encryption_format"] == EncryptionFormat.NONE

    @pytest.mark.parametrize("backup_type", [BackupType.MYDUMPER, BackupType.BINLOG])
    def test_key_file_infers_aes_for_mydumper_and_binlog(self, backup_type: BackupType):
        """Infer ``aes256`` from a key file on Mydumper and Binlog tasks too.

        AES-256 is no longer XtraBackup-only; a leftover key file on these engines
        is a real AES configuration, not noise to ignore.
        """
        result = parse_backup_task_data(
            self._make_task_dict(
                {"ENCRYPT": False, "XTRABACKUP_AES256_KEYFILE": "/etc/keyfile"},
                backup_type=backup_type,
            )
        )
        assert result["encryption_format"] == EncryptionFormat.AES256

    def test_stored_format_passes_through_unchanged(self):
        """Keep an explicit stored format rather than re-inferring it.

        A task saved with ``gpg`` but still carrying a stale key file must reload
        as ``gpg``; re-inferring would read the stale field and report ``dual``.
        """
        result = parse_backup_task_data(
            self._make_task_dict(
                {
                    "ENCRYPTION_FORMAT": EncryptionFormat.GPG.value,
                    "ENCRYPT": True,
                    "XTRABACKUP_AES256_KEYFILE": "/etc/keyfile",
                }
            )
        )
        assert result["encryption_format"] == EncryptionFormat.GPG


class TestParseBackupTaskDataEncryptionRecipient:
    """Read the GPG recipient back out of a stored ``SERVER_LIST`` entry.

    The create path serializes the block through ``BackupConfigServer`` and
    ``DirEncryptConfig``, so what lands in the config is an uppercase
    ``DIR_ENCRYPT_CONFIG`` holding a space-separated ``encryption recipient``.
    Reading only the models' attribute spellings recovers nothing, and a GPG
    ``encryption_format`` requires a recipient — so the reconstructed form cannot
    validate and the task is left as it was.
    """

    def _make_task_dict(self, server_extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": "test_task",
            "data": {
                "meta": {
                    "target": "host.example.com",
                    "config": yaml.dump(
                        {
                            "SERVER_LIST": [
                                {
                                    "ALIAS": "db1-mysql",
                                    "HOST": "10.0.0.5",
                                    "PORT": 3306,
                                    "BACKUP_TYPE": BackupType.XTRABACKUP.value,
                                    **server_extra,
                                }
                            ],
                            "ALL_SERVERS": {"ENCRYPT": True},
                        }
                    ),
                }
            },
        }

    @pytest.mark.parametrize(
        "server_extra",
        [
            pytest.param(
                {"DIR_ENCRYPT_CONFIG": {"encryption recipient": "ops@example.com"}},
                id="serialized-shape",
            ),
            pytest.param(
                {"dir_encrypt_config": {"encryption_recipient": "ops@example.com"}},
                id="attribute-name-shape",
            ),
            pytest.param(
                {"DIR_ENCRYPT_CONFIG": {"encryption_recipient": "ops@example.com"}},
                id="mixed-shape",
            ),
        ],
    )
    def test_recovers_the_recipient_from_every_stored_spelling(
        self, server_extra: dict[str, Any]
    ):
        """Recover the recipient whichever of the two spellings a config carries."""
        result = parse_backup_task_data(self._make_task_dict(server_extra))
        assert result["encryption_recipient"] == "ops@example.com"

    @pytest.mark.parametrize(
        "server_extra",
        [
            pytest.param({}, id="no-block"),
            pytest.param({"DIR_ENCRYPT_CONFIG": {}}, id="empty-block"),
            pytest.param(
                {"DIR_ENCRYPT_CONFIG": {"encryption recipient": None}},
                id="null-recipient",
            ),
            pytest.param({"DIR_ENCRYPT_CONFIG": None}, id="null-block"),
        ],
    )
    def test_names_no_recipient_when_none_is_stored(self, server_extra: dict[str, Any]):
        """Leave ``encryption_recipient`` out rather than reporting a blank one.

        A blank recipient would satisfy the key's presence while failing the form's
        non-empty constraint, so the absence has to stay an absence.
        """
        result = parse_backup_task_data(self._make_task_dict(server_extra))
        assert "encryption_recipient" not in result
