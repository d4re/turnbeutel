# Course Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Plan view with ranked per-day course suggestions written by Claude, with picks and feedback stored server-side and read back by the next `/plan-courses` run.

**Architecture:** New `planner_store.py` (its own SQLite file), new token-protected `/api/planner/*` endpoints in `server.py`, new `frontend/planner.js` hooked into the existing view switcher, and a project skill `.claude/skills/plan-courses/`.

**Tech Stack:** FastAPI + Pydantic + sqlite3 (backend), vanilla JS + Leaflet (frontend), pytest, ruff, ESLint 9.

**Spec:** `docs/superpowers/specs/2026-10-19-course-planner-design.md`

## Global Constraints

- Disabled planner (no `PLANNER_TOKEN`): 503 `{"detail": "planner disabled: PLANNER_TOKEN not set"}`; bad or missing bearer: 401; token compared with `secrets.compare_digest`.
- Planner DB path: env `PLANNER_DB`, default `backend/data/planner.db`; every DB access holds `planner_store._lock`.
- Enumerations: went `yes|no|came_up`; no_reason `cancelled|full|didnt_feel_like_it|other`; rating 1–5; again `no|maybe|yes`; again_after `1w|2-4w|1-3m|someday`.
- Frontend stays zero-build; `init()` stays the last statement in `app.js`.
- Don't add USC traffic: the planner only reads the existing course cache.
- PR title: `feat: course planner with recommendation runs and feedback`.

## Review Focus

- A re-run whose suggestions overlap a kept (picked) suggestion with the same `(course_id, date)`: expect an update of the kept row, not a duplicate → test in Task 1.
- Picking a second suggestion on the same date: the first one is unpicked → Task 1.
- Feedback combinations that contradict each other (rating with `went=no`, `again_after` with `again=no`): 422 → Task 2.
- A suggested course that has since been purged from the course cache: the Plan view still renders it from its snapshot, with spots `null` → Task 2.
- The token stored in the browser is wrong or rotated: the view asks for it again instead of showing an empty plan → Task 3 (verify).

---

### Task 1: Planner storage

**Files:**
- Create: `backend/planner_store.py`, `backend/test_planner_store.py`
- Modify: `backend/models.py` (planner models), `backend/storage.py` (`find_courses`), `backend/test_storage.py`

