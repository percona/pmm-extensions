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

"""Install Percona Server for MongoDB from Percona's official OS packages.

First (and, for now, only) implementation of
:class:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy` — Ubuntu and Rocky
Linux only, matching the phase-1 OS scope. ``DockerInstallStrategy`` and
``PodmanInstallStrategy`` are future siblings of this module, implementing the same
protocol.

Every per-host step here is package-manager-specific (``apt`` vs. ``dnf``), which
is exactly what the strategy boundary is for: :meth:`PackagesInstallStrategy.plan_steps`
returns the same step *names* regardless of OS, so the stepper never branches on
OS — only :meth:`PackagesInstallStrategy.build_step` does, once, per step.

Data path, log path, port and bind IP all come from :class:`BootstrapSpec`.
Per-member election settings (priority, votes, hidden, delayed) come from
``spec.member_configs``. TLS is still future scope, same as encryption at rest
(``security.enableEncryption``/``encryptionKeyFile``) — ``_mongod_config``
below writes only ``security.authorization``/``keyFile`` (intra-cluster auth),
never an encryption block, so a run today gets neither.

Rollback never touches a MongoDB this run did not install: ``pre_check``
refuses a host that already has one (:data:`CONFIG_PATH`, a non-empty
``spec.data_path``, or ``mongod`` on ``PATH``), ``install_package`` then writes
this run's id to :data:`OWNERSHIP_MARKER_PATH` before installing anything, and
every rollback step is a no-op on a host whose marker does not hold this run's
id. The marker outlives a successful run, so a later run that fails
``pre_check`` on the same host and rolls back leaves the earlier run's MongoDB
intact.
"""

import base64
import json
import posixpath
import shlex

from app.extensions.apps.om_bootstrap.strategy import (
    BootstrapSpec,
    MemberConfig,
    OperatingSystem,
    StepAction,
)

#: Where every step here reads or writes the shared keyFile — planted by the
#: ``distribute_keyfile`` step, ahead of ``configure_mongod``. Fixed, not a
#: :class:`BootstrapSpec` field — keyFile *content* is per-run, but where it
#: lands on disk isn't something the Configure step exposes.
KEY_FILE_PATH = "/etc/mongod.key"

#: Where the packaged mongod's own config file lives on both supported OSes.
#: Fixed for the same reason as :data:`KEY_FILE_PATH`.
CONFIG_PATH = "/etc/mongod.conf"

#: Written by ``install_package``, holding the run's id, once ``pre_check`` has
#: proven the host had no MongoDB of its own. Every rollback step first checks
#: that it holds its own run's id, so rolling back a host this run never
#: installed on removes nothing; ``remove_data``, the last rollback step,
#: deletes it.
OWNERSHIP_MARKER_PATH = "/etc/mongod.om-bootstrap"

#: Matches the packaged ``mongod.service``'s own ``PIDFile=`` on both supported
#: OSes. The unit is ``Type=forking``, so this has to agree with the systemd unit
#: exactly — see :meth:`PackagesInstallStrategy._configure_mongod`. Fixed for
#: the same reason as :data:`KEY_FILE_PATH`.
PID_FILE_PATH = "/var/run/mongod.pid"

#: Minimum free space at ``spec.data_path`` ``pre_check`` requires, in bytes.
#: 5 GiB — generous for phase-1's single-member/three-member replica sets, not a
#: sized-for-production figure.
MIN_DATA_DISK_BYTES = 5 * 1024 * 1024 * 1024

#: Roles PMM's ``mongodb_exporter`` needs, granted to the user
#: ``create_pmm_monitoring_user`` creates — ``clusterMonitor`` for replication/
#: server-status metrics, ``read`` on ``local`` for oplog metrics. The same
#: minimum PMM's own client-side setup docs grant a manually-created monitoring
#: user.
PMM_MONITORING_USER_ROLES = [
    {"role": "clusterMonitor", "db": "admin"},
    {"role": "read", "db": "local"},
]

#: How long ``create_pmm_monitoring_user``'s driver waits for a primary to exist
#: before giving up, in milliseconds.
#:
#: This is a wait for an *election*, not for a network round trip. ``rs.initiate``
#: returns as soon as the config is accepted, and the first election follows it by
#: whatever ``electionTimeoutMillis`` and the members' own timers work out to -
#: measured at 11s on a three-member set on a real deployment, against a 10s
#: default election timeout. 30s leaves room for a member that is slow to answer
#: its first heartbeat without waiting so long that a genuinely broken set holds
#: the step past its own dispatch timeout.
PRIMARY_SELECTION_TIMEOUT_MS = 30_000


def _psmdb_channel(mongodb_version: str) -> str:
    """Turn ``"8.0"`` (or ``"8.0.4"``) into the channel name ``"psmdb-80"``.

    Uses only the first two dot-separated components: PMM's own
    ``TriggerHostBootstrapRequest.mongodb_version`` field docs a full patch
    version as a valid example (``"7.0.8"``) and pmm-managed passes it through
    unchanged (``managed/services/om/inventory.go``), so this has to accept
    one: naively stripping every dot from ``"7.0.14"`` produced the
    nonexistent channel ``"psmdb-7014"`` instead of ``"psmdb-70"`` (confirmed
    against a live ``percona-release enable``: "Specified repository does not
    exist"). PSMDB does not ship parallel repos per patch version, matching
    the request field's own "only the major version selects the install
    source" comment.

    :param mongodb_version: A dotted version, major.minor (``"8.0"``) or
        major.minor.patch (``"8.0.4"``).
    :return: The channel name ``percona-release setup`` expects.
    """
    major_minor = ".".join(mongodb_version.split(".")[:2])
    return f"psmdb-{major_minor.replace('.', '')}"


