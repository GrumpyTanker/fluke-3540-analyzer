# Multi-session comparison (`fluke-analyze compare`)

The `compare` subcommand overlays N sessions on the same axes so you can
spot drift, before/after differences, or daily variability without
juggling separate reports.

## Usage

```bash
fluke-analyze compare SESSION1 SESSION2 [SESSION3 ...] -o OUTDIR [options]
```

Each session can be an `ES.NNN/` directory or a `.fel` zip-bundle —
mix-and-match is fine.

### Options

| Flag | Default | What |
|---|---|---|
| `-o`, `--output` | *required* | Output directory |
| `--labels L1,L2,...` | derived from input names | Display labels per session |
| `--plot QTYS` | `power,voltage,current,pf,frequency` | Which quantities to overlay |
| `--reverse-cts [PHASES]` | off | Applied uniformly to every session |
| `--format` | `png` | `png` or `svg` |

### Example

```bash
# Before vs after a panel rebuild
fluke-analyze compare \
    sessions/2024-01-13-before/ \
    sessions/2024-02-15-after.fel \
    -o reports/before_vs_after \
    --labels "before","after" \
    --reverse-cts a \
    --plot power,current
```

## Output

`OUTDIR/` contains:

```
compare_summary.csv               # side-by-side totals
compare_power.png                 # P_total overlay
compare_voltage.png               # V_LN_a overlay
compare_current.png               # I_a overlay
compare_pf.png                    # PF_total overlay
compare_frequency.png             # freq overlay
session_0.csv, session_1.csv, ... # per-session full parses
compare_<quantity>_session_N.tsv  # gnuplot intermediates
compare_<quantity>.gp             # gnuplot scripts (for tweaking)
```

`compare_summary.csv` has one column per session labeled with the
`--labels` value:

```csv
metric,before,after
rows,75123,74980
net_kwh,-1032.27,-985.40
imported_kwh,56.51,60.20
exported_kwh,-1088.79,-1045.60
peak_import_kw,9.25,9.90
peak_export_kw,-136.81,-130.15
peak_current_a,314.22,295.50
```

## Time alignment

Sessions are aligned by **relative time from each session's first
record** (the x-axis is "seconds from session start"), not by absolute
wall-clock time. That way you can compare a 2-hour morning run against
a 2-hour evening run without one being shifted off-screen.

If you need absolute-time-aligned overlays (e.g. comparing two
simultaneous loggers at the same site), strip non-overlapping prefixes
from each `session_N.csv` first and re-run `compare` with the
trimmed inputs.

## Out of scope

- Web comparison UI — multi-file UX in-browser is a significant project.
  v0.2 ships CLI-only compare. v0.3 candidate.
- Per-event overlay — `compare` does full-session overlays. To compare
  specific events, use the v0.1 `fluke-analyze ... --events ID` flow on
  each session separately.
- Statistical drift detection (KL divergence, mean-shift tests, etc.) —
  the CSV makes that easy to do in pandas if you need it.
