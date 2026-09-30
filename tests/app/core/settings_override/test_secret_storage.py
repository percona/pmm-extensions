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

"""Define tests for the schema-driven secret-leaf walker.

Which leaves are secret is derived from the settings annotation, never from the
stored JSON, so every case here pairs a settings class and an override key with
the JSON shape that key actually persists.
"""

import logging
from collections import defaultdict, OrderedDict
from collections.abc import MutableMapping
from typing import Any
from urllib.parse import urlparse

import pytest
from pydantic import BaseModel, SecretStr

from app.core.alerts.config import AlertSettings
from app.core.config import BaseYamlSettings, PMMSettings, Settings
from app.core.encryption import (
    decrypt,
    DecryptionError,
    encrypt,
    is_encrypted,
    mark_ciphertext,
    marked_ciphertext,
)
from app.core.settings_override.registry import (
    annotated_type,
    annotation_is_credential_url,
    coerce_field_value,
)
from app.core.settings_override.resolution import resolve_nested_field
from app.core.settings_override.secret_storage import (
    _ALL_LEAF_KINDS,
    _positional_args,
    _transform_leaves,
    decrypt_credential_url_leaves,
    decrypt_secret_leaves,
    encrypt_secret_leaves,
    reencrypt_credential_url_leaves,
    reencrypt_secret_leaves,
    unmark_secret_leaves,
)
from app.core.utils.fields import CredentialHttpUrl
from app.extensions.config import ExtensionsSettings
from app.tasks.config import TasksSettings
from tests.app.encryption_fixtures import (
    FERNET_SHAPED_PLAINTEXT,
    foreign_token,
    is_stored_ciphertext,
    stored_plaintext,
)

PMM_KEY = "PMM"
PMM_NESTED_KEY = "PMM__API_KEY"
PMM_ENDPOINT_KEY = "PMM__ENDPOINT"
PROVIDERS_KEY = "PROVIDERS"
DELIVERY_INPUTS_KEY = "DIAGNOSTICS_DELIVERY_INPUTS"
INVENTORY_ENDPOINT_KEY = "INVENTORY_ENDPOINT"
TASKS_ENDPOINT_KEY = "TASKS_ENDPOINT"
NOMAD_ENDPOINT_KEY = "NOMAD__ENDPOINT"

CREDENTIAL_URL = "https://inv-user:hunter2@inv.example.com:8443/api"
CREDENTIAL_PASSWORD = "hunter2"

#: The walker's own logger name, so a caplog assertion cannot silently widen to
#: a line some other module emitted.
SECRET_STORAGE_LOGGER = "app.core.settings_override.secret_storage"


def url_password(url: object) -> str:
    """Return the userinfo password of a stored URL leaf.

    Fails the calling test when the leaf carries none: every caller asserts on a
    password the walker was supposed to transform, so an absent one is a failure
    to surface rather than a value to hand on.

    :param url: The stored leaf, which the walker normalizes to text.
    :return: The raw password segment.
    """
    password = urlparse(str(url)).password
    assert password is not None, f"no password segment in {url!r}"
    return password


def url_without_password(url: object) -> str:
    """Return ``url`` with its password blanked, for comparing the surrounding parts.

    :param url: The stored leaf to strip.
    :return: The URL with an empty password segment.
    """
    parsed = urlparse(str(url))
    host = parsed.netloc.rsplit("@", 1)[-1]
    return str(url).replace(f":{parsed.password}@{host}", f":@{host}")


def pmm_payload(api_key: object = "pmm-secret") -> dict[str, object]:
    """Return the stored JSON shape of a whole-object ``PMM`` override.

    :param api_key: The value to place at ``$.api_key``.
    :return: The override row's stored value.
    """
    return {
        "endpoint": "https://pmm.example.com",
        "api_key": api_key,
        "verify_ssl": True,
    }


def pmm_payload_with_credential_endpoint(
    api_key: object = "pmm-secret",
    endpoint: str = CREDENTIAL_URL,
) -> dict[str, object]:
    """Return a whole-object ``PMM`` override carrying both leaf kinds at once.

    The mixed row is what separates the broad entry points from the
    credential-URL-scoped pair the data migrations call.

    :param api_key: The value to place at the ``SecretStr`` leaf.
    :param endpoint: The value to place at the credential-URL leaf.
    :return: The override row's stored value.
    """
    return {"endpoint": endpoint, "api_key": api_key, "verify_ssl": True}


def delivery_inputs_payload_with_credential_endpoint() -> dict[str, object]:
    """Return a materializer-backed override carrying both leaf kinds.

    :return: The override row's stored value.
    """
    return {
        "endpoint": CREDENTIAL_URL,
        "secrets": {"token": "intake-token", "api_key": "intake-api-key"},
    }


def pagerduty_payload(
    routing_key_field: str = "routing_key",
) -> list[dict[str, object]]:
    """Return the stored JSON shape of a ``PROVIDERS`` override with one PagerDuty entry.

    :param routing_key_field: The spelling the client used for the secret field.
    :return: The override row's stored value.
    """
    return [
        {
            "PROVIDER": "PAGERDUTY",
            "api_endpoint": "https://events.pagerduty.com/v2/",
            routing_key_field: "pd-routing-secret",
        }
    ]


def delivery_inputs_payload() -> dict[str, object]:
    """Return the stored JSON shape of a ``DIAGNOSTICS_DELIVERY_INPUTS`` override.

    :return: The override row's stored value.
    """
    return {
        "endpoint": "https://intake.example.com",
        "secrets": {"token": "intake-token", "api_key": "intake-api-key"},
    }


