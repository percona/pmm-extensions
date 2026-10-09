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

"""Run the ProxySQL status script against a stub ``mysql`` client.

The script parses two values out of the client's output — the table list it
dumps and the ``admin-datadir`` it reads ``host_priority.conf`` from — and only
prints the rest. A client can write a notice to stderr on a call that otherwise
succeeds, so these tests stage such a notice and check that it reaches neither
parsed value, while a failing call still reports the client's own error and the
display-only dumps keep showing everything the client wrote.

The admin credentials come from the ``--defaults-file``, which the script reads
as ``KEY=value`` text and never runs. These tests also check that each value
shape loads as the shell would read it, and that defaults fill the settings the
file lacks. A value only shell evaluation could complete is ignored with a
warning, nothing written in the file ever executes, and an unreadable file
stops the run before any query.
"""

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.extensions.snippets.config import snippets_settings

SCRIPT = snippets_settings.SNIPPETS_DIR / "proxysql_status.sh"
NOTICE = "mysql: [Warning] OpenSSL 3.0.13 differs from the 3.0.2 it was built against"
MYLOGIN_NOTICE = (
    "mysql: [Warning] Found option without preceding group in /root/.mylogin.cnf"
)
ACCESS_DENIED = "ERROR 1045 (28000): Access denied for user 'admin'@'%'"
PASSWORD = "s3cr3t-admin-pass"
ADMIN_CNF = (
    "PROXYSQL_USERNAME='admin'\n"
    f"PROXYSQL_PASSWORD='{PASSWORD}'\n"
    "PROXYSQL_HOSTNAME='127.0.0.1'\n"
    "PROXYSQL_PORT='6032'\n"
)
DEFAULT_CLIENT = {"user": "", "password": "", "host": "127.0.0.1", "port": "6032"}
ADMIN_CLIENT = {
    "user": "admin",
    "password": PASSWORD,
    "host": "127.0.0.1",
    "port": "6032",
}
# The shape proxysql-admin ships, with values distinct enough to tell apart the
# four keys the script reads from the look-alikes it must ignore, and from the
# defaults.
SHIPPED_CNF = """\
# proxysql admin interface credentials.
export PROXYSQL_DATADIR='/var/lib/proxysql'
export PROXYSQL_USERNAME='proxy-admin'
export PROXYSQL_PASSWORD='proxy-pass'
export PROXYSQL_HOSTNAME='10.0.0.5'
export PROXYSQL_PORT='16032'

# PXC admin credentials for connecting to pxc-cluster-node.
export CLUSTER_USERNAME='cluster-admin'
export CLUSTER_PASSWORD='cluster-pass'
export CLUSTER_HOSTNAME='localhost'
export CLUSTER_PORT='3306'

# proxysql monitoring user.
export MONITOR_USERNAME="monitor"
export MONITOR_PASSWORD="monit0r"

export WRITER_HOSTGROUP_ID='10'
export MODE="singlewrite"
"""
SHIPPED_CLIENT = {
    "user": "proxy-admin",
    "password": "proxy-pass",
    "host": "10.0.0.5",
    "port": "16032",
}
FIELD_OF = {
    "PROXYSQL_USERNAME": "user",
    "PROXYSQL_PASSWORD": "password",
    "PROXYSQL_HOSTNAME": "host",
    "PROXYSQL_PORT": "port",
}
HOST_PRIORITY = "[node1]\nweight=10\n"
TABLE_DUMP = "+----+\n| id |\n+----+\n| 1  |\n+----+\n"
DEFAULT_DATADIR = "/var/lib/proxysql"
# What ``getopt --test`` exits with when it is the GNU one.
GNU_GETOPT_TEST_STATUS = 4

# The stub answers by the first rule whose pattern occurs in the ``-e`` query, so
# the specific listings precede the generic ``SHOW TABLES`` they also contain.
DEFAULT_RULES = (
    ("LIKE 'runtime_%'", "runtime_mysql_servers\n"),
    ("SHOW TABLES FROM stats", "stats_mysql_global\n"),
    ("SHOW TABLES FROM monitor", "mysql_server_ping_log\n"),
    ("SHOW TABLES", "mysql_servers\nmysql_users\n"),
    ("SELECT * FROM ", TABLE_DUMP),
)

STUB_MYSQL = """\
#!/usr/bin/env bash
cat > "{root}/stdin.log"
printf '%s\\n' "$*" >> "{root}/calls.log"
if [[ -f "{root}/hang" ]]; then
    sleep 30
fi
query=""
while (( $# )); do
    if [[ $1 == -e ]]; then
        query=$2
        shift
    fi
    shift
done
if [[ -f "{root}/notice" ]]; then
    cat "{root}/notice" >&2
fi
for rule in "{root}"/rules/*; do
    if [[ $query == *"$(cat "$rule/pattern")"* ]]; then
        cat "$rule/stdout"
        cat "$rule/stderr" >&2
        exit "$(cat "$rule/status")"
    fi
done
echo "stub mysql: unexpected query: $query" >&2
exit 99
"""


def _has_gnu_getopt() -> bool:
    """Return whether ``getopt`` is the GNU one the script hands its options to.

    The script runs ``getopt`` only when ``getopt --test`` exits 4, as the GNU
    one does; elsewhere its own loop reads the options as given.

    :return: ``True`` when ``getopt --test`` exits 4.
    """
    probe = subprocess.run(["getopt", "--test"], capture_output=True, check=False)
    return probe.returncode == GNU_GETOPT_TEST_STATUS


def _has_runtime() -> bool:
    """Return whether the tools the script needs before reaching ``mysql`` exist.

    :return: ``True`` when ``bash`` and ``getopt`` are on ``PATH``.
    """
    return all(shutil.which(tool) for tool in ("bash", "getopt"))


pytestmark = pytest.mark.skipif(not _has_runtime(), reason="requires bash and getopt")


@dataclass
class Rule:
    """Describe how the stub answers a query containing ``pattern``.

    :param pattern: A substring of the ``-e`` query.
    :param stdout: What the stub writes to stdout.
    :param stderr: What the stub writes to stderr after the global notice.
    :param status: The stub's exit status.
    """

    pattern: str
    stdout: str = ""
    stderr: str = ""
    status: int = 0


