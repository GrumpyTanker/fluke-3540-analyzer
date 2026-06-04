"""Tests for multi-session stitching (Feature D)."""
from __future__ import annotations

import datetime as dt

from fluke_3540.stitch import stitch_stores
from fluke_3540.store import ColumnStore

from conftest import make_records

BASE = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)


def _store(count, base):
    return ColumnStore.from_records(make_records(count, base=base))


def test_stitch_consecutive_continuous():
    # S1: 60 records [BASE .. BASE+60). S2 starts exactly where S1 ended.
    s1 = _store(60, BASE)
    s2 = _store(40, BASE + dt.timedelta(seconds=60))
    res = stitch_stores([("S1", s1), ("S2", s2)])
    assert res.store.n == 100
    # No gap — abuts exactly.
    assert res.gaps == []
    # Timeline is monotonic + contiguous.
    times = list(res.store.iter_times())
    for i in range(1, len(times)):
        assert times[i] > times[i - 1]
    assert (times[-1] - times[0]).total_seconds() == 99
    # Provenance covers both sources, in order.
    assert [s.label for s in res.sources] == ["S1", "S2"]
    assert res.sources[0].lo == 0 and res.sources[0].hi == 60
    assert res.sources[1].lo == 60 and res.sources[1].hi == 100


def test_stitch_orders_by_start_time():
    # Pass out of order — stitch must sort by start time.
    s1 = _store(10, BASE)
    s2 = _store(10, BASE + dt.timedelta(seconds=10))
    res = stitch_stores([("later", s2), ("earlier", s1)])
    assert [s.label for s in res.sources] == ["earlier", "later"]


def test_stitch_records_gap():
    # 1-hour gap between S1 end and S2 start.
    s1 = _store(60, BASE)
    s2 = _store(60, BASE + dt.timedelta(seconds=60 + 3600))
    res = stitch_stores([("S1", s1), ("S2", s2)])
    assert res.store.n == 120
    assert len(res.gaps) == 1
    g = res.gaps[0]
    assert g.after_label == "S1"
    assert g.before_label == "S2"
    assert abs(g.seconds - 3600.0) < 1e-6


def test_stitch_small_gap_within_tolerance_not_recorded():
    s1 = _store(60, BASE)
    s2 = _store(60, BASE + dt.timedelta(seconds=61))  # 1s gap, within 2s tol
    res = stitch_stores([("S1", s1), ("S2", s2)])
    assert res.gaps == []


def test_stitch_preserves_channel_values():
    s1 = ColumnStore.from_records(
        make_records(5, base=BASE, defaults={"P_total_avg_W": 11_000.0}))
    s2 = ColumnStore.from_records(
        make_records(5, base=BASE + dt.timedelta(seconds=5),
                     defaults={"P_total_avg_W": 22_000.0}))
    res = stitch_stores([("S1", s1), ("S2", s2)])
    p = res.store.col("P_total_avg_W")
    assert list(p[:5]) == [11_000.0] * 5
    assert list(p[5:]) == [22_000.0] * 5


def test_stitch_empty_inputs():
    res = stitch_stores([])
    assert res.store.n == 0
    assert res.sources == []
    assert res.gaps == []


def test_stitch_jsonable():
    s1 = _store(10, BASE)
    s2 = _store(10, BASE + dt.timedelta(seconds=10))
    res = stitch_stores([("S1", s1), ("S2", s2)])
    j = res.to_jsonable()
    assert j["total_records"] == 20
    assert len(j["sources"]) == 2
    assert j["sources"][0]["label"] == "S1"


# --- CLI subcommand end-to-end (two synthetic session dirs) -----------------

def _make_session_dir(tmp_path, name, count, base):
    """Write a minimal ES.NNN/ dir with a trend.bin of `count` healthy records."""
    import struct
    from fluke_3540.parser import (
        DATA_FLOATS, HEADER_BYTES, RECORD_MAGIC, RECORD_SIZE,
    )
    from conftest import FIELD_INDEX, dt_to_filetime

    d = tmp_path / name
    d.mkdir()
    healthy = [0.0] * DATA_FLOATS
    for ph in ("a", "b", "c"):
        for stt in ("min", "max", "avg"):
            healthy[FIELD_INDEX[f"V_LN_{ph}_{stt}_V"]] = 277.0
            healthy[FIELD_INDEX[f"I_{ph}_{stt}_A"]] = 100.0
    healthy[FIELD_INDEX["freq_avg_Hz"]] = 60.0
    healthy[FIELD_INDEX["P_total_avg_W"]] = 50_000.0
    with (d / "trend.bin").open("wb") as fh:
        for n in range(count):
            sft = dt_to_filetime(base + dt.timedelta(seconds=n))
            eft = dt_to_filetime(base + dt.timedelta(seconds=n + 1))
            header = (RECORD_MAGIC
                      + struct.pack("<II", sft >> 32 & 0xFFFFFFFF, sft & 0xFFFFFFFF)
                      + struct.pack("<II", eft >> 32 & 0xFFFFFFFF, eft & 0xFFFFFFFF)
                      + struct.pack("<I", 0))
            fh.write(header + struct.pack(f"<{DATA_FLOATS}f", *healthy))
            assert len(header) == HEADER_BYTES
    return d


def test_stitch_cli_two_sessions(tmp_path):
    import json as _json
    from fluke_3540.cli import main

    s1 = _make_session_dir(tmp_path, "ES.001", 120, BASE)
    s2 = _make_session_dir(tmp_path, "ES.002", 80,
                           BASE + dt.timedelta(seconds=120))  # abuts S1
    out = tmp_path / "stitched_out"
    rc = main(["stitch", str(s1), str(s2), "-o", str(out)])
    assert rc == 0
    prov = _json.loads((out / "stitch.json").read_text())
    assert prov["total_records"] == 200
    assert len(prov["sources"]) == 2
    assert prov["gaps"] == []
    # Stitched CSV has 200 data rows + a source column.
    lines = (out / "session.csv").read_text().strip().splitlines()
    assert len(lines) == 201  # header + 200
    assert "source" in lines[0]
    assert (out / "events.json").exists()
    assert (out / "stats.json").exists()
