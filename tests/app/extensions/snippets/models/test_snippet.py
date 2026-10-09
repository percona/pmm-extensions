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

"""Tests for BaseSnippet and Snippet models."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.extensions.apps.field_names import RESERVED_EXECUTION_FIELD_NAMES
from app.extensions.snippets.config import (
    DEFAULT_SNIPPETS_TASK,
    snippets_settings,
    SnippetSudoOption,
)
from app.extensions.snippets.models.snippet import (
    BaseSnippet,
    BaseSnippetArgs,
    EXECUTOR_HOSTS_INPUT_NAME,
    EXTRA_ARGS_INPUT_NAME,
    Snippet,
)

EXPECTED_PARAM_COUNT = 2
EXPECTED_RESERVED_ERROR_COUNT = 2
UPDATED_SIZE = 200
MD5_DIGEST_LENGTH = 32


@pytest.fixture
def sample_snippet_file(tmp_path):
    """Create a temporary snippet file with YAML frontmatter."""
    content = (
        "# ---\n"
        "# title: Test Snippet\n"
        "# description: A test snippet for unit testing\n"
        "# parameters:\n"
        "#   - name: host\n"
        "#     type: str\n"
        "#     required: true\n"
        "# ---\n"
        "echo 'hello'\n"
    )
    snippet_file = tmp_path / "test_snippet.sh"
    snippet_file.write_text(content)
    return snippet_file


@pytest.fixture
def sample_snippet_no_frontmatter(tmp_path):
    """Create a temporary snippet file without frontmatter."""
    content = "echo 'hello world'\n"
    snippet_file = tmp_path / "plain_snippet.sh"
    snippet_file.write_text(content)
    return snippet_file


class TestGetMetaByPath:
    """Test the get_meta_by_path static method."""

    @pytest.mark.asyncio
    async def test_with_frontmatter(self, sample_snippet_file):
        """Verify metadata extraction from file with YAML frontmatter."""
        meta = await BaseSnippet.get_meta_by_path(sample_snippet_file)
        assert meta["title"] == "Test Snippet"
        assert meta["description"] == "A test snippet for unit testing"
        assert isinstance(meta["parameters"], list)
        assert len(meta["parameters"]) == 1
        assert meta["parameters"][0]["name"] == "host"

    @pytest.mark.asyncio
    async def test_no_frontmatter(self, sample_snippet_no_frontmatter):
        """Verify empty dict returned for file without frontmatter."""
        meta = await BaseSnippet.get_meta_by_path(sample_snippet_no_frontmatter)
        assert meta == {}

    @pytest.mark.asyncio
    async def test_binary_file_returns_empty(self, tmp_path):
        """Verify empty dict returned for binary file (UnicodeDecodeError)."""
        binary_file = tmp_path / "binary.bin"
        binary_file.write_bytes(b"\x00\x01\x02\xff\xfe")
        meta = await BaseSnippet.get_meta_by_path(binary_file)
        assert meta == {}


class TestBaseSnippetProperties:
    """Test BaseSnippet cached properties."""

    def test_title_from_meta(self):
        """Verify title is extracted from meta."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"title": "My Title"},
        )
        assert snippet.title == "My Title"

    def test_title_defaults_to_filename(self):
        """Verify title defaults to filename when not in meta."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert snippet.title == "test.sh"

    def test_description_from_meta(self):
        """Verify description is extracted from meta."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"description": "A useful snippet"},
        )
        assert snippet.description == "A useful snippet"

    def test_description_defaults_to_empty(self):
        """Verify description defaults to empty string."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert snippet.description == ""

    @pytest.mark.parametrize("declared", [None, "", "   ", "\t\n"])
    def test_title_falls_back_for_blank_declared_value(self, declared):
        """Verify a blank declared title falls back to the filename."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"title": declared},
        )
        assert snippet.title == "test.sh"

    @pytest.mark.parametrize("declared", [None, "", "   ", "\t\n"])
    def test_description_falls_back_for_blank_declared_value(self, declared):
        """Verify a blank declared description falls back to an empty string."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"description": declared},
        )
        assert snippet.description == ""

    def test_title_preserves_padded_value(self):
        """Verify a padded but non-blank title is returned unchanged."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"title": "  My Title  "},
        )
        assert snippet.title == "  My Title  "

    def test_description_preserves_padded_value(self):
        """Verify a padded but non-blank description is returned unchanged."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"description": "  A useful snippet  "},
        )
        assert snippet.description == "  A useful snippet  "

    @pytest.mark.parametrize(
        ("declared", "expected"), [(5, "5"), (True, "True"), (1.5, "1.5")]
    )
    def test_title_renders_a_non_string_declared_value_as_text(
        self, declared, expected
    ):
        """Verify a non-string declared title is rendered as text.

        ``meta`` is whatever YAML parsed, and the consumers type both properties
        ``str``, so handing on the raw scalar fails the whole response.
        """
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"title": declared},
        )
        assert snippet.title == expected

    def test_description_renders_a_non_string_declared_value_as_text(self):
        """Verify a non-string declared description is rendered as text."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"description": 42},
        )
        assert snippet.description == "42"

    def test_service_type_keeps_blank_declared_value(self):
        """Verify the blank-aware fallback does not extend to service_type."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"service_type": ""},
        )
        assert snippet.service_type == ""

    def test_service_type_from_meta(self):
        """Verify service_type is extracted from meta."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"service_type": "mysql"},
        )
        assert snippet.service_type == "mysql"

    def test_service_type_defaults_to_none(self):
        """Verify service_type defaults to None when not in meta."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert snippet.service_type is None

    def test_path_property(self):
        """Verify path returns full path based on BASE_DIR."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        expected = Path(snippets_settings.SNIPPETS_DIR / "test.sh")
        assert snippet.path == expected

    def test_repr(self):
        """Verify __repr__ includes filename and hash."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert "test.sh" in repr(snippet)
        assert "a" * 32 in repr(snippet)

    def test_str(self):
        """Verify __str__ returns filename."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert str(snippet) == "test.sh"

    def test_fspath(self):
        """Verify __fspath__ returns full path string."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert snippet.__fspath__() == str(snippets_settings.SNIPPETS_DIR / "test.sh")

    def test_allow_extra_args_from_meta(self):
        """Verify allow_extra_args is read from meta."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"allow_extra_args": True},
        )
        assert snippet.allow_extra_args is True

    def test_allow_extra_args_defaults(self):
        """Verify allow_extra_args defaults to settings value."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert (
            snippet.allow_extra_args == snippets_settings.META.DEFAULT_ALLOW_EXTRA_ARGS
        )

    def test_sudo_from_meta(self):
        """Verify sudo option is read from meta."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"sudo": 1},
        )
        assert snippet.sudo == SnippetSudoOption.ALWAYS

    def test_sudo_invalid_defaults(self):
        """Verify invalid sudo value falls back to default."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"sudo": "invalid"},
        )
        assert snippet.sudo == snippets_settings.META.DEFAULT_SUDO_OPTION

    def test_mime_type(self):
        """Verify mime_type uses guess_mime_type."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert isinstance(snippet.mime_type, str)

    def test_requirements_with_packages(self):
        """Verify requirements returns joined package string."""
        snippet = BaseSnippet(
            filename="test.py",
            size=100,
            md5_digest="a" * 32,
            meta={"requires_packages": ["requests", "boto3"]},
        )
        assert snippet.requirements == "requests\nboto3"

    def test_requirements_without_packages(self):
        """Verify requirements returns None when no packages."""
        snippet = BaseSnippet(filename="test.py", size=100, md5_digest="a" * 32)
        assert snippet.requirements is None


