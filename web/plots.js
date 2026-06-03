// uPlot wrappers — turn parsed Records into time-series charts.
//
// Loads uPlot from window.uPlot (vendor/uPlot.iife.min.js, expected to be
// loaded by index.html before this module).

const HAS_UPLOT = typeof window !== 'undefined' && typeof window.uPlot !== 'undefined';
function uplotOrThrow() {
  if (!HAS_UPLOT) {
    throw new Error('uPlot global not found — ensure vendor/uPlot.iife.min.js is loaded');
  }
  return window.uPlot;
}

const COLORS = ['#cc0000', '#0066cc', '#009933', '#660066'];

// Semi-transparent fill colors for the anomaly overlay (full-session charts only).
const EVENT_BAND_COLORS = {
  outage:          'rgba(204, 0, 0, 0.18)',
  dip:             'rgba(255, 153, 0, 0.14)',
  swell:           'rgba(255, 200, 0, 0.14)',
  high_current:    'rgba(204, 0, 204, 0.14)',
  freq_excursion:  'rgba(102, 0, 102, 0.14)',
  imbalance_spike: 'rgba(0, 102, 204, 0.12)',
  power_step:      'rgba(0, 153, 51, 0.12)',
};

// Quantity definitions for the full-session view.
// Each: { title, ylabel, series: [{name, label, scale}] }
export const FULL_QUANTITIES = {
  power: {
    title: 'Active Power (P_total) — negative = exporting',
    ylabel: 'P (kW)',
    series: [{ name: 'P_total_avg_W', label: 'P_total', scale: 1e-3 }],
  },
  voltage: {
    title: 'Per-phase L-N Voltage',
    ylabel: 'V (V)',
    series: [
      { name: 'V_LN_a_avg_V', label: 'V_LN,a', scale: 1 },
      { name: 'V_LN_b_avg_V', label: 'V_LN,b', scale: 1 },
      { name: 'V_LN_c_avg_V', label: 'V_LN,c', scale: 1 },
    ],
  },
  current: {
    title: 'Per-phase Current',
    ylabel: 'I (A)',
    series: [
      { name: 'I_a_avg_A', label: 'I_a', scale: 1 },
      { name: 'I_b_avg_A', label: 'I_b', scale: 1 },
      { name: 'I_c_avg_A', label: 'I_c', scale: 1 },
    ],
  },
  pf: {
    title: 'Power Factor (true / displacement)',
    ylabel: 'PF / DPF (-1 to +1)',
    series: [
      { name: 'PF_total_avg', label: 'PF', scale: 1 },
      { name: 'DPF_total_avg', label: 'DPF', scale: 1 },
    ],
  },
  frequency: {
    title: 'Line Frequency',
    ylabel: 'Frequency (Hz)',
    series: [{ name: 'freq_avg_Hz', label: 'f', scale: 1 }],
  },
  thd: {
    title: 'Voltage THD per phase',
    ylabel: 'V_THD (%)',
    series: [
      { name: 'V_THD_pct_a_avg', label: 'V_THD,a', scale: 1 },
      { name: 'V_THD_pct_b_avg', label: 'V_THD,b', scale: 1 },
      { name: 'V_THD_pct_c_avg', label: 'V_THD,c', scale: 1 },
    ],
  },
  thdi: {
    title: 'Current THD per phase',
    ylabel: 'I_THD (%)',
    series: [
      { name: 'I_THD_pct_a_avg', label: 'I_THD,a', scale: 1 },
      { name: 'I_THD_pct_b_avg', label: 'I_THD,b', scale: 1 },
      { name: 'I_THD_pct_c_avg', label: 'I_THD,c', scale: 1 },
    ],
  },
};

// Zoom uses min/max columns where appropriate (catches sub-second behavior).
export const ZOOM_QUANTITIES = {
  voltage: {
    title: 'V_LN window MIN per phase',
    ylabel: 'V (V)',
    series: [
      { name: 'V_LN_a_min_V', label: 'V_LN,a min', scale: 1 },
      { name: 'V_LN_b_min_V', label: 'V_LN,b min', scale: 1 },
      { name: 'V_LN_c_min_V', label: 'V_LN,c min', scale: 1 },
    ],
  },
  current: {
    title: 'I window MAX per phase',
    ylabel: 'I peak (A)',
    series: [
      { name: 'I_a_max_A', label: 'I_a max', scale: 1 },
      { name: 'I_b_max_A', label: 'I_b max', scale: 1 },
      { name: 'I_c_max_A', label: 'I_c max', scale: 1 },
    ],
  },
  power: {
    title: 'Active Power',
    ylabel: 'P (kW)',
    series: [{ name: 'P_total_avg_W', label: 'P_total', scale: 1e-3 }],
  },
};

