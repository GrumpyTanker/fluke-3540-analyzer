// CSV input mode — pre-parsed session.csv reader.
//
// Pairs with python/.../parser.py:from_csv(). Accepts a CSV with the
// same shape that export_csv produces (timestamp_utc, window_end_utc,
// and any subset of the field-map column names), yields Record objects
// matching parser.js's output shape so the rest of the pipeline doesn't
// change.

export class CsvParseError extends Error {}

function splitCsvLine(line) {
  // Our exporter doesn't quote fields, but be defensive anyway.
  const out = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === '"' ) {
      if (inQuotes && line[i + 1] === '"') { cur += '"'; i++; }
      else inQuotes = !inQuotes;
    } else if (c === ',' && !inQuotes) {
      out.push(cur); cur = '';
    } else {
      cur += c;
    }
  }
  out.push(cur);
  return out;
}

function parseIsoMs(s) {
  // Date.parse handles ISO timestamps. Naive strings (no offset) are
  // ambiguous: JS treats them as LOCAL time, but our Python writer treats
  // them as UTC. Match Python by appending 'Z' when no offset is present.
  if (!s) return NaN;
  // Detect an offset at the END of the string: Z, or ±HH:MM / ±HHMM
  const hasOffset = /(Z|[+\-]\d{2}:?\d{2})$/.test(s);
  return Date.parse(hasOffset ? s : s + 'Z');
}

/**
 * Parse a CSV ArrayBuffer (or string) into {records}.
 * @param {ArrayBuffer|string} input
 * @param {object} spec
 * @returns {{records: Array}}
 */
export function parseCsvBuffer(input, spec) {
  const text = typeof input === 'string'
    ? input
    : new TextDecoder('utf-8').decode(input);
  const lines = text.split(/\r?\n/).filter((ln) => ln.length > 0);
  if (lines.length < 2) {
    throw new CsvParseError('CSV needs at least a header row and one data row.');
  }
  const headers = splitCsvLine(lines[0]);
  const tIdx = headers.indexOf('timestamp_utc');
  if (tIdx < 0) {
    throw new CsvParseError(
      'CSV is missing the timestamp_utc column — does this look like a fluke-analyze session.csv?'
    );
  }
  const endIdx = headers.indexOf('window_end_utc');
  // Map column-index → float-array-index for the field columns we recognise.
  const fieldByCol = new Array(headers.length).fill(-1);
  let recognisedCount = 0;
  const nameToIdx = new Map(spec.fields.map((f) => [f.name, f.index]));
  for (let c = 0; c < headers.length; c++) {
    const idx = nameToIdx.get(headers[c]);
    if (idx !== undefined) {
      fieldByCol[c] = idx;
      recognisedCount++;
    }
  }
  if (recognisedCount === 0) {
    throw new CsvParseError(
      'CSV has no recognised field columns — this does not look like a session.csv.'
    );
  }

  const records = new Array(lines.length - 1);
  let outIdx = 0;
  for (let li = 1; li < lines.length; li++) {
    const cells = splitCsvLine(lines[li]);
    const startMs = parseIsoMs(cells[tIdx]);
    if (!Number.isFinite(startMs)) continue;
    const endMs = endIdx >= 0
      ? (parseIsoMs(cells[endIdx]) || startMs + 1000)
      : startMs + 1000;
    const floats = new Float32Array(spec.data_floats);
    for (let c = 0; c < cells.length; c++) {
      const idx = fieldByCol[c];
      if (idx < 0) continue;
      const v = cells[c];
      if (v === '') continue;
      const n = Number(v);
      if (Number.isFinite(n)) floats[idx] = n;
    }
    records[outIdx++] = { index: outIdx - 1, startMs, endMs, floats };
  }
  records.length = outIdx;
  return { records };
}

export function looksLikeCsv(file) {
  return typeof file?.name === 'string' && file.name.toLowerCase().endsWith('.csv');
}
