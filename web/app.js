// Main orchestration: drop-zone handling, spec/file loading, worker dispatch,
// summary rendering, event detection, chart UI, exports. Pure ESM, no framework.

import { detectEvents } from './events.js';
import { pickSnapshots } from './snapshots.js';
import { FULL_QUANTITIES, ZOOM_QUANTITIES, renderChart } from './plots.js';
import { buildXlsx, downloadBlob } from './xlsx_export.js';
import { downloadBundleZip } from './bundle_export.js';
import { looksLikeFel, unpackFel } from './fel.js';
import { looksLikeCsv, parseCsvBuffer } from './csv_input.js';
import { downloadCompareHtmlReport, downloadHtmlReport } from './html_report.js';
import { downloadPdfReport } from './pdf_export.js';
import { clearCache, getCached, hashBuffer, putCached } from './cache.js';
import { MultiSession } from './multi_session.js';
import { ColumnStore } from './column_store.js';
import { wholeSessionStats, timeOfDayProfile, detectCtReversal, ctReversalNotice, ieee519Compliance, sarfiIndices, demandAnalysis } from './analysis.js';
import { buildNarrative } from './narrative.js';
import { formatLocalUtc, tzLabel } from './tzutil.js';
import {
  computeCost, loadTariff, normalizeTariff, parsePeakHoursString,
  peakHoursToString, saveTariff,
} from './tariff.js';
import { exportNotesJson, getAllNotes, getNote, setNote } from './notes.js';
import { analyzeInsights } from './insights.js';
import { analyzeCompareInsights } from './insights_compare.js';
import {
  rangeFromHash, rangeToHash, renderRangeSelector, scopeRecordsToRange,
} from './range_select.js';

// Try sibling first (e.g. when the Pages deploy flattens spec/ next to app.js),
// then fall back to ../spec/ for serving from the repo root.
const SPEC_CANDIDATES = [
  new URL('spec/field_map.json', import.meta.url).href,
  new URL('../spec/field_map.json', import.meta.url).href,
];

const DEFAULT_QUANTITIES = ['voltage', 'current', 'power', 'pf', 'frequency'];

const els = {
  dropZone:           document.getElementById('drop-zone'),
  fileInput:          document.getElementById('file-input'),
  dirInput:           document.getElementById('dir-input'),
  progressSec:        document.getElementById('progress-section'),
  progressBar:        document.getElementById('progress-bar'),
  progressLabel:      document.getElementById('progress-label'),
  summarySec:         document.getElementById('summary-section'),
  summaryGrid:        document.getElementById('summary-grid'),
  reverseA:           document.getElementById('reverse-cts-a'),
  reverseB:           document.getElementById('reverse-cts-b'),
  reverseC:           document.getElementById('reverse-cts-c'),
  errorSec:           document.getElementById('error-section'),
  errorMsg:           document.getElementById('error-message'),
  resetBtn:           document.getElementById('reset-button'),
  eventsSec:          document.getElementById('events-section'),
  eventsStatus:       document.getElementById('events-status'),
  eventsTbody:        document.getElementById('events-tbody'),
  eventsSearch:       document.getElementById('events-search'),
  eventsKindChips:    document.getElementById('events-kind-chips'),
  eventsClearFilters: document.getElementById('events-clear-filters'),
  eventsExportNotes:  document.getElementById('events-export-notes'),
  snapshotsSec:       document.getElementById('snapshots-section'),
  snapshotsList:      document.getElementById('snapshots-list'),
  controlsSec:        document.getElementById('controls-section'),
  quantityGrid:       document.getElementById('quantity-grid'),
  preSecsInput:       document.getElementById('pre-secs'),
  postSecsInput:      document.getElementById('post-secs'),
  renderBtn:          document.getElementById('render-button'),
  chartsSec:          document.getElementById('charts-section'),
  fullCharts:         document.getElementById('full-charts'),
  eventCharts:        document.getElementById('event-charts'),
  eventChartsHead:    document.getElementById('event-charts-heading'),
  snapshotCharts:     document.getElementById('snapshot-charts'),
  snapshotChartsHead: document.getElementById('snapshot-charts-heading'),
  insightsSec:        document.getElementById('insights-section'),
  insightsStatus:     document.getElementById('insights-status'),
  insightsList:       document.getElementById('insights-list'),
  rangeSec:           document.getElementById('range-section'),
  rangeContainer:     document.getElementById('range-container'),
  exportSec:          document.getElementById('export-section'),
  exportXlsxBtn:      document.getElementById('export-xlsx-btn'),
  exportHtmlBtn:      document.getElementById('export-html-btn'),
  exportPdfBtn:       document.getElementById('export-pdf-btn'),
  exportBundleBtn:    document.getElementById('export-bundle-btn'),
  sessionsSec:        document.getElementById('sessions-section'),
  sessionsBar:        document.getElementById('sessions-bar'),
  addSessionInput:    document.getElementById('add-session-input'),
  compareToggleBtn:   document.getElementById('compare-toggle-btn'),
  tariffSec:          document.getElementById('tariff-section'),
  tariffCurrency:     document.getElementById('tariff-currency'),
  tariffPeakRate:     document.getElementById('tariff-peak-rate'),
  tariffOffpeakRate:  document.getElementById('tariff-offpeak-rate'),
  tariffPeakHours:    document.getElementById('tariff-peak-hours'),
  tariffApplyBtn:     document.getElementById('tariff-apply-btn'),
  tariffResult:       document.getElementById('tariff-result'),
  breakerRating:      document.getElementById('breaker-rating'),
};

const ms = new MultiSession();

let cachedSpec = null;
let currentArrayBuffer = null;
let currentConfig = null;     // parsed ES.NNN-config.json companion (or null)
let currentStore = null;      // ColumnStore (memory-bounded; analysis + charts)
let currentRecords = null;    // record array — only for small/CSV paths or read-through
let currentFile = null;       // the dropped File/Blob, kept for read-through export
let currentRecordCount = 0;
let currentTimeRangeMs = null;
let currentEvents = [];       // detected events for currentRecords
let currentSnapshots = [];    // picked snapshots for currentRecords
let currentFindings = [];     // insights for currentRecords
let currentRange = null;      // {startMs, endMs} or null = whole session
let rangeSelector = null;     // the renderRangeSelector handle
let currentFileHash = null;   // SHA-256 of the parsed file bytes (for notes)
let currentWorker = null;

// --- Spec lazy-load ---------------------------------------------------------

async function getSpec() {
  if (cachedSpec) return cachedSpec;
  let lastStatus = null;
  for (const url of SPEC_CANDIDATES) {
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        cachedSpec = await resp.json();
        return cachedSpec;
      }
      lastStatus = resp.status;
    } catch (_) {
      // network error, try next candidate
    }
  }
  throw new Error(`Failed to load spec/field_map.json (last status: ${lastStatus ?? 'no response'})`);
}

// --- File picking -----------------------------------------------------------

function findInFileList(files, predicate) {
  for (const f of files) {
    if (predicate(f)) return f;
  }
  return null;
}

async function handleFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  // 0) Pre-parsed CSV — fastest path, skips the binary parser entirely.
  const csvFile = findInFileList(fileList, looksLikeCsv);
  if (csvFile) {
    await handleCsvFile(csvFile);
    return;
  }

  // 1) If any dropped file is a .fel, unpack in memory and use that.
  const felFile = findInFileList(fileList, looksLikeFel);
  if (felFile) {
    await handleFelFile(felFile);
    return;
  }

  // 2) Otherwise look for trend.bin (+ optional config.json) by filename.
  let trendFile = null;
  let configFile = null;
  for (const f of fileList) {
    const name = f.name.toLowerCase();
    if (name === 'trend.bin') trendFile = f;
    else if (name.endsWith('-config.json')) configFile = f;
  }
  // 3) Last-resort: accept the first .bin
  if (!trendFile) {
    trendFile = findInFileList(fileList, (f) => f.name.toLowerCase().endsWith('.bin'));
  }
  if (!trendFile) {
    showError(new Error('Drop a trend.bin, a .fel file, or a session folder containing one.'));
    return;
  }

  currentConfig = null;
  if (configFile) {
    try {
      const text = await configFile.text();
      currentConfig = JSON.parse(text);
    } catch (e) {
      console.warn('Could not parse config JSON:', e);
    }
  }

  await parseFile(trendFile);
}

async function handleFelFile(file) {
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;
  setProgress(0, 100, 'unpacking .fel');
  try {
    const ab = await file.arrayBuffer();
    const { trendBuffer, config } = await unpackFel(ab);
    currentConfig = config;
    currentArrayBuffer = trendBuffer;
    await parseBuffer();
  } catch (e) {
    showError(e);
  }
}

