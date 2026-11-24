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

"""Run the derived-router contract suite against the migrated MySQL Backups app.

The shared :class:`DerivedRouterContractTests` exercises the body-independent
derived surface (schema, list, detail, 404, delete, execute, conflict,
status-filter, route presence/absence, auth) against the real ``mysql_backups``
definition. The create/update/connectivity methods are overridden here with
hand-built per-``backup_type`` bodies because ``mysql_backups`` is heavily gated:
``build_valid_create_body``'s Polyfactory pass over the create model trips the
per-mode ``FailRule``s and the upload-consistency validator, so the generic
body-dependent methods cannot synthesize a valid body. The two additive surfaces
the migration introduces — the ``connectivity_warning`` field and the new
``PUT /{task_name}`` create-mirror route — get explicit standalone assertions.
"""

from typing import Any
from unittest.mock import AsyncMock

import yaml
from fastapi import status
from pytest_mock import MockerFixture

from app.extensions.apps.framework import ConnectivityWarning
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.apps.mysql_backups.app import app as mysql_backups_app
from app.extensions.apps.mysql_backups.forms import (
    ALLOWED_COMPRESSIONS,
    ALLOWED_XTRABACKUP_BIN_COMPRESSIONS,
    BackupCreate,
    CompressionAlgorithm,
)
from app.extensions.apps.mysql_backups.models import BackupType
from app.extensions.connectivity import CONNECTIVITY_META_HOST_KEY
from tests.app.extensions.apps.framework.contract_suite import (
    app_base_url,
    build_contract_client,
    DerivedRouterContractTests,
)
from tests.app.extensions.apps.framework.kit import (
    MockInventoryAPI,
    MockTaskAPI,
    SEEDED_TASK_NAME,
    SYNTH_CREATED_BY_NAME,
    SYNTH_EXECUTOR_HOST,
)
from tests.app.extensions.apps.mysql_backups.description_coverage import (
    assert_every_declared_field_is_described,
    assert_schema_serves_only_declared_descriptions,
)
from tests.app.extensions.unknown_body_field import (
    assert_unknown_field_rejected,
    UNKNOWN_FIELD,
)
from tests.app.factories import MOCK_CREATED_SERVICE_ID

_NEW_TASK_NAME = "contract-new-backup"
_UNKNOWN_TASK_NAME = "contract-unknown-backup"
_CONNECTIVITY_PATCH_TARGET = (
    "app.extensions.apps.framework.connectivity.record_connectivity_warning"
)


def _valid_body(
    *, task_name: str = _NEW_TASK_NAME, backup_type: BackupType = BackupType.MYDUMPER
) -> dict[str, Any]:
    """Return a valid gated create/update body resolving against the kit mocks.

    Pairs the seeded MySQL service / executor host with an RSYNC upload and its
    destination so the upload-consistency validator and the per-mode gates pass.
    """
    return {
        "task_name": task_name,
        "hostname": SYNTH_EXECUTOR_HOST,
        "service_id": MOCK_CREATED_SERVICE_ID,
        "backup_type": backup_type.value,
        "backup_dir": "/backups",
        "upload": ["RSYNC"],
        "rsync_path": "/data/rsync",
    }


