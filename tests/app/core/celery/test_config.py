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

"""Define tests for the app.core.celery.config module."""

import pytest
from celery import Celery
from pydantic import ValidationError

from app.core.celery.config import CeleryOptions, PoolEngineOptions
from app.core.config import Settings, YamlPrefixConfigSettingsSource

_BROKER_URL = "redis://localhost:6379/0"
_MASKED_BROKER_URL = "amqp://celery-user:****@rabbit:5672/vhost"
_MASKED_RESULT_BACKEND = "redis://celery-user:****@redis:6379/0"
_WORKER_CONCURRENCY = 4
_ENV_WORKER_CONCURRENCY = 8


def _beat_engine_options_for_shipped_profile(profile: str) -> PoolEngineOptions:
    """Resolve ``CELERY.beat_engine_options`` for a settings.yaml profile."""
    source = YamlPrefixConfigSettingsSource(Settings, prefixes=(profile,))
    return CeleryOptions.model_validate(source.yaml_data["CELERY"]).beat_engine_options


def test_beat_engine_options_pre_pings_by_default():
    """Emit pool_pre_ping by default; sizing keys stay unset."""
    options = CeleryOptions(broker_url=_BROKER_URL)

    assert options.beat_engine_options.model_dump(exclude_none=True) == {
        "pool_pre_ping": True
    }


@pytest.mark.parametrize(
    "profile",
    ["development", "production_docker"],
)
def test_shipped_settings_yaml_enables_beat_pool_pre_ping(profile: str) -> None:
    """Resolve CELERY.beat_engine_options.pool_pre_ping from each CELERY profile."""
    assert _beat_engine_options_for_shipped_profile(profile).pool_pre_ping is True


def test_beat_engine_options_round_trip_through_model_dump():
    """Confirm the pool dict reaches the Celery-conf surface intact via model_dump()."""
    pool = {"pool_size": 20, "max_overflow": 5, "pool_timeout": 30}
    options = CeleryOptions(broker_url=_BROKER_URL, beat_engine_options=pool)

    assert options.model_dump()["beat_engine_options"] == {
        "pool_pre_ping": True,
        **pool,
    }


def test_beat_engine_options_accepts_zero_max_overflow():
    """Accept max_overflow=0 (no overflow) for the side-car cap."""
    options = CeleryOptions(
        broker_url=_BROKER_URL, beat_engine_options={"max_overflow": 0}
    )

    assert options.beat_engine_options.model_dump(exclude_none=True) == {
        "pool_pre_ping": True,
        "max_overflow": 0,
    }


@pytest.mark.parametrize(
    "pool",
    [
        {"poolsize": 1},
        {"pool_size": 0},
        {"max_overflow": -1},
        {"pool_timeout": 0},
        {"pool_size": 7.5},
        {"max_overflow": 3.5},
    ],
)
def test_beat_engine_options_rejects_invalid_values(pool):
    """Reject unknown keys, fractional integer keys, and out-of-range values."""
    with pytest.raises(ValidationError):
        CeleryOptions(broker_url=_BROKER_URL, beat_engine_options=pool)


def test_beat_engine_options_allows_fractional_pool_timeout():
    """Accept a fractional pool_timeout while integer keys stay whole."""
    options = CeleryOptions(
        broker_url=_BROKER_URL, beat_engine_options={"pool_timeout": 25.5}
    )

    assert options.beat_engine_options.model_dump(exclude_none=True) == {
        "pool_pre_ping": True,
        "pool_timeout": 25.5,
    }


def test_beat_engine_options_pre_ping_opt_out_on_env_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow disabling pool_pre_ping via CELERY__BEAT_ENGINE_OPTIONS__POOL_PRE_PING."""
    monkeypatch.setenv("CELERY__BEAT_ENGINE_OPTIONS__POOL_PRE_PING", "false")

    assert Settings(_env_file=None).CELERY.beat_engine_options.model_dump(
        exclude_none=True
    ) == {"pool_pre_ping": False}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"broker_url": _MASKED_BROKER_URL}, "broker_url"),
        (
            {"broker_url": _BROKER_URL, "result_backend": _MASKED_RESULT_BACKEND},
            "result_backend",
        ),
    ],
)
def test_credential_url_mask_rejected_on_yaml_path(
    kwargs: dict[str, str], match: str
) -> None:
    """Fail when a masked export is re-fed as a Celery broker or result backend URL."""
    with pytest.raises(ValidationError, match=match):
        CeleryOptions(**kwargs)


@pytest.mark.parametrize(
    ("env_name", "value", "match"),
    [
        ("CELERY__BROKER_URL", _MASKED_BROKER_URL, "broker_url"),
        ("CELERY__RESULT_BACKEND", _MASKED_RESULT_BACKEND, "result_backend"),
    ],
)
def test_credential_url_mask_rejected_on_env_path(
    env_name: str, value: str, match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail Settings load when a masked Celery URL arrives via the environment."""
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ValidationError, match=match):
        Settings(_env_file=None)


def test_worker_concurrency_is_unset_by_default():
    """Leave worker_concurrency unset so Celery keeps its per-CPU default."""
    options = CeleryOptions(broker_url=_BROKER_URL)

    assert options.worker_concurrency is None
    assert Celery("test", **options.model_dump()).conf.worker_concurrency is None


def test_worker_concurrency_reaches_celery_conf():
    """Carry a declared worker_concurrency into the Celery config.

    The declared-field assertion is what ties this to the typed field: an
    undeclared extra of the same name also reaches ``conf``, so the value check
    alone would pass without it.
    """
    assert "worker_concurrency" in CeleryOptions.model_fields
    options = CeleryOptions(
        broker_url=_BROKER_URL, worker_concurrency=_WORKER_CONCURRENCY
    )

    conf = Celery("test", **options.model_dump()).conf

    assert conf.worker_concurrency == _WORKER_CONCURRENCY


def test_worker_concurrency_coerces_env_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coerce CELERY__WORKER_CONCURRENCY to the integer the prefork pool needs.

    :param monkeypatch: Sets the environment variable for this test.
    """
    monkeypatch.setenv("CELERY__WORKER_CONCURRENCY", str(_ENV_WORKER_CONCURRENCY))

    assert Settings(_env_file=None).CELERY.worker_concurrency == _ENV_WORKER_CONCURRENCY


@pytest.mark.parametrize("value", [0, -1, "x", 2.5])
def test_worker_concurrency_rejects_invalid_values(value: str | float) -> None:
    """Reject non-positive, non-numeric and fractional concurrency values.

    :param value: A concurrency value the field must refuse.
    """
    with pytest.raises(ValidationError):
        CeleryOptions(broker_url=_BROKER_URL, worker_concurrency=value)