def _shell_step(body: str, *, timeout_s: int = 30) -> StepAction:
    """Build a ``StepAction`` running ``body`` through ``sh -c``.

    :param body: The shell script to run on the host.
    :param timeout_s: How long the step may run, in seconds.
    :return: The step action.
    """
    return StepAction(command=["sh", "-c", body], timeout_s=timeout_s)


def _mongod_config(spec: BootstrapSpec, *, with_auth: bool) -> str:
    """Render ``mongod.conf``'s contents, with or without the security block.

    Shared by :meth:`PackagesInstallStrategy._configure_mongod` (``with_auth=False``,
    always — see its own docstring for why) and
    :meth:`PackagesInstallStrategy._enable_auth` (``with_auth=True``, turning it
    on afterward): every other setting is identical between the two, so this is
    the one place that has to stay in sync rather than two configs drifting
    apart under maintenance.

    :param spec: The host's bootstrap spec.
    :param with_auth: Whether to include ``security.authorization``/``keyFile``.
    :return: The full config file contents, including a trailing newline on the
        last section.
    """
    security = (
        f"security:\n  authorization: enabled\n  keyFile: {KEY_FILE_PATH}\n"
        if with_auth
        else ""
    )
    return (
        f"net:\n  bindIp: {spec.bind_ip}\n  port: {spec.port}\n"
        f"storage:\n  dbPath: {spec.data_path}\n"
        f"{security}"
        f"replication:\n  replSetName: {spec.replica_set_name}\n"
        f"processManagement:\n  fork: true\n  pidFilePath: {PID_FILE_PATH}\n"
        f"systemLog:\n  destination: file\n  path: {spec.log_path}\n  logAppend: true\n"
    )


def _mongosh_eval_command(js: str, port: int) -> str:
    """Build one ``mongosh --quiet --eval`` shell fragment.

    For JS that carries no secret only: ``--eval``'s argument is visible in
    ``ps`` for as long as mongosh runs. JS that embeds a secret goes through
    :func:`_mongosh_file` instead. ``shlex.quote`` on the whole ``--eval``
    argument, not string interpolation into a shell command, avoids the quoting
    bugs that show up trying to nest a JS string literal inside a shell
    double-quoted one.

    ``MONGOSH_DISABLE_ATLAS_LOCAL_DEV_CLUSTER_CHECK=1``: mongosh probes
    ``admin.atlascli`` (Atlas CLI local-deployment detection) as its first
    command on every connection, before anything in ``js`` runs. Against a
    member with authorization enabled and no credentials, that probe is
    rejected as unauthorized — set on every mongosh call so no caller has to
    reason about whether its member has authorization on yet.

    :param js: The JavaScript to evaluate.
    :param port: The port mongod listens on.
    :return: The shell fragment, not yet wrapped in a :class:`StepAction`.
    """
    return (
        "MONGOSH_DISABLE_ATLAS_LOCAL_DEV_CLUSTER_CHECK=1 "
        f"mongosh --quiet --port {port} --eval {shlex.quote(js)}"
    )


def _mongosh_eval(js: str, port: int) -> StepAction:
    """Build a ``StepAction`` running one ``mongosh --quiet --eval`` command.

    Every caller here runs before authorization is ever enabled (see
    :meth:`PackagesInstallStrategy._configure_mongod`'s own docstring) —
    deliberately, so this never has to route around MongoDB's localhost
    exception at all: ``rs_initiate`` and ``create_pmm_monitoring_user`` both
    just work, unauthenticated, on any member regardless of topology or
    timing. ``enable_auth`` (:meth:`PackagesInstallStrategy._enable_auth`) is
    what turns authorization on afterward, once the user this creates already
    exists.

    :param js: The JavaScript to evaluate.
    :param port: The port mongod listens on.
    :return: The step action.
    """
    return _shell_step(_mongosh_eval_command(js, port), timeout_s=60)


def _replica_set_uri(hosts: list[str], spec: BootstrapSpec) -> str:
    """Build a connection string that resolves to whichever member is primary.

    Every member is listed as a seed and ``replicaSet`` names the set, so the
    driver discovers the topology and sends a write to the primary rather than
    to whichever member it happened to connect to. ``serverSelectionTimeoutMS``
    makes it *wait* for that primary to exist, which is the other half of the
    same problem - see :meth:`PackagesInstallStrategy._create_pmm_monitoring_user`.

    Unauthenticated on purpose, like every other mongosh call here: these steps
    all run before ``enable_auth``.

    :param hosts: Every member of the set.
    :param spec: The run's bootstrap spec; its port and replica set name are read.
    :return: A ``mongodb://`` URI naming every member.
    """
    seeds = ",".join(f"{host}:{spec.port}" for host in hosts)
    return (
        f"mongodb://{seeds}/?replicaSet={spec.replica_set_name}"
        f"&serverSelectionTimeoutMS={PRIMARY_SELECTION_TIMEOUT_MS}"
    )


