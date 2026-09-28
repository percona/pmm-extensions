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

"""Test the one-of ``ArchivesCreate`` model's validation surface."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.utils.fields import TCP_PORT_MAX, TCP_PORT_MIN
from app.extensions.apps.archives.constants import SwapDropEnum
from app.extensions.apps.archives.models import (
    ArchivesCreate,
    DestByTable,
    HostManual,
    SourceByQuery,
    SourceByTable,
)
from app.extensions.apps.archives.views import archives_views
from app.extensions.apps.framework.form_dsl.derivation import derive_form_sections
from app.extensions.apps.framework.schema import FormSection, OneOfGroup

_MANUAL_PORT = 3307
_SCHEMA_ID = 5
_TABLE_ID = 6
_LIMIT_DEFAULT = 1000
_SLEEP_DEFAULT = 1


def _valid(**overrides: Any) -> dict[str, Any]:
    """Return a valid table->table create body, with overrides merged in."""
    body: dict[str, Any] = {
        "task_name": "arch",
        "hostname": "exec-host",
        "service_id": 1,
        "swap_drop": SwapDropEnum.PURGE_ONLY.value,
        "source": {"mode": "table", "source_db": "src_db", "source_table": "src_tbl"},
        "destination": {"mode": "table", "dest_table": "dst_tbl"},
        "where": "id < 100",
    }
    body.update(overrides)
    return body


class TestArchivesCreateBranches:
    """Cover the source / destination / host discriminated-union branches."""

    def test_valid_table_to_table(self) -> None:
        """Accept a table source with a table destination."""
        form = ArchivesCreate.model_validate(_valid())
        assert isinstance(form.source, SourceByTable)
        assert isinstance(form.destination, DestByTable)
        assert form.host is None

    def test_source_query_branch(self) -> None:
        """Accept a query source branch."""
        form = ArchivesCreate.model_validate(
            _valid(source={"mode": "query", "source_query": "SELECT 1"})
        )
        assert isinstance(form.source, SourceByQuery)
        assert form.source.source_query == "SELECT 1"

    def test_host_manual_branch(self) -> None:
        """Accept a manual destination-host branch with a port."""
        form = ArchivesCreate.model_validate(
            _valid(
                host={
                    "mode": "manual",
                    "dest_host": "remote",
                    "dest_port": _MANUAL_PORT,
                }
            )
        )
        assert isinstance(form.host, HostManual)
        assert form.host.dest_port == _MANUAL_PORT

    def test_free_solo_accepts_int_and_str(self) -> None:
        """Accept both an inventory id (int) and a free-typed name (str)."""
        by_id = ArchivesCreate.model_validate(
            _valid(
                source={
                    "mode": "table",
                    "source_db": _SCHEMA_ID,
                    "source_table": _TABLE_ID,
                }
            )
        )
        assert by_id.source.source_db == _SCHEMA_ID
        by_name = ArchivesCreate.model_validate(_valid())
        assert by_name.source.source_db == "src_db"

    def test_free_solo_rejects_empty_string(self) -> None:
        """Reject an empty free-typed reference on the source and manual host."""
        with pytest.raises(ValidationError):
            ArchivesCreate.model_validate(
                _valid(source={"mode": "table", "source_db": "", "source_table": "t"})
            )
        with pytest.raises(ValidationError):
            ArchivesCreate.model_validate(
                _valid(host={"mode": "manual", "dest_host": ""})
            )


class TestArchivesCreateRules:
    """Cover the conditional rules ported onto the one-of model."""

    def test_swap_drop_non_purge_rejected(self) -> None:
        """Reject any archive type other than Purge Only."""
        with pytest.raises(ValidationError, match="Purge Only"):
            ArchivesCreate.model_validate(
                _valid(swap_drop=SwapDropEnum.SWAP_DROP.value)
            )

    def test_destination_required_without_delete_data(self) -> None:
        """Reject an archiving run with no destination."""
        body = _valid()
        del body["destination"]
        with pytest.raises(ValidationError, match="destination"):
            ArchivesCreate.model_validate(body)

    def test_delete_data_forbids_destination(self) -> None:
        """Reject a destination when deleting without archiving."""
        with pytest.raises(ValidationError, match="destination cannot be set"):
            ArchivesCreate.model_validate(_valid(delete_data=True))

    def test_delete_data_allows_no_destination(self) -> None:
        """Accept a delete-only run with no destination."""
        body = _valid(delete_data=True)
        del body["destination"]
        form = ArchivesCreate.model_validate(body)
        assert form.destination is None
        assert form.delete_data is True

    def test_explicit_false_delete_data_still_requires_destination(self) -> None:
        """Reject an archiving run whose delete flag is submitted as ``False``."""
        body = _valid(delete_data=False)
        del body["destination"]
        with pytest.raises(ValidationError, match="destination"):
            ArchivesCreate.model_validate(body)

    def test_delete_data_allows_a_destination_host(self) -> None:
        """Accept a delete-only run that still carries a destination host.

        The form gates the host group out alongside the destination, but the
        contract stays permissive so cloning a legacy task that stored a host
        beside the delete flag keeps working.
        """
        body = _valid(delete_data=True, host={"mode": "manual", "dest_host": "remote"})
        del body["destination"]
        form = ArchivesCreate.model_validate(body)
        assert isinstance(form.host, HostManual)
        assert form.destination is None

    def test_where_required_for_purge(self) -> None:
        """Require a WHERE clause for a Purge Only run."""
        body = _valid()
        del body["where"]
        with pytest.raises(ValidationError):
            ArchivesCreate.model_validate(body)


class TestArchivesCreateDsnDelimiters:
    """Cover the DSN-delimiter validators on the manual host and destination db."""

    @pytest.mark.parametrize("bad", ["a,b", "a=b"])
    def test_dest_host_rejects_delimiters(self, bad: str) -> None:
        """Reject DSN delimiters in a manual destination host."""
        with pytest.raises(ValidationError, match="DSN delimiters"):
            ArchivesCreate.model_validate(
                _valid(host={"mode": "manual", "dest_host": bad})
            )

    @pytest.mark.parametrize("bad", ["a,b", "a=b"])
    def test_dest_db_rejects_delimiters(self, bad: str) -> None:
        """Reject DSN delimiters in a free-typed destination schema name."""
        with pytest.raises(ValidationError, match="DSN delimiters"):
            ArchivesCreate.model_validate(
                _valid(destination={"mode": "table", "dest_table": "t", "dest_db": bad})
            )

    @pytest.mark.parametrize("port", [0, 70000])
    def test_dest_port_range(self, port: int) -> None:
        """Reject an out-of-range manual destination port."""
        with pytest.raises(ValidationError):
            ArchivesCreate.model_validate(
                _valid(
                    host={"mode": "manual", "dest_host": "remote", "dest_port": port}
                )
            )


def _derived_sections() -> list[FormSection]:
    """Return the create model's derived form sections."""
    return derive_form_sections(ArchivesCreate, archives_views.layout)


