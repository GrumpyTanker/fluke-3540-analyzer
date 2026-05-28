"""Plain-prompt interactive pickers for --interactive mode. No TUI deps."""
from __future__ import annotations

from typing import Sequence

from .events import Event


def _parse_id_list(text: str, valid: set[int]) -> list[int] | None:
    """Parse 'all' / '' / '1,3,5' / '1-4,7'. Returns None on garbage; [] for none."""
    text = text.strip().lower()
    if text in ("", "all", "*"):
        return sorted(valid)
    if text in ("none", "skip"):
        return []
    picked: set[int] = set()
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            try:
                lo, hi = (int(x.strip()) for x in piece.split("-", 1))
            except ValueError:
                return None
            picked.update(range(lo, hi + 1))
        else:
            try:
                picked.add(int(piece))
            except ValueError:
                return None
    return sorted(picked & valid) if picked else []


def pick_events(events: Sequence[Event]) -> list[Event]:
    """Prompt the user to pick which events to render."""
    if not events:
        print("(no events detected)")
        return []
    print(f"\n{len(events)} event(s) detected:")
    for ev in events:
        phases = "/".join(ev.affected_phases) or "—"
        print(f"  [{ev.id:>3}]  {ev.kind:18s}  {ev.t_start.isoformat()}  "
              f"phases={phases}  severity={ev.severity:.3f}")
    valid_ids = {e.id for e in events}
    while True:
        raw = input(
            "Pick event IDs to render (e.g. 1,3-5  |  ALL  |  NONE): ",
        )
        picked = _parse_id_list(raw, valid_ids)
        if picked is None:
            print("  could not parse — try again")
            continue
        if not picked:
            print("  no events picked")
        return [e for e in events if e.id in picked]


def pick_quantities(defaults: Sequence[str], valid: Sequence[str]) -> list[str]:
    """Prompt the user for which chart quantities to render."""
    print(f"\nChart quantities (available: {', '.join(valid)})")
    print(f"  default: {', '.join(defaults)}")
    raw = input("Pick quantities (comma-separated, blank=default, 'all'): ").strip().lower()
    if raw in ("", "default"):
        return list(defaults)
    if raw in ("all", "*"):
        return list(valid)
    picked = [q.strip() for q in raw.split(",") if q.strip()]
    bad = [q for q in picked if q not in valid]
    if bad:
        print(f"  unknown: {bad}. Falling back to default.")
        return list(defaults)
    return picked
