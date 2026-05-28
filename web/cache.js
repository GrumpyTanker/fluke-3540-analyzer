// IndexedDB-backed session cache. After parsing a trend.bin, we store the
// resulting records keyed by a SHA-256 of the file bytes so a subsequent
// drop of the same file can skip the parse.
//
// Records contain Float32Arrays which structured-clone natively, so we can
// store the parsed array as-is. No serialisation cost.

const DB_NAME = 'fluke3540';
const STORE = 'sessions';
const DB_VERSION = 1;
const MAX_ENTRIES = 5;  // simple LRU bound: keep the 5 most-recently-used parses

function openDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') {
      reject(new Error('IndexedDB not available in this environment'));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const os = db.createObjectStore(STORE, { keyPath: 'hash' });
        os.createIndex('lastUsed', 'lastUsed');
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function hashBuffer(arrayBuffer) {
  const digest = await crypto.subtle.digest('SHA-256', arrayBuffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function getCached(hash) {
  try {
    const db = await openDb();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const req = tx.objectStore(STORE).get(hash);
      req.onsuccess = () => {
        const entry = req.result;
        if (!entry) { resolve(null); return; }
        // Touch last-used so LRU eviction keeps recently-hit entries.
        entry.lastUsed = Date.now();
        tx.objectStore(STORE).put(entry);
        resolve(entry);
      };
      req.onerror = () => reject(req.error);
    });
  } catch (e) {
    console.warn('Cache read failed:', e);
    return null;
  }
}

export async function putCached(hash, records, config) {
  try {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const store = tx.objectStore(STORE);
      store.put({ hash, records, config, lastUsed: Date.now() });
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    await evictLru();
  } catch (e) {
    console.warn('Cache write failed:', e);
  }
}

async function evictLru() {
  try {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const store = tx.objectStore(STORE);
      const req = store.index('lastUsed').openCursor();  // ascending = oldest first
      const stale = [];
      let total = 0;
      req.onsuccess = (e) => {
        const cursor = e.target.result;
        if (cursor) {
          total++;
          stale.push(cursor.primaryKey);
          cursor.continue();
        } else {
          // Stale = everything; trim to (total - MAX_ENTRIES) oldest.
          const toDelete = stale.slice(0, Math.max(0, total - MAX_ENTRIES));
          for (const k of toDelete) store.delete(k);
        }
      };
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } catch (e) {
    console.warn('Cache eviction failed:', e);
  }
}

export async function clearCache() {
  try {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).clear();
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    return true;
  } catch (e) {
    console.warn('Cache clear failed:', e);
    return false;
  }
}

export async function cacheStats() {
  try {
    const db = await openDb();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const req = tx.objectStore(STORE).count();
      req.onsuccess = () => resolve({ entries: req.result });
      req.onerror = () => reject(req.error);
    });
  } catch (e) {
    return { entries: 0, error: String(e) };
  }
}
