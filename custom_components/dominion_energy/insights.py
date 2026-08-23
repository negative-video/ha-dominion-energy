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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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

    ``18`` becomes ``"6 PM"``. Deliberately not localized: this integration
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


#: Length of one published interval. The Dominion API publishes half-hourly
#: readings throughout, which is what turns a kWh reading into a power figure.
INTERVAL_MINUTES = 30

#: How many hours of the day the standing load is measured across. Five gives
#: ten candidate intervals a day -- enough that HVAC can take some and still
#: leave a reading.
BASELINE_QUIET_HOURS = 5

#: Days of history the baseline is taken across. A single day can be entirely
#: covered by HVAC runtime; a week usually is not.
BASELINE_DAYS = 7

#: Below this many usable days the baseline is not published.
MIN_BASELINE_DAYS = 3

#: ``hvac_action`` values that mean the system is drawing power right now.
#: Note ``fan`` counts: an air handler is several hundred watts and is exactly
#: the kind of load that would otherwise be mistaken for standing draw.
HVAC_RUNNING_ACTIONS = frozenset(
    {"heating", "cooling", "drying", "fan", "preheating", "defrosting"}
)

#: ``hvac_action`` values that mean it is not.
HVAC_QUIET_ACTIONS = frozenset({"idle", "off"})

#: States that carry no information about what the system is doing.
UNKNOWN_STATES = frozenset({"unknown", "unavailable", ""})


@dataclass(frozen=True)
class TimeWindow:
    """A half-open span of time, ``[start, end)``."""

    start: datetime
    end: datetime

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """Whether this window shares any time with ``[start, end)``."""
        return self.start < end and self.end > start


def merge_windows(windows: Iterable[TimeWindow]) -> list[TimeWindow]:
    """Collapse overlapping or touching windows into a minimal set.

    Several thermostats run independently, so their windows interleave. Merging
    first means the overlap test below is a scan rather than a cross product.
    """
    ordered = sorted(windows, key=lambda w: w.start)
    merged: list[TimeWindow] = []
    for window in ordered:
        if merged and window.start <= merged[-1].end:
            if window.end > merged[-1].end:
                merged[-1] = TimeWindow(merged[-1].start, window.end)
            continue
        merged.append(window)
    return merged


def hvac_active_windows(
    samples: Sequence[tuple[datetime, str | None, str | None]],
    *,
    until: datetime,
) -> list[TimeWindow]:
    """Turn one climate entity's recorded states into spans of runtime.

    Each sample is ``(timestamp, state, hvac_action)`` and holds until the next
    one, or until ``until`` for the last. Pass one entity's history at a time;
    interleaving two entities' samples would end each one's span at the other's
    next state change.

    ``hvac_action`` is the signal that matters, not the state: a thermostat set
    to ``cool`` is not drawing power until the compressor actually starts, and
    treating "set to cool" as "running" would exclude the entire night for
    anyone who leaves it on -- which is precisely the household this feature is
    for.

    Thermostats that do not report ``hvac_action`` at all fall back to the
    state, counting anything but ``off`` as running. That is the conservative
    reading: it can exclude time the system was merely idle, which understates
    how much data is available, rather than quietly folding compressor runtime
    into the standing load.
    """
    windows: list[TimeWindow] = []
    for index, (timestamp, state, action) in enumerate(samples):
        end = samples[index + 1][0] if index + 1 < len(samples) else until
        if end <= timestamp:
            continue
        if _is_running(state, action):
            windows.append(TimeWindow(timestamp, end))
    return merge_windows(windows)


def _is_running(state: str | None, action: str | None) -> bool:
    """Whether a climate entity was drawing power in this state."""
    if action is not None:
        normalized = action.lower()
        if normalized in HVAC_RUNNING_ACTIONS:
            return True
        if normalized in HVAC_QUIET_ACTIONS:
            return False
        # An action this version has not heard of. Treat it as running: a new
        # HVAC mode is far more likely to draw power than not.
        return normalized not in UNKNOWN_STATES

    if state is None:
        return False
    normalized = state.lower()
    if normalized in UNKNOWN_STATES:
        return False
    return normalized != "off"


