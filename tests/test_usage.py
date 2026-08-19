"""Tests for the pure usage/statistics helpers."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

# Add the custom_components/dominion_energy directory to sys.path so we can
# import usage.py directly without pulling in homeassistant via __init__.py
_pkg_dir = str(
    Path(__file__).resolve().parent.parent / "custom_components" / "dominion_energy"
)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from rates import (  # noqa: E402
    VA_SCHEDULE_1,
    Season,
    calculate_schedule1_interval_cost,
    calculate_schedule1_period_bill,
)
from usage import (  # noqa: E402
    COST_ANOMALY_TOLERANCE,
    DEFAULT_BILLING_PERIOD_DAYS,
    MIN_BILLING_PERIOD_DAYS,
    MIN_COST_ANOMALY_DAYS,
    aggregate_hourly,
    billing_period_days,
    billing_period_end,
    billing_period_start,
    build_cumulative_statistics,
    day_looks_complete,
    deduplicate_hourly_by_utc,
    filter_incomplete_days,
    find_cost_anomalies,
    shift_months,
)

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class FakeInterval:
    """Stand-in for dompower's IntervalUsageData."""

    timestamp: datetime
    consumption: float


def make_day(day: date, kwh_per_interval: float = 0.5, count: int = 48):
    """Build a day of 30-minute intervals in local time."""
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=NY)
    return [
        FakeInterval(start + timedelta(minutes=30 * i), kwh_per_interval)
        for i in range(count)
    ]


def no_cost(_interval, _cumulative):
    """Cost function that ignores everything."""
    return 0.0


class TestShiftMonths:
    """Tests for whole-month date shifting."""

    def test_forward(self):
        assert shift_months(date(2026, 1, 18), 1) == date(2026, 2, 18)

    def test_backward(self):
        assert shift_months(date(2026, 1, 18), -1) == date(2025, 12, 18)

    def test_clamps_to_short_month(self):
        assert shift_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_year_wrap(self):
        assert shift_months(date(2026, 12, 5), 1) == date(2027, 1, 5)
        assert shift_months(date(2026, 1, 5), -13) == date(2024, 12, 5)

    def test_zero_is_identity(self):
        assert shift_months(date(2026, 7, 18), 0) == date(2026, 7, 18)


class TestBillingPeriodDays:
    """Tests for deriving the billing period length."""

    def test_real_period(self):
        # Meter read to meter read
        assert billing_period_days(date(2026, 7, 18), date(2026, 8, 17)) == 30

    def test_longer_period(self):
        assert billing_period_days(date(2026, 7, 18), date(2026, 8, 21)) == 34

    def test_missing_forecast_falls_back(self):
        assert billing_period_days(None, None) == DEFAULT_BILLING_PERIOD_DAYS
        assert billing_period_days(date(2026, 7, 18), None) == (
            DEFAULT_BILLING_PERIOD_DAYS
        )

    def test_implausible_period_falls_back(self):
        # A single day, or a reversed/garbage period, must never be used to
        # prorate the monthly customer charge (bug: $7.58 applied per day).
        assert billing_period_days(date(2026, 7, 18), date(2026, 7, 18)) == (
            DEFAULT_BILLING_PERIOD_DAYS
        )
        assert billing_period_days(date(2026, 8, 17), date(2026, 7, 18)) == (
            DEFAULT_BILLING_PERIOD_DAYS
        )
        assert billing_period_days(date(2026, 1, 1), date(2026, 12, 31)) == (
            DEFAULT_BILLING_PERIOD_DAYS
        )

    def test_end_not_in_the_future_falls_back(self):
        """The regression: a plausible *length* is not a plausible period.

        Observed live on 2026-08-12. The forecast reported 07-23 -> 08-12 while
        the meter's real cycle still had eight days to run, because the API
        returns the date usage is published through rather than the next read
        (and `dompower` substitutes `date.today()` when the field is absent).
        Twenty days clears MIN_BILLING_PERIOD_DAYS, so the length check alone
        waves it through and every downstream consumer believes the period is
        over.
        """
        start, truncated = date(2026, 7, 23), date(2026, 8, 12)
        assert (truncated - start).days == MIN_BILLING_PERIOD_DAYS
        assert billing_period_days(start, truncated) == MIN_BILLING_PERIOD_DAYS
        assert billing_period_days(start, truncated, today=truncated) == (
            DEFAULT_BILLING_PERIOD_DAYS
        )

    def test_end_already_past_falls_back(self):
        assert (
            billing_period_days(
                date(2026, 7, 23), date(2026, 8, 12), today=date(2026, 8, 15)
            )
            == DEFAULT_BILLING_PERIOD_DAYS
        )

    def test_future_end_is_still_trusted(self):
        assert (
            billing_period_days(
                date(2026, 7, 23), date(2026, 8, 20), today=date(2026, 8, 12)
            )
            == 28
        )

    def test_caller_supplied_default_is_used(self):
        # The last bill's real read-to-read length beats a nominal 30.
        assert (
            billing_period_days(
                date(2026, 7, 23),
                date(2026, 8, 12),
                default=28,
                today=date(2026, 8, 12),
            )
            == 28
        )


