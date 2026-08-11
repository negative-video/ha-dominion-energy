"""The Dominion Energy integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNT_NUMBER, CONF_METER_NUMBER
from .coordinator import DominionEnergyConfigEntry, DominionEnergyCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> bool:
    """Set up Dominion Energy from a config entry."""
    coordinator = DominionEnergyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_update_listener(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> None:
    """Reload the entry when its options change.

    Update listeners also fire when only the entry data changes (refreshed
    tokens, the stored cost signature), so compare the options the coordinator
    was set up with to avoid reloading on every token refresh.
    """
    coordinator: DominionEnergyCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is not None and not coordinator.options_changed(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> bool:
    """Migrate an old config entry."""
    if entry.version > 1:
        # Downgrading from a future version is not supported.
        return False

    # Version 1 keyed the entry on the account number alone, which prevented
    # setting up a second meter on the same account.
    account_number = entry.data.get(CONF_ACCOUNT_NUMBER)
    meter_number = entry.data.get(CONF_METER_NUMBER)
    unique_id = entry.unique_id
    if account_number and meter_number:
        unique_id = f"{account_number}_{meter_number}"
    else:
        _LOGGER.warning(
            "Config entry %s is missing an account or meter number; "
            "keeping unique_id %s",
            entry.entry_id,
            unique_id,
        )

    hass.config_entries.async_update_entry(entry, unique_id=unique_id, version=2)
    _LOGGER.debug(
        "Migrated config entry %s to version 2 (unique_id=%s)",
        entry.entry_id,
        unique_id,
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
