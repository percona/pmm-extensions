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
"""Test the pcs-collect-pmm-valkey.py payload script."""

import datetime
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

SCRIPT = (
    Path(__file__).parents[5]
    / "app/extensions/apps/dipper/payloads/pcs-collect-pmm-valkey.py"
)

FIXED_FROM = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
FIXED_TO = datetime.datetime(2024, 1, 3, tzinfo=datetime.UTC)
FIXED_DATE = FIXED_FROM.strftime("%Y-%m-%d")


def load_payload(argv):
    """Load the hyphenated payload as an importable module with ``argv`` baked in.

    The payload parses ``sys.argv`` and derives its module-level globals at import
    time, so the desired arguments must be in place before the module body runs.
    The filename contains hyphens and cannot be imported normally, hence the
    manual loader.

    :param argv: the ``sys.argv`` list to expose while the module executes.
    :return: the freshly loaded payload module.
    """
    spec = importlib.util.spec_from_file_location("pcs_collect_pmm_valkey", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "argv", argv):
        spec.loader.exec_module(module)
    return module


def _make_response(payload=None, content=b"", status_code=200):
    """Return a fake ``requests`` response with a canned JSON body."""
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.json.return_value = payload
    return response


def make_requests_fake(routes):
    """Return a ``requests`` stand-in that answers PMM URLs from ``routes``.

    The real ``codes`` and ``exceptions`` attributes are preserved so the
    collector's status-code comparison and ``except RequestException`` clauses
    keep working against the mock.

    :param routes: mapping of URL substring to the JSON payload the matching
        ``get``/``post`` call should return. A value may instead be a
        ``(payload, status_code)`` tuple to drive the non-200 branches.
    :return: a ``MagicMock`` shaped like the ``requests`` module.
    """
    fake = MagicMock(name="requests")
    fake.codes = requests.codes
    fake.exceptions = requests.exceptions

    def _respond(url, **_kwargs):
        for fragment, value in routes.items():
            if fragment in url:
                payload, status_code = (
                    value if isinstance(value, tuple) else (value, 200)
                )
                return _make_response(payload=payload, status_code=status_code)
        return _make_response(payload={}, status_code=404)

    fake.get.side_effect = _respond
    fake.post.side_effect = _respond
    return fake


PMM_ROUTES = {
    "/graph/api/health": {"database": "ok"},
    "/v1/version": {"version": "3.0.0"},
    "/graph/api/dashboards/uid/": {"dashboard": {"panels": []}, "meta": {"slug": "s"}},
}


def _frontmatter() -> str:
    """Return the YAML frontmatter block from the collector script."""
    content = SCRIPT.read_text()
    frontmatter_end = content.index("# ---", content.index("# ---") + 1)
    return content[:frontmatter_end]


def _parameter_block(frontmatter: str, parameter_name: str) -> str:
    """Return the frontmatter slice for a single parameter declaration."""
    marker = f"name: {parameter_name}"
    start = frontmatter.index(marker)
    next_parameter_index = frontmatter.find("name: ", start + len(marker))
    if next_parameter_index == -1:
        return frontmatter[start:]
    return frontmatter[start:next_parameter_index]


class TestPcsCollectPmmValkeyHelp:
    """Exercise the --help output of pcs-collect-pmm-valkey.py."""

    @pytest.fixture(scope="class")
    def help_output(self):
        """Run the script with --help and capture the output.

        Pin ``COLUMNS`` so argparse wraps the help text at a fixed width: under
        ``pytest -n`` (no tty) the inherited terminal width can be narrow enough
        to split ``self-signed`` across lines and break a substring assertion.
        """
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "COLUMNS": "80"},
        )

    def test_help_exits_zero(self, help_output):
        """Verify --help exits cleanly."""
        assert help_output.returncode == 0

    def test_help_shows_valkey_flags(self, help_output):
        """Verify the Valkey-specific and added flags appear in --help output."""
        stdout = help_output.stdout
        assert "--insecure" in stdout
        assert "--skip-valkey" in stdout
        assert "--sentinel" in stdout
        assert "--cluster" in stdout

    def test_insecure_flag_description_in_help(self, help_output):
        """Verify the --insecure help text mentions self-signed certs."""
        assert "self-signed" in help_output.stdout


