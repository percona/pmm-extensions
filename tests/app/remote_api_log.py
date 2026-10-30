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

"""Share what a client's tests expect ``RemoteAPI`` to log for a withheld body.

The transport's own placeholder is private to ``app.core.requests.remote_api``,
so a test of a client that withholds its bodies spells the expected text here
instead of importing it across modules.
"""

from typing import Final

#: The text a ``RemoteAPI`` log record shows in place of a withheld body.
WITHHELD_BODY_LOG: Final = "<withheld>"