class TestEncryptSecretLeaves:
    """Cover :func:`encrypt_secret_leaves` over every secret-bearing override shape."""

    def test_encrypts_top_level_model_secret_leaf(self) -> None:
        """Encrypt ``$.api_key`` of a whole-object ``PMM`` override, leaving siblings alone."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())

        assert is_stored_ciphertext(stored["api_key"])
        assert stored_plaintext(stored["api_key"]) == "pmm-secret"
        assert stored["endpoint"] == "https://pmm.example.com"
        assert stored["verify_ssl"] is True

    def test_encrypts_bare_nested_leaf(self) -> None:
        """Encrypt the whole value of a nested ``PMM__API_KEY`` row."""
        stored = encrypt_secret_leaves(Settings, PMM_NESTED_KEY, "pmm-secret")

        assert is_stored_ciphertext(stored)
        assert stored_plaintext(stored) == "pmm-secret"

    def test_encrypts_polymorphic_provider_secret(self) -> None:
        """Encrypt ``$[i].routing_key`` on a concrete provider subclass, keeping siblings."""
        stored = encrypt_secret_leaves(
            AlertSettings, PROVIDERS_KEY, pagerduty_payload()
        )

        assert is_stored_ciphertext(stored[0]["routing_key"])
        assert stored_plaintext(stored[0]["routing_key"]) == "pd-routing-secret"
        assert stored[0]["PROVIDER"] == "PAGERDUTY"
        assert stored[0]["api_endpoint"] == "https://events.pagerduty.com/v2/"

    def test_encrypts_polymorphic_provider_secret_uppercase_key(self) -> None:
        """Match the provider's field name case-folded, as the client may store it uppercase."""
        stored = encrypt_secret_leaves(
            AlertSettings, PROVIDERS_KEY, pagerduty_payload("ROUTING_KEY")
        )

        assert is_stored_ciphertext(stored[0]["ROUTING_KEY"])
        assert stored_plaintext(stored[0]["ROUTING_KEY"]) == "pd-routing-secret"

    def test_encrypts_secret_valued_dict(self) -> None:
        """Encrypt every value of a ``dict[str, SecretStr]`` leaf, never its keys."""
        stored = encrypt_secret_leaves(
            ExtensionsSettings, DELIVERY_INPUTS_KEY, delivery_inputs_payload()
        )

        assert sorted(stored["secrets"]) == ["api_key", "token"]
        assert is_stored_ciphertext(stored["secrets"]["token"])
        assert stored_plaintext(stored["secrets"]["token"]) == "intake-token"
        assert stored_plaintext(stored["secrets"]["api_key"]) == "intake-api-key"
        assert stored["endpoint"] == "https://intake.example.com"

    def test_non_secret_field_returned_by_identity(self) -> None:
        """Return a value whose annotation reaches no secret without rebuilding it.

        Asserted by identity rather than equality: a container the walker
        descended into would compare equal while being a different object, so
        only ``is`` proves the annotation was pruned before any descent.
        """
        payload = {"version": 1, "handlers": {"console": {"level": "DEBUG"}}}

        assert encrypt_secret_leaves(Settings, "LOGGING_CONFIG", payload) is payload

    @pytest.mark.parametrize("api_key", [None, 7, ["not-a-string"]])
    def test_non_str_leaves_pass_through(self, api_key: object) -> None:
        """Leave a non-``str`` value at a secret position untouched rather than encrypting it."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload(api_key))

        assert stored["api_key"] == api_key

    def test_empty_string_secret_is_encrypted(self) -> None:
        """Encrypt an empty secret, which Fernet pads to a full block and round-trips."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload(""))

        assert is_stored_ciphertext(stored["api_key"])
        assert stored_plaintext(stored["api_key"]) == ""

    def test_encrypts_a_secret_that_is_itself_shaped_like_ciphertext(self) -> None:
        """Encrypt a credential ``is_encrypted`` would misread as already-encrypted.

        ``is_encrypted`` is purely structural, so roughly one in 256 base64url
        secrets of 100 characters or more decodes to a leading ``0x80`` and
        passes it. On the write path the value is always plaintext, so consulting
        that predicate there would store such a credential in the clear, which is
        the exact exposure this module exists to remove, and the subsequent
        read would fail to decrypt it and drop the override silently.
        """
        secret = FERNET_SHAPED_PLAINTEXT
        assert is_encrypted(secret), "the fixture must exercise the misreading"

        stored = encrypt_secret_leaves(Settings, PMM_NESTED_KEY, secret)

        assert stored != secret
        assert stored_plaintext(stored) == secret

    def test_input_is_not_mutated(self) -> None:
        """Return new containers so the caller's payload keeps its plaintext."""
        payload = pmm_payload()

        encrypt_secret_leaves(Settings, PMM_KEY, payload)

        assert payload["api_key"] == "pmm-secret"

    def test_unresolvable_key_returns_value_unchanged(self) -> None:
        """Return the value untouched for a key that resolves to no field."""
        payload = pmm_payload()

        assert encrypt_secret_leaves(Settings, "NO_SUCH_FIELD", payload) == payload


class TestReencryptSecretLeaves:
    """Cover the idempotent variant the re-encryption migrations use."""

    def test_encrypts_a_plaintext_leaf(self) -> None:
        """Encrypt a leaf that is not yet ciphertext, like the write path does."""
        stored = reencrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())

        assert is_stored_ciphertext(stored["api_key"])
        assert stored_plaintext(stored["api_key"]) == "pmm-secret"

    def test_already_encrypted_leaf_not_re_encrypted(self) -> None:
        """Leave an already-encrypted leaf byte-identical instead of double-encrypting it."""
        once = reencrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())

        assert reencrypt_secret_leaves(Settings, PMM_KEY, once) == once

    def test_foreign_key_ciphertext_not_re_encrypted(self) -> None:
        """Leave ciphertext this key cannot decrypt alone, since re-encrypting destroys it."""
        token = foreign_token()

        stored = reencrypt_secret_leaves(Settings, PMM_KEY, pmm_payload(token))

        assert stored["api_key"] == token

    def test_a_plaintext_secret_shaped_like_ciphertext_is_skipped(self) -> None:
        """Pin the accepted limitation: a Fernet-shaped plaintext row stays in the clear.

        Telling this apart from ciphertext written under another key would take a
        decrypt attempt, and guessing wrong on the second destroys the only copy
        of its plaintext. The write path has no such ambiguity, so only rows
        predating the migration can be affected.
        """
        stored = reencrypt_secret_leaves(
            Settings, PMM_NESTED_KEY, FERNET_SHAPED_PLAINTEXT
        )

        assert stored == FERNET_SHAPED_PLAINTEXT


