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

"""Verify the local PMM environment under ``sidecar/dev/``.

Each test pins one property that lets the stack start beside a PMM a developer
already runs, or that keeps the pair on one released version.
"""

import re
from typing import Any

import pytest
import yaml

from app import BASE_DIR

COMPOSE_FILE = BASE_DIR / "sidecar" / "dev" / "compose.yaml"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """Return the parsed base compose file.

    :return: The mapping ``sidecar/dev/compose.yaml`` holds.
    """
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_every_published_port_binds_to_loopback(compose: dict[str, Any]) -> None:
    """Publish nothing beyond ``127.0.0.1``: PMM starts with a known password."""
    published = [
        port
        for service in compose["services"].values()
        for port in service.get("ports", [])
    ]

    assert published
    assert [port for port in published if not port.startswith("127.0.0.1:")] == []


def test_names_are_scoped_to_the_compose_project(compose: dict[str, Any]) -> None:
    """Fix no container or volume name, so a second PMM on the machine cannot collide."""
    named_containers = [
        name
        for name, service in compose["services"].items()
        if "container_name" in service
    ]
    named_volumes = [
        name
        for name, volume in compose["volumes"].items()
        if volume and "name" in volume
    ]

    assert named_containers == []
    assert named_volumes == []


def test_sidecar_waits_for_a_healthy_pmm(compose: dict[str, Any]) -> None:
    """Start the sidecar only once PMM has written its secrets and serves Grafana."""
    depends_on = compose["services"]["pmm-extensions"]["depends_on"]

    assert depends_on["pmm-server"]["condition"] == "service_healthy"


def test_every_image_follows_the_one_version_variable(compose: dict[str, Any]) -> None:
    """Pin every image through ``PMM_VERSION``, with no floating tag."""
    images = [
        service["image"]
        for service in compose["services"].values()
        if "image" in service
    ]

    assert images
    assert [image for image in images if "PMM_VERSION" not in image] == []
    assert [image for image in images if image.endswith((":3", ":latest"))] == []


def test_pmm_advertises_its_compose_service_name(compose: dict[str, Any]) -> None:
    """Give Nomad clients in other containers an address they can dial."""
    environment = compose["services"]["pmm-server"]["environment"]

    assert environment["PMM_PUBLIC_ADDRESS"] == "pmm-server"


PAM_ROOTOK_RULE = "account    sufficient   pam_rootok.so"
"""The rule that lets ``root`` pass ``sudo``'s account check without ``unix_chkpwd``."""

PAM_ROOTOK_GUARD = f"grep -q '^{PAM_ROOTOK_RULE}' /etc/pam.d/sudo"
"""The build step that fails the image unless the rule is in ``sudo``'s account stack."""


def _run_instructions(containerfile: str) -> list[str]:
    """Return the active ``RUN`` instructions of a Containerfile, continuations joined.

    :param containerfile: The Containerfile's text.
    :return: Each ``RUN`` instruction on one line; commented lines are left out.
    """
    joined = re.sub(r"\\\n", " ", containerfile)
    return [
        line.strip() for line in joined.splitlines() if line.strip().startswith("RUN ")
    ]


def _lets_root_use_sudo(containerfile: str) -> bool:
    """Return whether an active ``RUN`` adds the rule and fails the build without it.

    :param containerfile: The Containerfile's text.
    :return: ``True`` when one ``RUN`` edits ``/etc/pam.d/sudo`` and checks the result.
    """
    return any(
        "/etc/pam.d/sudo" in run and PAM_ROOTOK_RULE in run and PAM_ROOTOK_GUARD in run
        for run in _run_instructions(containerfile)
    )


@pytest.mark.parametrize(
    "containerfile",
    [
        pytest.param(
            f"# RUN sed -i 'x' /etc/pam.d/sudo \\\n#    && {PAM_ROOTOK_GUARD}\n",
            id="commented-out",
        ),
        pytest.param(
            "RUN sed -i '0,/^auth/s//auth    sufficient   pam_rootok.so\\nauth/' "
            "/etc/pam.d/sudo \\\n    && grep -q '^auth    sufficient   pam_rootok.so' "
            "/etc/pam.d/sudo\n",
            id="auth-stack",
        ),
        pytest.param(
            f"RUN sed -i 's/x/{PAM_ROOTOK_RULE}/' /etc/pam.d/sudo\n", id="unchecked"
        ),
    ],
)
def test_a_pam_edit_that_does_not_reach_the_account_stack_is_rejected(
    containerfile: str,
) -> None:
    """Reject an edit that is commented out, targets ``auth``, or is not checked."""
    assert not _lets_root_use_sudo(containerfile)


def test_root_passes_sudo_account_check_in_every_privileged_host(
    compose: dict[str, Any],
) -> None:
    """Let ``root`` use ``sudo`` in every privileged host, on any Docker host.

    A Docker host with AppArmor leaves a privileged container unconfined. There
    the setuid ``unix_chkpwd`` cannot read ``/etc/shadow``, so without
    ``pam_rootok`` PAM refuses every ``sudo``, and the PostgreSQL host's
    entrypoint and any task that declares ``sudo`` fail.
    """
    containerfiles = {
        name: (
            COMPOSE_FILE.parent
            / service["build"]["context"]
            / service["build"]["dockerfile"]
        ).read_text(encoding="utf-8")
        for name, service in compose["services"].items()
        if service.get("privileged") and "build" in service
    }
    with_sudo = {
        name: text
        for name, text in containerfiles.items()
        if any(" sudo" in run for run in _run_instructions(text))
    }

    assert set(with_sudo) == {"mysql-host", "postgresql-host"}
    assert [
        name for name, text in with_sudo.items() if not _lets_root_use_sudo(text)
    ] == []