class TestMysqlBackupsContract(DerivedRouterContractTests):
    """Assert the mysql_backups app's derived HTTP surface, knob by knob.

    The two create/update injected-extras methods are overridden here to feed the
    gated per-``backup_type`` body, since the generic Polyfactory body trips the
    create model's gates.
    """

    app_def = mysql_backups_app

    def test_create_injects_extras(self, contract_client: Any) -> None:
        """Assert create binds the context provider and omits internal fields.

        Overrides the generic suite method: the per-mode ``FailRule``s and
        upload-consistency validator defeat the Polyfactory body, so a hand-built
        gated body is used.
        """
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_body())

        assert response.status_code == status.HTTP_201_CREATED, response.text
        payload = response.json()
        assert "service_type" not in payload
        assert "owner" not in payload
        assert payload["created_by"] == SYNTH_CREATED_BY_NAME

    def test_update_derived_injects_extras(self, contract_client: Any) -> None:
        """Assert the derived PUT binds the context provider and omits internal fields.

        Overrides the generic suite method for the same gating reason as
        :meth:`test_create_injects_extras`.
        """
        base = app_base_url(self.app_def)

        response = contract_client.put(
            f"{base}/{SEEDED_TASK_NAME}",
            json=_valid_body(task_name=SEEDED_TASK_NAME),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        payload = response.json()
        assert "service_type" not in payload
        assert "owner" not in payload
        assert payload["created_by"] == SYNTH_CREATED_BY_NAME

    def _valid_update_body(self, *, task_name: str) -> dict[str, Any] | None:
        """Return the gated valid PUT body; the generic Polyfactory body 422s here.

        :param task_name: The task name stamped into the body.
        :return: A valid gated update body resolving against the kit mocks.
        """
        return _valid_body(task_name=task_name)

    def test_create_201(self, contract_client: Any, mock_task_api: Any) -> None:
        """Create a task via a real JSON POST with a valid gated body, returning 201."""
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_body())

        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert mock_task_api.create_count == 1
        body = response.json()
        assert body["backup_type"] == BackupType.MYDUMPER.value
        assert body["hostname"] == SYNTH_EXECUTOR_HOST
        assert "service_type" not in body
        assert "owner" not in body
        assert "anonymize_mask" in body
        assert "anonymized_entities" in body

    def test_create_serializes_encryption_block(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Serialize the encryption bools and dir_encrypt_config through the derived POST.

        Asserts the derived HTTP surface carries an encrypted selection into the
        exact wire keys the backup backend consumes — the ``ENCRYPTION_FORMAT``
        selector, the ``ENCRYPT`` / ``POST_RUN_ENCRYPT`` / ``ENCRYPT_USING_TMPDIR``
        timing booleans, and the ``DIR_ENCRYPT_CONFIG`` recipient block; full
        byte-identity of the spec path is frozen by the payload snapshot matrix.
        """
        body = _valid_body()
        body.update(
            encryption_format="gpg",
            encrypt=True,
            post_run_encrypt=True,
            encryption_recipient="ops@example.com",
        )
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        config = yaml.safe_load(
            mock_task_api.last_create_payload["data"]["meta"]["config"]
        )
        assert config["ALL_SERVERS"]["ENCRYPTION_FORMAT"] == "gpg"
        assert config["ALL_SERVERS"]["ENCRYPT"] is True
        assert config["ALL_SERVERS"]["POST_RUN_ENCRYPT"] is True
        assert config["ALL_SERVERS"]["ENCRYPT_USING_TMPDIR"] is False
        assert config["SERVER_LIST"][0]["DIR_ENCRYPT_CONFIG"] == {
            "encryption recipient": "ops@example.com"
        }

    def test_create_422(self, contract_client: Any, mock_task_api: Any) -> None:
        """Reject a body missing the required ``backup_type`` with 422, before any POST."""
        body = _valid_body()
        del body["backup_type"]
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert mock_task_api.create_count == 0

    def test_create_connectivity_warning(
        self, contract_client: Any, mocker: MockerFixture
    ) -> None:
        """Attach the connectivity probe warning to the create response."""
        mocker.patch(
            _CONNECTIVITY_PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=ConnectivityWarning(
                target="db-host", service_type="mysql", message="unreachable"
            ),
        )
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_body())

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is not None

    def test_create_threads_executor_host_to_meta_target(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Assert the submitted ``HostRef`` host threads through to ``meta.target``.

        The executor target equals the submitted host and stays distinct from the
        service address carried on the connectivity host key.
        """
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=_valid_body())

        assert response.status_code == status.HTTP_201_CREATED
        meta = mock_task_api.last_create_payload["data"]["meta"]
        assert meta["target"] == SYNTH_EXECUTOR_HOST
        assert meta["target"] != meta[CONNECTIVITY_META_HOST_KEY]

    def test_create_rejects_an_unknown_field(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject a valid body plus an undeclared key with 422 naming it, before any POST."""
        body = _valid_body()
        body[UNKNOWN_FIELD] = "value"
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/", json=body)

        assert_unknown_field_rejected(response)
        assert mock_task_api.create_count == 0

    def test_update_rejects_an_unknown_field(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Reject a ``PUT`` carrying an undeclared key with 422 naming it, changing nothing."""
        body = _valid_body(task_name=SEEDED_TASK_NAME)
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
            json=_valid_body(task_name=SEEDED_TASK_NAME),
        )

        assert response.status_code == status.HTTP_200_OK, response.text

    def test_update_404(self, contract_client: Any) -> None:
        """``PUT /{task_name}`` 404s for an unknown task name."""
        base = app_base_url(self.app_def)

        response = contract_client.put(
            f"{base}/{_UNKNOWN_TASK_NAME}",
            json=_valid_body(task_name=_UNKNOWN_TASK_NAME),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_stamps_form_input(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Persist the validated gated create body under ``data['_form']``.

        Overrides the generic suite method: the per-mode ``FailRule``s and
        upload-consistency validator defeat the Polyfactory pass, so a hand-built
        gated body is used.
        """
        base = app_base_url(self.app_def)
        body = _valid_body()

        response = contract_client.post(f"{base}/", json=body)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        expected = self.app_def.create_model.model_validate(body).model_dump(
            mode="json"
        )
        assert mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY] == expected

    def test_every_declared_field_is_described(self) -> None:
        """Require helper text on every field the create form declares itself.

        A backup misconfigured from a guessed field is not caught at submit time
        — it is caught at restore time, when the configuration can no longer be
        changed.
        """
        assert_every_declared_field_is_described(BackupCreate)

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
        assert_schema_serves_only_declared_descriptions(response.json(), BackupCreate)

    def test_schema_pins_section_collapse_posture(self, contract_client: Any) -> None:
        """Pin every create-form section's collapse posture and required fields.

        The form opens on what a backup needs: ``Task`` carries the required
        fields and never collapses, and every expert section is collapsible *and*
        collapsed, so the expanded-by-default wall of fields cannot come back and
        a section added later without a posture decision fails here. Where the
        required fields sit is pinned too, so none of them can drift behind a
        collapse toggle.

        Order and ``advanced`` are pinned as a list rather than a mapping, so a
        section changing place or losing its advanced marking fails here rather
        than quietly changing what the form opens on.
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
                section["advanced"],
            )
            for section in sections
        ] == [
            ("Task", False, False, False),
            ("Mydumper", True, True, False),
            ("XtraBackup", True, True, False),
            ("Binlog", True, True, False),
            ("General", True, True, True),
            ("Encryption", True, True, True),
            ("Upload", True, True, True),
        ]
        required_fields = {
            (section["title"], field["name"])
            for section in sections
            for field in section["fields"]
            if field["required"]
        }
        assert required_fields == {
            ("Task", "task_name"),
            ("Task", "hostname"),
            ("Task", "service_id"),
            ("Task", "backup_type"),
            ("Task", "backup_dir"),
        }

    def test_schema_publishes_the_binary_compression_gate(
        self, contract_client: Any
    ) -> None:
        """Serve one compression rule per gated binary, on that field's section.

        The renderer evaluates section-scoped rules only, so this is the scope that
        gets the operator a message before submit rather than after. Asserted
        against the section that actually declares the field, so moving the field
        without moving the rules fails here instead of silently detaching the
        message from the fields it is about.
        """
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK, response.text
        section = next(
            section
            for section in response.json()["forms"]
            if any(
                field.get("name") == "compression_algorithm"
                for field in section["fields"]
            )
        )
        rules = [
            rule
            for rule in section["fail_when"]
            if rule["error_fields"] == ["compression_algorithm"]
        ]
        # Derived with the builder's own skip condition rather than counting the
        # whole matrix: a binary that accepts every algorithm the type offers has
        # nothing to reject and is served no rule, so a row widened to the full
        # list would otherwise fail here for being correct.
        gated = {
            binary: allowed
            for binary, allowed in ALLOWED_XTRABACKUP_BIN_COMPRESSIONS.items()
            if set(allowed) != set(ALLOWED_COMPRESSIONS[BackupType.XTRABACKUP])
        }

        assert len(rules) == len(gated)
        for binary, allowed in gated.items():
            rule = next(
                rule for rule in rules if f"is {binary.value!r}" in rule["message"]
            )
            for algorithm in allowed:
                assert algorithm.value in rule["message"]

    def test_create_rejects_an_unsupported_binary_pairing(
        self, contract_client: Any
    ) -> None:
        """Refuse a POST whose algorithm the selected binary cannot run.

        Exercises the gate over the wire rather than through ``model_validate``,
        which is the only way to see what the operator's client receives: the
        rules are app-model-level, so the 422 carries a whole-body ``loc`` and the
        rejection is identified by its message rather than by a field path.
        """
        base = app_base_url(self.app_def)
        body = _valid_body(backup_type=BackupType.XTRABACKUP)
        body["xtrabackup_bin_cmd"] = "innobackupex"
        body["compression_algorithm"] = CompressionAlgorithm.ZSTD.value

        response = contract_client.post(base, json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        messages = [error["msg"] for error in response.json()["detail"]]
        assert any(
            "must be quicklz when the backup binary is 'innobackupex'" in message
            for message in messages
        ), messages

    def test_update_rejects_a_body_without_a_backup_directory(
        self, contract_client: Any
    ) -> None:
        """Refuse a PUT that drops the backup directory.

        The update route is how an operator repairs a task saved before the
        directory was required, so it has to insist on the value rather than
        accept the stored ``None`` back.
        """
        base = app_base_url(self.app_def)
        body = _valid_body(task_name=SEEDED_TASK_NAME)
        body.pop("backup_dir", None)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert ["body", "backup_dir"] in [
            error["loc"] for error in response.json()["detail"]
        ]

    def test_update_round_trips_stored_form(
        self, contract_client: Any, mock_task_api: Any
    ) -> None:
        """Assert the stored gated ``_form`` re-validates and re-stamps on PUT."""
        base = app_base_url(self.app_def)
        task_name = "contract-roundtrip-backup"
        create = contract_client.post(f"{base}/", json=_valid_body(task_name=task_name))
        assert create.status_code == status.HTTP_201_CREATED, create.text
        stored_form = mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY]

        response = contract_client.put(f"{base}/{task_name}", json=stored_form)

        assert response.status_code == status.HTTP_200_OK, response.text
        assert (
            mock_task_api.last_update_payload["data"][RESERVED_FORM_KEY] == stored_form
        )


