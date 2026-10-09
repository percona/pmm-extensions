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

"""Render a conformant PMM Extensions app skeleton for the task, script, or base flavor.

Run as ``python -m app.extensions.apps.framework.scaffold [--name <name>] [--type
task|script|base] [field flags]`` (driven by ``make startapp``). On a TTY with no
``--no-input``, an interactive ``rich`` wizard collects each field; a piped or
``--no-input`` run resolves every unset field to its default, so CI never hangs.
The engine validates the module name, refuses to clobber an existing plugin,
renders the flavor's templates into ``app/extensions/apps/<name>/`` and
``tests/app/extensions/apps/<name>/``, copies the run-python payload beside ``spec.py``
when one is supplied, and registers the app under the ``default:`` ``EXTENSIONS.APPS``
block (disabled unless ``--enable``) so an admin activates it from the App
Manager rather than the scaffolder doing so.

Templates carry ``<< var >>`` placeholders substituted by plain string
replacement — the skeletons need no loops or conditionals, and avoiding a template
engine keeps the rendered Python free of HTML autoescaping. Every import is limited
to the standard library so ``--help``, ``--no-input``, and the render path run
without touching a database; the interactive wizard imports ``rich`` lazily inside
:func:`resolve_config`, so the heavy framework package and ``rich`` stay out of the
non-interactive import graph.
"""

from __future__ import annotations

import argparse
import importlib
import json
import keyword
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from rich.prompt import Prompt


class Flavor(StrEnum):
    """Represent the supported scaffold flavors."""

    TASK = "task"
    SCRIPT = "script"
    BASE = "base"


class RunMode(StrEnum):
    """Represent the task flavor's spec variant."""

    RUN_COMMAND = "run-command"
    RUN_PYTHON = "run-python"


_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"<<\s*(\w+)\s*>>")
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SCRIPT_SUFFIXES = frozenset({".sh", ".py"})
_SAMPLE_SCRIPT_TEMPLATE = Path("snippets") / "sample.sh.tmpl"
_REPO_ROOT = Path(__file__).resolve().parents[4]

PLUGINS_DIR = _REPO_ROOT / "app" / "extensions" / "apps"
TESTS_DIR = _REPO_ROOT / "tests" / "app" / "extensions" / "apps"
SETTINGS_FILE_ENV_VAR = "EXTENSIONS_SCAFFOLD_SETTINGS_FILE"
#: The ``settings.yaml`` new apps are registered in. Redirectable through
#: :data:`SETTINGS_FILE_ENV_VAR` so a caller that must not touch the working
#: tree can point the registration at a throwaway copy — the seam a child
#: process needs, since it cannot monkeypatch this module the way an in-process
#: caller does.
SETTINGS_FILE = Path(
    os.environ.get(SETTINGS_FILE_ENV_VAR) or _REPO_ROOT / "settings.yaml"
)

# Stdlib-only mirrors of ServiceTypeEnum / NavIcon member names: importing the
# real enums would pull pydantic/sqlalchemy (ServiceTypeEnum) or the framework
# package (NavIcon) into the no-database --help/render path. A drift-guard unit
# test asserts each tuple still equals its enum.
_SERVICE_TYPES = (
    "MYSQL",
    "POSTGRESQL",
    "MONGODB",
    "PROXYSQL",
    "HAPROXY",
    "EXTERNAL",
    "VALKEY",
)
_NAV_ICONS = (
    "ASSIGNMENT",
    "CODE",
    "SUPPORT_AGENT",
    "DESCRIPTION",
    "TROUBLESHOOT",
    "TABLE_CHART",
    "CHECK_CIRCLE",
    "MYSQL",
    "MONGO",
    "POSTGRESQL",
    "ARCHIVE",
    "SCIENCE",
    "BAR_CHART",
    "ACCOUNT_TREE",
)


class ScaffoldAbortedError(Exception):
    """Signal a user-initiated wizard abort before any filesystem write."""


