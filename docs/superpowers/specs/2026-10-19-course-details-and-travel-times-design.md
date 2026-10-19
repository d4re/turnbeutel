# Course details & travel times — design

Date: 2026-10-19 · Status: approved in brainstorming, ready for planning

## Goal

Clicking a course in the Courses list shows its details (description, rating,
cancellation window, class-level tier requirement, on-site fee) and how long it
takes to get there from home, by bike and by public transport. Venues get the
same travel chips in their popups and list items.

A separate planner feature (built elsewhere) depends on this work through one
contract: a server-side home setting and a batch travel endpoint whose cache it
can read without waiting (see [Planner contract](#planner-contract)).

## Decisions and assumptions

- **Single user, private instance.** The deployed instance is reachable only by
  its owner. Home and plan are therefore stored server-side, and the travel and
  geocode endpoints have no per-IP limits. If the instance is ever shared, both
  must be revisited (documented in AGENTS.md).
- Home is set by **address search (Nominatim) plus a draggable map pin**.
- Course details **expand in place** in the list item; map focus behaviour is
  unchanged. Works identically in the desktop sidebar and the mobile sheet.
- Venues get **travel chips only** (popup + list item), no expanded venue view.
- Transit has two flavours: a **typical** time per venue (fixed reference:
  next Tuesday 17:30 Europe/Berlin), precomputed in the background, and an
  **exact** time for an expanded course (arrive by its start), fetched on
  click, including the departure time ("leave 18:22").
- "Your plan" lives in a separate **"You" panel** and only drives badges; the
  tier filters stay independent.
- Travel times are computed by an **in-process background worker** that warms
  every venue in the prefetch cities, plus on-demand lookups at higher
  priority. No self-hosted routing.
- Out of scope for the first cut: filtering/sorting by travel time; a
  selectable transit reference time.

## Background facts (from prototyping, Oct 2026)

- USC `/courses` list items already carry `planTypes`, `planTypesB2B` (S/M/L/XL
  codes in both, same as venues) and `extraPriceDescriptionText`.
- USC `GET /courses/{id}` returns `description`, `ratings{averageScore,
  totalRatings}`, `freeCancellationHours`, `bookingType`, plus the fields above.
- **Some restrictions are per class, not per venue**: e.g. Fl'air's *Intro to
  Aerial Silks/Straps/Hoop* need L although the venue is included in M.
- **On-site fees are often only in the title**: `USC Checkin + 15 €`,
  `+12 Euros`, `EXTRA 5 EUR PAID AT THE DOOR`, `+ 10€ Zuzahlung`.
- **OSRM** bike profile (`routing.openstreetmap.de/routed-bike`) gives good
  distances but its durations assume ~13 km/h, which is too slow. A 1×100
  table request takes ~0.2 s.
- **VBB** (`v6.vbb.transport.rest`) covers Berlin/Brandenburg, allows
  100 req/min, answers in ~0.6–1.8 s when up, but the public instance was down
  or timing out for long stretches.
- Address precision matters: a pin ~80 m off can make VBB pick a different
  stop and shift times by several minutes. Nominatim resolves house numbers
  with suffixes (e.g. "143G") correctly.

## 1. Course details (PR A)

### Data from the list call

`Course` (`backend/models.py`) gains:

| Field | Source |
|---|---|
| `tiers_private: list[str]` | `planTypes` mapped through `CORP_TO_PRIVATE` (as `transform_venue`) |
| `tiers_corporate: list[str]` | `planTypesB2B` filtered to `CORPORATE_TIER_ORDER` |
| `min_tier_private`, `min_tier_corporate: str \| None` | lowest tier of each list (same helper as venues) |
| `extra_fee_text: str \| None` | `extraPriceDescriptionText`, empty → `None` |
| `extra_fee_eur: int \| None` | `parse_extra_fee(title, extra_fee_text)` |

`parse_extra_fee(title, extra_text)` is a pure function. It searches
`extra_text` first, then `title`, for an integer amount adjacent to
`€`/`EUR`/`Euro`/`Euros` (case-insensitive) that is preceded by `+` or the word
`extra`. It returns the first match or `None`. Bare prices without `+`/`extra`
(e.g. `Drop-in 15 €`) do not match.

### Storage

The `courses` table gets the new columns (tier lists as JSON text). `_migrate`
extends its existing "drop on outdated shape" check: if the `courses` table
lacks an `extra_fee_eur` column, drop `courses` and `course_fetches`. Courses
are TTL-cached and refilled by `prefetch_loop`.

### Lazy detail endpoint

`GET /api/courses/{course_id}` → `CourseDetail { description: str,
rating: float | None, rating_count: int | None, free_cancellation_hours:
int | None, booking_type: str | None }`.

- Proxies USC `GET /courses/{id}`; cached in a new `course_details(course_id
  PRIMARY KEY, payload_json, fetched_at)` table, TTL 7 d.
- Expired entry: serve stale, refresh in the background.
- USC 404 → 404. Other USC failure without a cached copy → 502.
- Shares the course-fetch `Semaphore(5)`. One request per click; never
  prefetched for whole cities.

## 2. Settings and geocoding

### Settings store (PR A: plan; PR B: home, bike speed)

- New table `settings(id INTEGER PRIMARY KEY CHECK (id = 1), payload_json,
  updated_at)`, accessed under `storage._lock`.
- `Settings` model, all fields optional (`{}` = nothing set):
  - `plan: { type: "corporate" | "private", tier: str }` — tier must belong to
    the type (S/M/L/XL or Essential/Classic/Premium/Max).
  - `home: { lat: float, lng: float, label: str }` — lat/lng range-checked.
  - `bike_kmh: 15 | 18 | 21` (default 18 when unset).
- `GET /api/settings` returns it; `PUT /api/settings` replaces it whole (last
  write wins). A change of `home` triggers a travel warm (§3).
- The stored home is exact; travel cache keys use it rounded to 4 decimals.

### Geocoding (PR B)

- `GET /api/geocode?q=&viewbox=` proxies Nominatim `/search`
  (`format=jsonv2`, `addressdetails=1`, `limit=5`, `viewbox` as a bias, not
  bounded). Returns `[{label, lat, lng}]`.
- Descriptive User-Agent; global spacing of ≥1 s between requests
  (`asyncio.Lock` + last-request timestamp).
- Cached 30 d in `geocode_cache(query_norm PRIMARY KEY, payload_json,
  fetched_at)`; `query_norm` = lowercased, whitespace-collapsed query.
- Nominatim failure → 502; the UI suggests setting home on the map instead.
- The client searches on Enter / button press only (no autocomplete).

## 3. Travel times (PR B)

New module `backend/travel.py` (OSRM + VBB clients, helpers, worker). Storage
functions live in `storage.py`. Destinations are venue coordinates from the
`venues` table keyed by `venue_id` (= `Course.venue_id`); venues without
coordinates get no travel data.

### Cache tables

- `travel(origin_key, venue_id, bike_km, bike_at, pt_min, pt_lines_json,
  walk_m, pt_at, pt_failed_at, PRIMARY KEY (origin_key, venue_id))`.
  `origin_key` = `"lat,lng"` rounded to 4 decimals. Bike TTL 30 d, transit TTL
  7 d. `pt_failed_at` suppresses transit retries for 1 h.
- `travel_exact(origin_key, venue_id, arrive_by, pt_min, depart_at, arrive_at,
  lines_json, walk_m, fetched_at, PRIMARY KEY (origin_key, venue_id,
  arrive_by))`, TTL 24 h.

### Bike

OSRM `routed-bike/table/v1/driving/{origin};{dest…}?sources=0&annotations=distance`,
≤100 destinations per request, requests sequential. Only distance (km) is
stored. The client computes minutes = `bike_km / bike_kmh × 60 + 2`, so the
bike speed never affects the cache.

### Public transport

Only when origin and destination both lie inside the VBB bbox
(lat 51.3–53.6, lng 11.2–14.8); otherwise `pt_min` stays `null` and is not
retried.

- **Typical**: `GET /journeys?from…&to…&departure=<next Tue 17:30 Europe/Berlin>&results=3`.
  Take the earliest-arriving journey; `pt_min` = last arrival − first departure
  (includes walking and transfers, excludes waiting at home). Store the
  transit line names in order (e.g. `["M4", "U8"]`) and total walking metres.
- **Exact**: `arrival=<course date + start − 5 min>`. Take the
  latest-departing journey that arrives by then; additionally store
  `depart_at` / `arrive_at`.
- Pacing: one request at a time, ≥0.7 s between requests, 15 s timeout, one
  retry. A final failure sets `pt_failed_at` (typical) or returns an error
  (exact).

### Worker

One asyncio task started from the lifespan, consuming an
`asyncio.PriorityQueue` with de-duplication by job key:

| Priority | Job |
|---|---|
| 0 | exact lookup for an expanded course |
| 1 | venues requested through `POST /api/travel` |
| 2 | background warm: all venues of the prefetch cities, nearest first |

The warm is enqueued when home changes and on each `prefetch_loop` cycle for
entries that would expire before the next cycle. Bike batches for a set of
venues run before their transit lookups. Jobs carry their `origin_key`;
jobs for a stale origin are dropped when dequeued.

### Endpoints

- `POST /api/travel { venue_ids: [str], origin?: {lat, lng} }` →
  `{ results: { venue_id: { bike_km, pt_min, pt_lines, walk_m } },
  pending: [venue_id] }`.
  - `origin` defaults to the stored home; neither → 409.
  - Missing/expired ids are enqueued at priority 1 and listed in `pending`
    (a venue appears in `results` with whatever is cached and in `pending`
    while any part is still being computed).
  - At most 500 ids per call (422 above). Unknown ids or ids without
    coordinates appear in neither list.
- `GET /api/travel/exact?venue_id=&date=&start=` → `{ pt_min, depart_at,
  arrive_at, lines, walk_m }`. Uses the stored home (409 if unset). Enqueues
  at priority 0 and waits up to 10 s; timeout → 504, VBB failure → 502.
  Served from `travel_exact` when cached.

### Privacy

The home coordinate is sent only to OSRM and VBB, never logged; outgoing
requests carry a turnbeutel User-Agent.

### Planner contract

1. Home (and plan, bike speed) are known on the server via `/api/settings`.
2. `POST /api/travel` over venue ids; calling it warms the cache.
3. Travel is stored per venue, so the planner joins on `Course.venue_id`.

## 4. Frontend

All in `frontend/app.js` / `index.html` / `style.css`, following the existing
structure. Settings are loaded once in `init()` into a module-level `settings`
object.

### "You" panel

- A "You" button next to "Filters" in `#sidebar-header` opens a panel built
  like the filter panels (full-sheet overlay on mobile).
- **Home**: address input + Search button → up to 5 results to pick; "Pick on
  map" button; current label with "Clear".
- **Your plan**: Corporate/Private + tier select.
- **Bike speed**: 15 / 18 / 21 km/h.
- Every change does `PUT /api/settings`; on failure, show an inline error and
  revert the control.
- The membership toggle defaults to the plan's type on first load.
- Map: home shown as a distinct **draggable** marker in its own layer (outside
  the cluster group, never triggering a marker rebuild); dropping it saves the
  position with label "Pinned location". Map `contextmenu` (right-click /
  long-press) offers "Set home here".

### Course list items

Badges next to PLUS:
- `+N € on site` when `extra_fee_eur` is set (`extra_fee_text` as tooltip).
- `⬆ <tier>` when the plan's tier is not in the class's tiers for the plan's
  type (showing the class's min tier). With no plan set: when the class's min
  tier is above its venue's min tier for the active membership toggle.

### Expanded course

Clicking an item marks it active, focuses the map as today, and expands a
details block; clicking another item collapses the previous one.

- Immediately: fee text, class tiers, typical travel chips if cached.
- In parallel, memoized per session:
  - `GET /api/courses/{id}` → description (clamped to 4 lines, "more"
    toggle), ★ rating (count), "Free cancellation up to N h before", booking
    type.
  - `GET /api/travel/exact` → replaces the transit chip with
    "🚆 31 min · leave 18:22 · M4 → U8". On failure the typical chip stays,
    labelled "typical, Tue 17:30".
- "Book on USC" link (venue URL). Errors are shown inline and never block the
  rest of the block.

### Travel chips

- Format: `🚲 14 min · 🚆 22 min (M4 → U8)`; `🚆 –` when transit is unknown.
- A `travelByVenue` Map holds fetched results. Rendering venue/course list
  chunks or opening a popup collects unknown venue ids into a debounced
  `POST /api/travel`. `pending` ids are re-polled every 3 s for at most
  2 min. Chips update in place via `[data-travel-venue="…"]`; markers are
  never rebuilt. Popup content is built on open.
- No home set → no chips, no travel requests.
- Bike minutes are computed client-side; changing the speed re-renders chips.

## Testing

Backend (pytest, outcome-based, `httpx.MockTransport` for outside services):
- `parse_extra_fee` table test (four patterns, bare-price negative, empty).
- `transform_course` tier and fee fields; `_migrate` from an old-shape table.
- `/api/courses/{id}`: fetch, cache hit, 404, 502.
- Settings round-trip and validation (bad tier for type, bad speed, bad
  coords).
- Geocode: mocked Nominatim, cache hit, ≥1 s spacing (fake clock).
- OSRM 100-batch split; typical/exact journey selection from canned VBB
  responses; VBB bbox; next-Tuesday calculation incl. a DST week; TTLs and
  `pt_failed_at`; `POST /api/travel` pending → results; worker priority
  order; exact-lookup timeout.

Frontend: ESLint, then the `/verify` skill in a headless browser at desktop
and phone widths: set home by search and by pin drag; `⬆ L` on Fl'air silks
and a fee badge on a fee class; an expanded course with description and exact
transit; chips on venue popups.

## Delivery

Two PRs, each updating README (endpoints, structure) and AGENTS.md
(architecture, rate limits, single-user assumption, schema):

1. `feat: course details, class tiers and on-site fees` — §1, settings store
   with `plan`, "You" panel with the plan section, badges, expanded course
   (without travel).
2. `feat: home location and travel times` — §2 geocoding + home/bike speed,
   §3, travel chips, exact transit in the expanded course.

Rate-limit additions go into AGENTS.md's "Rate limiting" list: OSRM sequential
batches of 100; VBB one request at a time ≥0.7 s apart; Nominatim ≥1 s apart.
