"""fluke-analyze — orchestrate parse → detect → render for a Fluke session.

Modes:
    --auto         (default) parse, detect, render everything
    --interactive  prompt-driven pickers
    --parse-only   produce CSV + events.json, no plots
    --plot-only    reuse existing CSV + events.json, just render
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Sequence

import csv as _csv

from .events import Event, detect_events
from .insights import Finding, analyze as analyze_insights, to_jsonable as finding_to_jsonable
from .parser import (
    _parse_reverse_cts_arg, compute_time_shift, export_csv, export_csv_multi,
    find_session_files, from_csv, iter_records, open_session,
)
from .store import ColumnStore
from .plots import (
    GnuplotNotFound, WeasyPrintNotInstalled,
    render_event_zoom, render_full_session, render_snapshot_zoom,
    write_html_report, write_pdf_report, write_xlsx,
)
from .plots.full_session import QUANTITY_SPECS as FULL_QUANTITIES
from .snapshots import Snapshot, pick_snapshots


DEFAULT_PLOTS = ("voltage", "current", "power", "thd", "pf", "frequency")
DEFAULT_ZOOM_PLOTS = ("voltage", "current", "power")


def _isoformat(d: dt.datetime) -> str:
    return d.isoformat()


def _parse_time(text: str) -> dt.datetime:
    """Accept ISO-8601 with or without timezone; treat naive as UTC."""
    t = dt.datetime.fromisoformat(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def _event_to_json(ev: Event) -> dict:
    d = asdict(ev)
    d["t_start"] = _isoformat(ev.t_start)
    d["t_end"] = _isoformat(ev.t_end)
    d["affected_phases"] = list(ev.affected_phases)
    return d


def _event_from_json(d: dict) -> Event:
    return Event(
        id=d["id"], kind=d["kind"],
        t_start=_parse_time(d["t_start"]),
        t_end=_parse_time(d["t_end"]),
        severity=d["severity"],
        affected_phases=tuple(d["affected_phases"]),
    )


def _snapshot_to_json(s: Snapshot) -> dict:
    return {
        "id": s.id,
        "t_start": _isoformat(s.t_start),
        "t_end": _isoformat(s.t_end),
        "t_center": _isoformat(s.t_center),
        "p_total_mean_w": s.p_total_mean_w,
        "p_total_stdev_w": s.p_total_stdev_w,
    }


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fluke-analyze", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("session_dir", type=Path,
                    help="Path to an ES.NNN session directory, a .fel zip-bundle, "
                         "or a pre-parsed session.csv")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output directory (default: <session>_out next to the session)")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--auto", action="store_true",
                      help="Default — parse, detect, render everything")
    mode.add_argument("--interactive", action="store_true",
                      help="Prompt for event/quantity/phase picks")
    mode.add_argument("--parse-only", action="store_true",
                      help="Write CSV + events.json + summary; no charts")
    mode.add_argument("--plot-only", action="store_true",
                      help="Reuse existing CSV + events.json; render charts only")
    mode.add_argument("--json", action="store_true", dest="json_mode",
                      help="Parse + detect, emit a single JSON blob to stdout, "
                           "no charts, no logs. Useful for piping into jq.")

    # Filters
    ap.add_argument("--from", dest="from_time", type=_parse_time, default=None,
                    metavar="ISO_TIME", help="Restrict event scan & zooms to ≥ this time")
    ap.add_argument("--to", dest="to_time", type=_parse_time, default=None,
                    metavar="ISO_TIME", help="Restrict event scan & zooms to ≤ this time")
    ap.add_argument("--pre", type=int, default=30, metavar="SECS",
                    help="Pre-event zoom padding in seconds (default 30)")
    ap.add_argument("--post", type=int, default=60, metavar="SECS",
                    help="Post-event zoom padding in seconds (default 60)")
    ap.add_argument("--events", type=str, default=None, metavar="IDS",
                    help="Comma-separated event IDs to render (default: all)")
    ap.add_argument("--plot", type=str, default=None, metavar="QTYS",
                    help=f"Comma-separated quantities to chart "
                         f"(default: {','.join(DEFAULT_PLOTS)}). "
                         f"Valid: {','.join(sorted(FULL_QUANTITIES))}")
    ap.add_argument("--phase", type=str, default=None, metavar="PHASES",
                    help="Comma-separated phases (a,b,c,total). Reserved for future "
                         "per-phase chart filtering; currently informational.")
    ap.add_argument("--snapshots", type=int, default=3, metavar="N",
                    help="Number of normal-operation snapshots to pick (default 3)")

    # Parse options
    ap.add_argument("--reverse-cts", nargs="?", const="all", default=None,
                    metavar="PHASES",
                    help="Negate P/Q/PF/DPF/Wh/VARh for backwards iFlex CTs. "
                         "Bare flag = all phases; pass a comma list like 'a,c' to "
                         "only flip those phases (plus totals).")
    ap.add_argument("--auto-reverse-cts", action="store_true",
                    help="Auto-detect a reversed-CT install and apply "
                         "--reverse-cts automatically, with a loud notice. The "
                         "heuristic decides on the dominant high-current "
                         "(active) state: is real power negative when current "
                         "is high? No-op if the active state already reads as a "
                         "normal load.")
    ap.add_argument("--standby-threshold-a", dest="standby_threshold_a",
                    type=float, default=None, metavar="A",
                    help="Per-phase mean current (A) at/above which a record "
                         "counts as ACTIVE load (else standby). Default 50. "
                         "Drives the active/standby load-state split, the "
                         "energy correction, and the magnitude-weighted "
                         "reverse-CTs decision. See docs/LOAD_STATES.md.")
    ap.add_argument("--load-states", dest="load_states", action="store_true",
                    help="Force the active/standby load-state report "
                         "(load_states.csv/json). Emitted by default in --auto; "
                         "this flag is only needed to opt in elsewhere.")
    ap.add_argument("--every", type=int, default=1, metavar="K",
                    help="Emit every K-th record into the CSV (default 1, all)")
    ap.add_argument("--max-csv-rows", type=int, default=None, metavar="N",
                    help="Cap the full-resolution CSV at ~N rows. If the session "
                         "would exceed it, the stride is raised and the downsampling "
                         "is logged. The 1-min CSV and analysis keep full resolution.")

    # Clock-correction anchors (mutually exclusive)
    anchor = ap.add_mutually_exclusive_group()
    anchor.add_argument("--anchor-start", dest="anchor_start", type=_parse_time,
                        default=None, metavar="ISO_TIME",
                        help="Pin the real wall-clock START to this time, correcting "
                             "a wrong meter RTC. Shifts every timestamp by the delta.")
    anchor.add_argument("--anchor-end", dest="anchor_end", type=_parse_time,
                        default=None, metavar="ISO_TIME",
                        help="Pin the real wall-clock END to this time (mutually "
                             "exclusive with --anchor-start).")

    # Time-bucket splitting
    # Timezone-aware reporting (Feature H)
    ap.add_argument("--tz", dest="tz", type=str, default=None, metavar="ZONE",
                    help="IANA timezone (e.g. America/Chicago) for report "
                         "timestamps. Reports then show local + UTC. Default UTC "
                         "only. Anchors already accept ISO offsets.")

    ap.add_argument("--split-by", dest="split_by", type=str, default=None,
                    metavar="PERIOD",
                    help="Partition the session into time buckets, emitting a full "
                         "per-bucket report plus a roll-up. PERIOD: hour|day|week, "
                         "a duration like 30m, 6h, 2d, OR 'shifts' for named "
                         "shift windows (see --shifts / --shifts-file).")

    # Named, configurable shift windows (--split-by shifts)
    ap.add_argument("--shifts", dest="shifts", type=str, default=None,
                    metavar="SPEC",
                    help="Shift windows as 'name=HH:MM-HH:MM,...' (comma-separated). "
                         "A window where end<=start wraps past midnight "
                         "(e.g. night=18:00-06:00). Interpreted in --tz (UTC if "
                         "unset). Default day=06:00-18:00,night=18:00-06:00. "
                         "Requires --split-by shifts.")
    ap.add_argument("--shifts-file", dest="shifts_file", type=Path, default=None,
                    metavar="FILE",
                    help="JSON file of shift windows "
                         "({\"shifts\":[{\"name\",\"start\",\"end\"}]}); an "
                         "alternative to --shifts. See docs/SHIFTS.md.")

    # Event markers / correlation
    ap.add_argument("--mark", action="append", default=None, metavar="ISO=LABEL",
                    help="Add an event marker 'ISO_TIME=label' (repeatable). Anchored "
                         "timeline applies. Cross-referenced against detected events.")
    ap.add_argument("--marks", type=Path, default=None, metavar="FILE.csv",
                    help="CSV of markers with columns time,label.")

    # Whole-session statistics + diurnal profile
    ap.add_argument("--no-stats", action="store_true",
                    help="Skip whole-session statistics (stats.json/csv).")
    ap.add_argument("--tod-profile", dest="tod_profile", nargs="?",
                    const="00:00-24:00", default=None, metavar="HH:MM-HH:MM",
                    help="Build a time-of-day (diurnal) profile. Bare flag = full "
                         "24 h; pass a window like 08:00-17:00.")
    ap.add_argument("--tod-bin", dest="tod_bin", type=int, default=1, metavar="MINS",
                    help="Time-of-day bin width in minutes (default 1).")
    ap.add_argument("--demand-window", dest="demand_window", type=int,
                    default=900, metavar="SECS",
                    help="Rolling demand window in seconds (default 900 = 15 min). "
                         "Reports peak demand + the window it occurred in.")

    # Output knobs
    ap.add_argument("--no-xlsx", action="store_true", help="Skip XLSX report")
    ap.add_argument("--no-html", action="store_true",
                    help="Skip the self-contained HTML report (default writes report.html)")
    ap.add_argument("--pdf", action="store_true",
                    help="Also render report.pdf via weasyprint (install with "
                         "`pip install fluke-3540-analyzer[pdf]`)")
    ap.add_argument("--no-overview", action="store_true",
                    help="Skip the overview multiplot")
    ap.add_argument("--format", choices=("png", "svg"), default="png",
                    help="Chart image format (default png)")
    ap.add_argument("--nominal-ln-v", type=float, default=None, metavar="V",
                    help="Nominal L-N voltage (auto-inferred if omitted)")
    ap.add_argument("--rules-file", dest="rules_file", type=Path, default=None,
                    metavar="FILE",
                    help="JSON/TOML file overriding EventRules thresholds, keyed "
                         "by asset_id/name (see docs/RULES_FILE.md). Per-asset "
                         "values win over the file's defaults.")

    return ap


def _standby_threshold(args: argparse.Namespace) -> float:
    """Resolve the active/standby current cut (A), defaulting to the module value."""
    from .analysis import STANDBY_CURRENT_THRESHOLD_A
    v = getattr(args, "standby_threshold_a", None)
    return float(v) if v is not None else STANDBY_CURRENT_THRESHOLD_A


def _is_csv_input(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".csv"


def _resolve_outdir(args: argparse.Namespace) -> Path:
    if args.output:
        return args.output
    name = args.session_dir.name
    for suf in (".fel", ".csv"):
        if name.lower().endswith(suf):
            name = name[:-len(suf)]
            break
    return args.session_dir.parent / (name + "_out")


def _downsample_csv(src: Path, dst: Path, every: int) -> int:
    """Write a CSV containing only every Nth data row from src; header preserved."""
    if every <= 1:
        dst.write_bytes(src.read_bytes())
        return -1
    rows = 0
    with src.open("r", newline="", encoding="utf-8") as inp, \
         dst.open("w", newline="", encoding="utf-8") as out:
        reader = _csv.reader(inp)
        writer = _csv.writer(out)
        header = next(reader, None)
        if header is not None:
            writer.writerow(header)
        for i, row in enumerate(reader):
            if i % every == 0:
                writer.writerow(row)
                rows += 1
    return rows


def _parse_csv_session(args: argparse.Namespace, outdir: Path,
                      ) -> tuple[Path, Path, dict]:
    """CSV-input replacement for _parse_session: copies + downsamples."""
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    if args.session_dir.resolve() != full_csv.resolve():
        full_csv.write_bytes(args.session_dir.read_bytes())
    print(f"[csv-in] {args.session_dir}  →  {full_csv}")
    _downsample_csv(full_csv, min_csv, every=max(60, args.every))
    return full_csv, min_csv, {}  # config unavailable for CSV input


def _parse_session(args: argparse.Namespace, outdir: Path,
                   session_dir: Path,
                   ) -> tuple[Path, Path, dict, ColumnStore]:
    """Phase 1A: single-pass parse trend.bin → CSVs + ColumnStore.

    Walks the binary exactly once (was twice), building the full CSV, the 1-min
    CSV, and the in-memory analysis store together. Applies the --anchor-* clock
    correction and the --max-csv-rows guard.
    """
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    reverse_cts = _parse_reverse_cts_arg(args.reverse_cts)
    trend = find_session_files(session_dir)["trend"]

    time_shift = compute_time_shift(
        trend,
        anchor_start=getattr(args, "anchor_start", None),
        anchor_end=getattr(args, "anchor_end", None),
    )
    if time_shift:
        print(f"[parse] clock anchor: shifting timestamps by {time_shift}")

    print(f"[parse] {args.session_dir}  →  {full_csv} (+ 1-min, single pass)")
    res = export_csv_multi(
        session_dir, full_csv, min_csv,
        every=args.every, reverse_cts=reverse_cts,
        max_full_rows=getattr(args, "max_csv_rows", None),
        time_shift=time_shift, build_store=True,
        log=print, progress_every=100_000,
    )
    st = res["parse_stats"]
    print(f"        {res['rows_written_full']:,} full rows, "
          f"{res['rows_written_min']:,} 1-min rows, store n={res['store'].n:,}")
    if st.bad_magic or st.truncated or st.nonfinite:
        print(f"        robustness: {st.bad_magic} bad-magic, "
              f"{st.truncated} truncated, {st.nonfinite} non-finite (skipped/flagged)")

    # CT-reversal auto-detection (Feature C). Always check + notify; with
    # --auto-reverse-cts (and no explicit --reverse-cts already applied) re-run
    # the single-pass parse with the correction applied.
    store = res["store"]
    from .analysis import ct_reversal_notice, detect_ct_reversal
    thr = _standby_threshold(args)
    ct = detect_ct_reversal(store, active_threshold_a=thr)
    if ct["reversed"]:
        print(ct_reversal_notice(ct))
        already_reversed = bool(reverse_cts)
        if getattr(args, "auto_reverse_cts", False) and not already_reversed:
            print("[parse] --auto-reverse-cts: re-parsing with reverse-CTs applied…")
            res = export_csv_multi(
                session_dir, full_csv, min_csv,
                every=args.every, reverse_cts=True,
                max_full_rows=getattr(args, "max_csv_rows", None),
                time_shift=time_shift, build_store=True,
                log=print, progress_every=100_000,
            )
            store = res["store"]
            ct_after = detect_ct_reversal(store, active_threshold_a=thr)
            if ct_after.get("basis") == "active":
                print(f"        after auto-reverse: active-state P now negative "
                      f"for {ct_after['active_frac_negative'] * 100:.1f}% "
                      f"(active mean P = {ct_after['active_mean_p_w'] / 1000:.1f} kW)")
            else:
                print(f"        after auto-reverse: P now negative for "
                      f"{ct_after['frac_negative'] * 100:.1f}% of non-outage time "
                      f"(mean P = {ct_after['mean_p_w'] / 1000:.1f} kW)")
    return full_csv, min_csv, res["config"], store


def _store_from_csv(csv_path: Path) -> ColumnStore:
    """Build a ColumnStore from a previously-exported session CSV (streaming)."""
    return ColumnStore.from_records(from_csv(csv_path))


def _window_filter_store(store: ColumnStore, from_time, to_time) -> ColumnStore:
    """Return a store restricted to records within [from_time, to_time].

    Operates on the already-shifted timeline (store.start/end apply time_shift).
    Cheap: copies only the retained columns, not raw Records.
    """
    if not from_time and not to_time:
        return store
    keep = ColumnStore(time_shift=store.time_shift)
    cols = store._cols
    keep_cols = keep._cols
    for i in range(store.n):
        s = store.start(i)
        e = store.end(i)
        if from_time and s < from_time:
            continue
        if to_time and e > to_time:
            continue
        for name in store.columns:
            keep_cols[name].append(cols[name][i])
        keep._start_ticks.append(store._start_ticks[i])
        keep._end_ticks.append(store._end_ticks[i])
        keep._n += 1
    return keep


def _detect_and_save(args: argparse.Namespace, outdir: Path,
                     trend_path: Path | None,
                     config: dict | None = None,
                     *,
                     csv_path: Path | None = None,
                     store: ColumnStore | None = None,
                     ) -> tuple[list[Event], list[Snapshot], list[Finding], ColumnStore]:
    """Phase 1B: run event + snapshot + insights detection, write JSON.

    Pass a prebuilt store (binary input), or csv_path (CSV input). Returns the
    (possibly window-filtered) store alongside the detections.
    """
    print("[detect] running event scan…")
    if store is None:
        if csv_path is not None:
            store = _store_from_csv(csv_path)
        else:
            store = ColumnStore.from_records(iter_records(trend_path))
    if args.from_time or args.to_time:
        before = store.n
        store = _window_filter_store(store, args.from_time, args.to_time)
        print(f"         window filter: {before:,} → {store.n:,} records")

    # Per-asset threshold overrides (Feature I).
    rules = _load_event_rules(args, config)
    events = detect_events(store, nominal_ln_v=args.nominal_ln_v, rules=rules)
    snaps = pick_snapshots(store, events, n=args.snapshots)
    findings = analyze_insights(store, events, snaps, config or {})
    print(f"         {len(events)} events, {len(snaps)} snapshots, "
          f"{len(findings)} insight(s)")

    events_path = outdir / "events.json"
    snaps_path = outdir / "snapshots.json"
    insights_path = outdir / "insights.json"
    events_path.write_text(json.dumps(
        [_event_to_json(e) for e in events], indent=2,
    ), encoding="utf-8")
    snaps_path.write_text(json.dumps(
        [_snapshot_to_json(s) for s in snaps], indent=2,
    ), encoding="utf-8")
    insights_path.write_text(json.dumps(
        [finding_to_jsonable(f) for f in findings], indent=2,
    ), encoding="utf-8")
    print(f"         wrote {events_path.name}, {snaps_path.name}, {insights_path.name}")
    return events, snaps, findings, store


def _load_event_rules(args: argparse.Namespace, config: dict | None):
    """Return EventRules, applying --rules-file overrides for this asset."""
    from .events import DEFAULT_RULES
    rules_path = getattr(args, "rules_file", None)
    if not rules_path:
        return DEFAULT_RULES
    from .rules_file import describe_overrides, load_rules
    asset = (config or {}).get("asset_name")
    try:
        rules = load_rules(rules_path, asset_name=asset)
    except (OSError, ValueError) as e:
        print(f"ERROR: --rules-file {rules_path}: {e}", file=sys.stderr)
        raise SystemExit(2)
    diffs = describe_overrides(rules_path, asset)
    if diffs:
        print(f"[rules] {rules_path} (asset={asset or 'n/a'}): "
              + "; ".join(diffs))
    else:
        print(f"[rules] {rules_path}: no overrides applied")
    return rules


def _infer_nominal_ln_v(store: ColumnStore, fallback: float | None) -> float:
    """Best-effort nominal L-N voltage for ITIC residual %."""
    if fallback:
        return fallback
    from statistics import median
    pooled = []
    for name in ("V_LN_a_avg_V", "V_LN_b_avg_V", "V_LN_c_avg_V"):
        col = store.col(name)
        for v in col:
            if v > 50.0:
                pooled.append(v)
    return median(pooled) if pooled else 277.0


def _parse_markers(args: argparse.Namespace, time_shift) -> list:
    """Collect markers from --mark and --marks, applying the time shift.

    Marker times are given on the meter's raw timeline; we add the same shift
    used everywhere so they land on the anchored wall-clock.
    """
    from .analysis import Marker
    markers: list[Marker] = []
    for spec in (args.mark or []):
        iso, _, label = spec.partition("=")
        try:
            t = _parse_time(iso.strip())
        except ValueError:
            print(f"[mark] could not parse time in {spec!r} — skipped", file=sys.stderr)
            continue
        markers.append(Marker(time=t + time_shift, label=label.strip() or iso.strip()))
    if args.marks:
        try:
            with args.marks.open(newline="", encoding="utf-8") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    raw = row.get("time") or row.get("timestamp") or ""
                    try:
                        t = _parse_time(raw.strip())
                    except ValueError:
                        continue
                    markers.append(Marker(time=t + time_shift,
                                          label=(row.get("label") or "").strip() or raw))
        except OSError as e:
            print(f"[marks] could not read {args.marks}: {e}", file=sys.stderr)
    return markers


def _augment_events_itic(outdir: Path, events: Sequence[Event],
                         nominal_ln_v: float) -> None:
    """Rewrite events.json with an `itic` block on each dip/outage/swell."""
    from .analysis import event_itic
    payload = []
    for ev in events:
        d = _event_to_json(ev)
        info = event_itic(ev, nominal_ln_v)
        if info:
            d["itic"] = info
        payload.append(d)
    (outdir / "events.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_stats(outdir: Path, store: ColumnStore) -> dict:
    from .analysis import whole_session_stats
    stats = whole_session_stats(store)
    (outdir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    # CSV: one row per channel
    with (outdir / "stats.csv").open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["channel", "unit", "count", "min", "p1", "p5", "median",
                    "mean", "p95", "p99", "max", "stdev"])
        for name, d in stats.items():
            if name.startswith("_"):
                continue
            w.writerow([name, d["unit"], d["count"], d["min"], d["p1"], d["p5"],
                        d["median"], d["mean"], d["p95"], d["p99"], d["max"],
                        d["stdev"]])
    print(f"[stats] wrote stats.json + stats.csv")
    return stats


def _write_load_states(outdir: Path, store: ColumnStore,
                       threshold_a: float) -> dict:
    """Active/standby load-state split + the three energy figures.

    Writes load_states.csv (one row per state) + load_states.json (rows +
    threshold + the three energy figures + the standby-sign caveat). Returns the
    payload so the narrative/summary can surface the active PF + energy.
    """
    from .analysis import load_state_rows, session_energy
    rows = load_state_rows(store, threshold_a=threshold_a)
    energy = session_energy(store, threshold_a=threshold_a)
    cols = ["state", "records", "hours", "duty_pct", "kWh", "P_avg_kW",
            "P_min_kW", "P_max_kW", "I_avg_A", "S_avg_kVA", "PF_avg",
            "V_LN_avg_V", "V_THD_p95_pct"]
    with (outdir / "load_states.csv").open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt_load_state_cell(c, r[c]) for c in cols])
    payload = {
        "standby_threshold_a": threshold_a,
        "states": rows,
        "energy": energy,
        "note": energy["note"],
    }
    (outdir / "load_states.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    by = {r["state"]: r for r in rows}
    a = by.get("active", {})
    s = by.get("standby", {})
    print(f"[load] active {a.get('duty_pct', 0):.0f}% duty: "
          f"I={a.get('I_avg_A', 0):.0f} A  P={a.get('P_avg_kW', 0):+.1f} kW  "
          f"PF={a.get('PF_avg', 0):+.2f} | standby "
          f"I={s.get('I_avg_A', 0):.0f} A  P={s.get('P_avg_kW', 0):+.1f} kW")
    print(f"[load] energy kWh — as-measured {energy['energy_as_measured_kWh']:.0f} | "
          f"active {energy['energy_active_kWh']:.0f} | "
          f"net(clip standby≥0) {energy['energy_net_clip_standby_kWh']:.0f}")
    return payload


def _fmt_load_state_cell(col: str, val):
    """Format one load_states.csv cell (round floats; pass ints/strings)."""
    if col == "state" or isinstance(val, int):
        return val
    if col in ("kWh", "P_avg_kW", "P_min_kW", "P_max_kW", "S_avg_kVA"):
        return f"{val:.2f}"
    if col == "PF_avg":
        return f"{val:.3f}"
    if col in ("hours", "duty_pct", "I_avg_A", "V_LN_avg_V", "V_THD_p95_pct"):
        return f"{val:.2f}"
    return f"{val:.2f}"


def _write_markers(outdir: Path, markers, events: Sequence[Event]) -> None:
    from .analysis import correlate_markers
    corr = correlate_markers(markers, events)
    (outdir / "markers.json").write_text(json.dumps(corr, indent=2), encoding="utf-8")
    print(f"[mark] {len(markers)} marker(s) correlated → markers.json")
    for c in corr:
        ne = c["nearest_event"]
        if ne:
            print(f"        '{c['label']}' nearest {ne['kind']} #{ne['id']} "
                  f"(offset {ne['offset_secs']:+.1f}s)")


def _write_tod(outdir: Path, store: ColumnStore, args: argparse.Namespace) -> None:
    from .analysis import parse_tod_window, time_of_day_profile
    window = parse_tod_window(args.tod_profile)
    rows = time_of_day_profile(store, window=window, bin_minutes=max(1, args.tod_bin))
    with (outdir / "time_of_day_profile.csv").open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["bin", "n", "n_days", "p_avg_kW", "p_min_kW", "p_max_kW",
                    "v_avg_V", "v_min_V", "v_max_V", "i_avg_A", "i_min_A", "i_max_A"])
        for r in rows:
            w.writerow([r["bin"], r["n"], r["n_days"], r["p_avg_kW"], r["p_min_kW"],
                        r["p_max_kW"], r["v_avg_V"], r["v_min_V"], r["v_max_V"],
                        r["i_avg_A"], r["i_min_A"], r["i_max_A"]])
    print(f"[tod] time-of-day profile ({args.tod_profile}) → "
          f"time_of_day_profile.csv ({len(rows)} bins)")
    return rows


def _run_split_by(args: argparse.Namespace, outdir: Path, store: ColumnStore,
                  events: Sequence[Event], config: dict,
                  nominal_ln_v: float) -> None:
    """Emit a full per-bucket report + buckets_summary.csv roll-up."""
    from .analysis import (assign_buckets, bucket_label, bucket_summary_row,
                           parse_period, slice_store)
    period = parse_period(args.split_by)
    buckets = assign_buckets(store, period)
    print(f"[split] --split-by {args.split_by}: {len(buckets)} bucket(s)")

    summary_rows = []
    for (start, lo, hi) in buckets:
        label = bucket_label(start, period)
        sub = slice_store(store, lo, hi)
        bdir = outdir / label
        bdir.mkdir(parents=True, exist_ok=True)
        # Per-bucket events (filed under their t_start bucket; flag spanners).
        b_end = start + dt.timedelta(seconds=period.seconds)
        bucket_events = [e for e in events if start <= e.t_start < b_end]
        spanning = [e for e in bucket_events if e.t_end >= b_end]
        # Per-bucket CSV slice (from the in-memory store columns).
        _write_bucket_csv(bdir / "session.csv", sub)
        # Per-bucket events.json with ITIC
        from .analysis import event_itic
        ev_payload = []
        for e in bucket_events:
            d = _event_to_json(e)
            info = event_itic(e, nominal_ln_v)
            if info:
                d["itic"] = info
            if e in spanning:
                d["spans_bucket_boundary"] = True
            ev_payload.append(d)
        (bdir / "events.json").write_text(json.dumps(ev_payload, indent=2),
                                          encoding="utf-8")
        # Per-bucket summary.txt
        b_findings = analyze_insights(sub, bucket_events, [], config)
        b_snaps = pick_snapshots(sub, bucket_events, n=1)
        _write_summary_txt(bdir, bucket_events, b_snaps, b_findings, config)
        # Per-bucket XLSX (best-effort)
        if not args.no_xlsx:
            try:
                write_xlsx(bdir / "session.csv", bdir / "report.xlsx",
                           config=config, csv_per_second_path=bdir / "session.csv")
            except Exception as e:  # pragma: no cover - xlsx best-effort
                print(f"[split]   xlsx for {label} failed: {e}", file=sys.stderr)
        summary_rows.append(bucket_summary_row(label, sub, bucket_events))

    # Roll-up summary CSV
    with (outdir / "buckets_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["bucket", "records", "V_min_V", "V_avg_V", "V_max_V",
                    "I_max_A", "kWh", "n_outages", "n_dips", "n_swells",
                    "worst_PF", "peak_kW"])
        for r in summary_rows:
            w.writerow([r["bucket"], r["records"], f"{r['V_min_V']:.1f}",
                        f"{r['V_avg_V']:.1f}", f"{r['V_max_V']:.1f}",
                        f"{r['I_max_A']:.1f}", f"{r['kWh']:.2f}", r["n_outages"],
                        r["n_dips"], r["n_swells"], f"{r['worst_PF']:.3f}",
                        f"{r['peak_kW']:.2f}"])
    print(f"[split] wrote buckets_summary.csv ({len(summary_rows)} rows) + "
          f"per-bucket reports under {outdir}/<label>/")


def _resolve_shift_set(args: argparse.Namespace):
    """Build the ShiftSet from --shifts / --shifts-file, or the default."""
    from .analysis import ShiftSet
    if getattr(args, "shifts_file", None):
        from .shifts_file import load_shifts
        return load_shifts(args.shifts_file), f"file:{args.shifts_file}"
    if getattr(args, "shifts", None):
        return ShiftSet.parse(args.shifts), args.shifts
    return ShiftSet.default(), "default(day=06:00-18:00,night=18:00-06:00)"


def _run_shifts(args: argparse.Namespace, outdir: Path, store: ColumnStore,
                events: Sequence[Event], config: dict,
                nominal_ln_v: float) -> None:
    """Generalized named-shift splitting (--split-by shifts).

    Emits (1) the headline per-shift-name aggregate comparison
    (shift_comparison.csv + .json) and (2) per-occurrence buckets reusing the
    standard per-bucket report machinery. Windows are evaluated in --tz.
    """
    from .analysis import (event_itic, shift_comparison_rows,
                           shift_occurrences, slice_store)
    from .tzutil import tz_label

    tz = getattr(args, "_tz", None)
    tz_name = getattr(args, "tz", None)
    ss, spec_label = _resolve_shift_set(args)
    demand_min = max(1, getattr(args, "demand_window", 900) // 60)

    print(f"[shifts] --split-by shifts ({spec_label}); windows in "
          f"{tz_label(tz, tz_name)}")
    for issue in ss.coverage_issues():
        print(f"[shifts]   WARNING: {issue}", file=sys.stderr)

    # (1) Headline aggregate comparison ------------------------------------
    rows = shift_comparison_rows(store, ss, events, tz=tz,
                                 nominal_ln_v=nominal_ln_v,
                                 demand_window=demand_min,
                                 standby_threshold_a=_standby_threshold(args))
    cols = ["shift", "window", "records", "hours", "kWh", "P_total_avg_W",
            "P_total_min_W", "P_total_max_W", "peak_demand_kW",
            "peak_demand_window_secs", "PF_avg", "V_LN_avg_V", "V_LN_p5_V",
            "V_LN_p95_V", "V_THD_p95_pct", "n_outages", "n_dips", "n_swells",
            "outage_minutes", "active_records", "active_duty_pct",
            "active_kWh", "active_PF_avg"]
    with (outdir / "shift_comparison.csv").open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt_shift_cell(c, r[c]) for c in cols])
    payload = {
        "tz": tz_label(tz, tz_name),
        "spec": spec_label,
        "demand_window_secs": demand_min * 60,
        "coverage_issues": ss.coverage_issues(),
        "shifts": rows,
    }
    (outdir / "shift_comparison.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    for r in rows:
        print(f"[shifts]   {r['shift']:10s} {r['window']:>13s}  "
              f"{r['records']:>7d} rec  {r['hours']:6.2f} h  "
              f"{r['kWh']:9.2f} kWh  Pavg={r['P_total_avg_W']/1000:7.2f} kW  "
              f"peak={r['peak_demand_kW']:7.2f} kW")

    # (2) Per-occurrence buckets (contiguous) ------------------------------
    occurrences = shift_occurrences(store, ss, tz=tz)
    for (label, name, lo, hi) in occurrences:
        sub = slice_store(store, lo, hi)
        safe = label.replace(" ", "_").replace(":", "")
        bdir = outdir / "shifts" / safe
        bdir.mkdir(parents=True, exist_ok=True)
        b_start = store.start(lo)
        b_end = store.end(hi - 1)
        bucket_events = [e for e in events if b_start <= e.t_start < b_end]
        _write_bucket_csv(bdir / "session.csv", sub)
        ev_payload = []
        for e in bucket_events:
            d = _event_to_json(e)
            info = event_itic(e, nominal_ln_v)
            if info:
                d["itic"] = info
            ev_payload.append(d)
        (bdir / "events.json").write_text(json.dumps(ev_payload, indent=2),
                                          encoding="utf-8")
        b_findings = analyze_insights(sub, bucket_events, [], config)
        b_snaps = pick_snapshots(sub, bucket_events, n=1)
        _write_summary_txt(bdir, bucket_events, b_snaps, b_findings, config,
                           tz=tz, tz_name=tz_name, store=sub)
    print(f"[shifts] wrote shift_comparison.csv/json + "
          f"{len(occurrences)} per-occurrence report(s) under {outdir}/shifts/")
    return rows


def _fmt_shift_cell(col: str, val):
    """Format one comparison-CSV cell (round floats; pass ints/strings)."""
    if col in ("shift", "window") or isinstance(val, int):
        return val
    if col == "kWh":
        return f"{val:.2f}"
    if col in ("hours", "outage_minutes", "active_kWh"):
        return f"{val:.2f}"
    if col in ("PF_avg", "active_PF_avg"):
        return f"{val:.3f}"
    if col in ("peak_demand_kW",):
        return f"{val:.2f}"
    return f"{val:.1f}"


def _write_bucket_csv(path: Path, sub: ColumnStore) -> None:
    """Write a CSV for a bucket slice from the store's retained columns."""
    cols = list(sub.columns)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["timestamp_utc", "window_end_utc", *cols])
        col_arrays = [sub.col(c) for c in cols]
        for i in range(sub.n):
            w.writerow([sub.start(i).isoformat(), sub.end(i).isoformat(),
                        *[ca[i] for ca in col_arrays]])


