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

"""Define tests for the provider-agnostic auth models."""

import operator
from typing import Any, NoReturn
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.auth.models import BaseUser, UserRole


def _unsupported(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject any provider operation the stub identity below does not implement.

    :raises NotImplementedError: Always.
    """
    raise NotImplementedError


class CustomProviderUser(BaseUser):
    """Stand in for an out-of-tree provider's identity.

    A third-party subclass selected through
    ``AUTH__PROVIDER__CUSTOM__PROVIDER_CLASS`` inherits none of the in-tree
    providers' role reconstruction, so it is the shape that pins ``role`` as a
    required field. Every abstract provider operation is bound to a rejecting
    stub because the tests here only construct identities.
    """

    from_jwt = _unsupported
    from_token_payload = _unsupported
    get_oauth_token = _unsupported
    get_user = _unsupported
    get_users = _unsupported
    invalidate_oauth_token = _unsupported


def _build_user(**fields: Any) -> CustomProviderUser:
    """Build a stub identity, filling the fields the tests do not care about.

    :param fields: Field overrides forwarded to the model.
    :return: The constructed identity.
    """
    return CustomProviderUser(id=uuid4(), username="tester", **fields)


class TestBaseUserContract:
    """Verify the operations an out-of-tree provider's user model must implement."""

    def test_abstract_operations_are_exactly_the_routed_ones(self):
        """Verify only operations the auth routes reach are abstract."""
        assert BaseUser.__abstractmethods__ == {
            "from_jwt",
            "from_token_payload",
            "get_oauth_token",
            "get_user",
            "get_users",
            "invalidate_oauth_token",
        }

    def test_user_implementing_only_routed_operations_builds(self):
        """Verify a provider that implements only the routed operations is concrete."""
        assert _build_user(role=UserRole.VIEWER).username == "tester"

    def test_user_still_defining_dropped_operations_builds(self):
        """Verify a provider written against the wider contract keeps working."""

        class LegacyProviderUser(CustomProviderUser):
            from_code = _unsupported
            from_password = _unsupported
            invalidate_tokens_for_user = _unsupported

        user = LegacyProviderUser(id=uuid4(), username="legacy", role=UserRole.ADMIN)

        assert user.username == "legacy"


class TestUserRoleOrdering:
    """Verify ``UserRole`` members compare by rank rather than by name."""

    def test_declared_order_is_strictly_increasing(self):
        """Verify each member ranks above the one declared before it."""
        members = list(UserRole)
        assert members == [
            UserRole.NONE,
            UserRole.VIEWER,
            UserRole.EDITOR,
            UserRole.ADMIN,
            UserRole.SUPER_ADMIN,
        ]
        assert all(
            lower < higher for lower, higher in zip(members, members[1:], strict=False)
        )

    @pytest.mark.parametrize(
        ("compare", "expected"),
        [
            (operator.lt, True),
            (operator.le, True),
            (operator.gt, False),
            (operator.ge, False),
        ],
    )
    def test_every_operator_orders_by_rank(self, compare, expected):
        """Verify all four comparisons rank ``EDITOR`` below ``ADMIN``.

        ``"editor" > "admin"`` lexicographically, so an operator left to
        ``str``'s implementation answers the opposite of every case here.
        """
        assert compare(UserRole.EDITOR, UserRole.ADMIN) is expected

    def test_max_flattens_by_rank(self):
        """Verify ``max`` picks the highest rank, not the last name alphabetically."""
        assert max([UserRole.VIEWER, UserRole.EDITOR, UserRole.NONE]) is UserRole.EDITOR

    def test_sorted_orders_by_rank(self):
        """Verify ``sorted`` orders by rank, not by name."""
        assert sorted([UserRole.ADMIN, UserRole.NONE, UserRole.EDITOR]) == [
            UserRole.NONE,
            UserRole.EDITOR,
            UserRole.ADMIN,
        ]

    @pytest.mark.parametrize(
        "compare", [operator.lt, operator.le, operator.gt, operator.ge]
    )
    def test_comparison_with_a_foreign_type_is_unsupported(self, compare):
        """Verify no operator silently ranks a non-member."""
        with pytest.raises(TypeError):
            compare(UserRole.ADMIN, "editor")

    def test_members_are_not_strings(self):
        """Verify a member does not compare equal to a bare string."""
        assert UserRole.ADMIN != "admin"
        assert UserRole.SUPER_ADMIN != "super_admin"


class TestIsAdmin:
    """Verify the ``is_admin`` property derived from the ordered role."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (UserRole.NONE, False),
            (UserRole.VIEWER, False),
            (UserRole.EDITOR, False),
            (UserRole.ADMIN, True),
            (UserRole.SUPER_ADMIN, True),
        ],
    )
    def test_boundary_sits_at_admin(self, role, expected):
        """Verify ``is_admin`` is true exactly from ``ADMIN`` upwards."""
        assert _build_user(role=role).is_admin is expected

    def test_recomputes_after_the_role_is_reassigned(self):
        """Verify no cached value survives a role change."""
        user = _build_user(role=UserRole.VIEWER)
        assert user.is_admin is False
        user.role = UserRole.ADMIN
        assert user.is_admin is True

    def test_is_serialized(self):
        """Verify the derived flag is still part of the model's dump."""
        dumped = _build_user(role=UserRole.ADMIN).model_dump()
        assert dumped["is_admin"] is True
        assert dumped["role"] is UserRole.ADMIN


class TestRoleIsRequired:
    """Verify an identity built without a role fails loudly."""

    def test_omitting_the_role_raises(self):
        """Verify a missing role is a validation error, not the lowest role."""
        with pytest.raises(ValidationError):
            _build_user()

    def test_a_custom_provider_passing_only_the_legacy_flag_raises(self):
        """Verify an unmigrated out-of-tree provider breaks loudly.

        ``is_admin`` is derived, so a subclass that keeps constructing
        identities from the legacy flag alone supplies no role and must not
        silently resolve to one.
        """
        with pytest.raises(ValidationError):
            _build_user(is_admin=True)

    def test_an_unknown_role_value_raises(self):
        """Verify a value outside the enum is rejected."""
        with pytest.raises(ValidationError):
            _build_user(role="Bogus")

    def test_a_role_name_is_accepted(self):
        """Verify the enum mixin still coerces a member name."""
        assert _build_user(role="SUPER_ADMIN").role is UserRole.SUPER_ADMIN


class TestRoleFromAdminFlag:
    """Verify the legacy admin-flag mapping shared by both inbound paths."""

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            (True, UserRole.ADMIN),
            (False, UserRole.VIEWER),
            ("true", UserRole.ADMIN),
            ("True", UserRole.ADMIN),
            ("1", UserRole.ADMIN),
            (1, UserRole.ADMIN),
            ("false", UserRole.VIEWER),
            ("False", UserRole.VIEWER),
            ("0", UserRole.VIEWER),
            (0, UserRole.VIEWER),
            ("off", UserRole.VIEWER),
            (b"false", UserRole.VIEWER),
        ],
    )
    def test_validates_the_flag_instead_of_testing_truthiness(self, flag, expected):
        """Verify a spelled-out boolean resolves to the role it names.

        Truthiness would read every string here as admin, escalating a payload
        that spells the boolean out.
        """
        assert BaseUser._role_from_admin_flag(flag) is expected

    @pytest.mark.parametrize("flag", ["maybe", None, [], object()])
    def test_rejects_a_non_boolean_flag(self, flag):
        """Verify a non-boolean flag raises instead of resolving to a role."""
        with pytest.raises(ValueError, match="invalid admin flag"):
            BaseUser._role_from_admin_flag(flag)


class TestBuildServicePrincipal:
    """Verify the shared service-principal constructor."""

    def test_requires_an_explicit_role(self):
        """Verify the constructor no longer defaults the principal's authority."""
        with pytest.raises(TypeError):
            CustomProviderUser.build_service_principal(
                user_id=uuid4(), username="extensions-service"
            )

    def test_carries_the_requested_role(self):
        """Verify the role passed in reaches the built identity."""
        principal = CustomProviderUser.build_service_principal(
            user_id=uuid4(), username="extensions-service", role=UserRole.VIEWER
        )
        assert principal.role is UserRole.VIEWER
        assert principal.is_admin is False
