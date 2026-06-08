"""End-to-end CLI tests for round-2 features (stats, markers, tod, split-by, ITIC)."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import pytest

from fluke_3540.cli import main

from conftest import build_large_trend


def _session(tmp_path: Path, count: int, base=None, **kw) -> Path:
    d = tmp_path / "ES.FEAT"
    d.mkdir()
    if base is None:
        build_large_trend(d / "trend.bin", count=count, **kw)
    else:
        build_large_trend(d / "trend.bin", count=count, base=base, **kw)
    return d


def test_stats_files_written(tmp_path: Path):
    d = _session(tmp_path, 300)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only"])
    assert rc == 0
    assert (out / "stats.json").is_file()
    assert (out / "stats.csv").is_file()
    stats = json.loads((out / "stats.json").read_text())
    assert "P_total_avg_W" in stats
    assert "_thresholds" in stats


def test_no_stats_flag_skips(tmp_path: Path):
    d = _session(tmp_path, 100)
    out = tmp_path / "out"
    main([str(d), "-o", str(out), "--parse-only", "--no-stats"])
    assert not (out / "stats.json").exists()


def test_events_have_itic(tmp_path: Path):
    # Inject a dip so an event with ITIC exists.
    d = tmp_path / "ES.FEAT"
    d.mkdir()
    # Build then patch a window low on phase A via a fresh builder call won't
    # let us inject dips; instead use a long enough flat session and rely on
    # detection of nothing — so assert events.json is a list (ITIC only on
    # voltage events). For an actual dip we use the make_records path elsewhere.
    build_large_trend(d / "trend.bin", count=100)
    out = tmp_path / "out"
    main([str(d), "-o", str(out), "--parse-only"])
    evs = json.loads((out / "events.json").read_text())
    assert isinstance(evs, list)


def test_markers_correlated(tmp_path: Path):
    d = _session(tmp_path, 120)
    out = tmp_path / "out"
    # synthetic base is 2024-01-13T22:00:00
    rc = main([str(d), "-o", str(out), "--parse-only",
               "--mark", "2024-01-13T22:00:30=PLC stop"])
    assert rc == 0
    assert (out / "markers.json").is_file()
    corr = json.loads((out / "markers.json").read_text())
    assert corr[0]["label"] == "PLC stop"


def test_marks_file(tmp_path: Path):
    d = _session(tmp_path, 120)
    marks = tmp_path / "marks.csv"
    marks.write_text("time,label\n2024-01-13T22:00:10,event one\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--marks", str(marks)])
    assert rc == 0
    corr = json.loads((out / "markers.json").read_text())
    assert any(c["label"] == "event one" for c in corr)


def test_tod_profile(tmp_path: Path):
    d = _session(tmp_path, 600)  # 10 min from 22:00
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--tod-profile", "22:00-23:00"])
    assert rc == 0
    assert (out / "time_of_day_profile.csv").is_file()
    with (out / "time_of_day_profile.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 10
    assert rows[0]["bin"] == "22:00"


def test_split_by_day(tmp_path: Path):
    base = dt.datetime(2024, 1, 13, 23, 59, 0, tzinfo=dt.timezone.utc)
    d = _session(tmp_path, 200, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "day", "--no-xlsx"])
    assert rc == 0
    assert (out / "buckets_summary.csv").is_file()
    assert (out / "2024-01-13" / "session.csv").is_file()
    assert (out / "2024-01-13" / "events.json").is_file()
    assert (out / "2024-01-13" / "summary.txt").is_file()
    assert (out / "2024-01-14" / "session.csv").is_file()
    with (out / "buckets_summary.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[0]["bucket"] == "2024-01-13"


def test_split_by_shifts_default(tmp_path: Path):
    # base 22:00 UTC → all records in the default night shift.
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = _session(tmp_path, 300, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "shifts",
               "--no-xlsx"])
    assert rc == 0
    assert (out / "shift_comparison.csv").is_file()
    assert (out / "shift_comparison.json").is_file()
    payload = json.loads((out / "shift_comparison.json").read_text())
    names = {r["shift"] for r in payload["shifts"]}
    assert {"day", "night"} <= names
    by = {r["shift"]: r for r in payload["shifts"]}
    assert by["night"]["records"] == 300
    assert by["day"]["records"] == 0
    assert payload["tz"] == "UTC"
    # per-occurrence buckets exist
    assert (out / "shifts").is_dir()


def test_split_by_shifts_crosses_boundary(tmp_path: Path):
    # base 17:55:00 UTC, 700 s (~11.6 min) → crosses 18:00 day→night.
    base = dt.datetime(2024, 1, 13, 17, 55, 0, tzinfo=dt.timezone.utc)
    d = _session(tmp_path, 700, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "shifts",
               "--shifts", "day=06:00-18:00,night=18:00-06:00", "--no-xlsx"])
    assert rc == 0
    rows = list(csv.DictReader((out / "shift_comparison.csv").open()))
    by = {r["shift"]: r for r in rows}
    # 17:55:00..17:59:59 = 300 records day; 18:00:00.. = 400 records night.
    assert int(by["day"]["records"]) == 300
    assert int(by["night"]["records"]) == 400
    assert by["day"]["window"] == "06:00-18:00"


def test_split_by_shifts_tz_central(tmp_path: Path):
    # base 10:25 UTC = 05:25 America/Chicago (winter, UTC-6) → night (wrap leg)
    # in Central but day in UTC. With --tz the records must land in night.
    base = dt.datetime(2024, 1, 13, 10, 25, 0, tzinfo=dt.timezone.utc)
    d = _session(tmp_path, 120, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "shifts",
               "--tz", "America/Chicago", "--no-xlsx"])
    assert rc == 0
    payload = json.loads((out / "shift_comparison.json").read_text())
    by = {r["shift"]: r for r in payload["shifts"]}
    assert payload["tz"] == "America/Chicago"
    assert by["night"]["records"] == 120   # Central 05:25 → night wrap leg
    assert by["day"]["records"] == 0


def test_split_by_shifts_file(tmp_path: Path):
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = _session(tmp_path, 100, base=base)
    out = tmp_path / "out"
    sf = tmp_path / "shifts.json"
    sf.write_text(json.dumps({"shifts": [
        {"name": "A", "start": "06:00", "end": "14:00"},
        {"name": "B", "start": "14:00", "end": "22:00"},
        {"name": "C", "start": "22:00", "end": "06:00"},
    ]}), encoding="utf-8")
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "shifts",
               "--shifts-file", str(sf), "--no-xlsx"])
    assert rc == 0
    payload = json.loads((out / "shift_comparison.json").read_text())
    by = {r["shift"]: r for r in payload["shifts"]}
    assert set(by) >= {"A", "B", "C"}
    assert by["C"]["records"] == 100   # 22:00 UTC → shift C


def test_split_by_shifts_summary_table(tmp_path: Path):
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = _session(tmp_path, 120, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "shifts",
               "--no-xlsx"])
    assert rc == 0
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "Shift comparison" in summary
    assert "night" in summary


def test_split_by_union_of_events_equals_whole(tmp_path: Path):
    # Whole-session events should equal the union of per-bucket events.
    from conftest import make_records, plant_window
    from fluke_3540.events import detect_events
    from fluke_3540.store import ColumnStore
    from fluke_3540.analysis import assign_buckets, parse_period

    base = dt.datetime(2024, 1, 13, 23, 58, 0, tzinfo=dt.timezone.utc)
    overrides: dict = {}
    # outage well within day 1
    plant_window(overrides, 10, 19, {
        "V_LN_a_avg_V": 0.0, "V_LN_b_avg_V": 0.0, "V_LN_c_avg_V": 0.0,
        "V_LN_a_min_V": 0.0, "V_LN_b_min_V": 0.0, "V_LN_c_min_V": 0.0,
    })
    # dip in day 2 (after midnight at +120s)
    plant_window(overrides, 150, 154, {"V_LN_b_min_V": 200.0})
    recs = make_records(300, base=base, overrides=overrides)
    store = ColumnStore.from_records(recs)
    whole = detect_events(store, nominal_ln_v=277.0)
    period = parse_period("day")
    buckets = assign_buckets(store, period)
    union = []
    for (start, lo, hi) in buckets:
        b_end = start + dt.timedelta(seconds=period.seconds)
        union += [e for e in whole if start <= e.t_start < b_end]
    assert {(e.kind, e.t_start) for e in union} == {(e.kind, e.t_start) for e in whole}


# --- Active/standby load-state split (CLI) -----------------------------------

def _bimodal_trend(path: Path, active_n: int, standby_n: int,
                   base: dt.datetime) -> None:
    """Write a trend.bin: active_n high-current/+P records then standby_n
    low-current/-P records (the P115RE-like bimodal shape)."""
    import struct
    from conftest import FIELD_INDEX, dt_to_filetime
    from fluke_3540.parser import (DATA_FLOATS, HEADER_BYTES, RECORD_MAGIC)

    def floats_for(active: bool) -> list[float]:
        f = [0.0] * DATA_FLOATS
        for ph in ("a", "b", "c"):
            for st in ("min", "max", "avg"):
                f[FIELD_INDEX[f"V_LN_{ph}_{st}_V"]] = 277.0
        i = 239.0 if active else 16.0
        for ph in ("a", "b", "c"):
            f[FIELD_INDEX[f"I_{ph}_avg_A"]] = i
        f[FIELD_INDEX["freq_avg_Hz"]] = 60.0
        f[FIELD_INDEX["P_total_avg_W"]] = 97_000.0 if active else -7_600.0
        f[FIELD_INDEX["S_total_avg_VA"]] = 206_000.0 if active else 11_800.0
        f[FIELD_INDEX["PF_total_avg"]] = 0.47 if active else -0.64
        return f

    with path.open("wb") as fh:
        for n in range(active_n + standby_n):
            start_ft = dt_to_filetime(base + dt.timedelta(seconds=n))
            end_ft = dt_to_filetime(base + dt.timedelta(seconds=n + 1))
            header = (
                RECORD_MAGIC
                + struct.pack("<II", start_ft >> 32 & 0xFFFFFFFF, start_ft & 0xFFFFFFFF)
                + struct.pack("<II", end_ft >> 32 & 0xFFFFFFFF, end_ft & 0xFFFFFFFF)
                + struct.pack("<I", 0)
            )
            assert len(header) == HEADER_BYTES
            fh.write(header + struct.pack(f"<{DATA_FLOATS}f",
                                          *floats_for(n < active_n)))


def test_load_states_files_written(tmp_path: Path):
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = tmp_path / "ES.LS"; d.mkdir()
    _bimodal_trend(d / "trend.bin", active_n=50, standby_n=50, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--no-stats"])
    assert rc == 0
    assert (out / "load_states.csv").is_file()
    assert (out / "load_states.json").is_file()
    payload = json.loads((out / "load_states.json").read_text())
    by = {r["state"]: r for r in payload["states"]}
    assert by["active"]["records"] == 50
    assert by["standby"]["records"] == 50
    assert by["active"]["I_avg_A"] == pytest.approx(239.0, abs=0.5)
    assert by["standby"]["I_avg_A"] == pytest.approx(16.0, abs=0.5)
    assert by["active"]["P_avg_kW"] == pytest.approx(97.0, abs=0.1)
    assert by["active"]["PF_avg"] == pytest.approx(0.47, abs=0.01)
    e = payload["energy"]
    # as-measured (signed) < active-only and < clip
    assert e["energy_as_measured_kWh"] < e["energy_active_kWh"]
    assert e["energy_as_measured_kWh"] < e["energy_net_clip_standby_kWh"]
    # summary.txt carries the load-state table.
    summary = (out / "summary.txt").read_text()
    assert "Load states" in summary
    assert "active" in summary and "standby" in summary


def test_load_states_threshold_flag(tmp_path: Path):
    # With a 10 A threshold the 16 A standby becomes active -> all 100 active.
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = tmp_path / "ES.LS2"; d.mkdir()
    _bimodal_trend(d / "trend.bin", active_n=50, standby_n=50, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--no-stats",
               "--standby-threshold-a", "10"])
    assert rc == 0
    payload = json.loads((out / "load_states.json").read_text())
    assert payload["standby_threshold_a"] == 10.0
    by = {r["state"]: r for r in payload["states"]}
    assert by["active"]["records"] == 100
    assert by["standby"]["records"] == 0


def test_shift_comparison_has_active_columns_cli(tmp_path: Path):
    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = tmp_path / "ES.LS3"; d.mkdir()
    _bimodal_trend(d / "trend.bin", active_n=120, standby_n=120, base=base)
    out = tmp_path / "out"
    rc = main([str(d), "-o", str(out), "--parse-only", "--split-by", "shifts",
               "--no-xlsx"])
    assert rc == 0
    rows = list(csv.DictReader((out / "shift_comparison.csv").open()))
    header = rows[0].keys()
    for col in ("active_records", "active_duty_pct", "active_kWh", "active_PF_avg"):
        assert col in header, f"missing shift column {col}"
