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

"""Build immutable override snapshots for a settings class."""

from __future__ import annotations

__all__ = [
    "SubChainUpdates",
    "build_snapshot",
    "load_stored_overrides",
    "load_stored_rows",
    "merge_nested_overrides",
    "stored_overrides_from_rows",
]

import logging
from collections import defaultdict
from functools import partial
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from app.core.encryption import DecryptionError
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token, SettingOverride
from app.core.settings_override.registry import (
    clear_cached_properties,
    coerce_field_value,
    coerce_nested_field_value,
    is_hot_reloadable,
    is_nested_overridable_parent,
    materialize_override_value,
    MaterializerPurpose,
)
from app.core.settings_override.resolution import resolve_field_in_model
from app.core.settings_override.secret_storage import decrypt_secret_leaves
from app.core.utils.cross_field import drop_broken_overrides
from app.core.utils.pydantic import annotation_pydantic_class

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic.fields import FieldInfo
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.core.config import BaseYamlSettings

logger = logging.getLogger(__name__)

# A sub-chain is the tuple of canonical attribute names *relative to a parent
# model* (i.e. the resolver's full chain minus its top-level segment).
SubChainUpdates = dict[tuple[str, ...], Any]


async def build_snapshot(
    session: AsyncSession,
    settings_cls: type[BaseYamlSettings],
    base_settings: BaseModel | None = None,
) -> MappingProxyType[str, Any]:
    """Build a frozen snapshot of active overrides for a settings class.

    Two override shapes are handled:

    * **Top-level** rows (``key`` has no ``__``) override a whole field. Only
      rows whose ``key`` is declared HOT on ``settings_cls`` are kept; the
      coerced value is stored under the field name. A row targeting a
      ``NESTED_ONLY`` (or otherwise non-HOT) parent is logged and skipped.
    * **Nested** rows (``key`` contains ``__``) override an individual field
      inside a nested Pydantic-model attribute. Rows are grouped by their
      top-level prefix; the parent must be nested-overridable (``HOT`` or
      ``NESTED_ONLY``). Each leaf is coerced via
      :func:`coerce_nested_field_value` and folded into one
      ``model_copy(update=...)`` per parent, stored under the top-level key.

    Rows whose values fail Pydantic coercion, target unknown / not-overridable
    fields, or hit a non-Pydantic intermediate are logged and skipped without
    affecting their siblings.

    Overrides that together break one of the class's ``CROSS_FIELD_RULES`` are
    dropped as a group, so those fields take their base values and the
    application keeps running; one warning names them.

    The storage token used to filter override rows is derived from
    ``settings_cls`` via :func:`setting_class_token`: the SCREAMING_SNAKE
    form of the Pydantic class ``__name__``, matching the spelling already
    stored in ``settingoverride.setting_class``.

    :param session: The async SQLModel session used to query overrides. Must
        be bound to the engine of the service that owns ``settings_cls``.
    :param settings_cls: The Pydantic settings class being snapshotted.
    :param base_settings: The resolved (YAML/env) settings instance whose
        nested-parent attributes seed each merged copy, so leaves with no
        override row fall back to their YAML/env values, and the values
        cross-field rules judge un-overridden fields by. When ``None``, both come
        from each field's declared default -- sufficient for direct callers that
        only exercise top-level rows.
    :return: An immutable mapping of field name to coerced typed value.
    :raises sqlalchemy.exc.SQLAlchemyError: If the database query fails
        (connection lost, schema mismatch, transaction aborted, ...). This
        family is not caught here; the caller is expected to log-and-skip
        or swallow at a higher level (e.g. the background refresher's
        per-cycle ``except``).
    """
    stored = await load_stored_overrides(session, settings_cls, base_settings)
    if base_settings is None:
        base_value_of = partial(_declared_default, settings_cls)
    else:
        base_value_of = partial(getattr, base_settings)
    snapshot, _ = drop_broken_overrides(
        settings_cls.CROSS_FIELD_RULES, stored, base_value_of
    )
    if ignored := sorted(stored.keys() - snapshot.keys()):
        logger.warning(
            "Overrides ignored for %s: %s violate cross-field rules",
            settings_cls.__name__,
            ", ".join(ignored),
        )
    return MappingProxyType(snapshot)


