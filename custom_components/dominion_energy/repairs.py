"""Repair flows for the Dominion Energy integration.

There is one, and it exists for a specific kind of harm. A day recorded at
twice the going rate looks, on the Energy Dashboard, exactly like a billing
error -- and the reasonable response to a billing error is to phone the
utility about a number the utility never produced and cannot see. The issue
this flow fixes says whose fault it is, in the place the user is already
looking, and then repairs it without asking anyone to open Developer Tools.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir


class CostAnomalyRepairFlow(RepairsFlow):
    """Recompute the recorded cost history for one config entry."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the flow."""
        self.entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Handle the first step of a fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Confirm, then rebuild the cost statistics."""
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is not None:
                await coordinator.async_rebuild_cost_statistics()
            return self.async_create_entry(data={})

        issue_registry = ir.async_get(self.hass)
        description_placeholders = None
        if issue := issue_registry.async_get_issue(self.handler, self.issue_id):
            description_placeholders = issue.translation_placeholders

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=description_placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the flow that fixes an issue."""
    entry_id = (data or {}).get("entry_id")
    assert isinstance(entry_id, str)
    return CostAnomalyRepairFlow(entry_id=entry_id)
