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

"""Replace credential values in a recorded snippet argument string with a mask.

A snippet's arguments are recorded as the ``shlex.join``ed command line the
execution ran with, so the credentials an operator typed into the execution form
are present verbatim. Any surface that replays that line has to redact them
first.

Redaction works on the token list ``shlex.split`` recovers, never on a regex over
the joined string: each parameter carries its own ``arg_format`` template, so
there is no single argument shape to match. A parameter's emitted token shape is
derived by calling the production renderer
(:meth:`~app.extensions.snippets.models.snippet.BaseSnippetArgs.format_args`) with a
sentinel value, which keeps the positional / flag / default-format resolution
rules in one place.

Three independent signals fire over the same token list: the declared
parameter's metadata, an argument *name* that reads as a credential, and an
argument *value* shaped like a URL with embedded userinfo. The third is not
redundant -- a parameter named ``mongodb-uri`` carries a password while matching
no credential word.
"""

import logging
import re
import shlex
from dataclasses import dataclass

from app.core.utils.cache import ttl_cache
from app.core.utils.fields import redact_credential_url
from app.extensions.snippets.models.snippet import BaseSnippetArgs

logger = logging.getLogger(__name__)

#: Fixed-width replacement for a credential value, whatever its real length
SENSITIVE_ARG_MASK = "***"

ARG_MASKING_WARNING = (
    "Could not mask the arguments of a snippet run, so no masked copy is recorded "
    "and task history will withhold them."
)

#: Name segments that mark a parameter as credential-bearing on their own
CREDENTIAL_NAME_SEGMENTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "passphrase",
        "token",
        "secret",
        "key",
        "apikey",
        "credential",
        "credentials",
    }
)

_ONE_HOUR = 60 * 60

_SENTINEL = "extensionsArgMaskSentinel0"

#: Stand-in the mask is substituted for only after the tokens are re-joined.
#: Every character is one ``shlex.quote`` leaves bare, so a masked token is not
#: wrapped in quotes the way the ``*`` of the real mask would force. Because the
#: substitution runs over the joined string, a recorded value already containing
#: this literal would be indistinguishable from a token this module masked --
#: ``mask_snippet_args`` withholds such a string rather than rewrite it.
_MASK_PLACEHOLDER = "__extensionsMaskedArgPlaceholder0__"

_NAME_SEGMENT_RE = re.compile(r"[-_]")
_FLAG_RE = re.compile(r"^--(?P<name>[^\s=]+)$")
_FLAG_VALUE_RE = re.compile(r"^--(?P<name>[^\s=]+)=(?P<value>.+)$")
_GLUED_PASSWORD_RE = re.compile(r"^-p(?P<value>.+)$")
_PREFIXED_VALUE_RE = re.compile(r"^(?P<lead>-[^\s=]*=)(?P<value>.+)$")


@dataclass(frozen=True, slots=True)
class _ParamSpec:
    """Carry one declared parameter's rendered shape and masking policy.

    Every field is derived from the snippet's frontmatter, so a spec is safe to
    memoize -- no argument value ever reaches it.

    :param name: The parameter's frontmatter name.
    :param tokens: The tokens the parameter renders, with ``_SENTINEL`` standing
        in for its value.
    :param sensitive: Whether the parameter's value must be masked.
    :param positional: Whether the parameter renders as a bare positional token.
    :param omittable: Whether the parameter can be absent from the rendered line.
        Two things make it so: a value that can be ``None``, which
        ``to_args_string`` drops, and a non-positional boolean, which renders
        nothing at all whenever its value is falsy.
    """

    name: str
    tokens: tuple[str, ...]
    sensitive: bool
    positional: bool
    omittable: bool

    @property
    def carries_value(self) -> bool:
        """Return whether the parameter emits a value token that can be masked."""
        return any(_SENTINEL in token for token in self.tokens)


