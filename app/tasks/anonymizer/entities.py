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

"""Define an enumeration for PII types and related utilities."""

from enum import IntEnum

from app.core.utils.fields import EnumFieldMixin

__all__ = ["PIIEntity"]


class PIIEntity(EnumFieldMixin, IntEnum):
    """Enumeration of sensitive data types represented as bitmask flags.

    Each member value is a bit-shifted integer that uniquely identifies a
    particular sensitive data type.
    """

    CREDIT_CARD = 1 << 0
    EMAIL_ADDRESS = 1 << 1
    IBAN_CODE = 1 << 2
    IP_ADDRESS = 1 << 3
    NRP = 1 << 4
    LOCATION = 1 << 5
    PERSON = 1 << 6
    PHONE_NUMBER = 1 << 7
    MEDICAL_LICENSE = 1 << 8
    US_BANK_NUMBER = 1 << 9
    US_DRIVER_LICENSE = 1 << 10
    US_ITIN = 1 << 11
    US_PASSPORT = 1 << 12
    US_SSN = 1 << 13

    @classmethod
    def encode_selection(cls, selected_entities: set[PIIEntity]) -> int:
        """Encode a set of :class:`PIIEntity` values into an integer bitmask.

        :param selected_entities: A set of :class:`PIIEntity` members to encode.
        :type selected_entities: set[PIIEntity]
        :return: The integer bitmask representing the selected entities.
        :rtype: int
        """
        selected_entities = set(selected_entities)
        number = 0
        for entity in selected_entities:
            number |= entity
        return number

    @classmethod
    def decode_selection(cls, mask: int) -> set[PIIEntity]:
        """Decode an integer bitmask into a set of :class:`PIIEntity` values.

        :param mask: The integer bitmask to decode.
        :type mask: int
        :return: A set of :class:`PIIEntity` members corresponding to the
            provided bitmask.
        :rtype: set[PIIEntity]
        """
        return {entity for entity in cls if mask & entity}