function fieldIndexMap(spec) {
  const m = new Map();
  for (const f of spec.fields) m.set(f.name, f.index);
  return m;
}

// uPlot degrades badly well before a week of per-second points (~590 K/series).
// When the filtered series exceeds this, we min/max-decimate to ~plot-width
// buckets for rendering while keeping the full data for CSV/stats.
export const DECIMATE_THRESHOLD = 12000;

/**
 * Min/max-decimate a [xs, ...ys] uPlot dataset to ~targetPoints buckets.
 *
 * Each bucket emits TWO x-samples (the timestamps of the per-bucket min and max
 * of the FIRST y-series, in chronological order) so that both the lowest dip
 * and the highest spike in every bucket survive on every series. This preserves
 * outage/dip/spike visibility that naive stride-sampling would drop.
 *
 * Returns the original dataset unchanged if it already has <= targetPoints
 * x-samples. Pure function — no DOM, unit-testable under node --test.
 *
 * @param {Array<Array<number>>} data - [xs, y0, y1, ...]
 * @param {number} targetPoints - desired output x-sample count (approx)
 * @returns {{data: Array<Array<number>>, decimated: boolean, originalPoints: number}}
 */
export function decimateSeries(data, targetPoints = 4000) {
  const xs = data[0];
  const n = xs.length;
  const nSeries = data.length - 1;
  if (n <= targetPoints || targetPoints < 2 || nSeries < 1) {
    return { data, decimated: false, originalPoints: n };
  }
  const buckets = Math.max(1, Math.floor(targetPoints / 2));
  const bucketSize = n / buckets;
  const outXs = [];
  const outYs = Array.from({ length: nSeries }, () => []);
  const ref = data[1]; // decide min/max ordering by the first series

  for (let b = 0; b < buckets; b++) {
    const lo = Math.floor(b * bucketSize);
    const hi = Math.min(n, Math.floor((b + 1) * bucketSize));
    if (hi <= lo) continue;
    let minI = lo;
    let maxI = lo;
    for (let i = lo + 1; i < hi; i++) {
      const v = ref[i];
      if (v < ref[minI]) minI = i;
      if (v > ref[maxI]) maxI = i;
    }
    // Emit in chronological order so the line doesn't zig-zag in time.
    const [firstI, secondI] = minI <= maxI ? [minI, maxI] : [maxI, minI];
    for (const i of (firstI === secondI ? [firstI] : [firstI, secondI])) {
      outXs.push(xs[i]);
      for (let k = 0; k < nSeries; k++) outYs[k].push(data[k + 1][i]);
    }
  }
  return {
    data: [outXs, ...outYs],
    decimated: true,
    originalPoints: n,
  };
}

/**
 * Build uPlot data arrays from a set of records and a quantity definition.
 * Returns [xs, ...ySeries] suitable for uPlot's data prop.
 */
function buildPlotData(records, spec, quantityDef, startMs, endMs) {
  const fi = fieldIndexMap(spec);
  const xs = [];
  const ys = quantityDef.series.map(() => []);
  for (const rec of records) {
    if (startMs !== null && rec.startMs < startMs) continue;
    if (endMs !== null && rec.startMs > endMs) continue;
    xs.push(rec.startMs / 1000);  // uPlot expects unix seconds
    for (let k = 0; k < quantityDef.series.length; k++) {
      const idx = fi.get(quantityDef.series[k].name);
      ys[k].push(rec.floats[idx] * quantityDef.series[k].scale);
    }
  }
  return [xs, ...ys];
}

/**
 * Render one uPlot chart into a container.
 * @returns {{plot: uPlot, container: HTMLDivElement, data: any[], def: object}}
 */
