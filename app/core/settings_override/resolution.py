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

"""Resolve ``__``-delimited override keys against nested settings models."""

from __future__ import annotations

__all__ = [
    "SettingProvenance",
    "canonical_override_key",
    "override_provenance_for_rows",
    "override_rows_for_key",
    "resolve_field_in_model",
    "resolve_nested_field",
    "resolve_nested_field_metadata",
    "resolve_nested_segments",
    "resolve_nested_value",
]

from collections.abc import Iterator, Mapping
from typing import Any, NamedTuple, TYPE_CHECKING

from app.core.settings_override.constants import NESTED_VALUE_MISSING
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.utils.date_time import make_datetime_utc
from app.core.utils.pydantic import annotation_pydantic_class

if TYPE_CHECKING:
    from datetime import datetime

    from pydantic import BaseModel
    from pydantic.fields import FieldInfo
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.core.settings_override.models import SettingOverride
    from app.core.settings_override.proxy import OverridableSettingsProxy
    from app.core.settings_override.registry import FieldMetadata


def resolve_field_in_model(
    model_cls: type[BaseModel], segment: str
) -> tuple[str, FieldInfo] | None:
    """Return ``(canonical_attribute_name, FieldInfo)`` for ``segment`` on ``model_cls``.

    Performs case-insensitive matching across:

    1. The Pydantic attribute name in ``model_cls.model_fields``.
    2. The field's ``alias`` / ``validation_alias`` / ``serialization_alias``.

    Required so that ``SECURITY_HEADERS__X_FRAME_OPTIONS_DENY`` (uppercase, the
    override-key convention) resolves to ``x_frame_options_deny`` on
    :class:`app.core.middleware.security_headers.SecurityHeadersOptions` (a
    ``BaseCaseInsensitiveModel`` declaring lowercase attribute names with an
    uppercase alias).

    :param model_cls: The Pydantic model class to search.
    :type model_cls: type[BaseModel]
    :param segment: The path segment to resolve.
    :type segment: str
    :return: ``(attribute_name, FieldInfo)`` on success, or ``None`` when no
        field matches.
    :rtype: tuple[str, FieldInfo] | None
    """
    if segment in model_cls.model_fields:
        return segment, model_cls.model_fields[segment]
    seg_lower = segment.lower()
    for name, info in model_cls.model_fields.items():
        if name.lower() == seg_lower:
            return name, info
        for alias in (info.alias, info.validation_alias, info.serialization_alias):
            if isinstance(alias, str) and alias.lower() == seg_lower:
                return name, info
    return None


def resolve_nested_segments(
    settings_cls: type[BaseModel],
    key: str,
) -> list[tuple[type[BaseModel], str, FieldInfo]] | None:
    """Resolve every ``__`` segment of ``key`` to ``(owner_cls, canonical_name, FieldInfo)``.

    Walks one segment at a time, descending into nested Pydantic models. Returns
    ``None`` when any segment is unresolvable, the path hits a non-Pydantic
    intermediate, or the key is empty. The list preserves order from the
    top-level parent down to the leaf, so callers can inspect intermediate
    fields (e.g. for an explicit ``not_overridable_field`` marker) and not just
    the leaf. Each entry carries its owning class so classifiers can consult that
    class's :data:`app.core.settings_override.registry.INHERITED_MARKERS_ATTR`
    overlay.

    :param settings_cls: The top-level Pydantic settings class.
    :type settings_cls: type[BaseModel]
    :param key: The ``__``-delimited override key.
    :type key: str
    :return: One ``(owner_cls, canonical_name, FieldInfo)`` per segment, or ``None``.
    :rtype: list[tuple[type[BaseModel], str, FieldInfo]] | None
    """
    if not key:
        return None
    segments = key.split("__")
    resolved_chain = []
    current_cls = settings_cls
    for i, seg in enumerate(segments):
        resolved = resolve_field_in_model(current_cls, seg)
        if resolved is None:
            return None
        canonical, info = resolved
        resolved_chain.append((current_cls, canonical, info))
        if i < len(segments) - 1:
            next_cls = annotation_pydantic_class(info.annotation)
            if next_cls is None:
                return None
            current_cls = next_cls
    return resolved_chain


