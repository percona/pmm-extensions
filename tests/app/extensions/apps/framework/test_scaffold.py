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

"""Cover the app scaffolder in-process across all three flavors.

Each flavor test scaffolds a throwaway app into the *real* source tree (the
registry hardcodes the ``app.extensions.apps.<name>`` import path, so a generated
app must live there to import), builds a fresh registry from the short
``MODULE_NAME``, runs the conformance detectors, and exercises the derived
router through the contract client — then removes the generated package, test
package, and ``sys.modules`` entries in a ``finally``. ``settings.yaml`` is never
mutated in place: in-process tests take the ``tmp_settings`` fixture's monkeypatch
and the ``make startapp`` subprocess tests take the ``EXTENSIONS_SCAFFOLD_SETTINGS_FILE``
redirect, so the throwaway registration cannot dirty the worktree or race a
parallel worker.
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI, status
from fastapi.testclient import TestClient
from rich.prompt import Confirm, Prompt
from starlette.datastructures import URL

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.requests.remote_api import RemoteAPI
from app.core.utils.path import payload_uri, resolve_payload_reference
from app.extensions.apps.framework import scaffold
from app.extensions.apps.framework.apps import TaskExecutionApp
from app.extensions.apps.framework.base import BaseApp
from app.extensions.apps.framework.conformance import (
    check_actor_fields_resolvable,
    check_capability_route_consistency,
    check_item_display_names_declared,
    check_route_collisions,
    check_schema_derivation_succeeds,
    check_view_fields_reference_real_fields,
)
from app.extensions.apps.framework.registry import build_app_registry
from app.extensions.apps.framework.schema import ITEM_DISPLAY_NAME_KEYS
from app.extensions.apps.nav_icons import NavIcon
from app.extensions.config import App, ExtensionsSettings
from app.extensions.deps import get_current_user, IsApiAuthenticated
from app.extensions.snippets.config import snippets_settings
from app.inventory.models import ServiceTypeEnum
from tests.app.extensions.apps.framework.contract_suite import (
    app_base_url,
    build_contract_client,
    build_valid_create_body,
)
from tests.app.extensions.apps.framework.kit import (
    MockInventoryAPI,
    MockTaskAPI,
    SEEDED_TASK_NAME,
)


def _settings_copy(tmp_path: Path) -> Path:
    """Return a throwaway ``settings.yaml`` seeded from the repository's own."""
    copy = tmp_path / "settings.yaml"
    copy.write_text(
        scaffold.SETTINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return copy


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a per-test ``settings.yaml`` copy the engine writes to instead."""
    copy = _settings_copy(tmp_path)
    monkeypatch.setattr(scaffold, "SETTINGS_FILE", copy)
    return copy


def _startapp_env(settings_copy: Path) -> dict[str, str]:
    """Return a ``make startapp`` environment registering into ``settings_copy``.

    The subprocess tests below run the scaffolder in a child process, so they
    cannot redirect it with ``tmp_settings``'s monkeypatch. Left pointing at the
    worktree's own ``settings.yaml``, the child registers its app there, and any
    test constructing ``ExtensionsSettings()`` while the entry is live fails on the app
    module the entry names but the run has not written yet.

    :param settings_copy: The throwaway ``settings.yaml`` to register into.
    :return: ``os.environ`` plus the settings-file redirect.
    """
    return {**os.environ, scaffold.SETTINGS_FILE_ENV_VAR: str(settings_copy)}


def _cleanup(name: str) -> None:
    """Remove a scaffolded app's trees and purge its ``sys.modules`` entries."""
    shutil.rmtree(scaffold.PLUGINS_DIR / name, ignore_errors=True)
    shutil.rmtree(scaffold.TESTS_DIR / name, ignore_errors=True)
    for module in list(sys.modules):
        if module == f"app.extensions.apps.{name}" or module.startswith(
            f"app.extensions.apps.{name}."
        ):
            del sys.modules[module]
    importlib.invalidate_caches()


def _venv_root() -> Path:
    """Return the active virtualenv root for Makefile ``VIRTUAL_ENV`` forwarding.

    Use ``sys.prefix`` rather than resolving ``sys.executable``: following the
    interpreter symlink lands in the base install and breaks
    ``VIRTUAL_ENV/bin/python``.
    """
    return Path(sys.prefix)


@contextmanager
def _scaffolded_config(
    config: scaffold.ScaffoldConfig,
) -> Iterator[scaffold.ScaffoldResult]:
    """Yield a scaffolded ``config``, removing the trees and module entries on exit."""
    try:
        yield scaffold.scaffold_app(config)
    finally:
        _cleanup(config.name)


@contextmanager
def _scaffolded(
    name: str, flavor: scaffold.Flavor
) -> Iterator[scaffold.ScaffoldResult]:
    """Yield a scaffolded ``name``, removing the trees and module entries on exit."""
    with _scaffolded_config(scaffold.ScaffoldConfig.defaults(name, flavor)) as result:
        yield result


def _config_from_args(argv: list[str]) -> scaffold.ScaffoldConfig:
    """Resolve a CLI ``argv`` (non-interactive) into a ``ScaffoldConfig``."""
    parser = scaffold.build_parser()
    return scaffold.resolve_config(parser, parser.parse_args(argv))


def _force_wizard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prompt_answers: dict[str, object] | None = None,
    confirm: object = True,
) -> "_WizardStub":
    """Force the interactive branch and stub the ``rich`` prompt seams.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param prompt_answers: Per-prompt canned answers keyed by label prefix; a list
        value is consumed one entry per re-prompt.
    :param confirm: The ``Confirm.ask`` answer — a bool, or a callable raising to
        simulate ``Ctrl-C``.
    :return: The stub recording every prompt and confirm label asked.
    """
    stub = _WizardStub(prompt_answers, confirm)
    monkeypatch.setattr(scaffold, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(Prompt, "ask", staticmethod(stub.ask))
    monkeypatch.setattr(Confirm, "ask", staticmethod(stub.confirm))
    return stub


class _WizardStub:
    """Record wizard prompts and feed canned answers keyed by label prefix."""

    def __init__(
        self,
        prompt_answers: dict[str, object] | None,
        confirm: bool | Callable[[str], bool],
    ) -> None:
        self._answers = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in (prompt_answers or {}).items()
        }
        self._confirm: bool | Callable[[str], bool] = confirm
        self.prompts: list[str] = []
        self.confirms: list[str] = []

    def ask(
        self,
        label: str,
        *,
        choices: list[str] | None = None,
        default: object = None,
        case_sensitive: bool | None = None,
    ) -> object:
        """Record ``label`` and return its canned answer (or a sensible fallback)."""
        self.prompts.append(label)
        for key, queue in self._answers.items():
            if label.startswith(key):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        if default is not None:
            return default
        if choices:
            return choices[0]
        return "value"

    def confirm(self, label: str, *, default: bool | None = None) -> bool:
        """Record ``label`` and return the canned confirm answer."""
        self.confirms.append(label)
        confirm = self._confirm
        return confirm(label) if callable(confirm) else confirm


