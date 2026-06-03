"""Tests for per-asset threshold config (--rules-file, Feature I)."""
from __future__ import annotations

import json

import pytest

from fluke_3540.events import DEFAULT_RULES
from fluke_3540.rules_file import describe_overrides, load_rules


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_flat_json_defaults(tmp_path):
    p = _write(tmp_path, "r.json", json.dumps({"dip_pct_of_nominal": 0.92}))
    rules = load_rules(p)
    assert rules.dip_pct_of_nominal == 0.92
    # Unspecified keys keep the default.
    assert rules.outage_v_threshold == DEFAULT_RULES.outage_v_threshold


def test_defaults_and_per_asset_json(tmp_path):
    body = {
        "defaults": {"dip_pct_of_nominal": 0.92},
        "assets": {
            "P115RE-MAC03": {"outage_v_threshold": 60.0, "swell_pct_of_nominal": 1.08},
        },
    }
    p = _write(tmp_path, "r.json", json.dumps(body))
    # Matching asset gets defaults + its overrides.
    rules = load_rules(p, asset_name="P115RE-MAC03")
    assert rules.dip_pct_of_nominal == 0.92          # from defaults
    assert rules.outage_v_threshold == 60.0          # from asset
    assert rules.swell_pct_of_nominal == 1.08        # from asset
    # Non-matching asset gets only defaults.
    other = load_rules(p, asset_name="SOMETHING-ELSE")
    assert other.dip_pct_of_nominal == 0.92
    assert other.outage_v_threshold == DEFAULT_RULES.outage_v_threshold


def test_assets_default_fallback(tmp_path):
    body = {"assets": {"default": {"freq_excursion_hz": 0.3}}}
    p = _write(tmp_path, "r.json", json.dumps(body))
    rules = load_rules(p, asset_name="anything")
    assert rules.freq_excursion_hz == 0.3


def test_int_keys_coerced(tmp_path):
    p = _write(tmp_path, "r.json", json.dumps({"min_duration_secs": 3, "gap_tolerance_secs": 2}))
    rules = load_rules(p)
    assert rules.min_duration_secs == 3
    assert isinstance(rules.min_duration_secs, int)
    assert rules.gap_tolerance_secs == 2


def test_unknown_key_raises(tmp_path):
    p = _write(tmp_path, "r.json", json.dumps({"not_a_rule": 5}))
    with pytest.raises(ValueError, match="unknown EventRules key"):
        load_rules(p)


def test_toml_loading(tmp_path):
    toml = (
        "[defaults]\n"
        "dip_pct_of_nominal = 0.93\n\n"
        '[assets."MAC03"]\n'
        "outage_v_threshold = 55.0\n"
    )
    p = _write(tmp_path, "r.toml", toml)
    rules = load_rules(p, asset_name="MAC03")
    assert rules.dip_pct_of_nominal == 0.93
    assert rules.outage_v_threshold == 55.0


def test_describe_overrides(tmp_path):
    p = _write(tmp_path, "r.json", json.dumps({"dip_pct_of_nominal": 0.92}))
    lines = describe_overrides(p, None)
    assert any("dip_pct_of_nominal" in ln and "0.92" in ln for ln in lines)


def test_rules_file_changes_detection_end_to_end(tmp_path):
    """A raised dip threshold should flag a marginal dip the defaults miss."""
    import datetime as dt
    import struct
    from fluke_3540.cli import main
    from fluke_3540.parser import (
        DATA_FLOATS, HEADER_BYTES, RECORD_MAGIC, RECORD_SIZE,
    )
    from conftest import FIELD_INDEX, dt_to_filetime

    base = dt.datetime(2024, 1, 13, 22, 0, 0, tzinfo=dt.timezone.utc)
    d = tmp_path / "ES.RULE"
    d.mkdir()
    # 277 V nominal; a 10-s window dips to 260 V (= 93.9% — above the default
    # 90% dip threshold, so NOT a dip by default, but IS one at a 95% threshold).
    healthy = [0.0] * DATA_FLOATS
    for ph in ("a", "b", "c"):
        for stt in ("min", "max", "avg"):
            healthy[FIELD_INDEX[f"V_LN_{ph}_{stt}_V"]] = 277.0
            healthy[FIELD_INDEX[f"I_{ph}_{stt}_A"]] = 100.0
    healthy[FIELD_INDEX["freq_avg_Hz"]] = 60.0
    healthy[FIELD_INDEX["P_total_avg_W"]] = 50_000.0
    with (d / "trend.bin").open("wb") as fh:
        for n in range(120):
            vals = list(healthy)
            if 50 <= n < 60:
                vals[FIELD_INDEX["V_LN_a_min_V"]] = 260.0
            sft = dt_to_filetime(base + dt.timedelta(seconds=n))
            eft = dt_to_filetime(base + dt.timedelta(seconds=n + 1))
            header = (RECORD_MAGIC
                      + struct.pack("<II", sft >> 32 & 0xFFFFFFFF, sft & 0xFFFFFFFF)
                      + struct.pack("<II", eft >> 32 & 0xFFFFFFFF, eft & 0xFFFFFFFF)
                      + struct.pack("<I", 0))
            fh.write(header + struct.pack(f"<{DATA_FLOATS}f", *vals))
            assert len(header) == HEADER_BYTES

    rules = _write(tmp_path, "rules.json",
                   json.dumps({"defaults": {"dip_pct_of_nominal": 0.95}}))

    # Without rules-file: no dip (260/277 = 93.9% > 90%).
    out0 = tmp_path / "out_default"
    assert main([str(d), "-o", str(out0), "--parse-only",
                 "--nominal-ln-v", "277", "--no-stats"]) == 0
    ev0 = json.loads((out0 / "events.json").read_text())
    assert not any(e["kind"] == "dip" for e in ev0)

    # With rules-file (95% threshold): the 260 V window is now a dip.
    out1 = tmp_path / "out_rules"
    assert main([str(d), "-o", str(out1), "--parse-only", "--nominal-ln-v", "277",
                 "--no-stats", "--rules-file", str(rules)]) == 0
    ev1 = json.loads((out1 / "events.json").read_text())
    assert any(e["kind"] == "dip" for e in ev1)
