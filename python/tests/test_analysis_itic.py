"""Tests for ITIC/CBEMA classification."""
from __future__ import annotations

from fluke_3540.analysis import classify_itic


def test_full_outage_long_is_no_damage():
    # 0% residual for 5 s: well below the lower envelope -> dropout (no_damage)
    assert classify_itic(0.0, 5.0) == "no_damage"


def test_steady_normal_voltage_no_interruption():
    # 100% residual, long duration: inside envelope
    assert classify_itic(100.0, 60.0) == "no_interruption"


def test_deep_dip_steady_is_no_damage():
    # 60% residual sustained 30 s is below the 90% steady floor
    assert classify_itic(60.0, 30.0) == "no_damage"


def test_brief_dip_rides_through():
    # 75% residual for 100 ms is above the 70% floor in that band
    assert classify_itic(75.0, 0.1) == "no_interruption"


def test_overvoltage_prohibited():
    # 150% residual sustained -> prohibited (overvoltage)
    assert classify_itic(150.0, 30.0) == "prohibited"


def test_very_brief_transient_tolerated():
    # sub-ms transient at any level is tolerated on the lower side
    assert classify_itic(10.0, 0.0005) == "no_interruption"


def test_moderate_dip_in_one_to_ten_sec_band():
    # 75% residual at 3 s: lower floor in 0.5-10s band is 80% -> below -> no_damage
    assert classify_itic(75.0, 3.0) == "no_damage"
    # 85% residual at 3 s: above 80% floor -> rides through
    assert classify_itic(85.0, 3.0) == "no_interruption"