async function handleCsvFile(file) {
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;
  setProgress(0, 100, 'reading CSV');
  try {
    const ab = await file.arrayBuffer();
    const spec = await getSpec();
    const { records } = parseCsvBuffer(ab, spec);
    // CSV input has no companion config (unlike .fel/folder); keep currentConfig as-is.
    currentArrayBuffer = null;  // no binary to cache or re-parse
    await onParseDone({ records, recordCount: records.length });
  } catch (e) {
    showError(e);
  }
}

async function parseFile(file) {
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;
  currentFile = file;            // kept for read-through CSV/zoom export
  currentArrayBuffer = null;     // streaming path never holds the whole buffer
  await parseStreaming(file);
}

// Streaming columnar path (Feature A): hand the File to the worker, which reads
// it in 8 MB record-aligned chunks and transfers back typed-array columns. The
// full 438 MB ArrayBuffer is never resident on either thread.
async function parseStreaming(file) {
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;
  const spec = await getSpec();
  const reverseCts = selectedReversePhases();

  setProgress(0, 100, 'parsing (streaming)');
  if (currentWorker) currentWorker.terminate();
  currentWorker = new Worker(new URL('./parser_worker.js', import.meta.url),
                             { type: 'module' });
  currentWorker.onmessage = (event) => {
    const msg = event.data;
    if (msg.type === 'progress') {
      setProgress(msg.done, msg.total,
        `parsing record ${msg.done.toLocaleString()} / ${msg.total.toLocaleString()}`);
    } else if (msg.type === 'done-columnar') {
      const store = ColumnStore.fromTransfer(msg);
      onParseDoneColumnar(store).catch(showError);
    } else if (msg.type === 'error') {
      showError(new Error(msg.message));
    }
  };
  currentWorker.onerror = (event) => {
    showError(new Error(`Worker error: ${event.message ?? 'unknown'}`));
  };
  currentWorker.postMessage({ type: 'parse-stream', spec, blob: file, reverseCts });
}

async function parseBuffer() {
  if (!currentArrayBuffer) return;
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;

  const spec = await getSpec();
  const reverseCts = selectedReversePhases();

  // Cache key includes reverse-CTS choice so different toggles get separate
  // cache slots (cheap, avoids re-parsing for each phase combo).
  let cacheKey = null;
  try {
    setProgress(0, 100, 'hashing');
    const baseHash = await hashBuffer(currentArrayBuffer);
    const revTag = reverseCts === false ? 'none'
      : Array.isArray(reverseCts) ? reverseCts.sort().join('') : 'all';
    cacheKey = `${baseHash}:${revTag}`;
    const hit = await getCached(cacheKey);
    if (hit && hit.records) {
      setProgress(100, 100, 'cache hit — skipped parse');
      // Use cached config if our companion-file lookup didn't find one
      if (!currentConfig && hit.config) currentConfig = hit.config;
      await onParseDone({
        records: hit.records, recordCount: hit.records.length,
        cached: true, fileHash: baseHash,
      });
      return;
    }
  } catch (e) {
    console.warn('Cache lookup failed, parsing fresh:', e);
  }

  setProgress(0, 100, 'parsing');
  if (currentWorker) currentWorker.terminate();
  currentWorker = new Worker(new URL('./parser_worker.js', import.meta.url),
                             { type: 'module' });
  currentWorker.onmessage = (event) => {
    const msg = event.data;
    if (msg.type === 'progress') {
      setProgress(msg.done, msg.total, `parsing record ${msg.done.toLocaleString()} / ${msg.total.toLocaleString()}`);
    } else if (msg.type === 'done') {
      onParseDone({ ...msg, fileHash: cacheKey ? cacheKey.split(':')[0] : null }).then(() => {
        if (cacheKey && msg.records) {
          putCached(cacheKey, msg.records, currentConfig).catch(() => {});
        }
      });
    } else if (msg.type === 'error') {
      showError(new Error(msg.message));
    }
  };
  currentWorker.onerror = (event) => {
    showError(new Error(`Worker error: ${event.message ?? 'unknown'}`));
  };
  currentWorker.postMessage({
    type: 'parse',
    spec,
    arrayBuffer: currentArrayBuffer,
    reverseCts,
  });
}

function selectedReversePhases() {
  const phases = [];
  if (els.reverseA?.checked) phases.push('a');
  if (els.reverseB?.checked) phases.push('b');
  if (els.reverseC?.checked) phases.push('c');
  return phases.length === 0 ? false : phases;
}

async function onParseDone(msg) {
  currentRecords = msg.records;
  currentStore = null;          // legacy/CSV/small path keeps record objects
  currentRecordCount = msg.recordCount;
  currentFileHash = msg.fileHash ?? currentFileHash;
  if (msg.records.length > 0) {
    const first = msg.records[0];
    const last = msg.records[msg.records.length - 1];
    currentTimeRangeMs = [first.startMs, last.endMs];
  } else {
    currentTimeRangeMs = null;
  }
  els.progressSec.hidden = true;
  renderSummary();
  els.summarySec.hidden = false;

  // Run event detection on main thread — fast enough for ~75 K records.
  setProgress(0, 100, 'detecting events');
  els.progressSec.hidden = false;
  await new Promise((r) => setTimeout(r, 0));  // let progress repaint
  try {
    const spec = await getSpec();
    currentEvents = detectEvents(currentRecords, spec);
    currentSnapshots = pickSnapshots(currentRecords, currentEvents, spec, { n: 3 });
    currentFindings = analyzeInsights(currentRecords, currentEvents, spec,
                                      currentSnapshots, currentConfig,
                                      { breakerRatingA: loadBreakerRating() });
    // Push into multi-session state (idempotent if added externally).
    if (!msg.skipMsAdd) {
      ms.add({
        records: currentRecords,
        events: currentEvents,
        snapshots: currentSnapshots,
        findings: currentFindings,
        config: currentConfig,
        fileHash: msg.fileHash ?? null,
      });
    }
    renderInsights();
    renderEventsTable();
    renderSnapshotsList();
    renderQuantityGrid();
    renderStatsPanel(spec);
    renderNarrative(spec);
    checkCtReversal(spec);
    renderSessionsBar();
    els.insightsSec.hidden = currentFindings.length === 0;
    els.sessionsSec.hidden = false;
    els.eventsSec.hidden = false;
    els.snapshotsSec.hidden = currentSnapshots.length === 0;
    els.controlsSec.hidden = false;
    els.exportSec.hidden = false;
    els.rangeSec.hidden = false;
    els.tariffSec.hidden = false;
    setupRangeSelector(spec);
    loadTariffIntoForm();
    renderTariffResult();
    // Unlock Explore + Export tabs and auto-advance to Explore.
    tabState.hasSession = true;
    updateTabUnlocks();
    if (tabState.current === 'import') activateTab('explore');
  } catch (e) {
    showError(e);
    return;
  } finally {
    els.progressSec.hidden = true;
  }
}

// Columnar parse-done: analysis + charts run on the ColumnStore (memory-bounded);
// currentRecords stays null so the 7-day file never re-materialises 590 K objects.
async function onParseDoneColumnar(store) {
  currentStore = store;
  currentRecords = null;
  currentRecordCount = store.n;
  if (store.n > 0) {
    currentTimeRangeMs = [store.firstStartMs, store.lastEndMs];
  } else {
    currentTimeRangeMs = null;
  }
  els.progressSec.hidden = true;
  renderSummary();
  els.summarySec.hidden = false;

  setProgress(0, 100, 'detecting events');
  els.progressSec.hidden = false;
  await new Promise((r) => setTimeout(r, 0));
  try {
    const spec = await getSpec();
    currentEvents = detectEvents(currentStore, spec);
    currentSnapshots = pickSnapshots(currentStore, currentEvents, spec, { n: 3 });
    currentFindings = analyzeInsights(currentStore, currentEvents, spec,
                                      currentSnapshots, currentConfig,
                                      { breakerRatingA: loadBreakerRating() });
    ms.add({
      records: null, store: currentStore,
      events: currentEvents, snapshots: currentSnapshots,
      findings: currentFindings, config: currentConfig,
      fileHash: currentFileHash, file: currentFile,
    });
    renderInsights();
    renderEventsTable();
    renderSnapshotsList();
    renderQuantityGrid();
    renderStatsPanel(spec);
    renderNarrative(spec);
    checkCtReversal(spec);
    renderSessionsBar();
    els.insightsSec.hidden = currentFindings.length === 0;
    els.sessionsSec.hidden = false;
    els.eventsSec.hidden = false;
    els.snapshotsSec.hidden = currentSnapshots.length === 0;
    els.controlsSec.hidden = false;
    els.exportSec.hidden = false;
    els.rangeSec.hidden = false;
    els.tariffSec.hidden = false;
    setupRangeSelector(spec);
    loadTariffIntoForm();
    renderTariffResult();
    tabState.hasSession = true;
    updateTabUnlocks();
    if (tabState.current === 'import') activateTab('explore');
  } catch (e) {
    showError(e);
    return;
  } finally {
    els.progressSec.hidden = true;
  }
}

