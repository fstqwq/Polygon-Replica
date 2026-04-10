# Frontend Refactoring Plan

This document is the merged plan for the current `UI` workspace.

It combines:
- the local code-validated refactoring plan for the current frontend structure
- the newer color-direction update pulled from `yonghao`

It is intentionally based on the code that exists now:
- CSS lives in `app/static/style.css`
- frontend behavior lives in `app/static/ui.js`
- templates live in `app/template/` (not `app/templates/`)
- reusable template partials already exist, for example `app/template/_component_forms.html`

The goal is to improve maintainability without mixing too many kinds of change in one pass, while still allowing an intentional visual refresh for the neutral palette.

---

## Validated Current Snapshot

These points were verified against the current workspace before updating this plan.

### Frontend file layout

- CSS is still a single large file: `app/static/style.css`
- JavaScript is still a single large file: `app/static/ui.js`
- templates are server-rendered Jinja files under `app/template/`

### CSS facts already confirmed

- `:root` already contains the main token skeleton for layout, surface, text, link, button, and tag colors
- `style.css` still mixes tokenized values and hardcoded literals
- `!important` is concentrated in a few known areas:
  - `[hidden]` and related hidden states
  - tag-select variants
  - one inline-form layout rule
  - dirty / failed row highlighting
- wide run-details layout currently depends on `:has(...)` selectors
- the current neutral palette is visually weak in key places:
  - topbar `#e5e7eb` is too close to page background `#f3f4f6`
  - default and primary buttons are both dark and are not visually well separated
  - the gray family is still a mix of Gray and Slate values

### Template facts already confirmed

- popup dialog markup is duplicated across six templates with 12 overlay blocks total
- change-badge markup is duplicated in two places:
  - `app/template/base.html`
  - `app/template/workspace.html`
- truncation and preview notes are repeated in several templates, but they are not all the same shape
- `run_details.html` contains repeated inline style attributes for compile diagnostics wrapping

### JavaScript facts already confirmed

- password-proof preparation exists in four submit handlers, not three:
  - `initLoginProofForm()`
  - `initRegisterLikeProofForm()`
  - `initSettingsPasswordProofForm()`
  - `initSudoProofForm()`
- those password-proof forms currently guard only on `passwordPrepared === "1"`; they do not block the in-flight async window
- CodeMirror draft listener duplication is already guarded via `dataset.statementDraftCodeMirrorBound`; this should **not** be listed as an open bug
- settings judgehost table filtering currently runs on every keystroke with no debounce
- `tests.html` still contains an inline `syncSampleForm()` script that belongs in `ui.js`

### Asset versioning facts already confirmed

- `style.css` and `ui.js` already use a version query string in base templates
- CodeMirror vendor assets and `editor_init.js` do **not** consistently use cache-busting query strings yet

---

## Refactoring Principles

1. **Keep code-validated facts ahead of abstract cleanup.**
   The plan should reflect the code that exists now, not an assumed frontend shape.

2. **Allow one intentional visual palette change, then go back to mechanical cleanup.**
   The color refresh is the exception; later CSS cleanup should stay largely mechanical.

3. **Do not combine token cleanup with file splitting.**
   Keep behavior-preserving cleanup separate from file reorganization.

4. **Fix proven issues before chasing ideal abstractions.**
   Example: add a pending submit guard to password-proof flows before extracting shared helpers.

5. **Use existing template patterns.**
   New macros should follow the current partial naming style such as `_component_forms.html`.

6. **Keep validation page-driven.**
   Every phase should name the affected pages and interactions to re-check.

---

## Phase 0: Establish a Safe Baseline

**Goal:** measure the current frontend before making structural edits.

**Why first:** several later phases are intended to be mechanical, while the color refresh is intentionally visual. Baseline counts and screenshots make regressions easier to catch.

### Work

- record current counts for:
  - hardcoded hex values in `app/static/style.css`
  - `!important` usage in `app/static/style.css`
  - popup overlay occurrences in `app/template/*.html`
  - inline `style=` occurrences in templates
- capture representative screenshots for:
  - login / register / setup / sudo
  - statement preview
  - tests editor
  - solutions list and editor
  - run page and run details page
  - settings page
  - workspace page
  - root problems / root contests

### Deliverable

A short baseline note or checklist committed alongside the refactor branch, not a long-lived product doc.

---

## Phase 1: Refresh the Neutral Color Scheme

**Goal:** improve hierarchy and visual identity before the mechanical token cleanup pass.

**Risk:** medium because this is a deliberate UI change across many pages.

