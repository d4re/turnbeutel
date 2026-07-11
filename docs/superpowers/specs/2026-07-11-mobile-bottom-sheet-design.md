# Mobile Bottom-Sheet Layout — Design

**Date:** 2026-07-11
**Status:** Approved (user approved the bottom-sheet approach and delegated detail decisions)

## Problem

The frontend is desktop-only: `#sidebar` is fixed at `width: 380px; min-width: 380px`
with no media queries, so on a phone (~390px wide) the sidebar covers the entire
viewport and the map is unusable. The app also uses `100vh`, which on mobile
browsers hides the bottom of the layout behind the address bar.

Map-navigation polish (gestures, tap targets, popups) is explicitly out of scope
for now — the user wants to re-evaluate it once the layout is usable.

## Goals

- Phone-usable layout where map and list are both first-class (user uses both equally).
- Desktop layout unchanged.
- Zero-build vanilla JS/CSS, per project rules. No new dependencies.

## Approach (chosen from 3 options)

Google-Maps-style **bottom sheet**: on small screens the map fills the viewport and
the existing `#sidebar` element is restyled into a draggable bottom sheet.
Alternatives considered: full-screen map/list toggle (simpler, but never shows both
at once) and off-canvas drawer (least change, but the open drawer still covers the map).

## Design

### Breakpoint & shell

- Single breakpoint: `@media (max-width: 768px)` in CSS, mirrored by
  `window.matchMedia("(max-width: 768px)")` in JS (one helper, single source of truth).
- Mobile: `#map-container` fills the viewport; `#sidebar` becomes
  `position: fixed`, full-width, translated vertically via a CSS custom property.
  The map container's size never changes when the sheet moves, so no
  `invalidateSize()` churn; Leaflet's default `trackResize` covers rotation and
  breakpoint crossings.
- `#app` height: `100vh` with `100dvh` override; sheet bottom padding respects
  `env(safe-area-inset-bottom)`; viewport meta gains `viewport-fit=cover`.

### Sheet states

Three snap points, stored as data attribute `data-sheet` on `#sidebar`:

| State | Position | Shows |
|-------|----------|-------|
| `peek` | ~110px visible above bottom | drag handle, Venues/Courses tabs, stats |
| `half` | top at 50dvh | header + scrollable list alongside the map |
| `full` | top at 8dvh | header + list, strip of map remains as escape hatch |

Default on load: `half`.

### Dragging

- New drag-handle bar element at the top of the sheet (mobile-only, hidden on desktop).
- Pointer-event drag starts on the handle or the sheet header; moves and
  releases are handled on `document`, because a fast drag leaves the source
  element before its next pointermove fires (touch pointers are implicitly
  captured anyway). A ~6px threshold distinguishes tap from drag, and clicks
  within 150ms of a drag end are swallowed so buttons under the finger don't
  fire. The list body is NOT a drag zone — it scrolls normally (physically
  separate drag/scroll zones; no scroll-position heuristics).
- During drag: transform follows the pointer directly (no transition).
  On release: snap to the nearest state.
- Tapping the handle cycles peek → half → full → peek.
- `touch-action: none` on the handle/header so the browser doesn't hijack the gesture.

### Filters

- Filter panels stay where they are in the DOM. On mobile they are hidden by a
  stylesheet rule and shown as an overlay when `body.filters-open` is set. The
  sheet's `transform` makes it the containing block for positioned descendants,
  so the overlay fills the *sheet*, and opening filters first snaps the sheet
  to `full` (≈ full screen). This coexists with `switchView()`'s inline
  `display:none` on the inactive panel: the inline style always wins, so only
  the active view's panel ever appears in the overlay.
- A mobile-only "Filters" button sits in the sheet header next to the view tabs,
  with a badge showing the count of active (non-default) filters.
- Each filter panel gets a mobile-only top bar with a title and a "Done" close button.

### List → map interaction

- On mobile, tapping a venue/course list item first drops the sheet to `peek`,
  then pans/zooms to the marker and opens its popup; on mobile the popup's
  auto-pan padding keeps it above the peeked sheet.
- Tapping a map pin leaves the sheet state unchanged (at peek the map is nearly
  fully visible).

Two pre-existing bugs made "open the popup after a list click" unreliable and
were fixed as part of this work (they affect desktop too):

- `openPopup()` was called synchronously after `setView()`, while the marker
  was still inside a cluster. `focusMarker()` now waits for `moveend` and then
  retries briefly until the marker actually has a DOM element (the cluster
  group releases it an animation frame after the zoom ends).
- Every viewport change re-rendered all markers (venues: unconditional
  `applyFilters()`; courses: `fetchCourses()` re-rendered and flashed
  "Loading…" even with everything cached), destroying any open popup on pan.
  Both paths now skip the re-render unless new data arrived or the zoom-out
  branch actually cleared the marker layer (`mapMarkersCleared`).

### Out of scope

- Map gesture/tap-target/popup polish (deferred by user).
- Desktop changes of any kind.
- URL/state persistence for sheet position.

## Testing

Frontend has no test infrastructure (ESLint only, by design). Verification:

- `npx eslint app.js` passes.
- Manual/scripted check at a phone-sized viewport (e.g. 390×844): sidebar no
  longer covers the map, sheet snaps between three states, filters overlay
  opens/closes, list taps focus the map, desktop viewport unchanged.
