"""Pick "normal operation" snapshots — quiet windows useful as baseline plots.

A snapshot is a 5-minute window where total active power is at its calmest
(low rolling stdev) and which does not overlap any detected event. Useful for
showing what the system looks like *between* anomalies.

The rolling-stdev pass is O(N) via prefix sums of P and P², so it scales to
week-long sessions. ``pick_snapshots`` accepts either a
:class:`~fluke_3540.store.ColumnStore` (the production path) or an iterable of
:class:`~fluke_3540.parser.Record` (small fixtures / legacy callers).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .events import Event
from .store import ColumnStore


@dataclass(frozen=True)
class Snapshot:
    id: int
    t_start: dt.datetime
    t_end: dt.datetime
    t_center: dt.datetime
    p_total_mean_w: float
    p_total_stdev_w: float


def _prefix_sums(values: Sequence[float]) -> tuple[list[float], list[float]]:
    """Return (prefix_sum, prefix_sum_of_squares) with a leading 0.

    ``ps[k]`` is the sum of values[:k]; ``ps2[k]`` the sum of squares of
    values[:k]. The sum over [lo, hi) is ``ps[hi] - ps[lo]``.
    """
    n = len(values)
    ps = [0.0] * (n + 1)
    ps2 = [0.0] * (n + 1)
    s = 0.0
    s2 = 0.0
    for i, v in enumerate(values):
        s += v
        s2 += v * v
        ps[i + 1] = s
        ps2[i + 1] = s2
    return ps, ps2


def _window_mean_stdev(ps: list[float], ps2: list[float],
                       lo: int, hi: int) -> tuple[float, float]:
    """Population mean and stdev over values[lo:hi] from prefix sums."""
    count = hi - lo
    if count <= 0:
        return 0.0, 0.0
    total = ps[hi] - ps[lo]
    total_sq = ps2[hi] - ps2[lo]
    mean = total / count
    var = total_sq / count - mean * mean
    if var < 0.0:  # tiny negatives from float cancellation
        var = 0.0
    return mean, math.sqrt(var)


def _rolling_stdev(values: Sequence[float], window: int) -> list[float | None]:
    """Right-aligned rolling population stdev via prefix sums (O(N)).

    First ``window-1`` entries are None, matching the original semantics.
    """
    out: list[float | None] = [None] * len(values)
    n = len(values)
    if window <= 1 or window > n:
        return out
    ps, ps2 = _prefix_sums(values)
    for i in range(window - 1, n):
        _, sd = _window_mean_stdev(ps, ps2, i - window + 1, i + 1)
        out[i] = sd
    return out


def _as_store(records_or_store) -> ColumnStore:
    if isinstance(records_or_store, ColumnStore):
        return records_or_store
    return ColumnStore.from_records(records_or_store)


def pick_snapshots(
    records_or_store: Iterable | ColumnStore,
    events: Iterable[Event],
    n: int = 3,
    window_secs: int = 300,
    min_separation_secs: int = 3600,
) -> list[Snapshot]:
    """Pick up to N snapshots: low-stdev windows that don't overlap events.

    Snapshots span ``window_secs`` records ending at the chosen index.
    Consecutive picks are separated by ≥ ``min_separation_secs``.

    Accepts either a ColumnStore or an iterable of Records.
    """
    store = _as_store(records_or_store)
    nrec = store.n
    if nrec == 0:
        return []
    p = store.col("P_total_avg_W")
    rolling = _rolling_stdev(p, window_secs)
    times = list(store.iter_times())
    end_times = list(store.iter_end_times())

    ps, _ps2 = _prefix_sums(p)  # for window means

    event_intervals = [(ev.t_start, ev.t_end) for ev in events]

    def overlaps_event(i: int) -> bool:
        if rolling[i] is None:
            return True
        win_start = times[i - window_secs + 1]
        win_end = end_times[i]
        for es, ee in event_intervals:
            if not (win_end < es or win_start > ee):
                return True
        return False

    candidates = [
        (rolling[i], i) for i in range(nrec)
        if rolling[i] is not None and not overlaps_event(i)
    ]
    candidates.sort(key=lambda x: x[0])  # lowest stdev first

    picked: list[Snapshot] = []
    used_times: list[dt.datetime] = []
    for stdev_val, i in candidates:
        win_start = times[i - window_secs + 1]
        win_end = end_times[i]
        center = win_start + (win_end - win_start) / 2
        if any(
            abs((center - prev).total_seconds()) < min_separation_secs
            for prev in used_times
        ):
            continue
        lo = i - window_secs + 1
        win_mean = (ps[i + 1] - ps[lo]) / window_secs
        picked.append(Snapshot(
            id=len(picked),
            t_start=win_start, t_end=win_end, t_center=center,
            p_total_mean_w=win_mean,
            p_total_stdev_w=stdev_val,
        ))
        used_times.append(center)
        if len(picked) >= n:
            break

    return picked