// The data source the analysis / chart / range engines should read: the store
// when present (streaming path), else the records array (small / CSV path).
function dataSource() {
  return currentStore || currentRecords;
}

// Per-session column accessor for compare-overlay charts: reads from a
// session's ColumnStore when present, else its records array. Only the few
// channels FULL_QUANTITIES references (all retained) are needed here.
function sessionAccessor(session, spec, name) {
  if (session.store) {
    const col = session.store.cols[name];
    const n = session.store.n;
    return {
      n,
      startMs: (i) => session.store.startMs[i],
      value: (i) => (col ? col[i] : 0),
      base: n ? session.store.startMs[0] : 0,
    };
  }
  const recs = session.records || [];
  const fi = new Map(spec.fields.map((f) => [f.name, f.index]));
  const idx = fi.get(name);
  return {
    n: recs.length,
    startMs: (i) => recs[i].startMs,
    value: (i) => recs[i].floats[idx],
    base: recs.length ? recs[0].startMs : 0,
  };
}

// Materialise a records array for an export that genuinely needs every field
// (CSV / XLSX / bundle). For the store path this is a transient allocation that
// is dropped when the export finishes — it is NOT kept resident.
function recordsForExport(spec) {
  if (currentRecords) return currentRecords;
  if (currentStore) return currentStore.toRecords(spec);
  return [];
}

// Statistics panel (Feature B) — whole-session stats table + time-of-day chart,
// computed straight off the resident ColumnStore so the CLI and web agree.
let currentStats = null;       // last computed whole_session_stats (for exports)
let currentTodRows = null;     // last computed time-of-day profile (for exports)
let currentNarrative = null;   // executive-summary narrative (Feature E)
let currentPq = null;          // IEEE 519 + SARFI power-quality (Feature F)
let currentDemand = null;      // rolling peak-demand analysis (Feature G)
let currentTz = null;          // report timezone (IANA) or null = UTC (Feature H)

const TZ_STORAGE_KEY = 'fluke3540.tz';

// Render the tz-aware time range under the summary (local + UTC, or UTC only).
function renderTzRange() {
  const span = document.getElementById('tz-range');
  if (!span || !currentTimeRangeMs) { if (span) span.textContent = ''; return; }
  let valid = currentTz;
  if (valid) {
    // Validate the zone; fall back to UTC on a bad name.
    try { formatLocalUtc(currentTimeRangeMs[0], valid); } catch (_) { valid = null; }
  }
  const [t0, t1] = currentTimeRangeMs;
  span.textContent =
    ` ${tzLabel(valid)} — start ${formatLocalUtc(t0, valid)}; end ${formatLocalUtc(t1, valid)}`;
}

// Median non-outage L-N voltage for SARFI residual %, from the stats sketch
// (falls back to 277 V if voltage stats are unavailable).
function inferNominalForPq(src) {
  if (currentStats && currentStats.V_LN_a_avg_V) {
    const m = currentStats.V_LN_a_avg_V.median;
    if (Number.isFinite(m) && m > 50) return m;
  }
  return 277.0;
}

// CT-reversal banner (Feature C): warn when real power is sustained-negative on
// a load and reverse-CTs isn't already applied. The Apply button ticks all
// phase boxes and re-parses (which negates P/Q/PF/energy).
function checkCtReversal(spec) {
  const banner = document.getElementById('ct-reversal-banner');
  const msg = document.getElementById('ct-reversal-msg');
  if (!banner) return;
  const src = dataSource();
  const reverseOn = selectedReversePhases() !== false;
  if (!src || reverseOn) { banner.hidden = true; return; }
  let res;
  try { res = detectCtReversal(src, spec); } catch (_) { banner.hidden = true; return; }
  if (!res.reversed) { banner.hidden = true; return; }
  if (msg) msg.textContent = ' ' + ctReversalNotice(res);
  banner.hidden = false;
}

// Executive summary (Feature E) — built from events + insights + stats + ct.
function renderNarrative(spec) {
  const sec = document.getElementById('narrative-section');
  const el = document.getElementById('narrative-text');
  if (!sec || !el) return;
  const src = dataSource();
  if (!src) { sec.hidden = true; return; }
  let ct = null;
  try { ct = detectCtReversal(src, spec); } catch (_) { /* ignore */ }
  const durationSecs = currentTimeRangeMs
    ? (currentTimeRangeMs[1] - currentTimeRangeMs[0]) / 1000 : null;
  currentNarrative = buildNarrative(currentEvents, currentFindings, currentStats, ct, {
    config: currentConfig, totalRecords: currentRecordCount, durationSecs,
  });
  el.textContent = currentNarrative;
  sec.hidden = false;
}

