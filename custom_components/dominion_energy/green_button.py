"""Parsing and alignment for Green Button (ESPI) exports.

Dominion's portal API only serves roughly the last 68 days of interval data --
it ignores the requested date range and returns a fixed recent window. The
Green Button download in the billing profile is a different data path with a
rolling ~13 month window, so importing one extends Energy Dashboard history
well past what the API alone can reach.

Two properties of Dominion's export shape everything here:

**The timestamps are not trustworthy as recorded.** Comparing two exports of
the same account taken in different DST regimes, every reading in the later
(EDT) export is stamped exactly one hour earlier than the same reading in the
earlier (EST) one -- a 99.5% match under a one hour shift. The evidence is
consistent with a single UTC offset, whichever was in effect when the customer
clicked export, being applied to all thirteen months. So roughly half of any
given export is off by an hour. :func:`realign_to_local` undoes that by
recovering the intended wall-clock reading and re-localising it with real DST
rules; :func:`best_hour_shift` verifies the result against known-good data
rather than trusting the reconstruction blindly.

**The values are coarse.** Readings are whole kWh (``uom`` 72 with
``powerOfTenMultiplier`` 3), against two decimal places of 30-minute data from
the API. Where the two overlap the API wins; Green Button only fills in what
the API cannot reach.

This module imports no Home Assistant, so all of the above is unit testable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

ESPI = "{http://naesb.org/espi}"

# ESPI unit-of-measure codes we know how to convert to kWh.
UOM_WH = 72
UOM_KWH = 719

# Dominion's electric service is billed on Eastern time regardless of where a
# user's Home Assistant thinks it is.
DOMINION_TZ = ZoneInfo("America/New_York")

# A day is treated as fully published only if it reaches this local hour. The
# export pads out to the moment of download with zero-valued readings, so the
# final day is routinely partial and must not be written as real data.
MIN_COMPLETE_DAY_LAST_HOUR = 22

# Candidate whole-hour corrections tried when validating against reference
# data. Dominion's error is a DST offset, so +/- 2 hours is ample.
CANDIDATE_SHIFTS_HOURS = (-2, -1, 0, 1, 2)


class GreenButtonError(Exception):
    """Raised when an export cannot be parsed or trusted."""


@dataclass(frozen=True)
class GreenButtonExport:
    """A parsed Green Button export.

    Attributes:
        readings: ``(start, kwh)`` pairs. ``start`` is the instant exactly as
            recorded in the file -- not yet corrected. Sorted, unique.
        duration_seconds: Interval length actually observed in the readings.
            Note this can disagree with ``ReadingType/intervalLength``;
            Dominion's exports have been seen declaring 900 while emitting
            3600. The readings win.
        exported_at: When the export was generated, if the file says.
        flow_direction: ESPI flow direction; 1 is delivered (consumption).
        has_nonzero_cost: Whether any reading carried a non-zero cost.
            Dominion emits a ``cost`` element on every reading and leaves it
            zero, so cost cannot be taken from the file.
    """

    readings: tuple[tuple[datetime, float], ...]
    duration_seconds: int
    exported_at: datetime | None
    flow_direction: int | None
    has_nonzero_cost: bool

    @property
    def first_start(self) -> datetime | None:
        """Return the earliest recorded interval start."""
        return self.readings[0][0] if self.readings else None

    @property
    def last_start(self) -> datetime | None:
        """Return the latest recorded interval start."""
        return self.readings[-1][0] if self.readings else None

    @property
    def total_kwh(self) -> float:
        """Return the sum of every reading."""
        return sum(kwh for _, kwh in self.readings)


def _text(node: ElementTree.Element | None, tag: str) -> str | None:
    """Return the text of a child element, or None."""
    if node is None:
        return None
    child = node.find(f"{ESPI}{tag}")
    return None if child is None or child.text is None else child.text.strip()


def _int(node: ElementTree.Element | None, tag: str) -> int | None:
    """Return a child element's text as an int, or None."""
    raw = _text(node, tag)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _to_kwh(value: int, uom: int, power_of_ten: int) -> float:
    """Convert a raw ESPI reading to kWh.

    ``value * 10 ** power_of_ten`` yields the quantity in ``uom``'s base unit.
    """
    scaled = value * (10.0**power_of_ten)
    if uom == UOM_WH:
        return scaled / 1000.0
    if uom == UOM_KWH:
        return scaled
    raise GreenButtonError(
        f"Unsupported unit of measure {uom}; expected Wh ({UOM_WH}) or kWh ({UOM_KWH})"
    )


