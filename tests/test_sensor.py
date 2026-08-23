"""Tests for the sensor platform.

``sensor.py`` imports Home Assistant, which is not installed in the lightweight
CI job, so this module never imports it. Following the pattern already used by
``tests/test_translations.py``, the platform is inspected with ``ast`` instead:
the sensor descriptions, the unique-ID scheme and the generation gating are all
declarative enough to be read straight out of the source.

The checks here are about contracts that are easy to break silently and
impossible to notice without a live Home Assistant:

* a ``translation_key`` with no matching entry in the translation files (and
  the reverse) - the entity would fall back to an unnamed state,
* a ``device_class``/``state_class`` pair Home Assistant rejects.

The unique-ID and device-identifier schemes are shared by every platform and
are guarded in ``tests/test_entity.py``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from tests.astkit import describe, function, tuple_of_calls
from tests.conftest import COMPONENT_DIR

SENSOR_PATH = COMPONENT_DIR / "sensor.py"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATIONS_PATH = COMPONENT_DIR / "translations" / "en.json"

DESCRIPTION_CLASS = "DominionEnergySensorDescription"

# Subset of homeassistant.components.sensor.const.DEVICE_CLASS_STATE_CLASSES
# covering the device classes this platform uses. Kept here rather than
# imported so the check also runs without Home Assistant installed.
ALLOWED_STATE_CLASSES: dict[str, set[str]] = {
    "SensorDeviceClass.ENERGY": {
        "SensorStateClass.TOTAL",
        "SensorStateClass.TOTAL_INCREASING",
    },
    "SensorDeviceClass.MONETARY": {"SensorStateClass.TOTAL"},
    "SensorDeviceClass.POWER": {"SensorStateClass.MEASUREMENT"},
    "SensorDeviceClass.DATE": set(),
    "SensorDeviceClass.TIMESTAMP": set(),
}


def _tuple_of_descriptions(name: str) -> list[ast.Call]:
    """Return the description calls making up a module-level tuple."""
    return tuple_of_calls(SENSOR_PATH, name, DESCRIPTION_CLASS)


def _describe(call: ast.Call) -> dict[str, Any]:
    """Return a description call's keyword arguments as plain values."""
    return describe(call)


def _function(name: str, class_name: str | None = None) -> ast.AST:
    """Return a module-level or method function definition by name."""
    return function(SENSOR_PATH, name, class_name)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sensor_names(path: Path) -> dict[str, str]:
    return {
        key: value["name"]
        for key, value in _load_json(path)["entity"]["sensor"].items()
    }


SENSORS = [_describe(call) for call in _tuple_of_descriptions("SENSORS")]
BUDGET_SENSORS = [_describe(call) for call in _tuple_of_descriptions("BUDGET_SENSORS")]
GENERATION_SENSORS = [
    _describe(call) for call in _tuple_of_descriptions("GENERATION_SENSORS")
]
ALL_SENSORS = SENSORS + BUDGET_SENSORS + GENERATION_SENSORS
ALL_KEYS = [sensor["key"] for sensor in ALL_SENSORS]


def test_ast_extraction_found_something() -> None:
    """Guard against the AST helpers silently returning nothing."""
    assert len(SENSORS) >= 12
    assert len(GENERATION_SENSORS) == 3
    assert all("key" in sensor for sensor in ALL_SENSORS)


