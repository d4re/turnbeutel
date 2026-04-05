# USC Berlin Venue Explorer

An interactive map app that shows all Urban Sports Club venues in Berlin, filterable by membership type (Corporate / Private) and tier level. Built to answer: *"What venues would I gain or lose by changing my membership tier?"*

## Quick Start

### 1. Install uv (if not already installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Start the backend

```bash
cd backend
uv run uvicorn server:app --reload --port 8000
```

`uv` automatically creates a virtual environment and installs dependencies on first run.

The backend proxies the USC API, transforms the data, and caches it to disk. On first run it fetches all ~2,600 Berlin venues (~5 seconds), then enriches each with visit limit details in the background.

### 3. Open the frontend

Open `http://localhost:8080/frontend/index.html` in your browser (or serve with any static file server).

The frontend fetches live data from the backend at `localhost:8000`. If the backend is unavailable, it falls back to the static `data/venues_final.json` file.

## How to Use

- **Corporate / Private toggle** — switches between corporate tier names (S, M Pro, L Pro, XL Pro) and private ones (Essential, Classic, Premium, Max). Pick whichever matches your membership.
- **Tier range slider** — has two handles. Drag both to the same tier to see only that tier's exclusive venues. Example: both handles on "L Pro" shows the ~340 venues you'd gain by upgrading from M Pro.
- **District / Activity / Search** — narrow down further.
- **Map markers** are color-coded by minimum required tier (green → red).
- **Click a venue** on the map or in the list to see visit limits per tier (loaded on demand from the API).

## Project Structure

```
backend/
  server.py             # FastAPI proxy — fetches from USC API, transforms, caches
  pyproject.toml        # Python dependencies (managed by uv)
  cache/                # Persistent JSON cache (gitignored)

frontend/
  index.html            # Single-page app
  app.js                # Map, filters, rendering logic
  style.css             # Styling

data/                   # Static fallback data (gitignored)
  venues_final.json     # Pre-scraped dataset, used when backend is down
```

## API Endpoints

The backend exposes:

- `GET /api/venues` — All Berlin venues in the frontend's expected format (cached 24h)
- `GET /api/venues/{id}` — Single venue detail with parsed visit limits (cached 7 days)
- `GET /api/categories` — Activity categories (cached 7 days)

## Cache

Venue data is cached to `backend/cache/` as JSON files. The cache survives server restarts. To force a refresh, delete the cache files and restart the backend.
