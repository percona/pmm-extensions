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

"""Test the app.extensions.sync.syncers.system_facts.payload module."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from app.extensions.sync.syncers.system_facts import payload as payload_module
from app.extensions.sync.syncers.system_facts.payload import (
    _collect_mongodb_version,
    _collect_mysql_version,
    _collect_postgresql_version,
    _mysql_creds,
    _redact_secrets,
    collect_can_elevate,
    collect_host_facts,
    collect_installed_packages,
    collect_os_version,
    collect_service_version,
    main,
    parse_host_port,
    ServiceType,
)
from app.inventory.models import HOST_OBSERVATION_FIELD_NAMES

MODULE = "app.extensions.sync.syncers.system_facts.payload"
OS_RELEASE = (
    'NAME="Ubuntu"\n'
    'VERSION_ID="22.04"\n'
    'VERSION="22.04.3 LTS (Jammy Jellyfish)"\n'
    'PRETTY_NAME="Ubuntu 22.04.3 LTS"\n'
    "ID=ubuntu\n"
)
MYSQL_ADDRESS = "10.0.0.5:3306"
MYSQL_VERSION = "8.0.35"
PG_ADDRESS = "10.0.0.5:5432"
PG_VERSION = "15.4"
MONGO_ADDRESS = "10.0.0.5:27017"
MONGO_VERSION = "7.0.2"


def _stub_host_collectors(mocker) -> None:
    """Stub the three host collectors that read the machine the tests run on.

    Each shells out or reads ``/etc/os-release``, so a test exercising only the
    assembly must not reach them.
    """
    mocker.patch(f"{MODULE}.collect_os_version", return_value=None)
    mocker.patch(f"{MODULE}.collect_installed_packages", return_value=None)
    mocker.patch(f"{MODULE}.collect_host_config", return_value={})


def _write_config(tmp_path: Path, config: dict) -> Path:
    """Write a payload config file and return its path."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


