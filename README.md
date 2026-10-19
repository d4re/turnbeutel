# Turnbeutel

An interactive map app that shows Urban Sports Club venues and courses, filterable by membership type (Corporate / Private) and tier level. Built to answer: *"What venues would I gain or lose by changing my membership tier?"* Venues load per city as you pan the map — any USC city works, Berlin is just the default view.

## Quick Start

The simplest way to run locally (requires `make` and `curl`):

```bash
make serve
```

This installs [uv](https://docs.astral.sh/uv/) if needed, syncs dependencies, and starts the app at **http://localhost:8000**.

### Other make targets

| Command      | Description                              |
|--------------|------------------------------------------|
| `make serve` | Start the app locally (port 8000)        |
| `make test`  | Run backend tests                        |
| `make lint`  | Run ruff (Python) and ESLint (JS)        |
| `make clean` | Remove caches and virtual environment    |

### Docker

Build and run as a container:

```bash
docker compose up --build
```

Or run the prebuilt image published by CI (defaults to the `main` tag; override with `IMAGE_TAG`):

```bash
docker compose -f docker-compose.prod.yml up -d
IMAGE_TAG=1.1.0 docker compose -f docker-compose.prod.yml up -d
```

#### Releases & image tags

Releases are cut automatically: when a push to `main` contains [Conventional Commits](https://www.conventionalcommits.org/) that warrant one, CI bumps the version (`feat` → minor, `fix`/`perf` → patch, `!` / `BREAKING CHANGE` → major), publishes the image and creates the matching `vX.Y.Z` GitHub release. Commits like `chore`, `docs` or `ci` don't release on their own.

| Image tag | Tracks |
|-----------|--------|
| `1.2.3`, `1.2`, `1` | Releases — pin these (ideally with a digest) for Renovate / auto-updates |
| `main`, `latest` | Every push to `main` |
| `sha-<commit>` | A single commit |

The app is available at **http://localhost:8000**. Venue cache is persisted in a Docker volume across restarts.

### Agent sandbox (Docker Sandboxes)

The development environment for coding agents is a [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) mixin kit in `.sbx/kit/`, layered on the built-in `claude` agent:

```bash
sbx create --name turnbeutel --kit ./.sbx/kit claude .
sbx run turnbeutel
```

The kit installs Node 24, headless Chromium (Playwright), zsh and dev tools on top of the stock template (Python 3.14, uv, gh, Docker), and declares the network egress the project needs (USC API, GitHub, npm/PyPI/uv, Ubuntu/Node/Playwright downloads, VS Code server). Kit changes take effect on sandbox re-creation.

### Manual start (without make)

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Start the backend (serves both API and frontend)
cd backend
uv run uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

## How to Use

- **Corporate / Private toggle** — switches between corporate tier names (S, M Pro, L Pro, XL Pro) and private ones (Essential, Classic, Premium, Max). Pick whichever matches your membership.
- **Tier range slider** — has two handles. Drag both to the same tier to see only that tier's exclusive venues. Example: both handles on "L Pro" shows the ~340 venues you'd gain by upgrading from M Pro.
- **District / Activity / Search** — narrow down further.
- **Map markers** are color-coded by minimum required tier (green → red).
- **Click a venue** on the map or in the list to see visit limits per tier (loaded on demand from the API).
- **Courses tab** — switch to browse classes/courses in the visible cities for a specific day or range (tap a date chip, or drag across chips for a range). Filter by time of day, category, free text, free-spots-only, and PLUS-only. The sidebar shows courses sorted by time; the map shows a marker per venue hosting matching courses, with the full schedule in each popup.
- **Click a course** to expand it in place: description, rating, free-cancellation window, which tiers the class is included in, and a link to the venue on USC (loaded on demand). Badges flag on-site fees (`+15 € on site`, parsed from USC's text or the title) and classes your plan doesn't cover (`⬆ L Pro`).
- **You** (sidebar header) — set your own plan (corporate/private + tier), your home and your bike speed. The plan drives the `⬆` badges and the default Corporate/Private toggle; without one, the badge marks classes that need a higher tier than their venue. Set home by address search, "Pick on map", right-click/long-press on the map, or by dragging the 🏠 pin.
- **Travel times** — with a home set, venues and courses show `🚲 14 min · 🚆 22 min (M4 → U8)`. Bike minutes come from the route distance at your chosen speed; transit is a typical weekday-evening journey (Berlin/Brandenburg only). An expanded course shows the exact connection that gets you there 5 minutes before it starts, including when to leave. Times fill in gradually in the background.
- **Zoom out** to see pins for every USC city; click one to fly there and load its venues.
- **On a phone** the sidebar becomes a bottom sheet over a full-screen map: drag the handle (or tap it) to switch between peek, half, and full height, and open filters via the **Filters** button in the sheet header (the badge shows how many filters are active).

## Project Structure

```
backend/
  server.py             # FastAPI app — proxies USC API, transforms, caches, serves frontend
  storage.py            # SQLite persistence layer (venues, courses, cities, categories, settings)
  travel.py             # Travel times: OSRM bike distances, VBB transit, Nominatim, background worker
  models.py             # Pydantic domain models
  pyproject.toml        # Python dependencies (managed by uv)
  test_server.py        # Backend tests: pure functions + endpoints (pytest)
  test_storage.py       # Backend tests: SQLite layer (pytest)
  test_travel.py        # Backend tests: travel helpers, clients and worker (pytest)
  cache/                # Persistent SQLite cache, usc.db (gitignored)

frontend/
  index.html            # Single-page app
  app.js                # Map, filters, rendering logic
  style.css             # Styling

data/                   # Historical scraped snapshots (gitignored, not used by the app)

Dockerfile              # Production container image
docker-compose.yml      # Single-command container deployment (local build)
docker-compose.prod.yml # Same, but pulls the CI-published image from ghcr.io
Makefile                # Local dev shortcuts (serve, test, lint, clean)
.sbx/kit/               # Docker Sandboxes kit for the agent dev environment
```

## API Endpoints

The backend exposes:

- `GET /api/cities` — Index of all USC cities with centroids/bounding boxes (cached 7 days)
- `GET /api/venues?city_ids=1&city_ids=2` — Venues for one or more cities, grouped per city (cached 24h per city)
- `GET /api/venues/{id}` — Single venue detail with visit limits per tier (cached 7 days)
- `GET /api/categories` — Activity categories (cached 7 days)
- `GET /api/courses?start_date=YYYY-MM-DD&days=N&city_ids=1` — Courses for a date or range (1–14 days) in one or more cities, including class-level tiers and on-site fees. Cached per (city, day) with a 2-day TTL so overlapping queries hit the cache.
- `GET /api/courses/{id}` — One course's description, rating, free-cancellation hours and booking type (fetched on demand, cached 7 days)
- `GET /api/settings` / `PUT /api/settings` — The user's settings: plan, home and bike speed. Stored server-side for a single user.
- `GET /api/geocode?q=...&viewbox=...` — Address search via Nominatim (cached 30 days)
- `POST /api/travel` `{"venue_ids": [...], "origin"?: {"lat", "lng"}}` — Cached bike distance and typical transit time per venue from home (or `origin`), plus the ids still being computed in the background (`pending`)
- `GET /api/travel/exact?venue_id=...&date=YYYY-MM-DD&start=HH:MM` — The transit connection arriving 5 minutes before a class starts (waits up to 10 s; cached 24 h)
- `GET /api/health` — Health check (used by Docker HEALTHCHECK and load balancers)

## Cache

All fetched data (cities, venues, venue details, courses, course details, categories, travel times, geocoding results) and your settings are stored in a single SQLite database, `backend/cache/usc.db`. The cache survives server restarts. To force a refresh, delete the database (`make clean` or `rm backend/cache/usc.db*`) and restart the backend — this also clears your settings (plan, home, bike speed).

Travel times and address search need outbound access to `routing.openstreetmap.de` (OSRM), `v6.vbb.transport.rest` (VBB) and `nominatim.openstreetmap.org`. Your home coordinate is sent only to those services and is never logged.

When running via Docker, the cache is stored in a named volume (`venue-cache`) and persists across container rebuilds.

### Background prefetch

Set `PREFETCH_CITIES` (comma-separated city names or USC ids, e.g. `Berlin` or `1,Hamburg`) to keep those cities warm: the backend refreshes their venues, venue details and courses for the full 14-day date strip in the background, so visitors never wait on a live USC fetch or see expired data. `PREFETCH_RADIUS_KM` additionally warms every USC city whose centre lies within that distance of a listed one (Berlin's default map view also loads ~20 small surrounding towns such as Teltow or Bernau). A cycle runs at startup and then every `PREFETCH_INTERVAL_MINUTES` (default 60); each cycle only re-fetches what would expire before the next one. Every course day is re-fetched about every 2 days (picking up classes added at short notice), with the 14 days staggered ~3.4 h apart so USC sees a steady trickle instead of one big burst. Unset (the default for `make serve`) disables it; both Docker Compose files default to `Berlin` with a 30 km radius.

```bash
PREFETCH_CITIES=Berlin PREFETCH_RADIUS_KM=30 make serve
```

The frontend additionally keeps an in-memory cache of loaded cities and dates with a soft 60-minute TTL: panning back to already-loaded data within the TTL makes no requests, while stale data is transparently re-fetched (and replaced) the next time the viewport or date selection touches it. A page reload always re-fetches.

## License

[MIT](LICENSE)
