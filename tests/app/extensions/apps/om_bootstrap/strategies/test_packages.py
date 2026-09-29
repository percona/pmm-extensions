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

"""Assert PackagesInstallStrategy plans the same steps and builds OS-correct actions."""

import base64
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from app.extensions.apps.om_bootstrap.dispatch import build_step_script
from app.extensions.apps.om_bootstrap.strategies import packages
from app.extensions.apps.om_bootstrap.strategies.packages import (
    _mongosh_eval,
    _mongosh_eval_command,
    CONFIG_PATH,
    KEY_FILE_PATH,
    OWNERSHIP_MARKER_PATH,
    PackagesInstallStrategy,
    PID_FILE_PATH,
)
from app.extensions.apps.om_bootstrap.strategy import (
    BootstrapSpec,
    InstallMethod,
    InstallStrategy,
    MemberConfig,
    OperatingSystem,
    StepAction,
)


def _rs_initiate_config(action: StepAction) -> dict:
    """Extract the ``rs.initiate({...})`` config object from a built shell command."""
    raw = action.command[-1]
    match = re.search(r"rs\.initiate\((\{.*\})\)", raw)
    assert match is not None, raw
    return json.loads(match.group(1))


SUPPORTED_OSES = [OperatingSystem.UBUNTU, OperatingSystem.ROCKY]

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


def _spec(os_: OperatingSystem, run_id: UUID | None = RUN_ID) -> BootstrapSpec:
    return BootstrapSpec(
        install_method=InstallMethod.PACKAGES,
        os=os_,
        mongodb_version="8.0",
        replica_set_name="rs-test",
        run_id=run_id,
        data_path="/var/lib/mongo",
        log_path="/var/log/mongodb/mongod.log",
        port=27017,
        bind_ip="0.0.0.0",
    )


#: Derived from the strategy itself, so the parametrized build tests below cover
#: exactly what the strategy plans; the plan tests pin the lists' contents.
STEP_NAMES = PackagesInstallStrategy().plan_steps(_spec(OperatingSystem.UBUNTU))
RUN_STEP_NAMES = PackagesInstallStrategy().plan_run_steps(_spec(OperatingSystem.UBUNTU))
ROLLBACK_STEP_NAMES = PackagesInstallStrategy().plan_rollback_steps(
    _spec(OperatingSystem.UBUNTU)
)
FINALIZE_STEP_NAMES = PackagesInstallStrategy().plan_finalize_steps(
    _spec(OperatingSystem.UBUNTU)
)

#: Every per-host step's own required ``params``, so a single parametrized test
#: can build every step without hand-listing which ones need what.
_STEP_PARAMS: dict[str, dict[str, str]] = {
    "distribute_keyfile": {"key_file_content": "test-keyfile-content"},
}

_SH = shutil.which("sh") or "/bin/sh"


def _body(command: list[str]) -> str:
    """Return the shell body of an ``["sh", "-c", body]`` action."""
    assert command[:2] == ["sh", "-c"]
    return command[2]


class TestPackagesInstallStrategyIsAnInstallStrategy:
    """Pin the structural-protocol contract, not just the concrete class."""

    def test_satisfies_the_protocol(self) -> None:
        """Accept this class wherever a caller programs against InstallStrategy."""
        assert isinstance(PackagesInstallStrategy(), InstallStrategy)


class TestPlanSteps:
    """Assert the step list is fixed and OS-independent for phase 1."""

    @pytest.mark.parametrize("os_", SUPPORTED_OSES)
    def test_returns_the_fixed_step_names_regardless_of_os(
        self, os_: OperatingSystem
    ) -> None:
        """Give Ubuntu and Rocky the same step names; only build_step branches on OS."""
        assert PackagesInstallStrategy().plan_steps(_spec(os_)) == [
            "pre_check",
            "configure_repository",
            "install_package",
            "distribute_keyfile",
            "configure_mongod",
            "start_service",
            "verify",
        ]


