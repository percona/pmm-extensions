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

"""Define tests for the Grafana user and token-payload models."""

import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from aiohttp import ClientPayloadError
from fastapi import HTTPException, status
from itsdangerous import URLSafeTimedSerializer
from pydantic import ValidationError

from app.core.auth.exceptions import HTTPForbiddenException, HTTPUnauthorizedException
from app.core.auth.models import (
    OAuthToken,
    SessionExchangeTokenResponse,
    UserRole,
)
from app.core.auth.providers.grafana.models import (
    _find_org_user,
    _service_account_uuid,
    _TOKEN_SERIALIZER,
    _TokenType,
    GrafanaTokenPayload,
    GrafanaUser,
)
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider
from app.core.auth.providers.grafana.sdk import GrafanaException, GrafanaSDK
from app.core.config import settings
from app.core.exceptions import HTTPNotFoundException
from tests.app.conftest import (
    GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN,
    make_roleless_grafana_assertion,
)
from tests.app.factories import GrafanaUserFactory

_MODELS_LOGGER = "app.core.auth.providers.grafana.models"


class TestGrafanaUserIdentity:
    """Test the identity mapping and signed-assertion round-trip."""

    @pytest.mark.asyncio
    async def test_get_oauth_token_round_trips_to_user(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a minted assertion reverses back into the same user."""
        oauth = await GrafanaUser.get_oauth_token(username="alice", password="secret")
        assert isinstance(oauth, OAuthToken)
        assert oauth.token_type == "Bearer"
        assert oauth.access_token
        assert oauth.refresh_token

        user = await GrafanaUser.from_jwt(oauth.access_token)

        assert user.username == grafana_user_record["login"]
        assert user.email == grafana_user_record["email"]
        assert user.is_admin is False
        assert user.access_token == oauth.access_token
        grafana_mock.login.assert_awaited_once_with("alice", "secret")
        grafana_mock.get_current_user.assert_awaited_once()
        grafana_mock.get_current_user_orgs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_survives_the_assertion_round_trip(
        self, grafana_mock, grafana_user_record
    ):
        """Verify ``is_admin`` is carried through mint and verified on decode."""
        grafana_mock.get_current_user.return_value = {
            **grafana_user_record,
            "isGrafanaAdmin": True,
        }
        oauth = await GrafanaUser.get_oauth_token(username="root", password="secret")

        user = await GrafanaUser.from_jwt(oauth.access_token)

        assert user.is_admin is True

    def test_id_is_stable_and_distinct_per_grafana_id(self, grafana_user_record):
        """Verify the PMM Extensions id is deterministic per Grafana numeric id."""
        first = GrafanaUser._from_grafana_record(grafana_user_record, [])
        again = GrafanaUser._from_grafana_record(grafana_user_record, [])
        other = GrafanaUser._from_grafana_record(
            {**grafana_user_record, "id": grafana_user_record["id"] + 1}, []
        )

        assert first.id == again.id
        assert other.id != first.id


class TestGrafanaAmbientSession:
    """Test the ambient-session grant (``oauth_token_from_session``)."""

    @pytest.mark.asyncio
    async def test_valid_session_mints_pair_without_login(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a valid ambient session mints a pair, reusing the cookie (no login)."""
        oauth = await GrafanaUser.oauth_token_from_session("ambient-session")

        assert isinstance(oauth, OAuthToken)
        assert oauth.access_token
        assert oauth.refresh_token
        user = await GrafanaUser.from_jwt(oauth.access_token)
        assert user.username == grafana_user_record["login"]
        grafana_mock.get_current_user.assert_awaited_once_with("ambient-session")
        grafana_mock.get_current_user_orgs.assert_awaited_once_with("ambient-session")
        grafana_mock.login.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_survives(self, grafana_mock, grafana_user_record):
        """Verify an admin ambient session decodes to an admin access assertion."""
        grafana_mock.get_current_user.return_value = {
            **grafana_user_record,
            "isGrafanaAdmin": True,
        }

        oauth = await GrafanaUser.oauth_token_from_session("ambient-session")

        user = await GrafanaUser.from_jwt(oauth.access_token)
        assert user.is_admin is True

    @pytest.mark.asyncio
    async def test_rejected_session_returns_none(self, grafana_mock):
        """Verify a Grafana 401 (rejected session) returns ``None`` for a silent fallback."""
        grafana_mock.get_current_user.side_effect = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED
        )

        assert await GrafanaUser.oauth_token_from_session("stale") is None

    @pytest.mark.asyncio
    async def test_non_401_error_propagates(self, grafana_mock):
        """Verify a non-401 upstream error propagates instead of masking as no-session."""
        grafana_mock.get_current_user.side_effect = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY
        )

        with pytest.raises(HTTPException):
            await GrafanaUser.oauth_token_from_session("s")


class TestGrafanaUserSerialization:
    """Test the JSON serialization contract the React SPA depends on."""

    def test_serializes_with_camelcase_aliases(self):
        """Verify ``by_alias`` dumps camelCase keys the SPA reads (e.g. ``isAdmin``)."""
        user = GrafanaUserFactory.build(role=UserRole.ADMIN)

        dumped = user.model_dump(by_alias=True)

        for alias in ("isAdmin", "firstName", "lastName", "createdTime", "updatedTime"):
            assert alias in dumped
        assert dumped["isAdmin"] is True


