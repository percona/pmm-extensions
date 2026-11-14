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

"""Build the restore task-group envelopes for the MongoDB Restores app.

:func:`build_restore_payloads` is the pure ``(form, service_name) ->
RestoreTaskGroupPayloads`` builder shared by the JSON create route and the legacy
Jinja form path (both feed it the service name resolved by the impure, 404-tolerant
``_resolve_service_name`` in ``deps``), so a restore task group's payloads are
byte-identical regardless of the call origin. Each leg's envelope is assembled through
the framework's ``build_run_python_task`` builder (``run-python`` tasks carry no
connectivity meta; each child carries ``data["parent"]``), so the helpers here take no
inventory or API client.
"""

import yaml

from app.core.utils.path import payload_uri
from app.extensions.apps.backup_mongo.restore.models import (
    OWNER,
    PbmForceResyncPayloadModel,
    PbmListPayloadModel,
    restore_leg_payload_models_from_form,
    RestoreConfig,
    RestoreConfigPayloadModel,
    RestoreCreate,
    RestoreLegPayloadModel,
    RestoreTaskGroupPayloads,
    RestoreTaskLegModel,
)
from app.extensions.apps.framework.spec import build_run_python_task
from app.tasks.models import TaskWrite

RESTORE_CONFIG_PAYLOAD_MARKER = "pbm_restore_config_payload"
_BASE_REQUIREMENTS = "packaging\nPyYAML"


def _task_write_from_leg(leg: RestoreTaskLegModel) -> TaskWrite:
    """Build the run-python ``TaskWrite`` from a typed restore leg descriptor.

    :param leg: The typed restore leg carrying the envelope's target, config,
        requirements, payload name, and optional service name and parent.
    :return: The assembled leg ``TaskWrite``.
    """
    return build_run_python_task(
        name=leg.name,
        owner=OWNER,
        target=leg.target,
        config=leg.config_yaml,
        requirements=leg.requirements,
        payload=payload_uri(__file__, leg.payload_name),
        service_name=leg.service_name,
        extra_data=None if leg.parent is None else {"parent": leg.parent},
    )


def _build_restore_config_leg(
    payload: RestoreConfigPayloadModel,
) -> RestoreTaskLegModel:
    """Build typed task leg for restore-config task."""
    restore_config = RestoreConfig(
        restore=payload.restore,
        backup_source=payload.backup_source,
        backup_type=payload.backup_type,
        credentials_path=payload.credentials_path,
    )
    return RestoreTaskLegModel(
        name=payload.task_name,
        payload_name=RESTORE_CONFIG_PAYLOAD_MARKER,
        target=payload.hostname,
        requirements=_BASE_REQUIREMENTS,
        config_yaml=yaml.dump(
            restore_config.model_dump(by_alias=True, exclude_none=True, mode="json"),
            default_flow_style=False,
            allow_unicode=True,
        ),
        service_name=payload.service_name,
    )


def _build_restore_leg(payload: RestoreLegPayloadModel) -> RestoreTaskLegModel:
    """Build typed task leg for the restore execution task.

    The ``restore`` options are omitted from this leg's config because the config
    leg has already synced them to PBM, so the execution leg carries no restore
    sub-config.
    """
    restore_config = RestoreConfig(
        restore=None,
        backup_source=payload.backup_source,
        backup_type=payload.backup_type,
        namespace=payload.namespace,
        credentials_path=payload.credentials_path,
    )
    return RestoreTaskLegModel(
        name=f"{payload.task_name}-{payload.backup_type}",
        payload_name=payload.payload_script_name(),
        target=payload.hostname,
        requirements=_BASE_REQUIREMENTS,
        config_yaml=yaml.dump(
            restore_config.model_dump(by_alias=True, exclude_none=True, mode="json"),
            default_flow_style=False,
            allow_unicode=True,
        ),
        parent=payload.task_name,
        service_name=payload.service_name,
    )


def _build_pbm_list_leg(payload: PbmListPayloadModel) -> RestoreTaskLegModel:
    """Build typed task leg for pbm-list helper task."""
    config_dict = (
        {"credentials_path": payload.credentials_path}
        if payload.credentials_path
        else {}
    )
    return RestoreTaskLegModel(
        name=f"{payload.task_name}-pbm-list",
        payload_name="pbm_list_payload",
        target=payload.hostname,
        config_yaml=yaml.dump(config_dict, default_flow_style=False)
        if config_dict
        else "",
        parent=payload.task_name,
        service_name=payload.service_name,
    )


def _build_pbm_force_resync_leg(
    payload: PbmForceResyncPayloadModel,
) -> RestoreTaskLegModel:
    """Build typed task leg for pbm-force-resync helper task."""
    config_dict = (
        {"credentials_path": payload.credentials_path}
        if payload.credentials_path
        else {}
    )
    return RestoreTaskLegModel(
        name=f"{payload.task_name}-pbm-force-resync",
        payload_name="pbm_force_resync_payload",
        target=payload.hostname,
        config_yaml=yaml.dump(config_dict, default_flow_style=False)
        if config_dict
        else "",
        parent=payload.task_name,
        service_name=payload.service_name,
    )


def build_restore_payloads(
    form: RestoreCreate,
    service_name: str | None,
) -> RestoreTaskGroupPayloads:
    """Build restore config, restore, list and optional force-resync payloads."""
    leg_models = restore_leg_payload_models_from_form(form, service_name)

    config_task = _task_write_from_leg(_build_restore_config_leg(leg_models.config))
    restore_task = _task_write_from_leg(_build_restore_leg(leg_models.restore))
    pbm_list_task = _task_write_from_leg(_build_pbm_list_leg(leg_models.pbm_list))
    force_resync_task = (
        _task_write_from_leg(_build_pbm_force_resync_leg(leg_models.force_resync))
        if leg_models.force_resync is not None
        else None
    )
    return RestoreTaskGroupPayloads(
        config_task=config_task,
        restore_task=restore_task,
        pbm_list_task=pbm_list_task,
        force_resync_task=force_resync_task,
    )
