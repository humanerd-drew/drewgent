---
title: Cloudflare Workers Local Development
name: cloudflare-workers-local-dev
description: Patterns, pitfalls, and workflows for local Cloudflare Workers development with D1, static assets, multi-source integration, and the m-log refactoring architecture.
domain: devops
tags: [devops, cloudflare, workers, d1, wrangler, local-dev]
created: 2026-06-11
updated: 2026-06-12
---

# Cloudflare Workers Local Development

Patterns and pitfalls for local Cloudflare Workers development with D1, static assets, and multi-source integrations.

## Core Principles

### Understand the system before touching code
- Map the complete dependency graph first: which files import what, what data flows where, what's dead code
- Check the ACTUAL running system (curl the endpoints, read the files) before asserting connections
- Look for pre-existing architecture documents (ARCHITECTURE.md, README.md) before refactoring
- Compare original/NAS versions with working copies to find divergence points

### Never reimplement what the external API already provides
- When an external API returns computed data, use it — don't recalculate
- Only compute what the external API doesn't provide
- Check the ACTUAL API response structure before coding against assumptions

## D1 Local Development

### SQLITE_BUSY error
Cause: Another `wrangler dev` instance holds a lock.
Fix (least destructive first):
1. Kill the duplicate `wrangler dev` / `workerd` process — identify via `lsof -i :<port>` then `kill <pid>`
2. If that doesn't work, kill ALL wrangler/workerd processes and restart one
3. Last resort: clear `~/.wrangler/state/v3/d1/` to reset the database

### Auto-migration for local dev
Add an `ensureTables()` function that auto-creates tables on first write using try/catch pattern with SELECT probe → CREATE TABLE IF NOT EXISTS.

**IMPORTANT:** `env.DB.exec()` does NOT support multi-line template strings or multiple statements in one call. Each SQL statement MUST be a single line in a separate `.exec()` call:
```javascript
// BROKEN — multi-line + multi-statement
await env.DB.exec("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS history (...);");

// WORKS — single line, single statement
await env.DB.exec("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL)");
await env.DB.exec("CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)");
```

### D1 error breaks auth chain
If `/api/auth/me` queries users table and D1 doesn't have it, the SPA shows blank screen. Wrap DB queries in try/catch.

## Frontend Integration

### Import path resolution
Absolute imports like `/app/shared/core/store.js` work via HTTP. CSS `@theme/` aliases don't — use relative paths.

### Dev login bypass
Create `/api/auth/dev-login` that works only when `IS_LOCAL_DEV=true`, creates session cookie, redirects to `/app/`.

### Email/password auth in Workers
PBKDF2 password hashing is available via the Web Crypto API (`crypto.subtle`) — no external packages needed.
- Use `crypto.subtle.importKey`, `deriveBits` with salt (16 bytes), 100000 iterations, SHA-256
- Store salt+hash as base64 string; verify by re-deriving with the same salt

## Frontend UX Patterns (m-log)

### Report card navigation
Report cards in the dashboard have different destinations depending on the report type:

- **Free inline report** (나의 Log 리포트): Scroll to the dashboard's inline section. The card should NOT navigate away from the dashboard — use `scrollIntoView` on the hub container:
  ```javascript
  navigateToAiReport() {
      const hub = document.getElementById('aiReportHub');
      if (hub) hub.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  ```
- **Paid standalone reports** (대세월운 종합, 연애/궁합): Navigate to the dedicated route. Each report view has its own input→payment→result flow built in:
  ```javascript
  navigateToLuckReport()   { window.location.hash = '#/report-luck'; }
  navigateToDatingReport() { window.location.hash = '#/report-dating'; }
  ```
- **New/enhanced reports** (욕망 & 기질): Navigate to the report page route. The report view handles input form, payment redirect, and result display within itself:
  ```javascript
  navigateToDesireReport() { window.location.hash = '#/report-desire'; }
  ```

**Principle:** Keep the free report as an inline section on the dashboard to minimize navigation friction. Route paid/enhanced reports to dedicated pages that each encapsulate the full input→payment→result lifecycle.

### History list display
History items (both saju and report) should always show:
- **Label** (user-given name or auto-title)
- **Birth info** (year.month.day) from `item.formData`
- **Analysis date** (formatted timestamp from `item.timestamp`)

**Pitfall: Nested formData causes "undefined" in display**
When saving history, ensure `formData.year/month/day` exist at the top level. Reports that save nested formData (e.g. `{ personA: {...}, personB: {...}, mode: 'analyze' }`) will render `undefined.undefined.undefined` in the history sidebar:
```javascript
// BROKEN - formData exists but has no top-level year
const reportBirthStr = item.formData 
    ? `${item.formData.year}.${item.formData.month}.${item.formData.day}`
    : '';

// FIXED - check for top-level year existence first
const reportBirthStr = (item.formData && item.formData.year)
    ? `${item.formData.year}.${item.formData.month}.${item.formData.day}`
    : '';
```
Always validate `formData.year` before rendering birth info strings.

