# Agent Guide: USC Venue Explorer

Design decisions and things to know when working on this project.

## Architecture Overview

This is a two-part app: a Python FastAPI backend that proxies the USC API, and a vanilla JS frontend that displays venues on a map.

```
USC API (api.urbansportsclub.com)  →  FastAPI backend (proxy + cache + transform)  →  Vanilla JS frontend (Leaflet map)
```

The backend fetches venue data from the USC API, transforms it into the frontend's expected format, and caches results to disk (24h for venues, 7 days for details). The frontend falls back to a static `data/venues_final.json` if the backend is unavailable.

## Linting & Testing

A pre-commit hook (`.git/hooks/pre-commit`) runs all checks automatically before each commit.

### Backend (Python)
- **Linter/formatter**: Ruff — configured in `backend/pyproject.toml`
- **Tests**: Pytest — outcome-based tests in `backend/test_server.py`
- Install dev dependencies: `cd backend && uv sync --group dev`
- Run manually:
  ```bash
  cd backend
  uv run ruff check .
  uv run ruff format --check .
  uv run pytest test_server.py -v
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

## Things to Watch Out For

### Rate limiting

The backend uses `asyncio.Semaphore(5)` for concurrent API calls and fetches details in batches of 50. Don't increase concurrency — USC could start blocking.

### Coordinate gaps

~13 out of 1,931 venues had no coordinates. These show in the venue list but not on the map. The `has_coordinates` field tracks this.

## Data Schema

Each venue (as returned by `GET /api/venues`) has:

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
3. **Update agents.md** — If your change affects architecture, design decisions, data schema, or development workflows, update the relevant section in `agents.md` so future contributors have accurate context.
4. **Check documentation consistency** — Ensure the README, agents.md, and any inline code comments agree with each other and with the actual code.

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
