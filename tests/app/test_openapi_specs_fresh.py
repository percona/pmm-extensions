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

"""Guard the committed frontend OpenAPI spec fixtures and the dump's settings pins."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scripts import dump_openapi

REPO_ROOT = Path(__file__).resolve().parents[2]
DUMP_SCRIPT = REPO_ROOT / "scripts" / "dump_openapi.py"


def test_committed_openapi_specs_are_fresh():
    """Assert the committed whole-app specs match a fresh ``.openapi()`` dump.

    Runs ``scripts/dump_openapi.py --check`` in a subprocess so the imported
    app objects carry no conftest-injected routers, then asserts the committed
    ``frontend/packages/api/specs/*.json`` fixtures the frontend codegen
    consumes have not drifted from the live backend contract.
    """
    result = subprocess.run(
        [sys.executable, str(DUMP_SCRIPT), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_committed_specs_do_not_carry_the_app_version():
    """Assert every committed fixture's ``info.version`` is the fixed placeholder.

    A fixture carrying the app version goes stale on every release bump, which
    fails the freshness guard above on the bump commit itself.
    """
    versions = {
        spec.name: json.loads(spec.read_text(encoding="utf-8"))["info"]["version"]
        for spec in dump_openapi.SPECS_DIR.glob("*.json")
    }

    assert versions
    assert set(versions.values()) == {dump_openapi.FIXTURE_VERSION}


def test_unversioned_replaces_only_the_version():
    """Assert ``unversioned`` swaps ``info.version`` and leaves the input intact."""
    doc = {"info": {"title": "PMM Extensions", "version": "v3.10.0rc1"}, "paths": {}}

    result = dump_openapi.unversioned(doc)

    assert result == {
        "info": {"title": "PMM Extensions", "version": dump_openapi.FIXTURE_VERSION},
        "paths": {},
    }
    assert doc["info"]["version"] == "v3.10.0rc1"


def test_dump_script_ignores_developer_auth_config(tmp_path):
    """Assert the dump pins the canonical provider over a local auth configuration.

    A checkout configured for a non-Casdoor provider names it in the dotenv file
    ``ENV_FILE`` points at, and the dotenv settings source reads that file from
    disk, so clearing the environment alone leaves the provider in place. Point
    the subprocess at such a file *and* export a provider variable, so the dump
    has to neutralize both sources; otherwise ``AuthSettings`` resolves two
    providers, refuses to build, and ``make regen-specs`` cannot run at all.

    This test supplies the hostile environment the sibling freshness test cannot:
    that one inherits the pins ``pyproject.toml`` already sets for pytest.
    """
    dotenv = tmp_path / "developer.env"
    dotenv.write_text(
        "AUTH__PROVIDER='{\"casdoor\": null}'\n"
        "AUTH__PROVIDER__GRAFANA__ENDPOINT=https://grafana.invalid/graph\n"
        "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN=token\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(DUMP_SCRIPT), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "ENV_FILE": str(dotenv),
            "AUTH__PROVIDER__GRAFANA__VERIFY_SSL": "false",
        },
    )
    assert "auth provider" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr


def test_dump_pins_every_setting_the_suite_pins():
    """Assert the dump derives its pins from the same list pytest reads.

    The sibling freshness test runs the dump *from* pytest, so it inherits the
    ``pyproject.toml`` pins whether or not the script sets them; a developer
    running ``make regen-specs`` from a shell inherits nothing. A second
    hand-maintained copy therefore makes the generated spec depend on how the
    dump was invoked. Deriving the list removes that divergence; this pins the
    derivation.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = {
        entry.split("=", 1)[0]
        for entry in config["tool"]["pytest"]["ini_options"]["env"]
    }

    assert set(dump_openapi._pytest_env_pins()) == expected


def test_dump_resolves_the_env_file_pin_against_the_repo_root():
    """Assert ``ENV_FILE`` is absolute, since the dump can run from anywhere.

    ``pyproject.toml`` states the pin relative to pytest's rootdir. Passed
    through unchanged it would resolve against the caller's working directory,
    which for this script is not necessarily the repository.
    """
    env_file = Path(dump_openapi._pytest_env_pins()["ENV_FILE"])

    assert env_file.is_absolute()
    assert env_file == REPO_ROOT / "tests" / "pytest.env"


def test_dump_mints_a_key_rather_than_reading_a_committed_one():
    """Assert the key the dump supplies is minted, not pinned in a file.

    The repository is public and ``test_no_key_is_committed`` enforces that no
    working key ships, so the dump cannot take one from a committed source -
    and it cannot go without, because the setting is required at settings
    construction and pinning ``ENV_FILE`` puts local sources out of reach.
    """
    pins = dump_openapi._pytest_env_pins()

    assert pins, "no pins derived: the absence below would be vacuous"
    assert set(dump_openapi._MINTED_ENV) == {"ENCRYPTION_KEY"}
    assert "ENCRYPTION_KEY" not in pins
    Fernet(dump_openapi._MINTED_ENV["ENCRYPTION_KEY"])


def test_pinning_applies_every_derived_and_minted_variable(monkeypatch):
    """Assert the pins actually reach the environment the app is imported under.

    Every name the call will touch goes through ``monkeypatch`` first, including
    the ``AUTH__PROVIDER*`` variables it clears with a bare ``del``. Those are
    not all derivable from the pins - the suite pins three, and a developer may
    export others - and one deleted without a monkeypatch record would stay
    deleted for the rest of the session.
    """
    pins = dump_openapi._pytest_env_pins()
    for name in [
        *pins,
        *dump_openapi._MINTED_ENV,
        *[key for key in os.environ if key.startswith("AUTH__PROVIDER")],
    ]:
        monkeypatch.delenv(name, raising=False)

    dump_openapi._pin_canonical_settings_env()

    assert pins, "no pins derived: the loop below would assert nothing"
    for name, value in pins.items():
        assert os.environ[name] == value
    assert os.environ["ENCRYPTION_KEY"] == dump_openapi._MINTED_ENV["ENCRYPTION_KEY"]


def test_malformed_pin_is_rejected_rather_than_silently_dropped(monkeypatch, tmp_path):
    """Assert a pin that is not ``NAME=value`` fails loudly.

    A silently skipped entry is the failure this derivation exists to prevent,
    one layer down: the dump would run with fewer pins than the suite and say
    nothing.
    """
    broken = tmp_path / "pyproject.toml"
    broken.write_text(
        '[tool.pytest.ini_options]\nenv = ["ENV_FILE=tests/pytest.env", "OOPS"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(dump_openapi, "REPO_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="not NAME=value"):
        dump_openapi._pytest_env_pins()


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ('"D:ALLOWED_HOSTS=[]"', "flag prefix"),
        ('"R:ALLOWED_HOSTS=[]"', "flag prefix"),
        ('"D:R:ALLOWED_HOSTS=[]"', "flag prefix"),
        ('"EXTENSIONS_INTERNAL_TOKEN={HOME}/t"', "value interpolation"),
    ],
)
def test_unmodelled_pytest_env_syntax_is_rejected(
    monkeypatch, tmp_path, entry, expected
):
    """Assert pytest-env syntax the derivation does not model fails loudly.

    pytest-env strips ``D:`` / ``R:`` prefixes and interpolates ``{VAR}`` in
    unprefixed values. Read as plain ``NAME=value`` a prefixed entry yields the
    name ``D:NAME`` - exporting a variable nothing reads and leaving the real
    one unset - so pytest and the dump would apply different settings while
    both reported success, which is the divergence deriving the list removes.
    """
    broken = tmp_path / "pyproject.toml"
    broken.write_text(
        f"[tool.pytest.ini_options]\nenv = [{entry}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dump_openapi, "REPO_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match=expected):
        dump_openapi._pytest_env_pins()
