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

"""Test masked-secret and credential-URL preservation on the PATCH write path."""

import typing
from types import GenericAlias

import pytest
from pydantic import BaseModel, HttpUrl, SecretBytes, SecretStr
from pydantic.fields import FieldInfo

from app.core.alerts.config import AlertSettings
from app.core.alerts.providers.pagerduty import PagerDutyEventsAlertProvider
from app.core.settings_override import registry, secret_preservation
from app.core.settings_override.registry import (
    dump_field_value,
    hot_field,
    SECRET_STR_MASK,
    unwrap_secrets_for_storage,
)
from app.core.settings_override.secret_preservation import (
    _stable_collection_items,
    preserve_credential_urls_in_model_payload,
    preserve_patch_credential_url_value,
    preserve_patch_secret_value,
    preserve_secrets_in_model_payload,
)
from app.core.utils.fields import CredentialHttpUrl, redact_credential_url
from app.extensions.config import ExtensionsSettings
from app.tasks.config import TasksSettings
from app.tasks.execution.executors.nomad.models import NomadExecutor
from tests.app.core.settings_override.conftest import TaggedSecret


class _SecretLeafModel(BaseModel):
    """Declare a nested model with a scalar ``SecretStr`` leaf (PMM-shaped)."""

    api_key: SecretStr
    label: str = "ok"


class _OptionalValueSecretDict(BaseModel):
    """Declare a mapping whose value type unions a secret with ``None``."""

    tokens: dict[str, SecretStr | None] = {}


class _OptionalElementSecretList(BaseModel):
    """Declare a collection whose element type unions a secret with ``None``."""

    tokens: list[SecretStr | None] = []


class _OptionalElementModelList(BaseModel):
    """Declare a collection whose element type unions a model with ``None``."""

    items: list[_SecretLeafModel | None] = []


class _ModelList(BaseModel):
    """Declare a homogeneous collection of secret-bearing models."""

    items: list[_SecretLeafModel] = []


class _PlainCollections(BaseModel):
    """Declare secret-free mapping and collection fields with union members."""

    labels: dict[str, str | None] = {}
    names: list[str | None] = []


class _TaggedSecretSet(BaseModel):
    """Declare an unordered collection of secret-bearing models."""

    items: frozenset[TaggedSecret] = frozenset()


class _SecretSets(BaseModel):
    """Declare unordered collections of bare secret wrappers."""

    tokens: set[SecretStr] = set()
    blobs: frozenset[SecretBytes] = frozenset()


def _tagged_set() -> frozenset[TaggedSecret]:
    """Return stored items whose secret order disagrees with their label order.

    :return: Three items: sorted by secret they read ``b, c, a``; by label
        ``a, b, c``. A path sorting on labels would therefore pair wrongly.
    """
    return frozenset(
        {
            TaggedSecret(api_key=SecretStr("zz-secret"), label="a"),
            TaggedSecret(api_key=SecretStr("aa-secret"), label="b"),
            TaggedSecret(api_key=SecretStr("mm-secret"), label="c"),
        }
    )


class TestCollectionOrderingContract:
    """Cover the GET dump and PATCH restore paths agreeing on set order.

    Masked set elements are restored by position, so if the two paths sorted
    differently a PATCH would write one item's stored secret onto another.
    """

    def test_both_paths_share_one_sort_key(self) -> None:
        """Assert the restore path uses the registry's sort key object itself."""
        assert (
            secret_preservation.stable_collection_sort_key
            is registry.stable_collection_sort_key
        )

    def test_get_dump_order_matches_patch_restore_order(self) -> None:
        """Assert both paths list a set of models in the same order."""
        field_info = _TaggedSecretSet.model_fields["items"]
        current = _tagged_set()

        dumped = dump_field_value(field_info, current)
        restore_order = _stable_collection_items(current)

        assert [item["label"] for item in dumped] == ["b", "c", "a"]
        assert [item.label for item in restore_order] == ["b", "c", "a"]

    def test_round_tripped_masks_restore_each_item_own_secret(self) -> None:
        """Restore every masked item from its own stored model, never a neighbour."""
        field_info = _TaggedSecretSet.model_fields["items"]
        current = _tagged_set()

        preserved = preserve_patch_secret_value(
            field_info, current, dump_field_value(field_info, current)
        )

        assert sorted(preserved, key=lambda item: item["label"]) == [
            {"api_key": "zz-secret", "label": "a"},
            {"api_key": "aa-secret", "label": "b"},
            {"api_key": "mm-secret", "label": "c"},
        ]

    @pytest.mark.parametrize(
        ("field_name", "current", "replacement", "expected"),
        [
            (
                "tokens",
                {SecretStr("c"), SecretStr("a"), SecretStr("b")},
                "new",
                ["a", "new", "c"],
            ),
            (
                "blobs",
                frozenset({SecretBytes(b"c"), SecretBytes(b"a"), SecretBytes(b"b")}),
                "new",
                [b"a", "new", b"c"],
            ),
        ],
        ids=["secret-str-set", "secret-bytes-frozenset"],
    )
    def test_edit_at_get_position_replaces_only_that_element(
        self,
        field_name: str,
        current: set[SecretStr] | frozenset[SecretBytes],
        replacement: str,
        expected: list[str | bytes],
    ) -> None:
        """Replace exactly the element shown at the edited GET position.

        :param field_name: The secret-set field under test.
        :param current: The stored unordered collection.
        :param replacement: The new plaintext submitted at index 1.
        :param expected: The restored payload, in GET order.
        :return: ``None``.
        """
        field_info = _SecretSets.model_fields[field_name]
        dumped = dump_field_value(field_info, current)
        assert dumped == [SECRET_STR_MASK] * 3
        dumped[1] = replacement

        preserved = preserve_patch_secret_value(field_info, current, dumped)

        assert preserved == expected


