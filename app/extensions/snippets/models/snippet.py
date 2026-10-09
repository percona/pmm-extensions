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

"""Define the main models for the snippets feature as part of the PMM Extensions app."""

__all__ = ["BaseSnippetArgs", "Snippet", "SnippetExecutionMeta"]


import json
import logging
import shlex
from functools import cached_property
from io import SEEK_END
from os import PathLike
from pathlib import Path
from string import Template
from typing import Annotated, Any, ClassVar, NamedTuple, Self, TYPE_CHECKING

import aiofiles
import yaml
from aiofiles.ospath import getsize
from async_lru import alru_cache
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    computed_field,
    create_model,
    Field,
    PositiveInt,
    validate_call,
    ValidationError,
)
from sqlalchemy import Column, Index, JSON
from sqlmodel import Field as SQLField

from app.core.db import BaseSQLModel
from app.core.db.models import DateTimeWithTimezone
from app.core.utils import (
    json_serializer,
    run_pydantic_type_validator,
    ttl_cache,
    utc_now,
)
from app.core.utils.cli_args import render_value_arg
from app.core.utils.fields import (
    EmptyStrToNone,
    FilePathLike,
    NonEmptyStr,
    StrAnyUrl,
    UTCDatetime,
)
from app.core.utils.pydantic import CustomFieldMetadata
from app.extensions.apps.field_names import EXTRA_ARGS_FIELD_NAME, SUDO_FIELD_NAME
from app.extensions.snippets.checksums import digest_file
from app.extensions.snippets.config import (
    DEFAULT_SNIPPETS_TASK,
    SnippetFilterType,
    SnippetInterpreterConfig,
    snippets_settings,
    SnippetSudoOption,
)

if TYPE_CHECKING:
    from aiofiles.threadpool.text import AsyncTextIOWrapper
from app.extensions.snippets.models.meta import (
    META_KEY_ALLOW_EXTRA_ARGS,
    META_KEY_DESCRIPTION,
    META_KEY_PARAMETERS,
    META_KEY_REQUIRES_PACKAGES,
    META_KEY_SERVICE_TYPE,
    META_KEY_SUDO,
    META_KEY_TITLE,
    serialize_cli_value,
    SnippetMetaParameter,
    SnippetMetaParametersValidationResult,
)
from app.extensions.snippets.utils import generate_unique_identifiers, guess_mime_type

_ONE_HOUR = 60 * 60
_SEVEN_DAYS = 7 * 24 * _ONE_HOUR
EXECUTOR_HOSTS_INPUT_NAME = "-hostname-"
EXTRA_ARGS_INPUT_NAME = "-extra_args-"
SUDO_INPUT_NAME = "-sudo-"

logger = logging.getLogger(__name__)

ExtraArgsField = Annotated[list[str], BeforeValidator(shlex.split)]


def _meta_or_default(meta: dict[str, Any], key: str, default: str) -> str:
    """Read ``key`` from snippet metadata, treating a blank declared value as absent.

    Frontmatter reaches ``meta`` through ``yaml.safe_load``, which maps a valueless
    ``key:`` to ``None`` and ``key: ""`` to ``""``. Both are present keys, so a
    ``dict.get`` default never fires for them and the declared blank is handed to
    consumers whose fields reject it. A padded but non-blank value is returned
    unchanged, keeping this read consistent with ``SnippetManager.list_query_spec``'s
    ``_meta_text(META_KEY_TITLE)``, which sorts and searches on the value as declared
    — unlike ``_service_type_exprs`` alongside it, which trims before comparing.

    ``meta`` is untyped, so a non-string value such as ``title: 5`` is rendered as
    text rather than passed through: the ``str`` fields downstream reject an ``int``
    for the whole response, which is the same page-wide failure a blank value caused.
    A sequence or mapping takes the same path and renders as its ``repr``.

    :param meta: The snippet's parsed frontmatter mapping.
    :param key: The metadata key to read.
    :param default: The value to return when the key is absent or declared blank.
    :return: The declared value as text, or ``default`` when it is missing, ``None``,
        or whitespace-only.
    """
    value = meta.get(key)
    if value is None:
        return default
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else default