### Why this phase exists

The newer `yonghao` update correctly identifies that the current neutral palette is too flat:
- topbar and page background are too close in value
- default and primary buttons are both dark, so the action hierarchy is weak
- neutral values mix multiple Tailwind gray families without a clear direction

This phase should introduce a stronger neutral palette and a clear topbar anchor, while leaving semantic success / warning / danger status colors largely intact.

### Proposed palette direction

Use a dark Slate-based topbar and standardize the neutral family around Slate.

Recommended token targets:

```css
:root {
  --bg: #f1f5f9;
  --bg-topbar: #1e293b;
  --bg-topbar-hover: #334155;
  --surface: #ffffff;
  --surface-alt: #f8fafc;
  --surface-subtle: #f8fafc;
  --surface-hover: #eef2f7;

  --line: #cbd5e1;
  --line-strong: #94a3b8;

  --text: #0f172a;
  --text-secondary: #334155;
  --muted: #64748b;

  --link: #2563eb;
  --focus-ring: #93c5fd;

  --btn-primary-bg: #0f172a;
  --btn-primary-bg-hover: #1e293b;
  --btn-primary-border: #0f172a;
  --btn-primary-text: #ffffff;

  --btn-bg: #ffffff;
  --btn-bg-hover: #f1f5f9;
  --btn-border: #94a3b8;
  --btn-text: #0f172a;
}
```

### Scope

This phase should focus on the places where the palette shift is load-bearing:
- topbar
- brand / tagline / top action text
- main menu hover / active treatment
- default vs primary button separation
- neutral page surfaces and borders

### Specific implementation direction

#### 1. Topbar becomes the anchor band

Move the topbar to `--bg-topbar` and ensure text inside it is designed for dark background use.

Review these areas together rather than piecemeal:
- `.topbar`
- brand/logo text
- tagline text
- user/session text
- top action links

#### 2. Main menu becomes dark-surface aware

Current active and hover states should be retuned for the dark topbar:
- inactive links should be lower-contrast white
- hover states should use `--bg-topbar-hover`
- active state can stay light if it clearly reads as selected against the dark bar

#### 3. Buttons gain visible hierarchy

The remote update is correct that default and primary buttons are currently too similar.

Target distinction:
- primary buttons: dark filled
- default buttons: white or light neutral with border
- ghost/link-like buttons: remain visually lighter

#### 4. Keep submenu and content area light

Do **not** push the dark treatment into the whole app shell. The problem submenu and content panels should remain light so the topbar does the anchoring work without making the workspace feel heavy.

#### 5. Keep semantic status colors mostly stable

Do not redesign success/warning/error semantics in this phase. Reuse the existing status family unless a contrast problem is found during review.

### Gray-family direction

This merged plan adopts a deliberate neutral-family choice:
- standardize the neutral palette on **Slate**
- remove Gray-vs-Slate drift during follow-up token cleanup

This is acceptable here because the visual change is intentional and happens before the mechanical token pass.

### Validation

- screenshot every major page type before and after
- verify white text on `--bg-topbar` remains comfortably readable
- verify links remain legible on white surfaces
- verify muted text still passes practical readability on white cards
- verify flash messages inside the topbar area remain readable
- verify submenu warn/danger indicators still stand out

### Exit criteria

- the topbar clearly separates navigation from page content
- default and primary buttons are visually distinct
- the neutral family has a clear direction for later tokenization

---

## Phase 2: Finish the Color Token Pass

**Goal:** reduce hardcoded color usage after the palette direction is chosen.

**Risk:** low if kept mechanical.

### Scope

Work only in `app/static/style.css`.

### What to do

- add missing token names for repeated literals that are still clearly reused
- replace repeated bare hex values with `var(...)` references
- align remaining neutral literals to the chosen Slate-based palette from Phase 1
- keep semantic status families separate instead of collapsing them into one bucket

### Specifically validated candidates

This pass should be based on the actual remaining literals in the local file, including areas like:
- topbar background and hover states
- zebra table row backgrounds
- PDF preview background
- any remaining status backgrounds or borders

### What not to do

- do **not** reorganize selectors yet
- do **not** split the CSS file yet
- do **not** turn every one-off literal into a fake shared token if reuse is not real

### Exit criteria

- repeated literals are tokenized
- neutral colors consistently follow the chosen palette direction
- only true one-off literals remain where keeping a literal is clearer than inventing a fake shared token

---

## Phase 3: Clean Up CSS Cascade Hotspots

**Goal:** reduce the fragile parts of the stylesheet without broad visual churn.

