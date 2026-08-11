"""Pure helpers for the derived usage insights.

Like :mod:`usage`, this module deliberately contains no Home Assistant imports
so the logic can be unit tested without a Home Assistant installation. Every
timestamp handled here is timezone aware and in the meter's local time, which
is what makes "the 6 PM hour" meaningful at all.

What these helpers have in common is that they turn the interval data the
coordinator already fetches into a plain-language statement about the
household. None of it needs an extra API call -- the API hands back its whole
~68 day workbook whatever range was asked for, so the history is already in
memory by the time the sensors read it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median

from .usage import UsageInterval, day_looks_complete

#: Days of history averaged into the daily shape. Long enough to average out a
#: single odd day, short enough to still reflect the current season.
PROFILE_DAYS = 30

#: A profile built from fewer days than this says more about the week than the
#: household, so it is not published at all.
MIN_PROFILE_DAYS = 7


def hour_label(hour: int) -> str:
    """Render an hour of the day the way a person would say it.

    ``18`` becomes ``"6 PM"``. Deliberately not localised: this integration
    ships English strings only, and an hour rendered as ``18`` on a dashboard
    card is no use to the people these sensors are for.
    """
    if hour == 0:
        return "12 AM"
    if hour < 12:
        return f"{hour} AM"
    if hour == 12:
        return "12 PM"
    return f"{hour - 12} PM"


def complete_days_by_date[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    *,
    through: date,
    days: int,
) -> dict[date, list[IntervalT]]:
    """Group intervals into the most recent fully published days.

    Days the API has only partly published are dropped rather than averaged in:
    a day holding six hours of readings would drag every hourly average it
    touches towards zero and leave the quiet hours looking quieter than they
    are. ``through`` bounds the window at the top so a stray future-dated row
    cannot shift it.
    """
    grouped: dict[date, list[IntervalT]] = defaultdict(list)
    for interval in intervals:
        day = interval.timestamp.date()
        if day <= through:
            grouped[day].append(interval)

    complete = {day: rows for day, rows in grouped.items() if day_looks_complete(rows)}
    keep = sorted(complete)[-days:]
    return {day: sorted(complete[day], key=lambda i: i.timestamp) for day in keep}


@dataclass(frozen=True)
class UsageProfile:
    """The average shape of a day, and the hours at either end of it."""

    #: Mean kWh consumed in each hour of the day, indexed by hour (0-23).
    hourly_average: list[float]
    peak_hour: int
    peak_average: float
    quietest_hour: int
    quietest_average: float
    average_daily_kwh: float
    days: int
    first_day: date
    last_day: date

    @property
    def peak_label(self) -> str:
        """The busiest hour, as a person would say it."""
        return hour_label(self.peak_hour)

    @property
    def quietest_label(self) -> str:
        """The quietest hour, as a person would say it."""
        return hour_label(self.quietest_hour)


def usage_profile[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    *,
    through: date,
    days: int = PROFILE_DAYS,
    min_days: int = MIN_PROFILE_DAYS,
) -> UsageProfile | None:
    """Average recent complete days into a 24-hour shape.

    Averaging by hour-of-day across whole days rather than summing the window
    keeps the answer readable as "a typical day here": every day contributes
    one reading to each hour, so a single 40 kWh Saturday moves the shape but
    does not define it.

    Returns None when fewer than ``min_days`` complete days are available -- on
    a fresh install that is the honest answer, and an entity that reports
    ``unknown`` for its first week is better than one that confidently reports
    the shape of a Tuesday.
    """
    by_day = complete_days_by_date(intervals, through=through, days=days)
    if len(by_day) < min_days:
        return None

    hourly_totals: list[float] = [0.0] * 24
    for rows in by_day.values():
        for interval in rows:
            hourly_totals[interval.timestamp.hour] += interval.consumption

    day_count = len(by_day)
    hourly_average = [total / day_count for total in hourly_totals]

    peak_hour = max(range(24), key=lambda hour: hourly_average[hour])
    quietest_hour = min(range(24), key=lambda hour: hourly_average[hour])

    return UsageProfile(
        hourly_average=[round(value, 3) for value in hourly_average],
        peak_hour=peak_hour,
        peak_average=round(hourly_average[peak_hour], 3),
        quietest_hour=quietest_hour,
        quietest_average=round(hourly_average[quietest_hour], 3),
        average_daily_kwh=round(sum(hourly_average), 3),
        days=day_count,
        first_day=min(by_day),
        last_day=max(by_day),
    )


#: Weeks of the same weekday compared against when judging a day's usage.
COMPARISON_WEEKS = 4

#: How far a day has to sit from its own weekday's median to be worth pointing
#: at. Interval data is noisy enough that a tighter threshold would fire most
#: weeks, and an alert that fires most weeks is one people turn off.
UNUSUAL_DAY_THRESHOLD = 0.4

#: Fewer prior same-weekdays than this and "typical" is not a claim worth
#: making.
MIN_COMPARISON_DAYS = 2


@dataclass(frozen=True)
class DayComparison:
    """One day measured against how that weekday usually goes."""

    day: date
    total: float
    typical: float
    #: Signed fraction: ``0.5`` means half again as much as usual.
    delta: float
    compared_days: int
    threshold: float

    @property
    def unusual(self) -> bool:
        """Whether the day is far enough from typical to be worth flagging."""
        return abs(self.delta) >= self.threshold

    @property
    def direction(self) -> str:
        """Which way the day went, for a human-readable attribute."""
        if not self.unusual:
            return "typical"
        return "higher" if self.delta > 0 else "lower"


def compare_to_typical_day[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    *,
    day: date,
    weeks: int = COMPARISON_WEEKS,
    threshold: float = UNUSUAL_DAY_THRESHOLD,
    min_days: int = MIN_COMPARISON_DAYS,
) -> DayComparison | None:
    """Measure one day against the same weekday over the preceding weeks.

    Same weekday, not a trailing average: household electricity is strongly
    weekly, so comparing a Saturday against a window that was mostly weekdays
    reports "unusual" every Saturday. An alert that cries wolf on a schedule
    teaches people to ignore it.

    The comparison uses the median rather than the mean so one already
    exceptional day in the history cannot raise the bar and hide the next one.

    Returns None when the day itself is missing or incomplete, or when fewer
    than ``min_days`` comparable days sit behind it.
    """
    by_day = complete_days_by_date(intervals, through=day, days=weeks * 7 + 1)
    if day not in by_day:
        return None

    history = [
        sum(i.consumption for i in rows)
        for other, rows in by_day.items()
        if other != day
        and other.weekday() == day.weekday()
        and (day - other).days <= weeks * 7
    ]
    if len(history) < min_days:
        return None

    typical = median(history)
    if typical <= 0:
        return None

    total = sum(i.consumption for i in by_day[day])
    return DayComparison(
        day=day,
        total=round(total, 3),
        typical=round(typical, 3),
        delta=round((total - typical) / typical, 4),
        compared_days=len(history),
        threshold=threshold,
    )


__all__ = [
    "COMPARISON_WEEKS",
    "MIN_COMPARISON_DAYS",
    "MIN_PROFILE_DAYS",
    "PROFILE_DAYS",
    "UNUSUAL_DAY_THRESHOLD",
    "DayComparison",
    "UsageProfile",
    "compare_to_typical_day",
    "complete_days_by_date",
    "hour_label",
    "usage_profile",
]
