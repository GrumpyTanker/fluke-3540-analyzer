// Main orchestration: drop-zone handling, spec/file loading, worker dispatch,
// summary rendering. Pure ESM, no framework.

// Try sibling first (e.g. when the Pages deploy flattens spec/ next to app.js),
// then fall back to ../spec/ for serving from the repo root.
const SPEC_CANDIDATES = [
  new URL('spec/field_map.json', import.meta.url).href,
  new URL('../spec/field_map.json', import.meta.url).href,
];

const els = {
  dropZone:       document.getElementById('drop-zone'),
  fileInput:      document.getElementById('file-input'),
  dirInput:       document.getElementById('dir-input'),
  progressSec:    document.getElementById('progress-section'),
  progressBar:    document.getElementById('progress-bar'),
  progressLabel:  document.getElementById('progress-label'),
  summarySec:     document.getElementById('summary-section'),
  summaryGrid:    document.getElementById('summary-grid'),
  reverseToggle:  document.getElementById('reverse-cts-toggle'),
  errorSec:       document.getElementById('error-section'),
  errorMsg:       document.getElementById('error-message'),
  resetBtn:       document.getElementById('reset-button'),
};

let cachedSpec = null;
let currentArrayBuffer = null;
let currentConfig = null;     // parsed ES.NNN-config.json companion (or null)
let currentRecordCount = 0;
let currentTimeRangeMs = null;
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

function onParseDone(msg) {
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
  els.fileInput.value = '';
  els.dirInput.value = '';
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

// Prefetch the spec so first-drop is snappy.
getSpec().catch(() => {/* surface only when actually parsing */});