@dataclass(frozen=True, slots=True)
class ScaffoldConfig:
    """Carry every resolved field the templates and registration render against.

    A config built via :meth:`defaults` from just ``name`` + ``flavor`` fills every
    other field with today's defaults, so the render is byte-identical to the
    pre-wizard scaffolder.

    :param name: The validated module name.
    :param flavor: The flavor to render.
    :param display_name: The human-facing label (title-cased from ``name`` unless
        overridden).
    :param item_display_name: The name for one record the app's create form
        produces, in mid-sentence form. Defaults to ``display_name``, which
        :func:`~app.extensions.apps.framework.conformance.check_item_display_names_declared`
        rejects for the ``task`` flavor — the only one whose rendered app declares
        a create form — so an unedited task scaffold fails the conformance suite
        until its author names the record. Nothing consults that detector at
        registration time; it runs over the registry from the test suite. The
        ``base`` and ``script`` flavors render ``forms=[]`` at the app level and
        are skipped, and a ``script_source`` app's schema can never gain form
        sections, so for it the app-level noun is unenforced rather than deferred.
    :param item_display_name_plural: The name for several such records. Defaults
        by pluralising a declared singular (same Django-style rules as
        :func:`~app.extensions.apps.framework.schema.pluralize_item_display_name`);
        when the singular is also unset, defaults to ``display_name`` so the
        conformance detector still trips an unedited task scaffold.
    :param description: The plugin description; ``None`` for the ``base`` flavor,
        whose ``BaseApp`` has no description field.
    :param service_type: The ``ServiceTypeEnum`` member name for the task form's
        ``ServiceRef`` (task flavor only).
    :param nav_icon: The ``NavIcon`` member name, or ``None`` for the frontend's
        default icon.
    :param group: The sidebar nav-group key, or ``None`` for a top-level entry.
    :param derive_update: Whether the task app derives a ``PUT`` update route.
    :param derive_delete: Whether the task app derives a ``DELETE`` route.
    :param enabled: Whether to register the app enabled in ``settings.yaml``.
    :param run_mode: The task spec variant (``run-command`` or ``run-python``).
    :param command: The run-command executable, or ``None`` for the ``echo``
        placeholder.
    :param payload_path: The run-python payload file to copy beside ``spec.py``, or
        ``None``.
    :param script_path: The script file to copy into the app's ``snippets/`` directory
        in place of the runnable sample (script flavor only), or ``None``.
    """

    name: str
    flavor: Flavor
    display_name: str
    item_display_name: str
    item_display_name_plural: str
    description: str | None
    service_type: str
    nav_icon: str | None
    group: str | None
    derive_update: bool
    derive_delete: bool
    enabled: bool
    run_mode: RunMode
    command: str | None
    payload_path: Path | None
    script_path: Path | None

    @classmethod
    def defaults(cls, name: str, flavor: Flavor) -> ScaffoldConfig:
        """Build a config from ``name`` + ``flavor``, filling today's defaults.

        :param name: The module name.
        :param flavor: The flavor to render.
        :return: A config whose every non-identity field holds the pre-wizard
            default, so the render stays byte-identical.
        """
        display_name = _derive_display_name(name)
        return cls(
            name=name,
            flavor=flavor,
            display_name=display_name,
            item_display_name=display_name,
            item_display_name_plural=display_name,
            description=_default_description(flavor, display_name),
            service_type="MYSQL",
            nav_icon=None,
            group=None,
            derive_update=True,
            derive_delete=True,
            enabled=False,
            run_mode=RunMode.RUN_COMMAND,
            command=None,
            payload_path=None,
            script_path=None,
        )


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """Carry the outcome of a single scaffold run for the caller's summary.

    :param name: The scaffolded app's module name.
    :param flavor: The flavor rendered.
    :param app_dir: The generated package directory under ``app/extensions/apps``.
    :param tests_dir: The generated test package directory under ``tests``.
    :param written: Every file the render wrote, in render order.
    :param settings_changed: Whether the ``settings.yaml`` entry was inserted (a
        pre-existing entry leaves it ``False``).
    :param enabled: Whether the registration entry was written enabled.
    :param payload_written: The copied run-python payload path, or ``None`` when no
        payload was supplied.
    :param script_written: The copied seed-script path under ``snippets/``, or ``None``
        when no script was supplied (the runnable sample was rendered instead).
    """

    name: str
    flavor: Flavor
    app_dir: Path
    tests_dir: Path
    written: list[Path]
    settings_changed: bool
    enabled: bool
    payload_written: Path | None
    script_written: Path | None


def validate_name(name: str) -> None:
    """Reject a name that is not a usable lowercase Python package identifier.

    The registry hardcodes ``app.extensions.apps.<name>`` as the import path, so the
    name must be importable as-is — no silent normalization (a slugify would emit
    hyphens or an empty string). An all-underscore name and a Python keyword are
    rejected even though both satisfy :data:`_NAME_PATTERN`.

    :param name: The candidate module name.
    :raises ValueError: When the name is empty, mixed-case, hyphenated, leads with
        a digit, is all underscores, or is a Python keyword.
    """
    if (
        not _NAME_PATTERN.match(name)
        or re.search(r"[a-z0-9]", name) is None
        or keyword.iskeyword(name)
    ):
        raise ValueError(
            f"invalid app name {name!r}: NAME must be a lowercase Python identifier "
            f"matching {_NAME_PATTERN.pattern} (letters, digits, underscores; at "
            "least one letter or digit; not a Python keyword) — for example 'my_app'"
        )


