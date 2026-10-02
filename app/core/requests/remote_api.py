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

"""Manage remote API interactions."""

__all__ = [
    "UPSTREAM_NON_JSON_HEADER",
    "BaseRemoteAPI",
    "CredentialHeaderMixin",
    "JSONBody",
    "PendingCloses",
    "RemoteAPI",
    "StoredCredentialHeaderMixin",
    "as_json_array",
    "as_json_object",
    "exception_for_status",
    "is_non_json_success",
]

import asyncio
import logging
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Generator,
    Iterable,
    Mapping,
)
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from functools import cached_property, lru_cache
from ssl import create_default_context, SSLContext
from types import TracebackType
from typing import Annotated, Any, BinaryIO, ClassVar, NoReturn, Self
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit

from aiohttp import (
    ClientResponse,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
    encode_basic_auth,
    FormData,
    TCPConnector,
)
from aiohttp.abc import AbstractCookieJar
from fastapi import HTTPException, status
from pydantic import computed_field, Field, PrivateAttr

from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPGoneException,
    HTTPInternalServerErrorException,
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.log import correlation_id_var
from app.core.models import BaseCaseInsensitiveModel
from app.core.requests.connectivity import (
    build_connectivity_result,
    classify_connectivity_error,
    ConnectivityResult,
    ConnectivityStatusEnum,
    PROBE_TIMEOUT_SECONDS,
)
from app.core.utils import json_serializer
from app.core.utils.fields import (
    AuthCredentialSecretStr,
    AuthSchemeStr,
    CREDENTIAL_URL_MASK,
    CREDENTIAL_URL_STR_JSON_SERIALIZER,
    CredentialHttpUrl,
    NonEmptyStr,
    redact_credential_url,
    RelativeFilePathField,
    strip_credential_url_userinfo,
)
from app.core.utils.strings import shorten_text

# Maximum size of a single line yielded by RemoteAPI.stream(). aiohttp's default
# StreamReader caps lines at ~128 KiB (2 * read_bufsize), which is too small for
# verbose NDJSON log lines from tasks like xtrabackup. 16 MiB stays well below an
# OOM threshold while comfortably covering real log chunks.
_MAX_STREAM_LINE_BYTES = 16 * 1024 * 1024

# Request headers whose values carry credentials and must never reach the logs.
# Compared case-insensitively.
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "x-api-key", "cookie", "proxy-authorization"}
)
# Request body fields whose values carry credentials and must never reach the
# logs. Compared case-insensitively against JSON/form body keys.
_SENSITIVE_BODY_FIELDS = frozenset({"password", "secret", "token"})
_REDACTED_VALUE = "****"
# Stands in for a response body a caller withheld from the log, so the line
# keeps naming the request that produced it.
_WITHHELD_BODY = "<withheld>"
# Bounds the decoded body reaching the exception log: the upstream answering
# HTML rather than JSON decides that body's size, so a large error page would
# otherwise flood the log with a single record.
_NON_JSON_LOG_MAX_CHARS = 2000
_TRUNCATION_MARKER = "... (truncated)"

# Stamped on the raised ``HTTPException`` when an error response has a non-JSON
# body (e.g. an nginx HTML 502), letting callers tell a proxy/gateway failure
# apart from an app-level JSON error at the same status code.
UPSTREAM_NON_JSON_HEADER = "X-Upstream-Non-JSON"

#: The body of a single multipart file part: raw bytes held in memory, an open
#: binary handle, or an async iterator of chunks. aiohttp streams the latter two,
#: so a caller forwarding a file it never has on disk passes the iterator through.
FileContent = bytes | BinaryIO | AsyncIterable[bytes]

#: A single multipart file part: ``(filename, content, content_type)``.
FileSpec = tuple[str, FileContent, str]

#: The parsed body of a JSON response: an object, an array of objects, or
#: ``None`` when the server answered HTTP 204 with no body.
JSONBody = dict[str, Any] | list[dict[str, Any]] | None

# Maps an upstream error status to the project exception that represents it, so
# RemoteAPI raises app/core/exceptions classes instead of a bare HTTPException.
_HTTP_EXCEPTION_BY_STATUS: dict[int, type[HTTPException]] = {
    status.HTTP_400_BAD_REQUEST: HTTPBadRequestException,
    status.HTTP_404_NOT_FOUND: HTTPNotFoundException,
    status.HTTP_409_CONFLICT: HTTPConflictException,
    status.HTTP_410_GONE: HTTPGoneException,
    status.HTTP_422_UNPROCESSABLE_CONTENT: HTTPUnprocessableEntityException,
    status.HTTP_500_INTERNAL_SERVER_ERROR: HTTPInternalServerErrorException,
    status.HTTP_502_BAD_GATEWAY: HTTPBadGatewayException,
    status.HTTP_503_SERVICE_UNAVAILABLE: HTTPServiceUnavailableException,
}


def _is_redirect(status_code: int) -> bool:
    """Return whether ``status_code`` is a 3xx redirect.

    :param status_code: The upstream HTTP status to classify.
    :return: ``True`` for any 3xx status.
    """
    return status.HTTP_300_MULTIPLE_CHOICES <= status_code < status.HTTP_400_BAD_REQUEST


def exception_for_status(
    status_code: int, *, detail: Any, headers: dict[str, str] | None = None
) -> HTTPException:
    """Return the project exception mapped to ``status_code``, else a bare HTTPException.

    Fall back to a bare :class:`fastapi.HTTPException` when no project class is
    mapped. Every mapped class accepts a ``headers`` kwarg, so headers are always
    preserved.

    A non-JSON error body (marked with ``UPSTREAM_NON_JSON_HEADER``) signals a
    proxy/gateway failure rather than an app-level status, so a non-JSON 404 stays
    a bare HTTPException -- callers narrowing to ``except HTTPNotFoundException``
    must not mistake an upstream infra failure for a real resource-absent
    response. The other statuses keep their mapping on non-JSON bodies, matching
    how they already behave for JSON bodies.

    :param status_code: The upstream HTTP error status to translate.
    :param detail: The error detail payload to attach to the exception.
    :param headers: Optional response headers to preserve (e.g. ``X-Error-Code``).
    :return: The mapped project exception, or a bare HTTPException.
    """
    exc_class = _HTTP_EXCEPTION_BY_STATUS.get(status_code)
    is_non_json = bool(headers) and UPSTREAM_NON_JSON_HEADER in headers
    if exc_class is None or (is_non_json and exc_class is HTTPNotFoundException):
        return HTTPException(status_code=status_code, detail=detail, headers=headers)
    return exc_class(detail, headers=headers)


def as_json_object(payload: JSONBody) -> dict[str, Any]:
    """Return ``payload`` as a JSON object, rejecting any other shape.

    The verb methods declare the whole union a JSON body may take — an object,
    an array, or ``None`` on HTTP 204. A caller that reads the result as a
    mapping is asserting a shape the transport never checked; this checks it and
    turns a mis-shaped upstream answer into a 502 rather than a ``TypeError``
    further down.

    :param payload: The parsed body returned by a :class:`BaseRemoteAPI` verb.
    :return: The payload as a plain dict.
    :raises HTTPBadGatewayException: If the payload is not a JSON object.
    """
    if not isinstance(payload, dict):
        raise HTTPBadGatewayException(
            detail="The server answered with an unexpected payload shape."
        )
    return payload