class TestDecryptSecretLeaves:
    """Cover :func:`decrypt_secret_leaves` over the read path's inputs."""

    def test_decrypts_top_level_model_secret_leaf(self) -> None:
        """Restore the plaintext of an encrypted ``$.api_key``."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())

        assert decrypt_secret_leaves(Settings, PMM_KEY, stored) == pmm_payload()

    def test_decrypt_passes_legacy_plaintext_through(self) -> None:
        """Return a not-yet-encrypted leaf unchanged so pre-migration rows keep working."""
        payload = pmm_payload()

        assert decrypt_secret_leaves(Settings, PMM_KEY, payload) == payload

    def test_decrypt_raises_on_foreign_key_ciphertext(self) -> None:
        """Raise :class:`DecryptionError` for ciphertext minted under another key."""
        payload = pmm_payload(foreign_token())

        with pytest.raises(DecryptionError):
            decrypt_secret_leaves(Settings, PMM_KEY, payload)

    @pytest.mark.parametrize(
        ("settings_cls", "key", "payload"),
        [
            (Settings, PMM_KEY, pmm_payload()),
            (Settings, PMM_NESTED_KEY, "pmm-secret"),
            (AlertSettings, PROVIDERS_KEY, pagerduty_payload()),
            (ExtensionsSettings, DELIVERY_INPUTS_KEY, delivery_inputs_payload()),
        ],
        ids=["pmm-object", "pmm-nested-leaf", "providers-array", "delivery-inputs"],
    )
    def test_round_trip(
        self, settings_cls: type[BaseYamlSettings], key: str, payload: object
    ) -> None:
        """Restore every secret-bearing override shape to its original plaintext."""
        stored = encrypt_secret_leaves(settings_cls, key, payload)

        assert decrypt_secret_leaves(settings_cls, key, stored) == payload

    def test_ciphertext_differs_from_plaintext_for_every_shape(self) -> None:
        """Confirm the round trip above is not vacuous: the stored value really changed."""
        stored = encrypt_secret_leaves(
            AlertSettings, PROVIDERS_KEY, pagerduty_payload()
        )

        assert stored[0]["routing_key"] != "pd-routing-secret"
        assert stored != pagerduty_payload()

    def test_mixed_provider_array_leaves_non_secret_entries_alone(self) -> None:
        """Transform only the entries whose annotation carries a secret field."""
        payload = [*pagerduty_payload(), {"PROVIDER": "PAGERDUTY", "api_endpoint": "x"}]

        stored = encrypt_secret_leaves(AlertSettings, PROVIDERS_KEY, payload)

        assert is_stored_ciphertext(stored[0]["routing_key"])
        assert stored[1] == {"PROVIDER": "PAGERDUTY", "api_endpoint": "x"}

    def test_unmatched_json_key_is_left_alone(self) -> None:
        """Leave a stored key that matches no model field untouched."""
        payload = {**pmm_payload(), "retired_field": "plain"}

        stored = encrypt_secret_leaves(Settings, PMM_KEY, payload)

        assert stored["retired_field"] == "plain"
        assert is_stored_ciphertext(stored["api_key"])


class _SecretHolder(BaseModel):
    """Carry one secret-typed field for the annotation-shape cases below.

    :param token: The secret leaf a walker must reach.
    """

    token: SecretStr


class TestAnnotationShapes:
    """Cover container annotations no settings field declares today.

    Each shape is one the walker's own type-argument handling admits, so a gap
    here is a silent plaintext store rather than an error. Driven through
    ``_transform_leaves`` directly because no settings class declares
    them, which is exactly why the codebase cannot exercise them for us.
    """

    @staticmethod
    def _encrypt(annotation: Any, value: Any) -> Any:
        """Apply the write path's transform against ``annotation``.

        :param annotation: The annotation of the JSON position ``value`` occupies.
        :param value: The stored value at that position.
        :return: The transformed value.
        """
        return _transform_leaves(
            annotation,
            value,
            encrypt,
            kinds=_ALL_LEAF_KINDS,
            context="TestAnnotationShapes",
        )

    @pytest.mark.parametrize(
        "annotation",
        [
            dict[str, SecretStr],
            defaultdict[str, SecretStr],
            OrderedDict[str, SecretStr],
            MutableMapping[str, SecretStr],
        ],
        ids=["dict", "defaultdict", "OrderedDict", "MutableMapping"],
    )
    def test_every_mapping_flavour_reaches_its_secret_values(
        self, annotation: Any
    ) -> None:
        """Descend into any mapping type, not just the ``dict`` spelling.

        ``defaultdict`` and ``OrderedDict`` are both live override-field
        annotations, so a membership test against a fixed set of origins would
        leave a future secret-valued one in the clear.
        """
        stored = self._encrypt(annotation, {"a": "s1", "b": "s2"})

        assert stored_plaintext(stored["a"]) == "s1"
        assert stored_plaintext(stored["b"]) == "s2"

    @pytest.mark.parametrize(
        "annotation",
        [dict[str, str] | _SecretHolder, _SecretHolder | dict[str, str]],
        ids=["mapping-first", "model-first"],
    )
    def test_a_union_of_mapping_and_model_still_reaches_the_model_secret(
        self, annotation: Any
    ) -> None:
        """Fall through to the model branch when the mapping candidate holds no secret.

        Resolving the union to the ``dict``'s plain ``str`` values would take the
        mapping branch and never look inside the model, storing its credential in
        the clear whichever way the union is spelled.
        """
        stored = self._encrypt(annotation, {"token": "s1"})

        assert stored_plaintext(stored["token"]) == "s1"

    def test_a_heterogeneous_tuple_reaches_the_secret_at_a_later_index(self) -> None:
        """Resolve a fixed-length tuple per index, not from its first argument alone."""
        stored = self._encrypt(tuple[str, SecretStr], ["plain", "s1"])

        assert stored[0] == "plain"
        assert stored_plaintext(stored[1]) == "s1"

    def test_a_variadic_tuple_annotates_every_item_alike(self) -> None:
        """Apply the single element annotation to every item of a ``tuple[X, ...]``."""
        stored = self._encrypt(tuple[SecretStr, ...], ["s1", "s2"])

        assert stored_plaintext(stored[0]) == "s1"
        assert stored_plaintext(stored[1]) == "s2"

    def test_a_set_annotation_over_a_json_array_reaches_its_items(self) -> None:
        """Descend a ``set``-annotated field, which JSON stores as an array.

        This is the shape ``AlertSettings.PROVIDERS`` declares, so the collection
        predicate has to admit origins whose values never arrive as that type.
        """
        stored = self._encrypt(set[SecretStr], ["s1", "s2"])

        assert [stored_plaintext(item) for item in stored] == ["s1", "s2"]

    @pytest.mark.parametrize(
        "annotation",
        [
            list[CredentialHttpUrl],
            set[CredentialHttpUrl],
            tuple[CredentialHttpUrl, ...],
        ],
        ids=["list", "set", "variadic-tuple"],
    )
    def test_a_collection_of_credential_urls_reaches_every_element(
        self, annotation: Any
    ) -> None:
        """Resolve a collection's element annotation to its credential-URL member.

        No settings field declares this shape today, which is precisely why the
        codebase cannot exercise it: a gap here is a silent plaintext store
        rather than an error the day one is added.
        """
        stored = self._encrypt(annotation, [CREDENTIAL_URL, CREDENTIAL_URL])

        assert [stored_plaintext(url_password(item)) for item in stored] == [
            CREDENTIAL_PASSWORD,
            CREDENTIAL_PASSWORD,
        ]

    def test_a_mapping_of_credential_urls_reaches_every_value(self) -> None:
        """Resolve a mapping's value annotation to its credential-URL member."""
        stored = self._encrypt(
            dict[str, CredentialHttpUrl], {"a": CREDENTIAL_URL, "b": CREDENTIAL_URL}
        )

        assert stored_plaintext(url_password(stored["a"])) == CREDENTIAL_PASSWORD
        assert stored_plaintext(url_password(stored["b"])) == CREDENTIAL_PASSWORD

    def test_positional_flattening_discards_the_credential_url_marker(self) -> None:
        """Pin why the position predicate runs before ``_positional_args``.

        The credential-URL marker lives in ``__metadata__``, and this flattener
        strips ``Annotated`` — so a predicate written over its output answers
        ``False`` for the one live leaf whose marker is annotation-visible, and
        every credential URL would be stored in the clear. Ordering the branch
        ahead of the flattening is what the walker relies on, and nothing else
        would fail if that ordering were swapped.
        """
        annotation = annotated_type(PMMSettings.model_fields["endpoint"])

        assert annotation_is_credential_url(annotation)
        assert not any(
            annotation_is_credential_url(arg) for arg in _positional_args(annotation)
        )

    def test_union_members_reach_the_tie_break_in_declaration_order(self) -> None:
        """Preserve a union's declared order through the positional flattening.

        ``_first_secret_bearing`` resolves a contested position by taking the
        first candidate that reaches a secret, so the order this returns *is*
        the tie-break rule. A LIFO traversal that forgets to reverse silently
        inverts it, and no live annotation has two secret-bearing candidates
        today to fail on it.
        """
        assert _positional_args(dict[str, str] | list[int] | SecretStr) == [
            dict[str, str],
            list[int],
            SecretStr,
        ]