class TestTranslationKeys:
    """The entity.sensor block is inert unless translation_key is wired up."""

    def test_every_description_sets_a_translation_key(self) -> None:
        missing = [s["key"] for s in ALL_SENSORS if not s.get("translation_key")]
        assert not missing, (
            f"sensors without translation_key: {missing}. Without it Home "
            "Assistant never reads entity.sensor.* and the entity ends up "
            "named after its device only."
        )

    def test_translation_key_matches_the_description_key(self) -> None:
        """Keeping them equal is what makes the JSON files greppable."""
        mismatched = {
            s["key"]: s["translation_key"]
            for s in ALL_SENSORS
            if s["translation_key"] != s["key"]
        }
        assert not mismatched

    def test_no_description_hardcodes_a_name(self) -> None:
        """A literal ``name=`` shadows the translated name and defeats i18n."""
        named = [s["key"] for s in ALL_SENSORS if "name" in s]
        assert not named, (
            f"sensors still setting name=: {named}. Move the string into "
            "strings.json and translations/en.json instead."
        )

    def test_keys_are_unique(self) -> None:
        duplicates = {key for key in ALL_KEYS if ALL_KEYS.count(key) > 1}
        assert not duplicates, (
            f"duplicate sensor keys collide on unique_id: {duplicates}"
        )

    @pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
    def test_every_translation_key_is_translated(self, path: Path) -> None:
        """No sensor may reference a name that does not exist."""
        translated = _sensor_names(path)
        missing = sorted(set(ALL_KEYS) - set(translated))
        assert not missing, f"{path.name} is missing entity.sensor entries: {missing}"

    @pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
    def test_no_orphan_translations(self, path: Path) -> None:
        """And no name may exist without a sensor that uses it."""
        translated = _sensor_names(path)
        orphans = sorted(set(translated) - set(ALL_KEYS))
        assert not orphans, (
            f"{path.name} has entity.sensor entries no sensor references: {orphans}"
        )

    @pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
    def test_names_are_non_empty(self, path: Path) -> None:
        blank = sorted(key for key, name in _sensor_names(path).items() if not name)
        assert not blank

    def test_sensor_names_are_identical_in_both_files(self) -> None:
        """Home Assistant reads translations/en.json; strings.json is upstream's.

        ``tests/test_translations.py`` compares the files as a whole; this
        narrower check reports drift in the block this module owns.
        """
        assert _sensor_names(STRINGS_PATH) == _sensor_names(TRANSLATIONS_PATH)

    def test_existing_sensor_names_did_not_churn(self) -> None:
        """Renaming a shipped sensor changes entity IDs on new installs.

        These are the names ``sensor.py`` hardcoded before the strings were
        moved into the translation files.
        """
        shipped = {
            "latest_interval_usage": "Latest interval usage",
            "daily_usage": "Yesterday's usage",
            "monthly_usage": "Current month usage",
            "daily_cost": "Yesterday's cost",
            "monthly_cost": "Current month cost",
            "last_bill_charges": "Last bill charges",
            "last_bill_usage": "Last bill usage",
            "current_period_usage": "Current billing period usage",
            "effective_rate": "Effective rate",
            "billing_period_start": "Billing period start",
            "billing_period_end": "Billing period end",
            "is_time_of_use": "Time-of-use plan",
        }
        names = _sensor_names(TRANSLATIONS_PATH)
        assert {key: names.get(key) for key in shipped} == shipped