class FilePreview(NamedTuple):
    """Represent a preview of a snippet's content with separated frontmatter.

    :param preamble: Lines before frontmatter (e.g. shebang).
    :type preamble: str
    :param frontmatter: Raw frontmatter including ``# ---`` delimiters.
    :type frontmatter: str
    :param content: Code body after frontmatter, limited by preview settings.
    :type content: str
    :param is_truncated: Whether the preview content is truncated (i.e., if the full
        content exceeds the preview limits).
    :type is_truncated: bool
    """

    preamble: str
    frontmatter: str
    content: str
    is_truncated: bool

    @property
    def full_content(self) -> str:
        """Return the complete preview content including preamble and frontmatter.

        :return: Concatenation of preamble, frontmatter, and code body.
        :rtype: str
        """
        return self.preamble + self.frontmatter + self.content

    @classmethod
    @validate_call
    async def from_path(
        cls,
        path: FilePathLike,
        max_chars: PositiveInt,
        max_lines: PositiveInt,
        **_kwargs: Any,
    ) -> Self:
        """Create a FilePreview from a path.

        Read the file at ``path``, split frontmatter from code body, and generate a
        preview limited by character and line counts applied only to the code body.

        :param path: The file path to read for generating the preview.
        :type path: str | bytes | PathLike
        :param max_chars: The maximum number of characters to include in the preview.
        :type max_chars: PositiveInt
        :param max_lines: The maximum number of lines to include in the preview.
        :type max_lines: PositiveInt
        :return: A ``FilePreview`` instance containing the preview content and whether it
            is truncated.
        :rtype: FilePreview
        """
        return await cls._from_path(path, max_chars, max_lines, **_kwargs)

    @staticmethod
    async def _parse_frontmatter(
        f: "AsyncTextIOWrapper",
        max_scan_lines: int,
    ) -> tuple[list[str], list[str]]:
        """Read lines from an open file and separate preamble from frontmatter.

        Stop when the closing ``# ---`` delimiter is found, when a non-comment
        line is encountered before any delimiter, at EOF, or after scanning
        ``max_scan_lines`` lines without finding a closing delimiter.

        :param f: An open async text file handle positioned at the start.
        :type f: AsyncTextIOWrapper
        :param max_scan_lines: Safety bound on lines to scan for frontmatter.
        :type max_scan_lines: int
        :return: A ``(preamble_lines, frontmatter_lines)`` pair.
        :rtype: tuple[list[str], list[str]]
        """
        meta = snippets_settings.META
        preamble_lines = []
        frontmatter_lines = []
        in_frontmatter = False
        lines_scanned = 0

        while lines_scanned < max_scan_lines:
            line = await f.readline()
            if not line:
                break
            lines_scanned += 1
            if meta.STOP_SEARCH_PATTERN.match(line) and not in_frontmatter:
                preamble_lines.append(line)
                break
            match = meta.LINE_PATTERN.match(line)
            if not match:
                (frontmatter_lines if in_frontmatter else preamble_lines).append(line)
                continue
            matched_content = match.groupdict().get("line", match.group(0))
            if matched_content == meta.DELIMITER:
                frontmatter_lines.append(line)
                if in_frontmatter:
                    break
                in_frontmatter = True
            elif in_frontmatter:
                frontmatter_lines.append(line)
            else:
                preamble_lines.append(line)

        if in_frontmatter and lines_scanned >= max_scan_lines:
            preamble_lines = preamble_lines + frontmatter_lines
            frontmatter_lines = []

        return preamble_lines, frontmatter_lines

    @classmethod
    @alru_cache(ttl=_ONE_HOUR, maxsize=16)
    async def _from_path(
        cls, path: Path, max_chars: int, max_lines: int, **_kwargs: Any
    ) -> Self:
        logger.debug("Generating preview for file: %s", path)
        async with aiofiles.open(path) as f:
            preamble_raw, frontmatter_raw = await cls._parse_frontmatter(f, max_lines)

            if frontmatter_raw:
                preamble_str = "".join(preamble_raw)
                frontmatter_str = "".join(frontmatter_raw)
                initial_content = ""
            else:
                preamble_str = ""
                frontmatter_str = ""
                initial_content = "".join(preamble_raw)

            content = initial_content
            line_number = content.count("\n")
            while len(content) < max_chars and line_number < max_lines:
                line = await f.readline()
                if not line:
                    break
                content += line
                line_number += 1

            preview_content = content[:max_chars]
            is_truncated = content != preview_content or await f.tell() < await f.seek(
                0, SEEK_END
            )
        return cls(
            preamble=preamble_str,
            frontmatter=frontmatter_str,
            content=preview_content,
            is_truncated=is_truncated,
        )


