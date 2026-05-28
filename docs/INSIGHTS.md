# Insights — rule-based session analysis

The Insights engine cross-correlates Events, Snapshots, and the per-second
record stream into human-readable **Findings**. It's deterministic, local,
and fast — no cloud LLM, no API key, runs entirely in the browser or in
the CLI process.

The thresholds live in [`spec/field_map.json`](../spec/field_map.json) →
`insight_rules` so both implementations (`python/.../insights.py` and
`web/insights.js`) stay in lockstep.

## Finding shape

```python
@dataclass(frozen=True)
class Finding:
    id: int                            # sequential, time-ordered
    kind: str                          # one of the rule kinds below
    severity: str                      # "info" | "warn" | "alert"
    headline: str                      # one-line summary
    detail: str                        # multi-sentence explanation
    related_event_ids: tuple[int, ...] # links into the Events table
    recommended_actions: tuple[str, ...]
```

The JS equivalent uses camelCase (`relatedEventIds`, `recommendedActions`).

Findings are sorted alert → warn → info, then alphabetical by kind, and
re-issued sequential ids so external references are stable.

## Rules

### `outage_signature`

For each outage event, look in a ±N second window (default 30 s) for a
**leading dip**, **trailing high-current event** (restoration inrush),
and **trailing dip** (reclose flicker). Builds a caption like:

> Outage at 13:21:51 (424 s). Outage at 2024-01-14T13:21:51.174112+00:00
> lasted 424 s, with a leading dip on phase(s) a/b/c (0 V) and a trailing
> dip on phase(s) a/b/c.

Severity is `alert` for outages ≥ 60 s, otherwise `warn`.

If nothing surrounds the outage, the caption notes it's consistent with
a **hard supply loss** (no slow brown-out, no restart inrush).

### `phase_asymmetry`

Per-phase L-N voltage average across non-outage samples; report when
`(max − min) / mean × 100` exceeds `phase_asymmetry_pct` (default 2.0 %).

Severity `alert` at ≥ 3 %, otherwise `warn`.

### `pf_drift`

Fraction of non-outage time where `|PF_total_avg| < pf_drift_threshold`
(default 0.85). Trips when that fraction crosses `pf_drift_min_fraction`
(default 0.10).

Recommends a PFC bank sized by the mean reactive demand during the
low-PF periods. Severity `alert` when low-PF time exceeds 30 % of
operation, otherwise `warn`.

### `imbalance_sustained`

NEMA voltage imbalance (`(max − min) / mean × 100`) above
`imbalance_sustained_pct` (default 1.5 %) for at least
`imbalance_sustained_secs` consecutive seconds (default 60). Reports
total seconds + number of windows + peak imbalance.

### `freq_source_stiffness`

Counts `power_step` events that coincide with a frequency deviation
> `freq_stiffness_hz` (default 0.05 Hz) inside a ±2-second window. Trips
when ≥ `freq_stiffness_min_count` (default 3) such correlations are
found.

Implication: the upstream impedance is high enough that load changes
move the line frequency — typical of generators / weak feeders.

### `outage_frequency`

`(outage count) / (session duration in days)`. Trips above
`outage_frequency_per_day` (default 1.0/day); severity `alert` at twice
the threshold.

### `current_spike_ratio`

Per phase, `peak_I_max / mean_I_max` across non-outage samples. Reports
when the ratio exceeds `current_to_mean_ratio_alert` (default 5.0). Tags
as `info` — useful operational context, not necessarily a fault.

## Where the findings appear

- **CLI**: `summary.txt` opens with an Insights block; `insights.json`
  is the full structured form; `--json` adds an `insights` top-level key.
- **HTML report**: an Insights section between Summary and Events, with
  severity-colored cards.
- **PDF report**: severity-bar cards on dedicated pages.
- **Web UI**: an Insights section above the Events table. Cards have
  expandable "Recommended" sections and clickable "Related events" links
  that scroll + highlight the matching event row.

## Customising thresholds

Edit `spec/field_map.json` → `insight_rules`. Both implementations pick
up the change at next run (Python at module import; JS on the next
analyzeInsights call).

Example — make swell-equivalent recommendations fire on slighter
voltage spreads:

```json
"insight_rules": {
  "phase_asymmetry_pct": 1.0,
  ...
}
```

For one-off tuning without editing the spec, pass an `EventRules`
override in Python:

```python
from dataclasses import replace
from fluke_3540.events import DEFAULT_RULES, detect_events
from fluke_3540.insights import analyze
loose = replace(DEFAULT_RULES, swell_pct_of_nominal=1.05)
events = detect_events(records, rules=loose)
findings = analyze(records, events)
```

## Extending with new rules

Add a `_rule_<name>(...) -> list[Finding]` function in
`python/.../insights.py`, call it from `analyze()`, and write a parity
function in `web/insights.js`. Add the relevant thresholds to
`spec/field_map.json`.
