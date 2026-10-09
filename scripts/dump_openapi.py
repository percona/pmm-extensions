#!/usr/bin/env python3
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

"""Dump the four whole-app OpenAPI specs the frontend codegen consumes.

Writes canonical JSON for the ``main``, ``inventory``, ``tasks``, and ``extensions``
apps to ``frontend/packages/api/specs/``. The top-level ``main`` spec is the
core API only (``app.openapi()``), not the merged ``/api/openapi.json`` document.

Run this outside pytest: sibling conftests inject routers into the process-global
``extensions_app`` at import time, so a spec computed inside the test process would
depend on test-collection order.
"""

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import fastapi.openapi.utils
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[1]
SPECS_DIR = REPO_ROOT / "frontend" / "packages" / "api" / "specs"


def canonical(doc: dict[str, Any]) -> str:
    """Return deterministic JSON for ``doc``: sorted keys, 2-space indent, trailing newline.

    ``sort_keys`` neutralizes dict-key-order nondeterminism so the rendered bytes
    are stable across runs and Python versions, matching the byte format the
    backend snapshot tests use.

    :param doc: The OpenAPI document to render.
    :return: Canonical UTF-8 JSON text with a single trailing newline.
    """
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


_ORIGINAL_GET_MODEL_NAME_MAP = fastapi.openapi.utils.get_model_name_map


def _ordered_model_name_map(unique_models: set[Any]) -> dict[Any, str]:
    """Build FastAPI's schema-name map from models sorted by qualified name.

    ``get_model_name_map`` iterates a ``set`` of model classes, so when a model
    ``__name__`` collides across plugins (for example ``BackupTaskWrite`` exists
    in both the backup_mongo and backup_pg plugins) the model that wins the short
    schema name versus a module-qualified one depends on per-process object
    ordering. Sorting by qualified name first makes the generated spec
    reproducible, which the freshness guard relies on.

    :param unique_models: The model classes FastAPI collected for the spec.
    :return: The model-to-name mapping with deterministic collision names.
    """
    return _ORIGINAL_GET_MODEL_NAME_MAP(
        sorted(unique_models, key=lambda model: (model.__module__, model.__qualname__))
    )


def _patch_deterministic_schema_names() -> None:
    """Ensure FastAPI's schema-name resolution is deterministic across processes."""
    fastapi.openapi.utils.get_model_name_map = _ordered_model_name_map


#: Settings whose value cannot be pinned in a committed file.
#:
#: ``ENCRYPTION_KEY`` is required at settings construction and has no default,
#: and pinning ``ENV_FILE`` at the assignment-free dotenv deliberately puts
#: every developer-local source out of reach, so without a value here the dump
#: aborts before importing the app whatever the checkout supplies. A committed
#: key is not the alternative: ``tests/conftest.py`` mints one for the same
#: reason and ``test_no_key_is_committed`` enforces that nothing ships one. The
#: spec encrypts nothing, so the value is irrelevant as long as it is valid.
_MINTED_ENV = {"ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")}


