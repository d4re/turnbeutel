# Course Details & Travel Times Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clicking a course expands its details (description, rating, cancellation, class tier, on-site fee) and shows bike/transit travel times from a server-stored home; venues get travel chips.

**Architecture:** PR A extends the course pipeline (list-call fields, a lazily fetched and cached `/api/courses/{id}`) and adds a single-row settings store plus a "You" panel. PR B adds `backend/travel.py` (OSRM bike distances, VBB transit, one priority-queue worker), geocoding via Nominatim, travel cache tables, and the frontend chips/home marker.

**Tech Stack:** FastAPI, httpx, Pydantic v2, SQLite (`storage.py`), pytest with `httpx.MockTransport` and `asyncio.run`, vanilla JS + Leaflet, ESLint 9.

**Spec:** `docs/superpowers/specs/2026-10-19-course-details-and-travel-times-design.md`

## Global Constraints

- Branches: PR A = `feat/course-details` (this worktree, Tasks 1–5). PR B = `feat/travel-times`, branched from `feat/course-details` after Task 5 (Tasks 6–12). PR titles exactly: `feat: course details, class tiers and on-site fees` and `feat: home location and travel times`.
- Every storage access holds `storage._lock` (use `_fetchone` / `_fetchall`, or `with _lock:` for writes).
- Do not raise existing USC concurrency. Course detail fetches share a `Semaphore(5)`. OSRM: sequential requests, ≤100 destinations each. VBB: one request at a time, ≥0.7 s apart, 15 s timeout, one retry. Nominatim: ≥1 s apart.
- TTLs: course details 7 d; geocode 30 d; bike 30 d; transit (typical) 7 d; transit failure back-off 1 h; exact transit 24 h.
- Outgoing requests to OSRM/VBB/Nominatim send `User-Agent: turnbeutel (+https://github.com/d4re/turnbeutel)`. The home coordinate is never logged.
- Frontend: zero-build, everything in `frontend/app.js` / `index.html` / `style.css`; `init()` stays the last statement in `app.js`; never rebuild markers to update chips.
- Commit messages: Conventional Commits; end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Before each commit: `cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q` and `cd frontend && npx eslint app.js`.
- Run backend commands from `backend/` (`uv run pytest …`).

## Review Focus

1. **Venue without coordinates or unknown venue id** in `POST /api/travel` / `/api/travel/exact` — must be skipped (travel) or 404 (exact), never crash the worker. Tests in Tasks 7 (`get_venue_points`) and 10.
2. **VBB returns 200 with zero journeys** (e.g. origin == destination area, night) — typical stores `pt_failed_at`, exact returns 502; the worker keeps running. Tests in Tasks 6 (`pick_*` → None) and 9.
3. **Home changes while a warm is running** — stale-origin jobs are dropped and the new origin's results are not mixed with the old. Test in Task 9.
4. **Course detail fetch for a course id USC no longer knows** (past class) — 404 and the expanded block still shows list data. Test in Task 2.
5. **Settings PUT with a corporate tier under a private plan** (`{"type":"private","tier":"M"}`) — 422, nothing stored. Test in Task 3.

---

## PR A — course details

### Task 1: Class tiers and on-site fee from the list call

**Files:**
- Modify: `backend/models.py` (`Course`)
- Modify: `backend/server.py` (`transform_course`, new `parse_extra_fee`)
- Modify: `backend/storage.py` (`_migrate`, `courses` DDL, `upsert_courses_for_date`, `_row_to_course`)
- Test: `backend/test_server.py`, `backend/test_storage.py`

**Interfaces:**
- Produces: `Course` fields `tiers_private: list[str] = []`, `tiers_corporate: list[str] = []`, `min_tier_private: str | None = None`, `min_tier_corporate: str | None = None`, `extra_fee_text: str | None = None`, `extra_fee_eur: int | None = None` (defaults so existing constructors keep working).
- Produces: `parse_extra_fee(title: str, extra_text: str | None) -> int | None` in `server.py`.

- [ ] **Step 1: Write failing tests** in `test_server.py`:

```python
@pytest.mark.parametrize(
    ("title", "extra", "expected"),
    [
        ("Aerial Hoop | USC Checkin + 15 €", None, 15),
        ("Pottery +12 Euros", None, 12),
        ("Sauna EXTRA 5 EUR PAID AT THE DOOR", None, 5),
        ("Ice Bath", "+ 10€ Zuzahlung", 10),
        ("Ice Bath + 8 €", "+ 10€ Zuzahlung", 10),  # extra text wins
        ("Drop-in 15 €", None, None),  # bare price, no + / extra
        ("Yoga", "", None),
        ("", None, None),
    ],
)
def test_parse_extra_fee(title, extra, expected):
    assert parse_extra_fee(title, extra) == expected


def test_transform_course_class_tiers_and_fee():
    raw = _make_raw_course(
        title="Intro to Aerial Silks + 5 €",
        planTypes=["L", "XL"],
        planTypesB2B=["L", "XL"],
        extraPriceDescriptionText="",
    )
    c = transform_course(raw)
    assert c.tiers_private == ["Premium", "Max"]
    assert c.tiers_corporate == ["L", "XL"]
    assert c.min_tier_private == "Premium"
    assert c.min_tier_corporate == "L"
    assert c.extra_fee_text is None
    assert c.extra_fee_eur == 5


def test_transform_course_without_plan_types():
    c = transform_course(_make_raw_course())
    assert c.tiers_corporate == [] and c.min_tier_corporate is None and c.extra_fee_eur is None
```

In `test_storage.py`:
- `test_courses_roundtrip_keeps_tiers_and_fee`: upsert a `Course` with `tiers_corporate=["L","XL"]`, `min_tier_corporate="L"`, `extra_fee_text="+ 10€"`, `extra_fee_eur=10`; `get_courses_for_dates` returns equal values.
- `test_migrate_drops_courses_table_without_fee_column`: create a DB with the *current* `courses` DDL minus the six new columns (PK `(city_id, course_id, date)`), insert a row and a `course_fetches` row, call `storage.init`; assert `courses` has column `extra_fee_eur` and both tables are empty.

- [ ] **Step 2: Run** `uv run pytest -q -k "extra_fee or class_tiers or plan_types or tiers_and_fee or fee_column"` → FAIL (ImportError / AttributeError).

- [ ] **Step 3: Implement.**
  - `parse_extra_fee`: case-insensitive regex, checked on `extra_text` then `title`, first match wins:
    `(?:\+|\bextra\b)\s*(\d{1,3})\s*(?:€|eur(?:o|os)?\b)` — return `int(group(1))`.
  - `transform_course`: map tiers exactly as `transform_venue` does (`CORP_TO_PRIVATE`, `CORPORATE_TIER_ORDER`), min tiers via `min_tier`, `extra_fee_text = raw.get("extraPriceDescriptionText") or None`.
  - Storage: new columns `tiers_private_json TEXT, tiers_corporate_json TEXT, min_tier_private TEXT, min_tier_corporate TEXT, extra_fee_text TEXT, extra_fee_eur INTEGER`. In `_migrate`, after the existing PK check: if `courses` exists and `PRAGMA table_info(courses)` has no `extra_fee_eur`, `DROP TABLE courses` and `DROP TABLE IF EXISTS course_fetches`. Comment in the style of the existing PK migration.

- [ ] **Step 4: Run** the full backend suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): class tiers and on-site fee on courses`.

### Task 2: `GET /api/courses/{course_id}` with cache

**Files:**
- Modify: `backend/models.py` (new `CourseDetail`)
- Modify: `backend/server.py` (`fetch_course_detail`, `build_course_detail`, endpoint, `COURSE_DETAILS_TTL`)
- Modify: `backend/storage.py` (`course_details` table, `get_course_detail`, `upsert_course_detail`)
- Test: `backend/test_server.py`, `backend/test_storage.py`

**Interfaces:**
- Produces: `class CourseDetail(BaseModel): description: str = ""; rating: float | None; rating_count: int | None; free_cancellation_hours: int | None; booking_type: str | None; fetched_at: float`.
- Produces: `storage.get_course_detail(course_id: int) -> CourseDetail | None`, `storage.upsert_course_detail(course_id: int, detail: CourseDetail) -> None` (table `course_details(course_id INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, fetched_at REAL NOT NULL)`).
- Produces: `async def fetch_course_detail(course_id: int, client: httpx.AsyncClient | None = None) -> dict` (returns USC `data`), `build_course_detail(raw: dict) -> CourseDetail` (`ratings.averageScore`, `ratings.totalRatings`, `freeCancellationHours`, `bookingType`), `COURSE_DETAILS_TTL = 7 * 24 * 3600`.
- Endpoint: `GET /api/courses/{course_id}` → `CourseDetail`. Register it **after** `GET /api/courses` (path does not collide, but keep routes grouped).

- [ ] **Step 1: Write failing tests** (endpoint tests use `seeded_client` + `monkeypatch.setattr(server, "fetch_course_detail", fake)`; add a no-op default for `fetch_course_detail` to the `seeded_client` fixture):
  - `test_get_course_detail_fetches_and_caches`: fake returns `{"description": "Hang on", "ratings": {"averageScore": 4.8, "totalRatings": 120}, "freeCancellationHours": 12, "bookingType": "instant"}`; first GET → 200 with those values mapped; second GET → fake call count still 1; `storage.get_course_detail(7).rating_count == 120`.
  - `test_get_course_detail_stale_served_then_refreshed`: pre-store a detail with `fetched_at = time.time() - COURSE_DETAILS_TTL - 1` and description "old"; GET returns "old"; after the background task, storage has the new description (pattern: `test_get_venues_stale_cache_served_immediately_then_refreshed`).
  - `test_get_course_detail_404`: fake raises `httpx.HTTPStatusError` with a 404 response → endpoint 404.
  - `test_get_course_detail_upstream_error_is_502`: fake raises `httpx.ConnectError("x")` → 502.
  - Storage: `test_course_detail_roundtrip`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Fresh cache → return. Stale cache → return it and `asyncio.create_task` a refresh guarded by a module-level `_course_detail_refresh: set[int]` (cleared in the fixture). Miss → fetch under a module-level `_course_detail_semaphore = asyncio.Semaphore(5)`; 404 → `HTTPException(404)`; any other `httpx.HTTPError` → `HTTPException(502, "USC unavailable")`.
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): lazily fetched, cached course details`.

