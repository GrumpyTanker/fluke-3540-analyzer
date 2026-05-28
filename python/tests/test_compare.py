"""Tests for the compare subcommand and plots.compare module."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from fluke_3540.cli_compare import _resolve_labels
from fluke_3540.plots.compare import (
    COMPARE_QUANTITIES,
    _compute_session_stats,
    _session_relative_tsv,
    _write_summary_csv,
)


def _write_minimal_csv(path: Path, rows: int = 10, start: dt.datetime | None = None):
    """Tiny CSV with the columns the compare module needs."""
    start = start or dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "record_index", "timestamp_utc", "window_end_utc",
            "P_total_avg_W", "V_LN_a_avg_V", "I_a_avg_A", "I_b_avg_A",
            "I_c_avg_A", "PF_total_avg", "freq_avg_Hz", "Wh_total",
        ])
        for i in range(rows):
            t = start + dt.timedelta(seconds=i)
            w.writerow([
                i, t.isoformat(), (t + dt.timedelta(seconds=1)).isoformat(),
                50000 + i * 100, 277.0, 100.0, 100.0, 100.0,
                0.95, 60.0, 50000 / 3600,
            ])


def test_session_relative_tsv_uses_zero_based_seconds(tmp_path: Path):
    csv_path = tmp_path / "s.csv"
    _write_minimal_csv(csv_path, rows=5,
                       start=dt.datetime(2024, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc))
    tsv_path = tmp_path / "s.tsv"
    n = _session_relative_tsv(csv_path, tsv_path, ["P_total_avg_W"])
    assert n == 5
    lines = tsv_path.read_text().splitlines()
    assert lines[0] == "rel_s\tP_total_avg_W"
    # First data row must have rel_s = 0
    assert lines[1].split("\t")[0] == "0"
    # Subsequent rows are 1, 2, 3, 4 seconds in
    for i, ln in enumerate(lines[1:]):
        assert ln.split("\t")[0] == str(i)


def test_compute_session_stats(tmp_path: Path):
    csv_path = tmp_path / "s.csv"
    _write_minimal_csv(csv_path, rows=10)
    stats = _compute_session_stats(csv_path)
    assert stats["rows"] == 10
    assert stats["peak_current_a"] == 100.0
    assert stats["imported_kwh"] > 0  # we wrote positive Wh values
    assert stats["exported_kwh"] == 0


def test_write_summary_csv_side_by_side(tmp_path: Path):
    summary = tmp_path / "summary.csv"
    _write_summary_csv(
        summary,
        labels=["before", "after"],
        stats_per_session=[
            {"rows": 100, "net_kwh": 1.2, "peak_import_kw": 50.0,
             "peak_export_kw": -5.0, "peak_current_a": 120.0,
             "imported_kwh": 1.5, "exported_kwh": -0.3},
            {"rows": 200, "net_kwh": 2.4, "peak_import_kw": 60.0,
             "peak_export_kw": -3.0, "peak_current_a": 130.0,
             "imported_kwh": 2.7, "exported_kwh": -0.3},
        ],
    )
    rows = list(csv.reader(summary.open()))
    assert rows[0] == ["metric", "before", "after"]
    by_metric = {r[0]: r for r in rows[1:]}
    assert by_metric["rows"] == ["rows", "100", "200"]
    # Numeric metrics are formatted with 3 decimals
    assert by_metric["net_kwh"][1] == "1.200"
    assert by_metric["peak_current_a"][2] == "130.000"


def test_resolve_labels_default_uses_input_names(tmp_path: Path):
    labels = _resolve_labels(None, [
        Path("/tmp/ES.001"), Path("/tmp/ES.002"), Path("/tmp/ES.003.fel"),
    ])
    assert labels == ["ES.001", "ES.002", "ES.003"]


def test_resolve_labels_dedups_repeated_names():
    labels = _resolve_labels(None, [
        Path("/a/ES.001"), Path("/b/ES.001"), Path("/c/ES.001"),
    ])
    assert labels == ["ES.001", "ES.001-2", "ES.001-3"]


def test_resolve_labels_explicit_count_mismatch_raises():
    with pytest.raises(SystemExit, match="does not match"):
        _resolve_labels("a,b", [Path("/x"), Path("/y"), Path("/z")])


def test_resolve_labels_explicit_works():
    labels = _resolve_labels("before,after", [Path("/x"), Path("/y")])
    assert labels == ["before", "after"]


def test_compare_quantities_table_completeness():
    # Spot-check the table has the quantities documented in the plan
    for q in ("power", "voltage", "current", "pf", "frequency"):
        assert q in COMPARE_QUANTITIES
