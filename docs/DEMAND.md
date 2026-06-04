# Demand analysis (`--demand-window`)

Utilities bill on **demand** — a sliding/block-window average of real power, not
the instantaneous peak. The 15-minute interval is the most common. This
analyzer computes a trailing rolling mean of `P_total_avg_W` and reports the
peak demand and when it occurred.

```bash
fluke-analyze ES.004 --demand-window 900   # 900 s = 15 min (default)
```

## What it computes

For a window of `W` seconds (1 record = 1 s), at each index `i ≥ W−1` the
trailing demand is `mean(P_total over the last W records)`. The analyzer reports:

| Field | Meaning |
|---|---|
| `peak_demand_w` / `peak_demand_kw` | the highest rolling-window demand |
| `peak_window_start` / `peak_window_end` | the window that produced the peak |
| `mean_demand_w` | mean of all full-window demands |
| `n_windows` | number of full windows evaluated |
| `series` | an optional decimated demand series for charting |

Non-finite `P` samples are treated as 0 in the running sum. If the session is
shorter than one window, no peak is reported (`n_windows = 0`).

## Outputs

- CLI: `demand.json` + a one-line `[demand]` summary + Peak-demand rows in the
  XLSX **Summary** sheet.
- Web: surfaced in the Statistics panel and the exported HTML report
  (`web/analysis.js → demandAnalysis`).

## Notes

- The window is **trailing** (right-aligned), matching how interval meters
  accumulate demand within each interval.
- For true utility block-demand (fixed 15-min boundaries) rather than a sliding
  window, split first with `--split-by 15m` and read each bucket's mean kW.
