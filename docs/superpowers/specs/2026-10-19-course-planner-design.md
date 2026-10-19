# Course Planner with Recommendation Runs — Design

**Date:** 2026-10-19
**Status:** Approved in conversation 2026-10-19

## Problem

Turnbeutel shows what a tier gets you, but not "what should I do over the
next two weeks". The user wants a loop:

1. Every one or two weeks they run Claude (`/plan-courses`). Claude goes
   through upcoming courses in Turnbeutel's course cache and writes a ranked
   list of suggestions per day.
2. A **Plan** view in the app shows those suggestions per day, sorted by
   rank by default and easy to filter by time of day. The user picks at most
   one course per day.
3. After a picked course, the user leaves feedback: whether they went, how
   much they liked it, whether and when they would go again, and a note.
   They can also mark any suggestion "not interested".
4. The next run reads the feedback history and a preferences profile and
   suggests better courses.

There is one user per instance, so there are no accounts.

This supersedes §4 of the unreviewed draft
`2026-10-18-home-travel-details-planner-design.md`. Course details and travel
times are separate features, handed off in
`docs/superpowers/notes/2026-10-19-course-details-and-travel-times.md` (main
checkout). The planner integrates with them once they exist and works
without them.

## Non-goals

- Server-side ranking. Claude ranks; the app stores and displays.
- Confidence scores. A per-day rank is enough.
- Picks that aren't suggestions, `.ics`/text export, multi-user support.
- An MCP server. Claude uses the HTTP API.

## Architecture

```
/plan-courses (Claude, project skill)
   │  curl + Bearer token
   ▼
FastAPI  /api/planner/*  ──►  planner_store.py ──► backend/data/planner.db
   │            ▲                       (user data, never purged)
   │            └── snapshots from storage.py (usc.db course cache)
   ▼
frontend/planner.js  (Plan tab in the existing app)
```

### Storage: `backend/planner_store.py`

- A separate SQLite file, so that cache maintenance can never destroy
  feedback: `usc.db` is a cache, `purge_stale_courses` drops old courses, and
  `make clean` wipes `backend/cache/*`.
- Path: env `PLANNER_DB`, default `backend/data/planner.db`. `data/` is
  already gitignored. Docker gets a second named volume `planner-data`
  mounted at `/app/backend/data`, and the Dockerfile creates that directory
  owned by `appuser`.
- Same pattern as `storage.py`: one shared connection, every access holds
  the module's `_lock`.

Tables:

| Table | Columns |
|---|---|
| `profile` | single row: `text` (markdown), `updated_at` |
| `runs` | `id`, `created_at`, `date_from`, `date_to`, `summary` |
| `suggestions` | `id`, `run_id`, `course_id`, `date`, `rank`, `reason`, `tags` (JSON list), snapshot: `title`, `start_time`, `end_time`, `venue_id`, `venue_name`, `district`, `category`, `city_id`, `lat`, `lng`; flags `picked`, `not_interested`; `created_at` |
| `feedback` | `suggestion_id` (PK, FK), `went` (`yes`/`no`/`came_up`), `no_reason` (`cancelled`/`full`/`didnt_feel_like_it`/`other` or null), `rating` (1–5 or null), `again` (`no`/`maybe`/`yes` or null), `again_after` (`1w`/`2-4w`/`1-3m`/`someday` or null), `note`, `updated_at` |

Rules:

- **One pick per date.** Picking a suggestion unpicks any other pick on the
  same date, in one transaction.
- **Replacement on a new run.** For each date in `[date_from, date_to]`, the
  new run deletes earlier suggestions for that date that are *untouched*:
  not picked, not marked not interested, and without feedback. Touched ones
  stay, because they are history and signal. If a new suggestion has the same
  (course_id, date) as a kept one, the kept one is updated (rank, reason,
  tags, run_id) instead of being duplicated.
