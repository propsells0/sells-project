# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Ain Real Estate KPI & Sales Intelligence System. Flask + Jinja2 + Postgres app that bundles two products on the same deployment:
- **KPI tracking** for the sales org (sales reps, team leaders, managers, admin) with a weighted scoring formula and monthly evaluations.
- **PropFinder** — a units catalog synced from the Master V API every 14 days.

UI is bilingual (Arabic RTL by default, English LTR) and uses a custom glassmorphism CSS system. Charts use Plotly via `app/static/js/charts.js`.

## Commands

```bash
# Local run (uses Railway-hosted DB by default via config.py fallbacks)
python server.py                                  # http://localhost:8080
DISABLE_SYNC=true python server.py                # skip Master V scheduler

# Production-style run
gunicorn server:app --bind 0.0.0.0:8080 --timeout 300 --workers 1

# Install
pip install -r requirements.txt                   # Python 3.11

# Seed demo data (1 manager, 2 TLs, 10 sales, 3 months of KPI, 2 campaigns; idempotent)
python scripts/seed_demo.py

# Run a one-off script that needs the app context
DISABLE_SYNC=true PYTHONIOENCODING=utf-8 python -c "from app import create_app; ..."
```

There is no test suite, no linter config, and no build step. The Flask dev server emits Unicode log lines — set `PYTHONIOENCODING=utf-8` on Windows or piping will crash with `UnicodeEncodeError` (cp1252).

Default first-run admin: `admin / admin123` (override via `DEFAULT_ADMIN_*` env vars). Passwords are upgraded to PBKDF2 transparently on next login if still on the legacy SHA-256 format.

## Architecture

### Entry points
- `server.py` — Gunicorn target. Calls `create_app()` and starts the Master V sync thread unless `DISABLE_SYNC=true`. The Procfile runs `gunicorn server:app`.
- `app/__init__.py` — Flask factory. Calls `init_all_tables()` on import (creates/migrates schema on every boot) and registers all blueprints.

### Blueprints (URL prefix → file)
| Prefix | File | Purpose |
|---|---|---|
| (root) | `pages_bp.py` | HTML page routes, all `@role_required`/`@login_required` |
| `/api/auth` | `auth_bp.py` | login, logout, register, password reset, change-password, `/me` (returns CSRF) |
| `/api/users` | `users_bp.py` | user CRUD (admin) |
| `/api/kpi` | `kpi_bp.py` | KPI entries, monthly reports, TL evaluations |
| `/api/teams` | `teams_bp.py` | team CRUD + membership |
| `/api/finance` | `finance_bp.py` | revenue/commission projections |
| `/api/marketing` | `marketing_bp.py` | campaign tracking |
| `/api/util` | `util_bp.py` | small helpers consumed by the SPA (server `today`, mailer-configured flag) |
| `/api` (units, stats, sync) | `propfinder_bp.py` | PropFinder unit listing + manual sync trigger |

### Roles and home routing
Roles live in `app.auth.ROLES` and gate every page. `role_home(role)` in `app/auth.py` is the source of truth for post-login redirect:
- `admin → /dashboard`, `manager → /dashboard`, `team_leader → /team-leader`, `dataentry → /data-entry`, `marketing → /marketing`, `sales → /propfinder`. Admin retains access to `/admin` via sidebar nav; the post-login landing is the dashboard.

`@role_required(*roles)` always lets `admin` through. Sales reps are intentionally scoped to PropFinder only — they have no KPI self-entry page anymore (`/sales` redirects to `/propfinder`).

### KPI logic — single source of truth
All scoring lives in `app/kpi_logic.py`. Backend computations and frontend forms both consume `KPI_CONFIG` (sales) and `TL_KPI_CONFIG` (team leaders).

- Each KPI has `weight`, `target_type` (`fixed` or `leads_pct`), and `input_type` (`number`/`percent`/`passfail`).
- `compute_score(entry)` → `(total_score, rating_en, breakdown)` — weighted achievement out of 100.
- `compute_tl_score(tl_entry, team_entries)` — TL score blends auto-aggregated team metrics (`team_sum`, `team_leads_sum`, `team_avg`) with manual fields (`reports`, `clients_pipeline`, behavioural pass/fail).
- `compute_financials(entry, settings)` — projected revenue/commission from `deals` and `reservations` counts.
- Rating tiers (`RATINGS`) and the auto/manual TL field split (`TL_AUTO_FIELDS`, `TL_MANUAL_FIELDS`) are also exported from this module.

