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

"""Test the credential-free config facts a scan reads from the mongod config file.

The hash is computed on the host over an allowlisted, non-secret projection of the
mongod config, so no secret option is read, shipped, or hashed; the config path, the
TLS mode and the keyFile path are read from the same file as their own member facts.
These pin the projection against the server-side redaction helpers and assert a
planted secret never reaches the record.
"""

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.extensions.apps.om_inventory.payload import probe
from app.extensions.apps.shared.om.classification import config_sha256, redacted_config

#: The length of a hex SHA-256 digest, derived rather than spelled.
HEX_DIGEST_LENGTH = len(hashlib.sha256(b"").hexdigest())

#: A mongod config in the shape ``om_bootstrap`` writes, with planted secrets added:
#: a keyFile, a TLS certificate key file and its password, none of which are tracked.
SAMPLE_CONFIG = """net:
  bindIp: 0.0.0.0,127.0.0.1
  port: 27017
  tls:
    mode: requireTLS
    certificateKeyFile: /etc/mongo/tls.pem
    certificateKeyFilePassword: SUPER-SECRET
storage:
  dbPath: /var/lib/mongo
security:
  authorization: enabled
  keyFile: /etc/mongo/keyfile
  clusterAuthMode: keyFile
replication:
  replSetName: rs0
processManagement:
  fork: true
  pidFilePath: /var/run/mongodb/mongod.pid
systemLog:
  destination: file
  path: /var/log/mongodb/mongod.log  # the log
  logAppend: true
"""

#: Values the config carries that must never reach the projection: the keyfile and
#: TLS paths, the TLS password, and the data and log paths kept as their own member
#: facts instead.
EXCLUDED_VALUES = (
    "SUPER-SECRET",
    "/etc/mongo/keyfile",
    "/etc/mongo/tls.pem",
    "/var/lib/mongo",
    "/var/log/mongodb/mongod.log",
)


def write_config(tmp_path: Path, text: str = SAMPLE_CONFIG) -> str:
    """Write a mongod config file and return its path.

    :param tmp_path: The test's temporary directory.
    :param text: The config contents.
    :return: The path to the written file.
    """
    config = tmp_path / "mongod.conf"
    config.write_text(text, encoding="utf-8")
    return str(config)


def probe_record_for(config_path: str) -> dict[str, Any]:
    """Run the probe against a config path with the database read disabled.

    With ``pymongo`` absent the database read fails, so the record carries only the
    facts read from the config file and the host.

    :param config_path: The mongod config file the probed process names.
    :return: The probe record for a single mongod target.
    """
    target = {"service": "rs0-0", "service_id": "svc-1", "host": "n0", "port": 27017}
    processes = [
        {
            "program": "mongod",
            "pid": 1,
            "uptime_sec": 5,
            "port": 27017,
            "config_path": config_path,
            "argv": f"/usr/bin/mongod --config {config_path}",
        }
    ]
    with patch.dict(sys.modules, {"pymongo": None, "pymongo.errors": None}):
        return probe.probe(
            target, {"probe_database": True}, {}, processes, versions={None: "7.0"}
        )


class TestParseConfigOptions:
    """Assert the projection keeps only non-secret, allowlisted options."""

    def test_keeps_only_allowlisted_options(self, tmp_path: Path) -> None:
        """Project the config to exactly the allowlisted dotted paths it carries."""
        options = probe.parse_config_options(write_config(tmp_path))

        assert set(options) == {
            "net.bindIp",
            "net.port",
            "net.tls.mode",
            "security.authorization",
            "security.clusterAuthMode",
            "replication.replSetName",
            "processManagement.fork",
            "processManagement.pidFilePath",
            "systemLog.destination",
            "systemLog.logAppend",
        }

    def test_no_secret_or_excluded_value_reaches_the_projection(
        self, tmp_path: Path
    ) -> None:
        """Drop every secret and member-path option so none is read or hashed."""
        options = probe.parse_config_options(write_config(tmp_path))

        for key in (
            "security.keyFile",
            "net.tls.certificateKeyFile",
            "net.tls.certificateKeyFilePassword",
            "storage.dbPath",
            "systemLog.path",
        ):
            assert key not in options
        for value in EXCLUDED_VALUES:
            assert value not in repr(options)

    def test_reads_nested_and_quoted_values_without_their_comments(
        self, tmp_path: Path
    ) -> None:
        """Read a nested value and strip a quote or an inline comment from it."""
        options = probe.parse_config_options(
            write_config(
                tmp_path,
                'net:\n  tls:\n    mode: "requireTLS"  # on\n  port: 27018\n',
            )
        )

        assert options == {"net.tls.mode": "requireTLS", "net.port": "27018"}

    def test_an_unreadable_config_is_empty(self, tmp_path: Path) -> None:
        """Return nothing when the file is absent rather than guessing."""
        assert probe.parse_config_options(str(tmp_path / "absent.conf")) == {}
        assert probe.parse_config_options(None) == {}


