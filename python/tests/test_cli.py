"""Tests for the fluke-analyze CLI — focuses on parsing & --parse-only mode.

We do NOT actually invoke gnuplot here (CI may not have it installed). The
gnuplot-driven render paths get a separate, opt-in test if/when run locally.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from fluke_3540.cli import (
    _event_from_json,
    _event_to_json,
    _parse_quantities,
    _parse_time,
    build_argparser,
    main,
)
from fluke_3540.events import Event
from fluke_3540.interactive import _parse_id_list


# --- argparse ---------------------------------------------------------------

def test_argparser_minimum_args():
    args = build_argparser().parse_args(["some/dir"])
    assert args.session_dir == Path("some/dir")
    assert args.output is None
    assert args.every == 1
    assert args.pre == 30
    assert args.post == 60
    assert args.snapshots == 3
    assert args.format == "png"


def test_argparser_filter_flags():
    args = build_argparser().parse_args([
        "some/dir", "-o", "out",
        "--from", "2024-01-13T22:00:00",
        "--to", "2024-01-13T23:00:00",
        "--events", "1,3,5",
        "--plot", "voltage,current",
        "--reverse-cts", "--every", "60",
        "--snapshots", "2", "--format", "svg",
        "--no-xlsx", "--no-overview",
    ])
    assert args.from_time == dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    assert args.to_time == dt.datetime(2024, 1, 13, 23, 0, 0, tzinfo=dt.timezone.utc)
    assert args.events == "1,3,5"
    assert args.plot == "voltage,current"
    # --reverse-cts is now nargs='?' const='all', so bare flag yields 'all'
    assert args.reverse_cts == "all"
    assert args.every == 60
    assert args.snapshots == 2
    assert args.format == "svg"
    assert args.no_xlsx is True
    assert args.no_overview is True


def test_argparser_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_argparser().parse_args(["dir", "--parse-only", "--plot-only"])


def test_parse_time_accepts_iso_with_and_without_tz():
    assert _parse_time("2024-01-13T22:00:00").tzinfo == dt.timezone.utc
    assert _parse_time("2024-01-13T22:00:00+00:00") == \
        dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)


def test_parse_quantities_default():
    assert _parse_quantities(None, ("voltage", "current"),
                             {"voltage", "current", "power"}) == ["voltage", "current"]


def test_parse_quantities_explicit_and_invalid():
    valid = {"voltage", "current", "power"}
    assert _parse_quantities("power,voltage", ("voltage",), valid) == ["power", "voltage"]
    with pytest.raises(SystemExit):
        _parse_quantities("nope", ("voltage",), valid)


# --- Event JSON roundtrip ---------------------------------------------------

def test_event_json_roundtrip():
    ev = Event(
        id=7, kind="dip",
        t_start=dt.datetime(2024, 1, 13, 22, 5, 0, tzinfo=dt.timezone.utc),
        t_end=dt.datetime(2024, 1, 13, 22, 5, 3, tzinfo=dt.timezone.utc),
        severity=0.72, affected_phases=("a", "b"),
    )
    d = _event_to_json(ev)
    # JSON-serializable
    text = json.dumps(d)
    parsed = json.loads(text)
    back = _event_from_json(parsed)
    assert back == ev


# --- interactive id-list parser --------------------------------------------

def test_parse_id_list_all():
    assert _parse_id_list("all", {1, 2, 3}) == [1, 2, 3]
    assert _parse_id_list("", {1, 2}) == [1, 2]
    assert _parse_id_list("*", {0, 1}) == [0, 1]


def test_parse_id_list_explicit_and_ranges():
    assert _parse_id_list("1,3", {1, 2, 3}) == [1, 3]
    assert _parse_id_list("1-3", {1, 2, 3, 4}) == [1, 2, 3]
    assert _parse_id_list("1-2,4", {1, 2, 3, 4}) == [1, 2, 4]


def test_parse_id_list_none():
    assert _parse_id_list("none", {1, 2}) == []
    assert _parse_id_list("skip", {1, 2}) == []


def test_parse_id_list_garbage():
    assert _parse_id_list("abc", {1, 2}) is None
    assert _parse_id_list("1-x", {1, 2}) is None


def test_parse_id_list_ignores_unknown_ids():
    assert _parse_id_list("1,99", {1, 2, 3}) == [1]


# --- --parse-only end-to-end on synthetic fixture --------------------------

def test_parse_only_produces_expected_files(synthetic_session_dir, tmp_path):
    out = tmp_path / "po_out"
    rc = main([str(synthetic_session_dir), "-o", str(out), "--parse-only"])
    assert rc == 0
    assert (out / "session.csv").is_file()
    assert (out / "session_1min.csv").is_file()
    assert (out / "events.json").is_file()
    assert (out / "snapshots.json").is_file()
    assert (out / "summary.txt").is_file()
    # events.json should be valid JSON (list of events, possibly empty)
    evs = json.loads((out / "events.json").read_text())
    assert isinstance(evs, list)


def test_parse_only_with_window_filter(synthetic_session_dir, tmp_path):
    out = tmp_path / "filtered_out"
    # synthetic fixture has 10 records starting at 2024-01-13T22:00:00 UTC.
    # Filter to a sub-window of 5 seconds.
    rc = main([
        str(synthetic_session_dir), "-o", str(out), "--parse-only",
        "--from", "2024-01-13T22:00:03+00:00",
        "--to", "2024-01-13T22:00:07+00:00",
    ])
    assert rc == 0
    evs = json.loads((out / "events.json").read_text())
    assert isinstance(evs, list)