When the KPI catalog changes, edit only `kpi_logic.py`; the dashboard, data-entry, and TL evaluation pages all read it via `/api/kpi/config`.

### Database
`app/database.py:init_all_tables()` is idempotent and runs on every startup. It both creates new tables and runs additive migrations (`column_exists()` checks before `ALTER TABLE`). New columns must be added there, not in raw SQL elsewhere.

`get_conn()` retries twice with backoff. Either `DATABASE_URL` or the `DB_*` fallbacks in `config.py` are used.

### Auth & sessions
- Flask sessions, signed with `SECRET_KEY` from env (auto-generated ephemeral key if missing — sessions reset every restart, fine for dev).
- Password hashes: PBKDF2-SHA256 via Werkzeug. Legacy `sha256(password + 'ain_kpi_2026_salt')` hashes still verify and are upgraded on next login (`needs_rehash`).
- CSRF: double-submit token. Frontend `app/static/js/common.js#api()` fetches `/api/auth/me` once to learn the token, then includes it as `X-CSRF-Token` on every non-GET. Server enforces with `@csrf_protect`. On 403/`forbidden`, the client clears the cached token and refetches.
- Rate limit: `@rate_limit(name, limit, window)` from `app/auth.py` — in-memory only, fine for single-worker deploys.
- **Signup approval flow.** `/api/auth/register` lands new rows with `active=false, approval_status='pending'` and emails the requester. Admin lists pending at `GET /api/users/pending` and acts via `POST /api/users/<id>/approve` (optionally rewriting the role) or `/reject` (hard-delete). Both send the recipient a transactional email; the response carries `email_sent` so the admin UI can warn when the mailer isn't configured. Login of a pending account is rejected with `account_pending_approval` (revealed only on a correct password to avoid enumeration).
- **Online/offline.** A `before_request` hook in `app/__init__.py` stamps `users.last_seen = NOW()` once every ~30s for authenticated requests. The admin user listing derives `is_online = last_seen > NOW() - INTERVAL '2 minutes'`. Logout sets `last_seen` back 10 minutes so the user reads offline immediately.
- **Avatar uploads.** Stored inline in `users.avatar_url` as a base64 data URL (`data:image/png;base64,...`) — avoids needing S3 on Railway's ephemeral filesystem. The client resizes to 256×256 before POSTing; server validates the MIME and caps at 200 KB after decode. The data URL is intentionally **not** mirrored into the cookie session (a fat avatar blows past the ~4 KB cookie cap and silently drops the Set-Cookie). `current_user()` fetches it per request and caches via `flask.g`.

### Transactional mail (`app/mailer.py`)
- Two backends: Resend HTTPS API (preferred — works where outbound SMTP is firewalled like Railway), or `smtplib` fallback. `send_mail()` picks based on which env vars are set; if neither is configured it logs a warning and returns `False` without raising, so dev keeps working.
- `mailer_is_configured()` is surfaced via `/api/util/mailer-status` and powers the persistent "mailer not configured" banner in `/admin`.
- Four templates: `signup_pending_email`, `signup_approved_email`, `signup_rejected_email`, `password_reset_email`. Each takes a `theme="light"|"dark"` kwarg and renders the shell from `_palette(theme)` — keep both palettes in mind when adding a new template. Shared chrome is in helpers (`_wrap_html`, `_brand_header`, `_footer`, `_status_card`, `_cta_button`, `_info_card`).
- All recipient-facing copy is English-only inside emails (we drive theming, not language, per recipient).

