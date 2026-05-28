// Web Worker wrapper around parser.js. Keeps the 55 MB parse off the main
// thread so the UI stays responsive.
//
// Message protocol:
//   in:  { type: 'parse', spec, arrayBuffer, reverseCts }
//   out: { type: 'progress', done, total }            (~once per 1000 records)
//   out: { type: 'done', records, recordCount }
//   out: { type: 'error', message, stack }

import { parseTrendBin } from './parser.js';

self.onmessage = (event) => {
  const msg = event.data;
  if (msg?.type !== 'parse') {
    self.postMessage({
      type: 'error',
      message: `Unknown message type: ${msg?.type}`,
    });
    return;
  }
  try {
    const { spec, arrayBuffer, reverseCts = false } = msg;
    const result = parseTrendBin(arrayBuffer, spec, {
      reverseCts,
      onProgress: (done, total) => {
        self.postMessage({ type: 'progress', done, total });
      },
    });
    // Transfer the underlying Float32Array buffers back to main thread
    // (records are kept structured-cloneable; no transferable wrapping for now
    // since structured cloning is fast enough for ~75 K records).
    self.postMessage({
      type: 'done',
      records: result.records,
      recordCount: result.records.length,
    });
  } catch (err) {
    self.postMessage({
      type: 'error',
      message: err?.message ?? String(err),
      stack: err?.stack ?? null,
    });
  }
};