class TestConfigSha256:
    """Assert the digest is deterministic and matches the server-side helpers."""

    def test_the_digest_is_deterministic(self, tmp_path: Path) -> None:
        """Hash the same config to the same 64-hex digest every time."""
        path = write_config(tmp_path)

        first = probe.config_sha256(probe.parse_config_options(path))
        second = probe.config_sha256(probe.parse_config_options(path))

        assert first == second
        assert len(first) == HEX_DIGEST_LENGTH

    def test_an_empty_projection_has_no_digest(self) -> None:
        """Return ``None`` for an empty projection, so an unreadable config has none."""
        assert probe.config_sha256({}) is None
        assert probe.config_sha256(probe.parse_config_options(None)) is None

    def test_the_payload_matches_the_server_redaction_and_hash(
        self, tmp_path: Path
    ) -> None:
        """Pin the payload's parse and hash to ``redacted_config``/``config_sha256``.

        The payload cannot import the app, so it mirrors the server helpers; this
        keeps a drift between the two from silently changing a stored digest.
        """
        projection = probe.parse_config_options(write_config(tmp_path))
        full = {
            "security.keyFile": "/etc/mongo/keyfile",
            "net.tls.certificateKeyFilePassword": "SUPER-SECRET",
            **projection,
        }

        assert projection, "the sample config must yield allowlisted options"
        assert projection == redacted_config(full, probe.CONFIG_ALLOWLIST)
        assert probe.config_sha256(projection) == config_sha256(projection)


class TestProbeRecordEmitsConfigSha256:
    """Assert the probe record carries ``database.config_sha256``."""

    def test_the_record_carries_the_config_hash_even_when_the_database_failed(
        self, tmp_path: Path
    ) -> None:
        """Emit the config hash though the database read failed.

        The hash is read from the config file the process already names, so it
        survives on the record even when the database read could not run.
        """
        path = write_config(tmp_path)

        record = probe_record_for(path)

        assert isinstance(record["database"], dict)
        assert record["database"]["config_sha256"] == probe.config_sha256(
            probe.parse_config_options(path)
        )


class TestParseKeyfilePath:
    """Assert the credential-free keyFile path read takes only the path."""

    def test_reads_the_keyfile_path(self, tmp_path: Path) -> None:
        """Read ``security.keyFile`` as the member's keyfile path."""
        assert probe.parse_keyfile_path(write_config(tmp_path)) == "/etc/mongo/keyfile"

    def test_reads_a_quoted_keyfile_path_without_its_quotes(
        self, tmp_path: Path
    ) -> None:
        """Strip the quotes from a quoted keyFile path."""
        path = write_config(tmp_path, 'security:\n  keyFile: "/etc/mongo/keyfile"\n')
        assert probe.parse_keyfile_path(path) == "/etc/mongo/keyfile"

    def test_a_config_without_a_keyfile_has_none(self, tmp_path: Path) -> None:
        """Return ``None`` when the config names no keyFile or cannot be read."""
        no_keyfile = write_config(tmp_path, "net:\n  port: 27017\n")
        assert probe.parse_keyfile_path(no_keyfile) is None
        assert probe.parse_keyfile_path(None) is None


class TestProbeRecordEmitsConfigFacts:
    """Assert the probe record carries the config-file facts read without a login."""

    def test_the_record_carries_the_config_file_tls_mode_and_keyfile_path(
        self, tmp_path: Path
    ) -> None:
        """Carry the config path, TLS mode and keyFile path from the config file."""
        path = write_config(tmp_path)

        database = probe_record_for(path)["database"]

        assert database["config_file"] == path
        assert database["tls_mode"] == "requireTLS"
        assert database["keyfile_path"] == "/etc/mongo/keyfile"

    def test_a_config_without_tls_or_keyfile_omits_those_facts(
        self, tmp_path: Path
    ) -> None:
        """Omit the TLS mode and keyFile path when the config names neither."""
        path = write_config(
            tmp_path, "net:\n  port: 27017\nstorage:\n  dbPath: /data\n"
        )

        database = probe_record_for(path)["database"]

        assert database["config_file"] == path
        assert "tls_mode" not in database
        assert "keyfile_path" not in database
