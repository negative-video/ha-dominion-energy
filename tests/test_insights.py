"""Tests for the derived usage insights.

``insights.py`` is pure by design -- no Home Assistant imports -- so it is
imported directly here rather than through the loader dance that
``test_features.py`` needs for ``coordinator.py``.

The interval fixtures are built in local time, because every question these
helpers answer ("the 6 PM hour", "overnight") is a local-time question.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "dominion_energy"
)

# `insights.py` imports `usage.py` relatively, so it needs to be loaded inside
# *a* package -- but importing the real one executes `__init__.py`, which pulls
# in Home Assistant. Load both modules into a private synthetic package
# instead: the relative import resolves, and no name the rest of the suite
# might legitimately import is shadowed.
_PKG = "_dominion_pure"


def _load_pure_package() -> types.ModuleType:
    """Execute the Home-Assistant-free modules under a synthetic package."""
    if _PKG not in sys.modules:
        package = types.ModuleType(_PKG)
        package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[_PKG] = package
    for name in ("usage", "insights"):
        qualified = f"{_PKG}.{name}"
        if qualified in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(
            qualified, COMPONENT_DIR / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{_PKG}.insights"]


insights = _load_pure_package()

MIN_PROFILE_DAYS = insights.MIN_PROFILE_DAYS
complete_days_by_date = insights.complete_days_by_date
hour_label = insights.hour_label
usage_profile = insights.usage_profile

NY = ZoneInfo("America/New_York")


class FakeInterval:
    """Stand-in for ``dompower.IntervalUsageData``."""

    def __init__(self, timestamp: datetime, consumption: float) -> None:
        self.timestamp = timestamp
        self.consumption = consumption


def make_day(
    day: date,
    *,
    consumption: float = 0.5,
    count: int = 48,
    shape: dict[int, float] | None = None,
) -> list[FakeInterval]:
    """Build a day of 30-minute intervals in local time.

    ``shape`` overrides the per-interval consumption for specific hours, which
    is how these tests plant a peak somewhere findable.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=NY)
    rows = []
    for i in range(count):
        timestamp = start + timedelta(minutes=30 * i)
        value = (shape or {}).get(timestamp.hour, consumption)
        rows.append(FakeInterval(timestamp, value))
    return rows


def make_days(
    last: date,
    count: int,
    *,
    shape: dict[int, float] | None = None,
    consumption: float = 0.5,
) -> list[FakeInterval]:
    """Build ``count`` consecutive complete days ending on ``last``."""
    rows: list[FakeInterval] = []
    for offset in range(count):
        rows.extend(
            make_day(
                last - timedelta(days=offset), shape=shape, consumption=consumption
            )
        )
    return rows


LAST_DAY = date(2026, 8, 10)


class TestHourLabel:
    """The state of the busiest-hour sensor is this string."""

    @pytest.mark.parametrize(
        ("hour", "expected"),
        [
            (0, "12 AM"),
            (1, "1 AM"),
            (11, "11 AM"),
            (12, "12 PM"),
            (13, "1 PM"),
            (18, "6 PM"),
            (23, "11 PM"),
        ],
    )
    def test_reads_like_a_person_would_say_it(self, hour: int, expected: str) -> None:
        assert hour_label(hour) == expected

    def test_noon_and_midnight_are_not_zero(self) -> None:
        """The classic 12-hour clock off-by-one, pinned in both directions."""
        assert "0" not in hour_label(0).replace("12", "")
        assert hour_label(0) != hour_label(12)

    @pytest.mark.parametrize("hour", range(24))
    def test_every_hour_is_distinct(self, hour: int) -> None:
        labels = {hour_label(h) for h in range(24)}
        assert len(labels) == 24