### Task 3: Settings store (plan)

**Files:**
- Modify: `backend/models.py` (`Plan`, `Settings`)
- Modify: `backend/storage.py` (`settings` table, `get_settings`, `put_settings`)
- Modify: `backend/server.py` (`GET/PUT /api/settings`)
- Test: both test files

**Interfaces:**
- Produces: `class Plan(BaseModel): type: Literal["corporate", "private"]; tier: str` with a `model_validator` that the tier is in `CORPORATE_TIER_ORDER` for corporate and `PRIVATE_TIER_ORDER` for private (move those two lists to `models.py` and import them in `server.py`, so the model can validate without importing `server`).
- Produces: `class Settings(BaseModel): plan: Plan | None = None` (Task 8 adds `home`, `bike_kmh`).
- Produces: `storage.get_settings() -> Settings` (empty `Settings()` when no row), `storage.put_settings(s: Settings) -> None` (table `settings(id INTEGER PRIMARY KEY CHECK (id = 1), payload_json TEXT NOT NULL, updated_at REAL NOT NULL)`).
- Endpoints: `GET /api/settings` → `Settings`; `PUT /api/settings` (body `Settings`) → stored `Settings`.

- [ ] **Step 1: Write failing tests:**
  - `test_settings_default_empty`: GET → `{"plan": None}` (i.e. `resp.json()["plan"] is None`).
  - `test_settings_put_then_get`: PUT `{"plan": {"type": "corporate", "tier": "M"}}` → 200; GET returns it.
  - `test_settings_rejects_tier_of_other_type`: PUT `{"plan": {"type": "private", "tier": "M"}}` → 422 and GET still empty.
  - `test_settings_put_replaces_whole_object`: PUT plan, then PUT `{}` → GET plan is None.
  - Storage: `test_settings_roundtrip`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** as specified above.
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): server-side settings store with the user's plan`.

### Task 4: Frontend — "You" panel (plan), badges, expanded course

**Files:**
- Modify: `frontend/index.html` (button `#you-button` next to `#filters-button`; panel `#you-panel` before `#filters`, with its own `.filters-overlay-bar` "You" + Done)
- Modify: `frontend/app.js`
- Modify: `frontend/style.css`

**Interfaces:**
- Consumes: `GET/PUT /api/settings`, `GET /api/courses/{id}`, Course fields from Task 1.
- Produces (used by Task 11): module-level `let settings = {}`; `async function saveSettings(next)` (PUT, on success assigns `settings`, on failure throws); `function renderCourseDetails(item, course)` filling `.course-details` inside a course item; `const courseDetailCache = new Map()` (course id → detail or `null` on error).

Behaviour (spec §4):
- `#you-button` is visible on desktop and mobile. Desktop: toggles `#you-panel` open/closed in the sidebar. Mobile: `setSheetState("full")` + `body.you-open`, styled like `body.filters-open` (reuse the same rules by adding `body.you-open #you-panel` selectors). Done closes.
- Plan section: two radios Corporate/Private + `<select id="plan-tier">` filled from `tierConfig[type].order` / `.display` (an empty option "Not set"). Change → `saveSettings({...settings, plan})`; on failure show `.you-error` text "Couldn't save — try again" and restore the controls from `settings`.
- `init()`: fetch `/api/settings` right after `/api/cities` (failure → `settings = {}`, app continues). If `settings.plan` and no last-view membership was saved, check the matching `#membership-toggle` radio before the first render.
- Course item badges in `renderCourseListChunk`, after PLUS:
  - `<span class="fee-badge" title="${extra_fee_text}">+${extra_fee_eur} € on site</span>`
  - `<span class="upgrade-badge">⬆ ${display(minTier)}</span>` from `function courseUpgradeTier(course) -> string | null`: with `settings.plan`: class tiers for `plan.type` don't include `plan.tier` → that type's class min tier. Without plan: active toggle; class min tier index > venue min tier index (venue from `allVenues` by `address_id === course.venue_id`; no venue loaded → no badge).
