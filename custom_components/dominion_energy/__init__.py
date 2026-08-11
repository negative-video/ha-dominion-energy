"""The Dominion Energy integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import CONF_ACCOUNT_NUMBER, CONF_METER_NUMBER, DOMAIN
from .coordinator import DominionEnergyConfigEntry, DominionEnergyCoordinator
from .green_button import GreenButtonError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_IMPORT_GREEN_BUTTON = "import_green_button"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_FILE_PATH = "file_path"
ATTR_DRY_RUN = "dry_run"

IMPORT_GREEN_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_FILE_PATH): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


async def async_setup_entry(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> bool:
    """Set up Dominion Energy from a config entry."""
    coordinator = DominionEnergyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    _async_register_services(hass)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once, on first entry setup."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_GREEN_BUTTON):
        return

    async def _async_import_green_button(call: ServiceCall) -> ServiceResponse:
        """Import Green Button XML exports as statistics history."""
        entry_id: str = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise HomeAssistantError(
                f"No Dominion Energy config entry with id {entry_id}"
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError(
                f"Config entry {entry.title} is not loaded; "
                "reload the integration and try again"
            )

        paths: list[str] = call.data[ATTR_FILE_PATH]
        for path in paths:
            # Home Assistant restricts which directories an integration may
            # read, and this path comes straight from the caller.
            if not hass.config.is_allowed_path(path):
                raise HomeAssistantError(
                    f"Path {path} is not allowed. Put the export somewhere "
                    "under your config directory, or add its directory to "
                    "allowlist_external_dirs in configuration.yaml."
                )

        coordinator: DominionEnergyCoordinator = entry.runtime_data
        try:
            summary: dict[str, Any] = await coordinator.async_import_green_button(
                paths, dry_run=call.data[ATTR_DRY_RUN]
            )
        except GreenButtonError as err:
            raise HomeAssistantError(
                f"Could not read Green Button export: {err}"
            ) from err
        except OSError as err:
            raise HomeAssistantError(
                f"Could not open Green Button export: {err}"
            ) from err
        return summary

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_GREEN_BUTTON,
        _async_import_green_button,
        schema=IMPORT_GREEN_BUTTON_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


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
