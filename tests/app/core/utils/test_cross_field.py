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


"""Unit tests for cross-field settings rules."""

import logging
import operator

import pytest

from app.core.utils import cross_field
from app.core.utils.cross_field import (
    CrossFieldRule,
    drop_broken_overrides,
    violated_rules,
)

LE = CrossFieldRule(("A", "B"), operator.le, "A must not exceed B")
LT = CrossFieldRule(("B", "C"), operator.lt, "B must be below C")
#: Shares ``B`` with ``LE``, so restoring ``B``'s base value can break it.
GT = CrossFieldRule(("D", "B"), operator.gt, "D must exceed B")

#: A consistent base every override test falls back to.
BASE = {"A": 1, "B": 10, "C": 20, "D": 30}


class TestViolatedRules:
    """Group the checks of :func:`violated_rules`."""

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ({"A": 1, "B": 2, "C": 3}, []),
            ({"A": 3, "B": 2, "C": 3}, [LE]),
            ({"A": 1, "B": 5, "C": 5}, [LT]),
            ({"A": 9, "B": 5, "C": 5}, [LE, LT]),
        ],
        ids=["all-hold", "first-broken", "second-broken", "both-broken"],
    )
    def test_only_the_broken_rules_are_returned_in_declaration_order(
        self, values: dict[str, int], expected: list[CrossFieldRule]
    ) -> None:
        """Return the failing subset, keeping the order the rules were declared in.

        :param values: The fields' values.
        :param expected: The rules those values break.
        """
        assert violated_rules((LE, LT), values.__getitem__) == expected

    def test_values_reach_the_predicate_in_field_order(self) -> None:
        """Pass values positionally, in the order the rule lists its fields."""
        seen: list[tuple[str, ...]] = []
        rule = CrossFieldRule(("X", "Y"), lambda *args: seen.append(args) or True, "")

        violated_rules((rule,), {"X": "x", "Y": "y"}.__getitem__)

        assert seen == [("x", "y")]

    def test_a_field_the_lookup_does_not_know_fails_loudly(self) -> None:
        """Surface a misspelt rule instead of letting it pass silently."""
        with pytest.raises(KeyError):
            violated_rules((LE,), {"A": 1}.__getitem__)

    def test_no_rules_is_inert(self) -> None:
        """Report nothing, and look nothing up, for a class without rules."""
        assert violated_rules((), {}.__getitem__) == []


class TestMessage:
    """Group the checks of :attr:`CrossFieldRule.message`."""

    def test_it_names_every_field_and_the_relationship(self) -> None:
        """Say which fields disagree and how."""
        assert LE.message == "A, B: A must not exceed B"

    def test_it_never_carries_a_value(self) -> None:
        """Stay static, so a secret field's value cannot leak through it."""
        rule = CrossFieldRule(("TOKEN", "B"), operator.eq, "TOKEN must match B")

        assert rule.message == "TOKEN, B: TOKEN must match B"


class TestDropBrokenOverrides:
    """Group the checks of :func:`drop_broken_overrides`."""

    def test_a_consistent_set_is_kept_whole(self) -> None:
        """Keep every override when the rules hold."""
        overrides = {"A": 5, "B": 15}

        assert drop_broken_overrides((LE, LT), overrides, BASE.__getitem__) == (
            overrides,
            [],
        )

    def test_both_halves_of_a_jointly_broken_pair_go(self) -> None:
        """Drop the whole pair, since which half was meant is unknowable."""
        kept, broken = drop_broken_overrides(
            (LE, LT), {"A": 9, "B": 5}, BASE.__getitem__
        )

        assert (kept, broken) == ({}, [LE])

    def test_a_lone_override_broken_against_the_base_goes_alone(self) -> None:
        """Drop only the override, since the base side cannot be dropped."""
        kept, _ = drop_broken_overrides((LE,), {"A": 11, "C": 7}, BASE.__getitem__)

        assert kept == {"C": 7}

    def test_restoring_a_base_value_cascades_to_the_rule_it_breaks(self) -> None:
        """Repeat until every rule holds, reporting each rule that broke."""
        kept, broken = drop_broken_overrides(
            (LE, GT), {"A": 9, "B": 5, "D": 8}, BASE.__getitem__
        )

        assert (kept, broken) == ({}, [LE, GT])

    def test_the_input_is_not_mutated(self) -> None:
        """Leave the caller's mapping alone, so it can diff before and after."""
        overrides = {"A": 9, "B": 5}

        drop_broken_overrides((LE,), overrides, BASE.__getitem__)

        assert overrides == {"A": 9, "B": 5}

    def test_a_broken_base_stops_instead_of_looping(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log and stop when no override is left to drop.

        :param caplog: Fixture capturing log records.
        """
        broken_base = {**BASE, "A": 99}

        with caplog.at_level(logging.ERROR, logger=cross_field.logger.name):
            kept, broken = drop_broken_overrides((LE,), {}, broken_base.__getitem__)

        assert (kept, broken) == ({}, [LE])
        assert "A, B: A must not exceed B" in caplog.text
