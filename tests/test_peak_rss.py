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

"""Test the opt-in per-process peak-RSS report."""

import json
import os
import resource
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.peak_rss import (
    CONTROLLER,
    PEAK_RSS_FILE_ENV,
    peak_rss_summary,
    record_peak_rss,
)


@pytest.fixture
def report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the report variable at a fresh file and return its path."""
    path = tmp_path / "rss.jsonl"
    monkeypatch.setenv(PEAK_RSS_FILE_ENV, str(path))
    return path


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _fix_ru_maxrss(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    usage = SimpleNamespace(ru_maxrss=value)
    monkeypatch.setattr(resource, "getrusage", lambda _who: usage)


def test_writes_one_line_when_env_set(report: Path) -> None:
    """Record the process as the controller when it is not an xdist worker."""
    record_peak_rss(SimpleNamespace())

    [line] = _read_lines(report)
    assert line["worker"] == CONTROLLER
    assert isinstance(line["peak_rss_mib"], int)
    assert line["peak_rss_mib"] > 0


def test_worker_id_is_recorded(report: Path) -> None:
    """Record an xdist worker under its worker id."""
    record_peak_rss(SimpleNamespace(workerinput={"workerid": "gw3"}))

    [line] = _read_lines(report)
    assert line["worker"] == "gw3"


def test_appends_across_processes(report: Path) -> None:
    """Keep every process's line when several write to the same file."""
    record_peak_rss(SimpleNamespace(workerinput={"workerid": "gw0"}))
    record_peak_rss(SimpleNamespace(workerinput={"workerid": "gw1"}))

    assert [line["worker"] for line in _read_lines(report)] == ["gw0", "gw1"]


def test_noop_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Write nothing, and read nothing back, when the report is not requested."""
    monkeypatch.delenv(PEAK_RSS_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    record_peak_rss(SimpleNamespace())

    assert list(tmp_path.iterdir()) == []
    assert peak_rss_summary() == []


@pytest.mark.parametrize(
    ("platform", "ru_maxrss", "expected_mib"),
    [
        ("linux", 3 * 1024 * 1024, 3072),
        ("darwin", 3 * 1024 * 1024, 3),
    ],
)
def test_platform_units(
    monkeypatch: pytest.MonkeyPatch,
    report: Path,
    platform: str,
    ru_maxrss: int,
    expected_mib: int,
) -> None:
    """Normalise ``ru_maxrss`` from KiB on Linux and from bytes on macOS."""
    monkeypatch.setattr(sys, "platform", platform)
    _fix_ru_maxrss(monkeypatch, ru_maxrss)

    record_peak_rss(SimpleNamespace())

    [line] = _read_lines(report)
    assert line["peak_rss_mib"] == expected_mib


def test_median_summary_excludes_controller(report: Path) -> None:
    """Print every process's peak and the median over the workers alone."""
    report.write_text(
        "".join(
            json.dumps({"worker": worker, "peak_rss_mib": peak}) + "\n"
            for worker, peak in [
                ("gw0", 600),
                ("gw1", 700),
                ("gw2", 650),
                ("gw3", 900),
                (CONTROLLER, 100),
            ]
        ),
        encoding="utf-8",
    )

    summary = peak_rss_summary()

    assert summary[-1] == "median worker peak RSS: 675 MiB over 4 worker(s)"
    assert "controller: 100 MiB" in summary
    assert "gw3: 900 MiB" in summary


def test_nested_session_under_n0_counts_the_process_once(report: Path) -> None:
    """Report one ``controller`` line when a ``-n 0`` run nests ``pytest.main``.

    Without xdist, a test's in-process ``pytest.main`` session and the outer
    session both record as ``controller`` from the summarising process's own
    pid, so only the last line for that pid may count.
    """
    record_peak_rss(SimpleNamespace())
    record_peak_rss(SimpleNamespace())

    summary = peak_rss_summary()
    assert [line.split(":")[0] for line in summary[:-1]] == [CONTROLLER]


def test_controller_lines_from_other_processes_are_dropped(report: Path) -> None:
    """Drop a ``controller`` line a test's pytest subprocess wrote.

    A subprocess inherits the report variable, and ``ru_maxrss`` survives
    ``execve``, so its line repeats the spawning worker's peak under a pid that
    is not the summarising controller's.
    """
    foreign_pid = os.getpid() + 1
    report.write_text(
        json.dumps({"worker": CONTROLLER, "pid": foreign_pid, "peak_rss_mib": 645})
        + "\n"
        + json.dumps({"worker": "gw0", "pid": foreign_pid + 1, "peak_rss_mib": 655})
        + "\n",
        encoding="utf-8",
    )

    assert peak_rss_summary() == [
        "gw0: 655 MiB",
        "median worker peak RSS: 655 MiB over 1 worker(s)",
    ]


def test_summary_without_workers_reports_no_median(report: Path) -> None:
    """Report the single process of a ``-n 0`` run without inventing a median."""
    report.write_text(
        json.dumps({"worker": CONTROLLER, "peak_rss_mib": 800}) + "\n",
        encoding="utf-8",
    )

    assert peak_rss_summary() == [
        f"{CONTROLLER}: 800 MiB",
        "median worker peak RSS: n/a (no xdist workers)",
    ]
