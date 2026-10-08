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

"""Run the MongoDB shell snippets against stub ``mongosh`` and ``mongostat`` clients.

Every snippet that talks to MongoDB through a shell keeps the script on
``--eval`` and hands the credentials over stdin as three lines: authentication
database, user and password. Piping the script itself into ``mongosh`` would run
it as a REPL session, which echoes prompts into the report, carries on
unauthenticated after a failed ``db.auth()`` and exits 0.

The stubs log each call's argv and stdin and exit with ``STUB_RC_AUTH`` when
stdin carried anything and ``STUB_RC`` otherwise, so a test can stage a failed
authentication or an unreachable server without a real MongoDB.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from app.extensions.snippets.config import snippets_settings
from app.extensions.snippets.models.snippet import (
    BaseSnippet,
    EXECUTOR_HOSTS_INPUT_NAME,
)

# What ``getopt --test`` exits with when it is the GNU one.
GNU_GETOPT_TEST_STATUS = 4
MONGO_AUTH_FAILED = 3
MONGO_CONNECT_FAILED = 1

GROUP_A = tuple(
    f"mongodb_{name}_check.sh"
    for name in (
        "chunks_imbalance",
        "connections",
        "high_cache_miss",
        "high_cursor_count",
        "high_flow_control",
        "high_heap_usage",
        "high_write_conflict",
        "inconsistent_indexes",
        "opcounters",
        "oplog_window",
        "read_write_queue",
        "repl_lag",
        "repl_state",
        "tls_expiry",
        "wt_checkpoint_time",
        "wt_dirty_ratio",
        "wt_ticket_use",
    )
)
GROUP_B = (
    "mongodb_cmdline_opts.sh",
    "mongodb_current_op.sh",
    "mongodb_log_components.sh",
    "mongodb_profiling_status.sh",
    "mongodb_server_build_info.sh",
    "mongodb_server_status.sh",
    "mongodb_sharding_status.sh",
    "mongodb_blocked_writes_check.sh",
    "mongodb_replica_set_collect.sh",
    "mongodb_query_tuning.sh",
)
ALL_MONGO_SHELL_SNIPPETS = GROUP_A + GROUP_B

REFERENCE_SNIPPET = "mongodb_server_status.sh"
CONNECTION_PARAMETERS = ("host", "port", "user", "password", "auth-database")

USER = "u"
PASSWORD = "P@ss w0rd"
CREDENTIALS = ("--user", USER, "--password", PASSWORD)
DECODED_CREDENTIALS = ("admin", USER, PASSWORD)

UNAUTHENTICATED_OPTIONS = (
    "--host",
    "localhost",
    "--port",
    "27017",
    "--quiet",
    "--eval",
)
DEFAULT_ENDPOINT = "localhost:27017"
ENDPOINTS = {"mongodb_query_tuning.sh": "127.0.0.1:27017"}

# Tools the collectors run when installed; each is replaced by a no-op so a run
# finishes in seconds and touches nothing on the host.
NOOP_TOOLS = (
    "pidstat",
    "top",
    "vmstat",
    "iostat",
    "mpstat",
    "sar",
    "netstat",
    "dmesg",
    "journalctl",
    "sysctl",
    "pt-summary",
    "ps",
    "sleep",
    "free",
    "df",
)

STUB_CLIENT = """\
#!/usr/bin/env bash
call_dir=$(mktemp -d "{calls}/$(date +%s%N)-$(basename "$0").XXXXXX")
printf '%s\\0' "$@" > "$call_dir/argv"
for arg in "$@"; do
    if [[ $arg == --config=* ]]; then
        cat "${{arg#--config=}}" > "$call_dir/config"
    fi
done
cat > "$call_dir/stdin"
if [[ -s "$call_dir/stdin" ]]; then
    exit "${{STUB_RC_AUTH:-0}}"