def _mongosh_file(js: str, port: int, uri: str | None = None) -> StepAction:
    """Build a ``StepAction`` running ``js`` from a private temp file.

    For JS that embeds a secret: ``--eval`` would put it in mongosh's argv,
    visible in ``ps``. The JS is written through a quoted heredoc to a
    ``mktemp`` file created under ``umask 077`` and removed on exit, and mongosh
    reads it with ``--file``. The heredoc delimiter cannot appear inside ``js``
    as a line of its own because every caller builds ``js`` from ``json.dumps``
    output, which never contains a raw newline.

    :param js: The JavaScript to run.
    :param port: The port mongod listens on. Ignored when ``uri`` is given,
        which carries its own ports.
    :param uri: Connect through this connection string instead of to the local
        member on ``port``. For a step that must reach the primary rather than
        the host it runs on; :func:`_replica_set_uri` builds one. It names no
        credentials, so it is no more secret than the argv it replaces.
    :return: The step action.
    """
    target = f'"{uri}"' if uri else f"--port {port}"
    body = (
        "umask 077\n"
        "js=$(mktemp)\n"
        "trap 'rm -f \"$js\"' EXIT\n"
        "cat > \"$js\" <<'OM_BOOTSTRAP_JS'\n"
        f"{js}\n"
        "OM_BOOTSTRAP_JS\n"
        "MONGOSH_DISABLE_ATLAS_LOCAL_DEV_CLUSTER_CHECK=1 "
        f'mongosh --quiet {target} --file "$js"\n'
    )
    return _shell_step(body, timeout_s=60)


def _require_run_id(spec: BootstrapSpec, step_name: str) -> str:
    """Return ``spec.run_id`` as a string, for a step scoped to its run.

    :param spec: The host's bootstrap spec.
    :param step_name: The step being built, for the error message.
    :return: The run id.
    :raises ValueError: If ``spec.run_id`` is ``None``.
    """
    if spec.run_id is None:
        raise ValueError(f"{step_name} requires spec.run_id")
    return str(spec.run_id)


def _owned_step(body: str, run_id: str, *, timeout_s: int = 30) -> StepAction:
    """Build a rollback step that does nothing on a host this run never installed.

    :param body: The rollback step's shell body.
    :param run_id: The run the rollback belongs to.
    :param timeout_s: How long the step may run, in seconds.
    :return: A step running ``body`` only when :data:`OWNERSHIP_MARKER_PATH`
        holds ``run_id``.
    """
    guard = f'[ "$(cat {OWNERSHIP_MARKER_PATH} 2>/dev/null)" = {shlex.quote(run_id)} ]'
    return _shell_step(f"{guard} || exit 0\n{body}\n", timeout_s=timeout_s)


