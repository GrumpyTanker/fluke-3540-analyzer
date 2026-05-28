// Browser-side XLSX export using SheetJS. Mirrors the sheet layout the
// Python CLI produces in python/.../plots/xlsx.py:
//   Summary | Load Profile | Power Factor | Harmonics | Voltage & Current | Data
//
// Expects window.XLSX from vendor/xlsx.full.min.js. SheetJS in pure JS
// doesn't render embedded charts (that needs xlsx-chart or xlsx-js-style),
// so for the chart sheets we emit a thin "this is a placeholder, open the
// Data sheet" note. The user has the rendered PNGs + per-chart CSVs from
// the Render flow if they want bitmap charts.

function xlsxOrThrow() {
  if (typeof window === 'undefined' || typeof window.XLSX === 'undefined') {
    throw new Error('XLSX global not found — ensure vendor/xlsx.full.min.js is loaded');
  }
  return window.XLSX;
}

// Slim CSV-column → display-name map — matches Python.
const COLS_TO_KEEP = [
  ['timestamp_utc',      'Timestamp (UTC)'],
  ['V_LN_a_avg_V',       'V_LN_a (V)'],
  ['V_LN_b_avg_V',       'V_LN_b (V)'],
  ['V_LN_c_avg_V',       'V_LN_c (V)'],
  ['I_a_avg_A',          'I_a (A)'],
  ['I_b_avg_A',          'I_b (A)'],
  ['I_c_avg_A',          'I_c (A)'],
  ['P_total_avg_W',      'P_total (W)'],
  ['S_total_avg_VA',     'S_total (VA)'],
  ['Q_total_avg_VAR',    'Q_total (VAR)'],
  ['PF_total_avg',       'PF (true)'],
  ['DPF_total_avg',      'DPF (displacement)'],
  ['freq_avg_Hz',        'Frequency (Hz)'],
  ['V_THD_pct_a_avg',    'V_THD_a (%)'],
  ['V_THD_pct_b_avg',    'V_THD_b (%)'],
  ['V_THD_pct_c_avg',    'V_THD_c (%)'],
  ['I_THD_pct_a_avg',    'I_THD_a (%)'],
  ['I_THD_pct_b_avg',    'I_THD_b (%)'],
  ['I_THD_pct_c_avg',    'I_THD_c (%)'],
  ['Wh_total',           'Wh_total (per row)'],
];

function fieldIndex(spec, name) {
  const f = spec.fields.find((f) => f.name === name);
  return f ? f.index : null;
}

function downsample(records, everyN) {
  if (everyN <= 1) return records;
  const out = [];
  for (let i = 0; i < records.length; i += everyN) out.push(records[i]);
  return out;
}

function buildSummaryStats(records, spec) {
  const pIdx  = fieldIndex(spec, 'P_total_avg_W');
  const whIdx = fieldIndex(spec, 'Wh_total');
  const iaIdx = fieldIndex(spec, 'I_a_avg_A');
  const ibIdx = fieldIndex(spec, 'I_b_avg_A');
  const icIdx = fieldIndex(spec, 'I_c_avg_A');
  let whSum = 0, whFwd = 0, whRev = 0;
  let pPos = 0, pNeg = 0, iPeak = 0;
  let secImport = 0, secExport = 0, secIdle = 0;
  for (const r of records) {
    const wh = r.floats[whIdx], p = r.floats[pIdx];
    if (!Number.isFinite(wh) || !Number.isFinite(p)) continue;
    whSum += wh;
    if (wh > 0) whFwd += wh;
    if (wh < 0) whRev += wh;
    if (p > pPos) pPos = p;
    if (p < pNeg) pNeg = p;
    if (p > 10) secImport++;
    else if (p < -10) secExport++;
    else secIdle++;
    iPeak = Math.max(iPeak, r.floats[iaIdx], r.floats[ibIdx], r.floats[icIdx]);
  }
  return { whSum, whFwd, whRev, pPos, pNeg, iPeak, secImport, secExport, secIdle,
           rows: records.length };
}

/**
 * Build an XLSX Blob from parsed records + spec + optional config.
 * Down-samples to 1 row per minute for the Data sheet (records are 1/sec).
 * @returns {Blob}
 */
