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

"""Share the check that a task create or update call refuses an undeclared body key.

Every task create and update request model rejects a key it does not declare,
so a client that sends a setting under a wrong or retired name learns it was not
applied. Each app's route tests send a valid body plus :data:`UNKNOWN_FIELD` and
assert the answer with :func:`assert_unknown_field_rejected`.

The UI's create and edit pages send only the names :func:`submitted_field_names`
returns, so a request model that declares every one of them is safe to make
strict.
"""

from typing import Any

import pytest
from fastapi import status
from httpx import Response

UNKNOWN_FIELD = "retired_setting"


def assert_unknown_field_rejected(
    response: Response, field: str = UNKNOWN_FIELD
) -> None:
    """Fail unless ``response`` is a ``422`` whose detail names ``field`` as unknown.

    :param response: The route's response.
    :param field: The undeclared body key the request carried.
    """
    if response.status_code != status.HTTP_422_UNPROCESSABLE_ENTITY:
        pytest.fail(f"expected 422, got {response.status_code}: {response.text}")
    errors = response.json()["detail"]
    if not any(
        error["type"] == "extra_forbidden" and error["loc"][-1] == field
        for error in errors
    ):
        pytest.fail(f"no extra_forbidden error names {field!r}: {errors}")


def submitted_field_names(schema: dict[str, Any]) -> set[str]:
    """Return the top-level body keys the UI's create and edit pages can send.

    The pages build the body from the published form: every field, including
    each branch of a one-of group, and each one-of discriminator (a dotted name
    nests under its first segment), plus ``task_name``, and ``alert_on_fail`` when
    the app has that capability.

    :param schema: The app's published schema, as its schema route serves it.
    :return: The top-level key names.
    """
    names = {"task_name"}
    for section in schema.get("forms") or []:
        for field in section["fields"]:
            if field.get("type") == "one_of":
                names.add(field["discriminator"])
                leaves = [
                    leaf for branch in field["branches"] for leaf in branch["fields"]
                ]
            else:
                leaves = [field]
            names.update(leaf["name"] for leaf in leaves)
    if (schema.get("capabilities") or {}).get("alert_on_fail"):
        names.add("alert_on_fail")
    return {name.split(".", 1)[0] for name in names}


def edit_page_body(
    stored_form: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """Return the body the edit page submits when it opens on ``stored_form``.

    :param stored_form: The task's stored ``data['_form']``.
    :param schema: The app's published schema, as its schema route serves it.
    :return: The stored values of the keys the page sends.
    """
    names = submitted_field_names(schema)
    return {key: value for key, value in stored_form.items() if key in names}
