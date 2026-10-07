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

The script also prints its admin config file, which holds the admin password,
so these tests stage config files of every shape the script may meet and check
that no credential value reaches the report.
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
REDACTED_ADMIN_CNF = (
    "PROXYSQL_USERNAME='admin'\n"
    "PROXYSQL_PASSWORD=[REDACTED]\n"
    "PROXYSQL_HOSTNAME='127.0.0.1'\n"
    "PROXYSQL_PORT='6032'\n"
)
HIDDEN_LINE = "# [REDACTED: line not shown]"
CNF_START = "............ DUMPING PROXYSQL ADMIN CNF FILE ............"
CNF_END = "............ END OF DUMPING PROXYSQL ADMIN CNF FILE ............"
HOST_PRIORITY_START = "............ DUMPING HOST PRIORITY FILE ............"
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

    def write_cnf(self, text: str) -> None:
        """Replace the admin config with ``text``, line endings kept as given.

        :param text: The exact file content.
        """
        self.admin_cnf.write_bytes(text.encode())

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
        + f"{HOST_PRIORITY_START}\n"
        + HOST_PRIORITY
        + "............ END OF DUMPING HOST PRIORITY FILE ............\n\n"
        + f"{CNF_START}\n"
        + REDACTED_ADMIN_CNF
        + f"{CNF_END}\n\n"
    )


def _cnf_section(report: str) -> list[str]:
    """Return the lines printed inside the admin config section.

    :param report: The script's report.
    :return: The lines between the section's start and end banners.
    """
    lines = report.splitlines()
    return lines[lines.index(CNF_START) + 1 : lines.index(CNF_END)]


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


SECRET = "hunter2-cnf-secret"
REAL_SHAPED_CNF = f"""\
# proxysql admin interface credentials.
export PROXYSQL_DATADIR='/var/lib/proxysql'
export PROXYSQL_USERNAME='admin'
export PROXYSQL_PASSWORD='{PASSWORD}'
export PROXYSQL_HOSTNAME='localhost'
export PROXYSQL_PORT='6032'

# PXC admin credentials for connecting to pxc-cluster-node.
export CLUSTER_USERNAME='admin'
export CLUSTER_PASSWORD='cluster-pass-1'
export CLUSTER_HOSTNAME='localhost'
export CLUSTER_PORT='3306'

# proxysql monitoring user. The admin script creates this user on the nodes.
export MONITOR_USERNAME="monitor"
export MONITOR_PASSWORD="monitor-pass-2"

# Application user to connect to pxc-node through proxysql
export CLUSTER_APP_USERNAME="proxysql_user"
export CLUSTER_APP_PASSWORD=app-pass-3

export WRITER_HOSTGROUP_ID='10'
export READER_HOSTGROUP_ID='11'
export API_TOKEN='token-4'
export BACKUP_SECRET='secret-5'
export REPL_PWD='pwd-6'
export API_KEY='key-7'
export AUTH_STRING='auth-8'
export DB_CREDENTIALS='cred-9'
export BACKUP_URL='mysql://backup:url-10@db:3306/'
"""
REDACTED_REAL_SHAPED_CNF = """\
# proxysql admin interface credentials.
export PROXYSQL_DATADIR='/var/lib/proxysql'
export PROXYSQL_USERNAME='admin'
export PROXYSQL_PASSWORD=[REDACTED]
export PROXYSQL_HOSTNAME='localhost'
export PROXYSQL_PORT='6032'

# PXC admin credentials for connecting to pxc-cluster-node.
export CLUSTER_USERNAME='admin'
export CLUSTER_PASSWORD=[REDACTED]
export CLUSTER_HOSTNAME='localhost'
export CLUSTER_PORT='3306'

# proxysql monitoring user. The admin script creates this user on the nodes.
export MONITOR_USERNAME="monitor"
export MONITOR_PASSWORD=[REDACTED]

# Application user to connect to pxc-node through proxysql
export CLUSTER_APP_USERNAME="proxysql_user"
export CLUSTER_APP_PASSWORD=[REDACTED]

export WRITER_HOSTGROUP_ID='10'
export READER_HOSTGROUP_ID='11'
export API_TOKEN=[REDACTED]
export BACKUP_SECRET=[REDACTED]
export REPL_PWD=[REDACTED]
export API_KEY=[REDACTED]
export AUTH_STRING=[REDACTED]
export DB_CREDENTIALS=[REDACTED]
export BACKUP_URL=[REDACTED]
"""
REAL_SHAPED_SECRETS = (
    PASSWORD,
    "cluster-pass-1",
    "monitor-pass-2",
    "app-pass-3",
    "token-4",
    "secret-5",
    "pwd-6",
    "key-7",
    "auth-8",
    "cred-9",
    "url-10",
)


