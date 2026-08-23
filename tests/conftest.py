"""Shared test fixtures for the Dominion Energy integration.

This module is deliberately dependency-light: it imports nothing beyond the
standard library and pytest. Several test modules in this suite run without
Home Assistant (or ``dompower``) installed, and a top-level import of either
here would break collection for all of them.

The stand-in classes below duck-type the ``dompower`` models closely enough
for the code under test, which reads them via ``getattr``/``.get``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "dominion_energy"

# Sentinel credentials. These are deliberately distinctive, unlikely-to-collide
# strings so a test can assert that no substring of them survives into a
# diagnostics dump. Do not replace them with realistic-looking values.
FAKE_USERNAME = "zzz-diagnostics-username@example.invalid"
FAKE_PASSWORD = "zzz-diagnostics-password-Hunter2"
FAKE_ACCESS_TOKEN = "zzz-diagnostics-access-token-AAAABBBBCCCC"
FAKE_REFRESH_TOKEN = "zzz-diagnostics-refresh-token-DDDDEEEEFFFF"
FAKE_COOKIES = {"glt_zzz": "zzz-diagnostics-cookie-GGGGHHHH"}
FAKE_ACCOUNT_NUMBER = "123456789123"
FAKE_METER_NUMBER = "000000000296117800"
FAKE_SERVICE_ADDRESS = "742 Evergreen Terrace, Columbia, SC 29201"

#: Every sentinel that must never appear in a diagnostics dump.
FAKE_SECRETS: tuple[str, ...] = (
    FAKE_USERNAME,
    FAKE_PASSWORD,
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    FAKE_COOKIES["glt_zzz"],
    FAKE_ACCOUNT_NUMBER,
    FAKE_METER_NUMBER,
    FAKE_SERVICE_ADDRESS,
    # Address fragments that would identify the home on their own.
    "742 Evergreen Terrace",
    "Evergreen",
    "29201",
)


@dataclass
class FakeInterval:
    """Stand-in for ``dompower.IntervalUsageData``."""

    timestamp: datetime
    consumption: float
    generation: float = 0.0
    unit: str = "kWh"


@dataclass
class FakeBillPeriod:
    """Stand-in for ``dompower.BillPeriodData``."""

    charges: float
    usage: float
    period_start: date | None = None
    period_end: date | None = None


@dataclass
class FakeBillForecast:
    """Stand-in for ``dompower.BillForecast``.

    ``last_bill`` is optional and ``usage_through_date`` is named as the real
    dataclass names it, because a fake that is easier to reach through than the
    thing it stands in for lets a `None` dereference pass the suite and crash
    a live install.
    """

    last_bill: FakeBillPeriod | None
    current_period_start: date
    usage_through_date: date
    current_usage_kwh: float
    is_tou: bool = False

    @property
    def derived_rate(self) -> float | None:
        """Mirror ``BillForecast.derived_rate``."""
        if self.last_bill is not None and self.last_bill.usage > 0:
            return self.last_bill.charges / self.last_bill.usage
        return None


@dataclass
class FakeCoordinatorData:
    """Stand-in for ``coordinator.DominionEnergyData``."""

    intervals: list[FakeInterval] = field(default_factory=list)
    latest_interval: FakeInterval | None = None
    daily_total: float = 0.0
    monthly_total: float = 0.0
    daily_cost: float = 0.0
    monthly_cost: float = 0.0
    bill_forecast: FakeBillForecast | None = None
    data_date: date | None = None
    month_start_date: date | None = None
    month_end_date: date | None = None
    last_success: datetime | None = None


@dataclass
class FakeCoordinator:
    """Stand-in for ``DominionEnergyCoordinator``."""

    data: FakeCoordinatorData | None = None
    last_update_success: bool = True
    last_exception: BaseException | None = None
    update_interval: timedelta = timedelta(minutes=30)
    consecutive_failures: int = 0


@dataclass
class FakeConfigEntry:
    """Stand-in for a Home Assistant ``ConfigEntry``.

    Only the attributes the integration actually reads are modelled.
    """

    data: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    minor_version: int = 1
    source: str = "user"
    state: str = "loaded"
    disabled_by: Any = None
    pref_disable_polling: bool = False
    entry_id: str = "zzz_test_entry_id"
    title: str = f"Dominion Energy ({FAKE_ACCOUNT_NUMBER})"
    unique_id: str = f"{FAKE_ACCOUNT_NUMBER}_{FAKE_METER_NUMBER}"
    runtime_data: Any = None


def build_day_of_intervals(
    day: date,
    *,
    consumption: float = 0.5,
    generation: float = 0.0,
    tz: Any = UTC,
) -> list[FakeInterval]:
    """Build a full day of 48 half-hourly intervals.

    ``tz`` defaults to UTC so the helper stays usable without a timezone
    database; pass a real ``America/New_York`` tzinfo when the test cares.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return [
        FakeInterval(
            timestamp=start + timedelta(minutes=30 * i),
            consumption=consumption,
            generation=generation,
        )
        for i in range(48)
    ]


