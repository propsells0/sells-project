/* ════════════════════════════════════════════════════════════════════
   Plotly chart helpers — Ain Real Estate flat design system.
   Periwinkle primary, Teal accent, Coral support.
   Solid backgrounds, neutral grids, straight lines by default.
   ════════════════════════════════════════════════════════════════════ */

// Light/dark palettes. `CHART_COLORS` is mutated in place on theme change
// (rather than reassigned) so existing references in this module still
// point at the live object — and so callers can keep using property
// access (CHART_COLORS.brand) without needing to re-read every render.
const _CHART_PALETTES = {
  // Light-mode chart palette — vibrant, saturated, broadly distinct.
  // Earlier iterations were too pastel for users to tell categories apart
  // on dense surfaces (treemaps, donuts with 6+ slices). This pass picks
  // hues from across the wheel and keeps each one deep enough to read
  // confidently against a white card without going neon-bright.
  light: {
    brand:       '#5b3fd6',  // deep purple — primary
    brand2:      '#7c5cff',  // bright purple — secondary brand
    brand3:      '#b0a1ff',  // soft purple — backgrounds / 3rd accents
    accent:      '#0e7c70',  // dark teal — high-contrast secondary
    accent2:     '#22b39d',  // teal — visible on white
    accent3:     '#7ddacc',  // mint — fills / area
    secondary:   '#a25344',  // dark coral — distinct from brand
    secondary2:  '#e87a64',  // coral — bright accent
    info:        '#1d4ed8',  // dark blue — distinct from purple
    warning:     '#b86200',  // dark amber — readable on white
    warning2:    '#e89028',  // amber — bright accent
    danger:      '#b91c34',  // dark red — readable
    danger2:     '#e53e5b',  // red — bright accent
    muted:       '#6b7282',
    text:        '#1a1d2c',  // near-black for axis labels
    textMuted:   '#3f4554',
    bg:          '#ffffff',
    surface2:    '#f4f6fc',
    border:      '#e1e4ee',
    grid:        '#ebedf5',
    gridStrong:  '#d2d6e3',
  },
  dark: {
    // Bright pastels against deep navy — the dark-mode opposite of the
    // light palette. Each hue is light enough (lightness ≥60%) to stay
    // legible on the surface-solid background without buzzing.
    brand:       '#a78bff',  // light purple — primary
    brand2:      '#c4b0ff',  // lighter purple
    brand3:      '#7c5cff',  // mid purple — for fills
    accent:      '#5fe3d6',  // light teal
    accent2:     '#8fefe4',  // mint — bright
    accent3:     '#3ab8ad',  // mid teal
    secondary:   '#f5a690',  // light coral
    secondary2:  '#ffc7b8',  // peach
    info:        '#7faaff',  // light blue
    warning:     '#ffc46b',  // light amber
    warning2:    '#ffd690',  // peach-amber
    danger:      '#ff7f95',  // light pink
    danger2:     '#ffa3b3',  // softer pink
    muted:       '#8389a0',
    text:        '#eaecf3',
    textMuted:   '#b6b9c7',
    bg:          '#1c1f2b',          // matches --surface-solid
    surface2:    '#20232f',
    border:      '#2a2d3a',
    grid:        '#262934',
    gridStrong:  '#353948',
  },
};

const CHART_COLORS = Object.assign({}, _CHART_PALETTES.light);

function _currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function _applyChartPalette(theme) {
  const src = _CHART_PALETTES[theme] || _CHART_PALETTES.light;
  Object.keys(src).forEach(k => { CHART_COLORS[k] = src[k]; });
}
_applyChartPalette(_currentTheme());

// Per-container redraw closures. Every public draw function (drawBarChart
// / drawDonut / etc.) registers its argument list keyed by container id;
// on theme change we re-invoke each closure so the chart re-renders with
// the active palette. Means pages don't have to wire a per-page handler
// — the chart helpers handle their own theming end-to-end.
const _redrawCallbacks = new Map();
function _registerRedraw(id, fn) {
  if (id) _redrawCallbacks.set(id, fn);
}

window.addEventListener('themechange', () => {
  _applyChartPalette(_currentTheme());
  _rebuildPalette();

  // Three-layer redraw strategy. Pages can opt into whichever fits:
  //
  // 1) Closure replay — every Charts.drawXxx() call registers its args
  //    keyed by container id; we replay each. Catches charts whose
  //    drawer-side body reads CHART_COLORS at render time.
  //
  // 2) window.onThemeChange — explicit hook for pages whose render()
  //    captures hex strings at the call site (PALETTE[i], scoreColorHex)
  //    and so the closure replay would paint with stale colours.
  //    Implementing it should re-run the page's chart-rendering
  //    function(s) without making a network round-trip.
  //
  // 3) onLangChange fallback — older pages that already wire
  //    everything through onLangChange() get a free ride.
  _redrawCallbacks.forEach((fn, id) => {
    const el = document.getElementById(id);
    if (!el || !el.isConnected) {
      _redrawCallbacks.delete(id);
      return;
    }
    try { fn(); } catch (e) { console.warn('chart redraw failed:', id, e); }
  });

  if (typeof window.onThemeChange === 'function') {
    try { window.onThemeChange(_currentTheme()); } catch (e) { console.warn('onThemeChange failed:', e); }
  } else if (typeof window.onLangChange === 'function') {
    try { window.onLangChange(typeof getLang === 'function' ? getLang() : 'ar'); } catch (_) {}
  }
});

// Honored across all chart fns. Read once at module load.
const _PREFERS_REDUCED_MOTION = !!(window.matchMedia &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches);

function _isRTL() { return document.documentElement.dir === 'rtl'; }
function _chartLocale() {
  return (typeof getLang === 'function' && getLang() === 'ar') ? 'ar' : 'en';
}
function _chartFontFamily() {
  return _isRTL()
    ? 'IBM Plex Sans Arabic, system-ui, sans-serif'
    : 'Inter, system-ui, sans-serif';
}

