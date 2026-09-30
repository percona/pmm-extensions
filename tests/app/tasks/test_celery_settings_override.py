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

"""Define tests for the Tasks worker's settings-override wiring."""

import asyncio
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.pool import StaticPool

from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.lifecycle import ProxyEntry, refresh_all
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingClassEnum, SettingOverride
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.worker import SEED_TIMEOUT_FRACTION
from app.core.utils import json_serializer
from app.tasks import celery as celery_module
from app.tasks.anonymizer.config import anonymizer_settings, AnonymizerSettings
from app.tasks.celery import (
    refresh_tasks_overrides_if_due,
    start_settings_override_refresher,
    stop_settings_override_refresher,
    sync_running_items,
)
from app.tasks.config import tasks_settings, TasksSettings
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.execution.executors.nomad import NomadExecutor
from app.tasks.models import (
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskWrite,
)
from tests.app.core.settings_override.conftest import (
    ANONYMIZER_SETTINGS_TOKEN,
    BOUNDED_SEED,
    HangingSession,
    recording_bounded_seed,
    TASKS_SETTINGS_TOKEN,
)
from tests.app.db_schema import apply_schema
from tests.app.factories import TaskFactory

ANCHOR = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _write_cert(path: Path, *, not_valid_after: datetime) -> None:
    """Write a minimal self-signed PEM certificate to ``path``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_after - timedelta(days=365))
        .not_valid_after(not_valid_after)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@pytest_asyncio.fixture(name="override_session_maker")
async def _override_session_maker() -> AsyncGenerator[async_sessionmaker, None]:
    """Provide an in-memory SQLite session maker isolated from the main test DB."""
    # scaffolding-dup-ok: this duplication predates the change that
    # re-annotated the fixture's return type; promoting it against
    # its sibling bootstrap is a cross-tree refactor of its own.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    try:
        yield get_async_session_maker_from_engine(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def worker_loop_env(monkeypatch):
    """Wire a fresh event loop and in-memory Tasks DB as a prefork worker child.

    Mirrors the ``worker_process_init`` runtime: a dedicated ``celery.loop``, the
    refresher enabled, ``get_async_session_maker`` pointed at an in-memory engine,
    and the per-process refresher handle reset. Yields
    ``(loop, session_maker)`` and stops any started refresher on teardown.
    """
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(celery_module.celery, "loop", loop)
    celery_module._refresher.stop()
    monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", True)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    maker = get_async_session_maker_from_engine(engine)
    monkeypatch.setattr(celery_module, "get_async_session_maker", lambda: maker)
    loop.run_until_complete(_create_schema(engine))
    yield loop, maker
    stop_settings_override_refresher()
    loop.run_until_complete(engine.dispose())
    loop.close()


async def _create_schema(engine) -> None:
    """Create every SQLModel table on ``engine``."""
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)


async def _seed_override(maker, *, setting_class, key, value) -> None:
    """Insert a single active ``SettingOverride`` row through ``maker``."""
    async with maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(setting_class=setting_class, key=key, value=value),
        )


async def _seed_running_history(maker, *, sync_started_at) -> int:
    """Persist a RUNNING ``TaskHistory`` with the given sync-lock timestamp."""
    async with maker() as session:
        task = await TaskManager.create(
            session, TaskWrite.model_validate(TaskFactory.build(name="sync-task"))
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name, target="node1", meta={"target": "node1"}
            ),
            status=TaskHistoryStatusEnum.RUNNING,
            executed_by="test-user",
            sync_in_progress_started_at=sync_started_at,
        )
        saved = await TaskHistoryManager.save(session, history)
        assert saved.id is not None
        return saved.id


async def _get_sync_started_at(maker, row_id: int):
    """Return the persisted ``sync_in_progress_started_at`` for ``row_id``."""
    async with maker() as session:
        row = await TaskHistoryManager.get_or_404(session, id=row_id)
        return row.sync_in_progress_started_at


def _anonymizer_proxies() -> dict:
    """Return the Anonymizer-side proxy registry mirroring the worker wiring."""
    return {
        SettingClassEnum.ANONYMIZER_SETTINGS: ProxyEntry(
            anonymizer_settings, AnonymizerSettings
        ),
    }


def _tasks_proxies() -> dict:
    """Return the Tasks-side proxy registry mirroring the worker wiring."""
    return {
        SettingClassEnum.TASKS_SETTINGS: ProxyEntry(tasks_settings, TasksSettings),
    }


class TestSettingClassEnumMembership:
    """Test the ANONYMIZER_SETTINGS enum member this wiring introduces."""

    def test_anonymizer_member_value_is_class_name(self):
        """Encode the Pydantic class name as the ANONYMIZER_SETTINGS member value."""
        assert SettingClassEnum.ANONYMIZER_SETTINGS.value == AnonymizerSettings.__name__


class TestAnonymizerDefaultEntitiesOverride:
    """Test the HOT ``DEFAULT_ENTITIES`` materializer round-trips via the proxy."""

    @pytest.mark.asyncio
    async def test_no_override_resolves_same_as_wrapped_instance(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Resolve to the wrapped instance when no override row exists."""
        await refresh_all(lambda: override_session_maker, _anonymizer_proxies())
        assert "DEFAULT_ENTITIES" not in anonymizer_settings.get_snapshot()
        assert (
            anonymizer_settings.DEFAULT_ENTITIES["any_owner"]
            == AnonymizerSettings().DEFAULT_ENTITIES["any_owner"]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "probe"),
        [
            ("*", "any_owner"),
            (["CREDIT_CARD", "EMAIL_ADDRESS"], "any_owner"),
            ({"owner_a": ["PERSON"]}, "owner_a"),
        ],
    )
    async def test_override_shapes_match_owning_model(
        self,
        override_session_maker: async_sessionmaker,
        raw,
        probe,
    ) -> None:
        """Resolve each override shape to the value the owning model produces."""
        await _seed_override(
            override_session_maker,
            setting_class=ANONYMIZER_SETTINGS_TOKEN,
            key="DEFAULT_ENTITIES",
            value=raw,
        )
        await refresh_all(lambda: override_session_maker, _anonymizer_proxies())
        assert "DEFAULT_ENTITIES" in anonymizer_settings.get_snapshot()
        expected = AnonymizerSettings(DEFAULT_ENTITIES=raw).DEFAULT_ENTITIES[probe]
        assert anonymizer_settings.DEFAULT_ENTITIES[probe] == expected