def resolve_nested_field(
    settings_cls: type[BaseModel],
    key: str,
) -> tuple[tuple[str, ...], FieldInfo] | None:
    """Resolve a ``__``-delimited path to its canonical attribute chain and leaf field.

    Walks one segment at a time, descending into nested Pydantic models.
    Returns ``None`` when any segment is unresolvable, the path hits a
    non-Pydantic intermediate, or the path is empty.

    The returned chain uses canonical (case-corrected) attribute names so the
    caller can plug it straight into nested ``model_copy(update=...)`` calls.

    :param settings_cls: The top-level Pydantic settings class.
    :type settings_cls: type[BaseModel]
    :param key: The override key to resolve (e.g.
        ``"SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE"``).
    :type key: str
    :return: ``((canonical_segment, ...), leaf_FieldInfo)`` or ``None``.
    :rtype: tuple[tuple[str, ...], FieldInfo] | None
    """
    resolved = resolve_nested_segments(settings_cls, key)
    if resolved is None:
        return None
    return tuple(name for _owner, name, _info in resolved), resolved[-1][2]


def _mapping_segment_or_default(
    mapping: Mapping[Any, Any], segment: str, default: Any
) -> Any:
    """Read ``segment`` from ``mapping`` (case-insensitive) or return ``default``."""
    if segment in mapping:
        return mapping[segment]
    seg_lower = segment.lower()
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() == seg_lower:
            return value
    return default


def resolve_nested_value(
    *,
    settings_cls: type[BaseModel],
    proxy: OverridableSettingsProxy,
    key: str,
) -> tuple[FieldInfo, Any]:
    """Return the leaf field metadata and current value for a nested key.

    Walks the chain segment by segment using the resolver's canonical
    (case-corrected) names, so the returned value reflects the merged snapshot
    copy when an override is active and the YAML/env value otherwise. Each
    segment is read as a :class:`~collections.abc.Mapping` key when the current
    node is a mapping, and as an attribute otherwise. A **missing** segment
    returns :data:`NESTED_VALUE_MISSING`; a present ``None`` intermediate
    collapses the leaf to ``None`` (optional-intermediate contract).

    :param settings_cls: The Pydantic settings class the key belongs to.
    :type settings_cls: type[BaseModel]
    :param proxy: The proxy whose attribute chain yields the current value.
    :type proxy: OverridableSettingsProxy
    :param key: The ``__``-delimited nested key.
    :type key: str
    :return: A ``(leaf_FieldInfo, current_value)`` pair. ``current_value`` may
        be :data:`NESTED_VALUE_MISSING` when a segment is absent.
    :rtype: tuple[FieldInfo, Any]
    :raises KeyError: If ``key`` does not resolve to a nested field on
        ``settings_cls``.
    """
    resolved = resolve_nested_field(settings_cls, key)
    if resolved is None:
        raise KeyError(key)
    chain, leaf_info = resolved
    current = proxy
    for segment in chain:
        if current is None:
            return leaf_info, None
        if isinstance(current, Mapping):
            segment_value = _mapping_segment_or_default(
                current, segment, NESTED_VALUE_MISSING
            )
            if segment_value is NESTED_VALUE_MISSING:
                return leaf_info, NESTED_VALUE_MISSING
            current = segment_value
            continue
        if not hasattr(current, segment):
            return leaf_info, NESTED_VALUE_MISSING
        current = getattr(current, segment)
    return leaf_info, current


def canonical_override_key(settings_cls: type[BaseModel], key: str) -> str:
    """Return the canonical ``__``-joined attribute path for a nested key.

    Case-insensitive spellings of the same nested path (e.g.
    ``security_headers__x_frame_options_deny`` and its uppercase form) collapse
    to one deterministic key so DB rows, snapshot lookups, and DELETE/GET by key
    all agree. Top-level keys and keys that do not resolve are returned
    unchanged.

    :param settings_cls: The Pydantic settings class the key belongs to.
    :type settings_cls: type[BaseModel]
    :param key: The override key, possibly ``__``-delimited.
    :type key: str
    :return: The canonical key, or ``key`` unchanged when not a resolvable
        nested path.
    :rtype: str
    """
    if "__" not in key:
        return key
    resolved = resolve_nested_field(settings_cls, key)
    if resolved is None:
        return key
    chain, _ = resolved
    return "__".join(chain)


