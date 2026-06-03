"""Tests for whole-session statistics."""
from __future__ import annotations

from statistics import mean, pstdev

import pytest

from fluke_3540.analysis import whole_session_stats
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window


def test_stats_basic_channels():
    recs = make_records(100)  # flat healthy profile
    store = ColumnStore.from_records(recs)
    stats = whole_session_stats(store)
    assert "P_total_avg_W" in stats
    p = stats["P_total_avg_W"]
    assert p["count"] == 100
    assert p["mean"] == pytest.approx(50_000.0)
    assert p["stdev"] == pytest.approx(0.0, abs=1.0)
    assert p["min"] == pytest.approx(50_000.0)
    assert p["max"] == pytest.approx(50_000.0)


def test_stats_mean_matches_python_mean():
    overrides = {i: {"P_total_avg_W": 50_000.0 + i * 100.0} for i in range(200)}
    recs = make_records(200, overrides=overrides)
    store = ColumnStore.from_records(recs)
    stats = whole_session_stats(store)
    expected = mean([50_000.0 + i * 100.0 for i in range(200)])
    assert stats["P_total_avg_W"]["mean"] == pytest.approx(expected, rel=1e-6)


def test_stats_stdev_matches_pstdev():
    overrides = {i: {"V_LN_a_avg_V": 270.0 + (i % 11)} for i in range(300)}
    recs = make_records(300, overrides=overrides)
    store = ColumnStore.from_records(recs)
    stats = whole_session_stats(store)
    expected = pstdev([270.0 + (i % 11) for i in range(300)])
    assert stats["V_LN_a_avg_V"]["stdev"] == pytest.approx(expected, rel=1e-3)


def test_stats_percentiles_ordered():
    overrides = {i: {"I_a_avg_A": float(i)} for i in range(1000)}
    recs = make_records(1000, overrides=overrides)
    store = ColumnStore.from_records(recs)
    s = whole_session_stats(store)["I_a_avg_A"]
    assert s["min"] <= s["p1"] <= s["p5"] <= s["median"] <= s["p95"] <= s["p99"] <= s["max"]
    # median of 0..999 ~ 500, within a couple bin-widths
    assert s["median"] == pytest.approx(500.0, abs=5.0)


def test_stats_undervoltage_accounting():
    overrides: dict = {}
    plant_window(overrides, 10, 19, {"V_LN_a_avg_V": 200.0})  # 10s under 250 (still non-outage)
    recs = make_records(100, overrides=overrides)
    store = ColumnStore.from_records(recs)
    th = whole_session_stats(store)["_thresholds"]
    assert th["sec_undervoltage"] == 10
    assert th["pct_undervoltage"] == pytest.approx(10.0)


def test_stats_overcurrent_accounting():
    overrides: dict = {}
    plant_window(overrides, 0, 4, {"I_c_avg_A": 900.0})  # 5s over 800A
    recs = make_records(50, overrides=overrides)
    store = ColumnStore.from_records(recs)
    th = whole_session_stats(store)["_thresholds"]
    assert th["sec_overcurrent"] == 5
