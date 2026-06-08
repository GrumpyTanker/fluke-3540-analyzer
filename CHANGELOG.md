# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] — 2026-06-03

Active/standby load-state split for bimodal loads — classify each record by
current, report the two states separately, correct the session energy three
ways, headline the active-state power factor, and harden the auto reverse-CTs
heuristic to decide on the dominant high-current state. Parity-tested Python↔JS.
Built from the real P115RE coating-rectifier session.

### Added — load-state split (`load_states`)
- **Current-gated classifier.** Each record is **active** when its mean
  per-phase average current `(I_a_avg+I_b_avg+I_c_avg)/3` is **≥ the threshold**
  (default **50 A**), else **standby**. Current — not power — because the power
  *sign* at low current is exactly the thing in question. Configurable with
  **`--standby-threshold-a N`**.
- **`load_states.csv` + `load_states.json`** — one row per state (active,
  standby): records, hours, duty %, kWh, P avg/min/max (kW), I avg (A), S avg
  (kVA), PF avg, V_LN avg, V_THD p95. JSON also carries the threshold, the three
  energy figures, and the standby-sign caveat. Emitted automatically (cheap);
  `--load-states` is an explicit opt-in. A compact table is embedded in
  `summary.txt` and the HTML report.
- **Three explicit energy figures** (never silently changing the historic
  number): `energy_as_measured_kWh` (signed sum — current behavior),
  `energy_active_kWh` (active records only), and
  `energy_net_clip_standby_kWh` (standby real power clipped to ≥0). The standby
  real-power sign is unreliable at low current, so the active/clip figures are
  the defensible consumption.
- **Headline PF is the active-state PF.** The narrative, summary, and HTML
  report now headline the active-state power factor (the blended whole-session
  PF is meaningless for a bimodal load); the raw whole-session PF is kept but
  de-emphasized.

### Changed — magnitude-weighted reverse-CTs auto-detect
- `detect_ct_reversal` / `--auto-reverse-cts` now decide on the **dominant
  high-current (active) state** — *is real power negative when current is
  high?* — instead of the fragile whole-session negative-P count, which a
  bimodal load defeats. The whole-session count fields are still reported for
  context (`basis: "active"` vs `"whole_session"`, with `active_records`,
  `active_frac_negative`, `active_mean_p_w`); the operator notice keys off the
  active state. **Manual `--reverse-cts` behavior is unchanged** — only the AUTO
  heuristic + its printed notice were improved.

### Changed — shift integration
- `shift_comparison.csv/json` rows gain **`active_records`**,
  **`active_duty_pct`**, **`active_kWh`**, and **`active_PF_avg`** so each shift
  shows its active load too. `summary.txt`'s shift table shows the new columns.

### Parity / tests / docs
- JS port in `web/analysis.js` (`classifyLoadStates`, `loadStateRows`,
  `sessionEnergy`, `activeStatePf`; active-state `detectCtReversal`; active
  columns on `shiftComparisonRows`) with a Python↔JS parity test
  (`load_states_parity.test.js`) against a shared bimodal golden fixture.
- New Python tests: classifier (threshold, balanced bimodal fixture), the three
  energy figures, the magnitude-weighted reverse-CTs decision (active-positive
  and active-negative cases + fallback), shift active columns, narrative
  active-PF/energy, and CLI `load_states` outputs.
- `docs/LOAD_STATES.md` (concept, the energy caveat, the standby-sign
  explanation); README options; version bump to 0.8.0.

## [0.7.0] — 2026-06-03

Generalized, named, configurable shift/period splitting — so usage can be
compared across operator-defined shifts (day vs night, A/B/C), with windows
that may wrap past midnight. Parity-tested Python↔JS.

### Added — shift splitting (`--split-by shifts`)
- **`--split-by shifts`** activates named shift mode, with **`--shifts
  "name=HH:MM-HH:MM,..."`** (comma-separated; a window where `end<=start` wraps
  past midnight, e.g. `night=18:00-06:00`) or the JSON **`--shifts-file`**
  (`{"shifts":[{"name","start","end"}]}`, mirroring `--rules-file`). With neither
  flag the default is `day=06:00-18:00,night=18:00-06:00`.
- **Timezone-correct windows:** shift windows are evaluated in the report
  timezone (`--tz`), localizing each UTC record before applying the `HH:MM`
  rule (UTC if `--tz` unset, and the output says so). Validated on a real
  Central-time session: day/night split lands exactly on 06:00 / 18:00 Central.
- **Two outputs:**
  - `shift_comparison.csv` + `shift_comparison.json` — the headline per-shift
    aggregate (records, hours, kWh, P avg/min/max, peak rolling demand, PF avg,
    V_LN avg/p5/p95, V_THD p95, event counts, outage minutes), gathering each
    shift's non-contiguous records across the whole session.
  - Per-occurrence contiguous buckets under `<out>/shifts/<name>_<date>/`
    (session.csv, events.json+ITIC, summary.txt); a midnight-spanning night is
    one occurrence labeled by its start date.