@dataclass
class ProxysqlHarness:
    """Stage a stub ``mysql``, an admin config and a data directory, then run the script.

    :param root: The per-test directory holding every staged artefact.
    :param notice: What the stub writes to stderr on every call, if anything.
    :param rules: Answers that take precedence over the defaults, first match wins.
    """

    root: Path
    notice: str | None = None
    rules: list[Rule] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Create the stub ``bin``, temp dir, admin config and ``host_priority.conf``."""
        self.bin_dir.mkdir()
        self.tmp_dir.mkdir()
        self.datadir.mkdir()
        (self.datadir / "host_priority.conf").write_text(
            HOST_PRIORITY, encoding="utf-8"
        )
        self.admin_cnf.write_text(ADMIN_CNF, encoding="utf-8")
        stub = self.bin_dir / "mysql"
        stub.write_text(STUB_MYSQL.format(root=self.root), encoding="utf-8")
        stub.chmod(0o755)

    @property
    def bin_dir(self) -> Path:
        """Return the directory put first on ``PATH``."""
        return self.root / "bin"

    @property
    def tmp_dir(self) -> Path:
        """Return the directory the script sees as ``TMPDIR``."""
        return self.root / "tmp"

    @property
    def datadir(self) -> Path:
        """Return the directory the stub reports as ``admin-datadir``."""
        return self.root / "datadir"

    @property
    def admin_cnf(self) -> Path:
        """Return the config file passed as ``--defaults-file``."""
        return self.root / "proxysql-admin.cnf"

    @property
    def calls(self) -> list[str]:
        """Return the argument lines the stub was invoked with, in order."""
        log = self.root / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    @property
    def temp_files(self) -> list[Path]:
        """Return the files the pinned ``mktemp`` created, in order."""
        log = self.root / "mktemp.log"
        if not log.exists():
            return []
        return [Path(line) for line in log.read_text(encoding="utf-8").splitlines()]

    @property
    def credentials(self) -> dict[str, str]:
        """Return the ``[client]`` settings the script last handed the stub on stdin."""
        stanza = (self.root / "stdin.log").read_text(encoding="utf-8").splitlines()
        settings = dict(line.split("=", 1) for line in stanza[1:])
        # Mirrors mysql_client's printf, which quotes the password alone.
        settings["password"] = settings["password"].removeprefix('"').removesuffix('"')
        return settings

    def write_cnf(self, text: str) -> None:
        """Replace the admin config the run reads its credentials from.

        :param text: The file's full content.
        """
        self.admin_cnf.write_text(text, encoding="utf-8")

    def answer(self, rule: Rule) -> None:
        """Override the stub's answer to any query containing the rule's pattern.

        :param rule: The answer to give.
        """
        self.rules.append(rule)

    def _stub_mktemp(self, body: str) -> None:
        """Replace ``mktemp`` for the run.

        BSD ``mktemp`` without a template ignores ``TMPDIR``, so pinning where the
        script's temp files go, or making their creation fail, needs a stub.

        :param body: The shell body to run, appended to a ``bash`` shebang.
        """
        stub = self.bin_dir / "mktemp"
        stub.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)

    def pin_mktemp(self) -> None:
        """Make every ``mktemp`` call create its file under :attr:`tmp_dir`.

        Each created path is logged for :attr:`temp_files`.
        """
        real = shutil.which("mktemp")
        self._stub_mktemp(
            f'path=$("{real}" "{self.tmp_dir}/stderr.XXXXXX") || exit\n'
            f'echo "$path" >> "{self.root}/mktemp.log"\n'
            'echo "$path"'
        )

    def stage_default_datadir(self) -> None:
        """Serve ``host_priority.conf`` from the script's fallback data directory.

        The fallback is an absolute path outside the test's reach, so a ``cat``
        stub answers for that one file and hands every other path to the real one.
        """
        real = shutil.which("cat")
        stub = self.bin_dir / "cat"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'if [[ $1 == "{DEFAULT_DATADIR}/host_priority.conf" ]]; then\n'
            f"    printf '%s' '{HOST_PRIORITY}'\n"
            "    exit 0\n"
            "fi\n"
            f'exec "{real}" "$@"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)

    def break_mktemp(self) -> None:
        """Make ``mktemp`` fail, as it does on a full or read-only temp directory."""
        self._stub_mktemp("echo 'mktemp: No space left on device' >&2\nexit 1")

    def _write_rules(self) -> None:
        """Materialise the overrides ahead of the defaults for the stub to read."""
        rules_dir = self.root / "rules"
        shutil.rmtree(rules_dir, ignore_errors=True)
        defaults = [
            Rule("admin-datadir", f"{self.datadir}\n"),
            *(Rule(pattern, stdout) for pattern, stdout in DEFAULT_RULES),
        ]
        for index, rule in enumerate([*self.rules, *defaults]):
            rule_dir = rules_dir / f"{index:02d}"
            rule_dir.mkdir(parents=True)
            (rule_dir / "pattern").write_text(rule.pattern, encoding="utf-8")
            (rule_dir / "stdout").write_text(rule.stdout, encoding="utf-8")
            (rule_dir / "stderr").write_text(rule.stderr, encoding="utf-8")
            (rule_dir / "status").write_text(str(rule.status), encoding="utf-8")
        notice_file = self.root / "notice"
        if self.notice is None:
            notice_file.unlink(missing_ok=True)
        else:
            notice_file.write_text(f"{self.notice}\n", encoding="utf-8")

    def run(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        """Run the shipped script with the staged config and stub.

        :param args: Options passed after ``--defaults-file``.
        :param env: Variables added to the run's environment.
        :param timeout: Seconds before the run is killed and the test fails.
        :return: The completed process.
        """
        self._write_rules()
        return subprocess.run(
            self._command(args),
            capture_output=True,
            text=True,
            env={**self._env(), **(env or {})},
            cwd=self.root,
            timeout=timeout,
            check=False,
        )

    def spawn(self, *args: str) -> subprocess.Popen[bytes]:
        """Start the shipped script in its own process group, output discarded.

        :param args: Options passed after ``--defaults-file``.
        :return: The running process, whose group a test can signal.
        """
        self._write_rules()
        return subprocess.Popen(
            self._command(args),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._env(),
            cwd=self.root,
            start_new_session=True,
        )

    def _command(self, args: tuple[str, ...]) -> list[str]:
        """Return the command line that runs the script with the staged config.

        :param args: Options passed after ``--defaults-file``.
        :return: The argument vector.
        """
        return ["bash", str(SCRIPT), "--defaults-file", str(self.admin_cnf), *args]

    def _env(self) -> dict[str, str]:
        """Return the environment that puts the stubs first on ``PATH``.

        :return: The environment for the script.
        """
        return {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.tmp_dir),
            "LC_ALL": "C",
        }


@pytest.fixture
def harness(tmp_path: Path) -> ProxysqlHarness:
    """Provide a harness staged in a fresh directory, with no notice and no overrides.

    :param tmp_path: The per-test temporary directory.
    :return: The harness.
    """
    return ProxysqlHarness(tmp_path)


def _getopt_probe_output() -> str:
    """Return what the script's ``getopt --test`` probe prints on this host.

    The script does not silence the probe, and a BSD ``getopt`` echoes ``--``
    where the GNU one prints nothing, so the exact report starts with whichever
    this host produces.

    :return: The probe's standard output.
    """
    return subprocess.run(
        ["getopt", "--test"], capture_output=True, text=True, check=False
    ).stdout


def _dumped_tables(stdout: str) -> list[str]:
    """Return the table names the run opened a dump section for, in order.

    :param stdout: The script's standard output.
    :return: The names between ``***** DUMPING`` and ``*****``.
    """
    prefix, suffix = "***** DUMPING ", " *****"
    return [
        line.removeprefix(prefix).removesuffix(suffix)
        for line in stdout.splitlines()
        if line.startswith(prefix) and line.endswith(suffix)
    ]


def _dump_queries(calls: list[str]) -> list[str]:
    """Return the ``SELECT * FROM`` queries the stub received.

    :param calls: The stub's recorded argument lines.
    :return: The matching lines.
    """
    return [call for call in calls if "SELECT * FROM " in call]


def _section(name: str, body: str) -> str:
    """Return one table dump section exactly as the script prints it.

    :param name: The table name as printed in the header.
    :param body: The dump body.
    :return: The section text, trailing blank line included.
    """
    return f"***** DUMPING {name} *****\n{body}***** END OF DUMPING {name} *****\n\n"


def _database(title: str, sections: str) -> str:
    """Return one database block exactly as the script prints it.

    :param title: The upper-case database name in the banner.
    :param sections: The table sections inside the block.
    :return: The block text, trailing blank line included.
    """
    return (
        f"............ DUMPING {title} DATABASE ............\n"
        f"{sections}"
        f"............ END OF DUMPING {title} DATABASE ............\n\n"
    )


def _full_run(dump: str) -> str:
    """Return the report of a default run whose every table dump prints ``dump``.

    :param dump: The body printed inside each table section.
    :return: The report text after the ``getopt`` probe's output.
    """
    return (
        _database(
            "MAIN",
            _section("mysql_servers", dump) + _section("mysql_users", dump),
        )
        + _database("STATS", _section("stats.stats_mysql_global", dump))
        + _database("MONITOR", _section("monitor.mysql_server_ping_log", dump))
        + "............ DUMPING HOST PRIORITY FILE ............\n"
        + HOST_PRIORITY
        + "............ END OF DUMPING HOST PRIORITY FILE ............\n\n"
        + "............ DUMPING PROXYSQL ADMIN CNF FILE ............\n"
        + ADMIN_CNF
        + "............ END OF DUMPING PROXYSQL ADMIN CNF FILE ............\n\n"
    )


HEALTHY_FULL_RUN = _full_run(TABLE_DUMP)


class TestHealthyHost:
    """Pin the output of a run whose client writes nothing to stderr."""

    def test_default_run_prints_every_section(self, harness):
        """Print every database, file and section exactly once, in order."""
        result = harness.run()

        assert result.returncode == 0, result.stderr
        assert result.stdout == _getopt_probe_output() + HEALTHY_FULL_RUN
        assert result.stderr == ""

    def test_empty_table_list_is_not_a_failure(self, harness):
        """Treat a successful listing with no rows as an empty database, not an error."""
        harness.answer(Rule("SHOW TABLES"))

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert "Could not list" not in result.stdout
        assert _dumped_tables(result.stdout) == []


class TestNoticeOnSuccess:
    """Keep a notice from a successful call out of the values the script parses."""

    @pytest.mark.parametrize(
        ("option", "expected"),
        [
            ("--main", ["mysql_servers", "mysql_users"]),
            ("--runtime", ["runtime_mysql_servers"]),
            ("--stats", ["stats.stats_mysql_global"]),
            ("--monitor", ["monitor.mysql_server_ping_log"]),
        ],
    )
    def test_table_list_excludes_notice(self, harness, option, expected):
        """Dump only the real tables, never a word of the notice."""
        harness.notice = NOTICE

        result = harness.run(option)

        assert result.returncode == 0, result.stderr
        assert _dumped_tables(result.stdout) == expected
        assert "Could not dump" not in result.stdout
        assert len(_dump_queries(harness.calls)) == len(expected)

    def test_datadir_excludes_notice(self, harness):
        """Read ``host_priority.conf`` from the real data directory."""
        harness.notice = NOTICE

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert (
            "............ DUMPING HOST PRIORITY FILE ............\n"
            f"{HOST_PRIORITY}"
            "............ END OF DUMPING HOST PRIORITY FILE ............\n"
        ) in result.stdout
        assert "Could not read admin-datadir" not in result.stdout

    def test_default_run_shows_notice_only_inside_dumps(self, harness):
        """Print the healthy report with the notice in each dump and nowhere else."""
        harness.notice = NOTICE

        result = harness.run()

        assert result.returncode == 0, result.stderr
        assert result.stdout == _getopt_probe_output() + _full_run(
            f"{NOTICE}\n{TABLE_DUMP}"
        )

    def test_empty_datadir_falls_back_despite_notice(self, harness):
        """Read the fallback data directory when the server reports none."""
        harness.notice = NOTICE
        harness.stage_default_datadir()
        harness.answer(Rule("admin-datadir"))

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert (
            "............ DUMPING HOST PRIORITY FILE ............\n"
            f"{HOST_PRIORITY}"
            "............ END OF DUMPING HOST PRIORITY FILE ............\n"
        ) in result.stdout
        assert "Could not read admin-datadir" not in result.stdout

    def test_table_filter_applies_to_real_tables_only(self, harness):
        """Match ``--table`` against the real names alone."""
        harness.notice = NOTICE

        result = harness.run("--main", "--table", "mysql")

        expected = ["mysql_servers", "mysql_users"]
        assert _dumped_tables(result.stdout) == expected
        assert len(_dump_queries(harness.calls)) == len(expected)

    def test_file_output_excludes_notice(self, harness):
        """Apply the same separation when the report goes to a file."""
        harness.notice = NOTICE

        result = harness.run("--main", "--output", "file")

        assert result.returncode == 0, result.stderr
        (report,) = harness.root.glob("proxysql_status_*.log")
        assert _dumped_tables(report.read_text(encoding="utf-8")) == [
            "mysql_servers",
            "mysql_users",
        ]


class TestTableFilter:
    """Accept ``--table`` with its name in either of the usual option forms."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--table", "users"), id="separate"),
            pytest.param(
                ("--table=users",),
                id="attached",
                marks=pytest.mark.skipif(
                    not _has_gnu_getopt(),
                    reason="only GNU getopt splits an attached option value",
                ),
            ),
        ],
    )
    def test_dumps_only_matching_tables(self, harness, args):
        """Dump the tables whose name contains the filter, and only those."""
        result = harness.run("--main", *args)

        assert result.returncode == 0, result.stdout
        assert _dumped_tables(result.stdout) == ["mysql_users"]
        assert len(_dump_queries(harness.calls)) == 1