export function buildXlsx({ records, spec, config = null }) {
  const XLSX = xlsxOrThrow();
  const wb = XLSX.utils.book_new();

  // --- Summary sheet (first) -----------------------------------------------
  const stats = buildSummaryStats(records, spec);
  const total = stats.secImport + stats.secExport + stats.secIdle;
  const pct = (n) => total ? `${(n / total * 100).toFixed(1)}%` : 'n/a';
  const summaryRows = [['Fluke 3540 FC Session Summary'], []];
  if (config) {
    if (config.asset_name) summaryRows.push(['Asset', config.asset_name]);
    if (config.team_name)  summaryRows.push(['Team',  config.team_name]);
    if (config.type) {
      const fw = config.firmware_version ? ` fw ${config.firmware_version}` : '';
      summaryRows.push(['Instrument', `${config.type}${fw}`]);
    }
  }
  summaryRows.push(
    ['Records (per-second)', stats.rows.toLocaleString()],
    [],
    ['Net energy (kWh)',     (stats.whSum / 1000).toFixed(2)],
    ['Imported (kWh)',       (stats.whFwd / 1000).toFixed(2)],
    ['Exported (kWh)',       (stats.whRev / 1000).toFixed(2)],
    [],
    ['Peak import power (kW)', (stats.pPos / 1000).toFixed(2)],
    ['Peak export power (kW)', (stats.pNeg / 1000).toFixed(2)],
    ['Peak current (A)',       stats.iPeak.toFixed(1)],
    [],
    ['Time importing',  `${stats.secImport.toLocaleString()} s (${pct(stats.secImport)})`],
    ['Time exporting',  `${stats.secExport.toLocaleString()} s (${pct(stats.secExport)})`],
    ['Time idle (|P|<10W)', `${stats.secIdle.toLocaleString()} s (${pct(stats.secIdle)})`],
  );
  const wsSummary = XLSX.utils.aoa_to_sheet(summaryRows);
  wsSummary['!cols'] = [{ wch: 30 }, { wch: 50 }];
  XLSX.utils.book_append_sheet(wb, wsSummary, 'Summary');

  // --- Data sheet (1-minute downsample) ------------------------------------
  const oneMin = downsample(records, 60);
  const indices = COLS_TO_KEEP.map(([src]) => ({
    src,
    floatIdx: src === 'timestamp_utc' ? null : fieldIndex(spec, src),
  }));
  const data = [COLS_TO_KEEP.map(([, disp]) => disp)];
  for (const r of oneMin) {
    const row = [];
    for (const { src, floatIdx } of indices) {
      if (src === 'timestamp_utc') {
        row.push(new Date(r.startMs));
      } else if (floatIdx === null) {
        row.push(null);
      } else {
        row.push(r.floats[floatIdx]);
      }
    }
    data.push(row);
  }
  const wsData = XLSX.utils.aoa_to_sheet(data, { cellDates: true });
  wsData['!cols'] = COLS_TO_KEEP.map(([, disp]) => ({ wch: Math.max(12, disp.length + 2) }));
  // Format timestamp column as datetime
  const ref = XLSX.utils.decode_range(wsData['!ref']);
  for (let r = 1; r <= ref.e.r; r++) {
    const cellAddr = XLSX.utils.encode_cell({ r, c: 0 });
    const cell = wsData[cellAddr];
    if (cell) cell.z = 'yyyy-mm-dd hh:mm';
  }
  XLSX.utils.book_append_sheet(wb, wsData, 'Data');

  // --- Chart sheets — placeholder notes (SheetJS community can't embed charts)
  for (const sheetName of ['Load Profile', 'Power Factor', 'Harmonics', 'Voltage & Current']) {
    const ws = XLSX.utils.aoa_to_sheet([
      [sheetName],
      ['(Open the Data sheet to chart in Excel, or use the PNG downloads from the web app.)'],
    ]);
    ws['!cols'] = [{ wch: 80 }];
    XLSX.utils.book_append_sheet(wb, ws, sheetName);
  }

  const arrayBuffer = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
  return new Blob([arrayBuffer], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
}

/**
 * Trigger a browser download.
 */
export function downloadBlob(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
}
