// Per-event annotations stored in localStorage.
//
// Key shape: "note:<file-hash>:<event-id>". File hash makes the namespace
// per-session so notes survive cache hits and don't leak between sessions.

const PREFIX = 'note:';

export function noteKey(fileHash, eventId) {
  return `${PREFIX}${fileHash || 'unknown'}:${eventId}`;
}

export function getNote(fileHash, eventId) {
  try {
    return localStorage.getItem(noteKey(fileHash, eventId)) ?? '';
  } catch (_) { return ''; }
}

export function setNote(fileHash, eventId, text) {
  try {
    const t = (text ?? '').trim();
    if (t) localStorage.setItem(noteKey(fileHash, eventId), t);
    else localStorage.removeItem(noteKey(fileHash, eventId));
  } catch (_) {/* quota etc. */}
}

export function getAllNotes(fileHash) {
  const out = {};
  if (!fileHash) return out;
  const prefix = `${PREFIX}${fileHash}:`;
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(prefix)) {
        const id = parseInt(k.slice(prefix.length), 10);
        if (Number.isFinite(id)) out[id] = localStorage.getItem(k) ?? '';
      }
    }
  } catch (_) {/* ignore */}
  return out;
}

export function clearAllNotes(fileHash) {
  if (!fileHash) return;
  const prefix = `${PREFIX}${fileHash}:`;
  try {
    const keys = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(prefix)) keys.push(k);
    }
    for (const k of keys) localStorage.removeItem(k);
  } catch (_) {/* ignore */}
}

export function exportNotesJson(fileHash, events) {
  const notes = getAllNotes(fileHash);
  // Build a self-describing export including the events the notes refer to.
  return JSON.stringify({
    fileHash,
    exportedAt: new Date().toISOString(),
    notes: events
      .filter((ev) => notes[ev.id])
      .map((ev) => ({
        id: ev.id,
        kind: ev.kind,
        tStartUtc: new Date(ev.tStartMs).toISOString(),
        note: notes[ev.id],
      })),
  }, null, 2);
}