export function renderChart(parentEl, records, spec, quantityKey, quantityMap = FULL_QUANTITIES, { startMs = null, endMs = null, width = null, eventBands = null } = {}) {
  const uPlot = uplotOrThrow();
  const def = quantityMap[quantityKey];
  if (!def) throw new Error(`unknown quantity: ${quantityKey}`);
  const fullData = buildPlotData(records, spec, def, startMs, endMs);
  // Decimate for rendering only; CSV export keeps fullData.
  const plotWidthGuess = width || (parentEl && parentEl.clientWidth) || 900;
  const { data, decimated, originalPoints } = decimateSeries(
    fullData, Math.max(2000, plotWidthGuess * 3));

  // Wrap chart in a card with a toolbar
  const wrapper = document.createElement('article');
  wrapper.className = 'chart-wrapper';
  const header = document.createElement('header');
  header.className = 'chart-header';
  const titleEl = document.createElement('h3');
  titleEl.textContent = def.title;
  header.appendChild(titleEl);
  if (decimated) {
    const notice = document.createElement('span');
    notice.className = 'decimate-notice';
    notice.textContent =
      `large session — chart decimated (${originalPoints.toLocaleString()} → ` +
      `${data[0].length.toLocaleString()} pts; CSV keeps full data)`;
    notice.title =
      'Min/max decimation preserves spikes and dips. Downloaded CSV is full resolution.';
    header.appendChild(notice);
  }
  const toolbar = document.createElement('div');
  toolbar.className = 'chart-toolbar';
  const dlPng = document.createElement('button');
  dlPng.className = 'secondary outline';
  dlPng.type = 'button';
  dlPng.textContent = 'PNG';
  const dlCsv = document.createElement('button');
  dlCsv.className = 'secondary outline';
  dlCsv.type = 'button';
  dlCsv.textContent = 'CSV';
  const resetBtn = document.createElement('button');
  resetBtn.className = 'secondary outline';
  resetBtn.type = 'button';
  resetBtn.textContent = 'Reset zoom';
  resetBtn.title = 'Drag to box-zoom, scroll to zoom around cursor, Shift+drag to pan';
  toolbar.appendChild(resetBtn);
  toolbar.appendChild(dlPng);
  toolbar.appendChild(dlCsv);
  header.appendChild(toolbar);
  wrapper.appendChild(header);
  const chartDiv = document.createElement('div');
  chartDiv.className = 'chart-canvas';
  wrapper.appendChild(chartDiv);
  parentEl.appendChild(wrapper);

  const series = [
    { label: 'time' },
    ...def.series.map((s, i) => ({
      label: s.label,
      stroke: COLORS[i % COLORS.length],
      width: 1.4,
    })),
  ];

  const opts = {
    width: width || chartDiv.clientWidth || 900,
    height: 280,
    series,
    scales: { x: { time: true } },
    axes: [
      { stroke: '#666' },
      { stroke: '#666', label: def.ylabel },
    ],
    cursor: { drag: { x: true, y: false, setScale: true }, focus: { prox: 30 } },
    legend: { live: true },
    hooks: eventBands && eventBands.length ? {
      drawClear: [(u) => drawEventBands(u, eventBands)],
    } : {},
  };
  const plot = new uPlot(opts, data, chartDiv);

  // Re-fit width on container resize
  const resizeObs = new ResizeObserver(() => {
    plot.setSize({ width: chartDiv.clientWidth, height: 280 });
  });
  resizeObs.observe(chartDiv);

  attachZoomOnScroll(plot, chartDiv);
  attachShiftDragPan(plot, chartDiv);
  if (eventBands && eventBands.length) {
    attachBandTooltips(plot, chartDiv, eventBands);
  }

  // Toolbar handlers
  dlPng.addEventListener('click', () => downloadChartPng(plot, def.title));
  dlCsv.addEventListener('click', () => downloadChartCsv(fullData, def, def.title));
  resetBtn.addEventListener('click', () => {
    plot.batch(() => {
      plot.setScale('x', { min: null, max: null });
      // y autoscale follows when x changes
    });
  });

  return { plot, container: wrapper, data, fullData, decimated, def };
}