class TestHostFacts:
    """Test best-effort host fact collection."""

    def test_collect_os_version(self, tmp_path, monkeypatch):
        """os_version is parsed from /etc/os-release PRETTY_NAME."""
        os_release = tmp_path / "os-release"
        os_release.write_text(OS_RELEASE)
        monkeypatch.setattr(payload_module, "OS_RELEASE_PATH", os_release)
        assert collect_os_version() == "Ubuntu 22.04.3 LTS"

    def test_collect_os_version_missing_returns_none(self, tmp_path, monkeypatch):
        """A missing os-release file yields None, not an error."""
        monkeypatch.setattr(
            payload_module, "OS_RELEASE_PATH", tmp_path / "does-not-exist"
        )
        assert collect_os_version() is None

    def test_collect_os_version_falls_back_to_name_and_version(
        self, tmp_path, monkeypatch
    ):
        """Without PRETTY_NAME, NAME + VERSION_ID are joined."""
        os_release = tmp_path / "os-release"
        os_release.write_text('NAME="Debian GNU/Linux"\nVERSION_ID="12"\n')
        monkeypatch.setattr(payload_module, "OS_RELEASE_PATH", os_release)
        assert collect_os_version() == "Debian GNU/Linux 12"

    def test_collect_os_version_name_only(self, tmp_path, monkeypatch):
        """With only NAME present, the bare name is returned."""
        os_release = tmp_path / "os-release"
        os_release.write_text('NAME="Alpine Linux"\n')
        monkeypatch.setattr(payload_module, "OS_RELEASE_PATH", os_release)
        assert collect_os_version() == "Alpine Linux"

    def test_collect_installed_packages_dpkg(self, mocker):
        """Packages are parsed from the dpkg-query output when rpm is absent."""
        mocker.patch(
            f"{MODULE}.shutil.which",
            side_effect=lambda b: "/usr/bin/dpkg-query" if b == "dpkg-query" else None,
        )
        mocker.patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="libc6\t2.36-9\nbash\t5.2\n"),
        )
        packages = collect_installed_packages()
        assert packages is not None
        assert {"name": "libc6", "version": "2.36-9"} in packages
        assert {"name": "bash", "version": "5.2"} in packages

    def test_collect_installed_packages_nonzero_returncode_returns_none(self, mocker):
        """A failing package-manager query yields None, not partial output."""
        mocker.patch(
            f"{MODULE}.shutil.which",
            side_effect=lambda b: "/usr/bin/rpm" if b == "rpm" else None,
        )
        mocker.patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        )
        assert collect_installed_packages() is None

    def test_collect_installed_packages_rpm(self, mocker):
        """Packages are parsed from the rpm query output."""
        mocker.patch(
            f"{MODULE}.shutil.which",
            side_effect=lambda b: "/usr/bin/rpm" if b == "rpm" else None,
        )
        mocker.patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(
                returncode=0, stdout="glibc\t2.35-1\nopenssl\t3.0.2\n"
            ),
        )
        packages = collect_installed_packages()
        assert packages is not None
        assert {"name": "glibc", "version": "2.35-1"} in packages
        assert {"name": "openssl", "version": "3.0.2"} in packages

    def test_collect_installed_packages_no_manager_returns_none(self, mocker):
        """With neither rpm nor dpkg available, packages collection is skipped."""
        mocker.patch(f"{MODULE}.shutil.which", return_value=None)
        assert collect_installed_packages() is None

    def test_collect_host_facts_assembles_collected_at(self, mocker):
        """Host facts carry collected_at plus whatever was gathered."""
        mocker.patch(f"{MODULE}.collect_os_version", return_value="Ubuntu 22.04")
        mocker.patch(
            f"{MODULE}.collect_installed_packages",
            return_value=[{"name": "glibc", "version": "2.35"}],
        )
        mocker.patch(f"{MODULE}.collect_host_config", return_value={"kernel": "5.15.0"})
        facts = collect_host_facts()
        assert facts["os_version"] == "Ubuntu 22.04"
        assert facts["installed_packages"] == [{"name": "glibc", "version": "2.35"}]
        assert facts["config"] == {"kernel": "5.15.0"}
        assert "collected_at" in facts

    def test_host_fields_match_the_observation_field_set(self):
        """Pin the payload's literal field tuple against the model's derived set.

        The payload runs stdlib-only on the target node before any install, so it
        cannot import the model to derive these names and carries a hand-kept copy
        instead. A field added on one side only would publish a fact the observation
        drops, or drop one it publishes; nothing else compares the two.
        """
        assert set(payload_module.HOST_FIELDS) == HOST_OBSERVATION_FIELD_NAMES

    def test_collect_can_elevate_root_without_sudo(self, mocker):
        """Report a uid-0 task user as able even with no sudo binary on PATH."""
        mocker.patch(f"{MODULE}.os.geteuid", return_value=0)
        mocker.patch(f"{MODULE}.shutil.which", return_value=None)
        assert collect_can_elevate() is True

    def test_collect_can_elevate_root_with_sudo(self, mocker):
        """Report a uid-0 task user with sudo present as able."""
        mocker.patch(f"{MODULE}.os.geteuid", return_value=0)
        mocker.patch(f"{MODULE}.shutil.which", return_value="/usr/bin/sudo")
        assert collect_can_elevate() is True

    def test_collect_can_elevate_non_root_with_sudo(self, mocker):
        """Report a non-root task user with sudo on PATH as able."""
        mocker.patch(f"{MODULE}.os.geteuid", return_value=1000)
        mocker.patch(f"{MODULE}.shutil.which", return_value="/usr/bin/sudo")
        assert collect_can_elevate() is True

    def test_collect_can_elevate_non_root_without_sudo(self, mocker):
        """Report a non-root task user with no sudo as unable.

        This is the single row of the launch check's truth table that aborts with
        ``EXTENSIONS_UNLAUNCHABLE``, and the only one the collector reports ``False`` for.
        """
        mocker.patch(f"{MODULE}.os.geteuid", return_value=1000)
        mocker.patch(f"{MODULE}.shutil.which", return_value=None)
        assert collect_can_elevate() is False

    def test_collect_can_elevate_without_posix_uids_returns_none(self, mocker):
        """Leave the fact unmeasured on a platform with no geteuid to answer."""
        mocker.patch(f"{MODULE}.os", spec=[])
        assert collect_can_elevate() is None

    def test_collect_host_facts_keeps_a_measured_false(self, mocker):
        """Keep a measured ``False``, which a truthiness guard here would drop."""
        _stub_host_collectors(mocker)
        mocker.patch(f"{MODULE}.collect_can_elevate", return_value=False)
        assert collect_host_facts()["can_elevate"] is False

    def test_collect_host_facts_omits_an_unmeasured_can_elevate(self, mocker):
        """Omit the key entirely when the capability could not be measured.

        ``None`` is an absence rather than a value, so it must read downstream as
        never-observed instead of as a measured inability.
        """
        _stub_host_collectors(mocker)
        mocker.patch(f"{MODULE}.collect_can_elevate", return_value=None)
        facts = collect_host_facts()
        assert "collected_at" in facts
        assert "can_elevate" not in facts


