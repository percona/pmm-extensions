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

"""Probe and read a host's agent capabilities, as E0-SP1's shell capability step.

A shell step reports what the host lets the agent do before any fact payload runs,
because fapolicyd's default rules stop the Python payload's ``python3 -m venv`` for
root and non-root alike, so a Python-only scan fails at ``prepare-env`` on a
hardened host. It measures ``agent.elevation`` - replacing PMM's ``can_elevate``
euid/``which sudo`` heuristic, which reports yes where nothing elevates - and the
derived capability each privileged step maps its privilege to.

``NoNewPrivileges`` is read from ``/proc/self/status`` first: a unit under it lets
``sudo`` report only "effective uid is not 0 ... nosuid" and cannot read sudoers, so
euid and ``sudo`` answers alone would misread it as elevatable.

This module builds the probe script and reads its key-value report into classified
facts for ``ingest_observation``; it is the one place the capability facts are
derived, and carries no secret.
"""

import shlex
from enum import StrEnum

from app.extensions.apps.shared.om.classification import FactClass
from app.extensions.apps.shared.om.step_contract import AgentElevation

__all__ = [
    "CAPABILITY_FACT_CLASSES",
    "DerivedCapability",
    "build_capability_script",
    "capability_facts",
    "parse_capability_report",
]


class DerivedCapability(StrEnum):
    """Name whether the agent can perform a privileged action, and by what path.

    Each ``host``/``member`` capability fact carries one of these; a step whose
    privilege maps to :attr:`NO` is refused before any job is submitted.

    :cvar DIRECT: The agent performs it as its own user, needing no elevation.
    :cvar VIA_ELEVATION: The agent performs it only through a granted elevation.
    :cvar NO: No path performs it.
    """

    DIRECT = "direct"
    VIA_ELEVATION = "via_elevation"
    NO = "no"


#: The class of every capability fact. All public: a capability names what the agent
#: may do, never a credential. ``agent.elevation`` carries an
#: :class:`~app.extensions.apps.shared.om.step_contract.AgentElevation` value; the
#: rest a :class:`DerivedCapability`.
CAPABILITY_FACT_CLASSES: dict[str, FactClass] = {
    "agent.elevation": FactClass.PUBLIC,
    "host.unit_control": FactClass.PUBLIC,
    "host.config_writable": FactClass.PUBLIC,
    "host.package_install": FactClass.PUBLIC,
    "member.keyfile_readable": FactClass.PUBLIC,
}

#: The elevation states under which a granted ``sudo`` runs any command, so a
#: derived capability needing elevation is reachable without a per-command check.
_ELEVATION_RUNS_ANYTHING = frozenset({AgentElevation.ROOT, AgentElevation.SUDO_ALL})


def build_capability_script(*, config_path: str, keyfile_path: str) -> str:
    """Build the POSIX shell capability probe for one member's paths.

    The script emits ``key=value`` lines :func:`parse_capability_report` reads: the
    ``NoNewPrivileges`` flag from ``/proc/self/status`` first, the effective uid,
    whether ``sudo -n`` runs anything and whether it runs everything
    (``NOPASSWD: ALL``), per-command ``sudo -n -l`` for ``systemctl`` and the package
    managers, and the agent's own read of the keyFile and write of the config file.

    :param config_path: The member's mongod config file, tested for a direct write.
    :param keyfile_path: The member's keyFile, tested for a direct read.
    :return: The shell script's full text, including the shebang.
    """
    config = shlex.quote(config_path)
    keyfile = shlex.quote(keyfile_path)
    return (
        "#!/bin/sh\n"
        "nnp=$(awk '/^NoNewPrivs:/{print $2}' /proc/self/status 2>/dev/null)\n"
        'echo "nnp=${nnp:-0}"\n'
        'echo "euid=$(id -u)"\n'
        'if sudo -n -l >/dev/null 2>&1; then echo "sudo_any=1"; else echo "sudo_any=0"; fi\n'
        "if sudo -n -l 2>/dev/null | grep -q 'NOPASSWD: ALL'; "
        'then echo "sudo_all=1"; else echo "sudo_all=0"; fi\n'
        "if sudo -n -l systemctl >/dev/null 2>&1; "
        'then echo "sudo_systemctl=1"; else echo "sudo_systemctl=0"; fi\n'
        "if sudo -n -l dnf >/dev/null 2>&1 || sudo -n -l apt-get >/dev/null 2>&1; "
        'then echo "sudo_package=1"; else echo "sudo_package=0"; fi\n'
        f'if [ -w {config} ]; then echo "direct_config_writable=1"; '
        'else echo "direct_config_writable=0"; fi\n'
        f'if [ -r {keyfile} ]; then echo "direct_keyfile_readable=1"; '
        'else echo "direct_keyfile_readable=0"; fi\n'
    )


