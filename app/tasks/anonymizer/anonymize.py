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

"""Define the main anonymizing functions."""

__all__ = ["anonymize_text"]

import functools
import threading
from typing import Any, TYPE_CHECKING

from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.anonymizer.entities import PIIEntity

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngine
    from presidio_anonymizer import AnonymizerEngine, OperatorConfig


@functools.cache
def _default_operator_config() -> "OperatorConfig":
    """Build the default Presidio anonymizer operator config.

    The ``presidio_anonymizer`` import is deferred to first call so it stays off
    the module-import path that the backend and Celery worker pay on every
    startup, even when no text is ever anonymized.

    :return: The default operator configuration used by the anonymizer engine.
    """
    from presidio_anonymizer import OperatorConfig  # noqa: PLC0415

    return OperatorConfig("replace", {})


def _anonymizer_operators() -> dict[str, "OperatorConfig"]:
    """Return the operator configuration mapping used by the anonymizer engine."""
    return {"DEFAULT": _default_operator_config()}


class PresidioEngineManager:
    """Manage instances of Presidio's NLP, Analyzer, and Anonymizer engines."""

    def __init__(self) -> None:
        self._nlp_engine = None
        self._analyzer_engine = None
        self._anonymizer_engine = None
        self._engine_lock = threading.RLock()

    @property
    def nlp_engine(self) -> "NlpEngine":
        """Initialize and return the NLP engine.

        This property initializes the NLP engine if it hasn't been created yet,
        using the configuration defined in the `_build_nlp_config` method. It ensures
        thread-safe initialization using a reentrant lock.

        :return: The NLP engine instance.
        :rtype: NlpEngine
        """
        if self._nlp_engine is None:
            with self._engine_lock:
                if self._nlp_engine is None:
                    from presidio_analyzer.nlp_engine import (  # noqa: PLC0415
                        NlpEngineProvider,
                    )

                    nlp_provider = NlpEngineProvider(
                        nlp_configuration=self._build_nlp_config()
                    )
                    self._nlp_engine = nlp_provider.create_engine()
        return self._nlp_engine

    @property
    def analyzer(self) -> "AnalyzerEngine":
        """Initialize and return the Analyzer engine.

        This property initializes the Analyzer engine if it hasn't been created yet,
        using the NLP engine and supported languages defined in the settings. It ensures
        thread-safe initialization using a reentrant lock.

        :return: The Analyzer engine instance.
        :rtype: AnalyzerEngine
        """
        if self._analyzer_engine is None:
            with self._engine_lock:
                if self._analyzer_engine is None:
                    from presidio_analyzer import AnalyzerEngine  # noqa: PLC0415

                    self._analyzer_engine = AnalyzerEngine(
                        nlp_engine=self.nlp_engine,
                        supported_languages=list(anonymizer_settings.NLP_MODELS),
                    )
        return self._analyzer_engine

    @property
    def anonymizer(self) -> "AnonymizerEngine":
        """Initialize and return the Anonymizer engine.

        This property initializes the Anonymizer engine if it hasn't been created yet.
        It ensures thread-safe initialization using a reentrant lock.

        :return: The Anonymizer engine instance.
        :rtype: AnonymizerEngine
        """
        if self._anonymizer_engine is None:
            with self._engine_lock:
                if self._anonymizer_engine is None:
                    from presidio_anonymizer import AnonymizerEngine  # noqa: PLC0415

                    self._anonymizer_engine = AnonymizerEngine()
        return self._anonymizer_engine

    @staticmethod
    def _build_nlp_config() -> dict[str, Any]:
        """Build the NLP engine configuration.

        This method constructs the NLP engine configuration dictionary based on the
        models specified in the anonymizer settings.

        :return: A dictionary containing the NLP engine configuration.
        :rtype: dict[str, Any]
        """
        return {
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": lang, "model_name": model}
                for lang, model in anonymizer_settings.NLP_MODELS.items()
            ],
        }


engine_manager = PresidioEngineManager()


def anonymize_text(
    text: str, selected_entities: set[PIIEntity], language: str | None = None
) -> str:
    """Anonymize text using Microsoft Presidio.

    :param text: The original log text.
    :type text: str
    :param selected_entities: A set of selected anonymizer entities to apply.
    :type selected_entities: set[PIIEntity]
    :param language: The language code of the text. If None, the default language from
        settings will be used. Defaults to None.
    :type language: str | None
    :return: The encrypted log text.
    :rtype: str
    """
    if selected_entities:
        if language is None:
            language = anonymizer_settings.default_analyzer_language
        results = engine_manager.analyzer.analyze(
            text=text,
            entities=[entity.name for entity in selected_entities],
            language=language,
        )
        if results:
            anonymized_result = engine_manager.anonymizer.anonymize(
                text, results, _anonymizer_operators()
            )
            return anonymized_result.text
    return text
