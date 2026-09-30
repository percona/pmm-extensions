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

"""Answer which ``(settings class, key)`` pairs stay overridable.

``Settings.SETTINGS_OVERRIDE.ALLOWED_KEYS`` drives every predicate here.
``None``, the default, means unrestricted: every classification behaves exactly
as if this module did not exist. A set activates a default-locked allowlist, so
only the pairs it names stay overridable. Anything else is refused until an
entry deliberately allows it, including a field marked overridable by later
work.

Each entry is spelled ``"<SettingsClassName>.<KEY>"``, where the class token is
the Pydantic class ``__name__`` and the key token is either a top-level field
name or a ``__``-delimited canonical nested path, the same spelling an override
row carries.
"""

__all__ = ["has_allowed_key_under", "is_key_allowed", "is_restriction_active"]

from functools import lru_cache


def _allowed_entries() -> frozenset[str] | None:
    """Return the configured entries as a hashable set, or ``None`` when unset.

    :return: The entry set projected to a ``frozenset``, or ``None`` when
        ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` places no restriction.
    """
    # circular-import: config imports registry imports policy (this module)
    from app.core.config import settings

    entries = settings.SETTINGS_OVERRIDE.ALLOWED_KEYS
    return None if entries is None else frozenset(entries)


@lru_cache(maxsize=8)
def _keys_by_class(entries: frozenset[str]) -> dict[str, frozenset[str]]:
    """Group entries by their class token, dropping the token from each key.

    :param entries: The configured entries, already projected to a ``frozenset``
        so the parse is cached per distinct value.
    :return: A mapping of class token to the keys allowed on that class.
    """
    grouped: dict[str, set[str]] = {}
    for entry in entries:
        class_token, _, key = entry.partition(".")
        grouped.setdefault(class_token, set()).add(key)
    return {class_token: frozenset(keys) for class_token, keys in grouped.items()}


def _allowed_keys_for(setting_class: str) -> frozenset[str] | None:
    """Return the keys allowed on one settings class, or ``None`` when unset.

    :param setting_class: The Pydantic class ``__name__`` to look up.
    :return: The allowed keys, or ``None`` when ``SETTINGS_OVERRIDE.ALLOWED_KEYS``
        places no restriction, which every caller reads as "allow everything".
    """
    entries = _allowed_entries()
    if entries is None:
        return None
    return _keys_by_class(entries).get(setting_class, frozenset())


def is_restriction_active() -> bool:
    """Return whether ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` restricts anything.

    :return: ``True`` when an allowlist is configured, ``False`` when every
        statically overridable field stays overridable.
    """
    return _allowed_entries() is not None


def is_key_allowed(setting_class: str, key: str) -> bool:
    """Return whether one settings key may be overridden.

    :param setting_class: The Pydantic class ``__name__`` the key belongs to.
    :param key: The canonical override key: a top-level field name or a
        ``__``-delimited nested path.
    :return: ``True`` when no allowlist is configured, or when it names this
        exact pair.
    """
    allowed = _allowed_keys_for(setting_class)
    return allowed is None or key in allowed


def has_allowed_key_under(setting_class: str, parent: str) -> bool:
    """Return whether any allowed key targets a nested parent or its descendants.

    Answers whether the parent is still worth addressing at all: a parent none
    of whose leaves may be written no longer accepts nested overrides, while one
    with a single allowed leaf stays addressable so that leaf can be reached.

    :param setting_class: The Pydantic class ``__name__`` the parent belongs to.
    :param parent: The top-level field name of the nested parent.
    :return: ``True`` when no allowlist is configured, when an entry names the
        parent itself, or when an entry names a key beneath it.
    """
    allowed = _allowed_keys_for(setting_class)
    if allowed is None:
        return True
    prefix = f"{parent}__"
    return any(key == parent or key.startswith(prefix) for key in allowed)
