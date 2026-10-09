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

"""Tests for the ``scripts/sync_alembic_version_locations.py`` CLI."""

from configparser import RawConfigParser
from pathlib import Path

import pytest

from tests.scripts import load_script

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sync_alembic_version_locations = load_script("sync_alembic_version_locations")

_MINIMAL_INI = """\
[alembic]
databases = extensions

[extensions]
# path to migration scripts.
script_location = app/extensions/migrations
# stale hand-written comment
version_locations = %(here)s/app/extensions/migrations/versions

[post_write_hooks]
# keep this section marker
"""


def _migration_plugin(root: Path, name: str) -> Path:
    """Create a regular plugin package with ``migrations/versions/``.

    :param root: Parent directory acting as the apps root.
    :param name: Plugin directory name.
    :return: The plugin directory path.
    """
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text("")
    (plugin_dir / "migrations" / "versions").mkdir(parents=True)
    return plugin_dir


def test_sync_regenerates_from_synthetic_app_tree(tmp_path):
    """Rewrite ``version_locations`` from a synthetic apps tree."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "zebra")
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_MINIMAL_INI)

    assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
    text = ini_path.read_text()
    expected = (
        "%(here)s/app/extensions/migrations/versions:"
        "%(here)s/app/extensions/apps/alpha/migrations/versions:"
        "%(here)s/app/extensions/apps/zebra/migrations/versions"
    )
    assert f"version_locations = {expected}" in text
    assert "GENERATED" in text
    assert "stale hand-written comment" not in text


def test_sync_is_idempotent_on_second_run(tmp_path):
    """Leave the file byte-identical on a second sync."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_MINIMAL_INI)

    assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
    after_first = ini_path.read_text()
    assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
    assert ini_path.read_text() == after_first
    assert sync_alembic_version_locations.sync_alembic_ini(
        ini_path, apps_root, check=True
    )


def test_sync_preserves_crlf_line_endings(tmp_path):
    """Preserve CRLF ``alembic.ini`` without converting to ``LF``."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    crlf_ini = _MINIMAL_INI.replace("\n", "\r\n").encode("utf-8")
    ini_path.write_bytes(crlf_ini)

    assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
    rewritten = ini_path.read_bytes()
    assert b"\r\n" in rewritten
    assert b"\n" not in rewritten.replace(b"\r\n", b"")
    assert b"app/extensions/apps/alpha/migrations/versions" in rewritten


def test_sync_failed_write_leaves_original_and_no_temp_file(tmp_path, monkeypatch):
    """Keep the original ini and leave no temp file when the final swap fails."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_MINIMAL_INI, encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

    assert ini_path.read_text(encoding="utf-8") == _MINIMAL_INI
    assert [p.name for p in tmp_path.iterdir() if p.name != "apps"] == ["alembic.ini"]


def test_sync_preserves_file_mode(tmp_path):
    """Keep the original file mode of a rewritten ``alembic.ini``."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_MINIMAL_INI, encoding="utf-8")
    expected_mode = 0o644
    ini_path.chmod(expected_mode)

    assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
    assert "alpha" in ini_path.read_text(encoding="utf-8")
    assert ini_path.stat().st_mode & 0o777 == expected_mode


def test_sync_rejects_multiline_version_locations(tmp_path):
    """Reject ``version_locations`` values with indented continuation lines."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(
        _MINIMAL_INI.replace(
            "version_locations = %(here)s/app/extensions/migrations/versions\n",
            "version_locations = %(here)s/app/extensions/migrations/versions:\n"
            "    %(here)s/app/extensions/apps/stale/migrations/versions\n",
        )
    )

    with pytest.raises(ValueError, match="single line"):
        sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)


def test_sync_rejects_missing_extensions_section(tmp_path):
    """Reject an ini that lacks a ``[extensions]`` section."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text("[alembic]\ndatabases = extensions\n")

    with pytest.raises(ValueError, match=r"no \[extensions\] section"):
        sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)


def test_sync_rejects_missing_version_locations_assignment(tmp_path):
    """Reject a ``[extensions]`` section that has no ``version_locations`` assignment."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(
        "[alembic]\ndatabases = extensions\n\n[extensions]\nscript_location = x\n"
    )

    with pytest.raises(ValueError, match="no version_locations assignment"):
        sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)


