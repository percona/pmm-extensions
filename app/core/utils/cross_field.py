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


"""Define relationships that several settings fields must satisfy together."""

__all__ = [
    "CrossFieldRule",
    "drop_broken_overrides",
    "dropped_override_causes",
    "violated_rules",
]

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CrossFieldRule:
    """Declare a relationship that a set of top-level fields must satisfy together.

    Declared on a settings class (``CROSS_FIELD_RULES``) so YAML/env loading and
    the per-field override path judge a combination by one shared definition.

    :param fields: The top-level field names the relationship spans.
    :param holds: Predicate called with the fields' values, positionally and in
        ``fields`` order; ``True`` when the combination is acceptable.
    :param relation: Human-readable statement of the relationship. Kept static
        so a message built from it can never echo a (possibly secret) value.
    """

    fields: tuple[str, ...]
    holds: Callable[..., bool]
    relation: str

    @property
    def message(self) -> str:
        """Return a message naming every field and the relationship between them.

        :return: ``"<field>, <field>: <relation>"``.
        """
        return f"{', '.join(self.fields)}: {self.relation}"


def violated_rules(
    rules: Iterable[CrossFieldRule], value_of: Callable[[str], Any]
) -> list[CrossFieldRule]:
    """Return the rules broken by the values ``value_of`` yields, in order.

    :param rules: The rules to check.
    :param value_of: Return a field's effective value given its name.
    :return: Each rule whose predicate is false.
    :raises AttributeError: Or whatever ``value_of`` raises for a field it does
        not know, so a misspelt rule fails loudly instead of passing silently.
    """
    return [
        rule
        for rule in rules
        if not rule.holds(*(value_of(name) for name in rule.fields))
    ]


def drop_broken_overrides(
    rules: Iterable[CrossFieldRule],
    overrides: Mapping[str, Any],
    base_value_of: Callable[[str], Any],
) -> tuple[dict[str, Any], list[CrossFieldRule]]:
    """Return the overrides left once every one taking part in a broken rule goes.

    Every overridden field of a broken rule goes, not just one, because which
    half of a jointly-bad pair the operator meant to keep is unknowable. Repeats
    until the rules hold, since restoring one field's base value can break
    another rule that field shares.

    :param rules: The rules to enforce.
    :param overrides: Field name to override value; not mutated.
    :param base_value_of: Return the value a field takes with no override.
    :return: The overrides kept, and every rule that was broken along the way.
    """
    kept, broken, _ = _drop_rounds(rules, overrides, base_value_of)
    return kept, broken


def dropped_override_causes(
    rules: Iterable[CrossFieldRule],
    overrides: Mapping[str, Any],
    base_value_of: Callable[[str], Any],
) -> dict[str, tuple[CrossFieldRule, ...]]:
    """Return the rules that dropped each override :func:`drop_broken_overrides` drops.

    A field maps only to the rules violated in the round that dropped it, never
    to a rule that broke later because the field fell back to its base value:
    that rule judged the base, not the operator's override.

    :param rules: The rules to enforce.
    :param overrides: Field name to override value; not mutated.
    :param base_value_of: Return the value a field takes with no override.
    :return: Each dropped field's name mapped to the rules that dropped it, in
        declaration order. A kept field is absent.
    """
    _, _, causes = _drop_rounds(rules, overrides, base_value_of)
    return causes


def _drop_rounds(
    rules: Iterable[CrossFieldRule],
    overrides: Mapping[str, Any],
    base_value_of: Callable[[str], Any],
) -> tuple[dict[str, Any], list[CrossFieldRule], dict[str, tuple[CrossFieldRule, ...]]]:
    """Drop overrides round by round until the rules hold, recording what dropped each.

    :param rules: The rules to enforce.
    :param overrides: Field name to override value; not mutated.
    :param base_value_of: Return the value a field takes with no override.
    :return: The overrides kept, every rule broken along the way, and each
        dropped field mapped to the rules violated in the round that dropped it.
    """
    rules = tuple(rules)
    kept = dict(overrides)
    broken: list[CrossFieldRule] = []
    causes: dict[str, tuple[CrossFieldRule, ...]] = {}

    def value_of(name: str) -> Any:
        return kept[name] if name in kept else base_value_of(name)

    while violated := violated_rules(rules, value_of):
        broken.extend(rule for rule in violated if rule not in broken)
        droppable = {name for rule in violated for name in rule.fields} & kept.keys()
        if not droppable:
            # Unreachable while the base passed its own load-time check; stop
            # rather than loop forever if it somehow did not.
            logger.error(
                "Base settings violate cross-field rules: %s",
                "; ".join(rule.message for rule in violated),
            )
            break
        for name in droppable:
            causes[name] = tuple(rule for rule in violated if name in rule.fields)
            del kept[name]
    return kept, broken, causes
