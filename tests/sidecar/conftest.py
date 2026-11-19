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
"""Provide shared fixtures for the side-car image's baked configuration."""

import re
from configparser import RawConfigParser
from pathlib import Path
from typing import Any

import pytest
import yaml

from app import BASE_DIR

SIDECAR_DIR = BASE_DIR / "sidecar"
CONTAINERFILE = SIDECAR_DIR / "Containerfile.sidecar"
EMBEDDED_PROFILE = SIDECAR_DIR / "settings.yaml"
ENTRYPOINT = SIDECAR_DIR / "entrypoint.sh"
GATE = SIDECAR_DIR / "wait_for_schema.sh"
SETTINGS_ENV_HELPER = SIDECAR_DIR / "settings-env.sh"
SUPERVISORD_CONF = SIDECAR_DIR / "supervisord.conf"
SCHEMA_STEP_PREFIX = "migrate-"
"""Program-name prefix marking a supervisord one-shot that creates schema."""

SENTINEL_PREFIX = "/tmp/migrate-"
"""The prefix every shipped sentinel path is built from."""

HEALTHCHECK = SIDECAR_DIR / "healthcheck.sh"

TOKEN_SENTINEL = "/tmp/grafana-token.ok"
"""The marker ``entrypoint.sh`` publishes and ``healthcheck.sh`` requires.

One literal for both suites, so the two scripts cannot come to name different
paths while each suite still passes on its own.
"""

BUDGET_ASSIGNMENT = re.compile(r"^readonly WAIT_BUDGET_SECONDS=\d+$", re.MULTILINE)
"""The gate's budget constant, which a case needing a different wait rewrites."""

ALLOWLIST_KEY = ("SETTINGS_OVERRIDE", "ALLOWED_KEYS")
"""The nested profile path carrying the override allowlist.

Read from the profile by both the side-car resolution test and the policy
suite's shipped-value guard, which must agree on where the list lives.
"""


def read_allowlist(profile_data: dict) -> Any:
    """Walk ``ALLOWLIST_KEY`` in a parsed profile and return what sits there.

    :param profile_data: The parsed ``settings.yaml`` mapping.
    :return: The value at the nested path, or ``None`` when any segment is
        missing or the path runs into a non-mapping.
    """
    entries = profile_data["default"]
    for segment in ALLOWLIST_KEY:
        entries = entries.get(segment) if isinstance(entries, dict) else None
    return entries


def supervisord_programs() -> dict[str, dict[str, str]]:
    """Return the side-car's supervisord programs, keyed by program name.

    ``RawConfigParser`` rather than the interpolating default: the file carries
    ``%(ENV_...)s`` names supervisord expands, which the default parser rejects.

    :return: Each ``[program:...]`` section's settings, keyed by the bare name.
    """
    parser = RawConfigParser()
    parser.read_string(SUPERVISORD_CONF.read_text(encoding="utf-8"))
    return {
        section.split(":", 1)[1]: dict(parser[section])
        for section in parser.sections()
        if section.startswith("program:")
    }


def schema_steps() -> tuple[str, ...]:
    """Return the schema steps supervisord runs as one-shots, in program-table order.

    Derived from the program table rather than listed, so a step added there
    without being wired into the API gates, the healthcheck and the entrypoint's
    sentinel clearing fails the suites asserting those.

    :return: One bare step name per ``migrate-*`` program.
    """
    return tuple(
        name.removeprefix(SCHEMA_STEP_PREFIX)
        for name in supervisord_programs()
        if name.startswith(SCHEMA_STEP_PREFIX)
    )


SCHEMA_STEPS = schema_steps()
"""Every schema step the program table declares, in its order."""


SUITE_ENV_OVERRIDES = (
    "AUTH__PROVIDER__CASDOOR__CLIENT_ID",
    "AUTH__PROVIDER__CASDOOR__CLIENT_SECRET",
    "AUTH__PROVIDER__CASDOOR__ALLOWED_ISSUERS",
    "ALLOWED_HOSTS",
    "ALLOW_CONCURRENT_SESSIONS",
    "EXTENSIONS__MESSAGES__LEVEL",
    "EXTENSIONS_INTERNAL_TOKEN",
)
"""Names ``[tool.pytest.ini_options] env`` injects for the app suite.

They target the same settings fields the profile carries, so leaving them set
would overlay the file under test with a second auth provider and a narrower
``ALLOWED_HOSTS`` -- the profile would be validated only in part.
"""


@pytest.fixture
def embedded_profile_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve the baked profile as the process's ``settings.yaml``.

    Settings classes open the relative ``Path("settings.yaml")`` per
    instantiation, so a copy in the process CWD is what a freshly constructed
    class reads.

    ``ENCRYPTION_KEY`` is deliberately absent from :data:`SUITE_ENV_OVERRIDES`,
    so the suite-wide key survives the strip loop: the baked profile carries no
    key of its own, and an environment variable is how a real deployment
    supplies one.

    :param tmp_path: The per-test temporary directory.
    :param monkeypatch: The environment and CWD patcher.
    :return: The directory holding the profile copy.
    """
    (tmp_path / "settings.yaml").write_text(
        EMBEDDED_PROFILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for name in SUITE_ENV_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FASTAPI_ENV", "production_docker")
    return tmp_path


@pytest.fixture
def embedded_profile_data() -> dict:
    """Return the parsed baked profile.

    :return: The profile's YAML content.
    """
    return yaml.safe_load(EMBEDDED_PROFILE.read_text(encoding="utf-8"))


@pytest.fixture
def program_settings() -> dict[str, dict[str, str]]:
    """Return the side-car's supervisord programs, keyed by program name.

    :return: Each ``[program:...]`` section's settings, keyed by the bare name.
    """
    return supervisord_programs()
