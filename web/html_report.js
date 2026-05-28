// Self-contained HTML report — mirrors the Python html_report.py output so
// the artifact looks the same regardless of which side built it.
//
// Collects every rendered uPlot canvas from the current page, converts to
// base64 PNG via canvas.toDataURL, splices into a string template, and
// downloads the result as report.html.

import { downloadBlob } from './xlsx_export.js';

const CSS = `
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 1100px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; color: #222; background: #fff; }
@media (prefers-color-scheme: dark) {
  body { color: #ddd; background: #1a1a1a; }
  th { background: #2a2a2a; }
  td { border-top-color: #333; }
}
h1 { font-size: 1.5rem; margin-top: 0; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: 0.25rem; }
h3 { margin-top: 1.5rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 1.5rem;
     font-variant-numeric: tabular-nums; }
dt { font-weight: bold; } dd { margin: 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem;
        font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 0.35rem 0.5rem; }
th { background: #f4f4f4; border-bottom: 2px solid #ccc; }
td { border-top: 1px solid #eee; }
figure { margin: 1rem 0; }
figure img { max-width: 100%; height: auto; display: block; border: 1px solid #ddd; }
figcaption { font-size: 0.85rem; color: #666; margin-top: 0.3rem; }
footer { margin-top: 3rem; color: #888; font-size: 0.85rem;
         border-top: 1px solid #ccc; padding-top: 1rem; }
.insight { border-left: 4px solid #888; padding: 0.5rem 1rem;
           margin: 0.75rem 0; background: rgba(0, 0, 0, 0.025); }
.insight.alert { border-left-color: #cc0000; }
.insight.warn  { border-left-color: #cc6600; }
.insight.info  { border-left-color: #0066cc; }
.insight h3 { margin: 0 0 0.25rem 0; font-size: 1rem; }
.insight .meta { color: #888; font-size: 0.8rem; text-transform: uppercase; }
.insight ul { margin: 0.25rem 0 0 1.2rem; }
@media print { body { max-width: none; } h2 { page-break-before: always; } }
`;

