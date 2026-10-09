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

"""Define utilities for handling dictionaries."""

from collections.abc import Callable
from typing import Any

__all__ = [
    "deep_dict_update",
    "filter_dict",
    "merge_dict_at_start",
    "remove_falsy_values_from_dict",
    "sort_dict",
    "transform_dict_keys",
]


def deep_dict_update(main_dict: dict[Any, Any], update_dict: dict[Any, Any]) -> None:
    """Merge ``update_dict`` into ``main_dict`` recursively.

    Update ``main_dict`` with the contents of ``update_dict`` recursively. For each
    key in ``update_dict``, if the key exists in ``main_dict`` and both values are
    dictionaries, merge them recursively. If the key exists in ``main_dict`` and
    both values are lists, prepend the non-empty list from ``update_dict`` to the
    list in ``main_dict``, or replace it with an empty list when the overlay is
    empty. Otherwise, overwrite the value in ``main_dict`` with the value from
    ``update_dict``.

    When both arguments are the same object the merge is a no-op. This can
    happen when the selected YAML profile *is* the base profile, or when two
    distinct profiles share a nested mapping through a YAML anchor.

    :param main_dict: The dictionary to be updated.
    :param update_dict: The dictionary containing updates to apply.
    """
    if main_dict is update_dict:
        return
    for key, value in update_dict.items():
        if (
            key in main_dict
            and isinstance(main_dict[key], dict)
            and isinstance(value, dict)
        ):
            deep_dict_update(main_dict[key], value)
        elif (
            key in main_dict
            and isinstance(main_dict[key], list)
            and isinstance(value, list)
        ):
            main_dict[key] = value + main_dict[key] if value else []
        else:
            main_dict[key] = value


def transform_dict_keys(
    data: dict[Any, Any], transform: Callable[[Any], Any], *, deep: bool = False
) -> dict[Any, Any]:
    """Transform all keys in a dictionary using a specified function.

    Traverse the input dictionary and transform all keys using the specified `transform`
    function. If a value is a dictionary and `deep` is True, apply the conversion
    recursively.

    :param data: The dictionary whose keys are to be transformed.
    :type data: dict[Any, Any]
    :param transform: The transform function to use.
    :type transform: Callable[[Any], Any]
    :param deep: If `True`, apply the transform function recursively to all sub-dicts.
        Defaults to False.
    :type deep: bool
    :return: A new dictionary with all keys transformed.
    :rtype: dict[Any, Any]
    """
    transformed_dict = {}
    for key, value in data.items():
        new_value = (
            transform_dict_keys(value, transform, deep=deep)
            if deep and isinstance(value, dict)
            else value
        )
        transformed_dict[transform(key)] = new_value
    return transformed_dict


def sort_dict(unsorted_dict: dict, key: Callable[[Any], Any]) -> dict:
    """Sort a dictionary based on a specified key function.

    This function returns a new dictionary with its items sorted according to the
    provided key function. The sorting is performed on the dictionary's items, and the
    resulting dictionary maintains the sorted order.

    :param unsorted_dict: The dictionary to be sorted.
    :type unsorted_dict: dict
    :param key: A function that extracts a comparison key from each dictionary item.
    :type key: Callable[[Any], Any]
    :return: A new dictionary sorted by the specified key function.
    :rtype: dict
    """
    return dict(sorted(unsorted_dict.items(), key=key))


def filter_dict(
    data: dict[Any, Any], filter_func: Callable[[Any], bool]
) -> dict[Any, Any]:
    """Filter a dictionary based on a specified filter function.

    This function returns a new dictionary containing only the items for which the
    provided `filter_func` returns `True`. The filtering is performed on the
    dictionary's items.

    :param data: The dictionary to be filtered.
    :type data: dict[Any, Any]
    :param filter_func: A function that takes a dictionary item and returns a boolean
        indicating whether to include it in the result.
    :type filter_func: Callable[[Any], bool]
    :return: A new dictionary containing only the items that satisfy the filter
        function.
    :rtype: dict[Any, Any]
    """
    return {k: v for k, v in data.items() if filter_func(v)}


def remove_falsy_values_from_dict(data: dict[Any, Any]) -> dict[Any, Any]:
    """Remove all falsy values from a dictionary.

    This function returns a new dictionary containing only the items with truthy values.

    :param data: The dictionary to be filtered.
    :type data: dict[Any, Any]
    :return: A new dictionary with all falsy values removed.
    :rtype: dict[Any, Any]
    """
    return filter_dict(data, bool)


def merge_dict_at_start(
    main_dict: dict[Any, Any], prepend_dict: dict[Any, Any]
) -> dict[Any, Any]:
    """Prepend one dictionary to another.

    This function returns a new dictionary that contains all items from `prepend_dict`
    followed by all items from `main_dict`. If a key exists in both dictionaries, the
    value from `main_dict` is retained.

    :param main_dict: The main dictionary.
    :type main_dict: dict[Any, Any]
    :param prepend_dict: The dictionary to prepend.
    :type prepend_dict: dict[Any, Any]
    :return: A new dictionary with `prepend_dict` items followed by `main_dict` items.
    :rtype: dict[Any, Any]
    """
    new_dict = prepend_dict.copy()
    for key, value in main_dict.items():
        new_dict.setdefault(key, value)
    return new_dict
