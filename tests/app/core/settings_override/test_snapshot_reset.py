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

"""Pin that the autouse snapshot reset covers every wired override proxy."""

import os
import subprocess
import sys
from pathlib import Path

from app.core.alerts.config import alert_settings
from app.core.config import settings
from app.extensions.config import extensions_settings
from app.extensions.settings_override import build_extensions_override_proxies
from app.extensions.snippets.config import snippets_settings
from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.celery import build_tasks_override_proxies
from app.tasks.config import tasks_settings
from tests.app.conftest import wired_override_proxies

REPO_ROOT = Path(__file__).resolve().parents[4]
_LEAK_PAIRS = Path(__file__).with_name("test_snapshot_reset_leak_pairs.py")

#: Variables the child run must not inherit. ``-o addopts=`` clears the ini
#: option but not ``PYTEST_ADDOPTS``, which the pre-push gate uses to inject its
#: checkpoint plugin; the child would then deselect every node id the outer run
#: already verified, collect nothing, and exit 5 on a resumed push.
_INHERITED_PYTEST_VARS = ("PYTEST_ADDOPTS", "EXTENSIONS_PREPUSH_CHECKPOINT")


def test_wired_override_proxies_covers_both_registries() -> None:
    """Fail when a registry proxy is missing from the autouse reset set.

    A newly activated app that declares ``APP_OWNED_SETTINGS_CLASSES`` must
    appear here the moment it is activated; otherwise snapshots leak across
    tests in the same xdist worker again.
    """
    expected = {
        id(entry.proxy)
        for registry in (
            build_extensions_override_proxies(),
            build_tasks_override_proxies(),
        )
        for entry in registry.values()
    }
    wired_ids = {id(proxy) for proxy in wired_override_proxies()}
    assert wired_ids == expected
    # The six proxies the hand-written reset covered before SEP-2161.
    for proxy in (
        settings,
        extensions_settings,
        tasks_settings,
        snippets_settings,
        alert_settings,
        anonymizer_settings,
    ):
        assert id(proxy) in wired_ids


def test_app_owned_proxy_snapshots_do_not_leak_across_tests() -> None:
    """Publish then check each app-owned proxy in one ``-n0`` child process.

    ``xdist_group`` only keeps tests on one worker under ``--dist=loadgroup``.
    The Makefile defaults to ``worksteal``, so an in-suite publisher/checker
    pair can split across workers: the checker sees a fresh empty snapshot and
    passes even when the autouse reset no longer covers that proxy. Spawning
    the pairs under ``-n0`` forces one process and definition order, so each
    publisher is immediately followed by its checker against the real fixture
    lifecycle.

    The pairs live in ``test_snapshot_reset_leak_pairs.py``, which
    ``collect_ignore`` keeps out of directory collection so xdist never runs
    a checker alone; the child still collects them via an explicit path.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_LEAK_PAIRS.relative_to(REPO_ROOT)),
            "-n0",
            "--no-cov",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=REPO_ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in _INHERITED_PYTEST_VARS
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
