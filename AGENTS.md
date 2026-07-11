# Agent Guide: Turnbeutel

Design decisions and things to know when working on this project.

## Architecture Overview

This is a two-part app: a Python FastAPI backend that proxies the USC API, and a vanilla JS frontend that displays venues and courses on a map. The app is multi-city: the frontend loads whichever cities intersect the current map viewport.

```
USC API (api.urbansportsclub.com)  →  FastAPI backend (proxy + cache + transform)  →  Vanilla JS frontend (Leaflet map)
```

The backend (`server.py`) fetches venue, course, and city data from the USC API, transforms it into the frontend's expected format, and caches everything in a single SQLite database, `backend/cache/usc.db` (`storage.py`; TTLs: venues 24h, venue details 7d, courses 48h, cities/categories 7d). Venue details (visit limits) are enriched by a background task after a city's venues are first fetched. There is no static-data fallback — if the backend is down, the frontend shows an error.

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

### Frontend is intentionally zero-build

Vanilla HTML + JS + CSS. Leaflet.js and MarkerCluster loaded from CDN. No bundler, no framework. The only npm dependency is ESLint (dev-only, for linting).

### Mobile layout is a bottom sheet over a full-screen map

Below `max-width: 768px` (one CSS media query, mirrored by `matchMedia` in JS) the sidebar becomes a draggable bottom sheet with three snap states (`data-sheet="peek|half|full"` on `#sidebar`); the map always fills the viewport, so sheet moves never resize the map. Filter panels open as an overlay filling the sheet — the sheet's `transform` makes it the containing block for positioned descendants, so `position: fixed` inside it does NOT reach the viewport. Desktop layout is untouched. Design doc: `docs/superpowers/specs/2026-07-11-mobile-bottom-sheet-design.md`.

Related invariant: don't re-render markers on viewport changes unless new data arrived or the zoom-out branch cleared the layer (`mapMarkersCleared` in `app.js`) — rebuilding markers destroys open popups and made list-tap → popup flaky. Also, `init()` must stay the last statement in `app.js`: it runs immediately and reads top-level `const` bindings that only exist once the whole script has evaluated.

## Things to Watch Out For

### Rate limiting

All concurrency toward USC is deliberately bounded — don't increase it, USC could start blocking:
- Venue pages are fetched in batches of `VENUE_PAGE_BATCH` (5) per city, and at most `MAX_CONCURRENT_VENUE_FETCHES` (3) cities fetch live at once.
- Background detail enrichment and course fetches each use `asyncio.Semaphore(5)`.
- The frontend caps its own fan-out at `MAX_CONCURRENT_REQUESTS` (5) per-city requests.

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
