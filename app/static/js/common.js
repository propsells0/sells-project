/* ──────────────────────────────────────────────────────────────────
   Shared browser helpers — API, toast, formatting, modals.
   `api()` now auto-attaches a CSRF header and normalises errors so
   callers can pass the thrown error directly to `tError()` (which
   comes from i18n.js) to get a localised, code-driven message.
   ────────────────────────────────────────────────────────────────── */
const API = window.location.origin;

// ─── CSRF token (fetched lazily once; refreshed on 403) ─────────────
let _CSRF = null;
async function _fetchCsrf() {
  try {
    const r = await fetch(API + "/api/auth/me", { credentials: "same-origin" });
    if (!r.ok) return null;
    const d = await r.json();
    _CSRF = d.csrf || null;
    // Cross-device sync of display prefs: when /me returns the user's
    // saved theme/lang and they differ from this device's localStorage
    // (typical on a fresh browser), adopt the server-side identity so
    // the user picks up where they left off without flipping the
    // toggle manually. Only happens when there's no `?theme=` /
    // `?lang=` URL override (the head-script handles those first).
    try {
      const q = new URLSearchParams(window.location.search);
      if (d.preferred_theme && !q.get("theme")) {
        const local = localStorage.getItem("ain_theme");
        if (local !== d.preferred_theme && (d.preferred_theme === "light" || d.preferred_theme === "dark")) {
          localStorage.setItem("ain_theme", d.preferred_theme);
          // push:false — value came from the server, no need to echo back.
          if (typeof setTheme === "function") setTheme(d.preferred_theme, { push: false });
          else document.documentElement.setAttribute("data-theme", d.preferred_theme);
        }
      }
      if (d.preferred_lang && !q.get("lang")) {
        const local = localStorage.getItem("ain_lang");
        if (local !== d.preferred_lang && (d.preferred_lang === "ar" || d.preferred_lang === "en")) {
          localStorage.setItem("ain_lang", d.preferred_lang);
          if (typeof applyLang === "function") applyLang();
        }
      }
    } catch (_) { /* preferences sync is best-effort */ }
    return _CSRF;
  } catch { return null; }
}

// ─── Toast ─────────────────────────────────────────────────────────
function toast(message, type = "") {
  let el = document.getElementById("__toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "__toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = message || t("common.error");
  el.className = "toast show " + type;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    el.classList.remove("show");
  }, 3500);
}

// Toast with an inline action button — used for reversible operations like
// deactivate/archive that benefit from a one-click undo.
function toastWithAction(message, actionLabel, onAction, type = "") {
  let el = document.getElementById("__toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "__toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.innerHTML = "";
  const span = document.createElement("span");
  span.textContent = message || "";
  el.appendChild(span);
  const btn = document.createElement("button");
  btn.className = "toast-action";
  btn.type = "button";
  btn.textContent = actionLabel;
  btn.addEventListener("click", () => {
    el.classList.remove("show");
    if (typeof onAction === "function") onAction();
  });
  el.appendChild(btn);
  el.className = "toast show has-action " + type;
  clearTimeout(window.__toastTimer);
  // Longer dwell so the user can act on the offer.
  window.__toastTimer = setTimeout(() => { el.classList.remove("show"); }, 7000);
}

function toastError(err) { toast(tError(err), "error"); }

// ─── API wrapper ───────────────────────────────────────────────────
async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const needsCsrf = method !== "GET" && method !== "HEAD";

  const headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    ...(options.headers || {}),
  };

  if (needsCsrf) {
    if (!_CSRF) await _fetchCsrf();
    if (_CSRF) headers["X-CSRF-Token"] = _CSRF;
  }

  const opts = {
    credentials: "same-origin",
    ...options,
    headers,
    method,
  };

  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
  }

  let r;
  try {
    r = await fetch(API + path, opts);
  } catch (netErr) {
    const err = new Error(t("errors.server"));
    err.status = 0;
    err.netError = true;
    throw err;
  }

  let data = null;
  try { data = await r.json(); } catch { data = null; }

  if (!r.ok) {
    const code = data && data.error_code;
    const msg = (code && t("errors." + code)) || (data && data.error) || t("errors.server");
    const err = new Error(msg);
    err.status = r.status;
    err.data = data;
    err.error_code = code || null;
    if (r.status === 401 && path !== "/api/auth/me" && path !== "/api/auth/login") {
      // Session expired — bounce to login
      setTimeout(() => { window.location.href = "/login"; }, 50);
    }
    if (r.status === 403 && code === "forbidden" && needsCsrf) {
      _CSRF = null; // Force refetch on next call
    }
    throw err;
  }
  return data;
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
  window.location.href = "/login";
}

