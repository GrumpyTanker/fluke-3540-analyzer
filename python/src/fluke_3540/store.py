"""Columnar session store — memory-bounded representation of a session.

The original analysis path materialised every :class:`~fluke_3540.parser.Record`
(a 180-float tuple + 2 datetimes) into a Python list, often twice. For a
week-long capture (~590 K records) that is well over a gigabyte of objects and
will not fit in memory.

:class:`ColumnStore` instead keeps only the ~20 channels the event /
snapshot / insight engines actually read, each as a packed ``array.array('f')``
column, plus an ``array('q')`` of start FILETIME ticks. That is ~50 MB for a
full week instead of >1 GB, and it is built in a single streaming pass over
``iter_records`` — no intermediate list of Records.

Wrong-RTC sessions are corrected by an optional ``time_shift`` applied lazily
in :meth:`ColumnStore.start` / :meth:`ColumnStore.end` / :meth:`iter_times`,
so the raw FILETIME ticks are never lost.

Stdlib only — no numpy.
"""
from __future__ import annotations

import array
import datetime as dt
from typing import Iterable, Iterator, Sequence

from .parser import FIELDS, Record, filetime_to_dt


# --- Which channels the analysis engines need --------------------------------
#
# These are the only float columns kept in memory. Anything else lives only in
# the on-disk CSV. If a new rule needs another channel, add it here.
STORE_COLUMNS: tuple[str, ...] = (
    # Per-phase L-N voltage min/max/avg
    "V_LN_a_min_V", "V_LN_b_min_V", "V_LN_c_min_V",
    "V_LN_a_max_V", "V_LN_b_max_V", "V_LN_c_max_V",
    "V_LN_a_avg_V", "V_LN_b_avg_V", "V_LN_c_avg_V",
    # Per-phase current max + avg (avg used by stats/current-spike-ratio,
    # max used by high-current detection)
    "I_a_max_A", "I_b_max_A", "I_c_max_A",
    "I_a_avg_A", "I_b_avg_A", "I_c_avg_A",
    # Line frequency
    "freq_avg_Hz",
    # Power / apparent / reactive / power-factor totals
    "P_total_avg_W", "S_total_avg_VA", "Q_total_avg_VAR", "PF_total_avg",
    # Per-row energy (used by per-bucket kWh roll-ups)
    "Wh_total",
    # THD per phase (IEEE 519) — V and I, avg only
    "V_THD_pct_a_avg", "V_THD_pct_b_avg", "V_THD_pct_c_avg",
    "I_THD_pct_a_avg", "I_THD_pct_b_avg", "I_THD_pct_c_avg",
)

_FIELD_INDEX = {f.name: f.index for f in FIELDS}


def _resolve_indices(names: Sequence[str]) -> list[int]:
    out = []
    for name in names:
        try:
            out.append(_FIELD_INDEX[name])
        except KeyError as e:  # pragma: no cover - guards spec drift
            raise KeyError(
                f"Store column {name!r} missing from spec/field_map.json"
            ) from e
    return out


class ColumnStore:
    """Memory-bounded columnar view over a session.

    Build with :meth:`from_trend` (streaming, the production path) or
    :meth:`from_records` (from an in-memory iterable, used by the small-fixture
    tests). Read columns with :meth:`col`, timestamps with :meth:`start` /
    :meth:`end` / :meth:`iter_times`.
    """

    __slots__ = ("_cols", "_start_ticks", "_end_ticks", "time_shift", "_n")

    def __init__(self, time_shift: dt.timedelta | None = None) -> None:
        self._cols: dict[str, array.array] = {
            name: array.array("f") for name in STORE_COLUMNS
        }
        # FILETIME ticks (100 ns since 1601) as signed 64-bit ints.
        self._start_ticks = array.array("q")
        self._end_ticks = array.array("q")
        self.time_shift: dt.timedelta = time_shift or dt.timedelta(0)
        self._n = 0

    # --- size --------------------------------------------------------------
    @property
    def n(self) -> int:
        return self._n

    def __len__(self) -> int:
        return self._n

    # --- column access -----------------------------------------------------
    def col(self, name: str) -> array.array:
        """Return the packed column array for ``name`` (no copy)."""
        try:
            return self._cols[name]
        except KeyError as e:
            raise KeyError(
                f"Column {name!r} is not retained in the ColumnStore. "
                f"Retained columns: {', '.join(STORE_COLUMNS)}"
            ) from e

    @property
    def columns(self) -> tuple[str, ...]:
        return STORE_COLUMNS

    # --- timestamps --------------------------------------------------------
    def start_raw(self, i: int) -> dt.datetime:
        """Start time WITHOUT the time_shift applied (raw meter clock)."""
        return filetime_to_dt(self._start_ticks[i])

    def start(self, i: int) -> dt.datetime:
        """Shifted start time at record ``i`` (real wall-clock if anchored)."""
        return filetime_to_dt(self._start_ticks[i]) + self.time_shift

    def end(self, i: int) -> dt.datetime:
        """Shifted end time at record ``i``."""
        return filetime_to_dt(self._end_ticks[i]) + self.time_shift

    def iter_times(self) -> Iterator[dt.datetime]:
        """Yield shifted start times for every record, in order."""
        shift = self.time_shift
        for t in self._start_ticks:
            yield filetime_to_dt(t) + shift

    def iter_end_times(self) -> Iterator[dt.datetime]:
        shift = self.time_shift
        for t in self._end_ticks:
            yield filetime_to_dt(t) + shift

    @property
    def first_start(self) -> dt.datetime | None:
        if self._n == 0:
            return None
        return self.start(0)

    @property
    def last_end(self) -> dt.datetime | None:
        if self._n == 0:
            return None
        return self.end(self._n - 1)

    # --- mutation (build-time only) ---------------------------------------
    def _append_record(self, rec: Record, col_idx: list[int]) -> None:
        floats = rec.floats
        for name, idx in zip(STORE_COLUMNS, col_idx):
            self._cols[name].append(floats[idx])
        # Recompute the FILETIME ticks from the record's datetimes. Records
        # produced by iter_records carry datetimes; round-tripping through
        # ticks keeps the store independent of how the Record was built.
        self._start_ticks.append(_dt_to_ticks(rec.start))
        self._end_ticks.append(_dt_to_ticks(rec.end))
        self._n += 1

    # --- factories ---------------------------------------------------------
    @classmethod
    def from_records(
        cls,
        records: Iterable[Record],
        time_shift: dt.timedelta | None = None,
    ) -> "ColumnStore":
        """Build a store from an in-memory iterable of Records."""
        store = cls(time_shift=time_shift)
        col_idx = _resolve_indices(STORE_COLUMNS)
        for rec in records:
            store._append_record(rec, col_idx)
        return store


_FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


def _dt_to_ticks(value: dt.datetime) -> int:
    """datetime -> Windows FILETIME ticks (100 ns since 1601-01-01 UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    delta = value - _FILETIME_EPOCH
    return int(round(delta.total_seconds() * 10_000_000))
