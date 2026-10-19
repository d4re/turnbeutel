# Agent Guide: Turnbeutel

Design decisions and things to know when working on this project.

## Architecture Overview

This is a two-part app: a Python FastAPI backend that proxies the USC API, and a vanilla JS frontend that displays venues and courses on a map. The app is multi-city: the frontend loads whichever cities intersect the current map viewport.

```
USC API (api.urbansportsclub.com)  →  FastAPI backend (proxy + cache + transform)  →  Vanilla JS frontend (Leaflet map)
```

The backend (`server.py`) fetches venue, course, and city data from the USC API, transforms it into the frontend's expected format, and caches everything in a single SQLite database, `backend/cache/usc.db` (`storage.py`; TTLs: venues 24h, venue details 7d, courses 48h, course details 7d, cities/categories 7d; travel: see below). Venue details are enriched by a background task after a city's venues are first fetched; visit limits come from USC's structured `GET /venues/{id}/limits` (`b2c` = private, `b2e` = corporate; -1 = no monthly limit, stored as 30 = the 1-visit/day cap), see `parse_venue_limits`. The old free-text `bookingLimitsText` has been empty since ~May 2026 and is no longer used. There is no static-data fallback — if the backend is down, the frontend shows an error.

Cities listed in the `PREFETCH_CITIES` env var (names or ids), plus every city within `PREFETCH_RADIUS_KM` of one (Docker Compose defaults: `Berlin`, 30 km — the default Berlin view also loads ~20 surrounding towns), are kept warm by `prefetch_loop` in `server.py`: started from the lifespan, it runs every `PREFETCH_INTERVAL_MINUTES` (default 60) and re-fetches venues and the 14-day course strip for anything that would pass its TTL before the next cycle, so those cities never block on a live USC fetch. Courses are fetched one date at a time (nearest first) across all prefetched cities, sharing one `Semaphore(5)`; venue-detail enrichment is awaited city by city rather than fired off per city. Course days are refreshed on fixed per-date slots (`course_refresh_due`), staggered across the ~47 h refresh period so the 14 days don't all come due at once after a cold start. It reuses the stale-while-revalidate workers (`_refresh_venues_in_background`, `_refresh_courses_in_background`), so it stays within the rate limits below.

Clicking a course expands it in place (`renderCourseDetails` in `app.js`). Class-level tiers (`planTypes`/`planTypesB2B`, which can be stricter than the venue's — e.g. Fl'air's aerial silks need L in an M venue) and on-site fees (`parse_extra_fee`: USC's `extraPriceDescriptionText`, else the title, e.g. `USC Checkin + 15 €`) come free with the `/courses` list call. Description, rating, free-cancellation hours and booking type come from `GET /api/courses/{id}`, which proxies USC `/courses/{id}` once per click and caches it in `course_details` (7 d TTL, stale served while refreshing). Never prefetch course details for whole cities. Design doc: `docs/superpowers/specs/2026-10-19-course-details-and-travel-times-design.md`.

Travel times from home live in `backend/travel.py`. Bike uses only OSRM's *distance* (`routed-bike` table service, batches of 100) — its durations assume ~13 km/h, too slow — and the frontend computes minutes as km ÷ the user's speed (15/18/21 km/h) × 60 + 2. Transit uses `v6.vbb.transport.rest` (Berlin/Brandenburg only, `VBB_BBOX`): the "typical" time per venue is the earliest-arriving journey leaving next Tuesday 17:30 (first departure → last arrival), cached per (rounded home, venue) in `travel` (bike 30 d, transit 7 d, failures retried after 1 h). One `TravelWorker` (started in the lifespan) works a priority queue — requested venues before the background warm (prefetch cities, nearest first, on home change and each prefetch cycle), and within a priority bike batches before transit; warm jobs for a previous home are dropped. `POST /api/travel` returns what is cached plus a `pending` list (the frontend re-polls every 3 s for up to 2 min). The exact journey for an expanded course (`GET /api/travel/exact`, arriving 5 min before the class) bypasses the queue so a slow background request can't block it, but shares the VBB pacer; cached 24 h in `travel_exact`. Address search goes through `GET /api/geocode` (Nominatim, cached 30 d in `geocode_cache`). The home coordinate must never be logged: the worker logs only exception type names, because httpx messages contain the request URL.

