# Per-asset threshold config (`--rules-file`)

Event detection uses a single set of thresholds (`EventRules`) tuned to IEEE
1159 / NEMA defaults. When you know an asset's real behaviour — its actual trip
voltage, an expected swell ceiling, a noisier-than-usual feeder — you can
override those thresholds per asset with `--rules-file FILE`.

```
fluke-analyze ES.004 --rules-file fleet_rules.json
```

The asset is matched on the session's `asset_name` (from the `*-config.json`).

## File format (JSON or TOML)

Two optional sections:

- `defaults` — applied to every asset.
- `assets` — a map of `asset_name → overrides`. A special `"default"` asset key
  is used when the session's asset has no explicit entry.

Per-asset values win over `defaults`, which win over the built-in defaults.

### JSON

```json
{
  "defaults": {
    "dip_pct_of_nominal": 0.92
  },
  "assets": {
    "P115RE-MAC03": {
      "outage_v_threshold": 60.0,
      "swell_pct_of_nominal": 1.08
    },
    "default": {
      "freq_excursion_hz": 0.4
    }
  }
}
```

### TOML

```toml
[defaults]
dip_pct_of_nominal = 0.92

[assets."P115RE-MAC03"]
outage_v_threshold = 60.0
swell_pct_of_nominal = 1.08
```

A **flat file** with only threshold keys (no `defaults`/`assets`) is treated as
defaults for every asset.

## Overridable keys

| Key | Default | Meaning |
|---|---|---|
| `outage_v_threshold` | 50.0 | any phase L-N below this V is an outage |
| `dip_pct_of_nominal` | 0.90 | < this fraction of nominal = dip |
| `swell_pct_of_nominal` | 1.10 | > this fraction of nominal = swell |
| `high_current_sigma` | 2.0 | mean + Nσ on any phase = high current |
| `freq_excursion_hz` | 0.5 | \|f − nominal\| over this = excursion |
| `imbalance_pct_threshold` | 2.5 | NEMA % imbalance threshold |
| `power_step_pct_of_mean` | 0.50 | ΔP in 1 s over this × mean \|P\| = step |
| `min_duration_secs` | 1 | events shorter than this are ignored (int) |
| `gap_tolerance_secs` | 1 | merge runs split by ≤ this many samples (int) |
| `nominal_freq_hz` | 60.0 | line frequency baseline |

Unknown keys are rejected with an error listing the valid ones. `min_duration_secs`
and `gap_tolerance_secs` are coerced to integers; all others to floats.

On load the CLI prints a one-line `[rules]` note showing exactly which
thresholds changed (`key: old -> new`) for the matched asset.
