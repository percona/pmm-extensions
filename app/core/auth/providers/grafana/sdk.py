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

"""Provide the GrafanaSDK for interacting with Grafana services."""

import re
from contextlib import suppress
from datetime import timedelta
from functools import cached_property
from hashlib import sha256
from time import monotonic
from typing import Annotated, Any, Final, Literal, NotRequired

from aiohttp import (
    ClientConnectionError,
    ClientError,
    ClientTimeout,
    DummyCookieJar,
)
from aiohttp.abc import AbstractCookieJar
from annotated_types import Ge, Gt
from async_lru import _LRUCacheWrapper, alru_cache
from fastapi import HTTPException, status
from pydantic import (
    ConfigDict,
    SecretStr,
    TypeAdapter,
    ValidationError,
    with_config,
)
from typing_extensions import TypedDict

from app.core.auth.exceptions import (
    BaseAuthProviderException,
    HTTPUnauthorizedException,
)
from app.core.requests import as_json_array, as_json_object, RemoteAPI
from app.core.requests.remote_api import UPSTREAM_NON_JSON_HEADER
from app.core.utils.cache import TTLCache
from app.core.utils.fields import NonEmptyStr, TimedeltaSeconds

_SERVICE_ACCOUNT_TOKEN_CACHE_SIZE: Final = 1024
_SERVICE_ACCOUNT_CALL_TIMEOUT: Final = ClientTimeout(total=10)
_SERVICE_ACCOUNT_UID_PATTERN: Final = re.compile(r"service-account:([0-9]+)")
_SERVICE_ACCOUNTS_PAGE_SIZE: Final = 100


@with_config(ConfigDict(strict=True))
class GrafanaServiceAccountRecord(TypedDict):
    """Describe a Grafana ``/api/serviceaccounts/{id}`` record or search row.

    Validation is strict, so a field of the wrong JSON type is refused rather
    than coerced. ``role`` is ``NotRequired`` and nullable because Grafana
    models "holds no role" either way; the reader accesses it via ``.get()``.
    """

    id: int
    login: str
    role: NotRequired[str | None]
    isDisabled: bool


SERVICE_ACCOUNT_RECORD: Final = TypeAdapter(GrafanaServiceAccountRecord)
SERVICE_ACCOUNT_RECORDS: Final = TypeAdapter(list[GrafanaServiceAccountRecord])


def _is_grafana_verdict(exc: HTTPException, status_code: int) -> bool:
    """Return whether Grafana itself answered ``exc`` with ``status_code``.

    A non-JSON error body keeps its status but is stamped with
    ``UPSTREAM_NON_JSON_HEADER``: it came from something in front of Grafana,
    such as a proxy's HTML page, and says nothing about the credential.

    :param exc: The error a Grafana call raised.
    :param status_code: The status that would be Grafana's verdict.
    :return: ``True`` when the status matches and the body was Grafana's JSON.
    """
    return exc.status_code == status_code and not (exc.headers or {}).get(
        UPSTREAM_NON_JSON_HEADER
    )


class GrafanaException(BaseAuthProviderException):
    """Define exception for Grafana connection errors.

    :param status_code: The HTTP status code for the error response. Defaults to
        502 (Bad Gateway).
    :param detail: A message providing additional details about the exception.
        Defaults to "Grafana error".
    """

    def __init__(
        self,
        status_code: int = status.HTTP_502_BAD_GATEWAY,
        detail: str = "Grafana error",
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)