class BaseSnippetArgs(BaseModel):
    """Validate the arguments a snippet execution was submitted with.

    :cvar extra_args_field: The name of the field used to store extra arguments
        (``extra_args``).
    :cvar sudo_field: The name of the field used to store the sudo toggle (``sudo``).
    :param executor_host: The hostname of the target system where the snippet will be
        executed.
    """

    extra_args_field: ClassVar[str] = EXTRA_ARGS_FIELD_NAME
    sudo_field: ClassVar[str] = SUDO_FIELD_NAME
    executor_host: NonEmptyStr = Field(
        validation_alias=EXECUTOR_HOSTS_INPUT_NAME, exclude=True
    )

    def to_args_string(self) -> str:
        """Convert the model instance to a command-line argument string.

        :return: A string representing the command-line arguments.
        :rtype: str
        """
        args = [
            arg
            for field_identifier, value in self.model_dump(
                exclude={self.extra_args_field, self.sudo_field}, exclude_none=True
            ).items()
            for arg in self.format_args(field_identifier, value)
        ]
        return shlex.join(args + self.get_extra_args())

    def get_extra_args(self) -> list[str]:
        """Get the extra arguments from the model instance.

        :return: A list of extra arguments.
        :rtype: list[str]
        """
        return self.model_dump(include={self.extra_args_field}).get(
            self.extra_args_field, []
        )

    @classmethod
    def format_args(cls, field_identifier: str, value: Any) -> list[str]:
        """Format a field and its value as command-line arguments.

        :param field_identifier: The identifier of the field to format.
        :type field_identifier: str
        :param value: The value of the field to format.
        :type value: Any
        :return: A list of command-line arguments representing the field and its value.
        :rtype: list[str]
        """
        field_name, metadata = cls.get_field_metadata(field_identifier)
        if metadata.get("positional"):
            return [serialize_cli_value(value)]
        if (is_flag := metadata.get("is_flag")) and not value:
            return []
        if not (arg_format := metadata.get("arg_format")):
            if is_flag:
                return [f"--{field_name}"]
            arg_format = Template(
                snippets_settings.META.DEFAULT_ARG_FORMAT
            ).safe_substitute(name=field_name)
        return render_value_arg(arg_format, value, stringify=serialize_cli_value)

    @classmethod
    def get_field_metadata(cls, name: str) -> tuple[str, dict]:
        """Get the metadata for a specific field in the model.

        :param name: The name of the field to retrieve metadata for.
        :type name: str
        :return: A tuple containing the field's serialization alias (or name if no alias
            is set) and a dictionary of the field's metadata.
        :rtype: tuple[str, dict]
        """
        field = cls.model_fields[name]
        return field.serialization_alias or name, CustomFieldMetadata.field_to_dict(
            field, strict=True
        )