class TestProjectionDoesNotDegenerate:
    """The projection must not collapse to usage-to-date mid-cycle.

    `project_period_usage` is period_to_date / days_observed * days_in_period.
    When the reported end tracks today those two day counts are the same
    number, they cancel, and the projection returns exactly what has been used
    so far -- which reads as a confident forecast rather than a missing one.
    """

    START = date(2026, 7, 23)
    TO_DATE = 1323.505

    @staticmethod
    def _project(to_date, days_observed, days_in_period):
        return max(to_date / days_observed * days_in_period, to_date)

    def test_truncated_end_would_collapse_the_projection(self):
        collapsed = self._project(
            self.TO_DATE, 20, billing_period_days(self.START, date(2026, 8, 12))
        )
        assert collapsed == pytest.approx(self.TO_DATE)

    def test_repaired_end_keeps_projecting(self):
        projected = self._project(
            self.TO_DATE,
            20,
            billing_period_days(
                self.START, date(2026, 8, 12), default=28, today=date(2026, 8, 12)
            ),
        )
        assert projected == pytest.approx(1852.9, abs=0.1)
        assert projected > self.TO_DATE * 1.3


class TestBillingPeriodEnd:
    """Tests for repairing a period end that cannot be trusted."""

    def test_plausible_end_is_returned_unchanged(self):
        assert billing_period_end(date(2026, 7, 18), date(2026, 8, 17)) == (
            date(2026, 8, 17)
        )

    def test_period_truncated_at_today_is_extended(self):
        # The forecast reported the period as ending on the day it was read,
        # making a monthly cycle look 19 days long.
        assert billing_period_end(date(2026, 7, 23), date(2026, 8, 11)) == (
            date(2026, 8, 22)
        )

    def test_missing_end_falls_back_to_a_full_cycle(self):
        assert billing_period_end(date(2026, 7, 18), None) == date(2026, 8, 17)

    def test_missing_start_is_left_alone(self):
        assert billing_period_end(None, date(2026, 8, 17)) == date(2026, 8, 17)
        assert billing_period_end(None, None) is None

    def test_end_tracking_today_is_extended_by_the_real_cycle(self):
        # The live 2026-08-12 case, with the last bill's 28-day cycle as the
        # default rather than a nominal 30.
        assert billing_period_end(
            date(2026, 7, 23), date(2026, 8, 12), 28, date(2026, 8, 12)
        ) == date(2026, 8, 20)

    def test_repaired_period_lands_in_the_right_season(self):
        """The regression this exists for.

        A cycle running 09-20 to 10-19 has an October midpoint, so the tariff
        prices it at winter generation rates. Truncated at a late-September
        "today" the midpoint falls in September and the whole bill is priced as
        summer -- 4.62 c/kWh on generation over 800 kWh instead of 2.70.
        """
        start, truncated = date(2026, 9, 20), date(2026, 9, 25)
        summer = calculate_schedule1_period_bill(2000.0, start, truncated)
        repaired = calculate_schedule1_period_bill(
            2000.0, start, billing_period_end(start, truncated)
        )
        assert summer.season is Season.SUMMER
        assert repaired.season is Season.WINTER
        assert summer.total - repaired.total > 20.0