// ─── Dropdown / lang toggle ────────────────────────────────────────
function initUserDropdown() {
  const trigger = document.querySelector(".topnav-user");
  if (!trigger) return;
  const dropdown = trigger.querySelector(".dropdown");
  if (!dropdown) return;
  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    dropdown.classList.toggle("open");
  });
  document.addEventListener("click", () => dropdown.classList.remove("open"));
}

// Track the element that had focus before the drawer opened, so we can
// return focus on close (a11y requirement for modal dialogs).
let _sidebarReturnFocus = null;

function _isMobileViewport() { return window.innerWidth <= 1024; }

function toggleSidebar(force) {
  const sb = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  const burger = document.getElementById("navBurger");
  if (!sb) return;
  const willOpen = force === undefined ? !sb.classList.contains("open") : !!force;
  sb.classList.toggle("open", willOpen);
  if (backdrop) backdrop.classList.toggle("open", willOpen);
  if (burger) {
    burger.classList.toggle("open", willOpen);
    burger.setAttribute("aria-expanded", willOpen ? "true" : "false");
  }
  document.body.style.overflow = willOpen ? "hidden" : "";

  // Modal dialog behavior only applies when the sidebar acts as a drawer (mobile).
  if (_isMobileViewport()) {
    if (willOpen) {
      _sidebarReturnFocus = document.activeElement;
      // Move focus to the first focusable element inside the drawer.
      const first = sb.querySelector("a, button, [tabindex]:not([tabindex='-1'])");
      if (first) first.focus();
    } else if (_sidebarReturnFocus && typeof _sidebarReturnFocus.focus === "function") {
      _sidebarReturnFocus.focus();
      _sidebarReturnFocus = null;
    }
  }
}
window.toggleSidebar = toggleSidebar;

// Trap Tab inside the drawer while it's open on mobile, and close on Escape.
function _onSidebarKeydown(e) {
  const sb = document.getElementById("sidebar");
  if (!sb || !sb.classList.contains("open") || !_isMobileViewport()) return;

  if (e.key === "Escape") {
    e.preventDefault();
    toggleSidebar(false);
    return;
  }

  if (e.key !== "Tab") return;
  const focusable = sb.querySelectorAll(
    "a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])"
  );
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function initBurger() {
  const btn = document.getElementById("navBurger");
  if (!btn) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleSidebar();
  });
  // Auto-close when any sidebar link is tapped
  const sb = document.getElementById("sidebar");
  if (sb) sb.querySelectorAll("a, button").forEach(el => el.addEventListener("click", () => {
    if (window.innerWidth <= 1024) toggleSidebar(false);
  }));
  // Reset state when bumping back up to desktop layout
  window.addEventListener("resize", () => {
    if (window.innerWidth > 1024) toggleSidebar(false);
  });
}

function _refreshLangToggleLabel() {
  document.querySelectorAll("#langToggleBtn, .lang-toggle").forEach(btn => {
    // Show the *target* language: in AR mode → "EN", in EN mode → "AR".
    btn.textContent = getLang() === "ar" ? "EN" : "AR";
  });
}

function initLangToggle() {
  _refreshLangToggleLabel();
  document.querySelectorAll("#langToggleBtn, .lang-toggle").forEach(btn => {
    btn.addEventListener("click", () => {
      btn.classList.remove("spin-once"); void btn.offsetWidth;
      btn.classList.add("spin-once");
      const current = getLang();
      setLang(current === "ar" ? "en" : "ar");
      _refreshLangToggleLabel();
      if (typeof onLangChange === "function") onLangChange(getLang());
    });
  });
}

// ─── Theme (light/dark) ──────────────────────────────────────────────
// Theme is applied inline in <head> before render to avoid a flash. This
// only handles the runtime toggle. The chosen theme is persisted in
// localStorage; clearing it falls back to the OS preference.
function getTheme() {
  return document.documentElement.getAttribute("data-theme") || "light";
}