class BaseSnippet(BaseModel):
    """Base model for an executable snippet.

    :cvar BASE_DIR: The base directory where snippets are stored.
    :vartype BASE_DIR: ClassVar[Path]
    :param filename: The snippet filename.
    :type filename: str
    :param size: The size of the snippet file in bytes.
    :type size: PositiveInt
    :param md5_digest: The MD5 hash digest of the snippet file.
    :type md5_digest: str
    :param meta: Additional metadata about the snippet, such as title, description,
        parameters, etc.
    :type meta: dict[str, Any]
    """

    BASE_DIR: ClassVar[Path] = snippets_settings.SNIPPETS_DIR
    filename: str = Field(min_length=1, max_length=255)
    size: PositiveInt
    md5_digest: str = Field(min_length=32, max_length=32)
    meta: dict[str, Any] = Field(default_factory=dict)

    def __repr__(self) -> str:
        return f"'{self.filename}' ({self.md5_digest})"

    def __str__(self) -> str:
        return self.filename

    def __fspath__(self) -> str:
        return str(self.BASE_DIR / self.filename)

    @cached_property
    def title(self) -> str:
        """Get the title of the snippet.

        :return: The title of the snippet, or the filename when the metadata declares
            no title or declares a blank one.
        """
        return _meta_or_default(self.meta, META_KEY_TITLE, self.filename)

    @cached_property
    def description(self) -> str:
        """Get the description of the snippet.

        :return: The description of the snippet, or an empty string when the metadata
            declares no description or declares a blank one.
        """
        return _meta_or_default(self.meta, META_KEY_DESCRIPTION, "")

    @cached_property
    def service_type(self) -> str | None:
        """Get the free-form service type of the snippet.

        :return: The ``service_type`` declared in the snippet metadata (for
            example ``"mysql"`` or ``"mongodb"``), or ``None`` when the snippet
            declares no service type. This free-form frontmatter string is
            distinct from the inventory ``ServiceTypeEnum``.
        :rtype: str | None
        """
        return self.meta.get(META_KEY_SERVICE_TYPE)

    @cached_property
    def allow_extra_args(self) -> bool:
        """Determine whether extra arguments are allowed for the snippet.

        :return: ``True`` if extra arguments are allowed, else ``False``. Defaults to the
            value specified in the snippets settings if not explicitly set in the
            metadata.
        :rtype: bool
        """
        return self.meta.get(
            META_KEY_ALLOW_EXTRA_ARGS, snippets_settings.META.DEFAULT_ALLOW_EXTRA_ARGS
        )

    @cached_property
    def sudo(self) -> SnippetSudoOption:
        """Get the sudo option for the snippet.

        :return: The sudo option for the snippet. Defaults to the value specified in the
            snippets settings if not explicitly set in the metadata or if the value is
            invalid.
        :rtype: SnippetSudoOption
        """
        try:
            return run_pydantic_type_validator(
                SnippetSudoOption, self.meta.get(META_KEY_SUDO)
            )
        except ValidationError:
            return snippets_settings.META.DEFAULT_SUDO_OPTION

    @cached_property
    def path(self) -> Path:
        """Get the full path to the snippet file.

        :return: The full path to the snippet file.
        :rtype: Path
        """
        return Path(self)

    @cached_property
    def mime_type(self) -> str:
        """Get the MIME type of the snippet.

        :return: The MIME type of the snippet.
        :rtype: str
        """
        return guess_mime_type(self.path)

    @property
    def can_execute(self) -> bool:
        """Determine whether the snippet can be executed.

        A snippet can be executed if it has a valid executor
        interpreter, and either parameter errors are ignored in the settings or there
        are no validation errors in the parameters.

        :return: ``True`` if the snippet can be executed, else ``False``.
        :rtype: bool
        """
        return self.execution_interpreter is not None and (
            snippets_settings.META.IGNORE_INVALID_PARAMETERS
            or not self.validated_parameters.errors
        )

    @cached_property
    def validated_parameters(self) -> SnippetMetaParametersValidationResult:
        """Get the validated parameters of the snippet.

        :return: A ``SnippetMetaParametersValidationResult`` instance containing the list
            of valid parameters and any validation errors encountered.
        :rtype: SnippetMetaParametersValidationResult
        """
        parameters = self.meta.get(META_KEY_PARAMETERS, [])
        return self._get_parameters_from_json(
            json_serializer(parameters, sort_keys=True),
        )

    @property
    def execution_interpreter(self) -> str | None:
        """Get the interpreter for executing the snippet.

        :return: The interpreter for executing the snippet, or None if no interpreter is
            found.
        :rtype: str | None
        """
        interpreter_config = self._get_execution_interpreter_config(self.path)
        if interpreter_config is None:
            return None
        return interpreter_config.command

    @property
    def execution_task_name(self) -> str:
        """Get the task name for executing the snippet.

        :return: The task name for executing the snippet, or None if no interpreter is
            found.
        :rtype: str | None
        """
        interpreter_config = self._get_execution_interpreter_config(self.path)
        if interpreter_config is None:
            return DEFAULT_SNIPPETS_TASK
        if self.requirements:
            return interpreter_config.task_with_requirements
        return interpreter_config.task

    @cached_property
    def requires_packages(self) -> list[str]:
        """Return a normalized list of required packages from metadata.

        :return: A list of required package names.
        :rtype: list[str]
        """
        value = self.meta.get(META_KEY_REQUIRES_PACKAGES)
        if not value:
            return []
        if isinstance(value, str):
            return value.strip().split()
        return [
            normalized_item for item in value if (normalized_item := str(item).strip())
        ]

    @cached_property
    def requirements(self) -> str | None:
        """Get the requirements for the snippet.

        :return: A string containing the requirements for the snippet, or None if there
            are no requirements.
        :rtype: str | None
        """
        if requirements_list := self.requires_packages:
            return "\n".join(requirements_list)
        return None

    async def get_preview(self) -> FilePreview:
        """Get a preview of the snippet code.

        :return: A :class:`FilePreview` instance containing a preview of the snippet
            code.
        :rtype: FilePreview
        """
        return await FilePreview.from_path(
            self,
            snippets_settings.PREVIEW_MAX_CHARS,
            snippets_settings.PREVIEW_MAX_LINES,
            file_hash=self.md5_digest,
        )

    async def update_meta(self) -> None:
        """Update the snippet's metadata."""
        self.meta = await self.get_meta_by_path(self)

    def get_execution_model(self) -> type[BaseSnippetArgs]:
        """Generate a Pydantic model for validating snippet execution parameters.

        This method creates a dynamic Pydantic model based on the snippet's metadata,
        which can be used to validate the parameters required for executing the snippet.

        :return: A Pydantic model class for validating the snippet's execution
            parameters.
        :rtype: type[BaseSnippetArgs]
        """
        parameters = self.meta.get(META_KEY_PARAMETERS, [])
        logger.debug("Meta Snippet parameters: %s)", parameters)
        return self._get_execution_model(
            json_serializer(parameters, sort_keys=True),
            add_extra_args_field=self.allow_extra_args,
            add_sudo_field=self.sudo.is_optional,
            sudo_default=self.sudo.sudo_default,
        )

    @staticmethod
    async def get_meta_by_path(path: str | bytes | PathLike) -> dict[str, Any]:
        """Extract metadata from a snippet file.

        This method reads the first few lines of the file and extracts metadata
        according to the specified patterns. It returns a dictionary with the
        extracted metadata.

        :param path: The path to the snippet file.
        :type path: str | bytes | PathLike
        :return: A dictionary containing the extracted metadata.
        :rtype: dict[str, Any]
        """
        try:
            async with aiofiles.open(path) as f:
                line = await f.readline()
                front_matter_lines = None
                while not snippets_settings.META.STOP_SEARCH_PATTERN.match(line):
                    match = snippets_settings.META.LINE_PATTERN.match(line)
                    if match:
                        content = match.groupdict().get("line", match.group(0))
                        if content == snippets_settings.META.DELIMITER:
                            if front_matter_lines is not None:
                                break
                            front_matter_lines = []
                        elif front_matter_lines is not None:
                            front_matter_lines.append(content)
                    line = await f.readline()
            return (
                yaml.safe_load("\n".join(front_matter_lines))
                if front_matter_lines
                else {}
            )
        except UnicodeDecodeError:
            return {}

    @staticmethod
    @ttl_cache(ttl=_ONE_HOUR, maxsize=8)
    def _get_parameters_from_json(
        parameters_json: str,
    ) -> SnippetMetaParametersValidationResult:
        """Parse and validate snippet parameters from a JSON string.

        :param parameters_json: A JSON string representing a list of snippet parameters.
        :type parameters_json: str
        :return: A SnippetMetaParametersValidationResult instance containing the list of
            valid parameters and any validation errors encountered.
        :rtype: SnippetMetaParametersValidationResult
        """
        parameters = json.loads(parameters_json) or []
        if not isinstance(parameters, list):
            error_msg = f"Invalid snippet parameters, expected a list but got {parameters.__class__.__name__}"
            logger.warning("%s: %r", error_msg, parameters)
            return SnippetMetaParametersValidationResult(
                parameters=[], errors=[error_msg]
            )
        valid_parameters = []
        errors = []
        for param in parameters:
            try:
                valid_parameters.append(SnippetMetaParameter.model_validate(param))
            except ValidationError as exc:
                logger.warning("Invalid snippet parameter %r", param, exc_info=True)
                errors.extend(
                    SnippetMetaParameter.convert_validation_errors(exc, param)
                )
        declared = {
            name
            for param in parameters
            if (name := param.get("name") if isinstance(param, dict) else None)
            is not None
        }
        errors.extend(
            BaseSnippet._validate_visibility_references(valid_parameters, declared)
        )
        return SnippetMetaParametersValidationResult(
            parameters=valid_parameters, errors=errors
        )

    @staticmethod
    def _validate_visibility_references(
        parameters: list[SnippetMetaParameter],
        declared: set[str],
    ) -> list[str]:
        """Validate that visibility/gate conditions reference declared parameters.

        A ``visible_when`` / ``visible_when_not`` visibility condition or a
        ``requires_when`` / ``requires_when_not`` / ``forbidden_when`` /
        ``forbidden_when_not`` gate may only reference a sibling parameter declared
        in the same snippet. References to unknown parameters are surfaced as
        errors consistent with the per-parameter validation output.

        The declared-name set is derived best-effort from the raw parameter
        declarations so that a sibling which is declared but fails its own
        validation is not misreported as an "unknown parameter" reference.

        :param parameters: The successfully validated snippet parameters.
        :type parameters: list[SnippetMetaParameter]
        :param declared: The set of parameter names declared in the snippet meta,
            including those whose own validation failed.
        :type declared: set[str]
        :return: A list of error messages for unknown references.
        :rtype: list[str]
        """
        errors = []
        for param in parameters:
            for attr in (
                "visible_when",
                "visible_when_not",
                "requires_when",
                "requires_when_not",
                "forbidden_when",
                "forbidden_when_not",
            ):
                condition = getattr(param, attr)
                if condition is not None and condition.parameter not in declared:
                    errors.append(
                        f"Parameter error ({param.name!r}) at {f'{attr}.parameter'!r}: "
                        f"references unknown parameter {condition.parameter!r}"
                    )
        return errors

    @staticmethod
    @ttl_cache(ttl=_ONE_HOUR, maxsize=8)
    def _get_execution_model(
        parameters_json: str,
        *,
        add_extra_args_field: bool = False,
        add_sudo_field: bool = False,
        sudo_default: bool = False,
    ) -> type[BaseSnippetArgs]:
        """Generate a Pydantic model for validating snippet execution parameters.

        This internal method creates a dynamic Pydantic model based on the snippet's
        parameters. It is cached for performance.

        :param parameters_json: A JSON string representing a list of snippet parameters.
        :type parameters_json: str
        :param add_extra_args_field: Whether to include an extra arguments field in the
            model. Defaults to ``False``.
        :type add_extra_args_field: bool
        :param add_sudo_field: Whether to include a sudo field in the model. Defaults to
            ``False``.
        :type add_sudo_field: bool
        :param sudo_default: The default value for the sudo field. Only relevant when
            ``add_sudo_field`` is ``True``. Defaults to ``False``.
        :type sudo_default: bool
        :return: A Pydantic model class for validating the snippet's execution
            parameters.
        :rtype: type[BaseSnippetArgs]
        """
        parameters = BaseSnippet._get_parameters_from_json(parameters_json).parameters
        logger.debug(
            "Snippet params: %s",
            [param.name for param in parameters if not param.hidden],
        )
        unique_identifiers = generate_unique_identifiers()
        fields = {}
        positional_fields = {}
        for param in parameters:
            field_name = next(unique_identifiers)
            field = (param.validation_type, param.to_validation_field())
            logger.debug(
                "Generated snippet model field from param %s: %s", param.name, field
            )
            if param.positional:
                positional_fields[field_name] = field
            else:
                fields[field_name] = field

        if add_extra_args_field:
            fields[BaseSnippetArgs.extra_args_field] = (
                ExtraArgsField,
                Field(
                    default_factory=list,
                    validation_alias=AliasChoices(
                        EXTRA_ARGS_INPUT_NAME, EXTRA_ARGS_FIELD_NAME
                    ),
                    serialization_alias=EXTRA_ARGS_FIELD_NAME,
                ),
            )

        if add_sudo_field:
            fields[BaseSnippetArgs.sudo_field] = (
                bool,
                Field(
                    default=sudo_default,
                    alias=SUDO_INPUT_NAME,
                ),
            )

        return create_model(
            "SnippetArgs",
            **fields,
            **positional_fields,
            __base__=BaseSnippetArgs,
        )

    @staticmethod
    @ttl_cache(ttl=_ONE_HOUR, maxsize=16)
    def _get_execution_interpreter_config(
        snippet_path: Path,
    ) -> SnippetInterpreterConfig | None:
        interpreters = snippets_settings.INTERPRETERS
        snippet_filters = [
            (snippet_path.suffix.lower(), SnippetFilterType.EXTENSION),
            (guess_mime_type(snippet_path), SnippetFilterType.MIME_TYPE),
        ]
        snippet_filters.sort(
            key=lambda f: (
                f in interpreters and len(interpreters) - list(interpreters).index(f)
            ),
            reverse=True,
        )
        return interpreters.get(snippet_filters[0])

    @classmethod
    async def from_path(
        cls, path: str | bytes | PathLike, *, update_meta: bool = False
    ) -> Self:
        """Create a new Snippet instance from a file path.

        This method computes the MD5 hash digest of the file at the specified path and
        instantiates a new Snippet with the filename and computed MD5 hash.

        :param path: A path-like object pointing to the snippet file.
        :type path: str | bytes | PathLike
        :param update_meta: Whether to update the snippet's metadata after creation.
            Defaults to False.
        :type update_meta: bool
        :return: A new instance of Snippet with the corresponding filename and
            md5_digest.
        :rtype: Snippet
        """
        path = cls.BASE_DIR / Path(path)
        snippet = cls(
            filename=str(path.relative_to(cls.BASE_DIR)),
            md5_digest=await digest_file(path, "md5", usedforsecurity=False),
            size=await getsize(path),
        )
        if update_meta:
            await snippet.update_meta()
        return snippet