def quietest_hours(
    profile: UsageProfile, count: int = BASELINE_QUIET_HOURS
) -> tuple[int, ...]:
    """Return the hours of the day this household is habitually quietest.

    Which hours those are is a fact about the household, not a constant. An
    overnight window assumes the house is asleep and the HVAC is off, and gets
    both wrong for anyone who cools at night: on a real meter, midnight was the
    *second-heaviest* hour of the day and the quietest was 10 AM. Measuring a
    standing load between midnight and 5 AM there produced 1508 W -- almost
    exactly the household's average draw.

    Taken from the 30-day profile rather than the baseline's own week, so the
    window reflects a settled habit while the measurement stays current. The
    hours need not be contiguous: they are candidate slots to look for a
    minimum in, and scattering them only means HVAC has to cover more of the
    day to blank the reading.

    Returned in clock order, which is how they read on a dashboard.
    """
    ranked = sorted(range(24), key=lambda hour: profile.hourly_average[hour])
    return tuple(sorted(ranked[: max(count, 1)]))


@dataclass(frozen=True)
class BaselineLoad:
    """What the house draws when nothing in particular is happening."""

    #: Standing draw in watts.
    watts: float
    #: kWh the standing draw accounts for over a full day.
    daily_kwh: float
    #: The hours of the day it was measured in.
    quiet_hours: tuple[int, ...]
    days: int
    sampled_intervals: int
    excluded_intervals: int
    #: Whether any HVAC runtime was actually excluded, so the sensor can say
    #: whether the number is thermostat-aware or raw.
    hvac_filtered: bool
    first_day: date
    last_day: date


def baseline_load[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    *,
    through: date,
    quiet_hours: Sequence[int],
    hvac_windows: Sequence[TimeWindow] = (),
    days: int = BASELINE_DAYS,
    min_days: int = MIN_BASELINE_DAYS,
) -> BaselineLoad | None:
    """Measure the household's standing draw from its quietest half-hours.

    The quietest interval of the day is the closest an interval meter gets to
    "everything that is always on": the fridge, the network gear, the standby
    loads, the well pump's controller. It is one of the few numbers this data
    can produce that a person can act on directly.

    Three things make it trustworthy rather than merely plausible:

    - **The hours come from the household**, via `quietest_hours()`, rather
      than from an assumption about when people sleep. See that function for
      what assuming overnight cost.
    - **Any interval overlapping HVAC runtime is discarded.** A compressor
      cycling through the quiet hours otherwise sets the floor, and the sensor
      reports the air conditioner instead of the house. This is why the
      thermostats are worth configuring.
    - **The lowest interval of each day, then the median across days.** A
      single day can be fully covered by HVAC or spoilt by one odd reading;
      the median across a week is not.

    Returns None when fewer than ``min_days`` days have a usable interval --
    which under continuous air conditioning is the honest answer, and is why
    `excluded_intervals` is reported alongside.
    """
    per_interval = timedelta(minutes=INTERVAL_MINUTES)
    wanted = set(quiet_hours)
    if not wanted:
        return None

    by_day = complete_days_by_date(intervals, through=through, days=days)

    daily_minima: list[float] = []
    sampled = 0
    excluded = 0
    used_days: list[date] = []

    for day, rows in by_day.items():
        candidates = [row for row in rows if row.timestamp.hour in wanted]
        clean = []
        for row in candidates:
            row_end = row.timestamp + per_interval
            if any(w.overlaps(row.timestamp, row_end) for w in hvac_windows):
                excluded += 1
                continue
            clean.append(row)

        if not clean:
            continue
        sampled += len(clean)
        used_days.append(day)
        daily_minima.append(min(row.consumption for row in clean))

    if len(daily_minima) < min_days:
        return None

    # kWh in half an hour -> kW -> W.
    kwh_per_interval = median(daily_minima)
    watts = kwh_per_interval * (60 / INTERVAL_MINUTES) * 1000

    return BaselineLoad(
        watts=round(watts, 1),
        daily_kwh=round(kwh_per_interval * (60 / INTERVAL_MINUTES) * 24, 3),
        quiet_hours=tuple(sorted(wanted)),
        days=len(daily_minima),
        sampled_intervals=sampled,
        excluded_intervals=excluded,
        hvac_filtered=excluded > 0,
        first_day=min(used_days),
        last_day=max(used_days),
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
    "BASELINE_DAYS",
    "BASELINE_QUIET_HOURS",
    "COMPARISON_WEEKS",
    "HVAC_QUIET_ACTIONS",
    "HVAC_RUNNING_ACTIONS",
    "INTERVAL_MINUTES",
    "MIN_BASELINE_DAYS",
    "MIN_COMPARISON_DAYS",
    "MIN_PROFILE_DAYS",
    "PROFILE_DAYS",
    "UNUSUAL_DAY_THRESHOLD",
    "BaselineLoad",
    "DayComparison",
    "TimeWindow",
    "UsageProfile",
    "baseline_load",
    "compare_to_typical_day",
    "complete_days_by_date",
    "hour_label",
    "hvac_active_windows",
    "merge_windows",
    "quietest_hours",
    "usage_profile",
]
