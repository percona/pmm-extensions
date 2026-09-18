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

"""Exercise the ``pbm config`` storage-apply step, which the config payload owns alone.

``pbm config --file`` replaces PBM's cluster-wide configuration rather than merging
into it, so applying it from a backup rewrote deployment-wide state on every run and
blanked whatever the task form could not express -- S3 credentials among them. Only
``pbm_config_payload`` applies config now; the logical, physical and incremental
payloads just take backups.

These tests exec the real payloads with ``subprocess.Popen`` stubbed to capture the
full command sequence, assert the config payload's generated ``_apply_pbm_config``
region matches the one canonical source in ``pbm_creds_common.py``, and assert the
backup payloads carry neither that region nor a call to it.
"""

import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

from app.extensions.apps.backup_mongo import pbm_creds_common
from app.extensions.apps.backup_mongo.models import BackupCreate, BackupType
from app.extensions.apps.backup_mongo.pbm_creds_common import (
    CONFIG_APPLY_BEGIN,
    CONFIG_APPLY_END,
    config_apply_source,
)
from app.extensions.apps.backup_mongo.spec import (
    BackupMongoResolved,
    build_backup_mongo_spec,
)
from tests.app.extensions.apps.backup_mongo.pbm_payload_exec import (
    FakePopen,
    run_payload,
)

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[5]
_GEN_SCRIPT = _PROJECT_ROOT / "scripts" / "gen_pbm_payloads.py"
_gen_spec = importlib.util.spec_from_file_location("gen_pbm_payloads", _GEN_SCRIPT)
assert _gen_spec is not None
assert _gen_spec.loader is not None
gen_pbm_payloads = importlib.util.module_from_spec(_gen_spec)
sys.modules["gen_pbm_payloads"] = gen_pbm_payloads
_gen_spec.loader.exec_module(gen_pbm_payloads)

_APP_DIR = pathlib.Path(__file__).parents[5] / "app/extensions/apps/backup_mongo"
_BACKUP_PAYLOADS = {
    "logical": _APP_DIR / "pbm_logical_payload",
    "physical": _APP_DIR / "pbm_physical_payload",
}
# The only payload that carries the config-apply region, and so the only one the
# generator syncs it into.
_CONFIG_PAYLOADS = {"config": _APP_DIR / "pbm_config_payload"}
_ALL_PAYLOADS = {
    **_CONFIG_PAYLOADS,
    **_BACKUP_PAYLOADS,
}
_PARAMETRIZE_BACKUP = pytest.mark.parametrize("payload", ["logical", "physical"])
# Arbitrary non-zero code PBM reports when it rejects the applied config.
_PBM_REJECT_CODE = 3

_S3_CONFIG = {
    "storage": {
        "type": "s3",
        "s3": {"bucket": "backups", "region": "eu-west-1"},
    }
}


