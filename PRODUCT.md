# Product

## Register

product

## Users

Three roles, three contexts. Bilingual throughout (Arabic RTL primary, English LTR secondary).

- **Sales reps — mobile-first, in the field.** Showing properties to clients, checking their KPI score between calls, glanceable lookups in PropFinder. Touch-driven, often outdoors or mid-conversation, attention is fragmented.
- **Managers & team leaders — mobile sometimes, desktop primary.** Reading the dashboard, ranking the team, scoring evaluations, spotting underperformers early. Long focused sessions on desktop; quick spot-checks on phone.
- **Data entry staff — desktop only.** Filling monthly evaluation forms, entering KPI numbers across the team. Form-heavy, keyboard-driven, accuracy matters more than speed.

Audience spans senior Arabic-speaking managers and younger bilingual sales reps. Both sides of the language switch must read fluently, not just translate literally.

## Product Purpose

A KPI and sales intelligence system for Ain Real Estate. Two jobs in one shell:

1. **Monthly KPI tracking and scoring** for the sales team. Sales record their own activity (Calls, Meetings, CRM, Deals, Reports, Reservations, Follow-up, Attendance); data entry scores soft attributes (Attitude, Presentation, Behaviour, Appearance, HR Roles). A weighted formula yields a single 0–100 score and a rating band (Excellent / V.Good / Good / Medium / Weak / Bad).
2. **PropFinder** — property browser synced from the Master V API every 14 days. Filters, detail view, used during client calls.

Success = the sales team performs better month-over-month. The interface earns its place by making a rep's score feel honest and actionable, a manager's evaluation feel grounded in evidence, and the daily work flow without friction.

## Brand Personality

Confident, professional, calm.

The tool is the quiet authority in the room. Numbers and rankings speak for themselves; the UI doesn't editorialize. A rep who scores 47/100 sees a clear, unambiguous signal — not punished by red gradients, not coddled by encouraging illustrations. A manager evaluating their team feels they're looking at evidence, not a pitch deck.

Voice in copy: factual, direct, never patronizing. In Arabic, formal-modern (فصحى مبسطة), not colloquial street language. In English, plain and short — no marketing adjectives, no "Awesome!" or "🎉".

## Anti-references

This interface should NOT look like:

- **Generic SaaS dashboards** — gradient hero-metric tiles, emoji-icon cards, "premium glassmorphism" canvases with floating blobs, lavender-on-pastel everything. This is the direction the current implementation has drifted toward; future work pulls back from it.
- **Heavy CRM tools** — Salesforce, Zoho, HubSpot. Cluttered toolbars, ribbon nav, generic enterprise-blue, mid-2000s information density. We share a workflow with them but not an aesthetic.
- **Gamified performance apps** — Duolingo streaks, leaderboard fanfare, animated rank changes, badges, confetti on submit. KPI scores affect real compensation; the UI stays sober.

## Design Principles

1. **Numbers over decoration.** A KPI score is a fact, not a feature to celebrate. Strip ornament until the data carries the screen on its own.
2. **Trust through legibility.** If livelihoods are graded by this number, the number must be unambiguous: a solid color, generous size, never gradient-clipped, never sitting on a busy background. PropFinder unit prices follow the same rule.
3. **Bilingual is a first-class constraint, not a polish task.** RTL is the default test case, not the afterthought. Layouts, icons, motion, and form flows must mirror correctly. Numerals stay LTR within RTL text. The language toggle never reflows the page jarringly.
4. **Mobile first for consumption, desktop first for input.** Sales and managers read on phone; data entry writes on laptop. Don't compromise either flow for the other — different surfaces can have different densities.
5. **Silence over hype.** No `hyper-pop`, no `hyper-btn`, no auto-shimmer. Motion appears only when it conveys state change (load, save, navigate). Color appears only when it carries meaning (rating band, status, error).
6. **Aim at Linear / Stripe / Notion craft, not at our category.** Reference product-craft benchmarks, not real-estate-CRM peers. Tight typography, deliberate spacing, restrained color, strong hierarchy.

## Accessibility & Inclusion

- Target **WCAG 2.1 AA**, striving toward AAA where stakes are high (KPI score readability on a phone in midday sun is the canonical test).
- Bilingual RTL/LTR done right: every screen tested in both directions; mirrored chevrons and progress indicators; numerals LTR inside RTL paragraphs; language toggle preserves scroll position and form state.
- Full keyboard navigation; visible focus indicators on every interactive element; logical tab order in both directions.
- Screen-reader friendly: semantic HTML, proper labels on every KPI input, decorative icons hidden from AT, role/state announced on dynamic regions (charts, toasts, modals).
- Color is never the sole carrier of meaning. Rating bands carry text + position + (optional) color, never color alone.
- Respect `prefers-reduced-motion`: disable card pops, blob drift, hyper effects, and any decorative motion. Functional motion (loading spinner, focus shift) stays.
- Touch targets ≥ 44×44 px on mobile surfaces.
