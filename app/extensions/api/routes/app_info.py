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

"""Define the ``/api/extensions/app-info`` JSON endpoint for shell metadata.

Expose the rendered sidebar footer text that the React shell's sidebar
displays. The value comes from the shared
:func:`app.extensions.deps.render_footer_text` helper, which reads the live
``FOOTER_TEMPLATE`` hot setting per request so an ``EXTENSIONS__FOOTER_TEMPLATE``
override applies without restarting the application.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.extensions.deps import render_footer_text

router = APIRouter()


class AppInfo(BaseModel):
    """Represent the response of ``GET /api/extensions/app-info``.

    :param footer_text: The rendered sidebar footer text (application summary
        and version by default).
    :param version: The running PMM Extensions version, sourced from
        ``app.__version__``. Independent of ``FOOTER_TEMPLATE``.
    """

    footer_text: str
    version: str


@router.get("/")
async def get_app_info() -> AppInfo:
    """Return shell metadata for the React frontend.

    Render ``footer_text`` from the shared :func:`render_footer_text` helper.
    The helper reads the hot ``FOOTER_TEMPLATE`` setting per request, so a live
    ``EXTENSIONS__FOOTER_TEMPLATE`` override is reflected without a restart.
    Access is gated by the router-level ``IsApiAuthenticated`` dependency.

    :return: The rendered footer text and the running version.
    """
    return AppInfo(footer_text=render_footer_text(), version=__version__)
