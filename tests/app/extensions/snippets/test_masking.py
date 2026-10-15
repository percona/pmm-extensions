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

"""Cover credential masking of a recorded snippet argument string."""

import logging
import shlex
from typing import Any

import pytest
from pydantic import BaseModel, create_model, Field

from app.extensions.snippets.config import SnippetSudoOption
from app.extensions.snippets.masking import (
    _is_credential_name,
    _MASK_PLACEHOLDER,
    _masking_spec,
    is_credential_parameter,
    mask_snippet_args,
    masked_args_for,
    SENSITIVE_ARG_MASK,
)
from app.extensions.snippets.models.snippet import (
    BaseSnippet,
    BaseSnippetArgs,
    EXECUTOR_HOSTS_INPUT_NAME,
)

REPEATED_MASK_CALLS = 5


def _model(parameters: list[dict[str, Any]]) -> type[BaseSnippetArgs]:
    """Return the execution model a snippet with ``parameters`` would validate against.

    :param parameters: The frontmatter parameter definitions.
    :return: The snippet's dynamic execution model.
    """
    snippet = BaseSnippet(
        filename="masking-target.sh",
        size=100,
        md5_digest="a" * 32,
        meta={"parameters": parameters},
    )
    return snippet.get_execution_model()


def _args(model: type, **values: Any) -> str:
    """Return the argument string the model renders for ``values``.

    Building the string through the production renderer rather than hand-writing
    it keeps every case honest about what a real execution would have recorded.

    :param model: The snippet execution model to validate against.
    :param values: The frontmatter-named parameter values.
    :return: The ``shlex.join``ed argument string.
    """
    instance = model.model_validate({EXECUTOR_HOSTS_INPUT_NAME: "host1", **values})
    return instance.to_args_string()