- Click: existing active/focus behaviour, plus collapse any other `.course-item.expanded`, toggle `expanded` on this item, and on expand call `renderCourseDetails`. Clicks inside `.course-details` (links, "more") must not collapse (stopPropagation).
- `renderCourseDetails` shows immediately: fee text, class tiers (`tierBadgeHtml`), a "Loading details…" line; then `/api/courses/{id}` (memoized in `courseDetailCache`) → description (CSS `-webkit-line-clamp: 4`, "more" toggles), `★ 4.8 (120)`, `Free cancellation up to 12 h before`, booking type; venue link "Book on USC" when the venue is in `allVenues` and `isHttpUrl(venue.url)`. Error → "Details unavailable" in place of the loading line.

- [ ] **Step 1:** Implement markup, CSS and JS above.
- [ ] **Step 2: Lint** `cd frontend && npx eslint app.js` → no errors.
- [ ] **Step 3: Verify in browser** with the `verify` skill (desktop 1280×800 and phone 390×844): set plan to corporate M and reload (persists); in Courses, find a Fl'air "Aerial Silks" class → `⬆ L`; a class with "+ … €" in its title → fee badge; expand a course → description + rating appear, a second click elsewhere collapses it; map still focuses the venue; mobile "You" overlay opens and closes.
- [ ] **Step 4: Commit** `feat(frontend): course details, class tier and fee badges, You panel`.

### Task 5: PR A docs and PR

**Files:** `README.md` (API endpoints: `GET /api/courses/{id}`, `GET/PUT /api/settings`), `AGENTS.md` (architecture: course details cache + TTL; settings store and the **single-user assumption** "if the instance is ever shared, home/plan storage and per-IP limits must be revisited"; schema: new `Course` fields).

- [ ] **Step 1:** Update both files; check README/AGENTS/code agree.
- [ ] **Step 2:** Full checks (Global Constraints) → green.
- [ ] **Step 3: Commit** `docs: course details and settings`.
- [ ] **Step 4:** Use superpowers:finishing-a-development-branch; push and open PR titled `feat: course details, class tiers and on-site fees` (body ends with the Claude Code attribution line). Then `git switch -c feat/travel-times` for PR B.

---

## PR B — home & travel times

### Task 6: `travel.py` pure helpers and journey selection

**Files:**
- Create: `backend/travel.py`
- Create: `backend/test_travel.py`
- Modify: `backend/pyproject.toml` (+ `tzdata` dependency; `uv lock`)

**Interfaces:**
- Produces in `travel.py`:
  - `USER_AGENT`, `OSRM_TABLE_URL = "https://routing.openstreetmap.de/routed-bike/table/v1/driving/"`, `VBB_JOURNEYS_URL = "https://v6.vbb.transport.rest/journeys"`, `BERLIN = ZoneInfo("Europe/Berlin")`, `VBB_BBOX = (51.3, 53.6, 11.2, 14.8)` (lat_min, lat_max, lng_min, lng_max), TTL constants from Global Constraints.
  - `origin_key(lat: float, lng: float) -> str` → `f"{lat:.4f},{lng:.4f}"`.
  - `in_vbb_area(lat: float, lng: float) -> bool`.
  - `next_reference_departure(now: datetime) -> datetime`: the first Tuesday 17:30 Europe/Berlin strictly after `now + 1 h` (tz-aware result).
  - `@dataclass class Transit: pt_min: int; lines: list[str]; walk_m: int; depart_at: datetime; arrive_at: datetime`.
  - `parse_journey(j: dict) -> Transit`: `depart_at` = first leg departure, `arrive_at` = last leg arrival (ISO with offset; use `plannedDeparture`/`plannedArrival` when `departure`/`arrival` is null), `pt_min = round(minutes)`, lines = `leg["line"]["name"]` for legs with a line, `walk_m` = sum of `distance` of `walking` legs.
  - `pick_typical(journeys: list[dict]) -> Transit | None`: earliest `arrive_at`.
  - `pick_exact(journeys: list[dict], arrive_by: datetime) -> Transit | None`: among `arrive_at <= arrive_by`, latest `depart_at`; none → `None`.