class TestEncryptCredentialUrlLeaves:
    """Cover the write path over every override-reachable credential-URL leaf.

    Only the userinfo password is rewritten: the endpoint an operator reads out
    of a raw database dump stays legible, which is the whole point of
    transforming the segment rather than the leaf.
    """

    @pytest.mark.parametrize(
        ("settings_cls", "key"),
        [
            (ExtensionsSettings, INVENTORY_ENDPOINT_KEY),
            (ExtensionsSettings, TASKS_ENDPOINT_KEY),
            (Settings, PMM_ENDPOINT_KEY),
            (TasksSettings, NOMAD_ENDPOINT_KEY),
        ],
    )
    def test_encrypts_the_password_of_a_scalar_leaf(
        self, settings_cls: type[BaseYamlSettings], key: str
    ) -> None:
        """Encrypt the embedded password and leave the endpoint readable."""
        stored = encrypt_secret_leaves(settings_cls, key, CREDENTIAL_URL)

        assert is_stored_ciphertext(url_password(stored))
        assert stored_plaintext(url_password(stored)) == CREDENTIAL_PASSWORD
        assert url_without_password(stored) == url_without_password(CREDENTIAL_URL)

    def test_encrypts_a_password_that_is_itself_shaped_like_ciphertext(self) -> None:
        """Encrypt a URL password ``is_encrypted`` would misread as already-encrypted.

        The credential-URL mirror of
        ``test_encrypts_a_secret_that_is_itself_shaped_like_ciphertext``. The
        write path always receives plaintext, so short-circuiting on the
        structural predicate here would store an operator's password in the
        clear, and the read path would then fail to decrypt it and drop the
        override silently.
        """
        assert is_encrypted(FERNET_SHAPED_PLAINTEXT), (
            "the fixture must exercise the misreading"
        )
        url = f"https://u:{FERNET_SHAPED_PLAINTEXT}@host:8443/api"

        stored = encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url)

        assert url_password(stored) != FERNET_SHAPED_PLAINTEXT
        assert stored_plaintext(url_password(stored)) == FERNET_SHAPED_PLAINTEXT

    def test_encrypts_the_endpoint_inside_a_whole_object_override(self) -> None:
        """Rewrite the URL password and the ``SecretStr`` sibling in one pass."""
        stored = encrypt_secret_leaves(
            Settings, PMM_KEY, pmm_payload_with_credential_endpoint()
        )

        assert stored_plaintext(url_password(stored["endpoint"])) == CREDENTIAL_PASSWORD
        assert stored_plaintext(stored["api_key"]) == "pmm-secret"

    def test_encrypts_a_non_optional_child_inside_a_whole_object_override(self) -> None:
        """Reach a model child whose ``Annotated`` Pydantic hoisted onto its ``FieldInfo``.

        ``NomadExecutor.endpoint`` is non-``Optional``, so its marker is absent
        from ``.annotation`` and present only via ``annotated_type``. A
        whole-object ``NOMAD`` override descends through ``_field_annotation``
        to reach it, which is the one path a nested-leaf test does not exercise.
        """
        stored = encrypt_secret_leaves(
            TasksSettings, "NOMAD", {"endpoint": CREDENTIAL_URL, "verify_ssl": True}
        )

        assert stored_plaintext(url_password(stored["endpoint"])) == CREDENTIAL_PASSWORD
        assert stored["verify_ssl"] is True

    def test_encrypts_the_endpoint_inside_a_materializer_payload(self) -> None:
        """Reach the credential URL stored beside a secret-valued mapping."""
        stored = encrypt_secret_leaves(
            ExtensionsSettings,
            DELIVERY_INPUTS_KEY,
            delivery_inputs_payload_with_credential_endpoint(),
        )

        assert stored_plaintext(url_password(stored["endpoint"])) == CREDENTIAL_PASSWORD
        assert stored_plaintext(stored["secrets"]["token"]) == "intake-token"
        assert stored_plaintext(stored["secrets"]["api_key"]) == "intake-api-key"

    @pytest.mark.parametrize(
        "url",
        [
            "https://inv.example.com:8443/api",
            "https://inv-user@inv.example.com:8443/api",
            "https://inv-user:@inv.example.com:8443/api",
            "https://user:pw@[bad:ipv6/",
        ],
        ids=["no-userinfo", "username-only", "empty-password", "unparseable"],
    )
    def test_leaves_a_url_with_no_transformable_password_alone(self, url: str) -> None:
        """Return a URL byte-identical rather than inventing or corrupting a credential.

        An empty password is absence, not a credential, and the unparseable
        shape is the one ``urlparse`` raises on — neither may reach ``encrypt``.
        """
        assert (
            encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url)
            == url
        )

    @pytest.mark.parametrize("leaf", [None, 42, {"not": "a url"}, ["x"]])
    def test_leaves_a_non_url_leaf_alone(self, leaf: object) -> None:
        """Pass a leaf that is neither text nor a URL object through untouched."""
        assert (
            encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, leaf)
            == leaf
        )

    def test_naming_the_key_it_skipped_and_never_the_value(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Log which override key was left unparsed, without echoing the URL.

        Asserted rather than left implicit: a test that only checks the leaf came
        back unchanged passes identically against a silent implementation, and a
        silently skipped row is the shape an operator cannot diagnose. The URL
        carries a password so that the no-echo assertion has something to catch.
        """
        url = "https://user:pw@[bad:ipv6/"

        with caplog.at_level(logging.DEBUG, logger=SECRET_STORAGE_LOGGER):
            encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url)

        assert "could not be parsed" in caplog.text
        assert f"ExtensionsSettings.{INVENTORY_ENDPOINT_KEY}" in caplog.text
        assert url not in caplog.text

    def test_a_credential_free_endpoint_is_skipped_silently(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Say nothing about an endpoint that simply carries no credential.

        This runs per row on every snapshot refresh, so a line here would be
        steady-state noise rather than a diagnosable event.
        """
        url = "https://inv.example.com:8443/api"

        with caplog.at_level(logging.DEBUG, logger=SECRET_STORAGE_LOGGER):
            assert (
                encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url)
                == url
            )

        assert [
            record for record in caplog.records if record.name == SECRET_STORAGE_LOGGER
        ] == []

    def test_round_trips_a_percent_encoded_password(self) -> None:
        """Encrypt and restore a password carrying ``@`` and ``:`` byte-for-byte.

        ``urlparse`` hands back the still-encoded segment, so the encrypted and
        restored URL match the submitted one only when the transform never
        unquotes it.
        """
        url = "https://u:p%40ss%3Aword@host:8443/api"

        stored = encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url)

        assert stored_plaintext(url_password(stored)) == "p%40ss%3Aword"
        assert (
            decrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, stored)
            == url
        )

    def test_preserves_an_ipv6_host_and_port(self) -> None:
        """Keep the bracketed literal and port through the parse/reassemble."""
        url = "https://u:pw@[2001:db8::1]:8443/api"

        stored = encrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url)

        assert stored.startswith("https://u:")
        assert stored.endswith("@[2001:db8::1]:8443/api")
        assert (
            decrypt_secret_leaves(ExtensionsSettings, INVENTORY_ENDPOINT_KEY, stored)
            == url
        )

    def test_ciphertext_never_passes_through_url_validation(self) -> None:
        """Pin that the stored token survives, which pydantic's ``HttpUrl`` would corrupt.

        ``HttpUrl`` percent-encodes the Fernet token's ``=`` padding to ``%3D``.
        The orderings that keep it away — validate before encrypting on write,
        decrypt before validating on read — are what this asserts is intact.
        """
        stored = encrypt_secret_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
        )

        assert "%3D" not in url_password(stored)
        assert is_stored_ciphertext(url_password(stored))

    def test_unresolvable_key_returns_the_url_unchanged(self) -> None:
        """Return the value untouched for a key resolving to no field."""
        assert (
            encrypt_secret_leaves(ExtensionsSettings, "NO_SUCH_FIELD", CREDENTIAL_URL)
            == CREDENTIAL_URL
        )


