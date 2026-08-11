# Rate schedules (VA Residential Schedule 1)

`custom_components/dominion_energy/rates.py` encodes Dominion Energy Virginia's
Residential Schedule 1 tariff so the integration can estimate cost from
30-minute interval usage.

Rates change several times a year. The module therefore holds a **list of
effective-dated schedules** (`VA_SCHEDULE_1_HISTORY`) rather than one set of
numbers, and `get_schedule_for_date(d)` returns the schedule that was in effect
on a given day. This matters because historical backfill spans up to ~68 days
and can cross a rate change.

`VA_SCHEDULE_1` remains a module-level alias for the currently-effective
schedule, so existing callers keep working. Anything pricing historical usage
should call `get_schedule_for_date()` per interval instead.

## Where the numbers come from

All rates are per kWh in **dollars**. Dominion publishes them in **cents**, so
everything is divided by 100 on the way in.

| # | Source | What it gives you |
|---|--------|-------------------|
| [1] | [Schedule 1 tariff PDF](https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/residential/schedule-1.pdf) | Basic customer charge, distribution kWh charges, generation kWh charges, transmission kWh charge, and the tier boundary (800 kWh) |
| [2] | [Fuel Charge Rider A PDF](https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/shared/rider-a.pdf) | The fuel factor — the single largest and most volatile component |
| [3] | [Exhibit of Applicable Riders PDF](https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/shared/exhibit-of-applicable-riders.pdf) | Which riders apply to Schedule 1, and the effective date of each |
| [4] | [Bill calculation worksheet (.xlsx)](https://www.dominionenergy.com/-/media/content/paying-my-bill/understand-my-bill/files/bill-calculator-worksheet-va.xlsx) | **Every rider rate and its effective date, in one place.** Linked from [Understand My Bill](https://www.dominionenergy.com/virginia/paying-my-bill/understand-my-bill) |

Source [4] is the practical one. Its rate table lives in a **hidden worksheet
tab called "Rate Update Sheet"** — unhide it in Excel/LibreOffice, or read it
without a spreadsheet app:

```bash
python3 - <<'EOF'
import re, zipfile, datetime
z = zipfile.ZipFile("bill-calculator-worksheet-va.xlsx")
ss = z.read("xl/sharedStrings.xml").decode("utf8", "ignore")
S = [
    "".join(re.findall(r"<t[^>]*>(.*?)</t>", s, re.S))
    for s in re.findall(r"<si>(.*?)</si>", ss, re.S)
]
sheet = z.read("xl/worksheets/sheet7.xml").decode("utf8", "ignore")  # Rate Update Sheet
rows = {}
for m in re.finditer(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', sheet, re.S):
    col, r, attrs, inner = m.group(1), int(m.group(2)), m.group(3), m.group(4) or ""
    v = re.search(r"<v>(.*?)</v>", inner)
    if not v:
        continue
    val = S[int(v.group(1))] if 't="s"' in attrs else v.group(1)
    rows.setdefault(r, {})[col] = val
print("Last Updated:",
      datetime.date(1899, 12, 30) + datetime.timedelta(days=int(rows[32]["D"])))
for r in sorted(rows):
    d = rows[r]
    try:  # skips the header row, whose cells are text
        rate = float(d["K"]) / 100
        eff = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(d["M"])))
    except (KeyError, ValueError):
        continue
    print(f"{d.get('J', ''):<12} {rate:.6f} $/kWh   effective {eff}")
EOF
```

Column J is the rider name, K its rate in cents/kWh, M its effective date.
The base Schedule 1 charges are in columns A–G of the same tab, and the
consumption-tax tiers ("Total Tax Rate") are in columns X–AC. Cell B32 is the
worksheet's own "Last Updated" stamp — check it before trusting the file.

`docs/bill-calculator-worksheet-va.xlsx` is a pinned copy of the 2025-12-19
version, which is where the `2026-01-01` schedule came from.

## Verification status

Every schedule below carries a `source_url` and `source_retrieved` field.

On 2026-08-11 each rider rate in the current set was cross-checked against its
own tariff PDF (`.../virginia/shared/rider-<code>.pdf`), not just against the
worksheet. **13 of 14 matched exactly**; the one exception is Rider RBB, which
no longer exists as a tariff document — see the caveats below.

| Schedule | Status | Notes |
|---|---|---|
| `2026-01-01` | Verified | From the pinned 2025-12-19 worksheet [4]. Base charges independently confirmed against [1]. |
| `2026-03-01` | Verified | Adds Rider CERC at 0.0754¢/kWh; effective date confirmed in [3]. |
| `2026-04-01` | Verified | Rider GEN 0.7564¢ → 0.5729¢. |
| `2026-05-01` | Verified | Rider CE 0.3668¢ → 0.6054¢. |
| `2026-06-01` | Verified | Rider DIST 0.6241¢ → 0.7685¢; Rider RBB 0.0531¢ → 0. |
| `2026-07-01` | Verified | **Fuel factor: Rider A 2.968¢ → 3.7648¢.** Confirmed independently in [2] ("Filed 05-29-26 … Effective For Usage On and After 07-01-26 On An Interim Basis" → "increased by 3.7648 cents per kilowatt-hour") and in worksheet [4]. |
| `2026-08-01` | Verified | Rider DFCC 0.2906¢ → 0.2901¢. |

Notes and caveats:

- The **base charges did not change** between the 01-01-26 and 07-01-26 tariff
  filings. Both filings of [1] state the same customer charge ($7.58),
  distribution (3.5690¢ / 2.3596¢), generation (summer 3.1212¢ / 4.6243¢;
  winter 3.0064¢ / 2.6965¢) and transmission (0.970¢). The 2026 increases
  arrived entirely through riders.
- The 2026-03-01 through 2026-08-01 schedules are **reconstructed** by taking
  the current rate table from [4] and applying each rider from its own stated
  effective date. Every individual rate and date is sourced, but the composite
  sets were not observed as published snapshots. Only the newest one
  (`2026-08-01`) matches a worksheet Dominion actually published.
- **Rider A is interim.** [2] says the 07-01-26 fuel factor is effective "On An
  Interim Basis", so the SCC may still true it up. Re-check after the final
  order.
- Riders Dominion lists at 0.000¢ for Schedule 1 (PIPP, RGGI) are omitted from
  the encoded lists — they contribute nothing. RBB is retained at 0.0 because
  it was non-zero in an earlier schedule.
- **Rider RBB is the one unverified component.** `rider-rbb.pdf` returns 404,
  and the string "RBB" does not appear anywhere in the 375-page
  [entire filed tariff](https://www.dominionenergy.com/-/media/content/rates-and-tariffs/pdfs/virginia/shared/entire-filed-tariff.pdf)
  (revised 08-01-26). Search engines still surface the old URL with a 0.0531¢
  snippet; that is a stale cache and was not treated as evidence. Dominion's
  worksheet [4] carries RBB at 0.0000¢ effective 2026-06-01, consistent with
  the rider having been folded into Rider DIST (which itself takes a new rate
  on 06-01-26). The encoded 0.0 is therefore very likely right but rests on
  [4] alone.
- The worksheet's effective dates for **T1 and RGGI are one filing stale**
  (it says 2025-09-01 and 2024-07-12; the current PDFs say 07-01-26 and
  04-01-25). Both riders' *rates* are unchanged across those filings, so
  nothing here is mispriced — but do not treat worksheet dates as
  authoritative when deciding where a schedule boundary falls. Check the
  rider PDF header.
- **Net metering is not modelled.** Rider T1 carries a $1.605/kW demand charge
  that applies to Schedule 1 net-metering installations above 20 kW AC, which
  pay the greater of the T1 energy charge or the T1 demand charge. Schedule 1
  itself likewise adds distribution and transmission standby charges for those
  customers. A flat per-kWh model under-bills that subset.
- Riders split into two columns in the tariff: DIST, C1A and C4A are "cents per
  **Distribution** kWh charge"; A, GEN, CE, CERC, E, SNA, OSW, SMR and T1 are
  "cents per **Electricity Supply** kWh charge". The integration sums them, so
  the distinction does not matter today — but it is the line to split on if
  supply/delivery subtotals are ever added.
- Securitization interest (reported in the press as roughly +$1.80/month for a
  1,000 kWh customer, expected as early as October 2026) is **not** encoded.
  It had not appeared in [3] or [4] as of 2026-08-11. When it lands it will
  show up as a new or re-rated rider in the worksheet.
- The tariff prorates differently for bimonthly bills (doubling both the
  customer charge and the 800 kWh tier boundaries). That is not modelled.

## Adding a new dated schedule

When the SCC approves a change:

1. Download the current worksheet [4] and check its "Last Updated" stamp
   (cell B32 of the "Rate Update Sheet" tab).
2. Dump the rider table with the snippet above and diff it against the newest
   `_RIDERS_*` list in `rates.py`.
3. For each rider that moved, note its **effective date** from column M — that
   is the date the new schedule starts, not the date you downloaded the file.
4. Add a new `_RIDERS_<date>` built from the previous one:

   ```python
   _RIDERS_2027_07_01 = _riders(_RIDERS_2026_08_01, changed={"Fuel/A": 0.0412})
   ```

   `changed` raises `KeyError` for a name that does not already exist, so a
   typo fails loudly. Use `added={...}` for genuinely new riders.
5. Append a `_schedule("2027-07-01", _RIDERS_2027_07_01, <source url>)` entry to
   `VA_SCHEDULE_1_HISTORY`. **Keep the tuple sorted by effective date** — a
   module-level assertion enforces this.
6. Bump `_RETRIEVED` to the date you checked.
7. If a *base* charge changed (customer charge, distribution, generation,
   transmission, tier boundary), the `_schedule()` factory no longer fits;
   those constants are shared across all schedules today because they have not
   moved. Give the new schedule its own explicit `RateSchedule(...)` and cite
   the tariff filing from [1].
8. Update the verification table above.
9. Run `python3 -m pytest tests/test_rates.py -q`. The plausibility test
   (`test_1000kwh_summer_month_is_in_a_defensible_range`) is deliberately wide
   and should not need editing; if it trips, re-check the arithmetic before
   widening it.

## What to re-check, and when

| When | What |
|---|---|
| Annually, **around July 1** | The fuel factor (Rider A). This is the big one — it moved +$7.97/month for a 1,000 kWh customer in 2026. |
| Whenever a `Rider A` filing is marked "interim" | Watch for the SCC's final order superseding it. |
| Quarterly | Diff the whole rider table in [4]. Riders are re-filed on their own schedules throughout the year (2026 saw changes in March, April, May, June, July and August). |
| On a base rate case | Schedule 1 itself [1]. Rare, but changes the customer charge and the kWh charges. |

## Staleness self-defence

`rates.py` exposes a few dependency-free signals so the rest of the integration
can notice drift without anyone remembering to read this file:

- `LATEST_SCHEDULE_EFFECTIVE_DATE` — effective date of the newest encoded
  schedule. Good diagnostic-attribute material.
- `RateSchedule.effective_from` / `.source_url` / `.source_retrieved` — the
  provenance of the schedule actually in use.
- `is_schedule_possibly_stale(today=None, max_age_days=400)` — true when the
  newest encoded schedule is older than ~13 months, which (given the annual
  July fuel factor) means a filing was missed.
- `days_since_schedule_change(today=None)` — how old the newest schedule is.
- `calculate_schedule1_period_bill(total_kwh, period_start, period_end)` —
  computes a whole-period bill from a single total kWh figure, the way a paper
  bill does, returning a `PeriodBill` broken out by component.
- `bill_discrepancy(estimated, actual)` — signed fractional error. Feed it
  `PeriodBill.total` and the API's `last_bill.charges`; a persistent few-percent
  gap is the signal that the rate table here is behind a filing.