- [ ] **Step 1: Write failing tests** (journey fixtures built with a small helper `_journey(dep, arr, legs)` producing VBB-shaped dicts with walking + line legs):
  - `test_origin_key_rounds_to_4_decimals`: `origin_key(52.5579489, 13.4856965) == "52.5579,13.4857"`.
  - `test_in_vbb_area`: Berlin `(52.52, 13.40)` True; Hamburg `(53.55, 9.99)` False; Potsdam `(52.39, 13.06)` True.
  - `test_next_reference_departure_from_monday`: `now = 2026-10-19 10:00 Berlin` → `2026-10-20 17:30+02:00`.
  - `test_next_reference_departure_tuesday_evening_rolls_to_next_week`: `now = 2026-10-20 17:00 Berlin` → `2026-10-27 17:30+01:00` (DST ended 25 Oct; assert `utcoffset() == timedelta(hours=1)`).
  - `test_parse_journey_lines_walk_and_minutes`: legs walk 600 m → M4 → U8 → walk 177 m, dep 17:31 arr 18:09 → `Transit(38, ["M4","U8"], 777, …)`.
  - `test_parse_journey_falls_back_to_planned_times`.
  - `test_pick_typical_earliest_arrival`: three journeys arriving 18:17, 18:09, 18:18 → the 18:09 one.
  - `test_pick_exact_latest_departure_arriving_in_time`: arrive_by 18:55; journeys (dep 18:00 arr 18:40), (dep 18:12 arr 18:50), (dep 18:20 arr 19:00) → dep 18:12.
  - `test_pick_returns_none_for_no_journeys`: `pick_typical([]) is None`, `pick_exact([], …) is None`.
- [ ] **Step 2: Run** `uv run pytest test_travel.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(backend): travel helpers and VBB journey selection`.

### Task 7: OSRM and VBB clients with pacing; travel storage

**Files:**
- Modify: `backend/travel.py`, `backend/storage.py`
- Test: `backend/test_travel.py`, `backend/test_storage.py`

**Interfaces:**
- Produces in `travel.py`:
  - `async def fetch_bike_km(origin: tuple[float, float], dests: list[tuple[str, float, float]], client: httpx.AsyncClient) -> dict[str, float]` — dests are `(venue_id, lat, lng)`; chunks of 100, sequential; URL `{OSRM_TABLE_URL}{lng},{lat};{lng},{lat}…?sources=0&annotations=distance`; result `distances[0][i+1] / 1000` rounded to 2 decimals; `null` distances omitted. HTTP errors propagate.
  - `class Pacer: def __init__(self, min_interval: float, clock=time.monotonic, sleep=asyncio.sleep)`; `async def wait(self) -> None` — serialises callers with an `asyncio.Lock` and sleeps so starts are ≥ `min_interval` apart.
  - `async def fetch_journeys(origin, dest: tuple[float, float], client, pacer: Pacer, *, departure: datetime | None = None, arrival: datetime | None = None) -> list[dict]` — exactly one of departure/arrival; params `from.latitude/from.longitude/from.address=Home`, `to.latitude/to.longitude/to.address=Venue`, `results=3`, `stopovers=false`; 15 s timeout; one retry after 2 s on `httpx.HTTPError` or 5xx; returns `journeys` list (possibly empty); raises after the retry fails.
- Produces in `storage.py` (tables per spec §3):
  - Row model in `models.py`: `class TravelEntry(BaseModel): venue_id: str; bike_km: float | None; bike_at: float | None; pt_min: int | None; pt_lines: list[str]; walk_m: int | None; pt_at: float | None; pt_failed_at: float | None`.
  - `get_travel(origin_key: str, venue_ids: list[str]) -> dict[str, TravelEntry]`
  - `set_bike_km(origin_key: str, values: dict[str, float], at: float) -> None` (upsert, keeps transit columns)
  - `set_transit(origin_key: str, venue_id: str, pt_min: int, lines: list[str], walk_m: int, at: float) -> None` (clears `pt_failed_at`)
  - `mark_transit_failed(origin_key: str, venue_id: str, at: float) -> None`
  - `get_exact(origin_key: str, venue_id: str, arrive_by: str, max_age: float) -> dict | None` / `set_exact(origin_key, venue_id, arrive_by: str, result: dict, at: float)` — `result` keys `pt_min, depart_at, arrive_at, lines, walk_m` (ISO strings for times).
  - `get_venue_points(venue_ids: list[str]) -> dict[str, tuple[float, float]]` and `list_city_venue_points(city_ids: list[int]) -> list[tuple[str, float, float]]` — from `venues`, `has_coordinates = 1` only, deduplicated by `venue_id`.