class TestSecretValuedDictPayloads:
    """Cover ``dict``-shaped secret payloads, including malformed annotations."""

    def test_union_valued_mapping_restores_masked_entry(self) -> None:
        """Restore a masked entry whose annotated value type unions with ``None``."""
        current = _OptionalValueSecretDict(tokens={"first": SecretStr("stored")})

        preserved = preserve_secrets_in_model_payload(
            _OptionalValueSecretDict, current, {"tokens": {"first": SECRET_STR_MASK}}
        )

        assert preserved == {"tokens": {"first": "stored"}}

    def test_absent_stored_entry_keeps_the_mask(self) -> None:
        """Leave the mask in place when no stored secret backs the submitted key.

        Restoring from a missing entry would be a guess. Keeping the literal is
        what lets a mask-rejecting field validator refuse the payload, which is
        how a mask reaches storage only over a class that declares no such
        validator.
        """
        current = _OptionalValueSecretDict(tokens={"first": SecretStr("stored")})

        preserved = preserve_secrets_in_model_payload(
            _OptionalValueSecretDict, current, {"tokens": {"second": SECRET_STR_MASK}}
        )

        assert preserved == {"tokens": {"second": SECRET_STR_MASK}}

    def test_secret_free_union_valued_mapping_is_left_alone(self) -> None:
        """Skip restoration for a mapping whose union value type holds no secret."""
        preserved = preserve_patch_secret_value(
            _PlainCollections.model_fields["labels"],
            {"first": "stored"},
            {"first": SECRET_STR_MASK},
        )

        assert preserved == {"first": SECRET_STR_MASK}

    def test_mapping_annotation_of_unexpected_arity_is_not_a_secret_mapping(
        self,
    ) -> None:
        """Skip restoration for a mapping annotation that is not a key/value pair."""
        # Built at runtime: a three-argument ``dict[...]`` written literally is a
        # static type error, and the point of the test is the arity the resolver
        # has to survive, not the annotation being well-formed.
        field_info = FieldInfo.from_annotation(
            GenericAlias(dict, (str, SecretStr, str))
        )

        preserved = preserve_patch_secret_value(
            field_info, {"first": SecretStr("stored")}, {"first": SECRET_STR_MASK}
        )

        assert preserved == {"first": SECRET_STR_MASK}


class TestSecretValuedSequencePayloads:
    """Cover collection payloads whose elements are secrets."""

    def test_union_element_collection_restores_masked_element(self) -> None:
        """Restore a masked element whose annotated type unions with ``None``."""
        current = _OptionalElementSecretList(
            tokens=[SecretStr("first"), SecretStr("second")]
        )

        preserved = preserve_secrets_in_model_payload(
            _OptionalElementSecretList,
            current,
            {"tokens": [SECRET_STR_MASK, "replacement"]},
        )

        assert preserved == {"tokens": ["first", "replacement"]}

    def test_unparameterized_collection_annotation_restores_nothing(self) -> None:
        """Skip restoration for a collection annotation with no element type."""
        field_info = FieldInfo.from_annotation(typing.List)  # noqa: UP006

        preserved = preserve_patch_secret_value(field_info, None, [SECRET_STR_MASK])

        assert preserved == [SECRET_STR_MASK]


