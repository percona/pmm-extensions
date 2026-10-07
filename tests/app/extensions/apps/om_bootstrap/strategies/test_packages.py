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
import os
import re
import shlex
import shutil
import socket
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from app.extensions.apps.om_bootstrap.dispatch import build_step_script
from app.extensions.apps.om_bootstrap.strategies import packages
from app.extensions.apps.om_bootstrap.strategies.packages import (
    _mongosh_eval,
    _mongosh_eval_command,
    _with_loopback,
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


def _spec(
    os_: OperatingSystem,
    run_id: UUID | None = RUN_ID,
    member_configs: dict[str, MemberConfig] | None = None,
) -> BootstrapSpec:
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
        member_configs=member_configs or {},
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

        assert 'replSetName: "rs-test"' in " ".join(action.command)

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
        assert f'pidFilePath: "{PID_FILE_PATH}"' in command

    def test_configure_mongod_sets_a_logpath(self) -> None:
        """Set a logpath, without which mongod refuses to start with fork: true.

        ``BadValue: --fork has to be used with --logpath or --syslog`` —
        confirmed against a real run.
        """
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert 'path: "/var/log/mongodb/mongod.log"' in command

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

        ping = _mongosh_eval_command("db.adminCommand('ping').ok", 27017)
        assert f"{ping} || rc=$?" in _body(action.command)
        assert action.timeout_s == _mongosh_eval("", 27017).timeout_s

    def test_pre_check_looks_for_mongod_outside_sudo_s_path(self) -> None:
        """Look where a tarball mongod lands, which ``sudo``'s PATH leaves out."""
        action = PackagesInstallStrategy().build_step(
            "pre_check", "node00", _spec(OperatingSystem.UBUNTU)
        )

        body = _body(action.command)
        assert "command -v mongod" in body
        assert " /usr/local/bin/mongod " in body
        # Unquoted, so the host expands the glob.
        assert " /opt/*/bin/mongod;" in body
        assert "mongod is already installed at $m" in body

    def test_pre_check_checks_the_spec_port_is_free(self) -> None:
        """Check the port the new mongod will use, with ``ss`` and a fallback."""
        spec = _spec(OperatingSystem.UBUNTU).model_copy(update={"port": 27018})
        action = PackagesInstallStrategy().build_step("pre_check", "node00", spec)

        body = _body(action.command)
        assert "port=27018\n" in body
        assert 'ss -Hltnp "sport = :$port"' in body
        assert "/proc/net/tcp /proc/net/tcp6" in body
        assert "pre_check: port $port is already in use ($held)" in body

    @pytest.mark.parametrize("step_name", ["start_service", "verify"])
    def test_a_step_starting_mongod_prints_why_it_did_not_come_up(
        self, step_name: str
    ) -> None:
        """Print the journal and mongod's log errors, keeping the exit code."""
        action = PackagesInstallStrategy().build_step(
            step_name, "node00", _spec(OperatingSystem.UBUNTU)
        )

        body = _body(action.command)
        assert "journalctl -u mongod --no-pager -n 20 >&2" in body
        assert "/var/log/mongodb/mongod.log" in body
        assert 'om_mongod_why; exit "$rc"' in body


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
    """Assert build_run_step builds each run step and rejects unknown names."""

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
        assert '--file "$js"' in script
        assert "umask 077" in script
        assert "sh -c" not in script
        heredoc = script.split("<<'OM_BOOTSTRAP_JS'\n")[1].split("\nOM_BOOTSTRAP_JS\n")[
            0
        ]
        assert "generated-secret" in heredoc

    def test_create_pmm_monitoring_user_connects_to_the_replica_set(self) -> None:
        """Route the write to the primary, not to the member the step runs on.

        The regression this pins. createUser is a write, so it needs the
        primary, and this step runs on hosts[0] - the seed. rs.initiate only
        proposes the config; the election that follows is open to every member,
        so a run whose seed loses it rolls back a healthy replica set.

        Naming every member with replicaSet= makes the driver find the primary
        wherever it is.
        """
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00", "node01", "node02"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )
        script = build_step_script(action)

        assert "--port 27017 --file" not in script
        for host in ("node00:27017", "node01:27017", "node02:27017"):
            assert host in script
        assert "replicaSet=rs-test" in script

    def test_create_pmm_monitoring_user_waits_for_an_election(self) -> None:
        """Wait for a primary to exist rather than requiring one already elected.

        The same defect has a timing half: rs.initiate returns as soon as the
        config is accepted, and the first election lands some seconds later. A
        dispatch that arrives in that window has no primary to write to at all,
        whichever member it reaches.
        """
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00", "node01", "node02"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )

        assert (
            f"serverSelectionTimeoutMS={packages.PRIMARY_SELECTION_TIMEOUT_MS}"
            in build_step_script(action)
        )
        # The driver may spend that whole budget waiting, so the step's own
        # deadline has to outlast it or the wait is decorative.
        assert action.timeout_s * 1000 > packages.PRIMARY_SELECTION_TIMEOUT_MS

    def test_rs_initiate_still_runs_against_the_local_member(self) -> None:
        """Keep rs.initiate on the seed itself - it is what creates the set.

        Only create_pmm_monitoring_user needs the primary. rs.initiate has no
        primary to find yet, and must run on the member being initiated, so the
        replica-set URI must not spread to it.
        """
        action = PackagesInstallStrategy().build_run_step(
            "rs_initiate", ["node00", "node01", "node02"], _spec(OperatingSystem.UBUNTU)
        )

        command = " ".join(action.command)
        assert "--port 27017" in command
        assert "replicaSet=" not in command

    def test_create_pmm_monitoring_user_still_hides_the_password(self) -> None:
        """Keep the secret out of argv now that the target is a URI.

        The URI names no credentials, but it is on the same command line the
        password must stay off, so this is worth pinning next to the change.
        """
        action = PackagesInstallStrategy().build_run_step(
            "create_pmm_monitoring_user",
            ["node00", "node01", "node02"],
            _spec(OperatingSystem.UBUNTU),
            params={"username": "pmm_monitor", "password": "generated-secret"},
        )
        script = build_step_script(action)

        mongosh_line = next(line for line in script.splitlines() if "mongosh" in line)
        assert "generated-secret" not in mongosh_line
        assert "--eval" not in script

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
        assert 'replSetName: "rs-test"' in command
        assert "fork: true" in command
        assert 'path: "/var/log/mongodb/mongod.log"' in command

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


def _fake_bin(tmp_path: Path, *, with_mongod: bool, ss_output: str = "") -> Path:
    """Build a ``PATH`` directory with the real tools pre_check needs and fake ones.

    ``ss`` is always a fake, so the port check answers about the test's listeners
    rather than whatever the machine running the tests happens to have on 27017.

    :param tmp_path: The test's scratch directory.
    :param with_mongod: Whether a ``mongod`` is on this ``PATH``.
    :param ss_output: What the fake ``ss`` prints: nothing for a free port.
    :return: The directory.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("df", "tail", "ls", "dirname", "sed", "head"):
        real = shutil.which(tool)
        assert real is not None
        (bin_dir / tool).symlink_to(real)
    fakes = ["apt-get", "mongod"] if with_mongod else ["apt-get"]
    for fake in fakes:
        path = bin_dir / fake
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    ss = bin_dir / "ss"
    ss.write_text(f"#!/bin/sh\nprintf '%s' {shlex.quote(ss_output)}\n")
    ss.chmod(0o755)
    return bin_dir


class TestPreCheckCommand:
    """Run pre_check's generated shell for real against scratch paths."""

    @pytest.fixture
    def paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        """Point the fixed paths at scratch paths, with a 1-byte minimum."""
        config = tmp_path / "mongod.conf"
        data = tmp_path / "data"
        monkeypatch.setattr(packages, "CONFIG_PATH", str(config))
        monkeypatch.setattr(packages, "MIN_DATA_DISK_BYTES", 1)
        monkeypatch.setattr(
            packages,
            "EXTRA_MONGOD_DIRS",
            (str(tmp_path / "usr-local-bin"), str(tmp_path / "opt" / "*" / "bin")),
        )
        return config, data

    def _run(
        self, tmp_path: Path, *, with_mongod: bool = False, ss_output: str = ""
    ) -> subprocess.CompletedProcess[str]:
        spec = _spec(OperatingSystem.UBUNTU).model_copy(
            update={"data_path": str(tmp_path / "data")}
        )
        action = PackagesInstallStrategy().build_step("pre_check", "node00", spec)
        bin_dir = _fake_bin(tmp_path, with_mongod=with_mongod, ss_output=ss_output)
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

    @pytest.mark.parametrize(
        "where", [("usr-local-bin",), ("opt", "mongodb-linux-x86_64-8.0.4", "bin")]
    )
    def test_fails_when_mongod_is_outside_sudo_s_path(
        self, tmp_path: Path, paths: tuple[Path, Path], where: tuple[str, ...]
    ) -> None:
        """Fail on a tarball mongod ``command -v`` cannot see, naming where it is.

        :param where: The directory under ``tmp_path`` the mongod is in.
        """
        directory = tmp_path.joinpath(*where)
        directory.mkdir(parents=True)
        mongod = directory / "mongod"
        mongod.write_text("#!/bin/sh\nexit 0\n")
        mongod.chmod(0o755)

        result = self._run(tmp_path)

        assert result.returncode != 0
        assert result.stderr == f"pre_check: mongod is already installed at {mongod}\n"

    def test_fails_when_ss_reports_a_listener_naming_its_process(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Fail when the port is taken, naming the process ``ss`` says holds it."""
        result = self._run(
            tmp_path,
            ss_output="LISTEN 0 4096 0.0.0.0:27017 0.0.0.0:* "
            'users:(("mongod",pid=812,fd=11))\n',
        )

        assert result.returncode != 0
        assert result.stderr == (
            "pre_check: port 27017 is already in use (pid 812, mongod)\n"
        )

    def test_fails_when_ss_reports_a_listener_it_cannot_attribute(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Still fail when ``ss`` shows no process, which it does for non-root."""
        result = self._run(
            tmp_path, ss_output="LISTEN 0 4096 0.0.0.0:27017 0.0.0.0:*\n"
        )

        assert result.returncode != 0
        assert "port 27017 is already in use (held by an unknown process)" in (
            result.stderr
        )

    @pytest.mark.skipif(
        not Path("/proc/net/tcp").exists(), reason="needs Linux's /proc/net/tcp"
    )
    def test_falls_back_to_proc_without_ss(
        self, tmp_path: Path, paths: tuple[Path, Path]
    ) -> None:
        """Find a real listener through ``/proc/net/tcp`` on a host with no ``ss``."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            spec = _spec(OperatingSystem.UBUNTU).model_copy(
                update={"data_path": str(tmp_path / "data"), "port": port}
            )
            action = PackagesInstallStrategy().build_step("pre_check", "node00", spec)
            bin_dir = _fake_bin(tmp_path, with_mongod=False)
            (bin_dir / "ss").unlink()
            for tool in ("awk", "find", "cat"):
                real = shutil.which(tool)
                assert real is not None
                (bin_dir / tool).symlink_to(real)

            result = subprocess.run(
                [_SH, "-c", _body(action.command)],
                capture_output=True,
                text=True,
                env={"PATH": str(bin_dir)},
                check=False,
            )

        assert result.returncode != 0
        assert f"port {port} is already in use (pid {os.getpid()}, " in result.stderr

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
        assert re.search(
            r"pre_check: the data directory \S+ needs at least 4294967296 GiB free, "
            r"but \S+ has \d+\.\d GiB$",
            result.stderr.strip(),
        ), result.stderr

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


#: One error entry of mongod's JSON log, as the port-taken failure writes it.
_LISTENER_ERROR = (
    '{"t":{"$date":"2026-10-07T10:00:00.000+00:00"},"s":"E","c":"NETWORK",'
    '"id":23024,"ctx":"initandlisten","msg":"Error setting up listener",'
    '"attr":{"error":{"code":9001,"codeName":"SocketException",'
    '"errmsg":"Address already in use"}}}'
)


class TestMongodDiagnostics:
    """Run start_service and verify for real, with a mongod that will not come up."""

    def _run(
        self,
        tmp_path: Path,
        step_name: str,
        *,
        fails: bool,
        log_lines: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one step with ``systemctl``/``mongosh``/``journalctl`` faked.

        :param tmp_path: The test's scratch directory.
        :param step_name: ``start_service`` or ``verify``.
        :param fails: Whether ``systemctl restart`` and ``mongosh`` fail.
        :param log_lines: mongod's log, or ``None`` for no log file at all.
        :return: The finished process.
        """
        log = tmp_path / "mongod.log"
        if log_lines is not None:
            log.write_text("".join(f"{line}\n" for line in log_lines))
        spec = _spec(OperatingSystem.UBUNTU).model_copy(update={"log_path": str(log)})
        action = PackagesInstallStrategy().build_step(step_name, "node00", spec)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("tail", "grep"):
            real = shutil.which(tool)
            assert real is not None
            (bin_dir / tool).symlink_to(real)
        failing = (
            '[ "$1" = enable ] && exit 0\n'
            'echo "Job for mongod.service failed." >&2\nexit 1\n'
        )
        fakes = {
            "systemctl": failing if fails else "exit 0\n",
            "mongosh": "exit 1\n" if fails else "exit 0\n",
            "journalctl": 'echo "mongod.service: Failed with result exit-code."\n',
        }
        for name, script in fakes.items():
            path = bin_dir / name
            path.write_text(f"#!/bin/sh\n{script}")
            path.chmod(0o755)
        return subprocess.run(
            [_SH, "-c", _body(action.command)],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
            check=False,
        )

    @pytest.mark.parametrize("step_name", ["start_service", "verify"])
    def test_ends_stderr_with_mongod_s_own_error(
        self, tmp_path: Path, step_name: str
    ) -> None:
        """End with the log's error entry, the part a capped detail keeps."""
        result = self._run(
            tmp_path,
            step_name,
            fails=True,
            log_lines=[
                '{"s":"I","msg":"Build Info"}',
                _LISTENER_ERROR,
                '{"s":"I","msg":"Shutting down","attr":{"exitCode":48}}',
            ],
        )

        assert result.returncode == 1
        assert "mongod.service: Failed with result exit-code." in result.stderr
        assert result.stderr.rstrip().endswith(_LISTENER_ERROR)

    def test_prints_the_log_s_end_when_it_has_no_error_entry(
        self, tmp_path: Path
    ) -> None:
        """Print the last lines of a log without error entries, rather than nothing."""
        result = self._run(
            tmp_path, "start_service", fails=True, log_lines=["plain text line"]
        )

        assert result.returncode == 1
        assert result.stderr.rstrip().endswith("plain text line")

    def test_survives_a_missing_log(self, tmp_path: Path) -> None:
        """Still fail with the command's own code when there is no log to read."""
        result = self._run(tmp_path, "start_service", fails=True)

        assert result.returncode == 1
        assert "Failed with result exit-code." in result.stderr

    @pytest.mark.parametrize("step_name", ["start_service", "verify"])
    def test_says_nothing_when_mongod_comes_up(
        self, tmp_path: Path, step_name: str
    ) -> None:
        """Print no diagnostics for a step that succeeded."""
        result = self._run(
            tmp_path, step_name, fails=False, log_lines=[_LISTENER_ERROR]
        )

        assert result.returncode == 0
        assert result.stderr == ""


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


class TestPerMemberBindIP:
    """Assert mongod.conf's bindIp comes from the member when it names one.

    The safe default for a replica-set member is its *own* address, and a
    three-member set has three different ones. A single run-level ``bind_ip``
    can only be ``0.0.0.0`` or wrong for two of the three, which is why
    ``MemberConfig`` carries one at all.
    """

    @staticmethod
    def _config_for(host: str, spec: BootstrapSpec, step: str) -> str:
        action = PackagesInstallStrategy().build_step(step, host, spec)
        return "\n".join(action.command)

    def test_member_bind_ip_wins_over_the_run_level_one(self) -> None:
        """Use the member's own address where it names one."""
        spec = _spec(
            OperatingSystem.UBUNTU,
            member_configs={"node00": MemberConfig(bind_ip="10.0.0.1")},
        )

        assert 'bindIp: "127.0.0.1,10.0.0.1"' in self._config_for(
            "node00", spec, "configure_mongod"
        )

    def test_a_host_the_run_does_not_name_keeps_the_run_level_one(self) -> None:
        """Leave every other host on the run's value."""
        spec = _spec(
            OperatingSystem.UBUNTU,
            member_configs={"node00": MemberConfig(bind_ip="10.0.0.1")},
        )

        assert 'bindIp: "0.0.0.0"' in self._config_for(
            "node01", spec, "configure_mongod"
        )

    def test_a_member_config_without_a_bind_ip_keeps_the_run_level_one(self) -> None:
        """Treat a member that sets only election settings as naming no address."""
        spec = _spec(
            OperatingSystem.UBUNTU,
            member_configs={"node00": MemberConfig(priority=0, votes=False)},
        )

        assert 'bindIp: "0.0.0.0"' in self._config_for(
            "node00", spec, "configure_mongod"
        )

    def test_enable_auth_rewrites_the_same_bind_ip(self) -> None:
        """Keep the finalize step's rewrite on the member's address too.

        ``enable_auth`` rewrites the whole of mongod.conf, so if it read the
        run-level value it would silently undo the per-member one several steps
        after it was applied.
        """
        spec = _spec(
            OperatingSystem.UBUNTU,
            member_configs={"node00": MemberConfig(bind_ip="10.0.0.1")},
        )
        action = PackagesInstallStrategy().build_finalize_step(
            "enable_auth", "node00", spec
        )

        assert 'bindIp: "127.0.0.1,10.0.0.1"' in "\n".join(action.command)


class TestMongodConfigQuoting:
    """Assert every string in mongod.conf is a quoted YAML scalar.

    The validators bound these values to no
    whitespace and no control characters, which closes newline injection, but YAML
    still types a bare scalar by its content: ``#...`` is a comment, ``null`` is
    null, ``[a,b]`` a sequence, ``*x`` an alias. An operator naming an address or a
    path containing any of those would get a config mongod misreads or refuses.
    """

    @staticmethod
    def _config(**overrides: str) -> str:
        spec = _spec(OperatingSystem.UBUNTU)
        for key, value in overrides.items():
            setattr(spec, key, value)
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", spec
        )
        return "\n".join(action.command)

    def test_the_default_config_quotes_every_string(self) -> None:
        """Quote the ordinary values too, not only suspicious ones."""
        config = self._config()

        assert 'bindIp: "0.0.0.0"' in config
        assert 'dbPath: "/var/lib/mongo"' in config
        assert 'replSetName: "rs-test"' in config
        assert 'path: "/var/log/mongodb/mongod.log"' in config

    def test_a_hash_is_not_read_as_a_comment(self) -> None:
        """Keep a value containing ``#`` whole instead of truncating the line."""
        config = self._config(bind_ip="10.0.0.1#2")

        assert 'bindIp: "127.0.0.1,10.0.0.1#2"' in config

    def test_a_yaml_keyword_stays_a_string(self) -> None:
        """Emit ``null`` as a string rather than YAML's null."""
        config = self._config(replica_set_name="null")

        assert 'replSetName: "null"' in config

    def test_a_bracketed_value_is_not_read_as_a_sequence(self) -> None:
        """Emit ``[::1],...`` as a string, which is also how an IPv6 literal arrives."""
        config = self._config(bind_ip="[::1],127.0.0.1")

        assert 'bindIp: "[::1],127.0.0.1"' in config

    def test_an_asterisk_is_not_read_as_an_alias(self) -> None:
        """Emit ``*``, mongod's own bind-all value, as a string."""
        config = self._config(bind_ip="*")

        assert 'bindIp: "*"' in config

    def test_a_per_member_address_is_quoted_too(self) -> None:
        """Quote the member's own address on the same path as the run's."""
        spec = _spec(
            OperatingSystem.UBUNTU,
            member_configs={"node00": MemberConfig(bind_ip="10.0.0.1")},
        )
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", spec
        )

        assert 'bindIp: "127.0.0.1,10.0.0.1"' in "\n".join(action.command)

    def test_the_config_is_still_valid_yaml(self) -> None:
        """Parse it, so the quoting cannot be asserted into nonsense."""
        spec = _spec(OperatingSystem.UBUNTU)
        spec.bind_ip = "127.0.0.1,10.0.0.1#2"
        action = PackagesInstallStrategy().build_step(
            "configure_mongod", "node00", spec
        )
        config = "\n".join(action.command)
        # The heredoc delimiter appears twice: once in `<<'MONGOD_CONF' && ...` and
        # once closing the body, so the config starts after that preamble's newline.
        body = config.split("MONGOD_CONF")[1].split("\n", 1)[1]
        parsed = yaml.safe_load(body)

        # Compared against the spec, not literals: the point is that what went in
        # comes back out as the same string, whatever YAML would make of it bare.
        assert parsed["net"]["bindIp"] == spec.bind_ip
        assert parsed["net"]["port"] == spec.port
        assert parsed["storage"]["dbPath"] == spec.data_path
        assert parsed["replication"]["replSetName"] == spec.replica_set_name


class TestBindIpReachesLoopback:
    """Assert bindIp always reaches 127.0.0.1, where ``mongosh --port`` connects."""

    @pytest.mark.parametrize(
        ("bind_ip", "expected"),
        [
            ("pmm-client-node00", "127.0.0.1,pmm-client-node00"),
            ("10.0.0.1,10.0.0.2", "127.0.0.1,10.0.0.1,10.0.0.2"),
            ("127.0.0.1", "127.0.0.1"),
            ("LOCALHOST,10.0.0.1", "LOCALHOST,10.0.0.1"),
            ("0.0.0.0", "0.0.0.0"),
            ("*", "*"),
        ],
    )
    def test_loopback_is_added_only_where_missing(
        self, bind_ip: str, expected: str
    ) -> None:
        """Leave the wildcards alone: beside 127.0.0.1, mongod breaks on either."""
        assert _with_loopback(bind_ip) == expected