def _is_credential_name(name: str) -> bool:
    """Return whether a parameter name has a credential-bearing segment.

    Segments are compared exactly rather than as substrings, so ``api-key`` and
    ``auth_token`` match while ``keyspace`` and ``monkey`` do not.

    :param name: The parameter's frontmatter name.
    :return: ``True`` when a ``-``/``_``-delimited segment is a credential word.
    """
    return bool(CREDENTIAL_NAME_SEGMENTS & set(_NAME_SEGMENT_RE.split(name.lower())))


def is_credential_parameter(name: str, *, sensitive: bool) -> bool:
    """Return whether a snippet parameter carries a credential.

    The one classification of a declared parameter, shared by the run form and
    the masker so the two cannot disagree about a parameter. The masker goes
    further than this on a recorded line: it also masks credential-named extra
    arguments and passwords embedded in URLs.

    :param name: The parameter's frontmatter name.
    :param sensitive: The parameter's frontmatter ``sensitive`` flag.
    :return: ``True`` when the flag is set or the name reads as a credential.
    """
    return sensitive or _is_credential_name(name)


@ttl_cache(ttl=_ONE_HOUR, maxsize=32)
def _masking_spec(execution_model: type[BaseSnippetArgs]) -> tuple[_ParamSpec, ...]:
    """Return the per-parameter token shapes and sensitivity for a snippet.

    Only frontmatter-derived policy is cached; the substitution itself runs per
    request so no plaintext credential is held in a process-global cache. Keying
    on the model class is enough: the class is itself memoized upstream, and a
    frontmatter change rebuilds it and so misses here in lockstep.

    Which fields reach the rendered line is read off the model rather than named
    here, mirroring what ``to_args_string`` excludes: the two fields it drops at
    dump time, plus anything the model itself marks ``exclude``.

    :param execution_model: The snippet's dynamic execution model.
    :return: One spec per rendered parameter, in the order the model renders them.
    :raises ValueError: Propagated from ``format_args`` when a parameter's stored
        ``arg_format`` does not tokenise.
    """
    skipped = {
        execution_model.extra_args_field,
        execution_model.sudo_field,
    }
    specs: list[_ParamSpec] = []
    for identifier, field in execution_model.model_fields.items():
        if identifier in skipped or field.exclude:
            continue
        name, metadata = execution_model.get_field_metadata(identifier)
        tokens = tuple(execution_model.format_args(identifier, _SENTINEL))
        if not tokens:
            continue
        positional = bool(metadata.get("positional"))
        specs.append(
            _ParamSpec(
                name=name,
                tokens=tokens,
                sensitive=is_credential_parameter(
                    name, sensitive=bool(metadata.get("sensitive"))
                ),
                positional=positional,
                omittable=(not field.is_required() and field.default is None)
                or (bool(metadata.get("is_flag")) and not positional),
            )
        )
    return tuple(specs)


def _matches_at(tokens: list[str], start: int, spec: _ParamSpec) -> bool:
    """Return whether ``spec`` rendered the tokens beginning at ``start``.

    :param tokens: The recorded argument tokens.
    :param start: The index the parameter's first token would occupy.
    :param spec: The parameter whose rendered shape is being matched.
    :return: ``True`` when every one of the parameter's tokens lines up.
    """
    if start + len(spec.tokens) > len(tokens):
        return False
    for offset, shape in enumerate(spec.tokens):
        token = tokens[start + offset]
        if _SENTINEL not in shape:
            if token != shape:
                return False
            continue
        literals = shape.split(_SENTINEL)
        prefix, suffix = literals[0], literals[-1]
        if len(token) < len(prefix) + len(suffix):
            return False
        if not token.startswith(prefix) or not token.endswith(suffix):
            return False
    return True


def _shape_literals(shape: str) -> tuple[str, str] | None:
    """Return the literal text on each side of a shape's value.

    :param shape: One rendered token, possibly carrying ``_SENTINEL``.
    :return: The leading and trailing literals, or ``None`` when the token is a
        pure literal that carries no value at all.
    """
    if _SENTINEL not in shape:
        return None
    literals = shape.split(_SENTINEL)
    return literals[0], literals[-1]