def test_main_reports_malformed_ini_cleanly(tmp_path, capsys):
    """Report a malformed ini as a one-line CLI error, not a traceback."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text("[alembic]\ndatabases = extensions\n")

    assert (
        sync_alembic_version_locations.main(
            ["--ini", str(ini_path), "--apps-root", str(apps_root)]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "no [extensions] section" in err
    assert "Traceback" not in err


def test_sync_preserves_comment_block_and_other_sections(tmp_path):
    """Keep ``script_location`` and non-``[extensions]`` sections intact."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_MINIMAL_INI)

    sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
    text = ini_path.read_text()
    assert "script_location = app/extensions/migrations" in text
    assert "[post_write_hooks]" in text
    assert "# keep this section marker" in text
    assert "databases = extensions" in text
    assert "GENERATED — do not hand-edit" in text


def test_sync_check_detects_drift(tmp_path):
    """Fail ``--check`` when the committed value is stale."""
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    _migration_plugin(apps_root, "alpha")
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_MINIMAL_INI)

    assert not sync_alembic_version_locations.sync_alembic_ini(
        ini_path, apps_root, check=True
    )
    assert (
        sync_alembic_version_locations.main(
            ["--check", "--ini", str(ini_path), "--apps-root", str(apps_root)]
        )
        == 1
    )


def test_committed_alembic_ini_matches_discovery():
    """Confirm committed ``alembic.ini`` matches the filesystem walk."""
    assert sync_alembic_version_locations.main(["--check"]) == 0


def test_entry_separator_matches_the_committed_version_path_separator():
    """Split on the character the ini tells Alembic the value is joined with.

    The two are declared independently, and a mismatch is silent: the whole
    value would parse as one entry, so every configured path would look
    pruned and the generator would refuse forever.
    """
    parser = RawConfigParser()
    parser.read(_PROJECT_ROOT / "alembic.ini")

    assert (
        parser.get("extensions", "version_path_separator")
        == sync_alembic_version_locations.ENTRY_SEPARATOR
    )


def _ini_with_entries(*plugins: str) -> str:
    """Return a minimal ini whose ``version_locations`` lists ``plugins``.

    :param plugins: App names to list after the main-chain entry.
    :return: Ini text with the assignment already populated.
    """
    entries = [
        sync_alembic_version_locations.MAIN_VERSIONS_ENTRY,
        *(
            f"%(here)s/app/extensions/apps/{name}/migrations/versions"
            for name in plugins
        ),
    ]
    return _MINIMAL_INI.replace(
        "version_locations = %(here)s/app/extensions/migrations/versions",
        f"version_locations = "
        f"{sync_alembic_version_locations.ENTRY_SEPARATOR.join(entries)}",
    )


@pytest.fixture
def stripped_tree(tmp_path) -> tuple[Path, Path]:
    """Return an ini listing ``alpha`` beside an apps root that holds nothing.

    The shape a stripped image leaves behind: the entry is configured but the
    walk finds no package to rediscover it from.

    :param tmp_path: Pytest temporary directory.
    :return: The ``alembic.ini`` path and the apps root to scan.
    """
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(_ini_with_entries("alpha"))
    return ini_path, apps_root


class TestCurrentVersionLocations:
    """Cover reading the entries already listed in the ``[extensions]`` section."""

    def test_splits_the_committed_value_on_the_separator(self):
        """Return one entry per colon-separated path, in file order."""
        text = _ini_with_entries("zebra", "alpha")

        assert sync_alembic_version_locations._current_version_locations(text) == (
            "%(here)s/app/extensions/migrations/versions",
            "%(here)s/app/extensions/apps/zebra/migrations/versions",
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )

    def test_strips_padding_and_drops_empty_entries(self):
        """Tolerate a hand-spaced value without inventing phantom entries."""
        text = _MINIMAL_INI.replace(
            "version_locations = %(here)s/app/extensions/migrations/versions",
            "version_locations =  %(here)s/app/extensions/migrations/versions : "
            "%(here)s/app/extensions/apps/alpha/migrations/versions :",
        )

        assert sync_alembic_version_locations._current_version_locations(text) == (
            "%(here)s/app/extensions/migrations/versions",
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )

    def test_returns_nothing_for_an_empty_assignment(self):
        """Treat a blank value as zero entries rather than one empty entry."""
        text = _MINIMAL_INI.replace(
            "version_locations = %(here)s/app/extensions/migrations/versions",
            "version_locations =",
        )

        assert sync_alembic_version_locations._current_version_locations(text) == ()

    def test_rejects_a_missing_extensions_section(self):
        """Raise rather than report an empty list for a malformed ini."""
        with pytest.raises(ValueError, match=r"no \[extensions\] section"):
            sync_alembic_version_locations._current_version_locations(
                "[alembic]\ndatabases = extensions\n"
            )

    def test_ignores_a_version_locations_line_outside_the_extensions_section(self):
        """Read only the ``[extensions]`` value, not a same-named key elsewhere."""
        text = _MINIMAL_INI.replace(
            "[post_write_hooks]",
            "[other]\nversion_locations = %(here)s/decoy\n\n[post_write_hooks]",
        )

        assert sync_alembic_version_locations._current_version_locations(text) == (
            "%(here)s/app/extensions/migrations/versions",
        )


