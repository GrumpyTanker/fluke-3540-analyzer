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

const VALID_PHASES = new Set(['a', 'b', 'c']);

/**
 * Extract the phase letter from a reverse-CTS-eligible field name.
 * `P_a_avg_W` → 'a', `P_total_avg_W` → 'total', `V_LN_a_avg_V` → null.
 */
function fieldPhase(spec, name) {
  if (!spec.reverse_cts_prefixes.some((p) => name.startsWith(p))) return null;
  const parts = name.split('_');
  return parts.length >= 2 ? parts[1] : null;
}

/**
 * Compute the set of field indices whose sign should be flipped.
 * @param {object} spec
 * @param {boolean|string[]} phases - true = all, false/[] = none,
 *                                    iterable of {'a','b','c'} = those phases (+ totals)
 */
export function computeReverseCtsIndices(spec, phases = true) {
  const out = new Set();
  if (phases === true) {
    for (const f of spec.fields) {
      if (spec.reverse_cts_prefixes.some((p) => f.name.startsWith(p))) out.add(f.index);
    }
    return out;
  }
  if (!phases) return out;
  if (!Array.isArray(phases) && typeof phases[Symbol.iterator] !== 'function') {
    throw new FlukeBinaryError(`reverseCts must be a boolean or iterable of phases, got ${phases}`);
  }
  const phaseSet = new Set([...phases].map((p) => String(p).trim().toLowerCase()));
  for (const p of phaseSet) {
    if (!VALID_PHASES.has(p)) {
      throw new FlukeBinaryError(`Invalid reverse-CTS phase: ${p}. Use a/b/c only.`);
    }
  }
  if (phaseSet.size === 0) return out;
  for (const f of spec.fields) {
    const ph = fieldPhase(spec, f.name);
    if (ph === null) continue;
    if (phaseSet.has(ph)) out.add(f.index);
    else if (ph === 'total') out.add(f.index);
  }
  return out;
}

/**
 * Validate and pre-compute lookup tables from spec/field_map.json.
 * @param {object} spec - Parsed spec content.
 * @param {object} [opts]
 * @param {boolean|string[]} [opts.reverseCts] - which phases to flip (default true = all)
 * @returns {{recordMagic: Uint8Array, recordSize: number, headerBytes: number, dataFloats: number, fields: Array, reverseCtsIndices: Set<number>}}
 */
