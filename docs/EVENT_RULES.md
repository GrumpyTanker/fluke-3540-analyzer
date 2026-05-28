# Event-detection rules

The Python (`events.py`) and JavaScript (`events.js`) detectors share the
same rule set. Thresholds are tunable via the `EventRules` dataclass
(Python) or the `opts.rules` object (JS); defaults follow IEEE 1159 and
NEMA where applicable.

## Detection pipeline

For each record we extract per-phase L-N voltage (`min`, `max`, `avg`),
per-phase current `max`, frequency `avg`, and `P_total_avg_W`. A
"not-outage" mask is built first (avg voltage > 50 V on **every** phase)
and used to gate the other detectors so they don't double-classify the
outage window.

Detected ranges are merged with a gap tolerance of 1 second so a brief
recovery doesn't split one logical event into two.

## Event kinds

### `outage`

- **Rule:** average L-N voltage falls below `outage_v_threshold` (default 50 V) on **all three phases**.
- **Min duration:** `min_duration_secs` (default 1 s).
- **Severity:** deepest min L-N voltage seen during the window (V; lower = worse).
- **Affected phases:** always `a, b, c`.

### `dip`

- **Rule:** `V_LN_min` on **any** phase drops below `nominal_ln_v * dip_pct_of_nominal` (default 90 %), while **not** in an outage window.
- **Severity:** deepest dip as a fraction of nominal (`min_V / nominal_ln_v`; lower = worse).
- **Affected phases:** every phase that dipped.

### `swell`

- **Rule:** `V_LN_max` on **any** phase exceeds `nominal_ln_v * swell_pct_of_nominal` (default 110 %), while not in an outage.
- **Severity:** highest swell as a fraction of nominal (`max_V / nominal_ln_v`; higher = worse).
- **Affected phases:** every phase that swelled.

### `high_current`

- **Rule:** per-phase, `I_max > mean(I_max) + N · pstdev(I_max)` where `N = high_current_sigma` (default 2.0). The mean and stdev are computed over the non-outage samples only.
- **Severity:** peak A during the window.
- **Affected phases:** the single phase that triggered.

The default `N=2` works well for steady loads. Variable loads (EV chargers,
process machinery) often have a long tail of high current that pulls the
stdev up, so genuine peaks may not trip the threshold. If your load is
spiky, lower `N` or use a percentile-based fork.

### `freq_excursion`

- **Rule:** `|freq − nominal_freq_hz|` exceeds `freq_excursion_hz` (default 0.5 Hz off a 60 Hz nominal).
- **Severity:** signed deviation in Hz (negative = under-frequency).
- **Affected phases:** none (frequency is a system-wide quantity).

### `imbalance_spike`

- **Rule:** instantaneous NEMA voltage imbalance percentage exceeds `imbalance_pct_threshold` (default 2.5 %). NEMA % = `(max(V_avg) − min(V_avg)) / mean(V_avg) × 100`.
- **Severity:** peak % during the window.
- **Affected phases:** always `a, b, c` (it's an across-phase metric).

### `power_step`

- **Rule:** 1-sec `|ΔP_total|` exceeds `power_step_pct_of_mean` (default 50 %) of the session's mean `|P_total|` over non-outage samples.
- **Severity:** signed step in W (positive = sudden import, negative = sudden export).
- **Affected phases:** always `a, b, c`.

## Auto-inferred nominal voltage

If `nominal_ln_v` is not provided, the detector takes the **median** of every
per-phase L-N average voltage, after masking out samples below
`outage_v_threshold`. This works for 120 V, 230 V, 277 V, and other common
wye systems without configuration.

Pass `--nominal-ln-v 277` (CLI) or `{ nominalLnV: 277 }` (JS) to override.

## Customising rules in Python

```python
from dataclasses import replace
from fluke_3540.events import DEFAULT_RULES, detect_events

# Looser swell threshold to catch >105% events
loose = replace(DEFAULT_RULES, swell_pct_of_nominal=1.05)
events = detect_events(records, nominal_ln_v=277, rules=loose)
```

## Customising rules in JS

```javascript
import { detectEvents } from './events.js';
const events = detectEvents(records, spec, {
  nominalLnV: 277,
  rules: { swell_pct_of_nominal: 1.05 },  // merged with DEFAULT_RULES
});
```