def _task_conformance(app: TaskExecutionApp) -> list[str]:
    """Return every conformance violation for a derived task/script app."""
    return [
        *check_capability_route_consistency(app),
        *check_view_fields_reference_real_fields(app),
        *check_schema_derivation_succeeds(app),
        *check_actor_fields_resolvable(app),
    ]


def _mount_api_first(app_def: BaseApp, user: CasdoorUser) -> TestClient:
    """Mount a ``BaseApp``'s API router behind the production auth guard."""
    apps_router = APIRouter(prefix="/apps")
    apps_router.include_router(app_def.api_router, prefix=app_def.uri_path)
    api_router = APIRouter(prefix="/api", dependencies=[IsApiAuthenticated])
    api_router.include_router(apps_router)
    fastapi_app = FastAPI()
    fastapi_app.include_router(api_router)
    fastapi_app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(fastapi_app, raise_server_exceptions=False)


GOLDEN_DIR = Path(__file__).parent / "golden"

_GOLDEN_FILES = {
    scaffold.Flavor.TASK: ("golden_task", ("app.py", "models.py", "spec.py")),
    scaffold.Flavor.SCRIPT: ("golden_script", ("app.py",)),
    scaffold.Flavor.BASE: ("golden_base", ("app.py",)),
}


@pytest.mark.parametrize("flavor", list(scaffold.Flavor))
def test_default_render_is_byte_identical(
    tmp_settings: Path, flavor: scaffold.Flavor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert each flavor's default render is byte-identical to its golden snapshot.

    The goldens are the raw template substitution. ``ruff`` ships in the optional
    ``audit`` poetry group, which the CI test job (``poetry sync --no-root``) does
    not install, so the scaffolder's post-render ``_ruff_fix`` no-ops there and the
    goldens stay ruff-version-independent. A dev venv that installed the ``audit``
    group would otherwise reformat the render, so the pass is stubbed to a no-op to
    keep this comparison deterministic across environments.
    """
    monkeypatch.setattr(scaffold, "_ruff_fix", lambda *_: None)
    name, filenames = _GOLDEN_FILES[flavor]
    with _scaffolded(name, flavor) as result:
        for filename in filenames:
            rendered = (result.app_dir / filename).read_text()
            expected = (GOLDEN_DIR / flavor.value / filename).read_text()
            assert rendered == expected, f"{flavor.value}/{filename} drifted"
        assert result.payload_written is None
        assert not (result.app_dir / "payload").exists()

    assert (
        f"      - MODULE_NAME: {name}\n        ENABLED: false\n"
        in tmp_settings.read_text()
    )


@pytest.mark.parametrize("flavor", list(scaffold.Flavor))
def test_free_text_display_name_escaped_in_every_rendered_file(
    tmp_settings: Path, flavor: scaffold.Flavor
) -> None:
    """Ensure a hostile display name renders as valid Python in every generated file.

    The chosen display name carries the exact sequences that break an unescaped
    docstring interior: a triple-quote run (which would close the docstring early)
    and bare backslash-x / backslash-u escapes (which raise SyntaxError outside a
    raw string literal).
    """
    name = f"_scaffold_quote_{flavor.value}"
    config = _config_from_args(
        [
            "--name",
            name,
            "--type",
            flavor.value,
            "--display-name",
            r'Fast """ \x \u "Cool" \ Backup',
            "--no-input",
        ]
    )
    with _scaffolded_config(config) as result:
        for rendered in result.written:
            if rendered.suffix == ".py":
                compile(rendered.read_text(), str(rendered), "exec")
        importlib.import_module(f"app.extensions.apps.{name}")


def test_docstring_safe_preserves_plain_double_quotes() -> None:
    """Keep plain double quotes untouched so generated docstrings stay lint-clean."""
    assert scaffold._docstring_safe('My "Cool" App') == 'My "Cool" App'


def test_docstring_safe_escapes_backslashes_and_triple_quote_runs() -> None:
    """Escape only syntax-breaking sequences for triple-quoted docstring interiors."""
    assert (
        scaffold._docstring_safe(r'Fast """ \x \u "Cool" \ Backup')
        == 'Fast \\""" \\\\x \\\\u "Cool" \\\\ Backup'
    )


@pytest.mark.parametrize(
    "bad_name", ["My App!", "123app", "", "demo-app", "___", "class", "Demo"]
)
def test_rejects_invalid_name(bad_name: str) -> None:
    """Reject names that are not lowercase Python identifiers, keywords, or empty."""
    with pytest.raises(ValueError, match="invalid app name"):
        scaffold.validate_name(bad_name)


@pytest.mark.parametrize(
    "good_name", ["demo_task", "myapp", "_scaffold_smoke_task", "app2"]
)
def test_accepts_valid_name(good_name: str) -> None:
    """Accept lowercase identifiers, including a leading underscore."""
    scaffold.validate_name(good_name)


def test_type_defaults_to_task() -> None:
    """Resolve the ``task`` flavor when ``--type`` is omitted (non-interactive)."""
    config = _config_from_args(["--name", "demo", "--no-input"])

    assert config.flavor == scaffold.Flavor.TASK


def test_unknown_flavor_rejected() -> None:
    """Reject an unknown ``--type`` value at the argparse boundary."""
    with pytest.raises(SystemExit):
        scaffold.build_parser().parse_args(["--name", "demo", "--type", "bogus"])


def test_settings_insertion_is_idempotent() -> None:
    """Insert a disabled entry once; a second insert of the same name is a no-op."""
    original = scaffold.SETTINGS_FILE.read_text()

    once, changed_first = scaffold.insert_app_entry(original, "scaffold_idem_demo")
    twice, changed_second = scaffold.insert_app_entry(once, "scaffold_idem_demo")

    assert changed_first
    assert not changed_second
    assert twice == once
    assert once.count("MODULE_NAME: scaffold_idem_demo") == 1
    assert "      - MODULE_NAME: scaffold_idem_demo\n        ENABLED: false\n" in once


def test_insertion_fails_without_default_plugins_block() -> None:
    """Fail loudly rather than corrupt a settings file lacking the default block."""
    with pytest.raises(ValueError, match="default"):
        scaffold.insert_app_entry("development:\n  EXTENSIONS:\n    APPS:\n", "demo")