function setTheme(theme, opts) {
  const t = theme === "dark" ? "dark" : "light";
  // Pulse a transition class for ~320ms so backgrounds, text, borders
  // and SVG fills cross-fade between palettes instead of snapping. The
  // class is removed afterwards so it can't interfere with normal
  // hover / focus animations elsewhere in the app.
  const root = document.documentElement;
  root.classList.add("theme-transitioning");
  if (setTheme._pulseTimer) clearTimeout(setTheme._pulseTimer);
  setTheme._pulseTimer = setTimeout(() => {
    root.classList.remove("theme-transitioning");
  }, 320);

  root.setAttribute("data-theme", t);
  try { localStorage.setItem("ain_theme", t); } catch (_) {}
  // Persist for transactional emails — every email the system sends to
  // this user (forgot-password, signup approval, etc.) renders in the
  // skin they last picked. Best-effort: only fires when authenticated,
  // and a network blip just leaves the previous server-side value
  // intact (the next toggle retries). Opt out (`{push:false}`) when
  // we're applying a value that already came from the server, to avoid
  // an immediate echo-back POST.
  if (!opts || opts.push !== false) _pushPreference({ theme: t });
  // Broadcast so chart-rendering modules can swap their palette + redraw.
  window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: t } }));
}

// ─── Push display prefs to the server (best-effort) ─────────────────
// Only meaningful when the user is logged in. The endpoint short-circuits
// for anonymous calls (401) and we swallow that — anonymous toggles
// stay local-only.
let _prefsPushTimer = null;
function _pushPreference(patch) {
  // Coalesce rapid toggles (e.g. theme + lang in quick succession) into
  // a single PATCH-style POST. We keep the latest values on the window
  // and flush them after a short idle.
  if (!window.__pendingPrefs) window.__pendingPrefs = {};
  Object.assign(window.__pendingPrefs, patch || {});
  if (_prefsPushTimer) clearTimeout(_prefsPushTimer);
  _prefsPushTimer = setTimeout(async () => {
    const body = window.__pendingPrefs;
    window.__pendingPrefs = {};
    _prefsPushTimer = null;
    try {
      await api("/api/auth/preferences", { method: "POST", body });
    } catch (_) {
      // Anonymous (401) or transient — ignore. Local state already updated.
    }
  }, 250);
}

function initThemeToggle() {
  const btns = document.querySelectorAll("#themeToggleBtn, .theme-toggle");
  btns.forEach(btn => {
    btn.addEventListener("click", () => {
      btn.classList.remove("spin-once"); void btn.offsetWidth;
      btn.classList.add("spin-once");
      setTheme(getTheme() === "dark" ? "light" : "dark");
    });
  });

  // If the user hasn't explicitly chosen a theme, follow the OS as it
  // changes (e.g. system flips to dark at sunset). Once they click the
  // toggle, ain_theme is set and this listener becomes a no-op.
  if (window.matchMedia) {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e) => {
      if (localStorage.getItem("ain_theme")) return;
      document.documentElement.setAttribute("data-theme", e.matches ? "dark" : "light");
      window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: e.matches ? "dark" : "light" } }));
    };
    if (mq.addEventListener) mq.addEventListener("change", handler);
    else if (mq.addListener) mq.addListener(handler);
  }
}

// ─── Formatting ────────────────────────────────────────────────────
function ratingClass(rating) {
  return {
    "Excellent": "badge-excellent", "V.Good": "badge-vgood", "Good": "badge-good",
    "Medium": "badge-medium", "Weak": "badge-weak", "Bad": "badge-bad",
    "Pending": "badge-pending"
  }[rating] || "badge-pending";
}

function ratingLabel(rating) { return t("rating." + rating); }

function scoreColor(pct) {
  if (pct >= 75) return "success";
  if (pct >= 55) return "warn";
  return "danger";
}

// Six-band rating string from a 0–100 score. Mirrors app/kpi_logic.py RATINGS so
// the client falls back to the same bands when the server hasn't computed one yet.
function localRating(score) {
  const s = Number(score) || 0;
  if (s >= 90) return "Excellent";
  if (s >= 75) return "V.Good";
  if (s >= 55) return "Good";
  if (s >= 40) return "Medium";
  if (s >= 25) return "Weak";
  return "Bad";
}