class PackagesInstallStrategy:
    """Install Percona Server for MongoDB from Percona's Ubuntu/Rocky packages."""

    def plan_steps(self, spec: BootstrapSpec) -> list[str]:  # noqa: ARG002
        """Return this strategy's fixed per-host step names.

        Fixed rather than spec-dependent for phase 1: packages, Ubuntu or Rocky,
        no TLS. A spec asking for TLS would need this to grow a certificate step —
        not built here, since TLS is out of phase-1 scope.

        :param spec: The host's bootstrap spec.
        :return: Step names, in execution order.
        """
        return [
            "pre_check",
            "configure_repository",
            "install_package",
            "distribute_keyfile",
            "configure_mongod",
            "start_service",
            "verify",
        ]

    def build_step(
        self,
        step_name: str,
        host: str,  # noqa: ARG002
        spec: BootstrapSpec,
        params: dict[str, str] | None = None,
    ) -> StepAction:
        """Build the action for one of :meth:`plan_steps`' names.

        :param step_name: One of :meth:`plan_steps`' names.
        :param host: The node name being bootstrapped. Unused by every step below
            today — each builds a command to run *on* ``host``, not one
            referencing it — kept in the signature because
            :class:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy` requires
            it and a future step (e.g. one resolving this host's advertised
            address for ``configure_mongod``) will need it.
        :param spec: The host's bootstrap spec.
        :param params: ``{"key_file_content": ...}`` for ``distribute_keyfile``;
            ignored by every other step.
        :return: What the execution layer needs to run this step.
        :raises ValueError: If ``step_name`` is not one of :meth:`plan_steps`'
            names, ``spec.os`` is not a supported :class:`OperatingSystem`,
            ``distribute_keyfile`` is built without ``params["key_file_content"]``,
            or ``install_package`` is built without ``spec.run_id``.
        """
        builders = {
            "distribute_keyfile": lambda s: self._distribute_keyfile(s, params),
            "pre_check": self._pre_check,
            "configure_repository": self._configure_repository,
            "install_package": self._install_package,
            "configure_mongod": self._configure_mongod,
            "start_service": self._start_service,
            "verify": self._verify,
        }
        try:
            builder = builders[step_name]
        except KeyError:
            raise ValueError(
                f"{step_name!r} is not a PackagesInstallStrategy step; "
                f"expected one of {list(builders)}"
            ) from None
        return builder(spec)

    def _distribute_keyfile(
        self,
        spec: BootstrapSpec,  # noqa: ARG002
        params: dict[str, str] | None,
    ) -> StepAction:
        """Write the replica set's shared keyFile, owned by ``mongod`` and mode 400.

        Runs after ``install_package`` (so the ``mongod`` system user this chowns
        to already exists) and before ``configure_mongod``, which enables
        ``security.keyFile`` pointing at :data:`KEY_FILE_PATH`. The content comes
        from ``params`` rather than being generated here: keyFiles are generated
        once per run and persisted, encrypted, in PMM's Postgres — this strategy
        only ever plants the one copy the stepper hands it, transiently, at
        dispatch time (see
        :class:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy`'s docstring).

        The content travels base64-encoded through the shell builtin ``printf``,
        so it can never end a heredoc early or inject a command, and never
        appears in any process's argv. ``mongod`` ignores whitespace in a
        keyFile, so a trailing newline in the caller's content is harmless.

        :param spec: The host's bootstrap spec. Unused — the keyFile's content is
            entirely determined by ``params``, not by anything in ``spec``.
        :param params: Must contain ``"key_file_content"``.
        :return: The step action.
        :raises ValueError: If ``params`` is missing ``"key_file_content"``.
        """
        if not params or "key_file_content" not in params:
            raise ValueError("distribute_keyfile requires params['key_file_content']")
        encoded = base64.b64encode(params["key_file_content"].encode()).decode("ascii")
        return _shell_step(
            f"printf '%s' {shlex.quote(encoded)} | base64 -d | "
            f"install -m 400 -o mongod -g mongod /dev/stdin {KEY_FILE_PATH}"
        )

    def _pre_check(self, spec: BootstrapSpec) -> StepAction:
        """Verify OS, paths, and disk space before touching anything.

        The decided pre-checks — OS, path, disk space — all read-only and fast
        enough to run inline rather than as a background job:

        - **OS**: the OS's package manager is present.
        - **Path**: no MongoDB already lives on the host — no ``mongod`` on
          ``PATH``, no :data:`CONFIG_PATH`, and ``spec.data_path`` absent or
          empty. This is also what makes rollback safe: ``install_package``
          claims the host for its run with :data:`OWNERSHIP_MARKER_PATH` only
          after this passed, and rollback removes nothing unless that marker
          holds its own run's id.
        - **Disk space**: at least :data:`MIN_DATA_DISK_BYTES` free on the
          filesystem ``spec.data_path`` will live on — that of its nearest
          existing ancestor, since on a fresh host the path itself does not
          exist yet. Walking up rather than falling straight back to ``/``
          matters once ``data_path`` sits under its own mount:
          ``/mnt/mongo/data`` can be absent while ``/mnt/mongo`` is a distinct,
          already-mounted volume. The loop terminates because ``dirname`` of
          ``/`` is ``/``.

        Each failed check names itself on stderr, and a free-space figure ``df``
        could not produce fails the check rather than passing it.

        :param spec: The host's bootstrap spec; its OS and data path are read.
        :return: The step action.
        """
        pkg_manager = self._require_package_manager(spec.os)
        data_path = shlex.quote(spec.data_path)
        body = "\n".join(
            [
                f"command -v {pkg_manager} >/dev/null 2>&1 || "
                f'{{ echo "pre_check: {pkg_manager} not found" >&2; exit 1; }}',
                "if command -v mongod >/dev/null 2>&1; then "
                'echo "pre_check: mongod is already installed" >&2; exit 1; fi',
                f"if [ -e {CONFIG_PATH} ]; then "
                f'echo "pre_check: {CONFIG_PATH} already exists" >&2; exit 1; fi',
                f'if [ -d {data_path} ] && [ -n "$(ls -A {data_path})" ]; then '
                f'echo "pre_check: "{data_path}" is not empty" >&2; exit 1; fi',
                f"target={data_path}",
                'while [ ! -d "$target" ]; do target="$(dirname "$target")"; done',
                'avail=$(df --output=avail -B1 "$target" | tail -1)',
                "case \"$avail\" in ''|*[!0-9]*) "
                'echo "pre_check: could not measure free space at $target" >&2; '
                "exit 1;; esac",
                f'if [ "$avail" -lt {MIN_DATA_DISK_BYTES} ]; then '
                f'echo "pre_check: less than {MIN_DATA_DISK_BYTES} bytes free '
                'for the data directory" >&2; exit 1; fi',
            ]
        )
        return _shell_step(body)

    def _configure_repository(self, spec: BootstrapSpec) -> StepAction:
        """Install ``percona-release`` and enable the requested PSMDB channel.

        :param spec: The host's bootstrap spec; its OS and MongoDB version are read.
        :return: The step action.
        :raises ValueError: If ``spec.os`` is not a supported :class:`OperatingSystem`.
        """
        channel = _psmdb_channel(spec.mongodb_version)
        if spec.os is OperatingSystem.UBUNTU:
            command = (
                "curl -fsSL -o /tmp/percona-release.deb "
                "https://repo.percona.com/apt/percona-release_latest.generic_all.deb && "
                "dpkg -i /tmp/percona-release.deb && "
                f"percona-release setup -y {shlex.quote(channel)}"
            )
        elif spec.os is OperatingSystem.ROCKY:
            command = (
                "dnf install -y "
                "https://repo.percona.com/yum/percona-release-latest.noarch.rpm && "
                f"percona-release setup -y {shlex.quote(channel)}"
            )
        else:
            raise ValueError(f"unsupported OperatingSystem: {spec.os!r}")
        return _shell_step(command, timeout_s=120)

    def _install_package(self, spec: BootstrapSpec) -> StepAction:
        """Claim the host for this run, then install ``percona-server-mongodb``.

        The marker, holding the run's id, goes first so a rollback of a
        half-finished install still cleans up. It is only ever written after
        ``pre_check`` proved the host had no MongoDB of its own — see the module
        docstring.

        :param spec: The host's bootstrap spec; its OS and run id are read.
        :return: The step action.
        :raises ValueError: If ``spec.run_id`` is ``None``.
        """
        run_id = _require_run_id(spec, "install_package")
        pkg_manager = self._require_package_manager(spec.os)
        install = "apt-get install -y" if pkg_manager == "apt-get" else "dnf install -y"
        return _shell_step(
            f"printf '%s\\n' {shlex.quote(run_id)} > {OWNERSHIP_MARKER_PATH}\n"
            f"{install} percona-server-mongodb",
            timeout_s=300,
        )

    def _configure_mongod(self, spec: BootstrapSpec) -> StepAction:
        """Write ``mongod.conf`` enabling replication, with authorization left off.

        Deliberately does **not** set ``security.authorization``/``keyFile`` here,
        even though :data:`KEY_FILE_PATH` already exists on disk (planted by
        ``distribute_keyfile``, immediately before this step): MongoDB's localhost
        exception — the unauthenticated window a fresh member normally uses to
        bootstrap its first user — is unreliable once a replica set already has
        more than one member. Confirmed against a real multi-member run, not a
        theoretical concern: 40 consecutive, freshly-connected ``createUser``
        attempts all failed identically once the first one did, because the
        exception closes *permanently* for that mongod's whole lifetime the
        moment any privileged op on it fails once — not just for the one
        connection that failed it. Retrying, waiting for a stable primary, or
        avoiding mongosh's own extra connections none of it helped; the only
        reliable fix is to never need the exception at all. So authorization
        stays off through ``rs_initiate``/``create_pmm_monitoring_user``, and
        :meth:`_enable_auth` — a finalize step, dispatched only once that user
        already exists — turns it on afterward, per host.

        Also creates ``spec.data_path``, owned by ``mongod``, rather than
        assuming the package's own post-install already did — confirmed
        against a real failure that it does not: mongod exits immediately on
        first start with ``NonExistentPath: Data directory /var/lib/mongo not
        found``, and ``start_service`` (``systemctl restart``) reports
        success regardless, since ``Type=forking`` only waits for the initial
        fork, not for mongod's own startup logic to run. ``verify``, a step
        later, is what actually surfaces the failure — by then the run has
        already reported ``start_service`` as done.

        Creates each directory only when absent (``[ -d ... ] ||``), not
        unconditionally: ``install -d`` reapplies ``-m``/``-o``/``-g`` to a
        directory that already exists too, and a ``log_path`` of
        ``/var/log/mongod.log`` — a plausible operator value, and the mongod
        default on some layouts — has ``/var/log`` as its dirname. An
        unconditional ``install -d`` there hands the host's shared log
        directory to ``mongod:mongod`` at 750, breaking logging for
        everything else on the box.

        Sets ``processManagement.fork``/``pidFilePath`` for the same reason:
        the packaged ``mongod.service`` is ``Type=forking``, so systemd waits for
        mongod itself to daemonize and write :data:`PID_FILE_PATH`. Without
        ``fork: true`` mongod runs in the foreground indefinitely — confirmed
        against a real run where mongod started and stayed healthy, but systemd's
        default 90s ``TimeoutStartSec`` elapsed waiting for a fork that was never
        coming and killed it, so ``verify`` found nothing listening on 27017 a
        step later, again after ``start_service`` had already reported success.

        ``systemLog.path`` is required alongside ``fork: true`` — mongod refuses
        to start at all otherwise (``BadValue: --fork has to be used with
        --logpath or --syslog``), confirmed against a real run once the
        fork-without-a-logpath combination above was fixed. The unit's own
        ``STDOUT``/``STDERR`` redirects in ``/etc/default/mongod`` do not stand
        in for this: those capture only the pre-fork parent, which prints
        nothing once mongod backgrounds itself.

        Also creates ``spec.log_path``'s directory, owned by ``mongod``, the
        same way and for the same reason as ``spec.data_path`` above — a gap
        this one had until a real bootstrap run against a bare host (no
        pre-existing ``/var/log/mongo``, unlike the sandbox's own database
        topology images) confirmed it the same way: mongod's control process
        exits immediately (``Can't initialize rotatable log file :: caused by
        :: Failed to open <path>``) if the directory the configured log path
        names does not already exist, and ``start_service`` again reports
        success regardless, for the same ``Type=forking`` reason. The
        package's own post-install cannot be assumed to have created it: it
        defaults to a directory (``/var/log/mongo`` on both Ubuntu and Rocky)
        that only matches ``spec.log_path`` by coincidence, and the wizard's
        own default (``/var/log/mongodb/mongod.log``) does not.

        :param spec: The host's bootstrap spec.
        :return: The step action.
        """
        config = _mongod_config(spec, with_auth=False)
        quoted_data_path = shlex.quote(spec.data_path)
        quoted_log_dir = shlex.quote(posixpath.dirname(spec.log_path))
        command = (
            f"{{ [ -d {quoted_data_path} ] || "
            f"install -d -m 750 -o mongod -g mongod {quoted_data_path} ; }} && "
            f"{{ [ -d {quoted_log_dir} ] || "
            f"install -d -m 750 -o mongod -g mongod {quoted_log_dir} ; }} && "
            f"cat > {CONFIG_PATH} <<'MONGOD_CONF'\n{config}MONGOD_CONF\n"
        )
        return _shell_step(command)

    def _start_service(self, spec: BootstrapSpec) -> StepAction:  # noqa: ARG002
        """Enable the ``mongod`` systemd unit and (re)start it on the config just written.

        Explicitly ``restart``, not ``enable --now``: ``install_package`` may have
        left ``mongod`` already running, and ``enable --now`` only starts a unit
        that is not already active. Against one the package started it is a
        no-op, so the process stays live on whatever config it booted with, which
        is the package's own default and carries no ``replication`` block. It
        therefore never picks up :meth:`_configure_mongod`'s rewrite of
        :data:`CONFIG_PATH` a step earlier, and ``verify`` right after only pings
        the server, so that does not catch it either.

        Whether the package pre-starts the service is not something this step
        needs to distinguish: Ubuntu's ``.deb`` postinst does, Rocky's ``%post``
        does not, and ``restart`` is correct for both, since restarting a unit
        that ``install_package`` never started behaves exactly like starting it.

        :param spec: The host's bootstrap spec. Unused.
        :return: The step action.
        """
        return _shell_step(
            "systemctl enable mongod && systemctl restart mongod", timeout_s=60
        )

    def _verify(self, spec: BootstrapSpec) -> StepAction:
        """Confirm ``mongod`` answers before declaring this host done.

        :param spec: The host's bootstrap spec; only its port is read.
        :return: The step action.
        """
        return _mongosh_eval("db.adminCommand('ping').ok", spec.port)

    def _require_package_manager(self, os_: OperatingSystem) -> str:
        """Map a supported OS to its package manager, or reject an unsupported one.

        :param os_: The host's OS.
        :return: The package manager's command name.
        :raises ValueError: If ``os_`` is not a supported :class:`OperatingSystem`.
        """
        if os_ is OperatingSystem.UBUNTU:
            return "apt-get"
        if os_ is OperatingSystem.ROCKY:
            return "dnf"
        raise ValueError(f"unsupported OperatingSystem: {os_!r}")

    def plan_run_steps(self, spec: BootstrapSpec) -> list[str]:  # noqa: ARG002
        """Return this strategy's fixed run-level step names.

        Both need every member's mongod already running (every host's
        :meth:`plan_steps` succeeded) — the stepper's job to wait for, not this
        method's.

        :param spec: The run's bootstrap spec.
        :return: Step names, in execution order.
        """
        return ["rs_initiate", "create_pmm_monitoring_user"]

    def build_run_step(
        self,
        step_name: str,
        hosts: list[str],
        spec: BootstrapSpec,
        params: dict[str, str] | None = None,
    ) -> StepAction:
        """Build the action for one of :meth:`plan_run_steps`' names.

        :param step_name: One of :meth:`plan_run_steps`' names.
        :param hosts: Every host in this run — see
            :meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.build_run_step`'s
            own docstring for why index 0 is where this action actually runs.
        :param spec: The run's bootstrap spec.
        :param params: ``{"username": ..., "password": ...}`` for
            ``create_pmm_monitoring_user``; ignored by ``rs_initiate``.
        :return: What the execution layer needs to run this step.
        :raises ValueError: If ``step_name`` is not one of :meth:`plan_run_steps`'
            names, or ``create_pmm_monitoring_user`` is built without both
            ``params`` entries.
        """
        if step_name == "rs_initiate":
            return self._rs_initiate(hosts, spec)
        if step_name == "create_pmm_monitoring_user":
            return self._create_pmm_monitoring_user(hosts, spec, params)
        raise ValueError(
            f"{step_name!r} is not a PackagesInstallStrategy run step; "
            f"expected one of {self.plan_run_steps(spec)}"
        )

    def _rs_initiate(self, hosts: list[str], spec: BootstrapSpec) -> StepAction:
        """Initiate the replica set from its seed member (``hosts[0]``).

        Per-member priority/votes/hidden/delay come from ``spec.member_configs``,
        keyed by host — a host missing from it gets :class:`MemberConfig`'s own
        defaults.

        Tolerates ``rs.initiate`` already having succeeded: a dispatch that
        times out at the PMM Extensions/Nomad layer *after* the command actually took
        effect on the host looks, to the stepper's retry policy, exactly like
        one that never ran — it retries. A bare retry fails with
        ``AlreadyInitialized`` and, retries exhausted, triggers rollback
        (including ``rm -rf`` of the data directory), tearing down a replica
        set that had already initiated successfully. Swallowing exactly that
        one ``codeName`` makes the retry a no-op instead.

        :param hosts: Every member, in run order; ``hosts[0]`` is the seed.
        :param spec: The run's bootstrap spec.
        :return: The step action.
        """
        members = []
        for index, host in enumerate(hosts):
            member = spec.member_configs.get(host, MemberConfig())
            entry = {
                "_id": index,
                "host": f"{host}:{spec.port}",
                "priority": member.priority,
                "votes": 1 if member.votes else 0,
                "hidden": member.hidden,
            }
            if member.delay_secs:
                entry["secondaryDelaySecs"] = member.delay_secs
            members.append(entry)
        config = {"_id": spec.replica_set_name, "members": members}
        js = (
            f"try {{ rs.initiate({json.dumps(config)}) }} "
            "catch (e) { if (e.codeName !== 'AlreadyInitialized') throw e }"
        )
        return _mongosh_eval(js, spec.port)

    def _create_pmm_monitoring_user(
        self, hosts: list[str], spec: BootstrapSpec, params: dict[str, str] | None
    ) -> StepAction:
        """Create the MongoDB user PMM's ``mongodb_exporter`` authenticates as.

        Created once for the whole set — MongoDB replicates ``admin.system.users``
        to every other member automatically, so this never needs to run per host.
        ``params`` rather than a generated value here for the same reason
        ``distribute_keyfile`` takes one: PMM's encrypted Postgres is this
        secret's durable home, not this strategy. Run through
        :func:`_mongosh_file`, so the password never appears in any argv.

        Connects through :func:`_replica_set_uri` rather than to the member this
        action runs on, and that is the whole point of the indirection.
        ``createUser`` is a write, so it needs the primary — and the member this
        runs on is ``hosts[0]``, the seed, which is *not* reliably the primary.
        ``rs.initiate`` only proposes the config; the election that follows is
        open to every member, and any of them can win it.

        Confirmed against a real multi-member run, not a theoretical race: a
        different member won the election, every dispatch of this step died
        with ``MongoServerError: not primary``, and retrying could not help
        because each attempt went to the same secondary - so the run exhausted
        its retries and rolled back a set that was otherwise fine. An identical
        run whose seed happened to win succeeded, which is what made this look
        intermittent rather than wrong.

        The URI fixes the ordering too: the driver waits up to
        :data:`PRIMARY_SELECTION_TIMEOUT_MS` for a primary to exist, so this no
        longer depends on an election having finished before it is dispatched.

        Tolerates the user already existing, for the same reason
        :meth:`_rs_initiate` tolerates ``AlreadyInitialized``: a dispatch that
        times out after ``createUser`` already took effect looks, to the
        retry policy, like one that never ran. A bare retry fails with
        ``UserAlreadyExists`` (51003) and, retries exhausted, rolls the whole
        run back over a user that was actually created successfully.

        :param hosts: Every member of the set, used to build the connection
            string. Not an execution target: index 0 is still where this action
            runs, it just no longer has to be the primary.
        :param spec: The run's bootstrap spec; its port and replica set name are
            read.
        :param params: Must contain ``"username"`` and ``"password"``.
        :return: The step action.
        :raises ValueError: If ``params`` is missing ``"username"`` or
            ``"password"``.
        """
        if not params or "username" not in params or "password" not in params:
            raise ValueError(
                "create_pmm_monitoring_user requires params['username'] and "
                "params['password']"
            )
        username = json.dumps(params["username"])
        create = (
            f"db.getSiblingDB('admin').createUser({{"
            f"user: {username}, "
            f"pwd: {json.dumps(params['password'])}, "
            f"roles: {json.dumps(PMM_MONITORING_USER_ROLES)}"
            f"}})"
        )
        js = f"if (!db.getSiblingDB('admin').getUser({username})) {{ {create} }}"
        return _mongosh_file(js, spec.port, uri=_replica_set_uri(hosts, spec))

    def plan_finalize_steps(self, spec: BootstrapSpec) -> list[str]:  # noqa: ARG002
        """Return this strategy's fixed per-host finalize step names.

        :param spec: The host's bootstrap spec.
        :return: Step names, in execution order.
        """
        return ["enable_auth"]

    def build_finalize_step(
        self,
        step_name: str,
        host: str,  # noqa: ARG002
        spec: BootstrapSpec,
        params: dict[str, str] | None = None,  # noqa: ARG002
    ) -> StepAction:
        """Build the action for one of :meth:`plan_finalize_steps`' names.

        :param step_name: One of :meth:`plan_finalize_steps`' names.
        :param host: The node name being finalized. Unused — see
            :meth:`build_step`'s own docstring on why the signature carries it
            anyway.
        :param spec: The host's bootstrap spec.
        :param params: Unused — ``enable_auth`` needs no secret it doesn't
            already have on disk (:data:`KEY_FILE_PATH`, planted by
            ``distribute_keyfile``).
        :return: What the execution layer needs to run this step.
        :raises ValueError: If ``step_name`` is not one of
            :meth:`plan_finalize_steps`' names.
        """
        if step_name == "enable_auth":
            return self._enable_auth(spec)
        raise ValueError(
            f"{step_name!r} is not a PackagesInstallStrategy finalize step; "
            f"expected one of {self.plan_finalize_steps(spec)}"
        )

    def _enable_auth(self, spec: BootstrapSpec) -> StepAction:
        """Turn MongoDB authorization on, now that the first user exists.

        Rewrites the *same* :data:`CONFIG_PATH` :meth:`_configure_mongod` wrote,
        adding exactly the ``security`` block that method left out — see its own
        docstring for why authorization has to stay off until now. Restarts
        ``mongod`` to pick the new config up: unlike ``processManagement.fork``
        or ``systemLog.path``, ``security.authorization`` cannot be changed on a
        running server, only at startup.

        A plain ``restart`` rather than ``stop`` then ``start``: systemd runs
        them as one unit transaction either way, and a two-step version would
        leave a window (however short) where ``mongod`` isn't running at all if
        something between the two commands failed.

        Joins the write, the restart, and a readiness probe with ``&&``, not a
        bare newline: ``mongod.service`` is ``Type=forking`` (see
        :meth:`_configure_mongod`'s own docstring), so ``systemctl restart``
        reports success once mongod forks, not once it actually accepted the
        new config — a config write that failed (read-only filesystem, full
        disk) would otherwise still restart mongod on the *old*, auth-less
        config, and the step would report SUCCEEDED with authorization still
        off. The probe reuses the same unauthenticated ``ping`` ``verify``
        uses: MongoDB answers it without credentials even with
        ``security.authorization: enabled``, so this proves mongod actually
        came back up on the new config rather than forking and then exiting.

        :param spec: The host's bootstrap spec.
        :return: The step action.
        """
        config = _mongod_config(spec, with_auth=True)
        readiness = _mongosh_eval_command("db.adminCommand('ping').ok", spec.port)
        command = (
            f"cat > {CONFIG_PATH} <<'MONGOD_CONF' && systemctl restart mongod "
            f"&& {readiness}\n{config}MONGOD_CONF\n"
        )
        return _shell_step(command, timeout_s=120)

    def plan_rollback_steps(self, spec: BootstrapSpec) -> list[str]:  # noqa: ARG002
        """Return this strategy's fixed per-host rollback step names.

        The reverse of :meth:`plan_steps`, undoing what a host's forward steps
        did rather than mirroring their names one-for-one: there is nothing to
        undo for ``pre_check``/``verify`` (read-only), and ``configure_repository``
        is left alone deliberately — removing ``percona-release`` would affect
        anything else on the host that depends on it, well outside this run's
        blast radius.

        Every step is a no-op on a host whose :data:`OWNERSHIP_MARKER_PATH`
        does not hold this run's id — see the module docstring.

        :param spec: The host's bootstrap spec.
        :return: Step names, in the order rollback applies them.
        """
        return [
            "stop_service",
            "remove_config",
            "remove_keyfile",
            "purge_package",
            "remove_data",
        ]

    def build_rollback_step(
        self,
        step_name: str,
        host: str,  # noqa: ARG002
        spec: BootstrapSpec,
    ) -> StepAction:
        """Build the action for one of :meth:`plan_rollback_steps`' names.

        :param step_name: One of :meth:`plan_rollback_steps`' names.
        :param host: The node name being rolled back. Unused — same reasoning as
            :meth:`build_step`'s own ``host`` parameter.
        :param spec: The host's bootstrap spec.
        :return: What the execution layer needs to run this step.
        :raises ValueError: If ``step_name`` is not one of
            :meth:`plan_rollback_steps`' names, ``spec.run_id`` is ``None``, or
            ``spec.os`` is not a supported :class:`OperatingSystem`.
        """
        builders = {
            "stop_service": self._rollback_stop_service,
            "remove_config": self._rollback_remove_config,
            "remove_keyfile": self._rollback_remove_keyfile,
            "purge_package": self._rollback_purge_package,
            "remove_data": self._rollback_remove_data,
        }
        try:
            builder = builders[step_name]
        except KeyError:
            raise ValueError(
                f"{step_name!r} is not a PackagesInstallStrategy rollback step; "
                f"expected one of {list(builders)}"
            ) from None
        run_id = _require_run_id(spec, step_name)
        return builder(spec, run_id)

    def _rollback_stop_service(self, spec: BootstrapSpec, run_id: str) -> StepAction:  # noqa: ARG002
        """Stop and disable ``mongod``, tolerant of it never having started.

        :param spec: The host's bootstrap spec. Unused.
        :param run_id: The run the rollback belongs to.
        :return: The step action.
        """
        return _owned_step(
            "systemctl disable --now mongod || true", run_id, timeout_s=60
        )

    def _rollback_remove_config(self, spec: BootstrapSpec, run_id: str) -> StepAction:  # noqa: ARG002
        """Remove the config file ``configure_mongod`` wrote.

        :param spec: The host's bootstrap spec. Unused.
        :param run_id: The run the rollback belongs to.
        :return: The step action.
        """
        return _owned_step(f"rm -f {CONFIG_PATH}", run_id)

    def _rollback_remove_keyfile(self, spec: BootstrapSpec, run_id: str) -> StepAction:  # noqa: ARG002
        """Remove the keyFile ``distribute_keyfile`` wrote.

        :param spec: The host's bootstrap spec. Unused.
        :param run_id: The run the rollback belongs to.
        :return: The step action.
        """
        return _owned_step(f"rm -f {KEY_FILE_PATH}", run_id)

    def _rollback_purge_package(self, spec: BootstrapSpec, run_id: str) -> StepAction:
        """Purge the ``percona-server-mongodb`` package ``install_package`` installed.

        Tolerant of the install itself having failed part-way: the marker is
        planted before the package manager runs, so a host whose
        ``install_package`` failed still rolls back, and the purge tolerates a
        package that never landed.

        :param spec: The host's bootstrap spec; only its OS is read.
        :param run_id: The run the rollback belongs to.
        :return: The step action.
        """
        pkg_manager = self._require_package_manager(spec.os)
        remove = (
            "apt-get remove -y --purge percona-server-mongodb"
            if pkg_manager == "apt-get"
            else "dnf remove -y percona-server-mongodb"
        )
        return _owned_step(f"{remove} || true", run_id, timeout_s=120)

    def _rollback_remove_data(self, spec: BootstrapSpec, run_id: str) -> StepAction:
        """Remove the data directory, then the ownership marker, as the last step.

        :param spec: The host's bootstrap spec; only its data path is read.
        :param run_id: The run the rollback belongs to.
        :return: The step action.
        """
        body = f"rm -rf {shlex.quote(spec.data_path)}\nrm -f {OWNERSHIP_MARKER_PATH}"
        return _owned_step(body, run_id, timeout_s=60)