// Wait for Plotly to load (up to 5 seconds), then call the drawer function.
function _waitForPlotly(fn, attempt = 0) {
  if (typeof Plotly !== 'undefined') { fn(); return; }
  if (attempt >= 50) { console.warn('Plotly CDN failed to load'); return; }
  setTimeout(() => _waitForPlotly(fn, attempt + 1), 100);
}

// Per-container draw tokens — guards against the race where a pending Plotly
// draw fires AFTER an empty-state innerHTML write (or after a newer draw call
// has been queued). Each draw call captures its token; if the current token
// has advanced by the time the deferred callback fires, the draw is skipped.
const _drawTokens = new Map();
function _bumpToken(id) {
  const next = (_drawTokens.get(id) || 0) + 1;
  _drawTokens.set(id, next);
  return next;
}

// Per-container ResizeObserver registry. Plotly's `responsive: true` only
// reacts to window resizes — when a chart's PARENT changes size (e.g. opening
// a <details> deep-dive panel, sidebar collapse, mobile drawer), Plotly stays
// at its old layout. A debounced ResizeObserver per container fixes that.
const _resizeObservers = new Map();   // id → ResizeObserver
const _resizeDebounce  = new Map();   // id → setTimeout handle

function _attachResizeObserver(el) {
  if (typeof ResizeObserver === 'undefined') return;
  if (!el || !el.id) return;
  // Already observing? Disconnect first so each fresh draw starts clean.
  const prev = _resizeObservers.get(el.id);
  if (prev) { try { prev.disconnect(); } catch (_) {} }
  const ro = new ResizeObserver(() => {
    const handle = _resizeDebounce.get(el.id);
    if (handle) clearTimeout(handle);
    _resizeDebounce.set(el.id, setTimeout(() => {
      _resizeDebounce.delete(el.id);
      // Element may have been detached (route change / template re-render).
      if (!el.isConnected) return;
      // Custom (non-Plotly) renderers tag themselves so we can re-run
      // their tracked draw closure on resize instead of asking Plotly to
      // resize a chart it didn't render. Currently used by the custom
      // CSS treemap.
      if (el.classList.contains('ct-treemap')) {
        const fn = _redrawCallbacks.get(el.id);
        if (fn) { try { fn(); } catch (_) {} }
        return;
      }
      try {
        if (typeof Plotly !== 'undefined' && Plotly.Plots && Plotly.Plots.resize) {
          Plotly.Plots.resize(el);
        }
      } catch (_) {}
    }, 100));
  });
  try { ro.observe(el); } catch (_) { return; }
  _resizeObservers.set(el.id, ro);
}

function _detachResizeObserver(id) {
  const ro = _resizeObservers.get(id);
  if (ro) { try { ro.disconnect(); } catch (_) {} _resizeObservers.delete(id); }
  const handle = _resizeDebounce.get(id);
  if (handle) { clearTimeout(handle); _resizeDebounce.delete(id); }
}

// Single entry point for every chart mount. Bumps the container's token,
// waits for Plotly, then on resolution: re-checks the token, purges Plotly
// internal state on the element, wipes innerHTML (removes .chart-skel and
// any leftover empty-state), calls Plotly.newPlot, and attaches a debounced
// ResizeObserver so parent-size changes redraw at the correct dimensions.
function _drawChart(el, traces, layout, config) {
  if (!el || !el.id) return;
  const id = el.id;
  const myToken = _bumpToken(id);
  _waitForPlotly(() => {
    if (_drawTokens.get(id) !== myToken) return; // superseded by a newer call
    try { if (typeof Plotly !== 'undefined' && Plotly.purge) Plotly.purge(el); } catch (_) {}
    el.innerHTML = '';
    Plotly.newPlot(el, traces, layout, config);
    _attachResizeObserver(el);
  });
}

// Empty-state caller-facing helper: invalidate any pending draw + purge Plotly
// state so the caller can safely set its own innerHTML (e.g., empty-state).
// Without this, a deferred Plotly.newPlot fires after the empty-state write
// and renders the chart over the "No data" text.
function cancelPending(elOrId) {
  const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
  if (!el || !el.id) return;
  _bumpToken(el.id);
  _detachResizeObserver(el.id);
  try { if (typeof Plotly !== 'undefined' && Plotly.purge) Plotly.purge(el); } catch (_) {}
}

// Sequential palette: rotated for maximum hue separation between adjacent
// slots, so a 6-slice donut or 12-tile treemap reads as 6/12 distinct
// categories instead of three pairs of similar colours. Order picks one
// hue family per slot (purple → teal → amber → blue → coral → red →
// soft-purple → mint) before doubling back to lighter variants.
//
// Defined as a `let`-like mutable array so theme changes can rewrite
// values in place — callers that captured a reference (Charts.PALETTE,
// PALETTE[2], etc.) keep pointing at the same array. _rebuildPalette()
// runs on every theme change to keep entries in sync with CHART_COLORS.
const PALETTE = [];
function _rebuildPalette() {
  const next = [
    CHART_COLORS.brand,       // purple
    CHART_COLORS.accent,      // teal
    CHART_COLORS.warning,     // dark amber / yellow
    CHART_COLORS.info,        // dark blue
    CHART_COLORS.secondary2,  // coral
    CHART_COLORS.danger,      // red
    CHART_COLORS.brand2,      // bright purple
    CHART_COLORS.accent2,     // mid teal
    CHART_COLORS.warning2,    // bright amber
    CHART_COLORS.brand3,      // soft purple
    CHART_COLORS.secondary,   // dark coral
    CHART_COLORS.accent3,     // mint
    CHART_COLORS.danger2,     // bright red
  ];
  PALETTE.length = 0;
  next.forEach(c => PALETTE.push(c));
}
_rebuildPalette();

