"""Diagnostics support for the Dominion Energy integration.

The output of this module is routinely pasted into public GitHub issues, so
every field is chosen deliberately: identifiers that tie the dump to a real
person or account are redacted, while the shape of those identifiers (length
and character class) is preserved because account/meter number formats differ
between Dominion's service territories and are often the thing being debugged.
"""

from __future__ import annotations

from datetime import date, datetime
from importlib import metadata
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_NUMBER,
    CONF_COOKIES,
    CONF_METER_NUMBER,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SERVICE_ADDRESS,
    CONF_USERNAME,
    COST_MODE_API,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import DominionEnergyConfigEntry

# Keys redacted from any mapping that is emitted verbatim.
#
# CONF_COOKIES holds Gigya session cookies, which are bearer credentials just
# like the tokens. The account/meter numbers and the service address are not
# secrets in the credential sense, but they identify the customer and their
# home, so they are redacted too and summarised structurally instead.
TO_REDACT: set[str] = {
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_NUMBER,
    CONF_COOKIES,
    CONF_METER_NUMBER,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SERVICE_ADDRESS,
    CONF_USERNAME,
    # Defensive extras: keys the config flow does not write today but that a
    # future change (or an upstream rename) could plausibly introduce.
    "email",
    "token",
    "id_token",
    "session",
    "session_cookie",
    "premise_number",
    "contract_id",
    "unique_id",
    "title",
    "address",
    "service_address",
    "latitude",
    "longitude",
}

# Dominion Energy's electric service territories. Which one a customer is in
# materially changes API behaviour (see upstream issue #19, where South
# Carolina accounts fail to authenticate), so the region is worth surfacing
# even though the rest of the address is not.
_SERVICE_REGIONS: tuple[str, ...] = (
    "VA",
    "NC",
    "SC",
    "OH",
    "WV",
    "UT",
    "WY",
    "ID",
)


def describe_identifier(value: Any) -> str | None:
    """Summarise an identifier's shape without revealing the identifier.

    Account and meter numbers differ in length and formatting between service
    territories, which is frequently the root cause of an API rejection. This
    reports the structure (e.g. ``"12 chars, all digits"``) and nothing else.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return f"non-string ({type(value).__name__})"
    if not value:
        return "empty"

    if value.isdigit():
        shape = "all digits"
    elif value.isalpha():
        shape = "all letters"
    elif value.isalnum():
        shape = "alphanumeric"
    else:
        shape = "mixed"

    leading_zeros = len(value) - len(value.lstrip("0"))
    if leading_zeros:
        shape = f"{shape}, {leading_zeros} leading zero(s)"

    return f"{len(value)} chars, {shape}"


def describe_service_region(address: Any) -> str | None:
    """Extract the service-territory state from an address, if recognisable.

    Returns the two-letter code only - never any part of the street address.
    """
    if not isinstance(address, str) or not address:
        return None
    # Split on anything non-alphanumeric so the match survives both the
    # formatted address ("123 Main St, Richmond, VA 23220") and a raw
    # dataclass repr (state='VA'), should the stored format ever change.
    tokens = {token.upper() for token in re.split(r"[^A-Za-z0-9]+", address) if token}
    matches = sorted(tokens & set(_SERVICE_REGIONS))
    if not matches:
        return "unknown"
    return matches[0] if len(matches) == 1 else "ambiguous"


def _as_iso(value: Any) -> str | None:
    """Render a date/datetime as ISO-8601, passing through anything else."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _dompower_version() -> str:
    """Report the installed dompower version without importing the library."""
    try:
        return metadata.version("dompower")
    except metadata.PackageNotFoundError:  # pragma: no cover - env dependent
        return "not installed"