@pytest.fixture(autouse=True)
def _clear_interpreter_config_cache():
    """Clear the ttl_cache on _get_execution_interpreter_config between tests.

    The cache is keyed by the resolved snippet ``Path`` (which includes
    ``BASE_DIR``). Same-filename tests share a deterministic bucket, but
    ``TestFromPath`` mutates ``BASE_DIR``; clearing the cache on teardown keeps
    cases independent.
    """
    yield
    BaseSnippet._get_execution_interpreter_config.cache_clear()


class TestExecutionInterpreter:
    """Test the execution_interpreter and execution_task_name properties.

    These tests drive the real ``snippets_settings.INTERPRETERS`` lookup by
    varying the snippet ``filename`` and ``meta`` — no mocking of the subject
    under test. ``.sh`` resolves to bash, ``.py`` to python3, and an unmapped
    extension (``.xyz``) resolves to no interpreter.
    """

    def test_interpreter_found(self):
        """Verify the real .sh lookup resolves to the bash interpreter."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert snippet.execution_interpreter == "bash"

    def test_interpreter_not_found(self):
        """Verify an unmapped extension resolves to no interpreter."""
        snippet = BaseSnippet(filename="test.xyz", size=100, md5_digest="a" * 32)
        assert snippet.execution_interpreter is None

    def test_task_name_default_when_no_interpreter(self):
        """Verify default task name when no interpreter config is found."""
        snippet = BaseSnippet(filename="test.xyz", size=100, md5_digest="a" * 32)
        assert snippet.execution_task_name == DEFAULT_SNIPPETS_TASK

    def test_task_name_with_requirements(self):
        """Verify task_with_requirements is used when a .py snippet has packages."""
        snippet = BaseSnippet(
            filename="test.py",
            size=100,
            md5_digest="a" * 32,
            meta={"requires_packages": ["requests"]},
        )
        assert snippet.execution_task_name == "exec-python-artifact"

    def test_task_name_without_requirements(self):
        """Verify the default task is used when a .sh snippet has no packages."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert snippet.execution_task_name == DEFAULT_SNIPPETS_TASK