function chartLayout(opts = {}) {
  const fontFamily = _chartFontFamily();
  return {
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: {
      family: fontFamily,
      color: CHART_COLORS.textMuted,
      size: 12,
    },
    margin: { t: 16, r: 24, b: 44, l: 56, ...opts.margin },
    // Charts are explanatory, not exploratory — zoom/pan add complexity
    // (accidental scroll-zoom on touchpads, stuck zoom states) without
    // earning their keep on dashboards. dragmode:false kills canvas
    // drag-to-zoom; fixedrange on each axis kills axis-level zoom and
    // double-click-to-reset. Hover tooltips are unaffected.
    dragmode: false,
    xaxis: {
      gridcolor: CHART_COLORS.grid,
      linecolor: CHART_COLORS.grid,
      zerolinecolor: CHART_COLORS.gridStrong,
      zeroline: false,
      color: CHART_COLORS.textMuted,
      tickfont: { size: 11 },
      automargin: true,
      fixedrange: true,
      ...opts.xaxis,
    },
    yaxis: {
      gridcolor: CHART_COLORS.grid,
      linecolor: CHART_COLORS.grid,
      zerolinecolor: CHART_COLORS.gridStrong,
      zeroline: false,
      color: CHART_COLORS.textMuted,
      tickfont: { size: 11 },
      automargin: true,
      fixedrange: true,
      ...opts.yaxis,
    },
    showlegend: opts.showlegend !== false,
    legend: {
      font: { color: CHART_COLORS.textMuted, size: 11 },
      bgcolor: CHART_COLORS.bg,
      bordercolor: CHART_COLORS.border,
      borderwidth: 0,
      orientation: opts.legendOrientation || 'h',
      y: opts.legendY != null ? opts.legendY : -0.18,
      x: 0.5,
      xanchor: 'center',
      ...opts.legend,
    },
    hoverlabel: {
      bgcolor: CHART_COLORS.bg,
      bordercolor: CHART_COLORS.gridStrong,
      font: { color: CHART_COLORS.text, size: 12, family: fontFamily },
    },
    transition: { duration: _PREFERS_REDUCED_MOTION ? 0 : 300 },
    ...opts.layout,
  };
}

// Default chart config — modebar is hidden by default. Zoom/pan are
// disabled at the layout level (dragmode:false + axis fixedrange) so
// the only useful modebar button left would be toImage (PNG export);
// that's not common enough to justify the extra UI noise on every chart.
// Pages that want it can pass {displayModeBar: 'hover'} per-chart.
function _chartConfig(overrides = {}) {
  return {
    displayModeBar: false,
    displaylogo: false,
    scrollZoom: false,
    doubleClick: false,
    modeBarButtonsToRemove: [
      'zoom2d', 'pan2d', 'zoomIn2d', 'zoomOut2d',
      'autoScale2d', 'resetScale2d',
      'lasso2d', 'select2d',
    ],
    responsive: true,
    locale: _chartLocale(),
    ...overrides,
  };
}

// Backwards-compat global — anything that imports chartConfig still gets the defaults.
const chartConfig = _chartConfig();

function drawBarChart(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  // Narrow viewports get smaller fonts and a steeper x-axis tick angle so
  // labels don't ride over each other. Default tick angle is the page-
  // supplied options.xangle if any, else 0 / -25 depending on width.
  const containerW = (el.clientWidth || el.offsetWidth || 600);
  const isNarrow = containerW < 600;
  const isVeryNarrow = containerW < 420;
  const fontSize = isVeryNarrow ? 9 : (isNarrow ? 10 : 11);
  const tickAngle = (options.xangle != null)
    ? options.xangle
    : (isNarrow ? -35 : 0);

  const barColors = data.colors || PALETTE[0];
  // Per-bar contrast colour for inside-positioned labels — white on light
  // mint or pastel teal was unreadable. `insidetextfont.color` accepts an
  // array, so each bar's label gets the right colour for its own fill.
  const insideTextColors = _contrastTextColors(barColors);

  const trace = {
    type: 'bar',
    x: data.x,
    y: data.y,
    marker: {
      color: barColors,
      line: { width: 0 },
    },
    text: data.labels || data.y.map(v => typeof v === 'number' ? v.toFixed(1) : v),
    // 'auto' lets Plotly choose inside/outside per bar based on space —
    // prevents the outside label from being clipped at the top of the
    // chart on narrow viewports.
    textposition: isVeryNarrow ? 'inside' : 'auto',
    insidetextanchor: 'middle',
    insidetextfont: { color: insideTextColors, size: fontSize, family: _chartFontFamily(), weight: 700 },
    outsidetextfont: { color: CHART_COLORS.text, size: fontSize, family: _chartFontFamily(), weight: 600 },
    cliponaxis: false,
    hovertemplate: (options.hovertemplate || '<b>%{x}</b><br>%{y}<extra></extra>'),
  };

  const layout = chartLayout({
    xaxis: { tickangle: tickAngle, tickfont: { size: fontSize } },
    yaxis: { tickfont: { size: fontSize } },
    margin: { t: 12, r: isNarrow ? 14 : 24, b: isNarrow ? 64 : 44, l: isNarrow ? 44 : 56 },
    showlegend: false,
    bargap: 0.35,
    ...options,
  });

  _drawChart(el, [trace], layout, _chartConfig());
}

// Reused canvas for label width measurement — avoids creating a new context per call.
let _measureCanvas = null;
function _measureLabelWidth(labels, fontPx, fontFamily) {
  if (!labels || !labels.length) return 0;
  if (!_measureCanvas) _measureCanvas = document.createElement('canvas');
  const ctx = _measureCanvas.getContext('2d');
  ctx.font = `${fontPx}px ${fontFamily}`;
  let max = 0;
  for (const l of labels) {
    if (l == null) continue;
    const w = ctx.measureText(String(l)).width;
    if (w > max) max = w;
  }
  return max;
}

