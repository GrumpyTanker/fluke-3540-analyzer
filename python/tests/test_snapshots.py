"""Tests for snapshot picking."""
from __future__ import annotations

from statistics import pstdev

from fluke_3540.events import detect_events
from fluke_3540.snapshots import _rolling_stdev, pick_snapshots
from fluke_3540.store import ColumnStore

from conftest import make_records, plant_window


def _rolling_stdev_naive(values, window):
    """The original O(N*window) implementation, kept here as a parity oracle."""
    out = [None] * len(values)
    if window <= 1 or window > len(values):
        return out
    for i in range(window - 1, len(values)):
        out[i] = pstdev(values[i - window + 1:i + 1])
    return out


def test_prefix_sum_stdev_matches_naive():
    # Varied, non-trivial series so float cancellation is exercised.
    values = [50_000.0 + (i % 13) * 3_000.0 + (i * i % 97) for i in range(1000)]
    for window in (1, 2, 5, 60, 300, 1001):
        new = _rolling_stdev(values, window)
        old = _rolling_stdev_naive(values, window)
        assert len(new) == len(old)
        for a, b in zip(new, old):
            if a is None or b is None:
                assert a is b is None
            else:
                assert abs(a - b) < 1e-3, f"window={window}"


def test_pick_snapshots_accepts_store():
    recs = make_records(600)
    store = ColumnStore.from_records(recs)
    snaps = pick_snapshots(store, events=[], n=1, window_secs=300,
                           min_separation_secs=1)
    assert len(snaps) == 1
    assert snaps[0].p_total_mean_w == 50_000.0


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