class TestBillingPeriodStart:
    """Tests for locating the billing period containing a date."""

    anchor = date(2026, 7, 18)

    def test_date_inside_current_period(self):
        assert billing_period_start(date(2026, 7, 18), self.anchor) == self.anchor
        assert billing_period_start(date(2026, 8, 5), self.anchor) == self.anchor
        assert billing_period_start(date(2026, 8, 17), self.anchor) == self.anchor

    def test_calendar_month_start_is_not_a_period_start(self):
        # Regression for the tiering-reset bug: the 1st of the month sits in the
        # middle of a billing period, not at its start.
        assert billing_period_start(date(2026, 8, 1), self.anchor) == date(2026, 7, 18)

    def test_previous_periods(self):
        assert billing_period_start(date(2026, 7, 17), self.anchor) == date(2026, 6, 18)
        assert billing_period_start(date(2026, 6, 18), self.anchor) == date(2026, 6, 18)
        assert billing_period_start(date(2026, 5, 20), self.anchor) == date(2026, 5, 18)

    def test_next_period(self):
        assert billing_period_start(date(2026, 8, 18), self.anchor) == date(2026, 8, 18)

    def test_short_month_anchor_is_clamped(self):
        anchor = date(2026, 3, 31)
        assert billing_period_start(date(2026, 3, 1), anchor) == date(2026, 2, 28)
        assert billing_period_start(date(2026, 2, 28), anchor) == date(2026, 2, 28)

    def test_no_anchor_falls_back_to_calendar_month(self):
        assert billing_period_start(date(2026, 8, 5), None) == date(2026, 8, 1)

    def test_every_day_of_a_year_maps_to_a_period(self):
        # The period start must never be after the date it contains, and the
        # next period must always start later.
        day = date(2025, 9, 1)
        while day < date(2026, 9, 1):
            start = billing_period_start(day, self.anchor)
            assert start <= day
            assert billing_period_start(start - timedelta(days=1), self.anchor) < start
            day += timedelta(days=1)


class TestFilterIncompleteDays:
    """Tests for dropping days the API has not fully published."""

    def test_keeps_a_good_day(self):
        intervals = make_day(date(2026, 7, 15))
        kept, skipped = filter_incomplete_days(intervals)
        assert skipped == []
        assert kept == intervals

    def test_drops_zero_day(self):
        good = make_day(date(2026, 7, 15))
        zero = make_day(date(2026, 7, 16), kwh_per_interval=0.0)
        kept, skipped = filter_incomplete_days(good + zero)
        assert skipped == [date(2026, 7, 16)]
        assert kept == good

    def test_drops_sparse_day(self):
        good = make_day(date(2026, 7, 15))
        # A day where only two intervals have any data yet
        sparse = [
            FakeInterval(datetime(2026, 7, 16, 0, 0, tzinfo=NY), 0.4),
            FakeInterval(datetime(2026, 7, 16, 0, 30, tzinfo=NY), 0.3),
        ]
        kept, skipped = filter_incomplete_days(good + sparse)
        assert skipped == [date(2026, 7, 16)]
        assert kept == good

    def test_keeps_day_at_the_threshold(self):
        day = [
            FakeInterval(datetime(2026, 7, 16, i, 0, tzinfo=NY), 0.4) for i in range(4)
        ]
        kept, skipped = filter_incomplete_days(day)
        assert skipped == []
        assert len(kept) == 4

    def test_multiple_bad_days_are_reported_sorted(self):
        good = make_day(date(2026, 7, 15))
        bad = make_day(date(2026, 7, 17), kwh_per_interval=0.0) + make_day(
            date(2026, 7, 16), kwh_per_interval=0.0
        )
        kept, skipped = filter_incomplete_days(good + bad)
        assert skipped == [date(2026, 7, 16), date(2026, 7, 17)]
        assert kept == good

    def test_empty_input(self):
        assert filter_incomplete_days([]) == ([], [])