- [ ] **Step 1: Write failing tests:**
  - `test_fetch_bike_km_batches_of_100`: 230 dests → 3 requests with 100/100/30 destinations (count `;` in the path), results merged for all 230 ids; mock returns `distances=[[0, 1234.0, …]]`.
  - `test_fetch_bike_km_skips_null_distances`.
  - `test_pacer_spaces_calls`: fake clock + recording fake sleep; three `wait()` calls at t=0 → sleeps total ≥ 1.4 for `min_interval=0.7`.
  - `test_fetch_journeys_retries_once_then_raises`: handler returning 503 twice → raises; handler 503 then 200 → returns journeys; request count 2. (Patch the 2 s retry delay via a module constant `RETRY_DELAY` set to 0 in the test.)
  - `test_fetch_journeys_sends_arrival_param`.
  - Storage: `test_travel_bike_then_transit_merge` (set bike, set transit → one entry with both), `test_mark_transit_failed_then_success_clears_it`, `test_exact_respects_max_age`, `test_venue_points_skip_missing_coordinates`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): OSRM/VBB clients and travel cache tables`.

### Task 8: Home + bike speed settings; geocoding

**Files:**
- Modify: `backend/models.py` (`Home`, `Settings.home`, `Settings.bike_kmh`, `GeocodeResult`)
- Modify: `backend/travel.py` (`geocode`)
- Modify: `backend/storage.py` (`geocode_cache`)
- Modify: `backend/server.py` (`GET /api/geocode`)
- Test: `backend/test_server.py`, `backend/test_travel.py`

**Interfaces:**
- Produces: `class Home(BaseModel): lat: float = Field(ge=-90, le=90); lng: float = Field(ge=-180, le=180); label: str = ""`; `Settings.home: Home | None = None`; `Settings.bike_kmh: Literal[15, 18, 21] | None = None`.
- Produces: `class GeocodeResult(BaseModel): label: str; lat: float; lng: float`.
- Produces: `async def geocode(q: str, viewbox: str | None, client: httpx.AsyncClient, pacer: Pacer) -> list[GeocodeResult]` — Nominatim `https://nominatim.openstreetmap.org/search`, params per spec §2 (`bounded=0`), label = `display_name`; `travel.nominatim_pacer = Pacer(1.0)` module-level.
- Produces: `storage.get_geocode(query_norm: str, max_age: float) -> list[dict] | None`, `storage.set_geocode(query_norm: str, results: list[dict], at: float)`.
- Endpoint: `GET /api/geocode?q=&viewbox=` → `list[GeocodeResult]`; `q` stripped, 3–200 chars else 422; Nominatim error → 502.

- [ ] **Step 1: Write failing tests:**
  - `test_settings_home_and_bike_speed_roundtrip`; `test_settings_rejects_bad_speed` (`bike_kmh: 20` → 422); `test_settings_rejects_bad_coords` (`lat: 95` → 422).
  - `test_geocode_endpoint_caches_by_normalised_query`: monkeypatch `travel.geocode` with a counter; GET `q=Falkenberger Str 143G` then `q=  falkenberger   str 143g ` → one call, same body.
  - `test_geocode_endpoint_upstream_error_is_502`.
  - `test_geocode_parses_nominatim` (MockTransport, two results → labels/lat/lng as floats; User-Agent header == `travel.USER_AGENT`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): home and bike speed settings, address geocoding`.

### Task 9: Travel worker

**Files:**
- Modify: `backend/travel.py` (`TravelWorker`)
- Modify: `backend/server.py` (lifespan start/stop, warm on home change, warm from `prefetch_cycle`)
- Test: `backend/test_travel.py`, `backend/test_server.py`

**Interfaces:**
- Consumes: Task 6/7 functions and storage.
- Produces in `travel.py`:

```python
class TravelWorker:
    def __init__(self, client: httpx.AsyncClient, vbb_pacer: Pacer, now=time.time): ...
    def set_origin(self, lat: float, lng: float) -> None   # changes current origin_key; queued *warm* (p2) jobs of other origins are dropped when dequeued
    def request(self, venue_ids: list[str], origin: tuple[float, float] | None = None) -> None   # p1; origin defaults to current; coords via storage.get_venue_points
    def warm(self, points: list[tuple[str, float, float]]) -> None      # priority 2, sorted nearest-first by haversine from origin
    async def exact(self, venue_id: str, dest: tuple[float, float], arrive_by: datetime) -> dict  # priority 0; resolves a Future; raises TransitUnavailable
    def is_pending(self, venue_id: str) -> bool
    async def run(self) -> None   # forever loop