class TestGrafanaAssertionRoleRoundTrip:
    """Verify the role a minted assertion decodes to when it comes back.

    The assertion carries the role as its own claim, so every role survives the
    round trip rather than collapsing onto the admin boundary. An assertion
    carrying no such claim predates the claim and is refused.
    """

    @pytest.mark.parametrize(
        ("role", "expected_admin"),
        [
            (UserRole.SUPER_ADMIN, True),
            (UserRole.ADMIN, True),
            (UserRole.EDITOR, False),
            (UserRole.VIEWER, False),
            (UserRole.NONE, False),
        ],
    )
    @pytest.mark.parametrize(
        "token_type", [_TokenType.ACCESS, _TokenType.REFRESH, _TokenType.EXCHANGE]
    )
    def test_every_role_round_trips_to_itself(
        self, grafana_mock, token_type, role, expected_admin
    ):
        """Verify every assertion type carries its role back unchanged."""
        minted = GrafanaUser._mint(GrafanaUserFactory.build(role=role), token_type)

        decoded = GrafanaUser.model_validate(minted, context={"token_type": token_type})

        assert decoded.role is role
        assert decoded.is_admin is expected_admin

    @pytest.mark.parametrize(
        "token_type", [_TokenType.ACCESS, _TokenType.REFRESH, _TokenType.EXCHANGE]
    )
    def test_an_assertion_minted_before_the_claim_is_refused(
        self, grafana_mock, token_type
    ):
        """Verify a payload carrying only the legacy claim set is refused.

        This is the shape every assertion in flight during the rollout has: no
        ``role`` key at all. Refusing it rather than rebuilding a role from
        ``is_admin`` is what keeps a degraded role out of every assertion
        re-minted from it.
        """
        legacy = _TOKEN_SERIALIZER.dumps(
            {
                "id": str(uuid4()),
                "username": "alice",
                "email": "",
                "is_admin": True,
                "typ": token_type,
            }
        )

        with pytest.raises(ValidationError):
            GrafanaUser.model_validate(legacy, context={"token_type": token_type})

    def test_the_shared_roleless_helper_signs_a_verifiable_assertion(self):
        """Verify the helper the API-boundary tests use is signed the real way.

        Those tests assert only that a legacy assertion yields a 401, which a
        signature mismatch would also produce. The key and the salt are single
        sourced, so neither can drift; what is left to check is that the helper
        builds an *equivalent* serializer. Loading its output with the module's
        own serializer pins that, and pins the absent claim those tests rest on.
        """
        payload = _TOKEN_SERIALIZER.loads(make_roleless_grafana_assertion("access"))

        assert "role" not in payload
        assert payload["typ"] == _TokenType.ACCESS

    def test_a_claim_naming_no_known_role_is_refused(self, grafana_mock):
        """Verify a claim outside the enum fails closed rather than defaulting."""
        payload = _TOKEN_SERIALIZER.dumps(
            {
                "id": str(uuid4()),
                "username": "alice",
                "email": "",
                "role": "wizard",
                "typ": _TokenType.ACCESS,
            }
        )

        with pytest.raises(ValidationError):
            GrafanaUser.model_validate(payload)

    def test_a_claim_spelled_as_the_member_name_is_accepted(self, grafana_mock):
        """Verify the field coercion reads a member name as well as its value.

        Only PMM Extensions' own signing key can produce a payload at all, so the wider
        acceptance is documented here rather than narrowed.
        """
        payload = _TOKEN_SERIALIZER.dumps(
            {
                "id": str(uuid4()),
                "username": "alice",
                "email": "",
                "role": "SUPER_ADMIN",
                "typ": _TokenType.ACCESS,
            }
        )

        assert GrafanaUser.model_validate(payload).role is UserRole.SUPER_ADMIN


class TestGrafanaUserFromJwt:
    """Test signature verification in ``from_jwt``."""

    @pytest.mark.asyncio
    async def test_rejects_tampered_token(self, grafana_mock):
        """Verify a non-decodable token raises ``ValidationError``."""
        with pytest.raises(ValidationError):
            await GrafanaUser.from_jwt("not-a-valid-signed-token")

    @pytest.mark.asyncio
    async def test_rejects_wrong_salt_token(self, grafana_mock):
        """Verify a token signed with a different salt raises ``ValidationError``."""
        forged = URLSafeTimedSerializer(
            settings.SECRET_KEY.get_secret_value(), salt="wrong-salt"
        ).dumps({"id": str(uuid4()), "username": "x", "email": "", "is_admin": True})

        with pytest.raises(ValidationError):
            await GrafanaUser.from_jwt(forged)

    @pytest.mark.asyncio
    async def test_rejects_expired_token(self, grafana_mock, mocker):
        """Verify an access assertion past its lifetime raises ``ValidationError``."""
        oauth = await GrafanaUser.get_oauth_token(username="alice", password="secret")
        mocker.patch.object(grafana_mock, "access_token_max_age", timedelta(seconds=-1))

        with pytest.raises(ValidationError):
            await GrafanaUser.from_jwt(oauth.access_token)

    @pytest.mark.asyncio
    async def test_rejects_refresh_token_as_bearer(self, grafana_mock):
        """Verify a refresh assertion cannot authenticate as a Bearer token."""
        oauth = await GrafanaUser.get_oauth_token(username="alice", password="secret")
        with pytest.raises(ValidationError):
            await GrafanaUser.from_jwt(oauth.refresh_token)