function drawHorizontalBar(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  // Container width drives margin sizing — at typical mobile widths the old
  // fixed margins (left 180px, right 80px) ate so much of the canvas that
  // the bars themselves had ~115px to render in. Read the actual rendered
  // width and scale margins down proportionally.
  const containerW = (el.clientWidth || el.offsetWidth || 600);
  const isNarrow = containerW < 600;
  const isVeryNarrow = containerW < 420;

  const fontSize = isVeryNarrow ? 10 : (isNarrow ? 11 : 11);

  const barColors = data.colors || PALETTE[0];
  const insideTextColors = _contrastTextColors(barColors);

  const trace = {
    type: 'bar',
    orientation: 'h',
    x: data.x,
    y: data.y,
    marker: {
      color: barColors,
      line: { width: 0 },
    },
    text: data.labels || data.x.map(v => typeof v === 'number' ? v.toFixed(1) + '%' : v),
    // 'auto' lets Plotly pick inside vs outside per bar — prevents the
    // outside label from overlapping the next bar / the axis on narrow
    // viewports. For very tight viewports we force 'inside' so labels
    // never collide with the y-axis tick numbers.
    textposition: isVeryNarrow ? 'inside' : 'auto',
    insidetextanchor: 'end',
    // Per-bar inside text colour so labels stay readable on light pastels
    // (mint, light teal). Outside labels use the surface text colour.
    insidetextfont: { color: insideTextColors, size: fontSize, family: _chartFontFamily(), weight: 700 },
    outsidetextfont: { color: CHART_COLORS.text, size: fontSize, family: _chartFontFamily(), weight: 600 },
    cliponaxis: false,
    hovertemplate: '<b>%{y}</b><br>%{x}<extra></extra>',
  };

  // Pre-measure the longest y-label so long names don't overflow into the
  // bars, but cap it tighter on narrow viewports so the bar area stays
  // readable. On mobile a left margin of 110-130 leaves enough width for
  // the bars to actually mean something.
  const labelFontPx = fontSize;
  const measured = _measureLabelWidth(data.y, labelFontPx, _chartFontFamily());
  let leftMargin;
  if (isVeryNarrow) {
    leftMargin = Math.min(120, Math.ceil(measured) + 12);
  } else if (isNarrow) {
    leftMargin = Math.min(160, Math.max(110, Math.ceil(measured) + 18));
  } else if (options.measureLabels === true) {
    leftMargin = Math.min(300, Math.max(180, Math.ceil(measured) + 24));
  } else {
    leftMargin = 180;
  }

  // Right margin shrinks on mobile because labels are now 'auto'/'inside'
  // — we no longer need a wide outside-label gutter.
  const rightMargin = isNarrow ? 16 : 80;

  const layout = chartLayout({
    showlegend: false,
    margin: { l: leftMargin, r: rightMargin, t: 16, b: 36 },
    bargap: 0.35,
    ...options,
  });

  _drawChart(el, [trace], layout, _chartConfig());
}

function drawDonut(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const containerW = el.clientWidth || el.offsetWidth || 600;
  const isNarrow = containerW < 480;

  // Single-category donut renders as a full ring; the legend is redundant
  // and overlaps the slice label. Drop it and shrink the bottom margin.
  const isSingle = !data.values || data.values.length <= 1;
  const showLegend = isSingle ? false : (options.showlegend !== false);

  const sliceColors = data.colors || PALETTE;
  const insideTextColors = Array.isArray(sliceColors)
    ? sliceColors.map(_contrastTextColor)
    : _contrastTextColor(sliceColors);

  // Inside-only text. Outside labels were clipping at the chart-card edge
  // ("Deals Revenue 87.4%" was getting half-cut at the bottom of the
  // donut row on mobile). Since the legend below already carries the
  // label↔colour mapping, we put the percent INSIDE each slice in a
  // contrast colour and rely on the legend for the name. Slices too
  // small to fit the percent will hide their text automatically — the
  // legend still tells you what colour they are.
  const trace = {
    type: 'pie',
    labels: data.labels,
    values: data.values,
    hole: 0.62,
    marker: {
      colors: sliceColors,
      line: { color: CHART_COLORS.bg, width: 2 },
    },
    textinfo: options.textinfo || 'percent',
    textposition: options.textposition || 'inside',
    insidetextorientation: 'horizontal',
    insidetextfont: { color: insideTextColors, size: isNarrow ? 12 : 14, family: _chartFontFamily(), weight: 700 },
    automargin: true,
    hovertemplate: '<b>%{label}</b><br>%{value} (%{percent})<extra></extra>',
    sort: false,
  };

  const layout = chartLayout({
    showlegend: showLegend,
    legend: {
      orientation: 'h',
      y: -0.08,
      x: 0.5,
      xanchor: 'center',
      font: { color: CHART_COLORS.text, size: isNarrow ? 11 : 12, family: _chartFontFamily() },
    },
    margin: {
      // Tighter margins because we no longer have outside labels needing
      // a gutter. Legend reserves bottom padding when shown.
      t: 16,
      r: isNarrow ? 16 : 24,
      b: showLegend ? (isNarrow ? 64 : 72) : 16,
      l: isNarrow ? 16 : 24,
    },
    annotations: options.centerText ? [{
      text: options.centerText,
      showarrow: false,
      font: { size: isNarrow ? 18 : 22, color: CHART_COLORS.text, family: _chartFontFamily(), weight: 700 },
    }] : [],
    ...options,
  });

  _drawChart(el, [trace], layout, _chartConfig());
}