def as_json_array(payload: JSONBody) -> list[dict[str, Any]]:
    """Return ``payload`` as a JSON array of objects, rejecting any other shape.

    The elements are checked too, so the returned ``list[dict[str, Any]]`` is a
    verified claim rather than an asserted one.

    :param payload: The parsed body returned by a :class:`BaseRemoteAPI` verb.
    :return: The payload itself, once every element is confirmed to be an object.
    :raises HTTPBadGatewayException: If the payload is not a JSON array, or any
        element of it is not a JSON object.
    """
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise HTTPBadGatewayException(
            detail="The server answered with an unexpected payload shape."
        )
    return payload


def is_non_json_success(exc: HTTPException) -> bool:
    """Return whether ``exc`` reports a successful answer whose body was not JSON.

    :meth:`RemoteAPI.request` parses every body but a ``204`` before it checks
    the status, so a receiver answering ``200 text/plain`` (an acknowledgement
    string, an HTML health page, an empty non-``204`` body) surfaces as a
    ``2xx`` :class:`fastapi.HTTPException` rather than as the success it is.
    Callers that do not need the parsed body use this to tell that case from a
    real upstream error.

    :param exc: The exception :meth:`RemoteAPI.request` raised.
    :return: ``True`` when the status is below 400 and the body was not JSON.
    """
    return exc.status_code < status.HTTP_400_BAD_REQUEST and bool(
        (exc.headers or {}).get(UPSTREAM_NON_JSON_HEADER)
    )


def _carries_authorization(*header_maps: Mapping[str, str] | None) -> bool:
    """Report whether any of the header mappings already sets ``Authorization``.

    :param header_maps: Header mappings to inspect; ``None`` entries are skipped.
    :return: ``True`` when one of them carries the header under any casing.
    """
    return any(
        name.lower() == "authorization"
        for headers in header_maps
        if headers
        for name in headers
    )


