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

"""Cover where ``credentials_path`` is offered, and where it deliberately is not."""

from app.sep.apps.backup_mongo.config.schema import backup_mongo_config_schema
from app.sep.apps.backup_mongo.models import BackupCreate, BackupTaskWrite
from app.sep.apps.backup_mongo.schema import backup_mongo_schema


def _field_names(schema, section_title):
    """Return the field names under ``section_title``, or ``None`` if there is no such section.

    Sections are addressed by title because that is all the wire schema carries:
    the layout's ``key`` binds fields to sections during derivation and is not
    serialised.
    """
    section = next((s for s in schema.forms if s.title == section_title), None)
    return None if section is None else [field.name for field in section.fields]


def _all_field_names(schema):
    """Return every field name in ``schema``'s form, across all sections."""
    return [field.name for section in schema.forms for field in section.fields]


class TestCredentialsPathPlacement:
    """Pin the field to the config form's Advanced section and nowhere else."""

    def test_backups_form_does_not_ask_for_it(self) -> None:
        """Keep ``credentials_path`` off the backups form entirely."""
        assert "credentials_path" not in _all_field_names(backup_mongo_schema)

    def test_backups_task_section_is_service_and_host_only(self) -> None:
        """Leave the Task section as name / service / host once the path is gone."""
        assert _field_names(backup_mongo_schema, "Task") == [
            "task_name",
            "service_id",
            "hostname",
        ]

    def test_config_form_offers_it_under_advanced(self) -> None:
        """Offer it on the config form, alone in its ``Advanced`` section."""
        assert _field_names(backup_mongo_config_schema, "Advanced") == [
            "credentials_path"
        ]

    def test_expert_sections_share_one_disclosure(self) -> None:
        """Mark the three expert sections advanced and leave the everyday ones plain.

        The renderer collects every advanced section behind a single "Show advanced
        options" control, so marking three costs one row at rest rather than three.
        """
        advanced = [s.title for s in backup_mongo_config_schema.forms if s.advanced]

        assert advanced == ["Storage Tuning", "Restore Tuning", "Advanced"]

    def test_config_owns_every_cluster_wide_section(self) -> None:
        """Hold the whole of PBM's cluster-wide configuration, in reading order."""
        assert [section.title for section in backup_mongo_config_schema.forms] == [
            "Task",
            "Storage",
            "Storage Tuning",
            "Point-in-Time Recovery",
            "Backup Options",
            "Restore Tuning",
            "Advanced",
        ]

    def test_the_two_forms_share_only_the_task_section(self) -> None:
        """Overlap on identity alone.

        Both forms name a task, a MongoDB service and an executor -- that trio is
        declared once on ``_BackupMongoTaskForm``. Everything below it is disjoint,
        which is the property that stops a backup from restating, and overwriting,
        the cluster's configuration.
        """
        backups = {
            field.name
            for section in backup_mongo_schema.forms
            for field in section.fields
        }
        config = {
            field.name
            for section in backup_mongo_config_schema.forms
            for field in section.fields
        }

        assert backups & config == {"task_name", "service_id", "hostname"}

    def test_storage_lives_on_config_with_its_gates_intact(self) -> None:
        """Carry the storage-type gates over with the fields they hide."""
        storage = next(
            section
            for section in backup_mongo_config_schema.forms
            if section.title == "Storage"
        )
        fields = {field.name: field for field in storage.fields}

        assert fields["storage_type"].default == "s3"
        assert [
            gate.model_dump(exclude_none=True)
            for gate in fields["storage_s3_bucket"].forbidden
        ] == [{"when": {"not_equals": {"storage_type": "s3"}}}]
        assert [
            gate.model_dump(exclude_none=True)
            for gate in fields["storage_filesystem_path"].forbidden
        ] == [{"when": {"not_equals": {"storage_type": "filesystem"}}}]

    def test_backup_options_split_by_who_owns_the_value(self) -> None:
        """Keep per-run choices on backups and deployment properties on config.

        ``compression`` and the selective-namespace flags are read by the backup
        payload on each run; ``priority``, ``timeouts``, ``oplogSpanMin`` and
        ``numParallelCollections`` are PBM config keys that describe the cluster.
        """
        config_options = _field_names(backup_mongo_config_schema, "Backup Options")
        backup_options = _field_names(backup_mongo_schema, "Backup Options")

        assert config_options == [
            "backup_priority",
            "backup_timeouts_starting_status",
            "backup_oplog_span_min",
            "backup_num_parallel_collections",
            "backup_num_parallel_files",
            "backup_timeouts_balancer_stop",
        ]
        assert backup_options == [
            "backup_compression",
            "backup_compression_level",
            "backup_namespaces",
            "backup_with_users_and_roles",
        ]

    def test_service_selector_is_labelled_for_a_cluster(self) -> None:
        """Say "cluster member": PBM keeps one document per cluster, not per service."""
        task = next(
            section
            for section in backup_mongo_config_schema.forms
            if section.title == "Task"
        )
        service = next(f for f in task.fields if f.name == "service_id")

        assert service.label == "Cluster member"


class TestCredentialsPathRemainsOnTheWire:
    """Guard the request models the form change must not touch."""

    def test_create_model_still_carries_it(self) -> None:
        """Keep it on ``BackupCreate``: the spec builder reads it to build the payload."""
        assert "credentials_path" in BackupCreate.model_fields

    def test_request_body_still_accepts_it(self) -> None:
        """Accept it from a caller that sends it, form or no form."""
        assert "credentials_path" in BackupTaskWrite.model_fields
