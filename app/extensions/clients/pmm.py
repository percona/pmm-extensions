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

"""PMM API client for interacting with the PMM inventory system."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any, ClassVar, NoReturn

from aiohttp import ClientResponse, ClientResponseError
from async_lru import _LRUCacheWrapper, alru_cache
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.requests import (
    as_json_array,
    as_json_object,
    RemoteAPI,
    StoredCredentialHeaderMixin,
)
from app.core.requests.connectivity import (
    build_connectivity_result,
    classify_connectivity_error,
    ConnectivityResult,
    ConnectivityStatusEnum,
    PROBE_TIMEOUT_SECONDS,
)
from app.core.requests.remote_api import (
    exception_for_status,
    UPSTREAM_NON_JSON_HEADER,
)
from app.core.utils.dict import remove_falsy_values_from_dict
from app.core.utils.fields import AuthCredentialSecretStr, NonEmptyStr
from app.extensions.inventory import Node, Schema, Service
from app.inventory.models import SourceEnum

logger = logging.getLogger(__name__)


class AlertTemplate(BaseModel):
    """Represent a PMM alert template.

    :param name: The unique name of the template.
    :type name: str
    :param summary: A short human-readable description of the template.
    :type summary: str
    :param template: The raw YAML content of the template.
    :type template: str
    """

    model_config = ConfigDict(extra="allow")

    name: str
    summary: str
    template: str = ""


class AlertRule(BaseModel):
    """Represent a PMM alert rule.

    :param uid: The unique identifier of the rule.
    :type uid: str
    :param title: The display title of the rule.
    :type title: str
    :param labels: Labels attached to the rule.
    :type labels: dict[str, str]
    :param annotations: Annotations attached to the rule.
    :type annotations: dict[str, str]
    :param data: Query data for the rule.
    :type data: list[dict[str, Any]]
    """

    model_config = ConfigDict(extra="allow")

    uid: str
    title: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    data: list[dict[str, Any]] = []


class Folder(BaseModel):
    """Represent a Grafana folder in PMM.

    :param uid: The unique identifier of the folder.
    :type uid: str
    :param title: The display title of the folder.
    :type title: str
    :param id: The numeric identifier of the folder.
    :type id: int
    """

    model_config = ConfigDict(extra="allow")

    uid: str
    title: str
    id: int


class ContactPoint(BaseModel):
    """Represent a Grafana alert contact point.

    :param uid: The unique identifier of the contact point.
    :type uid: str
    :param name: The display name of the contact point.
    :type name: str
    :param type: The type of the contact point (e.g., "slack", "email").
    :type type: str
    :param settings: Type-specific configuration settings.
    :type settings: dict[str, Any]
    """

    model_config = ConfigDict(extra="allow")

    uid: str
    name: str
    type: str
    settings: dict[str, Any] = {}


class NotificationPolicy(BaseModel):
    """Represent a Grafana notification policy tree.

    :param receiver: The name of the default receiver.
    :type receiver: str
    :param group_by: Labels used to group alerts.
    :type group_by: list[str]
    :param routes: Nested routing rules.
    :type routes: list[dict[str, Any]]
    """

    model_config = ConfigDict(extra="allow")

    receiver: str
    group_by: list[str] = []
    routes: list[dict[str, Any]] = []


class PMMService(Service):
    """Represent a PMM-specific inventory service.

    This class extends the base `Service` model to include PMM-specific attributes
    such as the node string identifier (external_id).

    :param environment: The environment in which the service is running (e.g.,
        "production", "staging"). Defaults to None.
    :param cluster: The cluster in which the service is running. Defaults to None.
    :param replication_set: The replication set in which the service is running. Defaults to None.
    :param custom_labels: Custom labels associated with the service. Defaults to None.
    :param external_id: The external identifier for the service, aliased as
        "service_id".
    :param name: The name of the service, aliased as "service_name".
    :param port: The port number on which the service is running, aliased as
        "service_port". Defaults to None.
    :param type: The type of the service (e.g., "service_type"), aliased as
        "service_type". Defaults to "generic".
    :param node_id: The identifier of the node to which the service is associated.
    """

    node_id: str


PMMService.model_rebuild(_types_namespace={"Schema": Schema})


class PMMFetchDiagnostics(BaseModel):
    """Record what a PMM inventory fetch could not represent faithfully.

    An inventory fetch reads the service list and the node list as two separate
    calls and joins them, and it drops whatever fails validation. Neither loss is
    visible in the resulting node list, so a caller cannot tell a genuinely empty
    estate from a half-read one without this record.

    :param invalid_nodes: The number of nodes dropped by validation.
    :param invalid_services: The number of services dropped by validation.
    :param orphan_service_node_ids: Node IDs referenced by fetched services but
        absent from the node list, which means the two lists disagree.
    :param filtered_node_ids: The IDs of nodes excluded by the caller's filter.
    :param filtered_service_ids: The IDs of services excluded by the caller's filter.
    """

    invalid_nodes: int = 0
    invalid_services: int = 0
    orphan_service_node_ids: list[str] = []
    filtered_node_ids: set[str] = set()
    filtered_service_ids: set[str] = set()

    @property
    def is_complete(self) -> bool:
        """Return whether the fetch represented the remote inventory faithfully.

        Filtered entities do not count against completeness: an exclusion is an
        intentional operator choice, not a failure to read.

        :return: ``True`` when nothing was dropped and the two lists agree.
        """
        return not (
            self.invalid_nodes or self.invalid_services or self.orphan_service_node_ids
        )


class PMMInventorySnapshot(BaseModel):
    """Pair a fetched PMM node list with the diagnostics describing the fetch.

    :param nodes: The nodes fetched from PMM, with their services attached.
    :param diagnostics: What the fetch could not represent faithfully.
    """

    nodes: list[Node]
    diagnostics: PMMFetchDiagnostics


class PMMRemoteAPI(StoredCredentialHeaderMixin, RemoteAPI):
    """Handle remote API interactions specific to PMM.

    Provides methods to interact with the PMM inventory system, including fetching nodes
    and services, and managing service associations.

    :param endpoint: The base URL for the external API endpoint.
    :param verify_ssl: Whether to verify SSL certificates. Defaults to True.
    :param ssl_cafile: Path to the SSL certificate authority file. Defaults to None.
    :param ssl_keyfile: Path to the SSL key file. Defaults to None.
    :param ssl_certfile: Path to the SSL certificate file. Defaults to None.
    :param logger_name: Name to use for the logger. Defaults to `__name__`.
    :param api_key: The API key for authentication.
    :param auth_scheme: Scheme the ``Authorization`` header announces ahead of
        ``api_key``. Defaults to ``"Bearer"``.
    :param error_detail_key: The key to expect errors details to be. Defaults to
        "message".
    :param error_code_key: The key to expect error codes to be, or None if no error
        code is expected. Defaults to "code".
    :param default_to_v3: Whether to default to PMM v3 API endpoints if the API version
        cannot be determined. Defaults to True.
    :cvar CONNECTIVITY_CHECK_PATH: PMM exposes its version (and proves
        reachability) at this route, so the connectivity probe targets it
        instead of the base ``/``.
    """

    model_config = ConfigDict(ignored_types=(_LRUCacheWrapper,))
    CONNECTIVITY_CHECK_PATH: ClassVar[str] = "/v1/version"
    api_key: AuthCredentialSecretStr
    error_detail_key: NonEmptyStr = "message"
    error_code_key: NonEmptyStr | None = "code"
    default_to_v3: bool = True

    @alru_cache(ttl=600)
    async def is_older_than_v3(self) -> bool:
        """Check if the PMM version is older than 3.

        This method retrieves the PMM version and checks if it is older than 3.0.0.

        :return: True if the PMM version is older than 3, False otherwise.
        :rtype: bool
        """
        v3_major = 3
        try:
            version = await self.get_version()
        except (TypeError, KeyError):
            self.logger.exception(
                "Failed to retrieve PMM version, defaulting to %s",
                "v3" if self.default_to_v3 else "v2",
            )
            return not self.default_to_v3

        try:
            is_older = int(version.split(".")[0]) < v3_major
        except (AttributeError, ValueError):
            self.logger.exception(
                "Failed to parse PMM version, defaulting to %s: %s",
                "v3" if self.default_to_v3 else "v2",
                version,
            )
            return not self.default_to_v3

        if is_older:
            self.logger.warning(
                "Deprecation Warning: Support for PMM version < 3.0.0 is deprecated and will be removed in a future version (version found is %s).",
                version,
            )
        return is_older

    async def get_version(self) -> str:
        """Retrieve the PMM version.

        :return: The version of the PMM instance.
        :rtype: str
        """
        version_data = as_json_object(await self.get("/v1/version"))
        return version_data["version"]

    async def check_connectivity(
        self, service: str, *, path: str | None = None
    ) -> ConnectivityResult:
        """Probe PMM and report the PMM version on success.

        Override the generic probe so a successful check also reports the PMM
        version. The probe route is the class-level
        :attr:`CONNECTIVITY_CHECK_PATH` (``/v1/version``) unless an explicit
        ``path`` is given. A response with an unexpected body shape (missing
        ``version`` key) still counts as reachable -- the server answered --
        just without a version. All other failures are classified into the same
        outcome states as the base probe and never re-raised.

        :param service: Stable identifier of the probed service (``"pmm"``).
        :type service: str
        :param path: Optional override for the probe route. Defaults to
            :attr:`CONNECTIVITY_CHECK_PATH`.
        :type path: str | None
        :return: The normalized connectivity result, with ``version`` set on
            success.
        :rtype: ConnectivityResult
        """
        probe_path = path if path is not None else self.CONNECTIVITY_CHECK_PATH
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                version_data = as_json_object(await self.get(probe_path))
            version = version_data["version"]
        except (KeyError, TypeError):
            return build_connectivity_result(
                service,
                ConnectivityStatusEnum.REACHABLE,
                detail="Reachable (version unavailable).",
            )
        except Exception as exc:  # noqa: BLE001 -- classified, never re-raised
            return build_connectivity_result(service, classify_connectivity_error(exc))
        return build_connectivity_result(
            service, ConnectivityStatusEnum.REACHABLE, version=version
        )

    async def get_node(
        self,
        node_id: str,
        *,
        skip_failed_services: bool = True,
        filter_: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Node | None:
        """Retrieve a PMM node by its external ID.

        Send a request to the PMM API to fetch a node's details by its external ID.

        :param node_id: The external identifier of the node to retrieve.
        :type node_id: str
        :param skip_failed_services: Whether to skip services that fail validation.
            Defaults to True.
        :type skip_failed_services: bool
        :param filter_: Optional callable that takes a node or service dict and returns
            True if the item should be included, False if it should be filtered out.
            Used to filter the node and, when loading services, each service.
            Defaults to None.
        :type filter_: Callable[[dict[str, Any]], bool] | None
        :return: The retrieved node instance, or None if filtered out.
        :rtype: Node | None
        """
        if await self.is_older_than_v3():
            node_data = await self.post(
                "/v1/inventory/Nodes/Get",
                json={"node_id": node_id},
            )
        else:
            node_data = await self.get(
                f"/v1/inventory/nodes/{node_id}",
            )
        node_type, node = next(iter(node_data.items()))
        if filter_ is not None and not filter_(node):
            self.logger.debug(
                "Skipping node %s due to filter",
                node_id,
            )
            return None
        node |= {
            "source": SourceEnum.PMM,
            "type": node_type,
            "services": await self.get_services(
                node_id=node_id,
                skip_failed=skip_failed_services,
                filter_=filter_,
            ),
        }
        return Node.model_validate(node)

    async def get_service(
        self,
        service_id: str,
        *,
        filter_: Callable[[dict[str, Any]], bool] | None = None,
    ) -> PMMService | None:
        """Retrieve a PMM service by its ID.

        Send a request to the PMM API to fetch a service's details by its ID.

        :param service_id: The identifier of the service to retrieve.
        :type service_id: str
        :param filter_: Optional callable that takes a service dict and returns True if
            the service should be included, False if it should be filtered out.
            Defaults to None.
        :type filter_: Callable[[dict[str, Any]], bool] | None
        :return: The retrieved service instance, or None if filtered out.
        :rtype: PMMService | None
        """
        if await self.is_older_than_v3():
            service_data = await self.post(
                "/v1/inventory/Services/Get",
                json={"service_id": service_id},
            )
        else:
            service_data = await self.get(
                f"/v1/inventory/services/{service_id}",
            )
        service_type, service = next(iter(service_data.items()))
        if filter_ is not None and not filter_(service):
            self.logger.debug(
                "Skipping service %s due to filter",
                service_id,
            )
            return None
        service["type"] = service_type
        return PMMService.model_validate(service)

    async def get_services(
        self,
        node_id: str = "",
        service_type: str = "",
        external_group: str = "",
        *,
        skip_failed: bool = True,
        filter_: Callable[[dict[str, Any]], bool] | None = None,
        diagnostics: PMMFetchDiagnostics | None = None,
    ) -> list[PMMService]:
        """Fetch services from the PMM API.

        Retrieve a list of services filtered by node ID, service type, and external
        group.

        :param node_id: The ID of the node to filter services by. Defaults to an empty
            string, meaning the field won't be used as a filter.
        :param service_type: The type of services to filter by. Defaults to an empty
            string, meaning the field won't be used as a filter.
        :param external_group: The external group to filter services by. Defaults to an
            empty string, meaning the field won't be used as a filter.
        :param skip_failed: Whether to skip services that fail validation. Defaults to
            True.
        :param filter_: Optional callable that takes a service dict and returns True if
            the service should be included, False if it should be filtered out.
            Defaults to None.
        :param diagnostics: Optional record to accumulate what this fetch could not
            represent faithfully. Defaults to None, which records nothing.
        :return: A list of PMMService instances retrieved from the API.
        :raises ValidationError: If a service fails validation and `skip_failed` is
            False.
        """
        params = {
            "node_id": node_id,
            "service_type": service_type,
            "external_group": external_group,
        }
        params = remove_falsy_values_from_dict(params)
        if await self.is_older_than_v3():
            services_data = await self.post("/v1/inventory/Services/List", json=params)
        else:
            services_data = await self.get("/v1/inventory/services", params=params)

        services = []
        for services_type, service_list in services_data.items():
            for service in service_list:
                if filter_ is not None and not filter_(service):
                    self.logger.debug(
                        "Skipping service %s due to filter",
                        service.get("service_id"),
                    )
                    if diagnostics is not None and service.get("service_id"):
                        diagnostics.filtered_service_ids.add(service["service_id"])
                    continue
                try:
                    services.append(
                        PMMService.model_validate({"type": services_type, **service})
                    )
                except ValidationError:
                    if skip_failed:
                        if diagnostics is not None:
                            diagnostics.invalid_services += 1
                        self.logger.exception(
                            "Validation Error: Skipping service of type %s with data %s",
                            services_type,
                            service,
                        )
                    else:
                        raise
        return services

    @property
    def alerting_headers(self) -> dict[str, str]:
        """Return headers required for all PMM alerting API requests.

        The `X-Disable-Provenance` header ensures that items created through
        the API remain editable in the PMM UI.

        :return: A dictionary containing the alerting-specific request headers.
        """
        return {"X-Disable-Provenance": "true"}

    async def create_template(self, yaml: str) -> AlertTemplate | None:
        """Create a PMM alert template from a YAML definition.

        :param yaml: The YAML content of the alert template.
        :return: The created alert template, or ``None`` if the API returns no data
            (PMM v3).
        """
        if await self.is_older_than_v3():
            data = await self.post(
                "/v1/management/alerting/Templates/Create",
                json={"yaml": yaml},
                headers=self.alerting_headers,
            )
        else:
            data = await self.post(
                "/v1/alerting/templates",
                json={"yaml": yaml},
                headers=self.alerting_headers,
            )
        if not data:
            return None
        return AlertTemplate.model_validate(data)

    async def list_templates(
        self, page_size: int = 0, page_index: int = 0
    ) -> list[AlertTemplate]:
        """List all PMM alert templates.

        :param page_size: Number of templates per page. Defaults to 0 (no limit).
        :type page_size: int
        :param page_index: Page index for pagination. Defaults to 0.
        :type page_index: int
        :return: A list of alert templates.
        :rtype: list[AlertTemplate]
        """
        if await self.is_older_than_v3():
            data = await self.post(
                "/v1/management/alerting/Templates/List",
                json={},
                headers=self.alerting_headers,
            )
        else:
            params = {}
            if page_size:
                params["page_size"] = page_size
            if page_index:
                params["page_index"] = page_index
            data = await self.get(
                "/v1/alerting/templates",
                params=params,
                headers=self.alerting_headers,
            )
        return [AlertTemplate.model_validate(t) for t in data.get("templates", [])]

    async def template_exists(self, name: str) -> bool:
        """Check whether a PMM alert template with the given name exists.

        :param name: The template name to look up.
        :type name: str
        :return: True if a template with the given name exists, False otherwise.
        :rtype: bool
        """
        templates = await self.list_templates()
        return any(t.name == name for t in templates)

    async def create_rule(
        self,
        name: str,
        template_name: str,
        folder_uid: str,
        for_duration: str,
        group: str,
        labels: dict[str, str] | None = None,
        params: list[dict[str, Any]] | None = None,
    ) -> AlertRule | None:
        """Create a PMM alert rule from an existing template.

        :param name: The display name of the alert rule.
        :type name: str
        :param template_name: The name of the template to base the rule on.
        :type template_name: str
        :param folder_uid: The UID of the folder to place the rule in.
        :type folder_uid: str
        :param for_duration: How long the condition must be true before firing.
        :type for_duration: str
        :param group: The rule group name.
        :type group: str
        :param labels: Optional labels to attach to the rule.
        :type labels: dict[str, str] | None
        :param params: Optional template parameters for the rule.
        :type params: list[dict[str, Any]] | None
        :return: The created alert rule, or ``None`` if the API returns no
            data (PMM v3).
        :rtype: AlertRule | None
        """
        body = {
            "name": name,
            "template_name": template_name,
            "folder_uid": folder_uid,
            "for": for_duration,
            "group": group,
        }
        if labels is not None:
            body["labels"] = labels
        if params is not None:
            body["params"] = params

        if await self.is_older_than_v3():
            data = await self.post(
                "/v1/management/alerting/Rules/Create",
                json=body,
                headers=self.alerting_headers,
            )
        else:
            data = await self.post(
                "/v1/alerting/rules",
                json=body,
                headers=self.alerting_headers,
            )
        if not data:
            return None
        return AlertRule.model_validate(data)

    async def list_rules(self) -> list[AlertRule]:
        """List all PMM alert rules, flattening the nested folder/group structure.

        :return: A flat list of alert rules.
        :rtype: list[AlertRule]
        """
        data = await self.get(
            "/graph/api/ruler/grafana/api/v1/rules/",
            headers=self.alerting_headers,
        )
        return [
            AlertRule.model_validate(rule["grafana_alert"])
            for groups in data.values()
            for group in groups
            for rule in group.get("rules", [])
        ]

    async def delete_rule(self, uid: str) -> None:
        """Delete a PMM alert rule by its UID.

        Uses the raw `_request` context manager directly because the DELETE
        endpoint returns a 204 No Content response with no JSON body.

        :param uid: The UID of the rule to delete.
        :type uid: str
        """
        async with self._request(
            "DELETE",
            f"/graph/api/v1/provisioning/alert-rules/{uid}",
            headers=self.alerting_headers,
        ) as response:
            try:
                response.raise_for_status()
            except ClientResponseError as err:
                self._raise_upstream_no_body_error(response, err)

    async def update_rule(
        self,
        uid: str,
        name: str,
        template_name: str,
        folder_uid: str,
        for_duration: str,
        group: str,
        labels: dict[str, str] | None = None,
        params: list[dict[str, Any]] | None = None,
    ) -> AlertRule | None:
        """Update an existing PMM alert rule by deleting and recreating it.

        The PMM API has no native update endpoint for alert rules; the only
        supported pattern is to delete the existing rule and create a new one.

        :param uid: The UID of the existing rule to replace.
        :type uid: str
        :param name: The display name of the new rule.
        :type name: str
        :param template_name: The template to base the new rule on.
        :type template_name: str
        :param folder_uid: The folder UID to place the new rule in.
        :type folder_uid: str
        :param for_duration: How long the condition must hold before firing.
        :type for_duration: str
        :param group: The rule group name.
        :type group: str
        :param labels: Optional labels to attach to the new rule.
        :type labels: dict[str, str] | None
        :param params: Optional template parameters for the new rule.
        :type params: list[dict[str, Any]] | None
        :return: The newly created alert rule, or ``None`` if the API returns
            no data (PMM v3).
        :rtype: AlertRule | None
        """
        await self.delete_rule(uid)
        return await self.create_rule(
            name=name,
            template_name=template_name,
            folder_uid=folder_uid,
            for_duration=for_duration,
            group=group,
            labels=labels,
            params=params,
        )

    async def list_folders(self) -> list[Folder]:
        """List all Grafana folders available in PMM.

        :return: A list of Grafana folder objects.
        :rtype: list[Folder]
        """
        data = as_json_array(
            await self.get("/graph/api/folders/", headers=self.alerting_headers)
        )
        return [Folder.model_validate(f) for f in data]

    async def create_folder(self, title: str) -> Folder:
        """Create a new Grafana folder in PMM.

        :param title: The display title for the new folder.
        :type title: str
        :return: The created folder object.
        :rtype: Folder
        """
        data = await self.post(
            "/graph/api/folders/",
            json={"title": title},
            headers=self.alerting_headers,
        )
        return Folder.model_validate(data)

    async def list_contact_points(self) -> list[ContactPoint]:
        """List all alert contact points configured in PMM.

        :return: A list of contact point objects.
        :rtype: list[ContactPoint]
        """
        data = as_json_array(
            await self.get(
                "/graph/api/v1/provisioning/contact-points/",
                headers=self.alerting_headers,
            )
        )
        return [ContactPoint.model_validate(cp) for cp in data]

    async def create_contact_point(
        self, name: str, type_: str, settings: dict[str, Any]
    ) -> ContactPoint:
        """Create a new alert contact point in PMM.

        :param name: The display name of the contact point.
        :type name: str
        :param type_: The contact point type (e.g., "slack", "email").
        :type type_: str
        :param settings: Type-specific configuration settings.
        :type settings: dict[str, Any]
        :return: The created contact point.
        :rtype: ContactPoint
        """
        data = await self.post(
            "/graph/api/v1/provisioning/contact-points/",
            json={"name": name, "type": type_, "settings": settings},
            headers=self.alerting_headers,
        )
        return ContactPoint.model_validate(data)

    @staticmethod
    def _raise_upstream_no_body_error(
        response: ClientResponse, err: ClientResponseError
    ) -> NoReturn:
        """Translate an error from a no-JSON-body endpoint into a project exception.

        The provisioning PUT/DELETE endpoints return 202/204 with no body on
        success. On error, PMM itself answers with a Grafana JSON body
        (``{"message": ...}``), but a reverse proxy failing in front of PMM can
        return an HTML 404, which would otherwise map to
        :class:`HTTPNotFoundException` and be swallowed by restore callers that
        treat a 404 as "not provisioned". Inspect ``response.content_type`` and
        stamp ``UPSTREAM_NON_JSON_HEADER`` on a non-JSON error body so
        :func:`exception_for_status` keeps a proxy 404 as a bare
        :class:`fastapi.HTTPException`.

        :param response: The upstream error response.
        :param err: The :class:`ClientResponseError` raised by
            ``raise_for_status``.
        :raises HTTPException: Always -- the mapped project exception, or a bare
            HTTPException for a non-JSON body or an unmapped status.
        """
        headers = (
            None
            if response.content_type == "application/json"
            else {UPSTREAM_NON_JSON_HEADER: "1"}
        )
        raise exception_for_status(
            err.status, detail=err.message, headers=headers
        ) from None

    async def update_contact_point(
        self, uid: str, name: str, type_: str, settings: dict[str, Any]
    ) -> None:
        """Update an existing alert contact point in PMM.

        Use the raw `_request` context manager directly because the PUT
        endpoint returns a 202 Accepted response with no JSON body.

        :param uid: The UID of the contact point to update.
        :type uid: str
        :param name: The new display name.
        :type name: str
        :param type_: The contact point type.
        :type type_: str
        :param settings: Updated type-specific configuration settings.
        :type settings: dict[str, Any]
        """
        async with self._request(
            "PUT",
            f"/graph/api/v1/provisioning/contact-points/{uid}",
            json={"uid": uid, "name": name, "type": type_, "settings": settings},
            headers=self.alerting_headers,
        ) as response:
            try:
                response.raise_for_status()
            except ClientResponseError as err:
                self._raise_upstream_no_body_error(response, err)

    async def delete_contact_point(self, uid: str) -> None:
        """Delete an alert contact point by its UID.

        Use the raw ``_request`` context manager directly because the DELETE
        endpoint returns a 204 No Content response with no JSON body.

        :param uid: The UID of the contact point to delete.
        :type uid: str
        """
        async with self._request(
            "DELETE",
            f"/graph/api/v1/provisioning/contact-points/{uid}",
            headers=self.alerting_headers,
        ) as response:
            try:
                response.raise_for_status()
            except ClientResponseError as err:
                self._raise_upstream_no_body_error(response, err)

    async def get_notification_policy(self) -> NotificationPolicy:
        """Fetch the current notification policy tree from PMM.

        :return: The root notification policy.
        :rtype: NotificationPolicy
        """
        data = await self.get(
            "/graph/api/v1/provisioning/policies",
            headers=self.alerting_headers,
        )
        return NotificationPolicy.model_validate(data)

    async def update_notification_policy(self, policy: NotificationPolicy) -> None:
        """Replace the notification policy tree in PMM.

        :param policy: The new notification policy to apply.
        :type policy: NotificationPolicy
        """
        await self.put(
            "/graph/api/v1/provisioning/policies",
            json=policy.model_dump(exclude_none=True),
            headers=self.alerting_headers,
        )

    async def get_services_by_node_external_id(
        self,
        *,
        skip_failed: bool = True,
        filter_: Callable[[dict[str, Any]], bool] | None = None,
        diagnostics: PMMFetchDiagnostics | None = None,
    ) -> defaultdict[NonEmptyStr, list[PMMService]]:
        """Fetch and group services by node ID from the PMM API.

        Retrieve all services and organize them into a defaultdict where each key is a
        node ID and each value is a list of associated services.

        :param skip_failed: Whether to skip services that fail validation. Defaults to
            True.
        :param filter_: Optional callable that takes a service dict and returns True if
            the service should be included, False if it should be filtered out.
            Defaults to None.
        :param diagnostics: Optional record to accumulate what this fetch could not
            represent faithfully. Defaults to None, which records nothing.
        :return: A defaultdict mapping node IDs to lists of PMMService instances.
        """
        services_by_node_id = defaultdict(list)
        for service in await self.get_services(
            skip_failed=skip_failed,
            filter_=filter_,
            diagnostics=diagnostics,
        ):
            services_by_node_id[service.node_id].append(service)
        return services_by_node_id

    async def get_inventory_snapshot(
        self,
        node_type: str = "",
        *,
        skip_failed: bool = True,
        filter_: Callable[[dict[str, Any]], bool] | None = None,
    ) -> PMMInventorySnapshot:
        """Fetch nodes from the PMM API together with fetch-completeness diagnostics.

        Orphan detection runs against the **raw** node IDs, collected before the
        filter and before ``Node`` construction: an intentionally excluded node would
        otherwise make its own services look orphaned, and no generation would ever
        be complete.

        Only one direction of cross-list disagreement is detectable. A node present
        in the node list but missing from the service map is indistinguishable from a
        node that genuinely runs no services, so it is not reported.

        The diagnostics describe a full-estate fetch. ``node_type`` narrows the node
        list but not the service list, so every service belonging to an excluded
        node type is reported as an orphan.

        :param node_type: The type of nodes to retrieve (e.g., "generic"). Defaults to
            an empty string, meaning the field won't be used as a filter.
        :param skip_failed: Whether to skip nodes that fail validation. Defaults to
            True.
        :param filter_: Optional callable that takes a node or service dict and returns
            True if the item should be included, False if it should be filtered out.
            Used to filter nodes and, when loading services, each service.
            Defaults to None.
        :return: The fetched nodes paired with the diagnostics for this fetch.
        :raises ValidationError: If a node fails validation and ``skip_failed`` is False.
        """
        diagnostics = PMMFetchDiagnostics()
        services_by_node_id = await self.get_services_by_node_external_id(
            skip_failed=skip_failed,
            filter_=filter_,
            diagnostics=diagnostics,
        )
        # Snapshot the keys now: the node loop below reads the same defaultdict
        # through ``__getitem__``, which inserts empty entries as it goes.
        service_map_node_ids = set(services_by_node_id)
        params = remove_falsy_values_from_dict({"node_type": node_type})
        if await self.is_older_than_v3():
            nodes_data = await self.post(
                "/v1/inventory/Nodes/List",
                json=params,
            )
        else:
            nodes_data = await self.get("/v1/inventory/nodes", params=params)

        nodes: list[Node] = []
        fetched_node_ids: set[str] = set()
        for nodes_type, node_list in nodes_data.items():
            for node in node_list:
                node_id = node.get("node_id")
                if node_id:
                    fetched_node_ids.add(node_id)
                if filter_ is not None and not filter_(node):
                    self.logger.debug("Skipping node %s due to filter", node_id)
                    if node_id:
                        diagnostics.filtered_node_ids.add(node_id)
                    continue
                try:
                    nodes.append(
                        Node(
                            **node,
                            source=SourceEnum.PMM,
                            type=nodes_type,
                            services=services_by_node_id[node_id],
                        )
                    )
                except ValidationError:
                    if not skip_failed:
                        raise
                    diagnostics.invalid_nodes += 1
                    self.logger.exception(
                        "Failed to validate node of type %s with data: %s",
                        nodes_type,
                        node,
                    )
        diagnostics.orphan_service_node_ids = sorted(
            node_id
            for node_id in service_map_node_ids
            if node_id not in fetched_node_ids
        )
        return PMMInventorySnapshot(nodes=nodes, diagnostics=diagnostics)

    async def get_nodes(
        self,
        node_type: str = "",
        *,
        skip_failed: bool = True,
        filter_: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[Node]:
        """Fetch nodes from the PMM API.

        Retrieve a list of nodes filtered by node type and associate them with their
        services.

        :param node_type: The type of nodes to retrieve (e.g., "generic"). Defaults to
            an empty string, meaning the field won't be used as a filter.
        :param skip_failed: Whether to skip nodes that fail validation. Defaults to
            True.
        :param filter_: Optional callable that takes a node or service dict and returns
            True if the item should be included, False if it should be filtered out.
            Used to filter nodes and, when loading services, each service.
            Defaults to None.
        :return: A list of Node instances retrieved from the API.
        :raises ValidationError: If a node fails validation and ``skip_failed`` is False.
        """
        snapshot = await self.get_inventory_snapshot(
            node_type,
            skip_failed=skip_failed,
            filter_=filter_,
        )
        return snapshot.nodes

    async def get_advisor_checks(self) -> list[dict[str, Any]]:
        """List all advisor checks (enabled and disabled).

        :return: A list of check dictionaries.
        """
        if await self.is_older_than_v3():
            data = await self.post("/v1/management/SecurityChecks/List", json={})
        else:
            data = await self.get("/v1/advisors/checks")
        return data.get("checks", [])

    async def get_failed_advisor_checks(self) -> list[dict[str, Any]]:
        """List advisor checks that have failed.

        :return: A list of failed check result dictionaries.
        """
        if await self.is_older_than_v3():
            data = await self.post(
                "/v1/management/SecurityChecks/FailedChecks", json={}
            )
        else:
            data = await self.get("/v1/advisors/checks/failed")
        return data.get("results", [])

    async def start_advisor_checks(
        self, names: list[str] | None = None
    ) -> dict[str, Any]:
        """Trigger execution of advisor checks.

        :param names: Optional list of check names to run. When ``None``, all
            checks are started.
        :return: The API response.
        """
        payload = {"names": names} if names else {}
        if await self.is_older_than_v3():
            return as_json_object(
                await self.post("/v1/management/SecurityChecks/Start", json=payload)
            )
        return as_json_object(
            await self.post("/v1/advisors/checks:start", json=payload)
        )

    async def get_grafana_annotations(
        self,
        *,
        type_: str = "alert",
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch Grafana annotations, paginating until all results are retrieved.

        :param type_: Annotation type filter. Defaults to ``"alert"``.
        :type type_: str
        :param from_ts: Start timestamp in milliseconds.
        :type from_ts: int | None
        :param to_ts: End timestamp in milliseconds.
        :type to_ts: int | None
        :param limit: Page size for each request. Defaults to 100.
        :type limit: int
        :return: All matching annotations.
        :rtype: list[dict[str, Any]]
        """
        all_annotations: list[dict[str, Any]] = []
        current_limit = limit
        last_item: dict[str, Any] | None = None

        while True:
            params: dict[str, Any] = {"type": type_, "limit": current_limit}
            if from_ts is not None:
                params["from"] = from_ts
            if to_ts is not None:
                params["to"] = to_ts

            annotations = await self.get("/graph/api/annotations", params=params)
            if not annotations:
                break
            all_annotations = annotations
            new_last = annotations[-1]
            if new_last == last_item or len(annotations) < current_limit:
                break
            last_item = new_last
            current_limit += limit

        return all_annotations

    async def query_grafana_datasource(
        self,
        expressions: list[str],
        *,
        datasource_uid: str,
        datasource_id: int,
        from_ts: int | str,
        to_ts: int | str,
        max_data_points: int = 1000,
        instant: bool = True,
        range_: bool = False,
    ) -> dict[str, Any]:
        """Execute a Grafana datasource query with one or more PromQL expressions.

        :param expressions: List of PromQL expressions (one per refId A, B, C ...).
        :type expressions: list[str]
        :param datasource_uid: UID of the Grafana datasource.
        :type datasource_uid: str
        :param datasource_id: Numeric ID of the Grafana datasource.
        :type datasource_id: int
        :param from_ts: Start of the query range (milliseconds or relative string).
        :type from_ts: int | str
        :param to_ts: End of the query range.
        :type to_ts: int | str
        :param max_data_points: Maximum data points per query. Defaults to 1000.
        :type max_data_points: int
        :param instant: Whether queries are instant. Defaults to True.
        :type instant: bool
        :param range_: Whether queries are range queries. Defaults to False.
        :type range_: bool
        :return: The ``results`` dict keyed by refId (A, B, ...).
        :rtype: dict[str, Any]
        """
        ref_ids = [chr(ord("A") + i) for i in range(len(expressions))]
        queries = [
            {
                "refId": ref_id,
                "datasource": {"uid": datasource_uid},
                "expr": expr,
                "instant": instant,
                "range": range_,
                "format": "table",
                "datasourceId": datasource_id,
                "maxDataPoints": max_data_points,
            }
            for ref_id, expr in zip(ref_ids, expressions, strict=True)
        ]
        payload = {
            "queries": queries,
            "from": str(from_ts),
            "to": str(to_ts),
        }
        data = await self.post("/graph/api/ds/query", json=payload)
        return data.get("results", {})

    async def get_grafana_datasources(self) -> list[dict[str, Any]]:
        """List all Grafana datasources.

        :return: A list of datasource dictionaries.
        :rtype: list[dict[str, Any]]
        """
        return as_json_array(await self.get("/graph/api/datasources"))

    async def get_inventory_services_with_agents(self) -> list[dict[str, Any]]:
        """List services including agent status, for the management endpoint.

        :return: A list of service dicts with nested ``agents`` arrays.
        :rtype: list[dict[str, Any]]
        """
        if await self.is_older_than_v3():
            data = await self.post("/v1/management/Service/List", json={})
        else:
            data = await self.get("/v1/management/services")
        return data.get("services", [])