class TestGetExecutionModel:
    """Test the get_execution_model method."""

    def test_returns_model_class(self):
        """Verify get_execution_model returns a BaseSnippetArgs subclass."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "host", "type": "str", "required": True},
                ]
            },
        )
        model = snippet.get_execution_model()
        assert issubclass(model, BaseSnippetArgs)

    def test_model_validates_input(self):
        """Verify the generated model can validate input data."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "host", "type": "str", "required": True},
                ]
            },
        )
        model = snippet.get_execution_model()
        instance = model(**{"-hostname-": "server1", "host": "db-host"})
        assert instance.executor_host == "server1"

    def test_binds_extra_args_via_new_schema_alias(self):
        """Verify a value keyed by the schema-driven ``extra_args`` name binds."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"allow_extra_args": True, "parameters": []},
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {EXECUTOR_HOSTS_INPUT_NAME: "server1", "extra_args": "--verbose --foo"}
        )
        assert instance.extra_args == ["--verbose", "--foo"]

    def test_binds_extra_args_via_legacy_alias(self):
        """Verify the legacy ``-extra_args-`` alias still binds alongside the new one."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"allow_extra_args": True, "parameters": []},
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {EXECUTOR_HOSTS_INPUT_NAME: "server1", EXTRA_ARGS_INPUT_NAME: "--verbose"}
        )
        assert instance.extra_args == ["--verbose"]

    def test_extra_args_field_absent_when_not_allowed(self):
        """Verify the generated model declares no ``extra_args`` field by default."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": []},
        )
        model = snippet.get_execution_model()
        assert "extra_args" not in model.model_fields

    def test_extra_args_key_ignored_when_not_allowed(self):
        """Verify submitting ``extra_args`` for a non-opted-in snippet is silently dropped."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": []},
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {EXECUTOR_HOSTS_INPUT_NAME: "server1", "extra_args": "--verbose"}
        )
        assert instance.to_args_string() == ""