class TestGrafanaRoleDerivation:
    """Verify how the ordered role is derived from Grafana records and orgs.

    Every case pins ``is_admin`` alongside the role, so the boundary the admin
    gates read is asserted rather than implied.
    """

    @pytest.mark.parametrize(
        ("is_server_admin", "orgs", "expected_role", "expected_admin"),
        [
            (True, [], UserRole.SUPER_ADMIN, True),
            (True, [{"role": "Viewer"}], UserRole.SUPER_ADMIN, True),
            (False, [{"role": "Admin"}], UserRole.ADMIN, True),
            (False, [{"role": "Editor"}], UserRole.EDITOR, False),
            (False, [{"role": "Viewer"}], UserRole.VIEWER, False),
            (
                False,
                [{"role": "Viewer"}, {"role": "Editor"}, {"role": "Admin"}],
                UserRole.ADMIN,
                True,
            ),
            (
                False,
                [{"role": "Viewer"}, {"role": "Editor"}],
                UserRole.EDITOR,
                False,
            ),
            (False, [], UserRole.NONE, False),
            (False, [{"role": "Bogus"}], UserRole.NONE, False),
            (False, [{}], UserRole.NONE, False),
            (False, [{"role": "None"}], UserRole.NONE, False),
            (False, [{"role": "Bogus"}, {"role": "Editor"}], UserRole.EDITOR, False),
        ],
    )
    def test_user_record_maps_to_a_role(
        self,
        grafana_user_record,
        is_server_admin,
        orgs,
        expected_role,
        expected_admin,
    ):
        """Verify the server-admin flag outranks orgs, which flatten by rank."""
        record = {**grafana_user_record, "isGrafanaAdmin": is_server_admin}

        user = GrafanaUser._from_grafana_record(record, orgs)

        assert user.role is expected_role
        assert user.is_admin is expected_admin

    def test_absent_server_admin_flag_falls_through_to_orgs(self, grafana_user_record):
        """Verify an omitted ``isGrafanaAdmin`` is read as not a server admin."""
        record = {k: v for k, v in grafana_user_record.items() if k != "isGrafanaAdmin"}

        user = GrafanaUser._from_grafana_record(record, [{"role": "Editor"}])

        assert user.role is UserRole.EDITOR

    @pytest.mark.parametrize(
        ("org_role", "expected_role", "expected_admin"),
        [
            ("Admin", UserRole.ADMIN, True),
            ("Editor", UserRole.EDITOR, False),
            ("Viewer", UserRole.VIEWER, False),
            ("None", UserRole.NONE, False),
            ("Bogus", UserRole.NONE, False),
        ],
    )
    def test_org_user_record_maps_to_a_role(
        self, grafana_org_users, org_role, expected_role, expected_admin
    ):
        """Verify an org-users row maps its single role onto the ordered role."""
        record = {**grafana_org_users[0], "role": org_role}

        user = GrafanaUser._from_org_user_record(record)

        assert user.role is expected_role
        assert user.is_admin is expected_admin

    def test_org_user_record_without_a_role_is_lowest(self, grafana_org_users):
        """Verify a row carrying no role fails closed rather than defaulting up."""
        record = {k: v for k, v in grafana_org_users[0].items() if k != "role"}

        assert GrafanaUser._from_org_user_record(record).role is UserRole.NONE


def _org_row_for(
    record: dict[str, Any], role: str = "Viewer", **overrides: Any
) -> dict[str, Any]:
    """Return an ``/api/org/users`` row belonging to ``record``'s user.

    :param record: The looked-up user record the row must belong to.
    :param role: The Grafana org role the row grants.
    :param overrides: Row keys to override.
    :return: The org-users row.
    """
    return {"userId": record["id"], "login": record["login"], "role": role, **overrides}


