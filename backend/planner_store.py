"""SQLite store for the planner: profile, recommendation runs, suggestions, feedback.

This is user data, not cache, so it lives in its own database file (see
`PLANNER_DB` in server.py) that cache maintenance never touches. Same access
pattern as storage.py: one shared connection, every access holds `_lock`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from models import Course, Feedback, Profile, Run, RunCreate, Suggestion, SuggestionPatch

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


class NotFoundError(LookupError):
    pass


class ConflictError(Exception):
    pass


# ── Lifecycle ───────────────────────────────────────────────────────────────


def init(db_path: Path) -> None:
    """Open the shared connection and create the schema. Idempotent."""
    global _conn
    if _conn is not None:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profile (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            text        TEXT NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  REAL NOT NULL,
            date_from   TEXT NOT NULL,
            date_to     TEXT NOT NULL,
            summary     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES runs(id),
            course_id       INTEGER NOT NULL,
            date            TEXT NOT NULL,
            rank            INTEGER NOT NULL,
            reason          TEXT NOT NULL,
            tags            TEXT NOT NULL,
            title           TEXT NOT NULL,
            start_time      TEXT NOT NULL,
            end_time        TEXT NOT NULL,
            venue_id        TEXT NOT NULL,
            venue_name      TEXT NOT NULL,
            district        TEXT NOT NULL,
            category        TEXT NOT NULL,
            city_id         INTEGER NOT NULL,
            lat             REAL,
            lng             REAL,
            tiers_private       TEXT NOT NULL DEFAULT '[]',
            tiers_corporate     TEXT NOT NULL DEFAULT '[]',
            min_tier_private    TEXT,
            min_tier_corporate  TEXT,
            extra_fee_text      TEXT,
            extra_fee_eur       INTEGER,
            picked          INTEGER NOT NULL DEFAULT 0,
            not_interested  INTEGER NOT NULL DEFAULT 0,
            created_at      REAL NOT NULL,
            UNIQUE (course_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_suggestions_date ON suggestions(date);
        -- One check-in per day: at most one pick per date.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_suggestions_one_pick ON suggestions(date) WHERE picked = 1;

        CREATE TABLE IF NOT EXISTS feedback (
            suggestion_id  INTEGER PRIMARY KEY REFERENCES suggestions(id) ON DELETE CASCADE,
            went           TEXT NOT NULL,
            no_reason      TEXT,
            rating         INTEGER,
            again          TEXT,
            again_after    TEXT,
            note           TEXT NOT NULL,
            updated_at     REAL NOT NULL
        );
    """)
    _conn = conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("planner_store.init() has not been called")
    return _conn


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """Hold the lock for one write transaction."""
    with _lock:
        conn = _c()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


# A suggestion the user has interacted with is history and must survive re-runs.
_TOUCHED = "(s.picked = 1 OR s.not_interested = 1 OR EXISTS (SELECT 1 FROM feedback f WHERE f.suggestion_id = s.id))"

_SELECT_SUGGESTIONS = """
    SELECT s.*, f.went AS f_went, f.no_reason AS f_no_reason, f.rating AS f_rating,
           f.again AS f_again, f.again_after AS f_again_after, f.note AS f_note,
           f.updated_at AS f_updated_at
    FROM suggestions s LEFT JOIN feedback f ON f.suggestion_id = s.id
"""


