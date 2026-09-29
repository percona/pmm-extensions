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

"""Define settings for support snippets in the PMM Extensions app."""

__all__ = [
    "SnippetFilter",
    "SnippetFilterType",
    "SnippetInterpreterConfig",
    "snippets_settings",
]

import re
from collections import OrderedDict
from contextlib import suppress
from enum import Enum, StrEnum
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any, ClassVar, NamedTuple, Self

from pydantic import (
    BaseModel,
    field_validator,
    GetCoreSchemaHandler,
    model_validator,
    PositiveInt,
    ValidationError,
)
from pydantic_core import core_schema
from sqlalchemy_celery_beat.models import Period

from app.core.celery.models import IntervalSchedule
from app.core.config import BaseYamlSettings
from app.core.settings_override.constants import SNIPPETS_SETTINGS
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    hot_field,
    materialize_via_owning_model,
)
from app.core.utils import run_pydantic_type_validator, validate_module_is_importable
from app.core.utils.dict import merge_dict_at_start, transform_dict_keys
from app.core.utils.fields import (
    EnumFieldMixin,
    FilenameExtension,
    MimeType,
    NonEmptyStr,
    RelativeDirectoryPathField,
    URL,
)

DEFAULT_SNIPPETS_TASK = "exec-artifact"


class SnippetSudoRequirement(StrEnum):
    """Name the three elevation states a snippet's metadata distinguishes."""

    NEVER = "never"
    OPTIONAL = "optional"
    ALWAYS = "always"


class SnippetSudoOption(EnumFieldMixin, Enum):
    """Enumerate options for executing a snippet with sudo."""

    NEVER = 0
    ALWAYS = 1
    OPTIONAL = 2
    OPTIONAL_DEFAULT_TRUE = 3
    OPTIONAL_DEFAULT_FALSE = OPTIONAL

    @property
    def is_optional(self) -> bool:
        """Return whether this option renders a sudo checkbox.

        :return: `True` for optional variants, `False` otherwise.
        :rtype: bool
        """
        return self in (
            SnippetSudoOption.OPTIONAL,
            SnippetSudoOption.OPTIONAL_DEFAULT_TRUE,
        )

    @property
    def sudo_default(self) -> bool:
        """Return the default checked state for the sudo checkbox.

        :return: `True` only for `OPTIONAL_DEFAULT_TRUE`.
        :rtype: bool
        """
        return self == SnippetSudoOption.OPTIONAL_DEFAULT_TRUE

    @property
    def requirement(self) -> SnippetSudoRequirement:
        """Return the declared elevation state as its wire vocabulary.

        Collapses the enum's four members onto three states: both optional variants
        report ``OPTIONAL``, the default-checked nuance being carried separately by
        :attr:`sudo_default` for a consumer that needs it.

        :return: The wire name for this option's elevation state.
        """
        if self == SnippetSudoOption.ALWAYS:
            return SnippetSudoRequirement.ALWAYS
        if self.is_optional:
            return SnippetSudoRequirement.OPTIONAL
        return SnippetSudoRequirement.NEVER


class SnippetFilterType(EnumFieldMixin, StrEnum):
    """Define enumeration of snippet filter validation types.

    :cvar EXTENSION: Represents filtering by file extension.
    :vartype EXTENSION: type[FilenameExtension]
    :cvar MIME_TYPE: Represents filtering by MIME type.
    :vartype MIME_TYPE: type[MimeType]
    """

    EXTENSION = "ext"
    MIME_TYPE = "mime"

    @classmethod
    @lru_cache(maxsize=2)
    def get_validation_type(cls, filter_type: str) -> Any:
        """Get the validation type associated with the specified filter type.

        Annotated ``Any`` because both results are ``Annotated[str, ...]``
        aliases — type expressions rather than classes.

        :param filter_type: The filter type value.
        :return: The corresponding validation type.
        :raises ValueError: If the filter type is unknown.
        """
        if filter_type == cls.EXTENSION:
            return FilenameExtension
        if filter_type == cls.MIME_TYPE:
            return MimeType
        raise ValueError(f"Unknown filter type: {filter_type!r}")

    @classmethod
    def validate_value(cls, filter_type: str, value: Any) -> Any:
        """Validate the value based on the filter type.

        :param filter_type: The filter type value.
        :type filter_type: str
        :param value: The value to validate.
        :type value: Any
        :return: The validated value.
        :rtype: Any
        :raises ValueError: If the filter type is unknown.
        :raises ValidationError: If the value is invalid for the specified filter type.
        """
        return run_pydantic_type_validator(cls.get_validation_type(filter_type), value)


