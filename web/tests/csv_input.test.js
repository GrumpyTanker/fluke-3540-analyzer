// CSV-input parity tests — must produce the same records as the binary
// path for round-trip equivalence.
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { parseTrendBin } from '../parser.js';
import { CsvParseError, looksLikeCsv, parseCsvBuffer } from '../csv_input.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const spec = JSON.parse(readFileSync(resolve(repoRoot, 'spec', 'field_map.json'), 'utf8'));
const FIXTURE_PATH = resolve(
  repoRoot, 'python', 'tests', 'fixtures', 'synthetic_trend.bin'
);


function loadFixture() {
  const buf = readFileSync(FIXTURE_PATH);
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

function recordsToCsv(records, spec) {
  // Re-create what python's export_csv would produce: every field column,
  // ISO timestamps, raw values.
  const headers = ['record_index', 'timestamp_utc', 'window_end_utc',
                   ...spec.fields.map((f) => f.name)];
  const lines = [headers.join(',')];
  for (const r of records) {
    const row = [
      r.index,
      new Date(r.startMs).toISOString(),
      new Date(r.endMs).toISOString(),
      ...spec.fields.map((f) => r.floats[f.index]),
    ];
    lines.push(row.join(','));
  }
  return lines.join('\n');
}

test('parseCsvBuffer: round-trips parseTrendBin records', () => {
  const { records: binRecords } = parseTrendBin(loadFixture(), spec);
  const csv = recordsToCsv(binRecords, spec);
  const { records: csvRecords } = parseCsvBuffer(csv, spec);
  assert.equal(csvRecords.length, binRecords.length);
  // Spec covers 174 of 180 floats — the 6 "reserved" indices (164–169)
  // aren't named columns, so they don't round-trip via CSV. Check only
  // the indices that the spec actually has.
  const indicesInSpec = spec.fields.map((f) => f.index);
  for (let n = 0; n < binRecords.length; n++) {
    assert.equal(csvRecords[n].startMs, binRecords[n].startMs);
    assert.equal(csvRecords[n].endMs, binRecords[n].endMs);
    for (const i of indicesInSpec) {
      const diff = Math.abs(csvRecords[n].floats[i] - binRecords[n].floats[i]);
      assert.ok(diff < 1e-3,
        `record[${n}].floats[${i}] differs by ${diff} (csv=${csvRecords[n].floats[i]}, bin=${binRecords[n].floats[i]})`);
    }
  }
});

test('parseCsvBuffer: missing timestamp_utc column rejected', () => {
  const csv = 'foo,bar\n1,2\n';
  assert.throws(() => parseCsvBuffer(csv, spec), /timestamp_utc/);
});

test('parseCsvBuffer: empty cells become 0', () => {
  const csv = [
    'timestamp_utc,window_end_utc,P_total_avg_W,V_LN_a_avg_V',
    '2024-01-13T22:00:00Z,2024-01-13T22:00:01Z,,277',
  ].join('\n');
  const { records } = parseCsvBuffer(csv, spec);
  const byName = new Map(spec.fields.map((f) => [f.name, f.index]));
  assert.equal(records.length, 1);
  assert.equal(records[0].floats[byName.get('P_total_avg_W')], 0);
  assert.equal(records[0].floats[byName.get('V_LN_a_avg_V')], 277);
});

test('parseCsvBuffer: rejects CSV with no recognised columns', () => {
  const csv = 'timestamp_utc,window_end_utc,foo,bar\n2024-01-13T22:00:00Z,2024-01-13T22:00:01Z,1,2\n';
  assert.throws(() => parseCsvBuffer(csv, spec), /no recognised field columns/);
});

test('looksLikeCsv', () => {
  assert.equal(looksLikeCsv({ name: 'session.csv' }), true);
  assert.equal(looksLikeCsv({ name: 'SESSION.CSV' }), true);
  assert.equal(looksLikeCsv({ name: 'trend.bin' }), false);
  assert.equal(looksLikeCsv({ name: 'session.fel' }), false);
  assert.equal(looksLikeCsv(null), false);
});

test('parseCsvBuffer: handles naive timestamps as UTC', () => {
  const csv = [
    'timestamp_utc,window_end_utc,P_total_avg_W',
    '2024-01-13T22:00:00,2024-01-13T22:00:01,1000',
  ].join('\n');
  const { records } = parseCsvBuffer(csv, spec);
  assert.equal(records.length, 1);
  // Should parse as UTC, so 22:00 of that date
  assert.equal(records[0].startMs, Date.UTC(2024, 0, 13, 22, 0, 0));
});