```

  - Queue items: `(priority, seq, origin_key, kind, payload)` with `kind in {"bike", "transit", "exact"}`; de-dup key `(origin_key, kind, venue_id[, arrive_by])`.
  - `request`/`warm` read `storage.get_travel` and enqueue: one `"bike"` job per batch of up to 100 venues whose `bike_at` is missing or older than `BIKE_TTL`; one `"transit"` job per venue in the VBB area whose `pt_at` is missing/older than `PT_TTL` and whose `pt_failed_at` is missing/older than `PT_RETRY_AFTER`.
  - `"transit"`: `fetch_journeys(..., departure=next_reference_departure(now))` → `pick_typical` → `set_transit`, or `mark_transit_failed` on exception / `None`.
  - `"exact"`: `fetch_journeys(..., arrival=arrive_by)` → `pick_exact` → `set_exact` and resolve; failure → set `TransitUnavailable` on the future.
  - Every job catches and logs exceptions (without coordinates) so `run` never dies.
  - `class TransitUnavailable(Exception)`.
- Server wiring: module-level `travel_worker: TravelWorker | None`. Lifespan: create an `httpx.AsyncClient(headers={"User-Agent": travel.USER_AGENT}, timeout=15)`, the worker, `set_origin` from stored home if any, `asyncio.create_task(worker.run())`; cancel on shutdown. `PUT /api/settings` with a changed home → `set_origin` + `warm(list_city_venue_points(prefetch city ids))`. `prefetch_cycle` ends with the same `warm(...)` when a home is set. The `seeded_client` fixture monkeypatches `server.TRAVEL_ENABLED = False` (new module flag checked in lifespan) so tests never start the worker.

- [ ] **Step 1: Write failing tests** (drive the worker with fake `fetch_bike_km` / `fetch_journeys` monkeypatched on `travel`, a real temp storage via the `db`-style fixture, and `asyncio.run` of a helper that starts `run()` as a task, awaits a condition, then cancels):
  - `test_worker_bike_then_transit_stored`.
  - `test_worker_priority_order`: enqueue warm (p2) for A, request (p1) for B, exact (p0) for C before starting → call log order C, B, A for transit kinds.
  - `test_worker_drops_jobs_for_stale_origin`: `warm` for origin X, `set_origin(Y)`, run → no fetch with origin X; storage has no X rows.
  - `test_worker_transit_failure_marks_failed_and_continues`: fake raises for A, returns journeys for B → A has `pt_failed_at`, B has `pt_min`.
  - `test_worker_empty_journeys_marks_failed`.
  - `test_worker_skips_outside_vbb_area`: Hamburg venue → no transit call, bike still fetched.
  - `test_worker_request_skips_fresh_entries`.
  - `test_put_settings_home_change_triggers_warm` (server test: fake worker object records `set_origin`/`warm` calls).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): background travel worker with priority queue`.

### Task 10: Travel endpoints

**Files:**
- Modify: `backend/models.py` (`TravelRequest`, `TravelResult`, `TravelResponse`, `ExactTransit`)
- Modify: `backend/server.py`
- Test: `backend/test_server.py`

**Interfaces:**
- Produces: `TravelRequest(venue_ids: list[str] = Field(max_length=500), origin: LatLng | None = None)`; `TravelResult(bike_km: float | None, pt_min: int | None, pt_lines: list[str], walk_m: int | None)`; `TravelResponse(results: dict[str, TravelResult], pending: list[str])`; `ExactTransit(pt_min: int, depart_at: str, arrive_at: str, lines: list[str], walk_m: int)`.
- `POST /api/travel`: origin = body origin or stored home (neither → 409 "Set a home first"). If body origin differs from the worker's origin, call `worker.request` with that origin's key (pass it as `worker.request(ids, origin=...)`; explicitly requested jobs are processed even for a non-current origin). Results = cached entries for known venues with coordinates; `pending` = ids enqueued now or `worker.is_pending`. Unknown ids / no coordinates → in neither list.
- `GET /api/travel/exact?venue_id=&date=YYYY-MM-DD&start=HH:MM`: stored home required (409); unknown venue → 404; outside VBB area → 404 "No transit data for this area"; `arrive_by = date+start (Europe/Berlin) − 5 min`; cached `get_exact` (24 h) → return; else `await asyncio.wait_for(worker.exact(...), 10)`; timeout → 504; `TransitUnavailable` → 502.

