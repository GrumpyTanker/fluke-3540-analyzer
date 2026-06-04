# Power-quality standards: IEEE 519 & IEEE 1159 / SARFI

This analyzer reports two standards-based power-quality summaries alongside the
event log and whole-session statistics. Both are computed identically in the
Python CLI and the web app (parity-tested).

## IEEE 519-2014 — harmonic (THD) limits

IEEE 519 sets voltage-distortion limits at the point of common coupling. For
systems at or below 1 kV (the case for the 3540 FC's typical 277/480 V service):

| Quantity | Limit |
|---|---|
| Total voltage THD | **8.0 %** |
| Planning level / single-harmonic guidance | **5.0 %** |

**How it is assessed.** IEEE 519 evaluates compliance against the 95th
percentile of the measured distortion, not the instantaneous peak. The analyzer
therefore reports the **p95 of `V_THD_pct_<phase>_avg`** per phase and marks a
phase:

- `compliant` when p95 ≤ 8.0 %,
- `exceeds_planning` when p95 > 5.0 % (a yellow flag even if still compliant).

`all_voltage_compliant` is true only when all three phases pass.

**Current THD.** IEEE 519 current limits are expressed as TDD (total demand
distortion) and depend on the short-circuit ratio Isc/IL, which the meter does
not record. The analyzer therefore reports **p95 of `I_THD_pct_<phase>_avg`**
per phase as informational context, without a hard pass/fail.

Output: `pq_standards.json → ieee519`.

## IEEE 1159 / IEEE 1564 — SARFI indices

The **System Average RMS (variation) Frequency Index, SARFI-X**, counts the
number of voltage variation events whose **residual voltage dipped below X % of
nominal**. For a single monitoring point (one meter, one asset) the index is the
event count itself.

The analyzer reports the standard magnitude thresholds:

| Index | Counts events with residual voltage below |
|---|---|
| SARFI-90 | 90 % (i.e. any dip ≥ 10 %) |
| SARFI-80 | 80 % |
| SARFI-70 | 70 % |
| SARFI-50 | 50 % |
| SARFI-10 | 10 % (near-interruption / outage) |

Residual voltage is taken from each detected `dip` (severity = residual
fraction) and `outage` (deepest L-N voltage ÷ nominal). Swells and non-voltage
events are excluded. Because the bins are cumulative, SARFI-90 ≥ SARFI-80 ≥ … ≥
SARFI-10 by construction.

Output: `pq_standards.json → sarfi`.

## Where it shows up

- **CLI:** a one-line `[pq]` summary during analysis; full detail in
  `pq_standards.json`.
- **Web:** computed inline from the resident ColumnStore via
  `web/analysis.js` (`ieee519Compliance`, `sarfiIndices`).
- **Reports:** surfaced in the HTML/XLSX statistics area.

## Caveats

- THD field semantics are confidence `M` in `spec/field_map.json`; treat the THD
  numbers as strong-inference until cross-checked against a reference meter.
- SARFI here is a single-site event count, not the multi-site customer-weighted
  utility metric. It is directly comparable across captures of the same asset.