def test_views_declare_detail_view() -> None:
    """Assert the app declares a detail_view surfacing the backup config.

    Regression guard: the always-rendered "Task information" card already shows
    the list columns (``hostname`` / ``backup_type``), so the detail view must
    surface the config that is *not* a column — the executor target and the YAML
    config under ``data.meta`` — rather than duplicate the columns.
    """
    detail_view = mysql_backups_app.views.detail_view
    assert detail_view is not None
    assert [section.title for section in detail_view.sections] == [
        "Backup Configuration"
    ]
    paths = [field.path for field in detail_view.sections[0].fields]
    assert paths == ["data.meta.target", "data.meta.config"]


def test_update_returns_create_mirror_shape(regular_user: Any) -> None:
    """Return the create-mirror body from the derived PUT: backup_type, hostname, warning.

    Rebuilds the spec from the body (re-selecting the payload file by
    ``backup_type``) and renders the create response model, so the additive
    ``connectivity_warning`` field is present alongside the stamped
    ``backup_type`` / ``hostname``.
    """
    tasks_api = MockTaskAPI()
    tasks_api.seed_task(SEEDED_TASK_NAME, owner=mysql_backups_app.owner)
    client = build_contract_client(
        mysql_backups_app,
        user=regular_user,
        tasks_api=tasks_api,
        inventory_api=MockInventoryAPI(),
    )

    response = client.put(
        f"{app_base_url(mysql_backups_app)}/{SEEDED_TASK_NAME}",
        json=_valid_body(task_name=SEEDED_TASK_NAME, backup_type=BackupType.XTRABACKUP),
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["backup_type"] == BackupType.XTRABACKUP.value
    assert body["hostname"] == SYNTH_EXECUTOR_HOST
    assert "connectivity_warning" in body
    assert "service_type" not in body
    assert "owner" not in body
    assert "anonymize_mask" in body
    assert "anonymized_entities" in body


def test_create_check_connectivity_false_skips_probe(
    regular_user: Any, mocker: MockerFixture
) -> None:
    """``?check_connectivity=false`` skips the probe, leaving ``connectivity_warning`` null."""
    probe = mocker.patch(
        _CONNECTIVITY_PATCH_TARGET,
        new_callable=AsyncMock,
        return_value=ConnectivityWarning(
            target="db-host", service_type="mysql", message="unreachable"
        ),
    )
    tasks_api = MockTaskAPI()
    client = build_contract_client(
        mysql_backups_app,
        user=regular_user,
        tasks_api=tasks_api,
        inventory_api=MockInventoryAPI(),
    )

    response = client.post(
        f"{app_base_url(mysql_backups_app)}/?check_connectivity=false",
        json=_valid_body(),
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert response.json()["connectivity_warning"] is None
    probe.assert_not_awaited()
