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
IMAGE_TAG=v1.0.0 docker compose -f docker-compose.prod.yml up -d
```

The app is available at **http://localhost:8000**. Venue cache is persisted in a Docker volume across restarts.

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
- **Zoom out** to see pins for every USC city; click one to fly there and load its venues.

## Project Structure

```
backend/
  server.py             # FastAPI app — proxies USC API, transforms, caches, serves frontend
  storage.py            # SQLite persistence layer (venues, courses, cities, categories)
  models.py             # Pydantic domain models
  pyproject.toml        # Python dependencies (managed by uv)
  test_server.py        # Backend tests: pure functions + endpoints (pytest)
  test_storage.py       # Backend tests: SQLite layer (pytest)
  cache/                # Persistent SQLite cache, usc.db (gitignored)

frontend/
  index.html            # Single-page app
  app.js                # Map, filters, rendering logic
  style.css             # Styling

data/                   # Historical scraped snapshots (gitignored, not used by the app)

Dockerfile              # Production container image
docker-compose.yml      # Single-command container deployment
Makefile                # Local dev shortcuts (serve, test, lint, clean)
```

## API Endpoints

The backend exposes:

- `GET /api/cities` — Index of all USC cities with centroids/bounding boxes (cached 7 days)
- `GET /api/venues?city_ids=1&city_ids=2` — Venues for one or more cities, grouped per city (cached 24h per city)
- `GET /api/venues/{id}` — Single venue detail with parsed visit limits (cached 7 days)
- `GET /api/categories` — Activity categories (cached 7 days)
- `GET /api/courses?start_date=YYYY-MM-DD&days=N&city_ids=1` — Courses for a date or range (1–14 days) in one or more cities. Cached per (city, day) with a 2-day TTL so overlapping queries hit the cache.
- `GET /api/health` — Health check (used by Docker HEALTHCHECK and load balancers)

## Cache

All fetched data (cities, venues, venue details, courses, categories) is cached in a single SQLite database, `backend/cache/usc.db`. The cache survives server restarts. To force a refresh, delete the database (`make clean` or `rm backend/cache/usc.db*`) and restart the backend.

When running via Docker, the cache is stored in a named volume (`venue-cache`) and persists across container rebuilds.

## License

[MIT](LICENSE)
