"""Low-level gnuplot driver — discover the binary and run a script."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class GnuplotNotFound(RuntimeError):
    pass


_WINDOWS_CANDIDATES = [
    r"C:\Program Files\gnuplot\bin\gnuplot.exe",
    r"C:\Program Files (x86)\gnuplot\bin\gnuplot.exe",
]


def find_gnuplot() -> Path:
    """Locate gnuplot. Honors $GNUPLOT, then PATH, then common Windows install paths."""
    override = os.environ.get("GNUPLOT")
    if override and Path(override).is_file():
        return Path(override)
    on_path = shutil.which("gnuplot")
    if on_path:
        return Path(on_path)
    for candidate in _WINDOWS_CANDIDATES:
        if Path(candidate).is_file():
            return Path(candidate)
    raise GnuplotNotFound(
        "gnuplot not found. Install from http://www.gnuplot.info/ or set the "
        "GNUPLOT environment variable to the executable path."
    )


def run_script(script_path: Path, *, gnuplot: Path | None = None) -> None:
    """Execute a gnuplot script. Raises subprocess.CalledProcessError on failure.
    Warnings on stderr are printed but do not fail.
    """
    gnuplot = gnuplot or find_gnuplot()
    result = subprocess.run(
        [str(gnuplot), str(script_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr,
        )
    if result.stderr.strip():
        # gnuplot warnings are non-fatal; surface them to stderr.
        import sys
        print(f"(gnuplot notes) {result.stderr.strip()}", file=sys.stderr)


def gnuplot_path_str(p: Path) -> str:
    """Convert a path to a gnuplot-friendly forward-slash string."""
    return str(p).replace("\\", "/")