def summarize_intervals(intervals: Any) -> dict[str, Any]:
    """Summarise interval usage without emitting the raw consumption series.

    Half-hourly consumption is a behavioural fingerprint of the household, so
    only aggregates are reported. Counts, coverage and the generation flag are
    what actually diagnose a data problem.
    """
    if not intervals:
        return {
            "count": 0,
            "days_covered": 0,
            "first_timestamp": None,
            "last_timestamp": None,
            "total_consumption_kwh": None,
            "nonzero_count": 0,
            "has_generation": False,
        }

    timestamps = [getattr(i, "timestamp", None) for i in intervals]
    known = [t for t in timestamps if t is not None]
    consumption = [float(getattr(i, "consumption", 0.0) or 0.0) for i in intervals]
    generation = [float(getattr(i, "generation", 0.0) or 0.0) for i in intervals]

    return {
        "count": len(intervals),
        "days_covered": len({t.date() for t in known if hasattr(t, "date")}),
        "first_timestamp": _as_iso(min(known)) if known else None,
        "last_timestamp": _as_iso(max(known)) if known else None,
        "total_consumption_kwh": round(sum(consumption), 3),
        "nonzero_count": sum(1 for c in consumption if c > 0),
        "has_generation": any(g > 0 for g in generation),
        "unit": getattr(intervals[0], "unit", None),
    }


def summarize_bill_forecast(forecast: Any) -> dict[str, Any] | None:
    """Summarise the bill forecast used to derive the API-estimate rate."""
    if forecast is None:
        return None

    last_bill = getattr(forecast, "last_bill", None)
    try:
        derived_rate = getattr(forecast, "derived_rate", None)
    except Exception:  # noqa: BLE001 - never let diagnostics raise
        derived_rate = None

    return {
        "current_period_start": _as_iso(
            getattr(forecast, "current_period_start", None)
        ),
        "current_period_end": _as_iso(getattr(forecast, "current_period_end", None)),
        "current_usage_kwh": getattr(forecast, "current_usage_kwh", None),
        "is_tou": getattr(forecast, "is_tou", None),
        "derived_rate": derived_rate,
        "last_bill": None
        if last_bill is None
        else {
            "charges": getattr(last_bill, "charges", None),
            "usage": getattr(last_bill, "usage", None),
            "period_start": _as_iso(getattr(last_bill, "period_start", None)),
            "period_end": _as_iso(getattr(last_bill, "period_end", None)),
        },
    }