fi
exit "${{STUB_RC:-0}}"
"""


def _has_runtime() -> bool:
    """Return whether ``bash`` and the GNU ``getopt`` the scripts parse options with exist.

    :return: ``True`` when both are on ``PATH`` and ``getopt --test`` exits 4.
    """
    if any(shutil.which(tool) is None for tool in ("bash", "getopt")):
        return False
    probe = subprocess.run(["getopt", "--test"], capture_output=True, check=False)
    return probe.returncode == GNU_GETOPT_TEST_STATUS


pytestmark = pytest.mark.skipif(
    not _has_runtime(), reason="requires bash with GNU getopt"
)


@dataclass(frozen=True, slots=True)
class ShellCall:
    """Hold one logged invocation of a stub client.

    :param program: The stub's name, ``mongosh`` or ``mongostat``.
    :param argv: The arguments it was called with.
    :param stdin: Everything it read on stdin.
    :param config: The contents of the file passed as ``--config=``, if any.
    """

    program: str
    argv: tuple[str, ...]
    stdin: str
    config: str | None

    @property
    def credentials(self) -> tuple[str, ...]:
        """Return the stdin lines decoded from hex, one value per line.

        :return: The authentication database, user and password, in order.
        """
        return tuple(
            bytes.fromhex(line).decode("utf-8") for line in self.stdin.split("\n")[:-1]
        )


@dataclass
class MongoHarness:
    """Stage stub clients and no-op tools, then run a shipped MongoDB snippet.

    :param root: The per-test directory holding every staged artefact.
    """

    root: Path

    def __post_init__(self) -> None:
        """Create the stub ``bin`` and the call log directory."""
        self.bin_dir.mkdir()
        self.calls_dir.mkdir()
        for client in ("mongosh", "mongostat"):
            self._write_stub(client, STUB_CLIENT.format(calls=self.calls_dir))
        for tool in NOOP_TOOLS:
            self._write_stub(tool, "#!/usr/bin/env bash\nexit 0\n")

    @property
    def bin_dir(self) -> Path:
        """Return the directory put first on ``PATH``."""
        return self.root / "bin"

    @property
    def calls_dir(self) -> Path:
        """Return the directory each stub call logs into."""
        return self.root / "calls"

    @property
    def dest(self) -> Path:
        """Return the destination the collectors write into."""
        return self.root / "out"

    def _write_stub(self, name: str, body: str) -> None:
        """Write an executable stub onto the staged ``PATH``.

        :param name: The command the stub answers for.
        :param body: The script's full text.
        """
        stub = self.bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)

    def calls(self, program: str = "mongosh") -> list[ShellCall]:
        """Return the logged calls of one stub, oldest first.

        :param program: The stub whose calls to return.
        :return: Each call's argv and stdin.
        """
        return [
            ShellCall(
                program=program,
                argv=tuple(
                    (call_dir / "argv").read_text(encoding="utf-8").split("\0")[:-1]
                ),
                stdin=(call_dir / "stdin").read_text(encoding="utf-8"),
                config=(
                    (call_dir / "config").read_text(encoding="utf-8")
                    if (call_dir / "config").exists()
                    else None
                ),
            )
            for call_dir in sorted(self.calls_dir.glob(f"*-{program}.*"))
        ]

    def base_args(self, filename: str) -> tuple[str, ...]:
        """Return the options a snippet needs before it contacts MongoDB at all.

        :param filename: The snippet under test.
        :return: Its required options, empty for the stdout-only snippets.
        """
        if filename == "mongodb_blocked_writes_check.sh":
            return ("--dest", str(self.dest), "--iterations=1", "--sleep=1")
        if filename == "mongodb_replica_set_collect.sh":
            return ("--dest", str(self.dest))
        if filename == "mongodb_query_tuning.sh":
            return (
                "--execute",
                "--database",
                "d",
                "--collection",
                "c",
                "--dest",
                str(self.dest),
            )
        return ()

    def run(
        self,
        filename: str,
        *args: str,
        stub_rc: int = 0,
        stub_rc_auth: int = 0,
        locale: str = "C",
    ) -> subprocess.CompletedProcess[str]:
        """Run a shipped snippet with the stubs first on ``PATH``.

        :param filename: The snippet to run.
        :param args: Its options.
        :param stub_rc: What a client call without stdin exits with.
        :param stub_rc_auth: What a client call that received stdin exits with.
        :param locale: The ``LC_ALL`` the script runs under.
        :return: The completed process.
        """
        return subprocess.run(
            ["bash", str(snippets_settings.SNIPPETS_DIR / filename), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
                "STUB_RC": str(stub_rc),
                "STUB_RC_AUTH": str(stub_rc_auth),
                "LC_ALL": locale,
            },
            cwd=self.root,
            timeout=60,
            check=False,
        )


@pytest.fixture
def harness(tmp_path: Path) -> MongoHarness:
    """Provide a harness staged in a fresh directory.

    :param tmp_path: The per-test temporary directory.
    :return: The harness.
    """
    return MongoHarness(root=tmp_path)


async def _connection_parameters(filename: str) -> dict[str, dict[str, object]]:
    """Return a snippet's connection parameters, each dumped to a dict.

    :param filename: The snippet whose parameters to load.
    :return: The dumped parameters keyed by name, only those in
        :data:`CONNECTION_PARAMETERS`.
    """
    snippet = await BaseSnippet.from_path(filename, update_meta=True)
    assert snippet.validated_parameters.errors == []
    return {
        param.name: param.model_dump()
        for param in snippet.validated_parameters.parameters
        if param.name in CONNECTION_PARAMETERS
    }


class TestParameters:
    """Give the alert-linked checks the connection parameters the reference declares."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("filename", GROUP_A)
    async def test_declares_connection_parameters(self, filename):
        """Declare the five connection parameters exactly as the reference does."""
        expected = await _connection_parameters(REFERENCE_SNIPPET)

        assert await _connection_parameters(filename) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("filename", GROUP_A)
    async def test_args_reach_script_as_long_options(self, filename):
        """Dispatch every connection parameter as a long option."""
        snippet = await BaseSnippet.from_path(filename, update_meta=True)

        args = (
            snippet.get_execution_model()
            .model_validate(
                {
                    EXECUTOR_HOSTS_INPUT_NAME: "node1",
                    "host": "db1",
                    "port": 27018,
                    "user": USER,
                    "password": "pw",
                    "auth-database": "admin",
                }
            )
            .to_args_string()
        )

        for option in ("--host", "--port", "--user", "--password", "--auth-database"):
            assert option in args