class TestPcsCollectPmmValkeyVerifySsl:
    """Verify TLS verification stays controllable and is never hard-disabled."""

    def test_no_hardcoded_verify_false(self):
        """Verify no requests call site uses a literal verify=False."""
        assert "verify=False" not in SCRIPT.read_text()


class TestPcsCollectPmmValkeyFrontmatter:
    """Exercise the YAML frontmatter declarations."""

    def test_insecure_parameter_is_bool(self):
        """Verify the insecure parameter is a boolean form field."""
        assert "type: bool" in _parameter_block(_frontmatter(), "insecure")

    def test_apikey_marked_hidden(self):
        """Verify the apikey parameter declares ``hidden: true`` in the frontmatter."""
        assert "hidden: true" in _parameter_block(_frontmatter(), "apikey")

    def test_pmmserver_is_positional_and_not_hidden(self):
        """Verify pmmserver is positional and remains visible in the form."""
        block = _parameter_block(_frontmatter(), "pmmserver")
        assert "positional: true" in block
        assert "hidden: true" not in block

    def test_sibling_parameters_are_not_hidden(self):
        """Verify visible sibling params do not declare ``hidden``."""
        frontmatter = _frontmatter()
        for name in ("node", "service", "list"):
            assert "hidden: true" not in _parameter_block(frontmatter, name)

    def test_sentinel_marked_hidden(self):
        """Verify sentinel collection is hidden (always on, not user-controllable)."""
        assert "hidden: true" in _parameter_block(_frontmatter(), "sentinel")


class TestPcsCollectPmmValkeyParses:
    """Verify the hand-authored metadata header parses without validation errors."""

    @pytest.mark.asyncio
    async def test_metadata_header_parses_without_errors(self):
        """Assert the parsed metadata header yields no validation errors."""
        from app.extensions.snippets.models.snippet import BaseSnippet  # noqa: PLC0415

        meta = await BaseSnippet.get_meta_by_path(SCRIPT)
        snippet = BaseSnippet(
            filename="pcs-collect-pmm-valkey.py",
            size=1,
            md5_digest="a" * 32,
            meta=meta,
        )
        validated = snippet.validated_parameters
        assert validated.errors == []
        by_name = {p.name: p for p in validated.parameters}
        assert by_name["apikey"].hidden is True
        assert by_name["pmmserver"].hidden is False


class TestPcsCollectPmmValkeySuffixStrip:
    """Verify main() resolves the output directory from the -valkey-stripped hostname."""

    @pytest.mark.parametrize(
        ("service", "expected_host"),
        [
            ("server1-valkey", "server1"),
            ("server1", "server1"),
            ("my-valkey-node", "my-valkey-node"),
            ("-valkey", ""),
        ],
    )
    def test_output_directory_name(self, service, expected_host, tmp_path, monkeypatch):
        """Verify only a trailing ``-valkey`` is stripped when naming the output dir."""
        module = load_payload(
            [
                "pcs-collect-pmm-valkey.py",
                "https://u:p@localhost",
                "--node",
                "node1",
                # ``=`` form so a service value starting with "-" is not parsed as a flag.
                f"--service={service}",
                "--skip-valkey",
                "--skip-os",
                "--notar",
            ],
        )
        monkeypatch.setattr(module, "get_pmm_version", lambda: "3")
        monkeypatch.setattr(
            module, "get_graph_window", lambda *_args: (FIXED_FROM, FIXED_TO)
        )
        monkeypatch.chdir(tmp_path)

        assert module.main() == 0
        assert (tmp_path / f"{expected_host}_pmm_{FIXED_DATE}").is_dir()