class TestCredentialUrlWritePathValueType:
    """Cover the leaf type the write path actually hands the walker.

    ``coerce_field_value`` validates a ``CredentialHttpUrl`` field to a
    :class:`~pydantic.HttpUrl` and ``unwrap_secrets_for_storage`` passes the
    object through, so three of the four live leaves reach the walker as URL
    objects rather than text. Every payload here is built through
    ``coerce_field_value`` for that reason: a hand-written string cannot fail.
    """

    @staticmethod
    def _coerced(settings_cls: type[BaseYamlSettings], key: str) -> Any:
        """Return what the write path hands the walker for ``key``.

        :param settings_cls: The settings class owning ``key``.
        :param key: The override row's key.
        :return: The validated value, as ``coerce_field_value`` produces it.
        """
        if "__" in key:
            resolved = resolve_nested_field(settings_cls, key)
            assert resolved is not None, f"{key} must resolve to a nested field"
            field_info = resolved[1]
        else:
            field_info = settings_cls.model_fields[key]
        return coerce_field_value(field_info, CREDENTIAL_URL)

    @pytest.mark.parametrize(
        ("settings_cls", "key"),
        [
            (ExtensionsSettings, INVENTORY_ENDPOINT_KEY),
            (ExtensionsSettings, TASKS_ENDPOINT_KEY),
            (TasksSettings, NOMAD_ENDPOINT_KEY),
        ],
    )
    def test_a_url_object_leaf_is_encrypted_and_stored_as_text(
        self, settings_cls: type[BaseYamlSettings], key: str
    ) -> None:
        """Normalize the ``Url`` the write path produces instead of skipping it.

        A leaf branch guarded on ``isinstance(value, str)`` returns these three
        untouched, which is a green suite and plaintext on disk for exactly the
        fields the ticket names first.
        """
        coerced = self._coerced(settings_cls, key)
        assert not isinstance(coerced, str), "the fixture must exercise the URL object"

        stored = encrypt_secret_leaves(settings_cls, key, coerced)

        assert isinstance(stored, str)
        assert stored_plaintext(url_password(stored)) == CREDENTIAL_PASSWORD
        assert url_without_password(stored) == url_without_password(CREDENTIAL_URL)

    def test_a_str_leaf_is_encrypted_the_same_way(self) -> None:
        """Cover the sibling whose ``AsTypeValidator`` casts back to text."""
        coerced = self._coerced(Settings, PMM_ENDPOINT_KEY)
        assert isinstance(coerced, str), "PMM__ENDPOINT stores text"

        stored = encrypt_secret_leaves(Settings, PMM_ENDPOINT_KEY, coerced)

        assert stored_plaintext(url_password(stored)) == CREDENTIAL_PASSWORD

    def test_normalizing_a_url_object_reproduces_the_submitted_string(self) -> None:
        """Pin that storing the normalized text changes nothing about the column.

        The JSON column's serializer emits the same string for the ``Url``
        object, so returning text where the caller passed one is invisible
        downstream.
        """
        coerced = self._coerced(ExtensionsSettings, INVENTORY_ENDPOINT_KEY)

        assert str(coerced) == CREDENTIAL_URL