function renderStatsPanel(spec) {
  const sec = document.getElementById('stats-section');
  const wrap = document.getElementById('stats-table-wrap');
  const status = document.getElementById('stats-status');
  if (!sec || !wrap) return;
  const src = dataSource();
  if (!src) { sec.hidden = true; return; }
  try {
    currentStats = wholeSessionStats(src, spec);
    currentTodRows = timeOfDayProfile(src, spec, { window: [0, 1440], binMinutes: 1 });
  } catch (e) {
    console.warn('stats failed:', e);
    sec.hidden = true;
    return;
  }
  sec.hidden = false;

  // Stats table.
  const table = document.createElement('table');
  table.className = 'stats-table';
  const thead = document.createElement('thead');
  const hr = document.createElement('tr');
  for (const h of ['Channel', 'Unit', 'Min', 'p1', 'p5', 'Median', 'Mean', 'p95', 'p99', 'Max', 'Stdev']) {
    const th = document.createElement('th');
    th.textContent = h;
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  const fmt = (v) => (Number.isFinite(v) ? (Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2)) : '—');
  for (const [name, d] of Object.entries(currentStats)) {
    if (name.startsWith('_')) continue;
    const tr = document.createElement('tr');
    const cells = [name, d.unit, fmt(d.min), fmt(d.p1), fmt(d.p5), fmt(d.median),
                   fmt(d.mean), fmt(d.p95), fmt(d.p99), fmt(d.max), fmt(d.stdev)];
    for (const c of cells) {
      const td = document.createElement('td');
      td.textContent = c;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  const th = currentStats._thresholds || {};
  const note = document.createElement('p');
  note.className = 'stats-thresholds';
  const us = document.createElement('small');
  us.textContent =
    `Under-voltage (<${th.undervoltage_v} V, any phase, non-outage): ` +
    `${th.sec_undervoltage} s (${(th.pct_undervoltage ?? 0).toFixed(2)}%). ` +
    `Over-current (>${th.overcurrent_a} A, any phase): ` +
    `${th.sec_overcurrent} s (${(th.pct_overcurrent ?? 0).toFixed(2)}%).`;
  note.appendChild(us);

  // IEEE 519 + SARFI power-quality summary (Feature F).
  currentPq = null;
  const pqP = document.createElement('p');
  pqP.className = 'stats-pq';
  try {
    const ieee = ieee519Compliance(src, spec);
    const sarfi = sarfiIndices(currentEvents, inferNominalForPq(src));
    currentPq = { ieee519: ieee, sarfi };
    const vv = ieee.voltage;
    const pqs = document.createElement('small');
    pqs.textContent =
      `IEEE 519 V_THD p95 (limit ${ieee.limit_v_thd_pct.toFixed(0)}%): ` +
      `a=${vv.a.p95.toFixed(1)}% b=${vv.b.p95.toFixed(1)}% c=${vv.c.p95.toFixed(1)}% — ` +
      `${ieee.all_voltage_compliant ? 'COMPLIANT' : 'NON-COMPLIANT'}. ` +
      `SARFI-90=${sarfi['SARFI-90']}, SARFI-70=${sarfi['SARFI-70']}, ` +
      `SARFI-10=${sarfi['SARFI-10']} (${sarfi.events_considered} voltage events).`;
    pqP.appendChild(pqs);
  } catch (_) { /* THD columns may be absent on some inputs */ }

  // Rolling peak-demand (Feature G), 15-min window.
  currentDemand = null;
  const demP = document.createElement('p');
  demP.className = 'stats-demand';
  try {
    currentDemand = demandAnalysis(src, spec, { windowSecs: 900 });
    const ds = document.createElement('small');
    if (currentDemand.n_windows) {
      ds.textContent =
        `Peak 15-min demand: ${currentDemand.peak_demand_kw.toFixed(1)} kW ` +
        `(window ending ${new Date(currentDemand.peak_window_end).toISOString().slice(0, 19)}Z); ` +
        `mean demand ${(currentDemand.mean_demand_w / 1000).toFixed(1)} kW.`;
    } else {
      ds.textContent = 'Peak demand: session shorter than the 15-min window.';
    }
    demP.appendChild(ds);
  } catch (_) { /* ignore */ }

  wrap.replaceChildren(table, note, pqP, demP);
  if (status) status.firstChild
    ? (status.firstChild.textContent = `${Object.keys(currentStats).length - 1} channels over ${(th.total_records || 0).toLocaleString()} records.`)
    : (status.textContent = '');

  renderTodChart();
}

function renderTodChart() {
  const head = document.getElementById('tod-heading');
  const div = document.getElementById('tod-chart');
  const uPlot = window.uPlot;
  if (!div) return;
  div.replaceChildren();
  if (!uPlot || !currentTodRows || currentTodRows.length === 0) {
    if (head) head.hidden = true;
    return;
  }
  if (head) head.hidden = false;
  // x = minute-of-day index; series = P avg (kW), V avg (V), I avg (A).
  const xs = currentTodRows.map((_, i) => i);
  const pAvg = currentTodRows.map((r) => r.p_avg_kW);
  const vAvg = currentTodRows.map((r) => r.v_avg_V);
  const iAvg = currentTodRows.map((r) => r.i_avg_A);
  const plot = new uPlot({
    width: div.clientWidth || 900,
    height: 240,
    series: [
      { label: 'bin' },
      { label: 'P avg (kW)', stroke: '#cc0000', width: 1.4 },
      { label: 'V avg (V)', stroke: '#0066cc', width: 1, scale: 'v' },
      { label: 'I avg (A)', stroke: '#009933', width: 1, scale: 'i' },
    ],
    scales: { x: { time: false } },
    axes: [
      { stroke: '#666', label: 'time-of-day bin' },
      { stroke: '#666', label: 'P (kW)' },
      { stroke: '#666', scale: 'v', side: 1, label: 'V' },
    ],
    legend: { live: true },
  }, [xs, pAvg, vAvg, iAvg], div);
  const resizeObs = new ResizeObserver(() => {
    plot.setSize({ width: div.clientWidth, height: 240 });
  });
  resizeObs.observe(div);
}

// --- Summary rendering ------------------------------------------------------

function formatDuration(ms) {
  if (!ms) return '—';
  const s = Math.round(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${m}m ${sec}s`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function renderSummary() {
  const dl = document.createElement('dl');
  dl.className = 'summary-grid';

  const add = (k, v) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    if (v instanceof Node) dd.appendChild(v);
    else dd.textContent = v ?? '—';
    dl.appendChild(dt);
    dl.appendChild(dd);
  };

  if (currentConfig) {
    if (currentConfig.asset_name)  add('Asset',       currentConfig.asset_name);
    if (currentConfig.team_name)   add('Team',        currentConfig.team_name);
    if (currentConfig.type)        add('Instrument',  `${currentConfig.type}${currentConfig.firmware_version ? ' fw ' + currentConfig.firmware_version : ''}`);
  }
  add('Records',       currentRecordCount.toLocaleString());
  if (currentTimeRangeMs) {
    const [t0, t1] = currentTimeRangeMs;
    add('Start (UTC)', new Date(t0).toISOString());
    add('End (UTC)',   new Date(t1).toISOString());
    add('Duration',    formatDuration(t1 - t0));
  }
  const rev = selectedReversePhases();
  add('Reverse CTs', rev === false ? 'off' :
    rev.length === 3 ? 'all phases' : `phase(s) ${rev.join(', ')}`);

  els.summaryGrid.replaceWith(dl);
  dl.id = 'summary-grid';
  els.summaryGrid = dl;
  renderTzRange();
}

// --- UI plumbing ------------------------------------------------------------

function setProgress(done, total, label) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  els.progressBar.value = pct;
  els.progressBar.max = 100;
  els.progressLabel.textContent = `${pct}% — ${label}`;
}

function showError(err) {
  els.summarySec.hidden = true;
  els.progressSec.hidden = true;
  els.errorSec.hidden = false;
  els.errorMsg.textContent = err?.message ?? String(err);
}

function hideError() {
  els.errorSec.hidden = true;
}

function resetUi() {
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = true;
  els.sessionsSec.hidden = true;
  els.insightsSec.hidden = true;
  { const s = document.getElementById('stats-section'); if (s) s.hidden = true; }
  { const s = document.getElementById('narrative-section'); if (s) s.hidden = true; }
  els.eventsSec.hidden = true;
  els.snapshotsSec.hidden = true;
  els.controlsSec.hidden = true;
  els.chartsSec.hidden = true;
  els.rangeSec.hidden = true;
  els.tariffSec.hidden = true;
  if (rangeSelector) { rangeSelector.destroy(); rangeSelector = null; }
  currentRange = null;
  ms.clear();
  tabState.hasSession = false;
  tabState.hasCharts = false;
  updateTabUnlocks();
  activateTab('import');
  els.fileInput.value = '';
  els.dirInput.value = '';
}

// --- Tariff -----------------------------------------------------------------

function activeAssetName() {
  return currentConfig?.asset_name || 'default';
}

function loadTariffIntoForm() {
  const t = loadTariff(activeAssetName()) || {
    currency: 'USD', peakRate: 0, offpeakRate: 0, peakHours: [],
  };
  els.tariffCurrency.value = t.currency;
  els.tariffPeakRate.value = String(t.peakRate);
  els.tariffOffpeakRate.value = String(t.offpeakRate);
  els.tariffPeakHours.value = peakHoursToString(t.peakHours);
  els.breakerRating.value = String(loadBreakerRating() || '');
}

function loadBreakerRating() {
  try {
    const raw = localStorage.getItem('breaker:' + activeAssetName());
    return raw ? Number(raw) : 0;
  } catch (_) { return 0; }
}

function saveBreakerRating(amps) {
  try {
    if (amps > 0) localStorage.setItem('breaker:' + activeAssetName(), String(amps));
    else localStorage.removeItem('breaker:' + activeAssetName());
  } catch (_) {/* ignore */}
}

function getTariffFromForm() {
  return normalizeTariff({
    currency: els.tariffCurrency.value || 'USD',
    peakRate: Number(els.tariffPeakRate.value),
    offpeakRate: Number(els.tariffOffpeakRate.value),
    peakHours: parsePeakHoursString(els.tariffPeakHours.value),
  });
}

async function renderTariffResult() {
  els.tariffResult.replaceChildren();
  if (!dataSource()) return;
  const t = getTariffFromForm();
  if (t.peakRate === 0 && t.offpeakRate === 0) return;
  const spec = await getSpec();
  const cost = computeCost(dataSource(), spec, t);
  const fmt = (n) => `${t.currency} ${n.toFixed(2)}`;
  const fmtKwh = (n) => `${n.toFixed(2)} kWh`;
  const dl = document.createElement('dl');
  dl.className = 'summary-grid';
  const add = (k, v) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    dl.appendChild(dt); dl.appendChild(dd);
  };
  add('Imported cost', fmt(cost.importedCost));
  add('Exported cost', fmt(cost.exportedCost));
  add('Net cost',      fmt(cost.netCost));
  add('Peak / off-peak kWh', `${fmtKwh(cost.peakKwh)}  /  ${fmtKwh(cost.offpeakKwh)}`);
  add('Peak / off-peak cost', `${fmt(cost.peakCost)}  /  ${fmt(cost.offpeakCost)}`);
  els.tariffResult.replaceWith(dl);
  dl.id = 'tariff-result';
  els.tariffResult = dl;
}

function renderSessionsBar() {
  els.sessionsBar.replaceChildren();
  const all = ms.getAll();
  const active = ms.getActive();
  for (const s of all) {
    const pill = document.createElement('span');
    pill.className = 'session-pill' + (s === active ? ' is-active' : '');
    pill.style.color = s.color;
    const label = document.createElement('span');
    label.className = 'label-text';
    label.textContent = s.label;
    label.contentEditable = 'true';
    label.spellcheck = false;
    label.addEventListener('blur', () => {
      const newLabel = label.textContent.trim();
      if (newLabel && newLabel !== s.label) {
        if (!ms.rename(s.label, newLabel)) label.textContent = s.label;
      } else {
        label.textContent = s.label;
      }
    });
    label.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); label.blur(); }
    });
    pill.addEventListener('click', (e) => {
      if (e.target === label) return;  // editing the label
      switchToSession(s.label);
    });
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'close-btn';
    close.textContent = '×';
    close.title = 'Remove this session';
    close.addEventListener('click', (e) => {
      e.stopPropagation();
      ms.remove(s.label);
      const a = ms.getActive();
      if (a) switchToSession(a.label);
      else resetUi();
      els.compareToggleBtn.hidden = !ms.canCompare();
    });
    pill.appendChild(label);
    pill.appendChild(close);
    els.sessionsBar.appendChild(pill);
  }
  els.compareToggleBtn.hidden = !ms.canCompare();
  els.compareToggleBtn.classList.toggle('is-on', ms.compareMode);
  els.compareToggleBtn.textContent = ms.compareMode ? 'Exit compare overlay' : 'Compare overlay';
}

function switchToSession(label) {
  if (!ms.setActive(label)) return;
  const s = ms.getActive();
  if (!s) return;
  currentRecords = s.records || null;
  currentStore = s.store || null;
  currentFile = s.file || null;
  currentEvents = s.events;
  currentSnapshots = s.snapshots;
  currentFindings = s.findings;
  currentConfig = s.config;
  currentArrayBuffer = null;  // can't re-parse a switched-to session
  const ds = currentStore || currentRecords;
  if (currentStore) {
    currentRecordCount = currentStore.n;
    currentTimeRangeMs = currentStore.n
      ? [currentStore.firstStartMs, currentStore.lastEndMs] : null;
  } else if (currentRecords) {
    currentRecordCount = currentRecords.length;
    currentTimeRangeMs = currentRecords.length
      ? [currentRecords[0].startMs, currentRecords[currentRecords.length - 1].endMs]
      : null;
  } else {
    currentRecordCount = 0;
    currentTimeRangeMs = null;
  }
  renderSummary();
  renderInsights();
  renderEventsTable();
  renderSnapshotsList();
  renderSessionsBar();
}

function setupRangeSelector(spec) {
  if (rangeSelector) rangeSelector.destroy();
  rangeSelector = renderRangeSelector(
    els.rangeContainer, dataSource(), spec, (range) => {
      currentRange = range;
      const hash = rangeToHash(range);
      if (hash) history.replaceState(null, '', hash);
      else history.replaceState(null, '', location.pathname + location.search);
    }
  );
  // Restore from URL hash if present
  const hashRange = rangeFromHash(location.hash);
  if (hashRange) {
    currentRange = hashRange;
    rangeSelector.setRange(hashRange);
  }
}

function renderInsights() {
  els.insightsList.replaceChildren();
  els.insightsStatus.firstChild.textContent =
    currentFindings.length === 0
      ? 'No notable patterns detected.'
      : `${currentFindings.length} finding(s) — sorted by severity.`;
  for (const f of currentFindings) {
    const card = document.createElement('article');
    card.className = `insight-card severity-${f.severity}`;
    const title = document.createElement('h3');
    title.textContent = f.headline;
    const meta = document.createElement('p');
    meta.className = 'insight-meta';
    meta.textContent = `${f.kind} · ${f.severity}`;
    const detail = document.createElement('p');
    detail.textContent = f.detail;
    card.appendChild(title);
    card.appendChild(meta);
    card.appendChild(detail);

    if ((f.recommendedActions ?? []).length) {
      const det = document.createElement('details');
      const sum = document.createElement('summary');
      sum.textContent = `Recommended (${f.recommendedActions.length})`;
      det.appendChild(sum);
      const ul = document.createElement('ul');
      for (const a of f.recommendedActions) {
        const li = document.createElement('li');
        li.textContent = a;
        ul.appendChild(li);
      }
      det.appendChild(ul);
      card.appendChild(det);
    }

    if ((f.relatedEventIds ?? []).length) {
      const rel = document.createElement('p');
      rel.className = 'insight-related';
      rel.appendChild(document.createTextNode('Related events: '));
      for (const id of f.relatedEventIds) {
        const a = document.createElement('a');
        a.href = '#';
        a.textContent = `#${id}`;
        a.addEventListener('click', (e) => {
          e.preventDefault();
          scrollToEvent(id);
        });
        rel.appendChild(a);
      }
      card.appendChild(rel);
    }
    els.insightsList.appendChild(card);
  }
}

