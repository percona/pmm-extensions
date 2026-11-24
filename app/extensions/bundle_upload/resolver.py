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

"""Resolve the effective delivery plan from the baked skeleton and its inputs.

This module must stay out of ``app/extensions/bundle_upload/__init__.py``:
``app.extensions.config`` imports :class:`~app.extensions.bundle_upload.plan.DeliveryPlan` from
this package, so the package ``__init__`` executes while ``app.extensions.config`` is
still running its own module body, and importing ``extensions_settings`` from there
would fail. Import this module directly instead, as ``app.extensions.apps.atw.send``
already does for ``factory``.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from app.core.utils import deep_dict_update
from app.extensions.bundle_upload.plan import DeliveryPlan
from app.extensions.config import extensions_settings

__all__ = [
    "DRIFTED_INPUTS_REASON",
    "UNCONFIGURED_REASON",
    "DeliveryPlanResolution",
    "DeliveryUnavailableCode",
    "resolve_delivery_plan",
]

logger = logging.getLogger(__name__)

#: Reported when this deployment has no usable receiver at all: no baked plan,
#: or a declared secret still without a value. The operator has to configure
#: delivery.
UNCONFIGURED_REASON = "Diagnostics delivery is not configured"

#: Reported when delivery was configured and the inputs have since stopped
#: fitting the plan, which an image upgrade renaming a declared secret produces.
#: The operator has to re-supply the inputs, so this may not read as the
#: never-configured state. It names no secret: those are the receiver's own
#: internal field names, which the operator cannot act on.
DRIFTED_INPUTS_REASON = (
    "The stored diagnostics delivery inputs no longer match this deployment's "
    "delivery plan; re-supply them."
)


class DeliveryUnavailableCode(StrEnum):
    """Name why delivery is unavailable, for a consumer that must branch on it.

    The reason constants above are operator-facing prose and may be reworded;
    a caller choosing what to render or which action to offer branches on this
    instead, so no consumer has to match on the wording.
    """

    UNCONFIGURED = "unconfigured"
    DRIFTED_INPUTS = "drifted_inputs"


@dataclass(frozen=True, slots=True)
class DeliveryPlanResolution:
    """Report the effective delivery plan, or why there is not one.

    Exactly one member is set. The reason exists because three distinct causes
    — no baked plan, a declared secret left empty, and inputs that no longer
    fit the plan — would otherwise collapse into one indistinguishable "not
    configured", and the last of them calls for a different action from the
    other two.

    Build one through :meth:`resolved` or :meth:`unavailable` rather than the
    constructor, which states the same invariant but only as a runtime refusal.

    :param plan: The plan to deliver with, or ``None`` when there is none.
    :param unavailable_reason: The operator-facing reason there is no plan, or
        ``None`` when there is one. Never names a secret the plan declares.
    :param code: The machine-readable form of ``unavailable_reason``, set
        whenever that is; ``None`` when there is a plan.
    """

    plan: DeliveryPlan | None
    unavailable_reason: str | None
    code: DeliveryUnavailableCode | None = None

    def __post_init__(self) -> None:
        """Refuse a resolution that carries both outcomes, or neither.

        Callers read one member off the other's emptiness, so an instance
        holding neither would report a deployment as able to deliver with no
        plan to deliver by. The code moves with the reason for the same reason:
        a consumer branching on it would otherwise have to fall back to the
        prose exactly when the prose is all there is.

        :raises ValueError: When both outcomes are set, neither is, or the code
            and the reason do not accompany each other.
        """
        if (self.plan is None) == (self.unavailable_reason is None):
            raise ValueError(
                "A delivery plan resolution carries either a plan or a reason "
                "for there being none, not both and not neither."
            )
        if (self.unavailable_reason is None) != (self.code is None):
            raise ValueError(
                "A delivery plan resolution's unavailability code and reason "
                "are set together or not at all."
            )

    @classmethod
    def resolved(cls, plan: DeliveryPlan) -> DeliveryPlanResolution:
        """Return the resolution for a deployment that can deliver.

        :param plan: The plan to deliver with.
        :return: The resolution carrying it.
        """
        return cls(plan=plan, unavailable_reason=None)

    @classmethod
    def unavailable(
        cls, reason: str, code: DeliveryUnavailableCode
    ) -> DeliveryPlanResolution:
        """Return the resolution for a deployment that cannot deliver.

        :param reason: The operator-facing reason, from this module's constants.
        :param code: The machine-readable form of that reason.
        :return: The resolution carrying both.
        """
        return cls(plan=None, unavailable_reason=reason, code=code)

    @classmethod
    def unconfigured(cls) -> DeliveryPlanResolution:
        """Return the resolution for a deployment with no usable receiver.

        Names the outcome the three unconfigured paths below share, so each
        states which case it reached rather than restating the pairing of
        reason and code.

        :return: The unconfigured resolution.
        """
        return cls.unavailable(
            UNCONFIGURED_REASON, DeliveryUnavailableCode.UNCONFIGURED
        )


def resolve_delivery_plan() -> DeliveryPlanResolution:
    """Return the effective delivery plan, or the reason there is none.

    Merge ``DIAGNOSTICS_DELIVERY_INPUTS`` onto the baked
    ``DIAGNOSTICS_DELIVERY`` skeleton and rebuild the result through
    :class:`~app.extensions.bundle_upload.plan.DeliveryPlan` validation, so a merged
    plan has passed the same cross-reference checks a file-configured one does.
    Both settings are read on every call, since an override that landed after
    import must take effect.

    Every unconfigured and every invalid case resolves to a reason rather than
    raising, so a caller rendering UI state from this value never fails merely
    because delivery is unconfigured or misconfigured.

    Inputs naming a secret the skeleton does not declare resolve to the drift
    reason. Inputs merely omitting a declared name do not, because the merge
    below falls back to the skeleton's own value. The whole row is refused
    rather than the undeclared name dropped: ``DeliveryPlanInputs.endpoint`` is
    applied unconditionally in the same overlay, so a row whose secret names no
    longer fit can also point delivery at a receiver this plan never named.

    Check order carries meaning. A deployment with no baked plan is
    unconfigured, never drifted — there is nothing to have drifted from. Drift
    is then decided before the empty-value check, because a rename produces both
    symptoms and only the rename is actionable.

    :return: The resolved plan, or the reason delivery is unavailable.
    """
    skeleton = extensions_settings.DIAGNOSTICS_DELIVERY
    if skeleton is None:
        return DeliveryPlanResolution.unconfigured()

    inputs = extensions_settings.DIAGNOSTICS_DELIVERY_INPUTS
    secrets = dict(skeleton.secrets)
    if inputs is not None:
        if undeclared := set(inputs.secrets) - set(skeleton.secrets):
            logger.warning(
                "Diagnostics delivery inputs name secret(s) %s, which this "
                "deployment's plan does not declare; treating the stored inputs "
                "as drifted.",
                ", ".join(sorted(undeclared)),
            )
            return DeliveryPlanResolution.unavailable(
                DRIFTED_INPUTS_REASON, DeliveryUnavailableCode.DRIFTED_INPUTS
            )
        secrets.update(inputs.secrets)
    if empty := sorted(
        name for name, secret in secrets.items() if not secret.get_secret_value()
    ):
        logger.info(
            "Diagnostics delivery has no value for secret(s) %s; treating it as "
            "unconfigured.",
            ", ".join(empty),
        )
        return DeliveryPlanResolution.unconfigured()
    if inputs is None:
        return DeliveryPlanResolution.resolved(skeleton)

    data = skeleton.model_dump()
    overlay = (
        {"secrets": secrets}
        if inputs.endpoint is None
        else {"secrets": secrets, "endpoint": inputs.endpoint}
    )
    deep_dict_update(data, overlay)
    try:
        plan = DeliveryPlan.model_validate(data)
    except ValidationError as exc:
        logger.warning("Diagnostics delivery inputs produce an invalid plan: %s", exc)
        return DeliveryPlanResolution.unconfigured()
    return DeliveryPlanResolution.resolved(plan)
