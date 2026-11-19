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

"""Assert that every place pinning the project's Python names one version.

The builder stage's ``FROM`` tag in ``Containerfile.base`` is the reference; every
other pin is derived from it, so moving to a new interpreter means changing that
tag and letting these tests name each place that still disagrees. Workflows also
install Python 3.9, the oldest executor-host interpreter, for the host-payload
checks; those two pins are expected by name.
"""

import re
import tomllib
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from app import BASE_DIR

FROM_TAG = re.compile(
    r"^FROM docker\.io/library/python:(\d+\.\d+\.\d+)-slim\b", re.MULTILINE
)
HOST_PYTHON = "3.9"
HOST_PYTHON_WORKFLOWS = ["coverage-main.yml", "python.yaml"]


def _from_tag(relative: str) -> str:
    """Return the Python version in the first ``python:*-slim`` ``FROM`` line.

    :param relative: The Containerfile, relative to the repository root.
    :return: The full version, e.g. ``3.14.10``.
    """
    match = FROM_TAG.search((BASE_DIR / relative).read_text(encoding="utf-8"))
    assert match is not None, f"{relative} has no python:<version>-slim FROM line"
    return match.group(1)


def _workflow_pins(node: Any) -> Iterator[str]:
    """Yield every literal Python version a parsed workflow pins.

    A scalar ``python-version`` value and each entry of a ``python`` matrix list
    count, in any YAML spelling; an expression such as ``${{ matrix.python }}``
    carries no literal and is skipped.

    :param node: A parsed workflow, or any part of one.
    :return: An iterator over the pinned versions, as strings.
    """
    if isinstance(node, list):
        for item in node:
            yield from _workflow_pins(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "python-version" and isinstance(value, str | int | float):
                literals = [value]
            elif key == "python" and isinstance(value, list):
                literals = value
            else:
                yield from _workflow_pins(value)
                continue
            yield from (str(item) for item in literals if "${{" not in str(item))


@pytest.fixture(scope="module")
def version() -> str:
    """Return the reference version, from the builder stage's ``FROM`` tag."""
    return _from_tag("Containerfile.base")


@pytest.fixture(scope="module")
def minor(version: str) -> str:
    """Return the reference version's ``major.minor``.

    :param version: The reference version.
    """
    return version.rsplit(".", 1)[0]


def test_the_runtime_image_uses_the_builder_version(version: str) -> None:
    """Build and run the side-car on the same interpreter release.

    :param version: The reference version.
    """
    assert _from_tag("sidecar/Containerfile.sidecar") == version


def test_pyproject_targets_the_reference_minor(minor: str) -> None:
    """Declare, lint, type-check and test for the reference minor only.

    :param minor: The reference version's ``major.minor``.
    """
    pyproject = tomllib.loads((BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    major, minor_number = minor.split(".")

    assert pyproject["project"]["requires-python"] == (
        f">={minor},<{major}.{int(minor_number) + 1}"
    )
    assert pyproject["tool"]["ruff"]["target-version"] == f"py{major}{minor_number}"
    assert pyproject["tool"]["ty"]["environment"]["python-version"] == minor
    assert pyproject["tool"]["tox"]["env_list"] == [minor]


def test_workflows_pin_only_the_reference_and_the_host_python(version: str) -> None:
    """Find only the reference version, and 3.9 where the payload checks need it.

    :param version: The reference version.
    """
    pins: Counter[tuple[str, str]] = Counter()
    for workflow in sorted((BASE_DIR / ".github" / "workflows").glob("*.y*ml")):
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for pinned in _workflow_pins(parsed):
            pins[workflow.name, pinned] += 1

    assert {pinned for _, pinned in pins if pinned != HOST_PYTHON} <= {version}
    assert sorted(
        name for name, pinned in pins.elements() if pinned == HOST_PYTHON
    ) == (HOST_PYTHON_WORKFLOWS)


def test_the_setup_python_action_defaults_to_the_reference(version: str) -> None:
    """Default the composite action every Python job uses to the reference version.

    :param version: The reference version.
    """
    action = yaml.safe_load(
        (
            BASE_DIR / ".github" / "actions" / "setup-python-job" / "action.yml"
        ).read_text(encoding="utf-8")
    )

    assert action["inputs"]["python-version"]["default"] == version


def test_the_audit_pipeline_provisions_the_reference(version: str, minor: str) -> None:
    """Download and run the reference version in the Jenkins Audit job.

    :param version: The reference version.
    :param minor: The reference version's ``major.minor``.
    """
    pipeline = (BASE_DIR / "build" / "jenkins-audit.pipeline").read_text(
        encoding="utf-8"
    )

    assert re.search(
        rf'^\s*PYTHON_VERSION = "{re.escape(version)}"$', pipeline, re.MULTILINE
    )
    assert re.search(
        rf'^\s*PYTHON = "python{re.escape(minor)}"$', pipeline, re.MULTILINE
    )


def test_pre_commit_builds_hook_environments_on_the_reference(minor: str) -> None:
    """Build pre-commit's Python hook environments on the reference minor.

    :param minor: The reference version's ``major.minor``.
    """
    config = yaml.safe_load(
        (BASE_DIR / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )

    assert config["default_language_version"]["python"] == f"python{minor}"


def test_no_other_jenkins_pipeline_takes_a_python_version() -> None:
    """Find no Jenkins pipeline that still accepts the Python version as a parameter."""
    pipelines = sorted((BASE_DIR / "build").glob("jenkins-*.pipeline"))

    assert pipelines
    assert [
        p.name for p in pipelines if "pyVersion" in p.read_text(encoding="utf-8")
    ] == []
