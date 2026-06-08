# Load states: active vs standby (`load_states`)

Many factory loads are **bimodal** — they alternate between a heavy *active*
draw and a light *standby* state. Blending the two into one session mean buries
the real consumption and produces a meaningless average power factor. The
load-state report classifies every record as **active** or **standby**, reports
the two states separately, surfaces the **active-state PF**, and corrects the
session energy.

This was built from the real P115RE coating-rectifier session (ES.004,
`--reverse-cts`, `America/Chicago`):

| state   | duty | I/phase | P_total | P1 (fundamental) | PF    |
|---------|------|---------|---------|------------------|-------|
| active  | ~49% | ~239 A  | +97 kW  | —                | +0.47 |
| standby | ~47% | ~16 A   | −7.6 kW | −7.7 kW          | −0.64 |

## Why classify by *current*, not power

The whole question is whether the **power sign** is trustworthy. With the global
`--reverse-cts`, the active state reads +97 kW (correct — that is the coating
draw), but the standby state reads −7.6 kW. A rectifier in standby should draw
small *positive* core/copper losses, not export. The standby reading is balanced
across all three phases and the **fundamental** P1 is also negative (−7.7 kW), so
it is not a harmonic artifact — it is simply that **no single CT polarity makes
both states physical**, and the low-current sign is unreliable.

So we gate on **current**, which is unambiguous:

> A record is **active** when its mean per-phase average current
> `(I_a_avg + I_b_avg + I_c_avg) / 3` is **≥ the threshold** (default **50 A**),
> otherwise **standby**.

A single dropped phase is ignored (the mean is taken over the finite phases), so
one NaN does not drag a record into standby. The threshold is configurable with
`--standby-threshold-a N`. A single threshold is used (no transition band); set
it between the two clusters — for the P115RE the active state is ~239 A and the
standby state is ~16 A, so the 50 A default sits comfortably between them.

## The three energy figures

Because the standby sign is unreliable, the as-measured signed energy is
**understated** (the bogus −7.6 kW standby subtracts). The report surfaces all
three figures explicitly — it never silently changes the historic number:

- **`energy_as_measured_kWh`** — the signed sum of `P_total_avg_W` over all
  records. This is the existing/historic behavior. *(P115RE: ~6,054 kWh.)*
- **`energy_active_kWh`** — energy from the **active** records only.
  *(P115RE: ~6,638 kWh.)*
- **`energy_net_clip_standby_kWh`** — active records pass through unchanged;
  standby real power is **clipped to ≥ 0** (a rectifier in standby never
  exports). *(P115RE: ~6,684 kWh.)*

All three use the tool's standard convention: per record (1 s) energy =
`P_total_avg_W / 1000 / 3600`, summed; non-finite samples are skipped.

> **Caveat (carried in every output):** standby real-power sign is unreliable at
> low current, so the **active** and **clip** figures are the defensible
> consumption — not the as-measured signed sum.

## Headline power factor

For a bimodal load the blended whole-session PF is meaningless (the P115RE
blended PF is −0.09). The **meaningful** figure is the **active-state PF**
(~0.47), so that is what the narrative, summary, and HTML report headline. The
raw whole-session PF is still reported, but de-emphasized and labeled.

## Outputs

`load_states.csv` — one row per state (active, then standby):

| column          | meaning                                  |
|-----------------|------------------------------------------|
| `state`         | `active` or `standby`                    |
| `records`       | record count in the state                |
| `hours`         | hours (records / 3600)                   |
| `duty_pct`      | percent of all records in the state      |
| `kWh`           | mean P × hours for the state             |
| `P_avg_kW`      | mean real power (kW)                      |
| `P_min_kW`      | min real power (kW)                       |
| `P_max_kW`      | max real power (kW)                       |
| `I_avg_A`       | mean per-phase current (A)               |
| `S_avg_kVA`     | mean apparent power (kVA)                 |
| `PF_avg`        | mean power factor                        |
| `V_LN_avg_V`    | mean L-N voltage (outage zeros excluded) |
| `V_THD_p95_pct` | 95th-percentile V_THD                    |

`load_states.json` carries the same `states` rows plus `standby_threshold_a`,
the three `energy` figures, and the caveat `note`. A compact load-state table
(and the three energy figures) is embedded in `summary.txt` and the HTML report.

The report is emitted automatically (it is cheap — a couple of streaming
passes); `--load-states` is accepted as an explicit opt-in but is not required.

## Integration with shifts

When `--split-by shifts` is used, each shift row in `shift_comparison.csv/json`
also carries its **active** load, using the same current cut:

- `active_records` — active record count in the shift
- `active_duty_pct` — percent of the shift's records that are active
- `active_kWh` — active-only energy for the shift
- `active_PF_avg` — active-state PF for the shift

## The magnitude-weighted reverse-CTs decision

The same active/standby insight hardens the **auto** reverse-CTs heuristic
(`--auto-reverse-cts`). A naive count-based test ("is P negative for ≥ 50 % of
records?") is fragile for bimodal loads: the P115RE reads negative more than half
the time (standby) while the real consumption — the active state — is clearly
positive. So the auto-detect now decides on the **dominant high-current (active)
state**: *is real power negative when current is high?* The whole-session count
fields are still reported for context, but `reversed` and the operator notice key
off the active state (`basis: "active"`). If there is no active population at all
(everything below the threshold), it falls back to the whole-session count
(`basis: "whole_session"`). The manual `--reverse-cts` behavior is unchanged —
only the AUTO heuristic was improved.

## Tuning the threshold

```bash
# Default 50 A active/standby cut
fluke-analyze ES.004 --reverse-cts --tz America/Chicago

# Lower the cut so a small steady draw counts as active
fluke-analyze ES.004 --reverse-cts --standby-threshold-a 25 --tz America/Chicago
```

See also [CT_REVERSAL.md](CT_REVERSAL.md) and [SHIFTS.md](SHIFTS.md).
