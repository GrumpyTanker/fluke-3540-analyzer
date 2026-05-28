// Mini-map P_total strip with a brush-selectable time range.
//
// Uses uPlot (global window.uPlot). Calls back into `onRangeChange` with
// {startMs, endMs} on selection release, and `null` when the user clicks
// the Clear button.

function uplotOrThrow() {
  if (typeof window === 'undefined' || typeof window.uPlot === 'undefined') {
    throw new Error('uPlot global not found');
  }
  return window.uPlot;
}

function fieldIndex(spec, name) {
  const f = spec.fields.find((x) => x.name === name);
  if (!f) throw new Error('spec missing ' + name);
  return f.index;
}

/**
 * Render a small overview chart with brush-select enabled.
 *
 * @param {HTMLElement} container - empty div where the chart and label go
 * @param {Array} records - parsed Record list
 * @param {object} spec
 * @param {(range: {startMs: number, endMs: number} | null) => void} onRangeChange
 * @returns {{ destroy: () => void, setRange: (r) => void }}
 */
export function renderRangeSelector(container, records, spec, onRangeChange) {
  const uPlot = uplotOrThrow();
  container.replaceChildren();
  if (records.length === 0) return { destroy() {}, setRange() {} };
  const pIdx = fieldIndex(spec, 'P_total_avg_W');
  const xs = records.map((r) => r.startMs / 1000);
  const ys = records.map((r) => r.floats[pIdx] / 1000);

  const label = document.createElement('div');
  label.className = 'range-label';
  label.textContent = 'Drag on the strip below to pick a time range to scope charts and exports to.';
  container.appendChild(label);

  const chartDiv = document.createElement('div');
  chartDiv.className = 'range-strip';
  container.appendChild(chartDiv);

  const clearBtn = document.createElement('button');
  clearBtn.className = 'secondary outline';
  clearBtn.type = 'button';
  clearBtn.textContent = 'Clear range';
  clearBtn.hidden = true;
  label.appendChild(document.createTextNode(' '));
  label.appendChild(clearBtn);

  const opts = {
    width: chartDiv.clientWidth || 900,
    height: 90,
    series: [
      { label: 'time' },
      { label: 'P_total (kW)', stroke: '#0066cc', width: 1 },
    ],
    scales: { x: { time: true } },
    axes: [
      { stroke: '#666' },
      { stroke: '#666' },
    ],
    cursor: {
      // Brush-select without auto-zooming the strip itself.
      drag: { x: true, y: false, setScale: false },
    },
    legend: { show: false },
    hooks: {
      setSelect: [
        (u) => {
          const sel = u.select;
          if (!sel || sel.width <= 1) return;
          const leftPx = sel.left;
          const rightPx = sel.left + sel.width;
          const startS = u.posToVal(leftPx, 'x');
          const endS = u.posToVal(rightPx, 'x');
          if (Number.isFinite(startS) && Number.isFinite(endS) && endS > startS) {
            const startMs = Math.round(startS * 1000);
            const endMs = Math.round(endS * 1000);
            updateLabel(startMs, endMs);
            clearBtn.hidden = false;
            onRangeChange?.({ startMs, endMs });
          }
        },
      ],
    },
  };

  const plot = new uPlot(opts, [xs, ys], chartDiv);

  function updateLabel(startMs, endMs) {
    const fmt = (ms) => new Date(ms).toISOString().slice(11, 19);
    const durSec = Math.max(1, Math.round((endMs - startMs) / 1000));
    let dur;
    if (durSec >= 3600) dur = `${(durSec / 3600).toFixed(1)} h`;
    else if (durSec >= 60) dur = `${(durSec / 60).toFixed(1)} min`;
    else dur = `${durSec} s`;
    label.firstChild.textContent =
      `Range: ${fmt(startMs)} → ${fmt(endMs)} (${dur}). `;
  }

  clearBtn.addEventListener('click', () => {
    plot.setSelect({ left: 0, top: 0, width: 0, height: 0 });
    label.firstChild.textContent =
      'Drag on the strip below to pick a time range to scope charts and exports to.';
    clearBtn.hidden = true;
    onRangeChange?.(null);
  });

  const resizeObs = new ResizeObserver(() => {
    plot.setSize({ width: chartDiv.clientWidth, height: 90 });
  });
  resizeObs.observe(chartDiv);

  return {
    destroy() {
      resizeObs.disconnect();
      plot.destroy();
    },
    setRange(range) {
      if (!range) {
        plot.setSelect({ left: 0, top: 0, width: 0, height: 0 });
        clearBtn.hidden = true;
        return;
      }
      const leftPx = plot.valToPos(range.startMs / 1000, 'x');
      const rightPx = plot.valToPos(range.endMs / 1000, 'x');
      plot.setSelect({
        left: leftPx, top: 0,
        width: rightPx - leftPx, height: plot.bbox.height,
      });
      updateLabel(range.startMs, range.endMs);
      clearBtn.hidden = false;
    },
  };
}

/**
 * Encode / decode a range to/from the URL hash. Format: #t=ISO/ISO
 */
export function rangeToHash(range) {
  if (!range) return '';
  return `#t=${new Date(range.startMs).toISOString()}/${new Date(range.endMs).toISOString()}`;
}

export function rangeFromHash(hash) {
  if (!hash || !hash.startsWith('#t=')) return null;
  const parts = hash.slice(3).split('/');
  if (parts.length !== 2) return null;
  const start = Date.parse(parts[0]);
  const end = Date.parse(parts[1]);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return { startMs: start, endMs: end };
}

/**
 * Filter a record list to records whose start lies within [range.startMs, range.endMs].
 */
export function scopeRecordsToRange(records, range) {
  if (!range) return records;
  return records.filter((r) => r.startMs >= range.startMs && r.startMs <= range.endMs);
}