class TestStateClasses:
    """Home Assistant raises a repair issue for illegal combinations."""

    @pytest.mark.parametrize("sensor", ALL_SENSORS, ids=ALL_KEYS)
    def test_state_class_is_legal_for_the_device_class(
        self, sensor: dict[str, Any]
    ) -> None:
        device_class = sensor.get("device_class")
        state_class = sensor.get("state_class")
        if device_class is None or state_class is None:
            return
        allowed = ALLOWED_STATE_CLASSES.get(device_class)
        assert allowed is not None, f"unknown device class {device_class}"
        assert state_class in allowed, (
            f"{sensor['key']}: {state_class} is not valid for {device_class}"
        )

    @pytest.mark.parametrize("sensor", ALL_SENSORS, ids=ALL_KEYS)
    def test_measurement_kwh_sensors_declare_no_device_class(
        self, sensor: dict[str, Any]
    ) -> None:
        """The trap documented on latest_interval_usage, applied everywhere."""
        if sensor.get("state_class") != "SensorStateClass.MEASUREMENT":
            return
        if sensor.get("native_unit_of_measurement") != "UnitOfEnergy.KILO_WATT_HOUR":
            return
        assert sensor.get("device_class") is None, (
            f"{sensor['key']}: device_class=ENERGY requires a total state class"
        )

    @pytest.mark.parametrize("sensor", ALL_SENSORS, ids=ALL_KEYS)
    def test_numeric_sensors_carry_a_unit(self, sensor: dict[str, Any]) -> None:
        if sensor.get("state_class") is None:
            return
        assert sensor.get("native_unit_of_measurement"), (
            f"{sensor['key']} has a state class but no unit"
        )

    @pytest.mark.parametrize("sensor", ALL_SENSORS, ids=ALL_KEYS)
    def test_numeric_sensors_declare_display_precision(
        self, sensor: dict[str, Any]
    ) -> None:
        if sensor.get("state_class") is None:
            return
        assert isinstance(sensor.get("suggested_display_precision"), int), (
            f"{sensor['key']} would render at full float precision"
        )

    def test_energy_sensors_are_in_kwh(self) -> None:
        wrong = [
            s["key"]
            for s in ALL_SENSORS
            if s.get("device_class") == "SensorDeviceClass.ENERGY"
            and s.get("native_unit_of_measurement") != "UnitOfEnergy.KILO_WATT_HOUR"
        ]
        assert not wrong

    def test_monetary_sensors_are_in_usd(self) -> None:
        wrong = [
            s["key"]
            for s in ALL_SENSORS
            if s.get("device_class") == "SensorDeviceClass.MONETARY"
            and s.get("native_unit_of_measurement") != "USD"
        ]
        assert not wrong


class TestEntityCategories:
    """Diagnostics stay out of the way; user-facing numbers stay visible."""

    DIAGNOSTIC = "EntityCategory.DIAGNOSTIC"

    def _category(self, key: str) -> Any:
        return next(s for s in ALL_SENSORS if s["key"] == key).get("entity_category")

    @pytest.mark.parametrize(
        "key",
        [
            "billing_period_start",
            "billing_period_end",
            "is_time_of_use",
            "rate_check_estimated",
            "rate_check_drift",
            "rate_schedule_effective_date",
            "last_successful_update",
        ],
    )
    def test_plumbing_is_diagnostic(self, key: str) -> None:
        assert self._category(key) == self.DIAGNOSTIC

    @pytest.mark.parametrize(
        "key",
        [
            "period_to_date_usage",
            "period_to_date_cost",
            "projected_period_usage",
            "projected_period_cost",
            "daily_generation",
            "monthly_generation",
            "latest_interval_generation",
        ],
    )
    def test_user_facing_values_are_primary(self, key: str) -> None:
        assert self._category(key) is None


class TestRateDrift:
    """The drift check has to be readable, not a raw fraction."""

    def _sensor(self, key: str) -> dict[str, Any]:
        return next(s for s in ALL_SENSORS if s["key"] == key)

    def test_drift_is_reported_as_a_percentage(self) -> None:
        drift = self._sensor("rate_check_drift")
        assert drift["native_unit_of_measurement"] == "PERCENTAGE"
        assert drift["state_class"] == "SensorStateClass.MEASUREMENT"

    def test_drift_scales_the_signed_fraction(self) -> None:
        """``rate_check_discrepancy`` is a fraction; the sensor shows percent."""
        call = next(
            call
            for call in _tuple_of_descriptions("SENSORS")
            if _describe(call)["key"] == "rate_check_drift"
        )
        value_fn = next(kw.value for kw in call.keywords if kw.arg == "value_fn")
        source = ast.unparse(value_fn)
        assert "rate_check_discrepancy" in source
        assert "* 100" in source

    def test_tariff_effective_date_accompanies_the_drift(self) -> None:
        """Drift is meaningless without knowing which filing was priced."""
        effective = self._sensor("rate_schedule_effective_date")
        assert effective["device_class"] == "SensorDeviceClass.DATE"

    def test_effective_date_comes_from_the_rates_registry(self) -> None:
        source = SENSOR_PATH.read_text(encoding="utf-8")
        assert "LATEST_SCHEDULE_EFFECTIVE_DATE" in source