function scrollToEvent(eventId) {
  const cb = els.eventsTbody?.querySelector(`input[data-event-id="${eventId}"]`);
  if (!cb) return;
  cb.checked = true;
  const tr = cb.closest('tr');
  tr?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  tr?.style.setProperty('transition', 'background-color 1.5s ease');
  tr?.style.setProperty('background-color', 'rgba(255, 230, 100, 0.5)');
  setTimeout(() => tr?.style.removeProperty('background-color'), 1500);
}

async function exportXlsx() {
  if (!dataSource()) return;
  const spec = await getSpec();
  const scoped = scopeRecordsToRange(recordsForExport(spec), currentRange);
  const blob = buildXlsx({ records: scoped, spec, config: currentConfig });
  const name = (currentConfig?.asset_name ?? 'fluke_session').replace(/[^a-zA-Z0-9._-]+/g, '_');
  const suffix = currentRange ? '_range' : '';
  downloadBlob(blob, `${name}${suffix}_report.xlsx`);
}

async function exportBundle() {
  if (!dataSource()) return;
  const spec = await getSpec();
  const scoped = scopeRecordsToRange(recordsForExport(spec), currentRange);
  const xlsxBlob = buildXlsx({ records: scoped, spec, config: currentConfig });
  await downloadBundleZip({
    records: scoped, spec, xlsxBlob,
    assetName: currentConfig?.asset_name,
  });
}

async function exportPdf() {
  if (!dataSource()) return;
  const spec = await getSpec();
  const scoped = scopeRecordsToRange(recordsForExport(spec), currentRange);
  const scopedEvents = currentRange
    ? currentEvents.filter((e) =>
        !(e.tEndMs < currentRange.startMs || e.tStartMs > currentRange.endMs))
    : currentEvents;
  const rangeSuffix = currentRange
    ? ` (range ${new Date(currentRange.startMs).toISOString().slice(11, 19)}-${new Date(currentRange.endMs).toISOString().slice(11, 19)})`
    : '';
  const title = `Fluke 3540 FC — ${currentConfig?.asset_name ?? 'Session'} Report${rangeSuffix}`;
  await downloadPdfReport({
    title, config: currentConfig, records: scoped, spec,
    events: scopedEvents, snapshots: currentSnapshots,
    findings: currentRange ? [] : currentFindings,
  });
}

async function exportHtmlReport() {
  if (!dataSource()) return;
  const spec = await getSpec();
  if (ms.compareMode && ms.canCompare()) {
    // Compare-mode HTML uses the per-session summary + cross-session findings.
    downloadCompareHtmlReport({
      title: 'Fluke 3540 FC — Multi-session Comparison',
      sessions: ms.getAll(),
      spec,
      findings: currentFindings,  // cross-session findings while in compare mode
    });
    return;
  }
  const scoped = scopeRecordsToRange(recordsForExport(spec), currentRange);
  // When scoping, also scope events/findings to overlap the range.
  const scopedEvents = currentRange
    ? currentEvents.filter((e) =>
        !(e.tEndMs < currentRange.startMs || e.tStartMs > currentRange.endMs))
    : currentEvents;
  const scopedFindings = currentRange ? [] : currentFindings;  // insights are session-scoped
  const rangeSuffix = currentRange
    ? ` (range ${new Date(currentRange.startMs).toISOString().slice(11, 19)}-${new Date(currentRange.endMs).toISOString().slice(11, 19)})`
    : '';
  const title = `Fluke 3540 FC — ${currentConfig?.asset_name ?? 'Session'} Report${rangeSuffix}`;
  downloadHtmlReport({
    title, config: currentConfig, records: scoped, spec,
    events: scopedEvents, snapshots: currentSnapshots,
    findings: scopedFindings,
    wholeStats: currentRange ? null : currentStats,
    narrative: currentRange ? null : currentNarrative,
    pq: currentRange ? null : currentPq,
    demand: currentRange ? null : currentDemand,
    tz: currentTz,
  });
}

// --- Events + snapshots + controls rendering --------------------------------

function formatDate(ms) {
  return new Date(ms).toISOString();
}

// Tracks which event kinds are hidden by the chip toggles.
const hiddenKinds = new Set();

// Current events-table sort: {col: 'time'|'kind'|'severity'|'id', dir: 1|-1}
let eventsSort = { col: 'time', dir: 1 };

function sortedEvents() {
  const cmp = (a, b) => {
    switch (eventsSort.col) {
      case 'id':       return (a.id - b.id) * eventsSort.dir;
      case 'kind':     return a.kind.localeCompare(b.kind) * eventsSort.dir;
      case 'time':     return (a.tStartMs - b.tStartMs) * eventsSort.dir;
      case 'duration': return ((a.tEndMs - a.tStartMs) - (b.tEndMs - b.tStartMs)) * eventsSort.dir;
      case 'severity': return (Math.abs(b.severity) - Math.abs(a.severity)) * eventsSort.dir;
      default: return 0;
    }
  };
  return [...currentEvents].sort(cmp);
}

