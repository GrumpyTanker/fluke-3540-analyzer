# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.2.0
[0.1.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.1.0
