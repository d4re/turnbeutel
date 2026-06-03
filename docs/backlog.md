# Backlog

Deferred work and known limitations, captured so they aren't lost. These are
intentionally **not** scheduled into the current multi-city viewport plan
(`docs/superpowers/plans/2026-04-11-multi-city-viewport.md`).

---

## Client-side cache TTL and reload

**Context:** The multi-city frontend tracks what it has already loaded with two
add-only structures introduced in the viewport plan — `loadedVenueCities` (a
`Set<city_id>`) and `loadedCourseCities` (a `Map<city_id, Set<date>>`). Once a
city (venues) or a (city, date) pair (courses) is loaded, it is never requested
again for the lifetime of the page.

**Problem:** These client caches have no TTL and no invalidation. The backend
refreshes from USC after its own TTLs (`VENUES_TTL` = 24h, `COURSES_TTL` = 48h),
but a long-lived browser session will keep displaying whatever it first fetched,
with no way to pull the refreshed server-side data short of a full page reload.
This is a behavior change from the pre-multi-city app, which re-fetched
everything on every page load.

**Proposed fix:**
- Store a fetch timestamp alongside each loaded city (venues) and each
  (city, date) (courses) instead of a bare membership Set.
- Treat an entry as stale past a client TTL — either mirror the server TTLs or
  use a shorter "soft" TTL — and allow a re-fetch when the viewport or date
  selection next touches it.
- Consider a lightweight background refresh, or a "data may be stale — reload"
  affordance, so the user can opt into fresh data without losing map state
  (center/zoom, active filters).

**Acceptance:** Panning back to an already-loaded city after its TTL has elapsed
issues a fresh `/api/venues` (resp. `/api/courses`) request and the merged view
reflects the new data; within the TTL it stays a zero-request cache hit as it is
today.