class TestFailureKeepsClientError:
    """Report the client's own error text when a parsed call fails."""

    @pytest.mark.parametrize(
        ("option", "pattern", "label"),
        [
            ("--main", "SHOW TABLES", "main"),
            ("--stats", "SHOW TABLES FROM stats", "stats"),
            ("--monitor", "SHOW TABLES FROM monitor", "monitor"),
        ],
    )
    def test_table_list_failure_names_the_error(self, harness, option, pattern, label):
        """Name the config file and quote the client's error."""
        harness.answer(Rule(pattern, stderr=f"{ACCESS_DENIED}\n", status=1))

        result = harness.run(option)

        assert result.returncode == 0, result.stderr
        assert (
            f"Could not list the {label} tables from the ProxySQL admin interface "
            f"(check --defaults-file): {ACCESS_DENIED}"
        ) in result.stdout
        assert _dumped_tables(result.stdout) == []

    def test_datadir_failure_names_the_error_and_falls_back(self, harness):
        """Quote the error, then carry on with the default data directory."""
        harness.answer(Rule("admin-datadir", stderr=f"{ACCESS_DENIED}\n", status=1))

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert (
            "Could not read admin-datadir from the ProxySQL admin interface "
            f"(check --defaults-file): {ACCESS_DENIED}"
        ) in result.stdout
        assert "DUMPING PROXYSQL ADMIN CNF FILE" in result.stdout

    def test_failure_keeps_notice_and_error(self, harness):
        """Show everything the client wrote when the call did not succeed."""
        harness.notice = NOTICE
        harness.answer(Rule("SHOW TABLES", stderr=f"{ACCESS_DENIED}\n", status=1))

        result = harness.run("--main")

        assert NOTICE in result.stdout
        assert ACCESS_DENIED in result.stdout
        assert _dump_queries(harness.calls) == []

    def test_failure_quotes_output_then_error(self, harness):
        """Quote what the client printed on stdout, then its error on the next line."""
        harness.answer(Rule("SHOW TABLES", "partial\n", f"{ACCESS_DENIED}\n", 1))

        result = harness.run("--main")

        assert f"(check --defaults-file): partial\n{ACCESS_DENIED}\n" in result.stdout
        assert _dump_queries(harness.calls) == []

    def test_silent_failure_still_reports_the_listing(self, harness):
        """Report the failed listing even when the client printed nothing."""
        harness.answer(Rule("SHOW TABLES", status=1))

        result = harness.run("--main")

        assert (
            "Could not list the main tables from the ProxySQL admin interface "
            "(check --defaults-file): \n"
        ) in result.stdout
        assert _dump_queries(harness.calls) == []

    def test_failure_drops_mylogin_notice(self, harness):
        """Keep filtering the ``mylogin.cnf`` notice out of the reported error."""
        harness.answer(
            Rule("SHOW TABLES", stderr=f"{MYLOGIN_NOTICE}\n{ACCESS_DENIED}\n", status=1)
        )

        result = harness.run("--main")

        assert "mylogin.cnf" not in result.stdout
        assert f"(check --defaults-file): {ACCESS_DENIED}" in result.stdout


