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

"""Define tests for the effective-delivery-plan resolver."""

import logging

import pytest
from pytest_mock import MockerFixture

from app.extensions.bundle_upload.plan import (
    DeliveryPlan,
    ProbeStep,
    SecretValue,
    UploadStep,
)
from app.extensions.bundle_upload.resolver import (
    DeliveryPlanResolution,
    DeliveryUnavailableCode,
    DRIFTED_INPUTS_REASON,
    resolve_delivery_plan,
    UNCONFIGURED_REASON,
)
from app.extensions.config import DeliveryPlanInputs, extensions_settings

_BAKED_ENDPOINT = "https://intake.example.com/"
_RESOLVER_LOGGER = "app.extensions.bundle_upload.resolver"


def _plan(secrets: dict[str, str], *, endpoint: str = _BAKED_ENDPOINT) -> DeliveryPlan:
    """Build a plan whose upload cites every secret name it declares.

    :param secrets: The secret names and values the plan declares.
    :param endpoint: The receiver base URL.
    :return: The validated plan.
    """
    return DeliveryPlan(
        endpoint=endpoint,
        secrets=secrets,
        upload={
            "path": "attachment/upload",
            "headers": {
                f"x-{name}": {"source": "secret", "name": name} for name in secrets
            },
            "reference_pointer": "/result/sys_id",
        },
    )


@pytest.fixture(name="no_inputs")
def no_inputs_fixture(mocker: MockerFixture) -> None:
    """Leave the runtime inputs unset, as a deployment that never PATCHed them."""
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY_INPUTS", None)