function attachBandTooltips(plot, chartDiv, eventBands) {
  let tip = null;
  function showTip(ev, text) {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'anomaly-tooltip';
      chartDiv.appendChild(tip);
    }
    tip.textContent = text;
    tip.style.left = (ev.offsetX + 12) + 'px';
    tip.style.top = (ev.offsetY + 12) + 'px';
    tip.hidden = false;
  }
  function hideTip() { if (tip) tip.hidden = true; }
  chartDiv.addEventListener('pointermove', (e) => {
    if (!plot.over) return;
    const rect = plot.over.getBoundingClientRect();
    const px = e.clientX - rect.left;
    if (px < 0 || px > rect.width) { hideTip(); return; }
    const tSec = plot.posToVal(px, 'x');
    if (!Number.isFinite(tSec)) { hideTip(); return; }
    const tMs = tSec * 1000;
    const hits = eventBands.filter((b) => tMs >= b.tStartMs && tMs <= b.tEndMs);
    if (hits.length === 0) { hideTip(); return; }
    const ev = hits[0];
    const dur = Math.max(1, Math.round((ev.tEndMs - ev.tStartMs) / 1000));
    showTip(e, `${ev.kind} #${ev.id} · ${dur}s · sev ${ev.severity.toFixed(3)}`);
  });
  chartDiv.addEventListener('pointerleave', hideTip);
}

function drawEventBands(u, eventBands) {
  const ctx = u.ctx;
  const { top, height } = u.bbox;
  ctx.save();
  for (const ev of eventBands) {
    const color = EVENT_BAND_COLORS[ev.kind];
    if (!color) continue;
    const xStartSec = ev.tStartMs / 1000;
    const xEndSec = ev.tEndMs / 1000;
    const xs = u.scales.x;
    // Cull bands outside the current view
    if (xs.min != null && xEndSec < xs.min) continue;
    if (xs.max != null && xStartSec > xs.max) continue;
    const xs0 = u.valToPos(xStartSec, 'x', true);
    const xs1 = u.valToPos(xEndSec, 'x', true);
    const w = Math.max(2, xs1 - xs0);  // give pinpoint events at least 2px width
    ctx.fillStyle = color;
    ctx.fillRect(xs0, top, w, height);
  }
  ctx.restore();
}

function attachZoomOnScroll(plot, chartDiv) {
  chartDiv.addEventListener('wheel', (e) => {
    if (!e.deltaY) return;
    // Hold Shift+wheel as the chartjs-style "pan" gesture — defer to native scroll.
    if (e.shiftKey) return;
    e.preventDefault();
    const rect = plot.over.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const cursorX = plot.posToVal(px, 'x');
    const xScale = plot.scales.x;
    if (xScale.min == null || xScale.max == null) return;
    const factor = e.deltaY < 0 ? 0.8 : 1.25;  // wheel up = zoom in
    const newMin = cursorX - (cursorX - xScale.min) * factor;
    const newMax = cursorX + (xScale.max - cursorX) * factor;
    plot.setScale('x', { min: newMin, max: newMax });
  }, { passive: false });
}

function attachShiftDragPan(plot, chartDiv) {
  let dragging = false;
  let startX = 0;
  let startMin = 0;
  let startMax = 0;
  chartDiv.addEventListener('pointerdown', (e) => {
    if (!e.shiftKey) return;
    dragging = true;
    startX = e.clientX;
    const xScale = plot.scales.x;
    startMin = xScale.min;
    startMax = xScale.max;
    chartDiv.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  chartDiv.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const rect = plot.over.getBoundingClientRect();
    const pxRange = rect.width;
    const dataRange = startMax - startMin;
    const dxPx = e.clientX - startX;
    const dxVal = -(dxPx / pxRange) * dataRange;
    plot.setScale('x', { min: startMin + dxVal, max: startMax + dxVal });
  });
  const release = (e) => {
    if (!dragging) return;
    dragging = false;
    try { chartDiv.releasePointerCapture(e.pointerId); } catch (_) {}
  };
  chartDiv.addEventListener('pointerup', release);
  chartDiv.addEventListener('pointercancel', release);
}

function downloadChartPng(plot, name) {
  // uPlot renders into a <canvas>; grab it directly.
  const canvas = plot.root.querySelector('canvas');
  if (!canvas) return;
  canvas.toBlob((blob) => {
    if (!blob) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = sanitizeFilename(name) + '.png';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }, 'image/png');
}

function downloadChartCsv(data, def, name) {
  const [xs, ...ys] = data;
  const header = ['timestamp_utc', ...def.series.map((s) => s.label)].join(',');
  const lines = [header];
  for (let i = 0; i < xs.length; i++) {
    const row = [new Date(xs[i] * 1000).toISOString()];
    for (let k = 0; k < ys.length; k++) {
      const v = ys[k][i];
      row.push(Number.isFinite(v) ? String(v) : '');
    }
    lines.push(row.join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = sanitizeFilename(name) + '.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function sanitizeFilename(s) {
  return s.replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 80);
}