class TestDumpShowsBothStreams:
    """Show everything the client wrote in the display-only dumps."""

    def test_successful_dump_prints_notice(self, harness):
        """Print the notice alongside the dump for the reader to see."""
        harness.notice = NOTICE

        result = harness.run("--main", "--table", "users")

        assert _section("mysql_users", f"{NOTICE}\n{TABLE_DUMP}") in result.stdout

    def test_failed_dump_names_the_error(self, harness):
        """Name the config file and quote the client's error for a failed dump."""
        harness.answer(
            Rule("SELECT * FROM mysql_users", stderr=f"{ACCESS_DENIED}\n", status=1)
        )

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert (
            f"Could not dump mysql_users (check --defaults-file): {ACCESS_DENIED}"
        ) in result.stdout
        assert _section("mysql_servers", TABLE_DUMP) in result.stdout


class TestPercentSign:
    """Print a ``%`` from the client once, wherever it appears."""

    def test_dump_keeps_percent(self, harness):
        """Print a dumped value carrying ``%`` as the client wrote it."""
        dump = "+------+\n| hit  |\n+------+\n| 100% |\n+------+\n"
        harness.answer(Rule("SELECT * FROM ", dump))

        result = harness.run("--main", "--table", "users")

        assert _section("mysql_users", dump) in result.stdout

    def test_table_name_keeps_percent(self, harness):
        """Dump a table whose name carries ``%`` under that exact name."""
        harness.answer(Rule("SHOW TABLES", "my%tab\n"))

        result = harness.run("--main")

        assert _dumped_tables(result.stdout) == ["my%tab"]
        (query,) = _dump_queries(harness.calls)
        assert query.endswith("-e SELECT * FROM my%tab")

    @pytest.mark.parametrize(
        ("pattern", "line"),
        [
            ("SHOW TABLES", "Could not list the main tables"),
            ("SELECT * FROM mysql_users", "Could not dump mysql_users"),
        ],
    )
    def test_error_keeps_percent(self, harness, pattern, line):
        """Quote an error carrying ``%`` the same way for listings and dumps."""
        harness.answer(Rule(pattern, stderr=f"{ACCESS_DENIED}\n", status=1))

        result = harness.run("--main")

        (reported,) = [row for row in result.stdout.splitlines() if line in row]
        assert reported.endswith(f"(check --defaults-file): {ACCESS_DENIED}")


