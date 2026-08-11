"""Tests for the feature helpers added to the coordinator.

Covers excess generation, billing-period projection, rate drift detection and
the statistic-ID compatibility rule.

Like the rest of the fast suite, this module runs without Home Assistant
installed. The helpers under test are pure functions, but they live in
``coordinator.py`` next to the code that calls them, and that module imports
Home Assistant at the top. So when Home Assistant is genuinely absent the
loader below stands in placeholder modules just long enough to execute
``coordinator.py``, then removes them again -- ``sys.modules`` is left exactly
as it was found, so the modules that legitimately skip on a missing Home
Assistant still see it missing. When Home Assistant *is* installed nothing is
stubbed and the real module is imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
from zoneinfo import ZoneInfo

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "dominion_energy"
)

NY = ZoneInfo("America/New_York")


def _module_available(name: str) -> bool:
    """Report whether a top-level module can be imported."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _placeholder_modules() -> dict[str, types.ModuleType]:
    """Build the stand-ins ``coordinator.py`` needs to merely *import*.

    Only names bound at import time matter: the class statement needs a
    subscriptable base class, and every ``from x import y`` needs ``y`` to
    exist. Nothing here is ever called -- the tests below only exercise the
    module-level pure helpers and the plain-data ``DominionEnergyData``.
    """

    class _Coordinator:
        """Subscriptable, subclassable stand-in for DataUpdateCoordinator."""

        def __class_getitem__(cls, _item: Any) -> type:
            return cls

    class _Sentinel:
        """Attribute bag standing in for the recorder's enums and constants."""

        NONE = "none"
        KILO_WATT_HOUR = "kWh"

    def _unused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("placeholder Home Assistant API called in a unit test")

    contents: dict[str, dict[str, Any]] = {
        "dompower": {
            name: type(name, (Exception,), {})
            for name in (
                "ApiError",
                "CannotConnectError",
                "InvalidAuthError",
                "InvalidCredentialsError",
                "TFARequiredError",
                "TokenExpiredError",
            )
        }
        | {
            "BillForecast": object,
            "DompowerClient": object,
            "GigyaAuthenticator": object,
            "IntervalUsageData": object,
        },
        "homeassistant": {},
        "homeassistant.components": {},
        "homeassistant.components.recorder": {"get_instance": _unused},
        "homeassistant.components.recorder.models": {
            "StatisticData": dict,
            "StatisticMeanType": _Sentinel,
            "StatisticMetaData": dict,
        },
        "homeassistant.components.recorder.statistics": {
            "async_add_external_statistics": _unused,
            "get_last_statistics": _unused,
        },
        "homeassistant.config_entries": {"ConfigEntry": _Coordinator},
        "homeassistant.const": {"UnitOfEnergy": _Sentinel},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {
            "ConfigEntryAuthFailed": type("ConfigEntryAuthFailed", (Exception,), {})
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.aiohttp_client": {"async_get_clientsession": _unused},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": _Coordinator,
            "UpdateFailed": type("UpdateFailed", (Exception,), {}),
        },
        "homeassistant.util": {"dt": types.ModuleType("dt")},
    }

    modules: dict[str, types.ModuleType] = {}
    for name, attributes in contents.items():
        module = types.ModuleType(name)
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        modules[name] = module
    return modules


