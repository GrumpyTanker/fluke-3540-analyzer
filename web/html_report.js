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

// Whole-session statistics table (Feature B) from a wholeSessionStats() dict.
function wholeStatsTableHtml(wholeStats) {
  if (!wholeStats) return '';
  const cols = ['min', 'p1', 'p5', 'median', 'mean', 'p95', 'p99', 'max', 'stdev'];
  const head = ['Channel', 'Unit', ...cols].map((h) => `<th>${esc(h)}</th>`).join('');
  const fmt = (v) => (Number.isFinite(v)
    ? (Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2)) : '—');
  const rows = [];
  for (const [name, d] of Object.entries(wholeStats)) {
    if (name.startsWith('_')) continue;
    const cells = [esc(name), esc(d.unit), ...cols.map((c) => fmt(d[c]))]
      .map((c) => `<td>${c}</td>`).join('');
    rows.push(`<tr>${cells}</tr>`);
  }
  const th = wholeStats._thresholds || {};
  const note =
    `<p><small>Under-voltage (&lt;${th.undervoltage_v} V): ${th.sec_undervoltage} s ` +
    `(${(th.pct_undervoltage ?? 0).toFixed(2)}%). Over-current (&gt;${th.overcurrent_a} A): ` +
    `${th.sec_overcurrent} s (${(th.pct_overcurrent ?? 0).toFixed(2)}%).</small></p>`;
  return `<h2>Statistics</h2><table><thead><tr>${head}</tr></thead>` +
    `<tbody>${rows.join('')}</tbody></table>${note}`;
}

// IEEE 519 + SARFI power-quality block (Feature F).
function pqHtml(pq) {
  if (!pq) return '';
  const v = pq.ieee519.voltage;
  const s = pq.sarfi;
  const verdict = pq.ieee519.all_voltage_compliant ? 'COMPLIANT' : 'NON-COMPLIANT';
  return '<h2>Power quality (IEEE 519 / 1159)</h2>' +
    `<p>IEEE 519 voltage THD p95 (limit ${pq.ieee519.limit_v_thd_pct.toFixed(0)}%): ` +
    `A=${v.a.p95.toFixed(1)}%, B=${v.b.p95.toFixed(1)}%, C=${v.c.p95.toFixed(1)}% — ` +
    `<strong>${verdict}</strong>.</p>` +
    `<p>SARFI: 90=${s['SARFI-90']}, 80=${s['SARFI-80']}, 70=${s['SARFI-70']}, ` +
    `50=${s['SARFI-50']}, 10=${s['SARFI-10']} (${s.events_considered} voltage events).</p>`;
}

// Peak-demand block (Feature G).
function demandHtml(demand) {
  if (!demand || !demand.n_windows) return '';
  const wmin = Math.round(demand.window_secs / 60);
  return `<h2>Demand</h2><p>Peak ${wmin}-min demand: ` +
    `<strong>${demand.peak_demand_kw.toFixed(1)} kW</strong> ` +
    `(window ending ${esc((demand.peak_window_end || '').slice(0, 19))}Z); ` +
    `mean demand ${(demand.mean_demand_w / 1000).toFixed(1)} kW.</p>`;
}

