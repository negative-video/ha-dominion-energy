"""Pure helpers for turning interval usage data into hourly statistics.

This module deliberately contains no Home Assistant imports so the logic can be
unit tested without a Home Assistant installation. Every timestamp handled here
is timezone aware (the dompower client always returns America/New_York aware
datetimes), so ``astimezone(UTC)`` is enough to normalise them.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

# Fallback billing period length when the bill forecast is unavailable.
DEFAULT_BILLING_PERIOD_DAYS = 30

# Plausible bounds for a billing period reported by the API. Anything outside
# this range is treated as bogus so fixed charges are never prorated over an
# absurd number of days.
MIN_BILLING_PERIOD_DAYS = 20
MAX_BILLING_PERIOD_DAYS = 45

# A day with fewer non-zero intervals than this is treated as incomplete.
MIN_NONZERO_INTERVALS = 4

# A fully published day has 48 half-hour intervals (46 on DST spring-forward,
# 50 on fall-back) and extends into the last hours of the day.
MIN_COMPLETE_DAY_INTERVALS = 44
MIN_COMPLETE_DAY_LAST_HOUR = 22


class UsageInterval(Protocol):
    """Structural type matching ``dompower.IntervalUsageData``.

    Declared as read-only properties rather than plain attributes:
    ``IntervalUsageData`` is a frozen dataclass, so its attributes are
    read-only and would not satisfy a Protocol asking for settable ones.
    """

    @property
    def timestamp(self) -> datetime: ...

    @property
    def consumption(self) -> float: ...


@dataclass(frozen=True)
class CumulativeStatistic:
    """One hourly statistic: the hour's own value plus the running total."""

    start: datetime
    state: float
    sum: float


