// Feature A — streaming/columnar parse tests.
//
// Verifies:
//   1. parseTrendColumnar produces the correct record count + correct values
//      against the shared 10-record synthetic fixture (parity with parser.py).
//   2. parseTrendColumnarStream (chunked Blob.slice path) yields identical
//      columns to the one-shot path AND keeps peak heap bounded — it never
//      holds the whole buffer or 590 K record objects.
//   3. ColumnStore.fromRecords / toRecords round-trips the retained channels.
import { strict as assert } from 'node:assert';
import { readFileSync, writeFileSync, rmSync, mkdtempSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { test } from 'node:test';

import {
  parseTrendBin,
  parseTrendColumnar,
  parseTrendColumnarStream,
} from '../parser.js';
import { ColumnStore, STORE_COLUMNS } from '../column_store.js';
import { detectEvents } from '../events.js';
import { pickSnapshots } from '../snapshots.js';
import { analyzeInsights } from '../insights.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..', '..');
const SPEC_PATH = resolve(repoRoot, 'spec', 'field_map.json');
const FIXTURE_PATH = resolve(
  repoRoot, 'python', 'tests', 'fixtures', 'synthetic_trend.bin'
);
const spec = JSON.parse(readFileSync(SPEC_PATH, 'utf8'));

function loadFixture() {
  const buf = readFileSync(FIXTURE_PATH);
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

// Build a large healthy trend.bin in a temp file (mirrors conftest.build_large_trend).
function buildLargeTrend(path, count) {
  const recordSize = spec.record_size;
  const headerBytes = spec.header_bytes;
  const dataFloats = spec.data_floats;
  const magic = new Uint8Array(spec.record_magic);
  const nameToIdx = new Map(spec.fields.map((f) => [f.name, f.index]));
  // Healthy 277V / 60Hz / 100A / 50kW profile.
  const healthy = new Float32Array(dataFloats);
  for (const ph of ['a', 'b', 'c']) {
    for (const st of ['min', 'max', 'avg']) {
      healthy[nameToIdx.get(`V_LN_${ph}_${st}_V`)] = 277.0;
      healthy[nameToIdx.get(`I_${ph}_${st}_A`)] = 100.0;
    }
  }
  healthy[nameToIdx.get('freq_avg_Hz')] = 60.0;
  healthy[nameToIdx.get('P_total_avg_W')] = 50000.0;
  healthy[nameToIdx.get('S_total_avg_VA')] = 52000.0;
  healthy[nameToIdx.get('PF_total_avg')] = 0.96;

  const FILETIME_EPOCH_DIFF_MS = 11644473600000n;
  const base = Date.UTC(2024, 0, 13, 22, 0, 0);
  const buf = Buffer.alloc(recordSize * count);
  for (let n = 0; n < count; n++) {
    const o = n * recordSize;
    for (let m = 0; m < magic.length; m++) buf[o + m] = magic[m];
    const startMs = BigInt(base + n * 1000);
    const endMs = BigInt(base + (n + 1) * 1000);
    const startFt = (startMs + FILETIME_EPOCH_DIFF_MS) * 10000n;
    const endFt = (endMs + FILETIME_EPOCH_DIFF_MS) * 10000n;
    buf.writeUInt32LE(Number(startFt >> 32n & 0xffffffffn), o + 4);
    buf.writeUInt32LE(Number(startFt & 0xffffffffn), o + 8);
    buf.writeUInt32LE(Number(endFt >> 32n & 0xffffffffn), o + 12);
    buf.writeUInt32LE(Number(endFt & 0xffffffffn), o + 16);
    for (let i = 0; i < dataFloats; i++) {
      buf.writeFloatLE(healthy[i], o + headerBytes + i * 4);
    }
  }
  writeFileSync(path, buf);
}

test('parseTrendColumnar: record count + retained channels match legacy parse', () => {
  const ab = loadFixture();
  const legacy = parseTrendBin(ab, spec);
  const col = parseTrendColumnar(ab, spec);
  assert.equal(col.recordCount, legacy.records.length);
  const nameToIdx = new Map(spec.fields.map((f) => [f.name, f.index]));
  for (const name of STORE_COLUMNS) {
    const fi = nameToIdx.get(name);
    const arr = col.columns[name];
    for (let n = 0; n < col.recordCount; n++) {
      assert.equal(arr[n], legacy.records[n].floats[fi],
        `${name}[${n}] mismatch`);
    }
  }
  // Timestamps match.
  for (let n = 0; n < col.recordCount; n++) {
    assert.equal(col.startMs[n], legacy.records[n].startMs);
    assert.equal(col.endMs[n], legacy.records[n].endMs);
  }
});

test('parseTrendColumnar: reverseCts negates retained signed columns', () => {
  const ab = loadFixture();
  const plain = parseTrendColumnar(ab, spec, { reverseCts: false });
  const flipped = parseTrendColumnar(ab, spec, { reverseCts: true });
  // P_total_avg_W is a reverse-CT column; PF too. V/I/S are not.
  assert.equal(flipped.columns.P_total_avg_W[0], -plain.columns.P_total_avg_W[0]);
  assert.equal(flipped.columns.PF_total_avg[0], -plain.columns.PF_total_avg[0]);
  assert.equal(flipped.columns.V_LN_a_avg_V[0], plain.columns.V_LN_a_avg_V[0]);
  assert.equal(flipped.columns.I_a_avg_A[0], plain.columns.I_a_avg_A[0]);
});

test('parseTrendColumnarStream: identical to one-shot, bounded chunk reads', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'fluke-col-'));
  const path = join(dir, 'trend.bin');
  const COUNT = 20000;  // ~14.9 MB — forces multiple 8 MB chunks
  try {
    buildLargeTrend(path, COUNT);
    const buf = readFileSync(path);
    const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    const oneShot = parseTrendColumnar(ab, spec);
    assert.equal(oneShot.recordCount, COUNT);

    // Streaming source backed by the file — tracks the largest slice read so we
    // can assert no single read approaches the whole-file size.
    let maxSliceBytes = 0;
    let totalSliceBytes = 0;
    const source = {
      size: buf.length,
      readSlice: async (start, end) => {
        const sliceBytes = end - start;
        maxSliceBytes = Math.max(maxSliceBytes, sliceBytes);
        totalSliceBytes += sliceBytes;
        const sub = buf.subarray(start, end);
        return sub.buffer.slice(sub.byteOffset, sub.byteOffset + sub.byteLength);
      },
    };
    let lastDone = 0;
    const streamed = await parseTrendColumnarStream(source, spec, {
      chunkBytes: 8 * 1024 * 1024,
      onProgress: (done) => { lastDone = done; },
    });
    assert.equal(streamed.recordCount, COUNT);
    assert.equal(lastDone, COUNT);
    // No chunk read more than ~8 MB + one record (record-aligned rounding).
    assert.ok(maxSliceBytes <= 8 * 1024 * 1024 + spec.record_size,
      `max slice ${maxSliceBytes} should stay near the 8 MB chunk size`);
    assert.ok(maxSliceBytes < buf.length,
      'streaming must never read the whole file in one slice');
    // Every byte read exactly once.
    assert.equal(totalSliceBytes, COUNT * spec.record_size);
    // Columns identical to the one-shot decode.
    for (const name of STORE_COLUMNS) {
      assert.deepEqual(streamed.columns[name], oneShot.columns[name],
        `column ${name} mismatch between streamed and one-shot`);
    }
    assert.deepEqual(streamed.startMs, oneShot.startMs);
    assert.deepEqual(streamed.endMs, oneShot.endMs);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('ColumnStore.fromRecords / toRecords round-trips retained channels', () => {
  const ab = loadFixture();
  const { records } = parseTrendBin(ab, spec);
  const store = ColumnStore.fromRecords(records, spec);
  assert.equal(store.n, records.length);
  const nameToIdx = new Map(spec.fields.map((f) => [f.name, f.index]));
  const fi = nameToIdx.get('P_total_avg_W');
  for (let i = 0; i < store.n; i++) {
    assert.equal(store.col('P_total_avg_W')[i], records[i].floats[fi]);
  }
  const back = store.toRecords(spec);
  assert.equal(back.length, records.length);
  assert.equal(back[3].floats[fi], records[3].floats[fi]);
  assert.equal(back[3].startMs, records[3].startMs);
});

test('analysis engines: ColumnStore path == records path', () => {
  // Build records with planted events, then a store from those records, and
  // assert detectEvents / pickSnapshots / analyzeInsights agree.
  const nameToIdx = new Map(spec.fields.map((f) => [f.name, f.index]));
  const baseMs = Date.UTC(2024, 0, 13, 22, 0, 0);
  const N = 400;
  const records = [];
  for (let n = 0; n < N; n++) {
    const floats = new Float32Array(spec.data_floats);
    for (const ph of ['a', 'b', 'c']) {
      for (const st of ['min', 'max', 'avg']) {
        floats[nameToIdx.get(`V_LN_${ph}_${st}_V`)] = 277.0;
        floats[nameToIdx.get(`I_${ph}_${st}_A`)] = 100.0;
      }
    }
    floats[nameToIdx.get('freq_avg_Hz')] = 60.0;
    floats[nameToIdx.get('P_total_avg_W')] = 50000.0;
    floats[nameToIdx.get('PF_total_avg')] = 0.99;
    // Plant an outage 100..119 and a dip 200..204.
    if (n >= 100 && n <= 119) {
      for (const ph of ['a', 'b', 'c']) {
        for (const st of ['min', 'max', 'avg']) floats[nameToIdx.get(`V_LN_${ph}_${st}_V`)] = 0;
      }
    }
    if (n >= 200 && n <= 204) floats[nameToIdx.get('V_LN_a_min_V')] = 200.0;
    records.push({ index: n, startMs: baseMs + n * 1000, endMs: baseMs + (n + 1) * 1000, floats });
  }
  const store = ColumnStore.fromRecords(records, spec);

  const evRec = detectEvents(records, spec, { nominalLnV: 277.0 });
  const evStore = detectEvents(store, spec, { nominalLnV: 277.0 });
  assert.deepEqual(evStore, evRec, 'events differ between store and records path');

  const snRec = pickSnapshots(records, evRec, spec, { n: 2, windowSecs: 60, minSeparationSecs: 1 });
  const snStore = pickSnapshots(store, evStore, spec, { n: 2, windowSecs: 60, minSeparationSecs: 1 });
  assert.deepEqual(snStore, snRec, 'snapshots differ');

  const inRec = analyzeInsights(records, evRec, spec);
  const inStore = analyzeInsights(store, evStore, spec);
  assert.deepEqual(inStore.map((f) => f.kind), inRec.map((f) => f.kind), 'insights differ');
});

test('ColumnStore.fromTransfer wraps a worker payload', () => {
  const ab = loadFixture();
  const payload = parseTrendColumnar(ab, spec);
  const store = ColumnStore.fromTransfer(payload);
  assert.equal(store.n, payload.recordCount);
  assert.equal(store.col('V_LN_a_avg_V')[0], payload.columns.V_LN_a_avg_V[0]);
  assert.equal(store.firstStartMs, payload.startMs[0]);
});