function drawLineChart(containerId, series, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  // Real-data trends should default to linear lines — splines invent values
  // between data points, which contradicts "trust through legibility".
  // Pass options.shape: 'spline' explicitly when smoothing is intentional.
  const lineShape = options.shape || 'linear';

  const traces = series.map((s, i) => {
    const color = s.color || PALETTE[i % PALETTE.length];
    const fillColor = hexToRgba(color, 0.14);
    return {
      type: 'scatter',
      mode: 'lines+markers',
      name: s.name,
      x: s.x,
      y: s.y,
      line: { color, width: 2.5, shape: lineShape, smoothing: lineShape === 'spline' ? 1.0 : undefined },
      marker: {
        size: 7,
        color: CHART_COLORS.bg,
        line: { color, width: 2 },
      },
      fill: s.fill ? 'tozeroy' : 'none',
      fillcolor: s.fill ? fillColor : undefined,
      hovertemplate: '<b>%{x}</b><br>' + s.name + ': %{y}<extra></extra>',
    };
  });

  const layout = chartLayout({
    legendOrientation: 'h',
    legendY: -0.15,
    ...options,
  });
  _drawChart(el, traces, layout, _chartConfig());
}

function drawAreaChart(containerId, series, options = {}) {
  // Same as line but always fills.
  return drawLineChart(containerId,
    series.map(s => ({ ...s, fill: true })),
    options);
}

function drawGauge(containerId, value, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const color = value >= 75 ? CHART_COLORS.accent2
              : value >= 55 ? CHART_COLORS.brand
              : value >= 40 ? CHART_COLORS.warning2
              : CHART_COLORS.danger;

  // Scale number/tick fonts down on narrow viewports so the indicator fits
  // mobile screens without the value glyph overflowing the arc.
  const isNarrow = (typeof window !== 'undefined' && window.innerWidth < 600);
  const numSize = isNarrow ? 26 : 36;
  const tickSize = isNarrow ? 9 : 10;
  const margin = isNarrow ? { t: 14, r: 14, b: 14, l: 14 } : { t: 20, r: 20, b: 20, l: 20 };

  const trace = {
    type: 'indicator',
    mode: 'gauge+number',
    value: value,
    number: { suffix: '%', font: { color: CHART_COLORS.text, size: numSize, family: _chartFontFamily() } },
    gauge: {
      axis: { range: [0, 100], tickcolor: CHART_COLORS.muted, tickfont: { size: tickSize } },
      bar: { color: color, thickness: 0.78 },
      bgcolor: CHART_COLORS.surface2,
      borderwidth: 0,
      steps: [
        { range: [0, 25],   color: 'rgba(186, 26, 26, 0.10)' },
        { range: [25, 55],  color: 'rgba(255, 184, 77, 0.14)' },
        { range: [55, 75],  color: 'rgba(71, 77, 197, 0.10)' },
        { range: [75, 100], color: 'rgba(0, 131, 124, 0.14)' },
      ],
      threshold: {
        line: { color: CHART_COLORS.text, width: 3 },
        thickness: 0.85,
        value: options.target || 75,
      },
    },
  };

  const layout = chartLayout({
    margin: margin,
    showlegend: false,
    ...options,
  });

  // Single-indicator gauges don't need the export modebar.
  _drawChart(el, [trace], layout, _chartConfig({ displayModeBar: false }));
}

function drawRadarChart(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const trace = {
    type: 'scatterpolar',
    r: data.values,
    theta: data.labels,
    fill: 'toself',
    fillcolor: hexToRgba(CHART_COLORS.brand, 0.18),
    line: { color: CHART_COLORS.brand, width: 2.5, shape: 'spline', smoothing: 0.6 },
    marker: { size: 7, color: CHART_COLORS.bg, line: { color: CHART_COLORS.brand, width: 2 } },
    hovertemplate: '<b>%{theta}</b><br>%{r}%<extra></extra>',
  };

  const layout = {
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: {
      family: _chartFontFamily(),
      color: CHART_COLORS.textMuted,
      size: 11,
    },
    margin: { t: 30, r: 60, b: 30, l: 60 },
    showlegend: false,
    polar: {
      bgcolor: 'transparent',
      radialaxis: {
        visible: true,
        range: [0, 100],
        gridcolor: CHART_COLORS.grid,
        linecolor: CHART_COLORS.grid,
        tickfont: { size: 10, color: CHART_COLORS.muted },
        showline: false,
        fixedrange: true,
      },
      angularaxis: {
        gridcolor: CHART_COLORS.grid,
        linecolor: CHART_COLORS.grid,
        tickfont: { size: 11, color: CHART_COLORS.text },
      },
    },
    transition: { duration: _PREFERS_REDUCED_MOTION ? 0 : 300 },
    ...options,
  };

  _drawChart(el, [trace], layout, _chartConfig());
}

function drawStackedBar(containerId, series, xLabels, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const traces = series.map((s, i) => ({
    type: 'bar',
    name: s.name,
    x: xLabels,
    y: s.values,
    marker: {
      color: s.color || PALETTE[i % PALETTE.length],
      line: { width: 0 },
    },
    hovertemplate: '<b>%{x}</b><br>' + s.name + ': %{y}<extra></extra>',
  }));

  const layout = chartLayout({
    barmode: options.barmode || 'stack',
    bargap: 0.32,
    ...options,
  });

  _drawChart(el, traces, layout, _chartConfig());
}

function drawGroupedBar(containerId, series, xLabels, options = {}) {
  return drawStackedBar(containerId, series, xLabels, { ...options, barmode: 'group' });
}

/**
 * Heatmap — for matrices like "user × KPI achievement %".
 */