def _load_coordinator() -> Any:
    """Import ``coordinator.py`` without executing the integration's __init__."""
    pkg_name = "_dominion_features_pkg"
    mod_name = f"{pkg_name}.coordinator"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg

    installed: list[str] = []
    if not (_module_available("homeassistant") and _module_available("dompower")):
        for name, module in _placeholder_modules().items():
            if name not in sys.modules:
                sys.modules[name] = module
                installed.append(name)

    try:
        spec = importlib.util.spec_from_file_location(
            mod_name, COMPONENT_DIR / "coordinator.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    finally:
        # Leave sys.modules exactly as it was found, so the placeholders cannot
        # fool another test module's `importorskip("homeassistant")`.
        for name in installed:
            del sys.modules[name]
    return module


coordinator = _load_coordinator()

aggregate_hourly_generation = coordinator.aggregate_hourly_generation
statistics_window_is_fetchable = coordinator.statistics_window_is_fetchable
days_with_generation = coordinator.days_with_generation
filter_incomplete_days_allowing_generation = (
    coordinator.filter_incomplete_days_allowing_generation
)
generation_of = coordinator.generation_of
project_period_usage = coordinator.project_period_usage
rate_check = coordinator.rate_check
resolve_statistic_id_prefix = coordinator.resolve_statistic_id_prefix
DominionEnergyData = coordinator.DominionEnergyData

# rates.py is pure and imports cleanly on its own.
_rates_dir = str(COMPONENT_DIR)
if _rates_dir not in sys.path:
    sys.path.insert(0, _rates_dir)

from rates import (  # noqa: E402
    bill_discrepancy,
    calculate_schedule1_period_bill,
)

ACCOUNT = "123456789123"
METER = "000000000296117800"
METER_SCOPED = f"{ACCOUNT}_{METER}"

# A summer and a winter billing period. Schedule 1 prices a whole period at one
# season's rates, and the two seasons put the 800 kWh tier boundary in opposite
# directions, so both are worth pinning down.
SUMMER_PERIOD = (date(2026, 7, 5), date(2026, 8, 4))
WINTER_PERIOD = (date(2026, 1, 5), date(2026, 2, 4))


@dataclass(frozen=True)
class FakeInterval:
    """Stand-in for ``dompower.IntervalUsageData``."""

    timestamp: datetime
    consumption: float
    generation: float = 0.0


@dataclass(frozen=True)
class ConsumptionOnlyInterval:
    """An interval from a dompower release that predates generation support."""

    timestamp: datetime
    consumption: float


def make_day(
    day: date,
    *,
    consumption: float = 0.5,
    generation: float = 0.0,
    count: int = 48,
) -> list[FakeInterval]:
    """Build a day of 30-minute intervals in local time."""
    start = datetime(day.year, day.month, day.day, tzinfo=NY)
    return [
        FakeInterval(start + timedelta(minutes=30 * i), consumption, generation)
        for i in range(count)
    ]


class TestResolveStatisticIdPrefix:
    """The multi-meter compatibility rule.

    Getting this wrong either orphans an existing install's history or lets two
    meters write into one statistics stream, so both branches are pinned.
    """

    def test_fresh_entry_with_no_history_is_meter_scoped(self) -> None:
        """A brand new entry never adopts the account-scoped IDs."""
        assert (
            resolve_statistic_id_prefix(
                account_number=ACCOUNT,
                meter_number=METER,
                stored_prefix=None,
                account_statistics_exist=False,
                account_prefix_claimed=False,
            )
            == METER_SCOPED
        )

    def test_legacy_entry_keeps_its_account_scoped_history(self) -> None:
        """An upgraded install keeps writing where its statistics already are."""
        assert (
            resolve_statistic_id_prefix(
                account_number=ACCOUNT,
                meter_number=METER,
                stored_prefix=None,
                account_statistics_exist=True,
                account_prefix_claimed=False,
            )
            == ACCOUNT
        )

    def test_second_meter_does_not_steal_a_claimed_prefix(self) -> None:
        """The account-scoped stream belongs to at most one entry."""
        assert (
            resolve_statistic_id_prefix(
                account_number=ACCOUNT,
                meter_number="999999999999999999",
                stored_prefix=None,
                account_statistics_exist=True,
                account_prefix_claimed=True,
            )
            == f"{ACCOUNT}_999999999999999999"
        )

    @pytest.mark.parametrize("stored", [ACCOUNT, METER_SCOPED, "anything-at-all"])
    @pytest.mark.parametrize("stats_exist", [True, False])
    @pytest.mark.parametrize("claimed", [True, False])
    def test_a_stored_prefix_is_never_second_guessed(
        self, stored: str, stats_exist: bool, claimed: bool
    ) -> None:
        """Once resolved, the prefix is stable regardless of recorder state."""
        assert (
            resolve_statistic_id_prefix(
                account_number=ACCOUNT,
                meter_number=METER,
                stored_prefix=stored,
                account_statistics_exist=stats_exist,
                account_prefix_claimed=claimed,
            )
            == stored
        )

    def test_two_fresh_meters_on_one_account_get_distinct_prefixes(self) -> None:
        """The whole point: no two entries may share a statistics stream."""
        first = resolve_statistic_id_prefix(
            account_number=ACCOUNT,
            meter_number="meter-a",
            stored_prefix=None,
            account_statistics_exist=False,
            account_prefix_claimed=False,
        )
        second = resolve_statistic_id_prefix(
            account_number=ACCOUNT,
            meter_number="meter-b",
            stored_prefix=None,
            account_statistics_exist=False,
            account_prefix_claimed=False,
        )
        assert first != second

    def test_legacy_entry_and_a_new_sibling_get_distinct_prefixes(self) -> None:
        """The mixed case: one entry keeps history, the other is meter scoped."""
        legacy = resolve_statistic_id_prefix(
            account_number=ACCOUNT,
            meter_number="meter-a",
            stored_prefix=None,
            account_statistics_exist=True,
            account_prefix_claimed=False,
        )
        sibling = resolve_statistic_id_prefix(
            account_number=ACCOUNT,
            meter_number="meter-b",
            stored_prefix=None,
            account_statistics_exist=True,
            account_prefix_claimed=True,
        )
        assert legacy == ACCOUNT
        assert sibling == f"{ACCOUNT}_meter-b"


class TestProjectPeriodUsage:
    """Extrapolating the billing period from the days seen so far."""

    def test_half_a_period_projects_to_double(self) -> None:
        assert project_period_usage(600.0, 15, 30) == pytest.approx(1200.0)

    def test_a_complete_period_projects_to_itself(self) -> None:
        assert project_period_usage(950.0, 30, 30) == pytest.approx(950.0)

    def test_projection_uses_observed_days_not_calendar_days(self) -> None:
        """A day the API never published must not read as a zero-usage day.

        Ten days at 40 kWh with two days missing is still a 40 kWh/day site;
        dividing the same 400 kWh over twelve calendar days would understate
        the projection by a sixth.
        """
        observed = project_period_usage(400.0, 10, 30)
        calendar = project_period_usage(400.0, 12, 30)
        assert observed == pytest.approx(1200.0)
        assert calendar == pytest.approx(1000.0)
        assert observed > calendar

    def test_projection_never_falls_below_usage_already_incurred(self) -> None:
        """Energy already consumed cannot un-consume itself."""
        assert project_period_usage(900.0, 40, 30) == pytest.approx(900.0)

    @pytest.mark.parametrize(
        ("days_with_data", "days_in_period"),
        [(0, 30), (-1, 30), (15, 0), (15, -30), (0, 0)],
    )
    def test_nothing_to_extrapolate_from_returns_none(
        self, days_with_data: int, days_in_period: int
    ) -> None:
        assert project_period_usage(500.0, days_with_data, days_in_period) is None

    def test_zero_usage_so_far_projects_to_zero(self) -> None:
        """Zero is a fact here, not a missing value: there were days of data."""
        assert project_period_usage(0.0, 5, 30) == pytest.approx(0.0)


class TestScheduleOneIsNotLinearInUsage:
    """Why the projection prices usage rather than scaling cost.

    Schedule 1 breaks proportionality twice: the flat customer charge is billed
    once per period however much is used, and distribution and generation each
    change rate at 800 kWh. Doubling a part-period cost would get both wrong.
    """

    def test_the_customer_charge_is_billed_once_not_scaled(self) -> None:
        half = calculate_schedule1_period_bill(400.0, *SUMMER_PERIOD)
        full = calculate_schedule1_period_bill(800.0, *SUMMER_PERIOD)

        # Both totals sit entirely under the 800 kWh boundary, so the variable
        # components really are proportional...
        assert full.distribution == pytest.approx(2 * half.distribution)
        assert full.generation == pytest.approx(2 * half.generation)
        # ...but the fixed charge is not, so the bill still is not.
        assert full.customer_charge == pytest.approx(half.customer_charge)
        assert full.total < 2 * half.total

    def test_crossing_the_800_kwh_tier_bends_the_curve(self) -> None:
        under = calculate_schedule1_period_bill(600.0, *SUMMER_PERIOD)
        over = calculate_schedule1_period_bill(1200.0, *SUMMER_PERIOD)

        # In summer the tiers move in opposite directions above 800 kWh:
        # distribution gets cheaper, generation gets dearer.
        assert over.distribution < 2 * under.distribution
        assert over.generation > 2 * under.generation
        assert over.total != pytest.approx(2 * under.total)

    def test_winter_tiers_bend_the_curve_the_other_way(self) -> None:
        under = calculate_schedule1_period_bill(600.0, *WINTER_PERIOD)
        over = calculate_schedule1_period_bill(1200.0, *WINTER_PERIOD)

        assert over.distribution < 2 * under.distribution
        # Unlike summer, winter generation is also cheaper above the boundary.
        assert over.generation < 2 * under.generation
        assert over.total != pytest.approx(2 * under.total)

    def test_projecting_usage_then_pricing_differs_from_scaling_cost(self) -> None:
        """The end-to-end reason the coordinator projects usage first.

        Half a 30-day period at 40 kWh/day: 600 kWh so far, 1200 kWh projected.
        Pricing 1200 kWh is not the same number as doubling the 600 kWh bill.
        """
        projected_usage = project_period_usage(600.0, 15, 30)
        assert projected_usage == pytest.approx(1200.0)

        priced_projection = calculate_schedule1_period_bill(
            projected_usage, *SUMMER_PERIOD
        ).total
        scaled_cost = calculate_schedule1_period_bill(600.0, *SUMMER_PERIOD).total * 2

        assert abs(priced_projection - scaled_cost) > 0.01

    def test_linear_modes_may_safely_scale(self) -> None:
        """The contrast case, pinning why only Schedule 1 needs special care.

        A flat $/kWh rate is proportional by construction, which is what lets
        the other three cost modes project by scaling the period-to-date cost
        (and, for time-of-use, keeps the observed peak/off-peak split).
        """
        rate = 0.125
        assert 600.0 * rate * 2 == pytest.approx(1200.0 * rate)


class TestRateCheck:
    """Detecting that the hard-coded tariff has drifted from reality."""

    def test_matching_bill_reports_a_small_discrepancy(self) -> None:
        """Feeding back our own estimate must come out at ~0% drift."""
        estimated_bill = calculate_schedule1_period_bill(1000.0, *SUMMER_PERIOD)
        estimated, actual, discrepancy = rate_check(
            round(estimated_bill.total, 2), 1000.0, *SUMMER_PERIOD
        )

        assert estimated == pytest.approx(round(estimated_bill.total, 2))
        assert actual == pytest.approx(round(estimated_bill.total, 2))
        assert discrepancy == pytest.approx(0.0, abs=1e-9)

    def test_overestimate_is_reported_as_a_positive_fraction(self) -> None:
        """Rates encoded higher than the ones actually billed read positive."""
        estimated_bill = calculate_schedule1_period_bill(1000.0, *SUMMER_PERIOD)
        # Dominion billed 10% less than we make it.
        actual_charges = round(estimated_bill.total, 2) / 1.10

        estimated, actual, discrepancy = rate_check(
            actual_charges, 1000.0, *SUMMER_PERIOD
        )

        assert estimated is not None and actual is not None
        assert estimated > actual
        assert discrepancy == pytest.approx(0.10, abs=1e-3)

    def test_underestimate_is_reported_as_a_negative_fraction(self) -> None:
        """A missed rate increase reads negative: we are charging too little."""
        estimated_bill = calculate_schedule1_period_bill(1000.0, *SUMMER_PERIOD)
        actual_charges = round(estimated_bill.total, 2) * 1.25

        _estimated, _actual, discrepancy = rate_check(
            actual_charges, 1000.0, *SUMMER_PERIOD
        )

        assert discrepancy is not None
        assert discrepancy < 0
        assert discrepancy == pytest.approx(-0.2, abs=1e-3)

    def test_the_discrepancy_matches_the_rates_helper(self) -> None:
        estimated, actual, discrepancy = rate_check(140.0, 1000.0, *SUMMER_PERIOD)
        assert estimated is not None and actual is not None
        assert discrepancy == pytest.approx(bill_discrepancy(estimated, actual))

    @pytest.mark.parametrize(
        ("charges", "usage"),
        [
            (None, 1000.0),
            (140.0, None),
            (None, None),
            (0.0, 1000.0),
            (140.0, 0.0),
            (-5.0, 1000.0),
            (140.0, -20.0),
        ],
        ids=[
            "no-charges",
            "no-usage",
            "no-bill-at-all",
            "zero-charges",
            "zero-usage",
            "negative-charges",
            "negative-usage",
        ],
    )
    def test_missing_or_zero_bill_data_yields_all_none(
        self, charges: float | None, usage: float | None
    ) -> None:
        """The contract: never a fabricated figure."""
        assert rate_check(charges, usage, *SUMMER_PERIOD) == (None, None, None)

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (None, date(2026, 8, 4)),
            (date(2026, 7, 5), None),
            (None, None),
            # An inverted or zero-length period cannot pick a season.
            (date(2026, 8, 4), date(2026, 7, 5)),
            (date(2026, 7, 5), date(2026, 7, 5)),
        ],
        ids=["no-start", "no-end", "no-dates", "inverted", "zero-length"],
    )
    def test_unusable_period_dates_yield_all_none(
        self, start: date | None, end: date | None
    ) -> None:
        assert rate_check(140.0, 1000.0, start, end) == (None, None, None)

    def test_actual_is_reported_verbatim(self) -> None:
        """`rate_check_actual` is the billed amount, not a re-derived one."""
        _estimated, actual, _discrepancy = rate_check(137.4321, 1000.0, *SUMMER_PERIOD)
        assert actual == pytest.approx(137.4321)


