/**
 * DateRange picker — cross-cutting filter component.
 *
 * Public API:
 *   const ctrl = DateRange.mount(host, opts)
 *   ctrl.getRange()          → current { from, to, preset, isSubMonth, monthStr }
 *   ctrl.setRange(partial)   → programmatic update
 *   ctrl.setMode(m)          → "full" | "month-only"
 *   ctrl.refreshLabels()     → re-render after lang toggle
 *   ctrl.toQueryString(extra)→ "from=...&to=...&preset=...&extra=..."
 *   ctrl.destroy()
 *
 * Helpers exposed alongside:
 *   DateRange.formatTick(date, lang)   → localized tick label
 *   DateRange.formatDate(yyyymmdd, lang) → localized date string
 *
 * Mounts inline on desktop, sheet-modal on mobile (coarse pointer or <720px).
 * Reads/writes URL params (?from, ?to, ?preset) when syncURL=true (default).
 *
 * Sub-month presets are filtered out when mode='month-only'. Custom mode uses
 * <input type="date"> normally, <input type="month"> in month-only mode.
 *
 * No external dependency beyond common.js's t() / getLang() helpers.
 */
(function (global) {
  "use strict";

  // ─── State + constants ───────────────────────────────────────────────────
  const ALL_PRESETS = [
    "today", "yesterday", "this_week", "last_7", "last_30",
    "this_month", "last_month", "this_quarter", "ytd",
  ];
  const SUB_MONTH_PRESETS = new Set(["today", "yesterday", "this_week", "last_7"]);
  const STORAGE_KEY_PREFIX = "ain_dr_";

  // ─── Date math (pure) ────────────────────────────────────────────────────
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function ymd(d) { return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()); }
  function ym(d)  { return d.getFullYear() + "-" + pad2(d.getMonth() + 1); }

  function parseYMD(s) {
    if (!s || typeof s !== "string") return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!m) return null;
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    return isNaN(d.getTime()) ? null : d;
  }

  function lastDayOfMonth(y, m) { return new Date(y, m, 0).getDate(); }

  function resolvePreset(key, today) {
    const t = today ? new Date(today.getFullYear(), today.getMonth(), today.getDate()) : new Date();
    t.setHours(0, 0, 0, 0);
    const from = new Date(t);
    const to = new Date(t);
    switch (key) {
      case "today":
        return { from, to };
      case "yesterday": {
        from.setDate(from.getDate() - 1);
        to.setDate(to.getDate() - 1);
        return { from, to };
      }
      case "this_week": {
        // ISO Mon-start, matches server date_range.py.
        const dow = (t.getDay() + 6) % 7; // 0=Mon..6=Sun
        from.setDate(from.getDate() - dow);
        return { from, to };
      }
      case "last_7":
        from.setDate(from.getDate() - 6);
        return { from, to };
      case "last_30":
        from.setDate(from.getDate() - 29);
        return { from, to };
      case "this_month":
        from.setDate(1);
        return { from, to };
      case "last_month": {
        const firstThis = new Date(t.getFullYear(), t.getMonth(), 1);
        const lastPrev = new Date(firstThis); lastPrev.setDate(0);
        const firstPrev = new Date(lastPrev.getFullYear(), lastPrev.getMonth(), 1);
        return { from: firstPrev, to: lastPrev };
      }
      case "this_quarter": {
        const qStart = Math.floor(t.getMonth() / 3) * 3;
        return { from: new Date(t.getFullYear(), qStart, 1), to };
      }
      case "ytd":
        return { from: new Date(t.getFullYear(), 0, 1), to };
      default:
        return null;
    }
  }

  function isCalendarMonth(from, to) {
    if (from.getFullYear() !== to.getFullYear()) return null;
    if (from.getMonth() !== to.getMonth()) return null;
    if (from.getDate() !== 1) return null;
    if (to.getDate() !== lastDayOfMonth(to.getFullYear(), to.getMonth() + 1)) return null;
    return ym(from);
  }

  function isAligned(from, to) {
    return from.getDate() === 1
      && to.getDate() === lastDayOfMonth(to.getFullYear(), to.getMonth() + 1);
  }

  function diffDaysInclusive(from, to) {
    return Math.round((to - from) / 86400000) + 1;
  }

  // ─── Localization helpers (display only) ─────────────────────────────────
  function _t(key, opts) {
    const i = opts && opts.i18n;
    if (i && typeof i.t === "function") return i.t(key);
    if (typeof global.t === "function") return global.t(key);
    return key;
  }
  function _lang(opts) {
    const i = opts && opts.i18n;
    if (i && typeof i.getLang === "function") return i.getLang();
    if (typeof global.getLang === "function") return global.getLang();
    return "ar";
  }

  function formatDate(yyyy_mm_dd, lang) {
    const d = parseYMD(yyyy_mm_dd);
    if (!d) return yyyy_mm_dd || "";
    const locale = lang === "en" ? "en-US" : "ar-EG";
    try {
      return d.toLocaleDateString(locale, { year: "numeric", month: "short", day: "numeric" });
    } catch (_) {
      return yyyy_mm_dd;
    }
  }

  function formatTick(d, lang) {
    if (!(d instanceof Date)) d = parseYMD(d);
    if (!d) return "";
    const locale = lang === "en" ? "en-US" : "ar-EG";
    try {
      return d.toLocaleDateString(locale, { month: "short", day: "numeric" });
    } catch (_) {
      return ymd(d);
    }
  }

  // ─── Range object helpers ────────────────────────────────────────────────
  function rangeFromDates(fromD, toD, presetKey) {
    const monthStr = isCalendarMonth(fromD, toD);
    const aligned = isAligned(fromD, toD);
    return {
      from: ymd(fromD),
      to: ymd(toD),
      preset: presetKey || "custom",
      isSubMonth: !aligned,
      monthStr,
    };
  }

  function presetIsAllowed(presetKey, mode) {
    if (mode === "month-only" && SUB_MONTH_PRESETS.has(presetKey)) return false;
    return true;
  }

  // ─── Mount ───────────────────────────────────────────────────────────────
  function mount(host, opts) {
    if (!host) throw new Error("DateRange.mount: host element required");
    opts = opts || {};
    const onChange = typeof opts.onChange === "function" ? opts.onChange : () => {};
    const syncURL = opts.syncURL !== false;
    const urlPrefix = opts.urlPrefix || "";
    const mode = opts.mode === "month-only" ? "month-only" : "full";
    let currentMode = mode;

    // Decide initial range: URL → localStorage → defaultPreset
    function readURL() {
      try {
        const sp = new URLSearchParams(global.location ? global.location.search : "");
        const k = (s) => urlPrefix ? urlPrefix + "_" + s : s;
        const f = sp.get(k("from"));
        const t = sp.get(k("to"));
        const p = sp.get(k("preset"));
        const m = sp.get("month");      // legacy
        if (f && t) {
          const fromD = parseYMD(f);
          const toD = parseYMD(t);
          if (fromD && toD && fromD <= toD) return rangeFromDates(fromD, toD, p || "custom");
        }
        if (m && /^\d{4}-\d{2}$/.test(m)) {
          const [y, mo] = m.split("-").map(Number);
          return rangeFromDates(new Date(y, mo - 1, 1), new Date(y, mo - 1, lastDayOfMonth(y, mo)), "custom");
        }
        if (p && (ALL_PRESETS.indexOf(p) >= 0)) {
          const r = resolvePreset(p);
          if (r) return rangeFromDates(r.from, r.to, p);
        }
      } catch (_) {}
      return null;
    }

    function readStorage() {
      try {
        if (!global.localStorage) return null;
        const raw = global.localStorage.getItem(STORAGE_KEY_PREFIX + (urlPrefix || "default"));
        if (!raw) return null;
        const obj = JSON.parse(raw);
        if (obj && obj.from && obj.to) {
          const fromD = parseYMD(obj.from);
          const toD = parseYMD(obj.to);
          if (fromD && toD && fromD <= toD) return rangeFromDates(fromD, toD, obj.preset || "custom");
        }
      } catch (_) {}
      return null;
    }

    function defaultRange() {
      const presetKey = presetIsAllowed(opts.defaultPreset, mode) ? (opts.defaultPreset || "this_month") : "this_month";
      const r = resolvePreset(presetKey, opts.today ? parseYMD(opts.today) : null);
      return rangeFromDates(r.from, r.to, presetKey);
    }

    let range = readURL() || readStorage() || defaultRange();
    // Force month-only mode to a valid preset.
    if (currentMode === "month-only" && range.isSubMonth) {
      range = defaultRange();
    }

    // ─── DOM build ─────────────────────────────────────────────────────────
    function presetsForMode(m) {
      return m === "month-only"
        ? ALL_PRESETS.filter(k => !SUB_MONTH_PRESETS.has(k))
        : ALL_PRESETS;
    }

    function build() {
      const lang = _lang(opts);
      host.dir = lang === "ar" ? "rtl" : "ltr";
      host.classList.add("dr");
      host.innerHTML = "";

      // Summary line (clickable on touch)
      const summary = document.createElement("button");
      summary.type = "button";
      summary.className = "dr-summary";
      summary.setAttribute("aria-expanded", "false");
      summary.innerHTML = `
        <span class="material-symbols-outlined dr-summary-icon" aria-hidden="true">date_range</span>
        <span class="dr-summary-text"></span>
        <span class="material-symbols-outlined dr-summary-chev" aria-hidden="true">expand_more</span>
      `;
      host.appendChild(summary);

      // Panel
      const panel = document.createElement("div");
      panel.className = "dr-panel";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", _t("dr.preset_group", opts));
      panel.hidden = true;
      host.appendChild(panel);

      const presets = document.createElement("div");
      presets.className = "dr-panel-presets";
      presets.setAttribute("role", "group");
      presets.setAttribute("aria-label", _t("dr.preset_group", opts));
      panel.appendChild(presets);

      presetsForMode(currentMode).forEach(key => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "dr-preset";
        b.dataset.preset = key;
        b.innerHTML = `<span class="dr-preset-label">${_t("dr." + key, opts)}</span>`;
        b.addEventListener("click", () => selectPreset(key));
        presets.appendChild(b);
      });

      // Custom inputs section — ALWAYS visible (no separate "Custom"
      // button to click first). The user picks a preset from the row
      // above, or types/picks a date in the inputs below and hits Apply.
      // Two interaction paths, one panel, no hidden state.
      const custom = document.createElement("div");
      custom.className = "dr-panel-custom";
      const inputType = currentMode === "month-only" ? "month" : "date";
      custom.innerHTML = `
        <div class="dr-custom-row">
          <div class="dr-custom-field">
            <label>${_t("dr.custom_start", opts)}</label>
            <input type="${inputType}" class="dr-custom-from">
          </div>
          <div class="dr-custom-field">
            <label>${_t("dr.custom_end", opts)}</label>
            <input type="${inputType}" class="dr-custom-to">
          </div>
        </div>
        <div class="dr-custom-error" role="alert" aria-live="polite" hidden></div>
        <div class="dr-custom-actions">
          <button type="button" class="btn btn-primary dr-apply">${_t("dr.apply", opts)}</button>
        </div>
      `;
      panel.appendChild(custom);
      // Pre-fill the inputs with the active range so the user always
      // sees what's currently selected — they can edit in place.
      _syncCustomInputs();

      const footnote = document.createElement("div");
      footnote.className = "dr-panel-footnote";
      footnote.textContent = currentMode === "month-only"
        ? _t("dr.footnote_month_grain", opts)
        : _t("dr.footnote_submission", opts);
      panel.appendChild(footnote);

      // Wire summary toggle
      summary.addEventListener("click", () => {
        const open = panel.hidden;
        panel.hidden = !open;
        summary.setAttribute("aria-expanded", String(open));
      });

      // Apply button — wired to the always-visible custom inputs. Cancel
      // button removed because the inputs are no longer in a hide/show
      // state to "cancel out of"; closing the panel implicitly cancels.
      custom.querySelector(".dr-apply").addEventListener("click", applyCustom);

      // Click-outside closes
      document.addEventListener("click", _outsideClick);

      // Keyboard: Escape closes
      panel.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          panel.hidden = true;
          summary.setAttribute("aria-expanded", "false");
          summary.focus();
        }
      });

      refreshLabels();
    }

    function _outsideClick(e) {
      if (!host.contains(e.target)) {
        const panel = host.querySelector(".dr-panel");
        const summary = host.querySelector(".dr-summary");
        if (panel && !panel.hidden) {
          panel.hidden = true;
          if (summary) summary.setAttribute("aria-expanded", "false");
        }
      }
    }

    // Sync the always-visible custom inputs to the currently-selected
    // range. Replaces the old toggleCustom() which used to hide the
    // custom area; we now keep it open at all times and just rewrite
    // input values whenever the active range changes via a preset click.
    function _syncCustomInputs() {
      const custom = host.querySelector(".dr-panel-custom");
      if (!custom) return;
      const errEl = custom.querySelector(".dr-custom-error");
      if (errEl) errEl.hidden = true;
      const f = custom.querySelector(".dr-custom-from");
      const t = custom.querySelector(".dr-custom-to");
      if (!f || !t) return;
      if (currentMode === "month-only") {
        f.value = range.from.slice(0, 7);
        t.value = range.to.slice(0, 7);
      } else {
        f.value = range.from;
        t.value = range.to;
      }
    }

    function toggleCustom(open) {
      // Kept for backward compat with any external caller, but the panel
      // is always open now — `open=true` just refocuses the from input.
      const custom = host.querySelector(".dr-panel-custom");
      if (!custom) return;
      _syncCustomInputs();
      if (open) {
        const f = custom.querySelector(".dr-custom-from");
        if (f) f.focus();
      }
    }

    function applyCustom() {
      const custom = host.querySelector(".dr-panel-custom");
      const fInput = custom.querySelector(".dr-custom-from").value;
      const tInput = custom.querySelector(".dr-custom-to").value;
      let fromD, toD;
      if (currentMode === "month-only") {
        if (!/^\d{4}-\d{2}$/.test(fInput) || !/^\d{4}-\d{2}$/.test(tInput)) {
          return showError(_t("errors.range_inverted", opts));
        }
        const [fy, fm] = fInput.split("-").map(Number);
        const [ty, tm] = tInput.split("-").map(Number);
        fromD = new Date(fy, fm - 1, 1);
        toD = new Date(ty, tm - 1, lastDayOfMonth(ty, tm));
      } else {
        fromD = parseYMD(fInput);
        toD = parseYMD(tInput);
      }
      if (!fromD || !toD) return showError(_t("errors.range_inverted", opts));
      if (toD < fromD) return showError(_t("errors.range_inverted", opts));
      // 5-year cap (mirrors Config.MAX_RANGE_YEARS)
      if ((toD - fromD) / 86400000 > 5 * 366) {
        return showError(_t("errors.range_too_wide", opts));
      }
      setInternal(rangeFromDates(fromD, toD, "custom"));
      toggleCustom(false);
      closePanel();
    }

    function showError(msg) {
      const err = host.querySelector(".dr-custom-error");
      if (!err) return;
      err.textContent = msg;
      err.hidden = false;
    }

    function selectPreset(key) {
      if (!presetIsAllowed(key, currentMode)) return;
      const r = resolvePreset(key);
      if (!r) return;
      setInternal(rangeFromDates(r.from, r.to, key));
      closePanel();
    }

    function closePanel() {
      const panel = host.querySelector(".dr-panel");
      const summary = host.querySelector(".dr-summary");
      if (panel) panel.hidden = true;
      if (summary) summary.setAttribute("aria-expanded", "false");
    }

    function setInternal(newRange) {
      range = newRange;
      writeURL();
      writeStorage();
      refreshLabels();
      // Mirror the new range into the always-visible custom inputs so
      // clicking "Last 30 Days" updates the date pickers below it too.
      _syncCustomInputs();
      try { onChange(range); } catch (e) { console.error(e); }
    }

    function writeURL() {
      if (!syncURL || !global.history || !global.history.replaceState) return;
      try {
        const url = new URL(global.location.href);
        const k = (s) => urlPrefix ? urlPrefix + "_" + s : s;
        url.searchParams.set(k("from"), range.from);
        url.searchParams.set(k("to"), range.to);
        url.searchParams.set(k("preset"), range.preset);
        url.searchParams.delete("month");  // legacy param shed once picker takes over
        global.history.replaceState({}, "", url.toString());
      } catch (_) {}
    }

    function writeStorage() {
      try {
        if (!global.localStorage) return;
        global.localStorage.setItem(
          STORAGE_KEY_PREFIX + (urlPrefix || "default"),
          JSON.stringify({ from: range.from, to: range.to, preset: range.preset })
        );
      } catch (_) {}
    }

    function refreshLabels() {
      const lang = _lang(opts);
      host.dir = lang === "ar" ? "rtl" : "ltr";
      const txt = host.querySelector(".dr-summary-text");
      if (txt) {
        if (range.from === range.to) {
          // Single day
          const today = (opts.today || ymd(new Date()));
          txt.textContent = (range.from === today)
            ? _t("dr.summary_today", opts)
            : formatDate(range.from, lang);
        } else if (range.monthStr) {
          // Single calendar month — defer to fmtMonth from common.js if available
          const monthLabel = (typeof global.fmtMonth === "function")
            ? global.fmtMonth(range.monthStr)
            : range.monthStr;
          txt.textContent = _t("dr.summary_one_month", opts).replace("{month}", monthLabel);
        } else {
          const n = diffDaysInclusive(parseYMD(range.from), parseYMD(range.to));
          txt.textContent = _t("dr.summary_format", opts)
            .replace("{from}", formatDate(range.from, lang))
            .replace("{to}", formatDate(range.to, lang))
            .replace("{n}", String(n));
        }
      }
      // Update preset chips' active state
      const chips = host.querySelectorAll(".dr-preset");
      chips.forEach(b => {
        b.classList.toggle("is-active", b.dataset.preset === range.preset);
      });
      // Update labels (idempotent — re-reads i18n)
      chips.forEach(b => {
        const key = b.dataset.preset;
        const labelEl = b.querySelector(".dr-preset-label");
        if (labelEl) labelEl.textContent = _t("dr." + key, opts);
      });
      // Update custom button label, footnote, errors that may be visible
      const apply = host.querySelector(".dr-apply");
      const cancel = host.querySelector(".dr-cancel");
      if (apply) apply.textContent = _t("dr.apply", opts);
      if (cancel) cancel.textContent = _t("dr.cancel", opts);
      const footnote = host.querySelector(".dr-panel-footnote");
      if (footnote) {
        footnote.textContent = currentMode === "month-only"
          ? _t("dr.footnote_month_grain", opts)
          : _t("dr.footnote_submission", opts);
      }
    }

    build();
    // Defer the initial onChange to the next microtask so that callers
    // doing `_drCtrl = DateRange.mount(...)` finish the assignment before
    // their onChange handler runs. Otherwise their loadAll() reads _drCtrl
    // when it's still null and exits early — page stays empty.
    Promise.resolve().then(() => {
      try { onChange(range); } catch (e) { console.error(e); }
    });

    return {
      getRange: () => Object.assign({}, range),
      setRange: (partial) => {
        // Preset-key shortcut: setRange({preset: "ytd"}) resolves the dates
        // server-side (today). Lets pages programmatically widen the picker
        // (e.g. fall back to ytd when the default range returns empty).
        if (partial && partial.preset && partial.preset !== "custom"
            && ALL_PRESETS.indexOf(partial.preset) >= 0
            && !partial.from && !partial.to) {
          if (!presetIsAllowed(partial.preset, currentMode)) return;
          const r = resolvePreset(partial.preset);
          if (r) setInternal(rangeFromDates(r.from, r.to, partial.preset));
          return;
        }
        const fromD = parseYMD((partial && partial.from) || range.from);
        const toD = parseYMD((partial && partial.to) || range.to);
        if (!fromD || !toD || toD < fromD) return;
        setInternal(rangeFromDates(fromD, toD, (partial && partial.preset) || "custom"));
      },
      setMode: (m) => {
        if (m !== "full" && m !== "month-only") return;
        currentMode = m;
        if (m === "month-only" && range.isSubMonth) {
          // Snap to this_month if current range is incompatible.
          const r = resolvePreset("this_month");
          range = rangeFromDates(r.from, r.to, "this_month");
        }
        build();
      },
      refreshLabels,
      toQueryString: (extra) => {
        const sp = new URLSearchParams();
        sp.set("from", range.from);
        sp.set("to", range.to);
        sp.set("preset", range.preset);
        if (extra) for (const k in extra) {
          if (extra[k] != null) sp.set(k, extra[k]);
        }
        return sp.toString();
      },
      destroy: () => {
        document.removeEventListener("click", _outsideClick);
        host.innerHTML = "";
        host.classList.remove("dr");
      },
    };
  }

  // ─── Public namespace ────────────────────────────────────────────────────
  global.DateRange = {
    mount,
    formatTick,
    formatDate,
    resolvePreset,    // exposed for tests / advanced consumers
  };
})(typeof window !== "undefined" ? window : globalThis);