For **nested formData** (e.g. dating reports with `personA`/`personB`), extract the birth info from the first person that has data:
```javascript
const reportPerson = item.formData?.personA?.year ? item.formData.personA 
    : (item.formData?.personB?.year ? item.formData.personB : null);
const reportBirthStr = reportPerson 
    ? `${reportPerson.year}.${reportPerson.month}.${reportPerson.day}` 
    : '';
```

When **restoring** a dating report from history in `init()`, explicitly restore `personA` and `personB` from `__RESTORE_FORM_DATA__`:
```javascript
const formData = JSON.parse(localStorage.getItem('__RESTORE_FORM_DATA__') || '{}');
if (formData.mode) this.state.activeTab = formData.mode;
if (formData.personA) this.state.personA = { ...this.state.personA, ...formData.personA };
if (formData.personB) this.state.personB = { ...this.state.personB, ...formData.personB };
```
Without this, clicking a history item followed by a CTA tab switch would show empty forms.

Report history items were missing birth info — add via `${item.formData.year}.${item.formData.month}.${item.formData.day}`.

### Free report CTA
The free report's premium CTA should navigate to the comprehensive report (`#/report-luck`) instead of showing a toast placeholder. Change `<button onclick="...toast...">` to `<a href="#/report-luck">`.

## SPA Hash Routing

### History restore from same route (force hashchange)
When restoring a report from history while already on the same hash route (e.g., already on `#/report-dating` and clicking another dating history item), setting `window.location.hash = '#/report-dating'` does NOT trigger a `hashchange` event because the hash value hasn't changed. The Router never re-runs, so the restore data saved to localStorage is never read.

**Fix:** Append `?t=${Date.now()}` to make each navigation hash unique:
```javascript
const hash = '#/report-dating?t=' + Date.now();
window.location.hash = hash;
```
The Router strips query params (`path.split('?')[0]`) so routing still works. This pattern applies to ALL report history restore navigations.

### Payment-flow report restore
For paid reports, the intended flow is:

1. User enters data, clicks analyze
2. **API call runs immediately** (before payment check)
3. Report result saved to `__RESTORE_REPORT__` + `__RESTORE_FORM_DATA__` in localStorage
4. Redirect to payment page
5. After payment, user returns to the report route
6. `init()` finds restore data, restores `reportData`, sets `isSimulatingAnalysis: true`
7. `mounted()` runs loading simulation (2.5s) with animated text
8. Loading ends, `isSimulatingAnalysis: false`, report content renders

Key implementation details:
- **Remove payment early-return** from `handleAnalyze()`. The check `if (activeTab === 'X' && !isPaidX)` must be AFTER the API call, not before.
- **Save restore data** in the redirect branch:
  ```javascript
  localStorage.setItem('__RESTORE_REPORT__', JSON.stringify(savedReport));
  localStorage.setItem('__RESTORE_FORM_DATA__', JSON.stringify({ ...formData }));
  localStorage.setItem('__RESTORE_REPORT_TYPE__', 'dating');
  ```
- **In `init()`**, after restoring report data, check payment and set `isSimulatingAnalysis: true`:
  ```javascript
  const purchased = JSON.parse(localStorage.getItem('__PURCHASED_REPORTS__') || '{}');
  if (purchased.dating_compatibility || purchased.dating_divorce) {
      this.state.isSimulatingAnalysis = true;
  }
  ```
- **In `mounted()`**, guard against re-entry with a `_simStarted` flag:
  ```javascript
  if (this.state.isSimulatingAnalysis && !this._simStarted) {
      this._simStarted = true;
      this.startLoadingSimulation();
      setTimeout(() => {
          this.stopLoadingSimulation();
          this._simStarted = false;
          this.setState({ isSimulatingAnalysis: false });
      }, 2500);
  }
  ```
- **In template**, check `isSimulatingAnalysis || loading` to show the loading animation.

**Pitfall:** If the loading simulation uses `setState` (e.g., text animation), `mounted()` is called again each time. Without `!_simStarted`, multiple simulations stack and the report never stabilizes.

### CTA Tab Switch preserves form data
When clicking a CTA inside a report result (e.g., "궁합 CTA to 갈등"), form elements are NOT in the DOM because the report template is active. `syncInputsFromDOM()` finds no `#meYear` and leaves state unchanged.

