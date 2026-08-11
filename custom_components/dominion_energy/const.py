"""Constants for the Dominion Energy integration."""

from typing import Final

DOMAIN: Final = "dominion_energy"

# Config entry data keys
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_COOKIES: Final = "cookies"
CONF_ACCOUNT_NUMBER: Final = "account_number"
CONF_METER_NUMBER: Final = "meter_number"
CONF_SERVICE_ADDRESS: Final = "service_address"
# Signature of the cost options the stored cost statistics were built with.
# Persisted in the entry data so cost history is only rebuilt when the user
# actually changes how cost is calculated, not on every restart.
CONF_COST_SIGNATURE: Final = "cost_signature"
# Prefix the external statistic IDs of this entry are built from, i.e.
# `dominion_energy:{prefix}_energy_consumption`. Resolved once when the entry
# is first set up and persisted here so the choice is stable across restarts
# and never re-derived by probing the recorder. See
# `coordinator.resolve_statistic_id_prefix()` for the compatibility rule.
CONF_STATISTIC_ID_PREFIX: Final = "statistic_id_prefix"

# Options keys for cost configuration
CONF_COST_MODE: Final = "cost_mode"
CONF_FIXED_RATE: Final = "fixed_rate"
CONF_PEAK_RATE: Final = "peak_rate"
CONF_OFF_PEAK_RATE: Final = "off_peak_rate"
CONF_PEAK_START_HOUR: Final = "peak_start_hour"
CONF_PEAK_END_HOUR: Final = "peak_end_hour"

# Cost mode options
COST_MODE_FIXED: Final = "fixed"
COST_MODE_TOU: Final = "time_of_use"
COST_MODE_API: Final = "api_estimate"
COST_MODE_SCHEDULE_1: Final = "schedule_1"

# Update interval. The API only publishes complete days, so new data appears at
# most once a day; each fetch is a server-side Excel export behind a WAF, so we
# poll conservatively and skip fetches while the target day has not advanced.
UPDATE_INTERVAL_MINUTES: Final = 60

# Historical data backfill on first setup
# Note: The Dominion API returns ~68 days of 30-minute data regardless of
# the requested date range, so we request 60 days to capture most available data
BACKFILL_DAYS: Final = 60

# Default cost values
DEFAULT_FIXED_RATE: Final = 0.12  # $/kWh
DEFAULT_PEAK_RATE: Final = 0.15  # $/kWh
DEFAULT_OFF_PEAK_RATE: Final = 0.08  # $/kWh
DEFAULT_PEAK_START_HOUR: Final = 14  # 2 PM
DEFAULT_PEAK_END_HOUR: Final = 19  # 7 PM