def build_diagnostics(
    *,
    entry_data: Any,
    entry_options: Any,
    entry_version: Any,
    entry_minor_version: Any,
    entry_source: Any,
    entry_state: Any,
    entry_disabled_by: Any,
    entry_pref_disable_polling: Any,
    last_update_success: Any,
    last_exception: Any,
    update_interval: Any,
    coordinator_data: Any,
    has_statistics: Any = None,
    ha_version: Any = None,
) -> dict[str, Any]:
    """Assemble the redacted diagnostics payload.

    Split out from :func:`async_get_config_entry_diagnostics` so the redaction
    behaviour can be exercised directly, without standing up Home Assistant.
    """
    entry_data = dict(entry_data or {})
    entry_options = dict(entry_options or {})

    # Everything below reads from the coordinator's data object defensively:
    # its fields have changed shape before and diagnostics must never be the
    # thing that raises during a support request.
    data = coordinator_data
    intervals = getattr(data, "intervals", None) if data is not None else None

    payload: dict[str, Any] = {
        "config_entry": {
            "version": entry_version,
            "minor_version": entry_minor_version,
            "source": entry_source,
            "state": str(entry_state) if entry_state is not None else None,
            "disabled_by": str(entry_disabled_by) if entry_disabled_by else None,
            "pref_disable_polling": entry_pref_disable_polling,
            # Redacted copy of the raw entry data. Kept so a reader can see
            # which keys are present (and which are missing - a missing
            # username is why auto-reauth silently gives up), never the values.
            "data": async_redact_data(entry_data, TO_REDACT),
            "data_keys": sorted(entry_data),
            "options": async_redact_data(entry_options, TO_REDACT),
        },
        "account": {
            # Structure only - see describe_identifier().
            "account_number_format": describe_identifier(
                entry_data.get(CONF_ACCOUNT_NUMBER)
            ),
            "meter_number_format": describe_identifier(
                entry_data.get(CONF_METER_NUMBER)
            ),
            "service_region": describe_service_region(
                entry_data.get(CONF_SERVICE_ADDRESS)
            ),
            # Presence, not value: which credentials are stored determines
            # whether automatic re-authentication can even be attempted.
            "has_username": bool(entry_data.get(CONF_USERNAME)),
            "has_password": bool(entry_data.get(CONF_PASSWORD)),
            "has_access_token": bool(entry_data.get(CONF_ACCESS_TOKEN)),
            "has_refresh_token": bool(entry_data.get(CONF_REFRESH_TOKEN)),
            "has_cookies": bool(entry_data.get(CONF_COOKIES)),
        },
        "coordinator": {
            "last_update_success": last_update_success,
            # Type only. Exception messages from the API can echo back the
            # request, which may embed tokens or the account number.
            "last_exception_type": (
                type(last_exception).__name__ if last_exception is not None else None
            ),
            "update_interval_seconds": (
                update_interval.total_seconds()
                if hasattr(update_interval, "total_seconds")
                else None
            ),
            "has_data": data is not None,
        },
        "cost": {
            # The mode the coordinator will actually apply, with the default
            # resolved - an unset option is not the same as an absent feature.
            # The rates and peak hours themselves are user-entered
            # configuration, not secrets, and appear under config_entry.options.
            "mode": entry_options.get("cost_mode", COST_MODE_API),
        },
        "usage": {
            "data_date": _as_iso(getattr(data, "data_date", None)),
            "month_start_date": _as_iso(getattr(data, "month_start_date", None)),
            "month_end_date": _as_iso(getattr(data, "month_end_date", None)),
            "daily_total": getattr(data, "daily_total", None),
            "monthly_total": getattr(data, "monthly_total", None),
            "daily_cost": getattr(data, "daily_cost", None),
            "monthly_cost": getattr(data, "monthly_cost", None),
            "latest_interval_timestamp": _as_iso(
                getattr(getattr(data, "latest_interval", None), "timestamp", None)
            ),
            "intervals": summarize_intervals(intervals),
        },
        "bill_forecast": summarize_bill_forecast(
            getattr(data, "bill_forecast", None) if data is not None else None
        ),
        "statistics": {
            # Statistic IDs embed the account number, so only presence is
            # reported here.
            "has_statistics": has_statistics,
        },
        "versions": {
            "dompower": _dompower_version(),
            "home_assistant": ha_version,
        },
    }

    # Belt and braces: re-run redaction over the assembled payload so that any
    # sensitive key introduced by a future edit to this function is caught.
    return async_redact_data(payload, TO_REDACT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DominionEnergyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = getattr(entry, "runtime_data", None)

    has_statistics: bool | None = None
    account_number = entry.data.get(CONF_ACCOUNT_NUMBER)
    if account_number:
        has_statistics = await _async_has_statistics(hass, str(account_number))

    return build_diagnostics(
        entry_data=entry.data,
        entry_options=entry.options,
        entry_version=entry.version,
        entry_minor_version=getattr(entry, "minor_version", None),
        entry_source=entry.source,
        entry_state=getattr(entry, "state", None),
        entry_disabled_by=getattr(entry, "disabled_by", None),
        entry_pref_disable_polling=getattr(entry, "pref_disable_polling", None),
        last_update_success=getattr(coordinator, "last_update_success", None),
        last_exception=getattr(coordinator, "last_exception", None),
        update_interval=getattr(coordinator, "update_interval", None),
        coordinator_data=getattr(coordinator, "data", None),
        has_statistics=has_statistics,
        ha_version=getattr(hass.config, "version", None),
    )


async def _async_has_statistics(
    hass: HomeAssistant, account_number: str
) -> bool | None:
    """Report whether external statistics have been written for this account.

    Returns None if the recorder cannot be queried, so a recorder problem is
    distinguishable from genuinely absent statistics.
    """
    # Imported lazily: the recorder is a heavy dependency and a failure to
    # reach it must degrade to "unknown" rather than break diagnostics.
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import get_last_statistics

        from .const import DOMAIN

        statistic_id = f"{DOMAIN}:{account_number}_energy_consumption"
        last_stat = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, True, {"sum"}
        )
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return None
    return bool(last_stat.get(statistic_id))