**Fix:** Save form data to state BEFORE the API call:
```javascript
const personA = getPersonFromDOM('me');
const personB = getPersonFromDOM('partner');
// ... validation ...
this.state.personA = personA;
this.state.personB = personB;
```
When the CTA switches tabs (`setState({ activeTab, reportData: null })`), the template reads the saved `personA`/`personB` and prefills correctly.

## Refactoring Workflow

### One-at-a-time principle
Make one change, test with curl, show before committing. Never commit without user review.

### Controller-based architecture
1. Create controllers with identical logic
2. Update worker.ts to dispatch to controllers
3. Keep cross-cutting concerns in the router layer

### Analysis engine layers (L0-L3)
L0=원국, L1=+대운, L2=+세운, L3=+월운. Each adds 2 characters. Δ between layers reveals the time period's theme.

### Blank-screen diagnosis protocol
When the SPA loads (200 OK for HTML/CSS/JS) but the screen is blank:

**Check the root URL first (fastest diagnosis):**
1. Curl `http://localhost:8787/`. If it returns 404 instead of a redirect, the root→SPA redirect is inside `if (assets)` which is dead code in local dev (env.ASSETS binding not available). Move the redirect outside the assets check.

**If the root redirect works but page is still blank (JS issue):**
2. Check browser console for JS errors (SyntaxError, reference errors)
3. **Check for stray backticks in template literals** — A common subtle bug is an extra backtick inside an HTML template literal like `">\`` at the end of an HTML tag line. This prematurely closes the template, leaving the rest parsed as raw JS. The error is typically `Missing } in template expression` at a line number that looks like HTML, not JS. 
   - **Diagnosis without a browser:** Run `node --input-type=module -e "try { await import('./path/to/file.js'); } catch(e) { console.log(e.message); }"`. If the error is "Missing } in template expression" → syntax error. If it's "Cannot find module '/app/...'" → that's expected (absolute path resolution on filesystem), the code is syntactically fine.
   - **Targeted check:** Run `node --check file.js` for CommonJS files, or use the `--input-type=module` approach above for ES modules.
4. Run `node --check <file>` on ALL imported modules — Wrangler/esbuild may not catch template literal syntax errors during dev
5. Verify DOM mount points exist in the served HTML: `id="app"`, `id="contentView"` etc.
6. Verify all module import paths resolve by curling each one
7. Wrap each init step in try/catch to isolate which step breaks

## CSS Layer Architecture (z-index)

### Standard layer stack (variables.css)
```
--z-negative: -1       (behind everything)
--z-base: 1            (default content)
--z-sticky: 100        (sticky headers)
--z-header: 500        (top nav bar)
--z-dropdown: 1000     (dropdown menus)
--z-overlay: 2000      (semi-transparent backdrops)
--z-fab: 2000          (floating action buttons)
--z-drawer: 3000       (sidebars, drawers)
--z-backdrop: 4000     (modal backdrops)
--z-modal: 5000        (modal dialogs)
--z-popover: 6000      (popovers)
--z-tooltip: 7000      (tooltips)
--z-toast: 9000        (toast notifications)
--z-max: 10000         (loading overlays)
```

### Key rules
- **NEVER use hardcoded z-index values** — always use `var(--z-*, fallback)` so the layer system is maintainable
- **Find hardcoded z-index values in CSS:** `grep -rn "z-index:" public/app/css/ --include='*.css' | grep -v "var("` — then replace each with the appropriate CSS variable
- **Find hardcoded z-index values in JS inline styles:** `grep -rn "z-index:" public/app/js/ --include='*.js' | grep -v "var(--z"` — these are harder to catch but also need fixing
- **var(--z-breakdown-sheet, 5000)** sits at modal level; define it in variables.css
- **`position: fixed` inside `transform` becomes relative to the transform container, not viewport.** This is a CSS spec behavior. A sidebar with `transform: translateX(-100%)` creates a new containing block for any `position: fixed` children inside it. Fix: append modals to `document.body` via JS.
- **Hardcoded z-index values common in inline styles** — find with `grep -rn "z-index:"` in JS files and replace with CSS variables

### Breakdown sheet desktop fix
The `.breakdown-sheet` uses `position: fixed; left: 0; right: 0;` which covers the full viewport. On desktop with a visible sidebar (280px wide), add:
```css
@media (min-width: 1024px) { body.is-desktop .breakdown-sheet { left: 280px; } }
```

## Modal Positioning

### transform container trap
When a modal's HTML is inside a container with `transform`, `position: fixed; inset: 0` constrains the modal to the container bounds, not the viewport.

**Fix:** Append modal elements to `document.body` in `mounted()`:
```javascript
const container = document.createElement('div');
container.innerHTML = this.renderLoginModal();
document.body.appendChild(container.firstElementChild);
```

