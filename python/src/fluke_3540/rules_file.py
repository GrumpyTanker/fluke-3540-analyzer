"""Per-asset event-threshold overrides (Feature I).

``--rules-file FILE`` (JSON or TOML) overrides the default :class:`EventRules`
thresholds. The file may carry a flat set of defaults and/or a per-asset map so
one file can hold the known trip points for a whole fleet:

JSON::

    {
      "defaults": { "dip_pct_of_nominal": 0.92 },
      "assets": {
        "P115RE-MAC03": { "outage_v_threshold": 60.0, "swell_pct_of_nominal": 1.08 }
      }
    }

TOML::

    [defaults]
    dip_pct_of_nominal = 0.92

    [assets."P115RE-MAC03"]
    outage_v_threshold = 60.0

A flat file with only threshold keys (no ``defaults``/``assets``) is treated as
defaults. Per-asset values win over defaults; unknown keys are reported.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from .events import DEFAULT_RULES, EventRules

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None


_VALID_KEYS = {f.name for f in dataclasses.fields(EventRules)}


def _load_raw(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".toml":
        if tomllib is None:  # pragma: no cover
            raise ValueError("TOML rules-file requires Python 3.11+ (tomllib)")
        return tomllib.loads(text)
    if path.suffix.lower() in (".json", ".jsn"):
        return json.loads(text)
    # Try JSON first, then TOML, so a misnamed file still loads.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if tomllib is not None:
            return tomllib.loads(text)
        raise


def _split(raw: dict) -> tuple[dict, dict]:
    """Return (defaults, assets) from a raw rules dict.

    A file with no ``defaults``/``assets`` keys is treated as flat defaults.
    """
    if "defaults" in raw or "assets" in raw:
        return dict(raw.get("defaults") or {}), dict(raw.get("assets") or {})
    # Flat file = defaults only.
    return dict(raw), {}


def _coerce(overrides: dict, where: str) -> dict:
    """Validate keys + numeric-coerce values, raising on unknown keys."""
    out: dict = {}
    bad = [k for k in overrides if k not in _VALID_KEYS]
    if bad:
        raise ValueError(
            f"{where}: unknown EventRules key(s) {sorted(bad)}. "
            f"Valid: {sorted(_VALID_KEYS)}")
    for k, v in overrides.items():
        # min_duration_secs / gap_tolerance_secs are ints; the rest are floats.
        if k in ("min_duration_secs", "gap_tolerance_secs"):
            out[k] = int(v)
        else:
            out[k] = float(v)
    return out


def load_rules(path: Path, asset_name: str | None = None,
               base: EventRules = DEFAULT_RULES) -> EventRules:
    """Build an :class:`EventRules` from a rules file for the given asset.

    Precedence (low → high): base defaults → file ``defaults`` → file
    ``assets[asset_name]``. Asset lookup matches ``asset_name`` exactly, then
    falls back to a ``"default"`` entry under ``assets`` if present.
    """
    raw = _load_raw(path)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: rules file must be a mapping at the top level")
    defaults, assets = _split(raw)
    merged = dict(_coerce(defaults, f"{path}:defaults"))
    asset_over: dict = {}
    if asset_name and asset_name in assets:
        asset_over = assets[asset_name]
    elif "default" in assets:
        asset_over = assets["default"]
    merged.update(_coerce(asset_over, f"{path}:assets"))
    return dataclasses.replace(base, **merged)


def describe_overrides(path: Path, asset_name: str | None,
                       base: EventRules = DEFAULT_RULES) -> list[str]:
    """Return human-readable 'key: base -> new' lines for the applied overrides."""
    rules = load_rules(path, asset_name, base)
    lines: list[str] = []
    for f in dataclasses.fields(EventRules):
        b = getattr(base, f.name)
        n = getattr(rules, f.name)
        if b != n:
            lines.append(f"{f.name}: {b} -> {n}")
    return lines