def _derive_display_name(name: str) -> str:
    """Convert a module name into a title-cased display label.

    :param name: The validated module name.
    :return: The underscore-split, title-cased display name.
    """
    parts = [part for part in name.split("_") if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _default_description(flavor: Flavor, display_name: str) -> str | None:
    """Return the flavor's placeholder description, or ``None`` for ``base``.

    :param flavor: The flavor being scaffolded.
    :param display_name: The resolved display name woven into the placeholder.
    :return: The ``TODO``-prefixed description for task/script, else ``None``
        (``BaseApp`` has no description field).
    """
    if flavor is Flavor.TASK:
        return f"TODO: describe what the {display_name} task does."
    if flavor is Flavor.SCRIPT:
        return f"TODO: describe what the {display_name} scripts do."
    return None


def _group_line(group: str | None) -> str:
    """Render the optional ``group=`` kwarg as a complete line or the empty string.

    :param group: The nav-group key, or ``None``.
    :return: A newline-terminated ``group=`` kwarg line when set, else ``""``.
    """
    if group is None:
        return ""
    return f"    group={json.dumps(group)},\n"


def _nav_icon_import_line(nav_icon: str | None) -> str:
    """Render the optional ``NavIcon`` import as a complete line or the empty string.

    :param nav_icon: The ``NavIcon`` member name, or ``None``.
    :return: The ``NavIcon`` import line with its trailing newline, else ``""``.
    """
    if nav_icon is None:
        return ""
    return "from app.extensions.apps.nav_icons import NavIcon\n"


def _nav_icon_kwarg_line(nav_icon: str | None) -> str:
    """Render the optional ``nav_icon=`` kwarg as a complete line or the empty string.

    :param nav_icon: The ``NavIcon`` member name, or ``None``.
    :return: A newline-terminated ``nav_icon=NavIcon.<MEMBER>`` kwarg line when set,
        else ``""``.
    """
    if nav_icon is None:
        return ""
    return f"    nav_icon=NavIcon.{nav_icon},\n"


def _docstring_safe(value: str) -> str:
    r"""Return a free-text value made safe for verbatim inclusion in a docstring.

    Unlike the ``*_repr`` placeholders (which wrap a value in a full
    :func:`json.dumps` string literal), the raw ``display_name`` is interpolated
    into the *interior* of triple-quoted docstrings across the templates. There an
    unescaped backslash starts an escape sequence (``\x`` / ``\u`` raise
    ``SyntaxError``) and an unbroken three-double-quote run terminates the
    docstring early,
    so a free-text display name could render syntactically invalid Python.
    Doubling backslashes and only neutralizing triple-quote runs keeps the
    interior valid while leaving ordinary ``"`` unchanged (avoiding needless
    backslashes in generated docstrings).

    :param value: The free-text value.
    :return: The value with backslashes doubled and any triple-quote runs
        neutralized.
    """
    return value.replace("\\", "\\\\").replace('"""', '\\"""')


def _build_context(config: ScaffoldConfig) -> dict[str, str]:
    """Derive the template variables from a resolved config.

    Free-text values that land inside a Python string literal
    (``display_name``, ``description``, ``command``) render via :func:`json.dumps`
    so embedded quotes/backslashes escape safely; ``display_name_doc`` covers the
    docstring-interior positions via :func:`_docstring_safe`; the optional
    ``group`` / ``nav_icon`` lines render as complete-line-or-empty composites so an
    omitted value collapses the whole line, import included.

    :param config: The resolved scaffold config.
    :return: The ``<< var >>`` substitution mapping for the flavor's templates. A
        superset is harmless — :func:`_render` only requires that placeholders that
        *appear* in a template exist here.
    """
    parts = [part for part in config.name.split("_") if part]
    command = "echo" if config.command is None else config.command
    schema_description = f"TODO: describe the {config.display_name} app."
    sample_script = (
        config.script_path.name if config.script_path is not None else "sample.sh"
    )
    return {
        "name": config.name,
        "name_upper": config.name.upper(),
        "sample_script": sample_script,
        "class_prefix": "".join(part[:1].upper() + part[1:] for part in parts),
        "display_name": config.display_name,
        "display_name_doc": _docstring_safe(config.display_name),
        "display_name_repr": json.dumps(config.display_name),
        "item_display_name_repr": json.dumps(config.item_display_name),
        "item_display_name_plural_repr": json.dumps(config.item_display_name_plural),
        "description_repr": json.dumps(config.description),
        "schema_description_repr": json.dumps(schema_description),
        "service_type": config.service_type,
        "command": json.dumps(command),
        "derive_update": str(config.derive_update),
        "derive_delete": str(config.derive_delete),
        "group_line": _group_line(config.group),
        "nav_icon_import_line": _nav_icon_import_line(config.nav_icon),
        "nav_icon_kwarg_line": _nav_icon_kwarg_line(config.nav_icon),
    }


def _spec_output_name(relative: Path, run_mode: RunMode) -> str | None:
    """Map a template to its rendered filename, selecting the run-mode spec variant.

    The task flavor ships two spec templates — ``spec.py.tmpl`` (run-command) and
    ``spec_run_python.py.tmpl`` (run-python). Only the chosen mode's variant is
    rendered, and the run-python variant is remapped onto ``spec.py`` so the app
    exports the framework's expected ``spec`` module either way; the non-chosen
    variant is skipped so neither a stray ``spec_run_python.py`` nor a second
    ``spec.py`` is written.

    :param relative: The template path relative to its flavor directory.
    :param run_mode: The resolved run mode.
    :return: The output filename (``.tmpl`` stripped), or ``None`` to skip this
        template.
    """
    stem = relative.name.removesuffix(".tmpl")
    if stem == "spec.py":
        return None if run_mode is RunMode.RUN_PYTHON else "spec.py"
    if stem == "spec_run_python.py":
        return "spec.py" if run_mode is RunMode.RUN_PYTHON else None
    return stem


def _target_path(name: str, relative: Path, file_name: str) -> Path:
    """Map a template's path under its flavor dir to its rendered destination.

    Templates under ``tests/`` land in the app's test package; everything else
    lands in the app package (preserving subdirectories such as ``snippets/``).

    :param name: The app's module name.
    :param relative: The template path relative to its flavor directory.
    :param file_name: The rendered output filename (``.tmpl`` already stripped).
    :return: The absolute destination path for the rendered file.
    """
    if relative.parts[0] == "tests":
        return TESTS_DIR / name / Path(*relative.parts[1:-1]) / file_name
    return PLUGINS_DIR / name / Path(*relative.parts[:-1]) / file_name


def _render(text: str, context: dict[str, str], source: Path) -> str:
    """Render the template text, substituting every ``<< var >>`` placeholder.

    :param text: The raw template text.
    :param context: The template variables from :func:`_build_context`.
    :param source: The template path, named in the error on an unknown placeholder.
    :return: The rendered text.
    :raises ValueError: When a placeholder names a variable absent from ``context``.
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise ValueError(f"unknown placeholder '<< {key} >>' in {source}")
        return context[key]

    return _PLACEHOLDER.sub(replace, text)


def render_app(config: ScaffoldConfig) -> list[Path]:
    """Render every template for ``config.flavor`` into the app and test trees.

    :param config: The resolved scaffold config.
    :return: The written file paths, in render order.
    :raises ValueError: Propagates from :func:`_render` when a template references
        an unknown placeholder.
    """
    template_root = _TEMPLATES_DIR / config.flavor
    context = _build_context(config)
    written = []
    for template_path in sorted(template_root.rglob("*.tmpl")):
        relative = template_path.relative_to(template_root)
        file_name = _spec_output_name(relative, config.run_mode)
        if file_name is None:
            continue
        if config.script_path is not None and relative == _SAMPLE_SCRIPT_TEMPLATE:
            continue
        rendered = _render(
            template_path.read_text(encoding="utf-8"), context, template_path
        )
        target = _target_path(config.name, relative, file_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        written.append(target)
    _ruff_fix(written)
    return written


def _ruff_fix(paths: list[Path]) -> None:
    """Run ruff lint-autofix and format on ``paths``.

    Resolve ``ruff`` beside the running Python interpreter so generated files use
    the project's pinned version. Silently skip the step when it is not present
    there as an executable file.

    :param paths: The rendered files to lint-fix and format.
    """
    py_files = [str(p) for p in paths if p.suffix == ".py"]
    if not py_files:
        return
    ruff = Path(sys.executable).parent / "ruff"
    if not ruff.is_file() or not os.access(ruff, os.X_OK):
        return
    subprocess.run(  # noqa: S603 # nosec B603
        [ruff, "check", "--fix", "--quiet", *py_files],
        check=False,
        cwd=_REPO_ROOT,
    )
    subprocess.run(  # noqa: S603 # nosec B603
        [ruff, "format", "--quiet", *py_files],
        check=False,
        cwd=_REPO_ROOT,
    )


def _indent_width(line: str) -> int:
    """Return the number of leading spaces on ``line``.

    :param line: The settings-file line to measure.
    :return: The count of leading space characters.
    """
    return len(line) - len(line.lstrip(" "))


def _find_default_section(lines: list[str]) -> int:
    """Return the index of the top-level ``default:`` key.

    :param lines: The settings file split into lines (newline-terminated).
    :return: The index of the top-level ``default:`` line.
    :raises ValueError: When no top-level ``default:`` section exists.
    """
    for index, line in enumerate(lines):
        if _indent_width(line) == 0 and line.strip() == "default:":
            return index
    raise ValueError(
        "settings.yaml has no top-level 'default:' section; cannot register the app"
    )


def _find_apps_key(lines: list[str], default_index: int) -> int:
    """Return the index of the ``APPS:`` key inside the ``default:`` section.

    :param lines: The settings file split into lines.
    :param default_index: The index of the ``default:`` key to scan forward from.
    :return: The index of the ``APPS:`` line inside the ``default:`` section.
    :raises ValueError: When the ``default:`` section declares no ``APPS`` block.
    """
    for index in range(default_index + 1, len(lines)):
        line = lines[index]
        if _indent_width(line) == 0 and line.strip():
            break
        if line.strip() == "APPS:":
            return index
    raise ValueError(
        "settings.yaml 'default:' section has no EXTENSIONS.APPS block; cannot register "
        "the app"
    )


def _default_apps_span(lines: list[str]) -> tuple[int, int]:
    """Return the ``[start, end)`` line span of the default ``EXTENSIONS.APPS`` list body.

    ``start`` is the first entry line after ``APPS:``; ``end`` is one past the
    last list line (the next key at the ``APPS:`` indent or shallower ends it),
    so an insertion at ``end`` appends after the final entry.

    :param lines: The settings file split into lines.
    :return: The ``(start, end)`` index span of the list body.
    """
    apps_index = _find_apps_key(lines, _find_default_section(lines))
    apps_indent = _indent_width(lines[apps_index])
    start = apps_index + 1
    end = start
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if _indent_width(line) <= apps_indent:
            break
        end = index + 1
    return start, end


def insert_app_entry(
    settings_text: str, name: str, *, enabled: bool = False
) -> tuple[str, bool]:
    """Insert a ``MODULE_NAME`` entry under the default ``EXTENSIONS.APPS`` block.

    A pure text transform — PyYAML cannot round-trip the file's comments, so the
    list body is edited line by line. Inserting a name that already has an entry is
    a no-op, which is what makes the registration idempotent.

    :param settings_text: The full ``settings.yaml`` contents.
    :param name: The module name to register.
    :param enabled: Whether to write the entry ``ENABLED: true``. Defaults to
        ``False`` (``ENABLED: false``), preserving the pre-wizard behaviour.
    :return: The (possibly unchanged) text and whether it was modified.
    :raises ValueError: When the default ``EXTENSIONS.APPS`` block is absent.
    """
    lines = settings_text.splitlines(keepends=True)
    start, end = _default_apps_span(lines)
    existing = re.compile(rf"\s*-?\s*MODULE_NAME:\s*{re.escape(name)}\s*$")
    if any(existing.match(line) for line in lines[start:end]):
        return settings_text, False
    enabled_literal = "true" if enabled else "false"
    entry = f"      - MODULE_NAME: {name}\n        ENABLED: {enabled_literal}\n"
    return "".join([*lines[:end], entry, *lines[end:]]), True


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text`` via a same-directory temp swap.

    A plain :meth:`Path.write_text` truncates the target before writing, so a
    concurrent reader (e.g. a parallel ``pytest-xdist`` worker copying the shared
    ``settings.yaml``) can observe a torn, partially-written file. Writing to a
    sibling temp file and :func:`os.replace`-ing it in is atomic on POSIX, so every
    reader sees either the whole old file or the whole new one — and a crash mid-run
    leaves the original intact. The target's mode is copied onto the temp file
    (``mkstemp`` creates it ``0600``).

    :param path: The file to overwrite.
    :param text: The new contents.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        shutil.copymode(path, tmp)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_settings_entry(name: str, *, enabled: bool = False) -> bool:
    """Register ``name`` in ``settings.yaml`` idempotently.

    :param name: The module name to register.
    :param enabled: Whether to write the entry enabled. Defaults to ``False``.
    :return: Whether the file was modified.
    :raises ValueError: Propagates from :func:`insert_app_entry` when
        ``settings.yaml`` has no default ``EXTENSIONS.APPS`` block.
    """
    new_text, changed = insert_app_entry(
        SETTINGS_FILE.read_text(encoding="utf-8"), name, enabled=enabled
    )
    if changed:
        _atomic_write(SETTINGS_FILE, new_text)
    return changed


def _holds_plugin(path: Path) -> bool:
    """Return whether ``path`` holds a real plugin.

    A path holds a plugin when it is a file, a broken symlink (one that does not
    resolve yet still occupies the path), or a directory containing at least one
    entry other than ``__pycache__/``.  A truly absent path, an empty directory,
    or a directory whose only child is ``__pycache__/`` is not a plugin.

    :param path: The filesystem path to check.
    :return: ``True`` when ``path`` holds a real plugin.
    """
    if not path.exists():
        return path.is_symlink()
    if not path.is_dir():
        return True
    return any(child.name != "__pycache__" for child in path.iterdir())


def _clobbered_target(name: str) -> Path | None:
    """Return the app or test directory holding a real plugin for ``name``, if any.

    :param name: The candidate module name.
    :return: The first occupied directory, or ``None`` when neither holds a plugin.
    """
    for existing in (PLUGINS_DIR / name, TESTS_DIR / name):
        if _holds_plugin(existing):
            return existing
    return None


def scaffold_app(config: ScaffoldConfig) -> ScaffoldResult:
    """Validate, refuse to clobber, render, copy the payload, and register the app.

    The guards fire before any write, so a rerun against an existing plugin (or a
    supplied payload that is not a file) writes nothing — no templates, no payload
    copy, and no ``settings.yaml`` edit.

    :param config: The resolved scaffold config.
    :return: The scaffold outcome for the caller's summary.
    :raises ValueError: When the name is invalid, or ``settings.yaml`` has no
        default ``EXTENSIONS.APPS`` block to register into.
    :raises FileExistsError: When the app or test package directory holds a real
        plugin.
    :raises FileNotFoundError: When a run-python payload path or a supplied script
        path does not point at a file.
    """
    validate_name(config.name)
    app_dir = PLUGINS_DIR / config.name
    tests_dir = TESTS_DIR / config.name
    clobbered = _clobbered_target(config.name)
    if clobbered is not None:
        raise FileExistsError(
            f"{clobbered} already exists; refusing to overwrite an existing plugin"
        )
    if config.payload_path is not None and not config.payload_path.is_file():
        raise FileNotFoundError(
            f"payload file {config.payload_path} does not exist; nothing was written"
        )
    if config.script_path is not None and not config.script_path.is_file():
        raise FileNotFoundError(
            f"script file {config.script_path} does not exist; nothing was written"
        )

    written = render_app(config)
    payload_written = None
    if config.payload_path is not None:
        payload_written = app_dir / "payload"
        shutil.copyfile(config.payload_path, payload_written)
    script_written = None
    if config.script_path is not None:
        script_written = app_dir / "snippets" / config.script_path.name
        script_written.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.script_path, script_written)
    settings_changed = write_settings_entry(config.name, enabled=config.enabled)
    return ScaffoldResult(
        name=config.name,
        flavor=config.flavor,
        app_dir=app_dir,
        tests_dir=tests_dir,
        written=written,
        settings_changed=settings_changed,
        enabled=config.enabled,
        payload_written=payload_written,
        script_written=script_written,
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser.

    ``--name`` is optional at the argparse layer — the interactive wizard supplies
    it; a ``--no-input`` (or non-TTY) run without a name is the error path
    (:func:`resolve_config`). Every prompt has a matching flag so a value supplied
    on the command line skips its prompt. Task-only flags on a script/base flavor,
    and ``--description`` on ``base``, are rejected in :func:`resolve_config`.

    :return: The parser exposing the field flags plus ``--no-input``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.extensions.apps.framework.scaffold",
        description="Scaffold a conformant PMM Extensions app skeleton.",
    )
    parser.add_argument("--name", help="the app's module name (lowercase identifier)")
    parser.add_argument(
        "--type",
        choices=list(Flavor),
        default=None,
        type=Flavor,
        help="the app flavor to generate (wizard prompts when unset; default: task)",
    )
    parser.add_argument("--display-name", help="the human-facing sidebar label")
    parser.add_argument(
        "--item-display-name",
        help="what one record the create form produces is called (e.g. 'backup')",
    )
    parser.add_argument(
        "--item-display-name-plural",
        help="what several such records are called (e.g. 'backups')",
    )
    parser.add_argument(
        "--description", help="the plugin description (task and script flavors)"
    )
    parser.add_argument(
        "--service-type",
        choices=_SERVICE_TYPES,
        help="the target service type for the task form (task flavor)",
    )
    parser.add_argument("--nav-icon", choices=_NAV_ICONS, help="the sidebar icon key")
    parser.add_argument("--group", help="the sidebar nav-group key")
    parser.add_argument(
        "--derive-update",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="derive a PUT update route (task flavor; default: yes)",
    )
    parser.add_argument(
        "--derive-delete",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="derive a DELETE route (task flavor; default: yes)",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="register the app enabled in settings.yaml (default: disabled)",
    )
    parser.add_argument(
        "--run-mode",
        choices=list(RunMode),
        default=None,
        type=RunMode,
        help="the task spec variant (task flavor; default: run-command)",
    )
    spec_group = parser.add_mutually_exclusive_group()
    spec_group.add_argument(
        "--command", help="the run-command executable (task flavor)"
    )
    spec_group.add_argument(
        "--payload", help="the run-python payload file to copy (task flavor)"
    )
    parser.add_argument(
        "--script",
        help="a .sh or .py script to seed the app with instead of the sample "
        "(script flavor)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="skip the interactive wizard and resolve every unset field to its default",
    )
    return parser


def _stdin_is_tty() -> bool:
    """Return whether standard input is an interactive terminal.

    Isolated so a test can force the interactive branch without a real TTY.

    :return: ``True`` when ``sys.stdin`` is a TTY.
    """
    return sys.stdin.isatty()


def _reject_flavor_incompatible_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace, flavor: Flavor
) -> None:
    """Reject task-only flags off task, ``--script`` off script, ``--description`` on base.

    Surfacing an inapplicable flag beats silently dropping it: the ``script`` and
    ``base`` flavors have no service type, run mode, command, payload, or CRUD
    capabilities; ``--script`` seeds only a script-flavor app; and ``base``'s
    ``BaseApp`` has no description field.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        the rejection.
    :param args: The parsed arguments to check.
    :param flavor: The resolved flavor the flags are checked against.
    """
    if flavor is not Flavor.TASK:
        task_only = [
            ("--service-type", args.service_type is not None),
            ("--run-mode", args.run_mode is not None),
            ("--command", args.command is not None),
            ("--payload", args.payload is not None),
            ("--derive-update/--no-derive-update", args.derive_update is not None),
            ("--derive-delete/--no-derive-delete", args.derive_delete is not None),
        ]
        for flag, supplied in task_only:
            if supplied:
                parser.error(
                    f"{flag} is only valid for the task flavor, not {flavor.value!r}"
                )
    if flavor is not Flavor.SCRIPT and args.script is not None:
        parser.error(
            f"--script is only valid for the script flavor, not {flavor.value!r}"
        )
    if flavor is Flavor.BASE and args.description is not None:
        parser.error(
            "--description is not valid for the base flavor (BaseApp has no "
            "description field)"
        )


def _reject_run_mode_conflict(
    parser: argparse.ArgumentParser, args: argparse.Namespace, mode: RunMode
) -> None:
    """Reject a ``--command`` / ``--payload`` that contradicts an explicit run mode.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        the conflict.
    :param args: The parsed arguments carrying ``command`` / ``payload``.
    :param mode: The explicitly requested run mode.
    """
    if mode is RunMode.RUN_COMMAND and args.payload is not None:
        parser.error("--payload requires --run-mode run-python")
    if mode is RunMode.RUN_PYTHON and args.command is not None:
        parser.error("--command requires --run-mode run-command")


def _resolve_run_mode_non_interactive(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[RunMode, str | None, Path | None]:
    """Resolve the run-mode / command / payload triple for a non-interactive run.

    Explicit ``--run-mode`` wins; when unset, a lone ``--payload`` infers
    run-python and everything else infers run-command. A run-python run needs a
    ``--payload`` (no prompt is available), and the payload must be a file.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        an invalid combination.
    :param args: The parsed arguments.
    :return: The ``(run_mode, command, payload_path)`` triple.
    """
    if args.run_mode is not None:
        _reject_run_mode_conflict(parser, args, args.run_mode)
        mode = args.run_mode
    elif args.payload is not None:
        mode = RunMode.RUN_PYTHON
    else:
        mode = RunMode.RUN_COMMAND

    if mode is RunMode.RUN_COMMAND:
        return mode, args.command, None
    if args.payload is None:
        parser.error("--run-mode run-python requires --payload in non-interactive mode")
    payload_path = Path(args.payload)
    if not payload_path.is_file():
        parser.error(f"--payload {args.payload!r} is not a file")
    return mode, None, payload_path


def _validate_script_path(parser: argparse.ArgumentParser, value: str) -> Path:
    """Return the resolved seed-script path, erroring on a missing or wrong-extension file.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports an
        invalid value.
    :param value: The raw ``--script`` value.
    :return: The validated script path.
    """
    path = Path(value)
    if not path.is_file():
        parser.error(f"--script {value!r} is not a file")
    if path.suffix not in _SCRIPT_SUFFIXES:
        parser.error(f"--script {value!r} must be a .sh or .py file")
    return path


def _default_item_display_name_plural(
    display_name: str, item_display_name: str, *, singular_declared: bool
) -> str:
    """Return the scaffold plural default for an unresolved plural flag/prompt.

    Mirrors :func:`~app.extensions.apps.framework.schema._fill_item_display_names`:
    pluralise only when the author declared a singular; otherwise keep
    ``display_name`` so an unedited scaffold still trips the conformance
    detector. Imports the pluraliser lazily so this module's import graph
    stays stdlib-only until a config is actually resolved.

    :param display_name: The resolved app title.
    :param item_display_name: The resolved singular (declared or title fallback).
    :param singular_declared: Whether the resolved singular differs from
        ``display_name`` (the same signal both resolve paths use).
    :return: The plural to seed into the generated app.
    """
    if not singular_declared:
        return display_name
    from app.extensions.apps.framework.schema import (  # noqa: PLC0415
        pluralize_item_display_name,
    )

    return pluralize_item_display_name(item_display_name)


def _resolve_non_interactive(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> ScaffoldConfig:
    """Resolve a config from flags alone, each unset field taking its default.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        a missing ``--name`` or an invalid run-mode combination.
    :param args: The parsed arguments.
    :return: The resolved config.
    """
    if not args.name:
        parser.error(
            "--name is required in non-interactive mode (no TTY, or --no-input)"
        )
    flavor = args.type or Flavor.TASK
    _reject_flavor_incompatible_flags(parser, args, flavor)
    display_name = args.display_name or _derive_display_name(args.name)
    item_display_name = args.item_display_name or display_name
    item_display_name_plural = args.item_display_name_plural or (
        _default_item_display_name_plural(
            display_name,
            item_display_name,
            singular_declared=item_display_name != display_name,
        )
    )
    description = (
        args.description
        if args.description is not None
        else _default_description(flavor, display_name)
    )
    if flavor is Flavor.TASK:
        run_mode, command, payload_path = _resolve_run_mode_non_interactive(
            parser, args
        )
    else:
        run_mode, command, payload_path = RunMode.RUN_COMMAND, None, None
    script_path = (
        _validate_script_path(parser, args.script)
        if flavor is Flavor.SCRIPT and args.script is not None
        else None
    )
    return ScaffoldConfig(
        name=args.name,
        flavor=flavor,
        display_name=display_name,
        item_display_name=item_display_name,
        item_display_name_plural=item_display_name_plural,
        description=description,
        service_type=args.service_type or "MYSQL",
        nav_icon=args.nav_icon,
        group=args.group,
        derive_update=args.derive_update if args.derive_update is not None else True,
        derive_delete=args.derive_delete if args.derive_delete is not None else True,
        enabled=args.enable,
        run_mode=run_mode,
        command=command,
        payload_path=payload_path,
        script_path=script_path,
    )


def _prompt_optional(label: str, prompt: type[Prompt]) -> str | None:
    """Resolve an optional free-text value from a prompt; blank input maps to ``None``.

    :param label: The prompt label.
    :param prompt: The ``rich`` ``Prompt`` class.
    :return: The entered value, or ``None`` when left blank.
    """
    return prompt.ask(label, default="") or None


def _resolve_interactive(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> ScaffoldConfig:
    """Collect a config through the interactive wizard, prompting for unset fields.

    Imports ``rich`` lazily so the non-interactive import graph stays stdlib-only.
    The flavor gates which fields prompt (``base`` skips description / service type
    / run mode / capabilities; ``script`` additionally skips service type / run
    mode / command / payload), a flag supplied on the command line skips its
    prompt, the module-name prompt validates and clobber-checks immediately with a
    re-prompt on failure, and a pre-generation preview plus a final confirmation
    gate the write.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        a contradictory run-mode combination.
    :param args: The parsed arguments.
    :return: The resolved config.
    :raises ScaffoldAbortedError: When the user declines the final confirmation or
        interrupts the wizard.
    """
    rich_console = importlib.import_module("rich.console")
    rich_prompt = importlib.import_module("rich.prompt")
    console = rich_console.Console()
    prompt_cls = rich_prompt.Prompt
    confirm_cls = rich_prompt.Confirm

    try:
        name = _prompt_name(args, prompt_cls)
        flavor = (
            args.type
            if args.type is not None
            else Flavor(
                prompt_cls.ask(
                    "Flavor",
                    choices=[member.value for member in Flavor],
                    default=Flavor.TASK.value,
                    case_sensitive=False,
                )
            )
        )
        _reject_flavor_incompatible_flags(parser, args, flavor)
        display_name = args.display_name or prompt_cls.ask(
            "Display name", default=_derive_display_name(name)
        )
        item_display_name = args.item_display_name or prompt_cls.ask(
            "Record name (singular)", default=display_name
        )
        plural_default = _default_item_display_name_plural(
            display_name,
            item_display_name,
            singular_declared=item_display_name != display_name,
        )
        item_display_name_plural = args.item_display_name_plural or prompt_cls.ask(
            "Record name (plural)", default=plural_default
        )
        description = None
        if flavor is not Flavor.BASE:
            description = (
                args.description
                if args.description is not None
                else prompt_cls.ask(
                    "Description", default=_default_description(flavor, display_name)
                )
            )
        service_type = "MYSQL"
        if flavor is Flavor.TASK:
            service_type = args.service_type or prompt_cls.ask(
                "Service type",
                choices=list(_SERVICE_TYPES),
                default="MYSQL",
                case_sensitive=False,
            )
        nav_icon = (
            args.nav_icon
            if args.nav_icon is not None
            else prompt_cls.ask(
                "Sidebar icon (blank for the default)",
                choices=["", *_NAV_ICONS],
                default="",
                case_sensitive=False,
            )
            or None
        )
        group = (
            args.group
            if args.group is not None
            else _prompt_optional("Nav group (blank for a top-level entry)", prompt_cls)
        )
        derive_update = True
        derive_delete = True
        if flavor is Flavor.TASK:
            derive_update = (
                args.derive_update
                if args.derive_update is not None
                else confirm_cls.ask("Derive a PUT update route?", default=True)
            )
            derive_delete = (
                args.derive_delete
                if args.derive_delete is not None
                else confirm_cls.ask("Derive a DELETE route?", default=True)
            )
        enabled = args.enable or confirm_cls.ask(
            "Enable the app in settings.yaml now?", default=False
        )
        run_mode, command, payload_path = RunMode.RUN_COMMAND, None, None
        if flavor is Flavor.TASK:
            run_mode, command, payload_path = _collect_run_mode(
                parser, args, prompt_cls
            )
        script_path = None
        if flavor is Flavor.SCRIPT:
            script_path = _collect_script_path(parser, args, prompt_cls)

        config = ScaffoldConfig(
            name=name,
            flavor=flavor,
            display_name=display_name,
            item_display_name=item_display_name,
            item_display_name_plural=item_display_name_plural,
            description=description,
            service_type=service_type,
            nav_icon=nav_icon,
            group=group,
            derive_update=derive_update,
            derive_delete=derive_delete,
            enabled=enabled,
            run_mode=run_mode,
            command=command,
            payload_path=payload_path,
            script_path=script_path,
        )
        _print_preview(console, config)
        if not confirm_cls.ask("Scaffold this app now?", default=True):
            raise ScaffoldAbortedError
    except KeyboardInterrupt:
        raise ScaffoldAbortedError from None
    return config


def _prompt_name(args: argparse.Namespace, prompt: type[Prompt]) -> str:
    """Return the module name, prompting with immediate validation when unset.

    A ``--name`` supplied on the command line is returned as-is (validated later by
    :func:`scaffold_app`); an interactive prompt re-asks until the name both
    validates and does not clobber an existing plugin.

    :param args: The parsed arguments carrying an optional ``--name``.
    :param prompt: The ``rich`` ``Prompt`` class.
    :return: The resolved module name.
    """
    if args.name:
        return args.name
    while True:
        candidate = prompt.ask("Module name (lowercase identifier)")
        try:
            validate_name(candidate)
        except ValueError as error:
            sys.stderr.write(f"{error}\n")
            continue
        clobbered = _clobbered_target(candidate)
        if clobbered is not None:
            sys.stderr.write(
                f"{clobbered} already holds a plugin; choose another name\n"
            )
            continue
        return candidate


def _collect_run_mode(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    prompt: type[Prompt],
) -> tuple[RunMode, str | None, Path | None]:
    """Collect the run-mode / command / payload triple, prompting where needed.

    Explicit ``--run-mode`` wins (a contradicting ``--command`` / ``--payload`` is
    an error); an unset mode is inferred from a lone ``--command`` / ``--payload``
    or, failing both, prompted. A run-python run without ``--payload`` prompts for
    the payload path and re-asks until it names a file.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        a contradictory combination.
    :param args: The parsed arguments.
    :param prompt: The ``rich`` ``Prompt`` class.
    :return: The ``(run_mode, command, payload_path)`` triple.
    """
    if args.run_mode is not None:
        _reject_run_mode_conflict(parser, args, args.run_mode)
        mode = args.run_mode
    elif args.payload is not None:
        mode = RunMode.RUN_PYTHON
    elif args.command is not None:
        mode = RunMode.RUN_COMMAND
    else:
        mode = RunMode(
            prompt.ask(
                "Run mode",
                choices=[m.value for m in RunMode],
                default=RunMode.RUN_COMMAND.value,
                case_sensitive=False,
            )
        )

    if mode is RunMode.RUN_COMMAND:
        return mode, args.command, None
    if args.payload is not None:
        payload_path = Path(args.payload)
        if not payload_path.is_file():
            parser.error(f"--payload {args.payload!r} is not a file")
        return mode, None, payload_path
    while True:
        candidate = Path(prompt.ask("Payload file path"))
        if candidate.is_file():
            return mode, None, candidate
        sys.stderr.write(f"payload file {candidate} does not exist\n")


def _collect_script_path(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    prompt: type[Prompt],
) -> Path | None:
    """Resolve the optional seed script, prompting when unset (blank keeps the sample).

    A ``--script`` supplied on the command line is validated (an invalid one is an
    error); an unset script prompts and re-asks until the entry names an existing
    ``.sh`` / ``.py`` file or is left blank to keep the runnable sample.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports an
        invalid ``--script`` flag.
    :param args: The parsed arguments carrying an optional ``--script``.
    :param prompt: The ``rich`` ``Prompt`` class.
    :return: The validated script path, or ``None`` to keep the runnable sample.
    """
    if args.script is not None:
        return _validate_script_path(parser, args.script)
    while True:
        answer = prompt.ask("Script file (blank for the runnable sample)", default="")
        if not answer:
            return None
        candidate = Path(answer)
        if candidate.is_file() and candidate.suffix in _SCRIPT_SUFFIXES:
            return candidate
        sys.stderr.write(f"{answer} is not an existing .sh or .py file\n")


def _print_preview(console: Console, config: ScaffoldConfig) -> None:
    """Render the resolved config and a preview of the generated ``app.py`` to the console.

    :param console: The ``rich`` ``Console`` used for output.
    :param config: The resolved config to preview.
    :raises ValueError: Propagates from :func:`_render` when ``app.py.tmpl``
        references an unknown placeholder.
    """
    template = _TEMPLATES_DIR / config.flavor / "app.py.tmpl"
    preview = _render(
        template.read_text(encoding="utf-8"), _build_context(config), template
    )
    console.print(f"\nScaffolding {config.flavor.value!r} app {config.name!r}:")
    console.print(f"  display name: {config.display_name}")
    console.print(f"  enabled:      {config.enabled}")
    if config.payload_path is not None:
        console.print(f"  payload:      {config.payload_path}")
    if config.script_path is not None:
        console.print(f"  script:       {config.script_path}")
    console.print("\n--- app.py preview ---")
    console.print(preview, markup=False, highlight=False)


def resolve_config(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> ScaffoldConfig:
    """Resolve the parsed arguments into a frozen :class:`ScaffoldConfig`.

    Runs the interactive wizard when standard input is a TTY and ``--no-input`` is
    absent; otherwise resolves every unset field to its default. Each path resolves
    the flavor (``--type``, else the wizard prompt, else the ``task`` default) and
    rejects flavor-incompatible flags against it.

    :param parser: The parser whose :meth:`~argparse.ArgumentParser.error` reports
        an invalid combination.
    :param args: The parsed arguments.
    :return: The resolved config.
    :raises ScaffoldAbortedError: When the user aborts the interactive wizard.
    """
    if _stdin_is_tty() and not args.no_input:
        return _resolve_interactive(parser, args)
    return _resolve_non_interactive(parser, args)


def _print_summary(result: ScaffoldResult) -> None:
    """Summarize where the app was written and how to manage it.

    :param result: The scaffold outcome to summarise.
    """
    if result.settings_changed:
        state = "ENABLED" if result.enabled else "DISABLED"
        registration = f"Registered {result.name!r} {state} in settings.yaml."
    else:
        registration = (
            f"{result.name!r} was already registered in settings.yaml; left unchanged."
        )
    payload_note = ""
    if result.payload_written is not None:
        payload_note = f"  payload: {result.payload_written}\n"
    script_note = ""
    if result.script_written is not None:
        script_note = f"  script: {result.script_written}\n"
    sys.stdout.write(
        f"Scaffolded {result.flavor.value!r} app {result.name!r}:\n"
        f"  app:   {result.app_dir}\n"
        f"  tests: {result.tests_dir}\n"
        f"{payload_note}"
        f"{script_note}"
        f"\n{registration} Manage it from the Apps page in the sidebar "
        "(/admin/apps) once you have filled in the skeleton.\n"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the scaffolder CLI.

    :param argv: The argument vector, or ``None`` to read ``sys.argv``.
    :return: The process exit code (``0`` on success, ``1`` on a wizard abort or a
        clobber / render / registration / payload failure).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve_config(parser, args)
    except ScaffoldAbortedError:
        sys.stderr.write("Aborted; nothing was written.\n")
        return 1
    try:
        result = scaffold_app(config)
    except (FileExistsError, FileNotFoundError, ValueError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
