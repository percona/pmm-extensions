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

"""Tests for the ``scripts/check_nomad_payload_size.py`` CLI."""

import sys

import pytest

from tests.scripts import load_script, write_file

check_nomad_payload_size = load_script("check_nomad_payload_size")


VERBOSE_SCRIPT = '''\
"""A verbose module that minifies to something much smaller."""


def hello(name: str = "world") -> str:
    """Return a friendly greeting."""
    return f"hello, {name}"
'''

INVALID_SCRIPT = "def oops(:\n"
USAGE_ERROR_EXIT_CODE = 2


@pytest.mark.parametrize("argv", [[], ["--report"]], ids=["check", "report"])
def test_main_rejects_empty_paths(argv, capsys):
    """Exit with a usage error when no payload paths are provided in either mode."""
    with pytest.raises(SystemExit) as exc_info:
        check_nomad_payload_size.main(argv)
    assert exc_info.value.code == USAGE_ERROR_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err
    assert "the following arguments are required: path" in captured.err


def test_check_payload_uses_minified_text_for_size(tmp_path, monkeypatch):
    """Gzip-compress the minified source in ``check_payload``, not the original text."""
    path = write_file(tmp_path, "verbose.py", VERBOSE_SCRIPT)
    seen = {}
    real_minify = check_nomad_payload_size.minify

    def spy_minify(src, **kwargs):
        out = real_minify(src, **kwargs)
        seen["minified"] = out
        return out

    monkeypatch.setattr(check_nomad_payload_size, "minify", spy_minify)
    monkeypatch.setattr(check_nomad_payload_size, "NOMAD_PAYLOAD_SIZE_LIMIT", 0)
    _, size = check_nomad_payload_size.check_payload(str(path))
    assert len(seen["minified"]) < len(VERBOSE_SCRIPT)
    assert size > 0


def test_check_payload_falls_back_on_syntax_error(tmp_path, monkeypatch):
    """Ignore ``SyntaxError`` from ``minify`` in ``check_payload`` and use raw source."""
    path = write_file(tmp_path, "broken.py", INVALID_SCRIPT)

    def raise_syntax(src, **kwargs):
        raise SyntaxError("boom")

    monkeypatch.setattr(check_nomad_payload_size, "minify", raise_syntax)
    monkeypatch.setattr(check_nomad_payload_size, "NOMAD_PAYLOAD_SIZE_LIMIT", 0)
    result = check_nomad_payload_size.check_payload(str(path))
    assert result is not None
    assert result[0] == str(path)
    assert result[1] > 0


def test_main_reports_oversized_file_and_returns_one(tmp_path, monkeypatch, capsys):
    """Exit 1 from ``main`` and print path, size, and limit when a file exceeds the limit."""
    path = write_file(tmp_path, "verbose.py", VERBOSE_SCRIPT)
    monkeypatch.setattr(check_nomad_payload_size, "NOMAD_PAYLOAD_SIZE_LIMIT", 1)
    monkeypatch.setattr(sys, "argv", ["check_nomad_payload_size.py", str(path)])
    assert check_nomad_payload_size.main() == 1
    out = capsys.readouterr().out
    assert "exceed Nomad" in out
    assert str(path) in out
    assert "limit: 1" in out


def test_main_returns_zero_when_within_limit(tmp_path, monkeypatch, capsys):
    """Exit 0 from ``main`` and print nothing when all files are within the limit."""
    path = write_file(tmp_path, "verbose.py", VERBOSE_SCRIPT)
    monkeypatch.setattr(sys, "argv", ["check_nomad_payload_size.py", str(path)])
    assert check_nomad_payload_size.main() == 0
    assert capsys.readouterr().out == ""


def test_report_prints_size_headroom_and_returns_zero(tmp_path, monkeypatch, capsys):
    """Print size, limit, and headroom for a within-limit payload and exit 0."""
    path = write_file(tmp_path, "verbose.py", VERBOSE_SCRIPT)
    limit = 10_000
    monkeypatch.setattr(check_nomad_payload_size, "NOMAD_PAYLOAD_SIZE_LIMIT", limit)
    size = check_nomad_payload_size.payload_size(str(path))
    headroom = limit - size
    assert check_nomad_payload_size.main(["--report", str(path)]) == 0
    out = capsys.readouterr().out.strip()
    assert out == (f"{path}: {size:,} / {limit:,} bytes ({headroom:,} bytes headroom)")


def test_report_returns_zero_when_over_limit(tmp_path, monkeypatch, capsys):
    """Exit 0 from ``--report`` even when a payload exceeds the limit."""
    path = write_file(tmp_path, "verbose.py", VERBOSE_SCRIPT)
    monkeypatch.setattr(check_nomad_payload_size, "NOMAD_PAYLOAD_SIZE_LIMIT", 1)
    size = check_nomad_payload_size.payload_size(str(path))
    over = size - 1
    assert check_nomad_payload_size.main(["--report", str(path)]) == 0
    out = capsys.readouterr().out.strip()
    assert "OVER LIMIT" in out
    assert f"{over:,} bytes OVER LIMIT" in out
    assert str(path) in out


def test_report_prints_one_line_per_path(tmp_path, monkeypatch, capsys):
    """Print one line per path when ``--report`` is given multiple paths."""
    path_a = write_file(tmp_path, "a.py", VERBOSE_SCRIPT)
    path_b = write_file(tmp_path, "b.py", VERBOSE_SCRIPT)
    paths = [str(path_a), str(path_b)]
    limit = 10_000
    monkeypatch.setattr(check_nomad_payload_size, "NOMAD_PAYLOAD_SIZE_LIMIT", limit)
    size = check_nomad_payload_size.payload_size(paths[0])
    headroom = limit - size
    line = f"{paths[0]}: {size:,} / {limit:,} bytes ({headroom:,} bytes headroom)"
    assert check_nomad_payload_size.main(["--report", *paths]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == len(paths)
    assert lines[0] == line
    assert lines[1] == line.replace(paths[0], paths[1])