class TestMaskByParameterMetadata:
    """Cover the metadata-driven pass over the declared parameters."""

    def test_credential_name_masks_default_format_value(self):
        """Replace a name-matched credential's value under the default format."""
        model = _model(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "password", "type": "str"},
            ]
        )
        args = _args(model, dest="/tmp", password="s3cr3t")

        assert (
            mask_snippet_args(args, model)
            == f"--dest /tmp --password {SENSITIVE_ARG_MASK}"
        )

    def test_explicit_sensitive_flag_masks_non_credential_name(self):
        """Mask a parameter opted in via ``sensitive`` despite a neutral name."""
        model = _model([{"name": "auth-blob", "type": "str", "sensitive": True}])
        args = _args(model, **{"auth-blob": "opaque-value"})

        assert mask_snippet_args(args, model) == f"--auth-blob {SENSITIVE_ARG_MASK}"

    def test_equals_arg_format_masks_only_the_value(self):
        """Keep a ``--key=${value}`` prefix and mask only the value half."""
        model = _model(
            [
                {
                    "name": "password",
                    "type": "str",
                    "arg_format": "--password=${value}",
                }
            ]
        )
        args = _args(model, password="s3cr3t")

        assert mask_snippet_args(args, model) == f"--password={SENSITIVE_ARG_MASK}"

    def test_glued_short_arg_format_masks_only_the_value(self):
        """Keep a ``-p${value}`` prefix and mask only the value half."""
        model = _model(
            [{"name": "password", "type": "str", "arg_format": "-p${value}"}]
        )
        args = _args(model, password="s3cr3t")

        assert mask_snippet_args(args, model) == f"-p{SENSITIVE_ARG_MASK}"

    def test_credential_named_flag_leaves_neighbours_untouched(self):
        """Leave a boolean flag and its neighbours alone -- a flag carries no value."""
        model = _model(
            [
                {"name": "secret-mode", "type": "bool"},
                {"name": "dest", "type": "str", "required": True},
            ]
        )
        args = _args(model, **{"secret-mode": True, "dest": "/tmp"})

        assert mask_snippet_args(args, model) == "--secret-mode --dest /tmp"

    def test_repeated_value_placeholder_masks_every_occurrence(self):
        """Mask every token an ``arg_format`` renders from the same value."""
        model = _model(
            [
                {
                    "name": "password",
                    "type": "str",
                    "arg_format": "--password ${value} --confirm ${value}",
                }
            ]
        )
        args = _args(model, password="s3cr3t")

        assert mask_snippet_args(args, model) == (
            f"--password {SENSITIVE_ARG_MASK} --confirm {SENSITIVE_ARG_MASK}"
        )

    def test_value_equal_to_a_sibling_flag_literal_does_not_desync_the_walk(self):
        """Consume a value by rendered shape, not by scanning for the next ``--``."""
        model = _model(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "port", "type": "str", "required": True},
                {"name": "password", "type": "str", "required": True},
            ]
        )
        args = _args(model, dest="--port", port="3306", password="s3cr3t")

        assert mask_snippet_args(args, model) == (
            f"--dest --port --port 3306 --password {SENSITIVE_ARG_MASK}"
        )

    def test_omitted_sensitive_parameter_leaves_the_string_unchanged(self):
        """Leave the string alone when the sensitive parameter was never supplied."""
        model = _model(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "password", "type": "str"},
            ]
        )
        args = _args(model, dest="/tmp")

        assert mask_snippet_args(args, model) == "--dest /tmp"

    def test_value_with_spaces_survives_the_shlex_round_trip(self):
        """Preserve a whitespace-bearing non-sensitive value as one quoted token."""
        model = _model(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "password", "type": "str"},
            ]
        )
        args = _args(model, dest="/a b/c", password="pw with spaces")

        assert mask_snippet_args(args, model) == (
            f"--dest '/a b/c' --password {SENSITIVE_ARG_MASK}"
        )

    def test_empty_token_is_preserved(self):
        """Keep an empty argument token quoted in the masked output.

        A declared ``str`` parameter cannot render one -- ``min_length`` is a
        ``PositiveInt`` and an optional blank coerces to ``None`` -- so the only
        way an empty token reaches the recorded line is as an extra argument.
        """
        model = _model([{"name": "password", "type": "str", "required": True}])
        args = shlex.join(["--password", "s3cr3t", ""])

        assert mask_snippet_args(args, model) == (f"--password {SENSITIVE_ARG_MASK} ''")

    def test_mask_width_does_not_vary_with_secret_length(self):
        """Render the same fixed-width mask for a short and a long secret."""
        model = _model([{"name": "password", "type": "str"}])

        short = mask_snippet_args(_args(model, password="abcd"), model)
        long = mask_snippet_args(_args(model, password="z" * 64), model)

        assert short == long == f"--password {SENSITIVE_ARG_MASK}"

    def test_snippet_without_parameters_returns_the_string_unchanged(self):
        """Return the recorded string untouched when nothing is declared."""
        model = _model([])

        assert mask_snippet_args("--adhoc value", model) == "--adhoc value"

    def test_non_sensitive_parameters_only_returns_the_string_unchanged(self):
        """Return the recorded string untouched when no parameter is sensitive."""
        model = _model(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "port", "type": "int", "required": True},
            ]
        )
        args = _args(model, dest="/tmp", port=3306)

        assert mask_snippet_args(args, model) == args


class TestCredentialNameMatching:
    """Cover exact-segment matching of credential-bearing parameter names."""

    @pytest.mark.parametrize(
        "name",
        ["password", "api-key", "auth_token", "SECRET", "passphrase", "credentials"],
    )
    def test_credential_segment_is_recognised(self, name):
        """Recognise a name whose ``-``/``_`` segments include a credential word."""
        assert _is_credential_name(name) is True

    @pytest.mark.parametrize(
        "name", ["keyspace", "monkey", "passenger", "mongodb-uri", "dest", "compass"]
    )
    def test_credential_lookalike_is_not_recognised(self, name):
        """Reject a name that merely contains a credential word as a substring."""
        assert _is_credential_name(name) is False

    def test_lookalike_parameter_value_is_not_masked(self):
        """Leave a ``keyspace`` value intact -- substring matching would mask it."""
        model = _model([{"name": "keyspace", "type": "str", "required": True}])
        args = _args(model, keyspace="ks1")

        assert mask_snippet_args(args, model) == "--keyspace ks1"


