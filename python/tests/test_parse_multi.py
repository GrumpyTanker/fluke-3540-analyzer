"""Tests for the single-pass export_csv_multi + tolerant parser."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from fluke_3540.parser import (
    ParseStats,
    estimate_record_count,
    export_csv_multi,
    iter_records_safe,
    reverse_cts_indices,
)
from fluke_3540.store import ColumnStore

from conftest import build_large_trend, SYNTHETIC_BASE


def _make_session(tmp_path: Path, **kw) -> Path:
    d = tmp_path / "ES.BIG"
    d.mkdir()
    build_large_trend(d / "trend.bin", **kw)
    return d


# --- iter_records_safe -------------------------------------------------------

def test_safe_parser_clean(tmp_path: Path):
    d = _make_session(tmp_path, count=100)
    stats = ParseStats()
    recs = list(iter_records_safe(d / "trend.bin", stats=stats))
    assert len(recs) == 100
    assert stats.good == 100
    assert stats.bad_magic == 0
    assert stats.truncated == 0


def test_safe_parser_skips_bad_magic(tmp_path: Path):
    d = _make_session(tmp_path, count=50, inject_bad_magic_at=25)
    logged: list[str] = []
    stats = ParseStats()
    recs = list(iter_records_safe(d / "trend.bin", stats=stats, log=logged.append))
    # Resync may consume the corrupt record but recovers the rest.
    assert stats.bad_magic >= 1
    assert any("bad magic" in m for m in logged)
    assert stats.good >= 48  # lost at most the one corrupt record


def test_safe_parser_handles_truncated_tail(tmp_path: Path):
    d = _make_session(tmp_path, count=30, truncate_tail=True)
    stats = ParseStats()
    recs = list(iter_records_safe(d / "trend.bin", stats=stats))
    assert len(recs) == 30
    assert stats.truncated == 1


def test_estimate_record_count(tmp_path: Path):
    d = _make_session(tmp_path, count=123)
    assert estimate_record_count(d / "trend.bin") == 123


# --- export_csv_multi --------------------------------------------------------

def test_multi_writes_both_csvs_one_pass(tmp_path: Path):
    d = _make_session(tmp_path, count=180)
    full = tmp_path / "full.csv"
    mn = tmp_path / "min.csv"
    res = export_csv_multi(d, full, mn)
    assert res["rows_written_full"] == 180
    # one row per 60 records: indices 0, 60, 120 => 3
    assert res["rows_written_min"] == 3
    assert isinstance(res["store"], ColumnStore)
    assert res["store"].n == 180
    with full.open() as fh:
        assert len(list(csv.reader(fh))) == 181  # +header
    with mn.open() as fh:
        assert len(list(csv.reader(fh))) == 4


def test_multi_store_full_resolution_despite_every(tmp_path: Path):
    d = _make_session(tmp_path, count=120)
    res = export_csv_multi(d, tmp_path / "f.csv", tmp_path / "m.csv", every=10)
    # full CSV is downsampled by every=10 -> 12 rows
    assert res["rows_written_full"] == 12
    # but the store keeps every record
    assert res["store"].n == 120


def test_multi_row_cap_guard_downsamples_and_logs(tmp_path: Path):
    d = _make_session(tmp_path, count=1000)
    logged: list[str] = []
    res = export_csv_multi(
        d, tmp_path / "f.csv", tmp_path / "m.csv",
        max_full_rows=100, log=logged.append,
    )
    assert res["downsampled"] is True
    assert res["effective_every"] >= 10
    assert res["rows_written_full"] <= 100
    assert any("max-csv-rows" in m for m in logged)
    # 1-min CSV unaffected, store full
    assert res["store"].n == 1000
    assert res["rows_written_min"] == 1000 // 60 + 1


def test_multi_no_cap_when_under_limit(tmp_path: Path):
    d = _make_session(tmp_path, count=50)
    res = export_csv_multi(d, tmp_path / "f.csv", tmp_path / "m.csv",
                           max_full_rows=1000)
    assert res["downsampled"] is False
    assert res["effective_every"] == 1
    assert res["rows_written_full"] == 50


def test_multi_applies_time_shift(tmp_path: Path):
    d = _make_session(tmp_path, count=10)
    shift = dt.timedelta(days=730)
    full = tmp_path / "f.csv"
    res = export_csv_multi(d, full, tmp_path / "m.csv", time_shift=shift)
    with full.open() as fh:
        rows = list(csv.DictReader(fh))
    ts0 = dt.datetime.fromisoformat(rows[0]["timestamp_utc"])
    assert ts0 == SYNTHETIC_BASE + shift
    # store carries the same shift
    assert res["store"].start(0) == SYNTHETIC_BASE + shift


def test_multi_reverse_cts_flips_csv_and_store(tmp_path: Path):
    d = _make_session(tmp_path, count=5)
    full = tmp_path / "f.csv"
    res = export_csv_multi(d, full, tmp_path / "m.csv", reverse_cts=True)
    with full.open() as fh:
        rows = list(csv.DictReader(fh))
    # P_total_avg_W was +50000 healthy -> flipped negative in CSV
    assert float(rows[0]["P_total_avg_W"]) == pytest.approx(-50_000.0)
    # store column flipped too
    assert res["store"].col("P_total_avg_W")[0] == pytest.approx(-50_000.0)
    # voltage NOT flipped
    assert float(rows[0]["V_LN_a_avg_V"]) == pytest.approx(277.0)
    assert res["store"].col("V_LN_a_avg_V")[0] == pytest.approx(277.0)
