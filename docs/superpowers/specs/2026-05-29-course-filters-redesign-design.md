# Course Filters Redesign — Date Strip & Time-of-Day Slider

**Date:** 2026-05-29
**Status:** Approved design, ready for implementation planning
**Scope:** Frontend only (`frontend/index.html`, `frontend/app.js`, `frontend/style.css`). No backend changes.

## Problem

The Courses view has two filter controls that don't fit how people actually browse:

1. **Date range** uses two `<input type="date">` calendar pickers. Selecting "just today" or "tomorrow" takes several clicks through a full calendar, even though the useful horizon is almost always within one week (rarely up to two).
2. **Time of Day** uses three coarse checkboxes (Morning / Afternoon / Evening). The user wants continuous control over the start/end of the time window.

## Goals

- Tapping "today" or "tomorrow" should be a single click.
- Show a full week at a glance without scrolling; allow scrolling out to ~2 weeks for edge cases.
- No full-calendar date picker.
- Replace the time-of-day checkboxes with a dual-handle range slider with adjustable start and end times.
- Keep all filtering client-side; reuse existing patterns and styling so the controls feel native.

## Non-Goals

- No change to how courses are fetched, cached, or stored.
- No backend/API changes.
- No date selection beyond the existing ~2-week horizon (the fetch path already clamps to 13 days).

---

## Component 1 — Date Strip (horizontal day chips)

Replaces the `#course-date-start` / `#course-date-end` calendar inputs.

### Layout

- A horizontally scrollable row of **40px-wide chips**, each showing a weekday abbreviation (top, small) and day-of-month number (bottom, bold).
- At the 380px sidebar width (348px content area after padding), **7 chips are fully visible** (today through today+6). The 8th chip peeks past a **right-edge fade gradient** to signal that more dates exist by scrolling.
- The strip spans **14 days** total, starting at today. Scroll (swipe / drag / horizontal trackpad) reveals days 8–14.

### Visual states

| State | Treatment |
|-------|-----------|
| **Today** (persistent marker) | Amber tint background `#fdf1dc`, border `#f0cd92`, orange weekday label `#d98e04`. Always shown regardless of selection. |
| **Selected** (range endpoints or single day) | Navy fill `#1a1a2e`, white text. |
| **In range** (days between two selected endpoints) | Light-blue fill `#dbe8f7`, border `#a9cbf0`, blue number `#2471a3`. |
| **Today + selected** | Navy fill plus a thin amber ring (`box-shadow: 0 0 0 2px #d98e04`) so "today" stays flagged while selected. |
| Default (none of the above) | White background, grey `#ccc` border. |

Tomorrow is **not** specially marked.

A **legend** ("Today / Selected / In range") sits directly under the strip and is shown in **every** state (including on load).

### Interaction

- **Tap one chip** → single-day selection (start = end = that day).
- **Tap a second chip** → range selection spanning the two tapped days (earlier becomes start, later becomes end; days between render as in-range).
- **Tap again** (after a range exists) → starts a fresh single-day selection.
- On load, **today** is the default selection (preserves current behavior).

### Integration

- The strip writes to the same `startDate` / `endDate` values that `fetchCourses()` and `applyCourseFilters()` already read, so the existing per-(city,date) cache and viewport-scoped fetch continue to work unchanged.
- The current `change` listeners on the two date inputs are replaced by chip-tap handlers that update the selected range and call `fetchCourses()`.
- **Span reconciliation:** the strip shows 14 chips (today + 13). `fetchCourses()` currently clamps the fetched span to 13 days (`Math.min(13, …)`), so a full today→day-14 selection (14 days) would drop the last day. **Decision: bump the clamp to 14** so any range the strip allows is fully fetched.

---

## Component 2 — Time-of-Day Range Slider

Replaces the three-checkbox `#time-toggle` group and the `courseTimeSlot()` bucketing.

### Pattern reuse

Built on the **existing Tier Range slider** markup and styling: two overlaid `input[type=range]`, a fill element between the handles, and the anti-cross logic in `onSliderChange()` (handles cannot pass each other). Same track/fill/handle CSS (`#ddd` track, `#4a90d9` fill and handles).

### Scale

- Time scale runs **08:00 → 24:00** in **30-minute steps**.
- Rationale (from `backend/cache/courses_2026-04-06.json`, 1,802-course representative day): earliest start 09:45, latest 23:05, zero courses before 08:00, peak 17:00–20:00. An 08:00–24:00 scale frames the entire populated range with a small morning cushion and uses midnight as a natural upper bound.

### Open-ended extremes

- **Left "Any" stop:** a small grey segment to the *left* of the 08:00 tick, labeled **"Any"** (muted grey, visually separated from the time scale by a gap). When the min handle is parked here, there is **no lower bound** — courses at any early time match. The "Any" zone is only filled when the min handle is on it; moving the handle to 08:00 leaves "Any" unfilled and the handle lands exactly on the "8" tick.
- **Right end (24:00):** the scale simply runs to **24:00**. The max handle at 24:00 means **no upper bound** ("and later") — midnight is a natural end-of-day, so no separate cap or "+" marker is needed.
- This keeps both extremes open-ended (catching any out-of-range outliers) while every label is honest: "earlier than 08:00 courses showing" only ever happens when the handle literally reads **"Any"**, not when it reads a clock time.

### Description line

A text line under the slider mirrors the existing `.slider-description` style and reads according to handle positions:

| Min handle | Max handle | Text |
|-----------|-----------|------|
| Any | 24:00 | "Showing courses at any time" |
| Any | HH:MM | "Showing courses starting up to HH:MM" |
| HH:MM | 24:00 | "Showing courses starting from HH:MM" |
| HH:MM | HH:MM | "Showing courses starting HH:MM – HH:MM" |

### Filter semantics

- A course matches when its `start_time` (an `"HH:MM"` string) falls **within the selected band**, inclusive of both endpoints.
- Min handle on "Any" → no lower bound. Max handle on 24:00 → no upper bound.
- This replaces the `courseTimeSlot()` morning/afternoon/evening logic in `applyCourseFilters()`.

---

## Files Touched

- `frontend/index.html` — replace date-picker markup with the day-strip container + legend; replace the time-toggle group with the range-slider markup.
- `frontend/app.js` — date-strip rendering + tap/range selection state; time-slider init, anti-cross handling, "Any"/24:00 extreme handling, description text; update `applyCourseFilters()` to filter by time band; remove `courseTimeSlot()` / `getSelectedTimeSlots()` / time-toggle wiring.
- `frontend/style.css` — chip strip, chip states (today/selected/in-range), edge fade, legend; time-slider "Any" cap segment and tick row (reusing the existing slider track/fill/handle rules).

## Testing / Verification

- Manual: load Courses view, confirm today is selected by default and amber-marked; tap tomorrow (single click); tap two chips for a range; scroll to reach week 2; confirm the ≤13-day clamp still bounds fetches.
- Manual: drag time handles, confirm anti-cross, confirm "Any" / 24:00 extremes, confirm description text matches each state, confirm course list/map/stats update.
- Confirm no backend requests change shape (same `/api/courses` calls).
