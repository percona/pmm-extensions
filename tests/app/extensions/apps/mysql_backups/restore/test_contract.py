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

"""Run the derived-router contract suite against the migrated MySQL Restores app.

The shared :class:`DerivedRouterContractTests` exercises the body-independent
derived surface (schema, list, detail, 404, delete, execute, conflict,
status-filter, route presence/absence, auth) against the real
``mysql_backups.restore`` definition. The create/update methods are overridden
here with a hand-built body because the generic Polyfactory pass over the create
model trips the ``backup_source`` shell-safe validator. Restore declares no
``connectivity_check`` / ``detail_response_builder``, so the connectivity and
detail-model suite methods skip.
"""

from typing import Any

import yaml
from fastapi import status
from pytest_mock import MockerFixture

from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.apps.mysql_backups.forms import EncryptionFormat
from app.extensions.apps.mysql_backups.models import BackupType
from app.extensions.apps.mysql_backups.restore.app import app as restore_app
from app.extensions.apps.mysql_backups.restore.models import (
    RestoreCreate,
    S3Tool,
    SourceTransport,
)
from tests.app.extensions.apps.framework.contract_suite import (
    app_base_url,
    DerivedRouterContractTests,
)
from tests.app.extensions.apps.framework.kit import (
    SEEDED_TASK_NAME,
    SYNTH_CREATED_BY_NAME,
    SYNTH_EXECUTOR_HOST,
    SYNTH_SERVICE_HOST,
    SYNTH_SERVICE_PORT,
)
from tests.app.extensions.apps.mysql_backups.description_coverage import (
    assert_every_declared_field_is_described,
    assert_schema_serves_only_declared_descriptions,
    assert_schema_serves_only_declared_destructive_marks,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import legacy_default
from tests.app.extensions.unknown_body_field import (
    assert_unknown_field_rejected,
    UNKNOWN_FIELD,
)
from tests.app.factories import MOCK_CREATED_SERVICE_ID

_NEW_TASK_NAME = "contract-new-restore"
_UNKNOWN_TASK_NAME = "contract-unknown-restore"


def _valid_restore_body(
    *, task_name: str = _NEW_TASK_NAME, backup_type: BackupType = BackupType.MYDUMPER
) -> dict[str, Any]:
    """Return a valid restore create/update body resolving against the kit mocks.

    Pairs the seeded MySQL service / executor host with a shell-safe
    ``backup_source`` so the field validator passes. ``service_id`` is only gated
    on a Mydumper restore, and naming the seeded service satisfies that gate, so
    the same body is valid for every ``backup_type``.
    """
    return {
        "task_name": task_name,
        "hostname": SYNTH_EXECUTOR_HOST,
        "service_id": str(MOCK_CREATED_SERVICE_ID),
        "backup_type": backup_type.value,
        "backup_source": "/var/backups/latest",
        "datadir": "/var/lib/mysql",
    }


class TestRestoreContract(DerivedRouterContractTests):
    """Assert the restore app's derived HTTP surface, knob by knob."""

    app_def = restore_app

    def _valid_update_body(self, *, task_name: str) -> dict[str, Any] | None:
        """Return the gated valid PUT body; the generic Polyfactory body 422s here.

        :param task_name: The task name stamped into the body.
        :return: A valid restore update body resolving against the kit mocks.
        """
        return _valid_restore_body(task_name=task_name)

    def test_schema_id_is_labelled_for_its_meaning(self, contract_client: Any) -> None:
        """Serve the schema field under the database it targets, not the restore verb.

        The field selects which database to restore *into*; the old label read as
        the action itself and left operators guessing what to enter.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        fields = {
            field["name"]: field
            for form in response.json()["forms"]
            for field in form["fields"]
        }
        assert fields["schema_id"]["label"] == "Target database"

    def test_schema_capabilities(self, contract_client: Any) -> None:
        """Serve ``scheduling: false`` so every schedule control stays hidden.

        A scheduled restore would re-run a destructive restore of one fixed
        ``backup_source`` on every tick with nobody present to confirm the target.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.json()["capabilities"] == {
            "chaining": True,
            "alert_on_fail": True,
            "scheduling": False,
            "stats": False,
            "pii_anonymization": False,
        }

    def test_every_declared_field_is_described(self) -> None:
        """Require helper text on every field the restore form declares itself.

        A restore is configured under incident pressure, so a field an operator
        has to guess at is a field they get wrong.
        """
        assert_every_declared_field_is_described(RestoreCreate)

    def test_schema_serves_only_declared_descriptions(
        self, contract_client: Any
    ) -> None:
        """Serve each declared field's description verbatim, and only those.

        The inherited Task fields have to stay undescribed here, because
        describing them would move every other schema-driven app's schema too.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK, response.text
        assert_schema_serves_only_declared_descriptions(response.json(), RestoreCreate)

    def test_schema_marks_only_the_destructive_fields(
        self, contract_client: Any
    ) -> None:
        """Publish the consequence text on exactly the fields that destroy something.

        Pinning the whole marked set rather than one field is what makes an
        over-application visible: a confirmation operators learn to click
        through costs the qualifying fields the attention they need.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK, response.text
        assert_schema_serves_only_declared_destructive_marks(
            response.json(),
            RestoreCreate,
            {"overwrite_tables", "datadir", "restore_mycnf"},
        )

    def test_create_201(self, contract_client: Any, mock_task_api: Any) -> None:
        """Create a task via a real JSON POST with a valid body, returning 201.

        Restore declares no ``connectivity_check``, so the create response renders
        through the framework default builder; ``backup_type`` / ``hostname`` are
        stamped on the list/detail responses (see ``test_detail_stamps_extras``),
        not here.
        """
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_restore_body())

        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert mock_task_api.create_count == 1

    def test_detail_stamps_extras(self, contract_client: Any) -> None:
        """Assert the detail builder stamps ``backup_type`` / ``hostname`` / ``host`` / ``port``."""
        base = app_base_url(self.app_def)
        contract_client.post(f"{base}/", json=_valid_restore_body())

        response = contract_client.get(f"{base}/{_NEW_TASK_NAME}")

        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["backup_type"] == BackupType.MYDUMPER.value
        assert body["hostname"] == SYNTH_EXECUTOR_HOST
        assert body["host"] == SYNTH_SERVICE_HOST
        assert body["port"] == SYNTH_SERVICE_PORT

    def test_create_422(self, contract_client: Any, mock_task_api: Any) -> None:
        """Reject a body missing the required ``backup_type`` with 422, before any POST."""
        body = _valid_restore_body()
        del body["backup_type"]
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert mock_task_api.create_count == 0

    def test_create_threads_executor_host_to_meta_target(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Assert the submitted ``HostRef`` host threads through to ``meta.target``."""
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_restore_body())

        assert response.status_code == status.HTTP_201_CREATED, response.text
        meta = mock_task_api.last_create_payload["data"]["meta"]
        assert meta["target"] == SYNTH_EXECUTOR_HOST

    def test_create_rejects_an_unknown_field(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject a valid body plus an undeclared key with 422 naming it, before any POST."""
        body = _valid_restore_body()
        body[UNKNOWN_FIELD] = "value"
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=body)

        assert_unknown_field_rejected(response)
        assert mock_task_api.create_count == 0

    def test_update_rejects_an_unknown_field(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject a ``PUT`` carrying an undeclared key with 422 naming it, changing nothing."""
        body = _valid_restore_body(task_name=SEEDED_TASK_NAME)
        body[UNKNOWN_FIELD] = "value"
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert_unknown_field_rejected(response)
        assert mock_task_api.last_update_payload is None

    def test_update_200(self, contract_client: Any) -> None:
        """Update a task via a real ``PUT /{task_name}``, rebuilding the spec, returning 200."""
        base = app_base_url(self.app_def)

        response = contract_client.put(
            f"{base}/{SEEDED_TASK_NAME}",
            json=_valid_restore_body(task_name=SEEDED_TASK_NAME),
        )

        assert response.status_code == status.HTTP_200_OK, response.text

    def test_create_injects_extras(self, contract_client: Any) -> None:
        """Assert create resolves the creator and omits internal fields.

        Overrides the generic suite method: the Polyfactory body trips the create
        model's per-``backup_type`` rules, so the hand-built body is used.
        """
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_restore_body())

        assert response.status_code == status.HTTP_201_CREATED, response.text
        payload = response.json()
        assert "service_type" not in payload
        assert "owner" not in payload
        assert payload["created_by"] == SYNTH_CREATED_BY_NAME

    def test_update_derived_injects_extras(self, contract_client: Any) -> None:
        """Assert the derived PUT resolves the creator and omits internal fields.

        Overrides the generic suite method for the same body reason as
        :meth:`test_create_injects_extras`.
        """
        base = app_base_url(self.app_def)

        response = contract_client.put(
            f"{base}/{SEEDED_TASK_NAME}",
            json=_valid_restore_body(task_name=SEEDED_TASK_NAME),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        payload = response.json()
        assert "service_type" not in payload
        assert "owner" not in payload
        assert payload["created_by"] == SYNTH_CREATED_BY_NAME

    def test_update_404(self, contract_client: Any) -> None:
        """``PUT /{task_name}`` 404s for an unknown task name."""
        base = app_base_url(self.app_def)

        response = contract_client.put(
            f"{base}/{_UNKNOWN_TASK_NAME}",
            json=_valid_restore_body(task_name=_UNKNOWN_TASK_NAME),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_stamps_form_input(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Persist the validated restore create body under ``data['_form']``.

        Overrides the generic suite method: the str-typed ``service_id`` /
        ``schema_id`` reject the kit's int reference ids, so a hand-built body is
        used in place of the Polyfactory pass.
        """
        base = app_base_url(self.app_def)
        body = _valid_restore_body()

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        expected = self.app_def.create_model.model_validate(body).model_dump(
            mode="json"
        )
        assert mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY] == expected

    def test_schema_pins_section_collapse_posture(self, contract_client: Any) -> None:
        """Pin every restore-form section's collapse posture and the visible fields.

        Under incident pressure the form must be completable from what is on
        screen: ``Task`` stays expanded and now carries the required
        ``backup_source`` together with the ``service_id`` it depends on, while
        every expert section is collapsible *and* collapsed. Section order is
        pinned too, since it derives from field first-appearance on the model —
        ``General`` sits after the mode sections so the expert block reads last.
        ``advanced`` is pinned as ``False`` throughout: this form has one
        candidate section, and putting it alone behind the reveal control would
        add a click rather than save a row.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK, response.text
        sections = response.json()["forms"]
        assert [
            (
                section["title"],
                section["collapsible"],
                section["collapsed_by_default"],
            )
            for section in sections
        ] == [
            ("Task", False, False),
            ("Mydumper", True, True),
            ("XtraBackup", True, True),
            ("Binlog", True, True),
            ("General", True, True),
        ]
        assert [section["advanced"] for section in sections] == [False] * len(sections)
        task_fields = [field["name"] for field in sections[0]["fields"]]
        assert "service_id" in task_fields
        assert "backup_source" in task_fields

    def test_update_round_trips_stored_form(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Assert the stored restore ``_form`` re-validates and re-stamps on PUT."""
        base = app_base_url(self.app_def)
        task_name = "contract-roundtrip-restore"
        create = contract_client.post(
            f"{base}/", json=_valid_restore_body(task_name=task_name)
        )
        assert create.status_code == status.HTTP_201_CREATED, create.text
        stored_form = mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY]

        response = contract_client.put(f"{base}/{task_name}", json=stored_form)

        assert response.status_code == status.HTTP_200_OK, response.text
        assert (
            mock_task_api.last_update_payload["data"][RESERVED_FORM_KEY] == stored_form
        )

    def test_create_local_restore_stamps_no_transport_values(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Stamp a local restore without the SSH and object-store values it never uses.

        The three fields used to submit ``percona`` / ``22`` / ``s3cmd`` on every
        restore; declaring the source is what lets them stay out of the stamp.
        """
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        body["source_transport"] = SourceTransport.LOCAL.value

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        stamped = mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY]
        assert stamped["ssh_user"] is None
        assert stamped["ssh_port"] is None
        assert stamped["s3_tool"] is None

    def test_create_accepts_ssh_credentials_under_an_ssh_source(
        self, contract_client: Any
    ) -> None:
        """Accept the SSH trio when the declared source is reached over SSH."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        body.update(
            source_transport=SourceTransport.SSH.value,
            ssh_user="deploy",
            ssh_port=2222,
            ssh_key="prod-key",
        )

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_create_rejects_s3_tool_under_a_gcs_source(
        self, contract_client: Any
    ) -> None:
        """Reject ``s3_tool`` for a GCS source, whose download never consults it.

        A ``gs://`` source is fetched by ``gs_copy``'s ``gcloud storage rsync``,
        and the one path that does read ``s3_tool`` on a GCS restore selects
        ``aws s3 ls`` for a ``gs://`` URL, which cannot list the source.
        """
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        body.update(
            backup_source="gs://bucket/backups/latest",
            source_transport=SourceTransport.GCS.value,
            s3_tool=S3Tool.AWSCLI.value,
        )

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, (
            response.text
        )

    def test_create_accepts_a_cleared_s3_tool_select(
        self, contract_client: Any
    ) -> None:
        """Accept an emptied ``s3_tool`` select, which submits ``""`` rather than a value."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        body.update(
            backup_source="s3://bucket/backups/latest",
            source_transport=SourceTransport.S3.value,
            s3_tool="",
        )

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_create_422_on_ssh_credentials_under_a_local_source(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject SSH credentials a local source cannot consume, before any POST."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        body.update(source_transport=SourceTransport.LOCAL.value, ssh_user="deploy")

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert mock_task_api.create_count == 0

    def test_create_422_on_gpg_password_file_without_gpg(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject a GPG password file on a restore declaring no GPG pass."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        body.update(
            source_encryption=EncryptionFormat.NONE.value,
            gpg_password_file="/etc/gpg.pass",
        )

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert mock_task_api.create_count == 0

    def test_update_round_trips_a_stamp_predating_the_source_controls(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Accept an edit of a restore stamped before the source controls existed.

        A stored stamp is a full model dump, so every pre-existing one carries
        ``percona`` / ``22`` / ``s3cmd`` and the derived ``PUT`` re-submits it
        verbatim. Editing such a restore must not 422 while waiting for the
        manual backfill command to run.
        """
        base = app_base_url(self.app_def)
        task_name = "contract-legacy-restore"
        contract_client.post(f"{base}/", json=_valid_restore_body(task_name=task_name))
        legacy_form = {
            **mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY],
            **{
                name: legacy_default(name)
                for name in ("ssh_user", "ssh_port", "s3_tool")
            },
        }
        del legacy_form["source_transport"]
        del legacy_form["source_encryption"]

        response = contract_client.put(f"{base}/{task_name}", json=legacy_form)

        assert response.status_code == status.HTTP_200_OK, response.text
        restamped = mock_task_api.last_update_payload["data"][RESERVED_FORM_KEY]
        assert restamped["source_transport"] == SourceTransport.LOCAL.value
        assert restamped["ssh_user"] is None

    def test_schema_leads_the_task_section_with_the_destination_service(
        self, contract_client: Any
    ) -> None:
        """Serve ``service_id`` ahead of ``backup_type``, gated required for Mydumper.

        The field lists Backup Source, fills the Mydumper target database and is
        where a Mydumper restore loads, so it leads the backup-specific fields —
        after the two identity fields every task form inherits.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK, response.text
        sections = response.json()["forms"]
        task_fields = [field["name"] for field in sections[0]["fields"]]
        assert task_fields[:4] == [
            "task_name",
            "hostname",
            "service_id",
            "backup_type",
        ]
        fields = {field["name"]: field for form in sections for field in form["fields"]}
        service_id = fields["service_id"]
        assert service_id["required"] is False
        assert service_id["requires"] == [
            {
                "when": {"equals": {"backup_type": BackupType.MYDUMPER.value}},
                "message": (
                    "Destination Database Service is required for a Mydumper restore."
                ),
            }
        ]
        assert fields["backup_source"]["depends_on"] == "service_id"
        assert fields["schema_id"]["depends_on"] == "service_id"

    def test_create_422_on_a_mydumper_restore_without_a_destination_service(
        self,
        contract_client: Any,
        mock_task_api: Any,
        mock_inventory_api: Any,
        mocker: MockerFixture,
    ) -> None:
        """Reject a service-less Mydumper restore at body validation, before any lookup.

        The resolver refuses the same body with a 422 naming the field too, so the
        assertions pin the gate's own message rather than the field name alone.
        """
        lookup = mocker.spy(mock_inventory_api, "get")
        base = app_base_url(self.app_def)
        body = _valid_restore_body()
        del body["service_id"]

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert (
            "Destination Database Service is required for a Mydumper restore"
            in response.text
        )
        assert mock_task_api.create_count == 0
        assert lookup.await_count == 0

    def test_create_201_for_a_non_mydumper_restore_without_a_destination_service(
        self, contract_client: Any
    ) -> None:
        """Accept an XtraBackup restore that records no destination service."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body(backup_type=BackupType.XTRABACKUP)
        del body["service_id"]

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_a_stored_mydumper_stamp_without_a_service_still_reads(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """List and serve a Mydumper restore stamped before the service was required.

        Neither read path re-validates the stamp into a hard failure — the detail
        builder's source-declaring override returns nothing on a validation error
        — so such a restore keeps its row, its detail and the edit form seeded
        from it. Saving it unchanged is what the gate rejects, by name.
        """
        task_name = "contract-serviceless-mydumper"
        stored_form = {
            **_valid_restore_body(task_name=task_name),
            "service_id": None,
        }
        mock_task_api.seed_task(
            task_name,
            owner=self.app_def.owner,
            data_extra={RESERVED_FORM_KEY: stored_form},
        )
        base = app_base_url(self.app_def)

        listing = contract_client.get(f"{base}/")
        detail = contract_client.get(f"{base}/{task_name}")
        resubmit = contract_client.put(f"{base}/{task_name}", json=stored_form)

        assert listing.status_code == status.HTTP_200_OK, listing.text
        assert task_name in {row["name"] for row in listing.json()["items"]}
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["data"][RESERVED_FORM_KEY] == stored_form
        assert resubmit.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert (
            "Destination Database Service is required for a Mydumper restore"
            in resubmit.text
        )

    def test_schema_gates_transport_and_decryption_fields(
        self, contract_client: Any
    ) -> None:
        """Serve the source controls ungated and every field they govern gated.

        The gates use only ``equals`` / ``any`` / ``not``, which the renderer
        already evaluates, so no new predicate reaches a consumer.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK, response.text
        sections = response.json()["forms"]
        fields = {field["name"]: field for form in sections for field in form["fields"]}
        task_fields = [field["name"] for field in sections[0]["fields"]]
        assert "source_transport" in task_fields
        assert "source_encryption" in task_fields
        assert "forbidden" not in fields["source_transport"]
        assert "forbidden" not in fields["source_encryption"]
        for name in ("ssh_user", "ssh_port", "ssh_key"):
            assert fields[name]["forbidden"] == [
                {"when": {"not_equals": {"source_transport": "ssh"}}}
            ], name
        assert fields["s3_tool"]["forbidden"] == [
            {"when": {"not_equals": {"source_transport": "s3"}}}
        ]
        assert fields["gpg_password_file"]["forbidden"] == [
            {
                "when": {
                    "not": {
                        "any": [
                            {"equals": {"source_encryption": "gpg"}},
                            {"equals": {"source_encryption": "dual"}},
                        ]
                    }
                }
            }
        ]
        assert fields["xtrabackup_aes256_keyfile"]["forbidden"] == [
            {
                "when": {
                    "not": {
                        "any": [
                            {"equals": {"source_encryption": "aes256"}},
                            {"equals": {"source_encryption": "dual"}},
                        ]
                    }
                },
                "message": (
                    "'xtrabackup_aes256_keyfile' must not be set when "
                    "'source_encryption' does not include AES-256."
                ),
            }
        ]
        assert fields["xtrabackup_aes256_keyfile"]["requires"] == [
            {
                "when": {
                    "any": [
                        {"equals": {"source_encryption": "aes256"}},
                        {"equals": {"source_encryption": "dual"}},
                    ]
                },
                "message": (
                    "'xtrabackup_aes256_keyfile' is required when "
                    "'source_encryption' includes AES-256."
                ),
            }
        ]

    def test_create_422_on_a_key_file_no_declared_format_admits(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject a key file on a restore that declares no AES-256 encryption."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body(backup_type=BackupType.XTRABACKUP)
        body.update(
            source_encryption=EncryptionFormat.NONE.value,
            xtrabackup_aes256_keyfile="/etc/xb/aes.key",
        )

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "'xtrabackup_aes256_keyfile' must not be set" in response.text
        assert mock_task_api.create_count == 0

    def test_create_201_for_a_declared_aes_restore(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Accept a restore that declares AES-256 and names its key file."""
        base = app_base_url(self.app_def)
        body = _valid_restore_body(backup_type=BackupType.MYDUMPER)
        body.update(
            source_encryption=EncryptionFormat.AES256.value,
            xtrabackup_aes256_keyfile="/etc/xb/aes.key",
        )

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        config = yaml.safe_load(
            mock_task_api.last_create_payload["data"]["meta"]["config"]
        )
        assert (
            config["SERVER_LIST"][0]["XTRABACKUP_AES256_KEYFILE"] == "/etc/xb/aes.key"
        )

    def test_detail_reveals_a_key_file_its_stamp_hid(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Serve a stamp that names no AES format beside its key file with one declared.

        The edit form drops a field its gates hide, so serving such a stamp
        unrepaired would strip the key the restore decrypts with on save.
        """
        task_name = "contract-hidden-aes-keyfile"
        stored_form = {
            **_valid_restore_body(task_name=task_name, backup_type=BackupType.MYDUMPER),
            "source_transport": SourceTransport.LOCAL.value,
            "source_encryption": EncryptionFormat.NONE.value,
            "xtrabackup_aes256_keyfile": "/etc/xb/aes.key",
        }
        mock_task_api.seed_task(
            task_name,
            owner=self.app_def.owner,
            data_extra={RESERVED_FORM_KEY: stored_form},
        )
        base = app_base_url(self.app_def)

        detail = contract_client.get(f"{base}/{task_name}")

        assert detail.status_code == status.HTTP_200_OK, detail.text
        served = detail.json()["data"][RESERVED_FORM_KEY]
        assert served["source_encryption"] == EncryptionFormat.AES256.value
        assert served["xtrabackup_aes256_keyfile"] == "/etc/xb/aes.key"

        resubmit = contract_client.put(f"{base}/{task_name}", json=served)

        assert resubmit.status_code == status.HTTP_200_OK, resubmit.text