def _shapes_can_coincide(consumer: str, candidate: str) -> bool:
    """Return whether *some* value renders ``candidate`` into a token ``consumer`` fits.

    Reasons about the templates rather than trying one probe value, because a
    candidate's rendered token is only fixed where its own literals are: the value
    between them is arbitrary, so whether it satisfies ``consumer`` is a property
    of what the two templates leave open, not of any single value. Two open sides
    coincide exactly when one side's literal extends the other's -- an empty
    literal extends everything.

    :param consumer: The token shape that might consume the other.
    :param candidate: The token shape whose rendered token might be consumed.
    :return: ``True`` when some value makes ``candidate``'s token satisfy
        ``consumer``.
    """
    consumer_literals = _shape_literals(consumer)
    candidate_literals = _shape_literals(candidate)
    if consumer_literals is None:
        if candidate_literals is None:
            return consumer == candidate
        prefix, suffix = candidate_literals
        return _literal_fits(consumer, prefix, suffix)
    prefix, suffix = consumer_literals
    if candidate_literals is None:
        return _literal_fits(candidate, prefix, suffix)
    other_prefix, other_suffix = candidate_literals
    return (prefix.startswith(other_prefix) or other_prefix.startswith(prefix)) and (
        suffix.endswith(other_suffix) or other_suffix.endswith(suffix)
    )


def _literal_fits(literal: str, prefix: str, suffix: str) -> bool:
    """Return whether a fixed token can be produced around ``prefix``/``suffix``.

    :param literal: The fixed token one shape demands.
    :param prefix: The literal text the other shape puts before its value.
    :param suffix: The literal text the other shape puts after its value.
    :return: ``True`` when some value renders the second shape into ``literal``.
    """
    return (
        len(literal) >= len(prefix) + len(suffix)
        and literal.startswith(prefix)
        and literal.endswith(suffix)
    )


def _walk_is_ambiguous(specs: tuple[_ParamSpec, ...]) -> bool:
    """Return whether an omittable parameter's shape can shadow a later parameter's.

    The walk consumes tokens in render order and never backtracks, so an omittable
    parameter that was *not* supplied is indistinguishable from one that was
    whenever some value would render a later parameter into tokens its own shape
    also fits. The walk would then consume that later parameter's token under the
    wrong identity, advance past it, and find nothing left to match for the
    parameter that really owns it -- reading a supplied parameter as absent and
    leaving its value in the clear. Shapes alone cannot tell the two apart, so an
    ambiguous set has to withhold instead.

    Only *later* parameters can be shadowed: the walk visits every spec in order,
    so an earlier one has already been matched against its own token before the
    walk reaches this parameter.

    :param specs: The snippet's parameter specs, in render order.
    :return: ``True`` when some omittable parameter's shape can fit a later
        parameter's rendered tokens.
    """
    for index, consumer in enumerate(specs):
        if consumer.positional or not consumer.omittable:
            continue
        for candidate in specs[index + 1 :]:
            overlap = min(len(consumer.tokens), len(candidate.tokens))
            if overlap and all(
                _shapes_can_coincide(consumer.tokens[offset], candidate.tokens[offset])
                for offset in range(overlap)
            ):
                return True
    return False


