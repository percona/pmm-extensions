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

"""Smoke-test the API-first surface of the Operations Bootstrap app.

A ``BaseApp`` exposes a declared ``api_router`` rather than the derived task
contract, so this mounts that router behind the production auth guard and
asserts ``GET /schema`` answers. The real routes (``/runs`` and friends) have
their own tests in ``test_api_routes.py``.
"""

from fastapi import status

from app.core.auth.providers.casdoor.models import CasdoorUser
from tests.app.extensions.apps.om_bootstrap.conftest import api_client, BASE


def test_schema_200(regular_user: CasdoorUser) -> None:
    """Serve the plugin schema at ``GET /schema``."""
    response = api_client(regular_user).get(f"{BASE}/schema")

    assert response.status_code == status.HTTP_200_OK