@pytest.fixture
def data_date() -> date:
    """Return the date the coordinator would treat as the last complete day."""
    return date(2026, 8, 10)


@pytest.fixture
def fake_intervals(data_date: date) -> list[FakeInterval]:
    """Return one full day of consumption-only intervals."""
    return build_day_of_intervals(data_date, consumption=0.5)


@pytest.fixture
def fake_solar_intervals(data_date: date) -> list[FakeInterval]:
    """Return one full day of intervals that include solar generation."""
    return build_day_of_intervals(data_date, consumption=0.5, generation=0.2)


@pytest.fixture
def fake_bill_forecast() -> FakeBillForecast:
    """Return a bill forecast with a derivable rate of $0.125/kWh."""
    return FakeBillForecast(
        last_bill=FakeBillPeriod(
            charges=125.00,
            usage=1000.0,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
        ),
        current_period_start=date(2026, 8, 1),
        usage_through_date=date(2026, 8, 21),
        current_usage_kwh=480.0,
        is_tou=False,
    )


@pytest.fixture
def fake_entry_data() -> dict[str, Any]:
    """Return config entry data populated entirely with sentinel values."""
    return {
        "username": FAKE_USERNAME,
        "password": FAKE_PASSWORD,
        "access_token": FAKE_ACCESS_TOKEN,
        "refresh_token": FAKE_REFRESH_TOKEN,
        "cookies": FAKE_COOKIES,
        "account_number": FAKE_ACCOUNT_NUMBER,
        "meter_number": FAKE_METER_NUMBER,
        "service_address": FAKE_SERVICE_ADDRESS,
    }


@pytest.fixture
def fake_entry_options() -> dict[str, Any]:
    """Return a representative time-of-use options payload."""
    return {
        "cost_mode": "time_of_use",
        "peak_rate": 0.15,
        "off_peak_rate": 0.08,
        "peak_start_hour": 14,
        "peak_end_hour": 19,
    }


@pytest.fixture
def fake_coordinator_data(
    fake_intervals: list[FakeInterval],
    fake_bill_forecast: FakeBillForecast,
    data_date: date,
) -> FakeCoordinatorData:
    """Return a populated coordinator data object."""
    return FakeCoordinatorData(
        intervals=fake_intervals,
        latest_interval=fake_intervals[-1],
        daily_total=24.0,
        monthly_total=240.0,
        daily_cost=3.0,
        monthly_cost=30.0,
        bill_forecast=fake_bill_forecast,
        data_date=data_date,
        month_start_date=date(2026, 8, 1),
        month_end_date=data_date,
    )


@pytest.fixture
def fake_coordinator(fake_coordinator_data: FakeCoordinatorData) -> FakeCoordinator:
    """Return a coordinator stand-in holding successful data."""
    return FakeCoordinator(data=fake_coordinator_data)


@pytest.fixture
def fake_config_entry(
    fake_entry_data: dict[str, Any],
    fake_entry_options: dict[str, Any],
    fake_coordinator: FakeCoordinator,
) -> FakeConfigEntry:
    """Return a config entry stand-in wired to a loaded coordinator."""
    return FakeConfigEntry(
        data=fake_entry_data,
        options=fake_entry_options,
        runtime_data=fake_coordinator,
    )
