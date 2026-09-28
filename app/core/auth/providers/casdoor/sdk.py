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

"""Provide the CasdoorSDK for interacting with Casdoor services."""

from base64 import b64encode
from collections.abc import AsyncGenerator
from functools import cached_property
from math import ceil
from typing import Any, Literal, Self

from aiohttp import ClientConnectionError
from async_lru import _LRUCacheWrapper, alru_cache
from fastapi import HTTPException, status
from pydantic import computed_field, ConfigDict, model_validator, SecretStr

from app.core.auth.exceptions import (
    BaseAuthProviderException,
    HTTPUnauthorizedException,
)
from app.core.requests import (
    as_json_array,
    as_json_object,
    CredentialHeaderMixin,
    JSONBody,
    RemoteAPI,
)
from app.core.utils.fields import (
    NonEmptyStr,
    RelativeFilePathField,
    StrHttpUrl,
    URL,
)


class CasdoorException(BaseAuthProviderException):
    """Define exception for Casdoor connection errors.

    :param status_code: The HTTP status code for the error response. Defaults to
        502 (Bad Gateway).
    :param detail: A message providing additional details about the exception.
        Defaults to "Casdoor error".
    """

    def __init__(
        self,
        status_code: int = status.HTTP_502_BAD_GATEWAY,
        detail: str = "Casdoor error",
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)


