// One-click PDF report download — programmatic page composition via pdf-lib.
//
// Expects window.PDFLib from vendor/pdf-lib.min.js.
//
// Page plan:
//   1. Cover page (title, asset, time range, summary stats)
//   2+. One Insights page (or more if many findings)
//   N. Events table page (truncated if huge)
//   N+1..end. One page per rendered uPlot chart (canvas → PNG embed)

import { downloadBlob } from './xlsx_export.js';

function pdfLib() {
  if (typeof window === 'undefined' || typeof window.PDFLib === 'undefined') {
    throw new Error('PDFLib global not found — ensure vendor/pdf-lib.min.js is loaded');
  }
  return window.PDFLib;
}

const MARGIN = 50;
const SEVERITY_COLORS = {
  alert: { r: 0.80, g: 0, b: 0 },
  warn:  { r: 0.80, g: 0.40, b: 0 },
  info:  { r: 0, g: 0.40, b: 0.80 },
};

function collectChartCanvases() {
  const out = [];
  const groups = [
    ['Full-session', 'full-charts'],
    ['Event zooms', 'event-charts'],
    ['Snapshot zooms', 'snapshot-charts'],
  ];
  for (const [groupLabel, containerId] of groups) {
    const container = document.getElementById(containerId);
    if (!container) continue;
    const wrappers = container.querySelectorAll('article.chart-wrapper');
    for (const w of wrappers) {
      const canvas = w.querySelector('canvas');
      const title = w.querySelector('h3')?.textContent ?? 'chart';
      if (!canvas) continue;
      const dataUrl = canvas.toDataURL('image/png');
      out.push({ groupLabel, title, dataUrl });
    }
  }
  return out;
}