class TestGrafanaUserLookup:
    """Test the programmatic ``get_user`` / ``get_users`` mappings."""

    @pytest.mark.asyncio
    async def test_get_user(self, grafana_mock, grafana_user_record):
        """Verify get_user maps a looked-up record to a ``GrafanaUser``."""
        user = await GrafanaUser.get_user(grafana_user_record["login"])
        assert isinstance(user, GrafanaUser)
        assert user.username == grafana_user_record["login"]
        grafana_mock.lookup_user.assert_awaited_once_with(grafana_user_record["login"])

    @pytest.mark.asyncio
    async def test_get_users(self, grafana_mock, grafana_org_users):
        """Verify get_users maps org-user records to ``GrafanaUser`` instances."""
        users = await GrafanaUser.get_users()
        assert len(users) == len(grafana_org_users)
        assert all(isinstance(user, GrafanaUser) for user in users)
        assert users[0].username == grafana_org_users[0]["login"]
        grafana_mock.get_org_users.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_users_empty(self, grafana_mock):
        """Verify an empty org-users list returns an empty list, not an error."""
        grafana_mock.get_org_users.return_value = []
        assert await GrafanaUser.get_users() == []

    @pytest.mark.asyncio
    async def test_get_users_rejects_a_malformed_record(self, grafana_mock):
        """Verify a record breaking the listing contract reports an upstream error."""
        grafana_mock.get_org_users.return_value = [{"userId": 1, "role": "Viewer"}]
        with pytest.raises(GrafanaException) as exc_info:
            await GrafanaUser.get_users()
        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    @pytest.mark.parametrize(
        ("org_role", "expected_role", "expected_admin"),
        [
            ("Admin", UserRole.ADMIN, True),
            ("Editor", UserRole.EDITOR, False),
            ("Viewer", UserRole.VIEWER, False),
            ("None", UserRole.NONE, False),
            ("Bogus", UserRole.NONE, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_get_user_resolves_the_org_role(
        self,
        grafana_mock,
        grafana_user_record,
        org_role,
        expected_role,
        expected_admin,
    ):
        """Verify a non-server-admin target ranks by its own org membership."""
        grafana_mock.get_org_users.return_value = [
            _org_row_for(grafana_user_record, org_role)
        ]

        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert user.role is expected_role
        assert user.is_admin is expected_admin

    @pytest.mark.asyncio
    async def test_get_user_org_row_without_a_role_is_lowest(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a matched row carrying no role fails closed."""
        row = _org_row_for(grafana_user_record)
        grafana_mock.get_org_users.return_value = [
            {k: v for k, v in row.items() if k != "role"}
        ]

        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert user.role is UserRole.NONE

    @pytest.mark.parametrize("org_role", ["Viewer", "Admin"])
    @pytest.mark.asyncio
    async def test_get_user_never_downgrades_a_server_admin(
        self, grafana_mock, grafana_user_record, org_role
    ):
        """Verify the server-admin flag ranks highest without reading orgs."""
        grafana_mock.lookup_user.return_value = {
            **grafana_user_record,
            "isGrafanaAdmin": True,
        }
        grafana_mock.get_org_users.return_value = [
            _org_row_for(grafana_user_record, org_role)
        ]

        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert user.role is UserRole.SUPER_ADMIN
        grafana_mock.get_org_users.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_user_ignores_another_users_membership(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a stranger's row grants the target nothing."""
        grafana_mock.get_org_users.return_value = [
            {
                "userId": grafana_user_record["id"] + 1,
                "login": f"other-{grafana_user_record['login']}",
                "role": "Admin",
            }
        ]

        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert user.role is UserRole.NONE
        assert user.is_admin is False

    @pytest.mark.asyncio
    async def test_get_user_absent_from_an_empty_listing_is_lowest(
        self, grafana_mock, grafana_user_record
    ):
        """Verify an empty org-users listing leaves the target at the lowest role."""
        grafana_mock.get_org_users.return_value = []

        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert user.role is UserRole.NONE

    @pytest.mark.asyncio
    async def test_get_user_matches_on_the_numeric_id_not_the_login(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a differently-cased login in either payload still matches."""
        grafana_mock.get_org_users.return_value = [
            _org_row_for(
                grafana_user_record,
                "Editor",
                login=grafana_user_record["login"].upper(),
            )
        ]

        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert user.role is UserRole.EDITOR

    @pytest.mark.asyncio
    async def test_get_user_org_row_missing_a_user_id_fails_closed(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a row missing ``userId`` reads as upstream, not as a downgrade."""
        grafana_mock.get_org_users.return_value = [
            {"login": grafana_user_record["login"], "role": "Admin"}
        ]

        with pytest.raises(GrafanaException) as exc_info:
            await GrafanaUser.get_user(grafana_user_record["login"])

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    @pytest.mark.asyncio
    async def test_get_user_propagates_an_org_listing_failure(
        self, grafana_mock, grafana_user_record
    ):
        """Verify an unreachable listing fails loudly instead of under-reporting."""
        grafana_mock.get_org_users.side_effect = GrafanaException(
            detail="Cannot connect to Grafana"
        )

        with pytest.raises(GrafanaException):
            await GrafanaUser.get_user(grafana_user_record["login"])

    @pytest.mark.asyncio
    async def test_get_user_agrees_with_get_users(
        self, grafana_mock, grafana_user_record
    ):
        """Verify both read paths report the same role for the same user."""
        grafana_mock.get_org_users.return_value = [
            _org_row_for(grafana_user_record, "Editor")
        ]

        looked_up = await GrafanaUser.get_user(grafana_user_record["login"])
        listed = await GrafanaUser.get_users()

        assert looked_up.role is listed[0].role
        assert looked_up.id == listed[0].id


class TestGrafanaUserEdgeCases:
    """Test edge cases in record mapping."""

    def test_missing_email_defaults_to_empty(self, grafana_user_record):
        """Verify an absent email maps to an empty string."""
        record = {k: v for k, v in grafana_user_record.items() if k != "email"}
        assert GrafanaUser._from_grafana_record(record, []).email == ""

    def test_record_missing_id_fails_closed(self, grafana_user_record):
        """Verify a record without a numeric id propagates a ``KeyError``."""
        record = {k: v for k, v in grafana_user_record.items() if k != "id"}
        with pytest.raises(KeyError):
            GrafanaUser._from_grafana_record(record, [])

    def test_accepts_non_email_login(self):
        """Verify a non-email address such as ``admin@localhost`` validates."""
        user = GrafanaUserFactory.build(email="admin@localhost")
        assert user.email == "admin@localhost"


class TestGrafanaUnsupportedGrants:
    """Test that unsupported auth flows fail loudly with ``GrafanaException``."""

    @pytest.mark.asyncio
    async def test_get_oauth_token_with_code(self):
        """Verify the authorization-code grant is unsupported."""
        with pytest.raises(GrafanaException):
            await GrafanaUser.get_oauth_token(code="some-code")

    @pytest.mark.asyncio
    async def test_from_token_payload(self):
        """Verify from_token_payload is unsupported."""
        with pytest.raises(GrafanaException):
            await GrafanaUser.from_token_payload(None)

    @pytest.mark.asyncio
    async def test_token_payload_from_jwt(self):
        """Verify ``GrafanaTokenPayload.from_jwt`` is unsupported."""
        with pytest.raises(GrafanaException):
            await GrafanaTokenPayload.from_jwt("anything")


class TestGrafanaRefreshGrant:
    """Test the SPA refresh grant (PMM Extensions minted refresh assertions)."""

    @pytest.mark.asyncio
    async def test_refresh_remints_a_rotated_pair(
        self, grafana_mock, grafana_user_record, grafana_user_orgs
    ):
        """Verify the refresh grant re-mints a usable pair without calling Grafana.

        The re-mint reads the role off the presented assertion rather than
        Grafana, so an Editor must stay an Editor across the rotation.
        """
        grafana_mock.get_current_user_orgs.return_value = [
            {**grafana_user_orgs[0], "role": "Editor"}
        ]
        login = await GrafanaUser.get_oauth_token(username="alice", password="secret")

        refreshed = await GrafanaUser.get_oauth_token(refresh_token=login.refresh_token)

        assert refreshed.access_token
        assert refreshed.refresh_token
        user = await GrafanaUser.from_jwt(refreshed.access_token)
        assert user.username == grafana_user_record["login"]
        assert user.role is UserRole.EDITOR
        grafana_mock.login.assert_awaited_once()
        grafana_mock.get_current_user.assert_awaited_once()
        grafana_mock.get_current_user_orgs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_rejects_an_access_token(self, grafana_mock):
        """Verify an access assertion cannot be replayed at the refresh grant."""
        login = await GrafanaUser.get_oauth_token(username="alice", password="secret")
        with pytest.raises(ValidationError):
            await GrafanaUser.get_oauth_token(refresh_token=login.access_token)

    @pytest.mark.asyncio
    async def test_refresh_rejects_expired_refresh(self, grafana_mock, mocker):
        """Verify an expired refresh assertion raises ``ValidationError``."""
        login = await GrafanaUser.get_oauth_token(username="alice", password="secret")
        mocker.patch.object(
            grafana_mock, "refresh_token_max_age", timedelta(seconds=-1)
        )
        with pytest.raises(ValidationError):
            await GrafanaUser.get_oauth_token(refresh_token=login.refresh_token)


class TestGrafanaInvalidation:
    """Test that token invalidation is a safe no-op for Grafana."""

    @pytest.mark.asyncio
    async def test_invalidate_oauth_token_is_noop(self):
        """Verify invalidate_oauth_token returns None without raising."""
        assert await GrafanaUser.invalidate_oauth_token("token") is None


class TestGrafanaSessionExchange:
    """Verify the session-exchange grant (``exchange_token_from_session``)."""

    @pytest.mark.asyncio
    async def test_valid_session_mints_an_exchange_assertion(
        self, grafana_mock, grafana_user_record
    ):
        """Verify a valid ambient session mints an exchange assertion and its TTL."""
        exchange = await GrafanaUser.exchange_token_from_session("ambient-session")

        assert isinstance(exchange, SessionExchangeTokenResponse)
        assert exchange.access_token
        assert exchange.expires_in == grafana_mock.exchange_token_max_age
        user = await GrafanaUser.from_bearer(exchange.access_token)
        assert user.username == grafana_user_record["login"]
        grafana_mock.get_current_user.assert_awaited_once_with("ambient-session")
        grafana_mock.login.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claim_set_is_pinned(self, grafana_mock):
        """Verify the exchange assertion carries no claim beyond the identity set.

        Guards against a later change smuggling Grafana session material or a
        service-account credential into the minted payload.
        """
        exchange = await GrafanaUser.exchange_token_from_session("ambient-session")

        payload = _TOKEN_SERIALIZER.loads(exchange.access_token)

        assert set(payload) == {"id", "username", "email", "role", "is_admin", "typ"}
        assert payload["typ"] == "exchange"
        assert payload["role"] == "viewer"

    @pytest.mark.asyncio
    async def test_rejected_session_returns_none(self, grafana_mock):
        """Verify a Grafana 401 (rejected session) returns ``None``."""
        grafana_mock.get_current_user.side_effect = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED
        )

        assert await GrafanaUser.exchange_token_from_session("stale") is None

    @pytest.mark.asyncio
    async def test_non_401_error_propagates(self, grafana_mock):
        """Verify a non-401 upstream error propagates instead of masking as no-session."""
        grafana_mock.get_current_user.side_effect = GrafanaException()

        with pytest.raises(HTTPException):
            await GrafanaUser.exchange_token_from_session("s")

    @pytest.mark.asyncio
    async def test_admin_survives(self, grafana_mock, grafana_user_record):
        """Verify an admin ambient session decodes to an admin exchange assertion."""
        grafana_mock.get_current_user.return_value = {
            **grafana_user_record,
            "isGrafanaAdmin": True,
        }

        exchange = await GrafanaUser.exchange_token_from_session("ambient-session")

        user = await GrafanaUser.from_bearer(exchange.access_token)
        assert user.is_admin is True


class TestGrafanaUserFromBearer:
    """Verify the Bearer-surface accepted-type set (``from_bearer``)."""

    @staticmethod
    async def _exchange_token(session: str = "ambient-session") -> str:
        """Mint an exchange assertion and return its token string."""
        exchange = await GrafanaUser.exchange_token_from_session(session)
        return exchange.access_token

    @pytest.mark.asyncio
    async def test_accepts_an_access_assertion(self, grafana_mock, grafana_user_record):
        """Verify the common Bearer credential still authenticates."""
        oauth = await GrafanaUser.get_oauth_token(username="alice", password="secret")

        user = await GrafanaUser.from_bearer(oauth.access_token)

        assert user.username == grafana_user_record["login"]
        assert user.access_token == oauth.access_token

    @pytest.mark.asyncio
    async def test_accepts_an_exchange_assertion(
        self, grafana_mock, grafana_user_record
    ):
        """Verify an exchange assertion authenticates on the Bearer surface."""
        token = await self._exchange_token()

        user = await GrafanaUser.from_bearer(token)

        assert user.username == grafana_user_record["login"]
        assert user.access_token == token

    @pytest.mark.asyncio
    async def test_rejects_a_refresh_assertion(self, grafana_mock):
        """Verify a refresh assertion is refused on the Bearer surface."""
        oauth = await GrafanaUser.get_oauth_token(username="alice", password="secret")

        with pytest.raises(ValidationError):
            await GrafanaUser.from_bearer(oauth.refresh_token)

    @pytest.mark.asyncio
    async def test_rejects_garbage(self, grafana_mock):
        """Verify a non-decodable credential is refused."""
        with pytest.raises(ValidationError):
            await GrafanaUser.from_bearer("not-a-valid-signed-token")

    @pytest.mark.asyncio
    async def test_rejects_an_empty_credential(self, grafana_mock):
        """Verify an empty credential is refused rather than decoded."""
        with pytest.raises(ValidationError):
            await GrafanaUser.from_bearer("")

    @pytest.mark.asyncio
    async def test_exchange_expiry_is_enforced_against_its_own_lifetime(
        self, grafana_mock, mocker
    ):
        """Verify an exchange assertion past *its* lifetime is refused.

        The access lifetime stays at its default, so an implementation that
        widened the accepted set without trying each type against its own
        ``max_age`` would silently accept this token for the full access hour.
        """
        token = await self._exchange_token()
        mocker.patch.object(
            grafana_mock, "exchange_token_max_age", timedelta(seconds=-1)
        )

        with pytest.raises(ValidationError):
            await GrafanaUser.from_bearer(token)

    @pytest.mark.asyncio
    async def test_rejects_an_expired_access_assertion(self, grafana_mock, mocker):
        """Verify an expired access assertion is not rescued by the exchange attempt."""
        oauth = await GrafanaUser.get_oauth_token(username="alice", password="secret")
        mocker.patch.object(grafana_mock, "access_token_max_age", timedelta(seconds=-1))

        with pytest.raises(ValidationError):
            await GrafanaUser.from_bearer(oauth.access_token)


class TestGrafanaExchangeTokenTypeIsolation:
    """Verify an exchange assertion is refused everywhere but the Bearer surface."""

    @pytest.mark.asyncio
    async def test_from_jwt_rejects_an_exchange_assertion(self, grafana_mock):
        """Verify the cookie/session surface refuses an exchange assertion."""
        exchange = await GrafanaUser.exchange_token_from_session("ambient-session")

        with pytest.raises(ValidationError):
            await GrafanaUser.from_jwt(exchange.access_token)

    @pytest.mark.asyncio
    async def test_refresh_grant_rejects_an_exchange_assertion(self, grafana_mock):
        """Verify an exchange assertion cannot be replayed at the refresh grant."""
        exchange = await GrafanaUser.exchange_token_from_session("ambient-session")

        with pytest.raises(ValidationError):
            await GrafanaUser.get_oauth_token(refresh_token=exchange.access_token)


class TestGrafanaOrgScopedUserLookup:
    """Test the single-user lookup under both service-account scopes.

    A service account scoped to one Grafana org cannot call the server-admin
    user-lookup endpoint, so the lookup falls back to the org-users listing the
    same token already reads for the listing route.
    """

    upstream_detail: str = (
        "You'll need additional permissions to perform this action. "
        "Permissions needed: users:read"
    )

    def _refuse_lookup(
        self, grafana_mock: GrafanaAuthProvider, error: HTTPException | None = None
    ) -> None:
        """Make the server-admin lookup fail the way an org-scoped token does.

        :param grafana_mock: The mocked active Grafana provider.
        :param error: The refusal to raise. Defaults to the bare ``HTTPException``
            an unmapped upstream 403 surfaces as.
        """
        grafana_mock.lookup_user.side_effect = error or HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=self.upstream_detail
        )

    @pytest.mark.asyncio
    async def test_server_admin_scope_resolves_from_the_lookup_record(
        self, grafana_mock, grafana_user_record, grafana_org_users
    ):
        """Verify a successful lookup identifies the user from its own record.

        The listing carries a row under the same login but a different user, so
        an identity resolved through the fallback would name that row's email.
        """
        user = await GrafanaUser.get_user(grafana_user_record["login"])

        assert (
            user.email == grafana_user_record["email"] != grafana_org_users[0]["email"]
        )
        grafana_mock.lookup_user.assert_awaited_once_with(grafana_user_record["login"])

    @pytest.mark.asyncio
    async def test_org_scope_resolves_through_the_org_listing(
        self, grafana_mock, grafana_org_users, valid_username
    ):
        """Verify a refused lookup resolves the record from the org listing."""
        self._refuse_lookup(grafana_mock)

        user = await GrafanaUser.get_user(valid_username)

        assert user.username == grafana_org_users[0]["login"]
        assert user.email == grafana_org_users[0]["email"]
        grafana_mock.get_org_users.assert_awaited_once()

    @pytest.mark.parametrize(
        ("org_role", "expected_role"),
        [
            ("Viewer", UserRole.VIEWER),
            ("Editor", UserRole.EDITOR),
            ("Admin", UserRole.ADMIN),
        ],
    )
    @pytest.mark.asyncio
    async def test_org_scope_carries_the_org_role(
        self, grafana_mock, grafana_org_users, valid_username, org_role, expected_role
    ):
        """Verify the fallback reports the real org role rather than the lowest."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = [
            {**grafana_org_users[0], "role": org_role}
        ]

        user = await GrafanaUser.get_user(valid_username)

        assert user.role is expected_role

    @pytest.mark.asyncio
    async def test_org_scope_never_reports_super_admin(
        self, grafana_mock, grafana_org_users, valid_username
    ):
        """Verify no org row can assert the server-admin rank through this path."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = [
            {**grafana_org_users[0], "role": "Admin", "isGrafanaAdmin": True}
        ]

        user = await GrafanaUser.get_user(valid_username)

        assert user.role is UserRole.ADMIN

    @pytest.mark.parametrize("org_role", ["None", None])
    @pytest.mark.asyncio
    async def test_org_scope_reports_a_roleless_membership_as_none(
        self, grafana_mock, grafana_org_users, valid_username, org_role, caplog
    ):
        """Verify a membership Grafana itself ranks as none resolves, silently."""
        self._refuse_lookup(grafana_mock)
        row = {**grafana_org_users[0]}
        if org_role is None:
            del row["role"]
        else:
            row["role"] = org_role
        grafana_mock.get_org_users.return_value = [row]

        with caplog.at_level(logging.WARNING, logger=_MODELS_LOGGER):
            user = await GrafanaUser.get_user(valid_username)

        assert user.role is UserRole.NONE
        assert not caplog.records

    @pytest.mark.parametrize("org_role", ["Superuser", ""])
    @pytest.mark.asyncio
    async def test_org_scope_ranks_an_unknown_role_lowest_and_warns(
        self, grafana_mock, grafana_org_users, valid_username, org_role, caplog
    ):
        """Verify an unrecognized role grants nothing and is reported as drift."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = [
            {**grafana_org_users[0], "role": org_role}
        ]

        with caplog.at_level(logging.WARNING, logger=_MODELS_LOGGER):
            user = await GrafanaUser.get_user(valid_username)

        assert user.role is UserRole.NONE
        assert repr(org_role) in caplog.text

    @pytest.mark.asyncio
    async def test_both_read_paths_rank_a_roleless_membership_alike(
        self, grafana_mock, grafana_org_users, valid_username
    ):
        """Verify the single lookup and the listing never disagree on one row."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = [
            {**grafana_org_users[0], "role": "None"}
        ]

        single = await GrafanaUser.get_user(valid_username)
        listed = await GrafanaUser.get_users()

        assert single.role is listed[0].role is UserRole.NONE

    @pytest.mark.asyncio
    async def test_org_scope_resolves_by_email(
        self, grafana_mock, grafana_org_users, valid_username
    ):
        """Verify the fallback accepts an email, as the lookup endpoint does."""
        self._refuse_lookup(grafana_mock)

        user = await GrafanaUser.get_user(grafana_org_users[0]["email"])

        assert user.username == valid_username

    @pytest.mark.asyncio
    async def test_org_scope_matches_case_insensitively(
        self, grafana_mock, valid_username
    ):
        """Verify a differently-cased login resolves, as Grafana's own logins do."""
        self._refuse_lookup(grafana_mock)

        user = await GrafanaUser.get_user(valid_username.upper())

        assert user.username == valid_username

    @pytest.mark.asyncio
    async def test_org_scope_prefers_a_login_match_over_an_email_match(
        self, grafana_mock
    ):
        """Verify an email-shaped login resolves its own owner, not the email's."""
        wanted = "shared@example.com"
        grafana_mock.get_org_users.return_value = [
            {"userId": 1, "login": "bob", "email": wanted, "role": "Admin"},
            {
                "userId": 2,
                "login": wanted,
                "email": "alice@example.com",
                "role": "Viewer",
            },
        ]
        self._refuse_lookup(grafana_mock)

        user = await GrafanaUser.get_user(wanted)

        assert user.username == wanted

    @pytest.mark.asyncio
    async def test_org_scope_miss_is_not_found(self, grafana_mock):
        """Verify an absent user reads as not found, carrying no upstream detail."""
        self._refuse_lookup(grafana_mock)

        with pytest.raises(HTTPNotFoundException) as exc_info:
            await GrafanaUser.get_user("nobody")

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "users:read" not in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_org_scope_miss_on_an_empty_listing_is_not_found(self, grafana_mock):
        """Verify an empty listing raises rather than failing on an empty match."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = []

        with pytest.raises(HTTPNotFoundException):
            await GrafanaUser.get_user("nobody")

    def test_a_row_without_an_email_is_not_matched(self, grafana_org_users):
        """Verify an absent email is skipped rather than compared as empty.

        The match is exercised directly: its parameter is a plain ``str``, while
        every caller declares ``NonEmptyStr``, so a blank input is a value only
        this contract admits.
        """
        row = {k: v for k, v in grafana_org_users[0].items() if k != "email"}

        assert _find_org_user([row], "") is None

    @pytest.mark.parametrize(
        "error",
        [
            HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token"),
            HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="absent"),
            HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR),
            GrafanaException(),
        ],
    )
    @pytest.mark.asyncio
    async def test_only_a_refused_lookup_falls_back(
        self, grafana_mock, valid_username, error
    ):
        """Verify a bad credential, an absent user, and an outage all propagate."""
        grafana_mock.lookup_user.side_effect = error

        with pytest.raises(HTTPException) as exc_info:
            await GrafanaUser.get_user(valid_username)

        assert exc_info.value.status_code == error.status_code
        grafana_mock.get_org_users.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_typed_refusal_also_falls_back(self, grafana_mock, valid_username):
        """Verify the fallback keys on the status, not the exception class."""
        self._refuse_lookup(grafana_mock, HTTPForbiddenException())

        user = await GrafanaUser.get_user(valid_username)

        assert user.username == valid_username

    @pytest.mark.asyncio
    async def test_a_refused_org_listing_propagates(self, grafana_mock, valid_username):
        """Verify a token that reads neither surface reports the refusal itself."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.side_effect = HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="no org access"
        )

        with pytest.raises(HTTPException) as exc_info:
            await GrafanaUser.get_user(valid_username)

        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert not isinstance(exc_info.value, HTTPNotFoundException)

    @pytest.mark.asyncio
    async def test_org_scope_accepts_a_null_email(
        self, grafana_mock, grafana_org_users, valid_username
    ):
        """Verify a null email maps to an empty string rather than being refused."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = [
            {**grafana_org_users[0], "email": None}
        ]

        user = await GrafanaUser.get_user(valid_username)

        assert user.email == ""

    @pytest.mark.parametrize(
        "payload",
        [
            {"users": []},
            ["bob"],
            [{"userId": 1, "role": "Viewer"}],
            [{"userId": 1, "login": 7, "role": "Viewer"}],
            [{"userId": 1, "login": "bob", "email": 9, "role": "Viewer"}],
            [{"login": "bob", "email": "bob@example.com", "role": "Viewer"}],
            [{"userId": 1, "login": "bob", "role": ["Admin"]}],
            [{"userId": True, "login": "bob", "role": "Viewer"}],
        ],
    )
    @pytest.mark.asyncio
    async def test_org_scope_rejects_a_malformed_listing(
        self, grafana_mock, valid_username, payload
    ):
        """Verify a listing breaking the record contract fails closed as upstream."""
        self._refuse_lookup(grafana_mock)
        grafana_mock.get_org_users.return_value = payload

        with pytest.raises(GrafanaException) as exc_info:
            await GrafanaUser.get_user(valid_username)

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
        assert "users:read" not in str(exc_info.value.detail)


_SA_TOKEN = GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN


def _sa_record(**overrides: Any) -> dict[str, Any]:
    """Build a Grafana ``/api/serviceaccounts/{id}`` record."""
    return {
        "id": 7,
        "login": "sa-1-ci-runner",
        "name": "ci-runner",
        "orgId": 1,
        "isDisabled": False,
        "role": "Editor",
        **overrides,
    }


@pytest.fixture
def verify_service_account(grafana_mock, mocker):
    """Stub Grafana's verdict on a service-account token (the SDK boundary)."""
    return mocker.patch.object(
        GrafanaSDK,
        "verify_service_account_token",
        new=mocker.AsyncMock(return_value=_sa_record()),
    )


