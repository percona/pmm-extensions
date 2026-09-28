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

"""Preserve masked secrets and credential URLs submitted back by a PATCH."""

from __future__ import annotations

__all__ = [
    "preserve_credential_urls_in_model_payload",
    "preserve_patch_credential_url_value",
    "preserve_patch_secret_value",
    "preserve_secrets_in_model_payload",
]

import typing
from collections.abc import Mapping, Sequence
from types import UnionType
from typing import Any, TYPE_CHECKING, Union

from pydantic import BaseModel, SecretBytes, SecretStr

from app.core.settings_override.registry import (
    _field_contains_secret,
    _stable_collection_sort_key,
    _unwrap_secret_value,
    annotated_type,
    annotation_is_credential_url,
    is_credential_url_field,
    SECRET_STR_MASK,
)
from app.core.utils.fields import preserve_credential_url_password
from app.core.utils.pydantic import annotation_pydantic_class

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


def _read_mapping_or_model_attr(current: Any, name: str) -> Any:
    """Read ``name`` from a live model or a materializer fingerprint mapping.

    :param current: The stored value, a model instance or a mapping.
    :param name: The field name to read.
    :return: The attribute or mapping entry, or ``None`` when either is absent.
    """
    if current is None:
        return None
    if isinstance(current, Mapping):
        return current.get(name)
    return getattr(current, name, None)


