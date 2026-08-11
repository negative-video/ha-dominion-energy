"""Tests for VA Schedule 1 rate calculations."""

from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
import sys

import pytest

# Add the custom_components/dominion_energy directory to sys.path so we can
# import rates.py directly without pulling in homeassistant via __init__.py
_pkg_dir = str(
    Path(__file__).resolve().parent.parent / "custom_components" / "dominion_energy"
)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from rates import (  # noqa: E402
    LATEST_SCHEDULE_EFFECTIVE_DATE,
    VA_SCHEDULE_1,
    VA_SCHEDULE_1_HISTORY,
    ConsumptionTaxTier,
    Season,
    TieredRate,
    bill_discrepancy,
    calculate_consumption_tax,
    calculate_schedule1_interval_cost,
    calculate_schedule1_period_bill,
    calculate_tiered_cost,
    days_since_schedule_change,
    get_schedule_for_date,
    get_season,
    is_schedule_possibly_stale,
)

# The rate set the original hand-checked worksheet figures below were derived
# from (docs/bill-calculator-worksheet-va.xlsx, "Last Updated 2025-12-19").
# These tests exercise the calculation engine, so they are pinned to a fixed
# schedule rather than to whatever happens to be current.
SCHEDULE_2026_01_01 = get_schedule_for_date(date(2026, 1, 1))