class TestCredentialTransport:
    """Hand the credentials to the client over stdin, never on its command line."""

    @pytest.mark.parametrize("filename", ALL_MONGO_SHELL_SNIPPETS)
    def test_password_never_on_shell_argv(self, harness, filename):
        """Keep the password off every client argv, in plain text or encoded."""
        harness.run(filename, *harness.base_args(filename), *CREDENTIALS)

        calls = harness.calls("mongosh") + harness.calls("mongostat")
        assert calls
        assert all(PASSWORD not in arg for call in calls for arg in call.argv)
        assert all(
            PASSWORD.encode().hex() not in arg for call in calls for arg in call.argv
        )

    @pytest.mark.parametrize("filename", ALL_MONGO_SHELL_SNIPPETS)
    def test_every_shell_call_carries_credentials(self, harness, filename):
        """Authenticate every shell call, keeping the script on ``--eval``."""
        harness.run(filename, *harness.base_args(filename), *CREDENTIALS)

        calls = harness.calls()
        assert calls
        assert len(calls) > 1
        assert all(call.credentials == DECODED_CREDENTIALS for call in calls)
        assert all("--eval" in call.argv for call in calls)

    @pytest.mark.parametrize(
        "filename",
        [
            "mongodb_connections_check.sh",
            "mongodb_server_status.sh",
            "mongodb_query_tuning.sh",
        ],
    )
    @pytest.mark.parametrize(
        ("user", "password"),
        [
            ("wei rd", "a'b\\c d\"e\n2"),
            ("line\nbreak", "plain"),
            ("cr\ruser", "trailing newline\n"),
            ("ünï€😀", "p\tä\r\x01ss"),
        ],
        ids=["quotes", "user-lf", "user-cr", "unicode-control"],
    )
    def test_special_characters_reach_stdin_verbatim(
        self, harness, filename, user, password
    ):
        """Encode every value so line breaks and control characters survive intact."""
        harness.run(
            filename,
            *harness.base_args(filename),
            "--user",
            user,
            "--password",
            password,
        )

        calls = harness.calls()
        assert calls
        assert all(call.credentials == ("admin", user, password) for call in calls)
        assert all(call.stdin.count("\n") == len(DECODED_CREDENTIALS) for call in calls)