@pytest.mark.usefixtures("no_inputs")
class TestSkeletonOnly:
    """Cover the resolver's behaviour when no runtime inputs are supplied."""

    def test_reports_the_unconfigured_reason_without_a_baked_plan(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Report the ordinary unconfigured state silently, without a log line."""
        mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", None)

        with caplog.at_level(logging.INFO, logger=_RESOLVER_LOGGER):
            resolution = resolve_delivery_plan()

        assert resolution.plan is None
        assert resolution.unavailable_reason == UNCONFIGURED_REASON
        assert resolution.code is DeliveryUnavailableCode.UNCONFIGURED
        assert caplog.records == []

    def test_returns_the_baked_plan_unchanged(self, mocker: MockerFixture) -> None:
        """Hand back a fully-configured skeleton as-is for a standalone deployment."""
        skeleton = _plan({"api_key": "real-api-key"})
        mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", skeleton)

        resolution = resolve_delivery_plan()

        assert resolution.plan is skeleton
        assert resolution.unavailable_reason is None

    def test_reports_every_declared_secret_left_empty(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Refuse a plan whose declared secrets have no values, naming each one."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _plan({"client_token": "", "sn_api_key": ""}),
        )

        with caplog.at_level(logging.INFO, logger=_RESOLVER_LOGGER):
            resolution = resolve_delivery_plan()

        assert resolution.plan is None
        assert resolution.unavailable_reason == UNCONFIGURED_REASON
        assert resolution.code is DeliveryUnavailableCode.UNCONFIGURED
        assert "client_token, sn_api_key" in caplog.text


class TestMergedInputs:
    """Cover the merge of runtime inputs onto the baked skeleton."""

    def test_supplied_secrets_reach_the_merged_plan(
        self, mocker: MockerFixture
    ) -> None:
        """Deliver with the values an operator supplied, not the baked blanks."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _plan({"client_token": "", "sn_api_key": ""}),
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(
                secrets={"client_token": "token-value", "sn_api_key": "key-value"}
            ),
        )

        plan = resolve_delivery_plan().plan

        assert plan is not None
        assert plan.secrets["sn_api_key"].get_secret_value() == "key-value"
        assert plan.secrets["client_token"].get_secret_value() == "token-value"

    def test_reports_only_the_secret_still_left_empty(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Name just the missing half when the inputs supply one of two secrets."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _plan({"client_token": "", "sn_api_key": ""}),
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        with caplog.at_level(logging.INFO, logger=_RESOLVER_LOGGER):
            resolution = resolve_delivery_plan()

        assert resolution.plan is None
        assert resolution.unavailable_reason == UNCONFIGURED_REASON
        assert resolution.code is DeliveryUnavailableCode.UNCONFIGURED
        assert "client_token" in caplog.text
        assert "sn_api_key" not in caplog.text

    def test_endpoint_input_replaces_the_baked_endpoint(
        self, mocker: MockerFixture
    ) -> None:
        """Point delivery at the receiver an operator named instead of the baked one."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(
                endpoint="https://elsewhere.example.com/",
                secrets={"sn_api_key": "key-value"},
            ),
        )

        plan = resolve_delivery_plan().plan

        assert plan is not None
        assert str(plan.endpoint) == "https://elsewhere.example.com/"

    def test_absent_endpoint_input_keeps_the_baked_endpoint(
        self, mocker: MockerFixture
    ) -> None:
        """Keep the baked receiver when the inputs carry secrets only."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        plan = resolve_delivery_plan().plan

        assert plan is not None
        assert str(plan.endpoint) == _BAKED_ENDPOINT

    def test_a_subset_of_the_declared_secrets_still_merges(
        self, mocker: MockerFixture
    ) -> None:
        """Let the baked value stand for a declared secret the inputs omit."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _plan({"sn_api_key": "", "client_token": "baked-token"}),
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        plan = resolve_delivery_plan().plan

        assert plan is not None
        assert plan.secrets["client_token"].get_secret_value() == "baked-token"

    def test_a_declared_probe_survives_the_merge(self, mocker: MockerFixture) -> None:
        """Carry the probe through the rebuild the runtime inputs force.

        Supplying inputs takes the resolver down its dump-merge-revalidate path
        rather than handing the skeleton back whole, so this is the only route
        on which a newly added plan field can be dropped.
        """
        skeleton = _plan({"sn_api_key": ""}).model_copy(
            update={
                "probe": ProbeStep(
                    path="health",
                    headers={"x-key": SecretValue(source="secret", name="sn_api_key")},
                )
            }
        )
        mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", skeleton)
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        plan = resolve_delivery_plan().plan

        assert plan is not None
        assert plan.probe is not None
        assert plan.probe.path == "health"
        assert plan.probe.headers["x-key"].name == "sn_api_key"
        assert plan.secrets["sn_api_key"].get_secret_value() == "key-value"

    def test_reports_the_unconfigured_reason_when_the_merged_plan_fails_validation(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Degrade to unconfigured instead of raising into the request path.

        The skeleton is copied past validation so it cites a secret it never
        declares -- a shape the settings loader rejects but the annotation admits
        -- which is what makes the rebuilt plan fail cross-reference validation.
        """
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _plan({"sn_api_key": "key-value"}).model_copy(
                update={
                    "upload": UploadStep(
                        path="attachment/upload",
                        headers={
                            "x-client-token": {
                                "source": "secret",
                                "name": "client_token",
                            }
                        },
                    )
                }
            ),
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
            resolution = resolve_delivery_plan()

        assert resolution.plan is None
        assert resolution.unavailable_reason == UNCONFIGURED_REASON
        assert resolution.code is DeliveryUnavailableCode.UNCONFIGURED
        assert "client_token" in caplog.text


class TestUndeclaredFieldAtResolution:
    """Cover a merged plan whose rebuild meets a key no plan model declares."""

    def test_an_undeclared_field_in_the_merge_resolves_to_unconfigured(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Degrade to unconfigured, naming the key but not the value it carries.

        A configured plan cannot bring an undeclared key here: settings load
        refuses it first, and ``model_dump`` emits declared fields only. The dump
        is patched to stand in for a serialized shape that no longer validates,
        which is the one way such a key could reach the rebuild.
        """
        original_dump = DeliveryPlan.model_dump
        mocker.patch.object(
            DeliveryPlan,
            "model_dump",
            autospec=True,
            side_effect=lambda plan, **kwargs: (
                original_dump(plan, **kwargs)
                | {"stray_field": "sentinel-credential-value"}
            ),
        )
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
            resolution = resolve_delivery_plan()

        assert resolution.plan is None
        assert resolution.code is DeliveryUnavailableCode.UNCONFIGURED
        assert "stray_field" in caplog.text
        assert "sentinel-credential-value" not in caplog.text


class TestDriftedInputs:
    """Cover inputs whose secret names stopped matching the baked skeleton.

    An image upgrade that renames a declared secret leaves the stored inputs
    untouched, and the operator has to re-supply them. That is the opposite
    action from a deployment nobody ever configured, so the two states may not
    report the same thing.
    """

    def test_reports_an_undeclared_secret_name_as_drift(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Tell the operator the stored inputs no longer fit this deployment.

        Inverts the previous contract, under which an undeclared name was
        silently filtered out of the merged plan and delivery either carried on
        with a credential the operator did not mean to use or reported itself as
        never configured.
        """
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(
                secrets={"sn_api_key": "key-value", "renamed_token": "token-value"}
            ),
        )

        with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
            resolution = resolve_delivery_plan()

        assert resolution.plan is None
        assert resolution.unavailable_reason == DRIFTED_INPUTS_REASON
        assert resolution.code is DeliveryUnavailableCode.DRIFTED_INPUTS
        assert "renamed_token" in caplog.text

    def test_drift_outranks_a_secret_the_rename_left_without_a_value(
        self, mocker: MockerFixture
    ) -> None:
        """Blame the rename, not the value it left behind.

        A rename produces both symptoms at once: the old name is undeclared and
        the new one has only the skeleton's empty value. Reporting the empty
        value would send the operator looking for a secret to supply under a
        name they have never seen.
        """
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"case_token": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"client_token": "token-value"}),
        )

        assert resolve_delivery_plan().unavailable_reason == DRIFTED_INPUTS_REASON

    def test_stored_inputs_without_a_baked_plan_are_not_drift(
        self, mocker: MockerFixture
    ) -> None:
        """Call a deployment that bakes no plan unconfigured, never drifted.

        There is nothing for the inputs to have drifted from, and re-supplying
        them would not help.
        """
        mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", None)
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        assert resolve_delivery_plan().unavailable_reason == UNCONFIGURED_REASON

    def test_empty_stored_secrets_report_the_unconfigured_state(
        self, mocker: MockerFixture
    ) -> None:
        """Keep an inputs row that names nothing on the unconfigured path."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY_INPUTS", DeliveryPlanInputs()
        )

        assert resolve_delivery_plan().unavailable_reason == UNCONFIGURED_REASON

    def test_a_secret_a_later_plan_added_is_not_drift(
        self, mocker: MockerFixture
    ) -> None:
        """Route an upgrade that adds a declared secret to the unconfigured text.

        Every name the stored inputs carry is still declared; the plan simply
        declares one more, and supplying it is the remedy the unconfigured text
        already asks for. A rename is the case that needs its own reason,
        because it strands the stored credentials under names nothing reads.
        """
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _plan({"sn_api_key": "", "case_token": ""}),
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"sn_api_key": "key-value"}),
        )

        assert resolve_delivery_plan().unavailable_reason == UNCONFIGURED_REASON

    def test_a_case_variant_of_a_declared_name_is_drift(
        self, mocker: MockerFixture
    ) -> None:
        """Match secret names exactly, as every other read of them does."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"SN_API_KEY": "key-value"}),
        )

        assert resolve_delivery_plan().unavailable_reason == DRIFTED_INPUTS_REASON

    def test_the_reason_names_no_secret_the_plan_declares(
        self, mocker: MockerFixture
    ) -> None:
        """Keep receiver-internal names out of a string an operator reads.

        The operator cannot act on them: they name fields of the receiver's own
        API, not anything the deployment exposes.
        """
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", _plan({"sn_api_key": ""})
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(
                secrets={"sn_api_key": "key-value", "renamed_token": "token-value"}
            ),
        )

        reason = resolve_delivery_plan().unavailable_reason

        assert reason is not None
        assert "sn_api_key" not in reason
        assert "renamed_token" not in reason