The frontend keeps its own in-memory cache of what it has loaded (`loadedVenueCities`, `loadedCourseCities` in `app.js`), with a soft TTL of 60 min (`CLIENT_CACHE_TTL_MS`): panning back to a city (or re-selecting a date) past the TTL re-fetches and *replaces* that city's (resp. that city+date's) data instead of appending. Design doc: `docs/superpowers/specs/2026-07-11-client-cache-ttl-design.md`.

## Linting & Testing

A pre-commit hook (`.git/hooks/pre-commit`) runs all checks automatically before each commit.

### Backend (Python)
- **Linter/formatter**: Ruff — configured in `backend/pyproject.toml`
- **Tests**: Pytest — outcome-based tests in `backend/test_server.py` (pure functions + endpoints) and `backend/test_storage.py` (SQLite layer)
- Install dev dependencies: `cd backend && uv sync --group dev`
- Run manually:
  ```bash
  cd backend
  uv run ruff check .
  uv run ruff format --check .
  uv run pytest -v
  ```

### Frontend (JavaScript)
- **Linter**: ESLint 9 — configured in `frontend/eslint.config.js`
- Install: `cd frontend && npm install`
- Run manually:
  ```bash
  cd frontend
  npx eslint app.js
  ```

## Key Design Decisions

### Private vs Corporate tiers are NOT interchangeable

The USC website has two parallel tier systems:

| Corporate | Private   |
|-----------|-----------|
| S         | Essential |
| M (Pro)   | Classic   |
| L (Pro)   | Premium   |
| XL (Pro)  | Max       |

**These do NOT always map 1:1.** We verified that out of 1,931 venues:
- 3 venues have inclusion mismatches (e.g., Fenriz Gym is excluded from Classic/private but included in M/corporate)
- 21 venues have different visit counts between the "equivalent" tiers

This is why the app stores **both** `tiers_private` and `tiers_corporate` per venue.

### The slider filters on `min_tier`

The dual-handle tier slider filters venues by their **minimum required tier** (the lowest tier that grants access). A venue available on "M, L, XL" has `min_tier_corporate = "M"`. Setting both slider handles to "L" means: show venues where the minimum tier is exactly L (i.e., L-exclusive venues not available on M or S).

### Settings are server-side and assume a single user

`GET/PUT /api/settings` stores one settings row (`settings` table): the user's own plan (`Plan`: type + tier, validated against the type's tier order), home (`lat`, `lng`, `label`) and bike speed (15/18/21 km/h). Changing home re-points the travel worker and re-warms. It is separate from the tier filter: the plan drives the `⬆` badges (and the default Corporate/Private toggle), the filters only what is shown. **This assumes the instance is reachable only by its owner.** If it is ever shared, per-user storage (or `localStorage`) and per-IP rate limits must be revisited.

### Frontend is intentionally zero-build

Vanilla HTML + JS + CSS. Leaflet.js and MarkerCluster loaded from CDN. No bundler, no framework. The only npm dependency is ESLint (dev-only, for linting).

### Mobile layout is a bottom sheet over a full-screen map

Below `max-width: 768px` (one CSS media query, mirrored by `matchMedia` in JS) the sidebar becomes a draggable bottom sheet with three snap states (`data-sheet="peek|half|full"` on `#sidebar`); the map always fills the viewport, so sheet moves never resize the map. Filter panels open as an overlay filling the sheet — the sheet's `transform` makes it the containing block for positioned descendants, so `position: fixed` inside it does NOT reach the viewport. Desktop layout is untouched. On mobile, the tap that expands a course does not focus the map (that would drop the sheet to peek and hide the details); tapping it again collapses it and focuses the venue. The "You" panel opens as the same kind of overlay (`body.you-open`). Design doc: `docs/superpowers/specs/2026-07-11-mobile-bottom-sheet-design.md`.

Related invariant: don't re-render markers on viewport changes unless new data arrived or the zoom-out branch cleared the layer (`mapMarkersCleared` in `app.js`) — rebuilding markers destroys open popups and made list-tap → popup flaky. Also, `init()` must stay the last statement in `app.js`: it runs immediately and reads top-level `const` bindings that only exist once the whole script has evaluated.

### Releases are derived from commit messages

There is no manual release step. On every push to `main`, the CI `docker` job runs `.github/scripts/next-version.sh`, which bumps the latest `v*` tag based on the Conventional Commit messages since it (`feat` → minor, `fix`/`perf` → patch, `!`/`BREAKING CHANGE` → major, everything else → no release). If there's a bump, the image is also tagged `X.Y.Z`, `X.Y` and `X`, and a GitHub release `vX.Y.Z` is created after the push succeeds. Downstream deployments (Renovate) track these version tags, so **PR titles (= squash commit subjects) must use the right Conventional Commit type**.