class TestGenerationOf:
    """Reading exported energy off an interval defensively."""

    def test_reads_the_generation_field(self) -> None:
        interval = FakeInterval(datetime(2026, 8, 10, tzinfo=NY), 0.5, 0.3)
        assert generation_of(interval) == pytest.approx(0.3)

    def test_missing_attribute_reads_as_zero(self) -> None:
        interval = ConsumptionOnlyInterval(datetime(2026, 8, 10, tzinfo=NY), 0.5)
        assert generation_of(interval) == 0.0

    def test_none_reads_as_zero(self) -> None:
        interval = FakeInterval(datetime(2026, 8, 10, tzinfo=NY), 0.5, None)  # type: ignore[arg-type]
        assert generation_of(interval) == 0.0


class TestAggregateHourlyGeneration:
    """Bucketing exported energy into the hourly rows statistics need."""

    def test_two_half_hours_merge_into_one_hourly_bucket(self) -> None:
        hourly = aggregate_hourly_generation(
            make_day(date(2026, 8, 10), consumption=0.5, generation=0.2)
        )

        assert len(hourly) == 24
        assert all(value == pytest.approx(0.4) for value in hourly.values())
        assert sum(hourly.values()) == pytest.approx(9.6)

    def test_buckets_are_keyed_by_the_local_hour_start(self) -> None:
        hourly = aggregate_hourly_generation(
            make_day(date(2026, 8, 10), generation=0.2)
        )
        first = min(hourly)
        assert (first.minute, first.second, first.microsecond) == (0, 0, 0)
        assert first == datetime(2026, 8, 10, 0, 0, tzinfo=NY)

    def test_consumption_is_ignored(self) -> None:
        hourly = aggregate_hourly_generation(
            make_day(date(2026, 8, 10), consumption=99.0, generation=0.0)
        )
        assert sum(hourly.values()) == pytest.approx(0.0)

    def test_intervals_without_generation_support_aggregate_to_zero(self) -> None:
        start = datetime(2026, 8, 10, tzinfo=NY)
        intervals = [
            ConsumptionOnlyInterval(start + timedelta(minutes=30 * i), 0.5)
            for i in range(48)
        ]
        hourly = aggregate_hourly_generation(intervals)
        assert len(hourly) == 24
        assert sum(hourly.values()) == pytest.approx(0.0)

    def test_empty_input_yields_empty_buckets(self) -> None:
        assert aggregate_hourly_generation([]) == {}

    def test_unsorted_input_is_bucketed_correctly(self) -> None:
        day = make_day(date(2026, 8, 10), generation=0.2)
        assert aggregate_hourly_generation(list(reversed(day))) == (
            aggregate_hourly_generation(day)
        )