class TestParseHostPort:
    """Test host/port splitting across IPv4, hostname, and IPv6 forms."""

    def test_host_and_port(self):
        """A plain ``host:port`` splits on the colon."""
        assert parse_host_port(MYSQL_ADDRESS) == ("10.0.0.5", 3306)

    def test_host_only_uses_default_port(self):
        """A bare host falls back to the default port."""
        assert parse_host_port("db.internal", default_port=5432) == (
            "db.internal",
            5432,
        )

    def test_non_numeric_port_falls_back(self):
        """A non-numeric port yields the cleaned host and the default port."""
        assert parse_host_port("host:abc", default_port=3306) == ("host", 3306)

    def test_bracketed_ipv6_with_port(self):
        """A bracketed IPv6 literal keeps the address and parses the trailing port."""
        assert parse_host_port("[2001:db8::1]:5432") == ("2001:db8::1", 5432)

    def test_bracketed_ipv6_without_port(self):
        """A bracketed IPv6 literal with no port uses the default port."""
        assert parse_host_port("[2001:db8::1]", default_port=27017) == (
            "2001:db8::1",
            27017,
        )

    def test_bare_ipv6_not_mis_split(self):
        """A bare IPv6 literal is not split on its final colon."""
        assert parse_host_port("2001:db8::1", default_port=3306) == (
            "2001:db8::1",
            3306,
        )

    def test_trailing_colon_empty_port_falls_back(self):
        """A trailing colon (empty port) yields the cleaned host and default port."""
        assert parse_host_port("host:", default_port=3306) == ("host", 3306)

    def test_empty_string_uses_default_port(self):
        """An empty address yields an empty host and the default port, not a crash."""
        assert parse_host_port("", default_port=5432) == ("", 5432)


class TestMysqlCreds:
    """Test MySQL credential resolution from ``~/.mylogin.cnf``."""

    @staticmethod
    def _inject_myloginpath(monkeypatch, content: str) -> None:
        """Install a fake ``myloginpath`` module returning ``content``."""
        fake = MagicMock()
        fake.read.return_value = content
        monkeypatch.setitem(sys.modules, "myloginpath", fake)

    def test_garbled_port_section_skipped(self, monkeypatch):
        """A section with a non-numeric port is skipped, not fatal."""
        self._inject_myloginpath(
            monkeypatch,
            "[bad]\nuser=wrong\nhost=10.0.0.5\nport=not-a-number\n"
            "[client]\nuser=root\npassword=secret\nhost=10.0.0.5\nport=3306\n",
        )
        creds = _mysql_creds(MYSQL_ADDRESS)
        assert creds["user"] == "root"

    def test_garbled_port_no_client_returns_empty(self, monkeypatch):
        """A garbled-port section with no client fallback yields empty creds."""
        self._inject_myloginpath(
            monkeypatch,
            "[bad]\nuser=wrong\nhost=10.0.0.5\nport=not-a-number\n",
        )
        assert _mysql_creds(MYSQL_ADDRESS) == {}

    def test_matching_host_and_port_section_wins(self, monkeypatch):
        """The section whose host and port match the target address is selected."""
        self._inject_myloginpath(
            monkeypatch,
            "[other]\nuser=nope\nhost=10.0.0.9\nport=3306\n"
            "[prod]\nuser=appuser\npassword=pw\nhost=10.0.0.5\nport=3306\n"
            "[client]\nuser=root\npassword=secret\nhost=10.0.0.5\nport=3306\n",
        )
        creds = _mysql_creds(MYSQL_ADDRESS)
        assert creds["user"] == "appuser"
        assert creds["password"] == "pw"

    def test_no_explicit_port_matches_default(self, monkeypatch):
        """An address without a port matches a section on the engine default port."""
        self._inject_myloginpath(
            monkeypatch,
            "[prod]\nuser=appuser\npassword=pw\nhost=10.0.0.5\nport=3306\n",
        )
        assert _mysql_creds("10.0.0.5")["user"] == "appuser"

    def test_non_string_content_returns_empty(self, monkeypatch):
        """A login-path file that does not decode to a string yields empty creds."""
        self._inject_myloginpath(monkeypatch, content=None)
        assert _mysql_creds(MYSQL_ADDRESS) == {}