class SnippetFilter(NamedTuple):
    """Define a filter entity for snippets.

    :param value: The value of the filter, such as a specific file extension or MIME
        type.
    :type value: str
    :param type: The type of filter, either by file extension or MIME type. Defaults to
        :attr:`SnippetFilterType.EXTENSION`.
    :type type: SnippetFilterType
    """

    value: str
    type: SnippetFilterType = SnippetFilterType.EXTENSION

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        """Get the Pydantic core schema for SnippetFilter.

        This method defines a two-step validation process:
        1. Convert from string to SnippetFilter using the internal `_create_from_str`.
        2. Normalize the value based on its type using `normalize_value`.

        :param source_type: The source type for validation.
        :type source_type: Any
        :param handler: The handler for core schema retrieval.
        :type handler: GetCoreSchemaHandler
        :return: The core schema incorporating the two-step validation.
        :rtype: core_schema.CoreSchema
        """
        with_before = core_schema.no_info_before_validator_function(
            cls._create_from_str, handler(source_type)
        )
        return core_schema.no_info_after_validator_function(
            cls.normalize_value, with_before
        )

    @classmethod
    def _create_from_str(cls, data: Any) -> Any:
        """Create a SnippetFilter instance from a string.

        If the input is a string, it should be in the format "type:value", where "type"
        is optional and defaults to "extension". If not, it's returned as is.

        :param data: The input data, which can be a string or anything else.
        :type data: Any
        :return: A SnippetFilter instance or the original data if it's not a
            string.
        :rtype: Any
        """
        if isinstance(data, str):
            filter_type_value_delimiter = ":"
            if filter_type_value_delimiter in data:
                type_str, value = data.split(filter_type_value_delimiter, 1)
                data: dict[str, Any] = {"value": value}
                with suppress(ValidationError):
                    data["type"] = run_pydantic_type_validator(
                        SnippetFilterType, type_str.lower()
                    )
                return cls(**data)
            return cls(value=data)
        return data

    @classmethod
    def create_from_str(cls, data: str) -> Self:
        """Create a SnippetFilter instance from a string.

        :param data: The input string in the format "type:value" or just "value".
        :type data: str
        :return: A SnippetFilter instance.
        :rtype: SnippetFilter
        """
        return run_pydantic_type_validator(cls, data)

    @classmethod
    def normalize_value(cls, entity: "SnippetFilter") -> Self:
        """Normalize the value of the filter entity based on its type.

        :param entity: The SnippetFilter instance to normalize.
        :type entity: SnippetFilter
        :return: A new SnippetFilter instance with the normalized value.
        :rtype: SnippetFilter
        """
        return cls(
            value=SnippetFilterType.validate_value(entity.type, entity.value),
            type=entity.type,
        )


class SnippetInterpreterConfig(BaseModel):
    """Define an interpreter configuration for snippets.

    :param command: The interpreter command to use.
    :type command: str
    :param task: Optional task name override for executing snippets with this
        interpreter.
    :type task: str | None
    :param task_with_requirements: Optional task name to use when the snippet metadata
        includes `requires_packages`.
    :type task_with_requirements: str | None
    """

    command: str
    task: str = DEFAULT_SNIPPETS_TASK
    task_with_requirements: str = DEFAULT_SNIPPETS_TASK

    @model_validator(mode="before")
    @classmethod
    def from_str(cls, data: Any) -> Any:
        """Create a SnippetInterpreterConfig instance from a string.

        If the input is a string, it is treated as the `command`. If it's already
        a SnippetInterpreterConfig instance, it is returned as is.

        :param data: The input data, which can be a string or a
            SnippetInterpreterConfig instance.
        :type data: Any
        :return: A SnippetInterpreterConfig instance or the original data if it's
            already an instance.
        :rtype: Any
        """
        if isinstance(data, str):
            return {"command": data}
        return data


_BASH_INTERPRETER_CONFIG = SnippetInterpreterConfig(command="bash")
_PYTHON_INTERPRETER_CONFIG = SnippetInterpreterConfig(
    command="python3", task_with_requirements="exec-python-artifact"
)
DEFAULT_INTERPRETERS = OrderedDict(
    {
        SnippetFilter(".sh", SnippetFilterType.EXTENSION): _BASH_INTERPRETER_CONFIG,
        SnippetFilter(
            "text/x-shellscript", SnippetFilterType.MIME_TYPE
        ): _BASH_INTERPRETER_CONFIG,
        SnippetFilter(
            "text/x-sh", SnippetFilterType.MIME_TYPE
        ): _BASH_INTERPRETER_CONFIG,
        SnippetFilter(
            "application/x-shellscript", SnippetFilterType.MIME_TYPE
        ): _BASH_INTERPRETER_CONFIG,
        SnippetFilter(
            "application/x-sh", SnippetFilterType.MIME_TYPE
        ): _BASH_INTERPRETER_CONFIG,
        SnippetFilter(".py", SnippetFilterType.EXTENSION): _PYTHON_INTERPRETER_CONFIG,
        SnippetFilter(
            "text/x-python", SnippetFilterType.MIME_TYPE
        ): _PYTHON_INTERPRETER_CONFIG,
        SnippetFilter(
            "text/x-script.python", SnippetFilterType.MIME_TYPE
        ): _PYTHON_INTERPRETER_CONFIG,
    }
)