def parse_capability_report(output: str) -> dict[str, str]:
    """Read the probe's ``key=value`` lines into a mapping.

    Ignore a line without a ``=``, so a stray diagnostic the host wrote does not
    break the read; a repeated key keeps the last value.

    :param output: The capability script's stdout.
    :return: The reported values keyed by name, as raw strings.
    """
    report: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            report[key.strip()] = value.strip()
    return report


def _elevation(report: dict[str, str]) -> AgentElevation:
    """Derive ``agent.elevation`` from the probe report.

    ``NoNewPrivileges`` wins over every other signal: under it nothing elevates even
    when ``sudo`` is configured, and ``sudo`` cannot read its own rules to say so.

    :param report: The parsed capability report.
    :return: The measured elevation capability.
    """
    if report.get("nnp") == "1":
        return AgentElevation.BLOCKED_NO_NEW_PRIVILEGES
    if report.get("euid") == "0":
        return AgentElevation.ROOT
    if report.get("sudo_all") == "1":
        return AgentElevation.SUDO_ALL
    if report.get("sudo_any") == "1":
        return AgentElevation.SUDO_LIMITED
    return AgentElevation.NONE


def _derived(
    *, direct: bool, elevation: AgentElevation, sudo_grants: bool
) -> DerivedCapability:
    """Map one capability's signals to whether and how the agent can perform it.

    :param direct: Whether the agent performs the action as its own user.
    :param elevation: The measured elevation capability.
    :param sudo_grants: Whether a per-command ``sudo`` rule grants the action, which
        elevates only under ``AgentElevation.SUDO_LIMITED``.
    :return: The derived capability.
    """
    if direct:
        return DerivedCapability.DIRECT
    if elevation in _ELEVATION_RUNS_ANYTHING:
        return DerivedCapability.VIA_ELEVATION
    if elevation is AgentElevation.SUDO_LIMITED and sudo_grants:
        return DerivedCapability.VIA_ELEVATION
    return DerivedCapability.NO


def capability_facts(output: str) -> dict[str, str]:
    """Read the capability probe's output into the facts a scan observes.

    Derive ``agent.elevation`` and the ``host``/``member`` capability facts as the
    wire values a check set's privilege precondition reads. Unit control and package
    install are direct only as root; config write and keyFile read are direct when
    the agent's own test passed. Pair the result with :data:`CAPABILITY_FACT_CLASSES`
    when ingesting, so every fact is stored public.

    :param output: The capability script's stdout.
    :return: The capability facts keyed by name, with string wire values.
    """
    report = parse_capability_report(output)
    elevation = _elevation(report)
    is_root = elevation is AgentElevation.ROOT
    return {
        "agent.elevation": elevation.value,
        "host.unit_control": _derived(
            direct=is_root,
            elevation=elevation,
            sudo_grants=report.get("sudo_systemctl") == "1",
        ).value,
        "host.config_writable": _derived(
            direct=report.get("direct_config_writable") == "1",
            elevation=elevation,
            sudo_grants=False,
        ).value,
        "host.package_install": _derived(
            direct=is_root,
            elevation=elevation,
            sudo_grants=report.get("sudo_package") == "1",
        ).value,
        "member.keyfile_readable": _derived(
            direct=report.get("direct_keyfile_readable") == "1",
            elevation=elevation,
            sudo_grants=False,
        ).value,
    }
