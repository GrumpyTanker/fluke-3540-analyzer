"""Tests for IEEE 519 THD compliance + IEEE 1159 / SARFI indices (Feature F)."""
from __future__ import annotations

import datetime as dt

from fluke_3540.analysis import ieee519_compliance, sarfi_indices
from fluke_3540.events import Event
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window

BASE = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)


def test_ieee519_compliant_low_thd():
    recs = make_records(200, defaults={
        "V_THD_pct_a_avg": 2.0, "V_THD_pct_b_avg": 2.5, "V_THD_pct_c_avg": 3.0,
    })
    store = ColumnStore.from_records(recs)
    res = ieee519_compliance(store)
    assert res["all_voltage_compliant"] is True
    assert res["voltage"]["a"]["compliant"] is True
    assert res["voltage"]["a"]["exceeds_planning"] is False
    assert abs(res["voltage"]["a"]["p95"] - 2.0) < 0.2


def test_ieee519_noncompliant_high_thd():
    recs = make_records(200, defaults={
        "V_THD_pct_a_avg": 9.5, "V_THD_pct_b_avg": 3.0, "V_THD_pct_c_avg": 3.0,
    })
    store = ColumnStore.from_records(recs)
    res = ieee519_compliance(store)
    assert res["voltage"]["a"]["compliant"] is False   # 9.5 > 8.0
    assert res["voltage"]["b"]["compliant"] is True
    assert res["all_voltage_compliant"] is False


def test_ieee519_exceeds_planning_but_compliant():
    recs = make_records(200, defaults={"V_THD_pct_a_avg": 6.0})
    store = ColumnStore.from_records(recs)
    res = ieee519_compliance(store)
    assert res["voltage"]["a"]["compliant"] is True       # 6 <= 8
    assert res["voltage"]["a"]["exceeds_planning"] is True  # 6 > 5


def test_sarfi_counts_by_threshold():
    # Build events: dips at 85%, 65%, 45% residual; one outage at 0%.
    events = [
        Event(0, "dip", BASE, BASE + dt.timedelta(seconds=2), 0.85, ("a",)),
        Event(1, "dip", BASE, BASE + dt.timedelta(seconds=2), 0.65, ("a",)),
        Event(2, "dip", BASE, BASE + dt.timedelta(seconds=2), 0.45, ("a",)),
        Event(3, "outage", BASE, BASE + dt.timedelta(seconds=10), 0.0, ("a", "b", "c")),
        Event(4, "swell", BASE, BASE + dt.timedelta(seconds=1), 1.15, ("a",)),  # ignored
    ]
    res = sarfi_indices(events, nominal_ln_v=277.0)
    # residuals: 85, 65, 45, 0 (outage 0/277=0%); swell ignored.
    assert res["events_considered"] == 4
    assert res["SARFI-90"] == 4   # all four < 90
    assert res["SARFI-80"] == 3   # 65,45,0
    assert res["SARFI-70"] == 3   # 65,45,0
    assert res["SARFI-50"] == 2   # 45,0
    assert res["SARFI-10"] == 1   # 0


def test_sarfi_empty():
    res = sarfi_indices([], nominal_ln_v=277.0)
    assert res["events_considered"] == 0
    assert res["SARFI-90"] == 0
