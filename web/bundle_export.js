// Bulk "Download all" — wraps the workbook, full CSV, and every currently
// rendered chart PNG into a single .zip. Uses window.JSZip from
// vendor/jszip.min.js.

import { downloadBlob } from './xlsx_export.js';

function jszipOrThrow() {
  if (typeof window === 'undefined' || typeof window.JSZip === 'undefined') {
    throw new Error('JSZip global not found — ensure vendor/jszip.min.js is loaded');
  }
  return window.JSZip;
}

/**
 * Find all uPlot canvases inside the page and serialize each as a PNG blob.
 * Returns [{ name, blob }] keyed off the chart card's h3 title.
 */
async function collectRenderedPngs() {
  const wrappers = document.querySelectorAll('article.chart-wrapper');
  const out = [];
  for (const w of wrappers) {
    const canvas = w.querySelector('canvas');
    const title = w.querySelector('h3')?.textContent ?? 'chart';
    if (!canvas) continue;
    const blob = await new Promise((res) => canvas.toBlob(res, 'image/png'));
    if (blob) out.push({ name: sanitizeFilename(title) + '.png', blob });
  }
  return out;
}

/**
 * Build full per-second CSV from records + spec.
 * Uses just the slim COLS_TO_KEEP — the full 177-col version is huge.
 */
function buildSlimCsv(records, spec) {
  const cols = [
    'timestamp_utc',
    'V_LN_a_avg_V', 'V_LN_b_avg_V', 'V_LN_c_avg_V',
    'I_a_avg_A', 'I_b_avg_A', 'I_c_avg_A',
    'P_total_avg_W', 'S_total_avg_VA', 'Q_total_avg_VAR',
    'PF_total_avg', 'DPF_total_avg', 'freq_avg_Hz',
    'V_THD_pct_a_avg', 'V_THD_pct_b_avg', 'V_THD_pct_c_avg',
    'I_THD_pct_a_avg', 'I_THD_pct_b_avg', 'I_THD_pct_c_avg',
    'Wh_total',
  ];
  const fi = new Map(spec.fields.map((f) => [f.name, f.index]));
  const lines = [cols.join(',')];
  for (const r of records) {
    const row = [new Date(r.startMs).toISOString()];
    for (let k = 1; k < cols.length; k++) {
      const idx = fi.get(cols[k]);
      row.push(idx !== undefined ? String(r.floats[idx]) : '');
    }
    lines.push(row.join(','));
  }
  return lines.join('\n');
}

function sanitizeFilename(s) {
  return s.replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 80);
}

/**
 * Bundle and trigger download.
 * @param {{records: Array, spec: object, xlsxBlob: Blob, assetName?: string}} opts
 */
export async function downloadBundleZip({ records, spec, xlsxBlob, assetName }) {
  const JSZip = jszipOrThrow();
  const zip = new JSZip();
  const dirName = sanitizeFilename(assetName || 'fluke_session');
  const dir = zip.folder(dirName);

  // XLSX
  if (xlsxBlob) {
    dir.file('report.xlsx', xlsxBlob);
  }
  // Slim CSV
  dir.file('session_slim.csv', buildSlimCsv(records, spec));
  // PNGs
  const pngs = await collectRenderedPngs();
  const chartsDir = dir.folder('charts');
  for (const p of pngs) chartsDir.file(p.name, p.blob);

  const blob = await zip.generateAsync({
    type: 'blob',
    compression: 'DEFLATE',
    compressionOptions: { level: 6 },
  });
  downloadBlob(blob, `${dirName}.zip`);
  return { pngCount: pngs.length };
}
