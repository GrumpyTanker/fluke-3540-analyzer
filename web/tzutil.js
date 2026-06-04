// Timezone-aware reporting helpers (Feature H) — JS companion to
// python/.../tzutil.py. Timestamps flow as UTC epoch ms; when a tz is set,
// reports render local + UTC. Default (tz=null) is UTC only.

/**
 * ISO-8601 with the local offset for `ms` in IANA `tzName`. Mirrors Python's
 * datetime.astimezone(ZoneInfo(tz)).isoformat() — including the ±HH:MM offset.
 */
export function isoInZone(ms, tzName) {
  const d = new Date(ms);
  // Use Intl to get the wall-clock parts in the target zone.
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: tzName,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  });
  const parts = {};
  for (const p of fmt.formatToParts(d)) parts[p.type] = p.value;
  let hour = parts.hour === '24' ? '00' : parts.hour;
  const wall = `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}:${parts.second}`;
  // Offset = (wall-as-UTC) - actual-UTC, in minutes.
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day,
                         +hour, +parts.minute, +parts.second);
  const offsetMin = Math.round((asUtc - d.getTime()) / 60000);
  const sign = offsetMin >= 0 ? '+' : '-';
  const abs = Math.abs(offsetMin);
  const oh = String(Math.floor(abs / 60)).padStart(2, '0');
  const om = String(abs % 60).padStart(2, '0');
  return `${wall}${sign}${oh}:${om}`;
}

/** UTC ISO with explicit +00:00 (matches Python isoformat on a UTC datetime).
 *  Python omits the fractional part when it is zero; we mirror that. */
export function isoUtc(ms) {
  return new Date(ms).toISOString()
    .replace(/\.000Z$/, 'Z')   // drop zero-millisecond fraction (Python parity)
    .replace('Z', '+00:00');
}

/**
 * Render 'LOCAL (UTC)' when tzName is set, else just UTC ISO.
 * @param {number} ms epoch milliseconds (UTC)
 * @param {string|null} tzName IANA zone or null/'UTC'
 */
export function formatLocalUtc(ms, tzName) {
  if (!tzName || tzName.toUpperCase() === 'UTC') return isoUtc(ms);
  return `${isoInZone(ms, tzName)} (${isoUtc(ms)})`;
}

export function tzLabel(tzName) {
  return tzName ? tzName : 'UTC';
}