class GrafanaSDK(RemoteAPI):
    """Interact with Grafana's authentication and user-management APIs.

    The ``GrafanaSDK`` class extends ``RemoteAPI`` to authenticate human logins
    against Grafana's password-login endpoint and to read user and org-user data
    with a service-account bearer token. It sets no default ``Authorization``
    header: the human-login flow authenticates via Grafana's session cookie,
    while the programmatic calls inject the service-account bearer per request.

    :param endpoint: The base URL for the Grafana API endpoint.
    :param verify_ssl: Whether to verify SSL certificates. Defaults to True.
    :param service_account_token: The Grafana service-account token used as a
        bearer credential for programmatic user reads.
    :param access_token_max_age: How long a minted access assertion stays valid
        (the per-request Bearer credential). Defaults to 1 hour.
    :param refresh_token_max_age: How long a minted refresh assertion stays valid
        (the SPA's ``HttpOnly`` refresh cookie). Defaults to 7 days.
    :param exchange_token_max_age: How long a minted session-exchange assertion
        stays valid (the embedded UI's in-memory bearer). Defaults to 5 minutes.
        This value alone bounds how long a signed-out browser keeps embedded
        access, and how long a Grafana role change takes to take effect, so it
        deliberately does not fall back to ``access_token_max_age``. A
        non-positive value expires every assertion at mint time and is rejected
        at config load rather than silently disabling embedded-UI auth.
    :param service_account_bearer_revocation_window: How long a validated or
        refused service-account token verdict is reused, i.e. how long a
        revoked, disabled or re-roled service account keeps its previous
        verdict on new requests. ``0`` re-verifies on every request. Defaults to
        5 minutes.
    :param error_detail_key: The key Grafana uses for error details. Defaults to
        "message".
    :param session_cookie_name: The name of the cookie Grafana sets on a
        successful password login. Defaults to ``grafana_session``.
    :param withhold_body_log: Pinned to ``True``: Grafana's bodies carry user
        records, org memberships and service-account records, so none of them
        may reach the log. Not a switch, since the provider is built from
        settings and an operator value of ``False`` would put that identity
        data in the log.
    """

    model_config = ConfigDict(ignored_types=(_LRUCacheWrapper,))
    logger_name: str = __name__
    service_account_token: SecretStr
    access_token_max_age: TimedeltaSeconds = timedelta(hours=1)
    refresh_token_max_age: TimedeltaSeconds = timedelta(days=7)
    exchange_token_max_age: Annotated[TimedeltaSeconds, Gt(timedelta(0))] = timedelta(
        minutes=5
    )
    service_account_bearer_revocation_window: Annotated[
        TimedeltaSeconds, Ge(timedelta(0))
    ] = timedelta(minutes=5)
    error_detail_key: NonEmptyStr = "message"
    session_cookie_name: NonEmptyStr = "grafana_session"
    withhold_body_log: Literal[True] = True

    def _cookie_jar(self) -> AbstractCookieJar:
        """Return a jar that keeps no cookies.

        Grafana authenticates a request by its session cookie when the bearer
        it carries fails, so a ``grafana_session`` a login stored would answer
        for every later call's credential: a refused service-account token
        would come back as the human who logged in. :meth:`login` reads the
        cookie off its own response, and the session-bound reads pass theirs
        explicitly, so nothing needs a stored one.

        :return: A jar that stores nothing.
        """
        return DummyCookieJar()

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform an HTTP request and translate connection errors.

        :param method: The HTTP method to use for the request.
        :param path: The API endpoint path to request.
        :param kwargs: Additional keyword arguments to pass to the request.
        :return: The JSON response, or ``None`` on HTTP 204.
        :raises GrafanaException: If Grafana cannot be reached.
        """
        try:
            return await super().request(method, path, **kwargs)
        except ClientConnectionError:
            self.logger.exception("Failed to connect to Grafana.")
            raise GrafanaException(
                detail=f"Cannot connect to Grafana at {self.endpoint}"
            ) from None

    async def login(self, username: str, password: str) -> str:
        """Authenticate a user against Grafana and return the session cookie.

        Post the credentials to Grafana's JSON login endpoint and return the
        established session cookie value. The cookie is used only to read the
        logging-in user's identity and is never persisted.

        :param username: The Grafana username.
        :param password: The Grafana password.
        :return: The value of the established ``grafana_session`` cookie.
        :raises HTTPUnauthorizedException: If Grafana rejects the credentials
            (HTTP 401).
        :raises GrafanaException: If Grafana returns an unexpected status,
            establishes no session, or cannot be reached.
        """
        try:
            async with self._request(
                "POST",
                "/login",
                json={"user": username, "password": password},
            ) as response:
                if response.status == status.HTTP_401_UNAUTHORIZED:
                    raise HTTPUnauthorizedException("Invalid username or password")
                if response.status != status.HTTP_200_OK:
                    raise GrafanaException(
                        detail=f"Grafana login failed (HTTP {response.status})."
                    )
                session_cookie = response.cookies.get(self.session_cookie_name)
                if session_cookie is None:
                    raise GrafanaException(
                        detail="Grafana did not establish a session."
                    )
                return session_cookie.value
        except ClientConnectionError:
            self.logger.exception("Failed to connect to Grafana.")
            raise GrafanaException(
                detail=f"Cannot connect to Grafana at {self.endpoint}"
            ) from None

    async def get_current_user(self, session: str) -> dict[str, Any]:
        """Read the identity of the user owning ``session``.

        :param session: The ``grafana_session`` cookie value from :meth:`login`.
        :return: The Grafana ``/api/user`` record for the session's user.
        """
        with self.extra_headers({"Cookie": f"{self.session_cookie_name}={session}"}):
            return as_json_object(await self.get("/api/user"))

    async def get_current_user_orgs(self, session: str) -> list[dict[str, Any]]:
        """Read the org memberships of the user owning ``session``.

        :param session: The ``grafana_session`` cookie value from :meth:`login`.
        :return: The Grafana ``/api/user/orgs`` records for the session's user.
        """
        with self.extra_headers({"Cookie": f"{self.session_cookie_name}={session}"}):
            return as_json_array(await self.get("/api/user/orgs"))

    @alru_cache(ttl=300)
    async def get_org_users(self) -> list[dict[str, Any]]:
        """Read the org users via the service-account bearer token.

        :return: The Grafana ``/api/org/users`` records.
        """
        with self.auth(self.service_account_token.get_secret_value()):
            return as_json_array(await self.get("/api/org/users"))

    async def lookup_user(self, login: str) -> dict[str, Any]:
        """Fetch a single user by login or email via the service account.

        :param login: The login or email to look up.
        :return: The Grafana ``/api/users/lookup`` record for the user.
        """
        with self.auth(self.service_account_token.get_secret_value()):
            return as_json_object(
                await self.get("/api/users/lookup", params={"loginOrEmail": login})
            )

    @alru_cache(ttl=300)
    async def get_service_accounts(self) -> list[dict[str, Any]]:
        """Read every service account in PMM Extensions' org via the service-account token.

        Pages are read until the collected rows reach the listing's
        ``totalCount`` or a page comes back empty. Only the pagination fields
        are checked here; the rows are validated by their reader.

        :return: The ``serviceAccounts`` rows of every page.
        :raises HTTPException: Whatever a page request raised, including the 502
            for a page that is not a JSON object.
        :raises TimeoutError: If a page request times out.
        :raises aiohttp.ClientError: If a page request fails in transport.
        :raises ValueError: If a page body cannot be decoded.
        :raises GrafanaException: If Grafana cannot be reached, or a page carries
            no integer ``totalCount`` or no ``serviceAccounts`` list.
        """
        accounts: list[dict[str, Any]] = []
        page = 1
        with self.auth(self.service_account_token.get_secret_value()):
            while True:
                payload = as_json_object(
                    await self.get(
                        "/api/serviceaccounts/search",
                        params={"perpage": _SERVICE_ACCOUNTS_PAGE_SIZE, "page": page},
                    )
                )
                rows = payload.get("serviceAccounts")
                total = payload.get("totalCount")
                if not isinstance(rows, list) or not isinstance(total, int):
                    raise GrafanaException(
                        detail="Grafana returned an unreadable service-account list."
                    )
                accounts.extend(rows)
                if not rows or len(accounts) >= total:
                    return accounts
                page += 1

    @cached_property
    def _service_account_verdicts(
        self,
    ) -> TTLCache[GrafanaServiceAccountRecord | None]:
        """Return the verdict cache, sized once from this instance's settings.

        :return: A cache mapping a token digest to its record, or ``None`` for a
            refusal.
        """
        return TTLCache(
            ttl=self.service_account_bearer_revocation_window.total_seconds(),
            maxsize=_SERVICE_ACCOUNT_TOKEN_CACHE_SIZE,
            typed=False,
        )

    async def verify_service_account_token(
        self, token: str
    ) -> GrafanaServiceAccountRecord | None:
        """Verify a Grafana service-account token presented to PMM Extensions.

        A verdict (the account record, or a refusal Grafana itself answered)
        is reused for ``service_account_bearer_revocation_window``, keyed on the
        token's SHA-256 digest so the cache never holds the secret. The window
        is counted from when the check started rather than from when Grafana
        answered, since the record is read after that moment, so a slow answer
        cannot stretch a verdict past the window. An upstream failure is never
        reused.

        :param token: The ``glsa_`` token the caller presented.
        :return: The ``/api/serviceaccounts/{id}`` record, or ``None`` when
            Grafana rejected the token or the account is not in PMM Extensions' org.
        :raises GrafanaException: If Grafana could not decide: unreachable,
            slow, erroring, refusing PMM Extensions' own credential, or answering off
            contract.
        """
        cache = self._service_account_verdicts
        key = (sha256(token.encode()).hexdigest(),)
        started = monotonic()
        with cache.lock, suppress(KeyError):
            return cache.get(key, started)
        verdict = await self._fetch_service_account_verdict(token)
        with cache.lock:
            cache.set(key, verdict, started)
            cache.evict_if_needed(monotonic())
        return verdict

    async def _fetch_service_account_verdict(
        self, token: str
    ) -> GrafanaServiceAccountRecord | None:
        """Ask Grafana who ``token`` belongs to, then read that account's record.

        ``/api/user`` proves the token and names the account in ``uid``, but it
        reports every service account as enabled, so role and disabled flag are
        read from the account record through PMM Extensions' own credential. That call is
        org-scoped by Grafana, so a 404 covers an account in another org as well
        as one deleted in between.

        :param token: The presented service-account token.
        :return: The account record, or ``None`` for a Grafana-answered refusal.
        :raises GrafanaException: For every outcome that is not a verdict.
        """
        identity = await self._read_for_verification(
            "/api/user", token, refusal=status.HTTP_401_UNAUTHORIZED
        )
        if identity is None:
            return None
        uid, login = identity.get("uid"), identity.get("login")
        match = (
            _SERVICE_ACCOUNT_UID_PATTERN.fullmatch(uid)
            if isinstance(uid, str)
            else None
        )
        if match is None or not isinstance(login, str) or not login.strip():
            raise GrafanaException(
                detail="Grafana returned an unreadable service-account identity."
            )
        account_id = int(match.group(1))
        payload = await self._read_for_verification(
            f"/api/serviceaccounts/{account_id}",
            self.service_account_token.get_secret_value(),
            refusal=status.HTTP_404_NOT_FOUND,
        )
        if payload is None:
            return None
        try:
            record = SERVICE_ACCOUNT_RECORD.validate_python(payload)
        except ValidationError:
            raise GrafanaException(
                detail="Grafana returned an unreadable service-account record."
            ) from None
        if record["id"] != account_id or record["login"] != login:
            raise GrafanaException(
                detail="Grafana returned an unreadable service-account record."
            )
        return record

    async def _read_for_verification(
        self, path: str, credential: str, *, refusal: int
    ) -> dict[str, Any] | None:
        """Read one verification record, separating a verdict from a failure.

        :param path: The Grafana path to read.
        :param credential: The bearer token to read it with.
        :param refusal: The status that, answered by Grafana in JSON, is a
            refusal rather than a failure.
        :return: The JSON object, or ``None`` for a Grafana-answered refusal.
        :raises GrafanaException: For any other status, a timeout, a transport
            error, or a body that is not a JSON object.
        """
        try:
            with self.auth(credential):
                payload = await self.get(path, timeout=_SERVICE_ACCOUNT_CALL_TIMEOUT)
        except GrafanaException:
            raise
        except HTTPException as exc:
            if _is_grafana_verdict(exc, refusal):
                return None
            failed_status = exc.status_code
        except (TimeoutError, ClientError, ValueError):
            self.logger.warning(
                "Grafana did not answer %s while verifying a service-account token.",
                path,
                exc_info=True,
            )
            raise GrafanaException(
                detail="Grafana could not verify the service-account token."
            ) from None
        else:
            if not isinstance(payload, dict):
                raise GrafanaException(
                    detail=f"Grafana returned an unreadable {path} response."
                )
            return payload
        self.logger.error(
            "Grafana answered HTTP %s to %s while verifying a service-account token.",
            failed_status,
            path,
        )
        raise GrafanaException(
            detail=f"Grafana could not verify the service-account token "
            f"(HTTP {failed_status})."
        )