function drawHeatmap(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  // Theme-aware diverging colorscale: cold (low achievement) cells are tinted
  // toward danger / warning, hot (high achievement) cells lean into the brand
  // hue. Light mode uses near-white for the bottom; dark mode uses a low-
  // saturation navy so the darkest cells aren't louder than the busiest ones.
  const isDark = _currentTheme() === 'dark';
  const colorscale = isDark
    ? [
        [0,    hexToRgba(CHART_COLORS.danger, 0.22)],
        [0.30, hexToRgba(CHART_COLORS.warning, 0.30)],
        [0.55, hexToRgba(CHART_COLORS.brand, 0.30)],
        [0.80, hexToRgba(CHART_COLORS.brand, 0.65)],
        [1,    CHART_COLORS.brand],
      ]
    : [
        [0,    hexToRgba(CHART_COLORS.danger,  0.14)],
        [0.30, hexToRgba(CHART_COLORS.warning, 0.18)],
        [0.55, hexToRgba(CHART_COLORS.brand,   0.20)],
        [0.80, hexToRgba(CHART_COLORS.brand,   0.55)],
        [1,    CHART_COLORS.brand],
      ];

  const trace = {
    type: 'heatmap',
    z: data.z,
    x: data.x,
    y: data.y,
    colorscale: colorscale,
    colorbar: {
      thickness: 10,
      len: 0.8,
      tickfont: { size: 10, color: CHART_COLORS.muted },
      outlinewidth: 0,
    },
    hovertemplate: '<b>%{y}</b><br>%{x}: %{z}<extra></extra>',
    showscale: options.showscale !== false,
  };

  const layout = chartLayout({
    margin: { t: 20, r: 60, b: 80, l: 130 },
    xaxis: { tickangle: -30, gridcolor: 'transparent', showgrid: false },
    yaxis: { gridcolor: 'transparent', showgrid: false },
    ...options,
  });

  _drawChart(el, [trace], layout, _chartConfig());
}

/**
 * Treemap — useful for breaking down revenue / lead allocation.
 *
 * Plotly's default treemap text crams "label + raw value + percent" in any
 * available pixels and crops everything when a tile is small. We override:
 *   • `text` is pre-rendered: name on one line, compact value (M/B/K) on a
 *     second line separated by <br> — readable even on narrow tiles.
 *   • textfont stays bold and a touch larger (14px) so the smallest tiles
 *     still surface a name fragment instead of going blank.
 *   • textposition defaults to 'middle center' for visual balance.
 *
 * `options.compactValues = false` falls back to raw values for callers that
 * need exact numbers (e.g. count breakdowns under 1k).
 */
// ───────────────────────────────────────────────────────────────────
//  Custom treemap (CSS + squarify)
//
//  We dropped Plotly's treemap because, on the small-and-similar value
//  distributions this dataset produces, the squarify implementation
//  there leaves visible empty rectangles between tiles regardless of
//  config combination. Click also triggered a drill-down state that
//  recoloured tiles. The custom version:
//    • runs a textbook squarify that GUARANTEES every pixel of the
//      container is covered (no gaps);
//    • paints absolute-positioned divs we fully control;
//    • gives every tile a per-tile contrast text colour;
//    • re-renders on container resize and on theme change;
//    • doesn't drill down on click — flat data, flat behaviour.
// ───────────────────────────────────────────────────────────────────

// Standard squarify: lay out items into the container so the resulting
// tile aspect ratios stay close to 1. Items must be sorted by `value`
// descending. Returns an array of {item, x, y, w, h}.
function _squarifyLayout(items, x, y, w, h) {
  if (!items.length) return [];
  const total = items.reduce((s, i) => s + (i.value || 0), 0);
  if (total <= 0 || w <= 0 || h <= 0) return [];

  const totalArea = w * h;
  const withArea = items.map(it => ({
    item: it,
    area: ((it.value || 0) / total) * totalArea,
  }));
  return _squarifyRecurse(withArea, x, y, w, h);
}

function _squarifyRecurse(items, x, y, w, h) {
  if (!items.length) return [];
  if (w <= 0 || h <= 0) return [];
  if (items.length === 1) {
    return [{ ...items[0].item, x, y, w, h }];
  }

  const shortSide = Math.min(w, h);

  let row = [];
  let rowSum = 0;
  let bestRatio = Infinity;
  let i = 0;
  while (i < items.length) {
    const candidate = [...row, items[i]];
    const candSum = rowSum + items[i].area;
    const candRatio = _worstAspect(candidate, shortSide, candSum);
    if (row.length === 0 || candRatio < bestRatio) {
      row = candidate;
      rowSum = candSum;
      bestRatio = candRatio;
      i++;
    } else {
      break;
    }
  }

  // Lay out the chosen row along the short side; remaining items fill
  // the rectangle to the right of (or below) the row.
  const rowLen = rowSum / shortSide;
  const placed = [];

  if (w >= h) {
    // Row sits as a column on the left, item heights vary along the
    // short side (h).
    let cy = y;
    for (const r of row) {
      const itemH = r.area / rowLen;
      placed.push({ ...r.item, x: x, y: cy, w: rowLen, h: itemH });
      cy += itemH;
    }
    // Force last tile to extend to the bottom edge to avoid sub-pixel
    // gaps from accumulated rounding.
    if (placed.length) {
      const last = placed[placed.length - 1];
      last.h = (y + h) - last.y;
    }
    const rest = _squarifyRecurse(items.slice(row.length), x + rowLen, y, w - rowLen, h);
    return [...placed, ...rest];
  } else {
    // Row sits as a strip on top, widths vary along the short side (w).
    let cx = x;
    for (const r of row) {
      const itemW = r.area / rowLen;
      placed.push({ ...r.item, x: cx, y: y, w: itemW, h: rowLen });
      cx += itemW;
    }
    if (placed.length) {
      const last = placed[placed.length - 1];
      last.w = (x + w) - last.x;
    }
    const rest = _squarifyRecurse(items.slice(row.length), x, y + rowLen, w, h - rowLen);
    return [...placed, ...rest];
  }
}

function _worstAspect(row, shortSide, rowSum) {
  if (!row.length) return Infinity;
  let max = 0, min = Infinity;
  for (const r of row) {
    if (r.area > max) max = r.area;
    if (r.area < min) min = r.area;
  }
  const w2 = shortSide * shortSide;
  const s2 = rowSum * rowSum;
  return Math.max((w2 * max) / s2, s2 / (w2 * min));
}

