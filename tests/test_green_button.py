"""Tests for Green Button (ESPI) parsing and timestamp realignment.

The interesting behaviour under test is not parsing but *correction*. Dominion
stamps every reading in an export with whichever UTC offset was in effect when
the file was generated, rather than the offset that applied to each reading, so
about half of any export is an hour out. The fixtures below reproduce that
defect synthetically -- the same underlying usage exported twice, once in
standard time and once in daylight time -- and assert that after realignment
the two agree, which is exactly the property that makes an import safe.

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
    defect rather than the correct behaviour, which is the entire point.
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
    """Build a deterministic wall-clock series with a daily shape."""
    return [(start_local + timedelta(hours=i), base + (i % 5)) for i in range(hours)]


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


class TestExportOffsetDetection:
    """Which UTC offset the file was built with."""

    def test_summer_export_is_daylight_time(self):
        assert gb.export_offset_hours(datetime(2026, 8, 11, 22, tzinfo=UTC)) == -4

    def test_winter_export_is_standard_time(self):
        assert gb.export_offset_hours(datetime(2026, 2, 5, 22, tzinfo=UTC)) == -5

    def test_unknown_export_time_assumes_standard(self):
        assert gb.export_offset_hours(None) == -5


class TestRealignment:
    """The defect this module exists to correct."""

    def test_two_exports_of_the_same_usage_agree_after_realignment(self):
        """The decisive property.

        The same wall-clock usage, exported once in EST and once in EDT, is
        stamped an hour apart by Dominion. Raw, the two disagree; realigned,
        they must match exactly. This mirrors the real-world observation of
        54.6% raw agreement rising to 99.9% after correction.
        """
        readings = hourly_series(datetime(2025, 12, 1, 0), 24 * 40)

        winter_file = gb.parse_export(
            build_export(
                readings,
                export_offset_hours=-5,
                exported_at=datetime(2026, 2, 5, 22, tzinfo=UTC),
            )
        )
        summer_file = gb.parse_export(
            build_export(
                readings,
                export_offset_hours=-4,
                exported_at=datetime(2026, 8, 11, 22, tzinfo=UTC),
            )
        )

        raw_winter = gb.to_hourly(list(winter_file.readings))
        raw_summer = gb.to_hourly(list(summer_file.readings))
        shared_raw = set(raw_winter) & set(raw_summer)
        assert shared_raw, "fixtures must overlap for the test to mean anything"
        raw_matches = sum(1 for h in shared_raw if raw_winter[h] == raw_summer[h])
        assert raw_matches < len(shared_raw), (
            "fixture does not reproduce the defect: raw exports already agree"
        )

        fixed_winter = gb.to_hourly(
            gb.realign_to_local(
                winter_file.readings, gb.export_offset_hours(winter_file.exported_at)
            )
        )
        fixed_summer = gb.to_hourly(
            gb.realign_to_local(
                summer_file.readings, gb.export_offset_hours(summer_file.exported_at)
            )
        )
        assert fixed_winter == fixed_summer

    def test_realigned_hours_match_intended_wall_clock(self):
        """A reading meant for 18:00 local must land on 18:00 local."""
        wall = datetime(2026, 1, 15, 18)
        export = gb.parse_export(
            build_export(
                [(wall, 3)],
                export_offset_hours=-4,  # exported in summer, reading in winter
                exported_at=datetime(2026, 8, 11, 22, tzinfo=UTC),
            )
        )
        ((recorded, _),) = export.readings
        assert recorded.astimezone(NY).hour != 18, "fixture should be an hour out"

        fixed = gb.realign_to_local(export.readings, -4)
        assert fixed[0][0].astimezone(NY).hour == 18

    def test_spring_forward_gap_readings_are_dropped(self):
        """02:00 on a spring-forward date is not a real instant."""
        readings = [
            (datetime(2026, 3, 8, 1), 1),
            (datetime(2026, 3, 8, 2), 9),  # does not exist locally
            (datetime(2026, 3, 8, 3), 1),
        ]
        export = gb.parse_export(build_export(readings, export_offset_hours=-5))
        fixed = gb.realign_to_local(export.readings, -5)
        local_hours = {dt.astimezone(NY).hour for dt, _ in fixed}
        assert 2 not in local_hours
        assert len(fixed) == 2

    def test_total_is_preserved_when_no_gap_is_involved(self):
        readings = hourly_series(datetime(2026, 6, 1, 0), 72)
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        fixed = gb.realign_to_local(export.readings, -4)
        assert sum(v for _, v in fixed) == pytest.approx(export.total_kwh)


class TestShiftScoring:
    """Verification against known-good reference data."""

    @staticmethod
    def _series(start_local: datetime, hours: int):
        return {
            (start_local + timedelta(hours=i))
            .replace(tzinfo=NY)
            .astimezone(UTC): float(2 + (i % 5))
            for i in range(hours)
        }

    def test_perfectly_aligned_series_scores_zero_shift(self):
        ref = self._series(datetime(2026, 7, 1, 0), 96)
        best = gb.best_hour_shift(dict(ref), ref)
        assert best is not None
        assert best.shift_hours == 0
        assert best.mean_absolute_error_kwh == pytest.approx(0.0)

    def test_one_hour_skew_is_detected(self):
        ref = self._series(datetime(2026, 7, 1, 0), 96)
        skewed = {k + timedelta(hours=1): v for k, v in ref.items()}
        best = gb.best_hour_shift(skewed, ref)
        assert best is not None
        assert best.shift_hours == -1

    def test_insufficient_overlap_returns_none(self):
        ref = self._series(datetime(2026, 7, 1, 0), 96)
        tiny = self._series(datetime(2026, 7, 1, 0), 3)
        assert gb.best_hour_shift(tiny, ref) is None

    def test_no_overlap_at_all_returns_none(self):
        ref = self._series(datetime(2026, 7, 1, 0), 96)
        elsewhere = self._series(datetime(2025, 1, 1, 0), 96)
        assert gb.best_hour_shift(elsewhere, ref) is None


class TestIncompleteTail:
    """Dominion pads the export to the moment of download with zeros."""

    def test_trailing_zero_padding_is_dropped(self):
        readings = [(datetime(2026, 8, 9, h), 3) for h in range(24)]
        readings += [(datetime(2026, 8, 10, h), 3) for h in range(20)]
        readings += [(datetime(2026, 8, 10, h), 0) for h in range(20, 24)]
        readings += [(datetime(2026, 8, 11, h), 0) for h in range(19)]

        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        fixed = gb.realign_to_local(export.readings, -4)
        trimmed = gb.drop_incomplete_tail(fixed)

        days = {dt.astimezone(NY).date() for dt, _ in trimmed}
        assert max(days).isoformat() == "2026-08-09"

    def test_complete_final_day_is_kept(self):
        readings = [(datetime(2026, 8, 9, h), 3) for h in range(24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        trimmed = gb.drop_incomplete_tail(gb.realign_to_local(export.readings, -4))
        assert len({dt.astimezone(NY).date() for dt, _ in trimmed}) == 1

    def test_interior_zero_day_is_not_dropped(self):
        """A genuinely idle day mid-series is real data, not padding."""
        readings = [(datetime(2026, 8, 8, h), 3) for h in range(24)]
        readings += [(datetime(2026, 8, 9, h), 0) for h in range(24)]
        readings += [(datetime(2026, 8, 10, h), 3) for h in range(24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        trimmed = gb.drop_incomplete_tail(gb.realign_to_local(export.readings, -4))
        days = {dt.astimezone(NY).date() for dt, _ in trimmed}
        assert len(days) == 3

    def test_all_zero_export_yields_nothing(self):
        readings = [(datetime(2026, 8, 9, h), 0) for h in range(24)]
        export = gb.parse_export(build_export(readings, export_offset_hours=-4))
        assert gb.drop_incomplete_tail(gb.realign_to_local(export.readings, -4)) == []


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