def _mask_declared_parameters(tokens: list[str], specs: tuple[_ParamSpec, ...]) -> bool:
    """Mask the sensitive values of declared parameters, in place.

    Walks the non-positional parameters in render order, consuming each one's
    tokens from the front, then treats what remains as the emitted positionals
    followed by any extra arguments -- an ordering the model's field construction
    and ``to_args_string``'s single ``shlex.join`` together guarantee.

    :param tokens: The recorded argument tokens, mutated in place.
    :param specs: The snippet's parameter specs, in render order.
    :return: ``False`` when a sensitive value's position cannot be established and
        the whole string must be withheld.
    """
    positionals = [spec for spec in specs if spec.positional]
    masks_a_value = any(spec.sensitive and spec.carries_value for spec in specs)
    if masks_a_value and _walk_is_ambiguous(specs):
        return False

    cursor = 0
    for spec in specs:
        if spec.positional or not _matches_at(tokens, cursor, spec):
            continue
        for offset, shape in enumerate(spec.tokens):
            if spec.sensitive and _SENTINEL in shape:
                literals = shape.split(_SENTINEL)
                tokens[cursor + offset] = literals[0] + _MASK_PLACEHOLDER + literals[-1]
        cursor += len(spec.tokens)

    for index, spec in enumerate(positionals):
        if not (spec.sensitive and spec.carries_value):
            continue
        if any(earlier.omittable for earlier in positionals[:index]):
            return False
        if cursor + index < len(tokens):
            tokens[cursor + index] = _MASK_PLACEHOLDER
    return True


def _flag_name(literal: str) -> str | None:
    """Return the long-flag name a literal introduces, if it introduces one.

    :param literal: The leading literal text of one rendered token.
    :return: The name after ``--``, with any ``=`` separator dropped, or ``None``
        when the literal is not a long flag.
    """
    match = _FLAG_RE.match(literal.removesuffix("="))
    return match["name"] if match else None


def _sensitive_arg_names(specs: tuple[_ParamSpec, ...]) -> frozenset[str]:
    """Return every argument name under which a sensitive value can travel.

    A parameter's ``arg_format`` may emit a flag unrelated to its frontmatter name
    (``conn`` rendering as ``--connection``), and a repeat of that flag among the
    extra arguments carries the same secret, so both spellings have to be
    recognised.

    :param specs: The snippet's parameter specs.
    :return: The frontmatter names and emitted flag names of sensitive parameters.
    """
    names: set[str] = set()
    for spec in specs:
        if not spec.sensitive:
            continue
        names.add(spec.name)
        for shape in spec.tokens:
            literals = _shape_literals(shape)
            leading = shape if literals is None else literals[0]
            if name := _flag_name(leading):
                names.add(name)
    return frozenset(names)


def _valueless_flag_literals(specs: tuple[_ParamSpec, ...]) -> frozenset[str]:
    """Return the literals of declared parameters that emit no value of their own.

    A boolean flag renders one literal token and nothing else, so the token after
    it belongs to a different parameter and must not be read as its value.

    :param specs: The snippet's parameter specs.
    :return: Every token a valueless parameter renders.
    """
    return frozenset(
        token for spec in specs if not spec.carries_value for token in spec.tokens
    )


def _is_long_flag(token: str) -> bool:
    """Return whether a token reads as a long flag rather than as a value.

    Only the ``--`` spelling counts. A single-dash token is a plausible secret
    value, so it stays eligible to be read as one.

    :param token: One recorded argument token.
    :return: ``True`` when the token is a long flag, with or without a value.
    """
    return bool(_FLAG_RE.match(token) or _FLAG_VALUE_RE.match(token))