function fmtMonth(monthStr) {
  if (!monthStr) return "—";
  const [y, m] = monthStr.split("-");
  const lang = getLang();
  const namesAr = ["يناير","فبراير","مارس","أبريل","مايو","يونيو","يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"];
  const namesEn = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  const names = lang === "en" ? namesEn : namesAr;
  return names[parseInt(m) - 1] + " " + y;
}

function currentMonth() {
  const d = new Date();
  return d.getFullYear() + "-" + (d.getMonth() + 1).toString().padStart(2, "0");
}

function fmtNum(n, decimals = 0) {
  if (n == null || isNaN(n)) return "—";
  const locale = getLang() === "ar" ? "ar-EG" : "en-US";
  return Number(n).toLocaleString(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtMoney(n) {
  if (n == null || isNaN(n)) return "—";
  const locale = getLang() === "ar" ? "ar-EG" : "en-US";
  return Number(n).toLocaleString(locale, { maximumFractionDigits: 0 });
}

// Compact money — use on dense KPI tiles where 50,000,000 wouldn't fit.
// Billions → B, millions → M, thousands → K (English letters in both
// locales — easier to scan and matches global finance convention).
// Decimals trimmed to one when the magnitude is large enough that the
// unit already conveys order of magnitude (e.g. "1.2B" not "1.20B").
function fmtCompactMoney(n) {
  if (n == null || isNaN(n)) return "—";
  const v = Number(n);
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1e9) return sign + (a / 1e9).toFixed(a >= 10e9 ? 1 : 2) + "B";
  if (a >= 1e6) return sign + (a / 1e6).toFixed(a >= 10e6 ? 1 : 2) + "M";
  if (a >= 1e3) return sign + (a / 1e3).toFixed(0) + "K";
  return fmtMoney(v);
}

// Compact non-money number — share the same M/B/K abbreviations so dense
// charts (treemaps, bars, donuts) read at a glance instead of forcing the
// eye to count digit groups. Use this anywhere a raw number could exceed
// 10K and the surface is too dense for the full digit run.
function fmtCompactNum(n) {
  if (n == null || isNaN(n)) return "—";
  const v = Number(n);
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1e9) return sign + (a / 1e9).toFixed(a >= 10e9 ? 1 : 2) + "B";
  if (a >= 1e6) return sign + (a / 1e6).toFixed(a >= 10e6 ? 1 : 2) + "M";
  if (a >= 1e3) return sign + (a / 1e3).toFixed(a >= 10e3 ? 0 : 1) + "K";
  return fmtNum(v);
}

// ─── Modal dialog (focus trap, Escape, focus return) ──────────────
const _MODAL_FOCUSABLE = "input:not([type='hidden']):not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";
let _modalReturnFocus = null;
let _activeModal = null;

function openModalDialog(modalEl) {
  if (!modalEl) return;
  _modalReturnFocus = document.activeElement;
  _activeModal = modalEl;
  modalEl.classList.add("open");
  document.body.style.overflow = "hidden";
  const panel = modalEl.querySelector(".modal") || modalEl;
  // Honor an explicit [data-default-focus] (used by destructive modals to focus
  // the safer Cancel button). Otherwise focus the first focusable.
  const explicit = panel.querySelector("[data-default-focus]");
  const target = explicit || panel.querySelector(_MODAL_FOCUSABLE);
  if (target) setTimeout(() => target.focus(), 0);
}

function closeModalDialog(modalEl) {
  if (!modalEl) return;
  modalEl.classList.remove("open");
  if (_activeModal === modalEl) _activeModal = null;
  if (!document.querySelector(".modal-backdrop.open")) document.body.style.overflow = "";
  if (_modalReturnFocus && typeof _modalReturnFocus.focus === "function") {
    _modalReturnFocus.focus();
    _modalReturnFocus = null;
  }
}

function _onModalKeydown(e) {
  if (!_activeModal || !_activeModal.classList.contains("open")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    closeModalDialog(_activeModal);
    return;
  }
  if (e.key !== "Tab") return;
  const panel = _activeModal.querySelector(".modal") || _activeModal;
  const focusable = panel.querySelectorAll(_MODAL_FOCUSABLE);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

// Backwards-compatible by-id wrappers — still used by existing onclick handlers.
function openModal(id) {
  openModalDialog(document.getElementById(id));
}
function closeModal(id) {
  closeModalDialog(document.getElementById(id));
}

// ─── Pass/Fail segmented toggle (used by tl_evaluation + dataentry) ─
function initPassfailToggles() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".passfail-toggle button[data-pf-target]");
    if (!btn) return;
    const id  = btn.getAttribute("data-pf-target");
    const val = btn.getAttribute("data-pf-value");
    const input = document.getElementById(id);
    if (input) input.value = val;
    btn.parentElement.querySelectorAll("button").forEach(b => {
      const active = b === btn;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-checked", active ? "true" : "false");
    });
    // Trigger an "input" event on the hidden input so live previews can react.
    if (input) input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