class TestMaskPositionalParameters:
    """Cover the positional region, whose index must be provably stable."""

    def test_sensitive_positional_after_required_positionals_is_masked(self):
        """Mask a sensitive positional whose predecessors always render."""
        model = _model(
            [
                {"name": "dest", "type": "str", "required": True, "positional": True},
                {
                    "name": "password",
                    "type": "str",
                    "required": True,
                    "positional": True,
                },
            ]
        )
        args = _args(model, dest="/tmp", password="s3cr3t")

        assert mask_snippet_args(args, model) == f"/tmp {SENSITIVE_ARG_MASK}"

    def test_sensitive_positional_after_optional_positional_is_withheld(self):
        """Withhold the string when an optional positional makes the index unstable."""
        model = _model(
            [
                {"name": "dest", "type": "str", "positional": True},
                {
                    "name": "password",
                    "type": "str",
                    "required": True,
                    "positional": True,
                },
            ]
        )
        args = _args(model, dest="/tmp", password="s3cr3t")

        assert mask_snippet_args(args, model) is None

    def test_non_sensitive_positional_renders_after_the_flags(self):
        """Keep a non-sensitive positional in place while masking a flag value."""
        model = _model(
            [
                {"name": "password", "type": "str", "required": True},
                {"name": "target", "type": "str", "required": True, "positional": True},
            ]
        )
        args = _args(model, password="s3cr3t", target="node1")

        assert mask_snippet_args(args, model) == (
            f"--password {SENSITIVE_ARG_MASK} node1"
        )


class TestMaskExtraArgs:
    """Cover the name-pattern scan that covers free-form extra arguments."""

    def test_extra_arg_is_masked_even_when_the_walk_consumed_past_its_parameters(self):
        """Mask a pasted credential the declared walk swallowed the flag token of.

        An anchorless optional parameter that was not supplied still matches the
        first extra-argument token, so a scan starting where the walk stopped would
        begin *after* the credential flag and never see it. No declared parameter
        here is sensitive, so the ambiguity guard does not run either -- the scan
        has to cover the whole list on its own.
        """
        model = _model([{"name": "note", "type": "str", "arg_format": "${value}"}])
        args = "--password hunter2secret"

        masked = mask_snippet_args(args, model)

        assert masked == f"--password {SENSITIVE_ARG_MASK}"
        assert "hunter2secret" not in masked

    def test_extra_arg_credential_flag_and_value_is_masked(self):
        """Mask a pasted ``--token abc`` the snippet declares no parameter for."""
        model = _model([{"name": "dest", "type": "str", "required": True}])
        args = "--dest /tmp --token abc123"

        assert mask_snippet_args(args, model) == (
            f"--dest /tmp --token {SENSITIVE_ARG_MASK}"
        )

    def test_extra_arg_equals_form_is_masked(self):
        """Mask a pasted ``--password=abc`` while keeping its flag prefix."""
        model = _model([{"name": "dest", "type": "str", "required": True}])
        args = "--dest /tmp --password=abc123"

        assert mask_snippet_args(args, model) == (
            f"--dest /tmp --password={SENSITIVE_ARG_MASK}"
        )

    def test_extra_arg_glued_short_password_flag_is_masked(self):
        """Mask a pasted ``-pSECRET`` the way ``redact_secrets`` treats a glued ``-p``."""
        model = _model([{"name": "dest", "type": "str", "required": True}])
        args = "--dest /tmp -phunter2"

        assert mask_snippet_args(args, model) == f"--dest /tmp -p{SENSITIVE_ARG_MASK}"

    def test_bare_credential_flag_does_not_consume_a_following_credential_flag(self):
        """Mask the value owned by the flag after a valueless credential flag.

        A bare ``--password`` takes no value, so reading the next token as one both
        erases that token's own flag name and advances the scan past it -- leaving
        the value that flag really owns rendered in the clear.
        """
        model = _model([{"name": "dest", "type": "str", "required": True}])
        args = "--dest /tmp --password --token hunter2"

        masked = mask_snippet_args(args, model)

        assert masked == f"--dest /tmp --password --token {SENSITIVE_ARG_MASK}"
        assert "hunter2" not in masked

    def test_dash_leading_value_after_a_credential_flag_is_still_masked(self):
        """Keep masking a secret whose own first character is a single dash.

        Only the ``--`` spelling reads as a flag; a single-dash token stays
        eligible to be the credential flag's value.
        """
        model = _model([{"name": "dest", "type": "str", "required": True}])
        args = "--dest /tmp --password -abc123"

        masked = mask_snippet_args(args, model)

        assert masked == f"--dest /tmp --password {SENSITIVE_ARG_MASK}"
        assert "-abc123" not in masked

    def test_bare_short_password_flag_without_a_value_is_left_alone(self):
        """Leave a valueless ``-p`` token untouched -- there is nothing glued to it."""
        model = _model([{"name": "dest", "type": "str", "required": True}])
        args = "--dest /tmp -p"

        assert mask_snippet_args(args, model) == "--dest /tmp -p"

    def test_repeated_custom_sensitive_flag_in_extra_args_is_masked(self):
        """Mask a repeat of a sensitive parameter's *emitted* flag, not just its name.

        A custom ``arg_format`` can emit a flag unrelated to the frontmatter name,
        so a repeat among the extra arguments carries the same secret under a
        spelling the name-pattern scan would otherwise not recognise.
        """
        model = _model(
            [
                {
                    "name": "conn",
                    "type": "str",
                    "required": True,
                    "sensitive": True,
                    "arg_format": "--connection ${value}",
                }
            ]
        )
        args = "--connection secret1 --connection secret2"

        masked = mask_snippet_args(args, model)

        assert masked == (
            f"--connection {SENSITIVE_ARG_MASK} --connection {SENSITIVE_ARG_MASK}"
        )
        assert "secret2" not in masked

    def test_extra_arg_for_a_sensitive_declared_parameter_is_masked(self):
        """Mask a re-pasted sensitive parameter that the metadata walk skipped."""
        model = _model([{"name": "auth-blob", "type": "str", "sensitive": True}])
        args = "--auth-blob opaque"

        assert mask_snippet_args(args, model) == f"--auth-blob {SENSITIVE_ARG_MASK}"