class TestDaysWithGeneration:
    """Which days count as having really exported energy."""

    def test_a_solar_day_is_detected(self) -> None:
        day = date(2026, 8, 10)
        assert days_with_generation(make_day(day, generation=0.2)) == {day}

    def test_a_day_without_generation_is_not(self) -> None:
        assert days_with_generation(make_day(date(2026, 8, 10))) == set()

    def test_a_handful_of_intervals_is_below_the_threshold(self) -> None:
        """Two flickers of export are noise, not a published solar day."""
        day = make_day(date(2026, 8, 10))
        day[10] = FakeInterval(day[10].timestamp, day[10].consumption, 0.2)
        day[11] = FakeInterval(day[11].timestamp, day[11].consumption, 0.2)
        assert days_with_generation(day) == set()


class TestFilterIncompleteDaysAllowingGeneration:
    """The trap: a bright solar day can net out to zero consumption.

    ``usage.filter_incomplete_days`` judges a day unpublished from consumption
    alone, so without this wrapper a net-zero solar day would be discarded
    along with its generation.
    """

    def test_a_net_zero_solar_day_is_kept(self) -> None:
        solar_day = make_day(date(2026, 8, 10), consumption=0.0, generation=0.4)
        normal_day = make_day(date(2026, 8, 11), consumption=0.5)

        kept, dropped = filter_incomplete_days_allowing_generation(
            solar_day + normal_day
        )

        assert dropped == []
        assert {i.timestamp.date() for i in kept} == {
            date(2026, 8, 10),
            date(2026, 8, 11),
        }
        assert sum(generation_of(i) for i in kept) == pytest.approx(19.2)

    def test_a_genuinely_unpublished_day_is_still_dropped(self) -> None:
        empty_day = make_day(date(2026, 8, 10), consumption=0.0, generation=0.0)
        normal_day = make_day(date(2026, 8, 11), consumption=0.5)

        kept, dropped = filter_incomplete_days_allowing_generation(
            empty_day + normal_day
        )

        assert dropped == [date(2026, 8, 10)]
        assert {i.timestamp.date() for i in kept} == {date(2026, 8, 11)}

    def test_an_empty_day_with_a_flicker_of_generation_stays_dropped(self) -> None:
        """Re-admission needs real evidence the day was published."""
        empty_day = make_day(date(2026, 8, 10), consumption=0.0, generation=0.0)
        empty_day[3] = FakeInterval(empty_day[3].timestamp, 0.0, 0.1)
        normal_day = make_day(date(2026, 8, 11), consumption=0.5)

        _kept, dropped = filter_incomplete_days_allowing_generation(
            empty_day + normal_day
        )

        assert dropped == [date(2026, 8, 10)]

    def test_only_the_solar_day_is_rescued_from_a_mixed_batch(self) -> None:
        solar_day = make_day(date(2026, 8, 8), consumption=0.0, generation=0.4)
        empty_day = make_day(date(2026, 8, 9), consumption=0.0)
        normal_day = make_day(date(2026, 8, 10), consumption=0.5)

        kept, dropped = filter_incomplete_days_allowing_generation(
            solar_day + empty_day + normal_day
        )

        assert dropped == [date(2026, 8, 9)]
        assert {i.timestamp.date() for i in kept} == {
            date(2026, 8, 8),
            date(2026, 8, 10),
        }

    def test_ordinary_data_is_passed_through_untouched(self) -> None:
        days = make_day(date(2026, 8, 10)) + make_day(date(2026, 8, 11))
        kept, dropped = filter_incomplete_days_allowing_generation(days)
        assert dropped == []
        assert kept == days

    def test_empty_input_is_handled(self) -> None:
        assert filter_incomplete_days_allowing_generation([]) == ([], [])


