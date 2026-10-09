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

"""Resolve a plugin callable named by a ``"module:function"`` path, with caching.

Several per-task hooks let the owning plugin *declare* a callable by
``"module:function"`` string on the task and have core resolve it lazily the
first time it is needed — keeping the tasks service free of static ``app.extensions``
imports. Those hooks share this resolver so the ``importlib``
resolve-and-cache boilerplate lives in one place.
This process-local cache is correct because the import path deterministically
resolves to the same callable in every worker and needs no invalidation.

A hook path names a callable that this service imports and invokes, so the
module it names is constrained to
:attr:`app.tasks.config.TasksSettings.HOOK_MODULE_ALLOWLIST`. The same check
runs at the write boundary (:class:`app.tasks.models.TaskWrite`) and here, so a
path that reached the database by any other route fails closed at invoke time
instead of being imported.
"""

import importlib
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Cache of resolved callables, keyed by ``"module:function"`` path.
_RESOLVED: dict[str, Callable[..., Any]] = {}


class HookPathNotAllowedError(ValueError):
    """Define exception for a hook path that is malformed or not allow-listed.

    Subclasses :class:`ValueError` so the hook call sites, which already treat a
    bad path as a skipped enrichment rather than a failure, keep degrading
    gracefully.
    """


def is_dotted_module_path(module_path: str) -> bool:
    """Return whether every dot-separated segment is a Python identifier.

    :param module_path: A dotted module name, or the module part of a hook path.
    :return: ``True`` when the module part is a well-formed dotted name.
    """
    return bool(module_path) and all(
        part.isidentifier() for part in module_path.split(".")
    )


def validate_hook_path(path: str, field: str = "hook path") -> str:
    """Return ``path`` when it names an allow-listed callable, else raise.

    A hook path is admitted only when it is a well-formed ``"module:function"``
    pair naming a public function in a module under one of the configured
    allow-listed roots. Everything else is rejected — a malformed pair, a dunder
    or private attribute, a module outside the namespace — because the resolved
    callable is imported and invoked by the tasks service.

    :param path: The candidate callable path in ``"module:function"`` form.
    :param field: The name of the field carrying the path, quoted in the
        rejection message. Defaults to a generic label.
    :return: The validated path, unchanged.
    :raises HookPathNotAllowedError: When the path is malformed or names a
        module outside the allow-listed namespace.
    """
    # Deferred: app.tasks.config imports the Nomad executor, which imports
    # app.tasks.models, which imports this module, so tasks_settings does not
    # exist yet while that chain is still initialising.
    from app.tasks.config import tasks_settings  # noqa: PLC0415

    allowed = tasks_settings.HOOK_MODULE_ALLOWLIST
    module_path, _, func_name = path.partition(":")
    if (
        not is_dotted_module_path(module_path)
        or not func_name.isidentifier()
        or func_name.startswith("_")
    ):
        reason = 'is not a "module:function" path naming a public callable'
    elif not any(
        module_path == root or module_path.startswith(f"{root}.") for root in allowed
    ):
        reason = "names a module outside the allow-listed namespace"
    else:
        return path

    logger.warning("Rejected %s %r: it %s.", field, path, reason)
    raise HookPathNotAllowedError(
        f"{field} {path!r} {reason}; allow-listed module roots: {', '.join(allowed)}"
    )


def resolve_hook(path: str) -> Callable[..., Any]:
    """Import and return the callable named by a ``"module:function"`` path.

    Validate the path against the allow-list before anything else, so neither a
    denied module nor a cache entry poisoned before the allow-list narrowed can
    be served. Cache the resolved callable so a repeated lookup for the same
    path skips the import.

    :param path: The callable path in ``"module:function"`` form.
    :return: The resolved callable.
    :raises HookPathNotAllowedError: When the path is malformed or names a
        module outside the allow-listed namespace.
    :raises ImportError: When the named module cannot be imported.
    :raises AttributeError: When the module has no attribute ``function``.
    """
    validate_hook_path(path)
    cached = _RESOLVED.get(path)
    if cached is not None:
        return cached
    module_path, func_name = path.split(":", 1)
    func = getattr(importlib.import_module(module_path), func_name)
    _RESOLVED[path] = func
    return func