class TestDayLooksComplete:
    """Tests for the cache gate on a fully published day."""

    def test_full_day(self):
        assert day_looks_complete(make_day(date(2026, 7, 15))) is True

    def test_dst_spring_forward_day_has_46_intervals(self):
        intervals = make_day(date(2026, 3, 8), count=46)
        assert day_looks_complete(intervals) is True

    def test_partial_day(self):
        assert day_looks_complete(make_day(date(2026, 7, 15), count=20)) is False

    def test_empty_day(self):
        assert day_looks_complete([]) is False

    def test_enough_intervals_but_stops_early(self):
        # 44 intervals that all sit before 22:00 local
        start = datetime(2026, 7, 15, 0, 0, tzinfo=NY)
        intervals = [
            FakeInterval(start + timedelta(minutes=15 * i), 0.5) for i in range(44)
        ]
        assert day_looks_complete(intervals) is False


class TestDeduplicateHourlyByUtc:
    """Tests for collapsing local hours onto UTC instants."""

    def test_spring_forward_folds_two_local_keys_into_one_utc_hour(self):
        # 2026-03-08 02:00 local does not exist; it and 03:00 EDT both convert
        # to 07:00 UTC and must be merged, not overwrite each other.
        hourly = {
            datetime(2026, 3, 8, 1, 0, tzinfo=NY): 1.0,
            datetime(2026, 3, 8, 2, 0, tzinfo=NY): 2.0,
            datetime(2026, 3, 8, 3, 0, tzinfo=NY): 4.0,
        }
        utc = deduplicate_hourly_by_utc(hourly)

        assert len(utc) == 2
        assert utc[datetime(2026, 3, 8, 6, 0, tzinfo=UTC)] == pytest.approx(1.0)
        assert utc[datetime(2026, 3, 8, 7, 0, tzinfo=UTC)] == pytest.approx(6.0)
        assert sum(utc.values()) == pytest.approx(sum(hourly.values()))

    def test_fall_back_hours_keyed_by_utc_are_distinct(self):
        # 2026-11-01 01:00 happens twice; keyed by UTC instant the two hours are
        # separate rows, and no value is lost or duplicated.
        hourly = {
            datetime(2026, 11, 1, 0, 0, tzinfo=NY): 1.0,
            datetime(2026, 11, 1, 1, 0, tzinfo=NY, fold=0): 2.0,
            datetime(2026, 11, 1, 2, 0, tzinfo=NY): 4.0,
        }
        # Added separately: as a dict literal the two fold variants of 01:00
        # would collide (see test_fall_back_repeated_hour_merges).
        second_one_am = {datetime(2026, 11, 1, 1, 0, tzinfo=NY, fold=1): 3.0}
        utc = deduplicate_hourly_by_utc(hourly) | deduplicate_hourly_by_utc(
            second_one_am
        )

        assert len(utc) == 4
        assert utc[datetime(2026, 11, 1, 5, 0, tzinfo=UTC)] == pytest.approx(2.0)
        assert utc[datetime(2026, 11, 1, 6, 0, tzinfo=UTC)] == pytest.approx(3.0)
        assert sum(utc.values()) == pytest.approx(10.0)

    def test_fall_back_repeated_hour_merges(self):
        """Documents the PEP 495 limitation on the 25-hour day.

        Two same-zone datetimes differing only in ``fold`` compare and hash
        equal, so the repeated 01:00 shares one bucket upstream of this
        function. The day's total stays exact; that UTC hour carries both.
        """
        hourly: dict[datetime, float] = {}
        instant = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # local midnight
        for i in range(25):
            local = (instant + timedelta(hours=i)).astimezone(NY)
            hourly[local] = hourly.get(local, 0.0) + 1.0
        utc = deduplicate_hourly_by_utc(hourly)

        assert len(utc) == 24
        assert len(set(utc)) == len(utc)
        assert sum(utc.values()) == pytest.approx(25.0), "no energy may be lost"
        assert utc[datetime(2026, 11, 1, 5, 0, tzinfo=UTC)] == pytest.approx(2.0)

    def test_normal_day_is_unchanged(self):
        hourly = {
            datetime(2026, 7, 15, hour, 0, tzinfo=NY): float(hour) for hour in range(24)
        }
        utc = deduplicate_hourly_by_utc(hourly)
        assert len(utc) == 24
        assert sum(utc.values()) == pytest.approx(sum(hourly.values()))