class TestDeliveryPlanResolutionInvariant:
    """Cover the one-outcome rule the resolution type states."""

    def test_a_plan_alone_builds(self) -> None:
        """Accept the shape a deployment able to deliver produces."""
        plan = _plan({"sn_api_key": "key-value"})

        assert DeliveryPlanResolution.resolved(plan).plan is plan

    def test_a_reason_alone_builds(self) -> None:
        """Accept the shape a deployment unable to deliver produces."""
        resolution = DeliveryPlanResolution.unavailable(
            UNCONFIGURED_REASON, DeliveryUnavailableCode.UNCONFIGURED
        )

        assert resolution.unavailable_reason == UNCONFIGURED_REASON
        assert resolution.code is DeliveryUnavailableCode.UNCONFIGURED

    def test_a_resolved_plan_carries_no_code(self) -> None:
        """Leave the code unset when there is a plan, so it tracks the reason."""
        assert DeliveryPlanResolution.resolved(_plan({"sn_api_key": "v"})).code is None

    def test_a_reason_without_a_code_is_refused(self) -> None:
        """Refuse the shape that would force a consumer back onto the prose."""
        with pytest.raises(ValueError, match="set together or not at all"):
            DeliveryPlanResolution(
                plan=None, unavailable_reason=UNCONFIGURED_REASON, code=None
            )

    def test_carrying_both_outcomes_is_refused(self) -> None:
        """Refuse a resolution a caller would read two contradictory ways."""
        with pytest.raises(ValueError, match="not both and not neither"):
            DeliveryPlanResolution(
                plan=_plan({"sn_api_key": "key-value"}),
                unavailable_reason=UNCONFIGURED_REASON,
            )

    def test_carrying_neither_outcome_is_refused(self) -> None:
        """Refuse the empty resolution, which reads as a plan that is missing."""
        with pytest.raises(ValueError, match="not both and not neither"):
            DeliveryPlanResolution(plan=None, unavailable_reason=None)
