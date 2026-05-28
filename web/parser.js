// Fluke 3540 FC trend.bin parser — JavaScript port.
//
// Mirrors python/src/fluke_3540/parser.py exactly. Both implementations load
// the field map from spec/field_map.json so they stay in lockstep.
//
// Usage (browser):
//   import { parseTrendBin, loadSpec } from './parser.js';
//   const spec = await loadSpec('./spec/field_map.json');
//   const { header, records } = parseTrendBin(arrayBuffer, spec);
//
// Usage (Node test):
//   import { readFileSync } from 'node:fs';
//   const spec = JSON.parse(readFileSync('spec/field_map.json', 'utf8'));
//   const buf = readFileSync('trend.bin');
//   const { records } = parseTrendBin(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength), spec);

export class FlukeBinaryError extends Error {}

// FILETIME (100-ns ticks since 1601-01-01 UTC) → JS Date in unix milliseconds.
// Constant = number of ms between 1601-01-01 and 1970-01-01 UTC.
export const FILETIME_EPOCH_DIFF_MS = 11644473600000n;
export const TICKS_PER_MS = 10000n;

export function filetimeToUnixMs(filetimeBigInt) {
  const ms = filetimeBigInt / TICKS_PER_MS - FILETIME_EPOCH_DIFF_MS;
  return Number(ms);
}

export function filetimeToDate(filetimeBigInt) {
  return new Date(filetimeToUnixMs(filetimeBigInt));
}

/**
 * Validate and pre-compute lookup tables from spec/field_map.json.
 * @param {object} spec - Parsed spec content.
 * @returns {{recordMagic: Uint8Array, recordSize: number, headerBytes: number, dataFloats: number, fields: Array, reverseCtsIndices: Set<number>}}
 */
export function buildIndex(spec) {
  const recordMagic = new Uint8Array(spec.record_magic);
  const fields = spec.fields.slice();
  const seenIndices = new Set();
  for (const f of fields) {
    if (seenIndices.has(f.index)) {
      throw new FlukeBinaryError(`spec has duplicate field index: ${f.index}`);
    }
    if (f.index < 0 || f.index >= spec.data_floats) {
      throw new FlukeBinaryError(`spec has out-of-range field index: ${f.index}`);
    }
    seenIndices.add(f.index);
  }
  const reverseCtsIndices = new Set(
    fields
      .filter((f) => spec.reverse_cts_prefixes.some((p) => f.name.startsWith(p)))
      .map((f) => f.index)
  );
  return {
    recordMagic,
    recordSize: spec.record_size,
    headerBytes: spec.header_bytes,
    dataFloats: spec.data_floats,
    fields,
    reverseCtsIndices,
  };
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

/**
 * Parse a trend.bin ArrayBuffer.
 * @param {ArrayBuffer} arrayBuffer
 * @param {object} spec - Parsed spec/field_map.json content.
 * @param {{reverseCts?: boolean, onProgress?: (done:number, total:number)=>void}} [opts]
 * @returns {{records: Array<{index:number, startMs:number, endMs:number, floats:Float32Array}>}}
 */
export function parseTrendBin(arrayBuffer, spec, opts = {}) {
  const { reverseCts = false, onProgress = null } = opts;
  const idx = buildIndex(spec);
  const view = new DataView(arrayBuffer);
  const totalBytes = view.byteLength;
  const totalRecords = Math.floor(totalBytes / idx.recordSize);
  const records = new Array(totalRecords);
  const magicBytes = idx.recordMagic;

  for (let r = 0; r < totalRecords; r++) {
    const offset = r * idx.recordSize;
    // Magic check
    for (let m = 0; m < magicBytes.length; m++) {
      if (view.getUint8(offset + m) !== magicBytes[m]) {
        throw new FlukeBinaryError(
          `Bad magic at record ${r} (offset 0x${offset.toString(16)})`
        );
      }
    }
    // Header: magic(4) | start_filetime_hi(4) | start_filetime_lo(4)
    //                  | end_filetime_hi(4)   | end_filetime_lo(4)   | reserved(4)
    // FLUKE writes BOTH halves as little-endian uint32s, BUT in Python we
    // reconstruct as (hi << 32) | lo. That matches a little-endian uint64
    // read of the 8-byte block ONLY if the two uint32s are stored as
    // (lo_word, hi_word). Check carefully: the original code does:
    //   start_hi, start_lo = struct.unpack_from("<II", chunk, 4)
    // which unpacks two LE-uint32s as (lo_word_in_memory, next_word_in_memory).
    // So in memory the layout is [hi_word_LE][lo_word_LE], where
    // hi_word_LE = first uint32 (becomes start_hi), lo_word_LE = second (start_lo).
    // Final value = (hi << 32) | lo. To match this, we read the same way:
    const startHi = BigInt(view.getUint32(offset + 4, true));
    const startLo = BigInt(view.getUint32(offset + 8, true));
    const endHi = BigInt(view.getUint32(offset + 12, true));
    const endLo = BigInt(view.getUint32(offset + 16, true));
    const startFt = (startHi << 32n) | startLo;
    const endFt = (endHi << 32n) | endLo;
    const floats = new Float32Array(idx.dataFloats);
    const floatsBase = offset + idx.headerBytes;
    for (let i = 0; i < idx.dataFloats; i++) {
      let v = view.getFloat32(floatsBase + i * 4, true);
      if (reverseCts && idx.reverseCtsIndices.has(i)) {
        v = -v;
      }
      floats[i] = v;
    }
    records[r] = {
      index: r,
      startMs: filetimeToUnixMs(startFt),
      endMs: filetimeToUnixMs(endFt),
      floats,
    };
    if (onProgress && (r % 1000 === 0 || r === totalRecords - 1)) {
      onProgress(r + 1, totalRecords);
    }
  }
  return { records };
}

/**
 * Convert a {records} parse result to a labelled-row generator (one object per
 * record, keyed by field name). Convenience for downstream code; the raw
 * Float32Array form is faster if you only need a few columns.
 */
export function* rowsByName(parseResult, spec) {
  const idx = buildIndex(spec);
  for (const rec of parseResult.records) {
    const row = {
      record_index: rec.index,
      timestamp_utc: new Date(rec.startMs).toISOString(),
      window_end_utc: new Date(rec.endMs).toISOString(),
    };
    for (const f of idx.fields) {
      row[f.name] = rec.floats[f.index];
    }
    yield row;
  }
}

/**
 * Browser convenience: fetch + parse the spec JSON.
 */
export async function loadSpec(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new FlukeBinaryError(`Failed to fetch spec from ${url}: ${resp.status}`);
  }
  return resp.json();
}