export function buildReportHtml({ title, config, records, spec, events, snapshots, findings = [], wholeStats = null, narrative = null, pq = null, demand = null }) {
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
  const narrativeHtml = narrative
    ? `<section class="narrative"><h2>Executive summary</h2><p>${esc(narrative).replace(/\n/g, '<br>')}</p></section>`
    : '';
  const body = [
    `<h1>${esc(title)}</h1>`,
    narrativeHtml,
    '<h2>Summary</h2>',
    summaryDlHtml(stats, config),
    wholeStatsTableHtml(wholeStats),
    pqHtml(pq),
    demandHtml(demand),
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

function compareSummaryTableHtml(sessionStats) {
  if (!sessionStats?.length) return '';
  const metrics = [
    ['records',         'Records'],
    ['importedKwh',     'Imported (kWh)'],
    ['exportedKwh',     'Exported (kWh)'],
    ['peakImportKw',    'Peak import (kW)'],
    ['peakExportKw',    'Peak export (kW)'],
    ['peakCurrentA',    'Peak current (A)'],
    ['eventCount',      'Events detected'],
  ];
  const head = '<thead><tr><th>Metric</th>' +
    sessionStats.map((s) => `<th>${esc(s.label)}</th>`).join('') + '</tr></thead>';
  const body = metrics.map(([k, lbl]) => {
    const cells = [`<td>${esc(lbl)}</td>`];
    for (const s of sessionStats) {
      const v = s[k];
      if (typeof v === 'number') {
        cells.push(`<td>${k === 'records' || k === 'eventCount' ? v.toLocaleString() : v.toFixed(3)}</td>`);
      } else {
        cells.push('<td></td>');
      }
    }
    return '<tr>' + cells.join('') + '</tr>';
  }).join('\n');
  return '<table>' + head + '<tbody>' + body + '</tbody></table>';
}

function compareInsightsHtml(findings) {
  if (!findings?.length) return '';
  const out = ['<h2>Cross-session insights</h2>'];
  for (const f of findings) {
    const actions = (f.recommendedActions ?? []).map((a) => `<li>${esc(a)}</li>`).join('');
    const actionsBlock = actions ? `<p class="meta">Recommended</p><ul>${actions}</ul>` : '';
    out.push(
      `<section class="insight ${esc(f.severity)}">` +
      `<h3>${esc(f.headline)}</h3>` +
      `<p class="meta">${esc(f.kind)} · ${esc(f.severity)} · sessions: ${esc((f.sessionLabels ?? []).join(', '))}</p>` +
      `<p>${esc(f.detail)}</p>` +
      `${actionsBlock}` +
      `</section>`
    );
  }
  return out.join('\n');
}

function summarizeSession(s, spec) {
  const fi = new Map(spec.fields.map((f) => [f.name, f.index]));
  const whIdx = fi.get('Wh_total');
  const pIdx  = fi.get('P_total_avg_W');
  const iaIdx = fi.get('I_a_avg_A');
  const ibIdx = fi.get('I_b_avg_A');
  const icIdx = fi.get('I_c_avg_A');
  let whFwd = 0, whRev = 0, pPos = 0, pNeg = 0, iPeak = 0;
  for (const r of s.records) {
    const wh = r.floats[whIdx];
    const p = r.floats[pIdx];
    if (Number.isFinite(wh)) {
      if (wh > 0) whFwd += wh; else if (wh < 0) whRev += wh;
    }
    if (Number.isFinite(p)) {
      if (p > pPos) pPos = p;
      if (p < pNeg) pNeg = p;
    }
    iPeak = Math.max(iPeak, r.floats[iaIdx] || 0, r.floats[ibIdx] || 0, r.floats[icIdx] || 0);
  }
  return {
    label: s.label,
    records: s.records.length,
    importedKwh: whFwd / 1000,
    exportedKwh: whRev / 1000,
    peakImportKw: pPos / 1000,
    peakExportKw: pNeg / 1000,
    peakCurrentA: iPeak,
    eventCount: (s.events ?? []).length,
  };
}

export function buildCompareReportHtml({ title, sessions, spec, findings }) {
  const stats = sessions.map((s) => summarizeSession(s, spec));
  // Compare-mode page has overlay charts in #full-charts.
  const charts = (() => {
    const container = document.getElementById('full-charts');
    if (!container) return [];
    const out = [];
    for (const w of container.querySelectorAll('article.chart-wrapper')) {
      const canvas = w.querySelector('canvas');
      const t = w.querySelector('h3')?.textContent ?? 'chart';
      if (canvas) out.push({ section: 'overlay', title: t, dataUrl: canvas.toDataURL('image/png') });
    }
    return out;
  })();
  const body = [
    `<h1>${esc(title)}</h1>`,
    '<h2>Per-session summary</h2>',
    compareSummaryTableHtml(stats),
    compareInsightsHtml(findings),
    charts.length
      ? '<h2>Overlay charts</h2>' + charts.map(
          (c) => `<figure><img src="${c.dataUrl}" alt="${esc(c.title)}"><figcaption>${esc(c.title)}</figcaption></figure>`
        ).join('\n')
      : '',
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

export function downloadCompareHtmlReport(opts) {
  const html = buildCompareReportHtml(opts);
  const blob = new Blob([html], { type: 'text/html' });
  downloadBlob(blob, 'fluke_compare_report.html');
}
