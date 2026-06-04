# Multi-session stitching (`fluke-analyze stitch`)

The 3540 FC caps a single capture at ~7 days. To analyse a longer span, take
consecutive captures of the same asset and stitch them into one continuous
timeline:

```bash
fluke-analyze stitch ES.001 ES.002 ES.004 -o week_out/
```

Inputs may be `ES.NNN/` directories, `.fel` bundles, or pre-parsed `.csv`
files, in any order — they are sorted by their first record's start time.

## What it does

1. Parses each session into a memory-bounded `ColumnStore`.
2. Concatenates them in time order, carrying absolute (anchor-corrected)
   timestamps so the joined series is monotonic.
3. Records a **gap** wherever consecutive sessions don't abut (boundary
   difference > `--gap-tolerance`, default 2 s). No synthetic fill rows are
   inserted — the gap is noted, not papered over.
4. Runs the normal analysis over the stitched series.

## Outputs

| File | Contents |
|---|---|
| `stitch.json` | provenance: total records, per-source `{label, lo, hi, t_start, t_end, records}`, and `gaps[]` |
| `session.csv` | the stitched per-second series with a `source` provenance column |
| `events.json` | events detected across the whole stitched timeline |
| `insights.json` | insights over the stitched series |
| `stats.json` | whole-session statistics over the stitched series |

## Flags

| Flag | Default | What |
|---|---|---|
| `-o`, `--output` | required | output directory |
| `--labels A,B,…` | input names | per-source labels (must match count) |
| `--reverse-cts [PHASES]` | off | apply the same reverse-CTs to every source |
| `--gap-tolerance SECS` | `2.0` | boundary gaps larger than this are recorded |
| `--nominal-ln-v V` | auto | nominal L-N voltage for detection |
| `--no-stats` | | skip the stitched stats |
| `--no-csv` | | skip writing the (large) stitched `session.csv` |

## Example

Stitching ES.001 (5,024 recs) + ES.002 (74,873 recs) yields 79,897 continuous
records and a single recorded gap of ~802 s between the two captures — exactly
the meter's swap-out interval.

The web app exposes the same capability: load multiple sessions and call
`MultiSession.buildStitched(spec)` (see `web/multi_session.js`).