function renderEventsTable() {
  els.eventsStatus.firstChild.textContent =
    currentEvents.length === 0
      ? 'No events detected.'
      : `${currentEvents.length} event(s) detected. Tick rows to include them when you click Render.`;
  // Re-wire the header to be sortable (idempotent — replaces handlers on each render).
  const head = els.eventsTbody.parentElement?.querySelector('thead tr');
  if (head) {
    const cols = ['', 'id', 'kind', 'time', 'duration', 'phases', 'severity', ''];
    [...head.children].forEach((th, i) => {
      const key = cols[i];
      if (!key) return;
      th.style.cursor = 'pointer';
      th.onclick = () => {
        if (eventsSort.col === key) eventsSort.dir = -eventsSort.dir;
        else { eventsSort.col = key; eventsSort.dir = 1; }
        renderEventsTable();
      };
      const arrow = eventsSort.col === key ? (eventsSort.dir === 1 ? ' ▲' : ' ▼') : '';
      th.textContent = th.textContent.replace(/[ ▲▼]+$/, '') + arrow;
    });
  }
  els.eventsTbody.replaceChildren();
  for (const ev of sortedEvents()) {
    const tr = document.createElement('tr');
    tr.className = `kind-${ev.kind}`;
    tr.dataset.kind = ev.kind;
    tr.dataset.searchHaystack = (
      ev.kind + ' ' + (ev.affectedPhases ?? []).join(' ')
    ).toLowerCase();
    const td = (content) => {
      const c = document.createElement('td');
      if (content instanceof Node) c.appendChild(content);
      else c.textContent = content;
      return c;
    };
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.eventId = String(ev.id);
    cb.checked = false;
    tr.appendChild(td(cb));
    tr.appendChild(td(String(ev.id)));
    tr.appendChild(td(ev.kind));
    tr.appendChild(td(formatDate(ev.tStartMs)));
    const durSec = Math.max(1, Math.round((ev.tEndMs - ev.tStartMs) / 1000));
    tr.appendChild(td(`${durSec}s`));
    tr.appendChild(td((ev.affectedPhases ?? []).join('/') || '—'));
    tr.appendChild(td(ev.severity.toFixed(3)));
    // Snap-to-event button + note toggle in the action cell.
    const actionCell = document.createElement('td');
    const snap = document.createElement('button');
    snap.type = 'button';
    snap.className = 'snap-btn';
    snap.textContent = '⤓ snap';
    snap.title = 'Snap the range selector to this event ±60s';
    snap.addEventListener('click', () => snapRangeToEvent(ev));
    actionCell.appendChild(snap);

    const noteBtn = document.createElement('button');
    noteBtn.type = 'button';
    noteBtn.className = 'snap-btn note-btn';
    const existingNote = getNote(currentFileHash, ev.id);
    noteBtn.textContent = existingNote ? '📝*' : '📝';
    noteBtn.title = existingNote ? 'Edit note (saved)' : 'Add a note';
    noteBtn.addEventListener('click', () => toggleNoteRow(ev, tr, noteBtn));
    actionCell.appendChild(noteBtn);

    tr.appendChild(actionCell);
    els.eventsTbody.appendChild(tr);

    // If a note already exists, render an inline row beneath this one.
    if (existingNote) appendNoteRow(tr, ev, noteBtn);
  }
  renderKindChips();
  applyEventsFilter();
}

function toggleNoteRow(ev, tr, noteBtn) {
  const next = tr.nextElementSibling;
  if (next?.classList.contains('note-row') && next.dataset.eventId === String(ev.id)) {
    next.remove();
    if (!getNote(currentFileHash, ev.id)) noteBtn.textContent = '📝';
    return;
  }
  appendNoteRow(tr, ev, noteBtn);
}

function appendNoteRow(tr, ev, noteBtn) {
  const row = document.createElement('tr');
  row.className = 'note-row';
  row.dataset.eventId = String(ev.id);
  const cell = document.createElement('td');
  cell.colSpan = 8;
  const input = document.createElement('textarea');
  input.rows = 2;
  input.value = getNote(currentFileHash, ev.id);
  input.placeholder = `Add a note for event #${ev.id} (e.g. "verified utility outage SR-12345")`;
  input.style.width = '100%';
  input.addEventListener('blur', () => {
    const text = input.value;
    setNote(currentFileHash, ev.id, text);
    noteBtn.textContent = text.trim() ? '📝*' : '📝';
    noteBtn.title = text.trim() ? 'Edit note (saved)' : 'Add a note';
  });
  cell.appendChild(input);
  row.appendChild(cell);
  tr.parentElement.insertBefore(row, tr.nextElementSibling);
  input.focus();
}

function snapRangeToEvent(ev) {
  const padMs = 60_000;
  const range = {
    startMs: ev.tStartMs - padMs,
    endMs: ev.tEndMs + padMs,
  };
  currentRange = range;
  rangeSelector?.setRange(range);
  history.replaceState(null, '', rangeToHash(range));
  els.rangeSec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderKindChips() {
  els.eventsKindChips.replaceChildren();
  // Aggregate count and the max |severity| per kind so chips can carry
  // a colour intensity proportional to their worst event.
  const counts = new Map();
  const peakSev = new Map();
  for (const ev of currentEvents) {
    counts.set(ev.kind, (counts.get(ev.kind) ?? 0) + 1);
    const s = Math.abs(ev.severity);
    if (s > (peakSev.get(ev.kind) ?? 0)) peakSev.set(ev.kind, s);
  }
  for (const [kind, n] of [...counts.entries()].sort()) {
    const chip = document.createElement('span');
    chip.className = 'kind-chip kind-' + kind;
    if (hiddenKinds.has(kind)) chip.classList.add('is-off');
    chip.textContent = `${kind} (${n})`;
    chip.title = (hiddenKinds.has(kind) ? 'Click to show. ' : 'Click to hide. ') +
      `Peak |severity| ${(peakSev.get(kind) ?? 0).toFixed(2)}`;
    chip.addEventListener('click', () => {
      if (hiddenKinds.has(kind)) hiddenKinds.delete(kind);
      else hiddenKinds.add(kind);
      renderKindChips();
      applyEventsFilter();
    });
    els.eventsKindChips.appendChild(chip);
  }
}

function applyEventsFilter() {
  const query = (els.eventsSearch?.value ?? '').trim().toLowerCase();
  let shown = 0;
  for (const tr of els.eventsTbody.querySelectorAll('tr')) {
    const kind = tr.dataset.kind;
    const haystack = tr.dataset.searchHaystack ?? '';
    const matchesQuery = !query || haystack.includes(query);
    const matchesKind = !hiddenKinds.has(kind);
    const visible = matchesQuery && matchesKind;
    tr.classList.toggle('is-hidden', !visible);
    if (visible) shown++;
  }
  // Update the status line to reflect filtering
  if (currentEvents.length > 0) {
    const filterApplied = query || hiddenKinds.size > 0;
    if (filterApplied) {
      els.eventsStatus.firstChild.textContent =
        `Showing ${shown} of ${currentEvents.length} event(s).`;
    } else {
      els.eventsStatus.firstChild.textContent =
        `${currentEvents.length} event(s) detected. Tick rows to include them when you click Render.`;
    }
  }
}

function renderSnapshotsList() {
  els.snapshotsList.replaceChildren();
  for (const s of currentSnapshots) {
    const li = document.createElement('li');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.snapshotId = String(s.id);
    cb.checked = true;
    cb.style.marginRight = '0.5rem';
    li.appendChild(cb);
    li.appendChild(document.createTextNode(
      `#${s.id}  ${formatDate(s.tStartMs)}  →  ${formatDate(s.tEndMs)}  ` +
      `mean P=${(s.pTotalMeanW / 1000).toFixed(2)} kW  σ=${s.pTotalStdevW.toFixed(1)} W`
    ));
    els.snapshotsList.appendChild(li);
  }
}

function renderQuantityGrid() {
  els.quantityGrid.replaceChildren();
  for (const q of Object.keys(FULL_QUANTITIES)) {
    const lab = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.quantity = q;
    cb.checked = DEFAULT_QUANTITIES.includes(q);
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(' ' + q));
    els.quantityGrid.appendChild(lab);
  }
}

function selectedQuantities() {
  return [...els.quantityGrid.querySelectorAll('input[type=checkbox]:checked')]
    .map((cb) => cb.dataset.quantity);
}

function selectedEventIds() {
  return new Set(
    [...els.eventsTbody.querySelectorAll('input[type=checkbox]:checked')]
      .map((cb) => Number(cb.dataset.eventId))
  );
}

function selectedSnapshotIds() {
  return new Set(
    [...els.snapshotsList.querySelectorAll('input[type=checkbox]:checked')]
      .map((cb) => Number(cb.dataset.snapshotId))
  );
}