**Risk:** low to medium.

### 3.1 Remove unnecessary `!important`

Target the removable cases first:
- tag-select state classes
- `.form-row-inline-controls > label`
- `.table-row-dirty td` / related row highlighting if a more specific selector is sufficient

Keep defensive hidden-state rules when they are semantically correct:
- `[hidden]`
- overlay hidden selectors
- similar explicit hidden-state rules

Those are not the same category as accidental specificity fights and should not be removed just to hit zero.

### 3.2 Replace `:has(...)`-driven run-details layout switching

Current wide-layout behavior is tied to selectors like the compact run-details block inside `.page-grid.page-grid-wide`.

Replace this with an explicit template-driven class on `<body>` or another stable layout root. That keeps the layout decision in the page template instead of the cascade.

Recommended direction:
- add a compact run-details body/layout class in `app/template/run_details.html`
- switch the layout rules in `app/static/style.css` to target that class directly

### 3.3 Fold repeated inline diagnostics styles into CSS

Move the repeated wrapping styles from `app/template/run_details.html` into `.compile-diagnostics-error` in `app/static/style.css`.

### Exit criteria

- removable `!important` usage is gone
- defensive hidden-state rules remain only where intentional
- no `:has(...)` dependency remains for run-details layout
- `run_details.html` no longer carries repeated wrapping inline styles

---

## Phase 4: Extract Repeated Template Structures

**Goal:** reduce duplicated Jinja markup while staying close to current template conventions.

**Risk:** low.

### 4.1 Popup dialog macro

Create a shared partial in the existing style, for example:
- `app/template/_component_dialogs.html`

Use it for the repeated overlay/dialog structure currently duplicated in:
- `tests.html`
- `preview.html`
- `root_problems.html`
- `root_contests.html`
- `settings.html`
- `run_details.html`

The macro should handle only the shared shell:
- overlay container
- dialog container
- title hookup
- close link
- caller content

Do not over-generalize form bodies into the same macro.

### 4.2 Change-badge macro

Extract the duplicated change-badge block shared by:
- `app/template/base.html`
- `app/template/workspace.html`

This is a strong candidate because the duplicated markup is effectively identical.

### 4.3 Truncation-note macro only where repetition is genuinely uniform

The current codebase has several variants:
- first N characters
- first N bytes
- first shown of total items
- first available subset only

Do this only if a small macro clearly improves readability. Otherwise leave the more specialized messages in place.

### Exit criteria

- popup shell duplication is removed
- change-badge duplication is removed
- any extracted truncation helper is narrow and clearly justified

---

## Phase 5: Fix the Real JavaScript Issues First

**Goal:** improve correctness before doing bigger JS deduplication.

**Risk:** medium.

### 5.1 Add an in-flight submit guard to password-proof forms

This is the most concrete JS bug in the current code.

All four proof flows should reject repeat submits while async crypto is running:
- login
- register/setup-style forms
- settings password change
- sudo

Recommended behavior:
- if `passwordPrepared` is `"1"` or `"pending"`, return immediately
- set `passwordPrepared = "pending"` before async work begins
- reset or clear the pending state on failure paths
- set `passwordPrepared = "1"` only immediately before the final programmatic submit

### 5.2 Move tests sample toggle wiring into `ui.js`

The inline `syncSampleForm()` block in `app/template/tests.html` should move into `app/static/ui.js` and be initialized from the central ready hook.

This is a clear win because it removes page-local behavior from the template without needing a new abstraction layer.

### 5.3 Debounce judgehost table filtering

Add a small debounce around `initSettingsJudgehostTableFilter()` input handling.

A short delay such as ~150ms is enough. Keep the filtering logic itself unchanged.

### 5.4 Only then consider proof-helper extraction

A shared proof helper may still be worthwhile, but it should happen **after** the pending-state fix is in place and only if the resulting helper stays readable.

The helper should account for the fact that the four flows are similar but not identical:
- login fetches metadata remotely
- register/setup computes verifier fields directly
- settings handles both current and next password material
- sudo is simpler than settings but not identical to login

### What to remove from the older draft

Do **not** keep “fix CodeMirror duplicate draft listeners” as an active task. The current code already guards that path.

### Exit criteria

- all proof forms are safe against double-submit during async preparation
- `tests.html` no longer needs the inline sample toggle script
- judgehost table filtering remains responsive under rapid typing

---

## Phase 6: Consolidate Repeated Component Patterns

**Goal:** standardize component families once tokens and major duplication are under control.

**Risk:** medium.