class TestBuildStep:
    """Assert build_step produces the right command per step and per OS."""

    @pytest.mark.parametrize("os_", SUPPORTED_OSES)
    @pytest.mark.parametrize("step_name", STEP_NAMES)
    def test_every_planned_step_builds_a_runnable_action(
        self, step_name: str, os_: OperatingSystem
    ) -> None:
        """Build a non-empty command with a real timeout for every planned name."""
        action = PackagesInstallStrategy().build_step(
            step_name, "node00", _spec(os_), params=_STEP_PARAMS.get(step_name)
        )

        assert action.command
        assert all(action.command)
        assert action.timeout_s > 0

    def test_unknown_step_name_raises(self) -> None:
        """Reject a name outside plan_steps' list: a programming error, not a no-op."""
        with pytest.raises(ValueError, match="not a PackagesInstallStrategy step"):
            PackagesInstallStrategy().build_step(
                "rs_initiate", "node00", _spec(OperatingSystem.UBUNTU)
            )

    def test_configure_repository_uses_apt_on_ubuntu(self) -> None:
        """Install percona-release's .deb via dpkg on Ubuntu."""
        action = PackagesInstallStrategy().build_step(
            "configure_repository", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "percona-release_latest.generic_all.deb" in command
        assert "dpkg -i" in command
        assert "psmdb-80" in command

    def test_configure_repository_uses_dnf_on_rocky(self) -> None:
        """Install percona-release's .rpm via dnf on Rocky."""
        action = PackagesInstallStrategy().build_step(
            "configure_repository", "node00", _spec(OperatingSystem.ROCKY)
        )

        command = " ".join(action.command)
        assert "percona-release-latest.noarch.rpm" in command
        assert "dnf install" in command
        assert "psmdb-80" in command

    def test_configure_repository_quotes_the_channel(self) -> None:
        """Pass the channel to the shell quoted, whatever the version string held."""
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={"mongodb_version": "8.0;touch /tmp/x"}
        )

        action = PackagesInstallStrategy().build_step(
            "configure_repository", "node00", spec
        )

        assert "percona-release setup -y 'psmdb-80;touch /tmp/x'" in _body(
            action.command
        )

    def test_configure_repository_accepts_a_full_patch_version(self) -> None:
        """Select the channel by major.minor only, not the full patch version.

        Exactly like the request field's own "only the major version selects
        the install source" comment (TriggerHostBootstrapRequest.mongodb_version)
        promises: a full patch version like "7.0.14" must not leak into the
        channel name. Confirmed against a live host: this used to produce the
        nonexistent channel "psmdb-7014" and configure_repository failed with
        "Specified repository does not exist".
        """
        spec = BootstrapSpec(
            install_method=InstallMethod.PACKAGES,
            os=OperatingSystem.ROCKY,
            mongodb_version="7.0.14",
            replica_set_name="rs-test",
            data_path="/var/lib/mongo",
            log_path="/var/log/mongodb/mongod.log",
            port=27017,
            bind_ip="0.0.0.0",
        )
        action = PackagesInstallStrategy().build_step(
            "configure_repository", "node00", spec
        )

        command = " ".join(action.command)
        assert "psmdb-70" in command
        assert "psmdb-7014" not in command

    def test_install_package_uses_apt_get_on_ubuntu(self) -> None:
        """Install the package through apt-get, not dnf, on Ubuntu."""
        action = PackagesInstallStrategy().build_step(
            "install_package", "node00", _spec(OperatingSystem.UBUNTU)
        )

        assert "apt-get install -y percona-server-mongodb" in " ".join(action.command)

    @pytest.mark.parametrize("os_", SUPPORTED_OSES)
    def test_install_package_claims_the_host_before_installing(
        self, os_: OperatingSystem
    ) -> None:
        """Claim the host for this run first, so a half-finished install rolls back."""
        action = PackagesInstallStrategy().build_step(
            "install_package", "node00", _spec(os_)
        )

        lines = _body(action.command).splitlines()
        assert lines[0] == f"printf '%s\\n' {RUN_ID} > {OWNERSHIP_MARKER_PATH}"
        assert "percona-server-mongodb" in lines[1]

    def test_install_package_requires_a_run_id(self) -> None:
        """Refuse to write an ownership marker no run's rollback could match."""
        with pytest.raises(ValueError, match="install_package requires spec.run_id"):
            PackagesInstallStrategy().build_step(
                "install_package", "node00", _spec(OperatingSystem.UBUNTU, None)
            )

    def test_install_package_writes_the_run_id_to_the_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leave exactly this run's id in the marker once the shell has run."""
        marker = tmp_path / "mongod.om-bootstrap"
        monkeypatch.setattr(packages, "OWNERSHIP_MARKER_PATH", str(marker))
        action = PackagesInstallStrategy().build_step(
            "install_package", "node00", _spec(OperatingSystem.UBUNTU)
        )
        bin_dir = _recording_bin(tmp_path, ["apt-get"])

        subprocess.run(
            [_SH, "-c", _body(action.command)],
            env={"PATH": str(bin_dir)},
            check=True,
        )

        assert marker.read_text() == f"{RUN_ID}\n"

    def test_install_package_uses_dnf_on_rocky(self) -> None:
        """Install the package through dnf, not apt-get, on Rocky."""
        action = PackagesInstallStrategy().build_step(
            "install_package", "node00", _spec(OperatingSystem.ROCKY)
        )

        assert "dnf install -y percona-server-mongodb" in " ".join(action.command)

    def test_configure_mongod_names_the_spec_replica_set(self) -> None:
        """Write this host's actual replica set name into mongod.conf."""
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        assert "replSetName: rs-test" in " ".join(action.command)

    def test_configure_mongod_creates_the_data_directory(self) -> None:
        """Create the data directory, without which mongod exits on first start."""
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "install -d -m 750 -o mongod -g mongod /var/lib/mongo" in command

    def test_configure_mongod_creates_the_log_directory(self) -> None:
        """Create the log directory, or mongod's control process exits on first start.

        ``Can't initialize rotatable log file :: caused by :: Failed to open
        <path>`` — confirmed against a real run where the package's own
        default log directory (/var/log/mongo) existed but the wizard's
        default log path (/var/log/mongodb/mongod.log) named a different one
        that nothing had created.
        """
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "install -d -m 750 -o mongod -g mongod /var/log/mongodb" in command

    def _install_d_guard(self, spec: BootstrapSpec) -> str:
        """Return just the two ``[ -d ... ] || install -d ...`` clauses, unquoted.

        Stops before the ``cat > ... <<'MONGOD_CONF'`` heredoc: that part needs
        no real host to exercise, and a shell function override for
        ``install`` (see the callers below) must not accidentally shadow
        anything the heredoc's own content might contain.
        """
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", spec
        )
        return action.command[-1].split(" && cat > ", 1)[0]

    def test_skips_install_d_when_the_directory_already_exists(
        self, tmp_path: Path
    ) -> None:
        """Skip ``install -d`` on a directory that already exists.

        Reapplying ``-m``/``-o``/``-g`` unconditionally would repoint an
        existing directory's mode and ownership on every run — for a
        ``log_path`` like ``/var/log/mongod.log``, that directory is
        ``/var/log`` itself, a shared directory this must never touch once
        it's already there.
        """
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={
                "data_path": str(tmp_path / "data"),
                "log_path": str(tmp_path / "data" / "mongod.log"),
            }
        )
        (tmp_path / "data").mkdir()
        guard = self._install_d_guard(spec)
        marker = tmp_path / "install-was-called"
        script = f"install() {{ : > {shlex.quote(str(marker))}; }}\n{guard}"

        result = subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr
        assert not marker.exists()

    def test_runs_install_d_when_the_directory_is_missing(self, tmp_path: Path) -> None:
        """Create a genuinely missing directory, the other half of the guard."""
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={
                "data_path": str(tmp_path / "does-not-exist"),
                "log_path": str(tmp_path / "log-missing" / "mongod.log"),
            }
        )
        guard = self._install_d_guard(spec)
        marker = tmp_path / "install-was-called"
        script = f"install() {{ : > {shlex.quote(str(marker))}; }}\n{guard}"

        result = subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr
        assert marker.exists()

    def test_configure_mongod_forks(self) -> None:
        """Make mongod fork, since mongod.service is Type=forking.

        Without fork: true it never satisfies systemd's readiness check and
        gets killed once TimeoutStartSec elapses.
        """
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "fork: true" in command
        assert f"pidFilePath: {PID_FILE_PATH}" in command

    def test_configure_mongod_sets_a_logpath(self) -> None:
        """Set a logpath, without which mongod refuses to start with fork: true.

        ``BadValue: --fork has to be used with --logpath or --syslog`` —
        confirmed against a real run.
        """
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "path: /var/log/mongodb/mongod.log" in command

    def test_configure_mongod_leaves_authorization_off(self) -> None:
        """Leave authorization off until the first user already exists.

        MongoDB's localhost exception is unreliable once a replica set already
        has more than one member — confirmed against a real run where every
        createUser attempt failed identically once the first one did.
        enable_auth (a finalize step) turns authorization on afterward, once
        create_pmm_monitoring_user has actually succeeded.
        """
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "replSetName" in command
        assert "authorization" not in command
        assert "keyFile" not in command

    def test_start_service_restarts_rather_than_enable_now(self) -> None:
        """Force a restart, since install_package may have started mongod already.

        ``enable --now`` is a no-op against a unit that is already active, so it
        would leave that process running on the package's default config, which
        carries no ``replication`` block, and never pick up
        ``configure_mongod``'s rewrite of ``mongod.conf``. Only an explicit
        ``restart`` guarantees the config just written actually takes effect.
        """
        action = PackagesInstallStrategy().build_step(
            "start_service", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "systemctl restart mongod" in command
        assert "--now" not in command

    def test_start_service_still_enables_mongod_at_boot(self) -> None:
        """Keep mongod enabled at boot, restart alone would not persist that."""
        action = PackagesInstallStrategy().build_step(
            "start_service", "node00", _spec(OperatingSystem.UBUNTU)
        )

        assert "systemctl enable mongod" in " ".join(action.command)

    def test_distribute_keyfile_requires_params(self) -> None:
        """Reject a missing keyFile as a programming error, not a blank file."""
        with pytest.raises(ValueError, match="key_file_content"):
            PackagesInstallStrategy().build_step(
                "distribute_keyfile", "node00", _spec(OperatingSystem.UBUNTU)
            )

    def test_distribute_keyfile_writes_the_given_content(self) -> None:
        """Carry exactly the content the caller supplied, base64-encoded."""
        content = "super-secret-keyfile-bytes"
        action = PackagesInstallStrategy().build_step(
            "distribute_keyfile",
            "node00",
            _spec(OperatingSystem.UBUNTU),
            params={"key_file_content": content},
        )

        body = _body(action.command)
        encoded = shlex.split(body)[2]
        assert base64.b64decode(encoded).decode() == content
        assert content not in body
        assert "install -m 400 -o mongod -g mongod /dev/stdin" in body

    def test_distribute_keyfile_content_cannot_break_out_of_the_command(
        self,
    ) -> None:
        """Decode content holding a heredoc delimiter or a command verbatim."""
        content = "abc\nMONGOD_KEYFILE\n$(touch /tmp/pwned)'\"\n"
        action = PackagesInstallStrategy().build_step(
            "distribute_keyfile",
            "node00",
            _spec(OperatingSystem.UBUNTU),
            params={"key_file_content": content},
        )
        decoder = _body(action.command).split(" | install ")[0]

        result = subprocess.run([_SH, "-c", decoder], capture_output=True, check=True)

        assert result.stdout.decode() == content

    def test_verify_goes_through_mongosh_eval_too(self) -> None:
        """Suppress the Atlas CLI probe in ``verify``, as every mongosh call must."""
        action = PackagesInstallStrategy().build_step(
            "verify", "node00", _spec(OperatingSystem.UBUNTU)
        )

        assert action == _mongosh_eval("db.adminCommand('ping').ok", 27017)


class TestPlanRunSteps:
    """Assert the run-level step list is fixed and OS-independent."""

    def test_returns_the_fixed_run_step_names(self) -> None:
        """Plan rs_initiate, then create_pmm_monitoring_user."""
        spec = _spec(OperatingSystem.UBUNTU)
        assert PackagesInstallStrategy().plan_run_steps(spec) == [
            "rs_initiate",
            "create_pmm_monitoring_user",
        ]


class TestBuildRunStep:
    """Assert build_run_step targets the seed host and rejects unknown names."""

    def test_unknown_run_step_name_raises(self) -> None:
        """Reject a per-host step name as a run step, like the reverse."""
        with pytest.raises(ValueError, match="not a PackagesInstallStrategy run step"):
            PackagesInstallStrategy().build_run_step(
                "pre_check", ["node00"], _spec(OperatingSystem.UBUNTU)
            )

    def test_rs_initiate_names_every_host_as_a_member(self) -> None:
        """Name every host in the run as an rs.initiate() member, not just the seed."""
        action = PackagesInstallStrategy().build_run_step(
            "rs_initiate", ["node00", "node01", "node02"], _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "node00:27017" in command
        assert "node01:27017" in command
        assert "node02:27017" in command
        assert "rs-test" in command

    def test_rs_initiate_defaults_a_host_with_no_member_config(self) -> None:
        """Give a host missing from spec.member_configs MongoDB's own defaults."""
        action = PackagesInstallStrategy().build_run_step(
            "rs_initiate", ["node00"], _spec(OperatingSystem.UBUNTU)
        )

        config = _rs_initiate_config(action)
        member = config["members"][0]
        assert member["priority"] == 1
        assert member["votes"] == 1
        assert member["hidden"] is False
        assert "secondaryDelaySecs" not in member

    def test_rs_initiate_applies_a_host_s_member_config(self) -> None:
        """Apply a named host's own priority/votes/hidden/delay from member_configs."""
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={
                "member_configs": {
                    "node01": MemberConfig(
                        priority=0, votes=False, hidden=True, delay_secs=300
                    )
                }
            }
        )

        action = PackagesInstallStrategy().build_run_step(
            "rs_initiate", ["node00", "node01"], spec
        )

        config = _rs_initiate_config(action)
        seed, delayed = config["members"]
        assert seed["priority"] == 1
        assert seed["votes"] == 1
        assert "secondaryDelaySecs" not in seed
        assert delayed["priority"] == 0
        assert delayed["votes"] == 0
        assert delayed["hidden"] is True
        assert delayed["secondaryDelaySecs"] == 300  # noqa: PLR2004

    def test_rs_initiate_tolerates_already_being_initiated(self) -> None:
        """Keep a retried dispatch after a first, invisible success from failing the run.

        A bare ``rs.initiate`` fails a retry with ``AlreadyInitiated``, which
        (retries exhausted) triggers rollback — tearing down a replica set
        that had, in fact, already initiated successfully.
        """
        action = PackagesInstallStrategy().build_run_step(
            "rs_initiate", ["node00"], _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "try {" in command
        assert "AlreadyInitialized" in command

    def test_create_pmm_monitoring_user_requires_params(self) -> None:
        """Reject a missing username/password as a programming error."""
        with pytest.raises(ValueError, match="username"):
            PackagesInstallStrategy().build_run_step(
                "create_pmm_monitoring_user", ["node00"], _spec(OperatingSystem.UBUNTU)
            )

    def test_create_pmm_monitoring_user_embeds_the_given_credentials(self) -> None:
        """Create exactly the user the caller generated."""
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )

        command = " ".join(action.command)
        assert "pmm_monitor" in command
        assert "generated-secret" in command
        assert "clusterMonitor" in command

    def test_create_pmm_monitoring_user_keeps_the_password_out_of_argv(self) -> None:
        """Read the JS from a private temp file, never through --eval."""
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )
        script = build_step_script(action)

        assert "--eval" not in script
        assert 'mongosh --quiet --port 27017 --file "$js"' in script
        assert "umask 077" in script
        assert "sh -c" not in script
        heredoc = script.split("<<'OM_BOOTSTRAP_JS'\n")[1].split("\nOM_BOOTSTRAP_JS\n")[
            0
        ]
        assert "generated-secret" in heredoc

    def test_create_pmm_monitoring_user_disables_the_atlas_cli_check(self) -> None:
        """Disable mongosh's Atlas CLI probe, which closes the localhost exception.

        Confirmed against a real run where every attempt to create the first
        user failed "not authorized" even though create_pmm_monitoring_user's
        own command was correct — the probe, not our command, burned it.
        """
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )

        command = " ".join(action.command)
        assert "MONGOSH_DISABLE_ATLAS_LOCAL_DEV_CLUSTER_CHECK=1" in command

    def test_create_pmm_monitoring_user_tolerates_already_existing(self) -> None:
        """Keep a retried dispatch after a first, invisible success from failing the run.

        A bare ``createUser`` fails a retry with ``UserAlreadyExists``
        (51003), which (retries exhausted) rolls the whole run back over a
        user that was actually created successfully.
        """
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )

        command = " ".join(action.command)
        assert "getUser(" in command
        assert "if (!db" in command


