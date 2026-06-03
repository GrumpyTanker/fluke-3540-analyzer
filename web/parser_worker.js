// Web Worker wrapper around parser.js. Keeps the parse off the main thread so
// the UI stays responsive.
//
// Message protocol:
//   in:  { type: 'parse', spec, arrayBuffer, reverseCts }       (legacy small-file)
//   in:  { type: 'parse-stream', spec, blob, reverseCts }       (Feature A: streaming columnar)
//   out: { type: 'progress', done, total }
//   out: { type: 'done', records, recordCount }                 (legacy record objects)
//   out: { type: 'done-columnar', recordCount, columns, startMs, endMs }
//   out: { type: 'error', message, stack }

import { parseTrendBin, parseTrendColumnar, parseTrendColumnarStream } from './parser.js';
import { STORE_COLUMNS } from './column_store.js';

// Collect the Transferable typed-array buffers from a columnar payload so they
// move (zero-copy) back to the main thread instead of being structured-cloned.
function columnarTransferList(payload) {
  const list = [payload.startMs.buffer, payload.endMs.buffer];
  for (const name of STORE_COLUMNS) {
    const c = payload.columns[name];
    if (c && c.buffer) list.push(c.buffer);
  }
  return list;
}

self.onmessage = async (event) => {
  const msg = event.data;
  try {
    if (msg?.type === 'parse-stream') {
      const { spec, blob, reverseCts = false } = msg;
      const source = {
        size: blob.size,
        readSlice: (start, end) => blob.slice(start, end).arrayBuffer(),
      };
      const payload = await parseTrendColumnarStream(source, spec, {
        reverseCts,
        onProgress: (done, total) => {
          self.postMessage({ type: 'progress', done, total });
        },
      });
      self.postMessage({ type: 'done-columnar', ...payload },
                       columnarTransferList(payload));
      return;
    }
    if (msg?.type === 'parse') {
      const { spec, arrayBuffer, reverseCts = false, columnar = false } = msg;
      if (columnar) {
        const payload = parseTrendColumnar(arrayBuffer, spec, {
          reverseCts,
          onProgress: (done, total) => {
            self.postMessage({ type: 'progress', done, total });
          },
        });
        self.postMessage({ type: 'done-columnar', ...payload },
                         columnarTransferList(payload));
        return;
      }
      const result = parseTrendBin(arrayBuffer, spec, {
        reverseCts,
        onProgress: (done, total) => {
          self.postMessage({ type: 'progress', done, total });
        },
      });
      self.postMessage({
        type: 'done',
        records: result.records,
        recordCount: result.records.length,
      });
      return;
    }
    self.postMessage({ type: 'error', message: `Unknown message type: ${msg?.type}` });
  } catch (err) {
    self.postMessage({
      type: 'error',
      message: err?.message ?? String(err),
      stack: err?.stack ?? null,
    });
  }
};