def _mask_credential_named_tokens(
    tokens: list[str],
    sensitive_names: frozenset[str],
    flag_literals: frozenset[str],
) -> None:
    """Mask credential-named arguments anywhere in the token list, in place.

    Covers what the declared-parameter walk cannot reason about -- free-form extra
    arguments, where an operator may have pasted a credential flag the snippet
    declares no parameter for, or repeated one it does. Deliberately scans the
    whole list rather than only what the walk left: the walk can consume past its
    own parameters into the extra arguments, and a start index derived from it
    would then carry that error and skip exactly the tokens this pass exists to
    catch. Re-masking a value the walk already masked is a no-op.

    A credential flag is only read as taking a value when the token after it is not
    itself a long flag. Masking a successor that *is* one would erase its name and
    advance the scan past it, so the value that flag really owns would ship in the
    clear -- a bare ``--password`` before ``--token SECRET`` leaked ``SECRET``.

    :param tokens: The recorded argument tokens, mutated in place.
    :param sensitive_names: Argument names whose value must be masked, from
        :func:`_sensitive_arg_names`.
    :param flag_literals: Tokens known to carry no value, from
        :func:`_valueless_flag_literals`; the token after one of these belongs to
        another parameter, so it is left alone.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (match := _FLAG_VALUE_RE.match(token)) and (
            _is_credential_name(name := match["name"]) or name in sensitive_names
        ):
            tokens[index] = f"--{name}={_MASK_PLACEHOLDER}"
        elif (match := _GLUED_PASSWORD_RE.match(token)) and match[
            "value"
        ] != _MASK_PLACEHOLDER:
            tokens[index] = f"-p{_MASK_PLACEHOLDER}"
        elif (
            (match := _FLAG_RE.match(token))
            and (_is_credential_name(name := match["name"]) or name in sensitive_names)
            and token not in flag_literals
            and index + 1 < len(tokens)
            and not _is_long_flag(tokens[index + 1])
        ):
            tokens[index + 1] = _MASK_PLACEHOLDER
            index += 1
        index += 1


def _redact_credential_url_token(token: str) -> str:
    """Return ``token`` with any URL password it embeds replaced by the mask.

    Splitting an optional ``--key=`` prefix off first is load-bearing:
    ``urlparse`` reads ``--key=scheme://user:pass@host`` as a bare path, finds no
    password, and hands the string back untouched.

    :param token: One recorded argument token.
    :return: The token with its URL password masked, or unchanged when it carries
        no credential URL.
    """
    lead, candidate = "", token
    if match := _PREFIXED_VALUE_RE.match(token):
        lead, candidate = match["lead"], match["value"]
    if "://" not in candidate or "@" not in candidate:
        return token
    try:
        return lead + redact_credential_url(candidate, mask=_MASK_PLACEHOLDER)
    except ValueError:
        return _MASK_PLACEHOLDER


def mask_snippet_args(args: str, execution_model: type[BaseSnippetArgs]) -> str | None:
    """Return ``args`` with credential values replaced by a fixed-width mask.

    :param args: The ``shlex.join``ed argument string recorded on the execution.
    :param execution_model: The snippet's dynamic execution model, whose field
        metadata supplies each parameter's emitted token shape and sensitivity.
    :return: The masked argument string, or ``None`` when it cannot be masked
        safely and must be withheld entirely.
    :raises ValueError: Propagated from rendering a parameter's ``arg_format``
        when the stored template does not tokenise (an unbalanced quote is
        accepted at parse time and only fails here). A malformed ``args`` is
        *not* in this set -- it withholds instead.
    """
    if _MASK_PLACEHOLDER in args:
        return None
    try:
        tokens = shlex.split(args)
    except ValueError:
        return None
    specs = _masking_spec(execution_model)
    if not _mask_declared_parameters(tokens, specs):
        return None
    _mask_credential_named_tokens(
        tokens, _sensitive_arg_names(specs), _valueless_flag_literals(specs)
    )
    masked = [_redact_credential_url_token(token) for token in tokens]
    return shlex.join(masked).replace(_MASK_PLACEHOLDER, SENSITIVE_ARG_MASK)


def masked_args_for(execution_args: BaseSnippetArgs) -> str | None:
    """Return the masked rendering of a run's arguments, for recording beside them.

    Computed at submit time against the model the arguments were rendered from,
    so the copy stays correct after the snippet is edited or deleted. A
    ``TypeError``, ``ValueError``, ``KeyError`` or ``AttributeError`` raised
    while masking is logged and records no copy, and history then withholds the
    arguments rather than showing them.

    :param execution_args: The validated arguments of the run being dispatched.
    :return: The masked argument string, or ``None`` when the run renders no
        arguments or they cannot be masked safely.
    """
    args = execution_args.to_args_string()
    if not args:
        return None
    try:
        return mask_snippet_args(args, type(execution_args))
    except (TypeError, ValueError, KeyError, AttributeError):
        logger.warning(ARG_MASKING_WARNING, exc_info=True)
        return None