class TestPlanFinalizeSteps:
    """Assert the finalize step list is fixed and OS-independent."""

    def test_returns_the_fixed_finalize_step_names(self) -> None:
        """enable_auth, the only finalize step phase 1 needs."""
        spec = _spec(OperatingSystem.UBUNTU)
        assert (
            PackagesInstallStrategy().plan_finalize_steps(spec) == FINALIZE_STEP_NAMES
        )


class TestBuildFinalizeStep:
    """Assert build_finalize_step rejects unknown names and enables auth correctly."""

    def test_unknown_finalize_step_name_raises(self) -> None:
        """Reject a per-host forward step name as a finalize step."""
        with pytest.raises(
            ValueError, match="not a PackagesInstallStrategy finalize step"
        ):
            PackagesInstallStrategy().build_finalize_step(
                "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
            )

    def test_enable_auth_turns_authorization_on(self) -> None:
        """Add the one block configure_mongod deliberately left out."""
        action = PackagesInstallStrategy().build_finalize_step(
            "enable_auth", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "authorization: enabled" in command
        assert f"keyFile: {KEY_FILE_PATH}" in command

    def test_enable_auth_restarts_mongod(self) -> None:
        """Restart mongod, since security.authorization only takes effect at startup."""
        action = PackagesInstallStrategy().build_finalize_step(
            "enable_auth", "node00", _spec(OperatingSystem.UBUNTU)
        )

        assert "systemctl restart mongod" in " ".join(action.command)

    def test_enable_auth_keeps_the_replica_set_name(self) -> None:
        """Keep every setting configure_mongod wrote when rewriting the config."""
        action = PackagesInstallStrategy().build_finalize_step(
            "enable_auth", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "replSetName: rs-test" in command
        assert "fork: true" in command
        assert "path: /var/log/mongodb/mongod.log" in command

    def test_enable_auth_probes_readiness_after_restarting(self) -> None:
        """Follow the restart with an unauthenticated readiness probe.

        ``ping`` is one of the commands MongoDB answers without credentials
        even with ``security.authorization: enabled`` — the same one
        ``verify`` uses after the first, auth-less start.
        """
        action = PackagesInstallStrategy().build_finalize_step(
            "enable_auth", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert _mongosh_eval_command("db.adminCommand('ping').ok", 27017) in command

    def test_enable_auth_does_not_restart_when_the_config_write_fails(
        self, tmp_path: Path
    ) -> None:
        """Skip the restart when the config write fails, leaving no auth-less mongod.

        Joining the heredoc, the restart, and the probe with a bare newline
        instead of ``&&`` would let ``systemctl restart`` run regardless of
        whether ``cat`` actually wrote the new config — confirmed here by
        forcing the write itself to fail (read-only target file) and asserting
        neither ``restart`` nor ``mongosh`` shell function is ever invoked.
        """
        action = PackagesInstallStrategy().build_finalize_step(
            "enable_auth", "node00", _spec(OperatingSystem.UBUNTU)
        )
        script = action.command[-1]

        fake_config_path = tmp_path / "mongod.conf"
        marker = tmp_path / "restart-was-called"
        # Make the write fail (read-only target) instead of actually
        # restarting anything, and stand in for `systemctl`/`mongosh` so a
        # bug that *does* reach them fails loudly rather than by chance.
        fake_config_path.touch()
        fake_config_path.chmod(0o444)
        rigged = script.replace(CONFIG_PATH, str(fake_config_path))
        wrapped = (
            f"systemctl() {{ : > {shlex.quote(str(marker))}; }}\n"
            f"mongosh() {{ : > {shlex.quote(str(marker))}; }}\n{rigged}"
        )

        result = subprocess.run(
            ["sh", "-c", wrapped], capture_output=True, text=True, check=False
        )

        assert result.returncode != 0
        assert not marker.exists()


class TestPlanRollbackSteps:
    """Assert the rollback step list is fixed and OS-independent."""

    def test_returns_the_fixed_rollback_step_names(self) -> None:
        """Reverse the forward steps that actually change host state."""
        spec = _spec(OperatingSystem.UBUNTU)
        assert PackagesInstallStrategy().plan_rollback_steps(spec) == [
            "stop_service",
            "remove_config",
            "remove_keyfile",
            "purge_package",
            "remove_data",
        ]


class TestBuildRollbackStep:
    """Assert build_rollback_step produces the right teardown command per OS."""

    @pytest.mark.parametrize("os_", SUPPORTED_OSES)
    @pytest.mark.parametrize("step_name", ROLLBACK_STEP_NAMES)
    def test_every_rollback_step_is_scoped_to_its_run(
        self, step_name: str, os_: OperatingSystem
    ) -> None:
        """Skip every rollback step unless the marker holds this run's id."""
        action = PackagesInstallStrategy().build_rollback_step(
            step_name, "node00", _spec(os_)
        )

        assert _body(action.command).startswith(
            f'[ "$(cat {OWNERSHIP_MARKER_PATH} 2>/dev/null)" = {RUN_ID} ] || exit 0\n'
        )

    @pytest.mark.parametrize("step_name", ROLLBACK_STEP_NAMES)
    def test_every_rollback_step_requires_a_run_id(self, step_name: str) -> None:
        """Refuse to build a rollback step that no marker could scope."""
        with pytest.raises(ValueError, match=f"{step_name} requires spec.run_id"):
            PackagesInstallStrategy().build_rollback_step(
                step_name, "node00", _spec(OperatingSystem.UBUNTU, None)
            )

    def test_remove_data_removes_the_marker_last(self) -> None:
        """Remove the marker after everything else, so a retried rollback still runs."""
        action = PackagesInstallStrategy().build_rollback_step(
            "remove_data", "node00", _spec(OperatingSystem.UBUNTU)
        )

        lines = _body(action.command).strip().splitlines()
        assert lines[-2] == "rm -rf /var/lib/mongo"
        assert lines[-1] == f"rm -f {OWNERSHIP_MARKER_PATH}"

    def test_unknown_rollback_step_name_raises(self) -> None:
        """Reject a forward step name as a rollback step; no silent no-op."""
        with pytest.raises(
            ValueError, match="not a PackagesInstallStrategy rollback step"
        ):
            PackagesInstallStrategy().build_rollback_step(
                "install_package", "node00", _spec(OperatingSystem.UBUNTU)
            )

    def test_purge_package_uses_apt_get_on_ubuntu(self) -> None:
        """Purge the package through apt-get, not dnf, when rolling back Ubuntu."""
        action = PackagesInstallStrategy().build_rollback_step(
            "purge_package", "node00", _spec(OperatingSystem.UBUNTU)
        )

        assert "apt-get remove -y --purge percona-server-mongodb" in " ".join(
            action.command
        )

    def test_purge_package_uses_dnf_on_rocky(self) -> None:
        """Purge the package through dnf, not apt-get, when rolling back Rocky."""
        action = PackagesInstallStrategy().build_rollback_step(
            "purge_package", "node00", _spec(OperatingSystem.ROCKY)
        )

        assert "dnf remove -y percona-server-mongodb" in " ".join(action.command)


def _recording_bin(tmp_path: Path, fakes: list[str]) -> Path:
    """Build a ``PATH`` directory with ``cat``/``rm`` and fakes that log their argv.

    Each fake appends its name and arguments to ``calls.log`` in ``tmp_path``.

    :param tmp_path: The test's scratch directory.
    :param fakes: The commands to fake.
    :return: The directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("cat", "rm"):
        real = shutil.which(tool)
        assert real is not None
        (bin_dir / tool).symlink_to(real)
    log = tmp_path / "calls.log"
    for fake in fakes:
        path = bin_dir / fake
        path.write_text(f'#!/bin/sh\necho "{fake} $*" >> {shlex.quote(str(log))}\n')
        path.chmod(0o755)
    return bin_dir


def _fake_bin(tmp_path: Path, *, with_mongod: bool) -> Path:
    """Build a ``PATH`` directory with the real tools pre_check needs and fake ones.

    :param tmp_path: The test's scratch directory.
    :param with_mongod: Whether a ``mongod`` is on this ``PATH``.
    :return: The directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("df", "tail", "ls", "dirname"):
        real = shutil.which(tool)
        assert real is not None
        (bin_dir / tool).symlink_to(real)
    fakes = ["apt-get", "mongod"] if with_mongod else ["apt-get"]
    for fake in fakes:
        path = bin_dir / fake
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return bin_dir


class TestPreCheckCommand:
    """Run pre_check's generated shell for real against scratch paths."""

    @pytest.fixture
    def paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        """Point the config path at a scratch path, with a 1-byte minimum."""
        config = tmp_path / "mongod.conf"
        data = tmp_path / "data"
        monkeypatch.setattr(packages, "CONFIG_PATH", str(config))
        monkeypatch.setattr(packages, "MIN_DATA_DISK_BYTES", 1)
        return config, data

    def _run(
        self, tmp_path: Path, *, with_mongod: bool = False
    ) -> subprocess.CompletedProcess[str]:
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={"data_path": str(tmp_path / "data")}
        )
        action = PackagesInstallStrategy().build_step("pre_check", "node00", spec)
        bin_dir = _fake_bin(tmp_path, with_mongod=with_mongod)
        return subprocess.run(
            [_SH, "-c", _body(action.command)],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
            check=False,
        )

    def test_passes_on_a_clean_host(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Pass on a host with no MongoDB anywhere and enough disk."""
        result = self._run(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_passes_with_an_empty_data_directory(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Pass with an empty data directory, measuring its free space."""
        paths[1].mkdir()

        result = self._run(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_fails_when_mongod_is_on_path(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Fail when a mongod is installed: the host runs a MongoDB of its own."""
        result = self._run(tmp_path, with_mongod=True)

        assert result.returncode != 0
        assert "mongod is already installed" in result.stderr

    def test_fails_when_the_config_file_exists(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Fail when a mongod.conf from someone else's install is present."""
        paths[0].write_text("net: {}\n")

        result = self._run(tmp_path)

        assert result.returncode != 0
        assert "already exists" in result.stderr

    def test_fails_when_the_data_directory_is_not_empty(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Fail on existing data files, exactly what rollback must never delete."""
        paths[1].mkdir()
        (paths[1] / "WiredTiger").write_text("")

        result = self._run(tmp_path)

        assert result.returncode != 0
        assert "is not empty" in result.stderr

    def test_fails_without_enough_disk_space(
        self,
        tmp_path: Path,
        paths: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail with less free space than the minimum, naming why."""
        monkeypatch.setattr(packages, "MIN_DATA_DISK_BYTES", 2**62)

        result = self._run(tmp_path)

        assert result.returncode != 0
        assert "bytes free" in result.stderr

    def test_fails_when_free_space_cannot_be_measured(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Fail closed when ``df`` prints nothing, instead of passing the check."""
        action = PackagesInstallStrategy().build_step(
            "pre_check", "node00", _spec(OperatingSystem.UBUNTU)
        )
        bin_dir = _fake_bin(tmp_path, with_mongod=False)
        (bin_dir / "df").unlink()
        (bin_dir / "df").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "df").chmod(0o755)

        result = subprocess.run(
            [_SH, "-c", _body(action.command)],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
            check=False,
        )

        assert result.returncode != 0
        assert "could not measure free space" in result.stderr

    def test_measures_the_nearest_existing_ancestor_of_a_missing_data_path(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Measure the mount a missing data path will live on, not ``/``.

        ``/mnt/mongo/data`` can be absent while ``/mnt/mongo`` is a distinct,
        already-mounted volume; falling straight back to ``/`` would report the
        wrong filesystem's free space.
        """
        mount_point = tmp_path / "mnt-mongo"
        mount_point.mkdir()
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={"data_path": str(mount_point / "data")}
        )
        action = PackagesInstallStrategy().build_step("pre_check", "node00", spec)
        bin_dir = _fake_bin(tmp_path, with_mongod=False)
        log = tmp_path / "df.log"
        (bin_dir / "df").unlink()
        (bin_dir / "df").write_text(
            f'#!/bin/sh\necho "$3" >> {shlex.quote(str(log))}\necho 999999999999\n'
        )
        (bin_dir / "df").chmod(0o755)

        result = subprocess.run(
            [_SH, "-c", _body(action.command)],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines() == [str(mount_point)]


class TestRollbackCommands:
    """Run rollback steps' generated shell for real against scratch paths."""

    @pytest.fixture
    def paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path]:
        """Point the fixed paths at scratch copies holding a config file and data."""
        config = tmp_path / "mongod.conf"
        data = tmp_path / "data"
        marker = tmp_path / "mongod.om-bootstrap"
        monkeypatch.setattr(packages, "CONFIG_PATH", str(config))
        monkeypatch.setattr(packages, "OWNERSHIP_MARKER_PATH", str(marker))
        config.write_text("net: {}\n")
        data.mkdir()
        (data / "WiredTiger").write_text("")
        return config, data, marker

    def _run(self, tmp_path: Path, step_name: str) -> None:
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={"data_path": str(tmp_path / "data")}
        )
        action = PackagesInstallStrategy().build_rollback_step(
            step_name, "node00", spec
        )
        bin_dir = tmp_path / "bin"
        if not bin_dir.exists():
            _recording_bin(tmp_path, ["systemctl", "apt-get"])
        subprocess.run(
            [_SH, "-c", _body(action.command)],
            env={"PATH": str(bin_dir)},
            check=True,
        )

    def _run_all(self, tmp_path: Path) -> list[str]:
        """Run every rollback step in order and return the faked commands' calls."""
        for step_name in ROLLBACK_STEP_NAMES:
            self._run(tmp_path, step_name)
        log = tmp_path / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_leaves_a_host_without_the_marker_untouched(
        self, tmp_path: Path, paths: tuple[Path, Path, Path]
    ) -> None:
        """Leave a MongoDB this strategy never installed intact."""
        config, data, _marker = paths

        calls = self._run_all(tmp_path)

        assert calls == []
        assert config.exists()
        assert (data / "WiredTiger").exists()

    def test_leaves_another_runs_install_untouched(
        self, tmp_path: Path, paths: tuple[Path, Path, Path]
    ) -> None:
        """Make every step a no-op when the marker holds a different run's id.

        The host an earlier run bootstrapped keeps its marker; a later run
        that fails ``pre_check`` there and rolls back must not destroy it.
        """
        config, data, marker = paths
        marker.write_text(f"{OTHER_RUN_ID}\n")

        calls = self._run_all(tmp_path)

        assert calls == []
        assert config.exists()
        assert (data / "WiredTiger").exists()
        assert marker.read_text() == f"{OTHER_RUN_ID}\n"

    def test_removes_what_it_installed_when_the_marker_holds_its_run(
        self, tmp_path: Path, paths: tuple[Path, Path, Path]
    ) -> None:
        """Stop, purge, and remove everything, then the marker, for this run."""
        config, data, marker = paths
        marker.write_text(f"{RUN_ID}\n")

        calls = self._run_all(tmp_path)

        assert calls == [
            "systemctl disable --now mongod",
            "apt-get remove -y --purge percona-server-mongodb",
        ]
        assert not config.exists()
        assert not data.exists()
        assert not marker.exists()