- **Feedback validation.** `rating` only if `went = yes`. `no_reason` only if
  `went = no`. `again_after` only if `again` is `maybe`/`yes`. Feedback is only
  allowed on picked suggestions whose date is today or earlier (409
  otherwise).
- A suggestion with a pick or feedback can't be deleted (409).

### API: `/api/planner/*` in `server.py`

Auth: env `PLANNER_TOKEN`. When unset, every planner endpoint except
`status` returns **503** `{"detail": "planner disabled: PLANNER_TOKEN not set"}`.
A missing or wrong `Authorization: Bearer …` returns **401**. The token is
compared with `secrets.compare_digest`. Implemented as a FastAPI dependency.

| Method & path | Body / query | Response |
|---|---|---|
| `GET /api/planner/status` | none (no auth) | `{enabled: bool}` |
| `GET /api/planner/profile` | none | `{text, updated_at}` |
| `PUT /api/planner/profile` | `{text}` | `{text, updated_at}` |
| `GET /api/planner/history` | none | `{runs: [...], suggestions: [...]}`, every suggestion that is picked, not interested, or has feedback, newest date first, with feedback embedded |
| `POST /api/planner/runs` | `{date_from, date_to, summary, suggestions: [{course_id, date, rank, reason, tags}]}` | `{run, suggestions}`; **422** listing every `(course_id, date)` not found in the course cache, nothing written |
| `GET /api/planner/suggestions` | `from`, `to` (ISO dates) | `{runs, suggestions}`, each suggestion with snapshot, flags, feedback and live `free_spots`/`max_spots` from the course cache (null when no longer cached) |
| `PATCH /api/planner/suggestions/{id}` | any of `rank, reason, tags, picked, not_interested` | the suggestion |
| `DELETE /api/planner/suggestions/{id}` | none | 204; 409 if picked or has feedback |
| `PUT /api/planner/suggestions/{id}/feedback` | feedback fields | the suggestion with feedback; 422 on invalid combinations; 409 if not picked or in the future |

Snapshots are built server-side from the course cache. A new storage helper,
`storage.find_courses(pairs: list[tuple[int, str]]) -> dict[(course_id, date),
(city_id, Course)]`, looks them up across cities.

Candidate research uses the existing public `GET /api/courses`; there is no
new search endpoint.

### Recommendation run: `.claude/skills/plan-courses/SKILL.md`

A project skill, committed (`.gitignore` already whitelists
`.claude/skills/`). It needs `TURNBEUTEL_URL` and `PLANNER_TOKEN` in the
environment.

1. Load the profile, the history, and the latest run's summary.
2. Digest the feedback:
   - Ratings and notes show what to do more or less of.
   - `again` + `again_after` decide when a class or venue is eligible again;
     "no" excludes it.
   - Not-interested marks count against similar suggestions.
   - `came_up` is neutral.
   - `no: full` means suggest again, with a "book early" tag.