def test_refuses_to_clobber_existing_plugin(tmp_settings: Path) -> None:
    """Raise before any write when the target plugin directory holds a real plugin."""
    name = "_scaffold_smoke_clobber"
    app_dir = scaffold.PLUGINS_DIR / name
    app_dir.mkdir(parents=True)
    (app_dir / "routes.py").write_text("")
    before = tmp_settings.read_text()
    try:
        with pytest.raises(FileExistsError):
            scaffold.scaffold_app(
                scaffold.ScaffoldConfig.defaults(name, scaffold.Flavor.TASK)
            )

        assert tmp_settings.read_text() == before
        assert not (scaffold.TESTS_DIR / name).exists()
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def test_refuses_to_clobber_existing_tests_package(tmp_settings: Path) -> None:
    """Raise before any write when only the target test package holds a real plugin."""
    name = "_scaffold_smoke_clobber_tests"
    tests_dir = scaffold.TESTS_DIR / name
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_routes.py").write_text("")
    before = tmp_settings.read_text()
    try:
        with pytest.raises(FileExistsError):
            scaffold.scaffold_app(
                scaffold.ScaffoldConfig.defaults(name, scaffold.Flavor.TASK)
            )

        assert tmp_settings.read_text() == before
        assert not (scaffold.PLUGINS_DIR / name).exists()
    finally:
        shutil.rmtree(tests_dir, ignore_errors=True)


def test_empty_app_dir_does_not_block_scaffold(tmp_settings: Path) -> None:
    """Scaffold when the target app directory exists but is empty."""
    name = "_scaffold_smoke_empty_app"
    app_dir = scaffold.PLUGINS_DIR / name
    app_dir.mkdir(parents=True)
    try:
        with _scaffolded(name, scaffold.Flavor.TASK):
            assert (app_dir / "__init__.py").exists()
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def test_pycache_only_app_dir_does_not_block_scaffold(tmp_settings: Path) -> None:
    """Scaffold when the target app directory contains only ``__pycache__/``."""
    name = "_scaffold_smoke_pycache_app"
    app_dir = scaffold.PLUGINS_DIR / name
    (app_dir / "__pycache__").mkdir(parents=True)
    try:
        with _scaffolded(name, scaffold.Flavor.TASK):
            assert (app_dir / "__init__.py").exists()
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def test_empty_tests_dir_does_not_block_scaffold(tmp_settings: Path) -> None:
    """Scaffold when the target tests directory exists but is empty."""
    name = "_scaffold_smoke_empty_tests"
    tests_dir = scaffold.TESTS_DIR / name
    tests_dir.mkdir(parents=True)
    try:
        with _scaffolded(name, scaffold.Flavor.TASK):
            assert (tests_dir / "__init__.py").exists()
    finally:
        shutil.rmtree(tests_dir, ignore_errors=True)


def test_pycache_only_tests_dir_does_not_block_scaffold(tmp_settings: Path) -> None:
    """Scaffold when the target tests directory contains only ``__pycache__/``."""
    name = "_scaffold_smoke_pycache_tests"
    tests_dir = scaffold.TESTS_DIR / name
    (tests_dir / "__pycache__").mkdir(parents=True)
    try:
        with _scaffolded(name, scaffold.Flavor.TASK):
            assert (tests_dir / "__init__.py").exists()
    finally:
        shutil.rmtree(tests_dir, ignore_errors=True)


def test_pycache_plus_real_file_still_aborts(tmp_settings: Path) -> None:
    """Abort when the target directory has ``__pycache__/`` alongside a real file."""
    name = "_scaffold_smoke_pycache_real"
    app_dir = scaffold.PLUGINS_DIR / name
    (app_dir / "__pycache__").mkdir(parents=True)
    (app_dir / "routes.py").write_text("")
    before = tmp_settings.read_text()
    try:
        with pytest.raises(FileExistsError):
            scaffold.scaffold_app(
                scaffold.ScaffoldConfig.defaults(name, scaffold.Flavor.TASK)
            )

        assert tmp_settings.read_text() == before
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def test_broken_symlink_blocks_scaffold(tmp_settings: Path) -> None:
    """Abort when the target path is a broken symlink occupying the location."""
    name = "_scaffold_smoke_broken_symlink"
    app_dir = scaffold.PLUGINS_DIR / name
    app_dir.symlink_to(scaffold.PLUGINS_DIR / f"{name}__missing_target")
    before = tmp_settings.read_text()
    try:
        assert not app_dir.exists()
        with pytest.raises(FileExistsError):
            scaffold.scaffold_app(
                scaffold.ScaffoldConfig.defaults(name, scaffold.Flavor.TASK)
            )

        assert tmp_settings.read_text() == before
    finally:
        app_dir.unlink(missing_ok=True)


def _fail_run(*args, **kwargs):
    """Fail the calling test instead of spawning a subprocess."""
    raise AssertionError(f"ruff should not run: {args}, {kwargs}")


@pytest.fixture
def no_ruff_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any ``subprocess.run`` call from the scaffolder fail the test."""
    monkeypatch.setattr(scaffold.subprocess, "run", _fail_run)


@pytest.mark.usefixtures("no_ruff_run")
def test_ruff_fix_noop_without_python_files() -> None:
    """Skip ruff entirely when no rendered file is a ``.py`` file."""
    scaffold._ruff_fix([Path("a.txt"), Path("b.tmpl")])


@pytest.mark.usefixtures("no_ruff_run")
def test_ruff_fix_skips_when_ruff_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip ruff when the executable is absent beside the Python interpreter."""
    monkeypatch.setattr(scaffold.sys, "executable", str(tmp_path / "bin" / "python"))
    scaffold._ruff_fix([Path("a.py")])


@pytest.mark.usefixtures("no_ruff_run")
def test_ruff_fix_skips_when_ruff_not_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip ruff when the path exists but is not executable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ruff").touch()
    monkeypatch.setattr(scaffold.sys, "executable", str(bin_dir / "python"))
    scaffold._ruff_fix([Path("a.py")])