class TestMaskCredentialUrls:
    """Cover the value-shaped pass a credential-bearing URI needs."""

    def test_two_token_uri_password_is_masked_and_identity_preserved(self):
        """Mask a URI password while keeping scheme, user, host, port, and query."""
        model = _model([{"name": "mongodb-uri", "type": "str", "required": True}])
        args = _args(
            model,
            **{
                "mongodb-uri": (
                    "mongodb://USER:PASSWORD@localhost:27017/?authSource=admin"
                )
            },
        )

        masked = mask_snippet_args(args, model)

        assert masked is not None
        assert "PASSWORD" not in masked
        assert SENSITIVE_ARG_MASK in masked
        assert "USER" in masked
        assert "localhost:27017" in masked
        assert "authSource=admin" in masked

    def test_equals_form_uri_password_is_masked(self):
        """Split a ``--key=`` prefix off before redacting, or urlparse finds nothing."""
        model = _model(
            [
                {
                    "name": "mongodb-uri",
                    "type": "str",
                    "required": True,
                    "arg_format": "--mongodb-uri=${value}",
                }
            ]
        )
        args = _args(model, **{"mongodb-uri": "mongodb://u:p3cret@host"})

        assert mask_snippet_args(args, model) == (
            f"--mongodb-uri=mongodb://u:{SENSITIVE_ARG_MASK}@host"
        )

    def test_uri_without_a_password_is_left_unchanged(self):
        """Leave a passwordless URI exactly as recorded."""
        model = _model([{"name": "mongodb-uri", "type": "str", "required": True}])
        args = _args(model, **{"mongodb-uri": "mongodb://host:27017"})

        assert mask_snippet_args(args, model) == "--mongodb-uri mongodb://host:27017"

    def test_unparseable_uri_is_masked_whole_rather_than_recorded_in_the_clear(self):
        """Replace a URI the parser rejects outright instead of passing it through.

        A malformed bracketed IPv6 literal makes ``urlparse`` raise, so no
        password segment can be located to mask. Recording the token as-is would
        put the real credential in the stored arguments, so the whole value is
        replaced instead — the one direction that is safe when the parse fails.
        """
        model = _model([{"name": "mongodb-uri", "type": "str", "required": True}])
        args = _args(model, **{"mongodb-uri": "mongodb://u:p3cret@[bad:ipv6/db"})

        masked = mask_snippet_args(args, model)

        assert masked is not None
        assert "p3cret" not in masked
        assert SENSITIVE_ARG_MASK in masked

    def test_token_with_an_at_sign_but_no_scheme_is_left_unchanged(self):
        """Skip an ordinary ``user@host`` token -- the pre-filter needs ``://`` too."""
        model = _model([{"name": "login", "type": "str", "required": True}])
        args = _args(model, login="user@host")

        assert mask_snippet_args(args, model) == "--login user@host"

    def test_unparseable_url_token_is_replaced_wholesale(self):
        """Replace a token that trips ``urlparse`` rather than risk leaking it."""
        model = _model([{"name": "mongodb-uri", "type": "str", "required": True}])
        args = _args(model, **{"mongodb-uri": "mongodb://u:p@[::1"})

        assert mask_snippet_args(args, model) == (f"--mongodb-uri {SENSITIVE_ARG_MASK}")