def _row_to_suggestion(row: sqlite3.Row) -> Suggestion:
    feedback = None
    if row["f_went"] is not None:
        feedback = Feedback(
            went=row["f_went"],
            no_reason=row["f_no_reason"],
            rating=row["f_rating"],
            again=row["f_again"],
            again_after=row["f_again_after"],
            note=row["f_note"],
            updated_at=row["f_updated_at"],
        )
    return Suggestion(
        id=row["id"],
        run_id=row["run_id"],
        course_id=row["course_id"],
        date=row["date"],
        rank=row["rank"],
        reason=row["reason"],
        tags=json.loads(row["tags"]),
        title=row["title"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        venue_id=row["venue_id"],
        venue_name=row["venue_name"],
        district=row["district"],
        category=row["category"],
        city_id=row["city_id"],
        lat=row["lat"],
        lng=row["lng"],
        tiers_private=json.loads(row["tiers_private"]),
        tiers_corporate=json.loads(row["tiers_corporate"]),
        min_tier_private=row["min_tier_private"],
        min_tier_corporate=row["min_tier_corporate"],
        extra_fee_text=row["extra_fee_text"],
        extra_fee_eur=row["extra_fee_eur"],
        picked=bool(row["picked"]),
        not_interested=bool(row["not_interested"]),
        created_at=row["created_at"],
        feedback=feedback,
    )


def _query_suggestions(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[Suggestion]:
    return [_row_to_suggestion(r) for r in conn.execute(f"{_SELECT_SUGGESTIONS} {where}", params).fetchall()]


def _require(conn: sqlite3.Connection, sid: int) -> Suggestion:
    found = _query_suggestions(conn, "WHERE s.id = ?", (sid,))
    if not found:
        raise NotFoundError(f"suggestion {sid} not found")
    return found[0]


# ── Profile ─────────────────────────────────────────────────────────────────


def get_profile() -> Profile:
    with _lock:
        row = _c().execute("SELECT text, updated_at FROM profile WHERE id = 1").fetchone()
    if row is None:
        return Profile(text="", updated_at=None)
    return Profile(text=row["text"], updated_at=row["updated_at"])


def set_profile(text: str, now: float) -> Profile:
    with _tx() as conn:
        conn.execute(
            """
            INSERT INTO profile (id, text, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at
            """,
            (text, now),
        )
    return Profile(text=text, updated_at=now)


# ── Runs ────────────────────────────────────────────────────────────────────


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        created_at=row["created_at"],
        date_from=row["date_from"],
        date_to=row["date_to"],
        summary=row["summary"],
    )


def create_run(
    run: RunCreate,
    snapshots: dict[tuple[int, str], tuple[int, Course]],
    now: float,
) -> tuple[Run, list[Suggestion]]:
    """Store a run and its suggestions; `snapshots` maps every (course_id, date) to (city_id, Course).

    Untouched suggestions from earlier runs inside the run's window are replaced.
    A kept (touched) suggestion for the same course and date is updated in place.
    """
    missing = [(s.course_id, s.date) for s in run.suggestions if (s.course_id, s.date) not in snapshots]
    if missing:
        raise ValueError(f"no snapshot for {missing}")

    with _tx() as conn:
        run_id = conn.execute(
            "INSERT INTO runs (created_at, date_from, date_to, summary) VALUES (?, ?, ?, ?)",
            (now, run.date_from, run.date_to, run.summary),
        ).lastrowid
        conn.execute(
            f"DELETE FROM suggestions AS s WHERE s.date BETWEEN ? AND ? AND NOT {_TOUCHED}",
            (run.date_from, run.date_to),
        )
        for s in run.suggestions:
            city_id, c = snapshots[(s.course_id, s.date)]
            conn.execute(
                """
                INSERT INTO suggestions (
                    run_id, course_id, date, rank, reason, tags, title, start_time, end_time,
                    venue_id, venue_name, district, category, city_id, lat, lng,
                    tiers_private, tiers_corporate, min_tier_private, min_tier_corporate,
                    extra_fee_text, extra_fee_eur, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_id, date) DO UPDATE SET
                    run_id = excluded.run_id, rank = excluded.rank,
                    reason = excluded.reason, tags = excluded.tags
                """,
                (
                    run_id,
                    s.course_id,
                    s.date,
                    s.rank,
                    s.reason,
                    json.dumps(s.tags),
                    c.title,
                    c.start_time,
                    c.end_time,
                    c.venue_id,
                    c.venue_name,
                    c.district,
                    c.category,
                    city_id,
                    c.lat,
                    c.lng,
                    json.dumps(c.tiers_private),
                    json.dumps(c.tiers_corporate),
                    c.min_tier_private,
                    c.min_tier_corporate,
                    c.extra_fee_text,
                    c.extra_fee_eur,
                    now,
                ),
            )
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        suggestions = _query_suggestions(conn, "WHERE s.run_id = ? ORDER BY s.date, s.rank", (run_id,))
    return _row_to_run(row), suggestions


def list_runs() -> list[Run]:
    with _lock:
        rows = _c().execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC").fetchall()
    return [_row_to_run(r) for r in rows]


# ── Suggestions ─────────────────────────────────────────────────────────────


def list_suggestions(date_from: str, date_to: str) -> list[Suggestion]:
    with _lock:
        return _query_suggestions(
            _c(), "WHERE s.date BETWEEN ? AND ? ORDER BY s.date, s.rank, s.start_time", (date_from, date_to)
        )


def list_history() -> list[Suggestion]:
    """Every suggestion the user interacted with, newest date first."""
    with _lock:
        return _query_suggestions(_c(), f"WHERE {_TOUCHED} ORDER BY s.date DESC, s.rank")


def get_suggestion(sid: int) -> Suggestion | None:
    with _lock:
        found = _query_suggestions(_c(), "WHERE s.id = ?", (sid,))
    return found[0] if found else None


def update_suggestion(sid: int, patch: SuggestionPatch) -> Suggestion:
    with _tx() as conn:
        current = _require(conn, sid)
        changes = patch.model_dump(exclude_none=True)
        if changes.get("picked"):
            # Picking replaces any other pick that day; a pick also undoes "not interested".
            conn.execute("UPDATE suggestions SET picked = 0 WHERE date = ? AND id != ?", (current.date, sid))
            changes.setdefault("not_interested", False)
        if "tags" in changes:
            changes["tags"] = json.dumps(changes["tags"])
        if changes:
            assignments = ", ".join(f"{col} = ?" for col in changes)
            conn.execute(f"UPDATE suggestions SET {assignments} WHERE id = ?", (*changes.values(), sid))
        return _require(conn, sid)


def delete_suggestion(sid: int) -> None:
    with _tx() as conn:
        current = _require(conn, sid)
        if current.picked or current.feedback is not None:
            raise ConflictError("suggestion is picked or has feedback")
        conn.execute("DELETE FROM suggestions WHERE id = ?", (sid,))


def set_feedback(sid: int, fb: Feedback, today: str, now: float) -> Suggestion:
    with _tx() as conn:
        current = _require(conn, sid)
        if not current.picked:
            raise ConflictError("feedback is only allowed on picked suggestions")
        if current.date > today:
            raise ConflictError("feedback is only allowed once the course has taken place")
        conn.execute(
            """
            INSERT INTO feedback (suggestion_id, went, no_reason, rating, again, again_after, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(suggestion_id) DO UPDATE SET
                went = excluded.went, no_reason = excluded.no_reason, rating = excluded.rating,
                again = excluded.again, again_after = excluded.again_after,
                note = excluded.note, updated_at = excluded.updated_at
            """,
            (sid, fb.went, fb.no_reason, fb.rating, fb.again, fb.again_after, fb.note, now),
        )
        return _require(conn, sid)