@pytest.mark.usefixtures("no_ruff_run")
def test_ruff_fix_skips_when_ruff_is_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip ruff when the path beside the interpreter is a directory."""
    bin_dir = tmp_path / "bin"
    (bin_dir / "ruff").mkdir(parents=True)
    monkeypatch.setattr(scaffold.sys, "executable", str(bin_dir / "python"))
    scaffold._ruff_fix([Path("a.py")])


def test_ruff_fix_runs_check_then_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the venv ruff check then format over only rendered ``.py`` files."""
    commands = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    ruff = bin_dir / "ruff"
    ruff.touch()
    ruff.chmod(0o755)

    def _record(cmd, **kwargs):
        commands.append((cmd, kwargs))

    monkeypatch.setattr(scaffold.sys, "executable", str(python))
    monkeypatch.setattr(scaffold.subprocess, "run", _record)
    scaffold._ruff_fix([Path("a.py"), Path("b.txt")])
    invoked = [cmd for cmd, _kwargs in commands]
    assert [cmd[1] for cmd in invoked] == ["check", "format"]
    assert all(cmd[0] == ruff for cmd in invoked)
    assert all("a.py" in cmd and "b.txt" not in cmd for cmd in invoked)
    assert all(_kwargs["cwd"] == scaffold._REPO_ROOT for _cmd, _kwargs in commands)


def test_registers_app_disabled(tmp_settings: Path) -> None:
    """Write the registration entry disabled under the default ``EXTENSIONS.APPS``."""
    name = "_scaffold_smoke_disabled"
    with _scaffolded(name, scaffold.Flavor.TASK):
        assert (
            f"      - MODULE_NAME: {name}\n        ENABLED: false\n"
            in tmp_settings.read_text()
        )


