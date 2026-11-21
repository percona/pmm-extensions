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

"""Assert ``Ui`` text coverage over the MySQL Backups create and restore forms.

Covers the two operator-facing texts a form declares: the helper text every
declared field owes, and the consequence text only the destructive ones carry.
Shared by the two apps' contract tests, which make the same promise about two
different forms. The checks stay app-local on purpose: the other schema-driven
apps are only partly described, so promoting them to a registry-wide
conformance rule would fail them.
"""

import inspect
import re
from collections import Counter
from typing import Any

from app.extensions.apps.framework.form_dsl import TaskFormModel, Ui

_RST_INLINE_CODE = "``"
# The house voice never names a flag in helper text: the form shows a labelled
# control, not the command line it becomes, so a flag is only ever readable to
# someone who already knows the tool. Matching anywhere rather than at the start
# catches "Passes --safe-slave-backup so ...", which leads with the flag in every
# sense except the literal first character.
_CLI_FLAG = re.compile(r"(?<![\w-])--[A-Za-z]")
_SHARED_BASE_CHAIN = frozenset(TaskFormModel.__mro__)


def _declared_names(create_model: type[TaskFormModel]) -> set[str]:
    """Return the field names the model declares itself.

    Subtracts :class:`TaskFormModel`'s own fields rather than naming the
    inherited ones, so a field added to the shared base auto-exempts and a field
    added to ``create_model`` is required to describe itself. A field re-declared
    under an inherited name is then added back: re-declaring it is how a model
    takes over its presentation, which makes it the model's to describe. The
    annotations are read off every class the model adds above the shared base, so
    splitting fields across mixins does not exempt them either.

    :param create_model: The form model to inspect.
    :return: The locally declared field names.
    """
    served = set(create_model.model_fields)
    re_declared = {
        name
        for klass in create_model.__mro__
        if klass not in _SHARED_BASE_CHAIN
        for name in inspect.get_annotations(klass)
    }
    return (served - set(TaskFormModel.model_fields)) | (re_declared & served)


def _ui_marker(create_model: type[TaskFormModel], name: str) -> Ui | None:
    """Return a field's ``Ui`` marker, or ``None`` when it carries none.

    :param create_model: The form model to inspect.
    :param name: The field to read the marker off.
    :return: The field's ``Ui`` marker when it has one.
    """
    return next(
        (
            entry
            for entry in create_model.model_fields[name].metadata
            if isinstance(entry, Ui)
        ),
        None,
    )


def _marker_descriptions(create_model: type[TaskFormModel]) -> dict[str, str]:
    """Return each declared field's ``Ui`` description text, empty when absent.

    :param create_model: The form model to inspect.
    :return: Declared field name to its description, ``""`` when the field
        carries no ``Ui`` marker or an unset description.
    """
    descriptions: dict[str, str] = {}
    for name in _declared_names(create_model):
        marker = _ui_marker(create_model, name)
        descriptions[name] = (marker.description if marker else None) or ""
    return descriptions


def _marker_destructive_marks(create_model: type[TaskFormModel]) -> dict[str, str]:
    """Return the ``Ui`` consequence text of each declared field that carries one.

    Unmarked fields are left out rather than mapped to ``""``: presence is the
    mark, so an empty entry would read as a field marking itself with nothing to
    display.

    :param create_model: The form model to inspect.
    :return: Declared field name to its consequence text, marked fields only.
    """
    marks: dict[str, str] = {}
    for name in _declared_names(create_model):
        marker = _ui_marker(create_model, name)
        if marker and marker.destructive:
            marks[name] = marker.destructive
    return marks


