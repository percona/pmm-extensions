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

"""Define auth utility functions."""

from app.core.auth.config import get_active_auth_provider
from app.core.auth.models import BaseUser


def get_user_model() -> type[BaseUser]:
    """Return the user model class of the active authentication provider.

    :return: The active provider's user model class.
    """
    return get_active_auth_provider().user_model
