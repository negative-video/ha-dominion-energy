"""VA Residential Rate Schedule 1 data and calculation engine.

Rate data is effective-dated: Dominion re-files individual riders throughout
the year (the fuel factor, Rider A, is re-set annually around July 1), so a
single hard-coded schedule goes stale the moment the SCC approves a change.
`VA_SCHEDULE_1_HISTORY` holds every schedule we have encoded, and
`get_schedule_for_date()` picks the one that was in effect on a given day.
That matters for historical backfill, which can span a rate change.

See docs/rate-schedules.md for where the numbers come from and how to add a
new dated schedule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from itertools import pairwise


class Season(Enum):
    """Billing season."""

    SUMMER = "summer"  # Jun-Sep
    WINTER = "winter"  # Oct-May


@dataclass(frozen=True)
class TieredRate:
    """Rate with a kWh boundary (e.g., first 800 kWh vs. over 800 kWh)."""

    boundary_kwh: float
    rate_under: float  # $/kWh for usage <= boundary
    rate_over: float  # $/kWh for usage > boundary


@dataclass(frozen=True)
class SeasonalTieredRates:
    """Summer and winter tiered rates for a component."""

    summer: TieredRate
    winter: TieredRate


@dataclass(frozen=True)
class FlatRider:
    """A flat per-kWh rider/surcharge."""

    name: str
    rate: float  # $/kWh


@dataclass(frozen=True)
class ConsumptionTaxTier:
    """A consumption tax tier with kWh range."""

    lower_kwh: float  # inclusive
    upper_kwh: float  # exclusive (use float('inf') for unbounded)
    rate: float  # $/kWh


@dataclass(frozen=True)
class RateSchedule:
    """Complete rate schedule for a Dominion Energy tariff."""

    name: str
    effective_date: str
    customer_charge: float  # $/month flat charge
    distribution: SeasonalTieredRates
    generation: SeasonalTieredRates
    transmission_rate: float  # $/kWh flat
    riders: list[FlatRider] = field(default_factory=list)
    consumption_tax_tiers: list[ConsumptionTaxTier] = field(default_factory=list)
    # Provenance, so the integration can surface where a number came from.
    source_url: str = ""
    source_retrieved: str = ""  # ISO date this schedule was last checked

    @property
    def effective_from(self) -> date:
        """`effective_date` parsed as a `datetime.date`."""
        return date.fromisoformat(self.effective_date)

    @property
    def total_rider_rate(self) -> float:
        """Sum of all flat per-kWh riders, in $/kWh."""
        return sum(rider.rate for rider in self.riders)


# ---------------------------------------------------------------------------
# Virginia Residential Schedule 1
# ---------------------------------------------------------------------------
#
# PRIMARY SOURCES (all retrieved 2026-08-11):
#
# [1] Schedule 1 RESIDENTIAL SERVICE tariff
#     https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/residential/schedule-1.pdf
#     "Filed 06-16-26 Superseding Filing Effective For Usage On and After
#      01-01-26. This Filing Effective For Usage On and After 07-01-26."
#
# [2] FUEL CHARGE RIDER A
#     https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/shared/rider-a.pdf
#     "Filed 05-29-26 ... This Filing Effective For Usage On and After 07-01-26
#      On An Interim Basis." -> "increased by 3.7648 cents per kilowatt-hour."
#
# [3] EXHIBIT OF APPLICABLE RIDERS
#     https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/shared/exhibit-of-applicable-riders.pdf
#     "Filed 06-25-26 Effective 07-01-26" - lists which riders apply to
#     Schedule 1 and the effective date of each.
#
# [4] Dominion's own bill calculation worksheet (hidden "Rate Update Sheet"
#     tab carries every rider rate and its effective date)
#     https://www.dominionenergy.com/-/media/content/paying-my-bill/understand-my-bill/files/bill-calculator-worksheet-va.xlsx
#     Linked from https://www.dominionenergy.com/virginia/paying-my-bill/understand-my-bill
#     Live copy is stamped "Last Updated 2026-07-29".
#     docs/bill-calculator-worksheet-va.xlsx is the older 2025-12-19 copy that
#     the 2026-01-01 schedule below was originally derived from.
#
# The base Schedule 1 charges below (customer charge, distribution, generation,
# transmission) are IDENTICAL in the 01-01-26 and 07-01-26 tariff filings --
# confirmed against both [1] and [4]. Everything that moved in 2026 moved
# through the riders, so only the rider lists differ between schedules.

_CUSTOMER_CHARGE = 7.58  # $/billing month, source [1] II.A.1

# Source [1] II.A.2 - same rates in both seasons.
_DISTRIBUTION = SeasonalTieredRates(
    summer=TieredRate(boundary_kwh=800, rate_under=0.03569, rate_over=0.023596),
    winter=TieredRate(boundary_kwh=800, rate_under=0.03569, rate_over=0.023596),
)

# Source [1] II.B.1
_GENERATION = SeasonalTieredRates(
    summer=TieredRate(boundary_kwh=800, rate_under=0.031212, rate_over=0.046243),
    winter=TieredRate(boundary_kwh=800, rate_under=0.030064, rate_over=0.026965),
)

_TRANSMISSION_RATE = 0.0097  # $/kWh, source [1] II.B.2.a (0.970 cents)

# Source [4], "Total Tax Rate" block (state consumption tax + special
# consumption tax + local consumption tax, combined). Unchanged since
# 2025-08-01 and identical in both worksheet copies.
_CONSUMPTION_TAX_TIERS = [
    ConsumptionTaxTier(lower_kwh=0, upper_kwh=2500, rate=0.001565),
    ConsumptionTaxTier(lower_kwh=2500, upper_kwh=50000, rate=0.001055),
    ConsumptionTaxTier(lower_kwh=50000, upper_kwh=float("inf"), rate=0.000845),
]

# Riders in effect on 2026-01-01, from the 2025-12-19 worksheet copy in docs/.
# Rates are $/kWh; the worksheet states them in cents/kWh.
_RIDERS_2026_01_01 = [
    FlatRider("C1A", 0.000231),
    FlatRider("C4A", 0.001336),
    FlatRider("DIST", 0.006241),
    FlatRider("RBB", 0.000531),
    FlatRider("E", 0.000625),
    FlatRider("GEN", 0.007564),
    FlatRider("SMR", 0.000287),
    FlatRider("SNA", 0.003475),
    FlatRider("CCR", 0.001765),
    FlatRider("CE", 0.003668),
    FlatRider("OSW", 0.011229),
    FlatRider("RPS", 0.007676),
    FlatRider("T1", 0.011789),
    FlatRider("Fuel/A", 0.02968),
    FlatRider("DFCC", 0.002906),
    FlatRider("Sales&Use", 0.000921),
]

# Riders Dominion lists at 0.000 for Schedule 1 (PIPP, RGGI) are omitted; they
# contribute nothing and adding them would only be cosmetic.

_WORKSHEET_2026_07_29 = (
    "https://www.dominionenergy.com/-/media/content/paying-my-bill/"
    "understand-my-bill/files/bill-calculator-worksheet-va.xlsx"
)
_RETRIEVED = "2026-08-11"


def _riders(
    base: list[FlatRider],
    changed: dict[str, float] | None = None,
    added: dict[str, float] | None = None,
) -> list[FlatRider]:
    """Build a rider list from `base` by re-rating and/or adding riders.

    Args:
        base: The rider list to start from.
        changed: Rider name -> new $/kWh rate. Every name must already exist in
            `base`; a `KeyError` here means a typo rather than a real change.
        added: Rider name -> $/kWh rate for riders that did not exist in `base`.

    Returns:
        A new rider list. `base` is left untouched.
    """
    changed = changed or {}
    added = added or {}

    known = {rider.name for rider in base}
    if unknown := set(changed) - known:
        raise KeyError(f"Unknown rider(s) in `changed`: {sorted(unknown)}")
    if clashing := set(added) & known:
        raise KeyError(f"Rider(s) in `added` already exist: {sorted(clashing)}")

    result = [
        FlatRider(rider.name, changed.get(rider.name, rider.rate)) for rider in base
    ]
    result.extend(FlatRider(name, rate) for name, rate in added.items())
    return result


def _schedule(
    effective_date: str,
    riders: list[FlatRider],
    source_url: str,
) -> RateSchedule:
    """Build a Schedule 1 rate schedule; only the riders vary across 2026."""
    return RateSchedule(
        name="Schedule 1 - VA Residential",
        effective_date=effective_date,
        customer_charge=_CUSTOMER_CHARGE,
        distribution=_DISTRIBUTION,
        generation=_GENERATION,
        transmission_rate=_TRANSMISSION_RATE,
        riders=riders,
        consumption_tax_tiers=list(_CONSUMPTION_TAX_TIERS),
        source_url=source_url,
        source_retrieved=_RETRIEVED,
    )


# Rider changes through 2026, each reconstructed from the per-rider effective
# dates published in the "Rate Update Sheet" tab of source [4] and corroborated
# by the effective dates in source [3]. Every schedule below is a snapshot of
# the composite rate set on that day.
#
# Every rate below was also checked against its own tariff PDF
# (.../virginia/shared/rider-<code>.pdf) on 2026-08-11 and matched, with the
# single exception of Rider RBB noted at the 2026-06-01 entry.
_RIDERS_2026_03_01 = _riders(
    _RIDERS_2026_01_01,
    # Rider CERC (Chesterfield Energy Reliability Center) is new; source [3]
    # lists it "Effective for Usage On and After 03-01-26".
    added={"CERC": 0.000754},
)
_RIDERS_2026_04_01 = _riders(_RIDERS_2026_03_01, changed={"GEN": 0.005729})
_RIDERS_2026_05_01 = _riders(_RIDERS_2026_04_01, changed={"CE": 0.006054})
# RBB -> 0 is the one component we could NOT confirm from a tariff PDF: there
# is no rider-rbb.pdf and "RBB" appears nowhere in the entire filed tariff
# (revised 08-01-26). Source [4] carries it at 0.0000 effective 2026-06-01,
# consistent with it having been folded into Rider DIST, which takes its own
# new rate the same day. See docs/rate-schedules.md.
_RIDERS_2026_06_01 = _riders(_RIDERS_2026_05_01, changed={"DIST": 0.007685, "RBB": 0.0})
# The July 1 fuel factor: 2.968 -> 3.7648 cents/kWh, i.e. +$7.97/month for a
# 1,000 kWh customer. Independently confirmed in source [2].
_RIDERS_2026_07_01 = _riders(_RIDERS_2026_06_01, changed={"Fuel/A": 0.037648})
_RIDERS_2026_08_01 = _riders(_RIDERS_2026_07_01, changed={"DFCC": 0.002901})


VA_SCHEDULE_1_HISTORY: tuple[RateSchedule, ...] = (
    # Derived from docs/bill-calculator-worksheet-va.xlsx (worksheet copy
    # stamped "Last Updated 2025-12-19").
    _schedule(
        "2026-01-01",
        _RIDERS_2026_01_01,
        "https://www.dominionenergy.com/virginia/paying-my-bill/understand-my-bill",
    ),
    _schedule("2026-03-01", _RIDERS_2026_03_01, _WORKSHEET_2026_07_29),
    _schedule("2026-04-01", _RIDERS_2026_04_01, _WORKSHEET_2026_07_29),
    _schedule("2026-05-01", _RIDERS_2026_05_01, _WORKSHEET_2026_07_29),
    _schedule("2026-06-01", _RIDERS_2026_06_01, _WORKSHEET_2026_07_29),
    _schedule("2026-07-01", _RIDERS_2026_07_01, _WORKSHEET_2026_07_29),
    _schedule("2026-08-01", _RIDERS_2026_08_01, _WORKSHEET_2026_07_29),
)

# Sanity guard: a mis-ordered history would silently return the wrong schedule.
assert all(
    a.effective_from < b.effective_from for a, b in pairwise(VA_SCHEDULE_1_HISTORY)
), "VA_SCHEDULE_1_HISTORY must be sorted by ascending effective date"


def _today() -> date:
    """Today's date in local time.

    Tariff effective dates ("effective for usage on and after 07-01-26") are
    local calendar dates, so a UTC-forced date would switch schedules up to a
    day early or late. This module is deliberately free of Home Assistant
    imports, so `homeassistant.util.dt` is not available here.
    """
    return date.today()  # noqa: DTZ011


def get_schedule_for_date(d: date) -> RateSchedule:
    """Return the rate schedule that was in effect on `d`.

    That is, the most recent schedule whose effective date is <= `d`.

    Dates earlier than the oldest encoded schedule fall back to that oldest
    schedule rather than raising, so a historical backfill that reaches further
    back than our rate data still produces a usable (if approximate) number.

    Args:
        d: The usage date. A `datetime` is accepted and its date is used.

    Returns:
        The applicable `RateSchedule`.
    """
    if isinstance(d, datetime):
        d = d.date()

    applicable = VA_SCHEDULE_1_HISTORY[0]
    for schedule in VA_SCHEDULE_1_HISTORY:
        if schedule.effective_from > d:
            break
        applicable = schedule
    return applicable


# Currently-effective schedule. Kept as a module-level alias so existing
# callers keep working; anything that touches historical data should call
# `get_schedule_for_date()` instead, since a backfill can cross a rate change.
VA_SCHEDULE_1 = get_schedule_for_date(_today())

# Newest rate data we have encoded, for staleness checks.
LATEST_SCHEDULE_EFFECTIVE_DATE = VA_SCHEDULE_1_HISTORY[-1].effective_date

# Dominion re-sets the fuel factor annually around July 1, and individual
# riders are re-filed throughout the year. If the newest schedule we know about
# is older than this, rates.py has almost certainly missed a filing.
SCHEDULE_STALE_AFTER_DAYS = 400


def is_schedule_possibly_stale(
    today: date | None = None,
    max_age_days: int = SCHEDULE_STALE_AFTER_DAYS,
) -> bool:
    """Report whether the newest encoded schedule looks out of date.

    This is a pure date comparison against the rate table baked into this
    module - it does not reach the network.

    Args:
        today: Date to measure against. Defaults to the current date.
        max_age_days: Age beyond which the newest schedule is suspect.

    Returns:
        True if the newest encoded schedule took effect more than
        `max_age_days` ago.
    """
    today = today or _today()
    newest = VA_SCHEDULE_1_HISTORY[-1].effective_from
    return (today - newest).days > max_age_days


def get_season(month: int) -> Season:
    """Determine billing season from month number (1-12)."""
    if 6 <= month <= 9:
        return Season.SUMMER
    return Season.WINTER


def calculate_tiered_cost(
    interval_kwh: float,
    cumulative_before: float,
    tiered_rate: TieredRate,
) -> float:
    """Calculate cost for a single interval using tiered pricing.

    Handles the case where cumulative usage straddles the tier boundary
    within this interval.

    Args:
        interval_kwh: kWh consumed in this interval.
        cumulative_before: Total kWh consumed before this interval in the billing period.
        tiered_rate: The tiered rate to apply.

    Returns:
        Cost in dollars for this interval.
    """
    boundary = tiered_rate.boundary_kwh
    cumulative_after = cumulative_before + interval_kwh

    if cumulative_after <= boundary:
        # All usage in lower tier
        return interval_kwh * tiered_rate.rate_under
    if cumulative_before >= boundary:
        # All usage in upper tier
        return interval_kwh * tiered_rate.rate_over

    # Straddles the boundary
    kwh_under = boundary - cumulative_before
    kwh_over = interval_kwh - kwh_under
    return kwh_under * tiered_rate.rate_under + kwh_over * tiered_rate.rate_over


def calculate_consumption_tax(
    interval_kwh: float,
    cumulative_before: float,
    tax_tiers: list[ConsumptionTaxTier],
) -> float:
    """Calculate consumption tax for an interval across tiered tax brackets.

    Args:
        interval_kwh: kWh consumed in this interval.
        cumulative_before: Total kWh consumed before this interval in the billing period.
        tax_tiers: List of consumption tax tiers (must be sorted by lower_kwh).

    Returns:
        Tax in dollars for this interval.
    """
    tax = 0.0
    remaining = interval_kwh
    position = cumulative_before

    for tier in tax_tiers:
        if remaining <= 0:
            break
        if position >= tier.upper_kwh:
            # Already past this tier
            continue
        # Shouldn't happen with contiguous tiers, but handle gracefully
        position = max(position, tier.lower_kwh)

        # How much of the remaining interval falls in this tier
        room_in_tier = tier.upper_kwh - position
        kwh_in_tier = min(remaining, room_in_tier)
        tax += kwh_in_tier * tier.rate
        remaining -= kwh_in_tier
        position += kwh_in_tier

    return tax


def calculate_schedule1_interval_cost(
    interval_kwh: float,
    interval_dt: datetime,
    cumulative_before: float,
    schedule: RateSchedule,
    billing_period_days: int = 30,
) -> float:
    """Calculate full Schedule 1 cost for a single 30-minute interval.

    Args:
        interval_kwh: kWh consumed in this interval.
        interval_dt: Timestamp of the interval (used for season determination).
        cumulative_before: Total kWh consumed before this interval in the billing period.
        schedule: The rate schedule to use. For historical data, get this from
            `get_schedule_for_date(interval_dt.date())` so that a backfill
            spanning a rate change is priced correctly.
        billing_period_days: Length of billing period in days (for prorating customer charge).

    Returns:
        Total cost in dollars for this interval.
    """
    if interval_kwh <= 0:
        return 0.0

    season = get_season(interval_dt.month)

    # Distribution (tiered)
    dist_rate = (
        schedule.distribution.summer
        if season == Season.SUMMER
        else schedule.distribution.winter
    )
    dist_cost = calculate_tiered_cost(interval_kwh, cumulative_before, dist_rate)

    # Generation (tiered)
    gen_rate = (
        schedule.generation.summer
        if season == Season.SUMMER
        else schedule.generation.winter
    )
    gen_cost = calculate_tiered_cost(interval_kwh, cumulative_before, gen_rate)

    # Transmission (flat)
    trans_cost = interval_kwh * schedule.transmission_rate

    # Riders (flat per kWh)
    rider_cost = sum(rider.rate * interval_kwh for rider in schedule.riders)

    # Consumption tax (tiered)
    tax_cost = calculate_consumption_tax(
        interval_kwh, cumulative_before, schedule.consumption_tax_tiers
    )

    # Prorated customer charge: $7.58/month spread across all intervals
    # 48 intervals/day * billing_period_days
    intervals_in_period = 48 * billing_period_days
    customer_charge_per_interval = schedule.customer_charge / intervals_in_period

    return (
        dist_cost
        + gen_cost
        + trans_cost
        + rider_cost
        + tax_cost
        + customer_charge_per_interval
    )


@dataclass(frozen=True)
class PeriodBill:
    """A full-billing-period Schedule 1 bill, broken out by component."""

    total: float
    customer_charge: float
    distribution: float
    generation: float
    transmission: float
    riders: float
    consumption_tax: float
    total_kwh: float
    season: Season
    schedule_name: str
    schedule_effective_date: str


def calculate_schedule1_period_bill(
    total_kwh: float,
    period_start: date,
    period_end: date,
    schedule: RateSchedule | None = None,
) -> PeriodBill:
    """Compute a whole-billing-period Schedule 1 bill from a total kWh figure.

    This is the counterpart to `calculate_schedule1_interval_cost()`: rather
    than accumulating interval by interval, it applies the tiers once to the
    period total, the way a paper bill does. It exists so the integration can
    check its own estimate against the amount Dominion actually billed
    (`last_bill.charges`) and surface a discrepancy when the encoded rates have
    drifted -- see `bill_discrepancy()`.

    Season is taken from the midpoint of the period, since the tariff prices a
    whole billing month at one season's rates rather than day by day. The
    customer charge is applied once; bimonthly bills (which the tariff handles
    by doubling the charge and the tier boundaries) are not modelled.

    Args:
        total_kwh: Total kWh for the billing period.
        period_start: First day of the billing period.
        period_end: Last day of the billing period.
        schedule: Rate schedule to use. Defaults to the schedule in effect at
            the midpoint of the period.

    Returns:
        A `PeriodBill` with the total and its components in dollars.
    """
    midpoint = period_start + (period_end - period_start) / 2
    if isinstance(midpoint, datetime):
        midpoint = midpoint.date()
    if schedule is None:
        schedule = get_schedule_for_date(midpoint)

    season = get_season(midpoint.month)
    kwh = max(total_kwh, 0.0)

    dist_rate = (
        schedule.distribution.summer
        if season == Season.SUMMER
        else schedule.distribution.winter
    )
    gen_rate = (
        schedule.generation.summer
        if season == Season.SUMMER
        else schedule.generation.winter
    )

    distribution = calculate_tiered_cost(kwh, 0.0, dist_rate)
    generation = calculate_tiered_cost(kwh, 0.0, gen_rate)
    transmission = kwh * schedule.transmission_rate
    riders = kwh * schedule.total_rider_rate
    consumption_tax = calculate_consumption_tax(
        kwh, 0.0, schedule.consumption_tax_tiers
    )

    total = (
        schedule.customer_charge
        + distribution
        + generation
        + transmission
        + riders
        + consumption_tax
    )

    return PeriodBill(
        total=total,
        customer_charge=schedule.customer_charge,
        distribution=distribution,
        generation=generation,
        transmission=transmission,
        riders=riders,
        consumption_tax=consumption_tax,
        total_kwh=kwh,
        season=season,
        schedule_name=schedule.name,
        schedule_effective_date=schedule.effective_date,
    )


def bill_discrepancy(estimated: float, actual: float) -> float | None:
    """Signed fractional error of an estimated bill against the billed amount.

    A return of ``0.08`` means the estimate came out 8% above what Dominion
    actually charged, which usually means the rate table here is behind a
    filing (or ahead of one).

    Args:
        estimated: Our estimate in dollars, e.g. `PeriodBill.total`.
        actual: The amount actually billed, in dollars.

    Returns:
        `(estimated - actual) / actual`, or None if `actual` is not positive.
    """
    if actual <= 0:
        return None
    return (estimated - actual) / actual


def days_since_schedule_change(today: date | None = None) -> int:
    """Days since the newest encoded rate schedule took effect.

    Useful as a diagnostic attribute alongside
    `LATEST_SCHEDULE_EFFECTIVE_DATE`.
    """
    today = today or _today()
    return (today - VA_SCHEDULE_1_HISTORY[-1].effective_from).days


__all__ = [
    "LATEST_SCHEDULE_EFFECTIVE_DATE",
    "SCHEDULE_STALE_AFTER_DAYS",
    "VA_SCHEDULE_1",
    "VA_SCHEDULE_1_HISTORY",
    "ConsumptionTaxTier",
    "FlatRider",
    "PeriodBill",
    "RateSchedule",
    "Season",
    "SeasonalTieredRates",
    "TieredRate",
    "bill_discrepancy",
    "calculate_consumption_tax",
    "calculate_schedule1_interval_cost",
    "calculate_schedule1_period_bill",
    "calculate_tiered_cost",
    "days_since_schedule_change",
    "get_schedule_for_date",
    "get_season",
    "is_schedule_possibly_stale",
]
