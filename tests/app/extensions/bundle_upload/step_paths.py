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

"""Share the step path spellings that leave the plan endpoint and those that stay.

The plan tests hold every step kind's validator to these lists, and the factory
tests send the accepted ones through the real rebase and join, so whether a
path is refused and where an accepted one lands are judged on the same inputs.
"""

import pytest

#: Every spelling of a step path that leaves the plan endpoint's origin. ``//``
#: and ``///x`` carry an empty authority, which ``urlparse`` reports as a falsy
#: ``netloc``, so only the explicit ``//`` prefix check rejects them. The
#: leading-whitespace spellings rely on ``urlparse`` stripping them first. The
#: dot-segment spellings only name another origin once a join resolves them: a
#: leading ``/..`` pops the root itself, leaving whatever follows to read as a
#: scheme.
OFF_ORIGIN_PATHS = [
    pytest.param("https://attacker.example/x", id="absolute_url"),
    pytest.param("//attacker.example/x", id="network_path_reference"),
    pytest.param("//", id="bare_authority_marker"),
    pytest.param("///x", id="empty_authority"),
    pytest.param("HTTPS://attacker.example/x", id="uppercase_scheme"),
    pytest.param("https:attacker.example/x", id="scheme_without_authority"),
    pytest.param("\t//attacker.example/x", id="leading_control_character"),
    pytest.param(" //attacker.example/x", id="leading_space"),
    pytest.param("attacker.example:443/x", id="host_and_port_without_scheme"),
    pytest.param("/.//attacker.example/x", id="current_segment_before_authority"),
    pytest.param("/a/..//attacker.example/x", id="parent_segment_before_authority"),
    pytest.param("a/../..//attacker.example/x", id="parent_segment_past_the_root"),
    pytest.param("/a/%2E%2e//attacker.example/x", id="percent_encoded_parent_segment"),
    pytest.param(" /.//attacker.example/x", id="leading_space_before_dot_segment"),
    pytest.param("/../http://attacker.example/x", id="scheme_behind_root_parent"),
    pytest.param("/../https://attacker.example/x", id="tls_scheme_behind_root_parent"),
    pytest.param(
        "/../../../http://attacker.example/x", id="scheme_behind_root_parents"
    ),
    pytest.param(
        "/%2e%2e/http://attacker.example/x", id="scheme_behind_encoded_root_parent"
    ),
    pytest.param("/a/../../http://attacker.example/x", id="scheme_behind_deep_parent"),
    pytest.param("/../attacker.example:443/x", id="host_and_port_behind_root_parent"),
    pytest.param("../http://attacker.example/x", id="scheme_behind_relative_parent"),
]

#: Step path spellings that stay on the plan endpoint's origin wherever the
#: endpoint's own path roots them.
SAME_ORIGIN_PATHS = [
    pytest.param("api/now/table/x", id="relative"),
    pytest.param("/api/now/table/x", id="rooted"),
    pytest.param("api/now/table/x:y", id="colon_after_first_segment"),
    pytest.param("./a:b", id="colon_in_first_segment_behind_current_segment"),
    pytest.param("api/./now/table/x", id="current_segment"),
    pytest.param("api/../api/now/table/x", id="parent_segment_within_the_path"),
    pytest.param("../api/now/table/x", id="parent_segment_past_the_root"),
    pytest.param("../../../api/now/table/x", id="parent_segments_past_the_root"),
    pytest.param("/../api/now/table/x", id="root_parent_before_a_plain_segment"),
    pytest.param("api/%2e%2e/api/now/table/x", id="percent_encoded_parent_segment"),
    pytest.param("api//now/table/x", id="empty_segment_after_the_first"),
    pytest.param("api/now/table/x?sysparm_query=a:b^c=d", id="query_with_colons"),
]
