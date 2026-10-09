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

"""Tests for selecting the xtrabackup payload variant from the upload selection.

``xtrabackup_payload`` carries all three upload-provider classes and sits against
the 16 KiB Nomad dispatch limit, so each upload selection dispatches a variant
carrying only the providers it can reach. These tests pin the selection for all
eight selections, that ``boto3`` is requested only when an S3 provider ships, and
that the other two backup types are untouched by any of it.
"""

import ast
import sys
from pathlib import Path

import pytest
import yaml

from app.core.utils.path import resolve_payload_reference
from app.extensions.apps.mysql_backups.forms import UploadProvider
from app.extensions.apps.mysql_backups.payload_variants import PROVIDERS
from tests.app.extensions.apps.mysql_backups.variant_specs import spec_for

# Every upload selection, and the payload the dispatch must carry for it. The
# all-providers selection is the canonical payload, which is the complete source,
# not a generated variant.
_SELECTIONS = [
    ([], "xtrabackup_noupload_payload"),
    (["RSYNC"], "xtrabackup_rsync_payload"),
    (["S3"], "xtrabackup_s3_payload"),
    (["GSUTIL"], "xtrabackup_gsutil_payload"),
    (["RSYNC", "S3"], "xtrabackup_rsync_s3_payload"),
    (["RSYNC", "GSUTIL"], "xtrabackup_rsync_gsutil_payload"),
    (["S3", "GSUTIL"], "xtrabackup_s3_gsutil_payload"),
    (["RSYNC", "S3", "GSUTIL"], "xtrabackup_payload"),
]


class TestVariantSelection:
    """Assert each upload selection dispatches the payload carrying its providers."""

    @pytest.mark.parametrize(("upload", "expected"), _SELECTIONS, ids=str)
    def test_selection_picks_its_variant(
        self, upload: list[str], expected: str
    ) -> None:
        """Assert the selection resolves to the variant named for those providers."""
        assert spec_for(upload).payload.endswith(f"/{expected}")

    @pytest.mark.parametrize(("upload", "expected"), _SELECTIONS, ids=str)
    def test_selected_variant_exists_on_disk(
        self, upload: list[str], expected: str
    ) -> None:
        """Assert every selection resolves to a payload file that actually ships."""
        assert resolve_payload_reference(spec_for(upload).payload).is_file()

    def test_provider_order_in_the_form_does_not_change_the_variant(self) -> None:
        """Assert the variant is keyed on the set of providers, not the form's order."""
        forward = spec_for(["RSYNC", "S3"]).payload
        reverse = spec_for(["S3", "RSYNC"]).payload
        assert forward == reverse

    def test_every_selection_maps_to_a_distinct_payload(self) -> None:
        """Assert no two selections collide on one variant, which would ship dead code."""
        payloads = [spec_for(upload).payload for upload, _ in _SELECTIONS]
        assert len(set(payloads)) == len(_SELECTIONS)


class TestBoto3Requirement:
    """Assert ``boto3`` is requested exactly when the dispatched variant needs it."""

    @pytest.mark.parametrize("upload", [upload for upload, _ in _SELECTIONS], ids=str)
    def test_boto3_tracks_the_s3_provider(self, upload: list[str]) -> None:
        """Assert boto3 ships only for a variant carrying the S3 provider."""
        requirements = spec_for(upload).requirements.splitlines()
        assert ("boto3" in requirements) is ("S3" in upload)

    def test_non_s3_variant_keeps_the_other_requirements(self) -> None:
        """Assert dropping boto3 leaves the rest of the requirement set intact."""
        requirements = spec_for(["RSYNC"]).requirements.splitlines()
        assert set(requirements) == {
            "packaging",
            "PyYAML",
            "PyMySQL[rsa,ed25519]",
            "filelock",
        }


class TestOtherBackupTypesUnaffected:
    """Assert only the xtrabackup payload is variant-selected."""

    @pytest.mark.parametrize(
        ("backup_type", "expected"),
        [("M", "mydumper_payload"), ("B", "binlog_payload")],
    )
    def test_payload_is_not_variant_selected(
        self, backup_type: str, expected: str
    ) -> None:
        """Assert mydumper and binlog keep their single payload for any selection."""
        assert spec_for(["RSYNC"], backup_type).payload.endswith(f"/{expected}")

    @pytest.mark.parametrize("backup_type", ["M", "B"])
    def test_boto3_still_ships(self, backup_type: str) -> None:
        """Assert both payloads keep boto3, which each imports regardless of upload."""
        assert "boto3" in spec_for([], backup_type).requirements.splitlines()


class TestNamingRule:
    """Assert the shared naming rule still lines up with the form's enum."""

    def test_provider_slugs_match_the_form_enum(self) -> None:
        """Assert filename slugs and their order track ``UploadProvider``.

        ``PROVIDERS`` is stdlib-only so the generator can read it without standing
        up the app, which leaves the enum free to drift away from it. A renamed or
        reordered member would silently dispatch a payload that does not exist.
        """
        assert tuple(provider.value for provider in UploadProvider) == PROVIDERS