class TestToArgsString:
    """Test datetime CLI argument serialization via to_args_string."""

    @staticmethod
    def _snippet_with_parameters(parameters):
        return BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": parameters},
        )

    def test_datetime_serializes_with_t_separator(self):
        """Verify datetime values render as YYYY-MM-DDTHH:MM:SS in CLI args."""
        snippet = self._snippet_with_parameters(
            [{"name": "start", "type": "datetime", "required": False}]
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {
                EXECUTOR_HOSTS_INPUT_NAME: "host1",
                "start": "2024-06-10T14:30:00",
            }
        )

        args = instance.to_args_string()

        assert "--start" in args.split()
        assert "2024-06-10T14:30:00" in args
        assert "2024-06-10 14:30:00" not in args

    def test_empty_start_and_end_omitted_from_args(self):
        """Verify blank optional datetime params are excluded from CLI args."""
        snippet = self._snippet_with_parameters(
            [
                {"name": "start", "type": "datetime", "required": False},
                {"name": "end", "type": "datetime", "required": False},
            ]
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {
                EXECUTOR_HOSTS_INPUT_NAME: "host1",
                "start": "",
                "end": "",
            }
        )

        args = instance.to_args_string()

        assert "--start" not in args
        assert "--end" not in args

    def test_datetime_without_seconds_serializes_with_zero_seconds(self):
        """Verify datetime-local input without seconds normalises to :00 in CLI args."""
        snippet = self._snippet_with_parameters(
            [{"name": "start", "type": "datetime", "required": False}]
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {
                EXECUTOR_HOSTS_INPUT_NAME: "host1",
                "start": "2024-06-10T14:30",
            }
        )

        args = instance.to_args_string()

        assert "2024-06-10T14:30:00" in args

    def test_positional_datetime_serializes_as_string(self):
        """Verify positional datetime params use serialize_cli_value, not raw objects."""
        snippet = self._snippet_with_parameters(
            [
                {
                    "name": "timestamp",
                    "type": "datetime",
                    "positional": True,
                    "required": True,
                }
            ]
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {
                EXECUTOR_HOSTS_INPUT_NAME: "host1",
                "timestamp": "2024-06-10T14:30:00",
            }
        )

        args = instance.to_args_string()

        assert args == "2024-06-10T14:30:00"
        assert "2024-06-10 14:30:00" not in args

    def test_extra_args_from_new_alias_reach_command_string(self):
        """Verify a value bound via the schema-driven alias reaches the built command."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"allow_extra_args": True, "parameters": []},
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {EXECUTOR_HOSTS_INPUT_NAME: "host1", "extra_args": "--verbose"}
        )

        args = instance.to_args_string()

        assert "--verbose" in args.split()

    def test_empty_extra_args_produces_no_stray_tokens(self):
        """Verify a blank Extra Args value appends nothing to the command."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"allow_extra_args": True, "parameters": []},
        )
        model = snippet.get_execution_model()
        instance = model.model_validate(
            {EXECUTOR_HOSTS_INPUT_NAME: "host1", "extra_args": ""}
        )

        assert instance.to_args_string() == ""


