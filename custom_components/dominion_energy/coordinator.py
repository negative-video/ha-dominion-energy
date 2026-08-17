"""DataUpdateCoordinator for Dominion Energy integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import partial
import logging
from typing import Any

from dompower import (
    ApiError,
    BillForecast,
    CannotConnectError,
    DompowerClient,
    GigyaAuthenticator,
    IntervalUsageData,
    InvalidAuthError,
    InvalidCredentialsError,
    TFARequiredError,
    TokenExpiredError,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_DAYS,
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_NUMBER,
    CONF_COOKIES,
    CONF_COST_MODE,
    CONF_COST_SIGNATURE,
    CONF_FIXED_RATE,
    CONF_HVAC_ENTITIES,
    CONF_METER_NUMBER,
    CONF_OFF_PEAK_RATE,
    CONF_PASSWORD,
    CONF_PEAK_END_HOUR,
    CONF_PEAK_RATE,
    CONF_PEAK_START_HOUR,
    CONF_PERIOD_BUDGET,
    CONF_REFRESH_TOKEN,
    CONF_STATISTIC_ID_PREFIX,
    CONF_USERNAME,
    COST_MODE_API,
    COST_MODE_SCHEDULE_1,
    COST_MODE_TOU,
    DEFAULT_FIXED_RATE,
    DEFAULT_OFF_PEAK_RATE,
    DEFAULT_PEAK_END_HOUR,
    DEFAULT_PEAK_RATE,
    DEFAULT_PEAK_START_HOUR,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .green_button import (
    FLOW_DIRECTION_DELIVERED,
    MIN_CORRELATION,
    GreenButtonExport,
    apply_shift,
    best_alignment,
    drop_incomplete_tail,
    magnitude_looks_wrong,
    merge_preferring,
    parse_export,
    to_hourly,
)
from .insights import (
    BASELINE_DAYS,
    BaselineLoad,
    DayComparison,
    TimeWindow,
    UsageProfile,
    baseline_load,
    compare_to_typical_day,
    hvac_active_windows,
    merge_windows,
    quietest_hours,
    usage_profile,
)
from .rates import (
    VA_SCHEDULE_1_HISTORY,
    PeriodBill,
    bill_discrepancy,
    calculate_schedule1_interval_cost,
    calculate_schedule1_period_bill,
    get_schedule_for_date,
)
from .usage import (
    DEFAULT_BILLING_PERIOD_DAYS,
    MIN_NONZERO_INTERVALS,
    UsageInterval,
    aggregate_hourly,
    billing_period_days,
    billing_period_end,
    billing_period_start,
    build_cumulative_statistics,
    day_looks_complete,
    deduplicate_hourly_by_utc,
    filter_incomplete_days,
    shift_months,
)

_LOGGER = logging.getLogger(__name__)

type DominionEnergyConfigEntry = ConfigEntry[DominionEnergyCoordinator]

# How long the last good payload keeps being served once cycles start failing.
#
# The API publishes exactly one new day per day, so a payload less than 24
# hours old is as current as the source ever gets. Blanking every entity the
# first time an hourly poll fails throws away nothing but availability: the
# numbers are still the same true numbers for the day they are labelled with,
# and `data_date` says which day that is. Past the window the data really is
# behind and going unavailable is the honest answer.
STALE_DATA_GRACE = timedelta(hours=24)

# Characters of an API error body carried into a log line. Long enough for the
# WAF block pages and JSON fault documents Dominion answers with, short enough
# not to bury the log.
MAX_ERROR_BODY_CHARS = 300


def _stat_start_utc(stat: Mapping[str, Any]) -> datetime:
    """Return a statistic row's start as an aware UTC datetime."""
    start = stat["start"]
    if isinstance(start, (int, float)):
        return datetime.fromtimestamp(start, tz=dt_util.UTC)
    return start


def generation_of(interval: Any) -> float:
    """Return an interval's exported kWh, tolerating older dompower releases.

    `IntervalUsageData.generation` only exists from dompower 0.2, and is None
    rather than 0.0 for meters the Excel export has no generation sheet for.
    """
    return float(getattr(interval, "generation", 0.0) or 0.0)


# ---------------------------------------------------------------------------
# Pure helpers
#
# These take plain values and are deliberately free of Home Assistant state so
# the decidable parts of the features below can be unit tested. Anything that
# needs the recorder, the config entry or the API client stays on the
# coordinator itself.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ImportedInterval:
    """Stand-in satisfying `UsageInterval` for a costed Green Button hour.

    The cost helpers are written against `dompower.IntervalUsageData`, but an
    imported hour has no such object behind it. Only `timestamp` and
    `consumption` are ever read.
    """

    timestamp: datetime
    consumption: float


def _load_green_button_files(
    file_paths: list[str],
) -> list[tuple[str, GreenButtonExport]]:
    """Read and parse Green Button exports. Runs in an executor: file I/O."""
    parsed: list[tuple[str, GreenButtonExport]] = []
    for path in file_paths:
        with open(path, "rb") as handle:
            parsed.append((path, parse_export(handle.read())))
    return parsed


def _earliest_priceable_date(cost_mode: str) -> date | None:
    """Return the earliest date this cost mode can honestly price.

    Schedule 1 is bounded by the oldest tariff in the rate registry; pricing
    earlier usage would apply rates that were not in effect. The flat modes
    carry no such history, so they can price anything.
    """
    if cost_mode != COST_MODE_SCHEDULE_1:
        return None
    return min(schedule.effective_from for schedule in VA_SCHEDULE_1_HISTORY)


def describe_api_error(err: Exception, secrets: Iterable[Any] = ()) -> str:
    """Render an API failure with enough detail to act on.

    `str(ApiError)` is `"API error: 400"` and nothing else, which says the
    request was refused but not why -- and a 400 from this API can mean an
    inverted date window, an account the token does not cover, or a WAF block
    page served under the wrong status. The response body distinguishes them,
    so it rides along.

    The body is squashed onto one line, truncated, and has the account and
    meter numbers masked out: these lines end up in issue reports, and the
    fault documents echo the request back verbatim.
    """
    message = str(err)
    body = getattr(err, "response_text", None)
    if not body:
        return message

    detail = " ".join(str(body).split())
    for secret in secrets:
        # Short values would match half the body; only mask real identifiers.
        # `None` is explicitly skipped rather than stringified, or an entry
        # missing a meter number would mask every "None" in the response.
        if secret is None:
            continue
        text = str(secret)
        if len(text) >= 4:
            detail = detail.replace(text, "***")
    if len(detail) > MAX_ERROR_BODY_CHARS:
        detail = f"{detail[:MAX_ERROR_BODY_CHARS]}... (truncated)"
    return f"{message}: {detail}"


def stale_data_is_serviceable(
    last_success: datetime | None,
    now: datetime,
    grace: timedelta = STALE_DATA_GRACE,
) -> bool:
    """Return whether the last good payload may stand in for a failed cycle.

    A payload with no recorded success behind it (an older release's data
    restored across a restart) is never served: nothing is known about how old
    it is, and guessing "fresh" is the wrong way to be wrong.
    """
    if last_success is None:
        return False
    return now - last_success < grace


def statistics_window_is_fetchable(start_date: date, data_date: date) -> bool:
    """Return whether ``start_date``..``data_date`` is worth asking the API for.

    An inverted window is never a meaningful request, and the Dominion API
    answers one with HTTP 400. The stale-zero heal branch in
    ``_update_statistics`` can produce exactly that: it fetches from
    ``last_good_date + 1 day``, which lands past the end of the window whenever
    the last fully-populated day *is* ``data_date``. Left unguarded, that logs a
    fetch failure every cycle for statistics that are in fact up to date.
    """
    return start_date <= data_date


def resolve_statistic_id_prefix(
    *,
    account_number: str,
    meter_number: str,
    stored_prefix: str | None,
    account_statistics_exist: bool,
    account_prefix_claimed: bool,
) -> str:
    """Return the prefix this entry's external statistic IDs are built from.

    Statistic IDs are `dominion_energy:{prefix}_energy_{consumption,cost,
    generation}`.

    The scheme
    ----------
    Every release up to now scoped that prefix to the account number alone.
    Config entries are now keyed per meter, so two meters on one account can be
    added separately - and would then write into the *same* statistics stream,
    silently interleaving two meters' readings. New entries therefore use a
    meter-scoped prefix, `{account}_{meter}`.

    Renaming the statistics of an install that already has history would orphan
    that history (external statistics cannot be renamed in place), so an entry
    that already owns account-scoped statistics keeps them. The result is
    persisted in the entry data under `CONF_STATISTIC_ID_PREFIX`, so this
    decision is made exactly once and never re-derived from the recorder.

    Args:
        account_number: The entry's account number.
        meter_number: The entry's meter device ID.
        stored_prefix: Prefix already persisted for this entry, if any.
        account_statistics_exist: Whether the recorder holds consumption
            statistics under the account-scoped ID.
        account_prefix_claimed: Whether a *different* entry on the same account
            already owns the account-scoped ID.

    Returns:
        The prefix to use.
    """
    if stored_prefix:
        return stored_prefix
    if account_statistics_exist and not account_prefix_claimed:
        # Legacy install: keep writing where its history already lives.
        return account_number
    return f"{account_number}_{meter_number}"


def days_with_generation[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    min_nonzero_intervals: int = MIN_NONZERO_INTERVALS,
) -> set[date]:
    """Return the days that reported a meaningful amount of exported energy."""
    counts: dict[date, int] = {}
    for interval in intervals:
        if generation_of(interval) > 0:
            day = interval.timestamp.date()
            counts[day] = counts.get(day, 0) + 1
    return {day for day, count in counts.items() if count >= min_nonzero_intervals}


def filter_incomplete_days_allowing_generation[IntervalT: UsageInterval](
    intervals: Sequence[IntervalT],
    min_nonzero_intervals: int = MIN_NONZERO_INTERVALS,
) -> tuple[list[IntervalT], list[date]]:
    """Drop unpublished days, but keep days that solar merely made look empty.

    `usage.filter_incomplete_days` decides a day is unpublished from its
    *consumption* alone. On a net-metered site a bright day can legitimately
    net out to (near) zero consumption, and dropping it would throw away both
    that day's zero and its generation.

    A day the consumption filter rejected is re-admitted when it reported
    generation in at least `min_nonzero_intervals` intervals: the consumption
    and generation worksheets come from the same export, so generation being
    present is evidence the day *was* published.

    Returns the intervals to keep and the sorted list of days still dropped.
    """
    kept, skipped = filter_incomplete_days(intervals, min_nonzero_intervals)
    if not skipped:
        return kept, skipped

    generating = days_with_generation(intervals, min_nonzero_intervals)
    readmitted = set(skipped) & generating
    if not readmitted:
        return kept, skipped

    still_dropped = set(skipped) - readmitted
    kept = [i for i in intervals if i.timestamp.date() not in still_dropped]
    return kept, sorted(still_dropped)