### User preferences (theme + language)
- `users.preferred_theme` (`light`/`dark`) and `preferred_lang` (`ar`/`en`) are the source of truth for the user's display identity across the app **and** outbound emails. Seeded at signup from the body of `/api/auth/register` (the form sends the current screen's theme/lang) and rotated on every toggle while authenticated.
- `POST /api/auth/preferences` `{theme?, lang?}` persists from the client. Called by a debounced `_pushPreference()` in `common.js` whenever `setTheme()` / `setLang()` fires — anonymous toggles fail silently and stay local.
- `/api/auth/me` returns `preferred_theme`/`preferred_lang`. `_fetchCsrf()` adopts them into localStorage when this device is out of sync (cross-device).
- Email send sites (`forgot_password`, signup approve/reject) pass the user's `preferred_theme` to the mailer. `forgot_password` also appends `&theme=...&lang=...` to the reset URL so the page the link opens in matches the email — even on a fresh device with no localStorage.
- `base.html` head scripts honour `?theme=` and `?lang=` query params *before* first paint and persist them to localStorage. That's what makes email links open flash-free in the correct skin.

### Master V sync
`app/sync_service.py:start_sync_scheduler()` spawns a daemon thread that runs `run_sync()` 15s after boot then every 14 days. The job iterates `PLACES` (city → Master V id), pulls compounds, flattens unit details, and upserts into the `units` table. `sync_status` (module-level dict) backs `/api/sync/status`. **Always set `DISABLE_SYNC=true` for local development** — the sync hits a real API and takes minutes.

### Frontend
- All pages extend `app/templates/base.html`. The shell renders a fixed glass `<aside class="sidebar">` (desktop ≥1025px) and a slim `.topnav` above the main column. Below 1025px the sidebar becomes a drawer toggled by `#navBurger` via `toggleSidebar()` in `common.js`.
- The `.app-shell.has-sidebar` class on the body wrapper triggers the sidebar layout — only set when `user` is logged in. Auth pages don't have it.
- All copy is keyed via `data-i18n="namespace.key"`. Strings live in `app/static/js/i18n.js` (`I18N.ar`/`I18N.en`). `applyLang()` runs on `DOMContentLoaded` and on every language toggle. Pages can implement `function onLangChange(lang)` to re-render dynamic content. Both AR and EN keys must be added; falls back to AR then to the key itself.
- RTL is the default; `dir` and `lang` are set on `<html>` before the body renders to avoid Arabic flash.
- Charts: only `Plotly` plus the helpers in `app/static/js/charts.js` (`drawBarChart`, `drawHorizontalBar`, `drawDonut`, `drawLineChart`, `drawAreaChart`, `drawGauge`, `drawRadarChart`, `drawStackedBar`, `drawGroupedBar`, `drawHeatmap`, `drawTreemap`, `drawFunnel`, `drawScatter`, `drawComboBarLine`). Chart calls funnel through `_waitForPlotly()` so it's safe to call them before the Plotly CDN loads.
- Cache busting: `style.css`, `i18n.js`, and `common.js` are all requested with the same `?v=` token from `base.html` (currently `flat56`). Bump that token on every commit that touches CSS or those JS files so browsers refetch — `git log --oneline -- app/templates/base.html` shows the cadence.

