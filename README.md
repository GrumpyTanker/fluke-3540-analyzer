# fluke-3540-analyzer

**Open-source parser, event detector, and chart generator for Fluke 3540 FC three-phase power logger sessions.** Works on Windows, Linux, and macOS. Runs in your browser — no upload, no install.

> 🚧 **Status: scaffolding (M0).** Web app and Python CLI are being built. See [the roadmap](#roadmap) below. Star/watch to follow along.

## Why this exists

The Fluke 3540 FC is a great little three-phase power logger, but the only official way to read its session files (`.fel` archives, containing `trend.bin`) is **Fluke Energy Analyze Plus** — Windows-only, closed-source, and limited in what you can export. There is no Linux/macOS client, no CLI for batch analysis, and no good way to script reports across many sessions.

This project gives you:

- **A documented binary format** for the 3540 FC's `trend.bin` file — see [`docs/FILE_FORMAT.md`](docs/FILE_FORMAT.md)
- **A web app** that runs in your browser. Drag and drop a `trend.bin`, get charts, pick events, export CSV / XLSX / PNG. Nothing is uploaded — all parsing happens client-side in JavaScript.
- **A Python CLI** for scripting, batch jobs, and publication-quality gnuplot output. `pip install -e .` and you're going.
- **Auto event detection** — outages, voltage dips, swells, high-current peaks, frequency excursions, NEMA imbalance spikes, sudden load steps.
- **CT reversal correction** — `--reverse-cts` flag handles iFlex probes installed backwards (extremely common mistake).

## Web app (no install)

🚧 *Coming with milestone M5.* Will be live at: **https://grumpytanker.github.io/fluke-3540-analyzer/**

Drop `trend.bin` (or your unpacked `ES.NNN/` session directory) onto the page. The browser parses the file locally, runs event detection, and shows an interactive picker for which events / quantities / phases to chart. Download results as PNG, CSV, or XLSX. **No data ever leaves your machine.**

## Python CLI (for scripting)

🚧 *Coming with milestone M3.* Planned usage:

```bash
pip install -e python/

# auto mode: parse + detect + render everything
fluke-analyze path/to/ES.NNN -o output/ --reverse-cts

# interactive: walk through event picker
fluke-analyze path/to/ES.NNN -o output/ --interactive

# slice + filter
fluke-analyze path/to/ES.NNN -o output/ \
    --from "2024-01-13T22:50:00" --to "2024-01-13T22:55:00" \
    --plot power,current --phase c
```

## Credits

Built on top of the binary-format reverse-engineering work in **[FLUKELOAD](https://github.com/alaincc/FLUKELOAD)** by [@alaincc](https://github.com/alaincc) — the first open-source Fluke 3540 FC parser. This project's expanded 170-of-180-float field map was contributed back upstream as **[FLUKELOAD PR #1](https://github.com/alaincc/FLUKELOAD/pull/1)**.

If you're after a load-calculation viewer with a FastAPI backend, check out FLUKELOAD. If you want a generic CLI + browser app for general power-quality analysis, you're in the right place.

## Roadmap

Each milestone is a self-contained, ship-able chunk:

| | Milestone | Status |
|---|---|---|
| M0 | Repo scaffolding + field-map spec | 🚧 in progress |
| M1 | Python parser refactor + tests | ⏳ pending |
| M2 | Python event detection + snapshots | ⏳ pending |
| M3 | Python CLI orchestrator + gnuplot wrappers | ⏳ pending |
| M4 | JS parser port + parity test | ⏳ pending |
| M5 | Web app shell: drop zone + Worker parsing | ⏳ pending |
| M6 | Web app: event catalog + chart rendering | ⏳ pending |
| M7 | XLSX export (both sides) + bulk download | ⏳ pending |
| M8 | CI + GitHub Pages deploy | ⏳ pending |
| M9 | README polish + first release | ⏳ pending |

## File format

The Fluke 3540 FC writes sessions as a `.fel` archive containing:

- `trend.bin` — 744-byte records, one per second, with 180 float32 measurements each (voltages, currents, P/Q/S per phase + total, fundamental and RMS, PF/DPF, Wh/VAh/VARh, harmonics, frequency, rolling demand)
- `trend-meta.bin` — session metadata
- `session-meta.bin` — session ID, timestamps
- `configuration.bin` — instrument config snapshot
- `ES.NNN-config.json` — asset name, team, instrument firmware

The canonical record layout and field map live in [`spec/field_map.json`](spec/field_map.json) — both the Python and JavaScript parsers load from it, so they stay in lockstep. Header layout, FILETIME conversion, and confidence levels for each field are documented in [`docs/FILE_FORMAT.md`](docs/FILE_FORMAT.md) (coming with M9).

## Event detection

Auto-detected event kinds (rules in [`docs/EVENT_RULES.md`](docs/EVENT_RULES.md)):

| Kind | What it catches |
|---|---|
| `outage` | Any phase L-N voltage below 50 V, sustained ≥1 sec |
| `dip` | Voltage drop below 90 % nominal |
| `swell` | Voltage rise above 110 % nominal |
| `high_current` | Per-phase current peak > mean + 2σ |
| `freq_excursion` | Line frequency deviation > 0.5 Hz from 60 Hz |
| `imbalance_spike` | NEMA imbalance > 2.5 % sustained |
| `power_step` | P_total step change > 50 % of session mean within 1 sec |

## Privacy

The web app does **all parsing in your browser**, using a Web Worker. There is no backend, no upload endpoint, and no telemetry or analytics. You can verify this by opening DevTools → Network and confirming zero outbound requests after page load. The page is also fully offline-capable once cached — vendor libraries (uPlot, SheetJS) are served from the same origin, no CDN.

## License

MIT — see [LICENSE](LICENSE). Use it freely in commercial or personal projects.

## Keywords

Fluke 3540 FC, Fluke 3540, Fluke energy analyze plus alternative, Fluke 3540 Linux, Fluke 3540 macOS, three-phase power logger, power quality analyzer, .fel file format, trend.bin parser, Fluke FC open source, power quality monitoring, electrical event detection, voltage dip detection, voltage swell detection, outage detection, NEMA imbalance, current transformer reversal, iFlex probe direction, harmonic distortion, THD, kWh export, three-phase load profile, industrial energy monitoring, EV charger monitoring.
