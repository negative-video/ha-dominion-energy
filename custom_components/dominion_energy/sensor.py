"""Sensor platform for Dominion Energy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_METER_NUMBER,
    CONF_SERVICE_ADDRESS,
    COST_MODE_SCHEDULE_1,
    DOMAIN,
)
from .coordinator import (
    DominionEnergyConfigEntry,
    DominionEnergyCoordinator,
    DominionEnergyData,
)
from .rates import (
    LATEST_SCHEDULE_EFFECTIVE_DATE,
    PeriodBill,
    days_since_schedule_change,
    get_schedule_for_date,
    is_schedule_possibly_stale,
)

PARALLEL_UPDATES = 0  # Coordinator handles updates

# ``LATEST_SCHEDULE_EFFECTIVE_DATE`` is the ISO string off the newest encoded
# tariff; SensorDeviceClass.DATE needs a real ``datetime.date``.
_SCHEDULE_EFFECTIVE_FROM = date.fromisoformat(LATEST_SCHEDULE_EFFECTIVE_DATE)
_LATEST_SCHEDULE = get_schedule_for_date(_SCHEDULE_EFFECTIVE_FROM)


@dataclass(frozen=True, kw_only=True)
class DominionEnergySensorDescription(SensorEntityDescription):  # type: ignore[override]
    """Describes a Dominion Energy sensor."""

    value_fn: Callable[[DominionEnergyData], float | str | date | None]


SENSORS: tuple[DominionEnergySensorDescription, ...] = (
    # Existing sensors
    DominionEnergySensorDescription(
        key="latest_interval_usage",
        translation_key="latest_interval_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Note: Not using device_class=ENERGY because state_class=MEASUREMENT
        # is incompatible with energy device class (requires total/total_increasing)
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.latest_usage,
    ),
    DominionEnergySensorDescription(
        key="daily_usage",
        translation_key="daily_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.daily_total,
    ),
    DominionEnergySensorDescription(
        key="monthly_usage",
        translation_key="monthly_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.monthly_total,
    ),
    DominionEnergySensorDescription(
        key="daily_cost",
        translation_key="daily_cost",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data.daily_cost,
    ),
    DominionEnergySensorDescription(
        key="monthly_cost",
        translation_key="monthly_cost",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data.monthly_cost,
    ),
    # New bill forecast sensors - Primary
    DominionEnergySensorDescription(
        key="last_bill_charges",
        translation_key="last_bill_charges",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: (
            data.bill_forecast.last_bill.charges if data.bill_forecast else None
        ),
    ),
    DominionEnergySensorDescription(
        key="last_bill_usage",
        translation_key="last_bill_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: (
            data.bill_forecast.last_bill.usage if data.bill_forecast else None
        ),
    ),
    DominionEnergySensorDescription(
        key="current_period_usage",
        translation_key="current_period_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda data: (
            data.bill_forecast.current_usage_kwh if data.bill_forecast else None
        ),
    ),
    DominionEnergySensorDescription(
        key="effective_rate",
        translation_key="effective_rate",
        native_unit_of_measurement="USD/kWh",
        device_class=None,  # No standard device class for rates
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: (
            data.bill_forecast.derived_rate if data.bill_forecast else None
        ),
    ),
    # Billing period tracking - Primary
    #
    # ``current_period_usage`` above is whatever the bill forecast endpoint
    # reports; the two below are computed from our own interval data since the
    # start of the billing period, so they update daily instead of whenever
    # Dominion refreshes the forecast, and they carry a matching cost.
    DominionEnergySensorDescription(
        key="period_to_date_usage",
        translation_key="period_to_date_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # TOTAL rather than TOTAL_INCREASING: the value legitimately drops back
        # to ~0 when a new billing period starts, which is a reset, not a
        # meter rollover.
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data.period_to_date_usage,
    ),
    DominionEnergySensorDescription(
        key="period_to_date_cost",
        translation_key="period_to_date_cost",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data.period_to_date_cost,
    ),
    DominionEnergySensorDescription(
        key="projected_period_usage",
        translation_key="projected_period_usage",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.projected_period_usage,
    ),
    DominionEnergySensorDescription(
        key="projected_period_cost",
        translation_key="projected_period_cost",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data.projected_period_cost,
    ),
    # New bill forecast sensors - Diagnostic
    DominionEnergySensorDescription(
        key="billing_period_start",
        translation_key="billing_period_start",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.bill_forecast.current_period_start if data.bill_forecast else None
        ),
    ),
    DominionEnergySensorDescription(
        key="billing_period_end",
        translation_key="billing_period_end",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.bill_forecast.current_period_end if data.bill_forecast else None
        ),
    ),
    DominionEnergySensorDescription(
        key="is_time_of_use",
        translation_key="is_time_of_use",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            "Yes" if data.bill_forecast and data.bill_forecast.is_tou else "No"
        ),
    ),
    # Rate model self-check - Diagnostic
    #
    # The coordinator prices the last bill with our encoded Schedule 1 tariff
    # and compares the result against what Dominion actually charged. A drift
    # that grows over time means rates.py has missed a filing.
    DominionEnergySensorDescription(
        key="rate_check_estimated",
        translation_key="rate_check_estimated",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.rate_check_estimated,
    ),
    DominionEnergySensorDescription(
        key="rate_check_drift",
        translation_key="rate_check_drift",
        # ``rate_check_discrepancy`` is a signed fraction (0.031 = 3.1% high),
        # which nobody wants to read off a dashboard. Surface it as a percent;
        # the two dollar figures behind it ride along as attributes.
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.rate_check_discrepancy * 100
            if data.rate_check_discrepancy is not None
            else None
        ),
    ),
    DominionEnergySensorDescription(
        key="rate_schedule_effective_date",
        translation_key="rate_schedule_effective_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Constant for a given release: it says which tariff filing the bundled
        # rate table is built from, which is the context needed to judge the
        # drift sensor above.
        value_fn=lambda _data: _SCHEDULE_EFFECTIVE_FROM,
    ),
)

# Generation (solar export) sensors. Kept separate from SENSORS because they
# are only created for meters that actually report exported energy - see
# async_setup_entry.
GENERATION_SENSORS: tuple[DominionEnergySensorDescription, ...] = (
    DominionEnergySensorDescription(
        key="latest_interval_generation",
        translation_key="latest_interval_generation",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Same reason as latest_interval_usage: state_class=MEASUREMENT is
        # incompatible with device_class=ENERGY.
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.latest_generation,
    ),
    DominionEnergySensorDescription(
        key="daily_generation",
        translation_key="daily_generation",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.daily_generation_total,
    ),
    DominionEnergySensorDescription(
        key="monthly_generation",
        translation_key="monthly_generation",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.monthly_generation_total,
    ),
)

#: Every description this platform can create, in a stable order.
ALL_SENSORS: tuple[DominionEnergySensorDescription, ...] = SENSORS + GENERATION_SENSORS


def _breakdown_attributes(bill: PeriodBill) -> dict[str, Any]:
    """Render a priced billing period as flat, readable attributes.

    Flat rather than a nested dict so the more-info dialog lists the line items
    the way a bill does, and six attributes rather than six entities because
    nobody graphs "transmission charges" — they read it once when the total
    surprises them.
    """
    attrs: dict[str, Any] = dict(bill.components())
    attrs["breakdown_total"] = round(bill.total, 2)
    attrs["breakdown_largest"] = bill.largest_component()
    attrs["breakdown_basis"] = bill.schedule_name
    attrs["breakdown_effective_date"] = bill.schedule_effective_date
    attrs["season"] = bill.season.value
    return attrs


def _uses_legacy_identity(
    hass: HomeAssistant,
    entry: DominionEnergyConfigEntry,
    account_number: str,
) -> bool:
    """Return True when this entry keeps the original account-only identity.

    Entities used to be keyed ``{account_number}_{description.key}`` and the
    device was identified by ``(DOMAIN, account_number)``. Config entries are
    now unique per account *and* meter, so two meters on one account would
    collide on both. Simply switching everything to a meter-scoped scheme is
    not an option: a changed unique ID orphans the existing entity and throws
    away its recorded history.

    The scheme is therefore "first meter keeps the old identity":

    =============  ==========================  ==========================
    entry          unique_id                   device identifier
    =============  ==========================  ==========================
    first meter    ``{account}_{key}``         ``(DOMAIN, {account})``
    later meters   ``{account}_{meter}_{key}`` ``(DOMAIN, {account}_{meter})``
    =============  ==========================  ==========================

    "First" is resolved in two steps, in order:

    1. If the entity registry already holds an account-only entity owned by
       *this* entry, this entry is the first meter and keeps that identity
       forever - no matter how many meters are added afterwards.
    2. Otherwise (nothing registered yet) it is the first meter only if it is
       the sole config entry for the account.

    Both steps are stable across restarts and independent of the order in which
    entries are set up, so an entry never changes identity once it has run.
    """
    registry = er.async_get(hass)
    for description in ALL_SENSORS:
        entity_id = registry.async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{account_number}_{description.key}"
        )
        if entity_id is None:
            continue
        registry_entry = registry.async_get(entity_id)
        if (
            registry_entry is not None
            and registry_entry.config_entry_id == entry.entry_id
        ):
            return True

    return not any(
        other.entry_id != entry.entry_id
        and other.data.get(CONF_ACCOUNT_NUMBER) == account_number
        for other in hass.config_entries.async_entries(DOMAIN)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DominionEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Dominion Energy sensors."""
    coordinator = entry.runtime_data
    account_number = entry.data[CONF_ACCOUNT_NUMBER]
    meter_number = entry.data.get(CONF_METER_NUMBER)
    service_address = entry.data.get(CONF_SERVICE_ADDRESS)

    if meter_number is None or _uses_legacy_identity(hass, entry, account_number):
        unique_id_prefix = account_number
        device_identifier = account_number
        device_name = f"Dominion Energy {account_number}"
    else:
        unique_id_prefix = f"{account_number}_{meter_number}"
        device_identifier = unique_id_prefix
        # Only the additional meters get a disambiguating name, so the device
        # of a pre-existing single-meter entry is left alone. Meter numbers are
        # zero-padded to 18 characters; drop the padding to keep it readable.
        device_name = (
            f"Dominion Energy {account_number} "
            f"meter {meter_number.lstrip('0') or meter_number}"
        )

    device_info = DeviceInfo(
        identifiers={(DOMAIN, device_identifier)},
        name=device_name,
        manufacturer="Dominion Energy",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://myaccount.dominionenergy.com",
    )

    # Add service address as model if available
    if service_address:
        device_info["model"] = service_address

    def _build(
        description: DominionEnergySensorDescription,
    ) -> DominionEnergySensor:
        return DominionEnergySensor(
            coordinator=coordinator,
            description=description,
            device_info=device_info,
            unique_id_prefix=unique_id_prefix,
        )

    async_add_entities(_build(description) for description in SENSORS)

    # Generation entities would be dead weight on the overwhelming majority of
    # meters, which never export anything, so they are only created once the
    # coordinator has seen generation. ``has_generation`` is derived from a
    # fetch window and can be False on the first refresh and True later (a
    # brand-new PV system, or a fetch that only covered an overcast day), so
    # rather than deciding once at setup we keep watching the coordinator and
    # add the entities the moment generation shows up. That avoids both the
    # clutter and the "restart Home Assistant to see your solar sensors" trap
    # of a one-shot check, and beats shipping them disabled-by-default, which
    # Home Assistant would never re-enable on its own once registered.
    generation_added = False

    @callback
    def _async_add_generation_sensors() -> None:
        """Add the generation entities the first time export is reported."""
        nonlocal generation_added
        data = coordinator.data
        if generation_added or data is None or not data.has_generation:
            return
        generation_added = True
        async_add_entities(_build(description) for description in GENERATION_SENSORS)

    _async_add_generation_sensors()
    if not generation_added:
        entry.async_on_unload(
            coordinator.async_add_listener(_async_add_generation_sensors)
        )


