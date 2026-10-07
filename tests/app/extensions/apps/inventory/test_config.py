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

"""Test the Inventory app's collection settings."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.settings_override.registry import coerce_field_value
from app.extensions.apps.inventory.config import (
    inventory_app_settings,
    InventoryAppSettings,
)

DEFAULT_RETENTION_DAYS = 30


def test_the_default_retention_is_a_month() -> None:
    """Keep a tombstone for a month before it becomes eligible."""
    assert (
        timedelta(days=DEFAULT_RETENTION_DAYS)
        == inventory_app_settings.COLLECTION_RETENTION
    )


@pytest.mark.parametrize(
    "retention",
    [timedelta(0), timedelta(seconds=-1), timedelta(days=-30)],
)
def test_a_non_positive_retention_is_rejected(retention: timedelta) -> None:
    """Refuse a retention that would put the cutoff at or after the present.

    A non-positive retention collects every tombstone in a single pass, so the
    bound is a safety guard rather than a formality.
    """
    with pytest.raises(ValidationError):
        InventoryAppSettings(COLLECTION_RETENTION=retention)


def test_the_default_interval_seeds_no_schedule() -> None:
    """Default collection to off, since it deletes rows irreversibly."""
    assert InventoryAppSettings().COLLECTION_INTERVAL is None


@pytest.mark.parametrize("interval", ["1 days", "6 hours", "15 minutes"])
def test_a_manageable_interval_is_accepted(interval: str) -> None:
    """Accept every cadence the periodic-task write path also accepts.

    :param interval: The interval expression under test.
    """
    assert InventoryAppSettings(COLLECTION_INTERVAL=interval).COLLECTION_INTERVAL


@pytest.mark.parametrize("interval", ["20 seconds", "500 microseconds"])
def test_a_sub_minute_interval_is_rejected(interval: str) -> None:
    """Refuse a cadence the periodic-task write path would then refuse to edit.

    Rejecting it here rather than at first use is the point: a shorter period
    seeds a schedule that runs but that the operator can neither toggle nor
    edit, because the write path enforces a one-minute floor.

    :param interval: The interval expression under test.
    """
    with pytest.raises(ValidationError):
        InventoryAppSettings(COLLECTION_INTERVAL=interval)


def test_a_runtime_override_is_held_to_the_same_bound() -> None:
    """Re-check the bound on the override path, not only on YAML load.

    Override coercion re-checks annotated-type constraints but does not re-run
    field validators, which is why the bound is annotated onto the type.
    """
    field = InventoryAppSettings.model_fields["COLLECTION_INTERVAL"]

    assert coerce_field_value(field, "6 hours")
    with pytest.raises(ValidationError):
        coerce_field_value(field, "20 seconds")


@pytest.mark.parametrize("field", ["COLLECTION_BATCH_SIZE", "COLLECTION_MAX_BATCHES"])
def test_the_batch_bounds_reject_zero(field: str) -> None:
    """Refuse a zero batch bound — an unset interval is how you disable the job."""
    with pytest.raises(ValidationError):
        InventoryAppSettings(**{field: 0})


class TestIdentityLinkPinRetention:
    """Test the bound on how long a standing identity link pins its successor."""

    def test_the_default_holds_a_link_for_180_days(self) -> None:
        """Hold a reversible link's successor for 180 days before collection."""
        assert timedelta(days=180) == InventoryAppSettings().IDENTITY_LINK_PIN_RETENTION

    @pytest.mark.parametrize(
        "retention",
        [timedelta(0), timedelta(seconds=-1), timedelta(days=-180)],
    )
    def test_a_non_positive_retention_is_rejected(self, retention: timedelta) -> None:
        """Refuse a bound that would release every pinned tombstone in one pass.

        :param retention: The retention under test.
        """
        with pytest.raises(ValidationError):
            InventoryAppSettings(IDENTITY_LINK_PIN_RETENTION=retention)

    def test_a_runtime_override_is_accepted(self) -> None:
        """Coerce an override the way the YAML load reads the default."""
        field = InventoryAppSettings.model_fields["IDENTITY_LINK_PIN_RETENTION"]

        assert coerce_field_value(field, "90 days") == timedelta(days=90)

    def test_a_runtime_override_is_held_to_the_same_bound(self) -> None:
        """Re-check the positive bound on the override path, not only on YAML load."""
        field = InventoryAppSettings.model_fields["IDENTITY_LINK_PIN_RETENTION"]

        with pytest.raises(ValidationError):
            coerce_field_value(field, "0 days")

    def test_a_bound_shorter_than_collection_retention_is_accepted(self) -> None:
        """Treat the pin bound as an axis independent of ``COLLECTION_RETENTION``.

        The two are overridden separately at runtime, so a cross-field check
        could reject an operator's first override before the second lands.
        """
        settings = InventoryAppSettings(
            COLLECTION_RETENTION=timedelta(days=30),
            IDENTITY_LINK_PIN_RETENTION=timedelta(days=7),
        )

        assert timedelta(days=7) == settings.IDENTITY_LINK_PIN_RETENTION