def _sanitize_request_kwargs(
    kwargs: dict[str, Any],
    *,
    extra_sensitive_headers: frozenset[str] = frozenset(),
    extra_sensitive_body_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return a shallow copy of request kwargs with credentials redacted.

    The auth context injects an ``Authorization`` header into ``kwargs`` before
    the request is logged, and some endpoints post credentials in the request
    body (a password-login payload, for example); this scrubs both
    credential-bearing headers and known-sensitive body fields so secrets never
    reach the debug log. Only the returned copy is masked -- the outgoing
    request keeps the real values.

    :param kwargs: The request keyword arguments about to be logged.
    :param extra_sensitive_headers: Additional lowercase header names to mask,
        beyond the always-masked credential headers.
    :param extra_sensitive_body_fields: Additional lowercase body field names to
        mask, beyond the always-masked credential fields.
    :return: A copy safe to log, with sensitive header and body values masked.
    """
    sensitive_headers = _SENSITIVE_HEADERS | extra_sensitive_headers
    sensitive_body_fields = _SENSITIVE_BODY_FIELDS | extra_sensitive_body_fields
    safe = {**kwargs}
    headers = kwargs.get("headers")
    if headers:
        safe["headers"] = {
            key: (_REDACTED_VALUE if key.lower() in sensitive_headers else value)
            for key, value in headers.items()
        }
    for body_key in ("json", "data"):
        body = kwargs.get(body_key)
        if isinstance(body, dict):
            safe[body_key] = {
                key: (
                    _REDACTED_VALUE if key.lower() in sensitive_body_fields else value
                )
                for key, value in body.items()
            }
    return safe


def _raise_stream_line_too_big(size: int, path: str) -> NoReturn:
    """Raise :class:`ValueError` for a stream line larger than the cap.

    :param size: Size in bytes of the offending line or pending buffer.
    :param path: The stream path, included in the error message.
    :raises ValueError: Always — this function never returns.
    """
    msg = (
        f"Stream line exceeded {_MAX_STREAM_LINE_BYTES} bytes "
        f"(size={size}, path={path})"
    )
    raise ValueError(msg)


async def _iter_lines_from_chunks(
    chunks: AsyncIterator[bytes], path: str
) -> AsyncGenerator[bytes, None]:
    """Yield newline-terminated lines from an async iterator of byte chunks.

    Buffer chunks from ``chunks`` and yield each newline-terminated slice with
    the trailing newline byte preserved. Flush any remaining partial line at
    end-of-stream so callers receive the final unterminated chunk. Reject any
    line larger than ``_MAX_STREAM_LINE_BYTES`` to protect consumers from a
    runaway producer.

    Every chunk consumes the newlines it introduced and drops what precedes
    them, so the carried remainder is newline-free when the next chunk arrives.
    Searching only the arriving bytes therefore finds exactly what a search from
    the front finds, at a cost proportional to the chunk rather than to the
    remainder behind it.

    :param chunks: An async iterator producing byte chunks (e.g. from
        ``aiohttp`` ``StreamReader.iter_any()``).
    :param path: The stream path, included in the error message when a single
        line exceeds the cap.
    :yield: Each line as ``bytes`` with its trailing newline preserved; the
        final unterminated chunk is also yielded when the stream ends without
        a newline.
    :raises ValueError: If a single line exceeds ``_MAX_STREAM_LINE_BYTES``.
    """
    buffer = bytearray()
    async for chunk in chunks:
        if not chunk:
            continue
        # Two cursors, not one: the terminator can only be in the arriving
        # bytes, but the line it ends begins at the front of the buffer, where
        # the remainder carried from earlier chunks sits.
        search_from = len(buffer)
        buffer.extend(chunk)
        line_start = 0
        while True:
            newline_pos = buffer.find(b"\n", search_from)
            if newline_pos == -1:
                break
            line_end = newline_pos + 1
            line_size = line_end - line_start
            if line_size > _MAX_STREAM_LINE_BYTES:
                _raise_stream_line_too_big(line_size, path)
            yield bytes(buffer[line_start:line_end])
            line_start = search_from = line_end
        if line_start:
            del buffer[:line_start]
        if len(buffer) > _MAX_STREAM_LINE_BYTES:
            _raise_stream_line_too_big(len(buffer), path)
    if buffer:
        if len(buffer) > _MAX_STREAM_LINE_BYTES:
            _raise_stream_line_too_big(len(buffer), path)
        yield bytes(buffer)


class PendingCloses:
    """Track clients whose :meth:`BaseRemoteAPI.close_when_idle` deferred a close.

    Each owning component keeps its own instance so a shutdown sweep only
    reaches clients that owner itself retired. A client that drains normally
    before shutdown is removed here and is not force-closed again.

    Clients are keyed by identity: :class:`BaseRemoteAPI` compares by field
    values, so a value-keyed set would collapse two retired instances that
    shared an endpoint.

    :meth:`seal` (and :meth:`force_close`) permanently refuse new deferrals so a
    rebind that races shutdown cannot register a client after the sweep has
    already run. :meth:`add` returns ``False`` when sealed; the caller must
    close immediately. Callers that open a *replacement* client during
    shutdown also check :attr:`sealed` and discard the replacement instead of
    publishing it (the active slot is owned by teardown once sealing starts).
    """

    def __init__(self) -> None:
        self._clients: dict[int, BaseRemoteAPI] = {}
        self._sealed = False

    @property
    def sealed(self) -> bool:
        """Return whether shutdown has sealed this collection against new deferrals."""
        return self._sealed

    def seal(self) -> None:
        """Reject further deferrals; late retirements must close immediately."""
        self._sealed = True

    def add(self, client: "BaseRemoteAPI") -> bool:
        """Remember ``client`` until it drains or :meth:`force_close` runs.

        :param client: The client whose close was deferred.
        :return: ``True`` when registered for deferred close, ``False`` when
            this collection is already sealed and the caller must close
            ``client`` now (use :meth:`track` if that immediate close must
            stay discoverable on failure).
        """
        if self._sealed:
            return False
        self._clients[id(client)] = client
        return True

    def track(self, client: "BaseRemoteAPI") -> None:
        """Remember ``client`` until a successful close removes it.

        Unlike :meth:`add`, this registers even when sealed so an immediate
        close that fails or is cancelled stays discoverable for a later
        :meth:`force_close`. Callers that discard a late replacement use this
        before awaiting :meth:`~BaseRemoteAPI.close`.
        """
        self._clients[id(client)] = client

    def discard(self, client: "BaseRemoteAPI") -> None:
        """Drop ``client`` after a normal deferred close.

        :param client: The client that closed (or was force-closed).
        """
        self._clients.pop(id(client), None)

    async def force_close(self) -> None:
        """Seal, then close every still-deferred client.

        Sealing happens before any ``await`` so a concurrent rebind that tries
        to register after this returns cannot reintroduce a leak.
        Only the hold-triggered close flag is cleared before ``close`` so a
        concurrent :meth:`BaseRemoteAPI.hold` finally will not start another
        close from ``_close_when_idle``. The client stays registered here until
        a *successful* session close removes it, so a failed or cancelled close
        remains discoverable for a later sweep. If that hold already began
        tearing down the session, :meth:`BaseRemoteAPI.close` joins the
        in-progress operation so this sweep still waits for the socket to
        finish closing. Safe to call when empty.
        """
        self._sealed = True
        clients = list(self._clients.values())
        if not clients:
            return
        for client in clients:
            client.clear_hold_triggered_close()
        results = await asyncio.gather(
            *(client.close() for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results, strict=False):
            if isinstance(result, Exception):
                client.logger.warning(
                    "Error force-closing client %s: %s",
                    client.redacted_base_url,
                    result,
                )


class BaseRemoteAPI(BaseCaseInsensitiveModel):
    """Base class for interacting with external APIs.

    Provides foundational functionality for making HTTP requests, handling SSL
    configurations, and managing request paths and headers.

    :param endpoint: The base URL for the external API endpoint.
    :type endpoint: CredentialHttpUrl
    :param verify_ssl: Whether to verify SSL certificates. Defaults to True.
    :type verify_ssl: bool
    :param ssl_cafile: Path to the SSL certificate authority file. Defaults to None.
    :type ssl_cafile: RelativeFilePathField | None
    :param ssl_keyfile: Path to the SSL key file. Defaults to None.
    :type ssl_keyfile: RelativeFilePathField | None
    :param ssl_certfile: Path to the SSL certificate file. Defaults to None.
    :type ssl_certfile: RelativeFilePathField | None
    :param logger_name: Name to use for the logger. Defaults to `__name__`.
    :type logger_name: str
    :cvar CONNECTIVITY_CHECK_PATH: Lightweight route hit by
        :meth:`RemoteAPI.check_connectivity` for a reachability probe. Never
        enters the client-registry key or model serialization. Override per
        client, or pass an explicit ``path`` to ``check_connectivity``.
    """

    CONNECTIVITY_CHECK_PATH: ClassVar[str] = "/"
    endpoint: CredentialHttpUrl = Field(..., frozen=True)
    verify_ssl: bool = Field(default=True, frozen=True)
    ssl_cafile: RelativeFilePathField | None = Field(None, frozen=True)
    ssl_keyfile: RelativeFilePathField | None = Field(None, frozen=True)
    ssl_certfile: RelativeFilePathField | None = Field(None, frozen=True)
    logger_name: str = __name__
    _session: ClientSession | None = None
    _in_flight: int = 0
    _close_when_idle: bool = False
    _pending_closes: PendingCloses | None = None
    # Set while :meth:`__aexit__` runs so a concurrent :meth:`close` (e.g.
    # shutdown ``force_close``) awaits the same session teardown instead of
    # racing a second one. Cleared on failure so a later close can retry;
    # left completed on success so late joiners still observe the finish.
    _close_done: asyncio.Future[None] | None = None
    _extra_headers: ContextVar[dict[str, str] | None] = PrivateAttr(
        default_factory=lambda: ContextVar("api_extra_headers", default=None)
    )
    _extra_sensitive_headers: ContextVar[frozenset[str]] = PrivateAttr(
        default_factory=lambda: ContextVar(
            "api_extra_sensitive_headers", default=frozenset()
        )
    )
    _extra_sensitive_body_fields: ContextVar[frozenset[str]] = PrivateAttr(
        default_factory=lambda: ContextVar(
            "api_extra_sensitive_body_fields", default=frozenset()
        )
    )
    _suppress_response_log: ContextVar[bool] = PrivateAttr(
        default_factory=lambda: ContextVar("api_suppress_response_log", default=False)
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a subclass that declares its own ``base_url``.

        The JSON redaction rides on the return annotation of the computed field
        declared here. A redeclaration shadows it and puts the endpoint password
        back into every dump of that class, so the failure is raised at import
        rather than left for a dump to discover.

        :param kwargs: Class keyword arguments, forwarded unchanged.
        :raises TypeError: When the subclass body defines ``base_url``.
        """
        if "base_url" in cls.__dict__:
            msg = (
                f"{cls.__qualname__} must override _compute_base_url, not "
                "base_url: redeclaring base_url drops its credential redaction."
            )
            raise TypeError(msg)
        super().__init_subclass__(**kwargs)

    def __hash__(self) -> int:
        """Compute the hash based on the endpoint and SSL configuration.

        :return: The hash value of the remote API instance.
        :rtype: int
        """
        return hash(
            (
                self.endpoint,
                self.verify_ssl,
                self.ssl_cafile,
                self.ssl_keyfile,
                self.ssl_certfile,
            )
        )

    def _cookie_jar(self) -> AbstractCookieJar | None:
        """Return the cookie jar the client session is built with.

        ``None`` keeps aiohttp's default jar, which stores response cookies and
        sends them on later requests to the same host. A client whose upstream
        treats a cookie as a credential overrides this so no call inherits a
        cookie another call received.

        :return: The jar, or ``None`` for aiohttp's default.
        """
        return None

    async def __aenter__(self) -> Self:
        """Enter the asynchronous context manager.

        Initializes the aiohttp `ClientSession` if not already present.

        :return: The `BaseRemoteAPI` instance.
        :rtype: BaseRemoteAPI
        """
        if getattr(self, "_session", None) is None:
            # A prior close may have left ``_close_done`` completed; clear it
            # so a later close on this reopened session is not treated as a
            # join on the finished teardown.
            self._close_done = None
            self.logger.debug("Opening ClientSession for %s", self.redacted_base_url)
            connector = TCPConnector(
                ssl=self.ssl_context,
                enable_cleanup_closed=True,
                limit=25,
                limit_per_host=10,
                ttl_dns_cache=300,
                keepalive_timeout=15,
            )
            timeout = ClientTimeout(total=300, connect=5, sock_connect=5, sock_read=120)
            self._session = ClientSession(
                base_url=self.session_base_url,
                headers=self.headers or None,
                json_serialize=json_serializer,
                connector=connector,
                timeout=timeout,
                cookie_jar=self._cookie_jar(),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the asynchronous context manager.

        Closes the aiohttp ``ClientSession`` if it was initialized. Concurrent
        callers join the in-progress teardown via a shielded ``_close_done``
        wait so cancelling one waiter cannot cancel the shared close state.
        If the owning close is cancelled, a joiner that is not itself
        cancelling takes over teardown so the session does not stay open.
        Deferred-close bookkeeping is cleared only after a *successful*
        session close, so a failed or cancelled teardown stays discoverable
        on :class:`PendingCloses` and a later :meth:`close` can retry.

        :param exc_type: The exception type, if any.
        :type exc_type: type[BaseException] | None
        :param exc_val: The exception value, if any.
        :type exc_val: BaseException | None
        :param exc_tb: The traceback, if any.
        :type exc_tb: TracebackType | None
        """
        if self._close_done is not None:
            # Shield so cancelling this waiter does not cancel the shared
            # future other close() / force_close joiners still need.
            # A later close that finds the teardown already finished still
            # logs the already-closed line (password-redacted).
            already_closed = self._close_done.done()
            shared = self._close_done
            try:
                await asyncio.shield(shared)
            except asyncio.CancelledError:
                # The owning close may have published CancelledError into
                # ``shared`` without this task being cancelled. Take over
                # teardown so the session does not stay open; only re-raise
                # when we ourselves are being cancelled.
                task = asyncio.current_task()
                if task is not None and task.cancelling() == 0:
                    return await self.__aexit__(exc_type, exc_val, exc_tb)
                raise
            if already_closed:
                self.logger.debug(
                    "ClientSession already closed for %s", self.redacted_base_url
                )
            return None

        done = asyncio.get_running_loop().create_future()
        self._close_done = done
        try:
            if self._session and not self._session.closed:
                self.logger.debug(
                    "Closing ClientSession for %s", self.redacted_base_url
                )
                await self._session.close()
            else:
                self.logger.debug(
                    "ClientSession already closed for %s", self.redacted_base_url
                )
            self._session = None
        except BaseException as exc:
            # Leave the client on PendingCloses and clear ``_close_done`` so a
            # later close (e.g. force_close) can retry; publish the failure to
            # anyone already joined on ``done``.
            self._close_done = None
            if not done.done():
                done.set_exception(exc)
                # Mark retrieved when nobody joined, so asyncio does not warn
                # about an orphaned future exception on this failure path.
                done.exception()
            raise
        else:
            # Only after a successful close: drop pending tracking and wake
            # joiners. Keep the client discoverable for the whole await above.
            self.clear_deferred_close()
            if not done.done():
                done.set_result(None)

    async def open(self) -> Self:
        """Open the asynchronous context manager.

        Initializes the aiohttp `ClientSession` if not already present.

        :return: The `BaseRemoteAPI` instance.
        :rtype: BaseRemoteAPI
        """
        return await self.__aenter__()

    async def close(self) -> None:
        """Close the asynchronous context manager.

        Closes the aiohttp ``ClientSession`` if it was initialized. If a close is
        already in progress (a draining :meth:`hold`, or another caller), waits
        for that teardown to finish instead of starting a second one. A failed
        close leaves the client retryable for a later call.
        """
        await self.__aexit__(None, None, None)

    def clear_deferred_close(self) -> None:
        """Drop deferred-close bookkeeping without closing the session.

        Called after a *successful* session close (from :meth:`__aexit__`) so
        the owner no longer tracks this client for a shutdown sweep.
        :meth:`PendingCloses.force_close` does *not* call this before awaiting
        :meth:`close`; it only clears the hold-triggered flag via
        :meth:`clear_hold_triggered_close` so a failed close stays discoverable.
        """
        pending = self._pending_closes
        if pending is not None:
            pending.discard(self)
            self._pending_closes = None
        self._close_when_idle = False

    def clear_hold_triggered_close(self) -> None:
        """Clear the hold-triggered close flag without leaving :class:`PendingCloses`.

        :meth:`PendingCloses.force_close` calls this before awaiting
        :meth:`close` so a concurrent :meth:`hold` finally no longer sees
        ``_close_when_idle`` and starts a redundant close; :meth:`close` is
        still joinable via ``_close_done`` if teardown already began. The
        client stays registered on the owner's pending set until a successful
        session close removes it.
        """
        self._close_when_idle = False

    def remember_pending_close(self, pending: PendingCloses) -> bool:
        """Register on ``pending`` so a shutdown sweep can still force-close us.

        Owners call this under their eviction lock *before* awaiting
        :meth:`close_when_idle`, so a concurrent seal/sweep cannot miss a
        client that has left the live cache but not yet deferred. Idempotent
        when already registered on ``pending``.

        :param pending: The calling owner's deferred-close collection.
        :return: ``True`` when registered for deferred close, ``False`` when
            ``pending`` is already sealed and the caller must close this
            client now.
        """
        if not pending.add(self):
            return False
        self._pending_closes = pending
        return True

    def track_pending_close(self, pending: PendingCloses) -> None:
        """Register on ``pending`` even when sealed, for an immediate close.

        Used when discarding a late replacement: the close runs now, but the
        owner must still find us if that close fails or is cancelled.
        """
        pending.track(self)
        self._pending_closes = pending

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[Self, None]:
        """Count the caller as an in-flight consumer for the duration of the block.

        A consumer that resolved this client keeps it for every call it makes,
        including the ones it issues after an earlier response finished, so the
        accounting unit is the hold rather than the individual HTTP call. The
        releaser that drops the count to zero performs a close that
        :meth:`close_when_idle` deferred. That close stays discoverable on the
        owner's :class:`PendingCloses` until the session teardown finishes, so
        a concurrent shutdown sweep can await the same operation.

        The release runs during cancellation too, when the consuming task is
        cancelled by a client disconnecting mid-response, so the deferred close
        is shielded; a bare ``await`` would leave it interrupted with the
        session still open.

        :return: This client, unchanged.
        """
        self._in_flight += 1
        try:
            yield self
        finally:
            self._in_flight -= 1
            if not self._in_flight and self._close_when_idle:
                await asyncio.shield(self.close())

    async def close_when_idle(self, pending: PendingCloses | None = None) -> None:
        """Close the session now when idle, or once the last consumer releases.

        Unlike :meth:`close`, which closes unconditionally, this waits on the
        consumers registered by :meth:`hold`, and imposes no deadline on them.
        Callers that retire a client on a settings rebind use this so an
        in-flight stream or download is not cut off mid-response.

        When the close is deferred and ``pending`` is given, this client is
        registered there so the owner's shutdown path can still force-close it
        if the holder never unwinds. If ``pending`` is already sealed (shutdown
        has begun), the close runs immediately instead of being deferred past
        the sweep.

        :param pending: The calling owner's deferred-close collection, or
            ``None`` when the caller does not track retirements for shutdown.
        """
        if self._in_flight:
            self._close_when_idle = True
            if pending is not None and not self.remember_pending_close(pending):
                self._close_when_idle = False
                await self.close()
                return
            return
        await self.close()

    def set_extra_headers(self, extra_headers: dict[str, str] | None) -> Token:
        """Set extra headers to be included in API requests.

        :param extra_headers: A mapping of additional headers to include in requests.
        :type extra_headers: dict[str, str] | None
        :return: A token that can be used to reset the context variable.
        :rtype: Token
        """
        return self._extra_headers.set(extra_headers)

    def reset_extra_headers(self, token: Token) -> None:
        """Reset the extra headers context variable to a previous state.

        :param token: The token returned by `set_extra_headers`.
        :type token: Token
        """
        self._extra_headers.reset(token)

    @contextmanager
    def extra_headers(self, extra_headers: dict[str, str] | None) -> Generator[Self]:
        """Define context manager to temporarily set extra headers for API requests.

        :param extra_headers: A mapping of additional headers to include in requests.
        :type extra_headers: dict[str, str] | None
        :yield: The `BaseRemoteAPI` instance with the extra headers set.
        :rtype: Generator[Self]
        """
        token = self.set_extra_headers(extra_headers)
        try:
            yield self
        finally:
            self.reset_extra_headers(token)

    @contextmanager
    def redact_headers(self, names: Iterable[str]) -> Generator[Self]:
        """Mask additional request headers in the debug request log for the call.

        Register case-insensitive header names whose values must be redacted in
        the request-log line for the duration of the call, on top of the
        always-masked credential headers. Use this to hide a custom-named
        credential header a caller injects via :meth:`extra_headers`.

        :param names: Header names to mask, compared case-insensitively.
        :yield: The instance with the extra redaction set applied.
        """
        token = self._extra_sensitive_headers.set(
            self._extra_sensitive_headers.get() | frozenset(n.lower() for n in names)
        )
        try:
            yield self
        finally:
            self._extra_sensitive_headers.reset(token)

    @contextmanager
    def redact_body_fields(self, names: Iterable[str]) -> Generator[Self]:
        """Mask additional request-body fields in the debug request log for the call.

        Register case-insensitive body keys whose values must be redacted in the
        request-log line for the duration of the call, on top of the
        always-masked credential fields. Use this to hide a custom-named
        credential a caller posts in a JSON or form body.

        :param names: Body field names to mask, compared case-insensitively.
        :yield: The instance with the extra redaction set applied.
        """
        token = self._extra_sensitive_body_fields.set(
            self._extra_sensitive_body_fields.get()
            | frozenset(name.lower() for name in names)
        )
        try:
            yield self
        finally:
            self._extra_sensitive_body_fields.reset(token)

    @contextmanager
    def suppress_response_log(self) -> Generator[Self]:
        """Withhold the response body from the transport's own log records for the call.

        Register that the parsed response body must not reach the log for the
        duration of the call. Use this where the caller keeps only the values it
        names itself and the rest of the body is data it must neither retain nor
        return, so the body must not outlive the request in a log line either.

        Guards the two response-logging sites in :meth:`request` and nothing
        else: :meth:`stream` logs no response body of its own, so a caller
        wrapping it gains no guarantee here. The second of the two sites reports
        a non-JSON response and logs the body's decoded text, so suppression
        there withholds the upstream's own error page from the exception line.

        Unlike :meth:`redact_headers` and :meth:`redact_body_fields`, which
        accumulate onto the set an enclosing block registered, this flag has
        nothing to union; an enclosing suppression survives an inner block's
        exit unchanged.

        :return: The instance with response logging withheld.
        """
        token = self._suppress_response_log.set(True)
        try:
            yield self
        finally:
            self._suppress_response_log.reset(token)

    @cached_property
    def logger(self) -> logging.Logger:
        """Return logger object to use.

        :return: The logger object to use, created with the name set in
            `self.logger_name`.
        :rtype: logging.Logger
        """
        return logging.getLogger(self.logger_name)

    @property
    def session(self) -> ClientSession | None:
        """Get the ClientSession used in requests.

        :return: The ClientSession used in requests, or ``None`` before the
            client is opened and after it is closed — which is what callers
            test for to decide whether to enter it.
        """
        return self._session

    @session.setter
    def session(self, session: ClientSession) -> None:
        """Set the ClientSession used in requests."""
        self._session = session

    @property
    def ssl_context(self) -> SSLContext | bool:
        """Initialize and return the SSL context for secure connections.

        Configures the SSL context based on the provided SSL certificate files
        and verification settings. If `verify_ssl` is False, returns False to disable
        SSL verification.

        :return: The configured SSL context for HTTPS connections.
        :rtype: SSLContext | bool
        """
        return (
            self.create_ssl_context(
                self.ssl_cafile, self.ssl_certfile, self.ssl_keyfile
            )
            if self.verify_ssl
            else False
        )

    @computed_field
    @property
    def base_path(self) -> str:
        """Compute and return the base path of the API endpoint.

        Strips leading and trailing slashes from the endpoint path.

        :return: The base path of the API endpoint.
        :rtype: str
        """
        return "/" + self.endpoint.path.strip("/")

    def _compute_base_url(self) -> str:
        """Return the endpoint URL with the base path removed.

        The extension point for a subclass whose base URL is derived
        differently. Overriding this rather than :attr:`base_url` is what keeps
        the redaction in one place: a subclass that redeclared ``base_url`` as
        its own computed field would shadow the return annotation the
        serializer rides on, putting the endpoint password back into every
        dump.

        The base path is removed from the path component alone. Removing every
        occurrence of it from the whole URL also rewrites a query value that
        repeats it, and a mangled URL is one the redaction helper can refuse to
        parse.

        :return: The base URL of the API endpoint, credential included.
        """
        parsed = urlsplit(str(self.endpoint))
        path = parsed.path.rstrip("/")
        if self.base_path.strip("/") and path.endswith(self.base_path):
            path = path[: -len(self.base_path)]
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                path,
                parsed.query,
                parsed.fragment,
            )
        ).rstrip("/")

    @computed_field
    @property
    def base_url(self) -> Annotated[str, CREDENTIAL_URL_STR_JSON_SERIALIZER]:
        """Compute and return the base URL without the base path.

        The live value keeps whatever credential the endpoint embeds, because
        outbound authentication is derived from it. Only the JSON rendering is
        masked, and a dump passing
        :data:`~app.core.utils.fields.PRESERVE_CREDENTIALS_CONTEXT` still sees
        the real one.

        Subclasses customise :meth:`_compute_base_url`, never this property.

        :return: The base URL of the API endpoint.
        """
        return self._compute_base_url()

    @property
    def redacted_base_url(self) -> str:
        """Return :attr:`base_url` with any embedded password masked, for logging.

        A redaction failure collapses to the bare mask rather than propagating,
        so this is safe to call from an error-reporting path, where raising
        would replace the failure being reported with a parse error. Unlike
        :attr:`endpoint`, which is validated on the way in, this value comes
        from an overridable hook and is not guaranteed to parse.

        :return: The base URL with its password replaced by
            :data:`~app.core.utils.fields.CREDENTIAL_URL_MASK`, or the mask
            alone when the URL cannot be parsed.
        """
        try:
            return redact_credential_url(self.base_url)
        except ValueError:
            return CREDENTIAL_URL_MASK

    @property
    def session_base_url(self) -> str:
        """Return the base URL the aiohttp session is built from, minus userinfo.

        aiohttp derives basic auth from a URL's userinfo and then refuses any
        request that also carries an explicit ``Authorization`` header, raising
        ``ValueError`` before a connection is opened. Every caller that presents
        a header of its own — a forwarded user token via :meth:`RemoteAPI.auth`,
        or a client class whose :attr:`headers` names an API key — would hit
        that on a credential-bearing endpoint, so the userinfo is kept out of
        the session URL and re-applied per request by
        :meth:`_unopposed_endpoint_credential` only when nothing competes
        with it.

        :return: :attr:`base_url` with any userinfo segment removed.
        """
        return strip_credential_url_userinfo(self.base_url)

    @property
    def _endpoint_credential_header(self) -> str | None:
        """Return the basic ``Authorization`` value the endpoint's userinfo encodes.

        Reproduces the header aiohttp would have derived from the URL, so a
        client with no competing header puts the same bytes on the wire as
        before. Reads :attr:`base_url` rather than :attr:`endpoint` so it stays
        the exact complement of what :attr:`session_base_url` removes, including
        for a subclass that overrides either one. The segments are
        percent-decoded and then latin-1 encoded, the two steps aiohttp applies
        to a URL-embedded credential.

        :return: The encoded header value, or ``None`` when the base URL carries
            no userinfo.
        """
        parsed = urlparse(self.base_url)
        if not parsed.username and not parsed.password:
            return None
        return encode_basic_auth(
            unquote(parsed.username or ""), unquote(parsed.password or ""), "latin1"
        )

    def _unopposed_endpoint_credential(
        self, headers: Mapping[str, str] | None
    ) -> str | None:
        """Return the endpoint's embedded credential unless a header competes with it.

        An ``Authorization`` header the call site set — directly, via
        :meth:`extra_headers`, or through the client's own :attr:`headers` —
        wins over the endpoint's embedded credential. The two cannot share a
        request: HTTP carries one ``Authorization`` header, and the explicit one
        is the narrower credential (the identity this call acts as, or the key
        the remote API itself requires), while the URL userinfo is a
        configuration-wide default. :class:`NomadExecutor` already resolves the
        same conflict this way.

        :param headers: The per-call headers assembled for the outgoing request.
        :return: The credential to send, or ``None`` when there is none to send
            or a header already occupies the slot.
        """
        if _carries_authorization(headers, self.headers):
            return None
        return self._endpoint_credential_header

    @property
    def headers(self) -> dict[str, str]:
        """Return the headers to be used in API requests.

        By default, an empty dict is returned.

        :return: A dictionary containing the headers for API requests.
        :rtype: dict[str, str]
        """
        return {}

    def prepare_path(self, path: str) -> str:
        """Prepare and return the full endpoint path.

        Constructs the full URL path by combining the base path with the provided path.

        :param path: The API endpoint path to request.
        :type path: str
        :return: The full API path.
        :rtype: str
        """
        if self.base_path == "/":
            return urljoin(self.base_path, path)
        trailing_slash = path.endswith("/")
        path = path.strip("/")
        base_path = self.base_path + "/" if path and self.base_path else self.base_path
        path = urljoin(base_path, path)
        return path + "/" if trailing_slash else path

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> AsyncGenerator[ClientResponse, None]:
        """Define internal method to perform an HTTP request.

        Yields the aiohttp `ClientResponse` object for further processing.

        :param method: The HTTP method to use for the request.
        :type method: str
        :param path: The API endpoint path to request.
        :type path: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :yield: The aiohttp `ClientResponse` object.
        :rtype: AsyncGenerator[ClientResponse, None]
        """
        prepared_path = self.prepare_path(path)
        if extra_headers := self._extra_headers.get():
            kwargs["headers"] = kwargs.pop("headers", {}) | extra_headers
        correlation_id = correlation_id_var.get()
        if correlation_id != "-":
            kwargs["headers"] = kwargs.pop("headers", {}) | {
                "X-Correlation-ID": correlation_id
            }
        if credential := self._unopposed_endpoint_credential(kwargs.get("headers")):
            kwargs["headers"] = kwargs.pop("headers", {}) | {
                "Authorization": credential
            }
        self.logger.debug(
            "RemoteAPI (%s): Sending %s request to %s with kwargs %s",
            redact_credential_url(str(self.endpoint)),
            method,
            path,
            _sanitize_request_kwargs(
                kwargs,
                extra_sensitive_headers=self._extra_sensitive_headers.get(),
                extra_sensitive_body_fields=self._extra_sensitive_body_fields.get(),
            ),
        )
        async with (
            self.hold(),
            self._session.request(method, prepared_path, **kwargs) as response,
        ):
            yield response

    @staticmethod
    def _raise_stream_http_error(
        status_code: int,
        *,
        detail: Any,
        headers: dict[str, str] | None = None,
    ) -> NoReturn:
        """Raise the mapped project exception (or bare HTTPException) for a failed stream.

        :param status_code: The upstream HTTP error status.
        :param detail: The error detail payload for the raised exception.
        :param headers: Optional response headers to preserve.
        """
        raise exception_for_status(
            status_code, detail=detail, headers=headers
        ) from None

    async def stream_chunks(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> AsyncGenerator[bytes, None]:
        """Perform a streaming HTTP request and yield raw byte chunks as they arrive.

        Use this for binary, gzip, or any non-line-oriented payload. For NDJSON
        log streams, prefer :meth:`stream`, which buffers chunks across newline
        boundaries so each yield is a single line.

        :param path: The API endpoint path to request.
        :type path: str
        :param method: The HTTP method to use for the request. Defaults to "GET".
        :type method: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :yield: Raw byte chunks from the response body in arrival order.
        :rtype: AsyncGenerator[bytes, None]
        :raises HTTPGoneException: If the upstream API returns HTTP 410 (e.g. task data
            gone from the Nomad executor). Callers may use ``isinstance(..., HTTPGoneException)``
            or ``exc.status_code == 410`` without inspecting the status from a generic
            :class:`fastapi.HTTPException`.
        :raises HTTPException: For other error responses (same pattern as
            :meth:`RemoteAPI.request`). Without handling non-success statuses, error JSON
            bodies would be yielded as stream chunks; the caller would then continue (e.g.
            poll ``GET /history/{id}``), and later aiohttp requests could surface unrelated
            ``Connection timeout to host`` errors.
        """
        self.logger.debug("Stream started path=%s method=%s", path, method)
        try:
            async with self._request(method, path, **kwargs) as response:
                if response.status >= status.HTTP_400_BAD_REQUEST:
                    detail_key = getattr(self, "error_detail_key", "detail")
                    code_key = getattr(self, "error_code_key", None)
                    try:
                        response_data = await response.json()
                    except (ContentTypeError, ValueError):
                        text = await response.text()
                        fallback = text or "An unexpected error occurred on the server."
                        self._raise_stream_http_error(
                            response.status,
                            detail=fallback,
                            headers={UPSTREAM_NON_JSON_HEADER: "1"},
                        )
                    error_body = (
                        response_data if isinstance(response_data, Mapping) else {}
                    )
                    error_detail = error_body.get(
                        detail_key, "An unexpected error occurred on the server."
                    )
                    error_headers = None
                    if code_key and (error_code := error_body.get(code_key)):
                        error_headers = {"X-Error-Code": str(error_code)}
                    self._raise_stream_http_error(
                        response.status,
                        detail=error_detail,
                        headers=error_headers,
                    )
                async for chunk in response.content.iter_any():
                    if chunk:
                        yield chunk
            self.logger.debug("Stream ended normally path=%s method=%s", path, method)
        except HTTPException:
            raise
        except Exception as exc:
            self.logger.warning(
                "Stream error path=%s method=%s: %s",
                path,
                method,
                exc,
                exc_info=True,
            )
            raise

    async def stream(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> AsyncGenerator[bytes, None]:
        """Perform a streaming HTTP request and yield response content one line at a time.

        Buffer chunks across newline boundaries and yield each newline-terminated
        slice with its trailing newline byte preserved. Flush any remaining partial
        line at end-of-stream. Use this for NDJSON log streams; for binary or
        non-line payloads, use :meth:`stream_chunks`.

        :param path: The API endpoint path to request.
        :type path: str
        :param method: The HTTP method to use for the request. Defaults to "GET".
        :type method: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :yield: Each line of response content as ``bytes`` with its trailing
            newline preserved; the final unterminated chunk is also yielded
            when the body does not end with a newline.
        :rtype: AsyncGenerator[bytes, None]
        :raises HTTPGoneException: See :meth:`stream_chunks`.
        :raises HTTPException: See :meth:`stream_chunks`.
        :raises ValueError: If a single line exceeds ``_MAX_STREAM_LINE_BYTES``.
        """
        try:
            async for line in _iter_lines_from_chunks(
                self.stream_chunks(path, method, **kwargs), path
            ):
                yield line
        except ValueError as exc:
            self.logger.warning(
                "Stream line cap exceeded path=%s method=%s: %s",
                path,
                method,
                exc,
                exc_info=True,
            )
            raise

    @staticmethod
    @lru_cache(maxsize=8)
    def create_ssl_context(
        cafile: RelativeFilePathField | None = None,
        certfile: RelativeFilePathField | None = None,
        keyfile: RelativeFilePathField | None = None,
    ) -> SSLContext:
        """Initialize and return the SSL context for secure connections.

        Configures the SSL context based on the provided SSL certificate files
        parameters.

        :param cafile: The path to the CA certificate file.
        :type cafile: RelativeFilePathField | None
        :param certfile: The path to the certificate file.
        :type certfile: RelativeFilePathField | None
        :param keyfile: The path to the certificate key file.
        :type keyfile: RelativeFilePathField | None
        :return: The configured SSL context for HTTPS connections.
        :rtype: SSLContext
        """
        context = create_default_context(cafile=cafile)
        if certfile:
            context.load_cert_chain(
                certfile=certfile,
                keyfile=keyfile,
            )
        return context


class CredentialHeaderMixin(BaseRemoteAPI):
    """Emit a persistent ``Authorization`` header for :class:`BaseRemoteAPI` subclasses.

    Apply leftmost in the MRO (e.g. ``CredentialHeaderMixin, RemoteAPI``) so
    :attr:`headers` wins over the base. Formats ``Authorization`` from
    :attr:`_authorization_scheme` and :attr:`_credential_value` without each
    client reimplementing the string.

    This base declares no settings fields: subclasses with a derived credential
    and a fixed scheme (e.g. Casdoor Basic) override the two properties only.
    Clients that store a secret and a configurable scheme use
    :class:`StoredCredentialHeaderMixin` instead.

    Clients that deliberately carry no session-lifetime credential — GrafanaSDK,
    bare :class:`RemoteAPI` instances — simply do not apply this mixin.
    """

    @property
    def _authorization_scheme(self) -> str:
        """Return the scheme spliced ahead of the credential in ``Authorization``.

        Defaults to ``Bearer`` so a future client that supplies a credential but
        forgets to set a scheme still builds a valid header rather than failing
        on every request. Subclasses with a fixed scheme (e.g. Casdoor Basic)
        override this. Stored-credential clients inherit
        :class:`StoredCredentialHeaderMixin`, which reads ``auth_scheme``.

        :return: The authentication scheme token.
        """
        return "Bearer"

    @property
    def _credential_value(self) -> str | None:
        """Return the plain credential for the ``Authorization`` header, or ``None``.

        Subclasses with a derived credential override this. Stored-credential
        clients inherit :class:`StoredCredentialHeaderMixin`, which reads
        ``api_key`` (empty secret counts as unset).

        :return: The plain credential when one should be sent, else ``None``.
        """
        return None

    @property
    def headers(self) -> dict[str, str]:
        """Return request headers, adding ``Authorization`` when a credential is set.

        :return: The inherited headers, plus ``Authorization`` when
            :attr:`_credential_value` is non-``None``.
        """
        base_headers = super().headers
        credential = self._credential_value
        if credential is None:
            return base_headers
        return {
            **base_headers,
            "Authorization": f"{self._authorization_scheme} {credential}",
        }


class StoredCredentialHeaderMixin(CredentialHeaderMixin):
    """Back the ``Authorization`` header with a stored ``api_key`` and ``auth_scheme``.

    Use for clients whose credential is session-lifetime config (PMM, Nomad).
    Do not use for clients that derive the credential or hard-code the scheme
    (Casdoor): those fields would become operator settings that either do nothing
    or can break authentication.
    """

    api_key: AuthCredentialSecretStr | None = None
    auth_scheme: AuthSchemeStr = "Bearer"

    @property
    def _authorization_scheme(self) -> str:
        """Return the configured ``auth_scheme``.

        :return: The authentication scheme token.
        """
        return self.auth_scheme

    @property
    def _credential_value(self) -> str | None:
        """Return the plain ``api_key``, or ``None`` when unset.

        An empty secret counts as unset: :class:`~pydantic.SecretStr` defines
        ``__len__``, so a blank value is falsy and would otherwise emit a header
        with no credential.

        :return: The plain API key when a non-empty one is configured, else
            ``None``.
        """
        return self.api_key.get_secret_value() if self.api_key else None


class RemoteAPI(BaseRemoteAPI):
    """Interact with external services via HTTP requests.

    Extends `BaseRemoteAPI` to include authentication mechanisms and provides
    methods for standard HTTP operations (GET, POST, PUT, PATCH, DELETE) returning
    parsed JSON, or ``None`` when the response has no body (for example HTTP 204).

    :param endpoint: The base URL for the external API endpoint.
    :type endpoint: CredentialHttpUrl
    :param verify_ssl: Whether to verify SSL certificates. Defaults to True.
    :type verify_ssl: bool
    :param ssl_cafile: Path to the SSL certificate authority file. Defaults to None.
    :type ssl_cafile: RelativeFilePathField | None
    :param ssl_keyfile: Path to the SSL key file. Defaults to None.
    :type ssl_keyfile: RelativeFilePathField | None
    :param ssl_certfile: Path to the SSL certificate file. Defaults to None.
    :type ssl_certfile: RelativeFilePathField | None
    :param logger_name: Name to use for the logger. Defaults to `__name__`.
    :type logger_name: str
    :param error_detail_key: The key to expect error details to be. Defaults to
        "detail".
    :type error_detail_key: NonEmptyStr
    :param error_code_key: The key to expect error codes to be, or None if no error
        code is expected. Defaults to None.
    :type error_code_key: NonEmptyStr | None
    """

    error_detail_key: NonEmptyStr = "detail"
    error_code_key: NonEmptyStr | None = None

    @property
    def headers(self) -> dict[str, str]:
        """Return the default headers to be used in API requests.

        Includes content type and accept headers.

        :return: A dictionary containing the headers for API requests.
        :rtype: dict[str, str]
        """
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @contextmanager
    def auth(self, api_key: str, auth_scheme: str = "Bearer") -> Generator[Self]:
        """Define context manager to temporarily set authentication for API requests.

        :param api_key: The API key to use for authentication.
        :param auth_scheme: The authentication scheme to use. Defaults to "Bearer".
        :return: The `RemoteAPI` instance with authentication headers set.
        """
        with self.extra_headers(
            {"Authorization": f"{auth_scheme} {api_key}".strip()}
        ) as api:
            yield api

    async def check_connectivity(
        self, service: str, *, path: str | None = None
    ) -> ConnectivityResult:
        """Probe the endpoint and return a normalized connectivity result.

        Issue a lightweight ``GET`` against ``path`` (or
        :attr:`CONNECTIVITY_CHECK_PATH`) under a short bounded timeout and map
        the outcome to one of the :class:`ConnectivityStatusEnum` states:
        reachable, authentication failure, unreachable, SSL verification
        failure, or timeout. Any failure is captured and classified -- this
        method never raises -- so a single probe can be fanned out safely
        alongside others.

        The result carries only fixed, secret-free ``detail`` text; the
        configured API key and any credentials embedded in the endpoint URL are
        never echoed.

        :param service: Stable identifier of the probed service (e.g. ``"pmm"``).
        :type service: str
        :param path: Optional override for the probe route. Defaults to
            :attr:`CONNECTIVITY_CHECK_PATH`.
        :type path: str | None
        :return: The normalized connectivity result.
        :rtype: ConnectivityResult
        """
        probe_path = path if path is not None else self.CONNECTIVITY_CHECK_PATH
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                await self.get(probe_path)
        except Exception as exc:  # noqa: BLE001 -- classified, never re-raised
            return build_connectivity_result(service, classify_connectivity_error(exc))
        return build_connectivity_result(service, ConnectivityStatusEnum.REACHABLE)

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform an HTTP request and return the JSON response.

        :param method: The HTTP method to use for the request.
        :param path: The API endpoint path to request.
        :param kwargs: Additional keyword arguments to pass to the request.
        :return: The JSON response as a Python object, or ``None`` when the
            server returns HTTP 204 No Content (no response body).
        :raises HTTPException: If the request returns an error response -- the
            project exception mapped to the status (a subclass of
            :class:`fastapi.HTTPException`), or a bare :class:`fastapi.HTTPException`
            when the status is unmapped or a non-JSON 404 must stay unmapped.
            A ``3xx`` response also raises when the caller passed
            ``allow_redirects=False``: the redirect was not followed, so the
            status is reported rather than treated as a result.
        :raises aiohttp.ClientError: If the underlying
            :class:`aiohttp.ClientSession` request fails during transport or
            connection handling.
        :raises TimeoutError: If the request's effective timeout is exceeded,
            whether from the session's configured :class:`aiohttp.ClientTimeout` or
            a per-request ``timeout=`` override.
        :raises json.JSONDecodeError: If :meth:`aiohttp.ClientResponse.json`'s
            default loader fails to parse a JSON-content-typed response body.
        :raises UnicodeDecodeError: If a JSON-content-typed response body cannot
            be decoded using its declared or inferred character encoding.
        """
        follows_redirects = kwargs.get("allow_redirects", True)
        async with self._request(method, path, **kwargs) as response:
            if response.status == status.HTTP_204_NO_CONTENT:
                return None
            if not follows_redirects and _is_redirect(response.status):
                self.logger.warning(
                    "RemoteAPI (%s): %s request to %s answered %s, which this "
                    "caller does not follow.",
                    redact_credential_url(str(self.endpoint)),
                    method,
                    path,
                    response.status,
                )
                raise exception_for_status(
                    response.status,
                    detail="The server answered with an unfollowed redirect.",
                )
            withhold_body = self._suppress_response_log.get()
            response_data: JSONBody = None
            try:
                response_data = await response.json()
                self.logger.debug(
                    "RemoteAPI (%s): %s request to %s response (%s): %s",
                    redact_credential_url(str(self.endpoint)),
                    method,
                    path,
                    response.status,
                    _WITHHELD_BODY if withhold_body else response_data,
                )
                response.raise_for_status()
            except ContentTypeError as err:
                # %r, not %s: the body is untrusted upstream text, so rendering it
                # raw would let its own newlines and control characters forge
                # further log lines out of one record.
                self.logger.exception(
                    "RemoteAPI (%s): %s request to %s response content (%s): %r",
                    redact_credential_url(str(self.endpoint)),
                    method,
                    path,
                    response.status,
                    _WITHHELD_BODY
                    if withhold_body
                    else shorten_text(
                        await response.text(errors="replace"),
                        max_length=_NON_JSON_LOG_MAX_CHARS,
                        ellipsis=_TRUNCATION_MARKER,
                    ),
                )
                raise exception_for_status(
                    err.status,
                    detail="An unexpected error occurred on the server.",
                    headers={UPSTREAM_NON_JSON_HEADER: "1"},
                ) from None
            except ClientResponseError as err:
                error_body = response_data if isinstance(response_data, Mapping) else {}
                error_detail = error_body.get(
                    self.error_detail_key, "An unexpected error occurred on the server."
                )
                error_headers = None
                if self.error_code_key and (
                    error_code := error_body.get(self.error_code_key)
                ):
                    error_headers = {"X-Error-Code": str(error_code)}
                raise exception_for_status(
                    err.status, detail=error_detail, headers=error_headers
                ) from None

            return response_data

    async def get(
        self, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform a GET request and return the JSON response.

        :param path: The API endpoint path to request.
        :type path: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :return: The JSON response as a Python object, or ``None`` on HTTP 204.
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        return await self.request("GET", path, **kwargs)

    async def post(
        self, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform a POST request and return the JSON response.

        :param path: The API endpoint path to request.
        :type path: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :return: The JSON response as a Python object, or ``None`` on HTTP 204.
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        return await self.request("POST", path, **kwargs)

    async def put(
        self, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform a PUT request and return the JSON response.

        :param path: The API endpoint path to request.
        :type path: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :return: The JSON response as a Python object, or ``None`` on HTTP 204.
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        return await self.request("PUT", path, **kwargs)

    async def patch(
        self, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform a PATCH request and return the JSON response.

        :param path: The API endpoint path to request.
        :type path: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :return: The JSON response as a Python object, or ``None`` on HTTP 204.
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        return await self.request("PATCH", path, **kwargs)

    async def delete(
        self, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform a DELETE request and return the JSON response.

        :param path: The API endpoint path to request.
        :type path: str
        :param kwargs: Additional keyword arguments to pass to the request.
        :type kwargs: Any
        :return: The JSON response as a Python object, or ``None`` on HTTP 204.
        :rtype: dict[str, Any] | list[dict[str, Any]] | None
        """
        return await self.request("DELETE", path, **kwargs)

    async def upload(
        self,
        path: str,
        *,
        files: Mapping[str, FileSpec],
        fields: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Send a ``multipart/form-data`` body (file bundle plus scalar fields).

        Reuse the JSON request transport for error translation, credential
        redaction, correlation IDs, and SSL. Two deltas versus :meth:`request`:
        the multipart body carries its own boundary Content-Type, which must
        override the session's default ``application/json`` header; and a
        successful non-JSON response body is tolerated -- a vendor-neutral intake
        may answer ``201`` with a ``text/plain`` acknowledgement or an empty
        (non-204) body, which :meth:`request` alone surfaces as a bare ``2xx``
        ``HTTPException`` because it parses the body before ``raise_for_status``.

        :param path: The API endpoint path to POST to.
        :param files: Multipart file parts keyed by field name; each value is a
            ``(filename, content, content_type)`` tuple. Pass an open binary file
            handle or an async byte iterator as ``content`` to stream a large
            bundle with bounded memory.
        :param fields: Scalar form fields sent alongside the files.
        :param kwargs: Additional keyword arguments passed through to the request.
        :return: The parsed JSON response, or ``None`` on a ``2xx`` response with
            an empty or non-JSON body.
        :raises HTTPException: The project exception mapped to an error status,
            as translated by :meth:`request`.
        """
        form = FormData()
        for name, value in (fields or {}).items():
            form.add_field(name, value)
        for name, (filename, content, content_type) in files.items():
            form.add_field(name, content, filename=filename, content_type=content_type)
        payload = form()
        headers = {**kwargs.pop("headers", {}), "Content-Type": payload.content_type}
        try:
            return await self.request(
                "POST", path, data=payload, headers=headers, **kwargs
            )
        except HTTPException as exc:
            if is_non_json_success(exc):
                return None
            raise