## Things to Watch Out For

### Rate limiting

All concurrency toward USC is deliberately bounded — don't increase it, USC could start blocking:
- Venue pages are fetched in batches of `VENUE_PAGE_BATCH` (5) per city, and at most `MAX_CONCURRENT_VENUE_FETCHES` (3) cities fetch live at once.
- Background detail enrichment and course fetches each use `asyncio.Semaphore(5)`.
- The frontend caps its own fan-out at `MAX_CONCURRENT_REQUESTS` (5) per-city requests.

The free routing services used for travel times have their own limits:
- OSRM (`routing.openstreetmap.de`): table requests one after another, at most 100 destinations each (`OSRM_BATCH`).
- VBB (`v6.vbb.transport.rest`, 100 req/min, often flaky): every request goes through one `Pacer` spacing starts ≥0.7 s apart (`VBB_SPACING`), 15 s timeout, one retry. At most two are in flight (one background, one exact lookup).
- Nominatim: ≥1 s between requests (`nominatim_pacer`), results cached 30 d.

### SQLite access is serialized

`storage.py` shares one connection across all threadpool workers, so every access — reads included — must hold `storage._lock` (use the `_fetchone` / `_fetchall` helpers). Unlocked concurrent reads raised `sqlite3.InterfaceError: bad parameter or other API misuse` on Python 3.14.

### Coordinate gaps

~13 out of 1,931 venues had no coordinates. These show in the venue list but not on the map. The `has_coordinates` field tracks this.

## Data Schema

`GET /api/venues?city_ids=1&city_ids=2` returns a city-grouped payload:

```json
{
  "tier_config": { "private": { "..." : "..." }, "corporate": { "..." : "..." } },
  "cities": [
    { "city_id": 1, "city_name": "Berlin", "total": 1931, "venues": ["..."] }
  ]
}
```

Each venue in `venues` has:

```json
{
  "name": "Fenriz Gym",
  "slug": "fenriz-trainingszentrum",
  "url": "https://urbansportsclub.com/en/venues/fenriz-trainingszentrum",
  "tiers_private": ["Premium", "Max"],
  "tiers_corporate": ["M", "L", "XL"],
  "min_tier_private": "Premium",
  "min_tier_corporate": "M",
  "activities": ["Fitness", "Mixed Martial Arts"],
  "district": "Kreuzberg",
  "street": "Lobeckstr. 36",
  "is_plus": false,
  "lat": 52.5036,
  "lng": 13.4078,
  "has_coordinates": true,
  "visit_limits": {
    "private": { "Essential": null, "Classic": null, "Premium": 8, "Max": 8 },
    "corporate": { "S": null, "M": 4, "L": 8, "XL": 8 }
  },
  "rating": 4.9,
  "review_count": 3874
}
```

Each course in `GET /api/courses` additionally carries class-level plan coverage and the on-site fee (empty lists / `null` when USC sends none):

```json
{
  "tiers_private": ["Premium", "Max"],
  "tiers_corporate": ["L", "XL"],
  "min_tier_private": "Premium",
  "min_tier_corporate": "L",
  "extra_fee_text": null,
  "extra_fee_eur": 10
}
```

## Before Every Commit

Run these steps before committing any changes:

1. **Run tests and linting** — `make test && make lint` (or rely on the pre-commit hook which does this automatically).
2. **Update README.md** — If your change adds, removes, or modifies any of the following, update the README to match:
   - API endpoints
   - Make targets or CLI commands
   - Project structure (new top-level files or directories)
   - Quick start / deployment instructions
   - Environment requirements
3. **Update AGENTS.md** — If your change affects architecture, design decisions, data schema, or development workflows, update the relevant section in `AGENTS.md` (which `CLAUDE.md` symlinks to) so future contributors have accurate context.
4. **Check documentation consistency** — Ensure the README, AGENTS.md, and any inline code comments agree with each other and with the actual code.

## Common Tasks

### Start the app locally
```bash
make serve
```

### Run via Docker
```bash
docker compose up --build
```

### Add a new filter to the frontend
1. Add the UI element in `index.html` inside `#filters`
2. Bind the change event in `populateFilters()` in `app.js`
3. Add the filter logic in `applyFilters()`