class TestNonArgumentFieldsAreSkipped:
    """Cover which of the model's fields the spec derives a parameter shape from."""

    @staticmethod
    def _model_with_base_fields() -> type[BaseModel]:
        """Return an execution model carrying every non-argument base field.

        ``extra_args`` and ``sudo`` only exist on the model when the frontmatter
        opts into them, so both are enabled here alongside the always-present
        ``executor_host``.

        :return: The snippet's dynamic execution model.
        """
        snippet = BaseSnippet(
            filename="base-fields.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "parameters": [
                    {"name": "dest", "type": "str", "required": True},
                    {"name": "password", "type": "str"},
                ],
                "allow_extra_args": True,
                "sudo": SnippetSudoOption.OPTIONAL.value,
            },
        )
        return snippet.get_execution_model()

    def test_spec_covers_only_the_declared_parameters(self):
        """Derive a spec for the frontmatter parameters and nothing else.

        ``to_args_string`` renders whatever survives its dump, so the spec has to
        skip exactly what that drops: the two fields excluded there by name, plus
        anything the model itself marks ``exclude``.
        """
        model = self._model_with_base_fields()

        spec_names = {spec.name for spec in _masking_spec(model)}

        assert spec_names == {"dest", "password"}

    def test_base_fields_are_present_to_be_skipped(self):
        """Assert the model really carries the fields the skip is meant to drop.

        Without this the sibling test would still pass if the base fields stopped
        being generated, leaving the skip logic uncovered.
        """
        model = self._model_with_base_fields()

        assert {model.extra_args_field, model.sudo_field} <= set(model.model_fields)
        assert [
            identifier
            for identifier, field in model.model_fields.items()
            if field.exclude
        ] == ["executor_host"]

    def test_an_excluded_field_is_skipped_by_its_marking_not_its_name(self):
        """Skip a field marked ``exclude`` even under an unrecognised name."""
        model = self._model_with_base_fields()
        renamed = create_model(
            "RenamedBaseField",
            __base__=model,
            target_host=(str, Field(default="host1", exclude=True)),
        )

        spec_names = {spec.name for spec in _masking_spec(renamed)}

        assert spec_names == {"dest", "password"}


class TestMaskingSpecCache:
    """Cover the memoized policy and the guarantee that it holds no secrets."""

    def test_spec_is_computed_once_per_model(self):
        """Reuse the derived spec across repeated masks of the same snippet."""
        model = _model([{"name": "password", "type": "str", "required": True}])
        _masking_spec.cache_clear()

        for _ in range(REPEATED_MASK_CALLS):
            mask_snippet_args(_args(model, password="s3cr3t"), model)

        assert _masking_spec.cache_info().misses == 1
        assert _masking_spec.cache_info().hits == REPEATED_MASK_CALLS - 1

    def test_distinct_models_get_distinct_specs(self):
        """Return a distinct spec per model class so two snippets share no entry."""
        first = _model([{"name": "password", "type": "str", "required": True}])
        second = _model([{"name": "dest", "type": "str", "required": True}])

        assert _masking_spec(first) != _masking_spec(second)
        assert mask_snippet_args(_args(first, password="s3cr3t"), first) == (
            f"--password {SENSITIVE_ARG_MASK}"
        )
        assert mask_snippet_args(_args(second, dest="/tmp"), second) == "--dest /tmp"

    def test_spec_never_holds_an_argument_value(self):
        """Assert no supplied secret is reachable from the cached policy."""
        model = _model([{"name": "password", "type": "str", "required": True}])
        secret = "uniqueSecret4Cache"

        mask_snippet_args(_args(model, password=secret), model)

        assert secret not in repr(_masking_spec(model))


