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

"""Define the Casdoor user and token-payload models."""

from typing import Annotated, Any, cast, Literal, Self

from pydantic import (
    AliasChoices,
    computed_field,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from app.core.auth.models import BaseTokenPayload, BaseUser, OAuthToken, UserRole
from app.core.auth.providers.casdoor.sdk import CasdoorSDK

CasdoorUsernameField = Annotated[
    str,
    Field(
        validation_alias=AliasChoices("username", "name"),
        pattern=r"^[a-zA-Z0-9]+(?:[_-][a-zA-Z0-9]+)*$",
    ),
]


def _active_casdoor_sdk() -> CasdoorSDK:
    """Return the live ``CasdoorSDK`` from the active auth provider.

    :return: The active provider, which is a ``CasdoorSDK`` while Casdoor is the
        selected provider.
    """
    from app.core.auth.config import (  # noqa: PLC0415 - auth.config imports this module via the provider bundle (cycle)
        get_active_auth_provider,
    )

    return cast("CasdoorSDK", get_active_auth_provider())


class CasdoorTokenPayload(BaseTokenPayload):
    """Represent the payload of a Casdoor JWT token.

    :param iss: The issuer of the token.
    :param sub: The subject or user identifier the token refers to.
    :param aud: The audience for whom the token is intended.
    :param exp: The expiration time of the token.
    :param nbf: The time before which the token must not be accepted for processing.
    :param jti: The JWT token identifier.
    :param username: The user's Casdoor username.
    :param active: Whether the token is active or not. Must be True.
    """

    username: CasdoorUsernameField
    active: Literal[True] = True

    @field_validator("iss")
    @classmethod
    def validate_iss(cls, v: str) -> str:
        """Validate that the token's issuer is allowed by the active provider.

        :param v: The issuer value to validate.
        :return: The validated issuer.
        """
        casdoor = _active_casdoor_sdk()
        if casdoor.allowed_issuers != "*" and v not in casdoor.allowed_issuers:
            raise ValueError(f"Unknown token issuer: {v}")
        return v

    @field_validator("aud")
    @classmethod
    def validate_aud(cls, v: list[str]) -> list[str]:
        """Validate that the active provider's client ID is in the token's audience.

        :param v: The audience list to validate.
        :return: The validated audience list.
        """
        if _active_casdoor_sdk().client_id.get_secret_value() not in v:
            raise ValueError(f"Client ID not part of audience: {v}")
        return v

    @classmethod
    async def from_jwt(cls, token: str) -> Self:
        """Decode a JWT token and return an instance of ``CasdoorTokenPayload``.

        Decode and validate a JWT access token and return an instance
        of ``CasdoorTokenPayload``.

        :param token: The JWT token to decode.
        :return: An instance of ``CasdoorTokenPayload`` populated with the decoded data.
        """
        return cls.model_validate(await _active_casdoor_sdk().introspect_token(token))


class CasdoorUser(BaseUser):
    """Represent a Casdoor user.

    :param id: The unique identifier of the user.
    :param email: The email address of the user.
    :param first_name: The first name of the user.
    :param last_name: The last name of the user.
    :param role: The user's access level, derived from Casdoor's admin flag when
        the payload carries no role of its own.
    :param created_time: The datetime when the user was created. Defaults to current
        datetime.
    :param updated_time: The datetime when the user was last updated. Defaults to
        current datetime.
    :param username: The user's Casdoor username.
    :param owner: The user's Casdoor organization.
    :param is_forbidden: Whether the user has the ``is_forbidden`` attribute set in
        Casdoor. Defaults to False.
    :param is_deleted: Whether the user has the ``is_deleted`` attribute set in Casdoor.
        Defaults to False.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    username: CasdoorUsernameField
    owner: str
    is_forbidden: bool = False
    is_deleted: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_role(cls, data: Any) -> Any:
        """Derive the ordered role from Casdoor's admin flag.

        Casdoor exposes no role concept, so the payload's admin boolean is the
        only signal. An unset flag resolves to ``VIEWER`` rather than the lowest
        role, keeping the read access a non-admin Casdoor user has today; a flag
        that is present is validated as a boolean rather than tested for
        truthiness, so a spelled-out ``"false"`` still resolves to ``VIEWER``.

        Both wire spellings are read because ``model_config`` accepts either for
        every field on this model: Casdoor's API sends ``isAdmin``, and
        ``populate_by_name`` admits the snake-case form.

        :param data: The raw model input.
        :return: The input with a ``role`` filled in, or ``data`` unchanged.
        :raises ValueError: If the admin flag is not a value Pydantic accepts as
            a boolean.
        """
        if not isinstance(data, dict) or "role" in data:
            return data
        for key in ("isAdmin", "is_admin"):
            if key in data:
                return {**data, "role": cls._role_from_admin_flag(data[key])}
        return {**data, "role": UserRole.VIEWER}

    @computed_field
    @property
    def is_active(self) -> bool:
        """Return True if the user is not forbidden nor deleted.

        :return: True if the user is active.
        """
        return not self.is_forbidden and not self.is_deleted

    @classmethod
    def build_service_principal(cls, **fields: Any) -> Self:
        """Build the Casdoor service principal, injecting the required ``owner``.

        :return: A ``CasdoorUser`` service principal with ``owner`` set to
            ``"built-in"``.
        """
        return super().build_service_principal(owner="built-in", **fields)

    @staticmethod
    async def get_oauth_token(
        code: str | None = None,
        username: str | None = None,
        password: str | None = None,
        refresh_token: str | None = None,
    ) -> OAuthToken:
        """Retrieve an OAuth token for the user.

        Get an OAuth token for the user from Casdoor's SDK. Either ``code``,
        ``refresh_token``, or ``username`` and ``password`` must be set.

        :param code: An authorization code used to obtain the token.
        :param username: The username of the user.
        :param password: The password of the user.
        :param refresh_token: A refresh token used to obtain a new access token.
        :return: The OAuth token for the user.
        """
        casdoor = _active_casdoor_sdk()
        if refresh_token:
            oauth_data = await casdoor.refresh_token_request(refresh_token)
        else:
            oauth_data = await casdoor.get_access_token(code, username, password)
        return OAuthToken(**oauth_data)

    @staticmethod
    async def invalidate_oauth_token(access_token: str) -> None:
        """Invalidate an OAuth token.

        Invalidate an OAuth token by refreshing the token.

        :param access_token: The access token to invalidate.
        """
        casdoor = _active_casdoor_sdk()
        token_payload = await CasdoorTokenPayload.from_jwt(access_token)
        token_data = await casdoor.get_token(token_payload.jti)
        await casdoor.delete_token(token_data)

    @classmethod
    async def get_user(cls, username: CasdoorUsernameField) -> Self:
        """Get user by username.

        :param username: The username of the user.
        :return: An instance of ``CasdoorUser``.
        """
        user_data = await _active_casdoor_sdk().get_user(username)
        return cls(**user_data)

    @classmethod
    async def get_users(cls) -> list[Self]:
        """Get user list.

        :return: The list of users.
        """
        users_data = await _active_casdoor_sdk().get_users()
        return [cls(**user_data) for user_data in users_data]

    @classmethod
    async def from_token_payload(cls, token_payload: BaseTokenPayload) -> Self:
        """Create an instance of ``CasdoorUser`` from a ``CasdoorTokenPayload``.

        :param token_payload: The Casdoor token payload containing user information.
        :return: An instance of ``CasdoorUser``.
        :raises TypeError: If the payload is not a ``CasdoorTokenPayload``.
        """
        if not isinstance(token_payload, CasdoorTokenPayload):
            raise TypeError(
                f"CasdoorUser requires a CasdoorTokenPayload, got "
                f"{type(token_payload).__name__}"
            )
        return await cls.get_user(token_payload.username)

    @classmethod
    async def from_jwt(cls, token: str) -> Self:
        """Create an instance of ``CasdoorUser`` from a JWT token.

        :param token: The JWT token containing user information.
        :return: An instance of ``CasdoorUser``.
        """
        token_payload = await CasdoorTokenPayload.from_jwt(token)
        user = await cls.from_token_payload(token_payload)
        user.access_token = token
        return user
