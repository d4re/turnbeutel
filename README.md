# USC Berlin Venue Explorer

An interactive map app that shows all Urban Sports Club venues in Berlin, filterable by membership type (Corporate / Private) and tier level. Built to answer: *"What venues would I gain or lose by changing my membership tier?"*

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
- **Courses tab** — switch to browse classes/courses across Berlin for a specific day or range. Filter by time of day, category, free text, free-spots-only, and PLUS-only. The sidebar shows courses sorted by time; the map shows a marker per venue hosting matching courses, with the full schedule in each popup.

## Project Structure

```
backend/
  server.py             # FastAPI app — proxies USC API, transforms, caches, serves frontend
  pyproject.toml        # Python dependencies (managed by uv)
  test_server.py        # Backend tests (pytest)
  cache/                # Persistent JSON cache (gitignored)

frontend/
  index.html            # Single-page app
  app.js                # Map, filters, rendering logic
  style.css             # Styling

data/                   # Static fallback data (gitignored)
  venues_final.json     # Pre-scraped dataset, used when backend is down

Dockerfile              # Production container image
docker-compose.yml      # Single-command container deployment
Makefile                # Local dev shortcuts (serve, test, lint, clean)
```

## API Endpoints

The backend exposes:

- `GET /api/venues` — All Berlin venues in the frontend's expected format (cached 24h)
- `GET /api/venues/{id}` — Single venue detail with parsed visit limits (cached 7 days)
- `GET /api/categories` — Activity categories (cached 7 days)
- `GET /api/courses?date=YYYY-MM-DD&days=N` — Courses for a date or range (1–13 days). Cached per-day with a 2-day TTL so overlapping queries hit the cache.
- `GET /api/health` — Health check (used by Docker HEALTHCHECK and load balancers)

## Cache

Venue data is cached to `backend/cache/` as JSON files. The cache survives server restarts. To force a refresh, delete the cache files and restart the backend.

When running via Docker, the cache is stored in a named volume (`venue-cache`) and persists across container rebuilds.