def _run_extra_analyses(args: argparse.Namespace, outdir: Path,
                        store: ColumnStore,
                        events: Sequence[Event],
                        findings: Sequence[Finding],
                        config: dict,
                        full_csv: Path, min_csv: Path) -> dict:
    """Run the round-2 analysis features that depend on the in-memory store.

    Each feature is opt-in via its flag. Returns the stats dict (or {}) so the
    renderer can surface a Statistics sheet.
    """
    nominal_ln_v = _infer_nominal_ln_v(store, args.nominal_ln_v)
    threshold_a = _standby_threshold(args)
    # ITIC always augments events.json (cheap, high-value for the deliverable).
    _augment_events_itic(outdir, events, nominal_ln_v)

    # CT-reversal status snapshot for reports/web (cheap; one pass). The decision
    # is made on the dominant active (high-current) state.
    from .analysis import detect_ct_reversal, ieee519_compliance, sarfi_indices
    ct = detect_ct_reversal(store, active_threshold_a=threshold_a)
    (outdir / "ct_reversal.json").write_text(json.dumps(ct, indent=2), encoding="utf-8")

    # IEEE 519 (THD) + IEEE 1159 / SARFI power-quality (Feature F).
    pq = {
        "ieee519": ieee519_compliance(store),
        "sarfi": sarfi_indices(events, nominal_ln_v),
    }
    (outdir / "pq_standards.json").write_text(json.dumps(pq, indent=2), encoding="utf-8")
    v = pq["ieee519"]["voltage"]
    print(f"[pq] IEEE 519 V_THD p95: a={v['a']['p95']:.1f}% b={v['b']['p95']:.1f}% "
          f"c={v['c']['p95']:.1f}% (limit {pq['ieee519']['limit_v_thd_pct']:.0f}%) — "
          f"{'COMPLIANT' if pq['ieee519']['all_voltage_compliant'] else 'NON-COMPLIANT'}; "
          f"SARFI-90={pq['sarfi']['SARFI-90']}")

    # Demand analysis (Feature G).
    from .analysis import demand_analysis
    demand_window = max(1, getattr(args, "demand_window", 900))
    # Emit a series sampled at ~1/60 of the window so the JSON stays compact.
    demand = demand_analysis(store, window_secs=demand_window,
                             series_step_secs=max(1, demand_window // 1))
    (outdir / "demand.json").write_text(json.dumps(demand, indent=2), encoding="utf-8")
    if demand["n_windows"]:
        print(f"[demand] peak {demand['peak_demand_kw']:.1f} kW over a "
              f"{demand_window}s window ending {demand['peak_window_end']}")

    stats: dict = {}
    if not getattr(args, "no_stats", False):
        stats = _write_stats(outdir, store)

    # Active/standby load-state split + energy correction. Cheap (a couple of
    # streaming passes), so emit it alongside the other always-on artifacts
    # (narrative/pq/demand). --load-states is accepted as an explicit opt-in but
    # the report is produced unconditionally here.
    load_states = _write_load_states(outdir, store, threshold_a)

    time_shift = store.time_shift
    markers = _parse_markers(args, time_shift)
    if markers:
        _write_markers(outdir, markers, events)

    tod_rows = []
    if getattr(args, "tod_profile", None):
        tod_rows = _write_tod(outdir, store, args)

    shift_rows = None
    if getattr(args, "split_by", None):
        if str(args.split_by).strip().lower() == "shifts":
            shift_rows = _run_shifts(args, outdir, store, events, config,
                                     nominal_ln_v)
        else:
            _run_split_by(args, outdir, store, events, config, nominal_ln_v)

    # Auto-narrative / executive summary (Feature E) — needs stats + ct + the
    # load-state split (for the active-state PF + the corrected energy).
    narrative = _write_narrative(outdir, store, events, findings, stats, ct,
                                 config, load_states)

    return stats, tod_rows, narrative, demand, shift_rows, load_states


def _write_narrative(outdir: Path, store: ColumnStore, events, findings,
                     stats: dict, ct: dict, config: dict,
                     load_states: dict | None = None) -> str:
    """Build the executive summary, write narrative.md, return the prose."""
    from .narrative import build_narrative, narrative_markdown
    duration = None
    if store.n:
        duration = (store.last_end - store.first_start).total_seconds()
    narrative = build_narrative(
        events, findings, stats or None, ct, config=config,
        total_records=store.n, duration_secs=duration,
        load_states=load_states,
    )
    (outdir / "narrative.md").write_text(
        narrative_markdown(narrative, config), encoding="utf-8")
    print("[narrative] wrote narrative.md")
    return narrative


def _write_summary_txt(outdir: Path, events: Sequence[Event],
                       snaps: Sequence[Snapshot],
                       findings: Sequence[Finding],
                       config: dict,
                       narrative: str | None = None,
                       tz=None, tz_name: str | None = None,
                       store: "ColumnStore | None" = None,
                       shift_rows: "list[dict] | None" = None,
                       load_states: "dict | None" = None) -> None:
    lines: list[str] = ["Fluke 3540 FC Session Summary", "=" * 32, ""]
    if narrative:
        lines.append("Executive Summary")
        lines.append("-" * 17)
        lines.append(narrative)
        lines.append("")
    # Time range (Feature H): local + UTC when --tz set, else UTC only.
    if store is not None and store.n:
        from .tzutil import format_local_utc, tz_label
        lines.append(f"Time range ({tz_label(tz, tz_name)}):")
        lines.append(f"  start  {format_local_utc(store.first_start, tz)}")
        lines.append(f"  end    {format_local_utc(store.last_end, tz)}")
        lines.append("")
    if config:
        if config.get("asset_name"):
            lines.append(f"Asset:       {config['asset_name']}")
        if config.get("team_name"):
            lines.append(f"Team:        {config['team_name']}")
        if config.get("type"):
            lines.append(f"Instrument:  {config['type']}  fw={config.get('firmware_version', '?')}")
        lines.append("")
    if findings:
        lines.append("Insights")
        lines.append("-" * 8)
        for f in findings:
            lines.append(f"  [{f.severity:5s}] {f.kind:25s}  {f.headline}")
        lines.append("")
    if load_states and load_states.get("states"):
        thr = load_states.get("standby_threshold_a", 50.0)
        lines.append(f"Load states (active vs standby, cut at {thr:.0f} A/phase)")
        lines.append("-" * 16)
        lines.append(f"  {'state':<8} {'duty%':>6}  {'records':>8}  "
                     f"{'I_avg_A':>8}  {'P_avg_kW':>9}  {'S_avg_kVA':>10}  "
                     f"{'PF':>6}  {'kWh':>9}")
        for r in load_states["states"]:
            lines.append(
                f"  {r['state']:<8} {r['duty_pct']:>6.1f}  {r['records']:>8d}  "
                f"{r['I_avg_A']:>8.1f}  {r['P_avg_kW']:>9.2f}  "
                f"{r['S_avg_kVA']:>10.2f}  {r['PF_avg']:>6.3f}  {r['kWh']:>9.2f}")
        en = load_states.get("energy", {})
        if en:
            lines.append("")
            lines.append(f"  Energy as-measured (signed):   "
                         f"{en['energy_as_measured_kWh']:>10.1f} kWh")
            lines.append(f"  Energy active-only:            "
                         f"{en['energy_active_kWh']:>10.1f} kWh")
            lines.append(f"  Energy net (standby clip >=0): "
                         f"{en['energy_net_clip_standby_kWh']:>10.1f} kWh")
            lines.append("  Note: standby real-power sign is unreliable at low "
                         "current; active/clip are the defensible consumption.")
        lines.append("")
    if shift_rows:
        lines.append("Shift comparison")
        lines.append("-" * 16)
        lines.append(f"  {'shift':<10} {'window':>13}  {'records':>8}  "
                     f"{'kWh':>9}  {'Pavg_kW':>8}  {'peak_kW':>8}  {'PF':>5}  "
                     f"{'actDuty%':>8}  {'actkWh':>9}  {'actPF':>6}")
        for r in shift_rows:
            lines.append(
                f"  {r['shift']:<10} {r['window']:>13}  {r['records']:>8d}  "
                f"{r['kWh']:>9.2f}  {r['P_total_avg_W']/1000:>8.2f}  "
                f"{r['peak_demand_kW']:>8.2f}  {r['PF_avg']:>5.3f}  "
                f"{r.get('active_duty_pct', 0):>8.1f}  "
                f"{r.get('active_kWh', 0):>9.2f}  {r.get('active_PF_avg', 0):>6.3f}")
        lines.append("")
    lines.append(f"Events detected: {len(events)}")
    for ev in events:
        phases = "/".join(ev.affected_phases) or "—"
        lines.append(f"  #{ev.id:>3}  {ev.kind:18s}  {ev.t_start.isoformat()}  "
                     f"phases={phases}  severity={ev.severity:.3f}")
    lines.append("")
    lines.append(f"Quiet snapshots: {len(snaps)}")
    for s in snaps:
        lines.append(f"  #{s.id:>3}  {s.t_start.isoformat()}  "
                     f"mean P={s.p_total_mean_w / 1000:+.2f} kW  σ={s.p_total_stdev_w:.1f} W")
    (outdir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _filter_events(events: Sequence[Event], ids_arg: str | None) -> list[Event]:
    if not ids_arg:
        return list(events)
    wanted = {int(x.strip()) for x in ids_arg.split(",") if x.strip()}
    return [e for e in events if e.id in wanted]


def _parse_quantities(arg: str | None, default: Sequence[str],
                      valid: Iterable[str]) -> list[str]:
    if not arg:
        return list(default)
    picked = [q.strip() for q in arg.split(",") if q.strip()]
    bad = [q for q in picked if q not in valid]
    if bad:
        raise SystemExit(
            f"Unknown chart quantities: {bad}. Valid: {sorted(valid)}"
        )
    return picked


def _render_phase(args: argparse.Namespace, outdir: Path, full_csv: Path,
                  min_csv: Path, events: Sequence[Event],
                  snaps: Sequence[Snapshot], config: dict,
                  stats: dict | None = None, tod_rows=None,
                  narrative: str | None = None, demand: dict | None = None) -> None:
    full_qtys = _parse_quantities(args.plot, DEFAULT_PLOTS, FULL_QUANTITIES.keys())
    # Subset of zoom quantities that overlap with the user's --plot selection.
    zoom_qtys = [q for q in DEFAULT_ZOOM_PLOTS if q in full_qtys] or DEFAULT_ZOOM_PLOTS
    selected_events = _filter_events(events, args.events)

    charts_dir = outdir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    print(f"[render] full-session charts → {charts_dir}/  ({', '.join(full_qtys)})")
    fs_result = render_full_session(
        full_csv, charts_dir,
        quantities=full_qtys,
        include_overview=not args.no_overview,
        image_format=args.format,
        overview_title=(
            f"Fluke 3540 FC — {config.get('asset_name', 'Session')} Overview"
        ),
    )
    print(f"         {len(fs_result.chart_paths)} chart(s) written")

    if selected_events:
        print(f"[render] event zooms ({len(selected_events)} event(s))")
        for ev in selected_events:
            res = render_event_zoom(
                ev, full_csv, charts_dir,
                pre_secs=args.pre, post_secs=args.post,
                quantities=zoom_qtys, image_format=args.format,
            )
            print(f"         event #{ev.id} {ev.kind}: "
                  f"{len(res.chart_paths)} chart(s)")
    else:
        print("[render] no events to zoom")

    if snaps:
        print(f"[render] snapshot zooms ({len(snaps)} snapshot(s))")
        for s in snaps:
            res = render_snapshot_zoom(
                s, full_csv, charts_dir,
                quantities=zoom_qtys, image_format=args.format,
            )
            print(f"         snapshot #{s.id}: {len(res.chart_paths)} chart(s)")

    if not args.no_xlsx:
        xlsx_path = outdir / "report.xlsx"
        print(f"[render] xlsx workbook → {xlsx_path}")
        write_xlsx(min_csv, xlsx_path, config=config, csv_per_second_path=full_csv,
                   stats=stats, tod_rows=tod_rows, narrative=narrative,
                   demand=demand)

    html_path = outdir / "report.html"
    if not args.no_html:
        print(f"[render] html report → {html_path}")
        # Reload insights from disk so plot-only flows still get them.
        insights_path = outdir / "insights.json"
        loaded_findings: list[Finding] = []
        if insights_path.exists():
            import json as _json
            for d in _json.loads(insights_path.read_text(encoding="utf-8")):
                loaded_findings.append(Finding(
                    id=d["id"], kind=d["kind"], severity=d["severity"],
                    headline=d["headline"], detail=d["detail"],
                    related_event_ids=tuple(d.get("related_event_ids", [])),
                    recommended_actions=tuple(d.get("recommended_actions", [])),
                ))
        # Reload the load-state split from disk (also covers --plot-only).
        load_states_payload = None
        ls_path = outdir / "load_states.json"
        if ls_path.exists():
            import json as _json
            load_states_payload = _json.loads(ls_path.read_text(encoding="utf-8"))
        write_html_report(
            html_path, charts_dir=charts_dir,
            config=config,
            summary_stats=_build_summary_stats(events, snaps),
            events=events, snapshots=snaps,
            findings=loaded_findings,
            narrative=narrative,
            load_states=load_states_payload,
        )

    if args.pdf:
        pdf_path = outdir / "report.pdf"
        if not html_path.is_file():
            print(f"ERROR: --pdf requires the HTML report (don't combine with --no-html)",
                  file=sys.stderr)
            return
        print(f"[render] pdf report → {pdf_path}")
        try:
            write_pdf_report(html_path, pdf_path)
        except WeasyPrintNotInstalled as e:
            print(f"ERROR: {e}", file=sys.stderr)


def _load_existing(outdir: Path) -> tuple[Path, Path, list[Event], list[Snapshot], dict]:
    full_csv = outdir / "session.csv"
    min_csv = outdir / "session_1min.csv"
    events_path = outdir / "events.json"
    snaps_path = outdir / "snapshots.json"
    for p in (full_csv, min_csv, events_path):
        if not p.exists():
            raise SystemExit(f"--plot-only: required file missing: {p}")
    events = [_event_from_json(d) for d in json.loads(events_path.read_text())]
    snaps = []
    if snaps_path.exists():
        snaps = [
            Snapshot(
                id=d["id"], t_start=_parse_time(d["t_start"]),
                t_end=_parse_time(d["t_end"]),
                t_center=_parse_time(d["t_center"]),
                p_total_mean_w=d["p_total_mean_w"],
                p_total_stdev_w=d["p_total_stdev_w"],
            )
            for d in json.loads(snaps_path.read_text())
        ]
    # Synthesize a minimal config dict from the first row of session.csv.
    config: dict = {}
    return full_csv, min_csv, events, snaps, config


def _interactive(events: Sequence[Event], snaps: Sequence[Snapshot],
                 default_qtys: Sequence[str]) -> tuple[list[Event], list[str]]:
    """Lightweight plain-input picker. Returns (picked_events, picked_quantities)."""
    from .interactive import pick_events, pick_quantities
    picked_events = pick_events(events)
    picked_qtys = pick_quantities(default_qtys, list(FULL_QUANTITIES.keys()))
    return picked_events, picked_qtys


def _build_summary_stats(events: Sequence[Event], snaps: Sequence[Snapshot]) -> dict:
    by_kind: dict[str, int] = {}
    for ev in events:
        by_kind[ev.kind] = by_kind.get(ev.kind, 0) + 1
    return {
        "event_count": len(events),
        "events_by_kind": by_kind,
        "snapshot_count": len(snaps),
    }


def _emit_json(events: Sequence[Event], snaps: Sequence[Snapshot],
               findings: Sequence[Finding], config: dict) -> None:
    """Print a single JSON object to stdout. No trailing newline beyond json.dump's."""
    payload = {
        "config": config,
        "summary_stats": _build_summary_stats(events, snaps),
        "events": [_event_to_json(e) for e in events],
        "snapshots": [_snapshot_to_json(s) for s in snaps],
        "insights": [finding_to_jsonable(f) for f in findings],
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    # Subcommand dispatch — `fluke-analyze compare ...` routes to cli_compare.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "compare":
        from .cli_compare import compare_main
        return compare_main(raw_argv[1:])
    if raw_argv and raw_argv[0] == "stitch":
        from .cli_stitch import stitch_main
        return stitch_main(raw_argv[1:])

    # Make console output UTF-8 safe on Windows (default cp1252 chokes on →, σ, etc.)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    args = build_argparser().parse_args(argv)

    # In --json mode, suppress every other stdout print so the output stays
    # a single valid JSON blob. We do this by replacing the module-level
    # print() with a no-op for the duration of this call.
    if getattr(args, "json_mode", False):
        global print
        _original_print = print
        def print(*a, **kw):  # noqa: A001 — intentional shadow
            kw.setdefault("file", sys.stderr)
            _original_print(*a, **kw)

    # Resolve --tz once (Feature H). Invalid zones fail fast.
    from .tzutil import resolve_tz
    try:
        args._tz = resolve_tz(getattr(args, "tz", None))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not args.session_dir.exists():
        print(f"ERROR: {args.session_dir} does not exist", file=sys.stderr)
        return 1
    if not (args.session_dir.is_dir()
            or (args.session_dir.is_file()
                and args.session_dir.suffix.lower() in (".fel", ".csv"))):
        print(f"ERROR: {args.session_dir} is not a directory, a .fel, or a .csv",
              file=sys.stderr)
        return 1

    outdir = _resolve_outdir(args)
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        if args.plot_only:
            full_csv, min_csv, events, snaps, config = _load_existing(outdir)
            _render_phase(args, outdir, full_csv, min_csv, events, snaps, config)
            return 0

        # Phase 1: parse + detect (open_session transparently unpacks .fel;
        # CSV inputs skip the binary parse entirely).
        if _is_csv_input(args.session_dir):
            full_csv, min_csv, config = _parse_csv_session(args, outdir)
            events, snaps, findings, store = _detect_and_save(
                args, outdir, trend_path=None, config=config, csv_path=full_csv,
            )
            _write_summary_txt(outdir, events, snaps, findings, config)
        else:
            with open_session(args.session_dir) as session_dir:
                full_csv, min_csv, config, store = _parse_session(
                    args, outdir, session_dir)
                events, snaps, findings, store = _detect_and_save(
                    args, outdir, trend_path=None, config=config, store=store)
                _write_summary_txt(outdir, events, snaps, findings, config)

        # Post-detection analysis features (markers, stats, tod, split) run on
        # the in-memory store + on-disk CSVs.
        (stats, tod_rows, narrative, demand, shift_rows,
         load_states) = _run_extra_analyses(
            args, outdir, store, events, findings, config, full_csv, min_csv)
        # Re-write summary.txt with the executive narrative + tz-aware time range.
        _write_summary_txt(outdir, events, snaps, findings, config,
                           narrative=narrative, tz=getattr(args, "_tz", None),
                           tz_name=getattr(args, "tz", None), store=store,
                           shift_rows=shift_rows, load_states=load_states)

        if getattr(args, "json_mode", False):
            _emit_json(events, snaps, findings, config)
            return 0

        if args.parse_only:
            print(f"[done] parse-only: {outdir}")
            return 0

        if args.interactive:
            full_qtys_default = _parse_quantities(
                args.plot, DEFAULT_PLOTS, FULL_QUANTITIES.keys(),
            )
            picked_events, picked_qtys = _interactive(events, snaps, full_qtys_default)
            events = picked_events
            args.plot = ",".join(picked_qtys)

        _render_phase(args, outdir, full_csv, min_csv, events, snaps, config,
                      stats=stats, tod_rows=tod_rows, narrative=narrative,
                      demand=demand)
        print(f"[done] {outdir}")
        return 0

    except GnuplotNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