class SnippetsMetaOptions(BaseModel):
    """Define metadata options for snippets.

    :param LINE_PATTERN: Regular expression to match metadata lines in the snippet file.
        Use a group named "line" to capture only a part of the line. Defaults to
        `r"^# (?P<line>.+)$"`.
    :type LINE_PATTERN: re.Pattern[str]
    :param DELIMITER: The delimiter indicating the start/end of metadata in a snippet.
        Defaults to `"---"`.
    :type DELIMITER: NonEmptyStr
    :param STOP_SEARCH_PATTERN: Regular expression to stop searching for metadata lines.
        Defaults to `r"r"^[^#].+$"`.
    :type STOP_SEARCH_PATTERN: re.Pattern[str]
    :param DEFAULT_ALLOW_EXTRA_ARGS: Whether to allow extra arguments by default.
        Defaults to `False`.
    :type DEFAULT_ALLOW_EXTRA_ARGS: bool
    :param DEFAULT_SUDO_OPTION: The default option for executing snippets with sudo.
        Defaults to `SnippetSudoOption.NEVER`.
    :type DEFAULT_SUDO_OPTION: SnippetSudoOption
    :param DEFAULT_ARG_FORMAT: The default format for arguments. Use `${name}` and
        `${value}` as placeholders. Defaults to `"--$name $value"`.
    :type DEFAULT_ARG_FORMAT: NonEmptyStr
    :param IGNORE_INVALID_PARAMETERS: Whether to ignore parameters with errors. If
        `True`, such parameters are skipped in execution validation and a warning is
        logged. If `False`, an error is logged and the snippet execution is blocked.
        Defaults to `False`.
    :type IGNORE_INVALID_PARAMETERS: bool
    """

    LINE_PATTERN: re.Pattern[str] = re.compile(r"^# (?P<line>.+)$")
    DELIMITER: NonEmptyStr = "---"
    STOP_SEARCH_PATTERN: re.Pattern[str] = re.compile(r"^[^#].+$")
    DEFAULT_ALLOW_EXTRA_ARGS: bool = False
    DEFAULT_SUDO_OPTION: SnippetSudoOption = SnippetSudoOption.NEVER
    DEFAULT_ARG_FORMAT: NonEmptyStr = "--$name $value"
    IGNORE_INVALID_PARAMETERS: bool = False

    @field_validator("DEFAULT_ARG_FORMAT")
    @classmethod
    def _validate_value_identifier_is_in_template(cls, v: str) -> str:
        """Ensure the 'value' placeholder is present in the argument format string.

        :param v: The argument format string to validate.
        :type v: str
        :return: The validated argument format string.
        :rtype: str
        :raises ValueError: If the 'value' placeholder is missing.
        """
        if "value" not in Template(v).get_identifiers():
            raise ValueError(
                f"Invalid argument format string '{v}': the 'value' placeholder is required."
                f" Use '${{value}}' to include it."
            )
        return v


