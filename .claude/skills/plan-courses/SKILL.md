---
name: plan-courses
description: Recommendation run for the Turnbeutel planner. Reads the user's profile and feedback, goes through the upcoming courses in the course cache, and writes ranked per-day suggestions to the Plan view. Use when the user runs /plan-courses or asks to plan, recommend or suggest courses for the coming days or weeks.
---

# /plan-courses: a recommendation run

You are the recommender. The app only stores and shows what you write. Your
job: for each day in the window, pick a handful of upcoming courses the user
is likely to enjoy, rank them, explain each in a sentence, and learn from
their feedback on earlier picks. Design: `docs/superpowers/specs/2026-10-19-course-planner-design.md`.

## Setup

Needs `TURNBEUTEL_URL` (e.g. `http://localhost:8000` or the deployed URL) and
`PLANNER_TOKEN` in the environment. If either is missing, stop and ask the
user for them. Don't guess the URL. Check that the planner is enabled:

```bash
curl -s "$TURNBEUTEL_URL/api/planner/status"          # {"enabled": true}
curl -s "$TURNBEUTEL_URL/api/settings"                # plan, home, bike_kmh (the app's "You" panel)
```

The user's membership plan, home and bike speed come from `/api/settings`,
not from the profile. If `plan` or `home` is missing, ask the user to set it
in the app's **You** panel before continuing: tier checks and travel times
depend on it.

All planner calls below need `-H "Authorization: Bearer $PLANNER_TOKEN"`.
Keep large responses out of the conversation: save them to the scratchpad
and summarise them with a short script.

## Procedure

### 1. Load context

```bash
curl -s -H "Authorization: Bearer $PLANNER_TOKEN" "$TURNBEUTEL_URL/api/planner/profile"
curl -s -H "Authorization: Bearer $PLANNER_TOKEN" "$TURNBEUTEL_URL/api/planner/history" > <scratchpad>/history.json
```

If the profile text is empty, this is the first run. Show the user the
[initial profile](#initial-profile) and PUT it once they confirm or adjust
it. The latest run's `summary` (first entry of `history.runs`) says what you
were going for last time.

### 2. Digest the feedback

Go through `history.suggestions`. Each one has a course snapshot, `picked`,
`not_interested` and `feedback`. Write down what you learn before looking at
courses:

- `rating` and `note`: what to do more or less of (style, level, teacher,
  venue, time of day, crowd).
- `again` + `again_after`: when a class or venue becomes eligible again,
  counted from the course date. `1w` = 7 days, `2-4w` = 14 days, `1-3m` =
  30 days, `someday` = 60 days. `again = no` excludes that class for good.
  It doesn't exclude the venue, unless the note blames the venue.
- `went = no` with `no_reason = full`: worth suggesting again; tag it `book early`.
- `went = no` with `didnt_feel_like_it`: a mild negative for that kind of
  course or time slot.
- `went = came_up`: neutral. Ignore it.
- `not_interested`: counts against similar courses (same class, same
  activity at a similar venue). Several in one direction is a pattern worth
  writing into the profile.
- A pick in the past without feedback: no signal. Mention it in your report
  so the user can fill it in.

### 3. Window

Default: tomorrow to tomorrow + 12 (13 days; the course cache covers today
to today + 13). The user may give another range. Cities come from the
profile.

### 4. Candidates

Use the helper. It downloads `/api/courses` and `/api/venues`, takes the
plan from `/api/settings`, and by default drops online, Plus, full courses
and courses the plan doesn't cover. The check uses the class's own tiers,
which can be stricter than the venue's. `--travel` adds travel minutes from
home:

```bash
python3 -I .claude/skills/plan-courses/candidates.py --from <date_from> --days <n> \
  --exclude '<profile exclusion regex>' --out <scratchpad>/candidates.json
python3 -I .claude/skills/plan-courses/candidates.py --from <date_from> --days <n> \
  --exclude '<…>' --list --travel --category Dance --grep 'tango|forró'
```

Without `--list` it prints category counts. Use them to decide where to
look, then list narrowly. Rows show `course id | time | title | venue
(venue id) | district | category | free/max spots | venue rating | bike /
transit minutes | flags` (visit limit for the plan, `+N€ on site`, and
`needs L` with `--all`).

Watch out for:
- **Hard rules from the profile** apply to every course. The exclusion
  regex is only a first pass; read the titles. "Female Dancehall" is a
  dance style open to everyone, while "Taijiquan für FLINTA*" is not open
  to the user.
- **All-day slots** (e.g. `00:05-23:55 Forró Free Training`) are check-in
  windows or placeholders, not classes. Only suggest them if the profile
  asks for open-training check-ins.
- **Visit limits:** `4/mo` means 4 check-ins per calendar month at that
  venue on the user's tier. Count the user's picks there this month
  (history) plus your own top-ranked suggestions, and don't plan past the
  limit.
- **On-site fees** are parsed into `extra_fee_eur` (flag `+N€ on site`).
  Rank them lower unless the course is worth it, and mention the fee in
  the reason. The Plan view shows fee and upgrade badges itself, so tags
  don't need to repeat them.
- **Descriptions:** `GET /api/courses/{id}` returns the description, rating
  and cancellation window. Use it for courses whose title doesn't tell you
  enough (level, partner needed, language). It's one USC request per
  course, so only use it for shortlisted ones.
- **Travel:** use `--travel` minutes in the ranking. Prefer short trips on
  weekdays, and accept longer ones for something special. Transit can be
  `-` when VBB is down; then fall back to bike minutes.

### 5. Rank per day

For every day in the window, choose up to N suggestions (profile, default 6):

- Rank 1 is the one you'd pick for the user. Ranks are per day, 1..n.
- Mix **repeat** (liked and due again; tag `repeat`) with **new**
  (exploration; tag `new`). Lean towards new while the history is thin.
- Spread the times so the morning/afternoon/evening filter leaves options.
  Respect the profile's time preferences (e.g. weekday daytime is usually
  work).