def test_summary_points_to_apps_page_without_changelog(
    tmp_settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Assert the summary points at the Apps page and omits a changelog command."""
    name = "_scaffold_smoke_summary"
    try:
        assert scaffold.main(["--name", name, "--type", "task"]) == 0
        out = capsys.readouterr().out
        assert "/admin/apps" in out
        assert "changelog" not in out.lower()
    finally:
        shutil.rmtree(scaffold.PLUGINS_DIR / name, ignore_errors=True)
        shutil.rmtree(scaffold.TESTS_DIR / name, ignore_errors=True)


def test_summary_notes_preexisting_registration(
    tmp_settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report the registration as pre-existing when the settings entry already exists."""
    name = "_scaffold_smoke_preregistered"
    tmp_settings.write_text(
        scaffold.insert_app_entry(tmp_settings.read_text(), name)[0]
    )
    try:
        assert scaffold.main(["--name", name, "--type", "task"]) == 0
        out = capsys.readouterr().out
        assert "already registered" in out.lower()
        assert "/admin/apps" in out
    finally:
        shutil.rmtree(scaffold.PLUGINS_DIR / name, ignore_errors=True)
        shutil.rmtree(scaffold.TESTS_DIR / name, ignore_errors=True)


def test_main_reports_missing_plugins_block_without_traceback(
    tmp_settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit non-zero with a clean error, not a traceback, when registration fails."""
    name = "_scaffold_smoke_noblock"
    tmp_settings.write_text("development:\n  EXTENSIONS:\n    APPS:\n")
    try:
        assert scaffold.main(["--name", name, "--type", "task"]) == 1
        assert "error:" in capsys.readouterr().err
    finally:
        shutil.rmtree(scaffold.PLUGINS_DIR / name, ignore_errors=True)
        shutil.rmtree(scaffold.TESTS_DIR / name, ignore_errors=True)


def test_task_flavor_scaffolds_conformant_app(
    tmp_settings: Path, regular_user: CasdoorUser
) -> None:
    """Assert a scaffolded ``task`` app imports, conforms, and serves its CRUD surface."""
    name = "_scaffold_smoke_task"
    with _scaffolded(name, scaffold.Flavor.TASK):
        importlib.import_module(f"app.extensions.apps.{name}")
        registry = build_app_registry([App(module_name=name)])
        app = registry.get(name)
        assert isinstance(app, TaskExecutionApp)
        assert not _task_conformance(app)
        assert not check_route_collisions(registry)

        tasks_api = MockTaskAPI()
        tasks_api.seed_task(SEEDED_TASK_NAME, owner=app.owner)
        client = build_contract_client(
            app,
            user=regular_user,
            tasks_api=tasks_api,
            inventory_api=MockInventoryAPI(),
        )
        base = app_base_url(app)

        assert client.get(f"{base}/schema").status_code == status.HTTP_200_OK
        assert "items" in client.get(f"{base}/").json()
        body = build_valid_create_body(app)
        assert client.post(f"{base}/", json=body).status_code == status.HTTP_201_CREATED


def test_script_flavor_scaffolds_snippet_routes(
    tmp_settings: Path, regular_user: CasdoorUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert a scaffolded ``script`` app derives the ``/snippet/*`` routes, no Jinja."""
    monkeypatch.setattr(
        snippets_settings, "SNIPPETS_BASE_URL", URL("https://extensions.example")
    )
    name = "_scaffold_smoke_script"
    with _scaffolded(name, scaffold.Flavor.SCRIPT):
        importlib.import_module(f"app.extensions.apps.{name}")
        registry = build_app_registry([App(module_name=name)])
        app = registry.get(name)
        assert isinstance(app, TaskExecutionApp)
        assert not _task_conformance(app)

        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.get.return_value = {"items": [], "total": 0}
        tasks_api.post.return_value = {"id": 1}
        client = build_contract_client(app, user=regular_user, tasks_api=tasks_api)
        base = app_base_url(app)

        assert client.get(f"{base}/schema").status_code == status.HTTP_200_OK
        assert (
            client.get(
                f"{base}/snippet/schema", params={"snippet_filename": "sample.sh"}
            ).status_code
            == status.HTTP_200_OK
        )
        assert (
            client.post(
                f"{base}/snippet/execute",
                params={"snippet_filename": "sample.sh"},
                json={"executor_host": "exec-1", "args": {"message": "hi"}},
            ).status_code
            == status.HTTP_201_CREATED
        )


def test_base_flavor_scaffolds_api_first_app(
    tmp_settings: Path, regular_user: CasdoorUser
) -> None:
    """Assert a scaffolded API-first ``base`` app exposes ``/schema`` and a sample route."""
    name = "_scaffold_smoke_base"
    with _scaffolded(name, scaffold.Flavor.BASE):
        importlib.import_module(f"app.extensions.apps.{name}")
        registry = build_app_registry([App(module_name=name)])
        app = registry.get(name)
        assert app is not None
        assert app.api_router is not None
        assert not check_route_collisions(registry)

        client = _mount_api_first(app, regular_user)

        assert client.get(f"/api/apps/{name}/schema").status_code == status.HTTP_200_OK
        assert client.get(f"/api/apps/{name}/").status_code == status.HTTP_200_OK


def test_insert_app_entry_enabled_writes_enabled_true() -> None:
    """Write ``ENABLED: true`` when ``enabled=True`` is passed."""
    text, changed = scaffold.insert_app_entry(
        scaffold.SETTINGS_FILE.read_text(), "scaffold_enabled_demo", enabled=True
    )

    assert changed
    assert "      - MODULE_NAME: scaffold_enabled_demo\n        ENABLED: true\n" in text


def test_insert_app_entry_defaults_to_disabled() -> None:
    """Keep ``ENABLED: false`` by default (the pre-wizard positional-caller contract)."""
    text, changed = scaffold.insert_app_entry(
        scaffold.SETTINGS_FILE.read_text(), "scaffold_disabled_demo"
    )

    assert changed
    assert (
        "      - MODULE_NAME: scaffold_disabled_demo\n        ENABLED: false\n" in text
    )


def test_service_types_mirror_matches_enum() -> None:
    """Guard the stdlib-only service-type mirror against ``ServiceTypeEnum`` drift."""
    assert tuple(member.name for member in ServiceTypeEnum) == scaffold._SERVICE_TYPES


def test_nav_icons_mirror_matches_enum() -> None:
    """Guard the stdlib-only nav-icon mirror against ``NavIcon`` drift."""
    assert tuple(member.name for member in NavIcon) == scaffold._NAV_ICONS


def test_no_input_without_name_errors() -> None:
    """Raise ``SystemExit`` when ``--no-input`` is given without ``--name``."""
    with pytest.raises(SystemExit) as exc_info:
        scaffold.main(["--no-input"])

    assert exc_info.value.code != 0


def test_no_input_with_name_scaffolds_defaults(tmp_settings: Path) -> None:
    """Generate a default scaffold when ``--no-input --name`` is supplied."""
    name = "_scaffold_smoke_noinput"
    try:
        assert scaffold.main(["--no-input", "--name", name, "--type", "task"]) == 0
        assert (scaffold.PLUGINS_DIR / name / "app.py").exists()
    finally:
        _cleanup(name)


def test_scaffold_script_stays_stdlib_only() -> None:
    """Assert loading ``scaffold.py`` by path and rendering pulls in no heavy deps."""
    scaffolder = (
        scaffold._REPO_ROOT
        / "app"
        / "extensions"
        / "apps"
        / "framework"
        / "scaffold.py"
    )
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('scaffold', {str(scaffolder)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['scaffold'] = module\n"
        "spec.loader.exec_module(module)\n"
        "module.build_parser()\n"
        "config = module.ScaffoldConfig.defaults('demo', module.Flavor.TASK)\n"
        "module._build_context(config)\n"
        "heavy = {'rich', 'app.inventory.models', 'sqlalchemy', 'pydantic'}\n"
        "leaked = sorted(heavy & set(sys.modules))\n"
        "print(leaked)\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    result = scaffold.subprocess.run(
        [sys.executable, "-c", probe],
        cwd=scaffold._REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"heavy modules leaked: {result.stdout}{result.stderr}"
    )


def test_run_command_spec_uses_supplied_command(tmp_settings: Path) -> None:
    """Render ``spec.py`` with the supplied executable, keeping the declarative args."""
    name = "_scaffold_smoke_command"
    config = _config_from_args(
        ["--name", name, "--command", "/usr/bin/mytool", "--no-input"]
    )
    with _scaffolded_config(config) as result:
        spec = (result.app_dir / "spec.py").read_text()

    assert 'command="/usr/bin/mytool"' in spec
    assert "shlex.join(build_command_args(form))" in spec


def test_run_python_spec_renders_and_copies_payload(
    tmp_settings: Path, tmp_path: Path, regular_user: CasdoorUser
) -> None:
    """Render a ``RunPythonSpec`` ``spec.py``, copy the payload, and serve create."""
    name = "_scaffold_smoke_runpython"
    payload_src = tmp_path / "entrypoint.py"
    payload_src.write_text("print('scaffolded payload')\n")
    config = _config_from_args(
        [
            "--name",
            name,
            "--run-mode",
            "run-python",
            "--payload",
            str(payload_src),
            "--no-input",
        ]
    )
    with _scaffolded_config(config) as result:
        spec = (result.app_dir / "spec.py").read_text()
        assert "RunPythonSpec" in spec
        assert 'payload_uri(__file__, "payload")' in spec
        assert not (result.app_dir / "spec_run_python.py").exists()
        assert result.payload_written == result.app_dir / "payload"
        assert (result.app_dir / "payload").is_file()
        assert (
            result.app_dir / "payload"
        ).read_text() == "print('scaffolded payload')\n"

        importlib.import_module(f"app.extensions.apps.{name}")
        spec_module = importlib.import_module(f"app.extensions.apps.{name}.spec")
        reference = payload_uri(spec_module.__file__, "payload")
        assert (
            resolve_payload_reference(reference).resolve()
            == (result.app_dir / "payload").resolve()
        )

        registry = build_app_registry([App(module_name=name)])
        app = registry.get(name)
        assert isinstance(app, TaskExecutionApp)
        assert not _task_conformance(app)

        tasks_api = MockTaskAPI()
        tasks_api.seed_task(SEEDED_TASK_NAME, owner=app.owner)
        client = build_contract_client(
            app,
            user=regular_user,
            tasks_api=tasks_api,
            inventory_api=MockInventoryAPI(),
        )
        base = app_base_url(app)
        body = build_valid_create_body(app)
        assert client.post(f"{base}/", json=body).status_code == status.HTTP_201_CREATED


def test_script_flavor_copies_supplied_script_and_skips_sample(
    tmp_settings: Path, tmp_path: Path
) -> None:
    """Copy a supplied ``--script`` into ``snippets/`` and skip the runnable sample."""
    name = "_scaffold_smoke_seeded"
    script_src = tmp_path / "diag.sh"
    script_src.write_text("#!/usr/bin/env bash\necho hi\n")
    config = _config_from_args(
        ["--name", name, "--type", "script", "--script", str(script_src), "--no-input"]
    )
    with _scaffolded_config(config) as result:
        copied = result.app_dir / "snippets" / "diag.sh"
        assert result.script_written == copied
        assert copied.read_text() == "#!/usr/bin/env bash\necho hi\n"
        assert not (result.app_dir / "snippets" / "sample.sh").exists()
        contract_test = (result.tests_dir / "test_contract.py").read_text()
        assert '_SAMPLE = "diag.sh"' in contract_test


def test_script_flag_rejects_non_script_extension(tmp_path: Path) -> None:
    """Reject a ``--script`` whose file is not a ``.sh`` / ``.py`` script."""
    bad = tmp_path / "notes.txt"
    bad.write_text("nope\n")
    parser = scaffold.build_parser()
    with pytest.raises(SystemExit):
        scaffold.resolve_config(
            parser,
            parser.parse_args(
                ["--name", "x", "--type", "script", "--script", str(bad), "--no-input"]
            ),
        )


def test_script_flag_rejects_missing_file(tmp_path: Path) -> None:
    """Reject a ``--script`` that does not point at an existing file."""
    missing = tmp_path / "gone.sh"
    parser = scaffold.build_parser()
    with pytest.raises(SystemExit):
        scaffold.resolve_config(
            parser,
            parser.parse_args(
                [
                    "--name",
                    "x",
                    "--type",
                    "script",
                    "--script",
                    str(missing),
                    "--no-input",
                ]
            ),
        )


def test_run_python_inferred_from_payload(tmp_settings: Path, tmp_path: Path) -> None:
    """Resolve run-python when only ``--payload`` is given (no ``--run-mode``)."""
    payload_src = tmp_path / "entrypoint.py"
    payload_src.write_text("print('x')\n")
    config = _config_from_args(
        ["--name", "_scaffold_infer_py", "--payload", str(payload_src), "--no-input"]
    )

    assert config.run_mode is scaffold.RunMode.RUN_PYTHON
    assert config.payload_path == payload_src


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            [
                "--name",
                "x",
                "--run-mode",
                "run-command",
                "--payload",
                "/tmp/p",
                "--no-input",
            ],
            id="run-command-with-payload",
        ),
        pytest.param(
            [
                "--name",
                "x",
                "--run-mode",
                "run-python",
                "--command",
                "tool",
                "--no-input",
            ],
            id="run-python-with-command",
        ),
        pytest.param(
            ["--name", "x", "--run-mode", "run-python", "--no-input"],
            id="run-python-without-payload",
        ),
        pytest.param(
            ["--name", "x", "--command", "c", "--payload", "/tmp/p", "--no-input"],
            id="command-and-payload-mutually-exclusive",
        ),
        pytest.param(
            ["--name", "x", "--type", "base", "--service-type", "MYSQL", "--no-input"],
            id="task-only-flag-on-base",
        ),
        pytest.param(
            ["--name", "x", "--type", "base", "--description", "d", "--no-input"],
            id="description-on-base",
        ),
        pytest.param(
            ["--name", "x", "--type", "task", "--script", "s.sh", "--no-input"],
            id="script-on-task",
        ),
    ],
)
def test_run_mode_matrix_invalid_combos_exit(argv: list[str]) -> None:
    """Reject every invalid run-mode / flavor flag combination with a non-zero exit."""
    parser = scaffold.build_parser()
    with pytest.raises(SystemExit):
        scaffold.resolve_config(parser, parser.parse_args(argv))


