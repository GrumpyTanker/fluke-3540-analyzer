"""Full-session charts — one PNG per quantity, plus a 2x2 overview multiplot."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .gnuplot import gnuplot_path_str, run_script


# Mapping from CLI --plot quantity → list of (csv_column_name, label, color)
QUANTITY_SPECS: dict[str, tuple[str, str, list[tuple[str, str, str]]]] = {
    # key: (title, ylabel, [(csv_col_name, gnuplot_label, hex_color)])
    "power":    ("Active Power (P)  — negative = exporting", "P (kW)",
                 [("P_total_avg_W", "P_{total}", "#cc0000")]),
    "apparent": ("Apparent (S) and Reactive (Q) Power", "kVA / kVAR",
                 [("S_total_avg_VA", "S_{total} (kVA)", "#0066cc"),
                  ("Q_total_avg_VAR", "Q_{total} (kVAR)", "#009933")]),
    "pf":       ("True PF and Displacement PF", "Power Factor (-1 to +1)",
                 [("PF_total_avg", "PF (true)", "#cc0000"),
                  ("DPF_total_avg", "DPF", "#0066cc")]),
    "voltage":  ("Per-phase L-N RMS Voltage", "V (V)",
                 [("V_LN_a_avg_V", "V_{LN,a}", "#cc0000"),
                  ("V_LN_b_avg_V", "V_{LN,b}", "#0066cc"),
                  ("V_LN_c_avg_V", "V_{LN,c}", "#009933")]),
    "current":  ("Per-phase RMS Current", "I (A)",
                 [("I_a_avg_A", "I_a", "#cc0000"),
                  ("I_b_avg_A", "I_b", "#0066cc"),
                  ("I_c_avg_A", "I_c", "#009933")]),
    "thd":      ("Voltage Total Harmonic Distortion per phase", "V_THD (%)",
                 [("V_THD_pct_a_avg", "V_{THD,a}", "#cc0000"),
                  ("V_THD_pct_b_avg", "V_{THD,b}", "#0066cc"),
                  ("V_THD_pct_c_avg", "V_{THD,c}", "#009933")]),
    "thdi":     ("Current Total Harmonic Distortion per phase", "I_THD (%)",
                 [("I_THD_pct_a_avg", "I_{THD,a}", "#cc0000"),
                  ("I_THD_pct_b_avg", "I_{THD,b}", "#0066cc"),
                  ("I_THD_pct_c_avg", "I_{THD,c}", "#009933")]),
    "frequency": ("Line Frequency", "Frequency (Hz)",
                  [("freq_avg_Hz", "f", "#660066")]),
}

# Columns that need W → kW (etc) scaling when chart-rendered.
SCALES = {
    "P_total_avg_W":   1e-3,
    "S_total_avg_VA":  1e-3,
    "Q_total_avg_VAR": 1e-3,
}


@dataclass
class FullSessionResult:
    tsv_path: Path
    script_path: Path
    chart_paths: list[Path]


def _write_tsv(csv_path: Path, tsv_path: Path, columns: Sequence[str]) -> int:
    """Convert source CSV to a slim TSV with epoch timestamps."""
    rows = 0
    with csv_path.open(newline="") as src, tsv_path.open("w", newline="") as dst:
        reader = csv.DictReader(src)
        dst.write("epoch_s")
        for c in columns:
            dst.write("\t" + c)
        dst.write("\n")
        for r in reader:
            try:
                t = dt.datetime.fromisoformat(r["timestamp_utc"])
                epoch = t.timestamp()
            except (KeyError, ValueError):
                continue
            parts = [f"{epoch:.0f}"]
            for c in columns:
                raw = r.get(c, "")
                if raw == "":
                    parts.append("NaN")
                    continue
                try:
                    v = float(raw) * SCALES.get(c, 1.0)
                    parts.append(f"{v:.6g}")
                except ValueError:
                    parts.append("NaN")
            dst.write("\t".join(parts) + "\n")
            rows += 1
    return rows


def _gnuplot_header(image_format: str) -> list[str]:
    terminal = (
        "set terminal pngcairo size 1600,600 enhanced font 'Segoe UI,11' "
        "background '#ffffff'"
        if image_format == "png" else
        "set terminal svg size 1600,600 enhanced font 'Segoe UI,11' "
        "background '#ffffff'"
    )
    return [
        "set datafile separator '\\t'",
        "set datafile missing 'NaN'",
        "set xdata time",
        "set timefmt '%s'",
        "set format x '%m/%d\\n%H:%M'",
        "set xtics rotate by -30 font ',9'",
        "set grid xtics ytics linecolor rgb '#cccccc'",
        "set border linecolor rgb '#666666'",
        "set key outside right top box",
        "set tics nomirror",
        terminal,
        "",
    ]


def write_gnuplot_script(
    tsv_path: Path, charts_dir: Path, script_path: Path,
    quantities: Sequence[str], include_overview: bool,
    image_format: str = "png",
    overview_title: str = "Session Overview",
) -> tuple[list[Path], list[str]]:
    """Emit a gnuplot script for the requested quantities. Returns expected output paths."""
    tsv_gp = gnuplot_path_str(tsv_path)
    charts_gp = gnuplot_path_str(charts_dir)
    out_paths: list[Path] = []
    lines = _gnuplot_header(image_format)
    ext = image_format

    # Build a column-name → tsv-column-index map (index 1 is epoch_s)
    used_cols: list[str] = []
    for q in quantities:
        for col_name, _, _ in QUANTITY_SPECS[q][2]:
            if col_name not in used_cols:
                used_cols.append(col_name)
    col_index = {name: i + 2 for i, name in enumerate(used_cols)}  # +2: epoch=1

    for q in quantities:
        title, ylabel, specs = QUANTITY_SPECS[q]
        png = f"full_{q}.{ext}"
        out_paths.append(charts_dir / png)
        lines.append(f"# --- {png}")
        lines.append(f"set output '{charts_gp}/{png}'")
        lines.append(f"set title '{title}' font ',13'")
        lines.append("set xlabel 'Time (UTC)'")
        lines.append(f"set ylabel '{ylabel}'")
        plots = [
            f"'{tsv_gp}' using 1:{col_index[col]} with lines "
            f"linewidth 1.4 linecolor rgb '{color}' title '{label}'"
            for col, label, color in specs
        ]
        lines.append("plot " + ", \\\n     ".join(plots))
        lines.append("")

    if include_overview:
        png = f"overview.{ext}"
        out_paths.append(charts_dir / png)
        lines.append(f"# --- {png}")
        if image_format == "png":
            lines.append(
                "set terminal pngcairo size 1800,1200 enhanced "
                "font 'Segoe UI,11' background '#ffffff'"
            )
        else:
            lines.append(
                "set terminal svg size 1800,1200 enhanced "
                "font 'Segoe UI,11' background '#ffffff'"
            )
        lines.append(f"set output '{charts_gp}/{png}'")
        lines.append(
            f"set multiplot layout 2,2 title '{overview_title}' font ',16'"
        )
        lines.append("unset key")
        lines.append("set key inside top right box")
        overview_panels = [
            ("Active Power", "P (kW)",
             [("P_total_avg_W", "P_{total}", "#cc0000")]),
            ("Power Factor", "PF / DPF",
             [("PF_total_avg", "PF", "#cc0000"),
              ("DPF_total_avg", "DPF", "#0066cc")]),
            ("Per-phase Voltage", "V (V)",
             [("V_LN_a_avg_V", "a", "#cc0000"),
              ("V_LN_b_avg_V", "b", "#0066cc"),
              ("V_LN_c_avg_V", "c", "#009933")]),
            ("Per-phase Current", "I (A)",
             [("I_a_avg_A", "a", "#cc0000"),
              ("I_b_avg_A", "b", "#0066cc"),
              ("I_c_avg_A", "c", "#009933")]),
        ]
        for title, ylabel, specs in overview_panels:
            lines.append(f"set title '{title}'")
            lines.append(f"set ylabel '{ylabel}'")
            # If any panel column isn't in used_cols (because user disabled
            # that quantity), append it now so the index map stays valid.
            for col, _, _ in specs:
                if col not in col_index:
                    used_cols.append(col)
                    col_index[col] = len(used_cols) + 1
            plots = [
                f"'{tsv_gp}' using 1:{col_index[col]} with lines "
                f"linewidth 1.2 linecolor rgb '{color}' title '{label}'"
                for col, label, color in specs
            ]
            lines.append("plot " + ", \\\n     ".join(plots))
        lines.append("unset multiplot")
        lines.append("")

    script_path.write_text("\n".join(lines), encoding="utf-8")
    return out_paths, used_cols


def render_full_session(
    csv_path: Path, output_dir: Path,
    quantities: Iterable[str] = ("power", "voltage", "current", "pf", "frequency",
                                 "thd", "thdi", "apparent"),
    include_overview: bool = True,
    image_format: str = "png",
    overview_title: str = "Fluke 3540 FC Session Overview",
    gnuplot: Path | None = None,
) -> FullSessionResult:
    """Generate full-session charts. Returns the paths of artifacts produced."""
    quantities = list(quantities)
    bad = [q for q in quantities if q not in QUANTITY_SPECS]
    if bad:
        raise ValueError(f"Unknown quantity/quantities: {bad}. "
                         f"Valid: {sorted(QUANTITY_SPECS)}")
    output_dir.mkdir(parents=True, exist_ok=True)

    tsv_path = output_dir / "full_session.tsv"
    script_path = output_dir / "full_session.gp"
    chart_paths, used_cols = write_gnuplot_script(
        tsv_path, output_dir, script_path,
        quantities=quantities, include_overview=include_overview,
        image_format=image_format, overview_title=overview_title,
    )
    _write_tsv(csv_path, tsv_path, used_cols)
    run_script(script_path, gnuplot=gnuplot)
    return FullSessionResult(
        tsv_path=tsv_path, script_path=script_path, chart_paths=chart_paths,
    )