### Event delegation with body-appended modals
Elements appended to `document.body` won't be caught by event delegation on a child container (`#app`). **Bind events directly** after appending:
```javascript
document.getElementById('myModalBtn')?.addEventListener('click', () => this.handler());
```

## Backend Route Alignment

### Check frontend API calls match registered routes
Search for all `fetch('/api/...')` calls in the frontend and verify each has a corresponding route in worker.ts. Common mismatches:
- `/api/report/generate` → must add route or change frontend to call `/api/report`
- `/api/report/free-log` → same pattern
- Missing routes cause 404 → JSON parse error on frontend

### Pattern: controller file exists but route is missing
This is a common failure mode. The controller file is already written (`src/controllers/something.ts`) but never imported or routed in `worker.ts`:

1. **Add the import** at the top of worker.ts:
   ```javascript
   import { handleMyController } from './src/controllers/my-controller';
   ```

2. **Add the route** inside the API try-block, before the catch:
   ```javascript
   if (url.pathname.startsWith('/api/my-path/') && request.method === 'POST') {
       return handleMyController(request, env, url);
   }
   ```

3. **Verify** with `curl -X POST http://localhost:8788/api/my-path/test -H "Content-Type: application/json" -d '{}'`. A 401 (Unauthorized) means the route is live — the auth check inside the handler is working. A 404 means the route is still missing.

### Cross-check: frontend URL in run-time API call vs worker.ts route pattern
If the frontend calls `/api/dating/${mode}` (with the mode from the `activeTab` state), the worker.ts route must match with `startsWith('/api/dating/')`. Any mismatch in prefix or HTTP method produces a silent 404 on the frontend, which manifests as a generic "리포트 생성 실패" error in the report view.

## `env.ASSETS` Binding vs `assets` Config

### The two systems
- **`wrangler.jsonc` `assets: { directory: "./public" }`** (v4+): Static files served at the workerd RUNTIME level. This does NOT create an `env.ASSETS` binding in local dev.
- **`env.ASSETS: Fetcher`**: A worker binding that lets the worker code programmatically fetch static files. Only available in PRODUCTION (Cloudflare edge) or when using the legacy `workers.dev` site config.

### What this means for your code
Any code inside `if (env.ASSETS)` is DEAD in local dev:
```javascript
const assets = env.ASSETS;
if (assets) {
    // This block NEVER runs in wrangler dev
    // Root redirect, SPA fallback, etc. all dead
}
```

### Fix: move root redirects OUTSIDE the assets check
```javascript
// ✓ WORKS everywhere — outside the assets block
if (url.pathname === '/' || url.pathname === '') {
    return Response.redirect(new URL('/app/', url.origin).toString(), 302);
}

// Only for programmatic asset access (production only)
const assets = env.ASSETS;
if (assets) {
    let assetRes = await assets.fetch(request);
    // SPA fallback...
}
```

### Production note
On deployed Cloudflare Workers, both systems coexist — the runtime serves `./public/` files directly, AND `env.ASSETS` is available as a `Fetcher` binding for programmatic access. So moving redirects outside `if (assets)` fixes local dev without breaking production.

## Assets Config and Root Index

### `public/index.html` is NOT auto-served at `/` in local dev
Even when a `public/index.html` exists, workerd in local dev mode does NOT serve it at the root URL. Requesting `/index.html` returns a 307 redirect to `/` which then falls to the worker → 404.

**Fix:** Always add an explicit `/` → `/app/` redirect in the worker (outside `if (assets)`), or configure proper `html_handling` in wrangler.jsonc.

## Pitfalls
- **Patch tool backtick escaping:** When `patch` adds JS template literals containing backticks, it encodes them as `\`` which is a syntax error. Verify with `node --check` after every patch that touches template literals.
- **Root redirect inside `if (assets)` is dead code in local dev** — always place root/SPA redirects before the assets check.
- **Template literal stray backtick:** A `\`` character at the end of an HTML tag line inside a template literal (like `">\``) terminates the template early, causing `Missing } in template expression`. This is NOT caught by bundlers — only by `node --input-type=module` or browser JS engine.
- **`node --input-type=module` for ES module syntax check** — `node --check` doesn't work for ES modules. Use the full `await import()` approach with try/catch instead.
- Overwriting `public/app/` loses custom modal work — re-apply
- SynologyDrive corrupts node_modules/.bin symlinks (XSym files)
- CSS `@theme/` alias doesn't resolve without build tool — use `../shared/theme/` instead
- localStorage→sessionStorage migration may break Router's cached-data checks (search ALL files for `localStorage.getItem('__SAJU_DATA__')`)
- Service worker caches old files — use Cmd+Shift+R or clear SW in DevTools
- `npm run build:frontend` fails (no vite config) — use `npm run dev` for static serving