class TestAggregateHourly:
    """Tests for the 30-minute to hourly aggregation."""

    def test_pairs_of_intervals_become_one_hour(self):
        intervals = make_day(date(2026, 7, 15), kwh_per_interval=0.25)
        consumption, _cost = aggregate_hourly(
            intervals, no_cost, partial(billing_period_start, anchor=None)
        )

        assert len(consumption) == 24
        assert all(value == pytest.approx(0.5) for value in consumption.values())

    def test_spring_forward_day_yields_23_local_hours(self):
        # 46 intervals on the short day: 00:00-01:30 then 03:00 onwards
        intervals = [
            FakeInterval(datetime(2026, 3, 8, 0, 0, tzinfo=NY), 0.25),
            FakeInterval(datetime(2026, 3, 8, 0, 30, tzinfo=NY), 0.25),
            FakeInterval(datetime(2026, 3, 8, 1, 0, tzinfo=NY), 0.25),
            FakeInterval(datetime(2026, 3, 8, 1, 30, tzinfo=NY), 0.25),
            FakeInterval(datetime(2026, 3, 8, 3, 0, tzinfo=NY), 0.25),
            FakeInterval(datetime(2026, 3, 8, 3, 30, tzinfo=NY), 0.25),
        ]
        consumption, _cost = aggregate_hourly(
            intervals, no_cost, partial(billing_period_start, anchor=None)
        )
        utc = deduplicate_hourly_by_utc(consumption)

        assert len(utc) == 3
        assert sum(utc.values()) == pytest.approx(1.5)

    def test_fall_back_day_end_to_end_is_sane(self):
        # The 25-hour day: 50 half-hour intervals, with the repeated 01:00
        # marked by fold as a correct tz-aware source would.
        intervals: list[FakeInterval] = []
        instant = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # local midnight
        for i in range(50):
            intervals.append(
                FakeInterval((instant + timedelta(minutes=30 * i)).astimezone(NY), 0.5)
            )
        assert intervals[-1].timestamp.date() == date(2026, 11, 1)

        consumption, _cost = aggregate_hourly(
            intervals, no_cost, partial(billing_period_start, anchor=None)
        )
        utc = deduplicate_hourly_by_utc(consumption)
        rows = build_cumulative_statistics(utc)

        # Nothing is lost, keys are unique and ascending, sums are monotonic
        assert sum(utc.values()) == pytest.approx(25.0)
        assert len(set(utc)) == len(utc)
        assert [r.start for r in rows] == sorted(r.start for r in rows)
        assert [r.sum for r in rows] == sorted(r.sum for r in rows)
        assert rows[-1].sum == pytest.approx(25.0)

    def test_cost_fn_receives_cumulative_within_period(self):
        seen: list[float] = []

        def record(interval, cumulative):
            seen.append(cumulative)
            return 0.0

        intervals = make_day(date(2026, 7, 20), kwh_per_interval=1.0, count=4)
        aggregate_hourly(
            intervals, record, partial(billing_period_start, anchor=date(2026, 7, 18))
        )

        assert seen == [0.0, 1.0, 2.0, 3.0]

    def test_cumulative_resets_on_billing_period_not_calendar_month(self):
        """Regression: tiering must reset on the bill cycle, not the 1st."""
        anchor = date(2026, 7, 18)
        intervals: list[FakeInterval] = []
        day = date(2026, 7, 18)
        while day <= date(2026, 8, 5):
            intervals += make_day(day, kwh_per_interval=1.0)
            day += timedelta(days=1)

        by_period: list[tuple[datetime, float]] = []
        by_calendar_month: list[tuple[datetime, float]] = []

        def record(target):
            def _record(interval, cumulative):
                target.append((interval.timestamp, cumulative))
                return 0.0

            return _record

        aggregate_hourly(
            intervals, record(by_period), partial(billing_period_start, anchor=anchor)
        )
        aggregate_hourly(
            intervals, record(by_calendar_month), lambda d: d.replace(day=1)
        )

        # 48 kWh/day * 17 days is well past the 800 kWh Schedule 1 boundary by
        # 2026-08-05, but only if the counter does not reset on 2026-08-01.
        assert max(c for _, c in by_period) > 800
        assert max(c for _, c in by_calendar_month) < 800

        first_august = next(
            c for ts, c in by_period if ts.date() == date(2026, 8, 1) and ts.hour == 0
        )
        assert first_august == pytest.approx(48 * 14)

    def test_cumulative_resets_at_the_period_boundary(self):
        anchor = date(2026, 7, 18)
        intervals = make_day(date(2026, 7, 17), kwh_per_interval=1.0) + make_day(
            date(2026, 7, 18), kwh_per_interval=1.0
        )
        seen: list[tuple[date, float]] = []

        def record(interval, cumulative):
            seen.append((interval.timestamp.date(), cumulative))
            return 0.0

        aggregate_hourly(
            intervals, record, partial(billing_period_start, anchor=anchor)
        )

        assert seen[0] == (date(2026, 7, 17), 0.0)
        # First interval of the new period starts over at zero
        assert next(c for d, c in seen if d == date(2026, 7, 18)) == 0.0

    def test_unsorted_input_is_sorted(self):
        intervals = list(reversed(make_day(date(2026, 7, 15), kwh_per_interval=1.0)))
        seen: list[float] = []

        def record(interval, cumulative):
            seen.append(cumulative)
            return 0.0

        aggregate_hourly(intervals, record, partial(billing_period_start, anchor=None))
        assert seen == sorted(seen)


