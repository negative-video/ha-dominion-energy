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
* a ``device_class``/``state_class`` pair Home Assistant rejects,
* a change to the unique-ID or device-identifier scheme, which orphans every
  existing entity and discards its recorded history.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import COMPONENT_DIR

SENSOR_PATH = COMPONENT_DIR / "sensor.py"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATIONS_PATH = COMPONENT_DIR / "translations" / "en.json"

DESCRIPTION_CLASS = "DominionEnergySensorDescription"
ENTITY_CLASS = "DominionEnergySensor"

# The unique-ID prefix every install used before config entries became unique
# per account *and* meter. An entry that already owns entities under this
# scheme must keep it, or its history is thrown away.
LEGACY_UNIQUE_ID_TEMPLATE = "{account_number}_{key}"
LEGACY_DEVICE_NAME_TEMPLATE = "Dominion Energy {account_number}"

# Subset of homeassistant.components.sensor.const.DEVICE_CLASS_STATE_CLASSES
# covering the device classes this platform uses. Kept here rather than
# imported so the check also runs without Home Assistant installed.
ALLOWED_STATE_CLASSES: dict[str, set[str]] = {
    "SensorDeviceClass.ENERGY": {
        "SensorStateClass.TOTAL",
        "SensorStateClass.TOTAL_INCREASING",
    },
    "SensorDeviceClass.MONETARY": {"SensorStateClass.TOTAL"},
    "SensorDeviceClass.DATE": set(),
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _module() -> ast.Module:
    """Parse sensor.py."""
    return ast.parse(SENSOR_PATH.read_text(encoding="utf-8"))


def _dotted(node: ast.expr) -> str:
    """Render a Name/Attribute chain as dotted source text."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    raise AssertionError(f"not a dotted name: {ast.dump(node)}")


def _literal(node: ast.expr) -> Any:
    """Render a keyword value as a comparable Python value.

    Constants come through as themselves; enum members and module constants
    come through as their dotted source text (``"SensorDeviceClass.ENERGY"``),
    which is enough to compare against without importing Home Assistant.
    """
    if isinstance(node, ast.Constant):
        return node.value
    return _dotted(node)


def _format_template(node: ast.expr) -> str:
    """Render a string expression as a ``str.format`` template.

    ``f"{a}_{b}"`` becomes ``"{a}_{b}"`` and a bare ``a`` becomes ``"{a}"``, so
    an f-string and the variable it is built from can be compared and filled in
    by the tests below. An interpolation that is not a plain dotted name is
    rendered as ``«source»`` - still inspectable, but deliberately not a
    ``str.format`` field, since there is nothing sensible to substitute.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_format_template(value) for value in node.values)
    if isinstance(node, ast.FormattedValue):
        try:
            return "{" + _dotted(node.value) + "}"
        except AssertionError:
            return f"«{ast.unparse(node.value)}»"
    return "{" + _dotted(node) + "}"


def _tuple_of_descriptions(name: str) -> list[ast.Call]:
    """Return the description calls making up a module-level tuple."""
    for node in _module().body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target != name or node.value is None:
            continue
        assert isinstance(node.value, ast.Tuple), f"{name} is not a tuple literal"
        calls = [
            element
            for element in node.value.elts
            if isinstance(element, ast.Call)
            and isinstance(element.func, ast.Name)
            and element.func.id == DESCRIPTION_CLASS
        ]
        return calls
    raise AssertionError(f"{name} not found in {SENSOR_PATH}")


def _describe(call: ast.Call) -> dict[str, Any]:
    """Return a description call's keyword arguments as plain values."""
    fields: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg == "value_fn":
            continue
        fields[keyword.arg] = _literal(keyword.value)
    return fields


def _function(name: str, class_name: str | None = None) -> ast.FunctionDef:
    """Return a module-level or method function definition by name."""
    scope: list[ast.stmt] = _module().body
    if class_name is not None:
        for node in scope:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                scope = node.body
                break
        else:
            raise AssertionError(f"class {class_name} not found")
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return node  # type: ignore[return-value]
    raise AssertionError(f"function {name} not found")


def _assigned_templates(function: ast.AST, target_name: str) -> list[str]:
    """Return every value assigned to ``target_name``, as format templates."""
    return [
        _format_template(node.value)
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == target_name
            for target in node.targets
        )
    ]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sensor_names(path: Path) -> dict[str, str]:
    return {
        key: value["name"]
        for key, value in _load_json(path)["entity"]["sensor"].items()
    }