def parse_export(data: bytes | str) -> GreenButtonExport:
    """Parse a Green Button XML export.

    Args:
        data: Raw XML.

    Returns:
        The parsed export, with timestamps exactly as recorded.

    Raises:
        GreenButtonError: If the XML is malformed, carries no readings, or
            uses a unit of measure we cannot convert.
    """
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as err:
        raise GreenButtonError(f"Not valid XML: {err}") from err

    reading_type = root.find(f".//{ESPI}ReadingType")
    uom = _int(reading_type, "uom")
    power_of_ten = _int(reading_type, "powerOfTenMultiplier")
    flow_direction = _int(reading_type, "flowDirection")
    if uom is None or power_of_ten is None:
        raise GreenButtonError(
            "Export has no ReadingType with uom and powerOfTenMultiplier; "
            "this does not look like an ESPI Green Button file"
        )

    # The export timestamp tells us which UTC offset was in effect when the
    # file was generated, which is what realign_to_local needs. UsageSummary
    # nests timeStamp inside its reading-quality children rather than exposing
    # it as a direct child, so search descendants.
    exported_at: datetime | None = None
    summary = root.find(f".//{ESPI}UsageSummary")
    if summary is not None:
        stamps = [
            int(node.text)
            for node in summary.iter(f"{ESPI}timeStamp")
            if node.text and node.text.strip().isdigit()
        ]
        if stamps:
            exported_at = datetime.fromtimestamp(max(stamps), tz=UTC)

    by_start: dict[datetime, float] = {}
    durations: dict[int, int] = defaultdict(int)
    has_nonzero_cost = False

    for reading in root.iter(f"{ESPI}IntervalReading"):
        period = reading.find(f"{ESPI}timePeriod")
        start = _int(period, "start")
        duration = _int(period, "duration")
        value = _int(reading, "value")
        if start is None or value is None:
            continue
        cost = _int(reading, "cost")
        if cost:
            has_nonzero_cost = True
        if duration:
            durations[duration] += 1

        when = datetime.fromtimestamp(start, tz=UTC)
        # Duplicate starts would silently overwrite. They should not occur, but
        # a DST fall-back handled badly upstream could produce them, so add
        # rather than replace: the day's total stays right either way.
        by_start[when] = by_start.get(when, 0.0) + _to_kwh(value, uom, power_of_ten)

    if not by_start:
        raise GreenButtonError("Export contains no interval readings")

    duration_seconds = max(durations, key=lambda d: durations[d]) if durations else 3600
    declared = _int(reading_type, "intervalLength")
    if declared is not None and declared != duration_seconds:
        # Seen in the wild: ReadingType says 900, every reading says 3600.
        _LOGGER.debug(
            "Green Button ReadingType declares intervalLength=%ss but readings "
            "are %ss; trusting the readings",
            declared,
            duration_seconds,
        )

    readings = tuple(sorted(by_start.items()))
    if exported_at is None and readings:
        # Dominion pads the export out to the moment of download, so the last
        # reading dates it. Only the DST regime matters here, and an hour of
        # error cannot change which side of a transition a date falls on.
        exported_at = readings[-1][0]
    return GreenButtonExport(
        readings=readings,
        duration_seconds=duration_seconds,
        exported_at=exported_at,
        flow_direction=flow_direction,
        has_nonzero_cost=has_nonzero_cost,
    )