- Prefer variety across the window: don't put the same class or venue at
  rank 1 on several days.
- `reason`: one or two sentences in the user's language, specific to this
  course ("Absolute-beginner WCS, the thing you wanted to try; small
  studio, 15 min by tram"). No filler.
- `tags`: short badges such as `new`, `repeat`, `beginner`, `book early`,
  `outdoor`, `social` (fees and upgrades get their own badges).

### 6. Write the run

```bash
curl -s -X POST -H "Authorization: Bearer $PLANNER_TOKEN" -H "Content-Type: application/json" \
  "$TURNBEUTEL_URL/api/planner/runs" -d @<scratchpad>/run.json
```

`run.json`:

```json
{"date_from": "2026-10-20", "date_to": "2026-11-01",
 "summary": "What this run aimed for, and what you learned from feedback.",
 "suggestions": [{"course_id": 107288441, "date": "2026-10-20", "rank": 1,
                  "reason": "…", "tags": ["new", "beginner"]}]}
```

A 422 lists courses that aren't in the cache (wrong id/date pair, or the
day isn't cached yet); fix them and post again. Nothing is written on a
422. Earlier untouched suggestions in the window are replaced. Picked,
dismissed and reviewed ones stay. Posting a run also queues travel times for
all suggested venues, so the Plan view has them ready.

To adjust one suggestion afterwards: `PATCH /api/planner/suggestions/{id}`
with `rank`, `reason` or `tags`, or `DELETE` it if it's untouched.

### 7. Propose profile updates

Draft the profile changes this run's feedback supports: new likes and
dislikes, new hard rules, time preferences. Show them to the user as a
diff. **Only `PUT /api/planner/profile` after the user approves.** The
profile is theirs.

```bash
curl -s -X PUT -H "Authorization: Bearer $PLANNER_TOKEN" -H "Content-Type: application/json" \
  "$TURNBEUTEL_URL/api/planner/profile" -d @<scratchpad>/profile.json   # {"text": "…markdown…"}
```

### 8. Report

In chat:
- two or three highlights per week
- anything you deliberately left out and why
- past picks still missing feedback
- the link: `$TURNBEUTEL_URL` → Plan tab

## Initial profile

Use this on the first run (profile empty). Show it to the user, adjust it,
then PUT it.

```markdown
# Planner profile

## Basics
- Cities: Berlin (USC city id 1)
- Membership, home and bike speed: from /api/settings (currently corporate M Pro; home in Weißensee, tram M4 is the main line).
- Exclude Plus check-ins and courses the plan doesn't cover.
- One check-in per day.
- Suggestions per day: 6

## Hard rules
- No women-only or FLINTA*-only courses (exclusion regex: `women|frauen|ladies|flinta|girls only|nur für frauen`; check titles, the regex has false positives like "Female Dancehall").
- No online courses.

## Likes
- Dance, especially beginner-friendly classes; wants to try absolute-beginner West Coast Swing.
- Exotic or unusual activities worth trying once: circus/aerials, contact improvisation, partner acro, capoeira, stage sword fighting, ice baths, ecstatic dance.

## Dislikes
- (none yet)

## Time preferences
- Weekdays: from 17:00 (work). Weekends: any time.
```
