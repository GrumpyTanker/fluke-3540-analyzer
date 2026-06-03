"""Tests for per-bucket summary rows and event ITIC augmentation."""
from __future__ import annotations

import datetime as dt

import pytest

from fluke_3540.analysis import bucket_summary_row, event_itic
from fluke_3540.events import Event, detect_events
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window


def test_bucket_summary_healthy():
    recs = make_records(3600)  # 1 hour flat: 50kW, 277V, 100A
    store = ColumnStore.from_records(recs)
    row = bucket_summary_row("2026-05-27T00", store, [])
    assert row["records"] == 3600
    assert row["V_avg_V"] == pytest.approx(277.0)
    assert row["I_max_A"] == pytest.approx(100.0)
    assert row["peak_kW"] == pytest.approx(50.0)
    # 50 kW for 1 hour = 50 kWh
    assert row["kWh"] == pytest.approx(50.0, rel=1e-3)
    assert row["n_outages"] == 0


def test_bucket_summary_counts_events():
    overrides: dict = {}
    plant_window(overrides, 100, 109, {
        "V_LN_a_avg_V": 0.0, "V_LN_b_avg_V": 0.0, "V_LN_c_avg_V": 0.0,
        "V_LN_a_min_V": 0.0, "V_LN_b_min_V": 0.0, "V_LN_c_min_V": 0.0,
    })
    recs = make_records(400, overrides=overrides)
    store = ColumnStore.from_records(recs)
    events = detect_events(store, nominal_ln_v=277.0)
    row = bucket_summary_row("b", store, events)
    assert row["n_outages"] == 1


def test_event_itic_dip():
    ev = Event(0, "dip",
               dt.datetime(2026, 5, 27, 1, 0, 0, tzinfo=dt.timezone.utc),
               dt.datetime(2026, 5, 27, 1, 0, 3, tzinfo=dt.timezone.utc),
               0.65, ("a",))  # residual 65% for 3 s
    info = event_itic(ev, nominal_ln_v=277.0)
    assert info["residual_pct"] == pytest.approx(65.0)
    assert info["duration_secs"] == pytest.approx(3.0)
    assert info["itic_class"] == "no_damage"  # 65% at 3s below 80% floor


def test_event_itic_outage():
    ev = Event(0, "outage",
               dt.datetime(2026, 5, 27, 1, 0, 0, tzinfo=dt.timezone.utc),
               dt.datetime(2026, 5, 27, 1, 0, 30, tzinfo=dt.timezone.utc),
               0.0, ("a", "b", "c"))
    info = event_itic(ev, nominal_ln_v=277.0)
    assert info["residual_pct"] == pytest.approx(0.0)
    assert info["itic_class"] == "no_damage"


def test_event_itic_non_voltage_empty():
    ev = Event(0, "power_step",
               dt.datetime(2026, 5, 27, 1, 0, 0, tzinfo=dt.timezone.utc),
               dt.datetime(2026, 5, 27, 1, 0, 1, tzinfo=dt.timezone.utc),
               100000.0, ("a", "b", "c"))
    assert event_itic(ev, 277.0) == {}
