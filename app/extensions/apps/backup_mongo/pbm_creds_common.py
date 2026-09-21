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

"""Define the single canonical PBM credential-resolution preamble.

The nine ``backup_mongo`` payload scripts are shipped to executors by ``file://``
reference and read directly from disk, so they cannot import a shared module at
execution time. Instead, each marked block below is the one canonical definition of
its generated region -- the PBM credential-resolution helpers between
:data:`PREAMBLE_BEGIN` / :data:`PREAMBLE_END`, the :func:`_apply_pbm_config`
storage-apply helper between :data:`CONFIG_APPLY_BEGIN` / :data:`CONFIG_APPLY_END`
(carried only by the config/logical/physical payloads that apply the config), and
the :func:`_append_pbm_restore_yes_if_supported` probe between
:data:`RESTORE_YES_BEGIN` / :data:`RESTORE_YES_END` (logical/physical restore
payloads only), and the textfile-collector metric writer between
:data:`TEXTFILE_BEGIN` / :data:`TEXTFILE_END` (the logical/physical/snapshot backup
payloads that emit PMM stale-backup metrics). All are materialized verbatim into each payload's marked region by
``scripts/gen_pbm_payloads.py`` and guarded in-sync (and behaviorally) by
``tests/app/extensions/apps/backup_mongo/test_pbm_payload_preamble.py`` (creds/config/restore
regions) and ``tests/app/extensions/apps/backup_mongo/test_pbm_textfile_collector.py`` (the
textfile-collector region).

A hardening fix to credential handling therefore lands here once and propagates
to all nine payloads via the regen step, instead of drifting across nine copies.
The two ``_creds_path`` shapes are both served here: :func:`_creds_path` reads the
``NOMAD_META_CONFIG`` environment variable (the backup/restore standalone legs),
and :func:`_creds_path_from_config` reads an already-parsed config dict (the
restore legs that load ``script_config``). The ``config_source`` word ("backup"
or "restore") is passed by the payload's call site so each script emits exactly
the stderr message it does today.

This module is deliberately importable and linted (unlike the payloads, which are
excluded from ruff), so the helpers are exercised directly by test. Only the
region bodies are copied -- imports are not -- so a region reaching for a module
its payloads do not already import needs that import hand-added to each opted-in
payload header before the regen step.
"""

import contextlib
import getpass
import json
import os
import pwd
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

import yaml

PREAMBLE_BEGIN = "# --- BEGIN GENERATED PBM CREDS PREAMBLE ---"
PREAMBLE_END = "# --- END GENERATED PBM CREDS PREAMBLE ---"
CONFIG_APPLY_BEGIN = "# --- BEGIN GENERATED PBM CONFIG APPLY ---"
CONFIG_APPLY_END = "# --- END GENERATED PBM CONFIG APPLY ---"
RESTORE_YES_BEGIN = "# --- BEGIN GENERATED PBM RESTORE YES ---"
RESTORE_YES_END = "# --- END GENERATED PBM RESTORE YES ---"
TEXTFILE_BEGIN = "# --- BEGIN GENERATED PBM TEXTFILE COLLECTOR ---"
TEXTFILE_END = "# --- END GENERATED PBM TEXTFILE COLLECTOR ---"


# --- BEGIN GENERATED PBM CREDS PREAMBLE ---
def _creds_path(config_source: str) -> str:
    """Resolve the MongoDB URI credentials path from ``NOMAD_META_CONFIG``.

    :param config_source: The config surface named in the error message, either
        ``"backup"`` or ``"restore"``.
    :return: The credentials-file path (from the config or the ``$HOME`` fallback).
    """
    config_yaml = os.environ.get("NOMAD_META_CONFIG")
    if config_yaml:
        try:
            config = yaml.safe_load(config_yaml)
            if isinstance(config, dict):
                path = config.get("credentials_path")
                if path:
                    return path
        except yaml.YAMLError as err:
            print(
                f"Failed to parse NOMAD_META_CONFIG as YAML: {err}. Falling back to HOME-based credentials path.",
                file=sys.stderr,
            )
    envhome = os.environ.get("HOME")
    if not envhome:
        print(
            f"PBM credentials path not set (credentials_path in {config_source} config) and HOME is unset",
            file=sys.stderr,
        )
        sys.exit(1)
    return f"{envhome}/.mongodb_uri"