class Snippet(BaseSnippet, BaseSQLModel, table=True):
    """Represent a support snippet stored in the database.

    :param filename: The snippet filename. Must be unique.
    :type filename: str
    :param size: The size of the snippet file in bytes.
    :type size: PositiveInt
    :param md5_digest: The MD5 hash digest of the snippet file.
    :type md5_digest: str
    :param approved_at: The approval time for the snippet, or None if the snippet is not
        approved.
    :type approved_at: UTCDatetime | None
    :param reason: The reason for the approval or disapproval of the snippet, if any.
        Defaults to "New snippet".
    :type reason: str
    :param meta: Additional metadata about the snippet, such as title, description,
        parameters, etc.
    :type meta: dict[str, Any]
    """

    __table_args__ = (Index("ix_snippet_filename", "filename", unique=True),)
    approved_at: UTCDatetime | None = SQLField(
        sa_type=DateTimeWithTimezone,  # ty: ignore[invalid-argument-type]
        default=None,
        index=True,
    )
    updated_by: str | None = None
    reason: str = "New snippet"
    meta: dict[str, Any] = SQLField(
        sa_column=Column(JSON, nullable=False),
        default_factory=dict,
    )

    @computed_field
    @property
    def is_approved(self) -> bool:
        """Determine whether the snippet has been approved.

        :return: True if the snippet is approved (i.e. approved_at is not None), else
            False.
        :rtype: bool
        """
        return self.approved_at is not None

    @property
    def is_human_revoked(self) -> bool:
        """Return whether an administrator explicitly removed this snippet's approval.

        Human revocation is recorded as an unapproved row whose ``updated_by`` is
        still set to a user id. Automatic content-change removals clear
        ``updated_by``.

        :return: ``True`` when the snippet is unapproved and ``updated_by`` is set.
        """
        return self.approved_at is None and self.updated_by is not None

    @property
    def can_execute(self) -> bool:
        """Determine whether the snippet can be executed.

        A snippet can be executed if it is approved, it has a valid executor
        interpreter, and either parameter errors are ignored in the settings or there
        are no validation errors in the parameters.

        :return: ``True`` if the snippet can be executed, else ``False``.
        :rtype: bool
        """
        return self.is_approved and super().can_execute

    async def update_from_snippet(self, snippet: "Snippet") -> None:
        """Update file-derived fields from another snippet without changing approval.

        Leave ``approved_at``, ``updated_by``, and ``reason`` unchanged.

        :param snippet: The snippet from which to copy file-derived fields.
        """
        self.sqlmodel_update(
            snippet,
            update={
                "id": self.id,
                "approved_at": self.approved_at,
                "updated_by": self.updated_by,
                "reason": self.reason,
            },
        )
        self.meta = await self.get_meta_by_path(self)

    def approve(self, reason: str, user_id: str) -> None:
        """Mark the snippet as approved.

        Set the snippet's approved_at to the current time and change the reason.

        :param reason: The reason for the approval of the snippet.
        :type reason: str
        :param user_id: The ID of the user approving the snippet.
        :type user_id: str
        """
        self.approved_at = utc_now()
        self.updated_by = user_id
        self.reason = reason

    def remove_approval(self, reason: str, user_id: str | None) -> None:
        """Mark the snippet as unapproved.

        Set the snippet's approved_at to None and change the reason.

        :param reason: The reason for the approval removal of the snippet.
        :type reason: str
        :param user_id: The ID of the user removing the approval of the snippet.
        :type user_id: str | None
        """
        self.approved_at = None
        self.updated_by = user_id
        self.reason = reason


