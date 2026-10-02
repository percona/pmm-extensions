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
"""

import os
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

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the shipped script with the staged config and stub.

        :param args: Options passed after ``--defaults-file``.
        :return: The completed process.
        """
        self._write_rules()
        return subprocess.run(
            self._command(args),
            capture_output=True,
            text=True,
            env=self._env(),
            cwd=self.root,
            timeout=60,
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
