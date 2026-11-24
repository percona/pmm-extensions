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
"""Pin the shared side-car runtime contracts through their public entrypoints."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.fernet import Fernet

from app import BASE_DIR
from sidecar import encryption_key, grafana_service_account
from tests.sidecar.conftest import CONTAINERFILE

EXPECTED_RETRY_INTERVAL_SECONDS = 3.0

IMAGE_IMPORT_PATH = (
    "import os, runpy, sys;"
    "sys.path[:] = [p for p in sys.path"
    " if not os.path.exists(os.path.join(p, 'sidecar', '__init__.py'))];"
    "runpy.run_path(sys.argv[1], run_name='__main__')"
)
"""Start a script with no installed ``sidecar`` package on the import path.

The image ships ``runtime.py`` under a bare ``sidecar/`` directory and no
``__init__.py``, so it resolves as a namespace package beside the scripts. A
checkout is an editable install whose root carries ``sidecar/__init__.py``, and
a regular package anywhere on the path wins over a namespace portion ahead of
it. Without this the copied file is never the one imported and the layout goes
unchecked.
"""


@pytest.mark.parametrize("helper", [encryption_key, grafana_service_account])
class TestStateDirectory:
    """Preserve state-directory resolution and the retry cadence."""

    def test_unset_uses_image_default(
        self, helper: ModuleType, monkeypatch: pytest.MonkeyPatch
    ):
        """Use the original default when no directory is configured."""
        monkeypatch.delenv("EXTENSIONS_STATE_DIR", raising=False)

        assert Path("/home/extensions/state") == helper.DEFAULT_STATE_DIR
        assert helper.state_dir() == helper.DEFAULT_STATE_DIR

    @pytest.mark.parametrize("configured", ["", " ", "\t\n"])
    def test_blank_uses_image_default(
        self, helper: ModuleType, monkeypatch: pytest.MonkeyPatch, configured: str
    ):
        """Treat empty and whitespace-only directories as unconfigured."""
        monkeypatch.setenv("EXTENSIONS_STATE_DIR", configured)

        assert helper.state_dir() == helper.DEFAULT_STATE_DIR

    @pytest.mark.parametrize(
        "configured", ["/configured/state", "relative/state", " /padded/state "]
    )
    def test_configured_path_is_not_stripped(
        self, helper: ModuleType, monkeypatch: pytest.MonkeyPatch, configured: str
    ):
        """Preserve whitespace in a nonblank configured path."""
        monkeypatch.setenv("EXTENSIONS_STATE_DIR", configured)

        assert helper.state_dir() == Path(configured)

    def test_retry_cadence(self, helper: ModuleType):
        """Keep the existing retry interval."""
        assert helper.RETRY_INTERVAL_SECONDS == EXPECTED_RETRY_INTERVAL_SECONDS


@pytest.mark.parametrize(
    ("warn", "prefix"),
    [
        (encryption_key.warn, "[encryption-key]"),
        (grafana_service_account.warn, "[grafana-mint]"),
    ],
)
def test_warning_keeps_its_prefix_and_leaves_stdout_empty(
    warn: Callable[[str], None], prefix: str, capsys: pytest.CaptureFixture[str]
):
    """Keep each script's diagnostic label off its credential channel."""
    warn("diagnostic")

    assert capsys.readouterr() == ("", f"{prefix} diagnostic\n")


@pytest.mark.parametrize(
    ("script", "stdout_template", "returncode"),
    [("encryption_key.py", "{key}\n", 0), ("grafana_service_account.py", "", 1)],
)
def test_standalone_script_uses_the_shipped_runtime(
    tmp_path: Path, script: str, stdout_template: str, returncode: int
):
    """Run the image's copied scripts without the checkout on the import path.

    The directory holds no settings profile, so the Grafana helper ends on its
    own unresolved-settings diagnostic. Reaching that line, rather than an
    import error, is what shows the shipped runtime module was found.
    """
    sources = {
        "./sidecar/encryption_key.py",
        "./sidecar/grafana_service_account.py",
        "./sidecar/runtime.py",
    }
    copied: set[str] = set()
    for line in CONTAINERFILE.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields and fields[0] == "COPY" and fields[-2] in sources:
            source, destination = fields[-2:]
            target = tmp_path / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BASE_DIR / source, target)
            copied.add(source)
    assert copied == sources
    (tmp_path / "app").symlink_to(BASE_DIR / "app", target_is_directory=True)
    key = Fernet.generate_key().decode("ascii")
    result = subprocess.run(
        [sys.executable, "-c", IMAGE_IMPORT_PATH, str(tmp_path / script)],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": "",
            "ENCRYPTION_KEY": key,
            "AUTH__PROVIDER": "{}",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == returncode, result.stderr
    assert result.stdout == stdout_template.format(key=key)
    assert "Traceback" not in result.stderr