def preserve_credential_urls_in_model_payload(
    model_cls: type[BaseModel],
    current: Any,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore masked URL passwords inside a materializer PATCH payload.

    Each child is classified with the **position** predicate rather than the
    subtree one :func:`app.core.settings_override.registry.is_credential_url_field`
    asks. A model-typed child whose
    own leaf is a credential URL answers the subtree question ``True``, would
    take the scalar branch below, fail its ``isinstance(result[name], str)``
    guard and ``continue`` — skipping the nested recursion that child needs.

    :param model_cls: The model whose fields ``incoming`` is keyed by.
    :param current: The effective stored value to restore passwords from.
    :param incoming: The payload submitted in the PATCH body.
    :return: ``incoming`` with any masked URL passwords restored.
    """
    result = dict(incoming)
    for name, field_info in model_cls.model_fields.items():
        if name not in result:
            continue
        leaf_current = _read_mapping_or_model_attr(current, name)
        if annotation_is_credential_url(annotated_type(field_info)):
            if isinstance(result[name], str) and leaf_current is not None:
                result[name] = preserve_credential_url_password(
                    str(leaf_current), result[name]
                )
            continue
        nested_cls = annotation_pydantic_class(field_info.annotation)
        if nested_cls and isinstance(result[name], Mapping):
            result[name] = preserve_credential_urls_in_model_payload(
                nested_cls, leaf_current, result[name]
            )
    return result


def preserve_patch_credential_url_value(
    field_info: FieldInfo,
    current: Any,
    incoming: Any,
) -> Any:
    """Restore masked credentials in a PATCH value before validation/persist.

    Handles credential-bearing URL passwords (``****``) and
    :class:`~pydantic.SecretStr` / :class:`~pydantic.SecretBytes` JSON masks
    (:data:`app.core.settings_override.registry.SECRET_STR_MASK`). Non-mask submissions are left unchanged.

    Routing is decided by the payload shape before
    :func:`app.core.settings_override.registry.is_credential_url_field` is consulted.
    That predicate descends into
    nested models, so it answers "does this subtree carry a credential URL",
    not "is this field itself one". A model-typed field holding a
    credential-URL leaf answers ``True`` to the first question while needing
    the model-payload walk, not the scalar restore.

    :param field_info: The Pydantic field metadata for the target attribute.
    :param current: The effective stored value (model, mapping, or secret wrapper).
    :param incoming: The value submitted in the PATCH body.
    :return: ``incoming`` with any masked credentials restored from ``current``.
    """
    parent_cls = annotation_pydantic_class(field_info.annotation)
    if parent_cls is not None and isinstance(incoming, Mapping):
        value = preserve_credential_urls_in_model_payload(parent_cls, current, incoming)
    elif (
        is_credential_url_field(field_info)
        and isinstance(incoming, str)
        and current is not None
    ):
        value = preserve_credential_url_password(str(current), incoming)
    else:
        value = incoming
    return preserve_patch_secret_value(field_info, current, value)


def _annotation_is_secret_valued_dict(annotation: Any) -> bool:
    """Return whether ``annotation`` is a ``dict`` whose values are secrets.

    Unwraps optional/union wrappers at the top level only (does not descend into
    nested :class:`~pydantic.BaseModel` fields), matching
    ``dict[str, SecretStr]`` / ``dict[str, SecretBytes]`` shapes.

    :param annotation: The field annotation to inspect.
    :return: ``True`` when the annotation is a secret-valued mapping type.
    """
    candidates: list[Any] = [annotation]
    origin = typing.get_origin(annotation)
    if origin in {Union, UnionType}:
        candidates = list(typing.get_args(annotation))
    for candidate in candidates:
        if candidate is None or candidate is type(None):
            continue
        dict_origin = typing.get_origin(candidate)
        if dict_origin not in {dict, Mapping}:
            continue
        try:
            _, value_ann = typing.get_args(candidate)
        except ValueError:
            continue
        value_candidates: list[Any] = [value_ann]
        value_origin = typing.get_origin(value_ann)
        if value_origin in {Union, UnionType}:
            value_candidates = list(typing.get_args(value_ann))
        for value_type in value_candidates:
            if isinstance(value_type, type) and issubclass(
                value_type, SecretStr | SecretBytes
            ):
                return True
    return False


def _annotation_is_secret_valued_sequence(annotation: Any) -> bool:
    """Return whether ``annotation`` is a collection whose elements are secrets.

    Matches ``list[SecretStr]`` / ``set[SecretStr]`` /
    ``tuple[SecretBytes, ...]``-style shapes (optional/union wrappers
    unwrapped at the top level only).

    :param annotation: The field annotation to inspect.
    :return: ``True`` when the annotation is a secret-element collection type.
    """
    candidates: list[Any] = [annotation]
    origin = typing.get_origin(annotation)
    if origin in {Union, UnionType}:
        candidates = list(typing.get_args(annotation))
    collection_origins = {list, set, tuple, frozenset, Sequence}
    for candidate in candidates:
        if candidate is None or candidate is type(None):
            continue
        coll_origin = typing.get_origin(candidate)
        if coll_origin not in collection_origins:
            continue
        args = typing.get_args(candidate)
        if not args:
            continue
        element = args[0]
        element_candidates: list[Any] = [element]
        element_origin = typing.get_origin(element)
        if element_origin in {Union, UnionType}:
            element_candidates = list(typing.get_args(element))
        for element_type in element_candidates:
            if isinstance(element_type, type) and issubclass(
                element_type, SecretStr | SecretBytes
            ):
                return True
    return False


def _stable_collection_items(current: Any) -> list[Any]:
    """Return ``current`` as a list, sorting when the source is unordered.

    :param current: A stored ``list``/``set``/``tuple``/``frozenset``, or
        ``None``.
    :return: A list of items; ``set``/``frozenset`` inputs are sorted by
        :func:`app.core.settings_override.registry._stable_collection_sort_key`.
    """
    if isinstance(current, set | frozenset):
        return sorted(current, key=_stable_collection_sort_key)
    if isinstance(current, list | tuple):
        return list(current)
    return []


def _collection_item_value_score(
    incoming: Mapping[str, Any], current: BaseModel
) -> int:
    """Score how well ``incoming`` identifies the stored model ``current``.

    Masked secret fields are ignored so a round-tripped GET payload can still
    match on stable non-secret identity (e.g. ``api_endpoint``). Equal
    non-masked values raise the score; mismatches lower it.

    :param incoming: One element of the PATCH collection payload.
    :param current: A live stored model candidate.
    :return: A higher score means a better identity match.
    """
    score = 0
    for name in type(current).model_fields:
        if name not in incoming:
            continue
        incoming_val = incoming[name]
        if incoming_val == SECRET_STR_MASK:
            continue
        current_val = getattr(current, name, None)
        unwrapped = _unwrap_secret_value(current_val)
        compare: Any = unwrapped if unwrapped is not None else current_val
        if incoming_val == compare or str(incoming_val) == str(compare):
            score += 2
        else:
            score -= 10
    return score


def _preserve_masked_secret_scalar(current: Any, incoming: Any) -> Any:
    """Restore a stored secret when ``incoming`` equals :data:`app.core.settings_override.registry.SECRET_STR_MASK`.

    :param current: The live stored value, possibly a ``SecretStr``/``SecretBytes``.
    :param incoming: The PATCH value that may be the secret JSON mask.
    :return: The unwrapped stored secret when ``incoming`` is the mask and
        ``current`` is a secret wrapper; otherwise ``incoming`` unchanged.
    """
    if incoming != SECRET_STR_MASK:
        return incoming
    stored = _unwrap_secret_value(current)
    return incoming if stored is None else stored


def _preserve_secrets_in_dict_payload(
    current: Any,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore masked secret values inside a ``dict[str, SecretStr|SecretBytes]`` payload.

    :param current: The stored mapping of secret wrappers (or ``None``).
    :param incoming: The PATCH mapping that may contain mask literals.
    :return: A copy of ``incoming`` with masked keys restored from ``current``.
    """
    result = dict(incoming)
    for key, value in result.items():
        if value != SECRET_STR_MASK:
            continue
        leaf_current = current.get(key) if isinstance(current, Mapping) else None
        stored = _unwrap_secret_value(leaf_current)
        if stored is not None:
            result[key] = stored
    return result


def _preserve_secrets_in_secret_sequence_payload(
    current: Any,
    incoming: Sequence[Any],
) -> list[Any]:
    """Restore masked secrets inside a secret-element collection payload.

    Pairing is positional against :func:`_stable_collection_items`: index ``i``
    of ``incoming`` is restored from index ``i`` of the stabilized ``current``
    when the submitted value equals :data:`app.core.settings_override.registry.SECRET_STR_MASK`. Unordered stored
    collections (``set``/``frozenset``) are sorted so GET/PATCH workers agree.

    :param current: The stored collection of secret wrappers (or ``None``).
    :param incoming: The PATCH sequence that may contain mask literals.
    :return: A list copy of ``incoming`` with masked elements restored.
    """
    current_items = _stable_collection_items(current)
    result: list[Any] = []
    for index, value in enumerate(incoming):
        if value != SECRET_STR_MASK:
            result.append(value)
            continue
        leaf = current_items[index] if index < len(current_items) else None
        stored = _unwrap_secret_value(leaf)
        result.append(value if stored is None else stored)
    return result


def _annotation_collection_element_model(annotation: Any) -> type[BaseModel] | None:
    """Return the ``BaseModel`` element type of a ``list``/``set``/``tuple`` annotation.

    Unwraps optional/union wrappers at the top level. Matches shapes such as
    ``set[SomeModel]`` or ``list[SomeModel]``; returns ``None`` for scalars,
    mappings, and collections whose element type is not a Pydantic model.

    :param annotation: The field annotation to inspect.
    :return: The element ``BaseModel`` subclass, or ``None``.
    """
    candidates: list[Any] = [annotation]
    origin = typing.get_origin(annotation)
    if origin in {Union, UnionType}:
        candidates = list(typing.get_args(annotation))
    collection_origins = {list, set, tuple, frozenset, Sequence}
    for candidate in candidates:
        if candidate is None or candidate is type(None):
            continue
        coll_origin = typing.get_origin(candidate)
        if coll_origin not in collection_origins:
            continue
        args = typing.get_args(candidate)
        if not args:
            continue
        element = args[0]
        element_candidates: list[Any] = [element]
        element_origin = typing.get_origin(element)
        if element_origin in {Union, UnionType}:
            element_candidates = list(typing.get_args(element))
        for element_type in element_candidates:
            if isinstance(element_type, type) and issubclass(element_type, BaseModel):
                return element_type
    return None


def _collection_discriminator_candidates(
    incoming: Mapping[str, Any],
    current_items: Sequence[Any],
    used: set[int],
) -> list[int]:
    """Return unused model indexes matching an optional type discriminator.

    :param incoming: One element of the PATCH collection payload.
    :param current_items: The live stored collection as a sequence.
    :param used: Indexes already paired with an earlier incoming element.
    :return: Candidate indexes; empty when none match the discriminator filter.
    """
    provider = incoming.get("PROVIDER") or incoming.get("provider")
    needle = str(provider).upper() if provider is not None else None
    candidates: list[int] = []
    for index, current in enumerate(current_items):
        if index in used or not isinstance(current, BaseModel):
            continue
        if needle is not None and needle not in current.__class__.__name__.upper():
            continue
        candidates.append(index)
    return candidates


def _pick_best_scored_index(
    incoming: Mapping[str, Any],
    current_items: Sequence[Any],
    candidates: Sequence[int],
    preferred_index: int,
) -> int:
    """Pick the candidate with the best value-identity score.

    Ties break on ``preferred_index`` when that slot is among the top scorers.

    :param incoming: One element of the PATCH collection payload.
    :param current_items: The live stored collection as a sequence.
    :param candidates: Unused model indexes to score.
    :param preferred_index: The incoming element's position (list order).
    :return: The winning index into ``current_items``.
    """
    best_score = _collection_item_value_score(incoming, current_items[candidates[0]])
    best_indices = [candidates[0]]
    for index in candidates[1:]:
        score = _collection_item_value_score(incoming, current_items[index])
        if score > best_score:
            best_score = score
            best_indices = [index]
        elif score == best_score:
            best_indices.append(index)
    if preferred_index in best_indices:
        return preferred_index
    return best_indices[0]


def _match_by_field_name_overlap(
    incoming: Mapping[str, Any],
    current_items: Sequence[Any],
    unused: Sequence[int],
) -> int | None:
    """Return the unused model with the largest field-name overlap, if any.

    :param incoming: One element of the PATCH collection payload.
    :param current_items: The live stored collection as a sequence.
    :param unused: Indexes not yet paired.
    :return: The best-overlap index, or ``None``.
    """
    best_index: int | None = None
    best_overlap = 0
    incoming_keys = set(incoming)
    for index in unused:
        current = current_items[index]
        if not isinstance(current, BaseModel):
            continue
        overlap = len(set(type(current).model_fields) & incoming_keys)
        if overlap > best_overlap:
            best_overlap = overlap
            best_index = index
    return best_index if best_overlap > 0 else None


def _match_collection_item_index(
    incoming: Mapping[str, Any],
    current_items: Sequence[Any],
    used: set[int],
    preferred_index: int,
) -> int | None:
    """Return the index of the stored collection item that ``incoming`` updates.

    Matching order: optional type-discriminator (case-insensitive substring of
    the concrete class name) narrows candidates; among those, non-masked field
    value identity (:func:`_collection_item_value_score`) picks a winner;
    ties break on ``preferred_index`` when that slot is still a candidate
    (stable against :func:`_stable_collection_items` order); else the sole
    remaining unused item; else largest field-name overlap.

    :param incoming: One element of the PATCH collection payload.
    :param current_items: The live stored collection as a sequence.
    :param used: Indexes already paired with an earlier incoming element.
    :param preferred_index: The incoming element's position (list order).
    :return: The matched index into ``current_items``, or ``None``.
    """
    candidates = _collection_discriminator_candidates(incoming, current_items, used)
    if candidates:
        return _pick_best_scored_index(
            incoming, current_items, candidates, preferred_index
        )
    if preferred_index < len(current_items) and preferred_index not in used:
        return preferred_index
    unused = [index for index in range(len(current_items)) if index not in used]
    if len(unused) == 1:
        return unused[0]
    return _match_by_field_name_overlap(incoming, current_items, unused)


def _preserve_secrets_in_sequence_payload(
    current: Any,
    incoming: Sequence[Any],
) -> list[Any]:
    """Restore masked secrets inside a list/set-of-models PATCH payload.

    Pairs each mapping element with a live stored item via
    :func:`_match_collection_item_index` against
    :func:`_stable_collection_items`, then restores masks through
    :func:`preserve_secrets_in_model_payload` using the item's concrete
    runtime class (polymorphic bases often declare no secret fields of their
    own). Fingerprint mappings restore masked keys shallowly.

    :param current: The stored collection (``list``/``set``/``tuple``/
        ``frozenset``) or ``None``.
    :param incoming: The PATCH sequence that may contain mask literals.
    :return: A list copy of ``incoming`` with masked secrets restored.
    """
    current_items = _stable_collection_items(current)
    result: list[Any] = []
    used: set[int] = set()
    for index, item in enumerate(incoming):
        if not isinstance(item, Mapping):
            result.append(item)
            continue
        match_index = _match_collection_item_index(
            item, current_items, used, preferred_index=index
        )
        if match_index is None:
            result.append(dict(item))
            continue
        used.add(match_index)
        matched = current_items[match_index]
        if isinstance(matched, BaseModel):
            result.append(
                preserve_secrets_in_model_payload(type(matched), matched, item)
            )
            continue
        if isinstance(matched, Mapping):
            restored = dict(item)
            for key, value in restored.items():
                if value != SECRET_STR_MASK:
                    continue
                stored = _unwrap_secret_value(matched.get(key))
                if stored is not None:
                    restored[key] = stored
            result.append(restored)
            continue
        result.append(dict(item))
    return result


def preserve_secrets_in_model_payload(
    model_cls: type[BaseModel],
    current: Any,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore masked SecretStr/SecretBytes values inside a nested-model PATCH payload.

    Recurses into nested models, secret-valued dicts, secret-element sequences,
    and homogeneous list/set-of-model fields; scalar secret leaves use
    :func:`_preserve_masked_secret_scalar`.

    :param model_cls: The Pydantic model whose fields ``incoming`` addresses.
    :param current: The live stored model or mapping fingerprint.
    :param incoming: The PATCH mapping for this model.
    :return: A copy of ``incoming`` with masked secrets restored from ``current``.
    """
    result = dict(incoming)
    for name, field_info in model_cls.model_fields.items():
        if name not in result:
            continue
        leaf_current = _read_mapping_or_model_attr(current, name)
        nested_cls = annotation_pydantic_class(field_info.annotation)
        if nested_cls and isinstance(result[name], Mapping):
            result[name] = preserve_secrets_in_model_payload(
                nested_cls, leaf_current, result[name]
            )
            continue
        if _annotation_is_secret_valued_dict(field_info.annotation) and isinstance(
            result[name], Mapping
        ):
            result[name] = _preserve_secrets_in_dict_payload(leaf_current, result[name])
            continue
        if _annotation_is_secret_valued_sequence(field_info.annotation) and isinstance(
            result[name], list | tuple
        ):
            result[name] = _preserve_secrets_in_secret_sequence_payload(
                leaf_current, result[name]
            )
            continue
        if _annotation_collection_element_model(field_info.annotation) is not None and (
            isinstance(result[name], list | tuple)
        ):
            result[name] = _preserve_secrets_in_sequence_payload(
                leaf_current, result[name]
            )
            continue
        if _field_contains_secret(field_info):
            result[name] = _preserve_masked_secret_scalar(leaf_current, result[name])
    return result


def preserve_patch_secret_value(
    field_info: FieldInfo,
    current: Any,
    incoming: Any,
) -> Any:
    """Restore masked SecretStr/SecretBytes values in a PATCH value before persist.

    When a client resubmits Pydantic's secret JSON mask
    (:data:`app.core.settings_override.registry.SECRET_STR_MASK`), replace it with the stored secret's plain value.
    Non-mask submissions are left unchanged. Recurses into nested Pydantic
    models, ``dict[str, SecretStr]`` / ``dict[str, SecretBytes]`` payloads,
    ``list[SecretStr]`` / ``set[SecretStr]``-style collections, and homogeneous
    ``list``/``set`` collections of models (including polymorphic bases whose
    secrets live only on concrete subclasses).

    :param field_info: The Pydantic field metadata for the target attribute.
    :param current: The effective stored value (model, mapping, or secret wrapper).
    :param incoming: The value submitted in the PATCH body.
    :return: ``incoming`` with any masked secrets restored from ``current``.
    """
    parent_cls = annotation_pydantic_class(field_info.annotation)
    if parent_cls and isinstance(incoming, Mapping):
        return preserve_secrets_in_model_payload(parent_cls, current, incoming)
    if _annotation_is_secret_valued_dict(field_info.annotation) and isinstance(
        incoming, Mapping
    ):
        return _preserve_secrets_in_dict_payload(current, incoming)
    if isinstance(incoming, list | tuple) and _annotation_is_secret_valued_sequence(
        field_info.annotation
    ):
        return _preserve_secrets_in_secret_sequence_payload(current, incoming)
    if isinstance(incoming, list | tuple) and (
        _annotation_collection_element_model(field_info.annotation) is not None
        or (
            isinstance(current, list | set | tuple | frozenset)
            and any(isinstance(item, BaseModel | Mapping) for item in current)
        )
    ):
        return _preserve_secrets_in_sequence_payload(current, incoming)
    if _field_contains_secret(field_info):
        return _preserve_masked_secret_scalar(current, incoming)
    return incoming
