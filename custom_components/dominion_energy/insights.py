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


__all__ = [
    "MIN_PROFILE_DAYS",
    "PROFILE_DAYS",
    "UsageProfile",
    "complete_days_by_date",
    "hour_label",
    "usage_profile",
]