### Design system
`app/static/css/style.css` is hand-written (no Tailwind/build). Conventions:
- Glass primitives: `.glass-card`, `.card`, `.chart-card`, `.form-section`, `.kpi-tile`, `.hero-ribbon`. They share `backdrop-filter: var(--blur)`, soft white-ish surface, periwinkle-tinted shadows.
- Layout helpers: `.bento` 12-col grid + `.span-3..span-12` (auto-stacks below 1100px); `.stats-grid` for KPI tile rows.
- Color tokens are CSS variables on `:root`: `--brand` (#474dc5 periwinkle), `--accent` (#006762 teal), `--secondary` (#884f41 coral), `--warning` (#c47200), `--danger` (#ba1a1a). Use the gradients (`--grad-brand`, `--grad-cool`, `--grad-warm`) for primary buttons and accent strips.
- Typography: Inter (LTR/numbers/headings) + IBM Plex Sans Arabic (RTL body). `--font-display` is Inter for KPI numerics and titles regardless of language.

When adding a new page, prefer wrapping its title in `.hero-ribbon` (a glass card with floating gradient blobs) and using the existing `.kpi-tile`/`.bento` primitives instead of inventing new ones.

## Things to know before editing

- **Editing KPI weights/targets** → only `app/kpi_logic.py`. Don't fork the formula into JS.
- **Adding a column to a table** → add it inside `init_all_tables()` with a `column_exists()` guard so it migrates on every deploy.
- **Adding a route** → put it in the matching blueprint, decorate with `@login_required` or `@role_required`, and for non-GET API routes also `@csrf_protect`. Return `error_response(error_code, status)` from `app.auth` so the frontend's `tError()` can localise it.
- **Adding UI strings** → add both `ar` and `en` entries in `i18n.js`. Don't hardcode Arabic in templates outside `data-i18n`.
- **Charts** → call helpers in `Charts.*` rather than `Plotly.newPlot` directly so theming, RTL fonts, and the load-wait helper stay consistent.
- **Sidebar nav** → edit `app/templates/base.html`. Each link checks `request.path` for the `active` class; icons use Material Symbols Outlined.
- **Master V API access** is gated by `MASTER_V_TOKEN`. The token in `config.py` is a development fallback; production sets it via env.
- **Date-range filtering** → range-aware endpoints (`/api/kpi/report`, `/api/kpi/summary`, `/api/kpi/team-leaders`, `/api/kpi/teams-summary`, `/api/finance/report`) accept `?from=YYYY-MM-DD&to=YYYY-MM-DD&preset=...` as well as the legacy `?month=YYYY-MM`. Parsing/validation lives in `app/util/date_range.py` (`parse_range(args, allow_sub_month=...)`). Front-end picker is `DateRange.mount(host, opts)` in `app/static/js/date_range.js` — one component, used everywhere. Soft cap: 10K rows → HTTP 413 `range_too_large`. Max range: `Config.MAX_RANGE_YEARS = 5` (overridable via env).

## Known issues / future work

These are tracked design debts, not bugs. Each lists how to enable / refactor when the time comes.

- **Daily activity log (option c, deferred from the date-range initiative).** `kpi_entries` is monthly-grain by schema (`UNIQUE(user_id, month)`). Sub-month presets in the date-range picker filter rows by `dataentry_submitted_at` / `sales_submitted_at`, but the activity counts inside each row remain monthly totals — surfaced via the `dr.footnote_submission` line on every range-aware page. If enterprise needs true per-day metric values (not just per-day filtering), add a sibling `kpi_activity_log(user_id, date, metric_key, value)` table, write to it from every `submit/sales` and `submit/evaluation` path, and rewrite report aggregation to roll up from the log. Estimated cost: 1–2 weeks.

- **TIMESTAMPTZ migration.** All `TIMESTAMP` columns are without time zone. Server reads `NOW()` in app-server local time (Africa/Cairo). Cutover plan when enterprise demands UTC discipline: (1) add TZ-aware columns alongside, (2) dual-write for one release cycle, (3) backfill existing values via `created_at AT TIME ZONE 'Africa/Cairo'`, (4) drop the old columns. Coordinate with any Railway TZ change. Frontend already converts via `Intl.DateTimeFormat` so display is locale-agnostic.

- **Audit trail (built but off by default).** `query_audit` table + `@audit_query` decorator on the 5 range-aware endpoints. Enable with `AUDIT_QUERIES=true` env (no redeploy needed). Recommended retention: 90 days via `DELETE FROM query_audit WHERE created_at < NOW() - INTERVAL '90 days'` on cron. For multi-worker deploys, the in-memory rate limiter (`app/auth.py:_RateLimiter`) and the synchronous audit insert may need a Redis/queue refactor — flagged as a follow-up.

- **Date-range pagination (deferred).** Range-aware endpoints currently return up to 10K rows then 413 with `range_too_large`. Marker comment `# TODO: paginate when consistently exceeding 5K rows` at each fetch site. Cursor-based pagination on `(month DESC, user_id ASC)` is the natural shape. Don't pre-build — measure first.

- **Date-range UI rollback.** The reliable path is `git`: rollback tag `pre-date-range-v1` or `git revert` of UI commits 8–12 (each commit isolated to one page; the backend BC contract means reverting the picker won't break the API). The `Config.DATE_RANGE_ENABLED` env knob exists for a future graceful kill-switch but is **not currently wired into the templates** — wiring it would require each page to fall back to a legacy `<input type="month">` host. Worth doing only if a need arises that revert can't address.