def _pytest_env_pins() -> dict[str, str]:
    """Return the settings pins ``pyproject.toml`` applies to the test suite.

    Read rather than duplicated. The freshness guard runs this script as a
    subprocess *from* pytest, so it inherits those pins and the dump resolves
    them whether or not this file lists them; a developer running the dump from
    a shell inherits nothing. A hand-maintained second copy therefore produces a
    spec that depends on how the dump was invoked, so the list is derived and
    the two cannot diverge.

    ``ENV_FILE`` is resolved against the repository root: the pin is relative,
    which pytest resolves from its rootdir, and this script can run from
    anywhere.

    Only the plain ``NAME=value`` form is modelled. pytest-env also accepts
    ``D:`` / ``R:`` flag prefixes and interpolates ``{VAR}`` in unprefixed
    values, so reading an entry that uses either would reinstate the very
    divergence this derivation removes, silently and one layer down. Such an
    entry is rejected rather than half-read.

    :return: The pinned variables, in ``pyproject.toml`` order.
    :raises RuntimeError: When the pins are missing, malformed, or use
        pytest-env syntax this function does not model.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        entries = config["tool"]["pytest"]["ini_options"]["env"]
    except KeyError as exc:
        raise RuntimeError(
            "[tool.pytest.ini_options] env is missing from pyproject.toml; the "
            "dump derives its settings pins from it"
        ) from exc

    pins: dict[str, str] = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if not separator:
            raise RuntimeError(f"pytest env pin is not NAME=value: {entry!r}")
        if not name.isidentifier():
            raise RuntimeError(
                f"pytest env pin uses a flag prefix this dump does not model: {entry!r}"
            )
        if "{" in value:
            raise RuntimeError(
                f"pytest env pin uses value interpolation this dump does not "
                f"model: {entry!r}"
            )
        pins[name] = value
    if "ENV_FILE" in pins:
        pins["ENV_FILE"] = str(REPO_ROOT / pins["ENV_FILE"])
    return pins


def _pin_canonical_settings_env() -> None:
    """Pin the settings environment so the generated spec is environment-independent.

    The spec is a build artifact whose shape must not vary with the developer's
    local configuration, so both settings sources able to name an auth provider
    are neutralized:

    - ``ENV_FILE`` is repointed at the committed assignment-free dotenv, since
      it selects the file the dotenv source reads. Exporting variables cannot
      dislodge a provider that a developer's ``ENV_FILE=.env.local`` supplies,
      because the dotenv source reads that file from disk regardless.
    - Pre-existing ``AUTH__PROVIDER*`` variables are cleared, so an exported
      provider cannot survive alongside the canonical one.

    Leaving either source in place resolves a second provider, which
    ``AuthSettings`` rejects outright rather than merging.

    Must be called before ``_load_apps()`` imports the application, since the
    settings object is constructed at import time.

    :raises RuntimeError: When the pytest env pins are missing, malformed, or
        use pytest-env syntax the derivation does not model.
    """
    for key in [k for k in os.environ if k.startswith("AUTH__PROVIDER")]:
        del os.environ[key]
    for key, value in {**_pytest_env_pins(), **_MINTED_ENV}.items():
        os.environ[key] = value


def _load_apps() -> dict[str, Any]:
    """Import the whole-app objects from the worktree this script lives in.

    The shared virtualenv carries an editable ``.pth`` that appends one fixed
    worktree to ``sys.path``. Executing a script puts its own ``scripts/``
    directory on ``sys.path`` but not the repo root, so a bare ``import app``
    would resolve to that ``.pth`` worktree rather than the tree whose specs this
    script writes. Prepending ``REPO_ROOT`` binds the dump to the local worktree.

    :return: The four whole-app objects keyed by spec name.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from app.extensions.main import extensions_app  # noqa: PLC0415
    from app.inventory.main import inventory_app  # noqa: PLC0415
    from app.main import app as main_app  # noqa: PLC0415
    from app.tasks.main import tasks_app  # noqa: PLC0415

    return {
        "main": main_app,
        "inventory": inventory_app,
        "tasks": tasks_app,
        "extensions": extensions_app,
    }


def main() -> int:
    """Write or check the committed spec fixtures.

    :return: ``0`` when every fixture is fresh (or written); ``1`` when ``--check``
        finds a missing or drifted fixture.
    """
    _patch_deterministic_schema_names()
    _pin_canonical_settings_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the committed fixtures against a fresh dump without writing",
    )
    args = parser.parse_args()
    apps = _load_apps()
    # Imported after _load_apps() prepends REPO_ROOT to sys.path so the local
    # worktree's app package is resolved, not the editable .pth worktree.
    from app.core.utils.openapi import namespaced_openapi  # noqa: PLC0415

    drift = []
    for name, fastapi_app in apps.items():
        content = canonical(namespaced_openapi(fastapi_app))
        target = SPECS_DIR / f"{name}.json"
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                drift.append(name)
        else:
            SPECS_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    if drift:
        print(
            f"OpenAPI spec drift: {drift}; regenerate with `python scripts/dump_openapi.py`",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
