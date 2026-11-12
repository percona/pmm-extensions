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

"""Define the alert restore-from-backup logic."""

import logging
from typing import Any

from app.core.exceptions import HTTPNotFoundException
from app.extensions.apps.alerts.config import alerts_settings
from app.extensions.apps.alerts.deps import find_or_create_alert_folder
from app.extensions.apps.alerts.loader import get_alert_templates
from app.extensions.apps.alerts.models import (
    AlertBackup,
    AlertTemplate,
    DEFAULT_FOR_DURATION,
    to_pmm_template_yaml,
)
from app.extensions.clients.pmm import (
    AlertRule,
    Folder,
    NotificationPolicy,
    PMMRemoteAPI,
)

logger = logging.getLogger(__name__)


def is_conflicting_rule(
    rule: AlertRule, rule_name: str, folder_uid: str, *, include_empty: bool = True
) -> bool:
    """Return whether ``rule`` conflicts with ``rule_name`` in the folder.

    A rule conflicts when it lives in ``folder_uid`` and its title equals
    ``rule_name`` or, when ``include_empty`` is set, is empty (ghost rule).

    :param rule: The rule to test.
    :param rule_name: The rule title being created.
    :param folder_uid: The folder UID the rule is created in.
    :param include_empty: Treat empty-title rules as conflicting.
    :return: ``True`` if the rule conflicts.
    """
    if getattr(rule, "namespace_uid", "") != folder_uid:
        return False
    titles = (rule_name, "") if include_empty else (rule_name,)
    return rule.title in titles


async def delete_conflicting_rules(
    pmm_api: PMMRemoteAPI, rule_name: str, folder_uid: str
) -> None:
    """Delete rules that conflict with the given name in the folder.

    Remove any existing rule whose title matches ``rule_name`` as well as
    ghost rules (empty title) within the same folder so that a subsequent
    ``create_rule`` call can succeed.

    :param pmm_api: The PMM API client.
    :param rule_name: The rule title that triggered the conflict.
    :param folder_uid: The folder UID where the conflict occurred.
    """
    rules = await pmm_api.list_rules()
    for rule in rules:
        if is_conflicting_rule(rule, rule_name, folder_uid):
            logger.info("Deleting conflicting rule %s (title=%r)", rule.uid, rule.title)
            await pmm_api.delete_rule(rule.uid)


async def _restore_contact_point(
    pmm_api: PMMRemoteAPI,
    uid: str,
    name: str,
    type_: str,
    cp_settings: dict[str, Any],
) -> None:
    """Update a contact point, falling back to delete+create on 404.

    Grafana's provisioning PUT endpoint returns 404 for contact points
    that were not originally provisioned. In that case, delete the
    existing contact point and recreate it from the backup data. If
    delete also returns 404 (the provisioning API cannot manage this
    contact point at all), skip silently — the contact point already
    exists with the correct name.

    :param pmm_api: The PMM API client.
    :type pmm_api: PMMRemoteAPI
    :param uid: The UID of the existing contact point.
    :type uid: str
    :param name: The contact point display name.
    :type name: str
    :param type_: The contact point type (e.g. ``"email"``, ``"slack"``).
    :type type_: str
    :param cp_settings: Type-specific configuration settings.
    :type cp_settings: dict[str, Any]
    """
    try:
        await pmm_api.update_contact_point(uid, name, type_, cp_settings)
    except HTTPNotFoundException:
        try:
            await pmm_api.delete_contact_point(uid)
        except HTTPNotFoundException:
            return
        await pmm_api.create_contact_point(name, type_, cp_settings)