class SnippetExecutionMeta(BaseModel):
    """Metadata required for executing a snippet.

    :param target: The target hostname where the snippet will be executed.
    :type target: NonEmptyStr
    :param interpreter: The interpreter to use for executing the snippet (e.g., bash,
        python).
    :type interpreter: NonEmptyStr
    :param snippet_source: The URL to the snippet source file.
    :type snippet_source: StrAnyUrl
    :param md5_checksum: The MD5 checksum of the snippet file to verify integrity.
    :type md5_checksum: str
    :param snippet_filename: The filename of the snippet.
    :type snippet_filename: NonEmptyStr
    :param args: Additional command-line arguments to pass to the snippet during
        execution.
    :type args: NonEmptyStr | None
    :param requirements: Optional requirements list to install when executing python
        snippets.
    :type requirements: NonEmptyStr | None
    """

    target: NonEmptyStr
    interpreter: NonEmptyStr
    snippet_source: StrAnyUrl
    snippet_filename: NonEmptyStr = Field(..., serialization_alias="_snippet_filename")
    md5_checksum: str = Field(min_length=32, max_length=32)
    args: NonEmptyStr | EmptyStrToNone = None
    requirements: NonEmptyStr | EmptyStrToNone = None

    @computed_field
    @property
    def _job_id_prefix(self) -> str:
        """Get a prefix for job IDs based on the snippet filename.

        :return: A string prefix for job IDs.
        :rtype: str
        """
        return self.snippet_filename