### 6.1 Status/tone consolidation

Unify repeated status color usage across:
- flash notifications
- verification tones
- run verdict styling
- lifecycle step styling

This phase should reuse semantic tokens rather than collapsing everything into one selector family.

### 6.2 Button tier cleanup

Inventory current button-like classes and converge them into a small set of stable tiers:
- primary
- default
- ghost / link-like

Do this carefully because button classes are used across workspace, settings, tests, and root pages.

### 6.3 Table base patterns

Introduce a shared table base only after verifying which tables truly share:
- structure
- zebra striping behavior
- header treatment
- cell alignment assumptions

Do not force every table onto one abstraction if it creates more overrides than it removes.

### Exit criteria

- shared component families are visibly more consistent
- pages do not need selector-specific hacks to restore previous behavior

---

## Phase 7: Normalize Spacing and Typography

**Goal:** reduce arbitrary spacing and type scales after component patterns are stable.

**Risk:** medium because this phase can create broad visual drift.

### Why later

Spacing cleanup is safer after:
- the color refresh is settled
- colors are tokenized
- cascade hotspots are fixed
- repeated component structures are consolidated

Otherwise spacing cleanup fights moving targets.

### Scope

- introduce a compact spacing scale in `:root`
- normalize the most repeated padding and gap values first
- define a small font-size scale only where repeated values are already serving the same semantic role

### Guardrails

- avoid “nearest token” replacements that visibly alter alignment in forms or tables
- treat 1px differences as intentional until proven otherwise on real pages
- keep desktop workspace constraints intact; this is not a mobile redesign phase

### Exit criteria

- the common spacing values come from tokens
- no obvious alignment regressions appear on forms, tables, popups, or top navigation

---

## Phase 8: Split the CSS File Last

**Goal:** make the stylesheet maintainable after semantic cleanup has settled.

**Risk:** low if done after previous phases.

### Why last

Splitting `style.css` too early makes every earlier phase harder to review. File organization should reflect the cleaned-up structure, not the pre-refactor mess.

### Recommended module shape

Keep the entry file small and split by responsibility, for example:
- variables
- reset/base
- layout
- navigation
- forms
- buttons
- tables
- popups
- workspace
- verification/run details
- tests
- solutions
- settings
- auth
- utilities

Exact filenames can follow the final selector distribution after Phases 1-7.

### Asset loading note

If CSS is split, preserve cascade order intentionally. Do not rely on accidental import order.

### Exit criteria

- `style.css` becomes an entry point or clear top-level bundle file
- selector order remains deliberate and reproducible

---

## Cross-Cutting Cleanup: Static Asset Versioning

This can be done as a small standalone change once template edits are already in progress.

### Work

Add the same version-query strategy already used for `style.css` and `ui.js` to:
- `editor_init.js`
- CodeMirror CSS
- CodeMirror JS vendor files

### Why separate

This is operational hygiene, not structural frontend cleanup. It should not block the main refactor sequence.

---

## Suggested Execution Order

1. Phase 0 baseline
2. Phase 1 color scheme refresh
3. Phase 2 color token pass
4. Phase 3 cascade cleanup
5. Phase 4 template extraction
6. Phase 5 JS correctness fixes
7. Phase 6 component consolidation
8. Phase 7 spacing and typography normalization
9. Phase 8 CSS file split
10. static asset versioning as a small opportunistic follow-up

### Parallelizable work

Safe to run in parallel if different people are involved:
- Phase 4 template extraction
- Phase 5 JS correctness fixes
- static asset versioning

Everything that heavily edits `style.css` should stay serialized.

---

## Validation Checklist by Area

### Auth flows

- login
- register
- setup
- sudo
- settings password change

### Problem workspace flows

- statement preview page
- tests page popup flows and sample toggle behavior
- solutions list and editor
- run start page
- run details regular and compact layouts
- workspace page change badges and destructive actions
- settings judgehost filtering and popup generation

### Root pages

- root problems popup flows
- root contests popup flows

### Presentation checks

- topbar and submenu colors
- button hierarchy and contrast
- table zebra striping and dirty/failed row emphasis
- tag-select status appearance
- popup open/close styling
- compile diagnostics wrapping
- CodeMirror asset loading after cache busting

---

## Explicit Non-Goals

These are not goals of this plan:
- SPA migration
- Tailwind / Bootstrap / framework migration
- mobile-first redesign
- backend route naming cleanup
- broad accessibility audit
- rewriting the server-rendered frontend architecture
- forcing all template repetition into macros when local inline markup is clearer