def _section(title: str) -> FormSection:
    """Return the derived form section titled ``title``."""
    return next(section for section in _derived_sections() if section.title == title)


def _one_of(title: str, name: str) -> OneOfGroup:
    """Return the one-of group named ``name`` from the section titled ``title``."""
    return next(
        field
        for field in _section(title).fields
        if isinstance(field, OneOfGroup) and field.name == name
    )


def _field_by_name(name: str) -> Any:
    """Return the derived schema field with ``name`` from the Advanced section."""
    advanced = _section("Advanced")
    return next(field for field in advanced.fields if field.name == name)


class TestArchivesCreateDefaults:
    """Cover the limit / sleep purge-script form-display defaults."""

    def test_limit_schema_default_is_purge_default(self) -> None:
        """Derive ``limit`` with the purge-script form default (1000)."""
        assert _field_by_name("limit").default == _LIMIT_DEFAULT

    def test_sleep_schema_default_is_purge_default(self) -> None:
        """Derive ``sleep`` with the purge-script form default (1)."""
        assert _field_by_name("sleep").default == _SLEEP_DEFAULT

    def test_model_runtime_defaults_unchanged(self) -> None:
        """Keep the model/runtime defaults ``None`` so the wire contract is intact."""
        assert ArchivesCreate.model_fields["limit"].default is None
        assert ArchivesCreate.model_fields["sleep"].default is None

    def test_omitted_limit_sleep_resolve_to_none(self) -> None:
        """Leave limit / sleep ``None`` when omitted, so the payload fallback runs."""
        form = ArchivesCreate.model_validate(_valid())
        assert form.limit is None
        assert form.sleep is None


def _dest_port_schema_field() -> Any:
    """Return the derived ``dest_port`` field from the manual destination-host branch."""
    host = _one_of("Destination Host", "host")
    manual = next(branch for branch in host.branches if branch.value == "manual")
    return next(field for field in manual.fields if field.name == "host.dest_port")


def test_dest_port_derived_schema_bounds_preserved() -> None:
    """Keep the derived ``dest_port`` bounds at 1-65535 via the shared ``TcpPort`` type.

    ``dest_port`` reuses ``TcpPort | None`` and carries its bounds inside the union
    member. The hardened form-DSL bounds scan descends into union members, so the
    1-65535 range surfaces in the derived schema instead of being silently dropped.
    """
    field = _dest_port_schema_field()
    assert field.ge == TCP_PORT_MIN
    assert field.le == TCP_PORT_MAX


_DELETE_ONLY_GATE = {"when": {"truthy": "delete_data"}}
_GATED_SECTIONS = {"Destination", "Destination Host"}


class TestArchivesDestinationGates:
    """Cover the section gates that make a delete-only run reachable from the form."""

    @pytest.mark.parametrize("title", sorted(_GATED_SECTIONS))
    def test_destination_sections_hide_on_delete_data(self, title: str) -> None:
        """Gate both destination groups out while deleting without archiving."""
        section = _section(title)
        assert [
            gate.model_dump(mode="json", exclude_none=True)
            for gate in section.forbidden or []
        ] == [_DELETE_ONLY_GATE]

    def test_no_other_section_is_gated(self) -> None:
        """Leave every other section unconditionally visible."""
        assert {
            section.title for section in _derived_sections() if section.forbidden
        } == _GATED_SECTIONS

    @pytest.mark.parametrize(
        ("title", "name", "expected"),
        [
            ("Destination", "destination", ("table", "file")),
            ("Destination Host", "host", ("service", "manual")),
        ],
    )
    def test_gated_groups_keep_their_branches(
        self, title: str, name: str, expected: tuple[str, ...]
    ) -> None:
        """Keep the gated groups' branch set and default untouched by the gate."""
        group = _one_of(title, name)
        assert tuple(branch.value for branch in group.branches) == expected
        assert group.default == expected[0]

    def test_source_group_stays_ungated(self) -> None:
        """Leave the required source group reachable on a delete-only run."""
        assert _section("Source").forbidden is None
