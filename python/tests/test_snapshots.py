"""Tests for snapshot picking."""
from __future__ import annotations

from fluke_3540.events import detect_events
from fluke_3540.snapshots import pick_snapshots

from conftest import make_records, plant_window


def test_no_records_returns_empty():
    assert pick_snapshots([], events=[], n=3) == []


def test_snapshot_picks_quietest_window():
    # 1200 seconds total. Inject noise (P_total varies wildly) in the first half;
    # leave second half flat. With window=300, the quietest window must lie in
    # the second half.
    overrides: dict[int, dict[str, float]] = {}
    for i in range(0, 600):
        overrides[i] = {"P_total_avg_W": 50_000.0 + (i % 7) * 10_000.0}  # noisy
    recs = make_records(1200, overrides=overrides)
    snaps = pick_snapshots(recs, events=[], n=1, window_secs=300,
                           min_separation_secs=1)
    assert len(snaps) == 1
    # The quiet window's center should fall in the second half (record index >= 600)
    assert snaps[0].t_center >= recs[600].start


def test_snapshot_avoids_events():
    # Flat-ish data; put a fake "event" in the middle that masks the otherwise-
    # quietest region. Picker should fall back to the next-quietest window.
    recs = make_records(1200)
    fake_event_window = (recs[400].start, recs[800].end)

    class FakeEvent:
        t_start = fake_event_window[0]
        t_end = fake_event_window[1]

    snaps = pick_snapshots(recs, events=[FakeEvent()], n=1, window_secs=300,
                           min_separation_secs=1)
    assert len(snaps) == 1
    assert snaps[0].t_end < fake_event_window[0] or snaps[0].t_start > fake_event_window[1]


def test_snapshot_respects_min_separation():
    recs = make_records(7200)
    snaps = pick_snapshots(recs, events=[], n=3, window_secs=300,
                           min_separation_secs=1800)
    assert len(snaps) <= 3
    centers = [s.t_center for s in snaps]
    for i in range(1, len(centers)):
        assert abs((centers[i] - centers[i - 1]).total_seconds()) >= 1800


def test_snapshot_reports_stats():
    recs = make_records(600)
    snaps = pick_snapshots(recs, events=[], n=1, window_secs=300,
                           min_separation_secs=1)
    assert len(snaps) == 1
    s = snaps[0]
    assert s.p_total_mean_w == 50_000.0
    assert s.p_total_stdev_w == 0.0
    assert s.id == 0
