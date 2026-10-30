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

"""Define the API routes for User actions."""

import logging

from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.core.auth.models import BaseUser
from app.core.auth.utils import get_user_model

logger = logging.getLogger(__name__)

router = APIRouter()

User = get_user_model()


@router.get("/me", response_model=User)
async def retrieve_current_user(current_user: CurrentUser) -> BaseUser:
    """Retrieve the current authenticated user.

    :param current_user: The current authenticated user.
    :type current_user: CurrentUser
    :return: The current authenticated user.
    :rtype: User
    """
    return current_user