def _accumulate_month(kwh, schedule, year, month, days=30):
    """Sum interval costs for a flat-usage month, mimicking the coordinator."""
    kwh_per_interval = kwh / (48 * days)
    total_cost = 0.0
    cumulative = 0.0
    for day in range(days):
        for half_hour in range(48):
            dt = datetime(year, month, 1 + day, half_hour // 2, (half_hour % 2) * 30)
            total_cost += calculate_schedule1_interval_cost(
                kwh_per_interval,
                dt,
                cumulative,
                schedule,
                billing_period_days=days,
            )
            cumulative += kwh_per_interval
    return total_cost


class TestGetSeason:
    """Tests for season determination."""

    def test_summer_months(self):
        for month in (6, 7, 8, 9):
            assert get_season(month) == Season.SUMMER

    def test_winter_months(self):
        for month in (1, 2, 3, 4, 5, 10, 11, 12):
            assert get_season(month) == Season.WINTER


class TestCalculateTieredCost:
    """Tests for tiered cost calculation."""

    rate = TieredRate(boundary_kwh=800, rate_under=0.04, rate_over=0.06)

    def test_all_under_boundary(self):
        # 0.5 kWh interval, cumulative 100 -> all under 800
        cost = calculate_tiered_cost(0.5, 100.0, self.rate)
        assert cost == pytest.approx(0.5 * 0.04)

    def test_all_over_boundary(self):
        # 0.5 kWh interval, cumulative already 900 -> all over 800
        cost = calculate_tiered_cost(0.5, 900.0, self.rate)
        assert cost == pytest.approx(0.5 * 0.06)

    def test_straddles_boundary(self):
        # 1.0 kWh interval, cumulative 799.5 -> 0.5 under + 0.5 over
        cost = calculate_tiered_cost(1.0, 799.5, self.rate)
        expected = 0.5 * 0.04 + 0.5 * 0.06
        assert cost == pytest.approx(expected)

    def test_exactly_at_boundary(self):
        # Cumulative exactly at boundary -> all over
        cost = calculate_tiered_cost(0.5, 800.0, self.rate)
        assert cost == pytest.approx(0.5 * 0.06)

    def test_interval_reaches_exactly_boundary(self):
        # 0.5 kWh from cumulative 799.5 -> ends exactly at 800, all under
        cost = calculate_tiered_cost(0.5, 799.5, self.rate)
        assert cost == pytest.approx(0.5 * 0.04)

    def test_zero_interval(self):
        cost = calculate_tiered_cost(0.0, 500.0, self.rate)
        assert cost == pytest.approx(0.0)


class TestCalculateConsumptionTax:
    """Tests for consumption tax calculation."""

    tiers = [
        ConsumptionTaxTier(lower_kwh=0, upper_kwh=2500, rate=0.001565),
        ConsumptionTaxTier(lower_kwh=2500, upper_kwh=50000, rate=0.001055),
        ConsumptionTaxTier(lower_kwh=50000, upper_kwh=float("inf"), rate=0.000845),
    ]

    def test_all_in_first_tier(self):
        tax = calculate_consumption_tax(1.0, 100.0, self.tiers)
        assert tax == pytest.approx(1.0 * 0.001565)

    def test_all_in_second_tier(self):
        tax = calculate_consumption_tax(1.0, 3000.0, self.tiers)
        assert tax == pytest.approx(1.0 * 0.001055)

    def test_straddles_first_second_tier(self):
        # Cumulative 2499, interval 2.0 -> 1 kWh in first + 1 kWh in second
        tax = calculate_consumption_tax(2.0, 2499.0, self.tiers)
        expected = 1.0 * 0.001565 + 1.0 * 0.001055
        assert tax == pytest.approx(expected)

    def test_zero_interval(self):
        tax = calculate_consumption_tax(0.0, 500.0, self.tiers)
        assert tax == pytest.approx(0.0)


class TestCalculateSchedule1IntervalCost:
    """Tests for full Schedule 1 interval cost calculation."""

    def test_zero_consumption(self):
        dt = datetime(2026, 7, 15, 12, 0)
        cost = calculate_schedule1_interval_cost(0.0, dt, 0.0, SCHEDULE_2026_01_01)
        assert cost == 0.0

    def test_single_interval_summer(self):
        # A single 30-min interval: 0.5 kWh, summer, no prior cumulative
        dt = datetime(2026, 7, 15, 12, 0)
        cost = calculate_schedule1_interval_cost(
            0.5, dt, 0.0, SCHEDULE_2026_01_01, billing_period_days=30
        )
        # Should be > 0 and include all components
        assert cost > 0

    def test_full_month_summer_1000kwh(self):
        """Verify a 1000 kWh summer month matches manual worksheet calculation.

        1000 kWh over 30 days = ~0.694 kWh per 30-min interval (48 intervals/day).
        Expected total: $176.2584 (calculated from the 2026-01-01 worksheet rates).
        """
        total_cost = _accumulate_month(1000.0, SCHEDULE_2026_01_01, 2026, 7)

        # Manual calculation: $176.2584
        assert total_cost == pytest.approx(176.2584, rel=1e-3)

    def test_full_month_winter_1000kwh(self):
        """Verify a 1000 kWh winter month.

        Distribution is same as summer. Generation differs:
        800 * 0.030064 + 200 * 0.026965 = 24.0512 + 5.393 = 29.4442
        vs summer generation = 34.2182
        Difference = -4.774
        Expected total: 176.2584 - 4.774 = 171.4844
        """
        total_cost = _accumulate_month(1000.0, SCHEDULE_2026_01_01, 2026, 1)

        # Winter generation: 800 * 0.030064 + 200 * 0.026965 = 29.4442
        # Summer generation was 34.2182, diff = -4.774
        # Expected: 176.2584 - 4.774 = 171.4844
        assert total_cost == pytest.approx(171.4844, rel=1e-3)

    def test_low_usage_all_under_boundary(self):
        """500 kWh month should use only lower-tier rates."""
        total_cost = _accumulate_month(500.0, SCHEDULE_2026_01_01, 2026, 7)

        # Manual: dist=500*0.03569=17.845, gen=500*0.031212=15.606,
        # trans=500*0.0097=4.85, riders=500*0.089924=44.962,
        # tax=500*0.001565=0.7825, cc=7.58
        # Total = 91.6255
        expected = 17.845 + 15.606 + 4.85 + 44.962 + 0.7825 + 7.58
        assert total_cost == pytest.approx(expected, rel=1e-3)

    def test_season_boundary_month_june(self):
        """June should use summer rates."""
        dt = datetime(2026, 6, 15, 12, 0)
        cost = calculate_schedule1_interval_cost(
            1.0, dt, 0.0, SCHEDULE_2026_01_01, billing_period_days=30
        )
        # Generation rate under for summer is 0.031212
        # vs winter 0.030064 — summer should yield slightly higher gen cost
        dt_winter = datetime(2026, 5, 15, 12, 0)
        cost_winter = calculate_schedule1_interval_cost(
            1.0, dt_winter, 0.0, SCHEDULE_2026_01_01, billing_period_days=30
        )
        # Summer gen rate is higher than winter for under-800 tier
        assert cost > cost_winter


class TestScheduleRegistry:
    """Tests for effective-dated schedule selection."""

    def test_history_is_sorted_and_non_empty(self):
        assert VA_SCHEDULE_1_HISTORY
        dates = [s.effective_from for s in VA_SCHEDULE_1_HISTORY]
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates)

    def test_date_before_first_schedule_falls_back_to_oldest(self):
        """We have no pre-2026 rate data; don't raise, use the oldest we have."""
        oldest = VA_SCHEDULE_1_HISTORY[0]
        assert get_schedule_for_date(date(2019, 3, 1)) is oldest
        assert (
            get_schedule_for_date(oldest.effective_from - timedelta(days=1)) is oldest
        )

    def test_exactly_on_effective_date(self):
        """A schedule applies on its own effective date ("on and after")."""
        for schedule in VA_SCHEDULE_1_HISTORY:
            assert get_schedule_for_date(schedule.effective_from) is schedule

    def test_day_before_effective_date_uses_previous(self):
        for previous, schedule in pairwise(VA_SCHEDULE_1_HISTORY):
            day_before = schedule.effective_from - timedelta(days=1)
            assert get_schedule_for_date(day_before) is previous

    def test_between_schedules(self):
        """A date in the middle of a schedule's span picks that schedule."""
        assert get_schedule_for_date(date(2026, 1, 17)).effective_date == "2026-01-01"
        assert get_schedule_for_date(date(2026, 2, 28)).effective_date == "2026-01-01"
        assert get_schedule_for_date(date(2026, 6, 15)).effective_date == "2026-06-01"

    def test_after_newest_schedule(self):
        newest = VA_SCHEDULE_1_HISTORY[-1]
        assert (
            get_schedule_for_date(newest.effective_from + timedelta(days=1)) is newest
        )
        assert get_schedule_for_date(date(2099, 12, 31)) is newest

    def test_accepts_datetime(self):
        """The coordinator holds datetimes; don't make it convert."""
        assert get_schedule_for_date(datetime(2026, 6, 15, 13, 30)) is (
            get_schedule_for_date(date(2026, 6, 15))
        )

    def test_va_schedule_1_is_the_currently_effective_schedule(self):
        assert VA_SCHEDULE_1 is get_schedule_for_date(date.today())
        assert VA_SCHEDULE_1 is VA_SCHEDULE_1_HISTORY[-1]
        assert (
            VA_SCHEDULE_1_HISTORY[-1].effective_date == LATEST_SCHEDULE_EFFECTIVE_DATE
        )

    def test_every_schedule_has_a_cited_source(self):
        for schedule in VA_SCHEDULE_1_HISTORY:
            assert schedule.source_url.startswith("https://")
            assert schedule.source_retrieved