async function renderAll() {
  if (!dataSource()) return;
  const spec = await getSpec();
  const quantities = selectedQuantities();
  if (quantities.length === 0) {
    showError(new Error('Pick at least one quantity to chart.'));
    return;
  }
  const preMs = Math.max(0, Number(els.preSecsInput.value) || 0) * 1000;
  const postMs = Math.max(0, Number(els.postSecsInput.value) || 0) * 1000;
  const evIds = selectedEventIds();
  const snIds = selectedSnapshotIds();

  els.chartsSec.hidden = false;
  els.fullCharts.replaceChildren();
  els.eventCharts.replaceChildren();
  els.snapshotCharts.replaceChildren();

  // Full-session charts — scoped to current range if one is set.
  // In compare mode, render an overlay chart per quantity stacking every session.
  if (ms.compareMode && ms.canCompare()) {
    for (const q of quantities) renderOverlayChart(els.fullCharts, spec, q, quantities);
  } else {
    const fullOpts = {
      eventBands: currentEvents,
      ...(currentRange
        ? { startMs: currentRange.startMs, endMs: currentRange.endMs }
        : {}),
    };
    for (const q of quantities) {
      renderChart(els.fullCharts, dataSource(), spec, q, FULL_QUANTITIES, fullOpts);
    }
  }

  // Event zooms — only quantities that exist in ZOOM_QUANTITIES
  const zoomQuantities = quantities.filter((q) => q in ZOOM_QUANTITIES);
  els.eventChartsHead.hidden = evIds.size === 0;
  for (const ev of currentEvents) {
    if (!evIds.has(ev.id)) continue;
    els.eventCharts.appendChild(makeWindowHeader(
      `Event #${ev.id}`, `${ev.kind} @ ${formatDate(ev.tStartMs)}`,
    ));
    for (const q of zoomQuantities) {
      renderChart(els.eventCharts, dataSource(), spec, q, ZOOM_QUANTITIES, {
        startMs: ev.tStartMs - preMs,
        endMs: ev.tEndMs + postMs,
      });
    }
  }

  tabState.hasCharts = true;
  // Snapshot zooms
  els.snapshotChartsHead.hidden = snIds.size === 0;
  for (const s of currentSnapshots) {
    if (!snIds.has(s.id)) continue;
    els.snapshotCharts.appendChild(makeWindowHeader(
      `Snapshot #${s.id}`, `@ ${formatDate(s.tStartMs)}`,
    ));
    for (const q of zoomQuantities) {
      renderChart(els.snapshotCharts, dataSource(), spec, q, ZOOM_QUANTITIES, {
        startMs: s.tStartMs,
        endMs: s.tEndMs,
      });
    }
  }
}

function renderOverlayChart(parentEl, spec, quantityKey, _allQuantities) {
  // Build a compare-overlay chart: each session's series for `quantityKey`,
  // x-axis = seconds-from-each-session-start so non-overlapping timestamps
  // can still be visually compared.
  const uPlot = window.uPlot;
  const def = FULL_QUANTITIES[quantityKey];
  if (!def || !uPlot) return;
  const all = ms.getAll();
  if (all.length === 0) return;
  // We only use the FIRST series of each quantity for overlay (keeps the
  // chart readable; FULL_QUANTITIES often has 3 phases — we'd otherwise
  // overlay 9+ lines for 3 sessions × 3 phases).
  const firstCol = def.series[0];
  const accessors = all.map((s) => sessionAccessor(s, spec, firstCol.name));

  const ySeries = all.map(() => []);
  // Build a unified sorted x axis from the union of all sessions' rel-seconds.
  const xSet = new Set();
  for (const acc of accessors) {
    for (let i = 0; i < acc.n; i++) {
      xSet.add(Math.round((acc.startMs(i) - acc.base) / 1000));
    }
  }
  const xsAll = [...xSet].sort((a, b) => a - b);
  // Per-session lookup: relSec → value
  for (let si = 0; si < all.length; si++) {
    const acc = accessors[si];
    const map = new Map();
    for (let i = 0; i < acc.n; i++) {
      const rel = Math.round((acc.startMs(i) - acc.base) / 1000);
      map.set(rel, acc.value(i) * firstCol.scale);
    }
    for (const x of xsAll) ySeries[si].push(map.has(x) ? map.get(x) : null);
  }

  // Wrap in a chart card with a basic toolbar.
  const wrapper = document.createElement('article');
  wrapper.className = 'chart-wrapper';
  const header = document.createElement('header');
  header.className = 'chart-header';
  const titleEl = document.createElement('h3');
  titleEl.textContent = def.title + ' — overlay (' + all.length + ' sessions)';
  header.appendChild(titleEl);
  wrapper.appendChild(header);
  const chartDiv = document.createElement('div');
  chartDiv.className = 'chart-canvas';
  wrapper.appendChild(chartDiv);
  parentEl.appendChild(wrapper);

  const series = [
    { label: 'sec from session start' },
    ...all.map((s) => ({ label: s.label, stroke: s.color, width: 1.4 })),
  ];
  const data = [xsAll, ...ySeries];
  const plot = new uPlot({
    width: chartDiv.clientWidth || 900,
    height: 280,
    series,
    scales: { x: { time: false } },
    axes: [{ stroke: '#666', label: 'Seconds' }, { stroke: '#666', label: def.ylabel }],
    cursor: { drag: { x: true, y: false, setScale: true }, focus: { prox: 30 } },
    legend: { live: true },
  }, data, chartDiv);
  const resizeObs = new ResizeObserver(() => {
    plot.setSize({ width: chartDiv.clientWidth, height: 280 });
  });
  resizeObs.observe(chartDiv);
}

function makeWindowHeader(boldText, plainText) {
  const p = document.createElement('p');
  const strong = document.createElement('strong');
  strong.textContent = boldText;
  p.appendChild(strong);
  p.appendChild(document.createTextNode(' ' + plainText));
  return p;
}

// --- Wire up events ---------------------------------------------------------

els.dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  els.dropZone.classList.add('is-dragging');
});
els.dropZone.addEventListener('dragleave', () => {
  els.dropZone.classList.remove('is-dragging');
});
els.dropZone.addEventListener('drop', async (e) => {
  e.preventDefault();
  els.dropZone.classList.remove('is-dragging');
  const dt = e.dataTransfer;
  if (!dt) return;
  // Prefer DataTransferItemList (handles directories via webkitGetAsEntry)
  if (dt.items && dt.items.length > 0 && dt.items[0].webkitGetAsEntry) {
    const files = await collectFiles(dt.items);
    await handleFiles(files);
  } else {
    await handleFiles(dt.files);
  }
});

async function collectFiles(items) {
  const all = [];
  const promises = [];
  for (const item of items) {
    const entry = item.webkitGetAsEntry?.();
    if (entry) {
      promises.push(walkEntry(entry, all));
    } else if (item.kind === 'file') {
      const f = item.getAsFile();
      if (f) all.push(f);
    }
  }
  await Promise.all(promises);
  return all;
}

function walkEntry(entry, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((f) => { out.push(f); resolve(); }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readBatch = () => {
        reader.readEntries((batch) => {
          if (batch.length === 0) {
            resolve();
            return;
          }
          Promise.all(batch.map((sub) => walkEntry(sub, out))).then(readBatch);
        }, () => resolve());
      };
      readBatch();
    } else {
      resolve();
    }
  });
}