class CasdoorSDK(CredentialHeaderMixin, RemoteAPI):
    """Interact with Casdoor's authentication and user management APIs.

    The ``CasdoorSDK`` class extends ``RemoteAPI`` to provide methods for managing OAuth
    tokens and retrieving user information from Casdoor. It handles authentication
    using client credentials and supports various grant types for obtaining access
    tokens.

    :param endpoint: The base URL for the external API endpoint.
    :param verify_ssl: Whether to verify SSL certificates. Defaults to True.
    :param ssl_cafile: Path to the SSL certificate authority file. Defaults to None.
    :param ssl_keyfile: Path to the SSL key file. Defaults to None.
    :param ssl_certfile: Path to the SSL certificate file. Defaults to None.
    :param logger_name: Name to use for the logger. Defaults to ``__name__``.
    :param client_id: The client ID for Casdoor authentication.
    :param client_secret: The client secret for Casdoor authentication.
    :param organization_name: The organization name in Casdoor.
    :param organization_name: The name of the organization in Casdoor. Defaults to
        "built-in".
    :param application_name: The name of the application in Casdoor. Defaults to
        "app-built-in"
    :param front_endpoint: The front-end endpoint for the Casdoor integration.
    :param certificate_path: The file path to the Casdoor certificate. Defaults to None.
    :param allowed_issuers: The allowed token issuers (iss) for JWT validation.
        Defaults to an empty list.
    :param error_detail_key: The key to expect errors details to be. Defaults to
        "message".
    :param error_code_key: The key to expect error codes to be, or None if no error
        code is expected. Defaults to "code".
    """

    model_config = ConfigDict(ignored_types=(_LRUCacheWrapper,))
    logger_name: str = __name__
    client_id: SecretStr
    client_secret: SecretStr
    organization_name: str = "built-in"
    application_name: str = "app-built-in"
    front_endpoint: URL = URL()
    certificate_path: RelativeFilePathField | None = None
    allowed_issuers: set[StrHttpUrl] | Literal["*"] = set()
    error_detail_key: NonEmptyStr = "error_description"
    error_code_key: NonEmptyStr | None = "error"

    @computed_field
    @cached_property
    def certificate(self) -> bytes | None:
        """The contents of the certificate file.

        :return: The certificate file contents or None if certificate_path is not
            defined.
        """
        if self.certificate_path is not None:
            with self.certificate_path.open("rb") as certificate_file:
                return certificate_file.read()
        return None

    @property
    def _authorization_scheme(self) -> str:
        """Return the fixed Basic scheme Casdoor's Management API expects.

        Not a settings field: an operator-configurable scheme would let a typo
        (e.g. Bearer) break every Casdoor request.

        :return: ``"Basic"``.
        """
        return "Basic"

    @property
    def _credential_value(self) -> str:
        """Return the Base64-encoded client credentials for Basic auth.

        Encodes ``client_id`` and ``client_secret`` as
        ``client_id:client_secret`` into a Base64 string.

        :return: The Base64-encoded credential.
        """
        return b64encode(
            f"{self.client_id.get_secret_value()}:{self.client_secret.get_secret_value()}".encode(),
        ).decode("utf-8")

    @model_validator(mode="after")
    def _set_default_allowed_issuers(self) -> Self:
        """Set default allowed issuers if not already set.

        If ``allowed_issuers`` is not set to "*", ensure that the API endpoint is
        included in it.

        :return: The updated instance with ``allowed_issuers`` set.
        """
        str_endpoint = str(self.endpoint).rstrip("/")
        if self.allowed_issuers != "*":
            self.allowed_issuers.add(str_endpoint)
        return self

    def get_frontend_url(self, base_url: URL | None = None) -> URL:
        """Get Casdoor's front-end URL from a base URL.

        Construct the frontend URL for Casdoor integration by replacing any missing
        parts (scheme, hostname, port, path) from the ``front_endpoint`` with
        corresponding parts from the ``base_url``.

        :param base_url: The base URL to be used when constructing the frontend
            URL. If not provided, the Casdoor API endpoint (``endpoint``) is used
            as the base.
        :return: The constructed front-end URL.
        """
        if self.front_endpoint.scheme:
            return self.front_endpoint
        base_url = URL(self.endpoint) if base_url is None else base_url
        url_data = {
            "scheme": self.front_endpoint.scheme or base_url.scheme,
            "hostname": self.front_endpoint.hostname or base_url.hostname,
            "port": self.front_endpoint.port or base_url.port,
            "path": self.front_endpoint.path or base_url.path,
        }
        return URL(str(self.front_endpoint.replace(**url_data)))

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> JSONBody:
        """Perform an HTTP request and return the JSON response.

        :param method: The HTTP method to use for the request.
        :param path: The API endpoint path to request.
        :param kwargs: Additional keyword arguments to pass to the request.
        :return: The JSON response as a Python object, or ``None`` when Casdoor
            answers HTTP 204 with no body.
        :raises HTTPException: If the request returns an error response.
        """
        try:
            return await super().request(method, path, **kwargs)
        except ClientConnectionError:
            self.logger.exception("Failed to connect to Casdoor.")
            raise CasdoorException(
                detail=f"Cannot connect to Casdoor at {self.endpoint}"
            ) from None

    async def refresh_token_request(
        self,
        refresh_token: str,
        scope: str = "",
    ) -> dict[str, Any]:
        """Request a new access token using a refresh token.

        Sends a request to Casdoor to obtain a new access token using the provided
        refresh token and optional scope.

        :param refresh_token: The refresh token to send to Casdoor.
        :param scope: The OAuth scope for the token request. Defaults to an
            empty string.
        :return: The response from Casdoor containing the new access token.
        """
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id.get_secret_value(),
            "client_secret": self.client_secret.get_secret_value(),
            "scope": scope,
            "refresh_token": refresh_token,
        }
        return as_json_object(
            await self.post("/api/login/oauth/refresh_token", json=data)
        )

    async def get_access_token(
        self,
        code: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Obtain an OAuth access token from Casdoor.

        Requests an OAuth token from Casdoor using either an authorization code,
        username and password, or client credentials.

        :param code: The authorization code received from Casdoor via redirect URL.
            Defaults to None.
        :param username: The username for resource owner password credentials
            grant. Defaults to None.
        :param password: The password for resource owner password credentials
            grant. Defaults to None.
        :return: The OAuth token response from Casdoor.
        :raises HTTPUnauthorizedException: If authentication fails due to incorrect
            credentials.
        :raises HTTPException: If Casdoor responds with an unexpected error.
        """
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id.get_secret_value(),
            "client_secret": self.client_secret.get_secret_value(),
        }
        invalid_grant_message = "Invalid credentials."
        if code:
            data["code"] = code
            data["grant_type"] = "authorization_code"
            invalid_grant_message = "Invalid authorization code."
        elif username and password:
            data["username"] = username
            data["password"] = password
            data["grant_type"] = "password"
            invalid_grant_message = "Invalid username or password."
        try:
            return as_json_object(
                await self.post("/api/login/oauth/access_token", json=data)
            )
        except HTTPException as exc:
            if exc.headers and exc.headers.get("X-Error-Code") == "invalid_grant":
                raise HTTPUnauthorizedException(invalid_grant_message) from None
            raise

    async def get_version_info(self) -> dict[str, Any]:
        """Retrieve the current version information from Casdoor.

        :return: A dictinoary containin Casdoor's version details.
        """
        version = as_json_object(await self.get("/api/get-version-info"))
        return version["data"]

    async def introspect_token(
        self,
        token: str,
        token_type: Literal["access-token", "refresh-token"] = "access-token",  # noqa: S107
    ) -> dict[str, Any]:
        """Introspect a token to verify its validity.

        Sends a request to Casdoor to verify the provided token. If Casdoor
        version is >= 1.765.0, the token type hint must be ``access-token`` or
        ``refresh-token``. Otherwise, it must be ``access_token`` or ``refresh_token``.

        :param token: The token to introspect.
        :param token_type: The type of the token being introspected. Defaults to
            "access-token". For Casdoor < 1.765.0, ``_`` is used instead of ``-``.
        :return: The introspection result from Casdoor.
        """
        version_info = await self.get_version_info()
        casdoor_version = version_info["version"].lstrip("v")
        major_str, minor_str, patch_str = casdoor_version.split(".")[:3]
        major, minor, patch = int(major_str), int(minor_str), int(patch_str)

        if (major, minor, patch) < (1, 765, 0):
            token_type_hint = token_type.replace("-", "_")
        else:
            token_type_hint = token_type
        return as_json_object(
            await self.post(
                "/api/login/oauth/introspect",
                data={"token": token, "token_type_hint": token_type_hint},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        )

    async def get_token(self, token_id: str) -> dict[str, Any]:
        """Retrieve a specific token by its ID.

        Fetches the token details from Casdoor using the provided token ID.

        :param token_id: The ID of the token to retrieve.
        :return: The token details retrieved from Casdoor.
        """
        response = as_json_object(
            await self.get("/api/get-token", params={"id": token_id})
        )
        return response["data"]

    async def get_tokens(
        self, owner: str, username: str | None = None, **params: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Retrieve the tokens for a username.

        Retrieves the tokens details from Casdoor from the provided username and yields
        each token data.

        :param owner: The owner of the tokens.
        :param username: The username to retrieve tokens for, or None to retrieve all
            tokens. Defaults to None.
        :param params: Additional query parameters.
        :yield: The tokens details retrieved from Casdoor.
        """
        page_size = 100
        params |= {
            "owner": owner,
            "organization": self.organization_name,
            "pageSize": page_size,
            "p": 1,
        }
        tokens: dict[str, Any] | None = as_json_object(
            await self.get(
                "/api/get-tokens",
                params=params,
            )
        )
        max_page = ceil((tokens.get("data2") or 0) / page_size)
        while params["p"] <= max_page:
            if tokens is None:
                tokens = as_json_object(
                    await self.get("/api/get-tokens", params=params)
                )
            for token in tokens["data"]:
                if username is None or token["user"] == username:
                    yield token
            params["p"] += 1
            tokens = None

    async def get_active_tokens(
        self, owner: str, username: str | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Retrieve the active tokens for a username.

        Retrieves the active tokens details from Casdoor from the provided username and
        yields each token data.

        :param owner: The owner of the tokens.
        :param username: The username to retrieve tokens for, or None to retrieve all
            tokens. Defaults to None.
        :yield: The tokens details retrieved from Casdoor.
        """
        async for token in self.get_tokens(
            owner, username, sortField="codeExpireIn", sortOrder="ascend"
        ):
            if token["codeExpireIn"] > 0:
                break
            yield token

    async def delete_token(self, token: dict[str, Any]) -> bool:
        """Delete a token from Casdoor.

        :param token: The token to delete.
        :return: Whether the token was deleted.
        """
        response = as_json_object(await self.post("/api/delete-token", json=token))
        return response["data"].lower() == "affected"

    @alru_cache(ttl=300)
    async def get_users(self) -> list[dict[str, Any]]:
        """Retrieve a list of users from Casdoor.

        Fetches all users associated with the configured organization from Casdoor.
        Casdoor reports a denied or failed listing as HTTP 200 with ``"data": null``,
        so the payload is parsed rather than trusted: that body raises instead of
        returning ``None``, and a raised call is not cached.

        :return: A list of user data.
        :raises HTTPBadGatewayException: If the payload is not a JSON object or its
            ``data`` is not a list of JSON objects, which includes Casdoor's error
            body.
        """
        users = as_json_object(
            await self.get("/api/get-users", params={"owner": self.organization_name})
        )
        return as_json_array(users["data"])

    async def get_user(self, username: str) -> dict[str, Any]:
        """Retrieve a specific user's information from Casdoor.

        Fetches the details of a user identified by the provided username.

        :param username: The username of the user to retrieve.
        :return: A dictionary containing the user's information.
        """
        user = as_json_object(
            await self.get(
                "/api/get-user",
                params={"id": f"{self.organization_name}/{username}"},
            )
        )
        return user["data"]

    async def get_user_application(self, username: str) -> dict[str, Any]:
        """Retrieve a specific user's application information from Casdoor.

        Fetches the details of a user's application by username.

        :param username: The username of the user to retrieve.
        :return: A dictionary containing the user's information.
        """
        user = as_json_object(
            await self.get(
                "/api/get-user-application",
                params={"id": f"{self.organization_name}/{username}"},
            )
        )
        return user["data"]