def _exec_payload_capture_cmds(
    payload: str,
    config: dict | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    config_ret: int = 0,
    captured: list[list[str]] | None = None,
    set_task_dir: bool = True,
    popen_error: Exception | None = None,
) -> list[list[str]]:
    """Exec a payload with a stubbed ``Popen`` and capture every command it runs.

    :param payload: Payload key (``config`` / ``logical`` / ``physical``).
    :param config: Config dict serialized to ``NOMAD_META_CONFIG``, or None to omit it.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest tmp_path fixture for HOME, the task dir, and creds.
    :param config_ret: Return code the stub reports for a ``pbm config`` command.
    :param captured: Out-parameter the captured commands are appended to; supply one
        to read the commands back on the exit paths where the payload raises
        ``SystemExit`` before this helper can return. A fresh list is used when None.
    :param set_task_dir: Whether to set ``NOMAD_TASK_DIR``; pass False to exercise the
        missing-task-dir abort path.
    :param popen_error: Exception the stubbed ``Popen`` raises on construction, to
        exercise the "``pbm`` binary cannot be run" path. None runs normally.
    :return: The list of argument lists passed to ``subprocess.Popen``, in order.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    if set_task_dir:
        monkeypatch.setenv("NOMAD_TASK_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("NOMAD_TASK_DIR", raising=False)
    (tmp_path / ".mongodb_uri").write_text("mongodb://localhost:27017/")

    if config is None:
        monkeypatch.delenv("NOMAD_META_CONFIG", raising=False)
    else:
        monkeypatch.setenv("NOMAD_META_CONFIG", yaml.safe_dump(config))

    if captured is None:
        captured = []

    def _stub(cmd: list[str], *args: object, **kwargs: object) -> FakePopen:
        return FakePopen(
            cmd,
            *args,
            captured=captured,
            returncode=lambda c: config_ret if c[:2] == ["pbm", "config"] else 0,
            construction_error=popen_error,
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", _stub)

    run_payload(_ALL_PAYLOADS[payload])
    return captured


class TestBackupsDoNotRewriteConfig:
    """Assert the backup payloads never write PBM config, whatever the task carries.

    PBM config is cluster-wide, so a backup that applied it rewrote state belonging to
    the whole deployment on every run -- and blanked any field the form cannot express,
    S3 credentials among them, because ``pbm config --file`` replaces rather than
    merges. Applying config is now the config payload's job alone (its own tab in the
    UI), so these assert the *absence* of that behaviour.
    """

    @_PARAMETRIZE_BACKUP
    def test_s3_storage_does_not_trigger_a_config_apply(
        self, payload: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        """Run only ``pbm backup``, even when the task config carries S3 storage."""
        cmds = _exec_payload_capture_cmds(payload, _S3_CONFIG, monkeypatch, tmp_path)

        assert cmds == [["pbm", "backup", "--type", payload, "--wait"]]
        assert not any(cmd[:2] == ["pbm", "config"] for cmd in cmds)

    @_PARAMETRIZE_BACKUP
    def test_no_config_apply_without_storage(
        self, payload: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        """Skip the config apply and run only ``pbm backup`` when no storage is set."""
        cmds = _exec_payload_capture_cmds(
            payload, {"backup": {"compression": "gzip"}}, monkeypatch, tmp_path
        )

        assert cmds == [
            [
                "pbm",
                "backup",
                "--type",
                payload,
                "--wait",
                "--compression",
                "gzip",
            ]
        ]

    @_PARAMETRIZE_BACKUP
    def test_absent_config_runs_backup_only(
        self, payload: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        """Run only ``pbm backup`` when NOMAD_META_CONFIG is absent."""
        cmds = _exec_payload_capture_cmds(payload, None, monkeypatch, tmp_path)

        assert cmds == [["pbm", "backup", "--type", payload, "--wait"]]


class TestRealSpecThreadsStorageIntoConfigFile:
    """Assert the real spec output applies its S3 storage through the payload's config file."""

    @pytest.mark.parametrize("payload", ["config"])
    def test_s3_storage_reaches_applied_config_file(
        self, payload: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        """Write the form's bucket/region into the file handed to ``pbm config --file``."""
        form = BackupCreate(
            task_name="mongo-backup",
            hostname="mongo-host",
            service_id=1,
            backup_type=BackupType.PBM_CONFIG,
            pitr_compression="gzip",
            storage_type="s3",
            storage_s3_region="eu-west-1",
            storage_s3_bucket="backups",
            storage_s3_prefix="mongo",
            storage_s3_endpoint_url="https://s3.example.com",
        )
        config_yaml = build_backup_mongo_spec(form, BackupMongoResolved()).data["meta"][
            "config"
        ]
        config = yaml.safe_load(config_yaml)

        _exec_payload_capture_cmds(payload, config, monkeypatch, tmp_path)

        applied = yaml.safe_load((tmp_path / "script_config").read_text())
        assert applied["storage"]["s3"]["bucket"] == "backups"
        assert applied["storage"]["s3"]["region"] == "eu-west-1"
        # PMM Extensions only keys must not leak into the PBM config file; ``pbm config``
        # rejects the ``credentials_path`` the payload reads directly.
        assert "credentials_path" not in applied


class TestApplyHelperNoDrift:
    """Assert the ``_apply_pbm_config`` helper tracks one canonical generated source."""

    def _region(self, path: pathlib.Path) -> str:
        """Return the config-apply region carried between the markers in ``path``."""
        lines = path.read_text().split("\n")
        begin = lines.index(CONFIG_APPLY_BEGIN)
        end = lines.index(CONFIG_APPLY_END, begin + 1)
        return "\n".join(lines[begin + 1 : end]).strip("\n")

    @pytest.mark.parametrize("payload", sorted(_CONFIG_PAYLOADS))
    def test_region_matches_canonical(self, payload: str) -> None:
        """Require each payload's ``_apply_pbm_config`` to equal the canonical source."""
        assert self._region(_CONFIG_PAYLOADS[payload]) == config_apply_source()

    @pytest.mark.parametrize("payload", sorted(_BACKUP_PAYLOADS))
    def test_backup_payloads_carry_no_config_apply(self, payload: str) -> None:
        """Keep the config-apply region out of the payloads that only take backups.

        The generator syncs a region into whichever payloads carry its BEGIN marker, so
        dropping the marker is what opts a payload out. Asserting on the helper name too
        catches a call left behind without its definition.
        """
        source = _BACKUP_PAYLOADS[payload].read_text()

        assert CONFIG_APPLY_BEGIN not in source
        assert "_apply_pbm_config" not in source

    def test_check_mode_reports_no_drift(self) -> None:
        """Accept the checked-in config-apply region under ``gen_pbm_payloads.py --check``."""
        assert gen_pbm_payloads.main(["--check"]) == 0


class TestApplyPbmConfigCanonical:
    """Exercise the importable ``_apply_pbm_config`` helper directly.

    The payload copies are byte-identical to this canonical source (guarded by
    ``TestApplyHelperNoDrift``), so calling the module helper covers the same
    branches at the single source of truth.
    """

    @staticmethod
    def _stub_popen(
        monkeypatch: pytest.MonkeyPatch,
        captured: list[list[str]],
        *,
        ret_code: int = 0,
    ) -> None:
        """Patch ``subprocess.Popen`` to record commands and report ``ret_code``."""
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda cmd, *a, **kw: FakePopen(
                cmd, *a, captured=captured, returncode=ret_code, **kw
            ),
        )

    def test_writes_config_file_and_runs_pbm_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        """Write the config (minus PMM Extensions only/None keys) and run ``pbm config --file``."""
        monkeypatch.setenv("NOMAD_TASK_DIR", str(tmp_path))
        captured: list[list[str]] = []
        self._stub_popen(monkeypatch, captured)

        pbm_creds_common._apply_pbm_config(
            {
                "storage": {"type": "s3", "s3": {"bucket": "backups"}},
                "credentials_path": "/creds/uri",
                "pitr": None,
            }
        )

        assert captured == [["pbm", "config", "--file", f"{tmp_path}/script_config"]]
        written = yaml.safe_load((tmp_path / "script_config").read_text())
        assert written == {"storage": {"type": "s3", "s3": {"bucket": "backups"}}}
        assert "credentials_path" not in written
        assert "pitr" not in written

    def test_strips_selective_backup_keys_before_pbm_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        """Strip ``namespaces`` / ``withUsersAndRoles`` from the backup block.

        Those keys drive ``pbm backup --ns`` in runners but are invalid PBM
        config-file keys, so ``pbm config --file`` must not see them.
        """
        monkeypatch.setenv("NOMAD_TASK_DIR", str(tmp_path))
        captured: list[list[str]] = []
        self._stub_popen(monkeypatch, captured)

        pbm_creds_common._apply_pbm_config(
            {
                "storage": {"type": "filesystem", "filesystem": {"path": "/tmp/pbm"}},
                "backup": {
                    "compression": "gzip",
                    "namespaces": "db1.*,db2.coll",
                    "withUsersAndRoles": True,
                },
            }
        )

        assert captured == [["pbm", "config", "--file", f"{tmp_path}/script_config"]]
        written = yaml.safe_load((tmp_path / "script_config").read_text())
        assert written == {
            "storage": {"type": "filesystem", "filesystem": {"path": "/tmp/pbm"}},
            "backup": {"compression": "gzip"},
        }
        assert "namespaces" not in written["backup"]
        assert "withUsersAndRoles" not in written["backup"]

    def test_exits_when_task_dir_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """Exit 1 when NOMAD_TASK_DIR is unset before touching ``pbm``."""
        monkeypatch.delenv("NOMAD_TASK_DIR", raising=False)

        with pytest.raises(SystemExit) as exc:
            pbm_creds_common._apply_pbm_config({"storage": {"type": "s3"}})

        assert exc.value.code == 1
        assert "cannot write the PBM config file" in capsys.readouterr().err

    def test_exits_with_pbm_return_code_on_rejection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture,
    ):
        """Propagate PBM's non-zero exit code and emit an actionable error."""
        monkeypatch.setenv("NOMAD_TASK_DIR", str(tmp_path))
        self._stub_popen(monkeypatch, [], ret_code=_PBM_REJECT_CODE)

        with pytest.raises(SystemExit) as exc:
            pbm_creds_common._apply_pbm_config({"storage": {"type": "s3"}})

        assert exc.value.code == _PBM_REJECT_CODE
        err = capsys.readouterr().err
        assert "storage configuration" in err
        assert "bucket/region/endpoint" in err

    def test_exits_when_pbm_binary_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
        capsys: pytest.CaptureFixture,
    ):
        """Exit 1 when ``subprocess.Popen`` raises OSError (pbm not executable)."""
        monkeypatch.setenv("NOMAD_TASK_DIR", str(tmp_path))
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda cmd, *a, **kw: FakePopen(
                cmd,
                *a,
                construction_error=OSError("No such file or directory: 'pbm'"),
                **kw,
            ),
        )

        with pytest.raises(SystemExit) as exc:
            pbm_creds_common._apply_pbm_config({"storage": {"type": "s3"}})

        assert exc.value.code == 1
        assert "Failed to run pbm config" in capsys.readouterr().err


class TestRegionExtraction:
    """Verify ``_region_between`` fails loudly when a generated-region marker is missing."""

    def test_missing_marker_raises(self) -> None:
        """Raise ValueError naming the missing marker when a marker line is absent."""
        with pytest.raises(ValueError, match="missing a generated-region marker"):
            pbm_creds_common._region_between("# NOPE BEGIN", "# NOPE END")
