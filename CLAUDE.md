# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration (domain `dominion_energy`) for monitoring Dominion Energy electricity usage. It exposes sensors for 30-minute interval data and daily/monthly totals, feeds hourly external statistics to the Energy Dashboard, and estimates cost using one of four calculation modes.

## Architecture

### Key Components

- **`config_flow.py`**: Username/password login through SAP Customer Data Cloud (Gigya), with two-factor authentication. Step order:
  `async_step_user` (email + password) → `async_step_tfa_select` (pick SMS/email target) → `async_step_tfa_code` (enter code) → `async_step_discover_accounts` → `async_step_select_meter`.
  TFA steps are skipped when Gigya does not ask for it. Discovery keeps only meters where `is_active and has_ami`; a single match auto-creates the entry, multiple matches show the selection form.
  Separate paths: `async_step_reauth` / `async_step_reauth_confirm` (retries stored credentials first, falls back to a credential form, then `reauth_tfa_select` / `reauth_tfa_code`) and `async_step_reconfigure`, which re-runs the user step.
  `DominionEnergyOptionsFlow` shows a menu at `init` with two branches: `cost` → `fixed_rate` | `tou` | `schedule1` (or straight to entry creation for API estimate mode), and `insights` for the thermostat list and the budget.
  **Every write goes through `_save()`, which merges into the existing options.** An options flow's `async_create_entry` *replaces* `entry.options` wholesale, so a step that writes only its own keys deletes everything the other steps stored. `tests/test_translations.py` fails if any step calls `async_create_entry` directly.

- **`coordinator.py`**: `DominionEnergyCoordinator` extends `DataUpdateCoordinator`, polling every `UPDATE_INTERVAL_MINUTES` (60). Fetches the bill forecast, then one interval window wide enough to cover both the calendar month and the billing period, sliced locally; calculates costs; and writes external statistics. A per-day cache (`_cached_data_date`) short-circuits repeat cycles once the day's data is complete and its statistics have settled, so a normal day costs ~4 API calls rather than one set per cycle. Also owns auto-reauth (`_async_attempt_reauth`, using stored credentials and cookies) and token persistence via `_token_update_callback`.

- **`green_button.py`**: Pure ESPI/Green Button parsing and timestamp calibration, **no Home Assistant imports**. Dominion's exports carry a constant timestamp offset that is *not derivable from the file* — a real August export measured **+5h** against the API, a February one **+4h**. `best_alignment()` measures it by correlation against known-good data; `apply_shift()` applies it uniformly.
  **Two traps, both already paid for:**
  1. **Do not model the offset.** An earlier version reconstructed the intended wall clock and re-localised with DST rules. It made two exports agree with each other at 100% while leaving both five hours from the truth. Export-vs-export agreement proves nothing; only comparison against independently-correct data does.
  2. **Trim after shifting, never before.** Whether a day is complete depends on the local hour its readings reach, which is only meaningful once the offset is applied. Trimming first moves the boundary by the size of the offset and discarded ~19 real hours per file.
  3. **Do not score with mean absolute error.** Readings are whole kWh, which leaves MAE nearly flat — 1.24 to 1.57 across shifts on real data, picking the wrong answer — while correlation ranged 0.02 to 0.98 and was unambiguous. MAE is still reported, as a quality signal once the shift is known (≈0.25 is the expected rounding error; ≈1.2 means misaligned).
  4. **Correlation cannot see scale.** A misread unit or multiplier correlates perfectly while being orders of magnitude out, so `magnitude_looks_wrong()` compares daily averages separately. Also guarded: XML entity declarations, and ESPI `flowDirection` other than delivered.

- **`usage.py`**: Pure helpers with **no Home Assistant imports** — interval filtering, hourly aggregation, UTC de-duplication, cumulative-sum building, and billing-period boundary maths. This is where testable logic belongs: `coordinator.py` cannot be imported without Home Assistant, so anything decidable should live here (or as a module-level helper in `coordinator.py`) rather than inside a method.

- **`rates.py`**: Full Virginia Residential Schedule 1 tariff (`VA_SCHEDULE_1`) plus the calculation engine — seasonal tiered distribution/generation rates, flat riders, transmission, and tiered consumption tax. `calculate_schedule1_interval_cost()` prices a single interval given cumulative kWh so far in the billing period.