async def load_stored_overrides(
    session: AsyncSession,
    settings_cls: type[BaseYamlSettings],
    base_settings: BaseModel | None = None,
) -> dict[str, Any]:
    """Return every usable stored override, coerced, before cross-field rules apply.

    :param session: The async SQLModel session used to query overrides.
    :param settings_cls: The Pydantic settings class being read.
    :param base_settings: The resolved (YAML/env) instance nested overrides merge
        onto, or ``None`` for each field's declared default.
    :return: A mapping of field name to coerced typed value.
    :raises sqlalchemy.exc.SQLAlchemyError: If the database query fails.
    """
    rows = await SettingsOverrideManager.list(
        session, setting_class=setting_class_token(settings_cls), is_active=True
    )
    return stored_overrides_from_rows(settings_cls, rows, base_settings)


def stored_overrides_from_rows(
    settings_cls: type[BaseYamlSettings],
    rows: Iterable[SettingOverride],
    base_settings: BaseModel | None = None,
) -> dict[str, Any]:
    """Return every usable override among ``rows``, coerced, before cross-field rules apply.

    The row-processing half of :func:`load_stored_overrides`, for a caller that
    already holds the class's active rows and must not read them again. Each
    unusable row, and each nested group whose merged parent fails validation, is
    logged and skipped.

    :param settings_cls: The Pydantic settings class being read.
    :param rows: The class's active override rows.
    :param base_settings: The resolved (YAML/env) instance nested overrides merge
        onto, or ``None`` for each field's declared default.
    :return: A mapping of field name to coerced typed value.
    """
    whole, leaves = _split_rows(settings_cls, rows)
    stored = dict(whole)
    for parent, updates in leaves.items():
        try:
            stored[parent] = merge_nested_overrides(
                settings_cls, parent, whole, updates, base_settings
            )
        except ValidationError as exc:
            logger.warning(
                "Nested override group for %s.%s failed to build a merged model: %s",
                settings_cls.__name__,
                parent,
                exc,
            )
    return stored


async def load_stored_rows(
    session: AsyncSession,
    settings_cls: type[BaseYamlSettings],
) -> tuple[dict[str, Any], dict[str, SubChainUpdates]]:
    """Return the stored overrides, coerced, before any leaf merges onto its parent.

    :param session: The async SQLModel session used to query overrides.
    :param settings_cls: The Pydantic settings class being read.
    :return: The whole-field values by field name, and each nested parent's leaf
        values by canonical parent name.
    :raises sqlalchemy.exc.SQLAlchemyError: If the database query fails.
    """
    rows = await SettingsOverrideManager.list(
        session, setting_class=setting_class_token(settings_cls), is_active=True
    )
    return _split_rows(settings_cls, rows)


def _split_rows(
    settings_cls: type[BaseYamlSettings],
    rows: Iterable[SettingOverride],
) -> tuple[dict[str, Any], dict[str, SubChainUpdates]]:
    """Coerce ``rows`` into whole-field values and each nested parent's leaf values.

    :param settings_cls: The Pydantic settings class being read.
    :param rows: The class's active override rows.
    :return: The whole-field values by field name, and each nested parent's leaf
        values by canonical parent name.
    """
    whole: dict[str, Any] = {}
    nested_groups = defaultdict(list)
    for row in rows:
        if "__" in row.key:
            # Case-fold the prefix so mixed-case sibling rows for one parent merge
            # into a single group instead of clobbering each other.
            nested_groups[row.key.split("__", 1)[0].lower()].append(row)
            continue
        _apply_top_level_row(whole, settings_cls, settings_cls.__name__, row)
    leaves: dict[str, SubChainUpdates] = {}
    for prefix, group in nested_groups.items():
        if resolved := _nested_group_updates(
            settings_cls, settings_cls.__name__, prefix, group
        ):
            parent, updates = resolved
            leaves[parent] = updates
    return whole, leaves