def _creds_path_from_config(config: object, config_source: str = "restore") -> str:
    """Resolve the MongoDB URI credentials path from an already-parsed config dict.

    :param config: The parsed restore/backup config value. Anything other than a
        ``dict`` (``None``, a bare scalar, a list, ...) is treated as missing config,
        since ``yaml.safe_load`` can legally return a truthy non-dict.
    :param config_source: The config surface named in the error message, either
        ``"backup"`` or ``"restore"``.
    :return: The credentials-file path (from the config or the ``$HOME`` fallback).
    """
    if isinstance(config, dict):
        path = config.get("credentials_path")
        if isinstance(path, str) and path:
            return path
    envhome = os.environ.get("HOME")
    if not envhome:
        print(
            f"PBM credentials path not set (credentials_path in {config_source} config) and HOME is unset",
            file=sys.stderr,
        )
        sys.exit(1)
    return f"{envhome}/.mongodb_uri"


def pbm_creds(creds_path: str) -> str:
    """Read the MongoDB URI from ``creds_path``, exiting 1 on any read failure.

    :param creds_path: The resolved credentials-file path to read.
    :return: The stripped MongoDB URI read from the file.
    """
    try:
        with open(creds_path, encoding="utf-8") as fpt:
            return fpt.read().strip()
    except FileNotFoundError as err:
        print(f"Credentials file not found: {creds_path}: {err}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as err:
        print(
            f"Permission denied reading credentials file {creds_path}: {err}",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as err:
        print(f"Error reading credentials file {creds_path}: {err}", file=sys.stderr)
        sys.exit(1)
    except Exception as err:
        print(f"Error reading credentials file {creds_path}: {err}", file=sys.stderr)
        sys.exit(1)


# --- END GENERATED PBM CREDS PREAMBLE ---


# --- BEGIN GENERATED PBM CONFIG APPLY ---
#: What ``pbm config`` prints in place of a secret on a whole-document read. A
#: targeted ``pbm config <dotted.key>`` returns the real value instead, which is the
#: only way to carry credentials through a read-merge-write cycle.
_PBM_SECRET_MASK = "***"  # noqa: S105 - a mask PBM prints, not a credential


def _pbm_config_read(*args: str) -> object | None:
    """Return ``pbm config [args] -o json`` parsed, or None when it cannot be read.

    None covers "PBM has no config yet" and "PBM answered something unparseable"
    alike: both mean there is nothing to merge onto, which is the first-run case.

    :param args: Extra arguments, e.g. a single dotted key to read one value.
    :return: The parsed answer, or None.
    """
    try:
        read_cmd = ["pbm", "config", *args, "-o", "json"]
        proc = subprocess.Popen(  # noqa: S603 # nosec B603
            read_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = proc.communicate()
    except OSError as err:
        print(f"Failed to run pbm config: {err}", file=sys.stderr)
        sys.exit(1)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(stdout.decode())
    except (ValueError, UnicodeDecodeError):
        return None


def _pbm_config_unmask(node: object, path: tuple, masked: list) -> object:
    """Return ``node`` with every masked secret replaced by its real value.

    Walks the document PBM returned and, for each value equal to the mask, reads
    that one key back on its own. Paths that cannot be recovered are collected in
    ``masked`` so the caller can refuse to write rather than persist the literal
    ``***`` over a working credential.

    :param node: The current node of the parsed config document.
    :param path: The dotted-key path to ``node``, as a tuple of segments.
    :param masked: Out-list of dotted keys whose real value could not be read.
    :return: The node with secrets restored.
    """
    if isinstance(node, dict):
        return {
            key: _pbm_config_unmask(value, (*path, key), masked)
            for key, value in node.items()
        }
    if node != _PBM_SECRET_MASK:
        return node
    dotted = ".".join(path)
    answer = _pbm_config_read(dotted)
    value = answer.get("value") if isinstance(answer, dict) else None
    if not isinstance(value, str) or value == _PBM_SECRET_MASK:
        masked.append(dotted)
        return node
    return value


def _pbm_config_merge(base: dict, over: dict) -> dict:
    """Return ``base`` deep-merged with ``over``, with ``over`` winning.

    Only keys ``over`` names are touched, so anything PMM Extensions does not model -- the S3
    credentials above all -- survives untouched.

    :param base: The document already on the cluster.
    :param over: The PMM Extensions-managed values to lay over it.
    :return: The merged document.
    """
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _pbm_config_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_pbm_config(config: dict) -> None:
    """Apply the PMM Extensions-managed PBM config, merged onto what the cluster already has.

    PBM's configuration is cluster-wide and ``pbm config --file`` *replaces* the
    whole document, so writing only what PMM Extensions models would delete everything else --
    S3 credentials included, which is exactly what used to happen on every backup.
    This reads the current document first, restores the secrets PBM masks on a bulk
    read, lays the PMM Extensions-managed values over it, and writes the result. Credentials
    therefore round-trip untouched: OM never changes them, and they stay the
    business of whoever has the ``pbm`` CLI on the host.

    Terminates the process (``sys.exit`` non-zero) rather than raising when the task
    dir is unset, ``pbm`` cannot be run, a masked secret cannot be recovered, or PBM
    rejects the config -- for example an unreachable or non-existent S3 bucket -- so
    a caller never silently continues against the wrong storage.

    :param config: The parsed task config; PMM Extensions only and ``None``-valued keys are
        dropped before it is merged into the PBM config file.
    """
    extensions_only_keys = frozenset({"credentials_path", "credentialsPath"})
    # Selective --ns flags live under backup for NOMAD_META_CONFIG readers but are
    # not valid PBM config keys, so strip them before ``pbm config --file``.
    extensions_only_backup_keys = frozenset({"namespaces", "withUsersAndRoles"})
    pbm_config = {}
    for key, value in config.items():
        if key in extensions_only_keys or value is None:
            continue
        if key == "backup" and isinstance(value, dict):
            backup_value = {
                backup_key: nested_value
                for backup_key, nested_value in value.items()
                if backup_key not in extensions_only_backup_keys
            }
            pbm_config[key] = backup_value
            continue
        pbm_config[key] = value
    current = _pbm_config_read()
    if isinstance(current, dict):
        masked = []
        current = _pbm_config_unmask(current, (), masked)
        if masked:
            print(
                "Refusing to apply the configuration: PBM masks "
                f"{', '.join(sorted(masked))} and the real value could not be read "
                "back, so writing this document would overwrite the stored "
                "credential with '***'. Check the pbm CLI on this host.",
                file=sys.stderr,
            )
            sys.exit(1)
        pbm_config = _pbm_config_merge(current, pbm_config)
    task_dir = os.environ.get("NOMAD_TASK_DIR")
    if not task_dir:
        print(
            "NOMAD_TASK_DIR is not set; cannot write the PBM config file.",
            file=sys.stderr,
        )
        sys.exit(1)
    config_file = f"{task_dir}/script_config"
    # 0600 and removed below: unlike the pre-merge version this document carries the
    # real S3 secret, so it must not outlive the command or be readable by anyone
    # else on the host. A file rather than ``--set`` arguments precisely because
    # process arguments are world-readable through /proc.
    fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.dump(pbm_config, f, default_flow_style=False, allow_unicode=True)
    cmd = ["pbm", "config", "--file", config_file]
    try:
        proc = subprocess.Popen(cmd)  # noqa: S603 # nosec B603
        proc.wait()
        ret_code = proc.poll()
        if ret_code != 0:
            print(
                "Failed to apply the task storage configuration to PBM "
                f"(pbm config --file exited {ret_code}). Verify the configured S3 "
                "bucket/region/endpoint exist and are reachable.",
                file=sys.stderr,
            )
            sys.exit(ret_code)
    except OSError as err:
        print(f"Failed to run pbm config: {err}", file=sys.stderr)
        sys.exit(1)
    finally:
        with contextlib.suppress(OSError):
            Path(config_file).unlink()


# --- END GENERATED PBM CONFIG APPLY ---


# --- BEGIN GENERATED PBM RESTORE YES ---
_PBM_RESTORE_HELP_TIMEOUT_SECONDS = 10


def _append_pbm_restore_yes_if_supported(cmd: list[str]) -> None:
    """Append ``--yes`` to ``cmd`` when ``pbm restore --help`` advertises it.

    PBM >=2.14 prompts for confirmation unless ``--yes`` is passed; under Nomad
    there is no TTY, so that prompt cancels and the restore can report SUCCESS
    while Mongo stays empty. Older builds do not know ``--yes``, so only add it
    when help text advertises the flag. Bound the help probe with a short
    timeout so a hung ``pbm`` cannot stall the restore forever.

    :param cmd: The restore command list being built (mutated in place).
    """
    try:
        help_cmd = ["pbm", "restore", "--help"]
        help_proc = subprocess.run(  # noqa: S603 # nosec B603
            help_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PBM_RESTORE_HELP_TIMEOUT_SECONDS,
        )
        help_text = f"{help_proc.stdout or ''}{help_proc.stderr or ''}"
        if "--yes" in help_text:
            cmd.append("--yes")
    except OSError as err:
        print(
            f"Could not probe pbm restore --help for --yes support: {err}",
            file=sys.stderr,
        )
    except subprocess.TimeoutExpired as err:
        print(
            f"pbm restore --help timed out while probing for --yes support: {err}",
            file=sys.stderr,
        )


# --- END GENERATED PBM RESTORE YES ---


# --- BEGIN GENERATED PBM TEXTFILE COLLECTOR ---
_TEXTFILE_COLLECTOR_DEFAULT_SUBPATH = "pmm/collectors/textfile-collector/low-resolution"


def _backup_user_home() -> Path | None:
    """Return the home directory of the user the backup runs as, or ``None``.

    Resolve the user the MySQL/PostgreSQL payloads resolve --
    ``PERCONA_BACKUP_USER``, then ``SUDO_USER``, then the login name -- and read
    the home from the passwd database rather than ``$HOME``. Under ``sudo`` the
    two disagree, and honouring ``$HOME`` would drop this payload's ``.prom``
    files somewhere PMM never scrapes while the MySQL/PG ones land correctly.

    Unlike those payloads the lookup is lazy and guarded: a host with no passwd
    entry for the resolved name must cost the metric, not abort the backup.

    :return: The backup user's home directory, or ``None`` when it cannot be
        resolved to an absolute path.
    """
    try:
        user = (
            os.environ.get("PERCONA_BACKUP_USER")
            or os.environ.get("SUDO_USER")
            or getpass.getuser()
        )
        home = Path(pwd.getpwnam(user).pw_dir)
    except (KeyError, OSError):
        return None
    return home if home.is_absolute() else None


def _textfile_collector_dir() -> Path | None:
    """Return the PMM textfile-collector directory for backup ``.prom`` files.

    Mirror the MySQL/PostgreSQL payloads: honour
    ``PERCONA_BACKUP_TEXTFILE_COLLECTOR_DIR`` and otherwise fall back to the PMM
    low-resolution collector under the backup user's home.

    :return: The collector directory, or ``None`` when neither the override nor a
        home directory resolves -- ``_write_textfile_metric`` then no-ops rather
        than writing a relative ``pmm/collectors/...`` path under the payload's
        cwd.
    """
    override = os.environ.get("PERCONA_BACKUP_TEXTFILE_COLLECTOR_DIR")
    if override:
        return Path(override)
    home = _backup_user_home()
    return home / _TEXTFILE_COLLECTOR_DEFAULT_SUBPATH if home else None


def _metric_alias() -> str:
    """Return the host alias for metric labels.

    Use ``NOMAD_META_TARGET`` -- the host identity Nomad always sets from the
    task target -- so the PMM ``StaleBackup`` rules can join the enable and
    last-report series ``on(type, alias)``. Returns ``""`` when it is unset, so a
    missing label never aborts the backup.

    :return: The alias string, or ``""`` when the target is unset.
    """
    return os.environ.get("NOMAD_META_TARGET", "")


def _escape_label_value(value: str) -> str:
    r"""Return ``value`` escaped for use as a Prometheus label value.

    The text-exposition format defines exactly three escapes inside a label value
    -- backslash, double-quote and newline -- and its parser rejects any other
    escape sequence. An un-escaped alias carrying one of those three would produce
    a ``.prom`` file the collector rejects wholesale, so escape them and leave
    every other character raw: a carriage return is copied through by the parser,
    while ``\r`` would be the invalid escape that breaks the file.

    :param value: The raw label value (the host alias).
    :return: The escaped label value.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _safe_filename_alias(value: str) -> str:
    """Return ``value`` with characters unsafe in a ``.prom`` filename replaced.

    The alias comes from ``NOMAD_META_TARGET``. Path separators could redirect
    the write outside the collector directory, so they and stray newlines
    collapse to ``_``.

    :param value: The raw alias.
    :return: The alias safe to embed in a filename.
    """
    for unsafe in ("\x00", "/", "\n", "\r"):
        value = value.replace(unsafe, "_")
    return value


def _write_textfile_metric(filename: str, body: str) -> None:
    """Write a ``.prom`` file into the collector directory, best-effort.

    Skip silently when the collector directory is absent (mirroring the
    MySQL/PG ``check_textcollector_dir`` guard) and swallow any write error so
    metric emission never changes the backup's exit status or aborts the run.

    Publish through a sibling temp file and an atomic rename: a scrape landing
    between truncate and write reads a partial file, and the collector rejects a
    malformed ``.prom`` wholesale rather than skipping the bad line.

    :param filename: The ``.prom`` file name to write under the collector dir.
    :param body: The Prometheus text-exposition content to write.
    """
    collector_dir = _textfile_collector_dir()
    if collector_dir is None or not collector_dir.is_dir():
        return
    target = collector_dir / filename
    # A deterministic temp name means a leftover from a failed rename is reused
    # by the next write instead of accumulating, and the collector reads only
    # ``*.prom``.
    tmp = target.with_name(f"{target.name}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(target)
    except OSError as err:
        print(
            f"Failed to write textfile-collector metric {filename}: {err}",
            file=sys.stderr,
        )


def write_backup_enabled(backup_type: str) -> None:
    """Mark this backup type enabled via ``msp_backup_enabled`` (best-effort).

    The PMM ``StaleBackup`` rules join ``on(type, alias) msp_backup_enabled == 1``,
    so this series must exist for the stale-backup alert to fire for this backup.

    :param backup_type: The stable ``type`` label for this PBM backup type.
    """
    alias = _metric_alias()
    label_alias = _escape_label_value(alias)
    body = (
        "# HELP msp_backup_enabled The status of the cron\n"
        "# TYPE msp_backup_enabled Untyped\n"
        f'msp_backup_enabled{{type="{backup_type}", alias="{label_alias}"}} 1\n'
    )
    _write_textfile_metric(
        f"driver.backup.{backup_type}.{_safe_filename_alias(alias)}.prom", body
    )


def write_backup_status(backup_type: str, status: int) -> None:
    """Write ``msp_backup_status`` and ``msp_backup_last_report_ts`` (best-effort).

    Mirror the MySQL/PG status metrics so the PMM stale-backup rules see a fresh
    report timestamp and an outcome code for this backup type. The outcome code is
    ``0`` on success and ``1`` on failure -- matching the PostgreSQL payload and
    the ``msp_backup_status`` report's ``{"0": pass, "1": fail}`` mapping.

    :param backup_type: The stable ``type`` label for this PBM backup type.
    :param status: The outcome code (``0`` on success, ``1`` on failure).
    """
    alias = _metric_alias()
    label_alias = _escape_label_value(alias)
    body = (
        "# HELP msp_backup_status The status of the job\n"
        "# TYPE msp_backup_status Untyped\n"
        "# HELP msp_backup_last_report_ts The Last Report Time\n"
        "# TYPE msp_backup_last_report_ts Untyped\n"
        f'msp_backup_status{{type="{backup_type}", alias="{label_alias}"}} {status}\n'
        f'msp_backup_last_report_ts{{type="{backup_type}", alias="{label_alias}"}} '
        f"{int(time.time())}\n"
    )
    _write_textfile_metric(
        f"backup.{backup_type}.{_safe_filename_alias(alias)}.prom", body
    )


# --- END GENERATED PBM TEXTFILE COLLECTOR ---


def _region_between(begin_marker: str, end_marker: str) -> str:
    """Return this module's source strictly between ``begin_marker`` and ``end_marker``.

    Extract the lines between the markers (both exclusive) from this module's own
    source, so the codegen step and the in-sync guard share one definition of each
    generated block.

    :param begin_marker: The BEGIN marker line delimiting the region.
    :param end_marker: The END marker line delimiting the region.
    :return: The region body text, stripped of its leading/trailing blank lines.
    :raises ValueError: When either marker is missing from the module source.
    """
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    try:
        begin = lines.index(begin_marker)
        end = lines.index(end_marker, begin + 1)
    except ValueError as exc:
        raise ValueError(
            "pbm_creds_common.py is missing a generated-region marker line"
        ) from exc
    return "\n".join(lines[begin + 1 : end]).strip("\n")


def preamble_source() -> str:
    """Return the canonical generated PBM creds-preamble region text.

    :return: The preamble body text, stripped of its leading/trailing blank lines.
    :raises ValueError: When either marker is missing from the module source.
    """
    return _region_between(PREAMBLE_BEGIN, PREAMBLE_END)


def config_apply_source() -> str:
    """Return the canonical generated ``_apply_pbm_config`` region text.

    :return: The ``_apply_pbm_config`` body text, stripped of leading/trailing blanks.
    :raises ValueError: When either marker is missing from the module source.
    """
    return _region_between(CONFIG_APPLY_BEGIN, CONFIG_APPLY_END)


def restore_yes_source() -> str:
    """Return the canonical generated restore ``--yes`` probe region text.

    :return: The ``_append_pbm_restore_yes_if_supported`` body text, stripped of
        leading/trailing blanks.
    :raises ValueError: When either marker is missing from the module source.
    """
    return _region_between(RESTORE_YES_BEGIN, RESTORE_YES_END)


def textfile_source() -> str:
    """Return the canonical generated textfile-collector metric-writer region text.

    :return: The metric-writer body text, stripped of leading/trailing blanks.
    :raises ValueError: When either marker is missing from the module source.
    """
    return _region_between(TEXTFILE_BEGIN, TEXTFILE_END)