export function buildIndex(spec, opts = {}) {
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
  const reverseCtsIndices = computeReverseCtsIndices(
    spec, opts.reverseCts !== undefined ? opts.reverseCts : true
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
  // The reverseCts indices depend on which phases the caller wants flipped,
  // so we build the index with the resolved set up-front.
  const idx = buildIndex(spec, { reverseCts });
  const flip = reverseCts ? idx.reverseCtsIndices : null;
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
    // FLUKE writes both halves as little-endian uint32s; the FIRST word in
    // memory is the high 32 bits, SECOND is the low. Reconstruct with
    // (hi << 32) | lo to match Python's struct.unpack_from('<II', ...).
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
      if (flip && flip.has(i)) {
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

// --- Streaming columnar parse (Feature A: ~1.6 GB → ~55 MB) ----------------
//
// Instead of holding one 180-float record object per second, decode straight
// into packed Float32Array columns (only the analysis channels) + Float64Array
// timestamp arrays. The full ArrayBuffer is processed record-aligned; callers
// that stream a Blob (parser_worker.js) feed chunks here so the whole 438 MB
// is never resident at once.

import { STORE_COLUMNS, resolveStoreIndices } from './column_store.js';

/**
 * Allocate the column arrays for a columnar parse of `recordCount` records.
 * @param {number} recordCount
 * @returns {{columns: Object<string,Float32Array>, startMs: Float64Array, endMs: Float64Array}}
 */
export function allocColumns(recordCount) {
  const columns = {};
  for (const name of STORE_COLUMNS) columns[name] = new Float32Array(recordCount);
  return {
    columns,
    startMs: new Float64Array(recordCount),
    endMs: new Float64Array(recordCount),
  };
}

/**
 * Decode a record-aligned slice of `arrayBuffer` into pre-allocated column
 * arrays, starting at output record index `outBase`. Used by both the one-shot
 * and the chunked-streaming columnar parsers.
 *
 * @param {ArrayBuffer} arrayBuffer a buffer whose length is a multiple of recordSize
 * @param {object} idx buildIndex() result
 * @param {{columns, startMs, endMs}} sink pre-allocated output
 * @param {Map<string,number>} storeIdx STORE_COLUMNS name -> float index
 * @param {Set<number>|null} flip reverse-CT indices to negate, or null
 * @param {number} outBase output record offset
 * @returns {number} number of records decoded from this slice
 */
function decodeColumnarSlice(arrayBuffer, idx, sink, storeIdx, flip, outBase) {
  const view = new DataView(arrayBuffer);
  const recs = Math.floor(view.byteLength / idx.recordSize);
  const magic = idx.recordMagic;
  // Pre-resolve the (column-array, floatIndex, flipFlag) tuples once.
  const plan = STORE_COLUMNS.map((name) => {
    const fi = storeIdx.get(name);
    return [sink.columns[name], fi, flip ? flip.has(fi) : false];
  });
  for (let r = 0; r < recs; r++) {
    const offset = r * idx.recordSize;
    for (let m = 0; m < magic.length; m++) {
      if (view.getUint8(offset + m) !== magic[m]) {
        throw new FlukeBinaryError(
          `Bad magic at record ${outBase + r} (offset 0x${offset.toString(16)})`
        );
      }
    }
    const startHi = BigInt(view.getUint32(offset + 4, true));
    const startLo = BigInt(view.getUint32(offset + 8, true));
    const endHi = BigInt(view.getUint32(offset + 12, true));
    const endLo = BigInt(view.getUint32(offset + 16, true));
    const out = outBase + r;
    sink.startMs[out] = filetimeToUnixMs((startHi << 32n) | startLo);
    sink.endMs[out] = filetimeToUnixMs((endHi << 32n) | endLo);
    const floatsBase = offset + idx.headerBytes;
    for (const [arr, fi, doFlip] of plan) {
      let v = view.getFloat32(floatsBase + fi * 4, true);
      if (doFlip) v = -v;
      arr[out] = v;
    }
  }
  return recs;
}

/**
 * One-shot columnar parse of a full trend.bin ArrayBuffer. Returns Transferable
 * typed-array columns instead of 590 K record objects.
 *
 * @param {ArrayBuffer} arrayBuffer
 * @param {object} spec
 * @param {{reverseCts?: boolean|string[], onProgress?: (done:number,total:number)=>void}} [opts]
 * @returns {{recordCount:number, columns:Object<string,Float32Array>, startMs:Float64Array, endMs:Float64Array}}
 */
export function parseTrendColumnar(arrayBuffer, spec, opts = {}) {
  const { reverseCts = false, onProgress = null } = opts;
  const idx = buildIndex(spec, { reverseCts });
  const flip = reverseCts ? idx.reverseCtsIndices : null;
  const storeIdx = resolveStoreIndices(spec);
  const total = Math.floor(arrayBuffer.byteLength / idx.recordSize);
  const sink = allocColumns(total);
  decodeColumnarSlice(arrayBuffer, idx, sink, storeIdx, flip, 0);
  if (onProgress) onProgress(total, total);
  return { recordCount: total, ...sink };
}

/**
 * Streaming columnar parse: read a Blob/File in record-aligned chunks so the
 * full ArrayBuffer is never resident. `readSlice(start, end)` must return a
 * Promise<ArrayBuffer> for the byte range [start, end) — in the browser that's
 * `blob.slice(start, end).arrayBuffer()`.
 *
 * @param {{size:number, readSlice:(start:number,end:number)=>Promise<ArrayBuffer>}} source
 * @param {object} spec
 * @param {{reverseCts?: boolean|string[], chunkBytes?: number, onProgress?: (done:number,total:number)=>void}} [opts]
 * @returns {Promise<{recordCount:number, columns, startMs, endMs}>}
 */
export async function parseTrendColumnarStream(source, spec, opts = {}) {
  const { reverseCts = false, chunkBytes = 8 * 1024 * 1024, onProgress = null } = opts;
  const idx = buildIndex(spec, { reverseCts });
  const flip = reverseCts ? idx.reverseCtsIndices : null;
  const storeIdx = resolveStoreIndices(spec);
  const recordSize = idx.recordSize;
  const total = Math.floor(source.size / recordSize);
  const sink = allocColumns(total);
  // Round the chunk down to a whole number of records so slices stay aligned.
  const recsPerChunk = Math.max(1, Math.floor(chunkBytes / recordSize));
  const bytesPerChunk = recsPerChunk * recordSize;
  let done = 0;
  for (let start = 0; done < total; start += bytesPerChunk) {
    const remaining = total - done;
    const recsThisChunk = Math.min(recsPerChunk, remaining);
    const end = start + recsThisChunk * recordSize;
    const buf = await source.readSlice(start, end);
    decodeColumnarSlice(buf, idx, sink, storeIdx, flip, done);
    done += recsThisChunk;
    if (onProgress) onProgress(done, total);
  }
  return { recordCount: total, ...sink };
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
