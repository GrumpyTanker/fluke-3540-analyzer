# Roadmap

Shipped releases live in [CHANGELOG.md](CHANGELOG.md). This file is the
backlog of ideas we've discussed but not yet committed to a specific
release.

## Shipped in v0.5 — large-session hardening

The v0.5 release was driven by a real ~6.8-day P115RE-MAC03 capture
(589,877 records / 438 MB) rather than the themes below. Delivered:
memory-bounded `ColumnStore`, single-pass parse, `--max-csv-rows` guard,
O(N) snapshot stdev, tolerant parser, `--anchor-start/--anchor-end` clock
correction, `--split-by` per-bucket reports + roll-up, event markers
(`--mark/--marks`), ITIC/CBEMA classification, whole-session statistics,
time-of-day diurnal profile, and web chart decimation. The themes below
remain the forward backlog.

### Deferred from the v0.5 web pass

The Python core is fully week-hardened. The browser app got chart
decimation (the biggest uPlot win) this pass; the remaining 7-day
robustness work is parked here:

- **Typed-array record storage** — `parser.js` currently allocates one
  object + one `Float32Array(180)` per record (~425 MB+ for a week).
  Replace with a single flat `Float32Array` (or per-column arrays mirroring
  the Python `ColumnStore`) indexed by record, eliminating per-record
  object overhead.
- **Chunked / streaming parse with progress** — parse in slices off a
  `File`/`Blob` stream so a 438 MB session never has to be held as one
  `ArrayBuffer` plus a parallel object array.
- **IndexedDB large-session verification** — confirm the cache layer
  holds a 438 MB session and evicts sanely under quota pressure.

## v0.5 candidates

Themes brainstormed after v0.4 shipped. Pick a coherent slice for the
next release; rest stay parked here.

### Theme A — Fleet / monitoring *(~14 h, highest-value)*

Turns the tool from "analyze one session" into "monitor a fleet of sites
over months."

- **Asset library** — save per-site profiles (tariff, breaker, notes,
  label conventions). Drop-down selector when loading a session.
- **Trending dashboard** — stack many captures of the same asset on one
  timeline: kWh/day, peak kW/day, event-count-by-kind/day, PF over
  weeks. Complements the cross-session insights that v0.4 already
  detects.
- **Stale-file warning** — re-hash on subsequent loads and flag when a
  parsed CSV has been edited since the original parse.
- **Webhook alerts** — POST to Slack / Teams / Discord / generic
  webhook when a finding fires above a configured severity. CLI flag
  for scripted runs, web settings for browser use.

### Theme B — Tariff + financial rigor *(~10 h)*

Adds depth for anyone who actually pays the power bill (finance /
energy-management folks, not just PQ engineers).

- **TOU-3 / weekday-weekend** schedules + holiday calendar.
- **Demand charges** (peak-kW pricing layered on top of kWh charges).
- **Multi-currency** + optional FX conversion.
- **Cost-driven insights** — "75 % of your cost came from 4 hours of
  peak — shift these loads to save $X/month."
- **CLI YAML config** for `tariff.compute_cost` (engine already ships
  in v0.4; this is just the CLI glue).
- **Python CLI breaker context** — web ships in v0.4; mirror in CLI.

### Theme C — Comparison polish *(~8 h)*

Incremental on the v0.4 multi-session work.

- **Side-by-side diff view** — table-style, not overlay; highlights
  where sessions diverge.
- **Designated baseline mode** — pick one session as "before", every
  other diffs against it.
- **Per-event PDF cards** — one-pager per event, useful as a tech work
  order.
- **Bulk annotation import** (reverse of W2's export).
- **Equipment fingerprinting** — detect repeating load signatures
  across sessions.

### Theme D — Cloud LLM narrative *(~6 h)*

The one to think hardest about — with the rule-based engine already
producing structured findings, an LLM mostly restates what's there.
Defer unless someone specifically asks.

- **BYO API key**, opt-in toast with clear disclosure.
- Sends only structured findings + summary stats — **never** raw
  waveforms.
- Generates a one-paragraph executive summary at the top of the
  HTML/PDF report.
- Cached per file hash so re-runs cost nothing.
- Falls back gracefully when no key configured.

### Theme E — Power-user / ecosystem *(~12 h)*

Meaty surface area; only worth shipping with real demand.

- **Custom event-rule editor** — sliders in the web UI for thresholds,
  live re-compute of insights.
- **Maintenance windows** — tag time ranges as "known good" to
  suppress events in those windows.
- **InfluxDB / TimescaleDB exporters** for long-term archive.
- **Grafana dashboard JSON export** for live monitoring.
- **CSV adapter library** — import from non-Fluke meters (Schneider,
  ABB) after a field-mapping pass.
- **REST/programmatic API mode** for invocation from another tool.

### Theme F — Polish *(~5 h, can bundle with any theme)*

- **Dark mode for chart canvases** themselves (uPlot has a dark CSS
  variant we're not using yet).
- **Print-preview improvements** — better page breaks in HTML report.
- **Compare-mode mobile layout** — v0.4 mobile media queries don't
  cover the compare flow.
- **i18n framework** — English-only translations to start, structure
  in place for future languages.
- **Audit log** of report generations to localStorage, exportable.

## v0.6+ deferred indefinitely

- Real-time / live capture from the meter (no public Fluke API).
- WebAssembly parser (parse already runs in 123 ms — not worth the
  complexity).
- Local in-browser LLM (Transformers.js) — model download is
  200 MB – 2 GB and gated on WebGPU.

## How to pick a slice

A recommended v0.5 cut: **Theme A + Theme B + 1-2 Theme F polish items**
(~25 h). Establishes the tool as a fleet monitor with proper financial
accounting. v0.6 could then be Theme C + Theme E if appetite remains.

If LLM appetite reappears, Theme D is the smallest sensible integration.