def aggregate_hourly_generation[IntervalT: UsageInterval](
    intervals: Iterable[IntervalT],
) -> dict[datetime, float]:
    """Bucket exported kWh into hourly totals keyed by the local hour start.

    Mirrors the bucketing `usage.aggregate_hourly` does for consumption, minus
    the billing-period bookkeeping: generation is not tiered or priced. Run the
    result through `usage.deduplicate_hourly_by_utc` before building statistics.
    """
    hourly: dict[datetime, float] = {}
    for interval in sorted(intervals, key=lambda i: i.timestamp):
        hour_start = interval.timestamp.replace(minute=0, second=0, microsecond=0)
        hourly[hour_start] = hourly.get(hour_start, 0.0) + generation_of(interval)
    return hourly


def project_period_usage(
    period_to_date_kwh: float,
    days_with_data: int,
    days_in_period: int,
) -> float | None:
    """Extrapolate billing-period usage from the days observed so far.

    Method: the mean daily usage of the days we actually have data for, times
    the length of the billing period. Deliberately the simplest defensible
    model - it needs no weather, no seasonality and no history beyond the
    current period, and it is what a person does in their head.

    `days_with_data` counts days that actually appear in the data rather than
    calendar days since the period started. A day the API has not published
    would otherwise be treated as a zero-usage day and drag the projection
    down; assuming an unpublished day looked like its neighbours is the less
    wrong of the two.

    The result never falls below `period_to_date_kwh`: energy already consumed
    cannot un-consume itself, however few days of it we have seen.

    Returns None when there is nothing to extrapolate from.
    """
    if days_with_data <= 0 or days_in_period <= 0:
        return None
    projected = period_to_date_kwh / days_with_data * days_in_period
    return max(projected, period_to_date_kwh)


def rate_check(
    charges: float | None,
    usage: float | None,
    period_start: date | None,
    period_end: date | None,
) -> tuple[float | None, float | None, float | None]:
    """Compare our Schedule 1 maths against a bill Dominion actually issued.

    `rates.py` encodes a tariff that goes stale whenever the SCC approves a
    filing we have not transcribed yet - which has happened, unnoticed, for
    months at a time. Re-deriving the last completed bill from its own kWh
    total and holding it up against the billed amount makes that
    self-announcing, so it is computed regardless of which cost mode the user
    picked.

    Returns `(estimated, actual, discrepancy)`, all None when the last bill is
    missing, zero or undated - never a fabricated figure.
    """
    if not charges or not usage or charges <= 0 or usage <= 0:
        return None, None, None
    if period_start is None or period_end is None or period_end <= period_start:
        return None, None, None

    estimated = round(
        calculate_schedule1_period_bill(usage, period_start, period_end).total, 2
    )
    actual = float(charges)
    return estimated, actual, bill_discrepancy(estimated, actual)


@dataclass
class DominionEnergyData:
    """Data returned by the coordinator."""

    intervals: list[IntervalUsageData]
    latest_interval: IntervalUsageData | None
    daily_total: float
    monthly_total: float
    daily_cost: float
    monthly_cost: float
    bill_forecast: BillForecast | None
    # Date tracking for delayed data
    data_date: date | None  # Which day the daily data represents (yesterday)
    month_start_date: date | None  # Start of the month range
    month_end_date: date | None  # End of month range (last complete day)

    # Excess generation exported to the grid (solar / net metering). Zero for
    # meters Dominion publishes no generation worksheet for.
    daily_generation_total: float = 0.0
    monthly_generation_total: float = 0.0
    has_generation: bool = False

    # Current billing period, from the bill forecast's period bounds rather
    # than the calendar month. None when there is no bill forecast to anchor
    # the period, or nothing yet to extrapolate from.
    period_to_date_usage: float | None = None
    period_to_date_cost: float | None = None
    projected_period_usage: float | None = None
    projected_period_cost: float | None = None

    # The average shape of a day over the trailing profile window. None until
    # enough complete days have accumulated to average.
    profile: UsageProfile | None = None

    # The latest complete day measured against the same weekday in recent
    # weeks. None until there are enough comparable days behind it.
    day_comparison: DayComparison | None = None

    # What the house draws overnight with the HVAC excluded. None until enough
    # usable nights have accumulated.
    baseline: BaselineLoad | None = None

    # The projected billing period priced by the Schedule 1 tariff, broken out
    # by component. Always the full tariff regardless of the configured cost
    # mode: it is what actually makes up a Dominion bill, and it is the only
    # mode that can say where the money goes. See `_projected_bill()`.
    projected_bill: PeriodBill | None = None

    # The end of the current billing period after `billing_period_end()` has
    # repaired a value that tracks today rather than naming the next meter
    # read. The sensor shows this rather than the raw forecast field so it
    # cannot contradict the projection, which is extrapolated over exactly
    # this period. None when there is no forecast to anchor it.
    period_end_date: date | None = None

    # Our Schedule 1 estimate of the last completed bill against what Dominion
    # actually charged, so stale rate data announces itself. See rate_check().
    rate_check_estimated: float | None = None
    rate_check_actual: float | None = None
    rate_check_discrepancy: float | None = None

    # The configured spending target for this billing period, copied onto the
    # data each cycle so the budget sensors read it the same way every other
    # sensor reads its value. None when no budget is set.
    period_budget: float | None = None

    # When a cycle last completed without an API failure -- either fetching
    # fresh data or deciding the day's data was already in hand. It stops
    # advancing the moment cycles start failing, which is what makes it worth
    # showing: the other sensors go on reading the last good numbers during
    # the grace window, and this is the one that says how old they are.
    last_success: datetime | None = None

    @property
    def budget_remaining(self) -> float | None:
        """Dollars left in the period's budget; negative once overspent."""
        if self.period_budget is None or self.period_to_date_cost is None:
            return None
        return round(self.period_budget - self.period_to_date_cost, 2)

    @property
    def budget_used(self) -> float | None:
        """Percent of the budget spent so far this billing period."""
        if not self.period_budget or self.period_to_date_cost is None:
            return None
        return round(self.period_to_date_cost / self.period_budget * 100, 1)

    @property
    def over_budget_pace(self) -> bool | None:
        """Whether the period is projected to finish over budget.

        Deliberately the *projection* rather than spend to date: "you have
        used 60% of your budget" says nothing useful on day 6 of 30, whereas
        "at this rate you will finish 20% over" does.
        """
        if self.period_budget is None or self.projected_period_cost is None:
            return None
        return self.projected_period_cost > self.period_budget

    @property
    def latest_usage(self) -> float | None:
        """Get the latest interval usage value."""
        return self.latest_interval.consumption if self.latest_interval else None

    @property
    def latest_generation(self) -> float | None:
        """Get the latest interval's exported kWh, or None without an interval."""
        if self.latest_interval is None:
            return None
        return generation_of(self.latest_interval)


