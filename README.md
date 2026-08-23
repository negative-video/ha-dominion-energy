<p align="center">
  <img src="assets/jaws-icon-raster-128.png" alt="Dominion Energy Integration Logo" width="128" height="128">
</p>

<h1 align="center">Dominion Energy for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/negative-video/ha-dominion-energy/releases"><img src="https://img.shields.io/github/v/release/negative-video/ha-dominion-energy?style=flat-square" alt="GitHub Release"></a>
  <a href="https://github.com/negative-video/ha-dominion-energy/blob/main/LICENSE"><img src="https://img.shields.io/github/license/negative-video/ha-dominion-energy?style=flat-square" alt="License"></a>
  <a href="https://github.com/negative-video/ha-dominion-energy/issues"><img src="https://img.shields.io/github/issues/negative-video/ha-dominion-energy?style=flat-square" alt="Issues"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2025.11%2B-blue?style=flat-square&logo=homeassistant&logoColor=white" alt="Home Assistant 2025.11+">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-orange?style=flat-square" alt="HACS"></a>
</p>

<p align="center">
  Monitor your Dominion Energy electricity usage in Home Assistant with high-resolution 30-minute interval data.
</p>

---

> **A fork of [YeomansIII/ha-dominion-energy](https://github.com/YeomansIII/ha-dominion-energy), by Jason Yeomans.**
> The original integration is his work — the config flow, the Schedule 1 tariff
> model, the external-statistics approach and the cost modes this is still
> built on. This fork adds reliability work, correctness fixes and new
> capabilities on top of that foundation; see
> [What this fork adds](#what-this-fork-adds).
>
> It installs the matching fork of the client library,
> [negative-video/dompower](https://github.com/negative-video/dompower),
> **not** the `dompower` package on PyPI.

## What this fork adds

52 commits ahead of upstream and none behind, against a source tree that grew
from 2,469 lines to 7,534 and a test suite that grew from 18 tests in one file
to 651 across twelve. Upstream's most recent commit is from March 2026.

Everything below is a change in behaviour, not a refactor. Where a number
appears it was measured against a real meter or a real incident.

### It stops going dark when the API has a bad hour

The API sits behind a WAF and has bad hours. A real one: from 00:33 to at
least 02:31 every cycle re-authenticated successfully and then took HTTP 400
on *both* the bill forecast and the interval export. By 15:05 the same
requests worked untouched. Nothing about them had changed.

Upstream raises `UpdateFailed` on the first failure, which takes every entity
unavailable — and when it happens during setup, leaves the entry retrying. A
user reload landing on an in-flight retry cancels it, and Home Assistant
schedules no further retry after a cancellation. The observed result was
twelve hours with the integration entirely gone.

- A refused poll now falls back to the last good payload for 24 hours, which
  is a full publication cycle: the API publishes exactly one new day per day,
  so a payload under 24 hours old is as current as the source ever gets.
- `ConfigEntryAuthFailed` still passes straight through, or the
  "Reauthenticate" button would never appear. Two tests assert this — one on
  the handler list, one by driving a real cycle.
- **`Last successful update` is the sensor that tells the truth.** Since the
  others keep reading through a failure, "the sensors have values" no longer
  proves the API is answering. This one stops advancing the moment cycles
  start failing, and diagnostics reports the consecutive failure count beside it.
- A rejected login and an unreachable one are now different things. The
  refresh token expires about once a day, so before this any WAN outage
  spanning one cycle ended with the integration demanding credentials that
  were never the problem.
- API errors carry the response body into the log — squashed onto one line,
  truncated, with account and meter numbers masked. `str(ApiError)` is
  `"API error: 400"` and nothing more, which cannot distinguish an inverted
  date window from an unentitled account from a WAF block page. The incident
  above was undiagnosable for exactly that reason.

### It notices when recorded history is wrong, and offers to fix it

The Energy Dashboard reads a day's cost as the difference between two running
totals. If a write is interrupted between chains — an unclean shutdown, a
reload landing on a recovery cycle after an outage — a day can end up carrying
a second copy of its own cost while **every hourly value stays individually
correct**. Auditing the raw sums misses the entire fault class.

This shipped once, and is what the work below exists to prevent: 2026-08-15
recorded 90.25 kWh at $32.95 instead of $16.34 — 0.365 $/kWh against a
sixty-day band of 0.174–0.185 — written when the API returned from an
eight-hour outage and the integration was reloaded ninety seconds later.

- Every statistic chain is now seeded from the window being rewritten, never
  from its own last row, so an interrupted cycle cannot stack a day on top of
  a sum that already contains it.
- After each settled update the last 14 days of *day totals* are read back out
  of the recorder and checked. A day whose implied $/kWh is off the window's
  median by more than 1.5× in either direction raises a repair, cleared
  automatically once it no longer does. A duplicated day measured 1.98× in the
  real incident; sixty days of Schedule 1 on a real meter spanned a factor of
  1.06, so the threshold clears both comfortably.
- The repair says whose fault it is and that the bill is unaffected. A day at
  twice the going rate looks exactly like a billing error, and the reasonable
  response to a billing error is to phone the utility — about a number the
  utility never produced, cannot see, and cannot explain.
- `dominion_energy.rebuild_cost_statistics` recomputes recorded cost from the
  meter's own interval data. Consumption is left alone. The alternative —
  Developer Tools → Statistics, or a `recorder/adjust_sum_statistics`
  WebSocket call — is well past what most people installing a HACS integration
  will attempt.

### More than one meter on an account

Upstream keys the config entry on the account number and each entity's unique
ID on `{account}_{sensor}`, so a second meter on the same account cannot be
added and would write into the first one's statistics if it could. Entries are
now keyed on `{account}_{meter}`, with a migration that leaves existing
installations on the identity they already have — statistic IDs are how the
Energy Dashboard finds history, and renaming one orphans it.

### Entity and config-flow names that actually appear

Home Assistant only loads `translations/<lang>.json` for custom integrations.
Upstream ships a complete `strings.json` — seven config-flow steps, options,
entity names — and no `translations/` directory, so none of it reaches the UI.
This fork ships both, kept byte-identical, with a test that fails if a step,
error, abort reason, service or repair issue loses its strings.

### New capabilities

- **Usage insights** derived from your own interval data — an always-on
  baseline, the busiest hour of the day, and a binary sensor for a day that
  was out of character for that weekday.
- **Budget** — an optional spending target for the billing period, with an
  on-pace warning that watches the *projection* rather than spend to date, so
  it can warn on day 6 of 30 while there is still time to act.
- **Solar / net metering** — a separate generation stream and statistic,
  created only once the meter actually reports export.
- **Green Button import**, extending Energy Dashboard history from the API's
  ~68 day ceiling to roughly 13 months.
- **A projected bill broken into its line items** — distribution, generation,
  transmission, fuel, taxes and the customer charge — so you can see *why* a
  bill moved when your usage did not.
- **A rate staleness alarm.** Dominion re-files its riders periodically and
  the tariff is hard-coded here; `Rate model drift` measures this integration's
  Schedule 1 estimate against the last real bill, so bundled rates falling
  behind announce themselves.
- **Downloadable diagnostics**, with account number, meter number, service
  address, credentials and tokens redacted — a test fails if any fake
  credential appears anywhere in the output.
- **Billing-period awareness throughout.** Dominion reads meters mid-month, so
  tiered pricing and the monthly customer charge reset on that cycle rather
  than on the 1st.

### Findings, measured rather than assumed

These are recorded in the code so the next person does not have to rediscover
them:

- **The overnight baseline was measuring the air conditioner.** The obvious
  window — midnight to 05:00 — assumes the house is asleep and the HVAC is
  off. On a real meter that cooled at night, midnight was the *second-heaviest*
  hour of the day and the quietest was 10 AM; that window returned 1508 W,
  near enough the household's whole average draw. The quiet hours are now
  taken from the household's own 30-day profile.
- **A cycling thermostat moves `hvac_action`, not its state.** `hvac_action`
  is an attribute, so `state_changes_during_period()` skips those rows
  entirely and an HVAC filter built on it silently does nothing. That shipped
  once: against a real ecobee it excluded 10 intervals out of 70 and reported
  the air conditioner as standing load.
- **Green Button timestamps are wrong by a constant the file does not
  disclose.** One August export measured +5 hours against the API, a February
  one +4. The importer measures the offset by correlation rather than
  modelling it — an earlier version reconstructed the intended wall clock and
  re-localised with DST rules, which made two exports agree with each other at
  100% while leaving both five hours from the truth.
- **Mean absolute error cannot score that alignment.** Readings are whole kWh,
  which leaves MAE nearly flat — 1.24 to 1.57 across shifts on real data,
  picking the wrong answer — while correlation ranged 0.02 to 0.98 and was
  unambiguous.
- **Billing cycle length is mean-reverting, not persistent.** Borrowing the
  last completed bill's length looks more principled than a nominal 30 days
  but measures worse: across 21 consecutive cycles from one account, a 33-day
  January was followed by a 28-day February, so carrying the previous length
  forward nearly doubled the error.
- **Household electricity is strongly weekly.** Comparing a Saturday against a
  mostly-weekday window flags every Saturday, so the unusual-usage check
  compares like weekdays and uses a median, which stops one already-exceptional
  day raising the bar and hiding the next.

### Testing

651 tests across twelve files, up from 18 in one. They are plain stdlib pytest
and deliberately avoid importing `homeassistant`, so the bulk of the logic —
tariff maths, usage aggregation, DST handling, ESPI parsing, diagnostics
redaction — is decidable without a Home Assistant checkout. A second CI job
runs the same suite against a pinned Home Assistant release, and a third
type-checks against the real client library.

## Features

- 30-minute interval energy usage data
- Daily and monthly usage totals
- Billing-period usage and cost to date, plus a projected end-of-period bill
- Excess generation (solar / net metering) usage and statistics, when the meter reports it
- Bill forecast sensors (last bill, current billing period, effective rate)
- A breakdown of where the projected bill goes — distribution, generation, riders, transmission and tax
- Usage insights derived from your own interval data:
  - **Always-on baseline** — what the house draws when nothing is happening, ignoring any half hour your thermostats were running
  - **Busiest hour** — which hour of the day the house uses most, with the full 24-hour shape
  - **Unusual usage** — a binary sensor for a day that was out of character for that weekday
  - **Budget** — optional spending target, with dollars left and an on-pace warning
- Cost estimation with four calculation modes:
  - **API Estimate**: Derives rate from your actual bill (charges / usage)
  - **Fixed Rate**: Single $/kWh rate
  - **Time-of-Use**: Peak and off-peak rates by hour
  - **Schedule 1 - VA Residential**: Full Dominion Energy Virginia residential tariff — tiered distribution and generation rates, seasonal pricing, riders, and consumption taxes
- Full Energy Dashboard compatibility
- Automatic token refresh and re-login
- Green Button import, extending Energy Dashboard history from ~68 days to ~13 months
- Downloadable diagnostics, with account and address details redacted

## Requirements

- Home Assistant **2025.11.0** or newer
- A Dominion Energy account with an AMI (smart) meter

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots in the top right corner
3. Select "Custom repositories"
4. Add `https://github.com/negative-video/ha-dominion-energy` and select "Integration" as the category
5. Click "Add"
6. Search for "Dominion Energy" and install it
7. Restart Home Assistant

> **Adding this fork over an existing upstream install?** Remove the upstream
> custom repository first, or HACS will keep offering you its releases. Your
> config entry, its options and your recorded statistics all survive the
> swap — the domain and the statistic IDs are unchanged.

### Manual Installation

1. Copy the `custom_components/dominion_energy` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Setup

### Add Integration

1. Go to Home Assistant Settings > Devices & Services
2. Click "Add Integration"
3. Search for "Dominion Energy"
4. Enter your Dominion Energy username (email) and password
5. Complete the two-factor authentication (TFA) when prompted
6. Select your account and meter if you have multiple

> **Note**: SMS-based TFA is recommended. Email TFA may have reliability issues.

If you have several meters on one account, add the integration once per meter — each meter gets its own entry.

### Configure Cost Calculation (Optional)

1. After setup, click "Configure" on the integration, then **Cost calculation**
2. Choose your cost calculation method:
   - **API Estimate**: Uses your actual bill rate (recommended)
   - **Fixed Rate**: Enter a single $/kWh rate
   - **Time-of-Use**: Configure peak/off-peak rates and hours
   - **Schedule 1 - VA Residential**: Applies the full Virginia residential tariff; no extra input needed

### Configure Usage Insights (Optional)

Under "Configure" → **Usage insights**:

- **Thermostats** — select your `climate` entities so the always-on baseline can ignore any half hour the heating or cooling was actually running. Without this the baseline measures your compressor rather than your house. Thermostats that report an *action* (`heating`, `cooling`, `idle`) give the best result; ones that only report a mode are treated as running whenever they are not `off`.
- **Billing period budget** — what you would like to keep each bill under. Set it to add the budget sensors; leave it at 0 and they are not created at all.

## Sensors

| Sensor | Description | State Class |
|--------|-------------|-------------|
| Latest interval usage | Most recent 30-minute reading (kWh) | measurement |
| Yesterday's usage | Previous day's total consumption (kWh) | total_increasing |
| Current month usage | Month-to-date consumption (kWh) | total_increasing |
| Yesterday's cost | Estimated cost for previous day ($) | total |
| Current month cost | Estimated cost for month-to-date ($) | total |
| Current billing period usage | Usage in current billing cycle, as reported by Dominion (kWh) | total_increasing |
| Billing period usage to date | Usage since the billing period started, from interval data (kWh) | total |
| Billing period cost to date | Cost of that usage in your configured mode ($) | total |
| Projected billing period usage | Projected usage for the full billing period (kWh) | total |
| Projected billing period cost | Projected cost for the full billing period ($) | total |
| Last bill charges | Charges from previous bill ($) | total |
| Last bill usage | Usage from previous bill (kWh) | total |
| Effective rate | Derived cost per kWh ($/kWh) | measurement |
| Always-on baseline | Standing draw when nothing is running (W) | measurement |
| Busiest hour | The hour of the day the house uses most, e.g. `6 PM` | — |

**Projected billing period cost** carries the bill's line items as attributes — `distribution_charge`, `generation_charge`, `transmission_charge`, `fuel_charge`, `taxes_and_fees` and `customer_charge` — so you can see *why* a bill moved when your usage did not. These are the sections Dominion prints, so each rider is folded into the charge it is recovered under rather than lumped into one opaque "riders" line. These are always priced with the full Schedule 1 tariff; `breakdown_matches_state` tells you whether they add up to the sensor's own value or sit alongside it (they only match in Schedule 1 cost mode).

> **Note**: `Last bill charges` and `Last bill usage` report `unknown` until
> your first bill closes. An account with no closed bill is deliberately kept
> distinguishable from one whose last bill genuinely came to zero.

### Understanding your usage

| Entity | Description |
|--------|-------------|
| Always-on baseline | The median of each day's quietest half hour, as watts. The hours it looks in are picked from your own profile, not assumed — `quiet_hours` says which. Attributes also carry the daily kWh it accounts for, an estimated monthly cost, and its share of a typical day. |
| Busiest hour | Averaged over the last 30 complete days. `hourly_average_kwh` is a 24-element list you can feed straight to a chart card. |
| Unusual usage | `binary_sensor`, device class `problem`. Compares the latest complete day against the median of the same weekday over the previous four weeks. |

The always-on baseline is the most directly actionable number here — it is the fridge, the network gear, the standby loads, everything you never switch off. A sudden jump in it usually means something broke rather than something changed.

> **Note**: These need history to say anything. The busiest hour needs seven complete days, the baseline needs that same profile plus three usable days of its own, and unusual usage needs two prior same-weekdays. Until then they report `unknown` rather than guessing.

### Budget (optional)

Set a billing period budget under "Configure" → "Usage insights" and three more entities appear:

| Entity | Description |
|--------|-------------|
| Budget remaining | Dollars left in the period; goes negative once overspent ($) |
| Budget used | Percent of the budget spent so far (%) |
| Over budget pace | `binary_sensor`, device class `problem` — on when the *projection* exceeds the budget |

**Over budget pace** deliberately watches the projection rather than spend to date, so it can warn you on day 6 of 30 while there is still time to do something. Its attributes carry the budget, the projection, and `days_left_in_period`.

### Solar / net metering

If your meter reports excess generation, three more sensors appear automatically — **Latest interval generation**, **Yesterday's generation**, and **Current month generation** — along with a generation statistic for the Energy Dashboard. Meters that never report export get none of these, so non-solar installations are unaffected.

> **Note**: Generation is recorded as its own separate stream. It does **not** currently offset the cost figures — net-metering credits are not modelled.

### Diagnostic sensors

| Sensor | Description |
|--------|-------------|
| Billing period start | First day of the current billing period |
| Billing period end | Estimated next meter read, ending the current billing period |
| Time-of-use plan | `Yes`/`No` — whether the account is billed on a time-of-use plan |
| Estimated last bill charges | What the Schedule 1 tariff model computes for the last bill ($) |
| Rate model drift | How far that estimate lands from the real bill (%) |
| Rate schedule effective date | Effective date of the newest tariff data bundled with this integration |
| Last successful update | When a poll last actually reached the API |

> **Note**: **Billing period end** is an estimate, and has to be. The API
> publishes no next-scheduled-meter-read date anywhere — 46 leaf keys across
> four `actionCode` values were probed and none carries one. The field that
> looks like a period end is the day usage is published *through*: it advances
> a day at a time and is never later than today, so the period always looks
> shorter than it is. Once that length passes the 20-day plausibility floor it
> stops looking obviously wrong, and the projection — usage-to-date ÷ days
> observed × days in period — collapses to exactly usage-to-date. The period
> start plus a nominal 30-day cycle is used instead. Borrowing the last
> completed bill's own length measures worse; see
> [Findings](#findings-measured-rather-than-assumed).

> **Tip**: **Rate model drift** is a staleness alarm. Dominion re-files its riders periodically — the fuel factor typically changes around July 1 — and this integration ships the rates hard-coded. A drift figure that suddenly grows means the bundled tariff data has fallen behind and should be refreshed. See [docs/rate-schedules.md](docs/rate-schedules.md).

> **Note**: The Dominion Energy API only provides data for **completed days**. Yesterday's data typically becomes available the following morning. The daily and interval sensors carry a `data_date` attribute showing which day the data represents; the monthly sensors carry `month_start` and `month_end`.

## Energy Dashboard

This integration provides **external statistics** for the Home Assistant Energy Dashboard with hourly granularity.

### Setup

1. Go to **Settings → Dashboards → Energy**
2. Under **Electricity grid**, click **Add consumption**
3. Search for your account number or "dominion"
4. Select the statistic: `dominion_energy:{account_number}_energy_consumption`
5. For cost tracking, select **"Use an entity tracking the total costs"**
6. Select the cost statistic: `dominion_energy:{account_number}_energy_cost`

### Available Statistics

| Statistic ID | Description |
|--------------|-------------|
| `dominion_energy:{prefix}_energy_consumption` | Cumulative energy consumption (kWh) |
| `dominion_energy:{prefix}_energy_cost` | Cumulative energy cost (uses configured cost mode) |
| `dominion_energy:{prefix}_energy_generation` | Cumulative excess generation (kWh) — only created once the meter reports export |

`{prefix}` is `{account_number}_{meter_device_id}` for any entry added since this scheme landed, so two meters on one account can never write into the same stream. Installations that already had account-scoped statistics keep using the bare account number, because external statistics cannot be renamed in place and changing the ID would orphan their history.

The exact IDs are easiest to find by searching for "dominion" in the Energy Dashboard's statistic picker rather than typing them out. The `statistic_id_prefix` in the diagnostics download also shows the resolved value.

To add generation to the Energy Dashboard, use **Add solar production** rather than **Add consumption**.

### How It Works

- The integration creates external statistics (not sensor entities) for the Energy Dashboard
- Data is aggregated from 30-minute intervals into hourly statistics
- Historical data is automatically backfilled on first setup. The window is set by `BACKFILL_DAYS` in `const.py`; the API returns roughly the last two months of 30-minute data regardless of the range requested
- Days that are missing or only partly published are skipped rather than recorded as zeros, and are picked up once the data lands
- Local hours that collapse to the same UTC instant on DST changeover days are merged rather than colliding
- Cost is calculated using your configured cost mode (API estimate, fixed rate, time-of-use, or Schedule 1)
- Statistics update daily with the previous day's data

## Importing history with Green Button

The Dominion API only serves about **68 days** of interval data — it ignores the date range you ask for and returns a fixed recent window, so no amount of patience gets you more. The **Green Button** download in your billing profile is a different data path covering roughly **13 rolling months**, and this integration can import it.

### Getting the file

Download the hourly Green Button XML from your Dominion Energy billing profile. The window rolls forward, so a fresh export always reaches yesterday; keeping an older one lets you cover more history than either file alone.

**Where to put it.** Home Assistant only reads from directories in `allowlist_external_dirs`, which by default is just your media directory and `config/www` — **the config directory itself is not allowed.** Put it under **`/media/`**: files in `www` are served publicly at `/local/` with no authentication, and a Green Button export contains your account number and a full hourly record of when your home is occupied. To use somewhere else:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /config/greenbutton
```

> **Two paths that catch almost everyone.** Both are invisible in a file browser:
>
> 1. **`/homeassistant` vs `/config`.** Add-ons like File Editor, Samba and Terminal mount your config directory as `/homeassistant`. This service runs in Home Assistant Core, which knows the same directory as `/config`. Always give the Core path.
> 2. **`/media` vs `/config/media`.** Home Assistant OS provides `/media` as its own **top-level** mount — a sibling of `/config`, and in Samba a *separate share*, not a folder inside `config`. A `media` folder sitting inside your config directory is a different place, is not on the allowlist, and looks identical when browsing. Put the file in the top-level one, alongside directories like `llmvision` or `wyze`, not inside `config`.
>
> The "not allowed" error detects both cases and says which one you've hit.

Also note that an XML file **will not appear in the Home Assistant media browser** — that only lists playable media. Its absence there doesn't mean it's in the wrong place.

### Running the import

Call `dominion_energy.import_green_button` from **Developer Tools → Actions**. **Do a dry run first** — it reports the date range, the total, and the timestamp-alignment result without writing anything:

```yaml
action: dominion_energy.import_green_button
data:
  config_entry_id: <your entry>
  file_path:
    - /config/green_button/GreenButton_hourly_latest.xml
    - /config/green_button/GreenButton_hourly_older.xml
  dry_run: true
```

Pass several files to merge them; later ones win on any hour they share. Drop `dry_run` to write the statistics.

### What it does, and why it isn't a straight copy

**Dominion's timestamps are wrong by a constant, and the file doesn't say by how much.** Measured against the utility's own API readings, one August export was out by **+5 hours** and a February one by **+4**. The offset is constant within a file but varies between exports, and nothing in the file predicts it.

So the importer **measures** the offset rather than guessing: it correlates each export against data known to be correctly stamped, and only proceeds when the fit is convincing. An export that doesn't reach the API's recent window is calibrated against another export in the same call that does — so pass old and new files together. If nothing fits, the import is refused rather than writing history that is silently hours out.

> This is worth stressing because the obvious sanity check doesn't work: two exports can be made to agree with each other perfectly while *both* are five hours from the truth. Only comparison against independently-correct data catches it.

**The API wins where both cover an hour.** Green Button is whole-kWh and hourly; the API is two decimal places and half-hourly. Green Button only fills in what the API cannot reach.

**Trailing days are dropped.** The export pads out to the moment of download with zero readings, so the last day or two isn't real data.

**Cost has limits.** Green Button carries no usable cost — Dominion emits the field and leaves it zero — so cost is recomputed from your configured mode. In Schedule 1 mode, hours before the oldest tariff in `rates.py` are imported as **consumption only**, rather than being priced with rates that were not yet in effect. The flat modes have no such limit.

### Why External Statistics?

The Energy Dashboard works best with cumulative statistics that track total consumption over time. External statistics allow the integration to:
- Backfill historical data that existed before you installed the integration
- Provide accurate hourly breakdowns for energy analysis
- Handle the 1-day data delay gracefully

> **Tip**: You can find your account number in the integration's device info or on your Dominion Energy bill.

## Authentication

Tokens automatically refresh in the background. When they expire, the integration first tries to sign in again on its own using the credentials and session cookies saved during setup, which usually avoids another TFA prompt.

If that fails (for example, Dominion asks for a new verification code):

1. Home Assistant will show a notification to re-authenticate
2. Click the notification to start the re-authentication flow
3. Confirm or update your username/password and complete TFA again

> **One re-login per day is the healthy pattern, not a fault.** The access
> token lasts 30 minutes and the per-day cache skips whole cycles, so the
> tokens usually go a full day untouched and the refresh token has expired by
> the time the next real fetch comes round. A daily
> `Refresh token expired, attempting auto-reauth` followed by
> `Successfully re-authenticated` is the integration working correctly.

## Troubleshooting

### "Cannot connect to API"
- Check your internet connection
- Verify Dominion Energy services are online

### "Invalid authentication"
- Tokens may have expired after extended inactivity
- Use the re-authentication flow to log in again

A prompt to reauthenticate means the login server answered and rejected the attempt. An internet outage that happens to span a token refresh is reported as a normal connection failure instead, and rides it out: the sensors keep serving the last published day for up to 24 hours rather than emptying, since the API only publishes one new day per day anyway. `Last successful update` is the sensor that stops advancing when polls are actually failing.

### Missing data
- Data may take up to an hour to appear after setup
- Historical data availability depends on Dominion Energy's API

### One day costs about twice what it should

**This is not a billing error and there is nothing to raise with Dominion Energy.** Your bill is calculated by the utility from the meter; nothing here changes it, and the utility cannot see these numbers at all.

What you are looking at is how this integration stores history. The Energy Dashboard reads a day's cost as the difference between two running totals, so if an update is interrupted part way through -- an outage, a power cut, a restart landing mid-write -- one day can end up carrying a second copy of its own cost while every underlying hourly figure stays correct. The usage in kWh is unaffected.

The integration checks for this after each update and raises a repair under **Settings → System → Repairs** when it finds one; the fix button recomputes the recorded cost history. To do it by hand, or for a day older than the check looks back:

```yaml
action: dominion_energy.rebuild_cost_statistics
data:
  config_entry_id: <your entry>
```

Usage history is left untouched -- only the pricing built on top of it is recalculated -- and nothing is sent to Dominion Energy.

### Reporting a problem

Download diagnostics before opening an issue: **Settings → Devices & Services → Dominion Energy → ⋮ → Download diagnostics**. Account number, meter number, service address, credentials and tokens are redacted, so the file is safe to attach to a public issue. It records the service region (for example `VA` or `SC`), which is usually the first thing needed to reproduce a fault.

## Development

```bash
git clone https://github.com/negative-video/ha-dominion-energy
cd ha-dominion-energy

# The fast gate: the whole suite, no Home Assistant checkout needed
uv sync --frozen --extra dev --python 3.13
uv run --no-sync pytest tests/ -q

# Against a pinned Home Assistant release
uv sync --frozen --extra dev --extra test-ha --python 3.14
uv run --no-sync pytest tests/ -q
uv run --no-sync mypy custom_components/dominion_energy/ --ignore-missing-imports

uvx ruff@0.16.2 check custom_components/dominion_energy/ tests/
uvx ruff@0.16.2 format --check custom_components/dominion_energy/ tests/
```

`pytest-homeassistant-custom-component` pins one exact Home Assistant version
and needs Python 3.14.2+, which is why it is an optional extra rather than a
base dependency. Keep new tests importable without `homeassistant`.

See [CLAUDE.md](CLAUDE.md) for the architecture and the reasoning behind the
parts that look strange, and [RELEASING.md](RELEASING.md) for the release
process — including why `dompower` is installed from a release URL with a
version fragment rather than from PyPI.

## API Constants

The Dominion Energy API uses SAP Customer Data Cloud (Gigya) for authentication. The following API key is the default for all users:

```
GIGYA_API_KEY = "4_6zEg-HY_0eqpgdSONYkJkQ"
```

This is a public client identifier embedded in the Dominion Energy web app. It can be overridden via the `GIGYA_API_KEY` environment variable if Dominion updates it.

## Support

- [Report an issue](https://github.com/negative-video/ha-dominion-energy/issues) — for this fork
- [negative-video/dompower](https://github.com/negative-video/dompower) — the client library this installs
- [YeomansIII/ha-dominion-energy](https://github.com/YeomansIII/ha-dominion-energy) and [YeomansIII/dompower](https://github.com/YeomansIII/dompower) — the upstream projects this is built on

## License

MIT License