def _declared_default(settings_cls: type[BaseYamlSettings], name: str) -> Any:
    """Return the default ``settings_cls`` declares for field ``name``.

    :param settings_cls: The Pydantic settings class.
    :param name: The field name.
    :return: The field's declared default, from its factory when it has one.
    """
    return settings_cls.model_fields[name].get_default(call_default_factory=True)


def _apply_top_level_row(
    snapshot: dict[str, Any],
    settings_cls: type[BaseYamlSettings],
    setting_class: str,
    row: SettingOverride,
) -> None:
    """Coerce and store one whole-field override row into ``snapshot``.

    :param snapshot: The in-progress snapshot mapping, mutated in place.
    :param settings_cls: The Pydantic settings class being snapshotted.
    :param setting_class: The class identifier, for log messages.
    :param row: The override row to apply.
    """
    field_info = settings_cls.model_fields.get(row.key)
    if field_info is None:
        logger.warning(
            "Override for unknown field ignored: %s.%s",
            setting_class,
            row.key,
        )
        return
    if not is_hot_reloadable(settings_cls, row.key):
        logger.warning(
            "Override for non-HOT field ignored: %s.%s",
            setting_class,
            row.key,
        )
        return
    try:
        raw = decrypt_secret_leaves(settings_cls, row.key, row.value)
        snapshot[row.key] = materialize_override_value(
            settings_cls,
            row.key,
            field_info,
            raw,
            purpose=MaterializerPurpose.SNAPSHOT,
        )
    # DecryptionError subclasses ValueError, so this clause must stay above the
    # coercion one or an undecryptable row is reported as a bad type instead.
    except DecryptionError as exc:
        logger.warning(
            "Override for %s.%s could not be decrypted: %s",
            setting_class,
            row.key,
            exc,
        )
    except ValueError as exc:
        logger.warning(
            "Override for %s.%s failed type coercion: %s",
            setting_class,
            row.key,
            exc,
        )


def _nested_group_updates(
    settings_cls: type[BaseYamlSettings],
    setting_class: str,
    prefix: str,
    group: list[SettingOverride],
) -> tuple[str, SubChainUpdates] | None:
    """Coerce every nested-override row sharing ``prefix`` into its parent's updates.

    :param settings_cls: The Pydantic settings class being snapshotted.
    :param setting_class: The class identifier, for log messages.
    :param prefix: The shared top-level field name for this group.
    :param group: The nested rows whose key starts with ``prefix__``.
    :return: The canonical parent name and its coerced leaves, or ``None`` when
        the group targets no usable parent or no row survives coercion.
    """
    resolved_parent = resolve_field_in_model(settings_cls, prefix)
    if resolved_parent is None:
        logger.warning(
            "Nested override for unknown parent ignored: %s.%s",
            setting_class,
            prefix,
        )
        return None
    # Use the canonical field name so the snapshot key matches the proxy
    # attribute the reader looks up.
    canonical_prefix, field_info = resolved_parent
    if not is_nested_overridable_parent(settings_cls, canonical_prefix):
        logger.warning(
            "Nested override for non-overridable parent ignored: %s.%s",
            setting_class,
            canonical_prefix,
        )
        return None
    parent_cls = annotation_pydantic_class(field_info.annotation)
    if parent_cls is None:
        logger.warning(
            "Nested override for non-model parent ignored: %s.%s",
            setting_class,
            canonical_prefix,
        )
        return None
    sub_updates: SubChainUpdates = {}
    for row in group:
        try:
            raw = decrypt_secret_leaves(settings_cls, row.key, row.value)
            chain, value = coerce_nested_field_value(settings_cls, row.key, raw)
        except DecryptionError as exc:
            logger.warning(
                "Nested override for %s.%s could not be decrypted: %s",
                setting_class,
                row.key,
                exc,
            )
            continue
        except KeyError:
            logger.warning(
                "Nested override for unknown or not-overridable field ignored: %s.%s",
                setting_class,
                row.key,
            )
            continue
        except ValidationError as exc:
            logger.warning(
                "Nested override for %s.%s failed type coercion: %s",
                setting_class,
                row.key,
                exc,
            )
            continue
        # Rows are listed newest-first; keep the first value seen for each
        # canonical sub-chain so the newest row wins deterministically when two
        # raw keys resolve to the same leaf (e.g. a legacy non-canonical row
        # alongside the canonical one).
        sub_updates.setdefault(chain[1:], value)
    if not sub_updates:
        return None
    return canonical_prefix, sub_updates


