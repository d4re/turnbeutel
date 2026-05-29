# Course Filters Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Courses view's two calendar date inputs with a one-week horizontal day-chip strip, and replace the three Morning/Afternoon/Evening checkboxes with a dual-handle time-of-day range slider.

**Architecture:** Pure frontend change in `frontend/` (vanilla ES-module `app.js`, `index.html`, `style.css`). The date strip and time slider write to module-level state that the existing `fetchCourses()` / `applyCourseFilters()` already drive, so the fetch/cache/render pipeline is untouched. The time slider reuses the existing dual-`input[type=range]` "Tier Range" slider pattern.

**Tech Stack:** Vanilla JavaScript (ES modules), HTML, CSS, Leaflet. ESLint (`@eslint/js` recommended) is the only frontend tooling — there is **no frontend unit-test runner**, so each task is verified by `npx eslint .` plus manual browser checks via `make serve` (app at http://localhost:8000). Backend is unchanged.

**Two deliberate simplifications vs. the approved mockups (call out at review):**
1. The time slider shows **sparse tick labels (Any · 8 · 12 · 16 · 20 · 24) plus a live description line**, matching the existing Tier Range slider idiom, instead of floating per-handle tooltips. Exact handle values are shown live in the description line.
2. The left **"Any"** stop is rendered as a tinted grey zone + divider at the 08:00 mark; the native range thumb still moves linearly across the value domain (no physical gap between "Any" and 08:00). Functionally identical to the mockup.

---

## File Structure

- `frontend/index.html` — swap the `#courses-filters` date-picker block for a day-strip container + legend; swap the `#time-toggle` block for the range-slider markup. Responsibility: static structure only.
- `frontend/style.css` — add day-strip + chip-state + legend rules; add time-slider "Any"-zone, divider, and tick-label rules (reusing the existing `.slider-*` track/fill/thumb rules). Responsibility: presentation.
- `frontend/app.js` — date-strip rendering & selection state machine; time-slider init/anti-cross/fill/description/bounds; rewire `initCoursesView`, `fetchCourses`, `applyCourseFilters`; delete `getSelectedTimeSlots`/`courseTimeSlot`. Responsibility: behavior + state.

State added to `app.js` (module-level, near the other Courses view state at lines 27-33):
- `let courseStartDate = null;` — selected range start (ISO `YYYY-MM-DD`).
- `let courseEndDate = null;` — selected range end (ISO `YYYY-MM-DD`).

Constants for the date strip and time slider are defined in the tasks below.

---

## Task 1: Date-strip markup + styles (no behavior yet)

**Files:**
- Modify: `frontend/index.html:81-89` (the date `filter-group`)
- Modify: `frontend/style.css` (append new rules)

- [ ] **Step 1: Replace the date-picker markup**

In `frontend/index.html`, replace lines 81-89 — currently:

```html
            <div id="courses-filters" style="display:none">
                <div class="filter-group">
                    <label class="filter-label">Date Range</label>
                    <div class="date-picker-row">
                        <input type="date" id="course-date-start">
                        <span>to</span>
                        <input type="date" id="course-date-end">
                    </div>
                </div>
```

with:

```html
            <div id="courses-filters" style="display:none">
                <div class="filter-group">
                    <label class="filter-label">Date</label>
                    <div id="date-strip" class="date-strip"></div>
                    <div class="date-legend">
                        <span class="legend-item"><i class="legend-swatch today"></i>Today</span>
                        <span class="legend-item"><i class="legend-swatch selected"></i>Selected</span>
                        <span class="legend-item"><i class="legend-swatch inrange"></i>In range</span>
                    </div>
                </div>
```

- [ ] **Step 2: Add date-strip CSS**

Append to `frontend/style.css`:

```css
/* Date strip (Courses view) */
.date-strip {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 4px;
    /* fade the right edge to hint at more dates */
    -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 24px), transparent);
    mask-image: linear-gradient(to right, #000 calc(100% - 24px), transparent);
}

.date-chip {
    flex: 0 0 40px;
    box-sizing: border-box;
    text-align: center;
    padding: 7px 0;
    border: 1px solid #ccc;
    border-radius: 8px;
    background: #fff;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
}

.date-chip .dow {
    display: block;
    font-size: 10px;
    line-height: 1.4;
    color: #999;
    text-transform: uppercase;
}

.date-chip .num {
    display: block;
    font-size: 14px;
    line-height: 1.2;
    font-weight: 600;
    color: #1a1a2e;
}

.date-chip.today {
    background: #fdf1dc;
    border-color: #f0cd92;
}
.date-chip.today .dow { color: #d98e04; }

.date-chip.inrange {
    background: #dbe8f7;
    border-color: #a9cbf0;
}
.date-chip.inrange .num { color: #2471a3; }

.date-chip.selected {
    background: #1a1a2e;
    border-color: #1a1a2e;
}
.date-chip.selected .num,
.date-chip.selected .dow { color: #fff; }

/* today + selected: navy fill keeps an amber ring so "today" stays flagged */
.date-chip.selected.today {
    box-shadow: 0 0 0 2px #d98e04;
}

.date-legend {
    display: flex;
    gap: 14px;
    margin-top: 10px;
    font-size: 11px;
    color: #777;
}
.legend-item { white-space: nowrap; }
.legend-swatch {
    display: inline-block;
    width: 11px;
    height: 11px;
    border-radius: 3px;
    vertical-align: -1px;
    margin-right: 4px;
}
.legend-swatch.today { background: #fdf1dc; border: 1px solid #f0cd92; }
.legend-swatch.selected { background: #1a1a2e; }
.legend-swatch.inrange { background: #dbe8f7; border: 1px solid #a9cbf0; }
```

> Note: the old `.date-picker-row` rule (if any) in `style.css` can stay — it's now unused and harmless. Do not spend time hunting it down unless ESLint/CSS tooling flags it (it won't; there is no CSS linter here).

