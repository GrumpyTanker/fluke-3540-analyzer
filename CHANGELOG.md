# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-05-28

### Added
- **Insights engine** — local, rule-based analysis cross-correlating
  events, snapshots, and per-second data into Findings with severity,
  headline, detail, related-events links, and recommended actions.
  Seven rule kinds: outage signature, phase asymmetry, PF drift,
  sustained imbalance, frequency / source stiffness, outage frequency,
  current-spike ratio. Python + JS implementations share thresholds via
  `spec/field_map.json` → `insight_rules`. See [`docs/INSIGHTS.md`](docs/INSIGHTS.md).
- **Range select with scoped export (web)** — mini-map P_total strip
  above main charts. Brush-drag to pick a time range; main charts
  re-render scoped to it; exports (CSV / XLSX / HTML / PDF / ZIP)
  apply to the selection. Per-event "snap to event ±60 s" buttons.
  URL-hash sync for bookmarkable ranges.
- **Zoom on scroll + pan (web)** — wheel zooms around the cursor X,
  Shift-drag pans, click-drag box-zooms. "Reset zoom" button per chart.
- **Anomaly overlay on full-session charts (web)** — colored vertical
  bands marking event windows (outage = red, dip = orange, swell =
  yellow, etc.) drawn via the uPlot hooks API.
- **PDF report (web)** — one-click download via pdf-lib. Cover page,
  insights pages with severity bars, events table, one rendered chart
  per page (embedded PNG). 525 KB pdf-lib vendored, MIT.
- **PDF report (CLI)** — `--pdf` flag (optional weasyprint dep).
  `pip install fluke-3540-analyzer[pdf]`.
- **IndexedDB session cache (web)** — SHA-256 of the file bytes keys
  parsed records in IndexedDB; subsequent drops of the same file skip
  the parse. LRU-bounded to 5 sessions. "Clear cache" link in footer.
- **Severity-colored event chips with peak-severity tooltip + sortable
  events table (web)** — click any column header to sort.
- **Keyboard shortcuts (web)** — R re-render, Z reset-zoom-all,
  ←/→ navigate events, Esc clear filters/range, ? toggle help overlay.
- **CI actions bumped** — checkout v5, setup-python v6, setup-node v5,
  upload-pages-artifact v4 (quiets the 2026-09-16 Node 20 deprecation).

### Changed
- HTML report now includes an Insights section between Summary and Events.
- `summary.txt` and `--json` output now include insights.
- README + new `docs/INSIGHTS.md` cover all the above.

### Tests
- 89 Python tests (up from 75 in v0.2.0)
- 39 JS tests (up from 25 in v0.2.0)
- **128 tests total**, all green in CI on every PR.

## [0.2.0] — 2026-05-28

### Added
- **`.fel` zip-bundle support** — the CLI and web app now accept Fluke's
  raw `.fel` export directly; no manual unpacking needed.
  ([docs/FILE_FORMAT.md](docs/FILE_FORMAT.md))
- **Per-phase reverse-CT** — `--reverse-cts a,c` flips only the named
  phases (plus matching `*_total_*` columns). Web UI has three Phase A/B/C
  checkboxes replacing the v0.1 single switch. Closes the
  "When NOT to use it" caveat in `docs/CT_REVERSAL.md`.
- **Event search/filter (web)** — filter the events table by free-text
  query (kind name or affected phases) and toggle visibility per event
  kind via colored chips above the table.
- **Dark mode (web)** — auto / light / dark radio toggle in the footer;
  preference persists in `localStorage`.
- **CLI `--json` mode** — emits a single JSON blob with
  `{config, summary_stats, events, snapshots}` to stdout for piping into
  `jq` or downstream tooling. All log lines redirect to stderr.
- **HTML report** — `report.html` is now part of the default CLI output
  (opt out with `--no-html`). The web app exposes a
  "Download HTML report" button that produces an equivalent file
  client-side. Single self-contained file embedding all charts as
  base64 PNGs.
- **`fluke-analyze compare` subcommand** — overlay multiple sessions
  on the same axis (aligned by relative-time-from-session-start) and
  produce a side-by-side `compare_summary.csv`. See
  [docs/COMPARE.md](docs/COMPARE.md).
- **PyPI publish workflow** — tag-triggered trusted publishing via
  `.github/workflows/release.yml`. After v0.2.0 lands,
  `pip install fluke-3540-analyzer` works directly without `-e`.

### Changed
- README updated for `.fel`, per-phase reverse-CT, `compare`, `--json`,
  HTML report, PyPI install, and dark mode.
- `docs/CT_REVERSAL.md` rewritten to cover per-phase usage.
- `find_session_files()` now globs `*-config.json` as a fallback when
  the directory name doesn't match (e.g. .fel-extracted folders).

### Tests
- 75 Python tests (up from 48 in v0.1.0)
- 25 JS tests (up from 20 in v0.1.0)
- 100 total — all green in CI on every PR

## [0.1.0] — 2026-05-27

First public release.

- Python CLI: `fluke-analyze ES.NNN [-o OUT] [...]` with four modes
  (`--auto`, `--interactive`, `--parse-only`, `--plot-only`), full
  filter set, gnuplot-driven PNG/SVG output, openpyxl XLSX with
  native Excel charts. Stdlib-only event detection.
- Web app at https://grumpytanker.github.io/fluke-3540-analyzer/
  - In-browser parsing via Web Worker (no upload)
  - Event detection + quiet-snapshot picking
  - uPlot charts with per-chart PNG/CSV download
  - XLSX export + "Download everything (.zip)" bundle
- Shared `spec/field_map.json` (174 of 180 floats identified) loaded by
  both Python and JS parsers — cross-language parity tested.
- Documentation: README, FILE_FORMAT, CT_REVERSAL, EVENT_RULES.
- CI on every PR (pytest + node --test); GitHub Pages auto-deploy.
- MIT license, credits FLUKELOAD upstream.

[0.3.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.3.0
[0.2.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.2.0
[0.1.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.1.0