class TestBuildCumulativeStatistics:
    """Tests for the cumulative sum chain fed to the recorder."""

    def _values(self, count: int, value: float = 1.0) -> dict[datetime, float]:
        base = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
        return {base + timedelta(hours=i): value for i in range(count)}

    def test_sums_are_monotonic_and_ascending_by_start(self):
        rows = build_cumulative_statistics(self._values(24, 2.0))

        assert [row.start for row in rows] == sorted(row.start for row in rows)
        assert [row.sum for row in rows] == sorted(row.sum for row in rows)
        assert rows[0].sum == pytest.approx(2.0)
        assert rows[-1].sum == pytest.approx(48.0)

    def test_start_sum_continues_an_existing_chain(self):
        rows = build_cumulative_statistics(self._values(3, 1.5), start_sum=100.0)
        assert [row.sum for row in rows] == pytest.approx([101.5, 103.0, 104.5])

    def test_rebuilt_window_stays_continuous(self):
        """A rewritten window must continue from the sum before it."""
        original = build_cumulative_statistics(self._values(48, 1.0))
        # Rewrite the last 24 rows with different values, continuing the chain
        # from the row immediately before the rebuilt window.
        boundary = original[23]
        window = {row.start: 3.0 for row in original if row.start > boundary.start}
        rebuilt = build_cumulative_statistics(window, start_sum=boundary.sum)

        combined = original[:24] + rebuilt
        sums = [row.sum for row in combined]
        assert sums == sorted(sums), "cumulative sum must never go backwards"
        assert rebuilt[0].sum == pytest.approx(boundary.sum + 3.0)
        assert rebuilt[-1].sum == pytest.approx(24.0 + 24 * 3.0)
        # Every step equals the row's own state
        for previous, current in zip(combined, combined[1:], strict=False):
            assert current.sum - previous.sum == pytest.approx(current.state)

    def test_out_of_order_input_is_sorted(self):
        base = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
        values = {
            base + timedelta(hours=2): 3.0,
            base: 1.0,
            base + timedelta(hours=1): 2.0,
        }
        rows = build_cumulative_statistics(values)
        assert [row.state for row in rows] == pytest.approx([1.0, 2.0, 3.0])
        assert [row.sum for row in rows] == pytest.approx([1.0, 3.0, 6.0])

    def test_empty_input(self):
        assert build_cumulative_statistics({}) == []


