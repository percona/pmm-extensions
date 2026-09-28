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

"""Define reusable fields, validators, and related utilities."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any, Generic, Self, TypeVar
from urllib.parse import urlparse, urlunparse

from annotated_types import Interval
from pydantic import (
    AfterValidator,
    AnyUrl,
    BeforeValidator,
    DirectoryPath,
    Field,
    FilePath,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    HttpUrl,
    PlainSerializer,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    UrlConstraints,
    ValidationInfo,
    WrapSerializer,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema, Url
from starlette.datastructures import URL as StarletteURL  # noqa: N811

from app.core.utils.date_time import make_datetime_utc
from app.core.utils.imports import (
    validate_attribute_is_importable,
    validate_module_is_importable,
)
from app.core.utils.iterators import unique_everseen
from app.core.utils.json_pointer import validate_json_pointer
from app.core.utils.path import resolve_relative_path

E = TypeVar("E", bound=Enum)


def value_is_present(value: Any) -> bool:
    """Return whether ``value`` counts as *present* to a presence/forbidden gate.

    Treats ``None``, ``False``, and empty strings/bytes/lists/tuples/sets/dicts
    as absent. ``0`` counts as present (numeric, just falsy). ``False`` is
    treated as the unset bool default so ``forbidden=`` ``FieldGate`` entries on
    a ``BoolField`` fire only on an explicit ``True`` toggle, matching the
    convention used by ``FailRule`` ``truthy(name)`` checks.

    Shared by the framework's runtime gate evaluation (``_field_is_present``)
    and authoring-time guards (such as the snippets plugin's meta validation) so
    the two presence checks cannot drift. It lives here, in the core utilities,
    rather than the framework package so core models can reuse it without
    importing the plugin layer.

    :param value: The value to classify.
    :return: Whether the value is considered present.
    """
    if value is None or value is False:
        return False
    return not (
        isinstance(value, str | bytes | list | tuple | set | frozenset | dict)
        and not value
    )


def get_enum_from_value_or_name_factory(enum_class: type[E]) -> Callable[[Any], E]:
    """Generate and return a function that returns the Enum from its value or name.

    :param enum_class: The Enum subclass to use.
    :type enum_class: type[E]
    :return: A function that returns the Enum value by name.
    :rtype: Callable[[Any], E]
    """
    enum_class_name = enum_class.__name__

    def get_enum_from_value_or_name(
        value_or_name: Any,
    ) -> enum_class:  # ty: ignore[invalid-type-form]
        """Return the {enum_class} from its value or name.

        :param value_or_name: The value or name of the {enum_class} to return.
        :type value_or_name: Any
        :return: The {enum_class} found.
        :rtype: {enum_class}
        :raises ValueError: If `value_or_name` is not a value in {enum_class} and
            `value_or_name` is not a valid name for an Enum (not a string).
        :raises ValueError: If `value_or_name` is neither a value nor a name in
            {enum_class}.
        """
        try:
            return enum_class(value_or_name)
        except ValueError:
            if not isinstance(value_or_name, str):
                raise ValueError(
                    f"Value not found and is not a valid name for {enum_class_name}: {value_or_name!r}"
                ) from None
            enum_dict = {
                name.upper(): value for name, value in enum_class.__members__.items()
            }
            try:
                return enum_dict[value_or_name.upper()]
            except KeyError:
                raise ValueError(
                    f"Value and name not found for {enum_class_name}: {value_or_name!r}"
                ) from None

    get_enum_from_value_or_name.__doc__ = get_enum_from_value_or_name.__doc__.format(
        enum_class=enum_class_name
    )
    return get_enum_from_value_or_name


V = TypeVar("V")
T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class AsTypeValidator(Generic[V, R]):
    """Validate an object with a specified class and optionally apply post-processing.

    This validator uses a designated type (`validate_class`) to validate an object. If a
    post-processing function is provided, it applies this function to the validated
    value before returning it. Otherwise, it returns the original object.

    :param validate_class: The class to use for validation.
    :type validate_class: type[V]
    :param post_processing: An optional callable to process the validated value.
        Defaults to None.
    :type post_processing: Callable[[V], R] | None
    """

    validate_class: type[V]
    post_processing: Callable[[V], R] | None = None

    def validate_as_type(self, obj: T) -> T | R:
        """Validate an object with `self.validate_class` and return the result.

        This method validates an object with `self.validate_class` and returns
        `self.post_processing(validated_value)` if `self.post_processing` is defined.
        If `self.post_processing` is None, the initial object is returned as is.

        :param obj: The object to validate.
        :type obj: T
        :return: The return of `self.post_processing(validated_value)` if
            `self.post_processing` is defined, or `obj` if not.
        :rtype: T | R
        :raises ValidationError: If the validation fails.
        """
        validated_value = TypeAdapter(self.validate_class).validate_python(obj)
        if self.post_processing is None:
            return obj
        return self.post_processing(validated_value)

    def __get_pydantic_core_schema__(
        self, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Provide the Pydantic core schema for the validator.

        This method integrates the `validate_as_type` method into Pydantic's
        validation schema.

        :param source_type: The source type for validation.
        :type source_type: Any
        :param handler: The handler for core schema retrieval.
        :type handler: GetCoreSchemaHandler
        :return: The core schema incorporating the validation logic.
        :rtype: core_schema.CoreSchema
        """
        return core_schema.no_info_after_validator_function(
            self.validate_as_type, handler(source_type)
        )


class URL(StarletteURL):
    """Define a custom URL type with Pydantic validation.

    This class extends Starlette's `URL` class and integrates custom validation
    for use with Pydantic models. It ensures that only valid URLs are accepted
    and provides a custom schema for Pydantic's core validation.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        """Provide the Pydantic core schema for URL validation and serialization.

        Integrate the ``validate_url`` method into Pydantic's validation schema
        and serialize URL values to their string form in JSON mode, so a
        ``URL``-typed value round-trips through ``model_dump(mode="json")``.

        :param source_type: The source type for validation.
        :type source_type: Any
        :param handler: The handler for core schema retrieval.
        :type handler: GetCoreSchemaHandler
        :return: The core schema incorporating the URL validation logic.
        :rtype: core_schema.CoreSchema
        """
        return core_schema.no_info_plain_validator_function(
            cls.validate_url,
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, when_used="json"
            ),
        )

    @classmethod
    def validate_url(cls, url: str) -> Self:
        """Validate the provided URL string and return a URL instance.

        Attempt to create a `URL` instance from the given string. Raise a
        `ValueError` if the URL is invalid.

        :param url: The URL string to validate.
        :type url: str
        :return: The validated `URL` instance.
        :rtype: URL
        :raises ValueError: If the provided string is not a valid URL.
        """
        try:
            return cls(url)
        except ValueError:
            raise ValueError(f"Invalid URL: {url}") from None

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: core_schema.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Provide the JSON schema for the custom URL type.

        This method defines the JSON schema properties for the `URL` type, specifying
        that it should be treated as a string with a URI format.

        :param core_schema: The core schema for the URL.
        :param handler: The handler for JSON schema retrieval.
        :return: The JSON schema for the `URL` type.
        """
        json_schema = handler(core_schema)
        json_schema.update(
            {
                "type": "string",
                "format": "uri",
            },
        )
        return json_schema


class EnumFieldMixin:
    """Provide a Pydantic core schema for Enum fields.

    This mixin integrates Enum validation into Pydantic's validation schema
    by utilizing the `get_enum_from_value_or_name_factory`.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls: type[Enum],
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        """Provide the Pydantic core schema for Enum validation.

        This method applies a before-validator function that retrieves Enum members
        based on their value or name.

        :param source_type: The source type for validation.
        :type source_type: Any
        :param handler: The handler for core schema retrieval.
        :type handler: GetCoreSchemaHandler
        :return: The core schema incorporating the Enum validation logic.
        :rtype: core_schema.CoreSchema
        """
        return core_schema.no_info_before_validator_function(
            get_enum_from_value_or_name_factory(cls), handler(source_type)
        )


class LogLevel(EnumFieldMixin, IntEnum):
    """Enumerate standard logging levels.

    This enumeration maps common logging level names to their corresponding
    integer values as defined in Python's `logging` module.

    :cvar CRITICAL: Critical logging level.
    :vartype CRITICAL: int
    :cvar FATAL: Fatal logging level (alias for CRITICAL).
    :vartype FATAL: int
    :cvar ERROR: Error logging level.
    :vartype ERROR: int
    :cvar WARNING: Warning logging level.
    :vartype WARNING: int
    :cvar WARN: Warn logging level (alias for WARNING).
    :vartype WARN: int
    :cvar INFO: Info logging level.
    :vartype INFO: int
    :cvar DEBUG: Debug logging level.
    :vartype DEBUG: int
    :cvar NOTSET: Notset logging level.
    :vartype NOTSET: int
    :cvar DISABLED: Disabled logging level (alias for NOTSET).
    :vartype DISABLED: int
    """

    CRITICAL = logging.CRITICAL
    FATAL = logging.CRITICAL
    ERROR = logging.ERROR
    WARNING = logging.WARNING
    WARN = logging.WARNING
    INFO = logging.INFO
    DEBUG = logging.DEBUG
    NOTSET = logging.NOTSET
    DISABLED = logging.NOTSET


class DatabaseDialect(EnumFieldMixin, StrEnum):
    """Represent supported database dialect names."""

    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class DatabaseEngine(EnumFieldMixin, StrEnum):
    """Represent supported database engines as SQLAlchemy engine strings."""

    SQLITE = "sqlite"
    POSTGRESQL = "postgresql+psycopg2"


class AsyncDatabaseEngine(EnumFieldMixin, StrEnum):
    """Represent supported async database engines as SQLAlchemy engine strings."""

    SQLITE = "sqlite+aiosqlite"
    POSTGRESQL = "postgresql+asyncpg"


def database_url_normalized_scheme_field_factory(
    engine_enum_class: type[DatabaseEngine] | type[AsyncDatabaseEngine],
) -> Any:
    """Generate and return an Url field that normalizes the scheme of a database URL.

    Annotated ``Any`` because the result is an ``Annotated[...]`` type expression
    assembled here, not a class.

    This factory function generates an annotated Url field with a BeforeValidator that
    normalizes the scheme of a database URL according to the `engine_enum_class`
    specified.

    :param engine_enum_class: The database engine enum type to use. Either
        ``DatabaseEngine`` or ``AsyncDatabaseEngine``.
    :return: The annotated Url field with the attached validator.
    """
    get_database_engine_enum = get_enum_from_value_or_name_factory(engine_enum_class)

    def normalize_database_url_scheme(db_url: Any) -> Any:
        """Normalize the scheme of the provided database URL.

        If the URL starts with a recognized scheme, it replaces it with the normalized
        scheme based on the `engine_enum_class`. Otherwise, it returns the URL
        unchanged.

        :param db_url: The database URL to normalize.
        :type db_url: Any
        :return: The normalized database URL.
        :rtype: Any
        """
        if not isinstance(db_url, str):
            return db_url
        pattern = re.compile(r"^(([a-zA-Z]+)(?:\+[a-zA-Z0-9]+)?)://")
        match = pattern.match(db_url)
        if not match:
            return db_url
        base_scheme = match.group(2)
        return pattern.sub(f"{get_database_engine_enum(base_scheme)}://", db_url)

    return Annotated[
        Url,
        UrlConstraints(allowed_schemes=list(engine_enum_class)),
        BeforeValidator(normalize_database_url_scheme),
    ]


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
"""Define a string field that must not be empty."""

AuthSchemeStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9A-Za-z!#$%&'*+.^_`|~-]+$")
]
"""Define an HTTP authentication scheme, the ``token`` production of RFC 7230.

A scheme is spliced into an ``Authorization`` header value, and a header value
cannot carry whitespace-leading, ``CR``, ``LF`` or ``NUL`` bytes under any
escaping — ``requests`` raises ``InvalidHeader`` and ``aiohttp`` raises
``ValueError`` at send time. Constraining the field rejects such a value where
an operator sets it, rather than leaving every later request to fail.
"""

ARBITRARY_ARGS_SCHEMA = {"additionalProperties": True}
"""Advertise a free-form argument map for OpenAPI / TypeScript clients.

openapi-typescript turns an object schema with no ``additionalProperties`` into
``Record<string, never>``. Pydantic emits ``additionalProperties: true`` for a bare
``dict`` field itself, so this states it explicitly rather than relying on that
default. Pass as ``Field(json_schema_extra=...)`` on a plain
Pydantic field, or nest under ``SQLField(..., schema_extra=...)`` for SQLModel.
"""


class ArbitraryMapping(dict):
    """Represent a free-form ``dict[str, Any]`` in an open object schema.

    Use for nested or nullable mappings, and for route-level free-form JSON
    bodies / responses. Putting ``additionalProperties`` only as a sibling of
    ``anyOf`` still leaves ``Record<string, never>`` in the generated union.

    Implemented as a ``dict`` subclass (not ``Annotated`` + ``WithJsonSchema``)
    for two reasons:

    1. **Fresh schema dicts.** FastAPI writes an operation-derived ``title``
       into the JSON schema it resolves for a return type or ``Body()``.
       ``WithJsonSchema`` returns the dict it stores, and equal metadata
       collapses across annotation sites, so one route's title leaks onto
       every other (including a GET response title on a PUT request body).
       An explicit ``title`` in the template, and a fresh dict *per*
       ``WithJsonSchema`` construction, both still leak. Returning a new dict
       from ``__get_pydantic_json_schema__`` on every call keeps titles local.
       Model fields are unaffected — they keep field-derived titles.
    2. **Stable component names.** The ``Annotated`` + ``WithJsonSchema`` form
       renamed generics to
       ``PaginatedResponse_Annotated_dict_str__Any___WithJsonSchema__``. A
       named subclass keeps ``PaginatedResponse_ArbitraryMapping_``.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Validate and coerce as a plain ``dict[str, Any]``.

        :param source_type: The annotated source type (ignored; always this class).
        :param handler: Pydantic's core-schema builder.
        :return: A core schema for ``dict[str, Any]``.
        """
        return handler.generate_schema(dict[str, Any])

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: core_schema.CoreSchema,
        handler: GetCoreSchemaHandler,
    ) -> JsonSchemaValue:
        """Emit an open object schema, copying so FastAPI title writes stay local.

        :param core_schema: The core schema produced for this type.
        :param handler: Pydantic's JSON-schema builder.
        :return: A fresh ``type: object`` schema with ``additionalProperties``.
        """
        return {**handler(core_schema), **ARBITRARY_ARGS_SCHEMA}


def dsn_safe(value: str) -> str:
    """Reject DSN/CLI delimiters in a free-typed schema, table, or host name.

    Percona Toolkit DSN strings and comma-separated CLI arguments treat ``,``
    and ``=`` as structural delimiters. Free-solo reference fields accept either
    an inventory id (``int``) or a free-typed name (``str``); callers receiving
    ``int | str`` values should guard with ``isinstance(value, str)`` first.

    :param value: The free-typed name to validate.
    :return: ``value`` unchanged when it contains no delimiter.
    :raises ValueError: When ``value`` contains ``,`` or ``=``.
    """
    if "," in value or "=" in value:
        raise ValueError(
            "Values cannot contain ',' or '=' characters (DSN delimiters)."
        )
    return value


NON_WHITESPACE_PATTERN = r"\S"
"""Match a value carrying at least one non-whitespace character."""

StrippedNonEmptyStr = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, pattern=NON_WHITESPACE_PATTERN
    ),
]
"""Define a string field that strips surrounding whitespace and must not be empty.

``strip_whitespace=True`` is a validation-time transform that never reaches the
published JSON Schema, so ``min_length`` alone would let a client accept a
whitespace-only value the server then rejects. ``NON_WHITESPACE_PATTERN`` states
that same contract in the schema, and is deliberately unanchored:
``strip_whitespace`` trims only the edges, so a value with interior whitespace
stays valid. Both constraints are kept so each rejects a blank value
independently.
"""

EmptyStrToNone = Annotated[None, BeforeValidator(lambda v: None if v == "" else v)]
"""Convert empty strings to None."""

TCP_PORT_MIN = 1
TCP_PORT_MAX = 65535

TcpPort = Annotated[int, Field(ge=TCP_PORT_MIN, le=TCP_PORT_MAX)]
"""Define a TCP port number constrained to the valid 1-65535 range."""


def bounded_int_from_empty_str_factory(ge: int, le: int | None = None) -> Any:
    """Build a bounded optional-int field type that coerces ``""`` to ``None``.

    The bounds sit at the returned type's outer ``Annotated`` level, carried by a single
    ``Interval`` container, so that Pydantic applies them only to real integers (not the
    coerced ``None``) and the form-DSL bounds scan reads them (it flattens
    ``GroupedMetadata`` containers such as ``Interval``). A plain
    ``int | EmptyStrToNone`` union cannot express this, hence a dedicated factory.

    :param ge: The inclusive lower bound applied to non-empty integer input.
    :param le: The inclusive upper bound; ``None`` leaves the field unbounded above.
    :return: An ``Annotated`` optional-int type carrying the bounds and blank coercion.
    """
    coerce_blank = BeforeValidator(lambda value: None if value == "" else value)
    return Annotated[int | None, Interval(ge=ge, le=le), coerce_blank]


RelativeFilePathField = Annotated[
    FilePath,
    BeforeValidator(resolve_relative_path),
]
"""Define a file path that resolves relative paths.

This annotated type ensures that the provided file path is valid and resolves
relative paths based on the application's directory structure.
"""

RelativeDirectoryPathField = Annotated[
    DirectoryPath,
    BeforeValidator(resolve_relative_path),
]
"""Define a directory path that resolves relative paths.

This annotated type ensures that the provided directory path is valid and resolves
relative paths based on the application's directory structure.
"""

RelativePathField = Annotated[
    Path,
    BeforeValidator(resolve_relative_path),
]
"""Define a path that resolves relative paths.

This annotated type ensures that the provided path is valid and resolves
relative paths based on the application's directory structure. This type does not
validate if the path exists, it only resolves the relative path to an absolute one.
"""

StrRelativePath = Annotated[str, AsTypeValidator(RelativePathField, str)]
"""Define a string field representing a relative paths.

This annotated type validates the string as a relative path and ensures it is returned
as a string.
"""

FilePathLike = Annotated[FilePath, BeforeValidator(lambda v: Path(v))]
"""Define a :class:`FilePath` that accepts any :class:`os.PathLike` object as input."""

DatabaseUrl = database_url_normalized_scheme_field_factory(DatabaseEngine)
"""Define a normalized synchronous database URL.

This annotated type normalizes the scheme of the database URL based on the
`synchronous` database engines defined in `DatabaseEngine`.
"""

AsyncDatabaseUrl = database_url_normalized_scheme_field_factory(AsyncDatabaseEngine)
"""Define a normalized asynchronous database URL.

This annotated type normalizes the scheme of the database URL based on the
`asynchronous` database engines defined in `AsyncDatabaseEngine`.
"""

StrDatabaseUrl = Annotated[str, AsTypeValidator(DatabaseUrl, str)]
"""Define a string field representing a synchronous database URL.

This annotated type validates the string as a synchronous database URL and
ensures it is returned as a string.
"""

StrAsyncDatabaseUrl = Annotated[str, AsTypeValidator(AsyncDatabaseUrl, str)]
"""Define a string field representing an asynchronous database URL.

This annotated type validates the string as an asynchronous database URL and
ensures it is returned as a string.
"""

StrHttpUrl = Annotated[str, AsTypeValidator(HttpUrl, lambda v: str(v).rstrip("/"))]
"""Define a string field representing an HTTP URL.

This annotated type validates the string as an HTTP URL and removes any trailing
slashes from the URL.
"""

StrAnyUrl = Annotated[str, AsTypeValidator(AnyUrl, str)]
"""Define a string field representing any valid URL.

This annotated type validates the string as any valid URL without additional processing.
"""

CREDENTIAL_URL_MASK = "****"
_PRESERVE_CREDENTIALS_KEY = "preserve_credentials"
PRESERVE_CREDENTIALS_CONTEXT: dict[str, bool] = {_PRESERVE_CREDENTIALS_KEY: True}


def _netloc_host(netloc: str) -> str:
    """Return the host portion of a URL netloc, preserving IPv6 bracket notation."""
    if "@" in netloc:
        _, host = netloc.rsplit("@", 1)
        return host
    return netloc


def credential_url_password(url: str) -> str | None:
    """Return the embedded userinfo password, or ``None`` when there is none.

    The shared resolver for callers that inspect or transform the password in
    isolation: the at-rest walker, the side-car's mint preflight,
    :func:`redact_credential_url` and the mask-rejecting validator all settle
    "which characters are the password" here rather than each parsing their
    own. :func:`preserve_credential_url_password` is the deliberate exception —
    it compares every other URL component in the same pass, so it keeps the
    whole parse rather than only the password.

    An empty password (``https://user:@host/``) collapses to ``None``: it is
    absence, not a credential, so a caller transforming the segment cannot
    invent one where the operator supplied none.

    A URL this cannot parse raises rather than answering ``None``, because the
    two mean opposite things to a caller that masks: ``None`` is "there is no
    credential here", while the exception is "there may be one and I cannot
    find it". Callers choose their own posture —
    ``secret_storage._transform_credential_url`` leaves the leaf alone, while
    ``masking._redact_credential_url_token`` replaces the whole token.

    :param url: The URL string to inspect.
    :return: The raw (still percent-encoded) password segment, or ``None`` when
        the URL carries none.
    :raises ValueError: If ``url`` cannot be parsed. A malformed bracketed
        IPv6 literal is one such shape, and a netloc whose characters decompose
        into a URL delimiter under NFKC normalisation is another — the contract
        is the parse failure itself, not either shape.
    """
    return urlparse(url).password or None


def map_credential_url_password(url: str, transform: Callable[[str], str]) -> str:
    """Return ``url`` with ``transform`` applied to its embedded password.

    Scheme, username, host, port, path, query and fragment are preserved
    byte-for-byte, and the password is handed over exactly as it appears in the
    URL — still percent-encoded — so an encrypt/decrypt round trip reproduces
    the original string.

    :param url: The URL string to rewrite.
    :param transform: Applied to the password segment.
    :return: The rewritten URL, or ``url`` when it carries no password.
    :raises ValueError: If ``url`` cannot be parsed. Propagated deliberately:
        a caller masking a credential must not receive the unredacted input
        when the parse fails.
    """
    password = credential_url_password(url)
    if password is None:
        return url
    parsed = urlparse(url)
    host = _netloc_host(parsed.netloc)
    username = parsed.username or ""
    netloc = f"{username}:{transform(password)}@{host}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def redact_credential_url(url: str, *, mask: str = CREDENTIAL_URL_MASK) -> str:
    """Return ``url`` with any embedded userinfo password replaced by ``mask``.

    Scheme, username, host, port, path, query, and fragment are preserved.
    URLs without an embedded password are returned unchanged.

    :param url: The URL string to redact.
    :param mask: The replacement for the password segment.
    :return: The URL with a redacted password, or ``url`` when none is present.
    """
    return map_credential_url_password(url, lambda _password: mask)


def strip_credential_url_userinfo(url: str) -> str:
    """Return ``url`` with any embedded userinfo segment removed.

    ``requests`` and ``aiohttp`` both derive basic auth from a URL's userinfo and
    let it override an explicit ``Authorization`` header, so a caller presenting
    a header of its own must remove the userinfo rather than rely on precedence.
    This differs from :func:`redact_credential_url`, which only masks the
    password: a masked URL still puts basic auth on the wire.

    The presence test reads the ``@`` delimiter rather than the parsed username
    and password, so the degenerate ``http://@host`` and ``http://:@host``
    shapes — whose parsed halves are both empty strings — lose their delimiter
    too.

    :param url: The URL string to strip.
    :return: The URL without its userinfo segment, or ``url`` when it carries none.
    """
    parsed = urlparse(url)
    if "@" not in parsed.netloc:
        return url
    return urlunparse(
        (
            parsed.scheme,
            _netloc_host(parsed.netloc),
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _credential_url_identity_parts(
    parsed: Any, *, include_password: bool = True
) -> tuple[Any, ...]:
    """Return URL components used to detect an unchanged redacted resubmit."""
    path = parsed.path or ""
    if path == "/":
        path = ""
    parts: tuple[Any, ...] = (
        parsed.scheme,
        parsed.username or "",
        _netloc_host(parsed.netloc),
        path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    )
    if include_password:
        return (*parts, parsed.password or "")
    return parts


def preserve_credential_url_password(current: str, incoming: str) -> str:
    """Keep the stored URL password when a PATCH resubmits the redacted display value.

    When the dashboard saves an endpoint unchanged, the client sends the JSON-redacted
    URL (``user:****@host``). Substitute the live password so the override row is
    not corrupted.

    :param current: The effective stored URL string.
    :param incoming: The URL string submitted in the PATCH body.
    :return: ``incoming`` unchanged unless it matches ``current`` with only the
        password masked.
    """
    current_str = str(current)
    incoming_str = str(incoming)
    current_parsed = urlparse(current_str)
    incoming_parsed = urlparse(incoming_str)
    if incoming_parsed.password != CREDENTIAL_URL_MASK:
        return incoming_str
    if not current_parsed.password:
        return incoming_str
    if _credential_url_identity_parts(current_parsed, include_password=False) != (
        _credential_url_identity_parts(incoming_parsed, include_password=False)
    ):
        return incoming_str
    return current_str


def _credential_url_serializer(
    value: Any,
    handler: Callable[[Any], Any],
    info: Any,
) -> Any:
    """Serialize a URL value, redacting embedded passwords unless context opts out."""
    serialized = handler(value)
    context = getattr(info, "context", None) or {}
    if context.get(_PRESERVE_CREDENTIALS_KEY):
        return serialized
    return redact_credential_url(str(serialized))


_CREDENTIAL_URL_JSON_SERIALIZER = WrapSerializer(
    _credential_url_serializer,
    when_used="json",
)

CREDENTIAL_URL_STR_JSON_SERIALIZER = WrapSerializer(
    _credential_url_serializer,
    when_used="json",
    return_type=str,
)
"""Redact an embedded URL password on JSON dumps of a plain string value.

Written for the return annotation of a ``computed_field`` whose value is a URL
string. A ``field_serializer`` cannot be used there: pydantic rejects
:func:`_credential_url_serializer`'s three-argument signature on a computed
field, so the annotation is the only shape that reaches it.

Kept separate from the marker the annotated field types share because it
carries ``return_type``: a bare wrap serializer erases ``"type": "string"``
from the serialization schema, and pinning it here leaves those types' own
schemas alone. It carries no validator either, which suits a derived value —
there is nothing to validate on the way in.
"""


def _reject_credential_url_mask(value: Any, info: ValidationInfo) -> Any:
    """Reject a URL whose embedded password is the redaction mask.

    The mask is what every JSON read surface emits, so it round-trips back as a
    plausible-looking value on the YAML and environment paths (and on any future
    import that coerces through the annotated types). A PATCH that resubmits a
    masked endpoint reaches this validator only when there is no stored password
    to restore: whenever the current value carries one, the preserve step swaps
    the mask back for it first.

    :param value: The validated URL value (``HttpUrl`` or ``str``).
    :param info: Pydantic validation info; ``field_name`` names the field when
        validating inside a model.
    :return: ``value`` unchanged when the password is not the mask.
    :raises ValueError: When the parsed userinfo password equals
        :data:`CREDENTIAL_URL_MASK`.
    """
    if credential_url_password(str(value)) == CREDENTIAL_URL_MASK:
        field = info.field_name or "value"
        raise ValueError(
            f"{field} carries the redacted display value in its password "
            "segment and cannot be stored; supply the real credential."
        )
    return value


_REJECT_CREDENTIAL_URL_MASK = AfterValidator(_reject_credential_url_mask)


CredentialHttpUrl = Annotated[
    HttpUrl,
    _REJECT_CREDENTIAL_URL_MASK,
    _CREDENTIAL_URL_JSON_SERIALIZER,
]
"""Define an HTTP URL that redacts embedded userinfo passwords on JSON serialization.

The in-memory / python-mode value retains the real credential for outbound
requests. Pass :data:`PRESERVE_CREDENTIALS_CONTEXT` when dumping internal
config fingerprints that must store the full URL.

Validation rejects a URL whose userinfo password equals
:data:`CREDENTIAL_URL_MASK`. A literal ``****`` password is therefore
unusable on this type: it is indistinguishable from the mask by construction.
"""

StrCredentialHttpUrl = Annotated[
    str,
    AsTypeValidator(HttpUrl, lambda v: str(v).rstrip("/")),
    _REJECT_CREDENTIAL_URL_MASK,
    _CREDENTIAL_URL_JSON_SERIALIZER,
]
"""Define a string HTTP URL that redacts embedded passwords on JSON serialization.

Validates as :class:`~pydantic.HttpUrl`, stores as a string with trailing
slashes stripped, and masks any embedded password in JSON dumps.

Validation rejects a URL whose userinfo password equals
:data:`CREDENTIAL_URL_MASK`. A literal ``****`` password is therefore
unusable on this type: it is indistinguishable from the mask by construction.
"""

StrCredentialAnyUrl = Annotated[
    str,
    AsTypeValidator(AnyUrl, str),
    _REJECT_CREDENTIAL_URL_MASK,
    _CREDENTIAL_URL_JSON_SERIALIZER,
]
"""Define a string URL (any scheme) that redacts embedded passwords on JSON serialization.

Use for broker/backend URLs that may carry credentials in the userinfo segment.

Validation rejects a URL whose userinfo password equals
:data:`CREDENTIAL_URL_MASK`. A literal ``****`` password is therefore
unusable on this type: it is indistinguishable from the mask by construction.
"""


def _preserve_secret_serializer(
    value: Any,
    handler: Callable[[Any], Any],
    info: Any,
) -> Any:
    """Serialize a secret, emitting the real value only under the preserve context.

    :param value: The secret being serialized.
    :param handler: Pydantic's default serializer for the field, which masks.
    :param info: Pydantic serialization info; its ``context`` opts out of masking.
    :return: The plain secret under :data:`PRESERVE_CREDENTIALS_CONTEXT`, else the
        masked value ``handler`` produces.
    """
    context = getattr(info, "context", None) or {}
    if context.get(_PRESERVE_CREDENTIALS_KEY) and isinstance(value, SecretStr):
        return value.get_secret_value()
    return handler(value)


PreservableSecretStr = Annotated[
    SecretStr,
    WrapSerializer(_preserve_secret_serializer, when_used="json"),
]
"""Define a secret string whose JSON dump can opt out of masking.

JSON dumps carry Pydantic's mask
(:data:`~app.core.settings_override.registry.SECRET_STR_MASK`) unless
:data:`PRESERVE_CREDENTIALS_CONTEXT` is passed, which emits the plain secret so a
config fingerprint survives a JSON round-trip and a rotated credential compares
unequal to the one it replaces.

:class:`~pydantic.SecretStr` remains reachable from the annotation, so
:func:`~app.core.settings_override.registry.annotation_contains_secret` still
classifies the field as a secret and settings-API masking and at-rest encryption
are unchanged.
"""

_HEADER_UNSENDABLE_CHARACTERS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _reject_header_unsendable_characters(value: Any) -> Any:
    """Reject a credential carrying a byte one of the HTTP clients refuses to send.

    :param value: The raw value on its way into the secret wrapper.
    :return: ``value`` unchanged when it is safe to splice into a header.
    :raises ValueError: If the value contains a character neither client will send.
    """
    if isinstance(value, str) and _HEADER_UNSENDABLE_CHARACTERS.search(value):
        raise ValueError(
            "cannot contain control characters: the HTTP clients refuse to send "
            "them, so every request presenting this credential would fail when it "
            "is sent rather than where it is set"
        )
    return value


AuthCredentialSecretStr = Annotated[
    PreservableSecretStr, BeforeValidator(_reject_header_unsendable_characters)
]
"""Define a :data:`PreservableSecretStr` safe to splice into an ``Authorization`` header.

The companion of :data:`AuthSchemeStr` for the other half of the header value.
A credential is *not* held to RFC 7230's ``token`` — base64 padding and other
non-``token`` bytes are legitimate in a real key — so the rejected set is
exactly what one of the two clients refuses to put on the wire: every control
character except ``HTAB``. Swept byte by byte against both, ``requests`` rejects
only ``LF`` and ``CR`` while ``aiohttp`` rejects the rest, and ``HTAB`` and space
are sent by both. A key pasted with a trailing newline is the ordinary way such a
value arrives, and it would otherwise fail every request rather than the
assignment.

The constraint sits in a ``BeforeValidator`` rather than
:class:`~pydantic.StringConstraints` because a constraint cannot be applied to
:class:`~pydantic.SecretStr`'s schema, and wrapping a constrained ``str`` in
:class:`~pydantic.Secret` instead would drop ``SecretStr`` from the annotation —
which is what
:func:`~app.core.settings_override.registry.annotation_contains_secret` reads to
classify the field, so masking and at-rest encryption would silently stop.
"""

URIPath = Annotated[str, StringConstraints(pattern=r"^\/[^\s]*$")]
"""Define a string field representing a URI path.

This annotated type ensures that the string starts with a forward slash and does
not contain any whitespace characters.
"""

URIPathPrefix = Annotated[str, StringConstraints(pattern=r"^(?:/[^\s/?#]+)*$")]
"""Define a string field holding a URL mount prefix.

The empty string is the valid unprefixed default; any other value is one or more
``/``-prefixed segments with no trailing slash, so the prefix concatenates
cleanly onto a root-anchored path. ``?`` and ``#`` are excluded because a mount
prefix is a path, never a query or fragment. Rejecting rather than normalizing
keeps a malformed value a startup failure instead of a doubled slash or an
unroutable prefix at request time.
"""

JsonPointerStr = Annotated[str, AfterValidator(validate_json_pointer)]
"""Define a string field holding an RFC 6901 JSON Pointer.

The empty string is the valid root pointer; any other value must start with a
forward slash and use ``~`` only in a ``~0`` or ``~1`` escape.
"""

StrImportableModule = Annotated[str, AfterValidator(validate_module_is_importable)]
"""Define a string field representing an importable module.

This annotated type validates that the string corresponds to a module that can be
imported.
"""

StrImportableAttribute = Annotated[
    str,
    AfterValidator(validate_attribute_is_importable),
]
"""Define a string field representing an importable attribute.

This annotated type validates that the string corresponds to an attribute that can be
imported.
"""

TimedeltaSeconds = Annotated[
    timedelta,
    PlainSerializer(lambda v: round(v.total_seconds()), return_type=int),
]
"""Define a timedelta field serialized as total seconds.

This annotated type serializes a `timedelta` object to an integer representing the
total number of seconds.
"""

UTCDatetime = Annotated[datetime, AfterValidator(make_datetime_utc)]
"""Define a datetime field converted to UTC.

This annotated type ensures that the `datetime` object is converted to UTC timezone.
"""

UniqueList = Annotated[list[T], AfterValidator(unique_everseen), AfterValidator(list)]
"""A list subclass that ensures all elements are unique.

This class can be used with type parameters (e.g., `UniqueList[int]`) to create
a list type where duplicates are automatically removed.
"""

UniqueTuple = Annotated[
    tuple[T, ...], AfterValidator(unique_everseen), AfterValidator(tuple)
]
"""A tuple subclass that ensures all elements are unique.

This class can be used with type parameters (e.g., `UniqueTuple[int]`) to create
a tuple type where duplicates are automatically removed.
"""

LenientStr = Annotated[str, BeforeValidator(str)]
"""A string field that accepts any input and converts it to a string."""

LowercaseStr = Annotated[str, StringConstraints(to_lower=True)]
"""A string field that is automatically converted to lowercase."""

FilenameExtension = Annotated[
    LowercaseStr, AfterValidator(lambda v: "." + v.lstrip("."))
]
"""A string field representing a filename extension.

This annotated type ensures that the string is lowercase and starts with a single dot.
"""

MimeType = Annotated[LowercaseStr, StringConstraints(pattern=r"^\w+\/[-+.\w]+$")]
"""A string type representing a MIME type.

This annotated type ensures that the string is lowercase and matches the MIME type
format.
"""