class TestCompleteDaysByDate:
    """Partial days must not be averaged in as if they were quiet ones."""

    def test_groups_whole_days(self) -> None:
        by_day = complete_days_by_date(
            make_days(LAST_DAY, 3), through=LAST_DAY, days=30
        )
        assert len(by_day) == 3
        assert all(len(rows) == 48 for rows in by_day.values())

    def test_drops_a_partly_published_day(self) -> None:
        """The trap: a six-hour day would halve every hourly average."""
        rows = make_days(LAST_DAY - timedelta(days=1), 2)
        rows.extend(make_day(LAST_DAY, count=12))
        by_day = complete_days_by_date(rows, through=LAST_DAY, days=30)
        assert LAST_DAY not in by_day
        assert len(by_day) == 2

    def test_keeps_only_the_most_recent_days(self) -> None:
        by_day = complete_days_by_date(
            make_days(LAST_DAY, 10), through=LAST_DAY, days=3
        )
        assert sorted(by_day) == [
            LAST_DAY - timedelta(days=2),
            LAST_DAY - timedelta(days=1),
            LAST_DAY,
        ]

    def test_ignores_days_after_the_window(self) -> None:
        """A future-dated row must not drag the window forward."""
        rows = make_days(LAST_DAY, 3)
        rows.extend(make_day(LAST_DAY + timedelta(days=1)))
        by_day = complete_days_by_date(rows, through=LAST_DAY, days=30)
        assert max(by_day) == LAST_DAY

    def test_rows_come_back_in_order(self) -> None:
        rows = list(reversed(make_days(LAST_DAY, 2)))
        by_day = complete_days_by_date(rows, through=LAST_DAY, days=30)
        for day_rows in by_day.values():
            timestamps = [row.timestamp for row in day_rows]
            assert timestamps == sorted(timestamps)


class TestUsageProfile:
    """The average shape of a day."""

    def test_finds_a_planted_evening_peak(self) -> None:
        profile = usage_profile(
            make_days(LAST_DAY, 14, shape={18: 3.0}), through=LAST_DAY
        )
        assert profile is not None
        assert profile.peak_hour == 18
        assert profile.peak_label == "6 PM"

    def test_finds_the_quietest_hour(self) -> None:
        profile = usage_profile(
            make_days(LAST_DAY, 14, shape={3: 0.05}), through=LAST_DAY
        )
        assert profile is not None
        assert profile.quietest_hour == 3
        assert profile.quietest_label == "3 AM"

    def test_hourly_average_is_per_day_not_per_window(self) -> None:
        """Two half-hour intervals of 0.5 kWh is 1 kWh in that hour, every day.

        Summing the window instead would report 14 kWh and make the sensor
        meaningless the moment the window length changed.
        """
        profile = usage_profile(make_days(LAST_DAY, 14), through=LAST_DAY)
        assert profile is not None
        assert profile.hourly_average[9] == pytest.approx(1.0)
        assert profile.average_daily_kwh == pytest.approx(24.0)

    def test_covers_all_twenty_four_hours(self) -> None:
        profile = usage_profile(make_days(LAST_DAY, 14), through=LAST_DAY)
        assert profile is not None
        assert len(profile.hourly_average) == 24

    def test_reports_the_window_it_used(self) -> None:
        profile = usage_profile(make_days(LAST_DAY, 14), through=LAST_DAY)
        assert profile is not None
        assert profile.days == 14
        assert profile.last_day == LAST_DAY
        assert profile.first_day == LAST_DAY - timedelta(days=13)

    def test_window_is_capped_at_the_requested_length(self) -> None:
        profile = usage_profile(make_days(LAST_DAY, 40), through=LAST_DAY, days=30)
        assert profile is not None
        assert profile.days == 30

    def test_too_little_history_reports_nothing(self) -> None:
        """A fresh install should say "unknown", not guess from three days."""
        assert usage_profile(make_days(LAST_DAY, 3), through=LAST_DAY) is None

    def test_the_minimum_is_exactly_min_profile_days(self) -> None:
        assert (
            usage_profile(make_days(LAST_DAY, MIN_PROFILE_DAYS - 1), through=LAST_DAY)
            is None
        )
        assert (
            usage_profile(make_days(LAST_DAY, MIN_PROFILE_DAYS), through=LAST_DAY)
            is not None
        )

    def test_one_odd_day_moves_the_shape_without_defining_it(self) -> None:
        """Averaging across days is what keeps a single spike in proportion."""
        rows = make_days(LAST_DAY - timedelta(days=1), 13, shape={18: 3.0})
        rows.extend(make_day(LAST_DAY, shape={2: 20.0}))

        profile = usage_profile(rows, through=LAST_DAY)
        assert profile is not None
        # 20 kWh in one 2 AM hour of 14 days averages to under the planted
        # 6 kWh evening peak, so the shape still reports the real habit.
        assert profile.peak_hour == 18

    def test_empty_input_reports_nothing(self) -> None:
        assert usage_profile([], through=LAST_DAY) is None