function _escapeHTML(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function drawTreemap(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  // Make the host a positioning context for absolute children.
  el.classList.add('ct-treemap');

  const labels = data.labels || [];
  const values = data.values || [];
  if (!labels.length) {
    el.innerHTML = `<div class="empty-state" style="padding:30px">${(typeof t === 'function' ? t('dash.no_data') : 'No data')}</div>`;
    return;
  }

  const useCompact = options.compactValues !== false;
  const valueFormatter = options.valueFormatter
    || (typeof window !== 'undefined' && window.fmtCompactMoney
        ? window.fmtCompactMoney
        : (v) => (typeof v === 'number' ? v.toLocaleString() : String(v)));

  // Resolve colours fresh each call so theme switches pick up the new
  // palette via the page-level onThemeChange path.
  const colors = (data.colors && data.colors.length)
    ? data.colors
    : labels.map((_, i) => PALETTE[i % PALETTE.length]);

  // Build sortable items. Sort descending by value — required by the
  // squarify algorithm to produce roughly-square tiles. `meta` is an
  // optional per-tile payload propagated to onTileClick so callers can
  // recover the original record (e.g. a rep's monthly breakdown).
  const metaArr = data.meta || [];
  const items = labels
    .map((lbl, i) => ({
      label: lbl,
      value: values[i] || 0,
      color: colors[i % colors.length],
      meta: metaArr[i],
      formatted: useCompact
        ? valueFormatter(values[i] || 0)
        : (typeof values[i] === 'number' ? values[i].toLocaleString() : String(values[i] || '')),
    }))
    .filter(it => it.value > 0)
    .sort((a, b) => b.value - a.value);

  if (!items.length) {
    el.innerHTML = `<div class="empty-state" style="padding:30px">${(typeof t === 'function' ? t('dash.no_data') : 'No data')}</div>`;
    return;
  }

  // Use the host's actual rendered dimensions. If the host hasn't been
  // laid out yet (display:none parent, deferred mount), fall back to
  // the CSS min-height and full row width — the ResizeObserver below
  // will redraw once real dimensions are available.
  let containerW = el.clientWidth || el.offsetWidth;
  let containerH = el.clientHeight || el.offsetHeight;
  if (containerW < 50 || containerH < 50) {
    containerW = Math.max(containerW, 320);
    containerH = Math.max(containerH, 280);
  }

  const tiles = _squarifyLayout(items, 0, 0, containerW, containerH);

  // Build the DOM in one shot — innerHTML is faster than per-tile
  // appendChild and avoids visible flicker on redraw.
  const clickable = typeof options.onTileClick === 'function';
  const sep = 1; // separator gutter in px (renders as inset, not gap)
  const html = tiles.map((tile, idx) => {
    const textCol = _contrastTextColor(tile.color);
    const isTiny = tile.w < 70 || tile.h < 44;
    const isSmall = tile.w < 110 || tile.h < 70;
    const labelSize = isTiny ? 10 : (isSmall ? 12 : 14);
    const valueSize = isTiny ? 9 : (isSmall ? 11 : 12);
    const tileCls = `ct-tile${clickable ? ' is-clickable' : ''}`;
    const tileAttrs = clickable
      ? ` data-tile-idx="${idx}" tabindex="0" role="button"`
      : '';
    return `
      <div class="${tileCls}"${tileAttrs}
           style="left:${tile.x}px;top:${tile.y}px;
                  width:${Math.max(0, tile.w - sep)}px;
                  height:${Math.max(0, tile.h - sep)}px;
                  background:${tile.color};
                  color:${textCol};"
           title="${_escapeHTML(tile.label)} · ${_escapeHTML(tile.formatted)}">
        <div class="ct-tile-inner">
          <div class="ct-tile-label" style="font-size:${labelSize}px">${_escapeHTML(tile.label)}</div>
          ${!isTiny ? `<div class="ct-tile-value" style="font-size:${valueSize}px">${_escapeHTML(tile.formatted)}</div>` : ''}
        </div>
      </div>`;
  }).join('');

  // Cancel any pending Plotly draw on this id from earlier (the tracked
  // closure system might still hold one) so it doesn't paint over us.
  if (typeof Plotly !== 'undefined' && Plotly.purge) {
    try { Plotly.purge(el); } catch (_) {}
  }
  _bumpToken(el.id);
  el.innerHTML = html;

  // Wire click + keyboard activation when a handler was supplied. tiles[]
  // and the rendered DOM children are in matching order — squarify
  // preserves item order via {...r.item, ...} spreads.
  if (clickable) {
    el.querySelectorAll('.ct-tile.is-clickable').forEach((node) => {
      const idx = Number(node.dataset.tileIdx);
      const tile = tiles[idx];
      if (!tile) return;
      const fire = (ev) => {
        ev.preventDefault();
        try { options.onTileClick(tile); } catch (e) { console.error(e); }
      };
      node.addEventListener('click', fire);
      node.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') fire(ev);
      });
    });
  }

  _attachResizeObserver(el);
}

/**
 * Funnel — useful for sales pipeline (leads → meetings → reservations → deals).
 */
function drawFunnel(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const trace = {
    type: 'funnel',
    y: data.y,
    x: data.x,
    text: data.labels || data.x.map(v => typeof v === 'number' ? v.toLocaleString() : v),
    textposition: 'inside',
    textfont: { color: CHART_COLORS.bg, size: 13, family: _chartFontFamily() },
    marker: {
      color: data.colors || [
        CHART_COLORS.brand3,
        CHART_COLORS.brand,
        CHART_COLORS.accent2,
        CHART_COLORS.accent3,
        CHART_COLORS.warning2,
      ],
      line: { width: 0 },
    },
    connector: { line: { color: 'rgba(124,131,253,0.30)', width: 1 } },
    hovertemplate: '<b>%{y}</b><br>%{x}<extra></extra>',
  };

  const layout = chartLayout({
    margin: { l: 130, r: 30, t: 20, b: 20 },
    showlegend: false,
    ...options,
  });

  _drawChart(el, [trace], layout, _chartConfig());
}