- **`sensor.py`**: Sensors built from a `DominionEnergySensorDescription` dataclass (a `SensorEntityDescription` with a `value_fn`). Three tuples: `SENSORS` (usage, cost, billing period, projection, the insight sensors, and six diagnostic entities), `BUDGET_SENSORS`, and `GENERATION_SENSORS`. The latter two are conditional, and gated differently on purpose — a budget is an option, and an options change reloads the entry, so a check at setup is enough; `has_generation` can flip true on a later refresh, so that one keeps a coordinator listener rather than making people restart. Descriptions set `translation_key`, never `name=`, so names come from the translation files.

- **`binary_sensor.py`**: `unusual_usage` (always) and `over_budget_pace` (only with a budget), both device class `PROBLEM`. Their `value_fn` returns `None` rather than `False` when there is not yet enough data — a PROBLEM sensor at `off` asserts that everything is fine, which is a claim the integration has to earn.

- **`entity.py`**: The unique-ID prefix and `DeviceInfo` both platforms share, resolved once per entry by `resolve_identity()`, plus the `DominionEnergyEntity` base that owns the one f-string every unique ID is built from. The prefix is the account number for an account's first meter and `{account}_{meter}` for later ones, so existing installations keep their identity. `_uses_legacy_identity()` enumerates the entry's own registry entries and excludes anything under the meter-scoped prefix — without that exclusion a meter-scoped entry re-reads itself as legacy on the next restart and changes its own identity. Never let a platform build its own unique ID or `DeviceInfo`; `tests/test_entity.py` fails if one does.

- **`insights.py`**: Pure derived metrics with **no Home Assistant imports** — the daily usage profile, the always-on baseline, and the same-weekday comparison. Two things here are load-bearing:
  1. **The baseline reads `hvac_action`, not the thermostat's state** — and the coordinator must fetch it with `get_significant_states(..., significant_changes_only=False)`. `hvac_action` is an *attribute*, so a cycling thermostat moves only `last_updated` while its state sits on `heat_cool`; `state_changes_during_period()` skips those rows and the filter silently does nothing. That shipped once: against a real ecobee it excluded 10 intervals out of 70 and the sensor reported the air conditioner as standing load. Thermostats reporting no action fall back to the mode (anything but `off` counts as running), which discards usable data rather than folding compressor draw into the answer.
  2. **The quiet hours come from the household, via `quietest_hours()`.** An overnight window assumes the house is asleep and the HVAC is off. On a real meter that cooled at night, midnight was the *second-heaviest* hour of the day and the quietest was 10 AM, so measuring 00:00-05:00 returned 1508 W — near enough the household's average draw. The window is taken from the 30-day profile, so no profile means no baseline.
  3. **Same-weekday, median, not a trailing mean.** Household electricity is strongly weekly; comparing a Saturday against a mostly-weekday window flags every Saturday. The median stops one already-exceptional day raising the bar and hiding the next.

- **`const.py`**: Config/option keys, the four cost-mode constants, `UPDATE_INTERVAL_MINUTES`, `BACKFILL_DAYS`, and rate defaults.

- **`strings.json`** and **`translations/en.json`**: must be kept identical. Home Assistant only loads `translations/<lang>.json` for custom integrations — `strings.json` alone is dead weight and the UI falls back to raw keys. `tests/test_translations.py` guards this.

### Data Flow

1. Config flow authenticates with Gigya and stores username, password, tokens, cookies, account number, meter device ID, and service address in the entry data.
2. `_async_setup()` builds a `DompowerClient` with a token-update callback that persists refreshed tokens back to the config entry.
3. Hourly, `_async_update_data()` fetches `async_get_bill_forecast()` and one interval window, then returns a `DominionEnergyData` dataclass that the sensors read from. Repeat cycles within the same completed day are served from cache without any API call.
4. The same refresh calls `_insert_statistics()` to backfill or extend the external statistics.

The Dominion API only publishes **completed days**, so "daily" always means yesterday. `data_date`, `month_start`, and `month_end` are exposed as entity attributes.

### Cost Modes

Read from `entry.options[CONF_COST_MODE]`, defaulting to `api_estimate`:

- `api_estimate` — `bill_forecast.derived_rate` (last bill charges / usage); falls back to the fixed rate if unavailable.
- `fixed` — one `$/kWh` rate.
- `time_of_use` — peak/off-peak rates with configurable peak start/end hours.
- `schedule_1` — full VA Schedule 1 tariff from `rates.py`; needs no user input.