def _upload_provider_map_keys(payload: Path) -> set[str]:
    """Return the keys of the ``upload_providers`` dict inside a payload file.

    Read from the file's AST rather than by importing it: the payloads carry
    runtime-only dependencies and re-exec themselves under ``sudo``.

    :param payload: The payload file to read.
    :return: The provider names the payload dispatches uploads on.
    :raises AssertionError: When the payload declares no ``upload_providers`` dict.
    """
    tree = ast.parse(payload.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "upload_providers" not in targets:
            continue
        return {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    raise AssertionError(f"{payload.name} declares no upload_providers dict")


#: The module each pip distribution in the requirement list provides. A payload
#: importing anything outside this map's values has no requirement declaring it.
_DISTRIBUTION_MODULES = {
    "packaging": {"packaging"},
    "PyYAML": {"yaml"},
    "PyMySQL[rsa,ed25519]": {"pymysql"},
    "filelock": {"filelock"},
    "boto3": {"boto3", "botocore"},
}


def _module_level_import_roots(payload: Path) -> set[str]:
    """Return the root module names a payload imports at module scope.

    Only module-scope imports matter: those run the moment the task starts, before
    any config is read, so a missing distribution fails the task outright. The walk
    descends into ``try`` and ``if`` bodies, because guarding an import with
    ``except ImportError`` is the idiomatic way to make a dependency optional and
    such an import still runs at module scope. It stops at function and class
    bodies, whose imports run only once something calls in.

    :param payload: The payload file to read.
    :return: The root names of its module-level imports.
    """
    roots: set[str] = set()
    pending: list[ast.AST] = [ast.parse(payload.read_text(encoding="utf-8"))]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        pending.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            )
        )
    return roots


class TestDispatchedVariantCarriesItsProviders:
    """Assert the dispatched payload can reach every provider its own config names.

    The payload looks its provider up in a dict keyed by the strings the config's
    ``UPLOAD`` list carries. Those two sides live in different files and no other
    test spans them, so a rename on either one dispatches a payload that raises
    ``Unsupported upload type`` only after the backup has been taken.
    """

    @pytest.mark.parametrize("upload", [upload for upload, _ in _SELECTIONS], ids=str)
    def test_config_upload_values_are_all_dispatchable(self, upload: list[str]) -> None:
        """Assert every provider the config names is a key of the payload's own map."""
        spec = spec_for(upload)
        payload = resolve_payload_reference(spec.payload)
        declared = yaml.safe_load(spec.config)["SERVER_LIST"][0].get("UPLOAD") or []
        keys = _upload_provider_map_keys(payload)
        assert {value.casefold() for value in declared} <= keys

    @pytest.mark.parametrize("upload", [upload for upload, _ in _SELECTIONS], ids=str)
    def test_map_carries_no_provider_the_config_cannot_name(
        self, upload: list[str]
    ) -> None:
        """Assert a stripped provider leaves no dispatch entry behind in the variant."""
        payload = resolve_payload_reference(spec_for(upload).payload)
        selected = {provider.casefold() for provider in upload}
        for provider in PROVIDERS:
            if provider in selected:
                continue
            assert provider not in _upload_provider_map_keys(payload)

    def test_legacy_capitalised_provider_still_dispatches(self) -> None:
        """Assert the map lookup tolerates the capitalisation older configs recorded.

        Configs written before the provider names were lowercased carry ``"Rsync"``
        and ``"S3"``. Those tasks keep their stored payload reference, so they reach
        this map and must not start failing on a case difference.
        """
        payload = resolve_payload_reference(spec_for(["RSYNC", "S3", "GSUTIL"]).payload)
        keys = _upload_provider_map_keys(payload)
        for legacy in ("Rsync", "S3", "GS", "GSUTIL"):
            assert legacy.casefold() in keys, legacy


class TestRequirementsCoverImports:
    """Assert every module a variant imports is installed by the task that runs it.

    ``build_backup_spec`` declares the pip requirements and the payload declares the
    imports. Nothing else compares them, so a module moved out of a provider region
    ships in variants whose tasks never install it.
    """

    @pytest.mark.parametrize("upload", [upload for upload, _ in _SELECTIONS], ids=str)
    def test_every_third_party_import_is_declared(self, upload: list[str]) -> None:
        """Assert the variant imports no third-party module its requirements omit."""
        spec = spec_for(upload)
        payload = resolve_payload_reference(spec.payload)
        available: set[str] = set()
        for line in spec.requirements.splitlines():
            available |= _DISTRIBUTION_MODULES.get(line.strip(), set())
        third_party = {
            root
            for root in _module_level_import_roots(payload)
            if root not in sys.stdlib_module_names
        }
        assert third_party <= available

    def test_every_requirement_maps_to_a_known_module(self) -> None:
        """Assert the distribution map covers every requirement the dispatcher declares.

        Without this the coverage check above passes vacuously for a new
        requirement, since an unmapped line contributes no module names.
        """
        declared = {
            line.strip()
            for upload, _ in _SELECTIONS
            for line in spec_for(upload).requirements.splitlines()
            if line.strip()
        }
        assert declared <= set(_DISTRIBUTION_MODULES)