class DominionEnergySensor(CoordinatorEntity[DominionEnergyCoordinator], SensorEntity):
    """Representation of a Dominion Energy sensor."""

    _attr_has_entity_name = True
    entity_description: DominionEnergySensorDescription

    def __init__(
        self,
        coordinator: DominionEnergyCoordinator,
        description: DominionEnergySensorDescription,
        device_info: DeviceInfo,
        unique_id_prefix: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{unique_id_prefix}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | str | date | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes including data_date.

        Adds data_date attribute to daily/interval sensors to indicate which
        day the data represents (since data is delayed by ~1 day).
        """
        key = self.entity_description.key
        attrs: dict[str, Any] = {}

        # Provenance of the bundled tariff, so a drifting rate check can be
        # traced back to the filing it was priced against. Independent of
        # coordinator data.
        if key == "rate_schedule_effective_date":
            attrs["schedule"] = _LATEST_SCHEDULE.name
            attrs["source_url"] = _LATEST_SCHEDULE.source_url
            attrs["source_retrieved"] = _LATEST_SCHEDULE.source_retrieved
            attrs["days_since_effective"] = days_since_schedule_change()
            attrs["possibly_stale"] = is_schedule_possibly_stale()
            return attrs

        if self.coordinator.data is None:
            return None

        data = self.coordinator.data

        # Add data_date for daily and interval sensors
        if (
            key
            in (
                "daily_usage",
                "daily_cost",
                "latest_interval_usage",
                "daily_generation",
                "latest_interval_generation",
            )
            and data.data_date
        ):
            attrs["data_date"] = data.data_date.isoformat()

        # Add date range for monthly sensors
        if key in ("monthly_usage", "monthly_cost", "monthly_generation"):
            if data.month_start_date:
                attrs["month_start"] = data.month_start_date.isoformat()
            if data.month_end_date:
                attrs["month_end"] = data.month_end_date.isoformat()

        # The drift sensor is a percentage; carry the two figures it was
        # derived from so the comparison is readable in one place.
        # ``rate_check_actual`` is the same number as the "Last bill charges"
        # sensor, so it deliberately does not get an entity of its own.
        if key == "rate_check_drift":
            attrs["estimated"] = data.rate_check_estimated
            attrs["actual"] = data.rate_check_actual

        # Where the projected bill actually goes. Six components rather than
        # six more entities: nobody puts "transmission charges" on a dashboard,
        # but everybody wants to know why the total moved.
        if key == "projected_period_cost" and data.projected_bill is not None:
            attrs.update(_breakdown_attributes(data.projected_bill))
            # The breakdown always prices the full tariff; the sensor's own
            # state follows the configured cost mode. Say plainly whether the
            # two are the same number, so nobody reconciles a difference that
            # is a mode choice rather than an error.
            attrs["breakdown_matches_state"] = (
                self.coordinator.cost_mode == COST_MODE_SCHEDULE_1
            )

        return attrs if attrs else None