class TestMaskingFailureIsWithheld:
    """Cover the fail-closed contract when the walk cannot be trusted."""

    def test_unparseable_argument_string_is_withheld(self):
        """Withhold a recorded string that is not a valid shell token list."""
        model = _model([{"name": "password", "type": "str", "required": True}])

        assert mask_snippet_args('--password "unbalanced', model) is None

    def test_untokenisable_arg_format_raises_for_the_caller_to_withhold(self):
        """Raise rather than mask when a stored ``arg_format`` will not tokenise.

        ``SnippetMetaParameter`` accepts an unbalanced quote, so a stale ``meta``
        column can reach the renderer and make ``shlex`` fail. Pinning the family
        keeps the caller's withhold-this-row arm honest about what it catches.
        """
        model = _model(
            [
                {
                    "name": "password",
                    "type": "str",
                    "required": True,
                    "arg_format": "--password '${value}",
                }
            ]
        )

        with pytest.raises(ValueError, match="No closing quotation"):
            mask_snippet_args("--password s3cr3t", model)

    def test_desynced_walk_over_a_sensitive_parameter_is_withheld(self):
        """Withhold when an anchorless optional parameter makes the walk ambiguous."""
        model = _model(
            [
                {"name": "first", "type": "str", "arg_format": "${value}"},
                {"name": "password", "type": "str", "required": True},
            ]
        )
        args = _args(model, password="s3cr3t")

        assert mask_snippet_args(args, model) is None

    def test_falsy_boolean_sharing_a_later_shape_is_withheld(self):
        """Withhold when a boolean that rendered nothing could fit a later token.

        A boolean renders no tokens at all whenever its value is falsy, whether or
        not its value could have been ``None``. So it vanishes from the recorded
        line exactly like an omitted optional, and the walk would consume the
        sensitive parameter's token under the boolean's identity, then read that
        parameter as never supplied and leave its value in the clear.
        """
        model = _model(
            [
                {
                    "name": "m",
                    "type": "bool",
                    "required": True,
                    "arg_format": "--m${value}",
                },
                {
                    "name": "muri",
                    "type": "str",
                    "required": True,
                    "sensitive": True,
                    "arg_format": "--muri:${value}",
                },
            ]
        )
        args = _args(model, m=False, muri="s3cr3t")

        assert args == "--muri:s3cr3t"
        assert mask_snippet_args(args, model) is None

    def test_optional_parameter_sharing_a_later_shape_is_withheld(self):
        """Withhold when an omitted parameter's shape also fits a later one's token.

        Two parameters may declare the same ``arg_format``. With the earlier one
        optional and absent, the walk would consume the later parameter's token
        under the earlier one's identity, then read the later -- sensitive --
        parameter as never supplied, leaving its value in the clear.
        """
        model = _model(
            [
                {"name": "port", "type": "str", "arg_format": "-p${value}"},
                {
                    "name": "password",
                    "type": "str",
                    "required": True,
                    "arg_format": "-p${value}",
                },
            ]
        )
        args = _args(model, password="hunter2secret")

        assert args == "-phunter2secret"
        assert mask_snippet_args(args, model) is None

    def test_anchorless_sensitive_parameter_shadowed_by_an_earlier_shape_is_withheld(
        self,
    ):
        """Withhold when a *value* could make an earlier shape fit a later token.

        The mirror image of the shape-collision case: here the shapes share no
        literal, but the sensitive parameter renders as a bare value, so a value
        that happens to start with the earlier parameter's prefix would be consumed
        under its identity. Whether that happens depends on the value, not the
        templates, so ambiguity has to be judged from what the templates leave
        open rather than by rendering one probe.
        """
        model = _model(
            [
                {"name": "mode", "type": "str", "arg_format": "-p${value}"},
                {
                    "name": "password",
                    "type": "str",
                    "required": True,
                    "arg_format": "${value}",
                },
            ]
        )
        args = _args(model, password="-pMySecretPassword")

        assert args == "-pMySecretPassword"
        assert mask_snippet_args(args, model) is None

    def test_distinct_shapes_are_not_treated_as_ambiguous(self):
        """Keep masking an ordinary snippet whose parameters render distinctly."""
        model = _model(
            [
                {"name": "port", "type": "str"},
                {"name": "password", "type": "str", "required": True},
            ]
        )
        args = _args(model, password="s3cr3t")

        assert mask_snippet_args(args, model) == f"--password {SENSITIVE_ARG_MASK}"

    def test_args_already_carrying_the_internal_placeholder_are_withheld(self):
        """Withhold a recorded value that collides with the internal placeholder.

        The mask replaces the placeholder over the joined string, so a value
        already containing it cannot be told apart from a token this module
        masked.
        """
        model = _model([{"name": "password", "type": "str", "required": True}])
        args = _args(model, password=_MASK_PLACEHOLDER)

        assert mask_snippet_args(args, model) is None