def _parent_base_value(
    snapshot: dict[str, Any],
    field_info: FieldInfo,
    prefix: str,
    base_settings: BaseModel | None,
) -> BaseModel | None:
    """Return the base parent instance to merge nested overrides onto.

    Resolution order:

    1. A whole-object override already written to ``snapshot[prefix]`` by
       :func:`_apply_top_level_row`, so a HOT parent's whole-object override is
       not silently discarded when nested-leaf rows for the same parent are
       merged on top of the YAML/env value afterwards.
    2. The live YAML/env value from ``base_settings`` so leaves with no override
       row keep their configured values.
    3. The field's declared default, when no resolved instance is available.

    :param snapshot: The in-progress snapshot mapping; consulted for a
        whole-object override already stored under ``prefix``.
    :param field_info: The parent field's metadata.
    :param prefix: The parent field name.
    :param base_settings: The resolved settings instance, or ``None``.
    :return: The base parent model, or ``None`` when the parent is unset and
        must be instantiated from the nested leaves alone.
    """
    stored = snapshot.get(prefix)
    if isinstance(stored, BaseModel):
        return stored
    if base_settings is not None:
        value = getattr(base_settings, prefix, None)
        return value if isinstance(value, BaseModel) else None
    if isinstance(field_info.default, BaseModel):
        return field_info.default
    return None


def merge_nested_overrides(
    settings_cls: type[BaseYamlSettings],
    parent: str,
    whole: dict[str, Any],
    sub_updates: SubChainUpdates,
    base_settings: BaseModel | None,
) -> Any:
    """Return the value ``parent`` takes once ``sub_updates`` merge onto it.

    Each leaf is coerced on its own, so a validator on the parent field, such as
    a bound spanning two of its leaves, can only judge the merged result.

    :param settings_cls: The Pydantic settings class declaring ``parent``.
    :param parent: The canonical name of a nested-overridable model field.
    :param whole: The whole-field overrides, whose model for ``parent`` is merged
        onto in preference to ``base_settings``.
    :param sub_updates: Mapping of canonical sub-chain to coerced value.
    :param base_settings: The resolved settings instance, or ``None``.
    :return: The merged parent, as the field's annotation validates it.
    :raises ValidationError: If the merged parent cannot be built, or the field's
        validators refuse it.
    """
    field_info = settings_cls.model_fields[parent]
    parent_cls = annotation_pydantic_class(field_info.annotation)
    if parent_cls is None:
        raise TypeError(f"{settings_cls.__name__}.{parent} is not a model field")
    parent_value = _parent_base_value(whole, field_info, parent, base_settings)
    merged = _merge_into(parent_value, parent_cls, sub_updates)
    return coerce_field_value(field_info, merged)


