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

BASELINE_END_HOUR = insights.BASELINE_END_HOUR
BASELINE_NIGHTS = insights.BASELINE_NIGHTS
MIN_BASELINE_NIGHTS = insights.MIN_BASELINE_NIGHTS
MIN_PROFILE_DAYS = insights.MIN_PROFILE_DAYS
UNUSUAL_DAY_THRESHOLD = insights.UNUSUAL_DAY_THRESHOLD
TimeWindow = insights.TimeWindow
baseline_load = insights.baseline_load
compare_to_typical_day = insights.compare_to_typical_day
complete_days_by_date = insights.complete_days_by_date
hour_label = insights.hour_label
hvac_active_windows = insights.hvac_active_windows
merge_windows = insights.merge_windows
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


class TestCompareToTypicalDay:
    """Yesterday against how that weekday usually goes."""

    # 2026-08-10 is a Monday, so the comparison days are the four Mondays
    # before it and every other day in the window is noise.
    MONDAY = date(2026, 8, 10)

    def _weeks_of(self, *, weekday_kwh: float, other_kwh: float) -> list[FakeInterval]:
        """Build five weeks where Mondays differ from every other day."""
        rows: list[FakeInterval] = []
        for offset in range(35):
            day = self.MONDAY - timedelta(days=offset)
            per_interval = (
                weekday_kwh if day.weekday() == self.MONDAY.weekday() else other_kwh
            )
            rows.extend(make_day(day, consumption=per_interval / 48))
        return rows

    def test_a_normal_day_is_not_flagged(self) -> None:
        rows = self._weeks_of(weekday_kwh=30.0, other_kwh=20.0)
        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.unusual is False
        assert comparison.direction == "typical"
        assert comparison.delta == pytest.approx(0.0)

    def test_a_spike_is_flagged_as_higher(self) -> None:
        rows = self._weeks_of(weekday_kwh=20.0, other_kwh=20.0)
        rows = [r for r in rows if r.timestamp.date() != self.MONDAY]
        rows.extend(make_day(self.MONDAY, consumption=40.0 / 48))

        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.unusual is True
        assert comparison.direction == "higher"
        assert comparison.delta == pytest.approx(1.0)

    def test_a_dip_is_flagged_as_lower(self) -> None:
        rows = self._weeks_of(weekday_kwh=20.0, other_kwh=20.0)
        rows = [r for r in rows if r.timestamp.date() != self.MONDAY]
        rows.extend(make_day(self.MONDAY, consumption=5.0 / 48))

        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.unusual is True
        assert comparison.direction == "lower"
        assert comparison.delta < 0

    def test_it_compares_against_the_same_weekday(self) -> None:
        """The whole point: a heavy-Monday household is not flagged weekly.

        Mondays run 50% above every other day here. A trailing average would
        put "typical" near 21 kWh and report every single Monday as unusual;
        comparing Monday to Mondays reports none of them.
        """
        rows = self._weeks_of(weekday_kwh=30.0, other_kwh=20.0)
        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.typical == pytest.approx(30.0)
        assert comparison.unusual is False

    def test_typical_uses_a_median_not_a_mean(self) -> None:
        """One already-exceptional Monday must not raise the bar.

        Three ordinary 20 kWh Mondays and one 200 kWh outlier: the mean is
        65 kWh and would hide a 40 kWh day, the median is 20 and catches it.
        """
        rows: list[FakeInterval] = []
        for offset in range(35):
            day = self.MONDAY - timedelta(days=offset)
            rows.extend(make_day(day, consumption=20.0 / 48))
        outlier = self.MONDAY - timedelta(days=7)
        rows = [r for r in rows if r.timestamp.date() not in (outlier, self.MONDAY)]
        rows.extend(make_day(outlier, consumption=200.0 / 48))
        rows.extend(make_day(self.MONDAY, consumption=40.0 / 48))

        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.typical == pytest.approx(20.0)
        assert comparison.unusual is True

    def test_the_threshold_is_the_documented_one(self) -> None:
        """Just inside and just outside the band, so the edge is pinned."""
        for factor, expected in (
            (1 + UNUSUAL_DAY_THRESHOLD - 0.05, False),
            (1 + UNUSUAL_DAY_THRESHOLD + 0.05, True),
        ):
            rows = self._weeks_of(weekday_kwh=20.0, other_kwh=20.0)
            rows = [r for r in rows if r.timestamp.date() != self.MONDAY]
            rows.extend(make_day(self.MONDAY, consumption=20.0 * factor / 48))

            comparison = compare_to_typical_day(rows, day=self.MONDAY)
            assert comparison is not None
            assert comparison.unusual is expected

    def test_too_little_history_reports_nothing(self) -> None:
        """One prior Monday is not enough to call anything typical."""
        rows: list[FakeInterval] = []
        for offset in range(10):
            rows.extend(make_day(self.MONDAY - timedelta(days=offset)))
        assert compare_to_typical_day(rows, day=self.MONDAY) is None

    def test_a_missing_day_reports_nothing(self) -> None:
        rows = self._weeks_of(weekday_kwh=20.0, other_kwh=20.0)
        rows = [r for r in rows if r.timestamp.date() != self.MONDAY]
        assert compare_to_typical_day(rows, day=self.MONDAY) is None

    def test_an_incomplete_day_reports_nothing(self) -> None:
        """A half-published day would look like a dramatic drop."""
        rows = self._weeks_of(weekday_kwh=20.0, other_kwh=20.0)
        rows = [r for r in rows if r.timestamp.date() != self.MONDAY]
        rows.extend(make_day(self.MONDAY, count=12))
        assert compare_to_typical_day(rows, day=self.MONDAY) is None

    def test_it_reports_how_many_days_it_compared(self) -> None:
        rows = self._weeks_of(weekday_kwh=20.0, other_kwh=20.0)
        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.compared_days == 4

    def test_it_ignores_days_beyond_the_window(self) -> None:
        """A tenth Monday back must not vote on this week."""
        rows: list[FakeInterval] = []
        for offset in range(70):
            day = self.MONDAY - timedelta(days=offset)
            weeks_back = offset // 7
            per_day = 20.0 if weeks_back <= 4 else 200.0
            rows.extend(make_day(day, consumption=per_day / 48))

        comparison = compare_to_typical_day(rows, day=self.MONDAY)
        assert comparison is not None
        assert comparison.typical == pytest.approx(20.0)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """A local-time instant on ``day``."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


class TestMergeWindows:
    """Several thermostats run independently; their spans interleave."""

    def test_disjoint_windows_are_left_alone(self) -> None:
        merged = merge_windows(
            [
                TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 2)),
                TimeWindow(at(LAST_DAY, 3), at(LAST_DAY, 4)),
            ]
        )
        assert len(merged) == 2

    def test_overlapping_windows_collapse(self) -> None:
        merged = merge_windows(
            [
                TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 3)),
                TimeWindow(at(LAST_DAY, 2), at(LAST_DAY, 4)),
            ]
        )
        assert merged == [TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 4))]

    def test_a_contained_window_does_not_shrink_the_outer_one(self) -> None:
        merged = merge_windows(
            [
                TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 5)),
                TimeWindow(at(LAST_DAY, 2), at(LAST_DAY, 3)),
            ]
        )
        assert merged == [TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 5))]

    def test_input_order_does_not_matter(self) -> None:
        windows = [
            TimeWindow(at(LAST_DAY, 3), at(LAST_DAY, 4)),
            TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 2)),
        ]
        assert merge_windows(windows) == merge_windows(reversed(windows))

    def test_empty_input_is_empty_output(self) -> None:
        assert merge_windows([]) == []


class TestHvacActiveWindows:
    """Turning recorded thermostat states into spans of real runtime."""

    UNTIL = at(LAST_DAY, 6)

    def test_a_running_action_becomes_a_window(self) -> None:
        windows = hvac_active_windows(
            [
                (at(LAST_DAY, 1), "cool", "idle"),
                (at(LAST_DAY, 2), "cool", "cooling"),
                (at(LAST_DAY, 3), "cool", "idle"),
            ],
            until=self.UNTIL,
        )
        assert windows == [TimeWindow(at(LAST_DAY, 2), at(LAST_DAY, 3))]

    def test_the_last_sample_runs_to_the_window_end(self) -> None:
        windows = hvac_active_windows(
            [(at(LAST_DAY, 4), "cool", "cooling")], until=self.UNTIL
        )
        assert windows == [TimeWindow(at(LAST_DAY, 4), self.UNTIL)]

    def test_set_to_cool_but_idle_is_not_running(self) -> None:
        """The central case: a thermostat left on cool all night.

        Reading the state instead of the action would exclude the entire
        night and leave nothing to measure -- for exactly the household this
        feature exists to serve.
        """
        windows = hvac_active_windows(
            [(at(LAST_DAY, 0), "cool", "idle")], until=self.UNTIL
        )
        assert windows == []

    @pytest.mark.parametrize(
        "action", ["heating", "cooling", "drying", "fan", "preheating", "defrosting"]
    )
    def test_every_drawing_action_counts(self, action: str) -> None:
        windows = hvac_active_windows(
            [(at(LAST_DAY, 1), "heat_cool", action)], until=self.UNTIL
        )
        assert len(windows) == 1

    def test_fan_only_counts_as_running(self) -> None:
        """An air handler is hundreds of watts, not standing load."""
        windows = hvac_active_windows(
            [(at(LAST_DAY, 1), "cool", "fan")], until=self.UNTIL
        )
        assert windows == [TimeWindow(at(LAST_DAY, 1), self.UNTIL)]

    @pytest.mark.parametrize("action", ["idle", "off"])
    def test_quiet_actions_do_not_count(self, action: str) -> None:
        assert (
            hvac_active_windows([(at(LAST_DAY, 1), "heat", action)], until=self.UNTIL)
            == []
        )

    def test_a_thermostat_without_an_action_falls_back_to_its_mode(self) -> None:
        """Conservative: excludes more time rather than hiding compressor draw."""
        windows = hvac_active_windows(
            [
                (at(LAST_DAY, 1), "off", None),
                (at(LAST_DAY, 2), "heat", None),
            ],
            until=self.UNTIL,
        )
        assert windows == [TimeWindow(at(LAST_DAY, 2), self.UNTIL)]

    @pytest.mark.parametrize("state", ["unavailable", "unknown"])
    def test_unavailable_says_nothing_about_runtime(self, state: str) -> None:
        assert (
            hvac_active_windows([(at(LAST_DAY, 1), state, None)], until=self.UNTIL)
            == []
        )

    def test_an_unrecognised_action_is_assumed_to_draw_power(self) -> None:
        """A new HVAC action is far more likely to run than to idle."""
        windows = hvac_active_windows(
            [(at(LAST_DAY, 1), "heat", "dehumidifying")], until=self.UNTIL
        )
        assert len(windows) == 1

    def test_consecutive_running_samples_merge(self) -> None:
        windows = hvac_active_windows(
            [
                (at(LAST_DAY, 1), "cool", "cooling"),
                (at(LAST_DAY, 2), "cool", "fan"),
                (at(LAST_DAY, 3), "cool", "idle"),
            ],
            until=self.UNTIL,
        )
        assert windows == [TimeWindow(at(LAST_DAY, 1), at(LAST_DAY, 3))]

    def test_samples_at_or_after_the_end_are_dropped(self) -> None:
        assert (
            hvac_active_windows([(self.UNTIL, "cool", "cooling")], until=self.UNTIL)
            == []
        )


class TestBaselineLoad:
    """The standing draw, and the reason the thermostats matter."""

    def _nights(
        self,
        *,
        overnight_kwh: float,
        daytime_kwh: float = 2.0,
        count: int = BASELINE_NIGHTS,
    ) -> list[FakeInterval]:
        """Days whose overnight hours are quiet and whose days are not."""
        rows: list[FakeInterval] = []
        for offset in range(count):
            day = LAST_DAY - timedelta(days=offset)
            rows.extend(
                make_day(
                    day,
                    shape={h: overnight_kwh for h in range(BASELINE_END_HOUR)},
                    consumption=daytime_kwh,
                )
            )
        return rows

    def test_it_measures_the_quietest_overnight_half_hour(self) -> None:
        """0.2 kWh in half an hour is 400 W."""
        baseline = baseline_load(self._nights(overnight_kwh=0.2), through=LAST_DAY)
        assert baseline is not None
        assert baseline.watts == pytest.approx(400.0)

    def test_it_reports_the_daily_energy_that_implies(self) -> None:
        baseline = baseline_load(self._nights(overnight_kwh=0.2), through=LAST_DAY)
        assert baseline is not None
        assert baseline.daily_kwh == pytest.approx(9.6)

    def test_daytime_usage_does_not_raise_it(self) -> None:
        """The evening peak is exactly what the overnight window excludes."""
        quiet = baseline_load(
            self._nights(overnight_kwh=0.2, daytime_kwh=2.0), through=LAST_DAY
        )
        busy = baseline_load(
            self._nights(overnight_kwh=0.2, daytime_kwh=9.0), through=LAST_DAY
        )
        assert quiet is not None and busy is not None
        assert quiet.watts == pytest.approx(busy.watts)

    def test_without_thermostats_a_running_ac_sets_the_floor(self) -> None:
        """The failure this feature exists to fix, pinned as a contrast case."""
        rows = self._nights(overnight_kwh=1.5)
        baseline = baseline_load(rows, through=LAST_DAY)
        assert baseline is not None
        assert baseline.watts == pytest.approx(3000.0)
        assert baseline.hvac_filtered is False

    def test_excluding_hvac_runtime_uncovers_the_real_baseline(self) -> None:
        """Same nights, but the compressor ran from midnight to 4 AM.

        The remaining 4-5 AM hour is the only honest reading, and it is the
        one the sensor should report.
        """
        rows: list[FakeInterval] = []
        windows: list[TimeWindow] = []
        for offset in range(BASELINE_NIGHTS):
            day = LAST_DAY - timedelta(days=offset)
            rows.extend(
                make_day(
                    day,
                    shape={0: 1.5, 1: 1.5, 2: 1.5, 3: 1.5, 4: 0.2},
                    consumption=2.0,
                )
            )
            windows.append(TimeWindow(at(day, 0), at(day, 4)))

        baseline = baseline_load(rows, through=LAST_DAY, hvac_windows=windows)
        assert baseline is not None
        assert baseline.watts == pytest.approx(400.0)
        assert baseline.hvac_filtered is True
        assert baseline.excluded_intervals == BASELINE_NIGHTS * 8

    def test_a_partial_overlap_still_excludes_the_interval(self) -> None:
        """A compressor running for ten minutes contaminates the whole half hour."""
        rows: list[FakeInterval] = []
        windows: list[TimeWindow] = []
        for offset in range(BASELINE_NIGHTS):
            day = LAST_DAY - timedelta(days=offset)
            rows.extend(
                make_day(
                    day, shape={0: 1.5, 1: 0.2, 2: 0.2, 3: 0.2, 4: 0.2}, consumption=2.0
                )
            )
            # Ten minutes inside the 00:00 interval only.
            windows.append(TimeWindow(at(day, 0, 10), at(day, 0, 20)))

        baseline = baseline_load(rows, through=LAST_DAY, hvac_windows=windows)
        assert baseline is not None
        assert baseline.excluded_intervals == BASELINE_NIGHTS
        assert baseline.watts == pytest.approx(400.0)

    def test_a_night_fully_covered_by_hvac_is_skipped_not_zeroed(self) -> None:
        """Four usable nights out of seven still answers the question."""
        rows: list[FakeInterval] = []
        windows: list[TimeWindow] = []
        for offset in range(BASELINE_NIGHTS):
            day = LAST_DAY - timedelta(days=offset)
            rows.extend(
                make_day(day, shape={h: 0.2 for h in range(5)}, consumption=2.0)
            )
            if offset < 3:
                windows.append(TimeWindow(at(day, 0), at(day, 5)))

        baseline = baseline_load(rows, through=LAST_DAY, hvac_windows=windows)
        assert baseline is not None
        assert baseline.nights == BASELINE_NIGHTS - 3
        assert baseline.watts == pytest.approx(400.0)

    def test_every_night_covered_reports_nothing(self) -> None:
        """A week of continuous air conditioning has no baseline to show."""
        rows: list[FakeInterval] = []
        windows: list[TimeWindow] = []
        for offset in range(BASELINE_NIGHTS):
            day = LAST_DAY - timedelta(days=offset)
            rows.extend(make_day(day, consumption=1.5))
            windows.append(TimeWindow(at(day, 0), at(day, 5)))

        assert baseline_load(rows, through=LAST_DAY, hvac_windows=windows) is None

    def test_one_odd_night_does_not_move_the_median(self) -> None:
        rows = self._nights(overnight_kwh=0.2)
        rows = [r for r in rows if r.timestamp.date() != LAST_DAY]
        rows.extend(
            make_day(LAST_DAY, shape={h: 0.01 for h in range(5)}, consumption=2.0)
        )

        baseline = baseline_load(rows, through=LAST_DAY)
        assert baseline is not None
        assert baseline.watts == pytest.approx(400.0)

    def test_too_few_nights_reports_nothing(self) -> None:
        rows = self._nights(overnight_kwh=0.2, count=MIN_BASELINE_NIGHTS - 1)
        assert baseline_load(rows, through=LAST_DAY) is None

    def test_the_minimum_is_exactly_min_baseline_nights(self) -> None:
        rows = self._nights(overnight_kwh=0.2, count=MIN_BASELINE_NIGHTS)
        assert baseline_load(rows, through=LAST_DAY) is not None

    def test_it_reports_the_nights_it_used(self) -> None:
        baseline = baseline_load(self._nights(overnight_kwh=0.2), through=LAST_DAY)
        assert baseline is not None
        assert baseline.last_night == LAST_DAY
        assert baseline.first_night == LAST_DAY - timedelta(days=BASELINE_NIGHTS - 1)
        assert baseline.sampled_intervals == BASELINE_NIGHTS * BASELINE_END_HOUR * 2

    def test_incomplete_days_are_not_sampled(self) -> None:
        """A partly published day's missing hours are not quiet hours.

        Two intervals of a barely-started day would otherwise be treated as
        that night's minimum and drag the median down.
        """
        rows: list[FakeInterval] = []
        for offset in range(1, BASELINE_NIGHTS):
            day = LAST_DAY - timedelta(days=offset)
            rows.extend(
                make_day(day, shape={h: 0.2 for h in range(5)}, consumption=2.0)
            )
        rows.extend(make_day(LAST_DAY, count=4, shape={0: 0.001}))

        baseline = baseline_load(rows, through=LAST_DAY)
        assert baseline is not None
        assert baseline.last_night == LAST_DAY - timedelta(days=1)
        assert baseline.watts == pytest.approx(400.0)

    def test_empty_input_reports_nothing(self) -> None:
        assert baseline_load([], through=LAST_DAY) is None
