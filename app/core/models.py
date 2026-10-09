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

"""Define core models for all apps."""

from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.utils import to_uppercase, transform_dict_keys
from app.core.utils.strings import lower_if_string

__all__ = [
    "BaseCaseInsensitiveModel",
    "BaseLowercaseModel",
    "BaseTransformFieldsModel",
]


class BaseTransformFieldsModel(BaseModel):
    """A base model that applies transformation to all fields pre-validation.

    This model uses `transform_dict_keys()` to apply transformation to all fields in
    the input data, if the data is a dictionary, using the class variables
    `TRANSFORM_CALLABLE` and `TRANSFORM_DEEP` as parameters.

    :cvar TRANSFORM_CALLABLE: Represent the parameter `transform` in
        `transform_dict_keys()`.
    :vartype TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]]
    :cvar TRANSFORM_DEEP: Represent the parameter `deep` in `transform_dict_keys()`.
        Defaults to False.
    :vartype TRANSFORM_DEEP: ClassVar[bool]
    """

    TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]]
    TRANSFORM_DEEP: ClassVar[bool] = False

    @model_validator(mode="before")
    @classmethod
    def transform_fields(cls, data: Any) -> Any:
        """Transform all keys in input data before validation, if the data is a dict.

        :param data: The input data to be validated, typically a dictionary.
        :type data: Any
        :return: The transformed data with all keys transformed.
        :rtype: Any
        """
        if isinstance(data, dict):
            return transform_dict_keys(
                data, cls.TRANSFORM_CALLABLE, deep=cls.TRANSFORM_DEEP
            )
        return data


class BaseCaseInsensitiveModel(BaseTransformFieldsModel):
    """A base model with case-insensitive alias generation.

    This model uses a custom alias generator that converts field names to uppercase.
    It also extends `BaseTransformFieldsModel` to make all fields uppercase
    pre-validation.

    :cvar TRANSFORM_CALLABLE: Represent the parameter `transform` in
        `transform_dict_keys()`. Set to a function to uppercase a string.
    :vartype TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]]
    :cvar TRANSFORM_DEEP: Represent the parameter `deep` in `transform_dict_keys()`.
        Defaults to False.
    :vartype TRANSFORM_DEEP: ClassVar[bool]
    """

    model_config = ConfigDict(alias_generator=to_uppercase)
    TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]] = lambda v: (
        v.upper() if isinstance(v, str) else v
    )


class BaseLowercaseModel(BaseTransformFieldsModel):
    """Define a base model that ensures all dictionary keys (recursively) are lowercase.

    This model extends `BaseTransformFieldsModel` to make all fields recursively
    lowercase pre-validation.

    :cvar TRANSFORM_CALLABLE: Represent the parameter `transform` in
        `transform_dict_keys()`. Set to a function to lowercase a string.
    :vartype TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]]
    :cvar TRANSFORM_DEEP: Represent the parameter `deep` in `transform_dict_keys()`.
        Set to True.
    :vartype TRANSFORM_DEEP: ClassVar[bool]
    """

    TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]] = lower_if_string
    TRANSFORM_DEEP: ClassVar[bool] = True