class TestPcsCollectPmmValkeyModeDispatch:
    """Verify main() selects the right dashboards for each collection mode."""

    def _load(self, extra_args, monkeypatch, tmp_path):
        module = load_payload(
            [
                "pcs-collect-pmm-valkey.py",
                "https://u:p@localhost",
                "--node",
                "node1",
                "--service",
                "server1-valkey",
                "--notar",
                *extra_args,
            ],
        )
        monkeypatch.setattr(module, "get_pmm_version", lambda: "3")
        monkeypatch.setattr(
            module, "get_graph_window", lambda *_args: (FIXED_FROM, FIXED_TO)
        )
        monkeypatch.setattr(module, "render_dashboard", Mock())
        monkeypatch.setattr(module, "list_services", Mock())
        monkeypatch.chdir(tmp_path)
        return module

    @staticmethod
    def _rendered_uids(module):
        return [call.args[1] for call in module.render_dashboard.call_args_list]

    def test_sentinel_is_default_mode(self, monkeypatch, tmp_path):
        """Verify that with no HA flag, Sentinel graphs render, then the OS node summary."""
        module = self._load([], monkeypatch, tmp_path)
        assert module.main() == 0
        assert self._rendered_uids(module) == ["VCFX6PdHk", "node-instance-summary"]

    def test_cluster_overrides_sentinel_dashboard(self, monkeypatch, tmp_path):
        """Verify --cluster renders the cluster dashboard (it overwrites the sentinel one)."""
        module = self._load(["--cluster"], monkeypatch, tmp_path)
        assert module.main() == 0
        # Both blocks run additively and share locals, so the cluster block
        # overwrites the sentinel selection; a single Valkey dashboard renders,
        # followed by the OS one.
        assert self._rendered_uids(module) == ["zddr6B2Hk", "node-instance-summary"]

    def test_skip_os_drops_node_dashboard(self, monkeypatch, tmp_path):
        """Verify --skip-os renders only the Valkey dashboard."""
        module = self._load(["--skip-os"], monkeypatch, tmp_path)
        assert module.main() == 0
        assert self._rendered_uids(module) == ["VCFX6PdHk"]

    def test_list_short_circuits_before_rendering(self, monkeypatch, tmp_path):
        """Verify --list lists services and returns before any dashboard render."""
        module = self._load(["--list"], monkeypatch, tmp_path)
        assert module.main() == 0
        module.list_services.assert_called_once_with("3")
        module.render_dashboard.assert_not_called()


class TestPcsCollectPmmValkeyBoundary:
    """Verify main() runs end-to-end against a mocked requests boundary (no live PMM)."""

    def test_runs_without_live_pmm(self, monkeypatch, tmp_path):
        """Verify health, version, and dashboard fetches all resolve through the mock."""
        module = load_payload(
            [
                "pcs-collect-pmm-valkey.py",
                "https://u:p@localhost",
                "--node",
                "node1",
                "--service",
                "server1-valkey",
                "--skip-os",
                "--notar",
            ],
        )
        fake = make_requests_fake(PMM_ROUTES)
        monkeypatch.setattr(module, "requests", fake)
        monkeypatch.chdir(tmp_path)

        assert module.main() == 0
        assert fake.get.called
        # TLS verification flows from --insecure (default: verification enabled).
        assert all(call.kwargs["verify"] is True for call in fake.get.call_args_list)

    def test_unreachable_pmm_raises(self, monkeypatch, tmp_path):
        """Verify a connection failure at the boundary propagates out of main()."""
        module = load_payload(
            [
                "pcs-collect-pmm-valkey.py",
                "https://u:p@localhost",
                "--node",
                "node1",
                "--service",
                "server1-valkey",
                "--notar",
            ],
        )
        fake = make_requests_fake({})
        fake.get.side_effect = requests.exceptions.ConnectionError
        monkeypatch.setattr(module, "requests", fake)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(requests.exceptions.RequestException):
            module.main()


def _argv(*extra, server="https://u:p@localhost"):
    """Build a payload argv with a valid server plus any ``extra`` flags."""
    return ["pcs-collect-pmm-valkey.py", server, *extra]