class TestReencryptCredentialUrlLeaves:
    """Cover the idempotent, credential-URL-scoped upgrade the revisions call."""

    def test_encrypts_a_plaintext_password(self) -> None:
        """Rewrite a password the pre-release write path stored in the clear."""
        stored = reencrypt_credential_url_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
        )

        assert stored_plaintext(url_password(stored)) == CREDENTIAL_PASSWORD

    def test_a_second_run_rewrites_nothing(self) -> None:
        """Leave an already-encrypted password byte-identical on a re-run.

        ``is_encrypted`` on the whole credential URL is always ``False`` — the
        URL is not a Fernet token — so an idempotence check applied to the leaf
        instead of the password re-encrypts every run and destroys the plaintext.
        """
        once = reencrypt_credential_url_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
        )

        assert not is_stored_ciphertext(once)
        assert (
            reencrypt_credential_url_leaves(
                ExtensionsSettings, INVENTORY_ENDPOINT_KEY, once
            )
            == once
        )

    def test_a_foreign_token_password_is_never_re_encrypted(self) -> None:
        """Leave ciphertext this key cannot decrypt alone, since re-encrypting destroys it."""
        url = f"https://u:{foreign_token()}@host:8443/api"

        assert (
            reencrypt_credential_url_leaves(
                ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url
            )
            == url
        )

    def test_a_fernet_shaped_plaintext_password_is_skipped(self) -> None:
        """Pin the inherited limitation: a token-shaped plaintext stays in the clear."""
        url = f"https://u:{FERNET_SHAPED_PLAINTEXT}@host:8443/api"

        assert (
            reencrypt_credential_url_leaves(
                ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url
            )
            == url
        )

    def test_leaves_a_secret_sibling_in_the_clear(self) -> None:
        """Rewrite only the URL password, never a plaintext ``SecretStr`` leaf.

        The ``SecretStr`` half belongs to the earlier revision; rewriting it
        here would make this revision's downgrade stop being its own inverse.
        """
        stored = reencrypt_credential_url_leaves(
            Settings, PMM_KEY, pmm_payload_with_credential_endpoint(api_key="plain")
        )

        assert stored_plaintext(url_password(stored["endpoint"])) == CREDENTIAL_PASSWORD
        assert stored["api_key"] == "plain"