els.fileInput.addEventListener('change', (e) => handleFiles(e.target.files));
els.dirInput.addEventListener('change', (e) => handleFiles(e.target.files));
for (const cb of [els.reverseA, els.reverseB, els.reverseC]) {
  cb?.addEventListener('change', () => {
    // Re-parse the cached buffer whenever the phase selection changes.
    if (currentArrayBuffer) parseBuffer();
    else renderSummary();
  });
}
document.getElementById('ct-reversal-apply')?.addEventListener('click', () => {
  if (els.reverseA) els.reverseA.checked = true;
  if (els.reverseB) els.reverseB.checked = true;
  if (els.reverseC) els.reverseC.checked = true;
  document.getElementById('ct-reversal-banner').hidden = true;
  if (currentFile) parseStreaming(currentFile).catch(showError);
  else if (currentArrayBuffer) parseBuffer();
});
els.resetBtn.addEventListener('click', resetUi);
els.eventsExportNotes?.addEventListener('click', () => {
  const json = exportNotesJson(currentFileHash, currentEvents);
  const blob = new Blob([json], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `notes_${(currentConfig?.asset_name || 'session').replace(/[^a-zA-Z0-9._-]+/g, '_')}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
});

els.tariffApplyBtn.addEventListener('click', async () => {
  const t = getTariffFromForm();
  saveTariff(activeAssetName(), t);
  const amps = Number(els.breakerRating.value) || 0;
  saveBreakerRating(amps);
  // Re-run insights with the new breaker context.
  if (dataSource()) {
    const spec = await getSpec();
    currentFindings = analyzeInsights(dataSource(), currentEvents, spec,
                                      currentSnapshots, currentConfig,
                                      { breakerRatingA: amps });
    renderInsights();
    els.insightsSec.hidden = currentFindings.length === 0;
  }
  renderTariffResult().catch(showError);
});

els.addSessionInput.addEventListener('change', (e) => {
  if (e.target.files?.length) handleFiles(e.target.files);
  e.target.value = '';
});
els.compareToggleBtn.addEventListener('click', async () => {
  ms.setCompareMode(!ms.compareMode);
  renderSessionsBar();
  // Insights swap between single-session and cross-session views.
  if (ms.compareMode) {
    const spec = await getSpec();
    currentFindings = analyzeCompareInsights(ms.getAll(), spec);
    renderInsights();
    els.insightsSec.hidden = currentFindings.length === 0;
  } else {
    const active = ms.getActive();
    if (active) {
      currentFindings = active.findings;
      renderInsights();
      els.insightsSec.hidden = currentFindings.length === 0;
    }
  }
  renderAll().catch(showError);
});
els.eventsSearch?.addEventListener('input', applyEventsFilter);
els.eventsClearFilters?.addEventListener('click', () => {
  els.eventsSearch.value = '';
  hiddenKinds.clear();
  renderKindChips();
  applyEventsFilter();
});
els.renderBtn.addEventListener('click', () => renderAll().catch(showError));
els.exportXlsxBtn.addEventListener('click', () => exportXlsx().catch(showError));
els.exportHtmlBtn.addEventListener('click', () => exportHtmlReport().catch(showError));
els.exportPdfBtn.addEventListener('click', () => exportPdf().catch(showError));
els.exportBundleBtn.addEventListener('click', () => exportBundle().catch(showError));

// --- Theme (light / dark / auto) -------------------------------------------

const THEME_STORAGE_KEY = 'fluke3540.theme';

function applyTheme(theme) {
  if (theme === 'auto') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', theme);
  }
}

function loadTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY) ?? 'auto';
  applyTheme(saved);
  const radio = document.getElementById('theme-' + saved);
  if (radio) radio.checked = true;
}

document.querySelectorAll('input[name=theme]').forEach((r) => {
  r.addEventListener('change', () => {
    const v = r.value;
    localStorage.setItem(THEME_STORAGE_KEY, v);
    applyTheme(v);
  });
});
loadTheme();

// --- Report timezone (Feature H) -------------------------------------------
(function initTz() {
  const input = document.getElementById('tz-input');
  if (!input) return;
  const saved = localStorage.getItem(TZ_STORAGE_KEY) || '';
  input.value = saved;
  currentTz = saved || null;
  input.addEventListener('change', () => {
    currentTz = input.value.trim() || null;
    if (currentTz) localStorage.setItem(TZ_STORAGE_KEY, currentTz);
    else localStorage.removeItem(TZ_STORAGE_KEY);
    renderTzRange();
  });
})();

// --- Keyboard shortcuts ----------------------------------------------------

document.addEventListener('keydown', (e) => {
  // Ignore when typing in inputs (so search, etc. still work normally)
  const tag = e.target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  switch (e.key) {
    case 'r':
    case 'R':
      if (dataSource()) { e.preventDefault(); renderAll().catch(showError); }
      break;
    case 'z':
    case 'Z': {
      e.preventDefault();
      for (const w of document.querySelectorAll('article.chart-wrapper')) {
        w.querySelector('.chart-toolbar button')?.click();  // first toolbar btn = Reset zoom
      }
      break;
    }
    case 'ArrowLeft':
    case 'ArrowRight':
      navigateEvents(e.key === 'ArrowRight' ? 1 : -1);
      break;
    case 'Escape':
      if (els.eventsSearch?.value) {
        e.preventDefault();
        els.eventsSearch.value = '';
        hiddenKinds.clear();
        renderKindChips();
        applyEventsFilter();
      } else if (currentRange) {
        e.preventDefault();
        rangeSelector?.setRange(null);
        currentRange = null;
        history.replaceState(null, '', location.pathname + location.search);
      }
      break;
    case '?':
      e.preventDefault();
      showShortcutsHelp();
      break;
  }
});

function navigateEvents(direction) {
  if (currentEvents.length === 0) return;
  const sorted = [...currentEvents].sort((a, b) => a.tStartMs - b.tStartMs);
  const currentIdx = sorted.findIndex((ev) =>
    els.eventsTbody?.querySelector(`input[data-event-id="${ev.id}"]`)?.checked
  );
  const nextIdx = currentIdx < 0
    ? (direction > 0 ? 0 : sorted.length - 1)
    : (currentIdx + direction + sorted.length) % sorted.length;
  snapRangeToEvent(sorted[nextIdx]);
}

function showShortcutsHelp() {
  const existing = document.getElementById('shortcuts-overlay');
  if (existing) { existing.remove(); return; }
  const overlay = document.createElement('div');
  overlay.id = 'shortcuts-overlay';
  overlay.innerHTML = '';  // safe — only sets empty
  const card = document.createElement('article');
  const h = document.createElement('h3');
  h.textContent = 'Keyboard shortcuts';
  card.appendChild(h);
  const kbds = [
    ['R', 'Re-render charts'],
    ['Z', 'Reset zoom on every chart'],
    ['← / →', 'Snap range to previous / next event'],
    ['Esc', 'Clear filters or range'],
    ['?', 'Toggle this help'],
  ];
  const dl = document.createElement('dl');
  dl.className = 'summary-grid';
  for (const [k, d] of kbds) {
    const dt = document.createElement('dt');
    const code = document.createElement('kbd');
    code.textContent = k;
    dt.appendChild(code);
    const dd = document.createElement('dd');
    dd.textContent = d;
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
  card.appendChild(dl);
  const close = document.createElement('button');
  close.textContent = 'Close';
  close.className = 'secondary';
  close.addEventListener('click', () => overlay.remove());
  card.appendChild(close);
  overlay.appendChild(card);
  document.body.appendChild(overlay);
}

// --- Tabs (3-step flow) ----------------------------------------------------

const tabState = {
  hasSession: false,    // unlocks Explore + Export tabs
  hasCharts:  false,    // shows the "Render first" hint on Export
  current:    'import',
};

function activateTab(name) {
  for (const btn of document.querySelectorAll('.tab-btn')) {
    const isActive = btn.dataset.tab === name;
    btn.classList.toggle('is-active', isActive);
    btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
  }
  for (const pane of document.querySelectorAll('.tab-pane')) {
    pane.hidden = pane.dataset.tabPane !== name;
  }
  tabState.current = name;
  if (name === 'export') refreshExportTab();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function updateTabUnlocks() {
  for (const btn of document.querySelectorAll('.tab-btn')) {
    if (btn.dataset.tab === 'import') continue;  // Import always available
    btn.disabled = !tabState.hasSession;
  }
  document.getElementById('step-nav-import').hidden = !tabState.hasSession;
  document.getElementById('step-nav-explore').hidden = !tabState.hasSession;
  document.getElementById('step-nav-export').hidden = !tabState.hasSession;
}

function refreshExportTab() {
  const hint = document.getElementById('export-hint');
  const scope = document.getElementById('export-scope-hint');
  hint.hidden = tabState.hasCharts;
  if (currentRange) {
    scope.firstChild.textContent =
      `Range active: ${new Date(currentRange.startMs).toISOString().slice(11, 19)}` +
      `–${new Date(currentRange.endMs).toISOString().slice(11, 19)}. ` +
      `Exports below will be scoped to this range.`;
  } else if (ms.compareMode && ms.canCompare()) {
    scope.firstChild.textContent =
      `Compare mode (${ms.count()} sessions). HTML / PDF exports will use the multi-session template.`;
  } else {
    scope.firstChild.textContent =
      `Whole-session export (no range or compare mode active).`;
  }
}

// Wire up tab navigation
for (const btn of document.querySelectorAll('.tab-btn')) {
  btn.addEventListener('click', () => {
    if (btn.disabled) return;
    activateTab(btn.dataset.tab);
  });
}
for (const btn of document.querySelectorAll('[data-goto-tab]')) {
  btn.addEventListener('click', () => activateTab(btn.dataset.gotoTab));
}

// "Clear cache" link in the footer
document.getElementById('clear-cache-link')?.addEventListener('click', async (e) => {
  e.preventDefault();
  const ok = await clearCache();
  e.target.textContent = ok ? 'Cache cleared' : 'Cache clear failed';
  setTimeout(() => { e.target.textContent = 'Clear cache'; }, 2000);
});

// PWA service worker registration — skipped on localhost to avoid
// stale-cache confusion during dev.
if ('serviceWorker' in navigator
    && !['localhost', '127.0.0.1'].includes(location.hostname)) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('service_worker.js')
      .catch((e) => console.warn('SW registration failed:', e));
  });
}

// Prefetch the spec so first-drop is snappy.
getSpec().catch(() => {/* surface only when actually parsing */});