def test_wizard_prompts_for_flavor_when_type_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collect the flavor from a prompt when ``--type`` is unset; the answer drives gating."""
    stub = _force_wizard(monkeypatch, prompt_answers={"Flavor": "script"})
    parser = scaffold.build_parser()
    config = scaffold.resolve_config(
        parser, parser.parse_args(["--name", "wiz_flavor"])
    )

    assert any(p.startswith("Flavor") for p in stub.prompts)
    assert config.flavor is scaffold.Flavor.SCRIPT
    assert not any(p.startswith("Service type") for p in stub.prompts)


def test_wizard_skips_flavor_prompt_when_type_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip the flavor prompt when ``--type`` is supplied on the command line."""
    stub = _force_wizard(monkeypatch)
    parser = scaffold.build_parser()
    config = scaffold.resolve_config(
        parser, parser.parse_args(["--name", "wiz_typed", "--type", "task"])
    )

    assert not any(p.startswith("Flavor") for p in stub.prompts)
    assert config.flavor is scaffold.Flavor.TASK


def test_wizard_task_flavor_prompts_full_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert the ``task`` wizard prompts for every task field."""
    stub = _force_wizard(monkeypatch)
    parser = scaffold.build_parser()
    scaffold.resolve_config(
        parser, parser.parse_args(["--name", "wiz_task", "--type", "task"])
    )

    assert any(p.startswith("Description") for p in stub.prompts)
    assert any(p.startswith("Service type") for p in stub.prompts)
    assert any(p.startswith("Run mode") for p in stub.prompts)
    assert any(c.startswith("Derive a PUT") for c in stub.confirms)
    assert any(c.startswith("Derive a DELETE") for c in stub.confirms)


def test_wizard_script_flavor_skips_task_only_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert the ``script`` wizard prompts for description but not the task-only fields."""
    stub = _force_wizard(monkeypatch)
    parser = scaffold.build_parser()
    scaffold.resolve_config(
        parser, parser.parse_args(["--name", "wiz_script", "--type", "script"])
    )

    assert any(p.startswith("Description") for p in stub.prompts)
    assert not any(p.startswith("Service type") for p in stub.prompts)
    assert not any(p.startswith("Run mode") for p in stub.prompts)
    assert not any(c.startswith("Derive") for c in stub.confirms)


def test_wizard_base_flavor_skips_description_and_task_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert the ``base`` wizard prompts for neither description nor the task fields."""
    stub = _force_wizard(monkeypatch)
    parser = scaffold.build_parser()
    scaffold.resolve_config(
        parser, parser.parse_args(["--name", "wiz_base", "--type", "base"])
    )

    assert not any(p.startswith("Description") for p in stub.prompts)
    assert not any(p.startswith("Service type") for p in stub.prompts)
    assert not any(p.startswith("Run mode") for p in stub.prompts)
    assert not any(c.startswith("Derive") for c in stub.confirms)
    assert any(p.startswith("Display name") for p in stub.prompts)


def test_wizard_supplied_flag_skips_its_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip a field's prompt when its value is supplied on the command line."""
    stub = _force_wizard(monkeypatch)
    parser = scaffold.build_parser()
    config = scaffold.resolve_config(
        parser,
        parser.parse_args(
            [
                "--name",
                "wiz_override",
                "--display-name",
                "Fixed Label",
                "--service-type",
                "POSTGRESQL",
            ]
        ),
    )

    assert config.display_name == "Fixed Label"
    assert config.service_type == "POSTGRESQL"
    assert not any(p.startswith("Display name") for p in stub.prompts)
    assert not any(p.startswith("Service type") for p in stub.prompts)


