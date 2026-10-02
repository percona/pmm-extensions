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

"""Share a term pattern that backtracks exponentially, and a term that trips it.

The standard library's ``re`` does not return on this pair in any useful time,
so the executor and the route that calls it are both held to aborting it.
"""

RUNAWAY_PATTERN = r"(a|aa)+$"
RUNAWAY_TERM = "a" * 40 + "!"
