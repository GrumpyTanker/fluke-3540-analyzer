"""Tests for rolling peak-demand analysis (Feature G)."""
from __future__ import annotations

from fluke_3540.analysis import demand_analysis
from fluke_3540.store import ColumnStore

from conftest import make_records


def test_demand_flat_load():
    recs = make_records(1000, defaults={"P_total_avg_W": 40_000.0})
    store = ColumnStore.from_records(recs)
    res = demand_analysis(store, window_secs=60)
    assert res["window_secs"] == 60
    assert abs(res["peak_demand_w"] - 40_000.0) < 1e-6
    assert abs(res["mean_demand_w"] - 40_000.0) < 1e-6
    assert res["n_windows"] == 1000 - 60 + 1


def test_demand_ramp_peak_at_end():
    # P ramps 0..999 over 1000 s; the highest 60-s trailing window is the last.
    overrides = {i: {"P_total_avg_W": float(i) * 100.0} for i in range(1000)}
    recs = make_records(1000, overrides=overrides)
    store = ColumnStore.from_records(recs)
    res = demand_analysis(store, window_secs=60)
    # Last window = mean of P over records 940..999 = mean(94000..99900 step 100).
    expected = sum(float(i) * 100.0 for i in range(940, 1000)) / 60.0
    assert abs(res["peak_demand_w"] - expected) < 1e-3
    # Peak window ends at the very last record.
    assert res["peak_window_end"] == store.end(999).isoformat()


def test_demand_series_decimation():
    recs = make_records(600, defaults={"P_total_avg_W": 10_000.0})
    store = ColumnStore.from_records(recs)
    res = demand_analysis(store, window_secs=60, series_step_secs=60)
    # First full window at i=59, then every 60 -> ~ (600-59)/60 + 1 samples.
    assert len(res["series"]) >= 9
    for pt in res["series"]:
        assert abs(pt["demand_w"] - 10_000.0) < 1e-6
        assert "t" in pt


def test_demand_window_larger_than_session():
    recs = make_records(30, defaults={"P_total_avg_W": 5_000.0})
    store = ColumnStore.from_records(recs)
    res = demand_analysis(store, window_secs=900)
    # No full 900-s window fits in 30 records -> no peak recorded.
    assert res["n_windows"] == 0
    assert res["peak_window_end"] is None


def test_demand_empty():
    store = ColumnStore.from_records([])
    res = demand_analysis(store, window_secs=900)
    assert res["n_windows"] == 0
    assert res["peak_demand_w"] == 0.0
