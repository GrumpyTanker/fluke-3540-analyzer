"""Tests for time-bucket partitioning, markers, and time-of-day profile."""
from __future__ import annotations

import datetime as dt

import pytest

from fluke_3540.analysis import (
    Marker,
    Period,
    assign_buckets,
    bucket_key,
    bucket_label,
    correlate_markers,
    parse_period,
    parse_tod_window,
    slice_store,
    time_of_day_profile,
)
from fluke_3540.events import Event
from fluke_3540.store import ColumnStore

from conftest import make_records


# --- parse_period ------------------------------------------------------------

def test_parse_period_keywords():
    assert parse_period("hour").seconds == 3600
    assert parse_period("day").seconds == 86400
    assert parse_period("week").seconds == 7 * 86400


def test_parse_period_durations():
    assert parse_period("30m").seconds == 1800
    assert parse_period("6h").seconds == 21600
    assert parse_period("2d").seconds == 2 * 86400


def test_parse_period_invalid():
    with pytest.raises(ValueError):
        parse_period("banana")
    with pytest.raises(ValueError):
        parse_period("0h")


# --- bucket_key / label ------------------------------------------------------

def test_bucket_key_6h_aligns_within_day():
    p = parse_period("6h")
    t = dt.datetime(2026, 5, 27, 14, 23, 5, tzinfo=dt.timezone.utc)
    assert bucket_key(t, p) == dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert bucket_label(bucket_key(t, p), p) == "2026-05-27T12-18"


def test_bucket_key_day():
    p = parse_period("day")
    t = dt.datetime(2026, 5, 27, 14, 23, tzinfo=dt.timezone.utc)
    assert bucket_label(bucket_key(t, p), p) == "2026-05-27"


def test_bucket_key_hour_label():
    p = parse_period("hour")
    t = dt.datetime(2026, 5, 27, 14, 23, tzinfo=dt.timezone.utc)
    assert bucket_label(bucket_key(t, p), p) == "2026-05-27T14"


# --- assign_buckets ----------------------------------------------------------

def test_assign_buckets_day_boundary():
    # 3 days of records at 1/sec would be huge; use a shift so records straddle
    # midnight with only a few hundred records. Build 200 records starting
    # 23:59:00 so they cross into the next day.
    base = dt.datetime(2026, 5, 27, 23, 59, 0, tzinfo=dt.timezone.utc)
    recs = make_records(200, base=base)
    store = ColumnStore.from_records(recs)
    buckets = assign_buckets(store, parse_period("day"))
    assert len(buckets) == 2
    # union of bucket ranges covers all records, contiguous
    assert buckets[0][1] == 0
    assert buckets[-1][2] == store.n
    for a, b in zip(buckets, buckets[1:]):
        assert a[2] == b[1]  # contiguous


def test_slice_store_preserves_columns_and_shift():
    recs = make_records(100)
    store = ColumnStore.from_records(recs, time_shift=dt.timedelta(days=1))
    sub = slice_store(store, 10, 30)
    assert sub.n == 20
    assert sub.time_shift == dt.timedelta(days=1)
    assert sub.col("P_total_avg_W")[0] == store.col("P_total_avg_W")[10]
    assert sub.start(0) == store.start(10)


# --- markers -----------------------------------------------------------------

def test_correlate_markers_finds_nearest():
    e1 = Event(0, "outage",
               dt.datetime(2026, 5, 27, 14, 23, 0, tzinfo=dt.timezone.utc),
               dt.datetime(2026, 5, 27, 14, 23, 5, tzinfo=dt.timezone.utc),
               0.0, ("a", "b", "c"))
    e2 = Event(1, "dip",
               dt.datetime(2026, 5, 27, 18, 0, 0, tzinfo=dt.timezone.utc),
               dt.datetime(2026, 5, 27, 18, 0, 2, tzinfo=dt.timezone.utc),
               0.8, ("a",))
    m = Marker(dt.datetime(2026, 5, 27, 14, 23, 4, tzinfo=dt.timezone.utc), "PLC stop")
    res = correlate_markers([m], [e1, e2])
    assert res[0]["label"] == "PLC stop"
    assert res[0]["nearest_event"]["id"] == 0
    assert res[0]["nearest_event"]["offset_secs"] == pytest.approx(4.0)


def test_correlate_markers_no_events():
    m = Marker(dt.datetime(2026, 5, 27, 14, 23, 4, tzinfo=dt.timezone.utc), "x")
    res = correlate_markers([m], [])
    assert res[0]["nearest_event"] is None


# --- time-of-day -------------------------------------------------------------

def test_parse_tod_window():
    assert parse_tod_window("08:00-17:00") == (480, 1020)
    assert parse_tod_window("00:00-24:00") == (0, 1440)


def test_time_of_day_profile_bins():
    base = dt.datetime(2026, 5, 27, 8, 0, 0, tzinfo=dt.timezone.utc)
    recs = make_records(600, base=base)  # 10 minutes at 08:00
    store = ColumnStore.from_records(recs)
    rows = time_of_day_profile(store, window=(480, 1020), bin_minutes=1)
    assert len(rows) == 10  # 08:00..08:09
    assert rows[0]["bin"] == "08:00"
    assert rows[0]["p_avg_kW"] == pytest.approx(50.0)
    assert rows[0]["n_days"] == 1


def test_time_of_day_window_filters():
    base = dt.datetime(2026, 5, 27, 6, 0, 0, tzinfo=dt.timezone.utc)
    recs = make_records(600, base=base)  # 06:00-06:10, outside 08-17 window
    store = ColumnStore.from_records(recs)
    rows = time_of_day_profile(store, window=(480, 1020), bin_minutes=1)
    assert rows == []