class TestAdminCnfRedaction:
    """Show the admin config for diagnosis without ever printing a credential."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param((), id="default"),
            pytest.param(("--files",), id="files"),
            pytest.param(("--output", "file"), id="output-file"),
        ],
    )
    def test_password_absent_in_every_run_mode(self, harness, args):
        """Mask the password on stdout, stderr and the report file alike."""
        result = harness.run(*args)

        reports = [
            result.stdout,
            result.stderr,
            *(
                path.read_text(encoding="utf-8")
                for path in harness.root.glob("proxysql_status_*.log")
            ),
        ]
        assert result.returncode == 0, result.stderr
        assert all(PASSWORD not in report for report in reports)
        assert any("PROXYSQL_PASSWORD=[REDACTED]" in report for report in reports)

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param(
                f"export PROXYSQL_PASSWORD='{SECRET}'\n",
                "export PROXYSQL_PASSWORD=[REDACTED]",
                id="export-single-quoted",
            ),
            pytest.param(
                f'PROXYSQL_PASSWORD="{SECRET}"\n',
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="double-quoted",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD={SECRET}\n",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="bare",
            ),
            pytest.param(
                f"  PROXYSQL_PASSWORD='{SECRET}'\n",
                "  PROXYSQL_PASSWORD=[REDACTED]",
                id="indented",
            ),
            pytest.param(
                f"export\tPROXYSQL_PASSWORD='{SECRET}'\n",
                "export\tPROXYSQL_PASSWORD=[REDACTED]",
                id="tab-after-export",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD='{SECRET}'  # admin\n",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="quoted-trailing-comment",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD={SECRET} # admin\n",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="bare-trailing-comment",
            ),
            pytest.param(
                f"proxysql_password='{SECRET}'\n",
                "proxysql_password=[REDACTED]",
                id="lowercase-key",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD='{SECRET}'\r\n",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="crlf",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD='{SECRET}'",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="no-final-newline",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD='%s\\n -e =#\"{SECRET}'\n",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="special-characters",
            ),
        ],
    )
    def test_masks_every_value_shape(self, harness, line, expected):
        """Mask the value whichever quoting, spacing or line ending it uses."""
        harness.write_cnf(f"PROXYSQL_USERNAME='admin'\n{line}")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert SECRET not in result.stdout
        assert _cnf_section(result.stdout) == ["PROXYSQL_USERNAME='admin'", expected]

    def test_masks_every_credential_of_a_real_shaped_cnf(self, harness):
        """Mask each credential, and keep every other line as is."""
        harness.write_cnf(REAL_SHAPED_CNF)

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert all(secret not in result.stdout for secret in REAL_SHAPED_SECRETS)
        assert _cnf_section(result.stdout) == REDACTED_REAL_SHAPED_CNF.splitlines()

    def test_keeps_comments_and_blank_lines(self, harness):
        """Print prose comments, commented-out settings and blank lines in place."""
        cnf = (
            "# ProxySQL admin settings, don't edit by hand\n"
            "\n"
            "PROXYSQL_USERNAME='admin'\n"
            "#PROXYSQL_PORT='6033'\n"
            "PROXYSQL_PORT='6032' # don't change\n"
            "\n"
            "  # trailing note\n"
        )
        harness.write_cnf(cnf)

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert _cnf_section(result.stdout) == cnf.splitlines()

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param(
                f"#PROXYSQL_PASSWORD='{SECRET}'",
                "#PROXYSQL_PASSWORD=[REDACTED]",
                id="commented-out",
            ),
            pytest.param(
                f"# export PROXYSQL_PASSWORD='{SECRET}'",
                "# export PROXYSQL_PASSWORD=[REDACTED]",
                id="commented-out-export",
            ),
            pytest.param(
                f"  ## CLUSTER_PASSWORD={SECRET}",
                "  ## CLUSTER_PASSWORD=[REDACTED]",
                id="indented-double-hash",
            ),
            pytest.param(
                f'#PROXYSQL_PASSWORD="{SECRET}$x"',
                HIDDEN_LINE,
                id="commented-out-complex-value",
            ),
            pytest.param(
                f"# old: PROXYSQL_PASSWORD={SECRET}",
                HIDDEN_LINE,
                id="prose-with-assignment",
            ),
            pytest.param(
                f"# admin password = {SECRET}",
                HIDDEN_LINE,
                id="prose-with-spaced-assignment",
            ),
            pytest.param(
                f"# old dsn mysql://admin:{SECRET}@db:3306/",
                HIDDEN_LINE,
                id="prose-with-url-credentials",
            ),
            pytest.param(
                f"PROXYSQL_USERNAME=admin # PROXYSQL_PASSWORD={SECRET}",
                HIDDEN_LINE,
                id="trailing-comment-assignment",
            ),
            pytest.param(
                f"PROXYSQL_USERNAME=admin # mysql://admin:{SECRET}@db",
                HIDDEN_LINE,
                id="trailing-comment-url-credentials",
            ),
            pytest.param(
                f"# PROXYSQL_USERNAME=admin # PROXYSQL_PASSWORD={SECRET}",
                HIDDEN_LINE,
                id="commented-out-trailing-comment-assignment",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD='{SECRET}' # PROXYSQL_PASSWORD={SECRET}",
                "PROXYSQL_PASSWORD=[REDACTED]",
                id="credential-with-trailing-comment-assignment",
            ),
        ],
    )
    def test_masks_credentials_in_comments(self, harness, line, expected):
        """Mask a credential a comment assigns, or hide the comment if it cannot."""
        harness.write_cnf(f"{line}\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert SECRET not in result.stdout
        assert _cnf_section(result.stdout) == [expected]

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("PROXYSQL_PASSWORD=''", "PROXYSQL_PASSWORD=''", id="single"),
            pytest.param('PROXYSQL_PASSWORD=""', 'PROXYSQL_PASSWORD=""', id="double"),
            pytest.param("PROXYSQL_PASSWORD=", "PROXYSQL_PASSWORD=", id="bare"),
            pytest.param(
                "export PROXYSQL_PASSWORD='' # not set",
                "export PROXYSQL_PASSWORD=''",
                id="trailing-comment",
            ),
            pytest.param(
                f'PROXYSQL_PASSWORD="" # {SECRET}',
                'PROXYSQL_PASSWORD=""',
                id="trailing-comment-secret",
            ),
            pytest.param(
                f"PROXYSQL_PASSWORD= #{SECRET}",
                "PROXYSQL_PASSWORD=",
                id="bare-trailing-comment-secret",
            ),
        ],
    )
    def test_shows_empty_credential(self, harness, line, expected):
        """Show an empty credential, since it reveals only that none is set.

        The trailing comment is dropped, since it may hold the old value.
        """
        harness.write_cnf(f"{line}\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert SECRET not in result.stdout
        assert _cnf_section(result.stdout) == [expected]

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(
                f"PROXYSQL_USERNAME='admin' PROXYSQL_PASSWORD='{SECRET}'",
                id="two-assignments",
            ),
            pytest.param(f"readonly PROXYSQL_PASSWORD='{SECRET}'", id="readonly"),
            pytest.param(f"declare -x PROXYSQL_PASSWORD='{SECRET}'", id="declare"),
            pytest.param(f"PROXYSQL_PASSWORD+='{SECRET}'", id="append"),
            pytest.param(
                f"PROXYSQL_PASSWORD=\"$(printf '%s' {SECRET})\"",
                id="command-substitution",
            ),
            pytest.param(f"PROXYSQL_PASSWORD='{SECRET}'suffix", id="concatenated"),
            pytest.param(
                f'PROXYSQL_PASSWORD="{SECRET}\\"quoted"', id="escaped-double-quote"
            ),
            pytest.param(f'PROXYSQL_DATADIR="$HOME/{SECRET}"', id="expansion"),
        ],
    )
    def test_hides_lines_it_cannot_read(self, harness, line):
        """Hide the whole line when it is not one plain assignment."""
        harness.write_cnf(f"{line}\nPROXYSQL_PORT='6032'\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert SECRET not in result.stdout
        assert _cnf_section(result.stdout) == [HIDDEN_LINE, "PROXYSQL_PORT='6032'"]

    @pytest.mark.parametrize(
        ("head", "tail"),
        [
            pytest.param("PROXYSQL_PASSWORD='value-head", "value-tail'", id="single"),
            pytest.param('PROXYSQL_PASSWORD="value-head', 'value-tail"', id="double"),
            pytest.param(
                "PROXYSQL_PASSWORD=$'value-head\\'", "value-tail'", id="ansi-c"
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=${UNSET:-value-head", "value-tail}", id="expansion"
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=$(printf '%s' value-head",
                "printf '%s' value-tail)",
                id="command-substitution",
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=\"$(printf '%s' \"value-head",
                'value-tail")"',
                id="nested-quotes",
            ),
            pytest.param(
                "PROXYSQL_PASSWORD=`printf '%s' value-head",
                "printf '%s' value-tail`",
                id="backticks",
            ),
            pytest.param("PROXYSQL_HOSTS=(value-head", "value-tail)", id="array"),
            pytest.param(
                'PROXYSQL_PASSWORD=value-head\\ #"', 'value-tail"', id="escaped-space"
            ),
        ],
    )
    def test_hides_every_line_of_a_multiline_value(self, harness, head, tail):
        """Hide each line of a value spanning lines, even one shaped like a comment."""
        harness.write_cnf(f"{head}\n#FOO=value-middle\n{tail}\nPROXYSQL_PORT='6032'\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert "value-" not in result.stdout
        assert _cnf_section(result.stdout) == [
            HIDDEN_LINE,
            HIDDEN_LINE,
            HIDDEN_LINE,
            "PROXYSQL_PORT='6032'",
        ]

    def test_hides_a_continued_line(self, harness):
        """Hide the line after a trailing backslash, which continues the value."""
        harness.write_cnf(
            "PROXYSQL_PASSWORD=value-head\\\nFOO=value-tail\nPROXYSQL_PORT='6032'\n"
        )

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert "value-" not in result.stdout
        assert _cnf_section(result.stdout) == [
            HIDDEN_LINE,
            HIDDEN_LINE,
            "PROXYSQL_PORT='6032'",
        ]

    def test_hides_the_rest_of_a_never_closed_value(self, harness):
        """Hide every line after a quote the file never closes."""
        harness.write_cnf("PROXYSQL_PASSWORD='value-head\nFOO=value-tail\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert "value-" not in result.stdout
        assert _cnf_section(result.stdout) == [HIDDEN_LINE, HIDDEN_LINE]

    def test_hides_the_rest_after_a_here_document(self, harness):
        """Hide every line after a here-document, whose body it does not track."""
        harness.write_cnf(
            "read -r PROXYSQL_PASSWORD <<'EOF'\n"
            "#FOO=value-body\n"
            "EOF\n"
            "PROXYSQL_PORT='6032'\n"
        )

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert "value-" not in result.stdout
        assert _cnf_section(result.stdout) == [HIDDEN_LINE] * 4

    def test_quote_in_a_comment_inside_a_value_opens_nothing(self, harness):
        """Show the line after a value whose comment line holds a lone quote."""
        harness.write_cnf(
            "PROXYSQL_PASSWORD=$(printf '%s' value-head\n"
            "# don't\n"
            ")\n"
            "PROXYSQL_PORT='6032'\n"
        )

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert _cnf_section(result.stdout) == [
            HIDDEN_LINE,
            HIDDEN_LINE,
            HIDDEN_LINE,
            "PROXYSQL_PORT='6032'",
        ]

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("A=1 B=2 # don't", id="quote-in-comment"),
            pytest.param("A=1 B=x\\\\", id="escaped-backslash"),
            pytest.param("A=$'x\\'y' B=1", id="ansi-c-escaped-quote"),
            pytest.param("A=${B:-x} C=1", id="expansion"),
            pytest.param("A=$(printf x) B=1", id="command-substitution"),
            pytest.param("A=$((1 + 2)) B=1", id="arithmetic"),
            pytest.param("A=`printf x` B=1", id="backticks"),
            pytest.param("A=(1 2) B=1", id="array"),
            pytest.param("read -r A <<< x", id="here-string"),
        ],
    )
    def test_hidden_line_that_ends_cleanly_hides_nothing_more(self, harness, line):
        """Show the next line when a hidden line leaves nothing open."""
        harness.write_cnf(f"{line}\nPROXYSQL_USERNAME='admin'\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert _cnf_section(result.stdout) == [
            HIDDEN_LINE,
            "PROXYSQL_USERNAME='admin'",
        ]

    def test_empty_cnf_prints_no_section(self, harness):
        """Leave the section out when the config file has nothing to show."""
        harness.write_cnf("\n\n")

        result = harness.run("--files")

        assert result.returncode == 0, result.stderr
        assert HOST_PRIORITY_START in result.stdout
        assert CNF_START not in result.stdout

    def test_table_filter_skips_cnf_section(self, harness):
        """Leave the config out of a run narrowed to tables."""
        result = harness.run("--files", "--main", "--table", "users")

        assert result.returncode == 0, result.stderr
        assert _dumped_tables(result.stdout) == ["mysql_users"]
        assert CNF_START not in result.stdout
