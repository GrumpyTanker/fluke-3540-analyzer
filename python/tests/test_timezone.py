"""Tests for timezone-aware reporting helpers (Feature H)."""
from __future__ import annotations

import datetime as dt

import pytest

from fluke_3540.tzutil import format_local_utc, resolve_tz, to_utc, tz_label

UTC = dt.timezone.utc
T = dt.datetime(2024, 1, 13, 15, 0, 0, tzinfo=UTC)  # 15:00 UTC


def test_resolve_tz_none_is_utc_default():
    assert resolve_tz(None) is None
    assert resolve_tz("UTC") is dt.timezone.utc


def test_resolve_tz_iana():
    tz = resolve_tz("America/Chicago")
    assert tz is not None
    # 15:00 UTC = 09:00 CST (UTC-6) in January.
    local = T.astimezone(tz)
    assert local.hour == 9
    assert local.utcoffset() == dt.timedelta(hours=-6)


def test_resolve_tz_unknown_raises():
    with pytest.raises(ValueError):
        resolve_tz("Not/AZone")


def test_to_utc_naive_assumed_utc():
    naive = dt.datetime(2024, 1, 13, 15, 0, 0)
    assert to_utc(naive) == T


def test_to_utc_converts_offset():
    cst = dt.datetime(2024, 1, 13, 9, 0, 0,
                      tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    assert to_utc(cst) == T


def test_format_default_utc_only():
    s = format_local_utc(T, None)
    assert s == "2024-01-13T15:00:00+00:00"


def test_format_local_and_utc():
    tz = resolve_tz("America/Chicago")
    s = format_local_utc(T, tz)
    assert s.startswith("2024-01-13T09:00:00-06:00")
    assert "(2024-01-13T15:00:00+00:00)" in s


def test_tz_label():
    assert tz_label(None, None) == "UTC"
    assert tz_label(resolve_tz("America/Chicago"), "America/Chicago") == "America/Chicago"


def test_anchor_iso_offset_still_parses():
    # The anchor parser (cli._parse_time) honours explicit offsets.
    from fluke_3540.cli import _parse_time
    parsed = _parse_time("2024-01-13T09:00:00-06:00")
    assert to_utc(parsed) == T