def shift_months(anchor: date, months: int) -> date:
    """Shift a date by whole months, clamping the day to the target month."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


def billing_period_days(
    period_start: date | None,
    period_end: date | None,
    default: int = DEFAULT_BILLING_PERIOD_DAYS,
) -> int:
    """Return the number of days covered by a billing period.

    Dominion reports the period as meter-read to meter-read dates, so the number
    of billed days is ``end - start``. A missing or implausible period falls back
    to ``default``.
    """
    if period_start is None or period_end is None:
        return default
    days = (period_end - period_start).days
    if MIN_BILLING_PERIOD_DAYS <= days <= MAX_BILLING_PERIOD_DAYS:
        return days
    return default


def billing_period_start(target: date, anchor: date | None) -> date:
    """Return the start date of the billing period containing ``target``.

    ``anchor`` is the start of the *current* billing period as reported by the
    bill forecast. Dominion reads meters mid-month, so tiered pricing and the
    monthly customer charge reset on that cycle rather than on the 1st of the
    calendar month.

    Only the current period start is known, so earlier periods are approximated
    by stepping the anchor whole months backwards (clamped to the length of each
    month). Real meter-read dates drift by a few days each cycle, so historical
    period boundaries may be off by a day or two — still far closer to the bill
    than a calendar month reset. Without an anchor we fall back to calendar
    months.
    """
    if anchor is None:
        return target.replace(day=1)

    # Start from the whole-month difference, then correct for clamping.
    months = (target.year - anchor.year) * 12 + (target.month - anchor.month)
    while shift_months(anchor, months) > target:
        months -= 1
    while shift_months(anchor, months + 1) <= target:
        months += 1
    return shift_months(anchor, months)


def filter_incomplete_days[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    min_nonzero_intervals: int = MIN_NONZERO_INTERVALS,
) -> tuple[list[IntervalT], list[date]]:
    """Drop days with zero or suspiciously incomplete data.

    A normal day has 48 half-hour intervals (46 on DST spring-forward). Days with
    zero total consumption, or with only a handful of non-zero intervals, are
    most likely not published by the API yet; recording them would leave
    permanent zero-value statistics behind.

    Returns the intervals to keep and the sorted list of days that were dropped.
    """
    totals: dict[date, float] = {}
    nonzero: dict[date, int] = {}
    for interval in intervals:
        day = interval.timestamp.date()
        totals[day] = totals.get(day, 0.0) + interval.consumption
        nonzero.setdefault(day, 0)
        if interval.consumption > 0:
            nonzero[day] += 1

    bad_days = {
        day
        for day, total in totals.items()
        if total == 0 or nonzero[day] < min_nonzero_intervals
    }
    if not bad_days:
        return list(intervals), []

    kept = [i for i in intervals if i.timestamp.date() not in bad_days]
    return kept, sorted(bad_days)


def day_looks_complete(intervals: Sequence[UsageInterval]) -> bool:
    """Return True when the intervals look like a fully published day.

    Used to decide whether a day's data can be cached until the next day rolls
    over, or whether it should be re-fetched because the API had not published
    all of it yet.
    """
    if len(intervals) < MIN_COMPLETE_DAY_INTERVALS:
        return False
    return max(i.timestamp.hour for i in intervals) >= MIN_COMPLETE_DAY_LAST_HOUR


def aggregate_hourly[IntervalT: UsageInterval](
    intervals: Iterable[IntervalT],
    cost_fn: Callable[[IntervalT, float], float],
    period_start_of: Callable[[date], date],
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """Aggregate 30-minute intervals into hourly consumption and cost buckets.

    ``cost_fn`` is called with each interval and the cumulative kWh consumed
    earlier in the same billing period (tiered schedules price on period-to-date
    usage). ``period_start_of`` maps a date onto the start of its billing period;
    the cumulative counter resets whenever that changes.

    Buckets are keyed by the local hour start — run them through
    :func:`deduplicate_hourly_by_utc` before building statistics.

    Note: on a DST fall-back day the repeated local hour cannot be split,
    because two same-zone datetimes differing only in ``fold`` compare and hash
    equal (PEP 495). Both hours land in the same bucket, so the day's total
    stays exact but that one UTC hour carries both hours' usage.
    """
    hourly_consumption: dict[datetime, float] = {}
    hourly_cost: dict[datetime, float] = {}
    cumulative_kwh = 0.0
    current_period: date | None = None

    for interval in sorted(intervals, key=lambda i: i.timestamp):
        period = period_start_of(interval.timestamp.date())
        if period != current_period:
            cumulative_kwh = 0.0
            current_period = period

        hour_start = interval.timestamp.replace(minute=0, second=0, microsecond=0)
        if hour_start not in hourly_consumption:
            hourly_consumption[hour_start] = 0.0
            hourly_cost[hour_start] = 0.0
        hourly_consumption[hour_start] += interval.consumption
        hourly_cost[hour_start] += cost_fn(interval, cumulative_kwh)
        cumulative_kwh += interval.consumption

    return hourly_consumption, hourly_cost


def deduplicate_hourly_by_utc(hourly: dict[datetime, float]) -> dict[datetime, float]:
    """Merge hourly buckets that map onto the same UTC hour.

    On DST spring-forward days two local keys (e.g. 02:00 EST and 03:00 EDT) can
    convert to the same UTC instant. Statistics are keyed by UTC start, so those
    values have to be merged instead of overwriting each other.
    """
    utc_data: dict[datetime, float] = {}
    for local_dt, value in hourly.items():
        utc_dt = local_dt.astimezone(UTC)
        utc_data[utc_dt] = utc_data.get(utc_dt, 0.0) + value
    return utc_data


def build_cumulative_statistics(
    utc_values: dict[datetime, float],
    start_sum: float = 0.0,
) -> list[CumulativeStatistic]:
    """Build ascending hourly rows carrying a continuous cumulative sum.

    ``start_sum`` is the cumulative sum immediately before the first row, so a
    rewritten window continues the existing chain instead of restarting at zero.
    """
    running = start_sum
    rows: list[CumulativeStatistic] = []
    for utc_dt in sorted(utc_values):
        value = utc_values[utc_dt]
        running += value
        rows.append(CumulativeStatistic(start=utc_dt, state=value, sum=running))
    return rows