class TestGrafanaServiceAccountBearer:
    """Verify a ``glsa_`` token authenticates as the service account behind it."""

    @pytest.mark.asyncio
    async def test_admits_the_service_account(self, verify_service_account):
        """Verify identity and org role come from the account record."""
        user = await GrafanaUser.from_bearer(_SA_TOKEN)

        assert user.username == "sa-1-ci-runner"
        assert user.email == ""
        assert user.role is UserRole.EDITOR
        assert user.is_admin is False
        assert user.id == _service_account_uuid(7)
        verify_service_account.assert_awaited_once_with(_SA_TOKEN)

    def test_identity_is_stable_and_disjoint_from_humans(self, grafana_user_record):
        """Verify SA ids are per account, rename-stable, and never a human's."""
        first = GrafanaUser._from_service_account_record(_sa_record(id=2))
        renamed = GrafanaUser._from_service_account_record(
            _sa_record(id=2, name="renamed")
        )
        other = GrafanaUser._from_service_account_record(_sa_record(id=4))
        human = GrafanaUser._from_grafana_record({**grafana_user_record, "id": 2}, [])

        assert first.id == renamed.id
        assert first.id != other.id
        assert first.id != human.id

    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            pytest.param(_sa_record(role="Viewer"), UserRole.VIEWER, id="viewer"),
            pytest.param(_sa_record(role="Editor"), UserRole.EDITOR, id="editor"),
            pytest.param(_sa_record(role="Admin"), UserRole.ADMIN, id="admin"),
            pytest.param(_sa_record(role=None), UserRole.NONE, id="null"),
            pytest.param(
                {k: v for k, v in _sa_record().items() if k != "role"},
                UserRole.NONE,
                id="absent",
            ),
            pytest.param(_sa_record(role="Owner"), UserRole.NONE, id="unknown"),
        ],
    )
    def test_role_follows_the_org_role(self, record, expected):
        """Verify the org role ranks through the provider policy, never super-admin."""
        user = GrafanaUser._from_service_account_record(record)

        assert user.role is expected
        assert user.role is not UserRole.SUPER_ADMIN

    def test_an_unknown_role_is_logged(self, caplog):
        """Verify schema drift in the role stays visible."""
        with caplog.at_level(logging.WARNING, logger=_MODELS_LOGGER):
            GrafanaUser._from_service_account_record(_sa_record(role="Owner"))

        assert "Owner" in caplog.text

    @pytest.mark.parametrize(
        "verdict",
        [
            pytest.param(None, id="refused"),
            pytest.param(_sa_record(isDisabled=True), id="disabled"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_refusal_is_unauthorized(self, verify_service_account, verdict):
        """Verify Grafana's no, or a disabled account, is a 401."""
        verify_service_account.return_value = verdict

        with pytest.raises(HTTPUnauthorizedException):
            await GrafanaUser.from_bearer(_SA_TOKEN)

    @pytest.mark.asyncio
    async def test_an_upstream_failure_is_a_bad_gateway(self, verify_service_account):
        """Verify an undecidable check propagates as 502, never 401."""
        verify_service_account.side_effect = GrafanaException()

        with pytest.raises(GrafanaException) as exc_info:
            await GrafanaUser.from_bearer(_SA_TOKEN)

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    @pytest.mark.parametrize(
        "token",
        [
            "glsa_",
            "glsa_short",
            f"{_SA_TOKEN[:-8]}{_SA_TOKEN[-8:].upper()}",
            f"{_SA_TOKEN}x",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_token_grafana_rejects_is_refused(
        self, verify_service_account, token, caplog
    ):
        """Verify any ``glsa_`` string is left to Grafana, whose 401 is a 401."""
        verify_service_account.return_value = None

        with caplog.at_level(logging.DEBUG), pytest.raises(HTTPUnauthorizedException):
            await GrafanaUser.from_bearer(token)

        verify_service_account.assert_awaited_once_with(token)
        assert token not in caplog.text

    @pytest.mark.asyncio
    async def test_other_bearers_keep_the_assertion_path(self, verify_service_account):
        """Verify a non-``glsa_`` bearer never reaches Grafana."""
        with pytest.raises(ValidationError):
            await GrafanaUser.from_bearer("not-a-valid-signed-token")

        verify_service_account.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forwards_a_minted_exchange_assertion(self, verify_service_account):
        """Verify downstream calls carry a PMM Extensions assertion, never the SA token."""
        user = await GrafanaUser.from_bearer(_SA_TOKEN)

        assert user.access_token != _SA_TOKEN
        assert "glsa_" not in user.access_token
        exchanged = GrafanaUser.model_validate(
            user.access_token, context={"token_type": _TokenType.EXCHANGE}
        )
        forwarded = await GrafanaUser.from_bearer(user.access_token)
        assert (exchanged.id, exchanged.role) == (user.id, user.role)
        assert (forwarded.id, forwarded.username, forwarded.role) == (
            user.id,
            user.username,
            user.role,
        )
        verify_service_account.assert_awaited_once()

    def test_a_rejected_bearer_is_not_echoed_in_the_error(self, grafana_mock):
        """Verify a validation error never renders the presented credential."""
        with pytest.raises(ValidationError) as exc_info:
            GrafanaUser.model_validate("bogus-bearer-xyz")

        rendered = str(exc_info.value)
        assert "value_error" in rendered
        assert "bogus-bearer-xyz" not in rendered


class TestGrafanaActors:
    """Verify ``get_actors`` names service accounts beside the org users."""

    @pytest.fixture
    def service_accounts(self, grafana_mock, mocker):
        """Stub the SDK's service-account listing."""
        return mocker.patch.object(
            GrafanaSDK,
            "get_service_accounts",
            new=mocker.AsyncMock(
                return_value=[
                    _sa_record(id=2, login="sa-1-ci", role="Admin"),
                    _sa_record(id=4, login="sa-1-old", isDisabled=True),
                ]
            ),
        )

    @pytest.mark.asyncio
    async def test_names_humans_and_service_accounts(
        self, service_accounts, grafana_org_users
    ):
        """Verify every account is named by its login, disabled ones included."""
        actors = await GrafanaUser.get_actors()

        by_id = {actor.id: actor.username for actor in actors}
        assert by_id == {
            **{
                user.id: user.username
                for user in [
                    GrafanaUser._from_org_user_record(row) for row in grafana_org_users
                ]
            },
            _service_account_uuid(2): "sa-1-ci",
            _service_account_uuid(4): "sa-1-old",
        }

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param({"side_effect": GrafanaException()}, id="grafana"),
            pytest.param({"side_effect": TimeoutError()}, id="timeout"),
            pytest.param({"side_effect": ClientPayloadError("cut")}, id="payload"),
            pytest.param(
                {"side_effect": json.JSONDecodeError("bad", "{", 0)}, id="decode"
            ),
            pytest.param({"return_value": ["x"]}, id="off-contract-row"),
            pytest.param({"return_value": [{"id": 2}]}, id="missing-login"),
            pytest.param({"return_value": [_sa_record(id=True)]}, id="bool-id"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_listing_failure_keeps_the_humans(
        self, service_accounts, grafana_org_users, failure, caplog
    ):
        """Verify the SA half degrades to humans-only with a warning."""
        service_accounts.configure_mock(**failure)

        with caplog.at_level(logging.WARNING, logger=_MODELS_LOGGER):
            actors = await GrafanaUser.get_actors()

        assert [actor.username for actor in actors] == [
            row["login"] for row in grafana_org_users
        ]
        assert caplog.records

    @pytest.mark.asyncio
    async def test_get_users_still_lists_humans_only(
        self, service_accounts, grafana_org_users
    ):
        """Verify ``GET /api/users``'s source is unchanged."""
        users = await GrafanaUser.get_users()

        assert [user.username for user in users] == [
            row["login"] for row in grafana_org_users
        ]
        service_accounts.assert_not_awaited()