class TestTempFiles:
    """Keep the separated error stream from outliving or breaking the run."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (0, ["mysql_servers", "stats.mysql_servers", "monitor.mysql_servers"]),
            (1, []),
        ],
    )
    def test_no_temp_file_left_behind(self, harness, status, expected):
        """Remove the captured stream after both successful and failed calls."""
        harness.pin_mktemp()
        harness.notice = NOTICE
        harness.answer(
            Rule("SHOW TABLES", "mysql_servers\n", f"{ACCESS_DENIED}\n", status)
        )

        result = harness.run()

        assert result.returncode == 0, result.stderr
        assert _dumped_tables(result.stdout) == expected
        assert harness.temp_files
        assert not any(path.exists() for path in harness.temp_files)

    def test_failing_mktemp_still_lists_tables(self, harness):
        """Fall back to the merged stream rather than losing the listing."""
        harness.break_mktemp()

        result = harness.run("--main")

        assert _dumped_tables(result.stdout) == ["mysql_servers", "mysql_users"]

    def test_failing_mktemp_still_dumps_real_tables_beside_notice(self, harness):
        """Dump the real tables when the fallback merges the notice back in.

        Without a file for stderr the listing falls back to the merged stream,
        so each word of the notice is tried as a table too: the price of keeping
        the error text when ``mktemp`` fails.
        """
        harness.break_mktemp()
        harness.notice = NOTICE

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert "Could not list" not in result.stdout
        assert _dumped_tables(result.stdout) == [
            *NOTICE.split(),
            "mysql_servers",
            "mysql_users",
        ]

    def test_killed_run_removes_temp_file(self, harness):
        """Remove the captured stream when a timeout kills the run mid-query."""
        harness.pin_mktemp()
        (harness.root / "hang").touch()
        proc = harness.spawn("--main")
        try:
            deadline = time.monotonic() + 30
            while not harness.calls and time.monotonic() < deadline:
                time.sleep(0.05)
            assert harness.calls, "the script never reached the client"

            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

        assert harness.temp_files
        assert not any(path.exists() for path in harness.temp_files)

    def test_failing_mktemp_still_reports_error(self, harness):
        """Fall back to the merged stream rather than losing the client's error."""
        harness.break_mktemp()
        harness.answer(Rule("SHOW TABLES", stderr=f"{ACCESS_DENIED}\n", status=1))

        result = harness.run("--main")

        assert f"(check --defaults-file): {ACCESS_DENIED}" in result.stdout


class TestCredentials:
    """Keep the admin password off the client's command line."""

    def test_password_travels_on_stdin_only(self, harness):
        """Hand the password to the client through stdin, never argv."""
        harness.run("--main")

        assert harness.calls
        assert all(PASSWORD not in call for call in harness.calls)
        assert PASSWORD in (harness.root / "stdin.log").read_text(encoding="utf-8")


def _ignored(key: str, cnf: Path) -> str:
    """Return the warning the script prints for a credential it will not read.

    :param key: The setting's name.
    :param cnf: The config file.
    :return: The warning line.
    """
    return f"Ignoring {key} in {cnf}: its value is not a plain string."


def _unread(key: str, cnf: Path) -> str:
    """Return the warning the script prints for a credential set in a form it does not read.

    :param key: The setting's name.
    :param cnf: The config file.
    :return: The warning line.
    """
    return f"Ignoring {key} in {cnf}: its line is not a plain KEY=value setting."