class DominionEnergyCoordinator(DataUpdateCoordinator[DominionEnergyData]):
    """Coordinator to manage fetching Dominion Energy data."""

    config_entry: DominionEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: DominionEnergyConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._client: DompowerClient | None = None
        # Track if backfill has been initiated to prevent race condition
        # where recorder hasn't committed stats yet and backfill runs again
        self._backfill_initiated: bool = False
        # Options this coordinator was created with, used to tell an options
        # change apart from an entry data change (e.g. refreshed tokens)
        self._options_snapshot: dict[str, Any] = dict(config_entry.options)
        # Day whose data has already been fetched, so repeated update cycles
        # within the same day don't re-download the same Excel exports
        self._cached_data_date: date | None = None
        # Consecutive cycles that failed outright. Used to log the first
        # degraded cycle loudly and the rest quietly, and reported in
        # diagnostics so an intermittent API shows up as a count.
        self._consecutive_failures: int = 0
        # Imports rewrite the whole statistics chain, so two running at once
        # would interleave their sums.
        self._import_lock = asyncio.Lock()
        # Prefix the external statistic IDs are built from, resolved once in
        # _async_setup() and persisted in the entry data from then on
        self._statistic_id_prefix: str | None = config_entry.data.get(
            CONF_STATISTIC_ID_PREFIX
        )

    def options_changed(self, options: Mapping[str, Any]) -> bool:
        """Return True if options differ from the ones used to set up this run."""
        return dict(options) != self._options_snapshot

    @property
    def consecutive_failures(self) -> int:
        """Cycles that have failed in a row since the last good one."""
        return self._consecutive_failures

    @property
    def cost_mode(self) -> str:
        """Return the configured cost calculation mode."""
        return str(self.config_entry.options.get(CONF_COST_MODE, COST_MODE_API))

    @property
    def period_budget(self) -> float | None:
        """Return the configured billing-period spending target, if any.

        None when unset or zero, which is what the platforms gate the budget
        entities on. Options changes reload the entry, so a budget set later
        creates them without a restart.
        """
        budget = self.config_entry.options.get(CONF_PERIOD_BUDGET)
        if budget is None:
            return None
        try:
            value = float(budget)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _token_update_callback(self, access_token: str, refresh_token: str) -> None:
        """Handle token updates from the client."""
        new_data = {
            **self.config_entry.data,
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
        }
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        _LOGGER.debug("Tokens updated and persisted")

    async def _async_setup(self) -> None:
        """Set up the coordinator (called once on first refresh)."""
        session = async_get_clientsession(self.hass)
        self._client = DompowerClient(
            session,
            access_token=self.config_entry.data[CONF_ACCESS_TOKEN],
            refresh_token=self.config_entry.data[CONF_REFRESH_TOKEN],
            token_update_callback=self._token_update_callback,
        )
        self._statistic_id_prefix = await self._async_resolve_statistic_id_prefix()

    def _account_prefix_claimed(self, account_number: str) -> bool:
        """Return True when another entry already owns the account-scoped IDs.

        Two entries on one account must not share a statistics stream. The
        account-scoped prefix therefore belongs to at most one of them.
        """
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self.config_entry.entry_id:
                continue
            if str(entry.data.get(CONF_ACCOUNT_NUMBER)) != account_number:
                continue
            if entry.data.get(CONF_STATISTIC_ID_PREFIX) == account_number:
                return True
            if (
                not entry.data.get(CONF_STATISTIC_ID_PREFIX)
                and entry.entry_id < self.config_entry.entry_id
            ):
                # A sibling that has not resolved yet could still turn out to
                # be the legacy owner of the account-scoped statistics. Break
                # the tie on entry_id so both entries reach the same conclusion
                # in any order, and reach it again after a restart.
                return True
        return False

    async def _async_account_statistics_exist(self, account_number: str) -> bool:
        """Report whether account-scoped consumption statistics already exist."""
        statistic_id = f"{DOMAIN}:{account_number}_energy_consumption"
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        return bool(last_stat.get(statistic_id))

    async def _async_resolve_statistic_id_prefix(self) -> str:
        """Resolve, persist and return this entry's statistic ID prefix.

        The recorder is only probed on the one setup where no prefix has been
        stored yet; afterwards the stored value is authoritative.
        """
        account_number = str(self.config_entry.data[CONF_ACCOUNT_NUMBER])
        meter_number = str(self.config_entry.data[CONF_METER_NUMBER])
        stored = self.config_entry.data.get(CONF_STATISTIC_ID_PREFIX)
        if stored:
            return str(stored)

        claimed = self._account_prefix_claimed(account_number)
        # Skip the recorder round trip when the answer cannot change it.
        exists = (
            False
            if claimed
            else await self._async_account_statistics_exist(account_number)
        )

        prefix = resolve_statistic_id_prefix(
            account_number=account_number,
            meter_number=meter_number,
            stored_prefix=None,
            account_statistics_exist=exists,
            account_prefix_claimed=claimed,
        )
        _LOGGER.info(
            "Using statistic ID prefix %s for this entry (existing account-scoped "
            "statistics: %s, claimed by another entry: %s)",
            prefix,
            exists,
            claimed,
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_STATISTIC_ID_PREFIX: prefix},
        )
        return prefix

    def _statistic_ids(self) -> tuple[str, str, str]:
        """Return the (consumption, cost, generation) statistic IDs."""
        prefix = self._statistic_id_prefix or str(
            self.config_entry.data[CONF_ACCOUNT_NUMBER]
        )
        return (
            f"{DOMAIN}:{prefix}_energy_consumption",
            f"{DOMAIN}:{prefix}_energy_cost",
            f"{DOMAIN}:{prefix}_energy_generation",
        )

    def _statistic_name(self, kind: str) -> str:
        """Build the human readable name shown next to a statistic."""
        account_number = str(self.config_entry.data[CONF_ACCOUNT_NUMBER])
        name = f"Dominion Energy {account_number}"
        if self._statistic_id_prefix and self._statistic_id_prefix != account_number:
            meter_number = str(self.config_entry.data[CONF_METER_NUMBER])
            name = f"{name} meter ...{meter_number[-8:]}"
        return f"{name} {kind}"

    async def _async_attempt_reauth(self) -> bool:
        """Attempt to re-authenticate using stored credentials.

        Returns True if successful, False if manual reauth needed.
        """
        username = self.config_entry.data.get(CONF_USERNAME)
        password = self.config_entry.data.get(CONF_PASSWORD)
        existing_cookies = self.config_entry.data.get(CONF_COOKIES)

        if not username or not password:
            _LOGGER.warning("No stored credentials for auto-reauth")
            return False

        _LOGGER.info("Attempting automatic re-authentication for %s", username)
        session = async_get_clientsession(self.hass)

        try:
            # Use GigyaAuthenticator.async_login() without TFA callback
            # This will raise TFARequiredError if TFA is needed
            auth = GigyaAuthenticator(session)

            # Import existing cookies to potentially bypass TFA
            if existing_cookies:
                auth.import_cookies(existing_cookies)

            tokens = await auth.async_login(username, password, tfa_code_callback=None)

            # Export new cookies after successful login
            new_cookies = auth.export_cookies()

            # Update stored tokens and cookies in config entry
            new_data = {
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_COOKIES: new_cookies,
            }
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )

            # Reinitialize client with new tokens
            self._client = DompowerClient(
                session,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                token_update_callback=self._token_update_callback,
            )

            _LOGGER.info("Successfully re-authenticated with stored credentials")
            return True

        except TFARequiredError:
            _LOGGER.info("TFA required during reauth - manual intervention needed")
            return False
        except InvalidCredentialsError as err:
            _LOGGER.warning("Auto-reauth failed - credentials invalid: %s", err)
            return False
        except CannotConnectError as err:
            _LOGGER.warning("Auto-reauth failed - connection error: %s", err)
            return False
        except Exception as err:
            _LOGGER.warning("Auto-reauth failed unexpectedly: %s", err)
            return False

    async def _async_update_data(self) -> DominionEnergyData:
        """Fetch data from the API, riding out a transient failure.

        A source that publishes once a day does not become wrong because one
        hourly poll was refused, so a failed cycle falls back to the last good
        payload for as long as `STALE_DATA_GRACE` allows rather than taking
        every entity unavailable and stalling the Energy Dashboard.

        Only `UpdateFailed` is absorbed. `ConfigEntryAuthFailed` passes
        straight through: credentials that need attention need it now, and
        hiding that behind day-old data would be the wrong kind of patience.
        """
        try:
            data = await self._async_fetch_data(allow_reauth=True)
        except UpdateFailed as err:
            return self._serve_last_good_data(err)

        if self._consecutive_failures:
            _LOGGER.info(
                "Data is flowing again after %d failed cycle(s)",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        return replace(data, last_success=dt_util.now())

    def _serve_last_good_data(self, err: UpdateFailed) -> DominionEnergyData:
        """Fall back to the last good payload, or re-raise once it is stale."""
        self._consecutive_failures += 1

        cached = self.data
        now = dt_util.now()
        if cached is None or not stale_data_is_serviceable(cached.last_success, now):
            raise err

        # Loud once, quiet after: an hourly poll against a source that
        # publishes daily can fail for a while without anything being wrong at
        # this end, and repeating the same warning 20 times helps nobody.
        age = now - cached.last_success if cached.last_success else None
        log = _LOGGER.warning if self._consecutive_failures == 1 else _LOGGER.debug
        log(
            "Update failed (%s); still serving data for %s, last refreshed %s ago "
            "(%d consecutive failures)",
            err,
            cached.data_date,
            age,
            self._consecutive_failures,
        )
        return cached

    async def _async_fetch_data(self, *, allow_reauth: bool) -> DominionEnergyData:
        """Fetch data from the API.

        Note: The Dominion Energy API only provides data for completed days,
        so we always fetch yesterday's data (the most recent complete day).

        Args:
            allow_reauth: Whether a token expiry may trigger an automatic
                re-authentication followed by a single retry. The retry passes
                False so a repeatedly-expiring token cannot recurse forever.
        """
        if self._client is None:
            await self._async_setup()

        assert self._client is not None

        account_number = self.config_entry.data[CONF_ACCOUNT_NUMBER]
        meter_number = self.config_entry.data[CONF_METER_NUMBER]

        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)

        # New data is published at most once a day, and every fetch below is a
        # server-side Excel export behind a WAF. Skip the whole cycle while the
        # target day hasn't advanced since the last complete fetch. The cache
        # lives on the coordinator, so a reload (e.g. after an options change)
        # always refetches.
        cached = self.data
        if cached is not None and self._cached_data_date == yesterday:
            _LOGGER.debug("Data for %s already fetched, skipping API calls", yesterday)
            return cached

        # Handle month boundary: determine which month's data we're working with
        if yesterday.month != today.month:
            # Yesterday was last day of previous month
            month_start = yesterday.replace(day=1)
        else:
            # Normal case: yesterday is in current month
            month_start = today.replace(day=1)

        try:
            # Fetch the bill forecast first: its billing period bounds decide
            # how far back the interval window has to reach.
            try:
                bill_forecast = await self._client.async_get_bill_forecast(
                    account_number=account_number,
                )
            except ApiError as err:
                _LOGGER.warning(
                    "Could not fetch bill forecast: %s", self._describe(err)
                )
                bill_forecast = None

            # One interval fetch covering everything the sensors need, sliced
            # locally afterwards. Dominion reads meters mid-month, so the
            # current billing period usually starts before the calendar month
            # and the window has to be widened to cover it. Each fetch is a
            # server-side Excel export behind a WAF and the API returns the
            # same ~68 day workbook whatever range is asked for, so one wide
            # fetch is strictly cheaper than the two narrow ones it replaces.
            period_start = bill_forecast.current_period_start if bill_forecast else None
            window_start = month_start
            if period_start is not None and period_start < window_start:
                window_start = period_start
            window_start = min(window_start, yesterday)

            # Sorted once here so every slice below is chronological: tiered
            # pricing accumulates in list order, and `latest` is the last
            # element. The API returns rows in order today, but a workbook that
            # came back shuffled would silently mis-tier the whole period.
            window_intervals = sorted(
                await self._client.async_get_interval_usage(
                    account_number=account_number,
                    meter_number=meter_number,
                    start_date=window_start,
                    end_date=yesterday,
                ),
                key=lambda i: i.timestamp,
            )

            # The API hands back its whole ~68 day workbook whatever range was
            # asked for, so every slice is bounded on both sides.
            intervals = [i for i in window_intervals if i.timestamp.date() == yesterday]
            monthly_intervals = [
                i
                for i in window_intervals
                if month_start <= i.timestamp.date() <= yesterday
            ]
            period_intervals = (
                []
                if period_start is None
                else [
                    i
                    for i in window_intervals
                    if period_start <= i.timestamp.date() <= yesterday
                ]
            )

            daily_total = sum(i.consumption for i in intervals)
            monthly_total = sum(i.consumption for i in monthly_intervals)

            # Excess generation (upstream issue #11). Design follows upstream
            # PR #18 by @emerssso, rebased onto the statistics helpers this
            # branch introduced:
            # https://github.com/YeomansIII/ha-dominion-energy/pull/18
            # That PR's README calls the statistic `_energy_excess_generation`
            # while its code emits `_energy_generation`; we use the latter.
            daily_generation_total = sum(generation_of(i) for i in intervals)
            monthly_generation_total = sum(generation_of(i) for i in monthly_intervals)
            has_generation = any(generation_of(i) > 0 for i in window_intervals)

            # Calculate costs
            daily_cost = self._calculate_cost(intervals, bill_forecast)
            monthly_cost = self._calculate_cost(monthly_intervals, bill_forecast)

            (
                period_to_date_usage,
                period_to_date_cost,
                projected_period_usage,
                projected_period_cost,
            ) = self._period_projection(period_intervals, bill_forecast)

            projected_bill = self._projected_bill(
                projected_period_usage, bill_forecast, dt_util.now().date()
            )
            period_end_date = (
                billing_period_end(
                    bill_forecast.current_period_start,
                    bill_forecast.current_period_end,
                    DEFAULT_BILLING_PERIOD_DAYS,
                    dt_util.now().date(),
                )
                if bill_forecast
                else None
            )

            # Built from the whole fetched window, not the month or period
            # slices: the API returns its full ~68 day workbook whatever range
            # was asked for, so the history is free and already in hand.
            profile = usage_profile(window_intervals, through=yesterday)
            day_comparison = compare_to_typical_day(window_intervals, day=yesterday)

            # The baseline is measured in whichever hours this household is
            # habitually quietest, which only the profile knows -- so no
            # profile, no baseline. Both need about a week of data anyway.
            baseline = None
            if profile is not None:
                hvac_windows = await self._async_hvac_windows(
                    yesterday - timedelta(days=BASELINE_DAYS - 1), yesterday
                )
                baseline = baseline_load(
                    window_intervals,
                    through=yesterday,
                    quiet_hours=quietest_hours(profile),
                    hvac_windows=hvac_windows,
                )

            (
                rate_check_estimated,
                rate_check_actual,
                rate_check_discrepancy,
            ) = self._rate_check(bill_forecast)

            latest = intervals[-1] if intervals else None

            # Insert/update external statistics for Energy Dashboard
            statistics_settled = await self._insert_statistics(
                yesterday, bill_forecast, has_generation=has_generation
            )

            # Only cache the day once the API published all of it and the
            # statistics are settled, so a partially published day or a backfill
            # still waiting for the recorder is retried on the next cycle.
            if statistics_settled and day_looks_complete(intervals):
                self._cached_data_date = yesterday
            else:
                self._cached_data_date = None

            return DominionEnergyData(
                intervals=intervals,
                latest_interval=latest,
                daily_total=daily_total,
                monthly_total=monthly_total,
                daily_cost=daily_cost,
                monthly_cost=monthly_cost,
                bill_forecast=bill_forecast,
                data_date=yesterday,
                month_start_date=month_start,
                month_end_date=yesterday,
                daily_generation_total=daily_generation_total,
                monthly_generation_total=monthly_generation_total,
                has_generation=has_generation,
                period_to_date_usage=period_to_date_usage,
                period_to_date_cost=period_to_date_cost,
                projected_period_usage=projected_period_usage,
                projected_period_cost=projected_period_cost,
                projected_bill=projected_bill,
                period_end_date=period_end_date,
                profile=profile,
                day_comparison=day_comparison,
                baseline=baseline,
                period_budget=self.period_budget,
                rate_check_estimated=rate_check_estimated,
                rate_check_actual=rate_check_actual,
                rate_check_discrepancy=rate_check_discrepancy,
            )

        except TokenExpiredError as err:
            return await self._async_handle_token_expiry(err, allow_reauth=allow_reauth)
        except InvalidAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed - please re-authenticate"
            ) from err
        except CannotConnectError as err:
            # dompower's `_async_request` re-raises only
            # `(InvalidAuthError, ApiError, RateLimitError)`. `TokenExpiredError`
            # is a *sibling* of `InvalidAuthError` under `AuthenticationError`,
            # not a subclass, so an expired refresh token falls through to its
            # bare `except Exception` and comes back as `CannotConnectError`.
            #
            # Left unhandled that is not a cosmetic mislabel: the expiry never
            # reaches the branch above, so auto-reauth never runs and
            # `ConfigEntryAuthFailed` is never raised. Home Assistant shows no
            # "Reauthenticate" button and the entry just retries forever, which
            # is what made every expiry look like the integration had broken.
            #
            # The wrap uses `raise ... from err`, so the original is on
            # `__cause__` and can be recovered exactly, without matching on the
            # message text.
            if isinstance(err.__cause__, TokenExpiredError):
                return await self._async_handle_token_expiry(
                    err.__cause__, allow_reauth=allow_reauth
                )
            raise UpdateFailed(f"Cannot connect to Dominion Energy API: {err}") from err
        except ApiError as err:
            if err.status_code in (401, 403):
                raise ConfigEntryAuthFailed(
                    "Authentication failed - please re-authenticate"
                ) from err
            raise UpdateFailed(self._describe(err)) from err

    def _describe(self, err: Exception) -> str:
        """Render an API error, masking this entry's own identifiers."""
        return describe_api_error(
            err,
            secrets=(
                self.config_entry.data.get(CONF_ACCOUNT_NUMBER),
                self.config_entry.data.get(CONF_METER_NUMBER),
            ),
        )

    # Why the fallback is a nominal month and not the last bill's own length:
    # the last bill is a real meter-read-to-meter-read cycle, so borrowing its
    # length looks like the better guess. Measured against 21 consecutive
    # cycles from one account's billing history it is materially worse --
    # 2.25 days mean absolute error against 1.40 for a flat 30, and off by two
    # days or more on 12 cycles out of 20 rather than 8.
    #
    # Cycle length is mean-reverting, not persistent (lag-1 autocorrelation
    # -0.43). Meters are read on a monthly schedule, so a cycle that runs long
    # is followed by a short one that pulls the read date back: 33 days in
    # January 2026 was followed by 28 in February. Carrying the previous
    # length forward propagates that swing in exactly the wrong direction.
    @staticmethod
    def _billing_period_days(
        bill_forecast: BillForecast | None, today: date | None = None
    ) -> int:
        """Return the length of the current billing period in days."""
        if bill_forecast is None:
            return billing_period_days(None, None)
        return billing_period_days(
            bill_forecast.current_period_start,
            bill_forecast.current_period_end,
            DEFAULT_BILLING_PERIOD_DAYS,
            today,
        )

    @staticmethod
    def _period_start_of(
        bill_forecast: BillForecast | None,
    ) -> Callable[[date], date]:
        """Return a function mapping a date to the start of its billing period."""
        anchor = bill_forecast.current_period_start if bill_forecast else None
        return partial(billing_period_start, anchor=anchor)

    def _period_projection(
        self,
        period_intervals: list[IntervalUsageData],
        bill_forecast: BillForecast | None,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Return billing-period usage/cost to date and the projected totals.

        Usage is projected first and only then priced, because cost is not
        linear in usage: Schedule 1 charges distribution and generation at one
        rate up to 800 kWh in a billing period and another beyond it, and the
        customer charge is a flat monthly amount. Scaling up a part-period cost
        would smear both of those across the projection.

        - Schedule 1: the projected kWh is run through
          `rates.calculate_schedule1_period_bill()`, which applies the tiers to
          the period total the way a paper bill does and adds the customer
          charge exactly once.
        - Every other mode prices each kWh independently of how many came
          before it, so the projected cost is the period-to-date cost scaled by
          the usage ratio. For time-of-use that also preserves the observed
          peak/off-peak split, which a flat rate multiplication would lose.

        Returns `(to_date_usage, to_date_cost, projected_usage,
        projected_cost)`, all None without a bill forecast to anchor the
        period.
        """
        if bill_forecast is None:
            return None, None, None, None

        usage = sum(i.consumption for i in period_intervals)
        cost = self._calculate_cost(period_intervals, bill_forecast)
        days_observed = len({i.timestamp.date() for i in period_intervals})
        today = dt_util.now().date()

        projected_usage = project_period_usage(
            usage,
            days_observed,
            self._billing_period_days(bill_forecast, today),
        )
        if projected_usage is None:
            return round(usage, 3), cost, None, None

        return (
            round(usage, 3),
            cost,
            round(projected_usage, 3),
            self._project_period_cost(
                projected_usage, usage, cost, bill_forecast, today
            ),
        )

    async def _async_handle_token_expiry(
        self, err: TokenExpiredError, *, allow_reauth: bool
    ) -> DominionEnergyData:
        """Try to recover from an expired refresh token, then hand off.

        Never returns normally: either the retry produces data or
        `ConfigEntryAuthFailed` starts the reauth flow. Raising that is what
        puts the "Reauthenticate" button in front of the user, which is far
        less work for them than the full credentials-and-two-factor round trip
        that Reconfigure demands.
        """
        _LOGGER.info("Refresh token expired, attempting auto-reauth")
        if allow_reauth and await self._async_attempt_reauth():
            # Retry the update once with new tokens, never deeper
            return await self._async_fetch_data(allow_reauth=False)
        raise ConfigEntryAuthFailed(
            "Authentication failed - please re-authenticate"
        ) from err

    async def _async_hvac_windows(
        self, first_day: date, last_day: date
    ) -> list[TimeWindow]:
        """Read when the configured thermostats were actually running.

        Returns an empty list when no climate entities are configured, which
        leaves the baseline unfiltered -- correct for a household whose heating
        and cooling never runs during its quiet hours, and clearly labelled on
        the sensor for one whose does.

        **This must read every recorded row, not just state changes.**
        `hvac_action` is an *attribute*: a thermostat cycles
        idle -> cooling -> idle all night while its state sits on `heat_cool`
        throughout, so `last_changed` never moves and only `last_updated` does.
        `state_changes_during_period()` returns state-*value* changes, which on
        a real ecobee meant two samples across a week instead of dozens of
        compressor cycles -- the filter silently did almost nothing and the
        baseline reported the air conditioner. `get_significant_states()` with
        `significant_changes_only=False` returns the attribute-only rows too.

        One call covers every entity; the result is keyed by entity ID, and the
        per-entity lists must stay separate because `hvac_active_windows()`
        runs each sample to the next one.

        Nothing is cached: `_async_fetch_data` already short-circuits once the
        day's data is complete, so this runs at most once per day. The window
        spans the days being measured, well inside a default `purge_keep_days`
        of 10.
        """
        entity_ids: list[str] = list(
            self.config_entry.options.get(CONF_HVAC_ENTITIES) or []
        )
        if not entity_ids:
            return []

        start = dt_util.start_of_local_day(first_day)
        end = dt_util.start_of_local_day(last_day) + timedelta(days=1)

        recorder = get_instance(self.hass)
        history = await recorder.async_add_executor_job(
            partial(
                get_significant_states,
                self.hass,
                start,
                end,
                entity_ids,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=False,
            )
        )

        windows: list[TimeWindow] = []
        for entity_id in entity_ids:
            # `get_significant_states` is typed as returning `State | dict`:
            # it hands back bare dicts under `minimal_response` or
            # `compressed_state_format`, and a compressed row carries no
            # attributes, so `hvac_action` would be invisible. Neither is
            # requested above, so narrowing here should never drop anything --
            # but it is the one place a future default change would otherwise
            # turn the filter silently inert again, which is exactly the
            # failure 1.5.0 shipped.
            states = [
                state
                for state in history.get(entity_id) or []
                if isinstance(state, State)
            ]
            if not states:
                _LOGGER.debug(
                    "No usable recorded history for %s; its runtime cannot be "
                    "excluded from the baseline",
                    entity_id,
                )
                continue
            windows.extend(
                hvac_active_windows(
                    [
                        (
                            state.last_updated,
                            state.state,
                            state.attributes.get("hvac_action"),
                        )
                        for state in states
                    ],
                    until=end,
                )
            )

        return merge_windows(windows)

    @staticmethod
    def _projected_bill(
        projected_usage: float | None,
        bill_forecast: BillForecast | None,
        today: date | None = None,
    ) -> PeriodBill | None:
        """Break a projected billing period into its Schedule 1 components.

        Computed for every cost mode, not just `schedule_1`. The other modes
        are deliberately crude — one blended rate, or peak/off-peak — so none
        of them can answer "what am I actually paying for?". The tariff can,
        and the answer is worth showing even to someone whose cost sensors are
        driven by the derived rate off their last bill: only the split between
        distribution, generation, riders and tax explains why a bill moves when
        usage did not.

        The caller is responsible for saying which of the two totals a
        dashboard is looking at — see the `breakdown_basis` attribute in
        `sensor.py`.

        The period end is run through `billing_period_end()` first. Only the
        midpoint of the period is used from these two dates -- to pick the
        season and the effective rate schedule -- and a period reported as
        ending today rather than on the next read date drags that midpoint
        backwards by half the remaining cycle.

        `today` is a parameter rather than a `dt_util.now()` call so this stays
        decidable without a clock, and so the truncation it guards against can
        be asserted directly. Callers on the update path pass the real date.
        """
        if projected_usage is None or bill_forecast is None:
            return None
        # `dompower` reports a period bound the API omitted as None rather
        # than substituting today's date. Without a start there is no midpoint
        # and so no season and no rate schedule; a breakdown built off a
        # guessed one would be worse than no breakdown.
        period_start = bill_forecast.current_period_start
        if period_start is None:
            return None
        return calculate_schedule1_period_bill(
            projected_usage,
            period_start,
            billing_period_end(
                period_start,
                bill_forecast.current_period_end,
                DEFAULT_BILLING_PERIOD_DAYS,
                today,
            ),
        )

    def _project_period_cost(
        self,
        projected_usage: float,
        period_to_date_usage: float,
        period_to_date_cost: float,
        bill_forecast: BillForecast,
        today: date | None = None,
    ) -> float | None:
        """Price a projected period usage in the configured cost mode.

        `today` must be the same date `_projected_bill()` is given on the
        update path, or the shown cost and the breakdown behind it would be
        priced over two different periods.
        """
        cost_mode = self.config_entry.options.get(CONF_COST_MODE, COST_MODE_API)

        if cost_mode == COST_MODE_SCHEDULE_1:
            period_bill = self._projected_bill(projected_usage, bill_forecast, today)
            assert period_bill is not None  # both arguments are non-None here
            return round(period_bill.total, 2)

        if period_to_date_usage <= 0:
            return None
        return round(period_to_date_cost * projected_usage / period_to_date_usage, 2)

    @staticmethod
    def _rate_check(
        bill_forecast: BillForecast | None,
    ) -> tuple[float | None, float | None, float | None]:
        """Check our Schedule 1 maths against the last bill Dominion issued."""
        if bill_forecast is None or bill_forecast.last_bill is None:
            return None, None, None

        last_bill = bill_forecast.last_bill
        period_start = last_bill.period_start
        period_end = last_bill.period_end
        if period_start is None or period_end is None:
            # The forecast does not always carry the previous period's dates.
            # Dominion's cycle is monthly, so the period ending where the
            # current one starts is a good enough stand-in for picking the
            # season and the effective rate schedule.
            period_end = bill_forecast.current_period_start
            if period_end is None:
                # Nor the current period's start: there is no date anywhere in
                # this forecast to hang a season off, and rate_check() would
                # refuse the same way one step later.
                return None, None, None
            period_start = shift_months(period_end, -1)

        return rate_check(last_bill.charges, last_bill.usage, period_start, period_end)

    def _calculate_cost(
        self,
        intervals: list[IntervalUsageData],
        bill_forecast: BillForecast | None,
    ) -> float:
        """Calculate cost based on configured mode."""
        if not intervals:
            return 0.0

        total_kwh = sum(i.consumption for i in intervals)
        options = self.config_entry.options
        cost_mode = options.get(CONF_COST_MODE, COST_MODE_API)

        if cost_mode == COST_MODE_SCHEDULE_1:
            # VA Schedule 1 with cumulative kWh tracking for tiered pricing.
            # The customer charge is prorated over the whole billing period, so
            # a partial window (a single day, or the month to date) only carries
            # its proportional share. Statistics use the same period length so
            # the sensors and the Energy Dashboard agree.
            # Each interval is priced with the schedule in effect on its own
            # date, since a window can straddle a rate change.
            cost = 0.0
            cumulative_kwh = 0.0
            billing_days = self._billing_period_days(
                bill_forecast, dt_util.now().date()
            )
            for interval in intervals:
                cost += calculate_schedule1_interval_cost(
                    interval.consumption,
                    interval.timestamp,
                    cumulative_kwh,
                    get_schedule_for_date(interval.timestamp),
                    billing_period_days=billing_days,
                )
                cumulative_kwh += interval.consumption
            return round(cost, 2)

        if cost_mode == COST_MODE_API and bill_forecast:
            # Derive rate from last bill: charges / usage
            rate = bill_forecast.derived_rate
            if rate:
                return round(total_kwh * rate, 2)
            # Fallback to fixed if no derived rate available
            return round(
                total_kwh * options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE), 2
            )

        elif cost_mode == COST_MODE_TOU:
            # Time-of-use calculation
            cost = 0.0
            peak_start = options.get(CONF_PEAK_START_HOUR, DEFAULT_PEAK_START_HOUR)
            peak_end = options.get(CONF_PEAK_END_HOUR, DEFAULT_PEAK_END_HOUR)
            peak_rate = options.get(CONF_PEAK_RATE, DEFAULT_PEAK_RATE)
            off_peak_rate = options.get(CONF_OFF_PEAK_RATE, DEFAULT_OFF_PEAK_RATE)

            for interval in intervals:
                hour = interval.timestamp.hour
                if peak_start <= hour < peak_end:
                    cost += interval.consumption * peak_rate
                else:
                    cost += interval.consumption * off_peak_rate
            return round(cost, 2)

        else:
            # Fixed rate
            fixed_rate = options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE)
            return round(total_kwh * fixed_rate, 2)

    def _calculate_interval_cost(
        self,
        interval: UsageInterval,
        bill_forecast: BillForecast | None,
        cumulative_kwh_before: float = 0.0,
        billing_period_days: int = 30,
    ) -> float:
        """Calculate cost for a single interval based on configured mode.

        Used for building cost statistics alongside consumption statistics.

        Args:
            interval: The interval usage data.
            bill_forecast: Bill forecast for API estimate mode.
            cumulative_kwh_before: Cumulative kWh before this interval in the
                billing period. Used by Schedule 1 for tiered pricing.
            billing_period_days: Days in billing period. Used by Schedule 1
                for prorating the customer charge.
        """
        options = self.config_entry.options
        cost_mode = options.get(CONF_COST_MODE, COST_MODE_API)

        if cost_mode == COST_MODE_SCHEDULE_1:
            return calculate_schedule1_interval_cost(
                interval.consumption,
                interval.timestamp,
                cumulative_kwh_before,
                get_schedule_for_date(interval.timestamp),
                billing_period_days=billing_period_days,
            )

        if cost_mode == COST_MODE_API and bill_forecast:
            rate = bill_forecast.derived_rate
            if rate:
                return interval.consumption * rate
            # Fallback to fixed if no derived rate available
            return interval.consumption * options.get(
                CONF_FIXED_RATE, DEFAULT_FIXED_RATE
            )

        elif cost_mode == COST_MODE_TOU:
            peak_start = options.get(CONF_PEAK_START_HOUR, DEFAULT_PEAK_START_HOUR)
            peak_end = options.get(CONF_PEAK_END_HOUR, DEFAULT_PEAK_END_HOUR)
            peak_rate = options.get(CONF_PEAK_RATE, DEFAULT_PEAK_RATE)
            off_peak_rate = options.get(CONF_OFF_PEAK_RATE, DEFAULT_OFF_PEAK_RATE)

            hour = interval.timestamp.hour
            if peak_start <= hour < peak_end:
                return interval.consumption * peak_rate
            return interval.consumption * off_peak_rate

        else:
            # Fixed rate
            fixed_rate = options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE)
            return interval.consumption * fixed_rate

    def _aggregate_hourly(
        self,
        intervals: list[IntervalUsageData],
        bill_forecast: BillForecast | None,
    ) -> tuple[dict[datetime, float], dict[datetime, float]]:
        """Aggregate intervals into hourly consumption and cost buckets.

        Tiered pricing accumulates over the billing period reported by the bill
        forecast, and the customer charge is prorated over that same period, so
        statistics agree with the sensors.
        """
        billing_days = self._billing_period_days(bill_forecast, dt_util.now().date())
        return aggregate_hourly(
            intervals,
            lambda interval, cumulative_before: self._calculate_interval_cost(
                interval,
                bill_forecast,
                cumulative_kwh_before=cumulative_before,
                billing_period_days=billing_days,
            ),
            self._period_start_of(bill_forecast),
        )

    @staticmethod
    def _statistic_rows(
        hourly: dict[datetime, float], start_sum: float = 0.0
    ) -> list[StatisticData]:
        """Turn local hourly buckets into recorder rows with continuous sums."""
        return [
            StatisticData(start=row.start, state=row.state, sum=row.sum)
            for row in build_cumulative_statistics(
                deduplicate_hourly_by_utc(hourly), start_sum
            )
        ]

    def _energy_metadata(self, statistic_id: str, kind: str) -> StatisticMetaData:
        """Build kWh statistic metadata for the consumption/generation streams."""
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self._statistic_name(kind),
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class="energy",
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    def _cost_metadata(self, statistic_id: str) -> StatisticMetaData:
        """Build cost statistic metadata (following the Opower pattern)."""
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=self._statistic_name("cost"),
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )

    def _cost_options_signature(self) -> str:
        """Build a signature of the options that affect cost calculation.

        Persisted in the config entry so recorded cost statistics are only
        rebuilt when the user actually changes how cost is calculated.
        """
        options = self.config_entry.options
        cost_mode = options.get(CONF_COST_MODE, COST_MODE_API)
        parts: list[Any] = [cost_mode]
        if cost_mode == COST_MODE_TOU:
            parts += [
                options.get(CONF_PEAK_RATE, DEFAULT_PEAK_RATE),
                options.get(CONF_OFF_PEAK_RATE, DEFAULT_OFF_PEAK_RATE),
                options.get(CONF_PEAK_START_HOUR, DEFAULT_PEAK_START_HOUR),
                options.get(CONF_PEAK_END_HOUR, DEFAULT_PEAK_END_HOUR),
            ]
        elif cost_mode != COST_MODE_SCHEDULE_1:
            # Fixed rate mode, and the fallback used by API estimate mode
            parts.append(options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE))
        return "|".join(str(part) for part in parts)

    def _store_cost_signature(self, signature: str) -> None:
        """Persist the cost options the recorded cost statistics were built with."""
        if self.config_entry.data.get(CONF_COST_SIGNATURE) == signature:
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_COST_SIGNATURE: signature},
        )

    async def _async_last_statistic(self, statistic_id: str) -> dict:
        """Return `get_last_statistics`' one-row result for a statistic.

        Empty (falsy) when the statistic has never been recorded.
        """
        return await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )

    async def _insert_statistics(
        self,
        data_date: date,
        bill_forecast: BillForecast | None,
        *,
        has_generation: bool,
    ) -> bool:
        """Insert or update external statistics for Energy Dashboard integration.

        Statistics are stored with hourly granularity, aggregated from 30-minute
        interval data. On first setup, backfills BACKFILL_DAYS days of history.

        Creates up to three statistics, where the prefix is resolved by
        `resolve_statistic_id_prefix()`:
        - {prefix}_energy_consumption (kWh)
        - {prefix}_energy_cost (USD)
        - {prefix}_energy_generation (kWh), only once the meter has actually
          reported exported energy - a flat zero series on every non-solar
          install would just clutter the Energy Dashboard's source pickers.

        Returns True when the statistics are settled through data_date, and
        False while something still needs another cycle (a backfill waiting for
        the recorder to commit, a rebuild, or a failed fetch).
        """
        consumption_stat_id, cost_stat_id, generation_stat_id = self._statistic_ids()
        _LOGGER.debug(
            "Checking statistics for %s, %s and %s (data_date=%s)",
            consumption_stat_id,
            cost_stat_id,
            generation_stat_id,
            data_date,
        )

        last_stat = await self._async_last_statistic(consumption_stat_id)
        last_cost_stat = await self._async_last_statistic(cost_stat_id)
        last_generation_stat = await self._async_last_statistic(generation_stat_id)

        consumption_exists = bool(last_stat.get(consumption_stat_id))
        cost_exists = bool(last_cost_stat.get(cost_stat_id))
        generation_exists = bool(last_generation_stat.get(generation_stat_id))
        # Once the stream exists it keeps being maintained, even through a
        # stretch of days that happened to export nothing.
        generation_wanted = has_generation or generation_exists
        cost_signature = self._cost_options_signature()

        if not consumption_exists:
            # No consumption statistics - backfill everything
            if self._backfill_initiated:
                # Backfill was already started, waiting for recorder to commit
                _LOGGER.debug(
                    "Backfill already initiated for %s, waiting for recorder to commit",
                    consumption_stat_id,
                )
                return False

            _LOGGER.info(
                "First statistics update for %s - backfilling %d days of data",
                consumption_stat_id,
                BACKFILL_DAYS,
            )
            self._backfill_initiated = True
            await self._backfill_statistics(
                consumption_stat_id=consumption_stat_id,
                cost_stat_id=cost_stat_id,
                generation_stat_id=generation_stat_id if generation_wanted else None,
                bill_forecast=bill_forecast,
            )
            self._store_cost_signature(cost_signature)
            return False

        cost_missing = not cost_exists
        generation_missing = generation_wanted and not generation_exists

        if cost_missing or generation_missing:
            # Consumption exists but one of the other streams doesn't. This is
            # the upgrade path for cost, and the path a site takes when solar
            # first starts exporting.
            if self._backfill_initiated:
                _LOGGER.debug(
                    "Backfill already initiated for %s, waiting for recorder to commit",
                    cost_stat_id,
                )
                return False

            _LOGGER.info(
                "Backfilling %d days for missing statistics (cost: %s, generation: %s)",
                BACKFILL_DAYS,
                cost_missing,
                generation_missing,
            )
            self._backfill_initiated = True
            await self._backfill_statistics(
                consumption_stat_id=None,  # Don't backfill consumption
                cost_stat_id=cost_stat_id if cost_missing else None,
                generation_stat_id=generation_stat_id if generation_missing else None,
                bill_forecast=bill_forecast,
            )
            self._store_cost_signature(cost_signature)
            return False

        # All expected statistics exist - reset backfill flag
        self._backfill_initiated = False

        if self.config_entry.data.get(CONF_COST_SIGNATURE) != cost_signature:
            # The cost calculation changed (or this is the first run after an
            # upgrade), so the recorded cost history no longer matches the
            # configured mode and has to be recomputed. Consumption and
            # generation do not depend on the cost options, so they are left
            # alone and stay in step with the next incremental update.
            await self._rebuild_cost_statistics(cost_stat_id, bill_forecast)
            self._store_cost_signature(cost_signature)
            # The rebuild already covers everything through data_date; let the
            # next cycle do the incremental update rather than chaining onto
            # sums the recorder has not committed yet.
            return False

        _LOGGER.debug(
            "Found existing statistics for %s, performing incremental update",
            consumption_stat_id,
        )
        return await self._update_statistics(
            consumption_stat_id,
            cost_stat_id,
            generation_stat_id if generation_exists else None,
            last_stat,
            last_cost_stat,
            data_date,
            bill_forecast,
        )

    @staticmethod
    def _filter_incomplete_days(
        intervals: list[IntervalUsageData],
    ) -> list[IntervalUsageData]:
        """Filter out days with zero or suspiciously incomplete data."""
        intervals, skipped_days = filter_incomplete_days_allowing_generation(intervals)
        if skipped_days:
            _LOGGER.warning(
                "Skipping %d days with missing/incomplete data: %s",
                len(skipped_days),
                skipped_days,
            )
        return intervals

    @staticmethod
    def _backfill_window(today: date) -> tuple[date, date]:
        """Return the (start, end) dates of the historical backfill window."""
        return today - timedelta(days=BACKFILL_DAYS), today - timedelta(days=1)

    async def _get_sum_before(
        self, stat_id: str, before_utc: datetime, count: int = 48
    ) -> float | None:
        """Get the cumulative sum just before a given UTC timestamp.

        Used when rewriting a window of statistics, so the rebuilt cumulative
        sum chain continues from the row immediately before that window.

        Args:
            stat_id: The statistic to look up.
            before_utc: Only rows starting strictly before this are considered.
            count: How many recent rows to scan (48 covers ~2 days of hours).
        """
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, count, stat_id, True, {"sum"}
        )
        rows = last_stats.get(stat_id)
        if not rows:
            return None

        # get_last_statistics returns rows newest first, but don't depend on the
        # ordering: explicitly pick the newest row starting before the cutoff.
        best_start: datetime | None = None
        best_sum: float | None = None
        for stat_data in rows:
            stat_dt = _stat_start_utc(stat_data)
            if stat_dt >= before_utc:
                continue
            if best_start is None or stat_dt > best_start:
                best_start = stat_dt
                best_sum = float(stat_data.get("sum") or 0)

        return best_sum

    async def _find_last_complete_day_stat(
        self,
        consumption_stat_id: str,
        cost_stat_id: str,
    ) -> tuple[date | None, float, float]:
        """Walk backwards to find the last stat from a fully-populated day.

        A fully-populated day has non-zero data extending to at least hour 22
        local time. Days with data only at hour 00:00 are artifacts from
        a previous buggy version and should be skipped.

        Returns (date, consumption_sum, cost_sum) of the last stat from a
        complete day, or (None, 0.0, 0.0) if none found.
        """
        local_tz = dt_util.get_default_time_zone()

        # Get enough stats to cover the backfill window (~68 days * 24 hours)
        num_stats = BACKFILL_DAYS * 24
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            num_stats,
            consumption_stat_id,
            True,
            {"state", "sum"},
        )
        if not last_stats.get(consumption_stat_id):
            return None, 0.0, 0.0

        # Walk backwards to find the last stat with state > 0 at hour >= 22
        # (indicating the end of a fully-populated day, not a sparse artifact).
        # Sort explicitly rather than relying on the order get_last_statistics
        # happens to return rows in.
        newest_first = sorted(
            last_stats[consumption_stat_id], key=_stat_start_utc, reverse=True
        )
        for stat_data in newest_first:
            state = float(stat_data.get("state") or 0)
            if state <= 0:
                continue

            stat_dt = _stat_start_utc(stat_data)
            stat_local = stat_dt.astimezone(local_tz)

            if stat_local.hour < 22:
                # Non-zero but early in the day — could be a sparse artifact.
                # Keep looking further back for a complete day.
                continue

            consumption_sum = float(stat_data.get("sum") or 0)

            # Get the matching cost sum
            cost_sum = 0.0
            last_cost_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                num_stats,
                cost_stat_id,
                True,
                {"sum"},
            )
            if last_cost_stats.get(cost_stat_id):
                for cost_data in sorted(
                    last_cost_stats[cost_stat_id], key=_stat_start_utc, reverse=True
                ):
                    if _stat_start_utc(cost_data) <= stat_dt:
                        cost_sum = float(cost_data.get("sum") or 0)
                        break

            return stat_local.date(), consumption_sum, cost_sum

        return None, 0.0, 0.0

    async def _backfill_statistics(
        self,
        consumption_stat_id: str | None,
        cost_stat_id: str | None,
        generation_stat_id: str | None,
        bill_forecast: BillForecast | None,
        cost_start_sum: float = 0.0,
    ) -> None:
        """Backfill historical statistics for initial setup or upgrade.

        Args:
            consumption_stat_id: If provided, backfill consumption statistics.
            cost_stat_id: If provided, backfill cost statistics.
            generation_stat_id: If provided, backfill generation statistics.
            bill_forecast: Bill forecast used for cost calculation.
            cost_start_sum: Cumulative cost sum immediately before the window.
                Non-zero when rewriting cost history that continues an older
                chain of statistics.

        At least one stat ID must be provided.
        """
        assert self._client is not None
        assert consumption_stat_id or cost_stat_id or generation_stat_id

        start_date, end_date = self._backfill_window(dt_util.now().date())

        _LOGGER.debug("Backfilling statistics from %s to %s", start_date, end_date)

        try:
            intervals = await self._client.async_get_interval_usage(
                account_number=self.config_entry.data[CONF_ACCOUNT_NUMBER],
                meter_number=self.config_entry.data[CONF_METER_NUMBER],
                start_date=start_date,
                end_date=end_date,
            )
        except ApiError as err:
            _LOGGER.warning("Could not fetch backfill data: %s", err)
            return

        if not intervals:
            _LOGGER.warning("No interval data available for backfill")
            return

        intervals = self._filter_incomplete_days(intervals)

        if not intervals:
            _LOGGER.warning("No valid interval data after filtering incomplete days")
            return

        # Group intervals by hour for hourly statistics. Tiered pricing and the
        # prorated customer charge follow the billing period from the forecast,
        # not the calendar month (upstream issue #15).
        hourly_consumption, hourly_cost = self._aggregate_hourly(
            intervals, bill_forecast
        )

        # Insert consumption statistics if requested
        consumption_statistics = self._statistic_rows(hourly_consumption)
        if consumption_stat_id and consumption_statistics:
            _LOGGER.info(
                "Adding %d hourly consumption statistics for %s",
                len(consumption_statistics),
                consumption_stat_id,
            )
            async_add_external_statistics(
                self.hass,
                self._energy_metadata(consumption_stat_id, "consumption"),
                consumption_statistics,
            )

        # Insert cost statistics if requested
        cost_statistics = self._statistic_rows(hourly_cost, cost_start_sum)
        if cost_stat_id and cost_statistics:
            _LOGGER.info(
                "Adding %d hourly cost statistics for %s",
                len(cost_statistics),
                cost_stat_id,
            )
            async_add_external_statistics(
                self.hass, self._cost_metadata(cost_stat_id), cost_statistics
            )

        # Insert generation statistics if requested. The stream is always
        # created from scratch, so it starts from a zero cumulative sum.
        if generation_stat_id:
            generation_statistics = self._statistic_rows(
                aggregate_hourly_generation(intervals)
            )
            if generation_statistics:
                _LOGGER.info(
                    "Adding %d hourly generation statistics for %s",
                    len(generation_statistics),
                    generation_stat_id,
                )
                async_add_external_statistics(
                    self.hass,
                    self._energy_metadata(generation_stat_id, "generation"),
                    generation_statistics,
                )

    async def _rebuild_cost_statistics(
        self,
        cost_stat_id: str,
        bill_forecast: BillForecast | None,
    ) -> None:
        """Recompute cost statistics for the whole available window.

        Called when the cost-affecting options change, so the Energy Dashboard
        cost history reflects the newly configured mode instead of staying
        frozen at whatever was configured first.

        async_add_external_statistics overwrites rows keyed by
        (statistic_id, start), so rewriting a window is safe as long as the
        cumulative sum chain stays continuous. The rebuild therefore starts from
        the sum of the statistic immediately before the window rather than zero.
        """
        start_date, _end_date = self._backfill_window(dt_util.now().date())
        window_start_utc = dt_util.as_utc(dt_util.start_of_local_day(start_date))
        cost_sum_before = await self._get_sum_before(
            cost_stat_id,
            window_start_utc,
            # Enough rows to look past the window being rewritten
            count=(BACKFILL_DAYS + 1) * 24,
        )

        _LOGGER.info(
            "Cost options changed - rebuilding cost statistics for %s from %s "
            "(starting sum=%.3f)",
            cost_stat_id,
            start_date,
            cost_sum_before or 0.0,
        )
        await self._backfill_statistics(
            # Neither consumption nor generation depends on the cost options
            consumption_stat_id=None,
            cost_stat_id=cost_stat_id,
            generation_stat_id=None,
            bill_forecast=bill_forecast,
            cost_start_sum=cost_sum_before or 0.0,
        )

    async def _update_statistics(
        self,
        consumption_stat_id: str,
        cost_stat_id: str,
        generation_stat_id: str | None,
        last_stat: dict,
        last_cost_stat: dict,
        data_date: date,
        bill_forecast: BillForecast | None,
    ) -> bool:
        """Update statistics with new data since last recorded statistic.

        The window to rewrite is decided from the consumption stream, which is
        the one with the self-healing rules; the cost and generation chains
        then continue from whatever they held immediately before that window.

        Returns True when the statistics are up to date through data_date.
        """
        assert self._client is not None

        try:
            # Get the last recorded statistic time and sum for consumption
            last_stat_data = last_stat[consumption_stat_id][0]
            last_stat_start = last_stat_data["start"]
            consumption_sum = float(last_stat_data.get("sum") or 0)

            _LOGGER.debug(
                "Last statistic for %s: start=%s (type=%s), sum=%.3f",
                consumption_stat_id,
                last_stat_start,
                type(last_stat_start).__name__,
                consumption_sum,
            )

            # Convert to datetime for comparison
            if isinstance(last_stat_start, (int, float)):
                last_stat_dt = datetime.fromtimestamp(last_stat_start, tz=dt_util.UTC)
            else:
                last_stat_dt = last_stat_start

            # Convert to local timezone for date comparison.
            # The dompower library returns timestamps in America/New_York timezone,
            # which are then converted to UTC when stored. We convert back to local
            # to get the correct date for comparison with data_date (which is local).
            local_tz = dt_util.get_default_time_zone()
            last_stat_local = last_stat_dt.astimezone(local_tz)
            last_stat_date = last_stat_local.date()

            _LOGGER.debug(
                "Date comparison: last_stat_dt=%s, last_stat_local=%s, "
                "last_stat_date=%s, data_date=%s",
                last_stat_dt,
                last_stat_local,
                last_stat_date,
                data_date,
            )

        except (KeyError, IndexError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Error parsing last statistic for %s: %s (last_stat=%s)",
                consumption_stat_id,
                err,
                last_stat,
            )
            return False

        # Get the last cost sum (default to 0 if cost stats don't exist yet)
        cost_sum = 0.0
        if last_cost_stat.get(cost_stat_id):
            try:
                cost_sum = float(last_cost_stat[cost_stat_id][0].get("sum") or 0)
            except (KeyError, IndexError, TypeError, ValueError):
                _LOGGER.debug("Could not get last cost sum, starting from 0")

        # Check if we need to fetch new data.
        # Also detect incomplete days: if the last stat is on data_date but
        # doesn't cover the full day (last local hour < 22), re-fetch that day
        # to fill in missing hours. This self-heals days that were previously
        # recorded with incomplete/zero data from the API.

        # Detect stale zero-value stats from a previous buggy version.
        # If the last stat has state=0, walk backwards through recent stats
        # to find the last non-zero entry, then re-fetch from that point.
        last_state = float(last_stat_data.get("state") or 0)
        if last_state == 0 and last_stat_date >= data_date:
            _LOGGER.info(
                "Last statistic has state=0 at %s — scanning for last non-zero entry",
                last_stat_date,
            )
            (
                last_good_date,
                last_good_sum,
                last_good_cost_sum,
            ) = await self._find_last_complete_day_stat(
                consumption_stat_id, cost_stat_id
            )
            if last_good_date is not None:
                _LOGGER.info(
                    "Last non-zero statistic on %s (sum=%.3f). "
                    "Re-fetching from %s to %s to heal stale zeros.",
                    last_good_date,
                    last_good_sum,
                    last_good_date + timedelta(days=1),
                    data_date,
                )
                start_date = last_good_date + timedelta(days=1)
                consumption_sum = last_good_sum
                cost_sum = last_good_cost_sum
            else:
                # All stats are zero — re-fetch everything
                _LOGGER.warning(
                    "All recent statistics are zero. Re-fetching from %d days ago.",
                    BACKFILL_DAYS,
                )
                start_date = dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
                consumption_sum = 0.0
                cost_sum = 0.0
        elif last_stat_date > data_date:
            _LOGGER.debug(
                "Statistics already up to date: last_stat_date=%s > data_date=%s",
                last_stat_date,
                data_date,
            )
            return True

        elif last_stat_date == data_date and last_stat_local.hour >= 22:
            _LOGGER.debug(
                "Statistics already up to date for %s (last hour: %d)",
                data_date,
                last_stat_local.hour,
            )
            return True

        elif last_stat_date == data_date:
            # Incomplete day detected — re-fetch from this day.
            # We need the cumulative sum from BEFORE the incomplete day
            # since we'll be replacing all of its statistics.
            _LOGGER.info(
                "Incomplete statistics for %s (last hour: %d), re-fetching",
                last_stat_date,
                last_stat_local.hour,
            )
            start_date = last_stat_date

            # Get the sum at the end of the day BEFORE the incomplete day
            # by subtracting the state values we're about to replace
            day_start_utc = dt_util.as_utc(
                last_stat_local.replace(hour=0, minute=0, second=0, microsecond=0)
            )
            consumption_sum_before = await self._get_sum_before(
                consumption_stat_id, day_start_utc
            )
            cost_sum_before = await self._get_sum_before(cost_stat_id, day_start_utc)
            if consumption_sum_before is not None:
                consumption_sum = consumption_sum_before
            if cost_sum_before is not None:
                cost_sum = cost_sum_before
        else:
            # Fetch data from day after last stat to data_date
            start_date = last_stat_date + timedelta(days=1)

        # Safety check: if start_date is older than API data availability (~68 days),
        # limit to BACKFILL_DAYS to avoid requesting unavailable data
        today = dt_util.now().date()
        oldest_available = today - timedelta(days=BACKFILL_DAYS)
        if start_date < oldest_available:
            _LOGGER.warning(
                "Statistics are very stale (last: %s). Limiting fetch to last %d days. "
                "Some historical data may be lost.",
                last_stat_date,
                BACKFILL_DAYS,
            )
            start_date = oldest_available

        if not statistics_window_is_fetchable(start_date, data_date):
            _LOGGER.debug(
                "Statistics already up to date: computed start_date=%s is after "
                "data_date=%s, nothing to fetch",
                start_date,
                data_date,
            )
            return True

        # The generation chain is continued from whatever the recorder holds
        # immediately before the window being (re)written, whichever branch
        # above picked that window. Doing it once here keeps generation in step
        # with consumption without threading a fourth running total through
        # every self-healing branch.
        generation_sum = 0.0
        if generation_stat_id:
            generation_sum = (
                await self._get_sum_before(
                    generation_stat_id,
                    dt_util.as_utc(dt_util.start_of_local_day(start_date)),
                    count=(BACKFILL_DAYS + 1) * 24,
                )
                or 0.0
            )

        _LOGGER.info(
            "Fetching statistics update from %s to %s "
            "(consumption_sum=%.3f, cost_sum=%.3f, generation_sum=%.3f)",
            start_date,
            data_date,
            consumption_sum,
            cost_sum,
            generation_sum,
        )

        try:
            intervals = await self._client.async_get_interval_usage(
                account_number=self.config_entry.data[CONF_ACCOUNT_NUMBER],
                meter_number=self.config_entry.data[CONF_METER_NUMBER],
                start_date=start_date,
                end_date=data_date,
            )
        except ApiError as err:
            _LOGGER.warning("Could not fetch statistics update data: %s", err)
            return False

        if not intervals:
            _LOGGER.debug(
                "No new interval data for statistics update (requested %s to %s). "
                "API may not have data available yet.",
                start_date,
                data_date,
            )
            return False

        intervals = self._filter_incomplete_days(intervals)

        if not intervals:
            _LOGGER.debug("No valid interval data after filtering incomplete days")
            return False

        _LOGGER.debug("Received %d intervals for statistics update", len(intervals))

        # Group intervals by hour (consumption and cost). Tiered pricing and the
        # prorated customer charge follow the billing period from the forecast,
        # not the calendar month (upstream issue #15).
        hourly_consumption, hourly_cost = self._aggregate_hourly(
            intervals, bill_forecast
        )

        # Build new statistics, continuing the existing cumulative sum chains
        consumption_statistics = self._statistic_rows(
            hourly_consumption, consumption_sum
        )
        cost_statistics = self._statistic_rows(hourly_cost, cost_sum)

        if not consumption_statistics:
            return False

        _LOGGER.info(
            "Adding %d new hourly statistics for %s (sum=%.3f) and %s (sum=%.3f)",
            len(consumption_statistics),
            consumption_stat_id,
            consumption_statistics[-1]["sum"],
            cost_stat_id,
            cost_statistics[-1]["sum"],
        )
        async_add_external_statistics(
            self.hass,
            self._energy_metadata(consumption_stat_id, "consumption"),
            consumption_statistics,
        )
        async_add_external_statistics(
            self.hass, self._cost_metadata(cost_stat_id), cost_statistics
        )

        if generation_stat_id:
            generation_statistics = self._statistic_rows(
                aggregate_hourly_generation(intervals), generation_sum
            )
            if generation_statistics:
                _LOGGER.info(
                    "Adding %d new hourly generation statistics for %s (sum=%.3f)",
                    len(generation_statistics),
                    generation_stat_id,
                    generation_statistics[-1]["sum"],
                )
                async_add_external_statistics(
                    self.hass,
                    self._energy_metadata(generation_stat_id, "generation"),
                    generation_statistics,
                )
        return True

    async def async_import_green_button(
        self, file_paths: list[str], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Import Green Button exports as external statistics history.

        The portal API serves only ~68 days of interval data. A Green Button
        download from the billing profile covers roughly thirteen rolling
        months, so importing one extends the Energy Dashboard well past what
        polling can reach.

        Dominion stamps every reading in an export with whichever UTC offset
        was in effect when the file was generated, so about half of any export
        is an hour out. `green_button.realign_to_local` reconstructs the
        intended wall-clock time and re-localises it properly; the result is
        then checked against the API's own readings for the window the two
        share, and the import is refused if they do not line up. Writing
        history that is silently an hour skewed would be worse than not
        importing at all.

        Where both sources cover an hour the API wins: it is half-hourly at two
        decimal places against Green Button's whole-kWh hours.

        Returns a summary describing what was (or, for a dry run, would be)
        written.
        """
        async with self._import_lock:
            return await self._async_import_green_button(file_paths, dry_run=dry_run)

    async def _async_import_green_button(
        self, file_paths: list[str], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Do the work of :meth:`async_import_green_button`, under its lock."""
        assert self._client is not None

        consumption_id, cost_id, _ = self._statistic_ids()

        # Parsing is CPU-bound XML work over a couple of megabytes.
        parsed = await self.hass.async_add_executor_job(
            _load_green_button_files, file_paths
        )
        # Reference data: the API window, which is the only series we know to be
        # correctly stamped. Every export is calibrated against it.
        start_date, end_date = self._backfill_window(dt_util.now().date())
        reference_intervals = await self._client.async_get_interval_usage(
            account_number=self.config_entry.data[CONF_ACCOUNT_NUMBER],
            meter_number=self.config_entry.data[CONF_METER_NUMBER],
            start_date=start_date,
            end_date=end_date,
        )
        reference_intervals, _dropped = filter_incomplete_days(reference_intervals)
        reference_local: dict[datetime, float] = {}
        for interval in reference_intervals:
            hour = interval.timestamp.replace(minute=0, second=0, microsecond=0)
            reference_local[hour] = (
                reference_local.get(hour, 0.0) + interval.consumption
            )
        reference_hourly = {
            dt_util.as_utc(hour): value for hour, value in reference_local.items()
        }
        if not reference_hourly:
            raise HomeAssistantError(
                "No API data available to calibrate the import against. Let the "
                "integration finish its initial backfill and try again."
            )

        # Each export carries a constant but unknowable timestamp offset, so it
        # is measured rather than modelled. A file that does not reach the API
        # window is calibrated against an already-calibrated export instead --
        # two exports overlap heavily, so the chain holds.
        imported: dict[datetime, float] = {}
        sources: list[dict[str, Any]] = []
        pending = list(parsed)
        calibrated_against = "API"

        while pending:
            progressed = False
            for entry_index, (path, export) in enumerate(pending):
                if (
                    export.flow_direction is not None
                    and export.flow_direction != FLOW_DIRECTION_DELIVERED
                ):
                    raise HomeAssistantError(
                        f"{path} has ESPI flowDirection "
                        f"{export.flow_direction}, which is not energy "
                        "delivered to the customer. Importing it as "
                        "consumption would misstate your usage."
                    )

                readings = list(export.readings)
                # Trim before calibrating only so the zero padding at the end
                # cannot drag the correlation around; the authoritative trim
                # happens below, once the timestamps are corrected.
                calibration_series = to_hourly(drop_incomplete_tail(readings))
                against = (
                    reference_hourly
                    if not imported
                    else {
                        **imported,
                        **reference_hourly,
                    }
                )
                alignment = best_alignment(calibration_series, against)
                if alignment is None or not alignment.is_convincing:
                    continue

                # Trim in the corrected frame. Deciding which trailing days are
                # complete depends on the local hour a day reaches, which is
                # only meaningful once the offset has been applied.
                corrected = drop_incomplete_tail(
                    apply_shift(readings, alignment.shift_hours)
                )
                shifted = to_hourly(corrected)

                if magnitude_looks_wrong(shifted, against):
                    raise HomeAssistantError(
                        f"{path} lines up in shape but not in magnitude: it "
                        f"averages {sum(shifted.values()) / max(len(shifted) / 24, 1):.0f} "
                        "kWh/day against known-good data. Correlation cannot "
                        "see a scale error, so the import was refused rather "
                        "than writing usage off by a factor."
                    )

                imported.update(shifted)
                sources.append(
                    {
                        "path": path,
                        "readings": len(export.readings),
                        "measured_shift_hours": alignment.shift_hours,
                        "calibrated_against": calibrated_against,
                        "correlation": round(alignment.correlation, 4),
                        "compared_hours": alignment.overlapping_hours,
                        "mean_absolute_error_kwh": round(
                            alignment.mean_absolute_error_kwh, 3
                        ),
                        "hours_after_trim": len(shifted),
                        "hours_dropped_as_incomplete": len(readings) - len(corrected),
                        "first_hour": min(shifted).isoformat() if shifted else None,
                        "last_hour": max(shifted).isoformat() if shifted else None,
                        "total_kwh": round(sum(shifted.values()), 3),
                    }
                )
                pending.pop(entry_index)
                calibrated_against = "an already-calibrated export"
                progressed = True
                break

            if not progressed:
                unmatched = ", ".join(path for path, _ in pending)
                raise HomeAssistantError(
                    f"Could not verify the timestamps of: {unmatched}. Every "
                    "Green Button export is stamped with a constant but "
                    f"unpredictable offset, so it is measured against known-good "
                    f"data and must reach at least {MIN_CORRELATION:.2f} "
                    "correlation. No shift fit well enough, which usually means "
                    "the export does not overlap either the API's recent window "
                    "or another export being imported alongside it. Try a "
                    "freshly downloaded export, and include it in the same call "
                    "as any older ones."
                )

        if not imported:
            raise HomeAssistantError(
                "No usable readings found in the supplied Green Button export(s)"
            )

        merged = merge_preferring(reference_hourly, imported)
        cutoff = _earliest_priceable_date(
            self.config_entry.options.get(CONF_COST_MODE, COST_MODE_API)
        )

        # Per-file calibration results live in `sources`; there is no single
        # alignment figure, because each export carries its own offset.
        summary: dict[str, Any] = {
            "sources": sources,
            "hours_imported": len(imported),
            "hours_from_api": len(reference_hourly),
            "hours_total": len(merged),
            "first_hour": min(merged).isoformat(),
            "last_hour": max(merged).isoformat(),
            "total_kwh": round(sum(merged.values()), 3),
            "cost_priced_from": cutoff.isoformat() if cutoff else None,
            "statistic_ids": {"consumption": consumption_id, "cost": cost_id},
            "dry_run": dry_run,
        }

        if dry_run:
            return summary

        consumption_rows = self._statistic_rows(merged)
        async_add_external_statistics(
            self.hass,
            self._energy_metadata(consumption_id, "consumption"),
            consumption_rows,
        )

        cost_hourly = self._price_hourly(merged, cutoff)
        if cost_hourly:
            async_add_external_statistics(
                self.hass,
                self._cost_metadata(cost_id),
                self._statistic_rows(cost_hourly),
            )
        summary["cost_hours_written"] = len(cost_hourly)

        _LOGGER.info(
            "Imported %d hours of Green Button history (%s to %s, %.1f kWh)",
            len(merged),
            summary["first_hour"],
            summary["last_hour"],
            summary["total_kwh"],
        )
        return summary

    def _price_hourly(
        self, hourly: dict[datetime, float], cutoff: date | None
    ) -> dict[datetime, float]:
        """Cost each hour using the configured mode.

        Hours before ``cutoff`` are skipped rather than priced with a tariff
        that was not in effect: the rate registry only reaches back so far, and
        `get_schedule_for_date` would otherwise fall back to its oldest
        schedule and quietly bill 2025 at 2026 rates.

        Cumulative kWh is tracked per billing period so Schedule 1's tier
        boundary lands where the bill puts it. Billing period boundaries for
        historical data are stepped back in whole months from the current
        period, which drifts a few days per cycle against real meter reads.
        """
        local_tz = dt_util.get_default_time_zone()
        anchor = None
        if self.data is not None and self.data.bill_forecast is not None:
            anchor = self.data.bill_forecast.current_period_start
        period_days = self._billing_period_days(
            self.data.bill_forecast if self.data else None, dt_util.now().date()
        )

        priced: dict[datetime, float] = {}
        cumulative = 0.0
        current_period: date | None = None

        for hour in sorted(hourly):
            local = hour.astimezone(local_tz)
            if cutoff is not None and local.date() < cutoff:
                continue
            period = (
                billing_period_start(local.date(), anchor)
                if anchor is not None
                else local.date().replace(day=1)
            )
            if period != current_period:
                cumulative = 0.0
                current_period = period
            kwh = hourly[hour]
            priced[hour] = self._calculate_interval_cost(
                _ImportedInterval(timestamp=local, consumption=kwh),
                self.data.bill_forecast if self.data else None,
                cumulative_kwh_before=cumulative,
                billing_period_days=period_days,
            )
            cumulative += kwh
        return priced