function dataUrlToBytes(dataUrl) {
  const base64 = dataUrl.split(',')[1];
  const bin = atob(base64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

function summarizeRecords(records, spec) {
  const fi = new Map(spec.fields.map((f) => [f.name, f.index]));
  const pIdx = fi.get('P_total_avg_W');
  const whIdx = fi.get('Wh_total');
  let whFwd = 0, whRev = 0, pPos = 0, pNeg = 0;
  for (const r of records) {
    const wh = r.floats[whIdx];
    const p = r.floats[pIdx];
    if (Number.isFinite(wh)) {
      if (wh > 0) whFwd += wh; else if (wh < 0) whRev += wh;
    }
    if (Number.isFinite(p)) {
      if (p > pPos) pPos = p;
      if (p < pNeg) pNeg = p;
    }
  }
  return { whFwd, whRev, pPos, pNeg };
}

class PdfBuilder {
  constructor(doc, fontReg, fontBold) {
    this.doc = doc;
    this.fontReg = fontReg;
    this.fontBold = fontBold;
    this.PageSizes = pdfLib().PageSizes;
    this.rgb = pdfLib().rgb;
  }

  newPage() {
    this.page = this.doc.addPage(this.PageSizes.A4);
    this.cursorY = this.page.getHeight() - MARGIN;
    this.width = this.page.getWidth();
    return this.page;
  }

  text(str, opts = {}) {
    const size = opts.size ?? 11;
    const font = opts.bold ? this.fontBold : this.fontReg;
    const color = opts.color ?? { r: 0.2, g: 0.2, b: 0.2 };
    const x = opts.x ?? MARGIN;
    const maxWidth = (this.width ?? 595) - MARGIN * 2;
    const lines = wrapText(str, font, size, maxWidth);
    for (const ln of lines) {
      if (this.cursorY < MARGIN + size + 4) {
        this.newPage();
      }
      this.page.drawText(ln, {
        x, y: this.cursorY - size,
        size, font, color: this.rgb(color.r, color.g, color.b),
      });
      this.cursorY -= size + 4;
    }
  }

  spacer(h) {
    this.cursorY -= h;
  }

  rule() {
    this.page.drawLine({
      start: { x: MARGIN, y: this.cursorY - 4 },
      end:   { x: this.width - MARGIN, y: this.cursorY - 4 },
      thickness: 0.5, color: this.rgb(0.8, 0.8, 0.8),
    });
    this.cursorY -= 8;
  }

  severityBar(severity, height) {
    const c = SEVERITY_COLORS[severity] ?? { r: 0.5, g: 0.5, b: 0.5 };
    this.page.drawRectangle({
      x: MARGIN - 6, y: this.cursorY - height,
      width: 3, height,
      color: this.rgb(c.r, c.g, c.b),
    });
  }
}

function wrapText(text, font, size, maxWidth) {
  const paragraphs = String(text).split('\n');
  const out = [];
  for (const para of paragraphs) {
    if (para.length === 0) { out.push(''); continue; }
    const words = para.split(/\s+/);
    let line = '';
    for (const w of words) {
      const candidate = line ? line + ' ' + w : w;
      if (font.widthOfTextAtSize(candidate, size) > maxWidth && line) {
        out.push(line);
        line = w;
      } else {
        line = candidate;
      }
    }
    if (line) out.push(line);
  }
  return out;
}

export async function buildPdfReport({
  title, config, records, spec, events, snapshots, findings,
}) {
  const { PDFDocument, StandardFonts } = pdfLib();
  const doc = await PDFDocument.create();
  const fontReg = await doc.embedFont(StandardFonts.Helvetica);
  const fontBold = await doc.embedFont(StandardFonts.HelveticaBold);
  const b = new PdfBuilder(doc, fontReg, fontBold);

  // ---- Cover page --------------------------------------------------------
  b.newPage();
  b.text(title, { size: 22, bold: true, color: { r: 0, g: 0, b: 0 } });
  b.spacer(6);
  b.rule();
  if (config?.asset_name)  b.text(`Asset: ${config.asset_name}`, { bold: true });
  if (config?.team_name)   b.text(`Team: ${config.team_name}`);
  if (config?.type) {
    const fw = config.firmware_version ? ` fw ${config.firmware_version}` : '';
    b.text(`Instrument: ${config.type}${fw}`);
  }
  b.spacer(8);
  if (records.length > 0) {
    const t0 = new Date(records[0].startMs).toISOString();
    const t1 = new Date(records[records.length - 1].endMs).toISOString();
    b.text(`Records: ${records.length.toLocaleString()}`);
    b.text(`Time range (UTC): ${t0}  →  ${t1}`);
  }
  b.spacer(8);
  if (spec) {
    const en = summarizeRecords(records, spec);
    b.text(`Imported: ${(en.whFwd / 1000).toFixed(2)} kWh   ` +
           `Exported: ${(en.whRev / 1000).toFixed(2)} kWh`);
    b.text(`Peak import: ${(en.pPos / 1000).toFixed(2)} kW   ` +
           `Peak export: ${(en.pNeg / 1000).toFixed(2)} kW`);
  }
  b.spacer(8);
  b.text(`Events: ${events.length}   Snapshots: ${snapshots.length}   Findings: ${findings.length}`);

  // ---- Insights ----------------------------------------------------------
  if (findings.length > 0) {
    b.spacer(20);
    b.text('Insights', { size: 16, bold: true, color: { r: 0, g: 0, b: 0 } });
    b.rule();
    for (const f of findings) {
      const startY = b.cursorY;
      b.text(f.headline, { bold: true, size: 12 });
      b.text(`${f.kind} · ${f.severity}`, { size: 9, color: { r: 0.55, g: 0.55, b: 0.55 } });
      b.text(f.detail, { size: 10 });
      if (f.recommendedActions?.length) {
        b.text('Recommended:', { size: 10, bold: true });
        for (const a of f.recommendedActions) b.text('  • ' + a, { size: 10 });
      }
      // Severity bar to the left, spanning this finding's vertical extent
      const usedH = startY - b.cursorY;
      b.cursorY = startY;       // move back so the bar aligns
      b.severityBar(f.severity, usedH);
      b.cursorY = startY - usedH;
      b.spacer(8);
    }
  }

  // ---- Events table ------------------------------------------------------
  if (events.length > 0) {
    b.newPage();
    b.text('Events', { size: 16, bold: true, color: { r: 0, g: 0, b: 0 } });
    b.rule();
    b.text(`ID   Kind                Start (UTC)               Dur     Phases   Severity`,
           { size: 10, bold: true });
    for (const ev of events.slice(0, 60)) {  // first 60 keeps the table to ~2 pages
      const dur = Math.max(1, Math.round((ev.tEndMs - ev.tStartMs) / 1000));
      const phases = (ev.affectedPhases ?? []).join('/') || '—';
      const line =
        String(ev.id).padEnd(5) +
        String(ev.kind).padEnd(20) +
        new Date(ev.tStartMs).toISOString().slice(0, 19).padEnd(24) +
        `${dur}s`.padEnd(8) +
        phases.padEnd(9) +
        ev.severity.toFixed(3);
      b.text(line, { size: 10 });
    }
    if (events.length > 60) {
      b.spacer(6);
      b.text(`(${events.length - 60} additional events truncated)`,
             { size: 9, color: { r: 0.55, g: 0.55, b: 0.55 } });
    }
  }

  // ---- One page per rendered chart ---------------------------------------
  const charts = collectChartCanvases();
  for (const c of charts) {
    b.newPage();
    b.text(c.groupLabel + ' — ' + c.title, { size: 14, bold: true });
    b.spacer(4);
    b.rule();
    const pngBytes = dataUrlToBytes(c.dataUrl);
    const img = await doc.embedPng(pngBytes);
    const maxW = b.width - MARGIN * 2;
    const scale = maxW / img.width;
    const w = img.width * scale;
    const h = img.height * scale;
    const y = Math.max(MARGIN, b.cursorY - h);
    b.page.drawImage(img, { x: MARGIN, y, width: w, height: h });
  }

  const bytes = await doc.save();
  return new Blob([bytes], { type: 'application/pdf' });
}

export async function downloadPdfReport(opts) {
  const blob = await buildPdfReport(opts);
  const name = (opts.config?.asset_name ?? 'fluke_session')
    .replace(/[^a-zA-Z0-9._-]+/g, '_');
  downloadBlob(blob, `${name}_report.pdf`);
}
