// Snapshot picking — port of python/src/fluke_3540/snapshots.py.
// Picks quiet, non-event windows by rolling stdev of P_total.

import { asColumnSource } from './column_source.js';

function pstdev(values, start, end) {
  if (end - start + 1 < 2) return 0;
  let s = 0;
  for (let i = start; i <= end; i++) s += values[i];
  const m = s / (end - start + 1);
  let sq = 0;
  for (let i = start; i <= end; i++) sq += (values[i] - m) * (values[i] - m);
  return Math.sqrt(sq / (end - start + 1));
}

function mean(values, start, end) {
  let s = 0;
  for (let i = start; i <= end; i++) s += values[i];
  return s / (end - start + 1);
}

/**
 * Pick up to N snapshot windows that are quiet (low P_total stdev) and don't
 * overlap any detected event.
 * @param {Array<{startMs:number, endMs:number, floats:Float32Array}>|import('./column_store.js').ColumnStore} source
 * @param {Array<{tStartMs:number, tEndMs:number}>} events
 * @param {object} spec
 * @param {{n?: number, windowSecs?: number, minSeparationSecs?: number}} [opts]
 */
export function pickSnapshots(source, events, spec, opts = {}) {
  const { n = 3, windowSecs = 300, minSeparationSecs = 3600 } = opts;
  const src = asColumnSource(source, spec);
  if (src.length === 0) return [];

  const p = src.column('P_total_avg_W');
  const N = src.length;

  if (windowSecs <= 1 || windowSecs > N) return [];

  // Rolling pstdev, right-aligned. Cheap to compute incrementally but the
  // straightforward window pass is fast enough for 75 K records.
  const rolling = new Array(N).fill(null);
  for (let i = windowSecs - 1; i < N; i++) {
    rolling[i] = pstdev(p, i - windowSecs + 1, i);
  }

  const eventIntervals = events.map((ev) => [ev.tStartMs, ev.tEndMs]);
  function overlapsEvent(i) {
    if (rolling[i] === null) return true;
    const winStart = src.startMs(i - windowSecs + 1);
    const winEnd = src.endMs(i);
    for (const [es, ee] of eventIntervals) {
      if (!(winEnd < es || winStart > ee)) return true;
    }
    return false;
  }

  const candidates = [];
  for (let i = 0; i < N; i++) {
    if (rolling[i] !== null && !overlapsEvent(i)) {
      candidates.push([rolling[i], i]);
    }
  }
  candidates.sort((a, b) => a[0] - b[0]);

  const picked = [];
  const usedCenters = [];
  for (const [stdevVal, i] of candidates) {
    const winStart = src.startMs(i - windowSecs + 1);
    const winEnd = src.endMs(i);
    const center = (winStart + winEnd) / 2;
    let tooClose = false;
    for (const prev of usedCenters) {
      if (Math.abs(center - prev) < minSeparationSecs * 1000) { tooClose = true; break; }
    }
    if (tooClose) continue;
    const meanP = mean(p, i - windowSecs + 1, i);
    picked.push({
      id: picked.length,
      tStartMs: winStart,
      tEndMs: winEnd,
      tCenterMs: center,
      pTotalMeanW: meanP,
      pTotalStdevW: stdevVal,
    });
    usedCenters.push(center);
    if (picked.length >= n) break;
  }
  return picked;
}
