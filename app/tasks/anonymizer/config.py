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

"""Define settings for data anonymization in the tasks app."""

from collections import defaultdict
from typing import Any, ClassVar

from pydantic import Field, field_validator

from app.core.config import BaseYamlSettings
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field, materialize_via_owning_model
from app.core.utils import run_pydantic_type_validator
from app.tasks.anonymizer.entities import PIIEntity


class AnonymizerSettings(BaseYamlSettings):
    """Define settings for task data anonymization.

    The module-level ``anonymizer_settings`` is wrapped in an
    :class:`OverridableSettingsProxy`, so its underlying instance is constructed
    and validated lazily on first attribute access rather than at import. The
    Tasks Celery worker's ``start_settings_override_refresher`` handler calls
    ``anonymizer_settings._resolve()`` at ``worker_process_init`` so a bad
    config surfaces in the worker log at child start rather than mid-task --
    Celery absorbs what a signal receiver raises, so the child still starts.
    Removing that call regresses to lazy validation.

    :cvar SETTINGS_PREFIXES: The prefixes for anonymizer related settings in the
        configuration file. Set to ``["TASKS", "ANONYMIZER"]``.
    :param DEFAULT_ENTITIES: A mapping of task owners to sets of anonymizer entities.
        If set to ``"*"``, defaults to all available anonymizer entities for all owners.
        If a list of anonymizer entities is provided, it will be used as default for all
        owners. Defaults to an empty set.
    :param NLP_MODELS: A mapping of language codes to spaCy model names for NLP
        processing. Defaults to ``{"en": "en_core_web_sm"}``.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["TASKS", "ANONYMIZER"]
    DEFAULT_ENTITIES: defaultdict[str, set[PIIEntity]] = hot_field(
        defaultdict(set),
        materializer=materialize_via_owning_model,
        validate_default=True,
    )
    NLP_MODELS: dict[str, str] = Field({"en": "en_core_web_sm"}, min_length=1)

    @field_validator("DEFAULT_ENTITIES", mode="before")
    @classmethod
    def _validate_entities_defaultdict(cls, v: Any) -> Any:
        """Validate and transform the DEFAULT_ENTITIES field.

        :param v: The value to be validated.
        :return: A defaultdict with task-owner keys and sets of PIIEntity as
            values.
        """
        if v == "*":
            v = list(PIIEntity)
        if isinstance(v, list):
            entities = run_pydantic_type_validator(set[PIIEntity], v)
            return defaultdict(lambda: entities)
        if isinstance(v, dict):
            return defaultdict(set, v)
        return v

    @property
    def default_analyzer_language(self) -> str:
        """Get the default language code for the NLP models.

        :return: The first language code from the NLP_MODELS dictionary.
        :rtype: str
        """
        return next(iter(self.NLP_MODELS))

    def get_anonymize_mask(self, owner: str) -> int:
        """Get the anonymization mask for a given task owner.

        :param owner: The task owner identifier.
        :type owner: str
        :return: The integer bitmask representing the anonymization entities for the
            specified owner.
        :rtype: int
        """
        return PIIEntity.encode_selection(self.DEFAULT_ENTITIES[owner])


anonymizer_settings: AnonymizerSettings = OverridableSettingsProxy(AnonymizerSettings)