function esc(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function summarizeRecords(records, spec) {
  const fi = new Map(spec.fields.map((f) => [f.name, f.index]));
  const pIdx = fi.get('P_total_avg_W');
  const whIdx = fi.get('Wh_total');
  let whFwd = 0, whRev = 0, pPos = 0, pNeg = 0;
  for (const r of records) {
    const wh = r.floats[whIdx];
    const p = r.floats[pIdx];
    if (Number.isFinite(wh)) {
      if (wh > 0) whFwd += wh;
      else if (wh < 0) whRev += wh;
    }
    if (Number.isFinite(p)) {
      if (p > pPos) pPos = p;
      if (p < pNeg) pNeg = p;
    }
  }
  return { whFwd, whRev, pPos, pNeg };
}

function summaryDlHtml(stats, config) {
  const rows = [];
  if (config) {
    for (const [disp, key] of [
      ['Asset', 'asset_name'],
      ['Team', 'team_name'],
      ['Instrument', 'type'],
      ['Firmware', 'firmware_version'],
    ]) {
      const v = config[key];
      if (v) rows.push([disp, String(v)]);
    }
  }
  for (const [k, v] of Object.entries(stats)) rows.push([k, String(v)]);
  return '<dl>\n' + rows.map(
    ([k, v]) => `  <dt>${esc(k)}</dt><dd>${esc(v)}</dd>`
  ).join('\n') + '\n</dl>';
}

function eventsTableHtml(events) {
  if (events.length === 0) return '<p><em>No events detected.</em></p>';
  const rows = events.map((ev) => {
    const phases = (ev.affectedPhases ?? []).join('/') || '—';
    const durSec = Math.max(1, Math.round((ev.tEndMs - ev.tStartMs) / 1000));
    const tStart = new Date(ev.tStartMs).toISOString();
    return `<tr><td>${ev.id}</td><td>${esc(ev.kind)}</td>` +
      `<td>${esc(tStart)}</td><td>${durSec} s</td>` +
      `<td>${esc(phases)}</td><td>${ev.severity.toFixed(3)}</td></tr>`;
  });
  return '<table>\n' +
    '<thead><tr><th>ID</th><th>Kind</th><th>Start (UTC)</th>' +
    '<th>Duration</th><th>Phases</th><th>Severity</th></tr></thead>\n' +
    '<tbody>\n' + rows.join('\n') + '\n</tbody></table>';
}

function snapshotsTableHtml(snaps) {
  if (snaps.length === 0) return '';
  const rows = snaps.map((s) => {
    const tStart = new Date(s.tStartMs).toISOString();
    return `<tr><td>${s.id}</td><td>${esc(tStart)}</td>` +
      `<td>${(s.pTotalMeanW / 1000).toFixed(2)} kW</td>` +
      `<td>${s.pTotalStdevW.toFixed(1)} W</td></tr>`;
  });
  return '<h3>Quiet snapshots</h3>\n<table>\n' +
    '<thead><tr><th>ID</th><th>Start (UTC)</th>' +
    '<th>Mean P</th><th>σ(P)</th></tr></thead>\n' +
    '<tbody>\n' + rows.join('\n') + '\n</tbody></table>';
}

function chartFiguresHtml(charts) {
  // charts: [{section, title, dataUrl}]
  const bySection = new Map();
  for (const c of charts) {
    if (!bySection.has(c.section)) bySection.set(c.section, []);
    bySection.get(c.section).push(c);
  }
  const sectionTitles = {
    full: 'Full-session charts',
    event: 'Event zooms',
    snapshot: 'Snapshot zooms',
    other: 'Other charts',
  };
  const order = ['full', 'event', 'snapshot', 'other'];
  const out = [];
  for (const section of order) {
    const items = bySection.get(section);
    if (!items?.length) continue;
    out.push(`<h2>${esc(sectionTitles[section])}</h2>`);
    for (const c of items) {
      out.push(
        `<figure><img src="${c.dataUrl}" alt="${esc(c.title)}">` +
        `<figcaption>${esc(c.title)}</figcaption></figure>`
      );
    }
  }
  return out.join('\n');
}

/**
 * Collect uPlot canvases from the DOM, group by which container they live in.
 */
function collectChartArtifacts() {
  const groups = [
    ['full', document.getElementById('full-charts')],
    ['event', document.getElementById('event-charts')],
    ['snapshot', document.getElementById('snapshot-charts')],
  ];
  const out = [];
  for (const [section, container] of groups) {
    if (!container) continue;
    const wrappers = container.querySelectorAll('article.chart-wrapper');
    for (const w of wrappers) {
      const canvas = w.querySelector('canvas');
      const title = w.querySelector('h3')?.textContent ?? 'chart';
      if (!canvas) continue;
      const dataUrl = canvas.toDataURL('image/png');
      out.push({ section, title, dataUrl });
    }
  }
  return out;
}

function insightsHtml(findings) {
  if (!findings?.length) return '';
  const out = ['<h2>Insights</h2>'];
  for (const f of findings) {
    const actions = (f.recommendedActions ?? []).map(
      (a) => `<li>${esc(a)}</li>`
    ).join('');
    const actionsBlock = actions
      ? `<p class="meta">Recommended</p><ul>${actions}</ul>` : '';
    out.push(
      `<section class="insight ${esc(f.severity)}">` +
      `<h3>${esc(f.headline)}</h3>` +
      `<p class="meta">${esc(f.kind)} · ${esc(f.severity)}</p>` +
      `<p>${esc(f.detail)}</p>` +
      `${actionsBlock}` +
      `</section>`
    );
  }
  return out.join('\n');
}

export function buildReportHtml({ title, config, records, spec, events, snapshots, findings = [] }) {
  const energy = summarizeRecords(records, spec);
  const stats = {
    'Records (per-second)': records.length.toLocaleString(),
    'Event count': events.length,
    'Snapshot count': snapshots.length,
    'Imported (kWh)':   (energy.whFwd / 1000).toFixed(2),
    'Exported (kWh)':   (energy.whRev / 1000).toFixed(2),
    'Peak import (kW)': (energy.pPos / 1000).toFixed(2),
    'Peak export (kW)': (energy.pNeg / 1000).toFixed(2),
  };
  const charts = collectChartArtifacts();
  const body = [
    `<h1>${esc(title)}</h1>`,
    '<h2>Summary</h2>',
    summaryDlHtml(stats, config),
    insightsHtml(findings),
    '<h2>Events</h2>',
    eventsTableHtml(events),
    snapshotsTableHtml(snapshots),
    chartFiguresHtml(charts),
    `<footer>Generated ${esc(new Date().toISOString())} by ` +
    '<a href="https://github.com/GrumpyTanker/fluke-3540-analyzer">fluke-3540-analyzer</a></footer>',
  ];
  return (
    '<!DOCTYPE html>\n' +
    "<html lang='en'><head><meta charset='utf-8'>" +
    `<title>${esc(title)}</title>` +
    `<style>${CSS}</style></head><body>\n` +
    body.join('\n') +
    '\n</body></html>\n'
  );
}

export function downloadHtmlReport(opts) {
  const html = buildReportHtml(opts);
  const blob = new Blob([html], { type: 'text/html' });
  const name = ((opts.config?.asset_name) || 'fluke_session')
    .replace(/[^a-zA-Z0-9._-]+/g, '_');
  downloadBlob(blob, `${name}_report.html`);
  return { chartCount: (html.match(/<figure>/g) ?? []).length };
}
