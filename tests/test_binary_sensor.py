"""Tests for the binary sensor platform.

Same approach and same reasons as ``test_sensor.py``: the platform imports
Home Assistant, so it is read with ``ast`` rather than imported.

The contract worth guarding here is narrower than the sensor platform's but
breaks the same way -- a ``translation_key`` with no entry behind it leaves an
entity named after its device, and a binary sensor that reports ``off`` when it
means "I don't know yet" quietly tells people everything is fine.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from tests.astkit import describe, tuple_of_calls
from tests.conftest import COMPONENT_DIR

BINARY_SENSOR_PATH = COMPONENT_DIR / "binary_sensor.py"
INIT_PATH = COMPONENT_DIR / "__init__.py"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATIONS_PATH = COMPONENT_DIR / "translations" / "en.json"

DESCRIPTION_CLASS = "DominionEnergyBinarySensorDescription"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _names(path: Path) -> dict[str, str]:
    entity = _load_json(path)["entity"]
    return {
        key: value["name"] for key, value in entity.get("binary_sensor", {}).items()
    }


def _calls() -> list[ast.Call]:
    return tuple_of_calls(BINARY_SENSOR_PATH, "BINARY_SENSORS", DESCRIPTION_CLASS)


BINARY_SENSORS = [
    describe(call, skip=("value_fn", "attributes_fn")) for call in _calls()
]
ALL_KEYS = [sensor["key"] for sensor in BINARY_SENSORS]


def test_ast_extraction_found_something() -> None:
    """Guard against the AST helpers silently returning nothing."""
    assert BINARY_SENSORS
    assert all("key" in sensor for sensor in BINARY_SENSORS)


class TestPlatformRegistration:
    """A platform file nothing forwards to is dead code."""

    def test_binary_sensor_is_in_platforms(self) -> None:
        source = INIT_PATH.read_text(encoding="utf-8")
        assert "Platform.BINARY_SENSOR" in source, (
            "binary_sensor.py exists but __init__.PLATFORMS does not forward "
            "to it, so none of its entities would ever be created"
        )


class TestTranslationKeys:
    """Same contract the sensor platform is held to."""

    def test_every_description_sets_a_translation_key(self) -> None:
        missing = [s["key"] for s in BINARY_SENSORS if not s.get("translation_key")]
        assert not missing

    def test_translation_key_matches_the_description_key(self) -> None:
        mismatched = {
            s["key"]: s["translation_key"]
            for s in BINARY_SENSORS
            if s["translation_key"] != s["key"]
        }
        assert not mismatched

    def test_no_description_hardcodes_a_name(self) -> None:
        named = [s["key"] for s in BINARY_SENSORS if "name" in s]
        assert not named

    @pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
    def test_every_translation_key_is_translated(self, path: Path) -> None:
        missing = sorted(set(ALL_KEYS) - set(_names(path)))
        assert not missing, (
            f"{path.name} is missing entity.binary_sensor entries: {missing}"
        )

    @pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
    def test_no_orphan_translations(self, path: Path) -> None:
        orphans = sorted(set(_names(path)) - set(ALL_KEYS))
        assert not orphans, (
            f"{path.name} has entity.binary_sensor entries nothing references: "
            f"{orphans}"
        )

    def test_names_are_identical_in_both_files(self) -> None:
        assert _names(STRINGS_PATH) == _names(TRANSLATIONS_PATH)


class TestKeysDoNotCollideWithSensors:
    """Both platforms share one unique-ID prefix."""

    def test_no_key_is_reused_by_a_sensor(self) -> None:
        """`{prefix}_{key}` must stay unique across every platform.

        The prefix comes from `entity.resolve_identity` and is the same for
        both, so two platforms sharing a key would produce the same unique ID.
        """
        from tests.test_sensor import ALL_KEYS as SENSOR_KEYS

        collisions = set(ALL_KEYS) & set(SENSOR_KEYS)
        assert not collisions, f"key used by both platforms: {collisions}"


class TestUnknownIsNotOff:
    """ "Off" and "not knowable yet" are different answers."""

    def test_value_fns_can_return_none(self) -> None:
        """Every description's value_fn has a None branch.

        A binary sensor reporting `off` before it has the data to judge tells
        people everything is fine, which is worse than saying nothing.
        """
        for call in _calls():
            value_fn = next(kw.value for kw in call.keywords if kw.arg == "value_fn")
            source = ast.unparse(value_fn)
            assert "None" in source or "else" in source, (
                f"{describe(call)['key']}: value_fn cannot express 'unknown'"
            )

    def test_entity_returns_none_without_coordinator_data(self) -> None:
        source = BINARY_SENSOR_PATH.read_text(encoding="utf-8")
        assert "if self.coordinator.data is None:" in source
        assert "return None" in source


class TestDeviceClasses:
    """A device class is what makes the state read as more than on/off."""

    @pytest.mark.parametrize("sensor", BINARY_SENSORS, ids=ALL_KEYS)
    def test_device_class_is_a_known_binary_sensor_class(
        self, sensor: dict[str, Any]
    ) -> None:
        device_class = sensor.get("device_class")
        if device_class is None:
            return
        assert device_class.startswith("BinarySensorDeviceClass."), device_class

    def test_unusual_usage_is_a_problem(self) -> None:
        """PROBLEM is what surfaces it as something to look at."""
        unusual = next(s for s in BINARY_SENSORS if s["key"] == "unusual_usage")
        assert unusual["device_class"] == "BinarySensorDeviceClass.PROBLEM"
