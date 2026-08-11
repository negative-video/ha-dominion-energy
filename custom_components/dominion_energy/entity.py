"""Shared entity identity for the Dominion Energy platforms.

Every platform this integration adds has to agree on two things: the prefix its
unique IDs are built from, and the device its entities attach to. Getting
either wrong is not a cosmetic bug -- a changed unique ID orphans an entity and
throws away its recorded history, and a mismatched device identifier scatters
the entities across duplicate devices.

That decision therefore lives here, resolved once per config entry, rather than
being re-derived by each platform.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT_NUMBER, CONF_METER_NUMBER, CONF_SERVICE_ADDRESS, DOMAIN
from .coordinator import DominionEnergyConfigEntry, DominionEnergyCoordinator


@dataclass(frozen=True)
class DominionEntityIdentity:
    """How one config entry's entities are named and grouped."""

    unique_id_prefix: str
    device_info: DeviceInfo


def _uses_legacy_identity(
    hass: HomeAssistant,
    entry: DominionEnergyConfigEntry,
    account_number: str,
    meter_number: str,
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

    Step 1 reads this entry's own registry entries rather than probing a list
    of known entity keys: a platform added in a later release registers under
    whichever scheme the entry already uses, so enumerating is both simpler and
    proof against a key the probe list has never heard of.
    """
    registry = er.async_get(hass)
    legacy_prefix = f"{account_number}_"
    meter_prefix = f"{account_number}_{meter_number}_"
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = registry_entry.unique_id
        if unique_id.startswith(legacy_prefix) and not unique_id.startswith(
            meter_prefix
        ):
            return True

    return not any(
        other.entry_id != entry.entry_id
        and other.data.get(CONF_ACCOUNT_NUMBER) == account_number
        for other in hass.config_entries.async_entries(DOMAIN)
    )


def resolve_identity(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> DominionEntityIdentity:
    """Resolve the unique-ID prefix and device this entry's entities use."""
    account_number = entry.data[CONF_ACCOUNT_NUMBER]
    meter_number = entry.data.get(CONF_METER_NUMBER)
    service_address = entry.data.get(CONF_SERVICE_ADDRESS)

    if meter_number is None or _uses_legacy_identity(
        hass, entry, account_number, meter_number
    ):
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

    return DominionEntityIdentity(
        unique_id_prefix=unique_id_prefix, device_info=device_info
    )


class DominionEnergyEntity(CoordinatorEntity[DominionEnergyCoordinator]):
    """Base class binding an entity to its coordinator, device and unique ID."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DominionEnergyCoordinator,
        description: EntityDescription,
        identity: DominionEntityIdentity,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{identity.unique_id_prefix}_{description.key}"
        self._attr_device_info = identity.device_info