class TestBackfillAcrossRateChange:
    """A ~68-day backfill window can span a rate change."""

    def test_backfill_window_spans_multiple_schedules(self):
        end = date(2026, 8, 11)
        start = end - timedelta(days=68)
        seen = {
            get_schedule_for_date(start + timedelta(days=n)).effective_date
            for n in range((end - start).days + 1)
        }
        assert len(seen) > 1, "backfill window should cross at least one rate change"
        assert {"2026-07-01", "2026-08-01"} <= seen

    def test_fuel_factor_steps_up_on_2026_07_01(self):
        """Rider A went 2.968 -> 3.7648 cents/kWh for usage on and after 07-01-26.

        Source: FUEL CHARGE RIDER A, filed 05-29-26.
        """

        def fuel(d):
            riders = get_schedule_for_date(d).riders
            return next(r.rate for r in riders if r.name == "Fuel/A")

        assert fuel(date(2026, 6, 30)) == pytest.approx(0.02968)
        assert fuel(date(2026, 7, 1)) == pytest.approx(0.037648)

    def test_same_usage_costs_more_after_the_change(self):
        """Identical June and July usage must not price identically."""
        june = calculate_schedule1_interval_cost(
            1.0,
            datetime(2026, 6, 30, 12, 0),
            0.0,
            get_schedule_for_date(date(2026, 6, 30)),
        )
        july = calculate_schedule1_interval_cost(
            1.0,
            datetime(2026, 7, 1, 12, 0),
            0.0,
            get_schedule_for_date(date(2026, 7, 1)),
        )
        # Same season (both summer), so the whole delta is the fuel factor.
        assert july - june == pytest.approx(0.037648 - 0.02968)


class TestCurrentScheduleIsPlausible:
    """Guard against the encoded rate set drifting into nonsense."""

    def test_1000kwh_summer_month_is_in_a_defensible_range(self):
        """A 1,000 kWh summer month on the current schedule.

        Working, from the 2026-08-01 rate set:
            customer charge                                    $  7.58
            distribution   800*0.03569  + 200*0.023596         $ 33.27
            generation     800*0.031212 + 200*0.046243         $ 34.22
            transmission   1000*0.0097                         $  9.70
            riders         1000*0.100105                       $100.11
            consumption tax 1000*0.001565                      $  1.57
                                                               -------
                                                               $186.44

        That is consistent with press coverage of the 2026 increases: roughly
        +$8/month of fuel charge from July 1 on top of the base increase that
        phased in during 2026.

        The band below is deliberately wide. It is here to catch a decimal
        point in the wrong place or a dropped component, not to re-assert the
        arithmetic above (which the fixed-schedule tests already cover). It
        should survive the next few rate filings without edits.
        """
        total = _accumulate_month(1000.0, VA_SCHEDULE_1, 2026, 7)
        assert 150.0 < total < 240.0

    def test_bill_grows_monotonically_with_usage(self):
        totals = [
            calculate_schedule1_period_bill(
                kwh, date(2026, 7, 12), date(2026, 8, 11)
            ).total
            for kwh in (0, 250, 500, 1000, 2000)
        ]
        assert totals == sorted(totals)

    def test_not_stale_today(self):
        """If this fails, the rate table needs a refresh — see docs/rate-schedules.md."""
        assert not is_schedule_possibly_stale()


