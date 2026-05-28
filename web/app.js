// Main orchestration: drop-zone handling, spec/file loading, worker dispatch,
// summary rendering. Pure ESM, no framework.

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
  reverseToggle:      document.getElementById('reverse-cts-toggle'),
  errorSec:           document.getElementById('error-section'),
  errorMsg:           document.getElementById('error-message'),
  resetBtn:           document.getElementById('reset-button'),
  eventsSec:          document.getElementById('events-section'),
  eventsStatus:       document.getElementById('events-status'),
  eventsTbody:        document.getElementById('events-tbody'),
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
};

let cachedSpec = null;
let currentArrayBuffer = null;
let currentConfig = null;     // parsed ES.NNN-config.json companion (or null)
let currentRecords = null;    // full parsed Records array (kept in memory)
let currentRecordCount = 0;
let currentTimeRangeMs = null;
let currentEvents = [];       // detected events for currentRecords
let currentSnapshots = [];    // picked snapshots for currentRecords
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
  // If a directory was dropped, we get many files. Find trend.bin and *-config.json
  let trendFile = null;
  let configFile = null;
  for (const f of fileList) {
    const name = f.name.toLowerCase();
    if (name === 'trend.bin') trendFile = f;
    else if (name.endsWith('-config.json')) configFile = f;
  }
  // Otherwise (single-file drop) accept the first .bin
  if (!trendFile) {
    trendFile = findInFileList(fileList, (f) => f.name.toLowerCase().endsWith('.bin'));
  }
  if (!trendFile) {
    showError(new Error('Drop a trend.bin or a session folder containing one.'));
    return;
  }

  currentConfig = null;
  if (configFile) {
    try {
      const text = await configFile.text();
      currentConfig = JSON.parse(text);
    } catch (e) {
      // non-fatal — we just won't have asset name
      console.warn('Could not parse config JSON:', e);
    }
  }

  await parseFile(trendFile);
}

async function parseFile(file) {
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;
  setProgress(0, file.size, 'reading file');
  currentArrayBuffer = await file.arrayBuffer();
  await parseBuffer();
}

async function parseBuffer() {
  if (!currentArrayBuffer) return;
  hideError();
  els.summarySec.hidden = true;
  els.progressSec.hidden = false;
  setProgress(0, 100, 'parsing');

  const spec = await getSpec();
  if (currentWorker) currentWorker.terminate();
  currentWorker = new Worker(new URL('./parser_worker.js', import.meta.url),
                             { type: 'module' });
  currentWorker.onmessage = (event) => {
    const msg = event.data;
    if (msg.type === 'progress') {
      setProgress(msg.done, msg.total, `parsing record ${msg.done.toLocaleString()} / ${msg.total.toLocaleString()}`);
    } else if (msg.type === 'done') {
      onParseDone(msg);
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
    reverseCts: els.reverseToggle.checked,
  });
}

async function onParseDone(msg) {
  currentRecords = msg.records;
  currentRecordCount = msg.recordCount;
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
    renderEventsTable();
    renderSnapshotsList();
    renderQuantityGrid();
    els.eventsSec.hidden = false;
    els.snapshotsSec.hidden = currentSnapshots.length === 0;
    els.controlsSec.hidden = false;
  } catch (e) {
    showError(e);
    return;
  } finally {
    els.progressSec.hidden = true;
  }
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
  add('Reverse CTs',   els.reverseToggle.checked ? 'on' : 'off');

  els.summaryGrid.replaceWith(dl);
  dl.id = 'summary-grid';
  els.summaryGrid = dl;
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
  els.eventsSec.hidden = true;
  els.snapshotsSec.hidden = true;
  els.controlsSec.hidden = true;
  els.chartsSec.hidden = true;
  els.fileInput.value = '';
  els.dirInput.value = '';
}

// --- Events + snapshots + controls rendering --------------------------------

function formatDate(ms) {
  return new Date(ms).toISOString();
}

function renderEventsTable() {
  els.eventsStatus.firstChild.textContent =
    currentEvents.length === 0
      ? 'No events detected.'
      : `${currentEvents.length} event(s) detected. Tick rows to include them when you click Render.`;
  els.eventsTbody.replaceChildren();
  for (const ev of currentEvents) {
    const tr = document.createElement('tr');
    tr.className = `kind-${ev.kind}`;
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
    els.eventsTbody.appendChild(tr);
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
  if (!currentRecords) return;
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

  // Full-session charts
  for (const q of quantities) {
    renderChart(els.fullCharts, currentRecords, spec, q, FULL_QUANTITIES);
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
      renderChart(els.eventCharts, currentRecords, spec, q, ZOOM_QUANTITIES, {
        startMs: ev.tStartMs - preMs,
        endMs: ev.tEndMs + postMs,
      });
    }
  }

  // Snapshot zooms
  els.snapshotChartsHead.hidden = snIds.size === 0;
  for (const s of currentSnapshots) {
    if (!snIds.has(s.id)) continue;
    els.snapshotCharts.appendChild(makeWindowHeader(
      `Snapshot #${s.id}`, `@ ${formatDate(s.tStartMs)}`,
    ));
    for (const q of zoomQuantities) {
      renderChart(els.snapshotCharts, currentRecords, spec, q, ZOOM_QUANTITIES, {
        startMs: s.tStartMs,
        endMs: s.tEndMs,
      });
    }
  }
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
els.reverseToggle.addEventListener('change', () => {
  // Re-parse using the cached ArrayBuffer when the user flips the toggle.
  if (currentArrayBuffer) {
    parseBuffer();
  } else {
    renderSummary();
  }
});
els.resetBtn.addEventListener('click', resetUi);
els.renderBtn.addEventListener('click', () => renderAll().catch(showError));

// Prefetch the spec so first-drop is snappy.
getSpec().catch(() => {/* surface only when actually parsing */});