class TestIsCredentialParameter:
    """Cover the one classification the form and the masker both apply."""

    @pytest.mark.parametrize(
        ("name", "sensitive", "expected"),
        [
            ("auth-blob", True, True),
            ("password", False, True),
            ("api_key", False, True),
            ("dest", False, False),
            ("keyspace", False, False),
        ],
        ids=["flag-only", "name-only", "name-segment", "neither", "lookalike"],
    )
    def test_classifies_by_flag_or_by_name(self, name, sensitive, expected):
        """Assert either signal alone marks a parameter as a credential.

        :param name: The parameter's frontmatter name.
        :param sensitive: The parameter's frontmatter ``sensitive`` flag.
        :param expected: Whether the parameter carries a credential.
        """
        assert is_credential_parameter(name, sensitive=sensitive) is expected

    def test_masker_agrees_with_the_classifier(self):
        """Assert the masker treats as sensitive exactly what the classifier does."""
        parameters = [
            {"name": "auth-blob", "type": "str", "sensitive": True},
            {"name": "password", "type": "str"},
            {"name": "dest", "type": "str"},
        ]

        specs = _masking_spec(_model(parameters))

        assert {spec.name: spec.sensitive for spec in specs} == {
            parameter["name"]: is_credential_parameter(
                parameter["name"], sensitive=parameter.get("sensitive", False)
            )
            for parameter in parameters
        }


class TestMaskedArgsFor:
    """Cover the masked copy recorded beside a run's arguments at submit time."""

    @staticmethod
    def _execution_args(parameters: list[dict[str, Any]], **values: Any) -> Any:
        """Return validated execution arguments for a snippet with ``parameters``.

        :param parameters: The frontmatter parameter definitions.
        :param values: The frontmatter-named parameter values.
        :return: The validated arguments of one run.
        """
        return _model(parameters).model_validate(
            {EXECUTOR_HOSTS_INPUT_NAME: "host1", **values}
        )

    def test_masks_a_credential_and_keeps_its_neighbours(self):
        """Assert the copy hides the credential and nothing else."""
        execution_args = self._execution_args(
            [
                {"name": "dest", "type": "str", "required": True},
                {"name": "password", "type": "str"},
            ],
            dest="/tmp",
            password="s3cr3t",
        )

        assert (
            masked_args_for(execution_args)
            == f"--dest /tmp --password {SENSITIVE_ARG_MASK}"
        )

    def test_a_run_without_credentials_is_copied_unchanged(self):
        """Assert non-credential arguments stay readable in the copy."""
        execution_args = self._execution_args(
            [{"name": "dest", "type": "str", "required": True}], dest="/tmp"
        )

        assert masked_args_for(execution_args) == execution_args.to_args_string()

    def test_a_run_rendering_no_arguments_has_no_copy(self):
        """Assert there is no copy to record where there are no arguments."""
        assert masked_args_for(self._execution_args([])) is None

    def test_arguments_that_cannot_be_masked_safely_have_no_copy(self):
        """Assert a walk the masker cannot trust records nothing, so history withholds."""
        execution_args = self._execution_args(
            [{"name": "password", "type": "str", "required": True}],
            password=_MASK_PLACEHOLDER,
        )

        assert masked_args_for(execution_args) is None

    def test_a_masking_error_records_no_copy_and_does_not_fail_the_run(self, caplog):
        """Assert arguments no parameter metadata describes are logged without a value.

        :param caplog: The log-capture fixture.
        """

        class _UndescribedArgs:
            """Stand in for arguments whose type is not a snippet execution model."""

            def to_args_string(self) -> str:
                """Return a rendered line carrying a credential.

                :return: The argument string.
                """
                return "--password s3cr3t-value"

        with caplog.at_level(logging.WARNING):
            masked = masked_args_for(_UndescribedArgs())

        assert masked is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "s3cr3t-value" not in caplog.text
