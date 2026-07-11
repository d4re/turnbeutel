# Client-side cache TTL and reload — design

Resolves the "Client-side cache TTL and reload" backlog item (`docs/backlog.md`).

## Problem

The frontend tracks loaded data with two add-only structures: `loadedVenueCities`
(a `Set<city_id>`) and `loadedCourseCities` (a `Map<city_id, Set<date>>`). Once
loaded, a city or (city, date) pair is never requested again for the lifetime of
the page, so a long-lived browser session keeps displaying whatever it first
fetched even after the backend has refreshed from USC (`VENUES_TTL` = 24h,
`COURSES_TTL` = 48h).

## Design

### Timestamped cache entries

- `loadedVenueCities` becomes a `Map<city_id, fetchedAtMs>`.
- `loadedCourseCities` becomes a `Map<city_id, Map<date, fetchedAtMs>>`.
- A single soft TTL, `CLIENT_CACHE_TTL_MS` = 60 minutes, covers both. An entry
  is fresh iff `Date.now() - fetchedAtMs < CLIENT_CACHE_TTL_MS`.

**Why 60 minutes instead of mirroring the server TTLs:** a client refetch is
normally a cheap hit on the backend's SQLite cache, and a short client TTL
bounds worst-case data age at roughly server TTL + 1h (the client picks up
whatever the freshest server copy is within an hour of it appearing). Mirroring
the 24h/48h server TTLs would allow up to ~2× server-TTL staleness and would
almost never fire in-session anyway. Sessions shorter than an hour behave
exactly as today: zero re-requests.

### Refetch trigger: passive, on next touch

Staleness is only acted on when the viewport or the date selection next
*touches* the entry — the same code paths that today decide "not yet loaded"
now decide "not yet loaded, or stale". No background refresh timer and no
"data may be stale" banner (the backlog listed these as optional; the
acceptance criteria don't need them). Map state (center/zoom, filters, open
sheet) is untouched by a refetch, same as any other incremental merge.

### Replace-on-refetch merge semantics

Because a stale refetch re-covers data that is already in `allVenues` /
`allCourses`, merging switches from append-only to replace:

- **Venues** (`mergeVenuesResponse`): before pushing a city's venues, drop all
  existing venues with that `city_id` from `allVenues`. Also drop the replaced
  venues' `address_id`s from `venueDetailFetched`, so the lazy visit-limit
  detail refetches on next popup open instead of pinning a stale copy.
- **Courses** (`refreshCoursesForViewport` merge): before pushing, drop existing
  courses matching (city_id, date) for the dates actually being refreshed —
  excluding dates the backend reported as failed, which keep their old data and
  their old (stale) timestamp so the next touch retries them.

### Failure behavior (unchanged in spirit)

A failed venue fetch or a per-date course error leaves the old data on screen
and does not bump the timestamp, so the next viewport/date interaction retries.
In-flight guards (`venueCitiesInFlight`, the courses load token) are unchanged.

## Acceptance (from the backlog)

Panning back to an already-loaded city after the client TTL has elapsed issues
a fresh `/api/venues` (resp. `/api/courses`) request and the merged view
reflects the new data; within the TTL it stays a zero-request cache hit.

## Out of scope

- Background/interval refresh and a "stale — reload" UI affordance.
- Persisting the cache across page loads (it stays in-memory; a reload
  re-fetches everything, as pre-multi-city).
