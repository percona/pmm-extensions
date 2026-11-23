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

"""Classify a scan's facts so no secret leaves the host.

Every fact a scan produces is public, sensitive, or secret. A secret value, and
any hash derived from one, is dropped on the host before ingestion - never
hashed-and-kept - so it never reaches ``om_observation``, its digest, or any
observable sink. The classification is fail-closed: a fact with no declared class
is treated as secret and dropped, so a fact added without a class cannot leak.

A config file is secret-bearing as a whole, so its stored projection is an
allowlist of non-secret options and its ``config_sha256`` is computed over that
projection, not the raw file.
"""

import hashlib
import json
from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Any

__all__ = [
    "FactClass",
    "config_sha256",
    "ingestible_facts",
    "redacted_config",
    "sensitive_keys",
]


class FactClass(StrEnum):
    """Name how exposable a scan fact is.

    The value space the scan declares a fact's class against and the API gates
    read access by: a secret fact never persists, a sensitive one persists but is
    admin-only on read, a public one is readable by anyone who may read the host.

    :cvar PUBLIC: Readable by any caller who may read the host.
    :cvar SENSITIVE: Persisted but admin-only on read.
    :cvar SECRET: Never persisted; dropped on the host before ingestion.
    """

    PUBLIC = "public"
    SENSITIVE = "sensitive"
    SECRET = "secret"  # noqa: S105 # nosec B105


def ingestible_facts(
    facts: Mapping[str, Any], classes: Mapping[str, FactClass]
) -> dict[str, Any]:
    """Drop every secret and unclassified fact, keeping public and sensitive ones.

    Fail-closed: a key absent from ``classes`` is treated as
    :attr:`FactClass.SECRET`, so a fact added without a declared class is dropped
    rather than leaked.

    :param facts: The facts a scan collected, keyed by fact name.
    :param classes: The declared class of each fact name.
    :return: The subset safe to ingest into ``om_observation``.
    """
    return {
        key: value
        for key, value in facts.items()
        if classes.get(key, FactClass.SECRET) is not FactClass.SECRET
    }


def sensitive_keys(classes: Mapping[str, FactClass]) -> frozenset[str]:
    """Return the fact names that persist but are admin-only on read.

    :param classes: The declared class of each fact name.
    :return: The names classified :attr:`FactClass.SENSITIVE`.
    """
    return frozenset(key for key, cls in classes.items() if cls is FactClass.SENSITIVE)


def redacted_config(
    options: Mapping[str, Any], allowed: Collection[str]
) -> dict[str, Any]:
    """Project a config to its allowlisted, non-secret options.

    An allowlist, not a denylist: the raw config is secret-bearing, so only the
    options named in ``allowed`` are kept and a secret-bearing option added later
    stays out by default.

    :param options: The config's options in effect.
    :param allowed: The non-secret option names to keep.
    :return: The projection safe to store and to hash.
    """
    allow = frozenset(allowed)
    return {key: value for key, value in options.items() if key in allow}


def config_sha256(redacted: Mapping[str, Any]) -> str:
    """Hash a config's redacted projection canonically.

    Computed over :func:`redacted_config`'s output, not the raw file, so the hash
    carries no secret and a change to a redacted-out option is not reported as a
    change.

    :param redacted: The allowlisted projection from :func:`redacted_config`.
    :return: The hex SHA-256 of the projection's canonical JSON.
    """
    canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
