"""Binary sensor platform for Dominion Energy.

These are the entities meant to be *acted on* rather than read: a binary
sensor is what an automation trigger and a notification actually want, and
`device_class=PROBLEM` is what makes Home Assistant render it as something
worth looking at rather than another number on a card.

Everything here is derived from data the coordinator already holds. The
detail behind each state rides along as attributes, because "yes, yesterday
was unusual" is only useful next to what it was measured against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DominionEnergyConfigEntry, DominionEnergyData
from .entity import DominionEnergyEntity, resolve_identity

PARALLEL_UPDATES = 0  # Coordinator handles updates


@dataclass(frozen=True, kw_only=True)
class DominionEnergyBinarySensorDescription(BinarySensorEntityDescription):  # type: ignore[override]
    """Describes a Dominion Energy binary sensor."""

    #: Returns None when the answer is not yet knowable, which Home Assistant
    #: renders as `unknown` rather than a confident `off`.
    value_fn: Callable[[DominionEnergyData], bool | None]
    attributes_fn: Callable[[DominionEnergyData], dict[str, Any]] | None = None


def _unusual_usage_attributes(data: DominionEnergyData) -> dict[str, Any]:
    """Explain the comparison behind the flag."""
    comparison = data.day_comparison
    if comparison is None:
        return {}
    return {
        "day": comparison.day.isoformat(),
        "usage_kwh": comparison.total,
        "typical_kwh": comparison.typical,
        # Signed percent, so a template can say "31% more than usual" without
        # rescaling a fraction first.
        "difference_percent": round(comparison.delta * 100, 1),
        "direction": comparison.direction,
        "weekday": comparison.day.strftime("%A"),
        "compared_days": comparison.compared_days,
        "threshold_percent": round(comparison.threshold * 100, 1),
    }


BINARY_SENSORS: tuple[DominionEnergyBinarySensorDescription, ...] = (
    DominionEnergyBinarySensorDescription(
        key="unusual_usage",
        translation_key="unusual_usage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: (
            data.day_comparison.unusual if data.day_comparison else None
        ),
        attributes_fn=_unusual_usage_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DominionEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Dominion Energy binary sensors."""
    coordinator = entry.runtime_data
    identity = resolve_identity(hass, entry)

    async_add_entities(
        DominionEnergyBinarySensor(
            coordinator=coordinator,
            description=description,
            identity=identity,
        )
        for description in BINARY_SENSORS
    )


class DominionEnergyBinarySensor(DominionEnergyEntity, BinarySensorEntity):
    """Representation of a Dominion Energy binary sensor."""

    entity_description: DominionEnergyBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the figures the state was derived from."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None or self.coordinator.data is None:
            return None
        return attributes_fn(self.coordinator.data) or None