class TestAuthenticationFailure:
    """Fail loudly on wrong credentials, and only on wrong credentials."""

    @pytest.mark.parametrize("filename", ALL_MONGO_SHELL_SNIPPETS)
    def test_wrong_credentials_fail_explicitly(self, harness, filename):
        """Stop with an explicit error after the single pre-check call."""
        result = harness.run(
            filename,
            *harness.base_args(filename),
            *CREDENTIALS,
            stub_rc_auth=MONGO_AUTH_FAILED,
        )

        assert result.returncode == 1
        assert "authentication failed" in result.stderr
        assert f"'{USER}'" in result.stderr
        assert ENDPOINTS.get(filename, DEFAULT_ENDPOINT) in result.stderr
        assert PASSWORD not in result.stderr
        assert "*********" not in result.stdout
        assert len(harness.calls()) == 1
        assert not (harness.dest / "getParameter.out").exists()
        assert not (harness.dest / "dbStats.out").exists()

    @pytest.mark.parametrize("filename", GROUP_A)
    def test_connection_failure_is_not_reported_as_auth(self, harness, filename):
        """Fall through to each section's own fallback when the server is unreachable."""
        unauthenticated = harness.run(filename, stub_rc=MONGO_CONNECT_FAILED)
        section_calls = len(harness.calls())

        result = harness.run(filename, *CREDENTIALS, stub_rc_auth=MONGO_CONNECT_FAILED)

        assert result.returncode == 0
        assert "authentication failed" not in result.stderr
        assert result.stdout == unauthenticated.stdout
        assert len(harness.calls()) == 2 * section_calls + 1

    @pytest.mark.parametrize("filename", ALL_MONGO_SHELL_SNIPPETS)
    @pytest.mark.parametrize(
        "partial",
        [("--user", USER), ("--password", PASSWORD)],
        ids=["user", "password"],
    )
    def test_partial_credentials_rejected(self, harness, filename, partial):
        """Reject a user without a password, or the reverse, before any call."""
        result = harness.run(filename, *harness.base_args(filename), *partial)

        assert result.returncode == 1
        assert "must be provided together" in result.stderr
        assert harness.calls() == []


class TestUnauthenticatedInvocation:
    """Leave a run without credentials as it was, apart from the endpoint."""

    @pytest.mark.parametrize("filename", GROUP_A)
    def test_no_credentials_invocation_unchanged(self, harness, filename):
        """Call the shell with the endpoint and ``--eval`` only, writing no stdin."""
        harness.run(filename)

        calls = harness.calls()
        assert calls
        for call in calls:
            *options, script = call.argv
            assert tuple(options) == UNAUTHENTICATED_OPTIONS
            assert "__creds" not in script
            assert call.stdin == ""

    @pytest.mark.parametrize("filename", ALL_MONGO_SHELL_SNIPPETS)
    def test_no_credentials_script_stays_on_eval(self, harness, filename):
        """Pass the script on ``--eval`` and nothing on stdin, so no REPL runs."""
        harness.run(filename, *harness.base_args(filename))

        calls = harness.calls()
        assert calls
        for call in calls:
            assert call.argv[-2] == "--eval"
            assert "__creds" not in call.argv[-1]
            assert call.stdin == ""