class TestDecryptCredentialUrlLeaves:
    """Cover the credential-URL-scoped downgrade the revisions call."""

    def test_restores_the_original_url(self) -> None:
        """Return the submitted URL byte-identical after an encrypt/decrypt round trip."""
        stored = reencrypt_credential_url_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
        )

        assert (
            decrypt_credential_url_leaves(
                ExtensionsSettings, INVENTORY_ENDPOINT_KEY, stored
            )
            == CREDENTIAL_URL
        )

    def test_passes_a_legacy_plaintext_password_through(self) -> None:
        """Leave a row written before the revision ran resolvable."""
        assert (
            decrypt_credential_url_leaves(
                ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
            )
            == CREDENTIAL_URL
        )

    def test_raises_on_a_foreign_token_password(self) -> None:
        """Raise :class:`DecryptionError` for a password minted under another key."""
        url = f"https://u:{foreign_token()}@host:8443/api"

        with pytest.raises(DecryptionError):
            decrypt_credential_url_leaves(
                ExtensionsSettings, INVENTORY_ENDPOINT_KEY, url
            )

    def test_leaves_a_secret_sibling_ciphertext_byte_identical(self) -> None:
        """Leave the earlier revision's ``SecretStr`` ciphertext exactly as it stands.

        Asserted byte-for-byte rather than with ``is_encrypted``: a
        re-encryption under a fresh Fernet IV would satisfy the weaker check
        while having rewritten the very data this downgrade must not touch.
        """
        seeded = encrypt_secret_leaves(
            Settings, PMM_KEY, pmm_payload_with_credential_endpoint()
        )

        restored = decrypt_credential_url_leaves(Settings, PMM_KEY, seeded)

        assert url_password(restored["endpoint"]) == CREDENTIAL_PASSWORD
        assert restored["api_key"] == seeded["api_key"]


class TestBroadEntryPointsCoverBothLeafKinds:
    """Pin that the read and write paths stay broad while the revisions narrow.

    ``cache.py`` and ``routes.py`` must transform both leaf kinds; only the
    credential-URL revisions are scoped to that kind. Narrowing the broad pair
    would leave a ``SecretStr`` in the clear on every write.
    """

    def test_the_write_path_transforms_both_kinds(self) -> None:
        """Encrypt the URL password and the ``SecretStr`` sibling in one call."""
        stored = encrypt_secret_leaves(
            Settings, PMM_KEY, pmm_payload_with_credential_endpoint()
        )

        assert is_stored_ciphertext(url_password(stored["endpoint"]))
        assert is_stored_ciphertext(stored["api_key"])

    def test_the_read_path_restores_both_kinds(self) -> None:
        """Restore the URL password and the ``SecretStr`` sibling in one call."""
        payload = pmm_payload_with_credential_endpoint()
        stored = encrypt_secret_leaves(Settings, PMM_KEY, payload)

        assert decrypt_secret_leaves(Settings, PMM_KEY, stored) == payload

    def test_the_broad_upgrade_transforms_both_kinds(self) -> None:
        """Cover both kinds from ``reencrypt_secret_leaves``, which the earlier revisions call."""
        stored = reencrypt_secret_leaves(
            Settings, PMM_KEY, pmm_payload_with_credential_endpoint()
        )

        assert is_stored_ciphertext(url_password(stored["endpoint"]))
        assert is_stored_ciphertext(stored["api_key"])


class TestTheWritePathMarksWhatItStores:
    """Cover the envelope marker every leaf this module writes now carries."""

    def test_marks_a_top_level_model_secret_leaf(self) -> None:
        """Mark the ciphertext at ``$.api_key`` of a whole-object override."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())

        assert marked_ciphertext(stored["api_key"]) is not None

    def test_marks_a_bare_nested_leaf(self) -> None:
        """Mark a leaf whose override key addresses the secret field directly."""
        stored = encrypt_secret_leaves(Settings, PMM_NESTED_KEY, "pmm-secret")

        assert marked_ciphertext(stored) is not None

    def test_marks_a_polymorphic_provider_secret(self) -> None:
        """Mark a secret found through a discriminated union inside a list."""
        stored = encrypt_secret_leaves(
            AlertSettings, PROVIDERS_KEY, pagerduty_payload()
        )

        assert marked_ciphertext(stored[0]["routing_key"]) is not None

    def test_marks_every_leaf_of_a_secret_valued_mapping(self) -> None:
        """Mark each value of a materializer-backed ``dict[str, SecretStr]``."""
        stored = encrypt_secret_leaves(
            ExtensionsSettings, DELIVERY_INPUTS_KEY, delivery_inputs_payload()
        )

        assert stored["secrets"]
        assert all(
            marked_ciphertext(leaf) is not None for leaf in stored["secrets"].values()
        )

    def test_marks_a_credential_url_password_and_preserves_the_rest(self) -> None:
        """Mark only the userinfo password, leaving every other URL component intact.

        The marker is drawn from RFC 3986's unreserved set precisely so it
        survives a userinfo segment: a character needing percent-encoding would
        be re-encoded on the way in and unrecoverable on the way out.
        """
        stored = encrypt_secret_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
        )

        parsed = urlparse(stored)
        original = urlparse(CREDENTIAL_URL)
        assert marked_ciphertext(url_password(stored)) is not None
        assert parsed.scheme == original.scheme
        assert parsed.username == original.username
        assert parsed.hostname == original.hostname
        assert parsed.port == original.port
        assert parsed.path == original.path


class TestTheEnvelopeDecidesBeforeTheStructuralCheck:
    """Cover the discriminator's two halves over the values that separate them."""

    def test_a_legacy_plaintext_carrying_the_marker_is_migrated(self) -> None:
        """Encrypt a pre-envelope plaintext that merely begins with the marker.

        Seeded directly rather than through :func:`encrypt_secret_leaves`,
        because the writer never produces this shape — the point is that a
        *stored* value carrying the prefix over a payload that is not a token
        is claimed by neither discriminator and so reaches the encrypt branch,
        rather than being skipped and left in the clear.
        """
        seeded = mark_ciphertext("operator-secret")

        stored = reencrypt_secret_leaves(Settings, PMM_NESTED_KEY, seeded)

        assert stored != seeded
        assert marked_ciphertext(stored) is not None
        assert stored_plaintext(stored) == seeded

    def test_a_legacy_plaintext_carrying_the_marker_reads_through_unchanged(
        self,
    ) -> None:
        """Return that same pre-migration value untouched instead of raising.

        Until the migration rewrites it, the row still holds plaintext, and a
        read that tried to decrypt it would drop the override.
        """
        seeded = mark_ciphertext("operator-secret")

        assert decrypt_secret_leaves(Settings, PMM_NESTED_KEY, seeded) == seeded

    def test_a_marker_over_a_fernet_shaped_plaintext_is_the_narrowed_residual(
        self,
    ) -> None:
        """Pin the one collision the envelope cannot remove, and its narrowness.

        Being skipped requires the exact marker *and* the ~0.4% structural
        shape behind it, so this is the pre-existing collision narrowed by many
        orders of magnitude rather than eliminated. Pinned so the residual is a
        recorded decision rather than an unexamined gap.
        """
        seeded = mark_ciphertext(FERNET_SHAPED_PLAINTEXT)

        assert reencrypt_secret_leaves(Settings, PMM_NESTED_KEY, seeded) == seeded

    def test_the_write_path_ignores_a_marker_in_the_plaintext(self) -> None:
        """Encrypt a submitted credential that begins with the marker, and round-trip it.

        The write path consults neither discriminator, so no prefix an operator
        types can divert it.
        """
        secret = mark_ciphertext("operator-secret")

        stored = encrypt_secret_leaves(Settings, PMM_NESTED_KEY, secret)

        assert marked_ciphertext(stored) is not None
        assert decrypt_secret_leaves(Settings, PMM_NESTED_KEY, stored) == secret

    def test_a_legacy_unmarked_row_still_decrypts(self) -> None:
        """Read a row written before the envelope through the structural fallback.

        No migration re-marks existing rows, so the fallback is the only thing
        keeping every pre-envelope deployment readable.
        """
        seeded = pmm_payload(encrypt("pmm-secret"))

        assert decrypt_secret_leaves(Settings, PMM_KEY, seeded) == pmm_payload()

    def test_a_legacy_unmarked_row_is_not_rewritten_by_the_migration(self) -> None:
        """Leave a pre-envelope ciphertext byte-identical rather than marking it.

        Marking it would require deciding it is ciphertext, which for an
        unmarked row is exactly the structural guess the envelope exists to
        stop trusting — and marking a legacy collision plaintext would freeze
        that misclassification permanently.
        """
        seeded = pmm_payload(encrypt("pmm-secret"))

        assert reencrypt_secret_leaves(Settings, PMM_KEY, seeded) == seeded

    def test_a_marked_leaf_is_not_re_encrypted(self) -> None:
        """Recognise this module's own output so a migration re-run is a no-op."""
        once = reencrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())

        assert reencrypt_secret_leaves(Settings, PMM_KEY, once) == once