class SettingProvenance(NamedTuple):
    """Carry the last-written stamp reported for one overridden key.

    :param updated_at: When the contributing row was last written, falling back
        to its creation time for a row that predates explicit stamping. Stamps
        carry second granularity.
    :param updated_by: The username that last wrote the contributing row, or
        ``None`` for a row written before the actor column existed.
    """

    updated_at: datetime
    updated_by: str | None


def _provenance_keys_for_row(
    settings_cls: type[BaseModel],
    row: SettingOverride,
) -> Iterator[str]:
    """Yield every key one override row reports an override for.

    The row's own stored ``key`` always counts, which keeps the report correct
    when a row was stored under a non-canonical casing. A ``__``-delimited row
    additionally contributes every canonical prefix of its resolved chain: the
    top-level parent, each intermediate sub-model path, and the canonical leaf
    key. A promoted parent therefore reports an override when only deeper nested
    rows exist.

    :param settings_cls: The Pydantic settings class the row belongs to.
    :param row: One active override row.
    :return: The stored key followed by each canonical prefix of its chain.
    """
    yield row.key
    if "__" not in row.key:
        return
    resolved = resolve_nested_field(settings_cls, row.key)
    if resolved is None:
        return
    chain, _ = resolved
    for i in range(1, len(chain) + 1):
        yield "__".join(chain[:i])


def override_provenance_for_rows(
    settings_cls: type[BaseModel],
    rows: list[SettingOverride],
) -> dict[str, SettingProvenance]:
    """Return the last-written stamp for every key with an active override.

    The mapping's key set is exactly the set of keys carrying an override, so a
    caller derives ``has_override`` as ``key in mapping`` and the flag cannot
    drift from the stamps beside it. :func:`_provenance_keys_for_row` decides
    which keys each row contributes.

    When several rows contribute to one key, which is the ordinary case for a
    nested parent, the row with the latest stamp wins, breaking ties on the
    higher ``id``. Ties are the common case rather than a corner: ``utc_now``
    zeroes microseconds and one PATCH batch stamps every key it writes with a
    single shared timestamp.

    ``id`` is creation order, not write order, so the tie-break orders rows the
    same batch wrote but cannot order two separate writes that land in the same
    second: there the reported pair comes from whichever contributing row was
    created later, which need not be the one written later. Second-granularity
    stamps make that distinction unrecoverable rather than merely unqueried, so
    the tie-break buys determinism, not accuracy.

    :param settings_cls: The Pydantic settings class the rows belong to.
    :param rows: The active override rows for the class.
    :return: One :class:`SettingProvenance` per key carrying an override.
    """
    ranked: dict[str, tuple[tuple[datetime, int], SettingOverride]] = {}
    for row in rows:
        # SQLModel skips validation on a ``table=True`` model, so a stamp loaded
        # from the database bypasses the ``UTCDatetime`` normalizer and can
        # arrive naive, which would not compare against an aware sibling.
        rank = (make_datetime_utc(row.updated_at or row.created_at), row.id or 0)
        for key in _provenance_keys_for_row(settings_cls, row):
            current = ranked.get(key)
            if current is None or rank > current[0]:
                ranked[key] = (rank, row)
    return {
        key: SettingProvenance(updated_at=rank[0], updated_by=row.updated_by)
        for key, (rank, row) in ranked.items()
    }


def _stored_key_matches_override_key(
    settings_cls: type[BaseModel],
    stored_key: str,
    key: str,
) -> bool:
    """Return whether a stored override key resolves to the same field as ``key``.

    Nested keys go through :func:`canonical_override_key`. Top-level keys also
    match case-insensitively so mixed-case stored keys remain visible to
    DELETE/PATCH after the filter moved into Python. Snapshot application still
    ignores unknown casing via
    :func:`app.core.settings_override.cache._apply_top_level_row`; DELETE
    removes those inert rows, and PATCH heals their stored key to the
    canonical spelling so the next snapshot can read them.

    :param settings_cls: The Pydantic settings class the rows belong to.
    :param stored_key: The key column value from an override row.
    :param key: The canonical override key requested by the caller.
    :return: ``True`` when ``stored_key`` should be treated as the same override.
    """
    if canonical_override_key(settings_cls, stored_key) == key:
        return True
    if "__" in key or "__" in stored_key:
        return False
    return stored_key.casefold() == key.casefold()