class TestCustomerChargeProration:
    """Regression tests for the over-applied monthly customer charge."""

    @staticmethod
    def _day_cost(day: date, billing_days: int, customer_charge: float) -> float:
        schedule = replace(VA_SCHEDULE_1, customer_charge=customer_charge)
        cumulative = 0.0
        total = 0.0
        for interval in make_day(day, kwh_per_interval=0.5):
            total += calculate_schedule1_interval_cost(
                interval.consumption,
                interval.timestamp,
                cumulative,
                schedule,
                billing_period_days=billing_days,
            )
            cumulative += interval.consumption
        return total

    def test_one_day_carries_one_thirtieth_of_the_customer_charge(self):
        """A single day must not carry a whole month's customer charge."""
        billing_days = billing_period_days(date(2026, 7, 18), date(2026, 8, 17))
        assert billing_days == 30

        day = date(2026, 7, 20)
        with_charge = self._day_cost(day, billing_days, VA_SCHEDULE_1.customer_charge)
        without_charge = self._day_cost(day, billing_days, 0.0)
        charge_share = with_charge - without_charge

        assert charge_share == pytest.approx(VA_SCHEDULE_1.customer_charge / 30)
        assert charge_share < VA_SCHEDULE_1.customer_charge / 10

    def test_old_span_derived_billing_days_inflated_a_single_day(self):
        """The previous behaviour (span of one day -> billing_days=1)."""
        day = date(2026, 7, 20)
        buggy = self._day_cost(day, 1, VA_SCHEDULE_1.customer_charge)
        fixed = self._day_cost(day, 30, VA_SCHEDULE_1.customer_charge)

        # The bug added a full month's charge to every single day
        assert buggy - fixed == pytest.approx(
            VA_SCHEDULE_1.customer_charge * (1 - 1 / 30)
        )

    def test_partial_period_carries_proportional_share(self):
        billing_days = 30
        total_charge = 0.0
        day = date(2026, 7, 18)
        for _ in range(10):
            total_charge += self._day_cost(
                day, billing_days, VA_SCHEDULE_1.customer_charge
            ) - self._day_cost(day, billing_days, 0.0)
            day += timedelta(days=1)

        assert total_charge == pytest.approx(
            VA_SCHEDULE_1.customer_charge * 10 / 30, rel=1e-6
        )

    def test_full_period_carries_the_whole_charge(self):
        billing_days = 30
        total_charge = 0.0
        day = date(2026, 7, 18)
        for _ in range(billing_days):
            total_charge += self._day_cost(
                day, billing_days, VA_SCHEDULE_1.customer_charge
            ) - self._day_cost(day, billing_days, 0.0)
            day += timedelta(days=1)

        assert total_charge == pytest.approx(VA_SCHEDULE_1.customer_charge, rel=1e-6)


