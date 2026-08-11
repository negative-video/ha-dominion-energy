<p align="center">
  <img src="assets/jaws-icon-raster-128.png" alt="Dominion Energy Integration Logo" width="128" height="128">
</p>

<h1 align="center">Dominion Energy for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/YeomansIII/ha-dominion-energy/releases"><img src="https://img.shields.io/github/v/release/YeomansIII/ha-dominion-energy?style=flat-square" alt="GitHub Release"></a>
  <a href="https://github.com/YeomansIII/ha-dominion-energy/blob/main/LICENSE"><img src="https://img.shields.io/github/license/YeomansIII/ha-dominion-energy?style=flat-square" alt="License"></a>
  <a href="https://github.com/YeomansIII/ha-dominion-energy/issues"><img src="https://img.shields.io/github/issues/YeomansIII/ha-dominion-energy?style=flat-square" alt="Issues"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-blue?style=flat-square&logo=homeassistant&logoColor=white" alt="Home Assistant">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-orange?style=flat-square" alt="HACS"></a>
</p>

<p align="center">
  Monitor your Dominion Energy electricity usage in Home Assistant with high-resolution 30-minute interval data.
</p>

---

## Features

- 30-minute interval energy usage data
- Daily and monthly usage totals
- Billing-period usage and cost to date, plus a projected end-of-period bill
- Excess generation (solar / net metering) usage and statistics, when the meter reports it
- Bill forecast sensors (last bill, current billing period, effective rate)
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
4. Add this repository URL and select "Integration" as the category
5. Click "Add"
6. Search for "Dominion Energy" and install it
7. Restart Home Assistant

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

1. After setup, click "Configure" on the integration
2. Choose your cost calculation method:
   - **API Estimate**: Uses your actual bill rate (recommended)
   - **Fixed Rate**: Enter a single $/kWh rate
   - **Time-of-Use**: Configure peak/off-peak rates and hours
   - **Schedule 1 - VA Residential**: Applies the full Virginia residential tariff; no extra input needed

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

### Solar / net metering

If your meter reports excess generation, three more sensors appear automatically — **Latest interval generation**, **Yesterday's generation**, and **Current month generation** — along with a generation statistic for the Energy Dashboard. Meters that never report export get none of these, so non-solar installations are unaffected.

> **Note**: Generation is recorded as its own separate stream. It does **not** currently offset the cost figures — net-metering credits are not modelled.

### Diagnostic sensors

| Sensor | Description |
|--------|-------------|
| Billing period start | First day of the current billing period |
| Billing period end | Last day of the current billing period |
| Time-of-use plan | `Yes`/`No` — whether the account is billed on a time-of-use plan |
| Estimated last bill charges | What the Schedule 1 tariff model computes for the last bill ($) |
| Rate model drift | How far that estimate lands from the real bill (%) |
| Rate schedule effective date | Effective date of the newest tariff data bundled with this integration |

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

## Troubleshooting

### "Cannot connect to API"
- Check your internet connection
- Verify Dominion Energy services are online

### "Invalid authentication"
- Tokens may have expired after extended inactivity
- Use the re-authentication flow to log in again

### Missing data
- Data may take up to an hour to appear after setup
- Historical data availability depends on Dominion Energy's API

### Reporting a problem

Download diagnostics before opening an issue: **Settings → Devices & Services → Dominion Energy → ⋮ → Download diagnostics**. Account number, meter number, service address, credentials and tokens are redacted, so the file is safe to attach to a public issue. It records the service region (for example `VA` or `SC`), which is usually the first thing needed to reproduce a fault.

## API Constants

The Dominion Energy API uses SAP Customer Data Cloud (Gigya) for authentication. The following API key is the default for all users:

```
GIGYA_API_KEY = "4_6zEg-HY_0eqpgdSONYkJkQ"
```

This is a public client identifier embedded in the Dominion Energy web app. It can be overridden via the `GIGYA_API_KEY` environment variable if Dominion updates it.

## Support

- [Report Issues](https://github.com/YeomansIII/ha-dominion-energy/issues)
- [dompower Library](https://github.com/YeomansIII/dompower)

## License

MIT License
