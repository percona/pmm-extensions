#!/usr/bin/env python3
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

"""Smoke-run the xtrabackup payload variants against their declared requirements.

CI never executes a payload, so a generated variant could ship importing a module
its task never installs, or referencing a name its own regions no longer define.
The generator's static guards catch the second; nothing catches the first, because
the requirement list lives in ``build_backup_spec`` and the import list lives in
the payload.

This harness closes that gap without a database host. For every upload selection
it asks ``build_backup_spec`` what the dispatcher would declare, builds a virtual
environment holding exactly that, and imports the variant inside it, so a missing
``boto3`` fails here rather than mid-backup on a customer host. The ``rsync`` phase
then drives the variant's own ``RsyncUploadProvider`` over real ``rsync(1)`` into a
temporary directory.

What still needs a host: a real XtraBackup run per provider, and the S3/GCS
uploads, which need credentials and a bucket. ``--phase host`` prints those steps
rather than pretending to have run them.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import venv
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_DIR = REPO_ROOT / "app/extensions/apps/mysql_backups"


class Dispatcher(NamedTuple):
    """Namespace the app-side callables this harness measures the payloads against.

    :param selections: Enumerate every upload selection in provider order.
    :param spec_for: Build the run-python spec a task would dispatch for a selection.
    """

    selections: Callable[[], list[tuple[str, ...]]]
    spec_for: Callable[..., object]


def load_dispatcher() -> Dispatcher:
    """Return the app-side selection and spec builders this harness drives.

    Executing a script puts its own ``scripts/`` directory on ``sys.path`` but not
    the repo root, and ``app`` is not an installed distribution, so a bare
    ``import app`` finds nothing. Prepending ``REPO_ROOT`` here rather than at
    module scope keeps the imports out of the module header, where they would read
    as ordinary top-level imports that happen to need a side effect first.

    :return: The selection enumerator and the spec builder.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from app.extensions.apps.mysql_backups.payload_variants import (  # noqa: PLC0415
        selections,
    )
    from tests.app.extensions.apps.mysql_backups.variant_specs import (  # noqa: PLC0415
        spec_for,
    )

    return Dispatcher(selections=selections, spec_for=spec_for)


class Dispatched(NamedTuple):
    """Namespace what the dispatcher declares for one upload selection.

    :param name: The variant filename the task would carry.
    :param requirements: The pip requirement lines the task would install.
    """

    name: str
    requirements: list[str]


def dispatched(dispatcher: Dispatcher, upload: tuple[str, ...]) -> Dispatched:
    """Return the payload filename and pip requirements the dispatcher declares.

    Derived from ``build_backup_spec`` rather than restated here, so the harness
    verifies the payload against what a task would really install.

    :param dispatcher: The app-side builders from :func:`load_dispatcher`.
    :param upload: The upload providers the form selects, in provider order.
    :return: The variant filename and its requirement lines.
    """
    spec = dispatcher.spec_for([provider.upper() for provider in upload])
    requirements = [line for line in spec.requirements.splitlines() if line.strip()]
    return Dispatched(spec.payload.rsplit("/", 1)[-1], requirements)


def make_env(workdir: Path, requirements: list[str]) -> Path:
    """Return the interpreter of a virtual environment holding exactly ``requirements``.

    Environments are keyed on the requirement set and reused, since the eight
    selections only ever produce two of them.

    :param workdir: The directory the environments are built under.
    :param requirements: The pip requirement lines to install.
    :return: Path to the environment's ``python``.
    :raises RuntimeError: When pip fails to install the requirements.
    """
    key = "\n".join(sorted(requirements)).encode("utf-8")
    env_dir = workdir / f"env-{hashlib.sha256(key).hexdigest()[:12]}"
    python = env_dir / "bin" / "python"
    if python.exists():
        return python
    venv.create(env_dir, with_pip=True, clear=True)
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", *requirements],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pip install {requirements} failed:\n{proc.stderr}")
    return python


def run_driver(
    python: Path, workdir: Path, driver: str, payload: Path, extra: list[str]
) -> tuple[int, str]:
    """Run a driver script under ``python`` against ``payload``.

    :param python: The interpreter to run under.
    :param workdir: The directory the driver file is written into.
    :param driver: The driver source to execute.
    :param payload: The variant the driver imports.
    :param extra: Further arguments appended after the payload path.
    :return: The driver's exit status and its combined output.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", dir=workdir, delete=False
    ) as handle:
        handle.write(driver)
        script = Path(handle.name)
    try:
        proc = subprocess.run(
            [str(python), str(script), str(payload), *extra],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script.unlink()
    return proc.returncode, (proc.stdout + proc.stderr).strip()


#: Loads a payload under a module name of its own. The variants are extensionless,
#: so an explicit source loader is the only way to import one.
_LOAD_PAYLOAD = """
import importlib.util
import sys
from importlib.machinery import SourceFileLoader

_name = "smoke_payload"
_spec = importlib.util.spec_from_file_location(
    _name, sys.argv[1], loader=SourceFileLoader(_name, sys.argv[1])
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_name] = module
_spec.loader.exec_module(module)
"""

IMPORT_DRIVER = (
    '"""Import the payload, exercise its CLI parser, and report the providers it carries."""'
    + _LOAD_PAYLOAD
    + """
import json

