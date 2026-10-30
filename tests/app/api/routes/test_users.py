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

"""Define tests for the app.api.routes.users module."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.auth.models import BaseUser
from app.core.auth.utils import get_user_model
from app.main import app

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    """Clear the overrides a test installed, whether or not it passed."""
    yield
    app.dependency_overrides = {}


@pytest.fixture
def test_client():
    """Create a test client for the app."""
    return TestClient(app)


def test_retrieve_current_user(test_client, regular_user):
    """Test retrieving the current authenticated user."""
    app.dependency_overrides[get_current_user] = lambda: regular_user
    response = test_client.get("/api/users/me")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(regular_user.id)


@pytest.mark.parametrize("path", ["/api/users/", "/api/users", "/api/users/someone"])
def test_removed_directory_paths_are_not_found(test_client, admin_user, path):
    """Verify the removed user-directory paths answer 404, even to an admin."""
    app.dependency_overrides[get_current_user] = lambda: admin_user

    response = test_client.get(path, follow_redirects=False)

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_removed_directory_paths_leave_the_schema(test_client):
    """Verify the OpenAPI schema keeps ``/me`` and drops the directory paths."""
    paths = test_client.get("/openapi.json").json()["paths"]

    assert "/api/users/me" in paths
    assert "/api/users/" not in paths
    assert "/api/users/{username}" not in paths


class TestUserRoleIsServed:
    """Verify the ordered role reaches clients through a real response.

    ``role`` and ``is_admin`` are only useful once serialized, so the
    ``User``-returning route is asserted at the request level rather than on
    the model.
    """

    def test_current_user_carries_the_role(self, test_client, regular_user):
        """Verify ``/me`` serves the role alongside the derived admin flag."""
        app.dependency_overrides[get_current_user] = lambda: regular_user

        body = test_client.get("/api/users/me").json()

        assert body["role"] == regular_user.role.value
        assert body["isAdmin"] is False


class TestProviderFieldsSurviveTheResponseModel:
    """Cover the fields only the configured provider's user class declares.

    ``/me`` annotates the static ``BaseUser`` and names the concrete class in an
    explicit ``response_model=``. Without that kwarg FastAPI would derive the
    response model from the return annotation and filter every provider-specific
    field out of both the body and the published schema.

    The expected set is derived rather than hardcoded, so it names whatever the
    configured provider adds over ``BaseUser``. Each case asserts that set is
    non-empty first: only Casdoor — the provider the suite runs under — adds
    any, and without that control the subset assertions would pass vacuously
    against a provider that adds none.
    """

    @staticmethod
    def _provider_only_fields() -> set[str]:
        """Return the serialized names the user class adds over ``BaseUser``.

        Named by alias, since the provider models generate camelCase ones and
        both the response body and the schema are keyed on those.
        """
        return {
            field.alias or name
            for name, field in User.model_fields.items()
            if name not in BaseUser.model_fields
        }

    def test_current_user_carries_them(self, test_client, regular_user):
        """Verify the self endpoint keeps the provider's own fields."""
        app.dependency_overrides[get_current_user] = lambda: regular_user

        response = test_client.get("/api/users/me")

        assert response.status_code == status.HTTP_200_OK
        assert self._provider_only_fields()
        assert self._provider_only_fields() <= response.json().keys()

    def test_published_schema_still_declares_them(self, test_client):
        """Verify the fields survive in the OpenAPI schema, not just in a body.

        A body assertion alone passes when a fixture happens to leave the field
        unset, so the schema is the half that has to be checked separately.
        """
        schema = test_client.get("/openapi.json").json()
        assert self._provider_only_fields()

        content = schema["paths"]["/api/users/me"]["get"]["responses"]["200"]["content"]
        ref = content["application/json"]["schema"]["$ref"]
        component = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]

        assert self._provider_only_fields() <= component["properties"].keys()
