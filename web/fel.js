// .fel zip-bundle support — Fluke's raw session export wraps the ES.NNN/
// folder in a zip. We unpack it in memory using JSZip (already vendored).
//
// Browser: pass window.JSZip explicitly OR rely on the global.
// Node tests: load vendor/jszip.min.js into a vm context, then pass JSZip.

export class FelError extends Error {}

/**
 * Unpack a .fel zip-bundle.
 * @param {ArrayBuffer} arrayBuffer
 * @param {object} [jszipCtor] - JSZip class. Defaults to globalThis.JSZip.
 * @returns {Promise<{trendBuffer: ArrayBuffer, config: object|null}>}
 */
export async function unpackFel(arrayBuffer, jszipCtor) {
  const JSZip = jszipCtor || globalThis.JSZip;
  if (!JSZip) {
    throw new FelError('JSZip not available. Ensure vendor/jszip.min.js is loaded.');
  }
  const zip = await JSZip.loadAsync(arrayBuffer);
  let trendEntry = null;
  let configEntry = null;
  for (const [name, entry] of Object.entries(zip.files)) {
    if (entry.dir) continue;
    const base = name.split('/').pop().toLowerCase();
    if (base === 'trend.bin') trendEntry = entry;
    else if (base.endsWith('-config.json')) configEntry = entry;
  }
  if (!trendEntry) {
    throw new FelError('No trend.bin found inside the .fel archive.');
  }
  // JSZip's blob output works in both browser and Node 18+
  const trendBlob = await trendEntry.async('blob');
  const trendBuffer = await trendBlob.arrayBuffer();
  let config = null;
  if (configEntry) {
    try {
      config = JSON.parse(await configEntry.async('text'));
    } catch (_) {
      // Non-fatal: continue without config; the user just won't see asset name.
    }
  }
  return { trendBuffer, config };
}

export function looksLikeFel(file) {
  return typeof file?.name === 'string' && file.name.toLowerCase().endsWith('.fel');
}
