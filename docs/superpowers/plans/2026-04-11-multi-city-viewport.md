# Multi-City Viewport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-city (Berlin-only) USC explorer into a multi-city app where the map viewport decides which cities to query, reusing the already multi-city-aware storage layer.

**Architecture:** Backend `/api/venues` and `/api/courses` become multi-city, taking a required `city_ids` list and returning city-grouped payloads. A new `/api/cities` endpoint exposes the existing in-memory cities index. The frontend fetches `/api/cities` once, then on every debounced map `moveend`/`zoomend` computes which cities intersect the viewport (using each city's stored bbox or a centroid fallback) and fetches venues/courses only for cities it hasn't loaded yet. Below a zoom threshold, it shows city pins instead of fetching.

**Tech Stack:** FastAPI, Pydantic, SQLite (existing), vanilla JS + Leaflet (existing), pytest + `fastapi.testclient.TestClient` (new for server tests).

**Reference spec:** `/home/node/.claude/plans/ticklish-wobbling-lamport.md` (brainstorming output — user-approved).

---

## File Structure

**Backend**
- Modify: `backend/server.py` — config rename, new `/api/cities`, multi-city `/api/venues` and `/api/courses`, tweak venue-cache invalidation in `get_venue_detail`.
- Modify: `backend/models.py` — add `CitiesResponse`, `CityVenuesEntry`, `MultiCityVenuesPayload`, `CityCoursesEntry`, `MultiCityCoursesResponse`; extend `CourseFetchError` with `city_id`.
- Modify: `backend/test_server.py` — add a `TestClient`-backed section with fixtures that monkeypatch USC fetchers and seed storage in `tmp_path`.
- No change: `backend/storage.py` (schema and lazy bbox derivation already in place).
- No change: `backend/test_storage.py` (cities/bbox round-trip already covered at `test_storage.py:118-160`).

**Frontend**
- Modify: `frontend/app.js` — new module-level state, `/api/cities` bootstrap, viewport handler, `citiesInViewport` helper, city-pin layer, per-`(city_id, date)` courses cache.
- Modify: `frontend/index.html` — drop "Berlin" from title/header.

**Docs**
- This plan lives at `docs/superpowers/plans/2026-04-11-multi-city-viewport.md`.

---

## Conventions used in this plan

- All commands assume CWD is `/workspace` unless otherwise noted.
- Backend tests run with `make test` (which wraps `cd backend && uv run pytest test_server.py`). To run a single test: `cd backend && uv run pytest test_server.py::test_name -v`.
- Backend test files run from `/workspace/backend`, so imports are `from server import ...`, `from models import ...`, `import storage`.
- Commit messages follow the existing conventional-commit style visible in `git log` (e.g. `feat(backend): ...`, `refactor(backend): ...`).

---

## Task 1 — Backend prep: rename `BERLIN_CITY_ID` → `DEFAULT_CITY_ID`, shorten cities TTL, per-city enrichment guard, venue-fetch cap

**Files:**
- Modify: `backend/server.py:48,60,84,448-451,486-487,495,557,592`

- [ ] **Step 1: Rename the constant and adjust TTL**

In `backend/server.py` replace line 48:

```python
BERLIN_CITY_ID = 1
```

with:

```python
DEFAULT_CITY_ID = 1
```

And replace line 60:

```python
CITIES_TTL = 30 * 24 * 3600
```

with:

```python
CITIES_TTL = 7 * 24 * 3600
```

- [ ] **Step 2: Update every remaining reference**

Replace the three remaining `BERLIN_CITY_ID` usages:

- `server.py:495` inside `get_venues`: `city_id = BERLIN_CITY_ID` → `city_id = DEFAULT_CITY_ID`
- `server.py:557` inside `get_venue_detail`: `_invalidate_venues_cache(BERLIN_CITY_ID)` → `_invalidate_venues_cache(DEFAULT_CITY_ID)` (this line gets rewritten again in Task 6 — keep it simple for now)
- `server.py:592` inside `get_courses`: `city_id = BERLIN_CITY_ID` → `city_id = DEFAULT_CITY_ID`

Verify no stragglers:

```bash
grep -n BERLIN_CITY_ID backend/server.py backend/test_server.py backend/test_storage.py
```

Expected: no output.

- [ ] **Step 3: Make venue-detail enrichment per-city**

`enrich_venue_details` is currently gated by a single module-global bool
(`server.py:84`, `_enrichment_running`). In single-city mode that's fine, but
once Task 5 fires enrichment per city, the bool lets only the *first* city
enrich — every later city's background task returns immediately and its
`visit_limits` never populate (until the 24h venue cache expires, leaving the
inline list visit-limit line at `app.js:551-557` blank for those cities).
Switch the guard to a per-city set.

Replace `server.py:84`:

```python
_enrichment_running = False
```

with:

```python
_enrichment_cities: set[int] = set()
```

Then in `enrich_venue_details` replace the guard preamble (`server.py:448-451`):

```python
    global _enrichment_running
    if _enrichment_running:
        return
    _enrichment_running = True
```

with:

```python
    if city_id in _enrichment_cities:
        return
    _enrichment_cities.add(city_id)
```

and replace the `finally` (`server.py:486-487`):

```python
    finally:
        _enrichment_running = False
```

with:

```python
    finally:
        _enrichment_cities.discard(city_id)
```

- [ ] **Step 4: Add a global cap on concurrent venue API fetches**

So a pan across many cities can't fan out into unbounded simultaneous USC venue
pulls, add a process-wide semaphore. It bounds only live API fetches — cache
reads (in-memory or DB) stay unthrottled. Task 5 wraps the cold path with it.

In the config block (near `server.py:49`, after `PAGE_SIZE = 100`), add:

```python
MAX_CONCURRENT_VENUE_FETCHES = 3
```

In the in-memory state block (near `server.py:82-84`, beside the other
module-level state), add:

```python
_venue_fetch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_VENUE_FETCHES)
```

- [ ] **Step 5: Run existing tests — must still pass**

```bash
make test
```

Expected: all existing tests pass (no behavioral change for a single city).

- [ ] **Step 6: Commit**

```bash
git add backend/server.py
git commit -m "refactor(backend): DEFAULT_CITY_ID rename, 7d cities TTL, per-city enrichment guard, venue-fetch cap"
```

---

## Task 2 — Add multi-city response models

**Files:**
- Modify: `backend/models.py`

- [ ] **Step 1: Extend `CourseFetchError` and add new response models**

Open `backend/models.py`. Replace the `CourseFetchError` class (currently `models.py:89-91`) with:

```python
class CourseFetchError(BaseModel):
    date: str
    reason: str
    city_id: int | None = None
```

Then append the following classes to the end of the file (after the existing `City` class at `models.py:104-117`):

```python
class CitiesResponse(BaseModel):
    """Response shape for GET /api/cities."""

    cities: list[City]
    default_city_id: int


class CityVenuesEntry(BaseModel):
    """One city's slice of a multi-city venues payload."""

    city_id: int
    city_name: str
    fetched_at: float
    total: int
    with_coordinates: int
    venues: list[Venue]


class MultiCityVenuesPayload(BaseModel):
    """Response shape for GET /api/venues (city-grouped)."""

    cities: list[CityVenuesEntry]
    tier_config: TierConfig


class CityCoursesEntry(BaseModel):
    """One city's slice of a multi-city courses payload."""

    city_id: int
    city_name: str
    date_from: str
    date_to: str
    total: int
    courses: list[Course]


class MultiCityCoursesResponse(BaseModel):
    """Response shape for GET /api/courses (city-grouped)."""

    cities: list[CityCoursesEntry]
    date_from: str
    date_to: str
    errors: list[CourseFetchError]
```

- [ ] **Step 2: Verify imports still resolve**

```bash
cd backend && uv run python -c "from models import CitiesResponse, MultiCityVenuesPayload, MultiCityCoursesResponse, CityVenuesEntry, CityCoursesEntry, CourseFetchError; print('ok')"
```

Expected output: `ok`.

- [ ] **Step 3: Run existing tests (nothing should break)**

```bash
make test
```

Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add backend/models.py
git commit -m "feat(backend): add multi-city response models and city_id on CourseFetchError"
```

---

## Task 3 — Add a `TestClient` fixture for server endpoint tests

This task introduces the first `TestClient`-based tests in this project, so it also sets up reusable fixtures. Tasks 4, 5, and 6 build on these fixtures.

**Files:**
- Modify: `backend/test_server.py`

- [ ] **Step 1: Add imports and a shared server test fixture**

At the top of `backend/test_server.py` (after the existing `import time` on line 3), add:

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import storage
from models import City, Venue, VenueAddress
```

Then append a new section at the end of the file:

```python
# ── Server endpoint fixtures ───────────────────────────────────────────────


@pytest.fixture
def seeded_client(tmp_path: Path, monkeypatch):
    """FastAPI TestClient with a temp SQLite DB pre-seeded with two cities.

    - `fetch_all_cities` and `fetch_venue_detail` are monkeypatched to no-ops so
      neither lifespan nor background enrichment hits USC.
    - `fetch_all_venue_pages` and `fetch_courses_for_date` are left as stubs
      that individual tests override (see `monkeypatch.setattr` calls inside
      each test).
    - Module-level caches (`_venues_response_cache`, `_enrichment_cities`) are
      cleared on setup so tests don't leak state into each other.
    - The temp DB is seeded with cities 1 ("Berlin") and 2 ("Hamburg") so
      `_cities_index` has content after lifespan runs `storage.list_cities()`.
    """
    storage.close()
    db_path = tmp_path / "test.db"
    storage.init(db_path)
    storage.upsert_cities(
        [
            City(
                id=1,
                name="Berlin",
                country_code="DE",
                centroid_lat=52.52,
                centroid_lng=13.405,
            ),
            City(
                id=2,
                name="Hamburg",
                country_code="DE",
                centroid_lat=53.55,
                centroid_lng=9.99,
            ),
        ],
        fetched_at=time.time(),
    )
    storage.close()

    monkeypatch.setattr(server, "DB_PATH", db_path)

    # Reset module-level caches so state doesn't leak between tests.
    server._venues_response_cache.clear()
    server._enrichment_cities.clear()

    async def _fake_cities():
        return []

    async def _fake_venue_detail(venue_id):
        return {}

    monkeypatch.setattr(server, "fetch_all_cities", _fake_cities)
    monkeypatch.setattr(server, "fetch_venue_detail", _fake_venue_detail)

    with TestClient(server.app) as client:
        yield client

    storage.close()


def _make_venue(venue_id: str, name: str, lat: float, lng: float) -> Venue:
    """Minimal Venue factory for endpoint tests."""
    return Venue(
        name=name,
        slug=name.lower().replace(" ", "-"),
        url=f"https://example.com/{venue_id}",
        tiers_private=["Essential"],
        tiers_corporate=["S"],
        min_tier_private="Essential",
        min_tier_corporate="S",
        activities=["Yoga"],
        district="",
        street="",
        is_plus=False,
        address_id=venue_id,
        lat=lat,
        lng=lng,
        address=VenueAddress(),
        rating=None,
        review_count=None,
        is_online=False,
        has_coordinates=True,
    )
```

- [ ] **Step 2: Add a smoke test that exercises the fixture**

Append:

```python
def test_seeded_client_boots_and_has_cities_index(seeded_client):
    # Lifespan ran; _cities_index should be populated from the seeded DB.
    assert len(server._cities_index) == 2
    assert {c.id for c in server._cities_index} == {1, 2}
```

- [ ] **Step 3: Run the smoke test**

```bash
cd backend && uv run pytest test_server.py::test_seeded_client_boots_and_has_cities_index -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/test_server.py
git commit -m "test(backend): add TestClient fixture with seeded SQLite and monkeypatched USC"
```

---

## Task 4 — Implement `GET /api/cities`

**Files:**
- Modify: `backend/server.py` (new endpoint near the existing `/api/health`)
- Modify: `backend/test_server.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/test_server.py`:

```python
def test_get_cities_returns_seeded_list(seeded_client):
    resp = seeded_client.get("/api/cities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_city_id"] == 1
    ids = {c["id"] for c in data["cities"]}
    assert ids == {1, 2}
    berlin = next(c for c in data["cities"] if c["id"] == 1)
    assert berlin["name"] == "Berlin"
    assert berlin["centroid_lat"] == 52.52
    assert berlin["centroid_lng"] == 13.405
```

- [ ] **Step 2: Run it — must fail**

```bash
cd backend && uv run pytest test_server.py::test_get_cities_returns_seeded_list -v
```

Expected: FAIL with `404` (endpoint doesn't exist yet).

- [ ] **Step 3: Add the endpoint**

In `backend/server.py`, add `CitiesResponse` to the existing model imports (the big `from models import (` block at `server.py:24-35`):

```python
from models import (
    CitiesResponse,
    City,
    Course,
    CourseFetchError,
    CoursesResponse,
    TierConfig,
    Venue,
    VenueAddress,
    VenueDetail,
    VenuesPayload,
    VisitLimits,
)
```

Then, just above `@app.get("/api/health")` (currently around `server.py:635`), insert:

```python
@app.get("/api/cities", response_model=CitiesResponse)
async def get_cities():
    """Return the cached cities index, seeded from USC on startup."""
    return CitiesResponse(cities=list(_cities_index), default_city_id=DEFAULT_CITY_ID)
```

- [ ] **Step 4: Run the test — must pass**

```bash
cd backend && uv run pytest test_server.py::test_get_cities_returns_seeded_list -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/server.py backend/test_server.py
git commit -m "feat(backend): add GET /api/cities endpoint"
```

---

## Task 5 — `GET /api/venues` accepts required `city_ids` and returns grouped payload

This is the biggest backend task. It rewrites the endpoint body. The existing helpers (`fetch_all_venue_pages`, `storage.upsert_venues`, `storage.get_venues_payload`, `_venues_response_cache`, `enrich_venue_details`) are all already per-city, so the work is a concurrent fan-out (`asyncio.gather`) plus a new response shape. Live USC pulls are bounded by `_venue_fetch_semaphore` (Task 1, Step 4); the per-city enrichment guard (Task 1, Step 3) makes firing `enrich_venue_details` per city safe.

**Files:**
- Modify: `backend/server.py:493-530`
- Modify: `backend/test_server.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/test_server.py`:

```python
def test_get_venues_requires_city_ids(seeded_client):
    resp = seeded_client.get("/api/venues")
    assert resp.status_code == 400
    assert "city_ids" in resp.json()["detail"].lower()


def test_get_venues_multi_city_grouped(seeded_client, monkeypatch):
    calls: list[int] = []

    async def fake_fetch(usc_city_id: int):
        calls.append(usc_city_id)
        if usc_city_id == 1:
            return [
                {
                    "name": "Berlin Studio",
                    "slug": "berlin-studio",
                    "url": "https://example.com/1",
                    "tiers": {"private": ["Essential"], "corporate": ["S"]},
                    "activities": ["Yoga"],
                    "addressId": "b1",
                    "location": {"latitude": 52.52, "longitude": 13.4},
                    "isOnline": 0,
                    "isPlus": 0,
                }
            ]
        return [
            {
                "name": "Hamburg Studio",
                "slug": "hamburg-studio",
                "url": "https://example.com/2",
                "tiers": {"private": ["Essential"], "corporate": ["S"]},
                "activities": ["Yoga"],
                "addressId": "h1",
                "location": {"latitude": 53.55, "longitude": 9.99},
                "isOnline": 0,
                "isPlus": 0,
            }
        ]

    monkeypatch.setattr(server, "fetch_all_venue_pages", fake_fetch)

    resp = seeded_client.get("/api/venues?city_ids=1&city_ids=2")
    assert resp.status_code == 200
    data = resp.json()

    assert "tier_config" in data
    assert len(data["cities"]) == 2
    by_id = {c["city_id"]: c for c in data["cities"]}
    assert by_id[1]["city_name"] == "Berlin"
    assert by_id[2]["city_name"] == "Hamburg"
    assert by_id[1]["total"] == 1
    assert by_id[2]["total"] == 1
    assert by_id[1]["venues"][0]["name"] == "Berlin Studio"
    assert by_id[2]["venues"][0]["name"] == "Hamburg Studio"
    # Each city fetched exactly once.
    assert sorted(calls) == [1, 2]


def test_get_venues_second_call_is_cached(seeded_client, monkeypatch):
    calls: list[int] = []

    async def fake_fetch(usc_city_id: int):
        calls.append(usc_city_id)
        return [
            {
                "name": "Studio A",
                "slug": "studio-a",
                "url": "https://example.com/a",
                "tiers": {"private": ["Essential"], "corporate": ["S"]},
                "activities": ["Yoga"],
                "addressId": "a1",
                "location": {"latitude": 52.52, "longitude": 13.4},
                "isOnline": 0,
                "isPlus": 0,
            }
        ]

    monkeypatch.setattr(server, "fetch_all_venue_pages", fake_fetch)

    first = seeded_client.get("/api/venues?city_ids=1")
    assert first.status_code == 200

    second = seeded_client.get("/api/venues?city_ids=1")
    assert second.status_code == 200

    # Exactly one upstream call — the second request is served from the
    # in-memory `_venues_response_cache`.
    assert calls == [1]
```

Expected behavior note: the unbracketed `tiers`, `activities`, `location`, etc. in the fake venue dicts must match `transform_venue`'s expectations. See `transform_venue` (currently around `server.py:200-270` — read it first if any field is unclear).

- [ ] **Step 2: Run the tests — must fail**

```bash
cd backend && uv run pytest test_server.py::test_get_venues_requires_city_ids test_server.py::test_get_venues_multi_city_grouped test_server.py::test_get_venues_second_call_is_cached -v
```

Expected: all three FAIL (endpoint still returns the old single-city shape / doesn't enforce `city_ids`).

- [ ] **Step 3: Import `Query` and the new models in `server.py`**

At `server.py:18`, extend the FastAPI import:

```python
from fastapi import FastAPI, HTTPException, Query
```

Update the models import block at `server.py:24-35` to also include:

```python
from models import (
    CitiesResponse,
    City,
    CityCoursesEntry,
    CityVenuesEntry,
    Course,
    CourseFetchError,
    CoursesResponse,
    MultiCityCoursesResponse,
    MultiCityVenuesPayload,
    TierConfig,
    Venue,
    VenueAddress,
    VenueDetail,
    VenuesPayload,
    VisitLimits,
)
```

- [ ] **Step 4: Add a helper that loads one city's venues**

Just above `@app.get("/api/venues", ...)` at `server.py:493`, add:

```python
async def _load_venues_for_city(city_id: int) -> CityVenuesEntry | None:
    """Resolve one city's venues, honoring memory and DB caches.

    Returns None if the city is not in `_cities_index` (unknown id).
    """
    city = next((c for c in _cities_index if c.id == city_id), None)
    if city is None:
        return None

    now = time.time()

    cached = _venues_response_cache.get(city_id)
    if cached and (now - cached[0]) < VENUES_TTL:
        payload = cached[1]
    else:
        fetched_at = await run_in_threadpool(storage.get_venues_fetched_at, city_id)
        if fetched_at is not None and (now - fetched_at) < VENUES_TTL:
            payload = await run_in_threadpool(storage.get_venues_payload, city_id)
            if payload is not None:
                payload.tier_config = TIER_CONFIG
                _venues_response_cache[city_id] = (now, payload)
        else:
            payload = None

        if payload is None:
            # Bound concurrent live USC pulls globally. The cache reads above are
            # unthrottled; only the cold fetch acquires the semaphore.
            async with _venue_fetch_semaphore:
                # Re-check the cache: another request may have fetched this same
                # city while we were queued on the semaphore.
                cached = _venues_response_cache.get(city_id)
                if cached and (time.time() - cached[0]) < VENUES_TTL:
                    payload = cached[1]
                else:
                    raw_venues = await fetch_all_venue_pages(usc_city_id=city_id)
                    venues = [transform_venue(v) for v in raw_venues]
                    venues = [v for v in venues if v.name and (v.tiers_private or v.tiers_corporate)]
                    venues.sort(key=lambda v: v.name)

                    total = len(venues)
                    with_coords = sum(1 for v in venues if v.has_coordinates)
                    now = time.time()
                    await run_in_threadpool(
                        storage.upsert_venues, city_id, venues, now, total, with_coords
                    )

                    payload = await run_in_threadpool(storage.get_venues_payload, city_id)
                    assert payload is not None
                    payload.tier_config = TIER_CONFIG
                    _venues_response_cache[city_id] = (now, payload)
                    asyncio.create_task(enrich_venue_details(city_id))

    return CityVenuesEntry(
        city_id=city_id,
        city_name=city.name,
        fetched_at=payload.fetched_at,
        total=payload.total_venues,
        with_coordinates=payload.venues_with_coords,
        venues=payload.venues,
    )
```

- [ ] **Step 5: Replace the `/api/venues` endpoint body**

Replace the whole `get_venues` function (currently `server.py:493-530`) with:

```python
@app.get("/api/venues", response_model=MultiCityVenuesPayload)
async def get_venues(city_ids: list[int] | None = Query(None)):
    if not city_ids:
        raise HTTPException(status_code=400, detail="city_ids query parameter is required")

    # Dedupe while preserving order.
    seen: set[int] = set()
    ordered: list[int] = []
    for cid in city_ids:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)

    # Resolve cities concurrently: cached cities return instantly, while live
    # USC pulls are bounded by `_venue_fetch_semaphore`. gather preserves the
    # input order, so `entries` stays in `ordered` order.
    results = await asyncio.gather(*[_load_venues_for_city(cid) for cid in ordered])
    entries = [e for e in results if e is not None]

    return MultiCityVenuesPayload(cities=entries, tier_config=TIER_CONFIG)
```

- [ ] **Step 6: Run the tests — must pass**

```bash
cd backend && uv run pytest test_server.py::test_get_venues_requires_city_ids test_server.py::test_get_venues_multi_city_grouped test_server.py::test_get_venues_second_call_is_cached -v
```

Expected: all three PASS. If `test_get_venues_multi_city_grouped` fails because `transform_venue` drops a venue (e.g. missing required field), read `transform_venue` in `server.py` and extend the fake venue dict in the test with the missing fields — do **not** weaken the transform.

- [ ] **Step 7: Run the full backend suite**

```bash
make test
```

Expected: every test passes (nothing else should regress).

- [ ] **Step 8: Commit**

```bash
git add backend/server.py backend/test_server.py
git commit -m "feat(backend): /api/venues accepts city_ids and returns grouped payload"
```

---

## Task 6 — `GET /api/courses` accepts required `city_ids` and returns grouped payload

**Files:**
- Modify: `backend/server.py:576-632`
- Modify: `backend/test_server.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/test_server.py`:

```python
def test_get_courses_requires_city_ids(seeded_client):
    resp = seeded_client.get("/api/courses?start_date=2026-04-11")
    assert resp.status_code == 400
    assert "city_ids" in resp.json()["detail"].lower()


def test_get_courses_multi_city_grouped(seeded_client, monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake_fetch(date_str, usc_city_id, client, semaphore):
        calls.append((date_str, usc_city_id))
        from models import Course

        return [
            Course(
                id=1000 * usc_city_id,
                date=date_str,
                title=f"Class in city {usc_city_id}",
                start_time="09:00",
                end_time="10:00",
                venue_id=f"v-{usc_city_id}",
                venue_name=f"Venue {usc_city_id}",
                lat=52.52 if usc_city_id == 1 else 53.55,
                lng=13.4 if usc_city_id == 1 else 9.99,
                district="",
                category="Yoga",
                category_id=1,
                teacher="",
                free_spots=5,
                max_spots=10,
                is_online=False,
                is_plus=False,
            )
        ]

    monkeypatch.setattr(server, "fetch_courses_for_date", fake_fetch)

    resp = seeded_client.get(
        "/api/courses?start_date=2026-04-11&days=1&city_ids=1&city_ids=2"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["date_from"] == "2026-04-11"
    assert data["date_to"] == "2026-04-11"
    assert len(data["cities"]) == 2
    by_id = {c["city_id"]: c for c in data["cities"]}
    assert by_id[1]["total"] == 1
    assert by_id[2]["total"] == 1
    assert by_id[1]["courses"][0]["title"] == "Class in city 1"
    assert by_id[2]["courses"][0]["title"] == "Class in city 2"
    # One fetch per (city, date).
    assert sorted(calls) == [("2026-04-11", 1), ("2026-04-11", 2)]
```

- [ ] **Step 2: Run the tests — must fail**

```bash
cd backend && uv run pytest test_server.py::test_get_courses_requires_city_ids test_server.py::test_get_courses_multi_city_grouped -v
```

Expected: FAIL (endpoint still uses `DEFAULT_CITY_ID` and returns the old shape).

- [ ] **Step 3: Replace the endpoint body**

Replace the whole `get_courses` function (currently `server.py:576-632`) with:

```python
@app.get("/api/courses", response_model=MultiCityCoursesResponse)
async def get_courses(
    start_date: str,
    days: int = 1,
    city_ids: list[int] | None = Query(None),
):
    """Get courses across one or more cities for a date range.

    Each (city_id, date) pair is cached independently with a 2-day TTL.
    """
    if not city_ids:
        raise HTTPException(status_code=400, detail="city_ids query parameter is required")

    try:
        start = date.fromisoformat(start_date)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail="Invalid start_date, expected YYYY-MM-DD",
        ) from e

    days = max(1, min(days, 13))
    date_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    # Dedupe city_ids while preserving order, keep only known ones.
    seen: set[int] = set()
    ordered_cities: list[int] = []
    for cid in city_ids:
        if cid in seen:
            continue
        seen.add(cid)
        if any(c.id == cid for c in _cities_index):
            ordered_cities.append(cid)

    now = time.time()
    entries: list[CityCoursesEntry] = []
    errors: list[CourseFetchError] = []

    # Partition per (city, date): fresh from DB vs stale -> needs fetch.
    # All stale fetches share one Semaphore so fan-out stays bounded.
    semaphore = asyncio.Semaphore(5)
    per_city_merged: dict[int, dict[str, list[Course]]] = {cid: {} for cid in ordered_cities}
    stale_work: list[tuple[int, str]] = []

    for cid in ordered_cities:
        fetches = await run_in_threadpool(storage.get_course_fetches, cid, date_list)
        fresh_dates = [d for d in date_list if d in fetches and (now - fetches[d]) < COURSES_TTL]
        stale_dates = [d for d in date_list if d not in fresh_dates]
        if fresh_dates:
            per_city_merged[cid].update(
                await run_in_threadpool(storage.get_courses_for_dates, cid, fresh_dates)
            )
        for d in stale_dates:
            stale_work.append((cid, d))

    if stale_work:
        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
            results = await asyncio.gather(
                *[
                    fetch_courses_for_date(d, cid, client, semaphore)
                    for (cid, d) in stale_work
                ],
                return_exceptions=True,
            )
        for (cid, d), r in zip(stale_work, results, strict=True):
            if isinstance(r, Exception):
                logger.warning("Failed to fetch courses for city=%s %s: %r", cid, d, r)
                errors.append(CourseFetchError(date=d, reason=str(r), city_id=cid))
                per_city_merged[cid][d] = []
                continue
            per_city_merged[cid][d] = r
            await run_in_threadpool(
                storage.upsert_courses_for_date, cid, d, r, time.time()
            )

    for cid in ordered_cities:
        city = next(c for c in _cities_index if c.id == cid)
        flat: list[Course] = []
        for d in date_list:
            flat.extend(per_city_merged[cid].get(d, []))
        flat.sort(key=lambda c: (c.date, c.start_time))
        entries.append(
            CityCoursesEntry(
                city_id=cid,
                city_name=city.name,
                date_from=date_list[0],
                date_to=date_list[-1],
                total=len(flat),
                courses=flat,
            )
        )

    return MultiCityCoursesResponse(
        cities=entries,
        date_from=date_list[0],
        date_to=date_list[-1],
        errors=errors,
    )
```

- [ ] **Step 4: Run the tests — must pass**

```bash
cd backend && uv run pytest test_server.py::test_get_courses_requires_city_ids test_server.py::test_get_courses_multi_city_grouped -v
```

Expected: PASS.

- [ ] **Step 5: Full suite**

```bash
make test
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add backend/server.py backend/test_server.py
git commit -m "feat(backend): /api/courses accepts city_ids and returns grouped payload"
```

---

## Task 7 — Flush the venues cache on detail updates

The old code only invalidated `_venues_response_cache[DEFAULT_CITY_ID]`, which is wrong once venues from multiple cities live in that dict. Flush the whole dict — it's small and detail updates are infrequent.

**Files:**
- Modify: `backend/server.py:533-558`

- [ ] **Step 1: Replace the single-city invalidation**

In `get_venue_detail` (`server.py:533-558`), find:

```python
    await run_in_threadpool(storage.upsert_venue_detail, vid, detail)
    _invalidate_venues_cache(DEFAULT_CITY_ID)
    return detail
```

Replace with:

```python
    await run_in_threadpool(storage.upsert_venue_detail, vid, detail)
    _venues_response_cache.clear()
    return detail
```

- [ ] **Step 2: Run the full suite**

```bash
make test
```

Expected: all green (no test asserts on which cache key got evicted).

- [ ] **Step 3: Commit**

```bash
git add backend/server.py
git commit -m "fix(backend): flush full venues cache on venue detail updates"
```

---

## Task 8 — Frontend: fetch `/api/cities` and persist map view

Frontend-side tasks don't have an automated test harness in this project, so each frontend task ends with a manual smoke check via `make serve` instead of a pytest run. Use a browser with DevTools open.

**Files:**
- Modify: `frontend/app.js:1-101`
- Modify: `frontend/index.html:6,16`

- [ ] **Step 1: Add module-level state near the existing declarations**

Near `frontend/app.js:7-21`, after the existing `let tierConfig = {};` line, add:

```javascript
let allCities = [];
let defaultCityId = 1;
const loadedVenueCities = new Set();
const loadedCourseCities = new Map(); // city_id -> Set<date-string>
const MIN_FETCH_ZOOM = 9;
const CENTROID_BBOX_HALF_DEG = 0.18; // ~20 km fallback until real bbox is known
const VIEWPORT_DEBOUNCE_MS = 200;
const LAST_VIEW_KEY = "usc.lastView";
let cityPinLayer = null;
let viewportDebounceTimer = null;
```

- [ ] **Step 2: Add helpers for last-view persistence**

Near the other small helpers (around `frontend/app.js:54-56`, just before `// ── Init ──`), add:

```javascript
function loadLastView() {
  try {
    const raw = localStorage.getItem(LAST_VIEW_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (
      typeof parsed.lat === "number" &&
      typeof parsed.lng === "number" &&
      typeof parsed.zoom === "number"
    ) {
      return parsed;
    }
  } catch {}
  return null;
}

function saveLastView() {
  const c = map.getCenter();
  localStorage.setItem(
    LAST_VIEW_KEY,
    JSON.stringify({ lat: c.lat, lng: c.lng, zoom: map.getZoom() }),
  );
}
```

- [ ] **Step 3: Rewrite `init()`**

Replace the whole `init` function (`frontend/app.js:60-101`) with:

```javascript
async function init() {
  document.getElementById("venue-list").innerHTML =
    '<div class="loading">Loading city index...</div>';

  try {
    const citiesResp = await fetch(`${API_BASE}/api/cities`);
    if (!citiesResp.ok) throw new Error(`/api/cities ${citiesResp.status}`);
    const citiesData = await citiesResp.json();
    allCities = citiesData.cities || [];
    defaultCityId = citiesData.default_city_id ?? 1;
    tierConfig = {}; // filled on first /api/venues response
  } catch (err) {
    document.getElementById("venue-list").innerHTML =
      `<div class="loading">Error loading cities: ${esc(err.message)}</div>`;
    return;
  }

  const lastView = loadLastView();
  const defaultCity = allCities.find((c) => c.id === defaultCityId);
  const fallbackCenter =
    defaultCity && defaultCity.centroid_lat != null
      ? [defaultCity.centroid_lat, defaultCity.centroid_lng]
      : [52.52, 13.405];

  map = L.map("map").setView(
    lastView ? [lastView.lat, lastView.lng] : fallbackCenter,
    lastView ? lastView.zoom : 11,
  );
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);

  markerCluster = L.markerClusterGroup({
    maxClusterRadius: 50,
    spiderfyOnMaxZoom: true,
    disableClusteringAtZoom: 15,
  });
  map.addLayer(markerCluster);

  cityPinLayer = L.layerGroup();

  allVenues = [];
  bindFilterEvents();
  rebuildFilterOptions(allVenues);
  updateSliderLabels();
  updateSliderFill();

  map.on("moveend zoomend", scheduleViewportUpdate);

  // Fire the handler once synchronously for the initial view.
  onMapViewportChange();

  // Courses view is independent from the venues fetch — always wire it up.
  initCoursesView();
}
```

Note: this step removes the old static-JSON fallback (`app.js:82-86`) as decided during brainstorming.

- [ ] **Step 4: Add new helper stubs and split `populateFilters` into bind-once + rebuild**

Just under the new `init()`, add placeholder definitions so the page loads without errors before Task 9 fleshes them out:

```javascript
function scheduleViewportUpdate() {
  if (viewportDebounceTimer) clearTimeout(viewportDebounceTimer);
  viewportDebounceTimer = setTimeout(onMapViewportChange, VIEWPORT_DEBOUNCE_MS);
}

function onMapViewportChange() {
  saveLastView();
  // Full behavior added in Task 9.
}
```

`populateFilters` (`app.js:386-420`) both **appends** `<option>`s and **binds**
event listeners, so calling it once per viewport merge (as Task 9's
`mergeVenuesResponse` does) would pile up duplicate dropdown entries and
multiply-bind the filter handlers. Replace the whole `populateFilters` function
with an idempotent pair: `bindFilterEvents` (called exactly once from `init`)
and `rebuildFilterOptions` (safe to call on every merge):

```javascript
function bindFilterEvents() {
  document.querySelectorAll("#membership-toggle input").forEach((r) =>
    r.addEventListener("change", () => {
      updateSliderLabels();
      updateSliderFill();
      applyFilters();
    }),
  );
  document.getElementById("slider-min").addEventListener("input", onSliderChange);
  document.getElementById("slider-max").addEventListener("input", onSliderChange);
  document.getElementById("district-filter").addEventListener("change", applyFilters);
  document.getElementById("activity-filter").addEventListener("change", applyFilters);
  document.getElementById("plus-filter").addEventListener("change", applyFilters);
  document.getElementById("coords-filter").addEventListener("change", applyFilters);
  document.getElementById("search-filter").addEventListener("input", applyFilters);
}

function rebuildFilterOptions(venues) {
  const distSelect = document.getElementById("district-filter");
  const actSelect = document.getElementById("activity-filter");
  const prevDist = distSelect.value;
  const prevAct = actSelect.value;

  // Each <select> ships a single hardcoded "All ..." placeholder option in
  // index.html (index.html:55-57, 62-64); truncate back to just that, then
  // repopulate from the full accumulated venue set.
  distSelect.length = 1;
  actSelect.length = 1;

  const districts = [...new Set(venues.map((v) => v.district).filter(Boolean))].sort();
  for (const d of districts) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    distSelect.appendChild(opt);
  }

  const activities = [...new Set(venues.flatMap((v) => v.activities).filter(Boolean))].sort();
  for (const a of activities) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    actSelect.appendChild(opt);
  }

  // Preserve the user's current selection if it still exists post-rebuild.
  if (districts.includes(prevDist)) distSelect.value = prevDist;
  if (activities.includes(prevAct)) actSelect.value = prevAct;
}
```

- [ ] **Step 5: Update HTML title and header**

In `frontend/index.html`, change:

- line 6: `<title>USC Berlin</title>` → `<title>USC Venue Explorer</title>`
- line 16 (the `<h1>` — verify the exact text via `grep -n Berlin frontend/index.html`): replace `Berlin` occurrence with `Venue Explorer`.

- [ ] **Step 6: Manual smoke check**

```bash
make serve
```

In a separate terminal / browser:

1. Open http://localhost:8000/
2. DevTools → Network: one `GET /api/cities` call, `200`, with >1 city in the response.
3. Map renders centered at Berlin (or the last saved view on subsequent loads).
4. Console is clean (no errors about undefined functions).
5. No `/api/venues` calls yet — that's Task 9's job. The venue list just shows no venues (filters exist but nothing to filter).
6. Pan the map; verify that the current center/zoom is persisted in DevTools → Application → Local Storage → `usc.lastView`.

Stop `make serve` with Ctrl-C.

- [ ] **Step 7: Commit**

```bash
git add frontend/app.js frontend/index.html
git commit -m "feat(frontend): bootstrap /api/cities, persist last map view"
```

---

## Task 9 — Frontend: viewport handler, cities-in-viewport, venue merging

**Files:**
- Modify: `frontend/app.js`

- [ ] **Step 1: Add `citiesInViewport` and the venue merge helper**

Just above the stub `onMapViewportChange` added in Task 8, insert:

```javascript
function cityBounds(city) {
  if (city.lat_min != null && city.lat_max != null) {
    return {
      south: city.lat_min,
      north: city.lat_max,
      west: city.lng_min,
      east: city.lng_max,
    };
  }
  if (city.centroid_lat == null || city.centroid_lng == null) return null;
  return {
    south: city.centroid_lat - CENTROID_BBOX_HALF_DEG,
    north: city.centroid_lat + CENTROID_BBOX_HALF_DEG,
    west: city.centroid_lng - CENTROID_BBOX_HALF_DEG,
    east: city.centroid_lng + CENTROID_BBOX_HALF_DEG,
  };
}

function boundsIntersect(viewport, city) {
  if (!city) return false;
  return !(
    city.east < viewport.getWest() ||
    city.west > viewport.getEast() ||
    city.north < viewport.getSouth() ||
    city.south > viewport.getNorth()
  );
}

function citiesInViewport(viewport) {
  const center = viewport.getCenter();
  const matches = [];
  for (const city of allCities) {
    const cb = cityBounds(city);
    if (!cb) continue;
    if (boundsIntersect(viewport, cb)) {
      const dx = (city.centroid_lat ?? 0) - center.lat;
      const dy = (city.centroid_lng ?? 0) - center.lng;
      matches.push({ id: city.id, distSq: dx * dx + dy * dy });
    }
  }
  matches.sort((a, b) => a.distSq - b.distSq);
  return matches.map((m) => m.id);
}

async function fetchVenuesForCities(cityIds) {
  const query = cityIds.map((id) => `city_ids=${id}`).join("&");
  const resp = await fetch(`${API_BASE}/api/venues?${query}`);
  if (!resp.ok) throw new Error(`/api/venues ${resp.status}`);
  return resp.json();
}

function mergeVenuesResponse(data) {
  // Only set tier_config the first time — it's global.
  if (data.tier_config && Object.keys(tierConfig).length === 0) {
    tierConfig = data.tier_config;
  }
  for (const cityEntry of data.cities || []) {
    loadedVenueCities.add(cityEntry.city_id);
    for (const venue of cityEntry.venues || []) {
      // Venue ids are unique per city; tag with city_id for dedupe.
      venue.city_id = cityEntry.city_id;
      allVenues.push(venue);
    }
  }
  rebuildFilterOptions(allVenues);
  updateSliderLabels();
  updateSliderFill();
  applyFilters();
}
```

- [ ] **Step 2: Replace the stub `onMapViewportChange`**

Replace the Task 8 stub with:

```javascript
async function onMapViewportChange() {
  saveLastView();
  const zoom = map.getZoom();

  if (zoom < MIN_FETCH_ZOOM) {
    markerCluster.clearLayers();
    showCityPins();
    return;
  }
  hideCityPins();

  const visible = citiesInViewport(map.getBounds());
  const needed = visible.filter((id) => !loadedVenueCities.has(id));

  if (needed.length > 0) {
    try {
      const data = await fetchVenuesForCities(needed);
      mergeVenuesResponse(data);
    } catch (err) {
      console.warn("Venue fetch failed", err);
    }
  } else if (currentView === "venues") {
    applyFilters();
  }

  if (currentView === "courses") refreshCoursesForViewport();
}
```

Note: `showCityPins`, `hideCityPins`, and `refreshCoursesForViewport` are added in Task 10 and Task 11. Add temporary stubs now so the page still loads:

```javascript
function showCityPins() {}
function hideCityPins() {}
function refreshCoursesForViewport() {}
```

Finally, guard `renderMap` so a venues fetch that resolves while the user is on
the Courses tab can't repaint venue markers over the course map (both views
share the single `markerCluster`). `onMapViewportChange` calls
`mergeVenuesResponse → applyFilters → renderMap` regardless of `currentView`, so
add an early return at the top of `renderMap` (`app.js:581`):

```javascript
function renderMap(venues) {
  if (currentView !== "venues") return;
  markerCluster.clearLayers();
  // ... existing body unchanged ...
}
```

`renderCourseMap` already has the symmetric guard (`app.js:326`). When the user
switches back to the venues tab, `switchView` sets `currentView` *before*
calling `applyFilters`, so the deferred render happens correctly.

- [ ] **Step 3: Manual smoke check**

```bash
make serve
```

1. Open http://localhost:8000/ (Berlin view).
2. Network tab: exactly one `/api/venues?city_ids=1` call (Berlin is the only city in view at zoom 11). Berlin venues render on the map.
3. Pan east toward Poland, then back to Berlin. No new `/api/venues` call on the return (Berlin already in `loadedVenueCities`).
4. Fly to Hamburg via the address bar (`map.setView([53.55, 9.99], 11)` in the JS console works). After ~200 ms, one `/api/venues?city_ids=2` call; Hamburg venues merge into the map.
5. Fly back to Berlin — no new requests.
6. Venues list + filters still work across the merged set.

Stop `make serve`.

- [ ] **Step 4: Commit**

```bash
git add frontend/app.js
git commit -m "feat(frontend): load venues for cities intersecting map viewport"
```

---

## Task 10 — Frontend: low-zoom city pins

**Files:**
- Modify: `frontend/app.js` (replace the stubs from Task 9)

- [ ] **Step 1: Implement `showCityPins` / `hideCityPins`**

Replace the empty stubs added in Task 9 with:

```javascript
function showCityPins() {
  if (!cityPinLayer) return;
  cityPinLayer.clearLayers();
  for (const city of allCities) {
    if (city.centroid_lat == null || city.centroid_lng == null) continue;
    const marker = L.circleMarker([city.centroid_lat, city.centroid_lng], {
      radius: 6,
      fillColor: "#4a90d9",
      color: "#fff",
      weight: 1.5,
      opacity: 1,
      fillOpacity: 0.9,
    });
    marker.bindTooltip(city.name, { permanent: false, direction: "top" });
    marker.on("click", () => {
      map.flyTo([city.centroid_lat, city.centroid_lng], MIN_FETCH_ZOOM);
    });
    cityPinLayer.addLayer(marker);
  }
  if (!map.hasLayer(cityPinLayer)) map.addLayer(cityPinLayer);
}

function hideCityPins() {
  if (cityPinLayer && map.hasLayer(cityPinLayer)) {
    map.removeLayer(cityPinLayer);
  }
}
```

- [ ] **Step 2: Manual smoke check**

```bash
make serve
```

1. Open the app.
2. Zoom out to level 6 (country level). Venue cluster empties; blue circle pins appear at each city's centroid.
3. Click the Berlin pin — `map.flyTo` runs, lands at zoom 9, viewport handler fires, pins disappear, venues load for Berlin.
4. Zoom out again and click Hamburg — pins reappear, click loads Hamburg venues.

Stop `make serve`.

- [ ] **Step 3: Commit**

```bash
git add frontend/app.js
git commit -m "feat(frontend): city pin layer at low zoom"
```

---

## Task 11 — Frontend: per-(city, date) courses cache

**Files:**
- Modify: `frontend/app.js:159-195,325-359`

- [ ] **Step 1: Update `fetchCourses` to target visible cities and cache per `(city_id, date)`**

Replace the whole `fetchCourses` function (`frontend/app.js:159-195`) with:

```javascript
async function fetchCourses() {
  const startDate = document.getElementById("course-date-start").value;
  let endDate = document.getElementById("course-date-end").value;
  if (!startDate) return;
  if (!endDate || endDate < startDate) {
    endDate = startDate;
    document.getElementById("course-date-end").value = startDate;
  }

  const days = Math.min(13, Math.max(1, daysBetween(startDate, endDate) + 1));
  const dateList = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(startDate + "T00:00:00");
    d.setDate(d.getDate() + i);
    dateList.push(d.toISOString().slice(0, 10));
  }

  const zoom = map.getZoom();
  const visible =
    zoom >= MIN_FETCH_ZOOM ? citiesInViewport(map.getBounds()) : [];

  const listEl = document.getElementById("course-list");
  if (visible.length === 0) {
    listEl.innerHTML =
      '<div class="loading">Zoom in on a city to load courses.</div>';
    allCourses = [];
    applyCourseFilters();
    return;
  }

  // Decide which (city_id, date) pairs still need fetching.
  const missingByCity = new Map();
  for (const cid of visible) {
    const cached = loadedCourseCities.get(cid) ?? new Set();
    const missing = dateList.filter((d) => !cached.has(d));
    if (missing.length > 0) missingByCity.set(cid, missing);
  }

  listEl.innerHTML = '<div class="loading">Loading courses...</div>';
  const token = ++coursesLoadToken;

  try {
    // One /api/courses call per city, covering that city's missing dates.
    // `missing` may be non-contiguous (e.g. the user shifted the date range
    // after a partial load), so requesting `start=missing[0], days=missing.length`
    // would skip the tail and silently lose dates. Instead request the whole
    // [first..last] span — the backend's per-(city,date) cache makes re-covering
    // the already-loaded middle cheap — and filter the response down to the
    // dates we actually lacked, so already-loaded courses are never double-added.
    for (const [cid, missing] of missingByCity.entries()) {
      const spanStart = missing[0];
      const spanEnd = missing[missing.length - 1];
      const spanDays = daysBetween(spanStart, spanEnd) + 1;
      const missingSet = new Set(missing);
      const resp = await fetch(
        `${API_BASE}/api/courses?start_date=${spanStart}&days=${spanDays}&city_ids=${cid}`,
      );
      const data = await resp.json().catch(() => ({}));
      if (token !== coursesLoadToken) return;
      if (!resp.ok) throw new Error(data.detail || `API ${resp.status}`);
      const cached = loadedCourseCities.get(cid) ?? new Set();
      for (const d of missing) cached.add(d);
      loadedCourseCities.set(cid, cached);
      // `data.cities` is always length 1 (we queried a single city).
      for (const entry of data.cities || []) {
        for (const c of entry.courses || []) {
          if (!missingSet.has(c.date)) continue; // already loaded — skip
          c.city_id = entry.city_id;
          allCourses.push(c);
        }
      }
    }
    coursesLoaded = true;
    populateCategoryFilter(allCourses);
    applyCourseFilters();
  } catch (err) {
    if (token !== coursesLoadToken) return;
    coursesLoaded = true;
    listEl.innerHTML = `<div class="loading">Error loading courses: ${esc(err.message)}</div>`;
  }
}
```

- [ ] **Step 2: Hook viewport changes into the courses refresh**

Replace the `refreshCoursesForViewport` stub from Task 9 with:

```javascript
function refreshCoursesForViewport() {
  if (currentView === "courses") fetchCourses();
}
```

Also inside `onMapViewportChange` (from Task 9), the call to `refreshCoursesForViewport` after a successful venues fetch is already present — confirm it still reads `if (currentView === "courses") refreshCoursesForViewport();` after this task.

- [ ] **Step 3: Drop stale filtering on the active date range**

`applyCourseFilters` (`frontend/app.js:227-250`) already filters off `allCourses`. Since we now accumulate courses across dates and cities, add a date-range guard near the top of the function's filter callback:

Replace the filter body at `app.js:234-245` with:

```javascript
  filteredCourses = allCourses.filter((c) => {
    // Only show courses within the currently selected date range.
    const startDate = document.getElementById("course-date-start").value;
    const endDate = document.getElementById("course-date-end").value || startDate;
    if (c.date < startDate || c.date > endDate) return false;

    const slot = courseTimeSlot(c.start_time);
    if (!slot || !timeSlots.includes(slot)) return false;
    if (category && c.category !== category) return false;
    if (spotsOnly && !(c.free_spots && c.free_spots > 0)) return false;
    if (plusOnly && !c.is_plus) return false;
    if (search) {
      const hay = (c.title + " " + c.venue_name + " " + (c.teacher || "")).toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
```

- [ ] **Step 4: Manual smoke check**

```bash
make serve
```

1. Open the app at the Berlin view.
2. Switch to the Courses tab. One `/api/courses?...&city_ids=1` call fires; courses render.
3. Switch back to Venues, pan to Hamburg, switch to Courses again. One `/api/courses?...&city_ids=2` call fires (Hamburg was not yet loaded). Berlin courses are already in memory.
4. Switch back to Berlin view → Courses: zero network calls, Berlin courses already cached.
5. Change the date range by one day: only the new dates trigger a fetch.
6. Zoom out below level 9 and switch to Courses: "Zoom in on a city..." message.

Stop `make serve`.

- [ ] **Step 5: Commit**

```bash
git add frontend/app.js
git commit -m "feat(frontend): per-(city,date) courses cache, viewport-scoped"
```

---

## Task 12 — End-to-end verification pass

This is a dedicated checklist task before merging — no code changes.

**Files:** none.

- [ ] **Step 1: Full backend suite and linters**

```bash
make test
make lint
```

Expected: all green.

- [ ] **Step 2: Cold-start E2E**

```bash
rm -f backend/cache/usc.db
make serve
```

In another terminal:

```bash
curl -s 'http://localhost:8000/api/cities' | jq '.cities | length'
```

Expected: a number greater than 1.

```bash
sqlite3 backend/cache/usc.db 'select count(*) from cities'
```

Expected: greater than 1. Pick any row to verify centroid is populated:

```bash
sqlite3 backend/cache/usc.db "select id, name, centroid_lat, centroid_lng from cities limit 5"
```

- [ ] **Step 3: Required-param guard**

```bash
curl -s -o /dev/null -w "%{http_code}\n" 'http://localhost:8000/api/venues'
curl -s -o /dev/null -w "%{http_code}\n" 'http://localhost:8000/api/courses?start_date=2026-04-11'
```

Expected: both print `400`.

- [ ] **Step 4: Multi-city grouped payload**

```bash
curl -s 'http://localhost:8000/api/venues?city_ids=1&city_ids=2' | jq '.cities | map({city_id, city_name, total})'
```

Expected: two entries (Berlin + Hamburg, or whichever ids USC assigns).

- [ ] **Step 5: Browser walkthrough**

With `make serve` still running, open http://localhost:8000/ and perform every check from the "Manual E2E via `make serve`" section of the spec (`/home/node/.claude/plans/ticklish-wobbling-lamport.md`). In particular:

- Load persists last center/zoom in `localStorage`.
- Panning to a new city triggers exactly one `/api/venues?city_ids=<new>` call.
- Panning back triggers zero calls.
- Zoom out below level 9 shows city pins, zero venue requests.
- Click a pin → `flyTo` → venues load for that city.
- Courses view loads per-city and dedupes by `(city_id, date)`.
- Tier / district / activity filters still work across the merged multi-city venue set.
- Venue detail popup still opens and the body loads (regression check on Task 7).

- [ ] **Step 6: Stop the server and wrap up**

```bash
# Ctrl-C in the `make serve` terminal
git status   # expect a clean tree
git log --oneline -12
```

- [ ] **Step 7 (optional): open a PR**

Not part of implementation — use the `finishing-a-development-branch` skill at the end to decide how to integrate.

---

## Out of scope (tracked in backlog)

- **Client-side cache TTL + reload** — the `loadedVenueCities` / `loadedCourseCities`
  structures introduced in Tasks 8–11 are add-only and never expire, so a
  long-lived browser session can keep serving data staler than the server's own
  TTLs with no way to refresh short of a full page reload. Deliberately deferred;
  captured in `docs/backlog.md` for a follow-up.
