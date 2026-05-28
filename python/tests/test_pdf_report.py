"""Tests for the optional weasyprint-backed PDF generator."""
from __future__ import annotations

from pathlib import Path

import pytest

from fluke_3540.plots.pdf_report import WeasyPrintNotInstalled, write_pdf_report


def test_write_pdf_raises_actionable_error_when_weasyprint_missing(
    tmp_path: Path, monkeypatch
):
    """Simulate a missing weasyprint import — the wrapper must raise our
    custom error with install instructions, not a bare ImportError."""
    import sys as _sys
    import importlib
    monkeypatch.setitem(_sys.modules, "weasyprint", None)  # block import
    html = tmp_path / "report.html"
    html.write_text("<html><body>x</body></html>", encoding="utf-8")
    pdf = tmp_path / "report.pdf"
    with pytest.raises(WeasyPrintNotInstalled, match=r"pip install"):
        write_pdf_report(html, pdf)


def test_write_pdf_raises_when_html_missing(tmp_path: Path):
    html = tmp_path / "missing.html"
    pdf = tmp_path / "out.pdf"
    with pytest.raises(FileNotFoundError, match=r"HTML report not found"):
        write_pdf_report(html, pdf)