class TestValidatedParameters:
    """Test the validated_parameters cached property."""

    def test_all_valid_parameters(self):
        """Verify valid parameters produce no errors."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "host", "type": "str", "required": True},
                    {"name": "port", "type": "int", "default": 3306},
                ]
            },
        )
        result = snippet.validated_parameters
        assert len(result.parameters) == EXPECTED_PARAM_COUNT
        assert len(result.errors) == 0

    def test_with_invalid_parameters(self):
        """Verify invalid parameters produce error list."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "", "type": "str"},
                ]
            },
        )
        result = snippet.validated_parameters
        assert len(result.errors) > 0

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    def test_reserved_named_parameter_is_rejected(self, reserved_name: str) -> None:
        """Drop a parameter reserved for a synthesized execution field.

        Regression test for a wire-name collision: an ordinary parameter
        sharing its wire name with a synthesized execution field would
        otherwise make the schema builders raise a duplicate-field error, or a
        submitted value silently double-bind in the execution model. It is now
        rejected like any other invalid parameter, before either path is
        reached.
        """
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": [{"name": reserved_name, "type": "str"}]},
        )
        result = snippet.validated_parameters
        assert len(result.parameters) == 0
        assert any(
            "reserved" in error and reserved_name in error for error in result.errors
        )

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    def test_reserved_named_parameter_blocks_execution(
        self, reserved_name: str
    ) -> None:
        """Block execution of a snippet declaring a reserved parameter name."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": [{"name": reserved_name, "type": "str"}]},
        )
        assert snippet.can_execute is False

    def test_every_reserved_named_parameter_is_reported(self) -> None:
        """Report one error per reserved name and keep the valid siblings."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "sudo", "type": "str"},
                    {"name": "mode", "type": "str"},
                    {"name": "extra_args", "type": "str"},
                ]
            },
        )
        result = snippet.validated_parameters
        assert [param.name for param in result.parameters] == ["mode"]
        assert len(result.errors) == EXPECTED_RESERVED_ERROR_COUNT
        assert any("'sudo'" in error for error in result.errors)
        assert any("'extra_args'" in error for error in result.errors)

    def test_reserved_named_parameter_is_not_reported_as_unknown_reference(
        self,
    ) -> None:
        """Keep a sibling's reference to a rejected parameter unflagged.

        Visibility references are checked against the names the frontmatter
        *declared*, not the ones that survived validation, so a rejected
        parameter is reported once as reserved rather than twice, once as
        reserved and again as an unknown reference.
        """
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "sudo", "type": "str"},
                    {
                        "name": "mode",
                        "type": "str",
                        "visible_when": {"parameter": "sudo", "equals": "1"},
                    },
                ]
            },
        )
        errors = snippet.validated_parameters.errors
        assert len(errors) == 1
        assert "reserved" in errors[0]

    def test_no_parameters(self):
        """Verify empty parameters produce no errors."""
        snippet = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        result = snippet.validated_parameters
        assert len(result.parameters) == 0
        assert len(result.errors) == 0

    def test_visibility_condition_referencing_declared_param(self):
        """A condition referencing a declared sibling produces no errors."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "list", "type": "bool"},
                    {"name": "start", "type": "str", "visible_when_not": "list"},
                ]
            },
        )
        result = snippet.validated_parameters
        assert len(result.errors) == 0
        assert len(result.parameters) == EXPECTED_PARAM_COUNT

    def test_visibility_condition_referencing_unknown_param(self):
        """A condition referencing an undeclared sibling surfaces an error."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "start", "type": "str", "visible_when_not": "nope"},
                ]
            },
        )
        result = snippet.validated_parameters
        assert len(result.errors) > 0
        assert any("nope" in e for e in result.errors)

    def test_visibility_condition_referencing_invalid_declared_param(self):
        """A reference to a declared-but-invalid sibling is not 'unknown'.

        The sibling fails its own validation, but because it is still declared in
        the meta the reference must not be misreported as an unknown parameter.
        """
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "list", "type": "str", "min_length": 0},
                    {"name": "start", "type": "str", "visible_when_not": "list"},
                ]
            },
        )
        result = snippet.validated_parameters
        assert len(result.errors) > 0
        assert not any("unknown parameter" in e for e in result.errors)

    @pytest.mark.parametrize(
        "attr",
        ["requires_when", "requires_when_not", "forbidden_when", "forbidden_when_not"],
    )
    def test_gate_referencing_declared_param(self, attr):
        """A gate referencing a declared sibling produces no errors."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "mode", "type": "bool"},
                    {"name": "reason", "type": "str", attr: "mode"},
                ]
            },
        )
        result = snippet.validated_parameters
        assert len(result.errors) == 0
        assert len(result.parameters) == EXPECTED_PARAM_COUNT

    @pytest.mark.parametrize(
        "attr",
        ["requires_when", "requires_when_not", "forbidden_when", "forbidden_when_not"],
    )
    def test_gate_referencing_unknown_param(self, attr):
        """A gate referencing an undeclared sibling surfaces an error."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": [{"name": "reason", "type": "str", attr: "nope"}]},
        )
        result = snippet.validated_parameters
        assert len(result.errors) > 0
        assert any("nope" in e and attr in e for e in result.errors)


