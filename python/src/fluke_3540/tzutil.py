"""Timezone-aware reporting helpers (Feature H).

Timestamps are stored/computed in UTC throughout the pipeline (the meter's
FILETIME is UTC, anchors are normalised to UTC). When the operator passes
``--tz ZONE`` (an IANA name like ``America/Chicago``), reports additionally
render the local wall-clock alongside UTC. Default behaviour (no ``--tz``) is
unchanged: UTC only.

Anchors (``--anchor-start`` / ``--anchor-end``) already accept ISO-8601 strings
with an explicit offset (e.g. ``2024-01-13T09:00:00-06:00``); that offset is
honoured by ``datetime.fromisoformat`` in cli._parse_time. ``--tz`` is the
display-side complement.
"""
from __future__ import annotations

import datetime as dt

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


def resolve_tz(name: str | None):
    """Return a tzinfo for ``name`` (IANA), or None for UTC/unset.

    Raises ValueError on an unknown zone so the CLI can report it cleanly.
    """
    if not name:
        return None
    if name.upper() == "UTC":
        return dt.timezone.utc
    if ZoneInfo is None:  # pragma: no cover
        raise ValueError("zoneinfo unavailable; --tz requires Python 3.9+")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as e:
        raise ValueError(f"Unknown timezone: {name!r}") from e


def to_utc(value: dt.datetime) -> dt.datetime:
    """Normalise a datetime to UTC (naive is assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def format_local_utc(value: dt.datetime, tz) -> str:
    """Render ``value`` as 'LOCAL (UTC)' when tz is set, else just UTC ISO.

    Example with tz=America/Chicago:
        '2024-01-13T09:00:00-06:00 (2024-01-13T15:00:00+00:00)'
    With tz=None:
        '2024-01-13T15:00:00+00:00'
    """
    utc = to_utc(value)
    if tz is None:
        return utc.isoformat()
    local = utc.astimezone(tz)
    return f"{local.isoformat()} ({utc.isoformat()})"


def tz_label(tz, name: str | None) -> str:
    """A short label for the configured zone, for report headers."""
    if tz is None:
        return "UTC"
    return name or "local"
