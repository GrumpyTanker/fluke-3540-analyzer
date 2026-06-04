"""Multi-session stitching (Feature D).

Concatenate consecutive Fluke sessions into one continuous timeline so analysis
can run across the meter's 7-day capture cap. Sessions are ordered by start
time; where session N+1 does not abut session N (gap > tolerance) an explicit
gap record is noted in the provenance so downstream consumers can see it.

The stitched result is a single :class:`~fluke_3540.store.ColumnStore` plus a
provenance list (one entry per source session: label, record range, time span)
and a gaps list. Stdlib only.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .store import STORE_COLUMNS, ColumnStore


@dataclass(frozen=True)
class SourceSpan:
    """Provenance for one source session inside the stitched timeline."""
    label: str
    lo: int               # first stitched record index (inclusive)
    hi: int               # last stitched record index (exclusive)
    t_start: str          # ISO start of this source within the timeline
    t_end: str            # ISO end
    records: int


@dataclass(frozen=True)
class Gap:
    """A discontinuity between two consecutive sources."""
    after_label: str
    before_label: str
    t_gap_start: str      # end of the earlier session
    t_gap_end: str        # start of the later session
    seconds: float


@dataclass
class StitchResult:
    store: ColumnStore
    sources: list[SourceSpan]
    gaps: list[Gap]

    def to_jsonable(self) -> dict:
        return {
            "total_records": self.store.n,
            "sources": [vars(s) for s in self.sources],
            "gaps": [vars(g) for g in self.gaps],
        }


def stitch_stores(
    labelled_stores: list[tuple[str, ColumnStore]],
    gap_tolerance_secs: float = 2.0,
) -> StitchResult:
    """Stitch labelled ColumnStores into one continuous-timeline store.

    Args:
        labelled_stores: ``[(label, store), ...]`` — order is irrelevant, they
            are sorted by first-record start time.
        gap_tolerance_secs: sessions whose boundary differs by more than this
            are recorded as a :class:`Gap` (records are still concatenated in
            time order; no synthetic fill rows are inserted).

    Returns a :class:`StitchResult`. Empty stores are skipped.
    """
    usable = [(lbl, st) for lbl, st in labelled_stores if st.n > 0]
    if not usable:
        return StitchResult(ColumnStore(), [], [])
    usable.sort(key=lambda ls: ls[1].start(0))

    out = ColumnStore(time_shift=dt.timedelta(0))
    sources: list[SourceSpan] = []
    gaps: list[Gap] = []
    prev_end: dt.datetime | None = None
    prev_label: str | None = None

    for label, st in usable:
        lo = out.n
        # Record a gap if this session does not abut the previous one.
        cur_start = st.start(0)
        if prev_end is not None:
            delta = (cur_start - prev_end).total_seconds()
            if abs(delta) > gap_tolerance_secs:
                gaps.append(Gap(
                    after_label=prev_label or "?",
                    before_label=label,
                    t_gap_start=prev_end.isoformat(),
                    t_gap_end=cur_start.isoformat(),
                    seconds=delta,
                ))
        # Append every record's retained columns + absolute (shifted) ticks.
        for name in STORE_COLUMNS:
            out._cols[name].extend(st._cols[name])
        # Carry absolute ticks (apply each store's own time_shift so the
        # stitched store needs no further shift).
        shift_ticks = int(round(st.time_shift.total_seconds() * 10_000_000))
        out._start_ticks.extend(t + shift_ticks for t in st._start_ticks)
        out._end_ticks.extend(t + shift_ticks for t in st._end_ticks)
        out._n += st.n

        hi = out.n
        sources.append(SourceSpan(
            label=label, lo=lo, hi=hi,
            t_start=st.start(0).isoformat(),
            t_end=st.end(st.n - 1).isoformat(),
            records=st.n,
        ))
        prev_end = st.end(st.n - 1)
        prev_label = label

    return StitchResult(store=out, sources=sources, gaps=gaps)
