# Energy cost / TOU tariff

The web app's **Energy cost (tariff)** section computes dollar cost from
the existing per-second `Wh_total` measurements. Hour-of-day-based
peak / off-peak schedules are supported in v0.4; weekday/weekend and
TOU-3 schedules are v0.5 candidates.

## How it works

For each record:

1. Look up the UTC hour from the record's timestamp.
2. Decide whether that hour falls inside any of the peak windows
   (`peakHours` list). If yes, bill at `peakRate $/kWh`; otherwise
   `offpeakRate $/kWh`.
3. `Wh_total / 1000 × rate` gives the per-second cost contribution.

Positive Wh = imported energy (you paid). Negative Wh = exported energy
(you got credited). Both flow into the imported / exported / net cost
totals.

## Peak hours format

Comma-separated `HH-HH` (or `HH:MM-HH:MM`) ranges in 24-hour UTC.

| Input               | Meaning                              |
|---------------------|---------------------------------------|
| `09-21`             | Peak 09:00–21:00 UTC                  |
| `09:00-12:00, 14-18`| Peak 09:00–12:00 and 14:00–18:00 UTC |
| `22-6`              | Peak 22:00–06:00 next day (wraps midnight) |

Empty / missing peak hours → everything bills at the off-peak rate
(useful for simple flat-rate tariffs).

## Result fields

The web Summary shows:

| Row                       | What                                         |
|---------------------------|----------------------------------------------|
| Imported cost             | `peak_imported_kwh × peakRate + offpeak_imported_kwh × offpeakRate` |
| Exported cost             | `peak_exported_kwh × peakRate + offpeak_exported_kwh × offpeakRate` (negative) |
| Net cost                  | Imported + Exported                          |
| Peak / off-peak kWh       | Total kWh moved (import + abs export) in each tariff band |
| Peak / off-peak cost      | Cost contribution from each tariff band     |

## Persistence

Tariffs persist in `localStorage` keyed by `tariff:<asset_name>` (the
asset name comes from your `ES.NNN-config.json` companion file, or
"default" if none is set). Different sites get separate saved tariffs.

The optional **Main breaker (A)** input under the tariff section also
persists, keyed by `breaker:<asset_name>`, and feeds the
`current_spike_ratio` and `breaker_margin` rules in the Insights engine
(see [`INSIGHTS.md`](INSIGHTS.md)).

## Limitations and v0.5 candidates

- Single peak / off-peak split only (no TOU-3 / TOU-4).
- No weekday/weekend differentiation.
- No demand charges (peak-kW charges separate from kWh).
- No holiday schedules.
- No CLI YAML config — the `tariff.py` module ships the engine; CLI
  glue waits for v0.5.

If you need any of these, file an issue with your tariff structure and
we'll prioritise.
