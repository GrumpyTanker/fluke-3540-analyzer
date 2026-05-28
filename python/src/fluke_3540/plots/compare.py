"""Overlay-chart rendering for multi-session comparison.

Given N session CSVs (produced by export_csv), align them by
relative-time-from-session-start (since absolute timestamps differ across
sessions) and plot each quantity on a single axis with per-session colors.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .gnuplot import gnuplot_path_str, run_script


# Per-quantity chart definition. Lighter set than the full-session view —
# overlays get noisy fast.
COMPARE_QUANTITIES: dict[str, tuple[str, str, str]] = {
    # key: (csv_column, title, ylabel)
    "power":     ("P_total_avg_W",   "Active Power",   "P (kW)"),
    "voltage":   ("V_LN_a_avg_V",    "Phase-A L-N Voltage", "V (V)"),
    "current":   ("I_a_avg_A",       "Phase-A Current",     "I (A)"),
    "pf":        ("PF_total_avg",    "True Power Factor",   "PF"),
    "frequency": ("freq_avg_Hz",     "Line Frequency",      "Hz"),
}

# Distinct colors for overlay series. Repeats if > 6 sessions, but practical
# limit is "you can still tell them apart" which is ~5.
_OVERLAY_COLORS = [
    "#cc0000", "#0066cc", "#009933", "#660066", "#cc6600", "#0099aa",
]


@dataclass
class CompareResult:
    summary_csv: Path
    chart_paths: list[Path]
    per_session_stats: list[dict]


def _session_relative_tsv(csv_path: Path, tsv_path: Path,
                          columns: Sequence[str]) -> int:
    """Write a TSV with epoch-from-session-start (relative seconds) + requested cols."""
    rows = 0
    first_ts: dt.datetime | None = None
    with csv_path.open(newline="") as src, tsv_path.open("w", newline="") as dst:
        reader = csv.DictReader(src)
        dst.write("rel_s")
        for c in columns:
            dst.write("\t" + c)
        dst.write("\n")
        for r in reader:
            try:
                t = dt.datetime.fromisoformat(r["timestamp_utc"])
            except (KeyError, ValueError):
                continue
            if first_ts is None:
                first_ts = t
            rel = (t - first_ts).total_seconds()
            parts = [f"{rel:.0f}"]
            for c in columns:
                raw = r.get(c, "")
                if raw == "":
                    parts.append("NaN")
                    continue
                try:
                    parts.append(f"{float(raw):.6g}")
                except ValueError:
                    parts.append("NaN")
            dst.write("\t".join(parts) + "\n")
            rows += 1
    return rows


def _compute_session_stats(csv_path: Path) -> dict:
    """Per-session totals lifted from the per-second CSV."""
    wh_sum = wh_fwd = wh_rev = 0.0
    p_pos = 0.0
    p_neg = 0.0
    i_peak = 0.0
    rows = 0
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            try:
                wh = float(r.get("Wh_total", "") or 0)
                p = float(r.get("P_total_avg_W", "") or 0)
                ia = float(r.get("I_a_avg_A", "") or 0)
                ib = float(r.get("I_b_avg_A", "") or 0)
                ic = float(r.get("I_c_avg_A", "") or 0)
            except ValueError:
                continue
            wh_sum += wh
            if wh > 0: wh_fwd += wh
            if wh < 0: wh_rev += wh
            if p > p_pos: p_pos = p
            if p < p_neg: p_neg = p
            i_peak = max(i_peak, ia, ib, ic)
            rows += 1
    return {
        "rows": rows,
        "net_kwh": wh_sum / 1000,
        "imported_kwh": wh_fwd / 1000,
        "exported_kwh": wh_rev / 1000,
        "peak_import_kw": p_pos / 1000,
        "peak_export_kw": p_neg / 1000,
        "peak_current_a": i_peak,
    }


def _write_summary_csv(
    summary_csv: Path, labels: Sequence[str],
    stats_per_session: Sequence[dict],
) -> None:
    metrics = [
        "rows", "net_kwh", "imported_kwh", "exported_kwh",
        "peak_import_kw", "peak_export_kw", "peak_current_a",
    ]
    with summary_csv.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric"] + list(labels))
        for m in metrics:
            row = [m]
            for s in stats_per_session:
                v = s.get(m)
                if isinstance(v, float):
                    row.append(f"{v:.3f}")
                else:
                    row.append("" if v is None else str(v))
            writer.writerow(row)


def _gnuplot_compare_script(
    tsv_paths: Sequence[Path], labels: Sequence[str],
    charts_dir: Path, quantity: str, image_format: str,
) -> tuple[str, Path]:
    """Return (script_text, output_png_path) for one quantity overlay chart."""
    col, title, ylabel = COMPARE_QUANTITIES[quantity]
    out = charts_dir / f"compare_{quantity}.{image_format}"
    out_gp = gnuplot_path_str(out)
    terminal = (
        "set terminal pngcairo size 1600,700 enhanced font 'Segoe UI,11' "
        "background '#ffffff'"
        if image_format == "png" else
        "set terminal svg size 1600,700 enhanced font 'Segoe UI,11' "
        "background '#ffffff'"
    )
    lines = [
        "set datafile separator '\\t'",
        "set datafile missing 'NaN'",
        "set xtics rotate by -30 font ',9'",
        "set grid xtics ytics linecolor rgb '#cccccc'",
        "set border linecolor rgb '#666666'",
        "set key outside right top box",
        "set tics nomirror",
        terminal,
        f"set output '{out_gp}'",
        f"set title '{title} (overlay)' font ',13'",
        "set xlabel 'Seconds from session start'",
        f"set ylabel '{ylabel}'",
    ]
    plots = []
    scale = "*0.001" if col.endswith("_W") or col.endswith("_VA") or col.endswith("_VAR") else ""
    for i, (tsv, label) in enumerate(zip(tsv_paths, labels)):
        gp = gnuplot_path_str(tsv)
        color = _OVERLAY_COLORS[i % len(_OVERLAY_COLORS)]
        plots.append(
            f"'{gp}' using 1:($2{scale}) with lines "
            f"linewidth 1.4 linecolor rgb '{color}' title '{label}'"
        )
    lines.append("plot " + ", \\\n     ".join(plots))
    return "\n".join(lines) + "\n", out


def render_compare(
    session_csvs: Sequence[Path],
    labels: Sequence[str],
    output_dir: Path,
    quantities: Iterable[str] = ("power", "voltage", "current", "pf"),
    image_format: str = "png",
    gnuplot: Path | None = None,
) -> CompareResult:
    """Build overlay charts + a side-by-side summary CSV."""
    if len(session_csvs) < 2:
        raise ValueError("compare requires at least 2 sessions")
    if len(labels) != len(session_csvs):
        raise ValueError("labels count must match sessions count")
    quantities = list(quantities)
    bad = [q for q in quantities if q not in COMPARE_QUANTITIES]
    if bad:
        raise ValueError(f"Unknown compare quantities: {bad}. "
                         f"Valid: {sorted(COMPARE_QUANTITIES)}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Per-quantity TSVs (one per session per quantity) reused across charts.
    # Cheaper to write a single multi-column TSV per session and reuse, but the
    # column re-mapping inside the gnuplot script gets fiddly. Per-quantity
    # TSVs keep the script generation simple.
    chart_paths: list[Path] = []
    stats_per_session = [_compute_session_stats(p) for p in session_csvs]

    for q in quantities:
        col, _, _ = COMPARE_QUANTITIES[q]
        tsv_paths: list[Path] = []
        for i, csv_path in enumerate(session_csvs):
            tsv = output_dir / f"compare_{q}_session_{i}.tsv"
            _session_relative_tsv(csv_path, tsv, [col])
            tsv_paths.append(tsv)
        script_text, out_path = _gnuplot_compare_script(
            tsv_paths, labels, output_dir, q, image_format,
        )
        script_path = output_dir / f"compare_{q}.gp"
        script_path.write_text(script_text, encoding="utf-8")
        run_script(script_path, gnuplot=gnuplot)
        chart_paths.append(out_path)

    summary_csv = output_dir / "compare_summary.csv"
    _write_summary_csv(summary_csv, labels, stats_per_session)

    return CompareResult(
        summary_csv=summary_csv, chart_paths=chart_paths,
        per_session_stats=list(stats_per_session),
    )