def _served_fields(schema_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every field entry in the payload, flattened across its forms.

    :param schema_payload: The decoded ``GET /schema`` response body.
    :return: The field entries, in served order.
    """
    return [field for form in schema_payload["forms"] for field in form["fields"]]


def assert_every_declared_field_is_described(
    create_model: type[TaskFormModel],
) -> None:
    """Assert each locally declared field carries usable helper text.

    Walks the field metadata, so the failure output names the fields still to
    write and a field added later cannot ship undescribed.

    :param create_model: The form model to inspect.
    :raises AssertionError: When the model declares no fields of its own, when a
        declared field has no description, when the text is blank once stripped,
        when it names a CLI flag the form never shows, or when it carries rST
        inline-code markup the renderer emits verbatim.
    """
    descriptions = _marker_descriptions(create_model)
    assert descriptions, (
        f"{create_model.__name__} declares no fields of its own, so every check "
        "below would pass without inspecting anything"
    )

    missing: set[str] = set()
    flagged: set[str] = set()
    marked_up: set[str] = set()
    for name, text in descriptions.items():
        if not text.strip():
            missing.add(name)
        if _CLI_FLAG.search(text):
            flagged.add(name)
        if _RST_INLINE_CODE in text:
            marked_up.add(name)

    assert not missing, f"fields with no description: {sorted(missing)}"
    assert not flagged, f"descriptions naming a CLI flag: {sorted(flagged)}"
    assert not marked_up, f"descriptions carrying rST markup: {sorted(marked_up)}"


def assert_schema_serves_only_declared_descriptions(
    schema_payload: dict[str, Any], create_model: type[TaskFormModel]
) -> None:
    """Assert the schema serves the declared descriptions and only those.

    Guards the endpoint half of the promise: a description that never reaches the
    wire is invisible to the operator, and the inherited Task fields must stay
    undescribed here because describing them would move all the other apps'
    schemas too.

    :param schema_payload: The decoded ``GET /schema`` response body.
    :param create_model: The form model the payload derives from.
    :raises AssertionError: When one field name is served more than once, so a
        name-keyed comparison would silently read only the last entry; when the
        model declares no fields of its own; when a declared field is absent from
        the schema; when its served text differs from its marker text; or when an
        inherited field the model did not re-declare gained a description.
    """
    entries = [
        (field["name"], field.get("description") or "")
        for field in _served_fields(schema_payload)
    ]
    repeated = sorted(
        name
        for name, count in Counter(name for name, _ in entries).items()
        if count > 1
    )
    assert not repeated, f"fields served more than once: {repeated}"

    served = dict(entries)
    declared = _marker_descriptions(create_model)
    assert declared, (
        f"{create_model.__name__} declares no fields of its own, so every check "
        "below would pass without inspecting anything"
    )

    absent = set(declared) - set(served)
    assert not absent, f"declared but absent from the schema: {sorted(absent)}"

    dropped = {name for name, text in declared.items() if served[name] != text}
    assert not dropped, f"marker text not served on the wire: {sorted(dropped)}"

    inherited = {
        name
        for name in (set(served) & set(TaskFormModel.model_fields)) - set(declared)
        if served[name]
    }
    assert not inherited, f"inherited fields described here: {sorted(inherited)}"


def assert_schema_serves_only_declared_destructive_marks(
    schema_payload: dict[str, Any],
    create_model: type[TaskFormModel],
    expected_marked: set[str],
) -> None:
    """Assert the schema marks the model's destructive fields, and only those.

    Reads the consequence text off the model instead of restating it, so the
    assertion cannot drift from what the form declares; ``expected_marked``
    carries the names alone, which is the part a reviewer has to agree with. A
    mark added to a fourth field therefore fails here until someone names it:
    a confirmation operators learn to click through costs the fields that do
    qualify the attention they need.

    Marked fields are picked out by truthiness rather than key presence, which
    is the check a consumer makes — a route serving without
    ``response_model_exclude_none`` publishes ``destructive: null`` on every
    unmarked field.

    :param schema_payload: The decoded ``GET /schema`` response body.
    :param create_model: The form model the payload derives from.
    :param expected_marked: The field names expected to carry a mark.
    :raises AssertionError: When the model's marked set differs from
        ``expected_marked``, or when a mark reaches the wire altered or not at
        all.
    """
    declared = _marker_destructive_marks(create_model)
    drifted = sorted(set(declared) ^ expected_marked)
    assert not drifted, f"marked fields no longer as agreed: {drifted}"

    served = {
        field["name"]: field["destructive"]
        for field in _served_fields(schema_payload)
        if field.get("destructive")
    }
    mismatched = sorted(
        name
        for name in set(served) | set(declared)
        if served.get(name) != declared.get(name)
    )
    assert not mismatched, f"marks not served as declared: {mismatched}"
