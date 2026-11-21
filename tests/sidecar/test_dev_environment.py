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
