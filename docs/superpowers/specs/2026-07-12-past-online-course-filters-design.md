# Past & Online Course Filters — Design

**Date:** 2026-07-12
**Status:** Approved

## Problem

The course list shows courses that already started earlier today, which is
useless when planning what to attend. It also mixes online courses into a
map-centric UI where they don't belong by default.

## Design

Both filters live in `applyCourseFilters()` in `frontend/app.js`, the single
funnel every course passes through. No backend changes: the API already
delivers `date`, `start_time` (`HH:MM`), and `is_online` per course, and the
cache should keep serving full days so toggling filters never re-fetches.

### Past-courses filter (always on, no toggle)

Compute `todayIso()` and the current minutes-of-day once per filter run. Hide
a course when:

- `course.date < today` (stale data straddling midnight), or
- `course.date === today` and its start time (minutes-of-day) is strictly
  before now — a course starting this exact minute is still shown.

The user chose "hide once started": no grace period, no end-time logic.
Filters re-run on every interaction, so the cutoff moves forward naturally.

### Online-courses filter (toggle, hidden by default)

New checkbox `#course-online-filter` ("Show online") in the existing
`inline-filters` row next to "Has free spots" / "PLUS only", unchecked by
default. When unchecked, courses with `is_online === true` are hidden.
Checked counts as non-default in `updateFilterBadge()`.

## Testing

The frontend has no unit-test harness (ESLint only); verification is
end-to-end via the project `verify` skill: load the app, confirm today's
already-started courses are absent, and toggle the online checkbox.