def test_wizard_reprompts_on_invalid_then_clobbering_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate the module name at the prompt, re-prompting until it passes."""
    name_answers = ["Bad-Name!", "_scaffold_wizard_valid"]
    stub = _force_wizard(monkeypatch, prompt_answers={"Module name": name_answers})
    parser = scaffold.build_parser()
    config = scaffold.resolve_config(parser, parser.parse_args([]))

    assert config.name == "_scaffold_wizard_valid"
    name_prompts = [p for p in stub.prompts if p.startswith("Module name")]
    assert len(name_prompts) == len(name_answers)


def test_wizard_reprompts_on_invalid_then_real_payload_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an invalid run-python payload path, re-prompting until a real file."""
    payload = tmp_path / "entrypoint.py"
    payload.write_text("print('scaffolded payload')\n")
    answers = [str(tmp_path / "missing.py"), str(payload)]
    stub = _force_wizard(monkeypatch, prompt_answers={"Payload file path": answers})
    parser = scaffold.build_parser()
    config = scaffold.resolve_config(
        parser,
        parser.parse_args(
            ["--name", "wiz_payload", "--type", "task", "--run-mode", "run-python"]
        ),
    )

    assert config.run_mode is scaffold.RunMode.RUN_PYTHON
    assert config.payload_path == payload
    payload_prompts = [p for p in stub.prompts if p.startswith("Payload file path")]
    assert len(payload_prompts) == len(answers)