class TestBudgetGating:
    """Most people never set a budget; they must not see empty entities."""

    def test_budget_sensors_are_a_separate_group(self) -> None:
        budget_keys = {s["key"] for s in BUDGET_SENSORS}
        assert budget_keys == {"budget_remaining", "budget_used"}
        assert not budget_keys & {s["key"] for s in SENSORS}

    def test_they_are_gated_on_a_configured_budget(self) -> None:
        source = ast.unparse(_function("async_setup_entry"))
        assert "period_budget" in source
        assert "BUDGET_SENSORS" in source

    def test_no_listener_is_needed_for_the_budget(self) -> None:
        """The budget is an option, and options changes reload the entry.

        Generation needs a coordinator listener because it can appear on a
        later refresh; a budget cannot, so a one-shot check at setup is both
        sufficient and simpler. This pins that they are gated differently on
        purpose.
        """
        source = ast.unparse(_function("async_setup_entry"))
        budget_branch = source.split("period_budget", 1)[1].split("\n\n", 1)[0]
        assert "async_add_listener" not in budget_branch


class TestGenerationGating:
    """Most meters never export; their owners must not see empty entities."""

    def test_generation_sensors_are_a_separate_group(self) -> None:
        generation_keys = {s["key"] for s in GENERATION_SENSORS}
        assert generation_keys == {
            "latest_interval_generation",
            "daily_generation",
            "monthly_generation",
        }
        assert not generation_keys & {s["key"] for s in SENSORS}

    def test_setup_adds_the_base_sensors_unconditionally(self) -> None:
        source = ast.unparse(_function("async_setup_entry"))
        assert "for description in SENSORS" in source
        assert source.count("async_add_entities(") == 3, (
            "expected one unconditional add for SENSORS and one gated add "
            "each for BUDGET_SENSORS and GENERATION_SENSORS"
        )

    def test_generation_sensors_are_gated_on_has_generation(self) -> None:
        source = ast.unparse(_function("async_setup_entry"))
        assert "has_generation" in source, (
            "generation entities must be gated on the coordinator's "
            "has_generation flag, not created for every meter"
        )
        assert "GENERATION_SENSORS" in source

    def test_gate_keeps_watching_after_the_first_refresh(self) -> None:
        """``has_generation`` can flip to True on a later refresh.

        A one-shot check at setup would hide the entities until the user
        restarts Home Assistant, so the platform stays subscribed to the
        coordinator until generation appears.
        """
        source = ast.unparse(_function("async_setup_entry"))
        assert "async_add_listener" in source
        assert "async_on_unload" in source

    def test_generation_entities_are_added_only_once(self) -> None:
        source = ast.unparse(_function("async_setup_entry"))
        assert "generation_added" in source


class TestOptionalLastBill:
    """``BillForecast.last_bill`` is None until an account has a closed bill.

    A new account, or one whose first cycle has not been read yet, gets a
    forecast with no ``last_bill``. Guarding only the forecast and then
    reaching through to ``.charges`` raises ``AttributeError`` inside
    ``native_value``, which takes down every sensor on the platform rather
    than just the two that have nothing to show.
    """

    def _value_fn_source(self, key: str) -> str:
        call = next(
            call
            for call in _tuple_of_descriptions("SENSORS")
            if _describe(call)["key"] == key
        )
        value_fn = next(kw.value for kw in call.keywords if kw.arg == "value_fn")
        return ast.unparse(value_fn)

    @pytest.mark.parametrize("key", ["last_bill_charges", "last_bill_usage"])
    def test_last_bill_is_read_through_the_guard(self, key: str) -> None:
        source = self._value_fn_source(key)
        assert "_last_bill(data)" in source, (
            f"{key} must read the last bill through _last_bill(), which checks "
            "both the forecast and the optional last_bill"
        )
        assert "data.bill_forecast.last_bill" not in source, (
            f"{key} reaches through last_bill without checking it for None"
        )

    def test_guard_checks_both_levels(self) -> None:
        source = ast.unparse(_function("_last_bill"))
        assert "data.bill_forecast is None" in source
        assert "return data.bill_forecast.last_bill" in source