class TestSyncLockTtlPositivityGuard:
    """Test the SYNC_LOCK_TTL positivity guard on the YAML and override paths."""

    def test_yaml_non_positive_value_raises(self):
        """Reject a non-positive load-time value with ``ValidationError``."""
        with pytest.raises(ValidationError, match="greater than 0 seconds"):
            TasksSettings(SYNC_LOCK_TTL=timedelta(0))

    @pytest.mark.asyncio
    async def test_non_positive_override_rejected_keeps_prior_value(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Reject a non-positive override and keep the resolved default."""
        baseline = tasks_settings.SYNC_LOCK_TTL
        await _seed_override(
            override_session_maker,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="SYNC_LOCK_TTL",
            value=0,
        )
        await refresh_all(lambda: override_session_maker, _tasks_proxies())
        assert baseline == tasks_settings.SYNC_LOCK_TTL
        assert timedelta(0) < tasks_settings.SYNC_LOCK_TTL

    @pytest.mark.asyncio
    async def test_positive_override_accepted(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Apply a positive override and expose it on the proxy."""
        override = timedelta(minutes=10)
        await _seed_override(
            override_session_maker,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="SYNC_LOCK_TTL",
            value=int(override.total_seconds()),
        )
        await refresh_all(lambda: override_session_maker, _tasks_proxies())
        assert override == tasks_settings.SYNC_LOCK_TTL


class TestWorkerRefresherHandlers:
    """Test the worker_process_init / worker_process_shutdown refresher handlers."""

    def test_disabled_resolves_proxy_and_arms_nothing(self, monkeypatch, mocker):
        """Resolve the proxy but arm nothing when disabled."""
        monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", False)
        celery_module._refresher.stop()
        mock_anonymizer = MagicMock(spec=OverridableSettingsProxy)
        monkeypatch.setattr(celery_module, "anonymizer_settings", mock_anonymizer)
        refresh = mocker.patch("app.core.settings_override.lifecycle.refresh_all")

        start_settings_override_refresher()

        mock_anonymizer._resolve.assert_called_once_with()
        refresh.assert_not_called()
        assert not celery_module._refresher._armed

    def test_shutdown_is_noop_when_not_started(self, monkeypatch):
        """Handle a never-started refresher as a no-op on shutdown."""
        celery_module._refresher.stop()
        stop_settings_override_refresher()
        assert not celery_module._refresher._armed

    @pytest.mark.usefixtures("worker_loop_env")
    def test_shutdown_disarms_started_refresher(self):
        """Disarm the started refresher so further boundaries no-op."""
        start_settings_override_refresher()
        assert celery_module._refresher._armed

        stop_settings_override_refresher()

        assert not celery_module._refresher._armed
        assert celery_module._refresher._proxies is None

    @pytest.mark.usefixtures("worker_loop_env")
    def test_init_is_idempotent_when_already_armed(self):
        """Keep the armed refresher and skip a second seed on re-entry."""
        start_settings_override_refresher()
        first_proxies = celery_module._refresher._proxies
        first_stamp = celery_module._refresher._last_refresh
        assert celery_module._refresher._armed

        start_settings_override_refresher()

        assert celery_module._refresher._armed
        assert celery_module._refresher._proxies is first_proxies
        assert celery_module._refresher._last_refresh == first_stamp

    def test_post_init_override_visible_after_loop_driven(self, worker_loop_env):
        """Expose an override inserted after init once the loop is driven again."""
        loop, maker = worker_loop_env
        start_settings_override_refresher()
        baseline = tasks_settings.STALENESS_THRESHOLD_SECONDS
        loop.run_until_complete(
            _seed_override(
                maker,
                setting_class=TASKS_SETTINGS_TOKEN,
                key="STALENESS_THRESHOLD_SECONDS",
                value=baseline + 1234,
            )
        )
        loop.run_until_complete(refresh_all(lambda: maker, _tasks_proxies()))
        assert baseline + 1234 == tasks_settings.STALENESS_THRESHOLD_SECONDS

    @pytest.mark.usefixtures("worker_loop_env")
    def test_init_forwards_a_budget_from_worker_proc_alive_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Derive the seed budget from Celery's prefork liveness deadline."""
        recorded: dict[str, object] = {}
        monkeypatch.setattr(BOUNDED_SEED, recording_bounded_seed(recorded))
        monkeypatch.setattr(celery_module.celery.conf, "worker_proc_alive_timeout", 6.0)

        start_settings_override_refresher()

        assert recorded["seed_timeout"] == pytest.approx(6.0 * SEED_TIMEOUT_FRACTION)

    @pytest.mark.usefixtures("worker_loop_env")
    def test_init_arms_when_the_seed_hangs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep the child armed and due for a boundary refresh after seed expiry."""
        monkeypatch.setattr(
            celery_module, "get_async_session_maker", lambda: HangingSession
        )
        monkeypatch.setattr(celery_module.celery.conf, "worker_proc_alive_timeout", 0.1)

        start_settings_override_refresher()

        refresher = celery_module._refresher
        assert refresher._armed
        assert time.monotonic() - refresher._last_refresh >= refresher._interval_seconds

    @pytest.mark.usefixtures("worker_loop_env")
    def test_task_prerun_receiver_calls_maybe_refresh(self, mocker) -> None:
        """Wire ``refresh_tasks_overrides_if_due`` to the boundary due-check."""
        maybe = mocker.spy(celery_module._refresher, "maybe_refresh")

        refresh_tasks_overrides_if_due()

        maybe.assert_called_once_with()


class TestSyncRunningItemsRespectsOverriddenTtl:
    """Drive the real ``sync_running_items`` consumer with an overridden TTL."""

    @pytest.mark.asyncio
    async def test_default_ttl_claims_recently_locked_row(
        self, override_session_maker: async_sessionmaker, monkeypatch
    ) -> None:
        """Dispatch a row locked 10m ago under the default 5m TTL."""
        monkeypatch.setattr(celery_module, "utc_now", lambda: ANCHOR)
        monkeypatch.setattr(
            celery_module, "get_async_session_maker", lambda: override_session_maker
        )
        row_id = await _seed_running_history(
            override_session_maker, sync_started_at=ANCHOR - timedelta(minutes=10)
        )

        with patch("app.tasks.celery.sync_task_history") as mock_sync:
            await sync_running_items()

        mock_sync.chunks.assert_called_once_with([(row_id,)], 100)

    def test_overridden_ttl_excludes_recently_locked_row(
        self, worker_loop_env, monkeypatch
    ):
        """Keep a row locked 10m ago out of the claim window under a longer TTL."""
        loop, maker = worker_loop_env
        monkeypatch.setattr(celery_module, "utc_now", lambda: ANCHOR)
        override_ttl = timedelta(minutes=30)
        locked_at = ANCHOR - timedelta(minutes=10)
        row_id = loop.run_until_complete(
            _seed_running_history(maker, sync_started_at=locked_at)
        )
        loop.run_until_complete(
            _seed_override(
                maker,
                setting_class=TASKS_SETTINGS_TOKEN,
                key="SYNC_LOCK_TTL",
                value=int(override_ttl.total_seconds()),
            )
        )

        start_settings_override_refresher()
        assert override_ttl == tasks_settings.SYNC_LOCK_TTL
        with patch("app.tasks.celery.sync_task_history") as mock_sync:
            loop.run_until_complete(sync_running_items())

        mock_sync.chunks.assert_not_called()
        # The excluded row's lock timestamp was not bumped to ``func.now()``.
        # SQLite returns the value tz-naive, so compare without tzinfo.
        stored = loop.run_until_complete(_get_sync_started_at(maker, row_id))
        assert stored.replace(tzinfo=None) == locked_at.replace(tzinfo=None)


class TestCheckNomadCertExpiryWithOverride:
    """Test ``_check_nomad_cert_expiry`` with nested NOMAD leaf overrides."""

    @pytest.mark.asyncio
    async def test_applies_nested_leaf_overrides(
        self,
        override_session_maker: async_sessionmaker,
        mocker,
        tmp_path,
    ) -> None:
        """Apply NOMAD CA and client leaf overrides so the cert task reads them.

        Both cert paths are overridden to controlled ``tmp_path`` certs;
        leaving ``ssl_certfile`` at its configured default would resolve to a
        real on-disk dev cert (present locally, absent in CI) and add a stray
        ``resolve`` call.
        """
        ca = tmp_path / "ca.pem"
        client = tmp_path / "client.pem"
        _write_cert(ca, not_valid_after=ANCHOR + timedelta(days=30))
        _write_cert(client, not_valid_after=ANCHOR + timedelta(days=30))
        await _seed_override(
            override_session_maker,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__ssl_cafile",
            value=str(ca),
        )
        await _seed_override(
            override_session_maker,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__ssl_certfile",
            value=str(client),
        )
        await _seed_override(
            override_session_maker,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__cert_expiry_warn_days",
            value=7,
        )
        await refresh_all(lambda: override_session_maker, _tasks_proxies())
        assert isinstance(tasks_settings.NOMAD, NomadExecutor)
        assert tasks_settings.NOMAD.ssl_cafile == ca
        assert tasks_settings.NOMAD.ssl_certfile == client

        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)

        await celery_module._check_nomad_cert_expiry()

        awaited_keys = {c.args[0] for c in mock_alert.resolve.await_args_list}
        assert awaited_keys == {
            f"nomad-cert-expiry:{ca.name}",
            f"nomad-cert-expiry:{client.name}",
        }
        mock_alert.trigger.assert_not_called()
