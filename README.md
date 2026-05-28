# fluke-3540-analyzer

**Open-source parser, event detector, and chart generator for Fluke 3540 FC three-phase power-quality logger sessions.** Works on Windows, Linux, and macOS. Runs in your browser — no upload, no install.

🌐 **Live web app:** [grumpytanker.github.io/fluke-3540-analyzer](https://grumpytanker.github.io/fluke-3540-analyzer/)

## Why this exists

The Fluke 3540 FC is a great little three-phase power logger, but the only official way to read its session files (`.fel` archives, containing `trend.bin`) is **Fluke Energy Analyze Plus** — Windows-only, closed-source, and limited in what you can export. There is no Linux/macOS client, no CLI for batch analysis, and no good way to script reports across many sessions.

This project gives you:

- **A documented binary format** for the 3540 FC's `trend.bin` file — see [`docs/FILE_FORMAT.md`](docs/FILE_FORMAT.md)
- **A web app** that runs in your browser. Drag and drop a `trend.bin`, get charts, pick events, export CSV / XLSX / PNG. Nothing is uploaded — all parsing happens client-side in JavaScript.
- **A Python CLI** for scripting, batch jobs, and publication-quality gnuplot output. `pip install -e .` and you're going.
- **Auto event detection** — outages, voltage dips, swells, high-current peaks, frequency excursions, NEMA imbalance spikes, sudden load steps.
- **CT reversal correction** — `--reverse-cts` flag handles iFlex probes installed backwards (extremely common mistake). See [`docs/CT_REVERSAL.md`](docs/CT_REVERSAL.md).

## Web app — no install

Visit **[grumpytanker.github.io/fluke-3540-analyzer](https://grumpytanker.github.io/fluke-3540-analyzer/)** and drop your `trend.bin` (or the whole unpacked `ES.NNN/` session folder, so the asset name from `ES.NNN-config.json` is picked up too) onto the page.

The browser parses the file locally in a Web Worker, runs event detection, and shows:

- A summary panel (records, time range, asset)
- An events table with severity, affected phases, and one-click zoom
- A snapshots list of quiet baseline windows
- A chart picker (voltage / current / power / PF / frequency / THD)
- A pre/post-event zoom window control
- Per-chart download to PNG or CSV
- A full workbook download (XLSX) and "Download everything (.zip)" bundle

**No data ever leaves your machine.** Verify yourself in DevTools → Network: zero outbound requests after page load.

## Python CLI — for scripting + publication charts

```bash
git clone https://github.com/GrumpyTanker/fluke-3540-analyzer
cd fluke-3540-analyzer
pip install -e "python/[dev]"
```

You'll need [gnuplot](http://www.gnuplot.info/) for chart rendering (`apt install gnuplot` / `brew install gnuplot` / `choco install gnuplot`). The CLI auto-discovers it via `$GNUPLOT`, then `$PATH`, then `C:\Program Files\gnuplot\bin\gnuplot.exe`.

```bash
# auto: parse + detect + render everything
fluke-analyze path/to/ES.NNN -o output/ --reverse-cts

# interactive: walk through event picker
fluke-analyze path/to/ES.NNN -o output/ --interactive

# parse only — write CSV + events.json + summary.txt, no charts
fluke-analyze path/to/ES.NNN -o output/ --parse-only

# plot only — reuse existing CSV + events.json, just (re-)render charts
fluke-analyze path/to/ES.NNN -o output/ --plot-only --plot voltage,current

# time-window slice + chart subset
fluke-analyze path/to/ES.NNN -o output/ \
    --from "2024-01-13T22:50:00" --to "2024-01-13T22:55:00" \
    --events 0,3,5 --plot power,current --pre 60 --post 120
```

### CLI flag reference

| Flag | Default | What |
|---|---|---|
| `SESSION_DIR` | required | Path to an `ES.NNN/` session directory |
| `-o`, `--output` | `<session>_out/` | Output directory |
| `--auto` | (default) | Parse + detect + render everything |
| `--interactive` | | Prompt for which events / quantities to render |
| `--parse-only` | | Just write CSV + events.json + summary.txt |
| `--plot-only` | | Reuse existing CSV/events.json, render charts only |
| `--from ISO_TIME` | | Restrict event scan & zooms to ≥ this time |
| `--to ISO_TIME` | | Restrict event scan & zooms to ≤ this time |
| `--pre SECS` | `30` | Pre-event zoom padding |
| `--post SECS` | `60` | Post-event zoom padding |
| `--events IDS` | all | Comma-separated event IDs to render |
| `--plot QTYS` | `voltage,current,power,thd,pf,frequency` | Quantities to chart |
| `--snapshots N` | `3` | Number of quiet snapshots to pick |
| `--reverse-cts [PHASES]` | off | Negate P/Q/PF/DPF/Wh/VARh. Bare flag = all phases; pass `a` or `a,c` for selected phases (totals follow). See `docs/CT_REVERSAL.md` |
| `--every K` | `1` | Emit every K-th record into the CSV |
| `--format` | `png` | `png` or `svg` |
| `--no-xlsx` | | Skip the XLSX workbook |
| `--no-overview` | | Skip the 2x2 overview multiplot |
| `--nominal-ln-v V` | auto | Override nominal L-N voltage (auto-inferred otherwise) |

### Output layout (`--auto`)

```
output/
├── session.csv           # one row per record, ~75K rows for a 20h session
├── session_1min.csv      # downsampled to 1 row/min
├── events.json           # detected events
├── snapshots.json        # quiet baseline windows
├── summary.txt           # asset + energy + per-event lines
├── report.xlsx           # multi-sheet workbook with native Excel charts
└── charts/
    ├── overview.png
    ├── full_<quantity>.png
    ├── event_<id>_<kind>_<quantity>.png
    └── snapshot_<id>_<quantity>.png
```

`--plot-only` consumes `session.csv`, `session_1min.csv`, and `events.json` from a previous `--parse-only` run — useful for iterating on chart styles without re-parsing 55 MB of binary data.

## Event detection

Auto-detected event kinds (full rules + rationale in [`docs/EVENT_RULES.md`](docs/EVENT_RULES.md)):

| Kind | What it catches | Default threshold |
|---|---|---|
| `outage` | All-phase L-N voltage collapse | < 50 V on every phase |
| `dip` | Voltage drop on any phase (excluding outage) | < 90 % of nominal |
| `swell` | Voltage rise on any phase | > 110 % of nominal |
| `high_current` | Per-phase current peak | > mean + 2σ |
| `freq_excursion` | Line frequency deviation | > 0.5 Hz from 60 Hz |
| `imbalance_spike` | NEMA voltage imbalance | > 2.5 % |
| `power_step` | Sudden 1-sec change in P_total | > 50 % of session mean \|P\| |

All thresholds are tunable via the `EventRules` dataclass when using the API directly.

## File format

The Fluke 3540 FC writes sessions as a `.fel` archive containing:

- `trend.bin` — 744-byte records, one per second, with 180 float32 measurements each (voltages, currents, P/Q/S per phase + total, fundamental and RMS, PF/DPF, Wh/VAh/VARh, harmonics, frequency, rolling demand)
- `trend-meta.bin` — session metadata
- `session-meta.bin` — session ID, timestamps
- `configuration.bin` — instrument config snapshot
- `ES.NNN-config.json` — asset name, team, instrument firmware

The canonical record layout and field map live in [`spec/field_map.json`](spec/field_map.json) — both the Python and JavaScript parsers load from it, so they stay in lockstep. The header layout, FILETIME conversion, and confidence levels for each field are documented in [`docs/FILE_FORMAT.md`](docs/FILE_FORMAT.md).

## Privacy

The web app does **all parsing in your browser**, using a Web Worker. There is no backend, no upload endpoint, and no telemetry or analytics. You can verify this by opening DevTools → Network and confirming zero outbound requests after page load. The page is also fully offline-capable once cached — vendor libraries (uPlot, SheetJS, JSZip, Pico CSS) are served from the same origin, no CDN.

## Architecture

```
fluke-3540-analyzer/
├── spec/field_map.json          ← canonical binary layout + field map
├── python/src/fluke_3540/
│   ├── parser.py                ← loads spec, decodes trend.bin
│   ├── events.py                ← detection rules
│   ├── snapshots.py             ← quiet-window picker
│   ├── cli.py                   ← fluke-analyze entry point
│   └── plots/                   ← gnuplot + openpyxl
├── web/
│   ├── parser.js                ← JS port; loads same spec
│   ├── events.js, snapshots.js
│   ├── plots.js                 ← uPlot wrappers
│   ├── xlsx_export.js           ← SheetJS workbook
│   ├── bundle_export.js         ← JSZip "download everything"
│   ├── parser_worker.js         ← Web Worker
│   ├── app.js / index.html / style.css
│   └── vendor/                  ← uPlot, SheetJS, JSZip, Pico (vendored)
└── .github/workflows/
    ├── ci.yml                   ← pytest + node --test on PR/push
    └── pages.yml                ← deploy web/ to grumpytanker.github.io
```

A cross-language parity test (`web/tests/parity.test.js`) parses the same `python/tests/fixtures/synthetic_trend.bin` fixture with both implementations and asserts byte-for-byte equality on every cell. CI runs both test suites on every PR.

## Credits

Built on top of the binary-format reverse-engineering work in **[FLUKELOAD](https://github.com/alaincc/FLUKELOAD)** by [@alaincc](https://github.com/alaincc) — the first open-source Fluke 3540 FC parser. This project's expanded 170-of-180-float field map was contributed back upstream as **[FLUKELOAD PR #1](https://github.com/alaincc/FLUKELOAD/pull/1)**.

If you're after a load-calculation viewer with a FastAPI backend, check out FLUKELOAD. If you want a generic CLI + browser app for general power-quality analysis, you're in the right place.

## License

MIT — see [LICENSE](LICENSE). Use it freely in commercial or personal projects.

## Keywords

Fluke 3540 FC, Fluke 3540, Fluke energy analyze plus alternative, Fluke 3540 Linux, Fluke 3540 macOS, three-phase power logger, power quality analyzer, .fel file format, trend.bin parser, Fluke FC open source, power quality monitoring, electrical event detection, voltage dip detection, voltage swell detection, outage detection, NEMA imbalance, current transformer reversal, iFlex probe direction, harmonic distortion, THD, kWh export, three-phase load profile, industrial energy monitoring, EV charger monitoring.