class TestPcsCollectPmmValkeyArgValidation:
    """Verify import-time argument validation rejects unusable invocations."""

    def test_missing_node_and_service_without_list_exits(self):
        """Verify omitting --node/--service outside --list mode aborts at import."""
        with pytest.raises(SystemExit):
            load_payload(_argv())

    def test_missing_credentials_without_apikey_exits(self):
        """Verify a URL with neither embedded creds nor --apikey aborts."""
        with pytest.raises(SystemExit):
            load_payload(
                _argv("--node", "n", "--service", "s", server="https://localhost"),
            )

    def test_missing_protocol_exits(self):
        """Verify a URL without an http(s) scheme aborts."""
        with pytest.raises(SystemExit):
            load_payload(
                _argv(
                    "--apikey",
                    "K",
                    "--node",
                    "n",
                    "--service",
                    "s",
                    server="ftp://localhost",
                ),
            )

    def test_trailing_slash_is_stripped(self):
        """Verify a trailing slash is removed from the PMM base URL."""
        module = load_payload(
            _argv("--node", "n", "--service", "s", server="https://u:p@localhost/"),
        )
        assert module.PMMSERVER == "https://u:p@localhost"

    def test_apikey_replaces_url_credentials(self):
        """Verify --apikey satisfies the credential requirement without URL creds."""
        module = load_payload(
            _argv(
                "--apikey",
                "K",
                "--node",
                "n",
                "--service",
                "s",
                server="https://localhost",
            ),
        )
        assert module.APIKEY == "K"


class TestPcsCollectPmmValkeyGraphWindow:
    """Verify get_graph_window parses timestamp arguments and defaults to the last 24h."""

    def _window(self):
        return load_payload(
            _argv("--node", "n", "--service", "s"),
        ).get_graph_window

    def test_defaults_to_last_24_hours(self):
        """Verify a 24h window ending now (UTC) when no bounds are given."""
        start, end = self._window()(None, None)
        assert end.tzinfo is datetime.UTC
        assert (end - start) == datetime.timedelta(seconds=86400)

    def test_parses_explicit_bounds(self):
        """Verify explicit start/end strings parse to UTC datetimes."""
        start, end = self._window()("2024-01-02T03:04:05", "2024-01-03T06:07:08")
        assert start == datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        assert end == datetime.datetime(2024, 1, 3, 6, 7, 8, tzinfo=datetime.UTC)

    def test_invalid_start_raises(self):
        """Verify an unparseable start timestamp raises ValueError."""
        with pytest.raises(ValueError, match="does not match"):
            self._window()("not-a-date", None)

    def test_invalid_end_raises(self):
        """Verify an unparseable end timestamp raises ValueError."""
        with pytest.raises(ValueError, match="does not match"):
            self._window()("2024-01-02T03:04:05", "not-a-date")


class TestPcsCollectPmmValkeyHelpers:
    """Verify the pure header/filename helpers behave as the renderer expects."""

    def test_get_valid_filename_sanitizes(self):
        """Verify spaces become underscores and illegal characters are dropped."""
        module = load_payload(_argv("--node", "n", "--service", "s"))
        assert module.get_valid_filename("CPU Usage (%)") == "CPU_Usage_"
        assert module.get_valid_filename("a-b.c_d") == "a-b.c_d"

    def test_build_header_includes_bearer_when_apikey_set(self):
        """Verify an API key is sent as a Bearer authorization header."""
        module = load_payload(
            _argv(
                "--apikey",
                "K",
                "--node",
                "n",
                "--service",
                "s",
                server="https://localhost",
            ),
        )
        assert module.build_header()["Authorization"] == "Bearer K"

    def test_build_header_omits_auth_without_apikey(self):
        """Verify no authorization header is sent when auth rides in the URL."""
        module = load_payload(_argv("--node", "n", "--service", "s"))
        assert "Authorization" not in module.build_header()


