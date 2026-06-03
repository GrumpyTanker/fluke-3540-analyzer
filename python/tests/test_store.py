"""Tests for the columnar session store."""
from __future__ import annotations

import array
import datetime as dt

import pytest

from fluke_3540.store import STORE_COLUMNS, ColumnStore, _dt_to_ticks

from conftest import make_records, SYNTHETIC_BASE


def test_from_records_length_and_columns():
    recs = make_records(50)
    store = ColumnStore.from_records(recs)
    assert store.n == 50
    assert len(store) == 50
    for name in STORE_COLUMNS:
        c = store.col(name)
        assert isinstance(c, array.array)
        assert c.typecode == "f"
        assert len(c) == 50


def test_col_values_match_records():
    recs = make_records(10)
    store = ColumnStore.from_records(recs)
    p = store.col("P_total_avg_W")
    assert p[0] == pytest.approx(50_000.0)
    v = store.col("V_LN_a_avg_V")
    assert v[3] == pytest.approx(277.0)


def test_unknown_column_raises():
    store = ColumnStore.from_records(make_records(3))
    with pytest.raises(KeyError, match="not retained"):
        store.col("Wh_a")


def test_times_without_shift():
    recs = make_records(5)
    store = ColumnStore.from_records(recs)
    assert store.start(0) == SYNTHETIC_BASE
    assert store.end(0) == SYNTHETIC_BASE + dt.timedelta(seconds=1)
    assert store.start(4) == SYNTHETIC_BASE + dt.timedelta(seconds=4)
    times = list(store.iter_times())
    assert times[0] == SYNTHETIC_BASE
    assert times[-1] == SYNTHETIC_BASE + dt.timedelta(seconds=4)


def test_time_shift_applied():
    recs = make_records(3)
    shift = dt.timedelta(days=365 * 2)  # crude jump forward
    store = ColumnStore.from_records(recs, time_shift=shift)
    assert store.start(0) == SYNTHETIC_BASE + shift
    assert store.start_raw(0) == SYNTHETIC_BASE  # raw retains original
    assert store.end(2) == SYNTHETIC_BASE + dt.timedelta(seconds=3) + shift


def test_first_last_helpers():
    recs = make_records(7)
    store = ColumnStore.from_records(recs)
    assert store.first_start == SYNTHETIC_BASE
    assert store.last_end == SYNTHETIC_BASE + dt.timedelta(seconds=7)


def test_empty_store():
    store = ColumnStore.from_records([])
    assert store.n == 0
    assert store.first_start is None
    assert store.last_end is None
    assert list(store.iter_times()) == []


def test_dt_to_ticks_roundtrip():
    t = dt.datetime(2026, 6, 2, 20, 45, 0, tzinfo=dt.timezone.utc)
    ticks = _dt_to_ticks(t)
    from fluke_3540.parser import filetime_to_dt
    assert filetime_to_dt(ticks) == t