class TestPeriodBill:
    """Tests for the whole-billing-period helper."""

    def test_components_sum_to_total(self):
        bill = calculate_schedule1_period_bill(
            1000.0, date(2026, 7, 12), date(2026, 8, 11)
        )
        parts = (
            bill.customer_charge
            + bill.distribution
            + bill.generation
            + bill.transmission
            + bill.riders
            + bill.consumption_tax
        )
        assert bill.total == pytest.approx(parts)

    def test_zero_usage_still_bills_the_customer_charge(self):
        bill = calculate_schedule1_period_bill(
            0.0, date(2026, 7, 12), date(2026, 8, 11)
        )
        assert bill.total == pytest.approx(VA_SCHEDULE_1.customer_charge)

    def test_season_comes_from_the_period_midpoint(self):
        summer = calculate_schedule1_period_bill(
            1000.0, date(2026, 7, 1), date(2026, 7, 31)
        )
        winter = calculate_schedule1_period_bill(
            1000.0, date(2026, 1, 1), date(2026, 1, 31)
        )
        assert summer.season == Season.SUMMER
        assert winter.season == Season.WINTER
        assert summer.total > winter.total

    def test_picks_the_schedule_effective_at_the_midpoint(self):
        bill = calculate_schedule1_period_bill(
            1000.0, date(2026, 6, 10), date(2026, 6, 20)
        )
        assert bill.schedule_effective_date == "2026-06-01"

    def test_explicit_schedule_is_honoured(self):
        bill = calculate_schedule1_period_bill(
            1000.0, date(2026, 7, 1), date(2026, 7, 30), schedule=SCHEDULE_2026_01_01
        )
        assert bill.schedule_effective_date == "2026-01-01"
        assert bill.total == pytest.approx(176.2584, rel=1e-3)

    def test_agrees_with_interval_accumulation(self):
        """The period helper and the interval engine must not disagree.

        Both price the same 1,000 kWh July month; the interval path prorates
        the customer charge across intervals but the total is the same.
        """
        interval_total = _accumulate_month(1000.0, SCHEDULE_2026_01_01, 2026, 7)
        period = calculate_schedule1_period_bill(
            1000.0, date(2026, 7, 1), date(2026, 7, 30), schedule=SCHEDULE_2026_01_01
        )
        assert period.total == pytest.approx(interval_total, rel=1e-6)


class TestBillDiscrepancy:
    """Tests for the estimate-vs-actual drift helper."""

    def test_overestimate_is_positive(self):
        assert bill_discrepancy(110.0, 100.0) == pytest.approx(0.10)

    def test_underestimate_is_negative(self):
        assert bill_discrepancy(90.0, 100.0) == pytest.approx(-0.10)

    def test_exact_match_is_zero(self):
        assert bill_discrepancy(100.0, 100.0) == pytest.approx(0.0)

    def test_non_positive_actual_returns_none(self):
        assert bill_discrepancy(100.0, 0.0) is None
        assert bill_discrepancy(100.0, -5.0) is None


class TestStalenessSelfDefence:
    """Tests for the drift signals exposed to the rest of the integration."""

    def test_stale_once_the_newest_schedule_is_old_enough(self):
        newest = VA_SCHEDULE_1_HISTORY[-1].effective_from
        assert not is_schedule_possibly_stale(newest + timedelta(days=399))
        assert is_schedule_possibly_stale(newest + timedelta(days=401))

    def test_max_age_is_configurable(self):
        newest = VA_SCHEDULE_1_HISTORY[-1].effective_from
        assert is_schedule_possibly_stale(newest + timedelta(days=40), max_age_days=30)

    def test_days_since_schedule_change(self):
        newest = VA_SCHEDULE_1_HISTORY[-1].effective_from
        assert days_since_schedule_change(newest) == 0
        assert days_since_schedule_change(newest + timedelta(days=45)) == 45