// ─── Password visibility toggles ───────────────────────────────────
function initPasswordToggles() {
  document.querySelectorAll(".pw-toggle").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-target");
      const input = id ? document.getElementById(id) : null;
      if (!input) return;
      const showing = input.getAttribute("type") === "text";
      input.setAttribute("type", showing ? "password" : "text");
      const icon = btn.querySelector(".material-symbols-outlined");
      if (icon) icon.textContent = showing ? "visibility" : "visibility_off";
      btn.setAttribute("aria-label", t(showing ? "common.show_password" : "common.hide_password"));
    });
  });
}

// ─── Sidebar Manager-Intervention badge (P3) ───────────────────────
// The sidebar link renders an empty `#navInterventionBadge` span. We
// populate it from /api/crm/intervention/open-count after auth resolves.
// The endpoint is role-gated to marketing/manager/admin — anonymous and
// out-of-scope users get a 401/403 and we silently leave the badge
// hidden, which is the correct visible state.
async function refreshInterventionBadge() {
  const el = document.getElementById("navInterventionBadge");
  if (!el) return;  // page doesn't have the sidebar (auth pages)
  try {
    const data = await api("/api/crm/intervention/open-count");
    const count = (data && data.open_count) || 0;
    if (count <= 0) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = count > 99 ? "99+" : String(count);
    // Red when there's at least one HIGH, amber otherwise.
    el.classList.toggle("is-high", (data && data.high_priority) > 0);
  } catch (_) {
    // 401/403/network — keep the badge hidden.
    el.hidden = true;
  }
}
// Expose for templates that toggle status to refresh the badge after a
// PATCH (e.g. marketing_lead_timeline.html, marketing_intervention.html).
window.refreshInterventionBadge = refreshInterventionBadge;

// ─── Reveal-on-scroll for .reveal elements ─────────────────────────
function initReveal() {
  if (!("IntersectionObserver" in window)) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach(en => {
      if (en.isIntersecting) {
        en.target.classList.add("revealed");
        io.unobserve(en.target);
      }
    });
  }, { threshold: 0.12 });
  document.querySelectorAll(".reveal").forEach(el => io.observe(el));
}

// Toggles `.is-stuck` on every `.controls-sticky` bar once it pins to the
// top of the viewport. Uses a 1px sentinel placed just before the bar
// rather than IntersectionObserver on the bar itself — observing the bar
// directly would trigger on its full vertical extent and produce a half-
// stuck state during the transition. The sentinel goes from "in view" to
// "out of view" the instant scrolling pins the bar, which is exactly the
// signal we want for shrinking the bar's padding/labels.
function initStickyShrink() {
  if (typeof IntersectionObserver === 'undefined') return;
  document.querySelectorAll('.controls-sticky').forEach(bar => {
    if (bar.dataset._stickyWired === '1') return;
    bar.dataset._stickyWired = '1';
    const sentinel = document.createElement('div');
    sentinel.style.cssText =
      'position:absolute;top:0;left:0;width:1px;height:1px;pointer-events:none;visibility:hidden;';
    bar.parentNode.insertBefore(sentinel, bar);
    const obs = new IntersectionObserver(
      ([entry]) => {
        bar.classList.toggle('is-stuck', !entry.isIntersecting);
      },
      { threshold: 0, rootMargin: '0px 0px 0px 0px' }
    );
    obs.observe(sentinel);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initUserDropdown();
  initBurger();
  initLangToggle();
  initThemeToggle();
  initReveal();
  initPasswordToggles();
  initPassfailToggles();
  initStickyShrink();
  refreshInterventionBadge();
  document.addEventListener("keydown", _onSidebarKeydown);
  document.addEventListener("keydown", _onModalKeydown);
  document.querySelectorAll(".modal-backdrop").forEach(m => {
    m.addEventListener("click", (e) => {
      if (e.target === m) m.classList.remove("open");
    });
  });
});