class TestCanExecute:
    """Test the can_execute property."""

    def test_with_interpreter_and_valid_params(self):
        """Verify can_execute is True with a real interpreter and valid params."""
        snippet = BaseSnippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            meta={"parameters": [{"name": "host", "type": "str"}]},
        )
        assert snippet.can_execute is True

    def test_no_interpreter(self):
        """Verify can_execute is False when no interpreter resolves.

        The snippet has no parameters, so there are no validation errors; a
        ``False`` result can only stem from the unresolved interpreter.
        """
        snippet = BaseSnippet(
            filename="test.unknown",
            size=100,
            md5_digest="a" * 32,
        )
        assert snippet.can_execute is False


class TestSnippetModel:
    """Test the Snippet model (extends BaseSnippet with DB fields)."""

    def test_is_approved_when_set(self):
        """Verify is_approved returns True when approved_at is set."""
        from app.core.utils import utc_now  # noqa: PLC0415

        snippet = Snippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            approved_at=utc_now(),
        )
        assert snippet.is_approved is True

    def test_is_not_approved_when_none(self):
        """Verify is_approved returns False when approved_at is None."""
        snippet = Snippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            approved_at=None,
        )
        assert snippet.is_approved is False

    def test_approve_sets_fields(self):
        """Verify approve() sets approved_at, updated_by, and reason."""
        snippet = Snippet(filename="test.sh", size=100, md5_digest="a" * 32)
        snippet.approve("Security reviewed", "user-123")
        assert snippet.approved_at is not None
        assert snippet.updated_by == "user-123"
        assert snippet.reason == "Security reviewed"

    def test_remove_approval_clears_approved_at(self):
        """Verify remove_approval() sets approved_at to None."""
        from app.core.utils import utc_now  # noqa: PLC0415

        snippet = Snippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            approved_at=utc_now(),
        )
        snippet.remove_approval("No longer valid", "user-456")
        assert snippet.approved_at is None
        assert snippet.updated_by == "user-456"
        assert snippet.reason == "No longer valid"

    def test_can_execute_requires_approval(self):
        """Verify Snippet.can_execute requires approval on top of base check.

        The base ``BaseSnippet.can_execute`` resolves to ``True`` for this
        snippet (bash interpreter, no parameter errors), so the unapproved
        ``Snippet`` returning ``False`` proves the approval gate is what blocks
        it — not a coincidentally-failing base check.
        """
        base = BaseSnippet(filename="test.sh", size=100, md5_digest="a" * 32)
        assert base.can_execute is True

        snippet = Snippet(
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            approved_at=None,
        )
        assert snippet.can_execute is False

    @pytest.mark.asyncio
    async def test_update_from_snippet(self):
        """Verify content updates preserve approval fields."""
        from app.core.utils import utc_now  # noqa: PLC0415

        approved_at = utc_now()
        original = Snippet(
            id=1,
            filename="test.sh",
            size=100,
            md5_digest="a" * 32,
            approved_at=approved_at,
            updated_by="user-123",
            reason="Approved by admin",
        )
        new_snippet = Snippet(
            filename="test.sh",
            size=UPDATED_SIZE,
            md5_digest="b" * 32,
        )
        with patch.object(
            BaseSnippet,
            "get_meta_by_path",
            new=AsyncMock(return_value={"title": "Updated"}),
        ):
            await original.update_from_snippet(new_snippet)
        assert original.id == 1
        assert original.md5_digest == "b" * 32
        assert original.size == UPDATED_SIZE
        assert original.meta == {"title": "Updated"}
        assert original.approved_at == approved_at
        assert original.updated_by == "user-123"
        assert original.reason == "Approved by admin"

    def test_is_human_revoked_when_unapproved_with_user(self):
        """Verify an administrator revocation is detected.

        Writer contract: both Jinja and API revoke routes pass a real user id
        into ``remove_approval``, which leaves ``updated_by`` set while clearing
        ``approved_at``. Sync relies on that signal to avoid re-approving.
        """
        snippet = Snippet(filename="test.sh", size=100, md5_digest="a" * 32)
        snippet.remove_approval("Approval removed by admin", "user-456")
        assert snippet.is_human_revoked is True
        assert snippet.updated_by == "user-456"

    def test_is_human_revoked_false_for_automatic_removal(self):
        """Verify automatic approval removals are not treated as human revocations.

        Writer contract: sync content-change clears pass ``user_id=None`` so
        ``is_human_revoked`` stays false and a later matching sync may re-approve.
        """
        snippet = Snippet(filename="test.sh", size=100, md5_digest="a" * 32)
        snippet.approve("Approved by admin", "user-123")
        snippet.remove_approval("File contents have changed", None)
        assert snippet.is_human_revoked is False
        assert snippet.updated_by is None

    def test_is_human_revoked_false_while_approved_with_user(self):
        """Verify approval writers that set ``updated_by`` are not treated as revoked.

        Writer contract: batch approve and single approve leave ``updated_by`` set
        together with ``approved_at``, so ``is_human_revoked`` stays false.
        """
        snippet = Snippet(filename="test.sh", size=100, md5_digest="a" * 32)
        snippet.approve("Batch approved by admin", "user-789")
        assert snippet.is_approved is True
        assert snippet.updated_by == "user-789"
        assert snippet.is_human_revoked is False


