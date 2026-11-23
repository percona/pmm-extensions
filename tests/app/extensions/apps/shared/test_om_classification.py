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

"""Test fact classification: secrets never ingest, the config hash is redacted."""

import hashlib
import json

from app.extensions.apps.shared.om.classification import (
    config_sha256,
    FactClass,
    ingestible_facts,
    redacted_config,
    sensitive_keys,
)

#: A distinctive value stood in for a secret, searched for across every sink the
#: classification produces. It must survive in none of them.
CANARY = "CANARY-9b3e1d7a-secret-value"


class TestIngestibleFacts:
    """Keep public and sensitive facts, drop secret and unclassified ones."""

    def test_keeps_public_and_sensitive_drops_secret(self) -> None:
        """Drop the secret fact and keep the public and sensitive ones."""
        facts = {"fcv": "7.0", "config_sha256": "abc", "admin_password": CANARY}
        classes = {
            "fcv": FactClass.PUBLIC,
            "config_sha256": FactClass.SENSITIVE,
            "admin_password": FactClass.SECRET,
        }
        kept = ingestible_facts(facts, classes)
        assert kept == {"fcv": "7.0", "config_sha256": "abc"}

    def test_drops_an_unclassified_fact_fail_closed(self) -> None:
        """Drop a fact with no declared class rather than leak it."""
        kept = ingestible_facts({"mystery": CANARY}, {})
        assert kept == {}

    def test_drops_a_secret_derived_hash_when_classified_secret(self) -> None:
        """Drop a hash of a secret when it is itself classified secret."""
        facts = {"uri_hash": hashlib.sha256(CANARY.encode()).hexdigest()}
        kept = ingestible_facts(facts, {"uri_hash": FactClass.SECRET})
        assert kept == {}


def test_a_planted_secret_reaches_no_ingested_sink() -> None:
    """Keep a planted secret out of the ingested facts and the config hash."""
    options = {"net.port": 27017, "security.keyFile": CANARY}
    facts = {
        "replset": "rs0",
        "pbm_uri_password": CANARY,
        "config_sha256": config_sha256(redacted_config(options, {"net.port"})),
    }
    classes = {
        "replset": FactClass.PUBLIC,
        "pbm_uri_password": FactClass.SECRET,
        "config_sha256": FactClass.SENSITIVE,
    }

    kept = ingestible_facts(facts, classes)

    assert kept["replset"] == "rs0", "the public fact must survive ingestion"
    assert "pbm_uri_password" not in kept
    assert CANARY not in json.dumps(kept)
    assert CANARY not in kept["config_sha256"]


class TestRedactedConfig:
    """Project a config to its allowlisted options and hash only those."""

    def test_keeps_only_allowlisted_options(self) -> None:
        """Keep the allowlisted option and drop the secret-bearing one."""
        options = {"net.port": 27017, "security.keyFile": CANARY}
        projection = redacted_config(options, {"net.port"})
        assert projection == {"net.port": 27017}
        assert CANARY not in json.dumps(projection)

    def test_hash_is_over_the_redacted_projection(self) -> None:
        """Compute the hash over the projection, so a redacted-out change is invisible."""
        allowed = {"net.port"}
        with_secret = {"net.port": 27017, "security.keyFile": CANARY}
        without_secret = {"net.port": 27017}
        assert config_sha256(redacted_config(with_secret, allowed)) == config_sha256(
            redacted_config(without_secret, allowed)
        )

    def test_hash_changes_when_an_allowlisted_option_changes(self) -> None:
        """Change the hash when a kept option changes."""
        allowed = {"net.port"}
        assert config_sha256(
            redacted_config({"net.port": 27017}, allowed)
        ) != config_sha256(redacted_config({"net.port": 27018}, allowed))


def test_sensitive_keys_names_the_admin_only_facts() -> None:
    """Return only the fact names classified sensitive."""
    classes = {
        "fcv": FactClass.PUBLIC,
        "users": FactClass.SENSITIVE,
        "keyfile": FactClass.SECRET,
    }
    assert sensitive_keys(classes) == frozenset({"users"})
