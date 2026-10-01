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

"""Tests for :class:`OverridableSettingsProxy`."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

from app.core.settings_override.proxy import OverridableSettingsProxy


class _Sample(BaseModel):
    name: str = "default-name"
    count: int = 1


def _factory() -> _Sample:
    return _Sample()


@pytest.fixture
def proxy() -> OverridableSettingsProxy[_Sample]:
    """Return a fresh proxy wrapping ``_Sample``."""
    return OverridableSettingsProxy(_factory)


def test_empty_snapshot_delegates_to_factory(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """With no snapshot entries, attribute reads delegate to the wrapped instance."""
    assert proxy.name == "default-name"
    assert proxy.count == 1


def test_get_snapshot_starts_empty(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """A fresh proxy exposes an empty snapshot mapping."""
    assert dict(proxy.get_snapshot()) == {}


def test_get_snapshot_reflects_published_mapping(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """``get_snapshot`` returns the mapping most recently published."""
    proxy._set_snapshot({"name": "override-name"})
    assert dict(proxy.get_snapshot()) == {"name": "override-name"}


def test_snapshot_intercepts_named_attribute(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """When the snapshot contains the field, it overrides the wrapped value."""
    proxy._set_snapshot({"name": "from-snapshot"})
    assert proxy.name == "from-snapshot"
    # Other fields still delegate to the wrapped instance.
    assert proxy.count == 1


def test_clearing_snapshot_falls_back_to_factory(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """Removing an override key falls back to the wrapped value."""
    proxy._set_snapshot({"name": "from-snapshot"})
    assert proxy.name == "from-snapshot"
    proxy._set_snapshot({})
    assert proxy.name == "default-name"


def test_class_property_reflects_wrapped_class(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """``__class__`` reports the wrapped class (preserves ``LazyProxy``)."""
    assert isinstance(proxy, _Sample)


def test_concurrent_swap_is_atomic(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """A reader during a snapshot swap sees one full snapshot or the other.

    The CPython STORE_ATTR opcode is single-instruction (GIL-protected), so a
    reader sees the previous snapshot or the new one -- never a partially
    mutated dict.
    """
    snapshot_a = {"name": "alpha"}
    snapshot_b = {"name": "beta"}
    proxy._set_snapshot(snapshot_a)

    observed: list[str] = []
    stop = threading.Event()

    def _reader() -> None:
        while not stop.is_set():
            value = proxy.name
            observed.append(value)

    def _writer() -> None:
        for _ in range(200):
            proxy._set_snapshot(snapshot_a)
            proxy._set_snapshot(snapshot_b)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(_reader)
        pool.submit(_writer).result()
        stop.set()
        reader.result()

    assert observed, "reader thread observed no values; concurrency was not exercised"
    assert set(observed) <= {"alpha", "beta"}


def test_missing_attribute_raises_attribute_error(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """Unknown attributes still raise ``AttributeError`` from the wrapped instance."""
    with pytest.raises(AttributeError):
        proxy.does_not_exist  # noqa: B018


def test_snapshot_returns_premerged_nested_model(
    proxy: OverridableSettingsProxy[_Sample],
) -> None:
    """A pre-merged nested model stored in the snapshot is returned verbatim.

    Confirms the proxy needs no nested-awareness: the snapshot-time merge
    stores the merged Pydantic copy under the top-level key, and the existing
    ``if name in snapshot`` read path returns it directly.
    """
    expected_count = 99
    merged = _Sample(name="merged", count=expected_count)
    proxy._set_snapshot({"name": merged})
    assert proxy.name is merged
    assert proxy.name.count == expected_count


def test_per_class_isolation_with_unknown_field() -> None:
    """Attribute access for a key absent from both snapshot and wrapped class raises.

    Snapshots are keyed by attribute name and are not filtered at the proxy
    boundary -- if a rogue snapshot entry slips past :func:`build_snapshot`'s
    field-existence check, ``proxy.<rogue_key>`` would still return the
    snapshot's value. The protection against phantom values lives in
    :func:`build_snapshot`, not the proxy.

    What the proxy *does* guarantee is that attribute access for a key
    present in neither the snapshot nor the wrapped instance raises
    ``AttributeError`` rather than silently returning ``None`` or empty.
    This test exercises that guarantee with a snapshot that contains an
    unrelated rogue key and an access for a distinct never-defined key.
    """
    proxy = OverridableSettingsProxy(_factory)
    proxy._set_snapshot({"unknown_field": "should-not-leak"})
    with pytest.raises(AttributeError):
        _ = proxy.also_absent


class TestConstructorContract:
    """Pin the constructor to the factory alone."""

    def test_rejects_setting_class_keyword(self) -> None:
        """Reject the removed ``setting_class`` keyword instead of ignoring it."""
        with pytest.raises(TypeError):
            OverridableSettingsProxy(
                _factory,
                setting_class="ExtensionsSettings",  # ty: ignore[unknown-argument]
            )