class TestRemovalRefusal:
    """Cover the guard that keeps a regeneration from pruning entries."""

    def test_refuses_to_drop_a_configured_entry(self, stripped_tree):
        """Fail without writing when the walk omits a listed app."""
        ini_path, apps_root = stripped_tree
        before = ini_path.read_text()

        with pytest.raises(
            sync_alembic_version_locations.VersionLocationsRemovalError
        ) as excinfo:
            sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

        assert excinfo.value.removed == (
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )
        assert ini_path.read_text() == before

    def test_refuses_a_tree_that_both_adds_and_removes(self, stripped_tree):
        """Use a set difference, not a subset test, to spot the removal.

        The discovered set is not a subset of the configured one here, so a
        subset test would wave this through and prune ``alpha``.
        """
        ini_path, apps_root = stripped_tree
        _migration_plugin(apps_root, "beta")

        with pytest.raises(
            sync_alembic_version_locations.VersionLocationsRemovalError
        ) as excinfo:
            sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

        assert excinfo.value.removed == (
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )

    def test_names_every_removed_entry_in_configuration_order(self, stripped_tree):
        """List each pruned entry as the file ordered them."""
        ini_path, apps_root = stripped_tree
        ini_path.write_text(_ini_with_entries("zebra", "alpha"))

        with pytest.raises(
            sync_alembic_version_locations.VersionLocationsRemovalError
        ) as excinfo:
            sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

        assert excinfo.value.removed == (
            "%(here)s/app/extensions/apps/zebra/migrations/versions",
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )

    def test_additive_only_change_still_writes(self, stripped_tree):
        """Leave the existing add-an-app workflow untouched."""
        ini_path, apps_root = stripped_tree
        _migration_plugin(apps_root, "alpha")
        _migration_plugin(apps_root, "beta")

        assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
        assert "app/extensions/apps/beta/migrations/versions" in ini_path.read_text()

    def test_reordering_alone_is_not_a_removal(self, stripped_tree):
        """Compare entry sets, so a re-sorted list writes without refusing."""
        ini_path, apps_root = stripped_tree
        _migration_plugin(apps_root, "alpha")
        _migration_plugin(apps_root, "zebra")
        ini_path.write_text(_ini_with_entries("zebra", "alpha"))

        assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

    def test_check_refuses_a_pruning_tree(self, stripped_tree):
        """Refuse under ``check`` too, so CI sees the pruning attempt."""
        ini_path, apps_root = stripped_tree

        with pytest.raises(sync_alembic_version_locations.VersionLocationsRemovalError):
            sync_alembic_version_locations.sync_alembic_ini(
                ini_path, apps_root, check=True
            )

    def test_opt_in_flag_performs_the_removing_write(self, stripped_tree):
        """Allow a deliberate deletion of an app's migration chain."""
        ini_path, apps_root = stripped_tree

        assert sync_alembic_version_locations.sync_alembic_ini(
            ini_path, apps_root, allow_removals=True
        )
        remaining = ini_path.read_text()
        assert "app/extensions/migrations/versions" in remaining
        assert "app/extensions/apps/alpha/migrations/versions" not in remaining

    def test_a_cosmetically_different_spelling_is_not_a_removal(self, stripped_tree):
        """Compare normalised paths, so a trailing slash still writes.

        The spelling names a directory that is present, and refusing it would
        leave ``--allow-removals`` — the one flag that does disarm the filter
        — as the only way to run the generator again.
        """
        ini_path, apps_root = stripped_tree
        _migration_plugin(apps_root, "alpha")
        ini_path.write_text(
            _ini_with_entries("alpha").replace(
                "%(here)s/app/extensions/apps/alpha/migrations/versions",
                "%(here)s/./app/extensions/apps/alpha/migrations/versions/",
            )
        )

        assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
        assert (
            "version_locations = %(here)s/app/extensions/migrations/versions:"
            "%(here)s/app/extensions/apps/alpha/migrations/versions"
            in ini_path.read_text()
        )

    def test_duplicate_entries_collapse_without_refusing(self, stripped_tree):
        """Treat a repeated entry as one, so the write dedupes it silently."""
        ini_path, apps_root = stripped_tree
        _migration_plugin(apps_root, "alpha")
        ini_path.write_text(_ini_with_entries("alpha", "alpha"))

        assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
        assert (
            ini_path.read_text().count("app/extensions/apps/alpha/migrations/versions")
            == 1
        )

    def test_refuses_when_only_the_versions_directory_was_deleted(self, stripped_tree):
        """Catch a half-deleted package, not just a missing app directory."""
        ini_path, apps_root = stripped_tree
        plugin_dir = _migration_plugin(apps_root, "alpha")
        (plugin_dir / "migrations" / "versions").rmdir()

        with pytest.raises(
            sync_alembic_version_locations.VersionLocationsRemovalError
        ) as excinfo:
            sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

        assert excinfo.value.removed == (
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )

    def test_refuses_when_init_py_is_gone_but_versions_remains(self, stripped_tree):
        """Refuse when versions/ survives without the package ``__init__.py``."""
        ini_path, apps_root = stripped_tree
        versions = apps_root / "alpha" / "migrations" / "versions"
        versions.mkdir(parents=True)
        before = ini_path.read_text(encoding="utf-8")

        with pytest.raises(
            sync_alembic_version_locations.VersionLocationsRemovalError
        ) as excinfo:
            sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)

        assert excinfo.value.removed == (
            "%(here)s/app/extensions/apps/alpha/migrations/versions",
        )
        assert ini_path.read_text(encoding="utf-8") == before

    def test_does_not_refuse_when_versions_is_empty_of_revisions(self, stripped_tree):
        """Keep the entry when the walk still finds the app with an empty versions/."""
        ini_path, apps_root = stripped_tree
        _migration_plugin(apps_root, "alpha")

        assert sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)
        assert "app/extensions/apps/alpha/migrations/versions" in ini_path.read_text(
            encoding="utf-8"
        )

    def test_allowing_removals_under_check_still_writes_nothing(self, stripped_tree):
        """Keep ``check`` a dry run even when removals are permitted."""
        ini_path, apps_root = stripped_tree
        before = ini_path.read_text()

        assert not sync_alembic_version_locations.sync_alembic_ini(
            ini_path, apps_root, check=True, allow_removals=True
        )
        assert ini_path.read_text() == before

    def test_malformed_ini_is_still_reported_as_malformed(self, stripped_tree):
        """Keep the parse errors ahead of the removal check."""
        ini_path, apps_root = stripped_tree
        ini_path.write_text(
            _MINIMAL_INI.replace(
                "version_locations = %(here)s/app/extensions/migrations/versions\n",
                "version_locations = %(here)s/app/extensions/migrations/versions:\n"
                "    %(here)s/app/extensions/apps/stale/migrations/versions\n",
            )
        )

        with pytest.raises(ValueError, match="single line"):
            sync_alembic_version_locations.sync_alembic_ini(ini_path, apps_root)