def _secret_leaves(*labels: str) -> list[_SecretLeafModel]:
    """Return one stored model per label, each holding ``secret-<label>``."""
    return [
        _SecretLeafModel(api_key=SecretStr(f"secret-{label}"), label=label)
        for label in labels
    ]


class TestModelCollectionPairing:
    """Cover which stored collection item a masked incoming item inherits from."""

    def test_union_element_model_collection_restores_masked_leaf(self) -> None:
        """Pair items when the annotated element type unions a model with ``None``."""
        current = _OptionalElementModelList(
            items=[_SecretLeafModel(api_key=SecretStr("k"))]
        )

        preserved = preserve_secrets_in_model_payload(
            _OptionalElementModelList,
            current,
            {"items": [{"api_key": SECRET_STR_MASK, "label": "public"}]},
        )

        assert preserved == {"items": [{"api_key": "k", "label": "public"}]}

    def test_secret_free_union_element_collection_is_left_alone(self) -> None:
        """Skip restoration for a collection whose union element holds no secret."""
        preserved = preserve_patch_secret_value(
            _PlainCollections.model_fields["names"], None, [SECRET_STR_MASK]
        )

        assert preserved == [SECRET_STR_MASK]

    def test_stored_scalar_item_keeps_the_mask(self) -> None:
        """Keep the mask when the paired stored item is neither model nor mapping."""
        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"],
            ["scalar"],
            [{"api_key": SECRET_STR_MASK}],
        )

        assert preserved == [{"api_key": SECRET_STR_MASK}]

    def test_stored_mapping_item_restores_masked_keys_shallowly(self) -> None:
        """Restore from a stored fingerprint mapping, keeping unbacked masks intact."""
        field_info = _ModelList.model_fields["items"]
        current = [{"api_key": SecretStr("stored")}]
        incoming = [
            {"api_key": SECRET_STR_MASK, "other": SECRET_STR_MASK, "label": "public"},
        ]

        preserved = preserve_patch_secret_value(field_info, current, incoming)

        assert preserved == [
            {"api_key": "stored", "other": SECRET_STR_MASK, "label": "public"},
        ]

    def test_non_mapping_item_passes_through(self) -> None:
        """Forward a collection element that is not a mapping untouched."""
        field_info = _ModelList.model_fields["items"]
        current = [_SecretLeafModel(api_key=SecretStr("stored"))]

        preserved = preserve_patch_secret_value(
            field_info, current, ["not-a-mapping", {"api_key": SECRET_STR_MASK}]
        )

        assert preserved == ["not-a-mapping", {"api_key": "stored"}]

    def test_tied_score_prefers_the_same_position(self) -> None:
        """Break a value-identity tie on the element's own position."""
        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"],
            _secret_leaves("a", "b"),
            ["scalar", {"api_key": SECRET_STR_MASK}],
        )

        assert preserved == ["scalar", {"api_key": "secret-b"}]

    def test_stored_mappings_pair_by_position(self) -> None:
        """Pair each element with the stored mapping at its own position."""
        current = [{"api_key": SecretStr("fp-a")}, {"api_key": SecretStr("fp-b")}]
        incoming = [{"api_key": SECRET_STR_MASK}, {"api_key": SECRET_STR_MASK}]

        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"], current, incoming
        )

        assert preserved == [{"api_key": "fp-a"}, {"api_key": "fp-b"}]

    def test_unmatched_item_keeps_the_mask(self) -> None:
        """Keep the mask when the discriminator matches no unused stored model.

        Item 0 claims the only model, so item 1 is left with stored fingerprint
        mappings, which a discriminator never pairs with.
        """
        field_info = _ModelList.model_fields["items"]
        current = [
            {"api_key": SecretStr("fingerprint")},
            _SecretLeafModel(api_key=SecretStr("model")),
            {"api_key": SecretStr("other-fingerprint")},
        ]
        incoming = [
            {"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK},
            {"PROVIDER": "nonesuch", "api_key": SECRET_STR_MASK},
        ]

        preserved = preserve_patch_secret_value(field_info, current, incoming)

        assert preserved == [
            {"PROVIDER": "secretleaf", "api_key": "model"},
            {"PROVIDER": "nonesuch", "api_key": SECRET_STR_MASK},
        ]

    def test_sole_unused_stored_item_pairs_past_the_preferred_index(self) -> None:
        """Pair with the only unused stored item when the preferred slot is absent."""
        field_info = _ModelList.model_fields["items"]
        current = [{"api_key": SecretStr("fingerprint")}]

        preserved = preserve_patch_secret_value(
            field_info, current, ["scalar", {"api_key": SECRET_STR_MASK}]
        )

        assert preserved == ["scalar", {"api_key": "fingerprint"}]

    def test_several_unused_stored_items_yield_no_pairing(self) -> None:
        """Keep the mask when the preferred slot is absent and no single item remains."""
        field_info = _ModelList.model_fields["items"]
        current = [
            {"api_key": SecretStr("first")},
            {"api_key": SecretStr("second")},
        ]

        preserved = preserve_patch_secret_value(
            field_info,
            current,
            ["scalar", "scalar", {"api_key": SECRET_STR_MASK}],
        )

        assert preserved == ["scalar", "scalar", {"api_key": SECRET_STR_MASK}]

    def test_non_collection_stored_value_yields_no_pairing(self) -> None:
        """Keep masks when the stored value is not a collection at all."""
        field_info = _ModelList.model_fields["items"]

        preserved = preserve_patch_secret_value(
            field_info, None, [{"api_key": SECRET_STR_MASK}]
        )

        assert preserved == [{"api_key": SECRET_STR_MASK}]


class _ProviderSecretLeaf(_SecretLeafModel):
    """Declare a model whose ``provider`` field names no class."""

    provider: str = "external"


class _ProviderModelList(BaseModel):
    """Declare a collection of models carrying a non-discriminating ``provider``."""

    items: list[_ProviderSecretLeaf] = []


class TestDiscriminatedPairing:
    """Cover pairing for elements that carry a ``PROVIDER``/``provider`` key."""

    @pytest.mark.parametrize("key", ["PROVIDER", "provider"])
    def test_unmatched_first_element_keeps_the_mask(self, key: str) -> None:
        """Keep the mask rather than inherit the secret stored at the same index."""
        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"],
            _secret_leaves("a", "b"),
            [{key: "nonesuch", "api_key": SECRET_STR_MASK}],
        )

        assert preserved == [{key: "nonesuch", "api_key": SECRET_STR_MASK}]

    def test_unmatched_alert_provider_keeps_routing_key_mask(self) -> None:
        """Keep a routing-key mask whose provider type no stored entry has."""
        current = {PagerDutyEventsAlertProvider(routing_key=SecretStr("real-key"))}
        incoming = [{"PROVIDER": "opsgenie", "routing_key": SECRET_STR_MASK}]

        preserved = preserve_patch_secret_value(
            AlertSettings.model_fields["PROVIDERS"], current, incoming
        )

        assert preserved == [{"PROVIDER": "opsgenie", "routing_key": SECRET_STR_MASK}]

    def test_mixed_payload_unmatched_element_does_not_consume_a_stored_item(
        self,
    ) -> None:
        """Leave the item an unmatched element skipped free for a later element."""
        incoming = [
            {"PROVIDER": "nonesuch", "api_key": SECRET_STR_MASK},
            {"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK, "label": "b"},
            {"api_key": SECRET_STR_MASK},
        ]

        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"], _secret_leaves("a", "b"), incoming
        )

        assert preserved == [
            {"PROVIDER": "nonesuch", "api_key": SECRET_STR_MASK},
            {"PROVIDER": "secretleaf", "api_key": "secret-b", "label": "b"},
            {"api_key": "secret-a"},
        ]

    def test_discriminator_matching_only_consumed_items_keeps_the_mask(
        self,
    ) -> None:
        """Keep the mask once every model the discriminator matches is taken."""
        incoming = [
            {"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK},
            {"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK},
        ]

        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"], _secret_leaves("a"), incoming
        )

        assert preserved == [
            {"PROVIDER": "secretleaf", "api_key": "secret-a"},
            {"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK},
        ]

    def test_non_string_discriminator_is_unmatched(self) -> None:
        """Treat a non-string discriminator that names no class as unmatched."""
        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"],
            _secret_leaves("a"),
            [{"PROVIDER": 7, "api_key": SECRET_STR_MASK}],
        )

        assert preserved == [{"PROVIDER": 7, "api_key": SECRET_STR_MASK}]

    def test_discriminated_element_skips_stored_mappings(self) -> None:
        """Keep the mask instead of pairing a typed element with a fingerprint."""
        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"],
            [{"api_key": SecretStr("fingerprint")}],
            [{"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK}],
        )

        assert preserved == [{"PROVIDER": "secretleaf", "api_key": SECRET_STR_MASK}]

    @pytest.mark.parametrize("blank", [None, "", 0], ids=["null", "empty", "zero"])
    @pytest.mark.parametrize("key", ["PROVIDER", "provider"])
    @pytest.mark.parametrize(
        "current",
        [_secret_leaves("a"), [{"api_key": SecretStr("secret-a")}]],
        ids=["stored-model", "stored-mapping"],
    )
    def test_blank_discriminator_pairs_like_no_discriminator(
        self, current: list[object], key: str, blank: str | int | None
    ) -> None:
        """Pair an element whose discriminator is blank as if it had none."""
        incoming = [{key: blank, "api_key": SECRET_STR_MASK}]

        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"], current, incoming
        )

        assert preserved == [{key: blank, "api_key": "secret-a"}]

    def test_unmatched_element_is_forwarded_as_a_copy(self) -> None:
        """Forward an unmatched element's values unchanged without aliasing it."""
        element = {"PROVIDER": "nonesuch", "api_key": "brand-new"}

        preserved = preserve_patch_secret_value(
            _ModelList.model_fields["items"], _secret_leaves("a"), [element]
        )

        assert preserved == [element]
        assert preserved[0] is not element

    @pytest.mark.parametrize(
        ("order", "expected_secrets"),
        [
            ((1,), ["secret-b", "secret-a", "secret-c"]),
            ((2, 0), ["secret-c", "secret-a", "secret-b"]),
        ],
        ids=["tie-takes-first-unused", "last-unused-item"],
    )
    def test_undiscriminated_elements_pair_without_reusing_a_stored_item(
        self, order: tuple[int, ...], expected_secrets: list[str]
    ) -> None:
        """Restore each stored secret at most once across undiscriminated elements."""
        current = _ProviderModelList(
            items=[
                _ProviderSecretLeaf(api_key=SecretStr(f"secret-{label}"), label=label)
                for label in "abc"
            ]
        )
        incoming_items = [
            current.items[index].model_dump(mode="json", exclude={"provider"})
            for index in order
        ]
        # Without labels every remaining model scores the same, so pairing
        # falls to the tie-break rather than value identity.
        anonymous = {"api_key": SECRET_STR_MASK}
        incoming_items.extend(anonymous.copy() for _ in range(4 - len(incoming_items)))

        preserved = preserve_secrets_in_model_payload(
            _ProviderModelList, current, {"items": incoming_items}
        )

        assert preserved == {
            "items": [
                {**item, "api_key": secret}
                for item, secret in zip(
                    incoming_items, [*expected_secrets, SECRET_STR_MASK], strict=True
                )
            ]
        }

    def test_unrecognized_provider_value_keeps_the_mask(self) -> None:
        """Keep the mask when a ``provider`` value names no stored class."""
        current = _ProviderModelList(
            items=[_ProviderSecretLeaf(api_key=SecretStr("secret-a"))]
        )
        element = current.items[0].model_dump(mode="json")

        preserved = preserve_secrets_in_model_payload(
            _ProviderModelList, current, {"items": [element]}
        )

        assert preserved == {"items": [{**element, "api_key": SECRET_STR_MASK}]}


class TestAbsentStoredValue:
    """Cover a PATCH that has no stored value to restore from."""

    def test_absent_stored_model_keeps_the_mask(self) -> None:
        """Keep the mask when there is no stored model to restore from."""
        preserved = preserve_secrets_in_model_payload(
            _SecretLeafModel, None, {"api_key": SECRET_STR_MASK}
        )

        assert preserved == {"api_key": SECRET_STR_MASK}


def test_preserve_patch_credential_url_value_for_scalar_field() -> None:
    """Assert scalar credential URL PATCH values restore the stored password when redacted."""
    field = ExtensionsSettings.model_fields["INVENTORY_ENDPOINT"]
    current = "http://inv-user:inv-secret@inventory.internal:8080"
    incoming = "http://inv-user:****@inventory.internal:8080"
    assert preserve_patch_credential_url_value(field, current, incoming) == current


def test_preserve_patch_credential_url_value_for_materializer_payload() -> None:
    """Assert whole-object materializer PATCH payloads preserve nested endpoint passwords."""
    field = TasksSettings.model_fields["NOMAD"]
    current = {"endpoint": "http://nomad-user:nomad-secret@nomad.internal:4646"}
    incoming = {"endpoint": "http://nomad-user:****@nomad.internal:4646"}
    preserved = preserve_patch_credential_url_value(field, current, incoming)
    assert preserved["endpoint"] == current["endpoint"]


def test_echoing_a_rendered_client_setting_restores_its_derived_base_url() -> None:
    """Restore the real password when a rendered Nomad setting is PATCHed back verbatim.

    The rendering carries the masked computed ``base_url`` next to the masked
    ``endpoint``. Only the endpoint is an input, so the restored endpoint must
    yield the real derived value and the mask must not reach storage.
    """
    field = TasksSettings.model_fields["NOMAD"]
    current = NomadExecutor.model_validate(
        {"endpoint": "http://nomad-user:nomad-secret@nomad.internal:4646"}
    )
    rendered = dump_field_value(field, current)

    preserved = preserve_patch_credential_url_value(field, current, rendered)
    restored = NomadExecutor.model_validate(preserved)

    assert restored.base_url == "http://nomad-user:nomad-secret@nomad.internal:4646"
    assert "base_url" not in unwrap_secrets_for_storage(restored)


class _CredentialUrlModel(BaseModel):
    """Represent a model with an optional credential-bearing endpoint."""

    endpoint: CredentialHttpUrl | None = HttpUrl.build(
        scheme="https",
        username="test-user",
        host="service.test",
        password="synthetic-test-value",
    )


def test_preserve_credential_urls_with_none_current_leaf() -> None:
    """Leave a masked URL unchanged when no live password exists to restore."""
    current = _CredentialUrlModel(endpoint=None)
    incoming = _CredentialUrlModel().model_dump(mode="json")

    preserved = preserve_credential_urls_in_model_payload(
        _CredentialUrlModel, current, incoming
    )

    assert preserved == incoming
    assert preserved is not incoming


def test_preserve_patch_credential_url_value_recurses_into_nested_model() -> None:
    """Restore a nested URL password while retaining an outer-field PATCH."""

    class _ConnectionGroup(BaseModel):
        connection: _CredentialUrlModel
        label: str

    class _ConnectionSettings(BaseModel):
        group: _ConnectionGroup

    current = _ConnectionGroup(
        connection=_CredentialUrlModel(),
        label="original",
    )
    incoming = current.model_dump(mode="json")
    incoming["label"] = "updated"

    preserved = preserve_patch_credential_url_value(
        _ConnectionSettings.model_fields["group"], current, incoming
    )

    assert preserved == {
        "connection": {"endpoint": str(current.connection.endpoint)},
        "label": "updated",
    }
    assert incoming["connection"]["endpoint"] == redact_credential_url(
        str(current.connection.endpoint)
    )


class _TopLevelSecretSettings(BaseModel):
    """Declare a settings class with a top-level ``SecretStr`` field."""

    TOKEN: SecretStr = hot_field(SecretStr("stored-top-secret"))


class _NestedSecretSettings(BaseModel):
    """Declare a settings class whose nested model holds a ``SecretStr``."""

    GROUP: _SecretLeafModel = hot_field(
        _SecretLeafModel(api_key=SecretStr("stored-nested-secret"))
    )


class _DictSecretSettings(BaseModel):
    """Declare a settings class with a ``dict[str, SecretStr]`` field."""

    secrets: dict[str, SecretStr] = hot_field(
        {"api_key": SecretStr("stored-dict-secret"), "token": SecretStr("keep-me")}
    )


def test_preserve_secrets_in_model_payload_with_secret_dict() -> None:
    """Restore masked dictionary values without replacing an explicit new secret."""
    current = _DictSecretSettings()
    incoming = current.model_dump(mode="json")
    incoming["secrets"]["token"] = "replacement-token"

    preserved = preserve_secrets_in_model_payload(
        _DictSecretSettings, current, incoming
    )

    assert preserved == {
        "secrets": {
            "api_key": current.secrets["api_key"].get_secret_value(),
            "token": "replacement-token",
        }
    }
    assert incoming["secrets"]["api_key"] == SECRET_STR_MASK


def test_preserve_secrets_in_model_payload_with_secret_list() -> None:
    """Restore masked list elements by position and keep explicitly replaced secrets."""

    class _ListOfSecretsSettings(BaseModel):
        tokens: list[SecretStr]

    current = _ListOfSecretsSettings(
        tokens=[SecretStr("keep-first"), SecretStr("replace-second")]
    )
    incoming = current.model_dump(mode="json")
    incoming["tokens"][1] = "new-second"

    preserved = preserve_secrets_in_model_payload(
        _ListOfSecretsSettings, current, incoming
    )

    assert preserved == {"tokens": ["keep-first", "new-second"]}
    assert incoming["tokens"][0] == SECRET_STR_MASK


@pytest.mark.parametrize(
    "order", [(2, 0, 1), (0, 2, 1)], ids=["last-first", "first-then-last"]
)
def test_preserve_secrets_in_model_payload_matches_reordered_models(
    order: tuple[int, ...],
) -> None:
    """Pair homogeneous model items by their public values rather than PATCH position."""

    class _ListSecretSettings(BaseModel):
        items: list[_SecretLeafModel]

    current = _ListSecretSettings(
        items=[
            _SecretLeafModel(api_key=SecretStr("secret-a"), label="a"),
            _SecretLeafModel(api_key=SecretStr("secret-b"), label="b"),
            _SecretLeafModel(api_key=SecretStr("secret-c"), label="c"),
        ]
    )
    incoming = {
        "items": [current.items[index].model_dump(mode="json") for index in order]
    }

    preserved = preserve_secrets_in_model_payload(
        _ListSecretSettings, current, incoming
    )

    assert preserved == {
        "items": [
            {
                "api_key": current.items[index].api_key.get_secret_value(),
                "label": current.items[index].label,
            }
            for index in order
        ]
    }


def test_preserve_patch_secret_value_for_top_level_field() -> None:
    """Assert a masked top-level SecretStr PATCH restores the stored secret."""
    field = _TopLevelSecretSettings.model_fields["TOKEN"]
    current = SecretStr("stored-top-secret")
    assert (
        preserve_patch_credential_url_value(field, current, SECRET_STR_MASK)
        == "stored-top-secret"
    )


def test_preserve_patch_secret_value_for_nested_leaf() -> None:
    """Assert a masked SecretStr on a nested model's leaf FieldInfo restores the stored secret."""
    field = _SecretLeafModel.model_fields["api_key"]
    current = SecretStr("stored-nested-secret")
    assert (
        preserve_patch_credential_url_value(field, current, SECRET_STR_MASK)
        == "stored-nested-secret"
    )


def test_preserve_patch_secret_value_overwrites_with_real_value() -> None:
    """Assert a non-mask SecretStr PATCH keeps the newly submitted value."""
    field = _TopLevelSecretSettings.model_fields["TOKEN"]
    current = SecretStr("stored-top-secret")
    assert (
        preserve_patch_credential_url_value(field, current, "brand-new-secret")
        == "brand-new-secret"
    )


def test_preserve_patch_secret_value_for_nested_model_payload() -> None:
    """Assert a whole-object nested-model PATCH restores masked SecretStr leaves."""
    field = _NestedSecretSettings.model_fields["GROUP"]
    current = _SecretLeafModel(api_key=SecretStr("stored-nested-secret"), label="ok")
    incoming = {"api_key": SECRET_STR_MASK, "label": "ok"}
    preserved = preserve_patch_credential_url_value(field, current, incoming)
    assert preserved["api_key"] == "stored-nested-secret"
    assert preserved["label"] == "ok"


def test_preserve_patch_secret_value_for_dict_of_secrets() -> None:
    """Assert masked values inside ``dict[str, SecretStr]`` are restored per key."""
    field = _DictSecretSettings.model_fields["secrets"]
    current = {
        "api_key": SecretStr("stored-dict-secret"),
        "token": SecretStr("keep-me"),
    }
    incoming = {"api_key": SECRET_STR_MASK, "token": "replacement-token"}
    preserved = preserve_patch_credential_url_value(field, current, incoming)
    assert preserved["api_key"] == "stored-dict-secret"
    assert preserved["token"] == "replacement-token"


def test_preserve_patch_secret_value_for_polymorphic_provider_set() -> None:
    """Assert masked subclass secrets in ``set[BaseAlertProvider]`` are restored."""
    field = AlertSettings.model_fields["PROVIDERS"]
    current = {PagerDutyEventsAlertProvider(routing_key=SecretStr("real-routing-key"))}
    incoming = [
        {
            "PROVIDER": "pagerduty",
            "routing_key": SECRET_STR_MASK,
            "api_endpoint": "https://events.pagerduty.com/v2/",
        }
    ]
    preserved = preserve_patch_credential_url_value(field, current, incoming)
    assert preserved[0]["routing_key"] == "real-routing-key"
    assert preserved[0]["PROVIDER"] == "pagerduty"


def test_preserve_patch_secret_value_for_two_pagerduty_providers() -> None:
    """Assert two same-type PROVIDERS restore by identity, not hash set order.

    Distinct ``api_endpoint`` values identify each entry when both routing keys
    are masked. Incoming order deliberately disagrees with sorted set order so
    positional pairing alone would swap secrets.
    """
    field = AlertSettings.model_fields["PROVIDERS"]
    first = PagerDutyEventsAlertProvider(
        routing_key=SecretStr("routing-key-a"),
        api_endpoint="https://events-a.example/v2/",
    )
    second = PagerDutyEventsAlertProvider(
        routing_key=SecretStr("routing-key-b"),
        api_endpoint="https://events-b.example/v2/",
    )
    incoming = [
        {
            "PROVIDER": "pagerduty",
            "routing_key": SECRET_STR_MASK,
            "api_endpoint": "https://events-b.example/v2/",
        },
        {
            "PROVIDER": "pagerduty",
            "routing_key": SECRET_STR_MASK,
            "api_endpoint": "https://events-a.example/v2/",
        },
    ]
    preserved = preserve_patch_credential_url_value(field, {first, second}, incoming)
    assert preserved[0]["routing_key"] == "routing-key-b"
    assert preserved[0]["api_endpoint"] == "https://events-b.example/v2/"
    assert preserved[1]["routing_key"] == "routing-key-a"
    assert preserved[1]["api_endpoint"] == "https://events-a.example/v2/"


def test_preserve_patch_secret_value_for_two_pagerduty_providers_same_endpoint() -> (
    None
):
    """Assert identical non-secret fields still restore via stable set order.

    When both entries share ``api_endpoint`` and both secrets are masked, GET
    dump order (stable sort by routing key) is the only identity signal.
    """
    field = AlertSettings.model_fields["PROVIDERS"]
    first = PagerDutyEventsAlertProvider(routing_key=SecretStr("routing-key-a"))
    second = PagerDutyEventsAlertProvider(routing_key=SecretStr("routing-key-b"))
    dumped = dump_field_value(field, {second, first})
    assert dumped[0]["routing_key"] == SECRET_STR_MASK
    assert dumped[1]["routing_key"] == SECRET_STR_MASK
    incoming = [
        {
            "PROVIDER": "pagerduty",
            "routing_key": SECRET_STR_MASK,
            "api_endpoint": dumped[0]["api_endpoint"],
        },
        {
            "PROVIDER": "pagerduty",
            "routing_key": SECRET_STR_MASK,
            "api_endpoint": dumped[1]["api_endpoint"],
        },
    ]
    preserved = preserve_patch_credential_url_value(field, {second, first}, incoming)
    assert preserved[0]["routing_key"] == "routing-key-a"
    assert preserved[1]["routing_key"] == "routing-key-b"


def test_preserve_patch_secret_value_for_list_of_models() -> None:
    """Assert masked secrets inside ``list[Model]`` whole-object PATCHes are restored."""

    class _Leaf(BaseModel):
        api_key: SecretStr
        label: str = "ok"

    class _ListSecretSettings(BaseModel):
        items: list[_Leaf] = hot_field([])

    field = _ListSecretSettings.model_fields["items"]
    current = [_Leaf(api_key=SecretStr("stored-list-secret"), label="a")]
    incoming = [{"api_key": SECRET_STR_MASK, "label": "a"}]
    preserved = preserve_patch_credential_url_value(field, current, incoming)
    assert preserved[0]["api_key"] == "stored-list-secret"
    assert preserved[0]["label"] == "a"


def test_preserve_patch_secret_value_for_list_of_secrets() -> None:
    """Assert masked elements inside ``list[SecretStr]`` are restored by index."""

    class _ListOfSecretsSettings(BaseModel):
        tokens: list[SecretStr] = hot_field([])

    field = _ListOfSecretsSettings.model_fields["tokens"]
    current = [SecretStr("keep-first"), SecretStr("keep-second")]
    incoming = [SECRET_STR_MASK, "brand-new-second"]
    preserved = preserve_patch_credential_url_value(field, current, incoming)
    assert preserved == ["keep-first", "brand-new-second"]


def test_preserve_patch_secret_value_for_set_of_secrets() -> None:
    """Assert masked elements inside ``set[SecretStr]`` restore via stable order."""

    class _SetOfSecretsSettings(BaseModel):
        tokens: set[SecretStr] = hot_field(set())

    field = _SetOfSecretsSettings.model_fields["tokens"]
    current = {SecretStr("keep-a"), SecretStr("keep-b")}
    dumped = dump_field_value(field, current)
    assert dumped == [SECRET_STR_MASK, SECRET_STR_MASK]
    preserved = preserve_patch_credential_url_value(field, current, dumped)
    assert preserved == ["keep-a", "keep-b"]
    preserved_partial = preserve_patch_credential_url_value(
        field, current, [SECRET_STR_MASK, "brand-new-b"]
    )
    assert preserved_partial == ["keep-a", "brand-new-b"]