async def override_rows_for_key(
    session: AsyncSession,
    *,
    settings_cls: type[BaseModel],
    setting_class: str,
    key: str,
) -> list[SettingOverride]:
    """Return the :class:`SettingOverride` rows whose stored key resolves to ``key``.

    Lists every row for ``setting_class`` and keeps those whose stored ``key``
    matches via :func:`_stored_key_matches_override_key`. That makes a legacy
    non-canonically-cased nested or top-level row visible to DELETE and PATCH,
    which previously matched the stored column with dialect-dependent SQL
    equality and, after the filter moved into Python, missed mixed-case
    top-level rows.

    Inactive rows are included: both write paths currently match on
    ``(setting_class, key)`` alone, so an inactive row stays deletable and
    re-activatable.

    :param session: The sub-app's database session.
    :param settings_cls: The Pydantic settings class the rows belong to.
    :param setting_class: The storage token (from
        :func:`~app.core.settings_override.models.setting_class_token`) used to
        filter override rows.
    :param key: The canonical override key to resolve against.
    :return: Every matching row, in the order :meth:`SettingsOverrideManager.list`
        returns them.
    """
    rows = await SettingsOverrideManager.list(session, setting_class=setting_class)
    return [
        row
        for row in rows
        if _stored_key_matches_override_key(settings_cls, row.key, key)
    ]


def resolve_nested_field_metadata(
    settings_cls: type[BaseModel], key: str
) -> FieldMetadata | None:
    """Return introspected metadata for a ``__``-delimited nested override key.

    Resolves ``key`` to its leaf field and synthesises a
    :class:`app.core.settings_override.registry.FieldMetadata`
    whose ``key`` is the full nested key while every other attribute
    (annotation, default, description, secret/complex flags) is taken from the
    leaf field. The reported ``reload`` is ``HOT`` for an override-eligible
    leaf (the default under a nested-overridable parent) and
    ``NOT_OVERRIDABLE`` when the leaf *or any intermediate in its chain* is
    explicitly marked by
    :func:`app.core.settings_override.registry.not_overridable_field`, or when
    ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` withholds the leaf. That is the same
    chain check that gates PATCH, so the reported classification matches what
    an override would actually be allowed to do. ``is_advanced`` is
    chain-resolved via
    :func:`app.core.settings_override.registry.chain_has_advanced`, so a leaf
    inherits the flag from an advanced parent.

    :param settings_cls: The top-level Pydantic settings class.
    :param key: The ``__``-delimited override key.
    :return: The synthesised leaf metadata, or ``None`` when ``key`` does not
        resolve to a nested field.
    """
    # Deferred, and not safe to hoist: registry imports
    # resolution.resolve_nested_segments at module scope, so a module-level
    # import here closes registry -> resolution -> registry. Whichever module
    # the interpreter reaches first would then be asked for names it has not
    # bound yet. The failure depends on the entry point rather than on this
    # file, so the module-boundary tests import each entry point in a fresh
    # interpreter instead of trusting that this one stays acyclic.
    from app.core.settings_override.registry import (  # noqa: PLC0415
        _field_contains_secret,
        _field_is_complex,
        _resolve_default,
        chain_has_advanced,
        chain_is_locked,
        FieldMetadata,
        ReloadClassification,
    )

    resolved = resolve_nested_field(settings_cls, key)
    if resolved is None:
        return None
    _chain, leaf_info = resolved
    reload = (
        ReloadClassification.NOT_OVERRIDABLE
        if chain_is_locked(settings_cls, key)
        else ReloadClassification.HOT
    )
    return FieldMetadata(
        key=key,
        annotation=leaf_info.annotation,
        default=_resolve_default(leaf_info),
        description=leaf_info.description,
        reload=reload,
        is_secret=_field_contains_secret(leaf_info),
        is_complex=_field_is_complex(leaf_info.annotation),
        is_advanced=chain_has_advanced(settings_cls, key),
    )