class TestRemovalRefusalCli:
    """Cover how the CLI surfaces a refused removal."""

    def test_exits_non_zero_naming_the_removed_entries(self, stripped_tree, capsys):
        """Print the pruned entries and every way out, not a traceback."""
        ini_path, apps_root = stripped_tree
        before = ini_path.read_text(encoding="utf-8")

        assert (
            sync_alembic_version_locations.main(
                ["--ini", str(ini_path), "--apps-root", str(apps_root)]
            )
            == 1
        )
        err = capsys.readouterr().err
        assert "app/extensions/apps/alpha/migrations/versions" in err
        assert "--allow-removals" in err
        assert "upgrade heads" in err
        assert "contributes no migration scripts" in err
        assert "Traceback" not in err
        assert ini_path.read_text(encoding="utf-8") == before

    def test_check_exits_non_zero_on_a_pruning_tree(self, stripped_tree, capsys):
        """Fail ``--check`` when entries would be pruned, without claiming a refusal.

        ``--check`` never writes, so the remedy that clears a real refusal —
        re-running with ``--allow-removals`` — clears nothing here. The message
        has to name the invocation that does perform the removal.
        """
        ini_path, apps_root = stripped_tree

        assert (
            sync_alembic_version_locations.main(
                ["--check", "--ini", str(ini_path), "--apps-root", str(apps_root)]
            )
            == 1
        )
        err = capsys.readouterr().err
        assert "app/extensions/apps/alpha/migrations/versions" in err
        assert "regenerating would remove" in err
        assert "refusing to remove" not in err
        assert "without `--check`" in err

    def test_opt_in_flag_writes_and_exits_zero(self, stripped_tree):
        """Let ``--allow-removals`` complete the removing write from the CLI."""
        ini_path, apps_root = stripped_tree

        assert (
            sync_alembic_version_locations.main(
                [
                    "--allow-removals",
                    "--ini",
                    str(ini_path),
                    "--apps-root",
                    str(apps_root),
                ]
            )
            == 0
        )
        remaining = ini_path.read_text()
        assert "app/extensions/migrations/versions" in remaining
        assert "app/extensions/apps/alpha/migrations/versions" not in remaining
