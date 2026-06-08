# Generalized shift splitting (`--split-by shifts`)

Define **named time periods within a day** — which may wrap past midnight — and
compare power usage across them (day vs night, or A/B/C shifts). This is the
generalized successor to the clock-aligned `--split-by hour|day|week`: instead of
fixed grid buckets, you define the windows that match how the plant actually
runs.

```bash
# Default day/night
fluke-analyze ES.004 --split-by shifts --tz America/Chicago

# Explicit two-shift
fluke-analyze ES.004 --split-by shifts \
  --shifts "day=06:00-18:00,night=18:00-06:00" --tz America/Chicago

# Three 8-hour shifts
fluke-analyze ES.004 --split-by shifts \
  --shifts "A=06:00-14:00,B=14:00-22:00,C=22:00-06:00" --tz America/Chicago

# From a committed schedule file
fluke-analyze ES.004 --split-by shifts --shifts-file shifts.json --tz America/Chicago
```

## Defining shifts

A shift is `name=HH:MM-HH:MM`. Multiple shifts are comma-separated. **A window
where `end <= start` wraps past midnight**: `night=18:00-06:00` covers
`[18:00, 24:00)` plus `[00:00, 06:00)`. Windows are half-open: start inclusive,
end exclusive.

- **Two shifts:** `day=06:00-18:00,night=18:00-06:00` (the default when
  `--split-by shifts` is given with no `--shifts`/`--shifts-file`).
- **Three shifts:** `A=06:00-14:00,B=14:00-22:00,C=22:00-06:00`.

### File form (`--shifts-file`)

```json
{
  "shifts": [
    {"name": "day",   "start": "06:00", "end": "18:00"},
    {"name": "night", "start": "18:00", "end": "06:00"}
  ]
}
```

A bare top-level list of `{name, start, end}` objects is also accepted.

## Timezone contract (important)

Captured timestamps are stored in **UTC**. Shift windows are interpreted in the
**report timezone** set by `--tz` (e.g. `America/Chicago`). Each record's
timestamp is localized to that zone *before* the `HH:MM` rule is applied. If you
omit `--tz`, windows are evaluated in **UTC** and the output says so. Always pass
`--tz` so a `06:00-18:00` shift means 6 AM local, not 6 AM UTC.

Validated on a real Central-time session: `day=06:00-18:00,night=18:00-06:00`
splits exactly on 06:00 / 18:00 **Central**, 43 200 one-second records per
half-day.

## Assignment rules

- Each record is assigned to the **first** window it matches (so overlapping
  windows resolve deterministically).
- Records that match **no** window go to an `unassigned` shift, with a printed
  notice.
- If the windows don't tile 24 h, a gap/overlap **warning** is printed to
  stderr (and recorded in `shift_comparison.json → coverage_issues`). Shifts
  ideally tile the day with no gaps or overlaps.

## Outputs

### 1. `shift_comparison.csv` / `shift_comparison.json` — the headline

One row **per shift name**, aggregating **all** records of that shift across the
whole session (a "night" recurs daily; those non-contiguous records are gathered
into one row). This is what lets you compare day vs night usage.

| Column | Meaning |
|---|---|
| `shift` | shift name (or `unassigned`) |
| `window` | the `HH:MM-HH:MM` window |
| `records` | number of records in the shift |
| `hours` | hours covered (1 record = 1 s) |
| `kWh` | energy = mean power × hours |
| `P_total_avg_W` / `P_total_min_W` / `P_total_max_W` | real-power avg/min/max |
| `peak_demand_kW` | peak rolling demand within the shift (15 min default; from `--demand-window`) |
| `peak_demand_window_secs` | the demand window used |
| `PF_avg` | average power factor |
| `V_LN_avg_V` / `V_LN_p5_V` / `V_LN_p95_V` | L-N voltage avg + p5/p95 (outage zeros excluded) |
| `V_THD_p95_pct` | 95th-percentile voltage THD |
| `n_outages` / `n_dips` / `n_swells` | event counts (filed by event start time) |
| `outage_minutes` | total outage minutes in the shift |

The JSON also carries `tz`, `spec`, `demand_window_secs`, and `coverage_issues`.

### 2. Per-occurrence buckets — `<out>/shifts/<name>_<date>/`

Each **individual** shift instance becomes its own contiguous, time-ordered
bucket reusing the standard per-bucket report machinery (`session.csv`,
`events.json` with ITIC, `summary.txt`). A night spanning midnight is **one**
occurrence labeled by its **start date** (e.g. `night 2026-05-29`).

A shift-comparison table is also embedded in the top-level `summary.txt`.

## Parity

The shift model (parse, midnight-wrap, tz-localized assignment, aggregate,
occurrences, comparison) is ported to the web side (`web/analysis.js`:
`ShiftSet`, `aggregateShifts`, `shiftOccurrences`, `shiftComparisonRows`) and
covered by a Python↔JS parity test on a shared synthetic multi-day fixture
(`web/tests/shifts_parity.test.js` vs `analysis_golden.json`).