def _warnings(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Return the ignored-credential warnings a run printed.

    :param result: The finished run.
    :return: The warning lines, in order.
    """
    assert result.stdout, "the run printed no report"
    return [line for line in result.stdout.splitlines() if line.startswith("Ignoring ")]


class TestCredentialShapes:
    """Read each value shape proxysql-admin.cnf uses as the shell would."""

    @pytest.mark.parametrize(
        ("line", "password"),
        [
            pytest.param("PROXYSQL_PASSWORD='p w'", "p w", id="single-quoted"),
            pytest.param('PROXYSQL_PASSWORD="p w"', "p w", id="double-quoted"),
            pytest.param("PROXYSQL_PASSWORD=pw", "pw", id="bare"),
            pytest.param("export PROXYSQL_PASSWORD='pw'", "pw", id="export"),
            pytest.param(
                "\t export\tPROXYSQL_PASSWORD=pw  ", "pw", id="surrounding-whitespace"
            ),
            pytest.param(
                "PROXYSQL_PASSWORD='pw' # rotated", "pw", id="quoted-trailing-comment"
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=pw # rotated", "pw", id="bare-trailing-comment"
            ),
            pytest.param("PROXYSQL_PASSWORD='p#w'", "p#w", id="hash-single-quoted"),
            pytest.param('PROXYSQL_PASSWORD="p#w"', "p#w", id="hash-double-quoted"),
            pytest.param("PROXYSQL_PASSWORD=p#w", "p#w", id="hash-in-bare-word"),
            pytest.param(
                r'PROXYSQL_PASSWORD="a\"b\\c\$d\`e"',
                'a"b\\c$d`e',
                id="double-quoted-escapes",
            ),
            pytest.param(
                r'PROXYSQL_PASSWORD="a\nb"', r"a\nb", id="double-quoted-other-escape"
            ),
            pytest.param(
                r"PROXYSQL_PASSWORD='a\nb'", r"a\nb", id="single-quoted-backslash"
            ),
            pytest.param(r"PROXYSQL_PASSWORD=a\ b\$c", "a b$c", id="bare-escapes"),
            pytest.param(
                "PROXYSQL_PASSWORD='$(id)'", "$(id)", id="single-quoted-dollar"
            ),
            pytest.param("PROXYSQL_PASSWORD=p*w?[x]", "p*w?[x]", id="glob-characters"),
            pytest.param("PROXYSQL_PASSWORD=pässwörd", "pässwörd", id="non-ascii"),
            pytest.param("PROXYSQL_PASSWORD=pa~:w", "pa~:w", id="tilde-mid-word"),
            pytest.param(
                r"PROXYSQL_PASSWORD=pa\:~", "pa:~", id="tilde-after-escaped-colon"
            ),
            pytest.param(
                r"PROXYSQL_PASSWORD=pa:\~", "pa:~", id="escaped-tilde-after-colon"
            ),
            pytest.param(
                "PROXYSQL_PASSWORD='pa:~'", "pa:~", id="tilde-after-quoted-colon"
            ),
        ],
    )
    def test_reads_value_shape(self, harness, line, password):
        """Hand the client the literal value each shape spells."""
        harness.write_cnf(f"PROXYSQL_USERNAME=admin\n{line}\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == []
        assert harness.credentials == {
            **DEFAULT_CLIENT,
            "user": "admin",
            "password": password,
        }

    def test_reads_shipped_cnf(self, harness):
        """Read the four admin settings and ignore every other key of the shipped file."""
        harness.write_cnf(SHIPPED_CNF)

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert harness.credentials == SHIPPED_CLIENT

    def test_strips_carriage_returns(self, harness):
        """Read a file saved with CRLF line endings without a stray carriage return."""
        harness.write_cnf(ADMIN_CNF.replace("\n", "\r\n"))

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert harness.credentials == ADMIN_CLIENT

    def test_reads_last_line_without_newline(self, harness):
        """Read a setting on a final line that has no newline."""
        harness.write_cnf(ADMIN_CNF.rstrip("\n"))

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert harness.credentials == ADMIN_CLIENT

    def test_last_assignment_wins(self, harness):
        """Take a repeated setting's last value, as sourcing the file did."""
        harness.write_cnf(ADMIN_CNF + "PROXYSQL_PORT=7032\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert harness.credentials == {**ADMIN_CLIENT, "port": "7032"}

    @pytest.mark.parametrize(
        "spanning",
        [
            pytest.param('NOTE="first\n{setting}\nlast"', id="double-quoted"),
            pytest.param("NOTE='first\n{setting}\nlast'", id="single-quoted"),
            pytest.param(
                'NOTE="first\nescaped \\" quote\n{setting}\nlast"',
                id="escaped-quote-does-not-close",
            ),
            pytest.param("NOTE=it's\n{setting}\n'", id="quote-opened-mid-word"),
            pytest.param("NOTE=pa#ss'\n{setting}\n'", id="quote-after-hash-in-word"),
            pytest.param("echo it's\n{setting}\n'", id="quote-in-a-command"),
            pytest.param("NOTE=first\\\n{setting}", id="line-continuation"),
            pytest.param(
                "NOTE=first\\\nsecond\\\n{setting}", id="chained-line-continuation"
            ),
        ],
    )
    def test_lines_of_a_multiline_value_are_not_settings(self, harness, spanning):
        """Skip the lines a value spans, then resume once it ends."""
        harness.write_cnf(
            "PROXYSQL_USERNAME='admin'\n"
            + spanning.replace("{setting}", "PROXYSQL_PASSWORD=hijacked")
            + "\nPROXYSQL_PORT=7032\n"
        )

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == []
        assert harness.credentials == {
            **DEFAULT_CLIENT,
            "user": "admin",
            "port": "7032",
        }

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("NOTE=a # it's", id="quote-in-a-comment"),
            pytest.param("NOTE=a;#'", id="quote-in-a-comment-after-a-command"),
            pytest.param("# it's a comment", id="quote-in-a-comment-line"),
        ],
    )
    def test_quote_in_a_comment_opens_nothing(self, harness, line):
        """Read the setting after a line whose only quote sits in a comment."""
        harness.write_cnf(f"{line}\nPROXYSQL_USERNAME=admin\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert harness.credentials == {**DEFAULT_CLIENT, "user": "admin"}

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("NOTE='PROXYSQL_PASSWORD=x y'", id="single-quoted"),
            pytest.param('NOTE="see PROXYSQL_PASSWORD=x"', id="double-quoted"),
            pytest.param("NOTE=a # PROXYSQL_PASSWORD=x", id="comment"),
            pytest.param("NOTE=a\\ PROXYSQL_PASSWORD=x", id="escaped-space"),
            pytest.param("echo PROXYSQL_PASSWORD=x", id="command-argument"),
        ],
    )
    def test_mentioned_credential_is_not_a_setting(self, harness, line):
        """Keep the password when a later line only mentions its setting."""
        harness.write_cnf(f"PROXYSQL_PASSWORD=kept\n{line}\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == []
        assert harness.credentials == {**DEFAULT_CLIENT, "password": "kept"}


class TestDefaultsWhenAbsent:
    """Fall back to the documented defaults for settings the file does not give."""

    @pytest.mark.parametrize(
        "cnf",
        [
            pytest.param("", id="empty"),
            pytest.param("# nothing here\n\n   \n", id="comments-only"),
            pytest.param(
                "#export PROXYSQL_PASSWORD='old'\n  # PROXYSQL_PORT=1\n",
                id="commented-out",
            ),
            pytest.param(
                "proxysql_password=pw\nPROXYSQL_PORT = 7032\nexport PROXYSQL_HOSTNAME\n",
                id="not-an-assignment",
            ),
            pytest.param(
                "PROXYSQL_USERNAME=''\nPROXYSQL_PASSWORD=\"\"\n"
                "PROXYSQL_HOSTNAME=\nPROXYSQL_PORT=''\n",
                id="empty-values",
            ),
        ],
    )
    def test_uses_defaults(self, harness, cnf):
        """Connect with the defaults when the file sets no usable credential."""
        harness.write_cnf(cnf)

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == []
        assert harness.credentials == DEFAULT_CLIENT

    def test_ignores_environment(self, harness):
        """Take a missing setting's default, not a same-named environment variable."""
        harness.write_cnf("PROXYSQL_USERNAME=admin\n")

        result = harness.run(
            "--main",
            env={
                "PROXYSQL_PASSWORD": "from-env",
                "PROXYSQL_HOSTNAME": "10.9.9.9",
                "PROXYSQL_PORT": "9999",
            },
        )

        assert result.returncode == 0, result.stderr
        assert harness.credentials == {**DEFAULT_CLIENT, "user": "admin"}


# Config lines that run a command when the file is sourced.
PAYLOADS = [
    pytest.param("touch {marker}", id="command"),
    pytest.param('PROXYSQL_PASSWORD="$(touch {marker})"', id="quoted-substitution"),
    pytest.param("PROXYSQL_PASSWORD=$(touch {marker})", id="bare-substitution"),
    pytest.param("PROXYSQL_PASSWORD=`touch {marker}`", id="backticks"),
    pytest.param("PROXYSQL_PORT=6032; touch {marker}", id="semicolon"),
    pytest.param("PROXYSQL_PORT=6032 && touch {marker}", id="and-list"),
    pytest.param("PROXYSQL_PORT=6032 | touch {marker}", id="pipe"),
    pytest.param("PROXYSQL_HOSTNAME=${X:=$(touch {marker})}", id="expansion"),
    pytest.param("PROXYSQL_USERNAME=admin touch {marker}", id="env-prefix"),
    pytest.param("cat <<EOF > {marker}\nx\nEOF", id="here-document"),
    pytest.param("f() { touch {marker}; }\nf", id="function"),
    pytest.param("trap 'touch {marker}' EXIT", id="exit-trap"),
]


class TestDefaultsFileNeverExecutes:
    """Read the config file as text, so nothing written in it ever runs."""

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_payload_runs_when_sourced(self, harness, payload):
        """Prove each payload does run under ``source``, so its absence below counts."""
        marker = harness.root / "executed"
        harness.write_cnf(payload.replace("{marker}", str(marker)) + "\n")

        subprocess.run(
            ["bash", "-c", 'source "$1" > /dev/null 2>&1', "bash", harness.admin_cnf],
            check=False,
            timeout=10,
        )

        assert marker.exists()

    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_does_not_run_payload(self, harness, payload):
        """Leave no trace of the payload and still read the settings around it."""
        marker = harness.root / "executed"
        harness.write_cnf(payload.replace("{marker}", str(marker)) + "\n" + ADMIN_CNF)

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert not marker.exists()
        assert harness.credentials == ADMIN_CLIENT

    def test_cannot_override_script_state(self, harness):
        """Keep the file from reassigning the script's own variables."""
        harness.write_cnf(
            "USER=evil\nPASSWORD=evil\nHOST=evil\nPORT=1\n"
            "DEFAULTS_FILE=/nonexistent\nOUTPUT_MODE=file\nPATH=/nonexistent\n"
            + ADMIN_CNF
        )

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert "mysql_servers" in result.stdout
        assert "Output written" not in result.stdout
        assert harness.credentials == ADMIN_CLIENT

    def test_script_has_no_source_command(self):
        """Pin that no line of the script sources or dot-includes a file."""
        script = SCRIPT.read_text(encoding="utf-8")

        assert not re.search(r"^\s*(source|\.)\s", script, re.MULTILINE)


class TestNonLiteralCredential:
    """Ignore, with a warning, a credential only shell evaluation could complete."""

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param('PROXYSQL_PASSWORD="pa${X}ss"', id="braced-expansion"),
            pytest.param("PROXYSQL_PASSWORD=pa$X", id="bare-expansion"),
            pytest.param('PROXYSQL_PASSWORD="$(printf leaked)"', id="substitution"),
            pytest.param("PROXYSQL_PASSWORD=`printf leaked`", id="backticks"),
            pytest.param("PROXYSQL_PASSWORD='a'b", id="quote-then-word"),
            pytest.param("PROXYSQL_PASSWORD='a';x", id="quote-then-command"),
            pytest.param("PROXYSQL_PASSWORD=\"a\"'b'", id="concatenated-quotes"),
            pytest.param("PROXYSQL_PASSWORD=a b", id="second-word"),
            pytest.param("PROXYSQL_PASSWORD=~admin", id="tilde"),
            pytest.param("PROXYSQL_PASSWORD=pa:~", id="tilde-after-colon"),
            pytest.param("PROXYSQL_PASSWORD=pa::~/x", id="tilde-after-colons"),
            pytest.param("PROXYSQL_HOSTNAME=$HOSTNAME", id="hostname"),
            pytest.param("PROXYSQL_PORT=$((6000 + 32))", id="arithmetic"),
            pytest.param("PROXYSQL_USERNAME=adm'in'", id="quote-mid-word"),
        ],
    )
    def test_warns_and_resets_to_default(self, harness, line):
        """Name the key, reset it to its default and keep the other settings."""
        key, raw = line.split("=", 1)
        harness.write_cnf(f"{SHIPPED_CNF}{line}\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == [_ignored(key, harness.admin_cnf)]
        field_name = FIELD_OF[key]
        assert harness.credentials == {
            **SHIPPED_CLIENT,
            field_name: DEFAULT_CLIENT[field_name],
        }
        assert raw not in result.stdout + result.stderr

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(
                "PROXYSQL_PASSWORD='first\nPROXYSQL_USERNAME=hijacked\nlast'",
                id="single-quoted",
            ),
            pytest.param(
                'PROXYSQL_PASSWORD="first\nPROXYSQL_USERNAME=hijacked\nlast"',
                id="double-quoted",
            ),
            pytest.param(
                'PROXYSQL_PASSWORD="first\\"\nPROXYSQL_USERNAME=hijacked\nlast"',
                id="escaped-quote",
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=adm'in\nPROXYSQL_USERNAME=hijacked\n'",
                id="quote-mid-word",
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=first\\\nPROXYSQL_USERNAME=hijacked",
                id="line-continuation",
            ),
        ],
    )
    def test_multiline_credential_is_ignored(self, harness, line):
        """Ignore a credential spanning lines, and every line it spans."""
        harness.write_cnf(f"{SHIPPED_CNF}{line}\nPROXYSQL_PORT=7032\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == [_ignored("PROXYSQL_PASSWORD", harness.admin_cnf)]
        assert harness.credentials == {
            **SHIPPED_CLIENT,
            "password": "",
            "port": "7032",
        }

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("A=1 PROXYSQL_PASSWORD=leaked", id="after-an-assignment"),
            pytest.param("export A=1 PROXYSQL_PASSWORD=leaked", id="export-list"),
            pytest.param("A='x y' PROXYSQL_PASSWORD=leaked", id="after-a-quoted-word"),
            pytest.param("PROXYSQL_PASSWORD+=leaked", id="append"),
            pytest.param("declare -x PROXYSQL_PASSWORD=leaked", id="declare"),
            pytest.param("readonly PROXYSQL_PASSWORD=leaked", id="readonly"),
            pytest.param(
                "NOTE=a\\\nexport PROXYSQL_PASSWORD=leaked", id="continued-line"
            ),
        ],
    )
    def test_warns_on_unread_assignment(self, harness, line):
        """Warn about, and reset, a credential the shell would set but the plain form misses."""
        harness.write_cnf(f"{SHIPPED_CNF}{line}\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == [_unread("PROXYSQL_PASSWORD", harness.admin_cnf)]
        assert harness.credentials == {**SHIPPED_CLIENT, "password": ""}
        warning_text = "\n".join(_warnings(result))
        assert warning_text
        assert "leaked" not in warning_text

    def test_warns_about_each_credential_on_a_line(self, harness):
        """Name both credentials of a two-assignment line, each with its own reason."""
        harness.write_cnf(f"{SHIPPED_CNF}PROXYSQL_PORT=7032 PROXYSQL_PASSWORD=leaked\n")

        result = harness.run("--main")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == [
            _ignored("PROXYSQL_PORT", harness.admin_cnf),
            _unread("PROXYSQL_PASSWORD", harness.admin_cnf),
        ]
        assert harness.credentials == {
            **SHIPPED_CLIENT,
            "password": "",
            "port": DEFAULT_CLIENT["port"],
        }

    def test_multiline_warning_omits_the_value(self, harness):
        """Leave the spanned value's text out of the warning."""
        harness.write_cnf(f"{SHIPPED_CNF}PROXYSQL_PASSWORD='first\nlast'\n")

        result = harness.run("--main")

        (warning,) = _warnings(result)
        assert "first" not in warning
        assert "last" not in warning

    def test_warning_stays_out_of_the_report_file(self, harness):
        """Print the warning to the run's output, not into the report file."""
        harness.write_cnf(f"{SHIPPED_CNF}PROXYSQL_PASSWORD=$X\n")

        result = harness.run("--main", "--output", "file")

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == [_ignored("PROXYSQL_PASSWORD", harness.admin_cnf)]
        (report,) = harness.root.glob("proxysql_status_*.log")
        report_text = report.read_text(encoding="utf-8")
        assert report_text
        assert "Ignoring " not in report_text


class TestUnreadableDefaultsFile:
    """Stop before any query when the config file cannot be read as a file."""

    def _assert_refused(self, harness, result):
        """Assert the run failed with the config error and never reached the client."""
        assert result.returncode == 1
        assert result.stdout == _getopt_probe_output() + (
            "Cannot find or read the config file (check --defaults-file): "
            f"{harness.admin_cnf}.\n"
        )
        assert harness.calls == []

    def test_missing_file(self, harness):
        """Refuse a path that does not exist."""
        harness.admin_cnf.unlink()

        self._assert_refused(harness, harness.run())

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable_file(self, harness):
        """Refuse a file the run has no permission to read."""
        harness.admin_cnf.chmod(0)

        self._assert_refused(harness, harness.run())

    def test_directory(self, harness):
        """Refuse a directory instead of connecting with the defaults."""
        harness.admin_cnf.unlink()
        harness.admin_cnf.mkdir()

        self._assert_refused(harness, harness.run())

    def test_symlink_to_directory(self, harness):
        """Refuse a link that resolves to a directory."""
        target = harness.root / "cnf-dir"
        target.mkdir()
        harness.admin_cnf.unlink()
        harness.admin_cnf.symlink_to(target)

        self._assert_refused(harness, harness.run())


class TestLargeDefaultsFile:
    """Read a big config file well within the run's time limit."""

    def test_reads_credentials_after_many_lines(self, harness):
        """Find the credentials below thousands of lines that need a quote scan."""
        filler = "# don't edit\nexport NOTE='it''s' \"a \\\"b\\\"\" c\\ d\n" * 10_000
        harness.write_cnf(filler + ADMIN_CNF)

        # A fork per line, as an earlier parser did, overruns this budget.
        result = harness.run("--main", timeout=20)

        assert result.returncode == 0, result.stderr
        assert _warnings(result) == []
        assert harness.credentials == ADMIN_CLIENT