class TestFromPath:
    """Test the from_path class method."""

    @pytest.fixture
    def base_dir(self, tmp_path, monkeypatch):
        """Redirect BaseSnippet.BASE_DIR to a tmp_path via a pytest seam."""
        monkeypatch.setattr(BaseSnippet, "BASE_DIR", tmp_path)
        return tmp_path

    @pytest.mark.asyncio
    async def test_creates_snippet_from_file(self, base_dir):
        """Verify from_path creates a snippet with correct hash and filename."""
        content = "echo hello\n"
        snippet_file = base_dir / "hello.sh"
        snippet_file.write_text(content, encoding="utf-8")

        snippet = await BaseSnippet.from_path("hello.sh")

        assert snippet.filename == "hello.sh"
        assert len(snippet.md5_digest) == MD5_DIGEST_LENGTH
        assert (
            snippet.md5_digest
            == hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        )
        assert snippet.size > 0

    @pytest.mark.asyncio
    async def test_from_path_with_update_meta(self, base_dir):
        """Verify from_path calls update_meta when flag is True."""
        content = "# ---\n# title: Hello Script\n# ---\necho hello\n"
        snippet_file = base_dir / "hello.sh"
        snippet_file.write_text(content)

        snippet = await BaseSnippet.from_path("hello.sh", update_meta=True)

        assert snippet.meta.get("title") == "Hello Script"


class TestRequiresPackages:
    """Test the requires_packages cached property."""

    def test_string_packages(self):
        """Verify string requires_packages is split into list."""
        snippet = BaseSnippet(
            filename="test.py",
            size=100,
            md5_digest="a" * 32,
            meta={"requires_packages": "requests boto3"},
        )
        assert snippet.requires_packages == ["requests", "boto3"]

    def test_list_packages(self):
        """Verify list requires_packages is returned as-is with normalization."""
        snippet = BaseSnippet(
            filename="test.py",
            size=100,
            md5_digest="a" * 32,
            meta={"requires_packages": ["requests", "boto3"]},
        )
        assert snippet.requires_packages == ["requests", "boto3"]

    def test_empty_packages(self):
        """Verify empty requires_packages returns empty list."""
        snippet = BaseSnippet(filename="test.py", size=100, md5_digest="a" * 32)
        assert snippet.requires_packages == []

    def test_none_packages(self):
        """Verify None requires_packages returns empty list."""
        snippet = BaseSnippet(
            filename="test.py",
            size=100,
            md5_digest="a" * 32,
            meta={"requires_packages": None},
        )
        assert snippet.requires_packages == []