class TestServiceVersion:
    """Test per-engine version dispatch and error handling."""

    def test_dispatch_mysql(self, mocker):
        """A MySQL service routes to the MySQL collector."""
        mocker.patch(f"{MODULE}._collect_mysql_version", return_value=MYSQL_VERSION)
        assert (
            collect_service_version(MYSQL_ADDRESS, ServiceType.MYSQL) == MYSQL_VERSION
        )

    def test_dispatch_postgresql(self, mocker):
        """A PostgreSQL service routes to the PostgreSQL collector."""
        mocker.patch(f"{MODULE}._collect_postgresql_version", return_value=PG_VERSION)
        assert collect_service_version(PG_ADDRESS, ServiceType.POSTGRESQL) == PG_VERSION

    def test_dispatch_mongodb(self, mocker):
        """A MongoDB service routes to the MongoDB collector."""
        mocker.patch(f"{MODULE}._collect_mongodb_version", return_value=MONGO_VERSION)
        assert (
            collect_service_version(MONGO_ADDRESS, ServiceType.MONGODB) == MONGO_VERSION
        )

    def test_unknown_type_returns_none(self):
        """An unsupported service type is not probed."""
        assert collect_service_version("10.0.0.5:6033", "proxysql") is None

    def test_collector_error_is_swallowed(self, mocker):
        """A failing collector (bad creds, unreachable) yields None, not a crash."""
        mocker.patch(
            f"{MODULE}._collect_mysql_version", side_effect=RuntimeError("no creds")
        )
        assert collect_service_version(MYSQL_ADDRESS, "mysql") is None

    def test_missing_type_with_address_returns_none(self):
        """A service entry with an address but no type is not probed."""
        assert collect_service_version(MYSQL_ADDRESS, None) is None

    def test_collector_error_redacts_uri_credentials(self, mocker, caplog):
        """A collector error echoing a connection URI never logs the password."""
        uri = "mongodb://admin:s3cret@host:27017/db"
        mocker.patch(
            f"{MODULE}._collect_mongodb_version",
            side_effect=RuntimeError(f"auth failed for {uri}"),
        )
        # The payload logger does not propagate to root, so attach caplog's handler
        # directly to capture its records.
        payload_module.logger.addHandler(caplog.handler)
        try:
            assert collect_service_version(MONGO_ADDRESS, "mongodb") is None
        finally:
            payload_module.logger.removeHandler(caplog.handler)
        assert "s3cret" not in caplog.text
        assert "mongodb://***@host:27017/db" in caplog.text


