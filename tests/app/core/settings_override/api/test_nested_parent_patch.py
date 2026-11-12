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

"""Test that a nested change is judged by the parent the next snapshot would build."""

from typing import Any, cast

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.exceptions import HTTPUnprocessableEntityException
from app.core.settings_override.api.models import SettingsPatch
from app.core.settings_override.api.routes import (
    _nested_parent_errors,
    _validate_patch_body,
)
from app.core.settings_override.models import setting_class_token
from app.core.settings_override.proxy import OverridableSettingsProxy
from tests.app.core.settings_override.conftest import (
    insert_override_row,
    WINDOW_HIGH,
    WINDOW_LOW,
    WindowSettings,
)


async def _patch(session: AsyncSession, body: dict[str, Any]) -> list[tuple[str, Any]]:
    """Validate ``body`` as a PATCH of :class:`WindowSettings`.

    :param session: The database session.
    :param body: The PATCH body.
    :return: The writes the PATCH would store.
    """
    return await _validate_patch_body(
        session=session,
        settings_cls=WindowSettings,
        proxy=OverridableSettingsProxy(WindowSettings),
        body=SettingsPatch(body),
    )


async def _store(session: AsyncSession, rows: dict[str, Any]) -> None:
    """Store ``rows`` as active overrides of :class:`WindowSettings`.

    :param session: The database session.
    :param rows: The override values, by key.
    """
    for key, value in rows.items():
        await insert_override_row(
            session,
            setting_class=setting_class_token(WindowSettings),
            key=key,
            value=value,
        )


async def _refusal(session: AsyncSession, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the 422 entries a PATCH of ``body`` is refused with.

    :param session: The database session.
    :param body: The PATCH body.
    :return: The refusal's entries.
    """
    with pytest.raises(HTTPUnprocessableEntityException) as exc_info:
        await _patch(session, body)
    return cast("list[dict[str, Any]]", exc_info.value.detail)


@pytest.mark.asyncio
class TestParentFieldValidators:
    """Group the checks of the merged parent against its own field."""

    async def test_a_leaf_breaking_the_parent_is_refused_at_that_leaf(
        self, session: AsyncSession
    ) -> None:
        """Refuse a leaf that leaves the parent invalid, naming the leaf's key.

        :param session: The database session.
        """
        entries = await _refusal(session, {WINDOW_LOW: 5})

        assert [entry["loc"] for entry in entries] == [["body", WINDOW_LOW]]
        assert "LOW must not exceed HIGH" in entries[0]["msg"]

    async def test_leaves_valid_together_are_accepted(
        self, session: AsyncSession
    ) -> None:
        """Accept two leaves that are only valid as a pair.

        :param session: The database session.
        """
        writes = await _patch(session, {WINDOW_LOW: 5, WINDOW_HIGH: 9})

        assert dict(writes) == {WINDOW_LOW: 5, WINDOW_HIGH: 9}

    async def test_a_leaf_merges_onto_the_stored_rows(
        self, session: AsyncSession
    ) -> None:
        """Judge a leaf against a sibling already stored, not the YAML/env value.

        :param session: The database session.
        """
        await _store(session, {WINDOW_HIGH: 9})

        assert await _patch(session, {WINDOW_LOW: 5})

    async def test_a_leaf_is_judged_with_the_leaves_the_snapshot_ignores(
        self, session: AsyncSession
    ) -> None:
        """Refuse a leaf that leaves an ignored sibling still breaking the parent.

        :param session: The database session.
        """
        await _store(session, {WINDOW_LOW: 5})

        entries = await _refusal(session, {WINDOW_HIGH: 3})

        assert [entry["loc"] for entry in entries] == [["body", WINDOW_HIGH]]
        assert await _patch(session, {WINDOW_HIGH: 9})

    async def test_a_whole_write_is_merged_with_the_stored_leaves(
        self, session: AsyncSession
    ) -> None:
        """Merge stored leaves over a whole object written beside a new leaf.

        :param session: The database session.
        """
        await _store(session, {WINDOW_HIGH: 3})

        entries = await _refusal(
            session, {"WINDOW": {"LOW": 1, "HIGH": 9}, WINDOW_LOW: 5}
        )

        assert ["body", WINDOW_LOW] in [entry["loc"] for entry in entries]

    async def test_a_whole_write_breaking_honoured_leaves_is_refused(
        self, session: AsyncSession
    ) -> None:
        """Refuse a whole object the stored leaves would no longer fit.

        :param session: The database session.
        """
        await _store(session, {WINDOW_LOW: 2})

        entries = await _refusal(session, {"WINDOW": {"LOW": 1, "HIGH": 1}})

        assert [entry["loc"] for entry in entries] == [["body", "WINDOW"]]

    async def test_a_whole_write_beside_ignored_leaves_is_accepted(
        self, session: AsyncSession
    ) -> None:
        """Accept a whole object while leaves that were already ignored stay so.

        :param session: The database session.
        """
        await _store(session, {WINDOW_LOW: 5})

        assert await _patch(session, {"WINDOW": {"LOW": 1, "HIGH": 3}})


@pytest.mark.asyncio
class TestRevertJudgedByParent:
    """Group the checks of a DELETE against the parent it would leave."""

    async def test_a_revert_breaking_honoured_leaves_is_refused(
        self, session: AsyncSession
    ) -> None:
        """Refuse reverting a leaf the remaining ones depend on.

        :param session: The database session.
        """
        await _store(session, {WINDOW_LOW: 5, WINDOW_HIGH: 9})

        entries = await _revert(session, WINDOW_HIGH)

        assert [entry["loc"] for entry in entries] == [["body", WINDOW_HIGH]]
        assert "LOW must not exceed HIGH" in entries[0]["msg"]

    async def test_a_revert_leaving_a_valid_parent_is_accepted(
        self, session: AsyncSession
    ) -> None:
        """Accept reverting a leaf the remaining ones do not depend on.

        :param session: The database session.
        """
        await _store(session, {WINDOW_LOW: 5, WINDOW_HIGH: 9})

        assert await _revert(session, WINDOW_LOW) == []

    async def test_a_revert_among_ignored_leaves_is_accepted(
        self, session: AsyncSession
    ) -> None:
        """Accept reverting a leaf of a parent the snapshot already ignores.

        :param session: The database session.
        """
        await _store(session, {WINDOW_LOW: 5, WINDOW_HIGH: 3})

        assert await _revert(session, WINDOW_HIGH) == []


async def _revert(session: AsyncSession, key: str) -> list[dict[str, Any]]:
    """Return the 422 entries a DELETE of ``key`` would be refused with.

    :param session: The database session.
    :param key: The key to revert.
    :return: The refusal's entries, empty when the DELETE may proceed.
    """
    return await _nested_parent_errors(
        session,
        WindowSettings,
        OverridableSettingsProxy(WindowSettings),
        clearing=key,
    )