**Interfaces:**
- Produces (models.py):
  - `Feedback(went, no_reason=None, rating=None, again=None, again_after=None, note="")` with a `model_validator` that raises on contradictory combinations (see Global Constraints and the spec's validation rules); `updated_at: float | None = None`.
  - `NewSuggestion(course_id: int, date: str, rank: int, reason: str = "", tags: list[str] = [])`
  - `RunCreate(date_from: str, date_to: str, summary: str = "", suggestions: list[NewSuggestion])`
  - `Run(id: int, created_at: float, date_from: str, date_to: str, summary: str)`
  - `Suggestion(id, run_id, course_id, date, rank, reason, tags, title, start_time, end_time, venue_id, venue_name, district, category, city_id, lat, lng, picked: bool, not_interested: bool, created_at, feedback: Feedback | None, free_spots: int | None = None, max_spots: int | None = None)`
  - `SuggestionPatch(rank, reason, tags, picked, not_interested)`, all optional
  - `Profile(text: str, updated_at: float | None)`
- Produces (storage.py): `find_courses(pairs: list[tuple[int, str]]) -> dict[tuple[int, str], tuple[int, Course]]`, mapping `(course_id, date)` → `(city_id, Course)`.
- Produces (planner_store.py), all synchronous:
  - `init(db_path: Path)`, `close()`
  - `get_profile() -> Profile`, `set_profile(text: str, now: float) -> Profile`
  - `create_run(run: RunCreate, snapshots: dict[tuple[int, str], tuple[int, Course]], now: float) -> tuple[Run, list[Suggestion]]`
  - `list_runs() -> list[Run]` (newest first)
  - `list_suggestions(date_from: str, date_to: str) -> list[Suggestion]` (by date, rank)
  - `list_history() -> list[Suggestion]` (touched only, newest date first)
  - `get_suggestion(sid: int) -> Suggestion | None`
  - `update_suggestion(sid: int, patch: SuggestionPatch) -> Suggestion`
  - `delete_suggestion(sid: int) -> None`
  - `set_feedback(sid: int, fb: Feedback, today: str, now: float) -> Suggestion`
  - Exceptions `NotFoundError(LookupError)`, `ConflictError(Exception)`

- [ ] **Step 1: Write the failing tests** in `backend/test_planner_store.py` (fixture: `planner_store.init(tmp_path / "planner.db")`, a `course(id, date, venue_id="v1")` factory):
  - `test_profile_defaults_empty_then_roundtrips`: `get_profile().text == ""` and `updated_at is None`; after `set_profile("x", 5.0)`, it returns `text == "x"` and `updated_at == 5.0`.
  - `test_create_run_snapshots_course_fields`: suggestion title/venue_name/start_time/city_id come from the snapshot; `tags` roundtrip as a list.
  - `test_rerun_replaces_untouched_keeps_touched`: run A on 10-20 with courses 1, 2, 3; pick 1, mark 2 not interested; run B on 10-20 with course 4 → the 10-20 suggestions are {1, 2, 4}.
  - `test_rerun_outside_window_keeps_other_dates`: run B covering only 10-21 leaves untouched suggestions for 10-20 alone.
  - `test_rerun_same_course_updates_kept_row`: pick course 1 (rank 3), then run B with course 1 at rank 1 → one row, rank 1, the same id, `picked` still true, `run_id` = B.
  - `test_pick_unpicks_other_on_same_date`: pick s1, then s2 (same date) → s1.picked False; a pick on another date stays.
  - `test_delete_touched_raises_conflict`, `test_delete_untouched_removes`.
  - `test_feedback_requires_pick_and_past_date`: unpicked → ConflictError; picked with date > today → ConflictError; picked and past → stored, and `list_history()` includes it with feedback.
  - `test_unknown_id_raises_not_found` for update, delete and feedback.
  - `test_feedback_model_rejects_contradictions` (models): `Feedback(went="no", rating=4)`, `Feedback(went="yes", no_reason="full")`, `Feedback(went="yes", again="no", again_after="1w")` and `rating=6` all raise `ValidationError`.
  - In `test_storage.py`: `test_find_courses_across_cities` (city 1 has course 10 on d1, city 2 has course 20 on d1 → both found with the right city_id; a missing pair is absent).

- [ ] **Step 2: Run** `cd backend && uv run pytest test_planner_store.py test_storage.py -q`. Expected: FAIL (import errors).

- [ ] **Step 3: Implement.** Mirror `storage.py`'s lifecycle (WAL, `foreign_keys=ON`, `_fetchone`/`_fetchall` under `_lock`). Writes that touch several rows (`create_run`, picking) run inside one `with _lock:` block and `BEGIN`/`COMMIT`. Schema per the spec. `feedback.suggestion_id` uses `ON DELETE CASCADE`. Index on `suggestions(date)`. "Touched" = `picked OR not_interested OR EXISTS feedback`. `find_courses` builds the query `WHERE (course_id, date) IN (VALUES …)` in chunks of 400 pairs.

- [ ] **Step 4: Run** the same pytest command. Expected: PASS. Then `uv run ruff check . && uv run ruff format --check .`.

- [ ] **Step 5: Commit** `feat(backend): planner storage for suggestions, picks and feedback`.

### Task 2: Planner API

**Files:**
- Modify: `backend/server.py`, `backend/test_server.py`
- Modify: `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml`, `README.md`, `AGENTS.md`

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces: the HTTP API from the spec's table. Response shapes: `GET /status` → `{"enabled": bool}`; profile → `Profile`; history → `{"runs": [Run], "suggestions": [Suggestion]}`; `POST /runs` → `{"run": Run, "suggestions": [Suggestion]}`; `GET /suggestions?from=&to=` → `{"runs": [Run], "suggestions": [Suggestion]}` with `free_spots`/`max_spots` filled from `storage.find_courses`; PATCH/feedback → `Suggestion`; DELETE → 204.
- Config in server.py: `PLANNER_TOKEN = os.environ.get("PLANNER_TOKEN", "")`, `PLANNER_DB = Path(os.environ.get("PLANNER_DB", <backend>/data/planner.db))`. Lifespan calls `planner_store.init` and `close`. A `require_planner` dependency on an `APIRouter(prefix="/api/planner")`, except `/status`. `NotFoundError` → 404, `ConflictError` → 409.
- 422 for unknown courses: `{"detail": {"message": "unknown courses", "missing": [{"course_id": …, "date": …}]}}`, with nothing written.

- [ ] **Step 1: Write the failing tests** in `test_server.py`, using a `planner_client` fixture that extends `seeded_client`: monkeypatch `PLANNER_DB` to tmp, set `PLANNER_TOKEN="t"`, and seed `storage.upsert_courses_for_date` with two Berlin courses on a future date and one on yesterday.
  - `test_planner_status_reports_enabled` (true with a token, false without).
  - `test_planner_disabled_returns_503` (token "" → GET /profile is 503 with the exact detail).
  - `test_planner_requires_bearer` (no header → 401, wrong token → 401, right → 200).
  - `test_post_run_builds_snapshots_from_cache` (title and venue_name equal the cached course's).
  - `test_post_run_unknown_course_422_writes_nothing` (one good, one bad → 422 lists the bad one; GET /suggestions is empty afterwards).
  - `test_suggestions_join_live_spots_and_survive_purge` (free_spots equals the cached value; after deleting the cache rows, the suggestion is still returned with `free_spots is None`).
  - `test_pick_and_feedback_flow` (PATCH picked on yesterday's suggestion → PUT feedback → GET /history contains it).
  - `test_feedback_conflicts_and_validation` (future-date pick → 409; `went=no` + rating → 422).
  - `test_delete_picked_is_409`, `test_unknown_suggestion_404`.

- [ ] **Step 2: Run** `uv run pytest test_server.py -q -k planner`. Expected: FAIL.

- [ ] **Step 3: Implement** the router and handlers in `server.py`, wrapping store calls in `run_in_threadpool`. Today = `date.today().isoformat()`. Register the router before the static mount.

- [ ] **Step 4: Run** the full backend suite and ruff. Expected: all pass.

- [ ] **Step 5: Deployment and docs.**
  - Dockerfile: `mkdir -p /app/backend/cache /app/backend/data` and chown both.
  - Both compose files: `PLANNER_TOKEN: ${PLANNER_TOKEN-}` plus a volume `planner-data:/app/backend/data`.
  - README: the endpoints, `PLANNER_TOKEN`/`PLANNER_DB`, the volume, `/plan-courses`.
  - AGENTS.md: an architecture paragraph and a "Planner" design-decision section (separate DB, Claude ranks, token).

- [ ] **Step 6: Commit** `feat(backend): token-protected planner API`.

### Task 3: Plan view

**Files:**
- Create: `frontend/planner.js`
- Modify: `frontend/index.html` (Plan tab button `data-view="plan"` hidden by default, `<div id="plan-view" style="display:none">`, `<script src="planner.js">` before `app.js`), `frontend/app.js` (`switchView`, `updateFilterBadge`, `init`), `frontend/style.css`, `frontend/eslint.config.js` (`planner` global for app.js; planner.js as `sourceType: "script"`)

**Interfaces:**
- Produces: global `const planner = { init(ctx), show(), hide() }`.
  - `ctx = { map, esc, todayIso, addDaysIso, popupOptions, collapseSheetForMap }`, passed from `init()` in app.js after the map exists.
  - `planner.init` fetches `/api/planner/status` and unhides the tab when enabled.
  - `show()` adds its own `L.layerGroup` to the map and renders. `hide()` removes the layer.
- app.js `switchView("plan")`: hide the venue/course lists and filters, hide `#filters-button`, `markerCluster.clearLayers(); mapMarkersCleared = true;`, call `planner.show()`. Any other view calls `planner.hide()`. `updateFilterBadge` counts 0 in plan.
- localStorage keys: `turnbeutel.plannerToken`, `turnbeutel.planTimeFilter` (JSON array of `"morning"|"afternoon"|"evening"`), `turnbeutel.planSort` (`"rank"|"time"`). Wrap every access in try/catch.

- [ ] **Step 1: Implement `planner.js`** per the spec's Plan view section:
  - token prompt via `window.prompt`, re-prompting on 401
  - feedback-due banner, day strip (today−14 … today+13), cards with Pick/Unpick and ✕, an "n hidden" toggle and the "n-th visit this month" note
  - time-filter chips and the rank/time sort toggle
  - rank-numbered `L.marker` with `L.divIcon` for the selected day; tapping a card opens that marker's popup
  - an inline feedback form with conditional fields
  - empty and error states
  - Re-fetch after each mutation and re-render.

- [ ] **Step 2: Lint** with `cd frontend && npx eslint .`. Expected: no errors.

- [ ] **Step 3: Verify end to end** with the `verify` skill:
  - `PLANNER_TOKEN=t make serve`
  - seed a run through `curl` (POST /api/planner/runs with real cached Berlin courses)
  - in the browser: the tab appears; the token prompt works; cards are ranked; the time filter hides cards; picking replaces the pick; ✕ hides the card; a past pick shows the form and saving clears the banner; a wrong stored token re-prompts; the mobile width works
  - screenshots in the scratchpad

- [ ] **Step 4: Commit** `feat(frontend): plan view for suggestions, picks and feedback`.

### Task 4: `/plan-courses` skill

**Files:**
- Create: `.claude/skills/plan-courses/SKILL.md`, `.claude/skills/plan-courses/candidates.py`

**Interfaces:**
- Consumes: the Task 2 API, `GET /api/courses`, `GET /api/venues`.
- `candidates.py --url URL --from YYYY-MM-DD --days N [--city-id 1]`: stdlib only (urllib, json); run with `python3 -I`. It writes the full JSON to a path given with `--out` and prints a compact table per day: `id | HH:MM-HH:MM | title | venue | district | category | spots | plus | online`. Online classes and full classes are skipped unless `--all` is given.

- [ ] **Step 1: Write SKILL.md** with frontmatter (`name: plan-courses`, a description that triggers on planning or recommending courses). Body: required env vars, steps 1–8 from the spec's run section with the exact curl calls, the feedback-digest rules, the initial profile template, and the "only PUT the profile after the user approves" rule.

- [ ] **Step 2: Write `candidates.py`** and check it against a local instance: `python3 -I .claude/skills/plan-courses/candidates.py --url http://localhost:8000 --from <tomorrow> --days 2 | head`. Expected: rows per day.

- [ ] **Step 3: Dry run** of the skill against the local instance with `PLANNER_TOKEN=t`. Expected: a run is created, `GET /api/planner/suggestions` returns ranked suggestions, and the Plan view shows them.

- [ ] **Step 4: Commit** `feat: plan-courses skill for recommendation runs`.