def test_wizard_final_decline_aborts_without_writing(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abort cleanly and write nothing when the final confirmation is declined."""
    name = "_scaffold_smoke_decline"
    before = tmp_settings.read_text()
    _force_wizard(monkeypatch, confirm=False)

    assert scaffold.main(["--name", name, "--type", "task"]) == 1
    assert not (scaffold.PLUGINS_DIR / name).exists()
    assert not (scaffold.TESTS_DIR / name).exists()
    assert tmp_settings.read_text() == before


def test_wizard_keyboard_interrupt_aborts_without_writing(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abort cleanly and write nothing when the wizard is interrupted."""

    def _interrupt(_label: str) -> bool:
        raise KeyboardInterrupt

    name = "_scaffold_smoke_interrupt"
    before = tmp_settings.read_text()
    _force_wizard(monkeypatch, confirm=_interrupt)

    assert scaffold.main(["--name", name, "--type", "task"]) == 1
    assert not (scaffold.PLUGINS_DIR / name).exists()
    assert not (scaffold.TESTS_DIR / name).exists()
    assert tmp_settings.read_text() == before


@pytest.mark.parametrize("override", [None, "", 'settings with "quotes".yaml'])
def test_settings_file_environment_override(
    tmp_path: Path, override: str | None
) -> None:
    """Resolve the settings redirect without importing application dependencies."""
    env = os.environ.copy()
    env.pop(scaffold.SETTINGS_FILE_ENV_VAR, None)
    expected = scaffold._REPO_ROOT / "settings.yaml"
    if override is not None:
        env[scaffold.SETTINGS_FILE_ENV_VAR] = (
            str(tmp_path / override) if override else ""
        )
        if override:
            expected = tmp_path / override
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import runpy, sys; print(runpy.run_path(sys.argv[1])['SETTINGS_FILE'])",
            scaffold.__file__,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(result.stdout.strip()) == expected


def test_makefile_forwards_quoted_values(tmp_path: Path) -> None:
    """Forward a description with spaces and a quote intact through ``make startapp``.

    Exercises the real Makefile ``$$VAR`` shell-environment forwarding. The child
    process cannot take ``tmp_settings``'s monkeypatch, so it is redirected at a
    throwaway settings file through :func:`_startapp_env` instead.
    """
    name = f"_scaffold_ci_makeforward_{uuid4().hex}"
    description = 'describe the "cool" widget here'
    settings_copy = _settings_copy(tmp_path)
    venv_root = _venv_root()
    try:
        result = scaffold.subprocess.run(
            [
                "make",
                "startapp",
                f"NAME={name}",
                "TYPE=task",
                "NO_INPUT=1",
                f"DESCRIPTION={description}",
                f"VIRTUAL_ENV={venv_root}",
            ],
            cwd=scaffold._REPO_ROOT,
            env=_startapp_env(settings_copy),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        rendered = (scaffold.PLUGINS_DIR / name / "app.py").read_text()
        # The value survives make → shell env → argv → json.dumps intact; ruff may
        # normalise the literal's quote style (double → single to avoid escapes), so
        # assert on the value content rather than a fixed quote form.
        assert "description=" in rendered
        assert (
            f"description={json.dumps(description)}" in rendered
            or f"description={description!r}" in rendered
        )
        assert f"MODULE_NAME: {name}" in settings_copy.read_text()
    finally:
        _cleanup(name)


def test_makefile_forwards_script_flag(tmp_path: Path) -> None:
    """Seed a supplied script through ``make startapp SCRIPT=<file>``.

    The ``SCRIPT`` make variable forwards to the scaffolder's ``--script`` flag the
    way ``PAYLOAD`` forwards ``--payload``, so the script flavor is reachable through
    the ``make startapp`` entry point. Exercises the real Makefile ``$$VAR``
    forwarding, redirecting the child's settings write through
    :func:`_startapp_env` like :func:`test_makefile_forwards_quoted_values`.
    """
    name = f"_scaffold_ci_scriptforward_{uuid4().hex}"
    script_src = tmp_path / "seed.sh"
    script_src.write_text("#!/usr/bin/env bash\necho hi\n")
    settings_copy = _settings_copy(tmp_path)
    venv_root = _venv_root()
    try:
        result = scaffold.subprocess.run(
            [
                "make",
                "startapp",
                f"NAME={name}",
                "TYPE=script",
                "NO_INPUT=1",
                f"SCRIPT={script_src}",
                f"VIRTUAL_ENV={venv_root}",
            ],
            cwd=scaffold._REPO_ROOT,
            env=_startapp_env(settings_copy),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        snippets_dir = scaffold.PLUGINS_DIR / name / "snippets"
        assert (
            snippets_dir / "seed.sh"
        ).read_text() == "#!/usr/bin/env bash\necho hi\n"
        assert not (snippets_dir / "sample.sh").exists()
        assert f"MODULE_NAME: {name}" in settings_copy.read_text()
    finally:
        _cleanup(name)


def test_concurrent_makefile_tests_leave_settings_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep repository settings unchanged while real make tests run and clean up."""
    settings_file = scaffold._REPO_ROOT / "settings.yaml"
    original = settings_file.read_bytes()
    original_apps = ExtensionsSettings().APPS
    registered = Barrier(3, timeout=30)
    inspected = Event()
    run = subprocess.run

    def run_until_registered(
        args: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        result = run(args, **kwargs)
        if list(args[:2]) == ["make", "startapp"]:
            registered.wait()
            assert inspected.wait(timeout=30), "Settings inspection did not finish"
        return result

    monkeypatch.setattr(subprocess, "run", run_until_registered)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures: list[Future[None]] = []
        for test in (
            test_makefile_forwards_quoted_values,
            test_makefile_forwards_script_flag,
        ):
            directory = tmp_path / test.__name__
            directory.mkdir()
            futures.append(executor.submit(test, directory))
        try:
            try:
                # The patched run holds both workers here, so neither can
                # restore settings or remove its package before the
                # inspection below.
                registered.wait()
            except BrokenBarrierError:
                for future in futures:
                    future.result()
                raise
            assert settings_file.read_bytes() == original
            assert original_apps == ExtensionsSettings().APPS
        finally:
            inspected.set()
        for future in futures:
            future.result()

    assert settings_file.read_bytes() == original
    assert original_apps == ExtensionsSettings().APPS


def test_makefile_forwards_item_display_names(tmp_path: Path) -> None:
    """Forward the record-name flags through ``make startapp``.

    Exercises the real Makefile ``$$VAR`` shell-environment forwarding for
    ``ITEM_DISPLAY_NAME`` and ``ITEM_DISPLAY_NAME_PLURAL``, the way
    :func:`test_makefile_forwards_quoted_values` exercises ``DESCRIPTION``.
    """
    name = "_scaffold_ci_itemnameforward"
    item_display_name = "gadget"
    item_display_name_plural = "gadgets"
    settings_copy = _settings_copy(tmp_path)
    venv_root = _venv_root()
    try:
        result = scaffold.subprocess.run(
            [
                "make",
                "startapp",
                f"NAME={name}",
                "TYPE=task",
                "NO_INPUT=1",
                f"ITEM_DISPLAY_NAME={item_display_name}",
                f"ITEM_DISPLAY_NAME_PLURAL={item_display_name_plural}",
                f"VIRTUAL_ENV={venv_root}",
            ],
            cwd=scaffold._REPO_ROOT,
            env=_startapp_env(settings_copy),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        rendered = (scaffold.PLUGINS_DIR / name / "app.py").read_text()
        # The values survive make → shell env → argv → json.dumps intact; ruff may
        # normalise the literal's quote style, so assert on value content rather
        # than a fixed quote form.
        assert "item_display_name=" in rendered
        assert (
            f"item_display_name={json.dumps(item_display_name)}" in rendered
            or f"item_display_name={item_display_name!r}" in rendered
        )
        assert (
            f"item_display_name_plural={json.dumps(item_display_name_plural)}"
            in rendered
            or f"item_display_name_plural={item_display_name_plural!r}" in rendered
        )
        assert f"MODULE_NAME: {name}" in settings_copy.read_text()
    finally:
        _cleanup(name)


@pytest.mark.parametrize("flavor", list(scaffold.Flavor))
def test_record_display_names_default_to_the_display_name(
    flavor: scaffold.Flavor,
) -> None:
    """Seed both record names from the display name when the author supplies neither.

    The default is deliberately the value the conformance detector rejects, so a
    ``task``-flavored scaffold — the only flavor whose rendered app declares a
    create form — fails the conformance suite until its author names the record.
    The other two flavors render ``forms=[]`` at the app level and are skipped by
    the detector, so for them the default just stands.
    """
    config = _config_from_args(["--name", "demo", "--type", flavor.value, "--no-input"])

    assert config.item_display_name == config.display_name
    assert config.item_display_name_plural == config.display_name


@pytest.mark.parametrize("flavor", list(scaffold.Flavor))
def test_record_display_name_flags_reach_the_config(flavor: scaffold.Flavor) -> None:
    """Carry both CLI flags through to the resolved config."""
    config = _config_from_args(
        [
            "--name",
            "demo",
            "--type",
            flavor.value,
            "--item-display-name",
            "widget",
            "--item-display-name-plural",
            "widgets",
            "--no-input",
        ]
    )

    assert config.item_display_name == "widget"
    assert config.item_display_name_plural == "widgets"


@pytest.mark.parametrize("flavor", list(scaffold.Flavor))
def test_record_display_names_rendered_into_every_declaration_site(
    tmp_settings: Path, flavor: scaffold.Flavor
) -> None:
    """Emit both kwargs on exactly the constructors that accept them, and nowhere else.

    ``BaseApp`` declares neither field and does not forbid extras, so a kwarg
    rendered onto its constructor would be silently discarded rather than
    rejected — dead configuration a scaffold author would read as load-bearing.
    The expectation is therefore per-file, not "wherever ``display_name`` appears".
    """
    name = f"_scaffold_nouns_{flavor.value}"
    expected_carriers = {
        scaffold.Flavor.BASE: {"schema.py"},
        scaffold.Flavor.TASK: {"app.py"},
        scaffold.Flavor.SCRIPT: {"source.py"},
    }[flavor]
    config = _config_from_args(
        [
            "--name",
            name,
            "--type",
            flavor.value,
            "--item-display-name",
            "widget",
            "--item-display-name-plural",
            "widgets",
            "--no-input",
        ]
    )

    with _scaffolded_config(config) as result:
        carriers = {
            rendered.name
            for rendered in result.written
            if 'item_display_name="widget"' in rendered.read_text()
        }
        plural_carriers = {
            rendered.name
            for rendered in result.written
            if 'item_display_name_plural="widgets"' in rendered.read_text()
        }

    assert carriers == expected_carriers
    assert plural_carriers == expected_carriers


def test_default_nouns_make_a_task_scaffold_fail_the_conformance_detector(
    tmp_settings: Path,
) -> None:
    """Pin the enforcement the defaults exist to trigger, end to end.

    The defaults are only useful if an unedited task scaffold actually trips
    ``check_item_display_names_declared``. Asserting that the rendered source
    carries the app title would only restate the template; this runs the detector
    over the schema the generated app derives.
    """
    name = "_scaffold_detector_task"
    config = _config_from_args(["--name", name, "--type", "task", "--no-input"])

    with _scaffolded_config(config):
        module = importlib.import_module(f"app.extensions.apps.{name}")
        payload = module.app._resolve_plugin_schema().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

    flagged = sorted(
        key
        for key in ITEM_DISPLAY_NAME_KEYS
        if any(repr(key) in m for m in check_item_display_names_declared(payload))
    )
    assert flagged == sorted(ITEM_DISPLAY_NAME_KEYS)


def test_declared_nouns_make_a_task_scaffold_pass_the_conformance_detector(
    tmp_settings: Path,
) -> None:
    """Clear the detector once the author names the record, the remedy the default points at."""
    name = "_scaffold_detector_task_named"
    config = _config_from_args(
        [
            "--name",
            name,
            "--type",
            "task",
            "--item-display-name",
            "widget",
            "--item-display-name-plural",
            "widgets",
            "--no-input",
        ]
    )

    with _scaffolded_config(config):
        module = importlib.import_module(f"app.extensions.apps.{name}")
        payload = module.app._resolve_plugin_schema().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

    assert check_item_display_names_declared(payload) == []