class TestDominionEnergyDataContract:
    """The dataclass fields the sensor platform is coded against."""

    @staticmethod
    def _build(**overrides: Any) -> Any:
        base: dict[str, Any] = {
            "intervals": [],
            "latest_interval": None,
            "daily_total": 0.0,
            "monthly_total": 0.0,
            "daily_cost": 0.0,
            "monthly_cost": 0.0,
            "bill_forecast": None,
            "data_date": None,
            "month_start_date": None,
            "month_end_date": None,
        }
        base.update(overrides)
        return DominionEnergyData(**base)

    def test_latest_generation_is_none_without_an_interval(self) -> None:
        assert self._build().latest_generation is None

    def test_latest_generation_reads_the_latest_interval(self) -> None:
        interval = FakeInterval(datetime(2026, 8, 10, 23, 30, tzinfo=NY), 0.5, 0.25)
        assert self._build(latest_interval=interval).latest_generation == pytest.approx(
            0.25
        )

    def test_latest_generation_is_zero_for_a_non_solar_meter(self) -> None:
        """Zero, not None: the meter reported, it just exported nothing."""
        interval = FakeInterval(datetime(2026, 8, 10, 23, 30, tzinfo=NY), 0.5)
        assert self._build(latest_interval=interval).latest_generation == 0.0

    def test_new_fields_default_to_none_or_zero(self) -> None:
        """Unavailable values are None; generation totals are real zeros."""
        data = self._build()

        assert data.daily_generation_total == 0.0
        assert data.monthly_generation_total == 0.0
        assert data.has_generation is False

        for field in (
            "period_to_date_usage",
            "period_to_date_cost",
            "projected_period_usage",
            "projected_period_cost",
            "rate_check_estimated",
            "rate_check_actual",
            "rate_check_discrepancy",
        ):
            assert getattr(data, field) is None, field


class TestInvertedStatisticsWindow:
    """A computed start_date after data_date must not reach the API.

    Regression for a live warning: `Could not fetch statistics update data:
    API error: 400`. The stale-zero heal branch sets
    ``start_date = last_good_date + 1 day``; when the last fully-populated day
    *is* ``data_date``, that lands one day past the end of the window. Dominion
    answers an inverted range with HTTP 400, so the cycle warned every hour
    instead of recognising it was already up to date.
    """

    def test_heal_branch_landing_past_data_date_is_not_fetched(self) -> None:
        data_date = date(2026, 8, 10)
        last_good_date = data_date  # last fully-populated day is data_date itself
        start_date = last_good_date + timedelta(days=1)
        assert start_date > data_date
        assert not statistics_window_is_fetchable(start_date, data_date)

    def test_normal_incremental_window_is_fetchable(self) -> None:
        data_date = date(2026, 8, 10)
        start_date = date(2026, 8, 8) + timedelta(days=1)
        assert statistics_window_is_fetchable(start_date, data_date)

    def test_same_day_rewrite_is_fetchable(self) -> None:
        data_date = date(2026, 8, 10)
        assert statistics_window_is_fetchable(data_date, data_date)