- [ ] **Step 3: Lint**

Run: `cd frontend && npx eslint .`
Expected: PASS (no JS changed; lints clean as before).

- [ ] **Step 4: Manual check**

Run `make serve` (from repo root), open http://localhost:8000, switch to the **Courses** tab. Expected: the "Date" label and an (empty) strip area + the 3-item legend render where the date pickers were. No console errors. (The strip is empty until Task 2 — that's expected.)

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/style.css
git commit -m "feat(frontend): date-strip markup and styles for courses filter"
```

---

## Task 2: Date-strip rendering + selection behavior

**Files:**
- Modify: `frontend/app.js` — add state (after line 32), add helpers + render/selection functions, rewire `initCoursesView` (162-187), `fetchCourses` (216-225), `applyCourseFilters` (332-334).

- [ ] **Step 1: Add module state**

In `frontend/app.js`, immediately after line 33 (`let courseMarkers = new Map();`), add:

```javascript
let courseStartDate = null; // ISO YYYY-MM-DD, selected range start
let courseEndDate = null;   // ISO YYYY-MM-DD, selected range end
const DATE_STRIP_DAYS = 14; // chips shown: today .. today+13
```

- [ ] **Step 2: Add date helpers + strip rendering**

Add these functions in `app.js` just above `function switchView(` (currently line 189):

```javascript
function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

// Add `n` days to an ISO date string, returning a new ISO date string.
// Mirrors the date math already used in fetchCourses (line ~228).
function addDaysIso(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

function renderDateStrip() {
  const strip = document.getElementById("date-strip");
  const today = todayIso();
  strip.innerHTML = "";
  for (let i = 0; i < DATE_STRIP_DAYS; i++) {
    const iso = addDaysIso(today, i);
    const d = new Date(iso + "T00:00:00");
    const dow = d.toLocaleDateString(undefined, { weekday: "short" });
    const chip = document.createElement("div");
    chip.className = "date-chip";
    chip.dataset.date = iso;
    chip.innerHTML =
      `<span class="dow">${dow}</span><span class="num">${d.getDate()}</span>`;
    chip.addEventListener("click", () => onDateChipClick(iso));
    strip.appendChild(chip);
  }
  updateDateChipStates();
}

// Tap one chip = single day. Tap a second (while a single day is selected) =
// range between them. Tap again (while a range is selected) = fresh single day.
function onDateChipClick(iso) {
  const isSingle = courseStartDate === courseEndDate;
  if (isSingle && courseStartDate) {
    const anchor = courseStartDate;
    courseStartDate = iso < anchor ? iso : anchor;
    courseEndDate = iso < anchor ? anchor : iso;
  } else {
    courseStartDate = iso;
    courseEndDate = iso;
  }
  updateDateChipStates();
  fetchCourses();
}

function updateDateChipStates() {
  const today = todayIso();
  document.querySelectorAll("#date-strip .date-chip").forEach((chip) => {
    const iso = chip.dataset.date;
    chip.classList.toggle("today", iso === today);
    chip.classList.toggle("selected", iso === courseStartDate || iso === courseEndDate);
    chip.classList.toggle(
      "inrange",
      iso > courseStartDate && iso < courseEndDate,
    );
  });
}
```

- [ ] **Step 3: Rewire `initCoursesView`**

Replace the body of `initCoursesView` (lines 162-187) with:

```javascript
function initCoursesView() {
  // Default selection = today.
  courseStartDate = todayIso();
  courseEndDate = courseStartDate;
  renderDateStrip();

  // Bind view tabs
  document.querySelectorAll(".view-tab").forEach((btn) =>
    btn.addEventListener("click", () => switchView(btn.dataset.view))
  );

  // Bind client-side filter events
  document.getElementById("category-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-spots-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-plus-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-search-filter").addEventListener("input", applyCourseFilters);

  initTimeSlider();
}
```

> `initTimeSlider` is added in Task 4. Until then this reference will throw at runtime when the Courses view initializes. To keep Task 2 independently verifiable, **temporarily** add a no-op stub at the bottom of `app.js` for now:
>
> ```javascript
> function initTimeSlider() { /* implemented in Task 4 */ }
> ```
>
> Task 4 replaces this stub with the real implementation.

- [ ] **Step 4: Rewire `fetchCourses` to read module state + bump clamp to 14**

Replace lines 217-225 — currently:

```javascript
  const startDate = document.getElementById("course-date-start").value;
  let endDate = document.getElementById("course-date-end").value;
  if (!startDate) return;
  if (!endDate || endDate < startDate) {
    endDate = startDate;
    document.getElementById("course-date-end").value = startDate;
  }

  const days = Math.min(13, Math.max(1, daysBetween(startDate, endDate) + 1));
```

with:

```javascript
  const startDate = courseStartDate;
  let endDate = courseEndDate;
  if (!startDate) return;
  if (!endDate || endDate < startDate) endDate = startDate;

  const days = Math.min(14, Math.max(1, daysBetween(startDate, endDate) + 1));
```

- [ ] **Step 5: Rewire `applyCourseFilters` to read module state**

Replace lines 333-334 — currently:

```javascript
  const startDate = document.getElementById("course-date-start").value;
  const endDate = document.getElementById("course-date-end").value || startDate;
```

with:

```javascript
  const startDate = courseStartDate;
  const endDate = courseEndDate || courseStartDate;
```

- [ ] **Step 6: Lint**

Run: `cd frontend && npx eslint .`
Expected: PASS. (If ESLint warns `no-unused-vars` for `getSelectedTimeSlots`/`courseTimeSlot`, ignore for now — they're removed in Task 4.)

- [ ] **Step 7: Manual check**

`make serve`, open http://localhost:8000, Courses tab. Expected:
- 14 chips render; ~7 fit with the right edge faded; the rest reachable by horizontal scroll.
- Today's chip is amber-tinted **and** navy-selected with an amber ring.
- Tapping tomorrow selects just that day (single) and the course list reloads.
- Tapping a second chip forms a range (navy endpoints, light-blue middle) and reloads.
- Tapping again resets to a single day.
- No console errors.

- [ ] **Step 8: Commit**

```bash
git add frontend/app.js
git commit -m "feat(frontend): day-strip date selection driving courses fetch/filter"
```

---

## Task 3: Time-slider markup + styles

**Files:**
- Modify: `frontend/index.html:91-108` (the Time of Day `filter-group`)
- Modify: `frontend/style.css` (append time-slider rules)

- [ ] **Step 1: Replace the time-toggle markup**

In `frontend/index.html`, replace lines 91-108 — currently:

```html
                <div class="filter-group">
                    <label class="filter-label">Time of Day</label>
                    <div class="toggle-group time-toggle" id="time-toggle">
                        <label class="toggle-option">
                            <input type="checkbox" value="morning" checked>
                            <span>Morning</span>
                        </label>
                        <label class="toggle-option">
                            <input type="checkbox" value="afternoon" checked>
                            <span>Afternoon</span>
                        </label>
                        <label class="toggle-option">
                            <input type="checkbox" value="evening" checked>
                            <span>Evening</span>
                        </label>
                    </div>
                    <div class="slider-description">Morning &lt;12, Afternoon 12–17, Evening ≥17</div>
                </div>
```

with:

```html
                <div class="filter-group">
                    <label class="filter-label">Time of Day</label>
                    <div id="time-slider">
                        <div class="slider-track-container">
                            <div class="slider-track"></div>
                            <div class="time-any-zone"></div>
                            <div class="time-divider"></div>
                            <div class="slider-fill" id="time-slider-fill"></div>
                            <input type="range" id="time-slider-min" min="0" max="33" value="0" step="1">
                            <input type="range" id="time-slider-max" min="0" max="33" value="33" step="1">
                        </div>
                        <div class="time-slider-ticks">
                            <span>Any</span><span>8</span><span>12</span><span>16</span><span>20</span><span>24</span>
                        </div>
                        <div class="slider-description" id="time-slider-description"></div>
                    </div>
                </div>
```

> Value domain: `0` = "Any" (no lower bound), `33` = "24:00" (no upper bound), and `1..32` map to 08:00..23:30 in 30-min steps. Wiring is in Task 4.

- [ ] **Step 2: Add time-slider CSS**

Append to `frontend/style.css`:

```css
/* Time-of-day slider extras (reuses .slider-track/.slider-fill/range thumb rules) */
#time-slider { padding: 4px 0; }

/* grey "Any" zone covering the leftmost step (index 0 -> 1 of 33) */
.time-any-zone {
    position: absolute;
    top: 14px;
    left: 0;
    width: 3.03%; /* 1/33 of the track */
    height: 4px;
    background: #c9d3df;
    border-radius: 2px 0 0 2px;
    pointer-events: none;
}
.time-divider {
    position: absolute;
    top: 11px;
    left: 3.03%;
    width: 1px;
    height: 10px;
    background: #fff;
    pointer-events: none;
}

.time-slider-ticks {
    display: flex;
    justify-content: space-between;
    padding: 2px 4px 0;
    margin: 0 8px;
    font-size: 11px;
    font-weight: 600;
    color: #999;
}
.time-slider-ticks span:first-child { color: #7d8a9a; font-style: italic; }
```

- [ ] **Step 3: Lint**

Run: `cd frontend && npx eslint .`
Expected: PASS.

- [ ] **Step 4: Manual check**

`make serve`, http://localhost:8000, Courses tab. Expected: a dual-handle slider renders with a grey "Any" zone at the far left, a divider, and the tick row `Any 8 12 16 20 24`. Handles are draggable (no behavior wired yet — list won't refilter; that's Task 4). No console errors. (Note: the Courses view will currently error on init because of the `initTimeSlider` stub only if Task 2's stub was removed — it should still be the no-op stub here.)

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/style.css
git commit -m "feat(frontend): time-of-day range slider markup and styles"
```

---

## Task 4: Time-slider behavior + time filtering

**Files:**
- Modify: `frontend/app.js` — replace the `initTimeSlider` stub with the real implementation + helpers; rewire `applyCourseFilters` time logic (327, 339-340); delete `getSelectedTimeSlots` (314-316) and `courseTimeSlot` (318-324).

- [ ] **Step 1: Delete the old time-slot helpers**

In `frontend/app.js`, delete `getSelectedTimeSlots` (lines 314-316) and `courseTimeSlot` (lines 318-324) entirely:

```javascript
function getSelectedTimeSlots() {
  return [...document.querySelectorAll("#time-toggle input:checked")].map((cb) => cb.value);
}

function courseTimeSlot(startTime) {
  const hour = parseInt(startTime.split(":")[0], 10);
  if (isNaN(hour)) return null;
  if (hour < 12) return "morning";
  if (hour < 17) return "afternoon";
  return "evening";
}
```

- [ ] **Step 2: Replace the `initTimeSlider` stub with the real slider**

Replace the temporary stub from Task 2:

```javascript
function initTimeSlider() { /* implemented in Task 4 */ }
```

with:

```javascript
// Value domain for both range inputs: 0 = "Any" (no lower bound),
// 33 = "24:00" (no upper bound), 1..32 = 08:00..23:30 in 30-min steps.
const TIME_MIN_INDEX = 0;
const TIME_MAX_INDEX = 33;

function timeIndexToMinutes(i) {
  // Concrete clock time for an inner index; callers handle the open extremes.
  return 480 + (i - 1) * 30; // index 1 -> 08:00 (480), index 33 -> 24:00 (1440)
}

function formatTimeIndex(i) {
  if (i <= TIME_MIN_INDEX) return "Any";
  const mins = timeIndexToMinutes(i);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function initTimeSlider() {
  document.getElementById("time-slider-min").addEventListener("input", onTimeSliderChange);
  document.getElementById("time-slider-max").addEventListener("input", onTimeSliderChange);
  updateTimeSliderFill();
  updateTimeSliderDescription();
}

function onTimeSliderChange() {
  const minSlider = document.getElementById("time-slider-min");
  const maxSlider = document.getElementById("time-slider-max");
  const minVal = parseInt(minSlider.value);
  const maxVal = parseInt(maxSlider.value);

  // Prevent the handles from crossing (same idiom as the tier slider).
  if (minVal > maxVal) {
    if (this === minSlider) {
      maxSlider.value = minVal;
    } else {
      minSlider.value = maxVal;
    }
  }

  updateTimeSliderFill();
  updateTimeSliderDescription();
  applyCourseFilters();
}

function updateTimeSliderFill() {
  const minVal = parseInt(document.getElementById("time-slider-min").value);
  const maxVal = parseInt(document.getElementById("time-slider-max").value);
  const fill = document.getElementById("time-slider-fill");
  const pctMin = (minVal / TIME_MAX_INDEX) * 100;
  const pctMax = (maxVal / TIME_MAX_INDEX) * 100;
  fill.style.left = pctMin + "%";
  fill.style.width = (pctMax - pctMin) + "%";
}

function updateTimeSliderDescription() {
  const minVal = parseInt(document.getElementById("time-slider-min").value);
  const maxVal = parseInt(document.getElementById("time-slider-max").value);
  const descEl = document.getElementById("time-slider-description");
  const openLow = minVal <= TIME_MIN_INDEX;
  const openHigh = maxVal >= TIME_MAX_INDEX;
  if (openLow && openHigh) {
    descEl.textContent = "Showing courses at any time";
  } else if (openLow) {
    descEl.textContent = `Showing courses starting up to ${formatTimeIndex(maxVal)}`;
  } else if (openHigh) {
    descEl.textContent = `Showing courses starting from ${formatTimeIndex(minVal)}`;
  } else {
    descEl.textContent =
      `Showing courses starting ${formatTimeIndex(minVal)} – ${formatTimeIndex(maxVal)}`;
  }
}

// Returns {lower, upper} bounds in minutes-of-day; open extremes use ±Infinity.
function timeFilterBounds() {
  const minVal = parseInt(document.getElementById("time-slider-min").value);
  const maxVal = parseInt(document.getElementById("time-slider-max").value);
  return {
    lower: minVal <= TIME_MIN_INDEX ? -Infinity : timeIndexToMinutes(minVal),
    upper: maxVal >= TIME_MAX_INDEX ? Infinity : timeIndexToMinutes(maxVal),
  };
}

// Parse an "HH:MM" (or "HH:MM:SS") start_time string to minutes-of-day.
function startTimeToMinutes(startTime) {
  if (!startTime || startTime.length < 5) return null;
  const h = parseInt(startTime.slice(0, 2), 10);
  const m = parseInt(startTime.slice(3, 5), 10);
  if (isNaN(h) || isNaN(m)) return null;
  return h * 60 + m;
}
```

- [ ] **Step 3: Rewire the time filter in `applyCourseFilters`**

In `applyCourseFilters`, replace line 327 — currently:

```javascript
  const timeSlots = getSelectedTimeSlots();
```

with:

```javascript
  const { lower: timeLower, upper: timeUpper } = timeFilterBounds();
```

Then replace lines 339-340 — currently:

```javascript
    const slot = courseTimeSlot(c.start_time);
    if (!slot || !timeSlots.includes(slot)) return false;
```

with:

```javascript
    const startMin = startTimeToMinutes(c.start_time);
    if (startMin === null || startMin < timeLower || startMin > timeUpper) return false;
```

- [ ] **Step 4: Lint**

Run: `cd frontend && npx eslint .`
Expected: PASS with no `no-unused-vars` warnings (the old helpers are gone; all new functions are referenced).

- [ ] **Step 5: Manual check**

`make serve`, http://localhost:8000, Courses tab. Zoom into a city so courses load, then:
- Default: both handles at extremes → description reads "Showing courses at any time"; all courses listed.
- Drag the **min** handle off "Any" to 08:00 → description "Showing courses starting from 08:00"; the "Any" grey zone is no longer under the handle.
- Drag both to a band (e.g. 09:00–18:00) → description "Showing courses starting 09:00 – 18:00"; list/map/stats shrink to matching courses.
- Drag the **max** handle to the far right (24:00) with min at a time → "Showing courses starting from HH:MM".
- Handles cannot cross.
- No console errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/app.js
git commit -m "feat(frontend): time-of-day range slider filtering by start time"
```

---

## Task 5: End-to-end verification & cleanup

**Files:** none (verification only), unless a leftover is found.

- [ ] **Step 1: Confirm no stale references remain**

Run: `grep -nE "course-date-start|course-date-end|time-toggle|courseTimeSlot|getSelectedTimeSlots|initTimeSlider\(\) \{ /\*" frontend/app.js frontend/index.html`
Expected: only the `initTimeSlider` *definition* and its call remain; **no** matches for `course-date-start`, `course-date-end`, `time-toggle`, `courseTimeSlot`, `getSelectedTimeSlots`, or the temporary stub comment.

- [ ] **Step 2: Full lint**

Run: `cd frontend && npx eslint .`
Expected: PASS, zero warnings.

- [ ] **Step 3: Full manual pass**

`make serve`, http://localhost:8000:
- Venues tab still works (tier slider, filters) — confirm nothing regressed.
- Courses tab: date strip (single tap, range, scroll to week 2, today amber+selected), time slider (any/band/open extremes), plus category/spots/plus/search filters all compose correctly. Stats line updates. Map markers update.
- Reload the page mid-Courses-view; confirm clean init.

- [ ] **Step 4: Final commit (if any cleanup was needed)**

```bash
git add -A
git commit -m "chore(frontend): tidy up after courses filter redesign"
```

(If Step 1-3 found nothing to change, skip this commit.)

---

## Self-Review Notes (author)

- **Spec coverage:** Date strip (14 chips, 7 visible + fade, today amber, selected navy, in-range blue, today+selected ring, legend always shown, tap/range/reset interaction, default today) → Tasks 1-2. 14-day clamp → Task 2 Step 4. Time slider (08:00–24:00, 30-min steps, reuse tier-slider pattern, "Any" left + 24:00 open right, description states, filter by start_time in band) → Tasks 3-4. No backend change → honored (no backend files touched).
- **Placeholder scan:** No TBD/TODO; the one intentional temporary stub (`initTimeSlider` no-op) is introduced in Task 2 and explicitly removed in Task 4 Step 2, with Task 5 Step 1 guarding against it being left behind.
- **Type/name consistency:** Element IDs (`date-strip`, `time-slider-min/max/fill/description`), state (`courseStartDate`/`courseEndDate`), and functions (`renderDateStrip`, `updateDateChipStates`, `onDateChipClick`, `initTimeSlider`, `onTimeSliderChange`, `updateTimeSliderFill`, `updateTimeSliderDescription`, `timeFilterBounds`, `startTimeToMinutes`, `formatTimeIndex`, `timeIndexToMinutes`) are referenced consistently across tasks. Domain constants `TIME_MIN_INDEX=0`/`TIME_MAX_INDEX=33` match the HTML `min/max` attributes in Task 3.