class TestTierBoundaryFollowsBillingPeriod:
    """Regression: the 800 kWh tier resets with the bill, not the calendar."""

    @staticmethod
    def _period_cost(period_start_of) -> float:
        intervals: list[FakeInterval] = []
        day = date(2026, 7, 18)
        while day <= date(2026, 8, 10):
            intervals += make_day(day, kwh_per_interval=1.0)
            day += timedelta(days=1)

        def cost(interval, cumulative):
            return calculate_schedule1_interval_cost(
                interval.consumption,
                interval.timestamp,
                cumulative,
                VA_SCHEDULE_1,
                billing_period_days=30,
            )

        _consumption, hourly_cost = aggregate_hourly(intervals, cost, period_start_of)
        return sum(hourly_cost.values())

    def test_billing_period_tiering_differs_from_calendar_month(self):
        by_period = self._period_cost(
            partial(billing_period_start, anchor=date(2026, 7, 18))
        )
        by_calendar_month = self._period_cost(lambda d: d.replace(day=1))

        # Summer over-800 rates are net higher than the under-800 rates, so
        # correctly crossing the boundary costs more than a spurious reset.
        assert by_period > by_calendar_month


# The fortnight around the incident, as the Energy Dashboard showed it: day
# totals differenced out of the cumulative sums. 15 August carries a second
# copy of its own cost and nothing else about it is wrong.
AUGUST = [
    (date(2026, 8, 6), 76.03, 14.03),
    (date(2026, 8, 7), 84.33, 15.53),
    (date(2026, 8, 8), 87.44, 16.10),
    (date(2026, 8, 9), 83.25, 15.34),
    (date(2026, 8, 10), 79.28, 14.62),
    (date(2026, 8, 11), 64.81, 12.00),
    (date(2026, 8, 12), 74.83, 13.81),
    (date(2026, 8, 13), 83.89, 15.45),
    (date(2026, 8, 14), 78.05, 14.40),
    (date(2026, 8, 15), 90.25, 32.95),
    (date(2026, 8, 16), 84.67, 15.35),
    (date(2026, 8, 17), 79.91, 14.50),
    (date(2026, 8, 18), 73.40, 13.34),
]

HEALTHY = [(day, kwh, cost) for day, kwh, cost in AUGUST if day != date(2026, 8, 15)]


class TestFindCostAnomalies:
    """The catch that should have spoken up on 16 August."""

    def test_it_finds_the_duplicated_day(self) -> None:
        found = find_cost_anomalies(AUGUST)
        assert [anomaly.day for anomaly in found] == [date(2026, 8, 15)]

    def test_it_reports_how_far_off_the_day_is(self) -> None:
        (anomaly,) = find_cost_anomalies(AUGUST)
        assert anomaly.ratio == pytest.approx(1.98, abs=0.01)
        assert anomaly.rate == pytest.approx(0.365, abs=0.001)
        assert anomaly.baseline_rate == pytest.approx(0.1843, abs=0.0005)

    def test_real_tariff_movement_is_not_an_anomaly(self) -> None:
        """Sixty days of Schedule 1 spanned a factor of 1.06. None of it counts."""
        assert find_cost_anomalies(HEALTHY) == []

    def test_a_halved_day_is_caught_too(self) -> None:
        """A seed that was too low reads as a bargain, and is just as wrong."""
        halved = [
            (day, kwh, cost / 3 if day == date(2026, 8, 12) else cost)
            for day, kwh, cost in HEALTHY
        ]
        assert [a.day for a in find_cost_anomalies(halved)] == [date(2026, 8, 12)]

    def test_a_fresh_install_accuses_nobody(self) -> None:
        assert find_cost_anomalies(AUGUST[: MIN_COST_ANOMALY_DAYS - 1]) == []

    def test_an_almost_empty_day_is_not_evidence(self) -> None:
        """Rounding on a 0.2 kWh day says nothing about the tariff."""
        quiet = [*HEALTHY, (date(2026, 8, 19), 0.2, 0.9)]
        assert find_cost_anomalies(quiet) == []

    def test_the_tolerance_leaves_room_on_both_sides(self) -> None:
        """Below a doubling, above real tariff movement."""
        assert 1.06 < COST_ANOMALY_TOLERANCE < 1.8

    def test_it_survives_a_day_with_no_cost(self) -> None:
        assert find_cost_anomalies([*HEALTHY, (date(2026, 8, 19), 50.0, 0.0)]) == []