class TestCollectors:
    """Pin the collector-specific behaviour around the shared client call."""

    def test_mongostat_password_in_config_file(self, harness):
        """Give ``mongostat`` the user on argv and the password in a ``--config`` file."""
        filename = "mongodb_blocked_writes_check.sh"

        harness.run(filename, *harness.base_args(filename), *CREDENTIALS)

        calls = harness.calls("mongostat")
        assert len(calls) == 1
        argv = calls[0].argv
        assert PASSWORD not in argv
        user_at = argv.index("-u")
        assert argv[user_at : user_at + 4] == (
            "-u",
            USER,
            "--authenticationDatabase",
            "admin",
        )
        assert "--authenticationMechanism" not in argv
        assert calls[0].config is not None
        assert calls[0].config.isascii()
        assert yaml.safe_load(calls[0].config) == {"password": PASSWORD}
        assert calls[0].stdin == ""

    def test_mongostat_line_break_password_uses_scram_sha1(self, harness):
        """Escape the password for YAML and pick the only mechanism that can hold it."""
        filename = "mongodb_blocked_writes_check.sh"

        harness.run(
            filename,
            *harness.base_args(filename),
            "--user",
            USER,
            "--password",
            'q"b\\s\tt\nn\r',
        )

        calls = harness.calls("mongostat")
        assert len(calls) == 1
        argv = calls[0].argv
        mechanism_at = argv.index("--authenticationMechanism")
        assert argv[mechanism_at + 1] == "SCRAM-SHA-1"
        assert yaml.safe_load(calls[0].config) == {"password": 'q"b\\s\tt\nn\r'}

    @pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
    @pytest.mark.parametrize(
        "password",
        [
            f"a{chr(0x2028)}b{chr(0x2029)}c",
            "c1\u0085\u009fend",
            'mixed "q" \\ \x01\x7f é€😀 : # end',
        ],
        ids=["line-separators", "c1-controls", "mixed"],
    )
    def test_mongostat_config_round_trips_password(self, harness, locale, password):
        """Write a ``--config`` whose YAML decodes back to the exact password."""
        filename = "mongodb_blocked_writes_check.sh"

        harness.run(
            filename,
            *harness.base_args(filename),
            "--user",
            USER,
            "--password",
            password,
            locale=locale,
        )

        calls = harness.calls("mongostat")
        assert len(calls) == 1
        assert calls[0].config is not None
        assert yaml.safe_load(calls[0].config) == {"password": password}

    def test_blocked_writes_shell_args_have_no_user(self, harness):
        """Keep ``-u`` off the shell, which would prompt and eat the credentials."""
        filename = "mongodb_blocked_writes_check.sh"

        harness.run(filename, *harness.base_args(filename), *CREDENTIALS)

        calls = harness.calls()
        assert calls
        assert all("-u" not in call.argv for call in calls)

    def test_query_tuning_archives_after_failed_explain(self, harness):
        """Write the archive even when a shell call fails under ``set -e``."""
        filename = "mongodb_query_tuning.sh"

        result = harness.run(
            filename,
            *harness.base_args(filename),
            "--query",
            "find({})",
            stub_rc=MONGO_CONNECT_FAILED,
            stub_rc_auth=MONGO_CONNECT_FAILED,
        )

        assert result.returncode == 0
        assert harness.dest.with_name(f"{harness.dest.name}.tar.gz").exists()

    def test_query_tuning_without_execute_contacts_no_shell(self, harness):
        """Contact no shell, not even for the pre-check, without ``--execute``."""
        result = harness.run(
            "mongodb_query_tuning.sh",
            "--database",
            "d",
            "--collection",
            "c",
            "--dest",
            str(harness.dest),
            *CREDENTIALS,
        )

        assert result.returncode == 0
        assert harness.calls() == []


# Evaluate a snippet's own assignments up to the auth prefix, then print it.
EXTRACT_AUTH_JS = (
    'eval "$(sed -n "/^MONGO_AUTH_FAILED=/,/^if (!__ok)/p" "$1")"; '
    'printf "%s" "$MONGO_AUTH_JS"'
)

