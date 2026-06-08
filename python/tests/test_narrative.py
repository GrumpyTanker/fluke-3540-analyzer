"""Tests for the auto-narrative / executive summary (Feature E)."""
from __future__ import annotations

import datetime as dt

from fluke_3540.events import Event
from fluke_3540.insights import Finding
from fluke_3540.narrative import build_narrative, narrative_markdown

BASE = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)


def _ev(id_, kind, start_s, end_s, sev, phases=("a", "b", "c")):
    return Event(id_, kind, BASE + dt.timedelta(seconds=start_s),
                 BASE + dt.timedelta(seconds=end_s), sev, phases)


def _finding(kind, severity, headline):
    return Finding(id=0, kind=kind, severity=severity, headline=headline,
                   detail="", related_event_ids=(), recommended_actions=())


def test_narrative_clean_session():
    n = build_narrative([], [], None, None, config={"asset_name": "PUMP-1"},
                        total_records=3600, duration_secs=3600)
    assert "PUMP-1" in n
    assert "No outages" in n
    assert "Bottom line: the supply looks clean" in n


def test_narrative_outage_headline_with_leading_dip():
    dip = _ev(0, "dip", 90, 92, 0.72, ("c",))
    outage = _ev(1, "outage", 100, 1210, 0.0)  # 1110 s outage
    n = build_narrative([dip, outage], [], None, None,
                        config={"asset_name": "MAC03"})
    assert "outage" in n
    assert "18.5 min" in n  # 1110 s -> 18.5 min
    assert "preceded by a phase-c dip to 72%" in n


def test_narrative_includes_pf_and_ct():
    stats = {
        "PF_total_avg": {"mean": 0.81, "p5": 0.70, "p95": 0.95},
        "_thresholds": {"total_records": 1000},
    }
    pf = _finding("pf_drift", "alert", "Power factor below 0.85 for 99.8% of non-outage time")
    ct = {"reversed": True, "frac_negative": 0.52}
    n = build_narrative([], [pf], stats, ct, config={"asset_name": "X"})
    assert "Power factor (total) averaged 0.81" in n
    assert "Power factor below 0.85 for 99.8%" in n
    assert "iFlex CTs are likely reversed" in n
    assert "Bottom line: 1 alert-level finding" in n


def test_narrative_prefers_active_state_pf_and_energy():
    stats = {
        "PF_total_avg": {"mean": -0.09, "p5": -0.70, "p95": 0.50},
        "_thresholds": {"total_records": 1000},
    }
    load_states = {
        "standby_threshold_a": 50.0,
        "states": [
            {"state": "active", "records": 490, "duty_pct": 49.0,
             "I_avg_A": 239.0, "P_avg_kW": 97.0, "PF_avg": 0.47, "kWh": 6638.0},
            {"state": "standby", "records": 470, "duty_pct": 47.0,
             "I_avg_A": 16.0, "P_avg_kW": -7.6, "PF_avg": -0.64, "kWh": -100.0},
        ],
        "energy": {
            "energy_as_measured_kWh": 6054.0,
            "energy_active_kWh": 6638.0,
            "energy_net_clip_standby_kWh": 6684.0,
        },
    }
    n = build_narrative([], [], stats, None, config={"asset_name": "P115RE"},
                        load_states=load_states)
    # headline PF is the ACTIVE-state PF, not the meaningless blended -0.09
    assert "Active-state power factor averaged 0.47" in n
    assert "not meaningful for a bimodal load" in n
    # bimodal description + three energy figures
    assert "bimodal" in n.lower()
    assert "6054 kWh as-measured" in n
    assert "6638 kWh active-only" in n
    assert "6684 kWh net" in n


def test_narrative_dip_only_no_outage():
    dip = _ev(0, "dip", 50, 53, 0.65, ("a",))
    n = build_narrative([dip], [], None, None)
    assert "No outages occurred" in n
    assert "65% of nominal" in n


def test_narrative_markdown_wrapper():
    md = narrative_markdown("Hello world.", config={"asset_name": "ABC"})
    assert md.startswith("# Executive Summary — ABC")
    assert "Hello world." in md


def test_narrative_is_deterministic():
    dip = _ev(0, "dip", 90, 92, 0.72, ("c",))
    outage = _ev(1, "outage", 100, 220, 0.0)
    args = ([dip, outage], [], None, None, {"asset_name": "Z"})
    assert build_narrative(*args) == build_narrative(*args)
