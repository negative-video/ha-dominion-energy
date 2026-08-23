"""Tests for Green Button (ESPI) parsing and timestamp realignment.

The interesting behavior under test is not parsing but *calibration*.
Dominion's exports carry a constant timestamp offset that cannot be derived
from the file -- a real August export measured +5 hours against the utility's
own API readings, and a February one +4. An earlier version of this module
tried to model the offset from the export date and DST rules; it produced two
exports that agreed with each other perfectly while both sat five hours from
the truth. So these tests assert that the offset is *measured* against known-
good data and that a bad fit is refused, not that any particular model holds.

No Home Assistant import, so this runs in the lightweight CI job. Fixtures are
generated in-process: real exports embed an account number and a full hourly
record of household occupancy and must never enter the repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "dominion_energy"
)
NY = ZoneInfo("America/New_York")


def _load_green_button():
    """Import green_button.py directly; it has no Home Assistant dependency."""
    spec = importlib.util.spec_from_file_location(
        "_dominion_green_button", COMPONENT_DIR / "green_button.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gb = _load_green_button()


def build_export(
    readings: list[tuple[datetime, int]],
    *,
    export_offset_hours: int,
    exported_at: datetime | None = None,
    uom: int = 72,
    power_of_ten: int = 3,
    declared_interval: int | None = 900,
    flow_direction: int = 1,
    with_cost: bool = True,
) -> bytes:
    """Render an ESPI export the way Dominion does.

    ``readings`` are (local wall-clock, whole kWh). Each is converted to an
    epoch using a *single* fixed offset for the whole file -- reproducing the
    defect rather than the correct behavior, which is the entire point.
    """
    entries = []
    for wall, kwh in readings:
        naive = wall.replace(tzinfo=None)
        epoch = (
            int((naive - datetime(1970, 1, 1)).total_seconds())
            - export_offset_hours * 3600
        )
        cost = "<cost>0</cost>" if with_cost else ""
        entries.append(
            f"<IntervalReading><timePeriod><duration>3600</duration>"
            f"<start>{epoch}</start></timePeriod>"
            f"{cost}<value>{kwh}</value></IntervalReading>"
        )

    summary = ""
    if exported_at is not None:
        stamp = int(exported_at.timestamp())
        summary = (
            f"<UsageSummary><currentBillingPeriodOverAllConsumption>"
            f"<timeStamp>{stamp}</timeStamp><value>0</value>"
            f"</currentBillingPeriodOverAllConsumption></UsageSummary>"
        )

    interval_length = (
        f"<intervalLength>{declared_interval}</intervalLength>"
        if declared_interval is not None
        else ""
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<feed xmlns="http://naesb.org/espi">'
        f"<ReadingType>{interval_length}"
        f"<flowDirection>{flow_direction}</flowDirection>"
        f"<powerOfTenMultiplier>{power_of_ten}</powerOfTenMultiplier>"
        f"<uom>{uom}</uom></ReadingType>"
        f"{summary}"
        f"<IntervalBlock>{''.join(entries)}</IntervalBlock>"
        f"</feed>"
    ).encode()


def hourly_series(start_local: datetime, hours: int, base: int = 2):
    """Build a deterministic, *non-periodic* wall-clock series.

    Deterministic so tests are reproducible, but non-repeating on purpose: a
    series with a short period correlates perfectly at every multiple of that
    period, which makes any alignment assertion meaningless. A simple LCG plus
    a daily shape keeps it realistic without that degeneracy.
    """
    readings = []
    state = 12345
    for i in range(hours):
        state = (1103515245 * state + 12345) % 2147483648
        daily = (i % 24) // 6  # coarse morning/day/evening/night shape
        readings.append((start_local + timedelta(hours=i), base + daily + state % 5))
    return readings


class TestParsing:
    """Structural parsing of the ESPI document."""

    def test_reads_values_durations_and_flow(self):
        readings = hourly_series(datetime(2026, 7, 1, 0), 48)
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        assert len(export.readings) == 48
        assert export.duration_seconds == 3600
        assert export.flow_direction == 1
        assert export.has_nonzero_cost is False

    def test_readings_win_over_declared_interval_length(self):
        """Dominion declares intervalLength=900 while emitting 3600s readings."""
        readings = hourly_series(datetime(2026, 7, 1, 0), 5)
        export = gb.parse_export(
            build_export(readings, export_offset_hours=-4, declared_interval=900)
        )
        assert export.duration_seconds == 3600

    def test_wh_multiplier_converts_to_kwh(self):
        """uom=72 (Wh) with powerOfTenMultiplier=3 means a raw 1 is 1 kWh."""
        export = gb.parse_export(
            build_export(
                [(datetime(2026, 7, 1, 0), 7)],
                export_offset_hours=-4,
                uom=72,
                power_of_ten=3,
            )
        )
        assert export.readings[0][1] == pytest.approx(7.0)

    def test_native_kwh_unit_is_not_rescaled(self):
        export = gb.parse_export(
            build_export(
                [(datetime(2026, 7, 1, 0), 7)],
                export_offset_hours=-4,
                uom=719,
                power_of_ten=0,
            )
        )
        assert export.readings[0][1] == pytest.approx(7.0)

    def test_unknown_unit_is_rejected(self):
        with pytest.raises(gb.GreenButtonError, match="Unsupported unit"):
            gb.parse_export(
                build_export(
                    [(datetime(2026, 7, 1, 0), 1)],
                    export_offset_hours=-4,
                    uom=38,
                )
            )

    def test_malformed_xml_is_rejected(self):
        with pytest.raises(gb.GreenButtonError, match="Not valid XML"):
            gb.parse_export(b"<feed><unclosed>")

    def test_empty_export_is_rejected(self):
        with pytest.raises(gb.GreenButtonError, match="no interval readings"):
            gb.parse_export(build_export([], export_offset_hours=-4))

    def test_export_timestamp_is_read_from_nested_usage_summary(self):
        """timeStamp is a grandchild of UsageSummary, not a direct child."""
        stamp = datetime(2026, 8, 11, 22, tzinfo=UTC)
        export = gb.parse_export(
            build_export(
                hourly_series(datetime(2026, 8, 1, 0), 3),
                export_offset_hours=-4,
                exported_at=stamp,
            )
        )
        assert export.exported_at == stamp

    def test_missing_timestamp_falls_back_to_last_reading(self):
        readings = hourly_series(datetime(2026, 8, 1, 0), 6)
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        assert export.exported_at == export.last_start


class TestCalibration:
    """Measuring the offset, rather than assuming one."""

    @staticmethod
    def _series(readings, shift=0):
        return gb.to_hourly(gb.apply_shift(readings, shift))

    def test_measures_a_known_offset(self):
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        truth = gb.parse_export(build_export(wall, export_offset_hours=-4))
        reference = self._series(list(truth.readings))
        # An export whose timestamps sit 5 hours early, as Dominion's do.
        skewed = self._series(list(truth.readings), -5)
        best = gb.best_alignment(skewed, reference)
        assert best is not None
        assert best.shift_hours == 5
        assert best.correlation > 0.99
        assert best.is_convincing

    def test_finds_offsets_outside_a_dst_sized_range(self):
        """The real answer was +5h; a range built for DST would have missed it."""
        assert max(gb.CANDIDATE_SHIFTS_HOURS) >= 12
        assert min(gb.CANDIDATE_SHIFTS_HOURS) <= -12

    def test_correlation_separates_shifts_that_error_cannot(self):
        """Why the metric is correlation and not mean absolute error.

        Rounding Green Button to whole kWh leaves error nearly flat across
        shifts -- on real data it moved only between 1.24 and 1.57 kWh while
        correlation ranged 0.02 to 0.98.
        """
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        truth = gb.parse_export(build_export(wall, export_offset_hours=-4))
        reference = self._series(list(truth.readings))
        candidate = self._series(list(truth.readings), -3)
        right = gb.score_alignment(candidate, reference, 3)
        wrong = gb.score_alignment(candidate, reference, 2)
        assert right.correlation > wrong.correlation

    def test_aligned_series_scores_zero_shift(self):
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        export = gb.parse_export(build_export(wall, export_offset_hours=-4))
        series = self._series(list(export.readings))
        best = gb.best_alignment(series, series)
        assert best is not None
        assert best.shift_hours == 0
        assert best.correlation == pytest.approx(1.0)

    def test_insufficient_overlap_returns_none(self):
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        export = gb.parse_export(build_export(wall, export_offset_hours=-4))
        reference = self._series(list(export.readings))
        tiny = dict(list(reference.items())[:3])
        assert gb.best_alignment(tiny, reference) is None

    def test_no_overlap_at_all_returns_none(self):
        a = gb.parse_export(
            build_export(
                hourly_series(datetime(2026, 7, 1, 0), 240), export_offset_hours=-4
            )
        )
        b = gb.parse_export(
            build_export(
                hourly_series(datetime(2024, 7, 1, 0), 240), export_offset_hours=-4
            )
        )
        assert (
            gb.best_alignment(
                self._series(list(a.readings)), self._series(list(b.readings))
            )
            is None
        )

    def test_unrelated_series_is_not_convincing(self):
        """A best fit is not necessarily a good one."""
        import math

        base = datetime(2026, 7, 1, 0)
        a = {
            (base + timedelta(hours=i)).replace(tzinfo=NY).astimezone(UTC): float(
                2 + (i % 5)
            )
            for i in range(240)
        }
        b = {k: 3.0 + math.sin(i) * 0.01 for i, k in enumerate(sorted(a))}
        best = gb.best_alignment(a, b)
        assert best is not None
        assert not best.is_convincing

    def test_shift_preserves_total_and_ordering(self):
        wall = hourly_series(datetime(2026, 7, 1, 0), 48)
        export = gb.parse_export(build_export(wall, export_offset_hours=-4))
        shifted = gb.apply_shift(list(export.readings), 5)
        assert sum(v for _, v in shifted) == pytest.approx(export.total_kwh)
        assert [t for t, _ in shifted] == sorted(t for t, _ in shifted)


class TestIncompleteTail:
    """Dominion pads the export to the moment of download with zeros."""

    def test_trailing_zero_padding_is_dropped(self):
        readings = [(datetime(2026, 8, 9, h), 3) for h in range(24)]
        readings += [(datetime(2026, 8, 10, h), 3) for h in range(20)]
        readings += [(datetime(2026, 8, 10, h), 0) for h in range(20, 24)]
        readings += [(datetime(2026, 8, 11, h), 0) for h in range(19)]

        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        trimmed = gb.drop_incomplete_tail(list(export.readings))

        days = {dt.astimezone(NY).date() for dt, _ in trimmed}
        assert max(days).isoformat() == "2026-08-09"

    def test_complete_final_day_is_kept(self):
        readings = [(datetime(2026, 8, 9, h), 3) for h in range(24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        trimmed = gb.drop_incomplete_tail(list(export.readings))
        assert len({dt.astimezone(NY).date() for dt, _ in trimmed}) == 1

    def test_interior_zero_day_is_not_dropped(self):
        """A genuinely idle day mid-series is real data, not padding."""
        readings = [(datetime(2026, 8, 8, h), 3) for h in range(24)]
        readings += [(datetime(2026, 8, 9, h), 0) for h in range(24)]
        readings += [(datetime(2026, 8, 10, h), 3) for h in range(24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        trimmed = gb.drop_incomplete_tail(list(export.readings))
        days = {dt.astimezone(NY).date() for dt, _ in trimmed}
        assert len(days) == 3

    def test_all_zero_export_yields_nothing(self):
        readings = [(datetime(2026, 8, 9, h), 0) for h in range(24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        assert gb.drop_incomplete_tail(list(export.readings)) == []


class TestMerge:
    """API data must win any hour the two sources share."""

    def test_preferred_series_overrides_shared_hours(self):
        hour = datetime(2026, 7, 1, 12, tzinfo=UTC)
        merged = gb.merge_preferring({hour: 2.75}, {hour: 3.0})
        assert merged[hour] == 2.75

    def test_fallback_supplies_hours_the_preferred_series_lacks(self):
        a = datetime(2026, 7, 1, 12, tzinfo=UTC)
        b = datetime(2026, 1, 1, 12, tzinfo=UTC)
        merged = gb.merge_preferring({a: 2.75}, {b: 3.0})
        assert merged == {a: 2.75, b: 3.0}

    def test_merge_does_not_mutate_inputs(self):
        a = datetime(2026, 7, 1, 12, tzinfo=UTC)
        preferred = {a: 1.0}
        fallback = {a: 2.0}
        gb.merge_preferring(preferred, fallback)
        assert preferred == {a: 1.0}
        assert fallback == {a: 2.0}


class TestPathGuidance:
    """The message shown when Home Assistant refuses to read a file.

    Two traps account for essentially every failed attempt, and both are
    invisible in a file browser: add-ons mount the config directory as
    /homeassistant while Core sees /config, and Home Assistant OS has a
    top-level /media that is a completely different place from a `media`
    folder inside the config directory. A bare list of allowed directories
    helps with neither, so the message names them explicitly.
    """

    ALLOWED = ["/media", "/config/www"]

    def test_always_lists_the_allowed_directories(self):
        message = gb.describe_path_problem("/tmp/x.xml", self.ALLOWED)
        assert "/media" in message
        assert "/config/www" in message

    def test_addon_path_is_translated_to_the_core_path(self):
        message = gb.describe_path_problem(
            "/homeassistant/greenbutton/export.xml", self.ALLOWED
        )
        assert "/config/greenbutton/export.xml" in message
        assert "add-on" in message.lower()

    def test_config_media_collision_is_called_out(self):
        message = gb.describe_path_problem(
            "/config/media/greenbutton/export.xml", self.ALLOWED
        )
        assert "/config/media and /media are different directories" in message

    def test_collision_is_caught_through_the_addon_path_too(self):
        """The way it actually presents: an add-on path into config/media."""
        message = gb.describe_path_problem(
            "/homeassistant/media/greenbutton/export.xml", self.ALLOWED
        )
        assert "/config/media and /media are different directories" in message

    def test_real_media_path_does_not_trigger_the_collision_note(self):
        message = gb.describe_path_problem("/media/greenbutton/x.xml", self.ALLOWED)
        assert "different directories" not in message

    def test_warns_that_www_is_publicly_served(self):
        message = gb.describe_path_problem("/tmp/x.xml", self.ALLOWED)
        assert "/local/" in message
        assert "no authentication" in message

    def test_empty_allowlist_does_not_crash(self):
        message = gb.describe_path_problem("/tmp/x.xml", [])
        assert "(none)" in message


class TestHardening:
    """Guards against inputs that would otherwise write plausible-looking junk."""

    def test_doctype_is_refused(self):
        """Entity expansion is the classic way to kill an XML parser."""
        payload = (
            b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "aaaaaaaaaa">]>'
            b'<feed xmlns="http://naesb.org/espi"></feed>'
        )
        with pytest.raises(gb.GreenButtonError, match="DOCTYPE or ENTITY"):
            gb.parse_export(payload)

    def test_entity_declaration_is_refused(self):
        payload = b'<!ENTITY x "y"><feed xmlns="http://naesb.org/espi"></feed>'
        with pytest.raises(gb.GreenButtonError, match="DOCTYPE or ENTITY"):
            gb.parse_export(payload)

    def test_ordinary_export_still_parses(self):
        """The guard must not reject legitimate files."""
        export = gb.parse_export(
            build_export(
                hourly_series(datetime(2026, 7, 1, 0), 24), export_offset_hours=-4
            )
        )
        assert len(export.readings) == 24

    def test_reverse_flow_direction_is_reported(self):
        """A generation export imported as consumption would double usage."""
        export = gb.parse_export(
            build_export(
                hourly_series(datetime(2026, 7, 1, 0), 24),
                export_offset_hours=-4,
                flow_direction=19,
            )
        )
        assert export.flow_direction == 19
        assert export.flow_direction != gb.FLOW_DIRECTION_DELIVERED

    def test_scale_error_is_caught_despite_perfect_correlation(self):
        """Correlation is scale-invariant, so magnitude needs its own check.

        A misread powerOfTenMultiplier produces a series that correlates
        perfectly while being a thousand times too large.
        """
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        export = gb.parse_export(build_export(wall, export_offset_hours=-4))
        good = gb.to_hourly(list(export.readings))
        thousand_x = {k: v * 1000 for k, v in good.items()}

        best = gb.best_alignment(thousand_x, good)
        assert best is not None
        assert best.correlation == pytest.approx(1.0), "correlation cannot see scale"
        assert gb.magnitude_looks_wrong(thousand_x, good)

    def test_matching_magnitude_passes(self):
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        export = gb.parse_export(build_export(wall, export_offset_hours=-4))
        good = gb.to_hourly(list(export.readings))
        assert not gb.magnitude_looks_wrong(good, good)

    def test_ordinary_variation_is_tolerated(self):
        """Seasonal difference between an export and the reference is normal."""
        wall = hourly_series(datetime(2026, 7, 1, 0), 24 * 10)
        export = gb.parse_export(build_export(wall, export_offset_hours=-4))
        good = gb.to_hourly(list(export.readings))
        somewhat_higher = {k: v * 1.8 for k, v in good.items()}
        assert not gb.magnitude_looks_wrong(somewhat_higher, good)

    def test_empty_series_is_not_flagged_as_wrong_magnitude(self):
        assert not gb.magnitude_looks_wrong({}, {})

    def test_trim_runs_in_the_corrected_frame(self):
        """Completeness depends on the local hour, so it must follow the shift.

        Trimming before the offset is applied judges each day against the wrong
        hour-of-day, and moves the boundary by the size of the offset.
        """
        readings = [(datetime(2026, 8, 9, h), 3) for h in range(24)]
        readings += [(datetime(2026, 8, 10, h), 3) for h in range(20)]
        readings += [(datetime(2026, 8, 10, h), 0) for h in range(20, 24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        raw = list(export.readings)

        wrong_order = gb.apply_shift(gb.drop_incomplete_tail(raw), 5)
        right_order = gb.drop_incomplete_tail(gb.apply_shift(raw, 5))

        last_wrong = max(t for t, _ in wrong_order).astimezone(NY)
        last_right = max(t for t, _ in right_order).astimezone(NY)
        assert last_wrong != last_right
        # Corrected frame: the last complete day ends at its own 23:00 local.
        assert last_right.hour == 23