`_calculate_cost()` prices a list of intervals (for sensors); `_calculate_interval_cost()` prices one interval (for statistics).

### External Statistics

Up to three external statistics per entry: `dominion_energy:{prefix}_energy_consumption` (kWh), `_energy_cost` (USD), and `_energy_generation` (kWh, only once the meter reports export). Inserted with `async_add_external_statistics` at hourly granularity aggregated from the 30-minute intervals.

`{prefix}` is resolved once in `_async_setup` and persisted under `CONF_STATISTIC_ID_PREFIX`: an entry that already has account-scoped statistics keeps them, otherwise the account-scoped prefix goes to a single claimant and siblings get `{account}_{meter}`. **Never re-derive this per cycle and never rename an existing prefix** — statistic IDs are how the Energy Dashboard finds history, and changing one orphans it.

Cost statistics are rebuilt when `CONF_COST_SIGNATURE` shows the cost-affecting options changed; the rebuild reseeds from the cumulative sum immediately *before* the rewritten window rather than from zero.

Sharp edges already handled — do not regress them:

- `_filter_incomplete_days()` drops days that are zero or barely populated, so partial days are not frozen into permanent zero statistics.
- `_deduplicate_hourly_by_utc()` merges local hours that collapse to the same UTC instant on DST fall-back/spring-forward days.
- `_find_last_complete_day_stat()` walks backwards past stale zero-value statistics written by older versions, and `_get_sum_before()` recovers the correct running sum when a day is re-processed.
- `_backfill_initiated` guards against a second backfill firing before the recorder has committed the first.

### Authentication and Reauth

- `TokenExpiredError` triggers `_async_attempt_reauth()`; only if that fails (or TFA is required) does the coordinator raise `ConfigEntryAuthFailed` and hand off to the reauth flow.
- `InvalidAuthError`, and `ApiError` with status 401/403, raise `ConfigEntryAuthFailed` directly.
- Gigya cookies are stored so reauth can often skip TFA.

### External Dependencies

- **`dompower`**: Python library for the Dominion Energy API. **Installed from the fork at `negative-video/dompower`, not from PyPI** — see below. Keep it in sync with the API surface used here.
- `recorder` is a Home Assistant dependency (external statistics).

#### Installing the forked `dompower`

The PyPI name `dompower` belongs to upstream, so the fork ships as a wheel attached to its own GitHub releases and `manifest.json` installs that asset by URL. Three files carry that URL and **must name the same release**: `manifest.json`, the `test-ha` extra in `pyproject.toml`, and mypy's `additional_dependencies` in `.pre-commit-config.yaml`.

Two forms of the same URL, and they are not interchangeable:

- `manifest.json` uses the **bare URL with a `#dompower==X.Y.Z` fragment**. Home Assistant parses this field itself (`homeassistant/util/package.py: is_installed`) and decides from it whether to install anything at all. Written the ordinary PEP 508 way — `dompower @ https://...` — the string parses as a requirement with an *empty* version specifier, which any installed version satisfies, so an existing PyPI `dompower` is judged good enough and the fork never installs. The fragment form fails that parse deliberately, sending HA down the branch that reads the version out of the fragment. Hassfest also rejects a requirement containing a space, which is the other reason the `name @ url` form cannot go here.
- `pyproject.toml` and `.pre-commit-config.yaml` are read by uv/pip, which want the normal `dompower @ https://...` (or a bare URL, in pre-commit's list) and do not need a fragment.

The version in the fragment must match the version the wheel actually carries. Name a version the wheel does not have and HA either never installs it or reinstalls it on every restart; the fork's release workflow fails the release rather than publish a mismatch.

**A change in the `dompower` working tree does not reach Home Assistant until it is released.** For local work against an unreleased library, install it over the top after syncing:

```bash
uv sync --frozen --extra dev --extra test-ha --python 3.14
uv pip install -e ../dompower
```

### Services

`dominion_energy.import_green_button` (see `services.yaml`) imports Green Button XML as statistics history, extending the Energy Dashboard past the API's ~68 day ceiling to roughly 13 months. It writes into the same statistic IDs and recomputes the whole cumulative sum chain, so the series stays continuous. Adding a service means adding its strings to **both** `strings.json` and `translations/en.json` under `services:`.

## Development Notes

### Home Assistant Integration Patterns Used

- `ConfigEntry.runtime_data` holds the coordinator; type alias `type DominionEnergyConfigEntry = ConfigEntry[DominionEnergyCoordinator]` (PEP 695, needs Python 3.12+).
- Platforms are `binary_sensor` and `sensor`, forwarded from `PLATFORMS` in `__init__.py`.
- Config entry unique ID is `f"{account_number}_{meter_device_id}"` so several meters on one account can each be added. `ConfigFlow.VERSION = 2`; the v1 → v2 unique ID migration lives in `__init__.py`.
- Token persistence via `hass.config_entries.async_update_entry()` inside the client callback.
- `ConfigEntryAuthFailed` triggers the reauth flow.
- Minimum Home Assistant version is **2025.11.0**, set in `hacs.json`. Driven by `StatisticMetaData["unit_class"]` (added in 2025.11) and `StatisticMeanType` (added in 2025.4). Bump it if you adopt newer core APIs.

### Testing

`pytest` (see `[tool.pytest.ini_options]` in `pyproject.toml`); run with `python3 -m pytest tests/ -q`. Tests are plain stdlib pytest and deliberately avoid importing `homeassistant`, so they run without a HA checkout.

- `tests/test_rates.py` — Schedule 1 tariff math and the effective-dated schedule registry. The engine tests pin `get_schedule_for_date(date(2026, 1, 1))` on purpose: their expected values come from that worksheet, so they are regression tests of the calculation, not of current rates.
- `tests/test_usage.py` — the pure helpers in `usage.py`, including the DST and billing-period regressions.
- `tests/test_features.py` — projection maths, rate-drift comparison, generation aggregation, statistic-ID resolution.
- `tests/test_sensor.py` — translation-key coverage in both directions, entity naming, device/state class legality, and the conditional-group gating.
- `tests/test_binary_sensor.py` — the same contract for the binary sensor platform, plus that "off" and "unknown" stay distinguishable.
- `tests/test_entity.py` — the unique-ID and device-identifier schemes, and that no platform reimplements either. Treat this the way `test_diagnostics.py` is treated: a regression here silently discards every user's recorded history.
- `tests/test_insights.py` — the derived metrics. Loads `insights.py` and `usage.py` into a private synthetic package so their relative imports resolve without executing the integration's `__init__.py`.
- `tests/astkit.py` — not a test module; the shared `ast` plumbing the source-inspecting tests use.
- `tests/test_diagnostics.py` — redaction. Treat this as security-relevant: it asserts no fake credential appears anywhere in the output.
- `tests/test_translations.py` — `translations/en.json` exists, matches `strings.json`, and covers every step, error, and abort reason parsed out of `config_flow.py`.
- `tests/test_green_button.py` — ESPI parsing and the timestamp realignment. Fixtures are generated in-process and reproduce Dominion's fixed-offset defect deliberately; a real export embeds an account number and a full hourly record of household occupancy and **must never enter the repository** (`.gitignore` covers `GreenButton*.xml`).

Two CI test jobs: `test` (Python 3.12/3.13, `dev` extra only) enforces that the suite stays runnable without Home Assistant, and `test-ha` (Python 3.14, `test-ha` extra) runs it against the pinned HA release. `pytest-homeassistant-custom-component` pins one exact HA version and needs Python >= 3.14.2, which is why it is an optional extra rather than a base dependency. Keep new tests importable without `homeassistant` — files that need it follow the loader pattern in `test_diagnostics.py`.

Not covered by tests: coordinator orchestration that genuinely needs a `hass` object (statistic-prefix resolution against a live recorder, backfill branch selection, the entity-registry probe in `entity.py`, and the recorder read in `_async_hvac_windows`), and the config flow state machine. Verifying those needs a real HA instance and a mocked `DompowerClient`.

Lint and format run over `custom_components/` **and** `tests/`, with Ruff pinned in both CI and pre-commit — keep those two versions in sync. `[tool.ruff.lint.isort]` in `pyproject.toml` mirrors Home Assistant's import convention; without it Ruff's defaults reorder imports away from the style used throughout this integration.

### HACS Distribution

- `hacs.json` holds HACS metadata, including the minimum Home Assistant version.
- `manifest.json` carries the integration version and the `dompower` requirement URL.
- This repo is a fork of `YeomansIII/ha-dominion-energy`; changes are intended to go back upstream, so keep the documentation/issue-tracker URLs and coding style as they are.