class TestUnmarkSecretLeaves:
    """Cover the rollback transform the new revisions' downgrade calls."""

    def test_strips_the_marker_and_leaves_the_ciphertext(self) -> None:
        """Return a bare token a release predating the envelope reads correctly."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())
        assert marked_ciphertext(stored["api_key"]) is not None, (
            "the seed must be marked, or the unmark below is a no-op and this "
            "test passes even with marking removed from the write path"
        )

        unmarked = unmark_secret_leaves(Settings, PMM_KEY, stored)

        assert marked_ciphertext(unmarked["api_key"]) is None
        assert is_encrypted(unmarked["api_key"])
        assert decrypt(unmarked["api_key"]) == "pmm-secret"

    def test_covers_both_leaf_kinds(self) -> None:
        """Strip the marker from the credential-URL password and its ``SecretStr`` sibling.

        :func:`encrypt_secret_leaves` marks both, so a rollback scoped to one
        would leave the other marked for a reader that cannot recognise it.
        """
        stored = encrypt_secret_leaves(
            Settings, PMM_KEY, pmm_payload_with_credential_endpoint()
        )
        assert marked_ciphertext(stored["api_key"]) is not None
        assert marked_ciphertext(url_password(stored["endpoint"])) is not None

        unmarked = unmark_secret_leaves(Settings, PMM_KEY, stored)

        assert marked_ciphertext(unmarked["api_key"]) is None
        assert marked_ciphertext(url_password(unmarked["endpoint"])) is None
        assert is_encrypted(url_password(unmarked["endpoint"]))

    def test_preserves_every_other_url_component(self) -> None:
        """Rewrite only the password, so the endpoint survives the rollback intact."""
        stored = encrypt_secret_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, CREDENTIAL_URL
        )

        unmarked = unmark_secret_leaves(
            ExtensionsSettings, INVENTORY_ENDPOINT_KEY, stored
        )

        assert url_without_password(unmarked) == url_without_password(CREDENTIAL_URL)
        assert decrypt(url_password(unmarked)) == CREDENTIAL_PASSWORD

    def test_never_decrypts_a_foreign_key_token(self) -> None:
        """Strip the marker off ciphertext this key cannot read, so the rollback runs keyless.

        Reaching for a decrypt here would fail on exactly the rows — foreign-key
        ciphertext — that the older release would otherwise have kept.
        """
        token = foreign_token()
        stored = pmm_payload(mark_ciphertext(token))

        unmarked = unmark_secret_leaves(Settings, PMM_KEY, stored)

        assert unmarked["api_key"] == token

    def test_leaves_a_legacy_unmarked_row_byte_identical(self) -> None:
        """Leave a row written before the envelope untouched, so the op is safe to re-run."""
        seeded = pmm_payload(encrypt("pmm-secret"))

        assert unmark_secret_leaves(Settings, PMM_KEY, seeded) == seeded

    def test_is_idempotent(self) -> None:
        """Return the first result unchanged, so a re-run issues no ``UPDATE``."""
        stored = encrypt_secret_leaves(Settings, PMM_KEY, pmm_payload())
        once = unmark_secret_leaves(Settings, PMM_KEY, stored)

        assert unmark_secret_leaves(Settings, PMM_KEY, once) == once

    def test_leaves_a_plaintext_leaf_alone(self) -> None:
        """Return a never-encrypted leaf unchanged rather than mangling it."""
        payload = pmm_payload()

        assert unmark_secret_leaves(Settings, PMM_KEY, payload) == payload