sys.argv = ["payload", "--config", "/tmp/does-not-exist.yml"]
options = module.parse_args()
assert options.config == "/tmp/does-not-exist.yml", options
print(json.dumps({"providers": sorted(
    name for name in ("RsyncUploadProvider", "S3UploadProvider", "GSUploadProvider")
    if hasattr(module, name)
)}))
"""
)

RSYNC_DRIVER = (
    '"""Drive the payload\'s own RsyncUploadProvider over real rsync into a temp tree."""'
    + _LOAD_PAYLOAD
    + """
from pathlib import Path

workdir = Path(sys.argv[2])
source = workdir / "backup"
dest = workdir / "remote"
logs = workdir / "logs"
for directory in (source, dest, logs):
    directory.mkdir(parents=True, exist_ok=True)
(source / "xtrabackup_info").write_text("smoke\\n", encoding="utf-8")

provider = module.RsyncUploadProvider(
    {
        "HOST": "localhost",
        "ALIAS": "smoke",
        "RSYNC_PATH": str(dest),
        "BACKUP_TYPE": "X",
        "LOGGING_DIR": str(logs),
        "UPLOAD_QUIET": True,
    },
    20,
)
returned = provider.upload(str(source), "full")
landed = sorted(path.name for path in (dest / source.name).rglob("*"))
assert landed == ["xtrabackup_info"], (returned, landed)

try:
    module.RsyncUploadProvider({"RSYNC_PATH": None, "LOGGING_DIR": str(logs)}, 20)
except module.BackupError as exc:
    assert "RSYNC_PATH is not set" in str(exc), exc
else:
    raise AssertionError("missing RSYNC_PATH did not raise BackupError")
print(returned)
"""
)

HOST_STEPS = """\
Remaining steps need a database host with XtraBackup installed, and credentials
for the object stores. Run each from the PMM Extensions UI so the dispatcher picks the
variant, then confirm on the host:

  1. XtraBackup + Rsync only
     - task's payload file is xtrabackup_rsync_payload
     - on the host: grep -c boto3 <task dir>/requirements.txt  -> 0
     - backup lands under the configured RSYNC_PATH
  2. XtraBackup + S3 only            -> xtrabackup_s3_payload, boto3 installed,
                                        object present in the bucket
  3. XtraBackup + Google Cloud only  -> xtrabackup_gsutil_payload, object present
  4. XtraBackup, no upload           -> xtrabackup_noupload_payload, no boto3,
                                        backup completes locally
  5. XtraBackup, all three providers -> xtrabackup_payload (the canonical source)

For each, the dispatched payload is visible in the task's Nomad payload
reference, and on the host under the allocation's local task directory.
"""


def phase_deps(dispatcher: Dispatcher, workdir: Path) -> int:
    """Import every variant inside an environment holding only its declared requirements.

    :param dispatcher: The app-side builders from :func:`load_dispatcher`.
    :param workdir: The directory the environments are built under.
    :return: 0 when every variant imports, 1 otherwise.
    """
    failures = 0
    for upload in dispatcher.selections():
        declared = dispatched(dispatcher, upload)
        python = make_env(workdir, declared.requirements)
        status, output = run_driver(
            python, workdir, IMPORT_DRIVER, PAYLOAD_DIR / declared.name, []
        )
        label = ",".join(upload) or "(none)"
        if status != 0:
            failures += 1
            print(f"FAIL  {declared.name}  [{label}]")
            print(f"      requirements: {' '.join(declared.requirements)}")
            print("      " + output.replace("\n", "\n      "))
            continue
        carried = json.loads(output)["providers"]
        print(f"ok    {declared.name}  [{label}]  providers={','.join(carried) or '-'}")
    return 1 if failures else 0


def phase_rsync(dispatcher: Dispatcher, workdir: Path) -> int:
    """Upload a fake backup with each rsync-carrying variant's own provider.

    :param dispatcher: The app-side builders from :func:`load_dispatcher`.
    :param workdir: The directory the environments and temp trees are built under.
    :return: 0 when every rsync-carrying variant uploads, 1 otherwise.
    """
    if not Path("/usr/bin/rsync").exists():
        print("SKIP  /usr/bin/rsync is absent; the payload hardcodes that path")
        return 0
    failures = 0
    for upload in dispatcher.selections():
        if "rsync" not in upload:
            continue
        declared = dispatched(dispatcher, upload)
        tree = workdir / f"rsync-{declared.name}"
        shutil.rmtree(tree, ignore_errors=True)
        tree.mkdir(parents=True)
        python = make_env(workdir, declared.requirements)
        status, output = run_driver(
            python, workdir, RSYNC_DRIVER, PAYLOAD_DIR / declared.name, [str(tree)]
        )
        if status != 0:
            failures += 1
            print(f"FAIL  {declared.name}")
            print("      " + output.replace("\n", "\n      "))
            continue
        print(f"ok    {declared.name}  uploaded to {output}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Run the requested smoke phases over the shipped variants.

    :param argv: CLI arguments (defaults to ``sys.argv[1:]``).
    :return: Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("deps", "rsync", "host", "all"),
        default="all",
        help="Which smoke phase to run (default: every phase runnable here).",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="Reuse environments across runs instead of a throwaway directory.",
    )
    args = parser.parse_args(argv)

    if args.phase == "host":
        print(HOST_STEPS)
        return 0

    dispatcher = load_dispatcher()
    temp = None
    if args.workdir:
        workdir = args.workdir
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        temp = tempfile.mkdtemp(prefix="xtrabackup-smoke-")
        workdir = Path(temp)
    try:
        status = 0
        if args.phase in ("deps", "all"):
            print("== phase deps: import each variant under its declared requirements")
            status |= phase_deps(dispatcher, workdir)
        if args.phase in ("rsync", "all"):
            print("== phase rsync: real rsync upload via each variant's own provider")
            status |= phase_rsync(dispatcher, workdir)
        if args.phase == "all":
            print("\n== phase host: not run here")
            print(HOST_STEPS)
        return status
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