- Gap/overlap validation (warns when windows don't tile 24 h), first-matching
  window wins on overlap, and an `unassigned` shift for records matching none.
- Shift-comparison table embedded in `summary.txt`.

### Added — model + parity
- `analysis.py`: `Shift` / `ShiftSet` (parse / from_spec / default /
  coverage_issues), `gather_store` (non-contiguous index slicing),
  `aggregate_shifts`, `shift_occurrences`, `shift_comparison_rows`; new
  `shifts_file.py` loader.
- `web/analysis.js`: full JS port (`ShiftSet`, `localMinuteOfDay`,
  `aggregateShifts`, `shiftOccurrences`, `shiftComparisonRows`) with a
  Python↔JS parity test (`web/tests/shifts_parity.test.js`) on a shared
  synthetic multi-day fixture.
- Docs: new [`docs/SHIFTS.md`](docs/SHIFTS.md); README options table updated.

### Tests
- Python 251 passing (was 223); web 120 passing (was 114).

## [0.6.0] — 2026-06-03

Web parity + analysis depth. Closes the web memory/parity gap from 0.5.0 and
adds six standards-grade analysis features, all parity-tested Python↔JS.

### Added — web streaming + parity (Features A, B)
- **Streaming columnar web parse** — `web/column_store.js` + `parseTrendColumnar`
  / `parseTrendColumnarStream` decode `trend.bin` straight into packed
  `Float32Array` columns, streamed from the dropped `File` in 8 MB
  record-aligned `Blob.slice` chunks and **Transferred** back from the worker.
  The 7-day file (589,877 recs / 438 MB) now parses + fully analyses at
  **~278 MB peak RSS** vs the old ~1.6 GB. Analysis, charts, range select, and
  tariff all read the resident store; exports materialise records transiently.
  Legacy `parseTrendBin` retained for small-file / CSV paths.
- **`web/analysis.js`** — JS port of `analysis.py` (Welford moments, percentile
  sketch, `wholeSessionStats`, `classifyItic`/`eventItic`, `timeOfDayProfile`,
  `correlateMarkers`). A Statistics panel + time-of-day chart in the web UI; the
  stats table is embedded in the HTML export.
- Fixed a latent `Math.max(...arr)` stack overflow in the insights engine that
  would have crashed the web app on a ~590 K-element session.

### Added — CT-reversal detection (Feature C)
- **`--auto-reverse-cts`** — detects a reversed-CT install (real power negative
  for ≥ 50 % of non-outage time) and applies `--reverse-cts` automatically with
  a loud notice. A matching web banner + one-click apply. Flags the real ES.004
  (52 % negative P, mean −37 kW). Python + JS.

### Added — multi-session stitching (Feature D)
- **`fluke-analyze stitch S1 S2 … -o OUT`** — concatenates consecutive sessions
  into one continuous, gap-aware timeline with per-source provenance, then runs
  the normal analysis over the stitched series (beats the meter's 7-day cap).
  `web/multi_session.js` gains `stitchStores` + `buildStitched`. Validated on
  ES.001 + ES.002 → 79,897 records with a detected 802 s gap.

### Added — executive summary (Feature E)
- **Auto-narrative** — deterministic, rule-based plain-English summary
  (`narrative.md` + top of `summary.txt`, HTML, XLSX). Python + JS parity.

### Added — power-quality standards (Feature F)
- **IEEE 519** voltage-THD compliance per phase (p95 vs 8 %/5 %) and **IEEE 1159
  / SARFI-90/80/70/50/10** indices, in stats + reports. `docs/PQ_STANDARDS.md`.

### Added — demand + timezone + per-asset rules (Features G, H, I)
- **`--demand-window`** rolling peak-demand (default 15 min) with peak window +
  series, in stats/XLSX. Python + JS.
- **`--tz ZONE`** renders report timestamps in local + UTC (default UTC
  unchanged); anchors still accept ISO offsets. Python + JS.
- **`--rules-file FILE`** (JSON/TOML) overrides `EventRules` thresholds keyed by
  asset name (defaults + per-asset). `docs/RULES_FILE.md`. Python + JS.

### Tests
- Python 176 → 223; web 82 → 114. New Python↔JS golden-parity harness covers
  stats, ITIC, CT reversal, narrative, IEEE 519/SARFI, demand, and timezone
  formatting.

## [0.5.0] — 2026-06-02

Large-session hardening — the tool now survives week-long captures
(589,877 one-second records / 438 MB) without OOM, and gains a round of
multi-day analysis features. Driven by a real ~6.8-day capture on the
P115RE-MAC03 rectifier.

### Added — core hardening
- **Memory-bounded `ColumnStore`** (`store.py`) — builds in one streaming
  pass, keeping only the ~20 channels analysis needs as `array.array('f')`
  columns (~50 MB for a week, vs >1 GB before). `events`, `snapshots`, and
  `insights` were refactored onto it; they still accept Records for the
  existing small-fixture tests.
- **Single-pass parse** — `parser.export_csv_multi` walks `trend.bin` once,
  writing the full CSV, the 1-min CSV, and the store together (the binary
  used to be parsed twice).
- **`--max-csv-rows N` guard** — caps the full CSV for week-long sessions by
  auto-raising the stride and logging exactly what was downsampled; the 1-min
  CSV and the analysis store keep full resolution.
- **O(N) snapshot stdev** — `pick_snapshots` rolling stdev is now prefix-sum
  based (was O(N×window) ≈ 177 M ops for a week); parity-tested against the
  old algorithm.
- **Tolerant parser** — `iter_records_safe` skips/logs bad magic (resync),
  truncated tails, and flags non-finite floats instead of aborting; periodic
  progress/ETA on long parses.

### Added — CLI features
- **Clock-correction anchors** — `--anchor-start` / `--anchor-end` (mutually
  exclusive) pin a known real start/end to correct a wrong meter RTC; the
  shift flows to CSV timestamps, the store, events, and `--split-by` buckets.
- **Time-bucket splitting** — `--split-by hour|day|week|<duration>` emits a
  full per-bucket report (`<label>/{session.csv,events.json,summary.txt,report.xlsx}`)
  plus a top-level roll-up and `buckets_summary.csv`. Boundary-spanning events
  are filed under their start bucket and flagged.

### Added — analysis (`analysis.py`)
- **Event markers / correlation** — `--mark "ISO=label"` (repeatable) and
  `--marks FILE.csv`; each marker's nearest event + offset written to
  `markers.json`. (Crux of the rectifier PLC-stop correlation.)
- **Per-bucket summary table** — `buckets_summary.csv` (V min/avg/max, I max,
  kWh, #outages/#dips/#swells, worst PF, peak kW per bucket).
- **ITIC / CBEMA classification** — every dip/outage/swell classified
  `no_interruption` / `prohibited` / `no_damage`; added to each event's JSON.
  See [`docs/ITIC.md`](docs/ITIC.md).
- **Whole-session statistics** — `stats.json` + `stats.csv` + an XLSX
  Statistics sheet (per-channel count/min/p1/p5/median/mean/p95/p99/max/stdev
  via streaming Welford + histogram percentiles, plus under-voltage /
  over-current time accounting).
- **Time-of-day (diurnal) profile** — `--tod-profile [HH:MM-HH:MM]` bins
  samples by time-of-day across all days into an avg/min/max envelope;
  `time_of_day_profile.csv` + an XLSX Time-of-Day sheet (works without gnuplot).

### Added — web app
- **Min/max chart decimation** — uPlot series are decimated to ~plot-width
  buckets (preserving per-bucket min+max so dips/spikes survive) for week-long
  sessions; a "large session — chart decimated" notice appears and CSV export
  keeps full resolution. Typed-array record storage + chunked parse for the
  full 7-day worst case are tracked in ROADMAP.

### Tests
- 175 Python tests (up from 109 in v0.4.0)
- 82 JS tests (up from 76 in v0.4.0)
- **257 total** — all green.

## [0.4.0] — 2026-05-28

### Added
- **CSV input mode** — drop a pre-parsed `session.csv` instead of the
  binary; the CLI accepts the same `.csv` as a session input.
- **Multi-session web compare UI** — load multiple files into one
  page, label each, toggle Compare overlay for side-by-side analysis.
- **Cross-session insights** — voltage_drift, recurring_outages,
  pf_degradation, source_stiffness_emergence, event_count_trend.
  Python + JS parity, thresholds in `spec/field_map.json`.
- **Compare HTML + PDF reports** — CLI compare writes
  `compare_report.html` (+ `compare_report.pdf` with `--pdf`); web
  compare mode reuses the existing Download HTML button.
- **Energy cost / TOU tariff calculator** — currency + peak/off-peak
  rates + peak hours, persisted per asset in `localStorage`. See
  [`docs/TARIFF.md`](docs/TARIFF.md).
- **Per-event annotations** — 📝 button on each event row, notes
  persist in `localStorage` keyed by file hash + event id, exportable
  as JSON.
- **Breaker context inputs** — main breaker rating drives a richer
  `current_spike_ratio` finding (% of rating, escalating severity)
  plus a new `breaker_margin` alert when peak exceeds rating.
- **Anomaly band tooltips** on hover over the colored event regions.
- **PWA / installable web app** — `manifest.json` + service worker,
  cache-first for offline use.
- **Mobile responsive layout** — events table scrolls horizontally,
  chart toolbars stack, range mini-map supports touch.
- **Crosshair tooltips** — uPlot legend.live readouts with wider
  cursor proximity for easier hover sampling.

### Tests
- 109 Python tests (up from 89 in v0.3.0)
- 76 JS tests (up from 39 in v0.3.0)
- **185 total** — all green in CI.

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

[0.5.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.5.0
[0.4.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.4.0
[0.3.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.3.0
[0.2.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.2.0
[0.1.0]: https://github.com/GrumpyTanker/fluke-3540-analyzer/releases/tag/v0.1.0