- [ ] **Step 1: Write failing tests** (fake worker injected via `monkeypatch.setattr(server, "travel_worker", fake)`; seed venues through `storage.upsert_venues` with `_make_venue`):
  - `test_travel_requires_home`: no home, no origin → 409.
  - `test_travel_returns_cached_and_pending`: venue A cached (bike + transit), B uncached → `results` has A, `pending == ["B"]`, fake worker got `request(["B"])`.
  - `test_travel_ignores_unknown_and_coordless_venues`.
  - `test_travel_rejects_more_than_500_ids` → 422.
  - `test_travel_exact_cached_hit_skips_worker`.
  - `test_travel_exact_timeout_is_504` (fake `exact` sleeps; patch the 10 s constant `EXACT_TIMEOUT` to 0.05).
  - `test_travel_exact_unavailable_is_502`; `test_travel_exact_unknown_venue_is_404`.
  - `test_travel_exact_arrive_by_is_five_minutes_before_start`: fake records `arrive_by` → `2026-10-22T18:55:00+02:00` for `date=2026-10-22&start=19:00`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full suite → PASS.
- [ ] **Step 5: Commit** `feat(backend): travel and exact-transit endpoints`.

### Task 11: Frontend — home, travel chips, exact transit

**Files:** `frontend/index.html`, `frontend/app.js`, `frontend/style.css`

**Interfaces:**
- Consumes: Task 4's `settings`, `saveSettings`, `renderCourseDetails`; endpoints from Tasks 8 and 10.
- Produces: `const travelByVenue = new Map()` (venue id → `TravelResult`); `function travelChipsHtml(venueId) -> string` (empty when no home or no data); `function requestTravel(venueIds)` (debounced 150 ms, de-duplicated, chunks of 500); `function updateTravelChips(venueId)` (rewrites every `[data-travel-venue="id"]`).

Behaviour (spec §4):
- **You panel → Home:** `<input id="home-search">` + Search button (Enter also) → `GET /api/geocode?q=&viewbox=<map bounds as lng1,lat1,lng2,lat2>` → result list; choosing one → `saveSettings({...settings, home})`. "Pick on map" → next map click sets home (cursor crosshair, Esc cancels). Shows the current label and "Clear" (sets `home` to null). 502 → "Search unavailable — set home on the map instead". Bike speed: three toggle buttons 15/18/21 → `saveSettings`; re-render all chips.
- **Map:** `homeLayer = L.layerGroup()` added once; `renderHomeMarker()` draws a draggable `L.marker` with a distinct `L.divIcon` (🏠); `dragend` → save with label "Pinned location". `map.on("contextmenu", e => …)` opens a small popup "Set home here" button at `e.latlng`.
- **On home change:** clear `travelByVenue`, re-request travel for currently rendered list items, re-render chips.
- **Chips:** `🚲 ${round(km / speed * 60 + 2)} min · 🚆 ${pt_min} min (${lines.join(" → ")})`, `🚆 –` when `pt_min` null; `speed = settings.bike_kmh ?? 18`. Wrapper `<div class="travel-chips" data-travel-venue="${id}">`. Insert into venue list items (`venue.address_id`), course list items (`course.venue_id`), `buildPopup`, `buildCoursePopup`. Each render site calls `requestTravel` for its ids. `requestTravel` → `POST /api/travel`; `pending` ids polled every 3 s, giving up after 2 min. If an affected marker popup is open, `setPopupContent` (like the visit-limit refresh in `buildPopup`).
- **Expanded course:** with a home set, `renderCourseDetails` also calls `/api/travel/exact?venue_id&date&start` (memoized per `course.id`); success → replace the transit chip in the details block with `🚆 31 min · leave 18:22 · M4 → U8`; failure/404 → keep typical chip with suffix `(typical, Tue 17:30)`.
- No home → no chips and no travel requests anywhere.

- [ ] **Step 1:** Implement.
- [ ] **Step 2: Lint** → clean.
- [ ] **Step 3: Verify** with the `verify` skill at desktop and phone widths: search "Falkenberger Str. 143G" → home marker at ~52.5579, 13.4857; drag the pin → label "Pinned location" persists after reload; venue popup shows bike chip at once and transit chip within ~1 min; switch bike speed 18 → 21 changes minutes without a network request; expand a course → "leave HH:MM" appears; clear home → chips disappear.
- [ ] **Step 4: Commit** `feat(frontend): home location and travel chips`.

### Task 12: PR B docs and PR

**Files:** `README.md` (endpoints `GET /api/geocode`, `POST /api/travel`, `GET /api/travel/exact`; `backend/travel.py` in project structure; outside services used), `AGENTS.md` (architecture paragraph on the travel worker and priorities; "Rate limiting" list gains OSRM/VBB/Nominatim lines from Global Constraints; cache TTLs; privacy note that home is never logged).

- [ ] **Step 1:** Update docs; check consistency.
- [ ] **Step 2:** Full checks → green.
- [ ] **Step 3: Commit** `docs: home location and travel times`.
- [ ] **Step 4:** superpowers:finishing-a-development-branch; PR titled `feat: home location and travel times`, based on `main` once PR A is merged (otherwise on `feat/course-details`).