def _merge_into(
    parent_value: BaseModel | None,
    parent_cls: type[BaseModel],
    sub_updates: SubChainUpdates,
) -> BaseModel:
    """Apply ``sub_updates`` to ``parent_value``, instantiating it when unset.

    :param parent_value: The current parent model, or ``None`` when the parent
        attribute is unset (e.g. an ``Optional`` field defaulting to ``None``).
    :type parent_value: BaseModel | None
    :param parent_cls: The parent model class, used to instantiate when
        ``parent_value`` is ``None``.
    :type parent_cls: type[BaseModel]
    :param sub_updates: Mapping of canonical sub-chain to coerced value.
    :type sub_updates: SubChainUpdates
    :return: A merged copy (or fresh instance) of the parent model.
    :rtype: BaseModel
    :raises ValidationError: If an unset parent cannot be instantiated from the
        provided leaves (required fields missing).
    """
    if parent_value is None:
        return _instantiate_from_updates(parent_cls, sub_updates)
    return _build_nested_update(parent_value, sub_updates)


def _build_nested_update(
    parent: BaseModel,
    sub_updates: SubChainUpdates,
) -> BaseModel:
    """Fold ``sub_updates`` into ``parent`` via one ``model_copy`` per level.

    Updates are grouped by their first sub-segment: single-segment chains
    become direct leaf updates, while longer chains recurse into the matching
    child model (instantiating it from defaults when the attribute is unset).

    :param parent: The base parent model to copy.
    :type parent: BaseModel
    :param sub_updates: Mapping of canonical sub-chain (relative to ``parent``)
        to coerced value.
    :type sub_updates: SubChainUpdates
    :return: A merged copy of ``parent`` with every leaf applied.
    :rtype: BaseModel
    :raises ValidationError: If an unset intermediate cannot be instantiated
        from the provided leaves.
    """
    direct = {}
    deeper = defaultdict(dict)
    for chain, value in sub_updates.items():
        head, *rest = chain
        if rest:
            deeper[head][tuple(rest)] = value
        else:
            direct[head] = value
    for head, child_updates in deeper.items():
        child_field = type(parent).model_fields[head]
        child_cls = annotation_pydantic_class(child_field.annotation)
        if child_cls is None:
            # Intermediate is not a Pydantic model -- the resolver should have
            # rejected this path, so treat it defensively as a no-op leaf set.
            continue
        # When a whole-child override is present in the same batch, layer the
        # deeper leaves on top of it instead of the original parent state.
        child = (
            direct[head]
            if isinstance(direct.get(head), BaseModel)
            else getattr(parent, head, None)
        )
        direct[head] = coerce_field_value(
            child_field, _merge_into(child, child_cls, child_updates)
        )
    merged = parent.model_copy(update=direct)
    # ``model_copy`` is shallow and carries over already-evaluated
    # ``cached_property`` memos from ``parent``; drop them at every level so a
    # nested child's cached value cannot go stale against its new fields.
    clear_cached_properties(merged)
    return merged


def _instantiate_from_updates(
    model_cls: type[BaseModel],
    sub_updates: SubChainUpdates,
) -> BaseModel:
    """Instantiate ``model_cls`` from nested leaves when no base instance exists.

    Used when an ``Optional`` parent (or intermediate) is ``None`` on the base
    settings: the model is constructed from the provided leaves plus its own
    field defaults via ``model_validate`` (so alias-aware / case-insensitive
    key handling applies).

    :param model_cls: The model class to instantiate.
    :type model_cls: type[BaseModel]
    :param sub_updates: Mapping of canonical sub-chain to coerced value.
    :type sub_updates: SubChainUpdates
    :return: A freshly-built model instance.
    :rtype: BaseModel
    :raises ValidationError: If required fields are missing from the leaves.
    """
    fields = {}
    deeper = defaultdict(dict)
    for chain, value in sub_updates.items():
        head, *rest = chain
        if rest:
            deeper[head][tuple(rest)] = value
        else:
            fields[head] = value
    for head, child_updates in deeper.items():
        child_cls = annotation_pydantic_class(model_cls.model_fields[head].annotation)
        if child_cls is None:
            continue
        # Layer deeper leaves on top of a same-batch whole-child override.
        if isinstance(fields.get(head), BaseModel):
            fields[head] = _build_nested_update(fields[head], child_updates)
        else:
            fields[head] = _instantiate_from_updates(child_cls, child_updates)
    return model_cls.model_validate(fields)