class TestPcsCollectPmmValkeyVersionHealth:
    """Verify get_pmm_version guards each PMM health/version failure mode."""

    def _module(self, monkeypatch, routes):
        module = load_payload(_argv("--node", "n", "--service", "s"))
        monkeypatch.setattr(module, "requests", make_requests_fake(routes))
        return module

    def test_returns_major_version(self, monkeypatch):
        """Verify only the major version digit is returned on success."""
        module = self._module(monkeypatch, PMM_ROUTES)
        assert module.get_pmm_version() == "3"

    def test_unhealthy_database_raises(self, monkeypatch):
        """Verify a non-ok database health status raises CollectPmmError."""
        routes = {**PMM_ROUTES, "/graph/api/health": {"database": "down"}}
        module = self._module(monkeypatch, routes)
        with pytest.raises(module.CollectPmmError):
            module.get_pmm_version()

    def test_non_ok_health_status_raises(self, monkeypatch):
        """Verify a non-200 health response raises CollectPmmError."""
        routes = {**PMM_ROUTES, "/graph/api/health": ({"database": "ok"}, 500)}
        module = self._module(monkeypatch, routes)
        with pytest.raises(module.CollectPmmError):
            module.get_pmm_version()

    def test_non_ok_version_status_raises(self, monkeypatch):
        """Verify a non-200 version response raises CollectPmmError."""
        routes = {**PMM_ROUTES, "/v1/version": ({"version": "3.0.0"}, 500)}
        module = self._module(monkeypatch, routes)
        with pytest.raises(module.CollectPmmError):
            module.get_pmm_version()


class TestPcsCollectPmmValkeyListServices:
    """Verify list_services fetches nodes/services and dispatches per PMM major version."""

    V3_ROUTES = {
        "/v1/inventory/nodes": {"generic": [{"node_name": "n1", "address": "1.2.3.4"}]},
        "/v1/inventory/services": {"valkey": [{"service_name": "s1"}]},
    }
    V2_ROUTES = {
        "Nodes/List": {"generic": [{"node_name": "n1", "address": "1.2.3.4"}]},
        "Services/List": {"valkey": [{"service_name": "s1"}]},
    }

    def _module(self, monkeypatch, routes):
        module = load_payload(_argv("--list"))
        fake = make_requests_fake(routes)
        monkeypatch.setattr(module, "requests", fake)
        return module, fake

    def test_v3_lists_over_get(self, monkeypatch, capsys):
        """Verify PMM v3 uses GET and prints node and service names."""
        module, fake = self._module(monkeypatch, self.V3_ROUTES)
        module.list_services("3")
        out = capsys.readouterr().out
        assert "n1" in out
        assert "s1" in out
        assert fake.get.called
        assert not fake.post.called

    def test_v2_lists_over_post(self, monkeypatch):
        """Verify PMM v2 fetches the inventory over POST."""
        module, fake = self._module(monkeypatch, self.V2_ROUTES)
        module.list_services("2")
        assert fake.post.called
        assert not fake.get.called

    def test_error_message_raises(self, monkeypatch):
        """Verify an error payload from the inventory API raises CollectPmmError."""
        module, _ = self._module(
            monkeypatch, {"/v1/inventory/nodes": {"message": "boom"}}
        )
        with pytest.raises(module.CollectPmmError):
            module.list_services("3")


class TestPcsCollectPmmValkeyTarball:
    """Verify main() compresses the graph directory unless --notar is given."""

    def test_creates_tarball_by_default(self, monkeypatch, tmp_path):
        """Verify main() shells out to tar when compression is not skipped."""
        module = load_payload(
            _argv(
                "--node",
                "n",
                "--service",
                "server1-valkey",
                "--skip-valkey",
                "--skip-os",
            ),
        )
        monkeypatch.setattr(module, "requests", make_requests_fake(PMM_ROUTES))
        run = Mock()
        monkeypatch.setattr(module.subprocess, "run", run)
        monkeypatch.chdir(tmp_path)

        assert module.main() == 0
        run.assert_called_once()
        tar_argv = run.call_args.args[0]
        assert tar_argv[0] == "/usr/bin/tar"
        assert any(arg.endswith(".tgz") for arg in tar_argv)
