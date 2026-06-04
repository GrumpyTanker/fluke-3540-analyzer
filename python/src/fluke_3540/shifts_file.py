"""Shift-window definitions loaded from a JSON file (``--shifts-file``).

Mirrors the ``--rules-file`` pattern: an alternative to the inline ``--shifts``
string for committing a reusable shift schedule alongside an asset.

JSON form::

    {
      "shifts": [
        {"name": "day",   "start": "06:00", "end": "18:00"},
        {"name": "night", "start": "18:00", "end": "06:00"}
      ]
    }

A bare top-level list of ``{name,start,end}`` dicts is also accepted. The same
HH:MM rules and wrap-past-midnight semantics as :class:`ShiftSet` apply.
"""
from __future__ import annotations

import json
from pathlib import Path

from .analysis import ShiftSet


def load_shifts(path: Path) -> ShiftSet:
    """Build a :class:`ShiftSet` from a JSON shifts file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        spec = raw.get("shifts")
        if spec is None:
            raise ValueError(
                f"{path}: shifts file must have a top-level 'shifts' list "
                "(or be a bare list of {name,start,end} objects)")
    elif isinstance(raw, list):
        spec = raw
    else:
        raise ValueError(f"{path}: shifts file must be an object or a list")
    if not isinstance(spec, list) or not spec:
        raise ValueError(f"{path}: 'shifts' must be a non-empty list")
    for i, s in enumerate(spec):
        if not isinstance(s, dict) or not {"name", "start", "end"} <= set(s):
            raise ValueError(
                f"{path}: shift #{i} must be an object with name/start/end")
    return ShiftSet.from_spec(spec)
