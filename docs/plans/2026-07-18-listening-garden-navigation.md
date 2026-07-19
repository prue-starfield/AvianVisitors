# Listening Garden Navigation Redesign Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the giant anchor-based archive page with trustworthy detection and species destinations, universal bird links, URL-backed exploration, and task-led navigation.

**Architecture:** Keep the loopback-only Python server and read-only SQLite boundary. Add allow-listed singular JSON endpoints and client routes under `/birds/`; serve one progressive-enhancement shell with route-specific sections and load only the data required by the current route. Preserve immutable detection IDs as evidence identity and scientific-name slugs as validated presentation routes.

**Tech Stack:** Python standard-library HTTP server, read-only SQLite, vanilla HTML/CSS/JavaScript, pytest, Node for pure router tests, Chrome DevTools for live browser verification.

---

### Task 1: Add safe singular API and route serving

**Objective:** Provide canonical detection/species data endpoints and serve the application shell safely for `/birds/*` routes.

**Files:**
- Modify: `tests/test_archive_site.py`
- Modify: `avian/archive/site/server.py`

**Steps:**
1. Write failing tests for `GET /api/detections/<64hex>`, invalid/missing IDs, `GET /api/species/<slug>`, invalid/missing slugs, no coordinates/filesystem paths, and `/birds/detection/...` shell fallback.
2. Run focused pytest and verify expected 404/route failures.
3. Implement allow-listed path parsing, singular response helpers, review/date summaries, and optional `/birds/` prefix normalisation without changing the read-only connection.
4. Run focused tests and full archive-site tests.

### Task 2: Define and test canonical client routing

**Objective:** Make route parsing and href generation deterministic across public `/birds/` and direct local paths.

**Files:**
- Create: `avian/archive/site/static/routes.js`
- Modify: `tests/test_archive_site.py`
- Modify: `avian/archive/site/server.py`
- Modify: `avian/archive/site/static/index.html`

**Steps:**
1. Write a failing Node-backed pytest for Today, Explore, Species index, species detail, detection detail, About, invalid route, and safe canonical href generation.
2. Verify RED.
3. Implement a small UMD-style pure routing module and allow-list it as a static asset.
4. Add `<base href="/birds/">` and load routes before the app.
5. Verify GREEN and full tests.

### Task 3: Build task-led route views and universal link contract

**Objective:** Make every bird representation link to species history and every recording link to a dedicated evidence page.

**Files:**
- Modify: `tests/test_archive_site.py`
- Modify: `avian/archive/site/static/index.html`
- Modify: `avian/archive/site/static/app.js`

**Steps:**
1. Write failing static/behaviour contract tests requiring route views, real anchors for Today/species/ledger rows, dedicated detection rendering, and no hash-based evidence route.
2. Verify RED.
3. Split the shell into Today, Explore, Species, Species Detail, Detection Detail, and About views.
4. Route-specific initialisation: load only common status plus current-view data.
5. Render species detail with identity, first/last heard, recognition/day counts, review summary, and chronological occurrence links.
6. Render detection detail audio-first with breadcrumb, exact preserved audio, BirdNET/Perch evidence, full digest, species link, and previous/next same-species links where available.
7. Verify GREEN and full tests.

### Task 4: Add URL-backed exploration and browser history

**Objective:** Preserve search, species, date, confidence, review and page state in shareable URLs and restore them with Back/Forward.

**Files:**
- Modify: `tests/test_archive_site.py`
- Modify: `avian/archive/site/static/app.js`

**Steps:**
1. Write failing route/state tests for query parse/serialise and safe page bounds.
2. Verify RED.
3. Initialise Explore filters from `location.search`; submit with `history.pushState`; restore on `popstate`; keep load-more/page state in the URL.
4. Ensure close/back links are ordinary navigable links, not `replaceState` hacks.
5. Verify GREEN and full tests.

### Task 5: Refine responsive field-journal UX

**Objective:** Preserve the distinctive Listening Garden aesthetic while making routes legible, mobile-first and obviously interactive.

**Files:**
- Modify: `avian/archive/site/static/styles.css`
- Modify: `avian/archive/site/static/index.html`

**Steps:**
1. Add route-aware header/nav, mobile bottom navigation, breadcrumbs, visible link/hover/focus states, audio-first detection composition, species occurrence cards, sticky Explore filters, and reduced-motion support.
2. Verify at 500×603 and desktop widths with screenshots and accessibility snapshots.
3. Run Lighthouse accessibility and confirm no console errors.

### Task 6: Integration, security review, deploy and prove

**Objective:** Ship only after exact alert, species exploration, and browser-history flows work on the live tailnet site.

**Steps:**
1. Run Ruff, py_compile, full pytest suite, and `git diff --check`.
2. Independent fail-closed review for path traversal, SQL injection, DOM XSS, unsafe route parsing, private-path/coordinate leakage, broken evidence links, and history traps.
3. Restart `com.prue.birdarchive`; verify `/health` and canonical/mirror integrity.
4. Live mobile and desktop proof:
   - Discord-style detection URL shows name/audio/review in first viewport without scrolling.
   - Any Today/species/ledger bird link reaches its species occurrences.
   - Occurrence reaches exact evidence.
   - Back restores species or Explore filters.
5. Commit `[verified] Redesign Listening Garden navigation` and push.
