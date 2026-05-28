"""Pick "normal operation" snapshots — quiet windows useful as baseline plots.

A snapshot is a 5-minute window where total active power is at its calmest
(low rolling stdev) and which does not overlap any detected event. Useful for
showing what the system looks like *between* anomalies.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import pstdev, mean
from typing import Iterable, Sequence

from .events import Event
from .parser import FIELDS, Record


_P_TOTAL_AVG = next(f.index for f in FIELDS if f.name == "P_total_avg_W")


@dataclass(frozen=True)
class Snapshot:
    id: int
    t_start: dt.datetime
    t_end: dt.datetime
    t_center: dt.datetime
    p_total_mean_w: float
    p_total_stdev_w: float


def _rolling_stdev(values: Sequence[float], window: int) -> list[float | None]:
    """Right-aligned rolling pstdev. First `window-1` entries are None."""
    out: list[float | None] = [None] * len(values)
    if window <= 1 or window > len(values):
        return out
    for i in range(window - 1, len(values)):
        out[i] = pstdev(values[i - window + 1:i + 1])
    return out


def pick_snapshots(
    records: Iterable[Record],
    events: Iterable[Event],
    n: int = 3,
    window_secs: int = 300,
    min_separation_secs: int = 3600,
) -> list[Snapshot]:
    """Pick up to N snapshots: low-stdev windows that don't overlap events.

    Snapshots are centered on the chosen index and span `window_secs` seconds.
    Consecutive picks are separated by ≥ min_separation_secs.
    """
    recs = list(records)
    if not recs:
        return []
    p = [r.floats[_P_TOTAL_AVG] for r in recs]
    rolling = _rolling_stdev(p, window_secs)

    # Mask out windows overlapping events. Each event covers [t_start, t_end].
    # A rolling-stdev value at index i summarises [i-window+1, i] in record
    # indices, which corresponds to [recs[i-window+1].start, recs[i].end] wall time.
    event_intervals = [(ev.t_start, ev.t_end) for ev in events]

    def overlaps_event(i: int) -> bool:
        if rolling[i] is None:
            return True
        win_start = recs[i - window_secs + 1].start
        win_end = recs[i].end
        for es, ee in event_intervals:
            if not (win_end < es or win_start > ee):
                return True
        return False

    candidates = [
        (rolling[i], i) for i in range(len(recs))
        if rolling[i] is not None and not overlaps_event(i)
    ]
    candidates.sort(key=lambda x: x[0])  # lowest stdev first

    picked: list[Snapshot] = []
    used_times: list[dt.datetime] = []
    for stdev_val, i in candidates:
        win_start = recs[i - window_secs + 1].start
        win_end = recs[i].end
        center = win_start + (win_end - win_start) / 2
        if any(
            abs((center - prev).total_seconds()) < min_separation_secs
            for prev in used_times
        ):
            continue
        window_p = p[i - window_secs + 1:i + 1]
        picked.append(Snapshot(
            id=len(picked),
            t_start=win_start, t_end=win_end, t_center=center,
            p_total_mean_w=mean(window_p),
            p_total_stdev_w=stdev_val,
        ))
        used_times.append(center)
        if len(picked) >= n:
            break

    return picked