class TestRedactSecrets:
    """Test the connection-URI credential scrubber."""

    def test_masks_user_and_password(self):
        """A ``user:password@`` userinfo is replaced with ``***@``."""
        result = _redact_secrets("conn mongodb://admin:s3cret@host:27017 failed")
        assert "s3cret" not in result
        assert "admin" not in result
        assert "mongodb://***@host:27017" in result

    def test_masks_user_only(self):
        """A ``user@`` userinfo with no password is still masked."""
        assert _redact_secrets("postgresql://bob@db:5432") == "postgresql://***@db:5432"

    def test_masks_srv_scheme(self):
        """The ``mongodb+srv://`` scheme is recognised and masked."""
        assert (
            _redact_secrets("mongodb+srv://u:p@cluster/")
            == "mongodb+srv://***@cluster/"
        )

    def test_leaves_bare_address_unchanged(self):
        """A bare ``host:port`` with no scheme or userinfo is untouched."""
        assert (
            _redact_secrets("cannot reach 10.0.0.5:27017")
            == "cannot reach 10.0.0.5:27017"
        )

    def test_leaves_plain_text_unchanged(self):
        """Text with no URI is returned verbatim."""
        assert _redact_secrets("connection timed out") == "connection timed out"

    def test_masks_password_query_param(self):
        """A ``password=`` query-string value is masked."""
        result = _redact_secrets("mongodb://host/?password=s3cret&authSource=admin")
        assert "s3cret" not in result
        assert "password=***" in result
        assert "authSource=admin" in result

    def test_masks_auth_mechanism_properties(self):
        """``authMechanismProperties`` (may carry an AWS session token) is masked."""
        result = _redact_secrets(
            "mongodb://host/?authMechanismProperties=AWS_SESSION_TOKEN:tok123"
        )
        assert "tok123" not in result
        assert "authMechanismProperties=***" in result

    def test_query_secret_masking_is_case_insensitive(self):
        """The query-param key match ignores case."""
        result = _redact_secrets("mongodb://host/?PassWord=s3cret")
        assert "s3cret" not in result


class TestMysqlVersionCollector:
    """Test the MySQL version collector's connect/query/row handling."""

    @staticmethod
    def _inject(monkeypatch, fetchone_result):
        """Install a fake ``pymysql`` whose cursor returns ``fetchone_result``."""
        # Empty creds: a login-path file that parses to no sections.
        myloginpath = MagicMock()
        myloginpath.read.return_value = ""
        monkeypatch.setitem(sys.modules, "myloginpath", myloginpath)
        pymysql = MagicMock()
        connection = pymysql.connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = fetchone_result
        monkeypatch.setitem(sys.modules, "pymysql", pymysql)
        return pymysql

    def test_version_extracted_from_row(self, monkeypatch):
        """The first column of the result row is returned as the version."""
        self._inject(monkeypatch, (MYSQL_VERSION,))
        assert _collect_mysql_version(MYSQL_ADDRESS) == MYSQL_VERSION

    def test_empty_result_returns_none(self, monkeypatch):
        """A query returning no row yields None rather than indexing into nothing."""
        self._inject(monkeypatch, None)
        assert _collect_mysql_version(MYSQL_ADDRESS) is None


class TestPostgresqlVersionCollector:
    """Test the PostgreSQL version collector's connect/query/row handling."""

    @staticmethod
    def _inject(monkeypatch, fetchone_result):
        """Install a fake ``psycopg`` whose cursor returns ``fetchone_result``."""
        psycopg = MagicMock()
        connection = psycopg.connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = fetchone_result
        monkeypatch.setitem(sys.modules, "psycopg", psycopg)
        return psycopg

    def test_version_extracted_from_row(self, monkeypatch):
        """The first column of the ``SHOW server_version`` row is returned."""
        self._inject(monkeypatch, (PG_VERSION,))
        assert _collect_postgresql_version(PG_ADDRESS) == PG_VERSION

    def test_empty_result_returns_none(self, monkeypatch):
        """A query returning no row yields None, not an index error."""
        self._inject(monkeypatch, None)
        assert _collect_postgresql_version(PG_ADDRESS) is None

    def test_pguser_env_passed_to_conninfo(self, monkeypatch):
        """A ``PGUSER`` in the environment is forwarded as the connection user."""
        psycopg = self._inject(monkeypatch, (PG_VERSION,))
        monkeypatch.setenv("PGUSER", "inspector")
        assert _collect_postgresql_version(PG_ADDRESS) == PG_VERSION
        assert psycopg.connect.call_args.kwargs["user"] == "inspector"


