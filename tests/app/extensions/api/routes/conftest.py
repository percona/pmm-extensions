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

"""Provide fixtures shared by the PMM Extensions API route test modules."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.deps import (
    get_api_authenticated_admin,
    get_current_user,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app


@pytest.fixture
def admin_client(admin_user: CasdoorUser) -> Iterator[TestClient]:
    """Yield an admin TestClient with the Bearer gate satisfied.

    :param admin_user: The administrator every request authenticates as.
    :return: The client, with its dependency overrides cleared on teardown.
    """
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_api_authenticated_admin] = lambda: (
        admin_user
    )
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}
