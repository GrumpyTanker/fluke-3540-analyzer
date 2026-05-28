"""Tests for the HTML report generator."""
from __future__ import annotations

import base64
import datetime as dt
import re
from pathlib import Path

import pytest

from fluke_3540.events import Event
from fluke_3540.plots.html_report import (
    _classify_chart,
    render_report_html,
    write_html_report,
)
from fluke_3540.snapshots import Snapshot


def test_classify_chart_known_patterns():
    assert _classify_chart("overview.png")[0] == "overview"
    assert _classify_chart("full_voltage.png") == ("full", "Full session — voltage")
    assert _classify_chart("event_009_dip_voltage.png") == \
        ("event", "Event 9 dip — voltage")
    assert _classify_chart("snapshot_001_power.png") == \
        ("snapshot", "Snapshot 1 — power")
    assert _classify_chart("strange.png")[0] == "other"


def test_render_report_html_includes_summary_and_events():
    ev = Event(
        id=0, kind="dip",
        t_start=dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc),
        t_end=dt.datetime(2024, 1, 13, 22, 0, 5, tzinfo=dt.timezone.utc),
        severity=0.72, affected_phases=("a",),
    )
    html_text = render_report_html(
        title="Test Report",
        config={"asset_name": "X-1"},
        summary_stats={"event_count": 1, "snapshot_count": 0},
        events=[ev], snapshots=[],
        charts=[],
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert "Test Report" in html_text
    assert "X-1" in html_text
    assert "<table>" in html_text
    assert "dip" in html_text
    assert "0.720" in html_text  # severity formatted
    # No charts → no figure tag
    assert "<figure>" not in html_text


def test_render_report_html_escapes_user_strings():
    html_text = render_report_html(
        title="<script>alert(1)</script>",
        config={"asset_name": "evil<>"},
        summary_stats={"event_count": 0},
        events=[], snapshots=[], charts=[],
    )
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "evil&lt;&gt;" in html_text


def test_render_report_html_embeds_chart_as_base64():
    fake_png = b"\x89PNG\r\n\x1a\nFAKE"
    html_text = render_report_html(
        title="Charts",
        config=None,
        summary_stats={},
        events=[], snapshots=[],
        charts=[("full_voltage.png", fake_png)],
    )
    encoded = base64.b64encode(fake_png).decode("ascii")
    assert f"data:image/png;base64,{encoded}" in html_text
    assert "Full session — voltage" in html_text


def test_write_html_report_reads_pngs(tmp_path: Path):
    charts_dir = tmp_path / "charts"
    charts_dir.mkdir()
    (charts_dir / "overview.png").write_bytes(b"\x89PNG\r\n\x1a\nA")
    (charts_dir / "full_power.png").write_bytes(b"\x89PNG\r\n\x1a\nB")
    out = tmp_path / "report.html"
    write_html_report(
        out, charts_dir=charts_dir,
        config={"asset_name": "Asset-1"},
        summary_stats={"event_count": 0},
        events=[], snapshots=[],
    )
    text = out.read_text(encoding="utf-8")
    assert "Asset-1" in text
    assert "Session overview" in text
    assert "Full session — power" in text
    assert "<img " in text


def test_write_html_report_handles_missing_charts_dir(tmp_path: Path):
    out = tmp_path / "report.html"
    write_html_report(
        out, charts_dir=tmp_path / "nope",
        config={}, summary_stats={}, events=[], snapshots=[],
    )
    assert out.is_file()