class TestMongodbVersionCollector:
    """Test the MongoDB version collector's client/buildInfo handling."""

    @staticmethod
    def _inject(monkeypatch, build_info):
        """Install a fake ``pymongo`` whose ``buildInfo`` returns ``build_info``."""
        monkeypatch.delenv("EXTENSIONS_MONGO_URI", raising=False)
        monkeypatch.delenv("MONGO_URI", raising=False)
        pymongo = MagicMock()
        client = pymongo.MongoClient.return_value
        client.admin.command.return_value = build_info
        monkeypatch.setitem(sys.modules, "pymongo", pymongo)
        return pymongo

    def test_version_extracted_from_build_info(self, monkeypatch):
        """The ``version`` field of ``buildInfo`` is returned."""
        self._inject(monkeypatch, {"version": MONGO_VERSION})
        assert _collect_mongodb_version(MONGO_ADDRESS) == MONGO_VERSION

    def test_missing_version_field_returns_none(self, monkeypatch):
        """A ``buildInfo`` lacking a version field yields None."""
        self._inject(monkeypatch, {})
        assert _collect_mongodb_version(MONGO_ADDRESS) is None

    def test_connects_via_host_and_port_without_uri(self, monkeypatch):
        """With no URI env var, the client is built from the parsed host and port."""
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        _collect_mongodb_version(MONGO_ADDRESS)
        kwargs = pymongo.MongoClient.call_args.kwargs
        assert (kwargs["host"], kwargs["port"]) == ("10.0.0.5", 27017)

    def test_uri_supplies_creds_but_connects_to_requested_address(self, monkeypatch):
        """A non-SRV URI supplies credentials; host/port come from the requested address.

        The URI's own host (``uri-host``) must be ignored so each service is probed at its
        own address rather than every service hitting the URI's host.
        """
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        monkeypatch.setenv(
            "EXTENSIONS_MONGO_URI",
            "mongodb://admin:s3cret@uri-host:27017/?authSource=admin",
        )
        _collect_mongodb_version(MONGO_ADDRESS)
        kwargs = pymongo.MongoClient.call_args.kwargs
        assert (kwargs["host"], kwargs["port"]) == ("10.0.0.5", 27017)
        assert kwargs["username"] == "admin"
        assert kwargs["password"] == "s3cret"
        assert kwargs["authSource"] == "admin"
        assert pymongo.MongoClient.call_args.args == ()

    def test_uri_credentials_are_percent_decoded(self, monkeypatch):
        """Percent-encoded credentials in the URI are decoded before connecting."""
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        monkeypatch.setenv(
            "EXTENSIONS_MONGO_URI", "mongodb://user%40corp:p%40ss@uri-host/"
        )
        _collect_mongodb_version(MONGO_ADDRESS)
        kwargs = pymongo.MongoClient.call_args.kwargs
        assert kwargs["username"] == "user@corp"
        assert kwargs["password"] == "p@ss"

    def test_srv_uri_used_verbatim_when_host_matches(self, monkeypatch):
        """An SRV URI whose host matches the requested address is used verbatim."""
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        srv_uri = "mongodb+srv://u:p@cluster.example.net/"
        monkeypatch.setenv("EXTENSIONS_MONGO_URI", srv_uri)
        _collect_mongodb_version("cluster.example.net:27017")
        assert pymongo.MongoClient.call_args.args[0] == srv_uri
        assert pymongo.MongoClient.call_args.kwargs.get("host") is None

    def test_srv_uri_host_mismatch_connects_plainly(self, monkeypatch):
        """An SRV URI for a different host is ignored: connect plainly, no URI creds.

        The URI targets a different cluster, so reusing it would misattribute that
        cluster's version to this service.
        """
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        monkeypatch.setenv(
            "EXTENSIONS_MONGO_URI", "mongodb+srv://u:p@cluster.example.net/"
        )
        _collect_mongodb_version("other-host:27017")
        kwargs = pymongo.MongoClient.call_args.kwargs
        assert (kwargs["host"], kwargs["port"]) == ("other-host", 27017)
        assert "username" not in kwargs
        assert "password" not in kwargs
        assert pymongo.MongoClient.call_args.args == ()

    def test_tls_allow_invalid_certificates_not_forwarded(self, monkeypatch):
        """A ``tlsAllowInvalidCertificates`` URI option is dropped, not forwarded.

        Forwarding it would disable certificate verification. Safe TLS options
        (``tlsCAFile``) are still carried over.
        """
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        monkeypatch.setenv(
            "EXTENSIONS_MONGO_URI",
            "mongodb://admin:s3cret@uri-host:27017/"
            "?tlsAllowInvalidCertificates=true&tlsCAFile=/etc/ca.pem",
        )
        _collect_mongodb_version(MONGO_ADDRESS)
        kwargs = pymongo.MongoClient.call_args.kwargs
        assert "tlsAllowInvalidCertificates" not in kwargs
        assert kwargs["tlsCAFile"] == "/etc/ca.pem"

    def test_multi_service_probes_each_own_address(self, monkeypatch):
        """With one shared URI, distinct services are each probed at their own port."""
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        monkeypatch.setenv(
            "EXTENSIONS_MONGO_URI", "mongodb://admin:s3cret@uri-host:27017/"
        )
        _collect_mongodb_version("10.0.0.5:27017")
        _collect_mongodb_version("10.0.0.5:27018")
        ports = [call.kwargs["port"] for call in pymongo.MongoClient.call_args_list]
        assert ports == [27017, 27018]

    def test_non_auth_uri_options_are_not_forwarded(self, monkeypatch):
        """Non-auth URI options (e.g. readPreferenceTags) are not splatted as kwargs."""
        pymongo = self._inject(monkeypatch, {"version": MONGO_VERSION})
        monkeypatch.setenv(
            "EXTENSIONS_MONGO_URI",
            "mongodb://admin:s3cret@uri-host:27017/?readPreferenceTags=dc:ny&authSource=admin",
        )
        _collect_mongodb_version(MONGO_ADDRESS)
        kwargs = pymongo.MongoClient.call_args.kwargs
        assert "readPreferenceTags" not in kwargs
        assert kwargs["authSource"] == "admin"


