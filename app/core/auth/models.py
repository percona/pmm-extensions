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

"""Define the base auth models."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from functools import cached_property, total_ordering
from typing import Any, Final, Self

from pydantic import (
    BaseModel,
    computed_field,
    Field,
    FutureDatetime,
    PastDatetime,
    TypeAdapter,
    UUID4,
    ValidationError,
)

from app.core.utils.date_time import utc_now
from app.core.utils.fields import (
    EmptyStrToNone,
    EnumFieldMixin,
    NonEmptyStr,
    TimedeltaSeconds,
)


class OAuthToken(BaseModel):
    """Represent an OAuth token.

    :param access_token: The token used to access protected resources.
    :type access_token: str
    :param id_token: The token that contains identity information about the user.
    :type id_token: str
    :param refresh_token: The token used to obtain new access tokens after the
        current one expires.
    :type refresh_token: str
    :param token_type: The type of token, typically "bearer".
    :type token_type: str
    :param expires_in: The time duration after which the token expires.
    :type expires_in: TimedeltaSeconds
    :param scope: The scope of the access granted by the token.
    :type scope: str
    """

    access_token: str
    id_token: str
    refresh_token: str
    token_type: str
    expires_in: TimedeltaSeconds
    scope: str


class SPAOAuthTokenResponse(BaseModel):
    """Represent the slim OAuth token response returned to SPA clients.

    Omit ``refresh_token``, ``id_token``, ``token_type``, and ``scope``. The
    SPA keeps the refresh token in an ``HttpOnly`` cookie and derives nothing
    from the other fields.

    :param access_token: The token used to access protected resources.
    :type access_token: str
    :param expires_in: The time duration after which the access token expires.
    :type expires_in: TimedeltaSeconds
    """

    access_token: str
    expires_in: TimedeltaSeconds


class SessionExchangeTokenResponse(BaseModel):
    """Represent the bearer returned when an ambient session is exchanged.

    Mirror :class:`SPAOAuthTokenResponse`'s field names so a client's token
    provider reads the same shape, but keep the contract separate: no refresh
    token is issued and no cookie is set, so the holder renews by exchanging
    again rather than by refreshing.

    :param access_token: The short-lived token used to access protected
        resources.
    :param expires_in: The time duration after which the token expires.
    """

    access_token: str
    expires_in: TimedeltaSeconds


class BaseTokenPayload(BaseModel, ABC):
    """Represent the payload of a JWT token.

    :param iss: The issuer of the token.
    :param sub: The subject or user identifier the token refers to.
    :param aud: The audience for whom the token is intended.
    :param exp: The expiration time of the token.
    :param nbf: The time before which the token must not be accepted for
        processing.
    :param jti: The JWT token identifier.
    """

    iss: str
    sub: str
    aud: list[str]
    exp: FutureDatetime
    nbf: PastDatetime
    jti: str

    @classmethod
    @abstractmethod
    async def from_jwt(cls, token: str) -> Self:
        """Create an instance from a JWT token.

        :param token: The JWT token string to decode.
        :type token: str
        :return: An instance of the token payload.
        :rtype: Self
        """


@total_ordering
class UserRole(EnumFieldMixin, Enum):
    """Enumerate an identity's access level, lowest to highest.

    Members and ordering mirror PMM's own authorization vocabulary so the two
    products stay semantically aligned; ``SUPER_ADMIN`` is PMM Extensions'
    provider-neutral name for the rank PMM calls ``grafanaAdmin``.

    Members compare by declared rank rather than by name: ``EDITOR < ADMIN``
    even though ``"editor" > "admin"`` lexicographically. Members are not
    ``str``, so ``ADMIN == "admin"`` is False; the serialized value is
    unchanged at ``"admin"``.
    """

    NONE = "none"
    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, UserRole):
            raise TypeError(f"cannot order UserRole against {type(other).__name__}")
        return _USER_ROLE_ORDER.index(self) < _USER_ROLE_ORDER.index(other)


_USER_ROLE_ORDER: Final = tuple(UserRole)


_ADMIN_FLAG_ADAPTER: Final = TypeAdapter(bool)


class BaseUser(BaseModel, ABC):
    """Represent the abstract base for a user.

    :param id: The unique identifier of the user.
    :param username: The username of the user. Must be at least 1 character long.
    :param email: The email address of the user.
    :param first_name: The first name of the user.
    :param last_name: The last name of the user.
    :param role: The user's access level. ``is_admin`` is derived from it.
    :param created_time: The datetime when the user was created. Defaults to
        current datetime.
    :param updated_time: The datetime when the user was last updated. Defaults
        to current datetime.
    """

    id: UUID4
    username: NonEmptyStr
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    role: UserRole
    created_time: datetime | EmptyStrToNone = Field(default_factory=utc_now)
    updated_time: datetime | EmptyStrToNone = Field(default_factory=utc_now)
    _access_token: str = ""

    @computed_field
    @property
    def full_name(self) -> str:
        """The combination of the user's first and last names.

        :return: The user's full name.
        :rtype: str
        """
        return f"{self.first_name} {self.last_name}".strip()

    @computed_field
    @cached_property
    def is_active(self) -> bool:
        """Indicate whether the user is currently active.

        :return: True if the user is active, False otherwise.
        :rtype: bool
        """
        return True

    @computed_field
    @property
    def is_admin(self) -> bool:
        """Indicate whether the user holds administrative privileges.

        :return: True from ``ADMIN`` upwards, False below it.
        """
        return self.role >= UserRole.ADMIN

    @property
    def access_token(self) -> str:
        """Get the user's access token.

        :return: The user's access token.
        :rtype: str
        """
        return self._access_token

    @access_token.setter
    def access_token(self, v: str) -> None:
        """Set the user's access token.

        :param v: The new access token.
        :type v: str
        """
        self._access_token = v

    @staticmethod
    def _role_from_admin_flag(flag: Any) -> UserRole:
        """Map a legacy admin flag onto the ordered role.

        The flag carries one bit, so it resolves only to the two roles the
        admin gates already distinguish: ``ADMIN`` preserves the access the flag
        granted, ``VIEWER`` the reads a non-admin has today.

        The flag is validated as a boolean rather than tested for truthiness, so
        a payload spelling it out as ``"false"``, ``"0"`` or ``b"false"``
        resolves to ``VIEWER`` instead of reading as truthy and escalating to
        ``ADMIN``.

        :param flag: The raw admin flag carried by a provider payload.
        :return: The role the flag maps to.
        :raises ValueError: If the flag is not a value Pydantic accepts as a
            boolean.
        """
        try:
            is_admin = _ADMIN_FLAG_ADAPTER.validate_python(flag)
        except ValidationError as exc:
            raise ValueError(f"invalid admin flag: {flag!r}") from exc
        return UserRole.ADMIN if is_admin else UserRole.VIEWER

    @classmethod
    def build_service_principal(
        cls,
        *,
        user_id: UUID4,
        username: NonEmptyStr,
        role: UserRole,
        email: str = "",
        first_name: str = "",
        last_name: str = "",
        **provider_fields: Any,
    ) -> Self:
        """Build the synthetic service-principal user for PMM Extensions internal auth.

        Fill the fields common to every provider's user model. A provider whose
        user model requires additional fields overrides this to inject them via
        ``provider_fields`` before delegating here.

        :param user_id: The service principal's stable unique identifier.
        :param username: The service principal's username.
        :param role: The service principal's access level.
        :param email: The service principal's email address; empty by default.
        :param first_name: The service principal's first name; empty by default.
        :param last_name: The service principal's last name; empty by default.
        :return: A user instance representing the service principal.
        """
        return cls(
            id=user_id,
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=role,
            **provider_fields,
        )

    @staticmethod
    @abstractmethod
    async def get_oauth_token(
        code: str | None = None,
        username: str | None = None,
        password: str | None = None,
        refresh_token: str | None = None,
    ) -> OAuthToken:
        """Obtain an OAuth token.

        This method must be overridden in subclasses to provide specific logic for
        obtaining OAuth tokens based on different grant types.

        :param code: The authorization code received from the OAuth2 provider via
            redirect URL.
        :type code: str | None
        :param username: The username for resource owner password credentials grant.
        :type username: str | None
        :param password: The password for resource owner password credentials grant.
        :type password: str | None
        :param refresh_token: The refresh token to obtain a new access token.
        :type refresh_token: str | None
        :return: The OAuth token response.
        :rtype: OAuthToken
        """

    @staticmethod
    @abstractmethod
    async def invalidate_oauth_token(access_token: str) -> None:
        """Invalidate an OAuth token.

        This method must be overridden in subclasses to provide specific logic for
        invalidating OAuth tokens.

        :param access_token: The access token to invalidate.
        :type access_token: str
        """

    @classmethod
    @abstractmethod
    async def get_user(cls, username: NonEmptyStr) -> Self:
        """Retrieve a user by username.

        This method must be overridden in subclasses to provide specific logic for
        retrieving a user from the data store.

        :param username: The username of the user to retrieve.
        :type username: NonEmptyStr
        :return: An instance of the user model.
        :rtype: Self
        """

    @classmethod
    @abstractmethod
    async def get_users(cls) -> Sequence[Self]:
        """Retrieve all users.

        This method must be overridden in subclasses to provide specific logic for
        retrieving all users from the data store.

        :return: A sequence of user instances.
        """

    @classmethod
    async def get_actors(cls) -> Sequence[Self]:
        """Retrieve every identity that can appear as a task actor.

        Defaults to :meth:`get_users`. A provider whose non-listed identities
        (such as Grafana service accounts) can also run tasks overrides it so
        those identities are named too.

        :return: A sequence of user instances.
        """
        return await cls.get_users()

    @classmethod
    @abstractmethod
    async def from_token_payload(cls, token_payload: BaseTokenPayload) -> Self:
        """Create a user instance from a token payload.

        This method must be overridden in subclasses to provide specific logic for
        constructing a user instance based on the contents of a JWT token payload.

        :param token_payload: The decoded JWT token payload.
        :type token_payload: BaseTokenPayload
        :return: An instance of the user model.
        :rtype: Self
        """

    @classmethod
    @abstractmethod
    async def from_jwt(cls, token: str) -> Self:
        """Create a user instance from a JWT token.

        This method must be overridden in subclasses to provide specific logic for
        decoding a JWT token and constructing a user instance.

        :param token: The JWT token string to decode.
        :type token: str
        :return: An instance of the user model.
        :rtype: Self
        """

    @classmethod
    async def from_bearer(cls, token: str) -> Self:
        """Create a user instance from a token presented as an API Bearer credential.

        Default to :meth:`from_jwt`, so a provider whose Bearer surface accepts
        exactly what its session-cookie surface accepts needs no override. A
        provider that mints an additional credential type valid *only* as a
        Bearer overrides this; widening the cookie surface then requires calling
        a different method rather than passing a different argument.

        :param token: The token carried in the ``Authorization: Bearer`` header.
        :return: An instance of the user model.
        """
        return await cls.from_jwt(token)