# Run the prefix against a mocked shell: ``quit`` throws a marker, ``db.auth``
# behaves as AUTH_MODE says, and stdin is served by ``require('fs')`` (mongosh)
# or ``cat()`` (legacy mongo).
NODE_AUTH_RUNNER = """
const vm = require('vm');
const env = process.env;
const seen = {};
const modes = {
    ok: () => ({ ok: 1 }),
    rejected: () => { throw Object.assign(new Error('Authentication failed.'), { code: 18 }); },
    legacy_false: () => 0,
    network: () => { throw Object.assign(new Error('connection reset'), { name: 'MongoNetworkError' }); },
};
const context = {
    quit: (code) => { throw { quit: code }; },
    db: {
        getSiblingDB: (name) => ({
            auth: (user, pwd) => { Object.assign(seen, { name, user, pwd }); return modes[env.AUTH_MODE](); },
        }),
    },
};
if (env.SHELL_KIND === 'mongosh') {
    context.require = () => ({ readFileSync: () => env.CREDS });
} else {
    context.cat = () => env.CREDS;
}
const out = { quit: null, threw: null, seen };
try {
    vm.runInNewContext(env.AUTH_JS, context);
} catch (e) {
    if (e && 'quit' in e) { out.quit = e.quit; } else { out.threw = e.name; }
}
process.stdout.write(JSON.stringify(out));
"""


def _encode_credentials(*values: str) -> str:
    """Return the stdin payload the snippets write: one hex-encoded value per line.

    :param values: The authentication database, user and password.
    :return: The newline-terminated hex lines.
    """
    return "".join(f"{value.encode().hex()}\n" for value in values)


def _run_auth_prefix(
    auth_mode: str, shell_kind: str, credentials: tuple[str, str, str]
) -> dict[str, object]:
    """Run the shipped auth prefix under Node against a mocked MongoDB shell.

    :param auth_mode: How the mocked ``db.auth`` answers.
    :param shell_kind: ``mongosh`` or ``mongo``, selecting how stdin is read.
    :param credentials: The authentication database, user and password.
    :return: Whether the prefix quit, and with what code, or what it threw, plus
        the values ``db.auth`` received.
    """
    extracted = subprocess.run(
        [
            "bash",
            "-c",
            EXTRACT_AUTH_JS,
            "_",
            str(snippets_settings.SNIPPETS_DIR / REFERENCE_SNIPPET),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = subprocess.run(
        ["node", "-e", NODE_AUTH_RUNNER],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AUTH_JS": extracted.stdout,
            "AUTH_MODE": auth_mode,
            "SHELL_KIND": shell_kind,
            "CREDS": _encode_credentials(*credentials),
        },
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="requires node")
class TestAuthPrefix:
    """Run the JavaScript auth prefix itself, which the stub clients never execute."""

    @pytest.mark.parametrize("shell_kind", ["mongosh", "mongo"])
    def test_decodes_credentials_exactly(self, shell_kind):
        """Hand ``db.auth`` the exact values, line breaks included."""
        credentials = ("ad\nmin", "line\nbreak", "p\r\nw é😀\n")

        out = _run_auth_prefix("ok", shell_kind, credentials)

        assert out["quit"] is None
        assert out["threw"] is None
        assert out["seen"] == dict(
            zip(("name", "user", "pwd"), credentials, strict=True)
        )

    @pytest.mark.parametrize(
        ("auth_mode", "shell_kind"),
        [("rejected", "mongosh"), ("legacy_false", "mongo")],
    )
    def test_rejection_quits_with_auth_failed(self, auth_mode, shell_kind):
        """Exit with the authentication sentinel when the server rejects the pair."""
        out = _run_auth_prefix(auth_mode, shell_kind, DECODED_CREDENTIALS)

        assert out["quit"] == MONGO_AUTH_FAILED

    def test_connection_error_propagates(self):
        """Let a non-authentication error escape, so it is not reported as bad credentials."""
        out = _run_auth_prefix("network", "mongosh", DECODED_CREDENTIALS)

        assert out["quit"] is None
        assert out["threw"] == "MongoNetworkError"