def export_offset_hours(
    exported_at: datetime | None, tz: ZoneInfo = DOMINION_TZ
) -> int:
    """Return the UTC offset, in hours, in effect when the export was made.

    This is the offset Dominion appears to have applied to every reading in the
    file regardless of the reading's own date. Defaults to standard time when
    the export carries no timestamp, which is the conservative choice: it is
    the offset in effect for the larger part of the year.
    """
    if exported_at is None:
        # No export timestamp: assume standard time, which is in effect for the
        # larger part of the year. best_hour_shift is what actually catches a
        # wrong guess, so this only decides which way we lean first.
        offset = datetime(2000, 1, 15, tzinfo=tz).utcoffset()
    else:
        offset = exported_at.astimezone(tz).utcoffset()
    if offset is None:  # pragma: no cover - ZoneInfo always supplies one
        return -5
    return int(offset.total_seconds() // 3600)


def realign_to_local(
    readings: tuple[tuple[datetime, float], ...],
    offset_hours: int,
    tz: ZoneInfo = DOMINION_TZ,
) -> list[tuple[datetime, float]]:
    """Correct timestamps that were built with a single fixed UTC offset.

    Recovers the wall-clock time Dominion meant -- ``recorded + offset`` -- and
    re-localises it with real DST rules. Readings whose date falls in the same
    DST regime as the export come back unchanged; the rest move by an hour,
    which is precisely the discrepancy observed between two exports of the same
    account taken in different seasons.

    Non-existent local times (the skipped hour at spring forward) are dropped:
    they denote no real instant, and Dominion's own export simply omits that
    hour. Ambiguous times (the repeated hour at fall back) resolve to the first
    occurrence, since the export carries only one reading for it.

    Args:
        readings: ``(start, kwh)`` pairs exactly as recorded.
        offset_hours: The offset the file was built with, from
            :func:`export_offset_hours`.
        tz: The utility's timezone.

    Returns:
        ``(start, kwh)`` pairs in true UTC, sorted, with duplicates summed.
    """
    corrected: dict[datetime, float] = {}
    dropped = 0

    for recorded, kwh in readings:
        wall = (recorded + timedelta(hours=offset_hours)).replace(tzinfo=None)
        localised = wall.replace(tzinfo=tz)
        # A local time inside the spring-forward gap does not round-trip.
        if localised.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != wall:
            dropped += 1
            continue
        as_utc = localised.astimezone(UTC)
        corrected[as_utc] = corrected.get(as_utc, 0.0) + kwh

    if dropped:
        _LOGGER.debug(
            "Dropped %d Green Button reading(s) falling in a DST gap", dropped
        )
    return sorted(corrected.items())


def to_hourly(readings: list[tuple[datetime, float]]) -> dict[datetime, float]:
    """Bucket readings into whole UTC hours.

    Green Button is already hourly, but API data is half-hourly, and both have
    to land on the same grid before they can be compared or merged.
    """
    hourly: dict[datetime, float] = defaultdict(float)
    for start, kwh in readings:
        hourly[start.replace(minute=0, second=0, microsecond=0)] += kwh
    return dict(hourly)


@dataclass(frozen=True)
class ShiftScore:
    """How well a candidate hour shift lines a series up with a reference."""

    shift_hours: int
    overlapping_hours: int
    mean_absolute_error_kwh: float

    @property
    def is_usable(self) -> bool:
        """Whether enough hours overlapped for the score to mean anything."""
        return self.overlapping_hours >= 24


def score_shift(
    candidate: dict[datetime, float],
    reference: dict[datetime, float],
    shift_hours: int,
) -> ShiftScore:
    """Score one candidate shift against reference data."""
    delta = timedelta(hours=shift_hours)
    errors: list[float] = []
    for hour, value in candidate.items():
        ref = reference.get(hour + delta)
        if ref is not None:
            errors.append(abs(value - ref))
    if not errors:
        return ShiftScore(shift_hours, 0, float("inf"))
    return ShiftScore(shift_hours, len(errors), sum(errors) / len(errors))


def best_hour_shift(
    candidate: dict[datetime, float],
    reference: dict[datetime, float],
    shifts: tuple[int, ...] = CANDIDATE_SHIFTS_HOURS,
) -> ShiftScore | None:
    """Find the whole-hour shift that best matches ``reference``.

    Used to verify :func:`realign_to_local` against data known to be correct --
    the API's own 30-minute readings for the window the two sources share. A
    result of 0 confirms the reconstruction; anything else means the assumption
    about Dominion's offset is wrong for this file and the import should stop
    rather than write history that is silently an hour out.

    Returns:
        The best-scoring shift, or None if nothing overlapped enough to judge.
    """
    scored = [score_shift(candidate, reference, s) for s in shifts]
    usable = [s for s in scored if s.is_usable]
    if not usable:
        return None
    return min(usable, key=lambda s: s.mean_absolute_error_kwh)


def drop_incomplete_tail(
    readings: list[tuple[datetime, float]],
    tz: ZoneInfo = DOMINION_TZ,
    min_last_hour: int = MIN_COMPLETE_DAY_LAST_HOUR,
) -> list[tuple[datetime, float]]:
    """Drop trailing days the export only partially covers.

    Dominion pads the export with zero-valued readings out to the moment of
    download, so the last day -- often the last two -- is not real data. Taken
    at face value it writes a near-zero day into history. Only the tail is
    trimmed; an interior day that genuinely used nothing is left alone.
    """
    if not readings:
        return []

    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for start, kwh in readings:
        by_day[start.astimezone(tz).date()].append((start, kwh))

    keep_through: date | None = None
    for day in sorted(by_day, reverse=True):
        # The padding is zero-valued but still carries timestamps, so the last
        # hour *present* reaches 23:00 even on a day that stops being real at
        # 19:00. Completeness has to be judged on the last non-zero reading.
        nonzero_hours = [s.astimezone(tz).hour for s, kwh in by_day[day] if kwh > 0]
        if nonzero_hours and max(nonzero_hours) >= min_last_hour:
            keep_through = day
            break

    if keep_through is None:
        return []
    return [r for r in readings if r[0].astimezone(tz).date() <= keep_through]


def describe_path_problem(path: str, allowed_dirs: Iterable[str]) -> str:
    """Explain why Home Assistant refused to read ``path``.

    Lives here rather than in the service handler so it can be unit tested
    without Home Assistant.

    Two traps account for essentially every failed attempt, and a bare list of
    allowed directories helps with neither:

    - **Add-on paths.** File Editor, Samba and Terminal mount the config
      directory as ``/homeassistant``. Services run in Core, which knows the
      same directory as ``/config``.
    - **Two directories named "media".** Home Assistant OS provides ``/media``
      as its own top-level mount, and that is what the allowlist contains. A
      ``media`` folder *inside* the config directory is a different place
      entirely, is not on the allowlist, and looks identical in a file browser.
    """
    allowed = sorted(allowed_dirs)
    lines = [
        f"Path {path} is not allowed.",
        f"Home Assistant can only read from: {', '.join(allowed) or '(none)'}.",
    ]

    if path.startswith("/homeassistant/"):
        core_path = path.replace("/homeassistant/", "/config/", 1)
        lines.append(
            "That looks like an add-on path: File Editor, Samba and Terminal "
            "show the config directory as /homeassistant, but this service "
            f"runs in Home Assistant Core, where it is /config. Try {core_path} "
            "-- though note the config directory is not readable by default "
            "either; see below."
        )

    normalised = path.replace("/homeassistant/", "/config/", 1)
    if normalised.startswith("/config/media/"):
        lines.append(
            "Careful: /config/media and /media are different directories. Only "
            "the top-level /media is on the allowlist -- it is its own mount, a "
            "sibling of /config, and in Samba it is a separate share rather "
            "than a folder inside config. A 'media' folder inside the config "
            "directory is not the same place and is not readable."
        )

    lines.append(
        "Either move the file into one of the allowed directories (/media "
        "keeps it private; anything under www is served publicly at /local/ "
        "with no authentication, and an export contains your account number "
        "and an hourly record of when your home is occupied), or add its "
        "directory to allowlist_external_dirs in configuration.yaml and "
        "restart."
    )
    return " ".join(lines)


def merge_preferring(
    preferred: dict[datetime, float],
    fallback: dict[datetime, float],
) -> dict[datetime, float]:
    """Merge two hourly series, letting ``preferred`` win any shared hour.

    API data is half-hourly at two decimal places; Green Button is hourly
    integers. Where both cover an hour the API reading is strictly better, so
    Green Button only supplies hours the API never had.
    """
    merged = dict(fallback)
    merged.update(preferred)
    return merged