class TestMain:
    """Test the payload entrypoint orchestration and JSON output."""

    def test_main_collect_host_and_services(
        self, tmp_path, monkeypatch, mocker, capsys
    ):
        """collect_host=True emits host facts plus per-service versions."""
        config = _write_config(
            tmp_path,
            {
                "collect_host": True,
                "services": [{"address": MYSQL_ADDRESS, "type": "mysql"}],
            },
        )
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        mocker.patch(
            f"{MODULE}.collect_host_facts",
            return_value={"os_version": "Ubuntu 22.04", "collected_at": "t"},
        )
        mocker.patch(f"{MODULE}.collect_service_version", return_value=MYSQL_VERSION)

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["host"]["os_version"] == "Ubuntu 22.04"
        assert out["services"][MYSQL_ADDRESS]["db_engine_version"] == MYSQL_VERSION
        assert "collected_at" in out["services"][MYSQL_ADDRESS]

    def test_main_collect_host_false(self, tmp_path, monkeypatch, mocker, capsys):
        """collect_host=False yields a null host but still probes services."""
        config = _write_config(
            tmp_path,
            {
                "collect_host": False,
                "services": [{"address": PG_ADDRESS, "type": "postgresql"}],
            },
        )
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        host = mocker.patch(f"{MODULE}.collect_host_facts")
        mocker.patch(f"{MODULE}.collect_service_version", return_value=PG_VERSION)

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["host"] is None
        host.assert_not_called()
        assert out["services"][PG_ADDRESS]["db_engine_version"] == PG_VERSION

    def test_main_service_failure_omitted(self, tmp_path, monkeypatch, mocker, capsys):
        """A service with no obtainable version is omitted; main still exits cleanly."""
        config = _write_config(
            tmp_path,
            {
                "collect_host": False,
                "services": [
                    {"address": MYSQL_ADDRESS, "type": "mysql"},
                    {"address": "10.0.0.9:5432", "type": "postgresql"},
                ],
            },
        )
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        mocker.patch(
            f"{MODULE}.collect_service_version",
            side_effect=lambda address, _type: (
                MYSQL_VERSION if address == MYSQL_ADDRESS else None
            ),
        )

        main()

        out = json.loads(capsys.readouterr().out)
        assert MYSQL_ADDRESS in out["services"]
        assert "10.0.0.9:5432" not in out["services"]

    def test_main_empty_host_facts_yields_null_host(
        self, tmp_path, monkeypatch, mocker, capsys
    ):
        """Host facts with no usable field collapse to a null host (no half-snapshot)."""
        config = _write_config(tmp_path, {"collect_host": True, "services": []})
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        mocker.patch(f"{MODULE}.collect_host_facts", return_value={"collected_at": "t"})

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["host"] is None

    def test_main_publishes_a_capability_only_false(
        self, tmp_path, monkeypatch, mocker, capsys
    ):
        """Emit a host document for a run whose only fact is a measured ``False``.

        The publication envelope is a second admission point, downstream of the
        assembly one: were either to test truthiness, this run would emit
        ``"host": null`` and the measurement would never reach the syncer.
        """
        config = _write_config(tmp_path, {"collect_host": True, "services": []})
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        mocker.patch(
            f"{MODULE}.collect_host_facts",
            return_value={"collected_at": "t", "can_elevate": False},
        )

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["host"]["can_elevate"] is False

    def test_main_publishes_a_capability_only_true(
        self, tmp_path, monkeypatch, mocker, capsys
    ):
        """Publish a capability-only ``True`` through the same envelope."""
        config = _write_config(tmp_path, {"collect_host": True, "services": []})
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        mocker.patch(
            f"{MODULE}.collect_host_facts",
            return_value={"collected_at": "t", "can_elevate": True},
        )

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["host"]["can_elevate"] is True

    def test_main_missing_config_file_yields_empty(self, tmp_path, monkeypatch, capsys):
        """A missing config file degrades to an empty snapshot, not a crash."""
        missing = tmp_path / "does-not-exist.json"
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(missing)])

        main()

        out = json.loads(capsys.readouterr().out)
        assert out == {"host": None, "services": {}}

    def test_main_invalid_json_yields_empty(self, tmp_path, monkeypatch, capsys):
        """Unparseable config JSON degrades to an empty snapshot, not a crash."""
        config = tmp_path / "config.json"
        config.write_text("{not valid json")
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])

        main()

        out = json.loads(capsys.readouterr().out)
        assert out == {"host": None, "services": {}}

    def test_main_non_object_config_yields_empty(self, tmp_path, monkeypatch, capsys):
        """A config that is valid JSON but not an object degrades to empty."""
        config = tmp_path / "config.json"
        config.write_text("[1, 2, 3]")
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])

        main()

        out = json.loads(capsys.readouterr().out)
        assert out == {"host": None, "services": {}}

    def test_main_services_wrong_shape_skipped(
        self, tmp_path, monkeypatch, mocker, capsys
    ):
        """A non-list ``services`` value is ignored rather than iterated."""
        config = _write_config(tmp_path, {"collect_host": False, "services": "nope"})
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        collector = mocker.patch(f"{MODULE}.collect_service_version")

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["services"] == {}
        collector.assert_not_called()

    def test_main_non_dict_service_entry_skipped(
        self, tmp_path, monkeypatch, mocker, capsys
    ):
        """Non-dict entries in ``services`` are skipped; valid ones still probed."""
        config = _write_config(
            tmp_path,
            {
                "collect_host": False,
                "services": [
                    "garbage",
                    None,
                    {"address": MYSQL_ADDRESS, "type": "mysql"},
                ],
            },
        )
        monkeypatch.setattr("sys.argv", ["payload", "-c", str(config)])
        mocker.patch(f"{MODULE}.collect_service_version", return_value=MYSQL_VERSION)

        main()

        out = json.loads(capsys.readouterr().out)
        assert out["services"][MYSQL_ADDRESS]["db_engine_version"] == MYSQL_VERSION
        assert len(out["services"]) == 1