SENSORS = [_describe(call) for call in _tuple_of_descriptions("SENSORS")]
GENERATION_SENSORS = [
    _describe(call) for call in _tuple_of_descriptions("GENERATION_SENSORS")
]
ALL_SENSORS = SENSORS + GENERATION_SENSORS
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
        assert source.count("async_add_entities(") == 2, (
            "expected one unconditional add for SENSORS and one gated add for "
            "GENERATION_SENSORS"
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


class TestUniqueIdScheme:
    """A changed unique_id orphans the entity and discards its history."""

    ACCOUNT = "123456789123"
    METER = "000000000296117800"
    OTHER_METER = "000000000296117801"
    KEY = "daily_usage"

    def _entity_template(self) -> str:
        init = _function("__init__", ENTITY_CLASS)
        assignments = [
            _format_template(node.value)
            for node in ast.walk(init)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Attribute) and target.attr == "_attr_unique_id"
                for target in node.targets
            )
        ]
        assert len(assignments) == 1, "expected exactly one unique_id assignment"
        return assignments[0]

    def _prefix_templates(self) -> list[str]:
        return _assigned_templates(_function("async_setup_entry"), "unique_id_prefix")

    def _unique_ids(self) -> list[str]:
        """Render the full unique_id for each identity branch."""
        entity_template = self._entity_template()
        rendered = []
        for prefix_template in self._prefix_templates():
            prefix = prefix_template.format(
                account_number=self.ACCOUNT, meter_number=self.METER
            )
            rendered.append(
                entity_template.format(
                    unique_id_prefix=prefix,
                    description=_Key(self.KEY),
                )
            )
        return rendered

    def test_entity_builds_its_id_from_a_prefix_and_the_key(self) -> None:
        assert self._entity_template() == "{unique_id_prefix}_{description.key}"

    def test_there_are_exactly_two_identity_branches(self) -> None:
        assert len(self._prefix_templates()) == 2, (
            "expected a legacy branch and a meter-scoped branch"
        )

    def test_legacy_identity_is_byte_identical_to_the_old_scheme(self) -> None:
        """An existing single-meter entry keeps the ID it already registered."""
        expected = LEGACY_UNIQUE_ID_TEMPLATE.format(
            account_number=self.ACCOUNT, key=self.KEY
        )
        assert expected in self._unique_ids(), (
            f"no identity branch still produces {expected!r}; every existing "
            "install would lose its entity history"
        )

    def test_second_meter_gets_a_distinct_identity(self) -> None:
        ids = self._unique_ids()
        assert len(set(ids)) == 2, f"identity branches collide: {ids}"

    def test_meter_scoped_ids_differ_between_meters(self) -> None:
        meter_template = next(
            template
            for template in self._prefix_templates()
            if "meter_number" in template
        )
        first = meter_template.format(
            account_number=self.ACCOUNT, meter_number=self.METER
        )
        second = meter_template.format(
            account_number=self.ACCOUNT, meter_number=self.OTHER_METER
        )
        assert first != second

    def test_every_sensor_key_survives_both_branches(self) -> None:
        """Keys must stay collision-free once the prefix is applied."""
        entity_template = self._entity_template()
        for prefix_template in self._prefix_templates():
            prefix = prefix_template.format(
                account_number=self.ACCOUNT, meter_number=self.METER
            )
            ids = [
                entity_template.format(unique_id_prefix=prefix, description=_Key(key))
                for key in ALL_KEYS
            ]
            assert len(set(ids)) == len(ALL_KEYS)


class TestDeviceIdentity:
    """The device is keyed the same way, for the same reason."""

    ACCOUNT = "123456789123"
    METER = "000000000296117800"

    def _device_identifiers(self) -> list[str]:
        return _assigned_templates(_function("async_setup_entry"), "device_identifier")

    def _device_names(self) -> list[str]:
        return _assigned_templates(_function("async_setup_entry"), "device_name")

    def test_legacy_device_identifier_is_the_bare_account_number(self) -> None:
        rendered = [
            template.format(
                account_number=self.ACCOUNT,
                meter_number=self.METER,
                unique_id_prefix=f"{self.ACCOUNT}_{self.METER}",
            )
            for template in self._device_identifiers()
        ]
        assert self.ACCOUNT in rendered, (
            "an existing entry's device must keep identifiers={(DOMAIN, account)}"
        )
        assert len(set(rendered)) == 2, f"device identifiers collide: {rendered}"

    def test_legacy_device_name_is_unchanged(self) -> None:
        rendered = [
            template.format(account_number=self.ACCOUNT)
            for template in self._device_names()
            if "meter_number" not in template
        ]
        expected = LEGACY_DEVICE_NAME_TEMPLATE.format(account_number=self.ACCOUNT)
        assert rendered == [expected]

    def test_additional_meters_get_a_distinguishing_device_name(self) -> None:
        distinguishing = [
            template for template in self._device_names() if "meter_number" in template
        ]
        assert len(distinguishing) == 1
        assert "meter" in distinguishing[0]


class TestIdentityResolution:
    """The "first meter" decision has to be stable across restarts."""

    def _source(self) -> str:
        return ast.unparse(_function("_uses_legacy_identity"))

    def test_it_consults_the_entity_registry(self) -> None:
        """Registered entities are the only durable record of the old scheme."""
        source = self._source()
        assert "async_get_entity_id" in source
        assert "config_entry_id" in source, (
            "a legacy entity belonging to a different entry must not make this "
            "entry claim the legacy identity"
        )

    def test_it_falls_back_to_being_the_only_entry_for_the_account(self) -> None:
        source = self._source()
        assert "async_entries" in source
        assert "CONF_ACCOUNT_NUMBER" in source

    def test_it_probes_every_sensor_key(self) -> None:
        """A partially-registered entry (new sensor added later) still counts."""
        assert "ALL_SENSORS" in self._source()

    def test_setup_falls_back_to_legacy_without_a_meter_number(self) -> None:
        """Entries predating the meter number must not key on ``None``."""
        source = ast.unparse(_function("async_setup_entry"))
        assert "meter_number is None or _uses_legacy_identity" in source


class _Key:
    """Stand-in exposing ``.key`` so ``{description.key}`` can be formatted."""

    def __init__(self, key: str) -> None:
        self.key = key