class SnippetsSettings(BaseYamlSettings):
    """Define configuration options for support snippets.

    :cvar SETTINGS_PREFIXES: The prefixes for snippets related settings in the
        configuration file. Set to `["EXTENSIONS", "SNIPPETS"]`.
    :param SNIPPETS_DIR: The directory containing support snippets. Defaults to
        `Path("snippets")`.
    :param SNIPPETS_BASE_URL: The base URL for accessing snippets. If `None`, the URL
        is dynamically built on execution. Defaults to `None`. Its path is preserved,
        with download URLs appended to it rather than replacing it, so it must
        already include ``EXTENSIONS.ROOT_PATH`` when a URL prefix is configured.
    :param META: Metadata options for snippets. See `SnippetsMetaOptions`.
    :param SYNC_FILTER: A set of filters to apply when loading snippets from
        `SNIPPETS_DIR`. Each filter can specify a file extension or MIME type. If
        `None`, no filtering is applied. Defaults to `None`.
    :param INTERPRETERS: A mapping of `SnippetFilter` to interpreter configs. This
        defines which interpreter to use for snippets matching the specified filter.
        Defaults to a predefined set of common script types.
    :param USE_MAGIC: Whether to use the `python-magic` package to determine file types.
        Defaults to `False`. If `True`, the `python-magic` package must be installed.
    :param SYNC_INTERVAL: The interval schedule for synchronizing snippets. Defaults to
        every 1 hour.
    :param ENABLE_MANUAL_SYNC: Whether to enable manual synchronization of snippets.
        Defaults to `False`.
    :param AUTO_APPROVE_BUILTIN_SNIPPETS: Whether sync may auto-approve snippets
        whose filename and SHA-256 digest match the built-in checksum manifest.
        Defaults to ``True``. When ``False``, sync never auto-approves any snippet.
    :param SYNC_ON_STARTUP: Whether to synchronize snippets on application startup.
        Defaults to `True`.
    :param PREVIEW_MAX_CHARS: The maximum number of characters to include in the snippet
        preview. Defaults to 10,000.
    :param PREVIEW_MAX_LINES: The maximum number of lines to include in the snippet
        preview. Defaults to 500.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "SNIPPETS"]
    SNIPPETS_DIR: RelativeDirectoryPathField = Path("snippets")
    # ``URL`` is a non-Pydantic Starlette type: the generic JSON serializer
    # stores it as ``{"_url": ...}`` rather than a plain string, which then
    # coerces back into a broken ``URL``. Persist the raw string and
    # re-materialize through the owning model on load instead.
    SNIPPETS_BASE_URL: URL | None = hot_field(  # ty: ignore[invalid-assignment]
        None, materializer=materialize_via_owning_model
    )
    META: SnippetsMetaOptions = SnippetsMetaOptions()
    SYNC_FILTER: set[SnippetFilter] | None = (  # ty: ignore[invalid-assignment]
        hot_field(None)
    )
    INTERPRETERS: OrderedDict[SnippetFilter, SnippetInterpreterConfig] = (
        DEFAULT_INTERPRETERS
    )
    USE_MAGIC: bool = False
    SYNC_INTERVAL: IntervalSchedule = hot_field(  # ty: ignore[invalid-assignment]
        IntervalSchedule(every=1, period=Period.HOURS)
    )
    ENABLE_MANUAL_SYNC: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=False
    )
    AUTO_APPROVE_BUILTIN_SNIPPETS: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=True
    )
    SYNC_ON_STARTUP: bool = True
    PREVIEW_MAX_CHARS: PositiveInt = hot_field(10000)  # ty: ignore[invalid-assignment]
    PREVIEW_MAX_LINES: PositiveInt = hot_field(500)  # ty: ignore[invalid-assignment]

    @model_validator(mode="before")
    @classmethod
    def merge_interpreters(cls, data: Any) -> Any:
        """Merge default interpreters with any user-defined interpreters.

        User-defined interpreters can be added via `INTERPRETERS`, or by using
        `INTERPRETERS_PREPEND` and/or `INTERPRETERS_APPEND` to add entries after the
        defaults.

        :param data: The input data to validate.
        :return: The validated data with merged interpreters.
        """
        if isinstance(data, dict) and "INTERPRETERS" not in data:
            data["INTERPRETERS"] = DEFAULT_INTERPRETERS
            if (
                (prepend_interpreters := data.pop("INTERPRETERS_PREPEND", None))
                and prepend_interpreters
                and isinstance(prepend_interpreters, dict)
            ):
                data["INTERPRETERS"] = merge_dict_at_start(
                    data["INTERPRETERS"],
                    transform_dict_keys(
                        prepend_interpreters, SnippetFilter.create_from_str
                    ),
                )
            if (
                (append_interpreters := data.pop("INTERPRETERS_APPEND", None))
                and append_interpreters
                and isinstance(append_interpreters, dict)
            ):
                data["INTERPRETERS"] |= append_interpreters
        return data

    @field_validator("USE_MAGIC")
    @classmethod
    def _validate_python_magic_is_installed(cls, v: bool) -> bool:  # noqa: FBT001
        """Ensure the 'python-magic' package is installed if USE_MAGIC is `True`.

        :param v: The value of the USE_MAGIC setting.
        :type v: bool
        :return: The validated value of USE_MAGIC.
        :rtype: bool
        :raises ValueError: If USE_MAGIC is `True` but the 'python-magic' package
            is not installed.
        """
        if v:
            try:
                validate_module_is_importable("magic")
            except ImportError:
                raise ValueError(
                    "The 'python-magic' package is required for this feature."
                ) from None
        return v


snippets_settings: SnippetsSettings = OverridableSettingsProxy(
    SnippetsSettings, setting_class=SNIPPETS_SETTINGS
)