async def _restore_rules(
    pmm_api: PMMRemoteAPI,
    backup_rules: list[dict[str, Any]],
    existing_rules: list[AlertRule],
    folder: Folder,
) -> dict[str, int]:
    """Restore alert rules idempotently, auto-pushing missing templates.

    Skip rules that already exist (by title). For new rules, ensure the
    required template exists in PMM — if not, push it from local PMM Extensions
    definitions. Skip the rule if neither PMM nor local definitions have
    the template.

    :param pmm_api: The PMM API client.
    :type pmm_api: PMMRemoteAPI
    :param backup_rules: Rule data from the backup snapshot.
    :type backup_rules: list[dict[str, Any]]
    :param existing_rules: Currently existing rules in PMM.
    :type existing_rules: list[AlertRule]
    :param folder: The alert folder to create rules in.
    :type folder: Folder
    :return: Counts of deleted, created, and skipped rules.
    :rtype: dict[str, int]
    """
    existing_by_title = {r.title: r for r in existing_rules}
    backup_titles = {r["title"] for r in backup_rules}
    local_templates: dict[str, AlertTemplate] = {
        t.name: t for ts in get_alert_templates().values() for t in ts
    }

    rules_to_delete = [r for r in existing_rules if r.title not in backup_titles]
    for rule in rules_to_delete:
        await pmm_api.delete_rule(rule.uid)

    created = 0
    skipped = 0
    for r_data in backup_rules:
        if r_data["title"] in existing_by_title:
            skipped += 1
            continue
        template_name = r_data.get("labels", {}).get("template_name", r_data["title"])
        if not await pmm_api.template_exists(template_name):
            local_tmpl = local_templates.get(template_name)
            if local_tmpl:
                await pmm_api.create_template(to_pmm_template_yaml(local_tmpl))
            else:
                logger.warning(
                    "Skipping rule %r: template %r not found in PMM or locally",
                    r_data["title"],
                    template_name,
                )
                skipped += 1
                continue
        try:
            await pmm_api.create_rule(
                name=r_data["title"],
                template_name=template_name,
                folder_uid=folder.uid,
                for_duration=r_data.get("for", DEFAULT_FOR_DURATION),
                group=r_data.get("group", alerts_settings.ALERT_FOLDER_NAME),
            )
            created += 1
        except HTTPNotFoundException:
            logger.warning(
                "Skipping rule %r: template %r not found in PMM",
                r_data["title"],
                template_name,
            )
            skipped += 1
    return {"deleted": len(rules_to_delete), "created": created, "skipped": skipped}


async def _restore_contact_points(
    pmm_api: PMMRemoteAPI,
    backup_cps: list[dict[str, Any]],
) -> dict[str, int]:
    """Restore contact points idempotently.

    Update existing contact points and create new ones.

    :param pmm_api: The PMM API client.
    :type pmm_api: PMMRemoteAPI
    :param backup_cps: Contact point data from the backup snapshot.
    :type backup_cps: list[dict[str, Any]]
    :return: Counts of created and updated contact points.
    :rtype: dict[str, int]
    """
    existing_cps = await pmm_api.list_contact_points()
    existing_cp_map = {cp.name: cp for cp in existing_cps}
    created = 0
    updated = 0
    for cp_data in backup_cps:
        name = cp_data["name"]
        type_ = cp_data["type"]
        cp_settings = cp_data.get("settings", {})
        if name in existing_cp_map:
            await _restore_contact_point(
                pmm_api, existing_cp_map[name].uid, name, type_, cp_settings
            )
            updated += 1
        else:
            await pmm_api.create_contact_point(name, type_, cp_settings)
            created += 1
    return {"created": created, "updated": updated}


async def restore_from_backup(
    pmm_api: PMMRemoteAPI, backup: AlertBackup
) -> dict[str, Any]:
    """Restore alert configuration from a backup snapshot.

    Reconcile existing alert rules against the backup (delete stale,
    skip matching, create missing), then restore templates, contact
    points, and notification policies.

    :param pmm_api: The PMM API client for making alerting API calls.
    :type pmm_api: PMMRemoteAPI
    :param backup: The backup record containing the configuration snapshot.
    :type backup: AlertBackup
    :return: A dictionary summarizing the restore operation counts per
        resource type.
    :rtype: dict[str, Any]
    """
    data = backup.data
    results: dict[str, Any] = {}

    existing_rules = await pmm_api.list_rules()
    folder = await find_or_create_alert_folder(pmm_api)

    created_templates = 0
    skipped_templates = 0
    for t_data in data.get("templates", []):
        if await pmm_api.template_exists(t_data["name"]):
            skipped_templates += 1
        else:
            await pmm_api.create_template(t_data["template"])
            created_templates += 1
    results["templates"] = {"created": created_templates, "skipped": skipped_templates}

    rule_results = await _restore_rules(
        pmm_api, data.get("rules", []), existing_rules, folder
    )
    results["rules_deleted"] = rule_results["deleted"]
    results["rules_created"] = rule_results["created"]
    results["rules_skipped"] = rule_results["skipped"]

    results["contact_points"] = await _restore_contact_points(
        pmm_api, data.get("contact_points", [])
    )

    policy_data = data.get("notification_policy", {})
    if policy_data:
        policy = NotificationPolicy.model_validate(policy_data)
        await pmm_api.update_notification_policy(policy)
        results["notification_policies"] = "restored"
    else:
        results["notification_policies"] = "skipped"

    return results