3. Window: tomorrow to +13 days unless given. Cities come from the profile.
4. Candidates: `GET /api/courses` for the window. Apply the profile's hard
   rules (initially: no women-only/FLINTA*-only classes; exclude Plus
   check-ins, classes above the user's tier, online and full classes). Skip
   venues whose monthly visit limit (from `/api/venues`, the user's tier) is
   already used up by picks.
5. Rank per day: up to N suggestions (profile, default 6). Mix `repeat`
   (liked and due again) and `new` (exploration). Each suggestion gets a
   one- or two-sentence reason and tags. Use the course-details and travel
   endpoints when they exist: warm travel times for the shortlisted venues.
6. Write with `POST /api/planner/runs`, including a summary. On 422, fix the
   request and retry.
7. Propose a profile diff of what was learned; `PUT` it only after the user
   approves in chat.
8. Report the highlights per day and the Plan view link.

A small helper script, `.claude/skills/plan-courses/candidates.py`, downloads
`/api/courses` for the window and prints a compact table per day (id, time,
title, venue, district, category, spots). The 16 MB payload is filtered
locally, not in the conversation.

The initial profile is seeded in the skill as a template the first run PUTs
when the profile is empty: Berlin, corporate M Pro, one check-in per day, no
women/FLINTA*-only classes, likes dance and unusual activities, home in
Weißensee.

### Plan view: `frontend/planner.js`

- A new file loaded before `app.js`. It exposes a small `planner` object
  (`init`, `show`, `hide`). `app.js` changes are minimal: a third tab
  (hidden until `/api/planner/status` returns enabled), `switchView`
  delegating to `planner.show()`/`hide()`, and a map hook so the planner can
  draw its own marker layer. `init()` stays the last statement of `app.js`.
- **Token:** prompted on first open and stored in localStorage
  (`turnbeutel.plannerToken`). A 401 prompts again.
- **Range:** today −14 days to today +13 days.
- **Feedback due:** a banner listing past picks without feedback; tapping
  one jumps to its day with the form open.
- **Day strip:** one chip per day with suggestions or a pick. A chip shows
  the picked venue or the suggestion count; a dot means feedback is due.
  It defaults to today, or the first day with suggestions.
- **Cards** for the selected day, sorted by rank (toggle: by time). Each card
  shows:
  - rank, time, title, venue · district, category
  - reason, tags as badges, live spots ("full" or "n left")
  - "n-th visit this month at <venue>" when other picks in the same
    calendar month share the venue
  - Pick/Unpick and ✕ (not interested); not-interested cards collapse
    into "n hidden" with a toggle
  - tapping a card focuses its venue on the map
- **Time filter:** chips morning (<12:00) / afternoon (12–17) / evening
  (≥17:00), multi-select, none selected = all, persisted in localStorage. It
  applies to cards, not to the pick shown on the strip.
- **Map:** while the Plan tab is active, the venue/course markers are
  replaced by the selected day's suggestions as rank-numbered markers.
- **Feedback form** inline on a past picked card: went (yes / no /
  something came up), no-reason chips if no, rating stars if yes, again (no
  / maybe / yes), when (if not no), note. Save → `PUT …/feedback`.
- **States:** disabled (tab hidden), token prompt, backend error,
  "No suggestions yet — run /plan-courses".
- **Mobile:** lives in the bottom sheet like the other views.

## Integration with course details and travel times

Added after rebasing onto #19 (course details) and #20 (travel times):

- Suggestions also snapshot `tiers_private`, `tiers_corporate`,
  `min_tier_private`, `min_tier_corporate`, `extra_fee_text` and
  `extra_fee_eur` from the course.
- Plan cards show app.js's fee and upgrade badges and travel chips. Tapping a
  card opens the same details block as in the Courses list: description,
  rating, exact connection. On mobile, the first tap only expands it. The
  "n-th visit" note includes the plan's monthly limit at that venue and is
  highlighted when a pick exceeds it.
- `POST /api/planner/runs` queues travel times for all suggested venues
  (priority "request") when a home is set, so they're precomputed by the
  time the Plan view opens.
- The skill reads plan, home and bike speed from `/api/settings`.
  `candidates.py` checks the class's own tiers, flags on-site fees, and with
  `--travel` adds bike and transit minutes (only for listed rows with
  `--list`, since transit lookups are paced).

## Testing

- `backend/test_planner_store.py`: replacement rule, one pick per date,
  feedback validation, suggestions survive a course-cache purge (separate DB).
- `backend/test_server.py`: auth (503 disabled, 401 wrong token, OK),
  snapshots from the cache, 422 for unknown courses with nothing written,
  409s, live spots join.
- Frontend: end-to-end with the `verify` skill.
- The skill: one real run against a local instance before merging.

## Delivery

One PR: `feat: course planner with recommendation runs and feedback`. It
updates README (endpoints, env vars, volume, skill) and AGENTS.md
(architecture, planner design decision).