/**
 * Scatter / bubble — e.g., user calls vs deals with bubble = revenue.
 */
function drawScatter(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const trace = {
    type: 'scatter',
    mode: 'markers',
    x: data.x,
    y: data.y,
    text: data.text,
    marker: {
      size: data.sizes || 14,
      sizemode: 'diameter',
      sizeref: data.sizeref || 1,
      color: data.colors || CHART_COLORS.brand,
      line: { color: CHART_COLORS.bg, width: 2 },
      opacity: 0.85,
    },
    hovertemplate: '<b>%{text}</b><br>%{xaxis.title.text}: %{x}<br>%{yaxis.title.text}: %{y}<extra></extra>',
  };

  const layout = chartLayout({
    showlegend: false,
    ...options,
  });

  _drawChart(el, [trace], layout, _chartConfig());
}

/**
 * Combo chart — bar + line on dual axes.
 */
function drawComboBarLine(containerId, barSeries, lineSeries, xLabels, options = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const traces = [
    {
      type: 'bar',
      name: barSeries.name,
      x: xLabels,
      y: barSeries.values,
      marker: { color: barSeries.color || CHART_COLORS.brand3, line: { width: 0 } },
      yaxis: 'y',
      hovertemplate: '<b>%{x}</b><br>' + barSeries.name + ': %{y}<extra></extra>',
    },
    {
      type: 'scatter',
      mode: 'lines+markers',
      name: lineSeries.name,
      x: xLabels,
      y: lineSeries.values,
      line: { color: lineSeries.color || CHART_COLORS.brand, width: 3, shape: 'spline' },
      marker: { size: 8, color: CHART_COLORS.bg, line: { color: lineSeries.color || CHART_COLORS.brand, width: 2 } },
      yaxis: 'y2',
      hovertemplate: '<b>%{x}</b><br>' + lineSeries.name + ': %{y}<extra></extra>',
    },
  ];

  const layout = chartLayout({
    yaxis: { title: barSeries.name, side: 'left', gridcolor: CHART_COLORS.grid },
    // The secondary axis isn't covered by chartLayout's default fixedrange,
    // so set it inline to keep the no-zoom contract consistent here too.
    yaxis2: {
      title: lineSeries.name,
      side: 'right',
      overlaying: 'y',
      showgrid: false,
      tickfont: { size: 11, color: CHART_COLORS.textMuted },
      fixedrange: true,
    },
    bargap: 0.4,
    legendOrientation: 'h',
    legendY: -0.18,
    ...options,
  });

  _drawChart(el, traces, layout, _chartConfig());
}

function scoreColorHex(pct) {
  return pct >= 75 ? CHART_COLORS.accent2
       : pct >= 55 ? CHART_COLORS.brand
       : pct >= 40 ? CHART_COLORS.warning2
       : CHART_COLORS.danger;
}

function hexToRgba(hex, alpha = 1) {
  const m = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex);
  if (!m) return hex;
  const r = parseInt(m[1], 16);
  const g = parseInt(m[2], 16);
  const b = parseInt(m[3], 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// Pick a foreground text colour that stays readable on `bgHex`. Returns
// near-black for light fills (light teal, light purple, mint, peach) and
// off-white for dark fills (deep purple, dark blue, etc.). Threshold is
// tuned to WCAG luminance — values ≥ 0.55 read as "light bg, use dark
// text", everything else uses light text. Returns CHART_COLORS.text
// for any input we can't parse so callers always get a usable colour.
function _contrastTextColor(bgHex) {
  if (!bgHex) return CHART_COLORS.text;
  const m = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(String(bgHex));
  if (!m) return CHART_COLORS.text;
  const r = parseInt(m[1], 16) / 255;
  const g = parseInt(m[2], 16) / 255;
  const b = parseInt(m[3], 16) / 255;
  // sRGB → linearised luminance approximation (cheap; full WCAG is overkill
  // for picking between two text colours).
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return lum > 0.55 ? '#1a1d2c' : '#ffffff';
}

// Map a colour or array of colours to their per-cell contrast text colour.
// Bar / treemap drawers feed this into Plotly's `insidetextfont.color`
// which accepts an array (one colour per data point).
function _contrastTextColors(bgColors) {
  if (Array.isArray(bgColors)) return bgColors.map(_contrastTextColor);
  return _contrastTextColor(bgColors);
}

// Wrap every public draw function so its call args are remembered against
// the container id. The themechange handler walks the registry and calls
// each closure to repaint with the active palette — pages don't need a
// per-template hook.
function _trackedDraw(fn) {
  return function trackedDraw(containerId, ...rest) {
    _registerRedraw(containerId, () => fn(containerId, ...rest));
    return fn(containerId, ...rest);
  };
}

window.Charts = {
  drawBarChart:     _trackedDraw(drawBarChart),
  drawHorizontalBar: _trackedDraw(drawHorizontalBar),
  drawDonut:        _trackedDraw(drawDonut),
  drawLineChart:    _trackedDraw(drawLineChart),
  drawAreaChart:    _trackedDraw(drawAreaChart),
  drawGauge:        _trackedDraw(drawGauge),
  drawRadarChart:   _trackedDraw(drawRadarChart),
  drawStackedBar:   _trackedDraw(drawStackedBar),
  drawGroupedBar:   _trackedDraw(drawGroupedBar),
  drawHeatmap:      _trackedDraw(drawHeatmap),
  drawTreemap:      _trackedDraw(drawTreemap),
  drawFunnel:       _trackedDraw(drawFunnel),
  drawScatter:      _trackedDraw(drawScatter),
  drawComboBarLine: _trackedDraw(drawComboBarLine),
  scoreColorHex,
  hexToRgba,
  cancelPending,
  COLORS: CHART_COLORS,
  PALETTE,
};
