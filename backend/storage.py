"""SQLite-backed storage for venues, courses, cities, and categories.

All public functions are synchronous. FastAPI handlers wrap them in
`fastapi.concurrency.run_in_threadpool`. A single process-wide connection is
used (WAL mode), with a `threading.Lock` guarding writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from models import City, Course, Venue, VenueAddress, VenueDetail, VenuesPayload, VisitLimits

# ── Module state ────────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None
_write_lock = threading.Lock()


# ── Lifecycle ───────────────────────────────────────────────────────────────


def init(db_path: Path) -> None:
    """Open the shared connection and run migrations. Idempotent."""
    global _conn
    if _conn is not None:
        _migrate(_conn)
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    _conn = conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("storage.init() has not been called")
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    # Legacy courses table used PRIMARY KEY (city_id, course_id), which crashed
    # whenever USC's /courses endpoint returned the same course id twice on a
    # single day or (defensively) the same id reappeared across dates. The new
    # PK includes date. Detect the old shape and drop the cached table — courses
    # are TTL-cached and will be re-fetched on demand.
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='courses'").fetchone()
    if row and row[0] and "PRIMARY KEY (city_id, course_id, date)" not in row[0]:
        conn.execute("DROP TABLE courses")
        conn.execute("DROP TABLE IF EXISTS course_fetches")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cities (
            id                   INTEGER PRIMARY KEY,
            name                 TEXT NOT NULL,
            country_code         TEXT,
            centroid_lat         REAL,
            centroid_lng         REAL,
            venue_address_count  INTEGER,
            lat_min              REAL,
            lat_max              REAL,
            lng_min              REAL,
            lng_max              REAL,
            fetched_at           REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS city_state (
            city_id             INTEGER PRIMARY KEY REFERENCES cities(id),
            venues_fetched_at   REAL,
            total_venues        INTEGER,
            venues_with_coords  INTEGER
        );

        CREATE TABLE IF NOT EXISTS global_state (
            id                       INTEGER PRIMARY KEY CHECK (id = 1),
            cities_fetched_at        REAL,
            categories_fetched_at    REAL,
            categories_payload_json  TEXT
        );
        INSERT OR IGNORE INTO global_state (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS venues (
            city_id              INTEGER NOT NULL REFERENCES cities(id),
            venue_id             TEXT NOT NULL,
            name                 TEXT,
            slug                 TEXT,
            url                  TEXT,
            street               TEXT,
            postal_code          TEXT,
            address_city         TEXT,
            district             TEXT,
            lat                  REAL,
            lng                  REAL,
            is_plus              INTEGER,
            is_online            INTEGER,
            tiers_private_json   TEXT,
            tiers_corporate_json TEXT,
            min_tier_private     TEXT,
            min_tier_corporate   TEXT,
            activities_json      TEXT,
            rating               REAL,
            review_count         INTEGER,
            has_coordinates      INTEGER,
            fetched_at           REAL NOT NULL,
            PRIMARY KEY (city_id, venue_id)
        );

        CREATE TABLE IF NOT EXISTS venue_details (
            venue_id             TEXT PRIMARY KEY,
            visit_limits_json    TEXT,
            booking_limits_text  TEXT,
            important_info       TEXT,
            phone                TEXT,
            website              TEXT,
            description          TEXT,
            fetched_at           REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS courses (
            city_id      INTEGER NOT NULL REFERENCES cities(id),
            course_id    INTEGER NOT NULL,
            date         TEXT NOT NULL,
            title        TEXT,
            start_time   TEXT,
            end_time     TEXT,
            venue_id     TEXT,
            venue_name   TEXT,
            lat          REAL,
            lng          REAL,
            district     TEXT,
            category     TEXT,
            category_id  INTEGER,
            teacher      TEXT,
            free_spots   INTEGER,
            max_spots    INTEGER,
            is_online    INTEGER,
            is_plus      INTEGER,
            PRIMARY KEY (city_id, course_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_courses_city_date ON courses(city_id, date);

        CREATE TABLE IF NOT EXISTS course_fetches (
            city_id      INTEGER NOT NULL REFERENCES cities(id),
            date         TEXT NOT NULL,
            fetched_at   REAL NOT NULL,
            course_count INTEGER NOT NULL,
            PRIMARY KEY (city_id, date)
        );
    """)


# ── Cities ──────────────────────────────────────────────────────────────────


def upsert_cities(cities: list[City], fetched_at: float) -> None:
    """Bulk UPSERT cities from USC /cities. Preserves any existing bbox columns."""
    with _write_lock:
        conn = _c()
        conn.execute("BEGIN")
        try:
            for city in cities:
                conn.execute(
                    """
                    INSERT INTO cities (
                        id, name, country_code, centroid_lat, centroid_lng,
                        venue_address_count, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name                = excluded.name,
                        country_code        = excluded.country_code,
                        centroid_lat        = excluded.centroid_lat,
                        centroid_lng        = excluded.centroid_lng,
                        venue_address_count = excluded.venue_address_count,
                        fetched_at          = excluded.fetched_at
                    """,
                    (
                        city.id,
                        city.name,
                        city.country_code,
                        city.centroid_lat,
                        city.centroid_lng,
                        city.venue_address_count,
                        fetched_at,
                    ),
                )
            conn.execute(
                "UPDATE global_state SET cities_fetched_at = ? WHERE id = 1",
                (fetched_at,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_cities_fetched_at() -> float | None:
    row = _c().execute("SELECT cities_fetched_at FROM global_state WHERE id = 1").fetchone()
    return row["cities_fetched_at"] if row else None


def list_cities() -> list[City]:
    rows = _c().execute("SELECT * FROM cities ORDER BY id").fetchall()
    return [_row_to_city(r) for r in rows]


def get_city(city_id: int) -> City | None:
    row = _c().execute("SELECT * FROM cities WHERE id = ?", (city_id,)).fetchone()
    return _row_to_city(row) if row else None


def _row_to_city(row: sqlite3.Row) -> City:
    return City(
        id=row["id"],
        name=row["name"],
        country_code=row["country_code"],
        centroid_lat=row["centroid_lat"],
        centroid_lng=row["centroid_lng"],
        venue_address_count=row["venue_address_count"],
        lat_min=row["lat_min"],
        lat_max=row["lat_max"],
        lng_min=row["lng_min"],
        lng_max=row["lng_max"],
    )


# ── Venues ──────────────────────────────────────────────────────────────────


def upsert_venues(
    city_id: int,
    venues: list[Venue],
    fetched_at: float,
    total: int,
    with_coords: int,
) -> None:
    """Replace all venues for a city, update snapshot meta, derive city bbox."""
    with _write_lock:
        conn = _c()
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM venues WHERE city_id = ?", (city_id,))
            conn.executemany(
                """
                INSERT INTO venues (
                    city_id, venue_id, name, slug, url,
                    street, postal_code, address_city, district,
                    lat, lng, is_plus, is_online,
                    tiers_private_json, tiers_corporate_json,
                    min_tier_private, min_tier_corporate,
                    activities_json, rating, review_count,
                    has_coordinates, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_venue_to_row(city_id, v, fetched_at) for v in venues],
            )
            conn.execute(
                """
                INSERT INTO city_state (
                    city_id, venues_fetched_at, total_venues, venues_with_coords
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(city_id) DO UPDATE SET
                    venues_fetched_at  = excluded.venues_fetched_at,
                    total_venues       = excluded.total_venues,
                    venues_with_coords = excluded.venues_with_coords
                """,
                (city_id, fetched_at, total, with_coords),
            )
            # Lazy bbox derivation from the venues just inserted.
            bbox = conn.execute(
                """
                SELECT MIN(lat) AS lat_min, MAX(lat) AS lat_max,
                       MIN(lng) AS lng_min, MAX(lng) AS lng_max
                FROM venues
                WHERE city_id = ? AND lat IS NOT NULL AND lng IS NOT NULL
                """,
                (city_id,),
            ).fetchone()
            if bbox and bbox["lat_min"] is not None:
                conn.execute(
                    """
                    UPDATE cities
                    SET lat_min = ?, lat_max = ?, lng_min = ?, lng_max = ?
                    WHERE id = ?
                    """,
                    (bbox["lat_min"], bbox["lat_max"], bbox["lng_min"], bbox["lng_max"], city_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _venue_to_row(city_id: int, v: Venue, fetched_at: float) -> tuple:
    return (
        city_id,
        v.address_id,
        v.name,
        v.slug,
        v.url,
        v.street,
        v.address.postal_code,
        v.address.city,
        v.district,
        v.lat,
        v.lng,
        int(v.is_plus),
        int(v.is_online),
        json.dumps(v.tiers_private),
        json.dumps(v.tiers_corporate),
        v.min_tier_private,
        v.min_tier_corporate,
        json.dumps(v.activities),
        v.rating,
        v.review_count,
        int(v.has_coordinates),
        fetched_at,
    )


def get_venues_fetched_at(city_id: int) -> float | None:
    row = (
        _c()
        .execute(
            "SELECT venues_fetched_at FROM city_state WHERE city_id = ?",
            (city_id,),
        )
        .fetchone()
    )
    return row["venues_fetched_at"] if row else None


def get_venues_payload(city_id: int) -> VenuesPayload | None:
    """Return the venues snapshot for a city, LEFT JOINed with venue_details.

    `tier_config` on the returned payload is a placeholder; the handler
    overwrites it with the hardcoded global TIER_CONFIG before responding.
    """
    conn = _c()
    meta = conn.execute(
        """
        SELECT venues_fetched_at, total_venues, venues_with_coords
        FROM city_state WHERE city_id = ?
        """,
        (city_id,),
    ).fetchone()
    if not meta or meta["venues_fetched_at"] is None:
        return None
    rows = conn.execute(
        """
        SELECT v.*,
               d.visit_limits_json AS d_visit_limits_json,
               d.booking_limits_text AS d_booking_limits_text
        FROM venues v
        LEFT JOIN venue_details d ON d.venue_id = v.venue_id
        WHERE v.city_id = ?
        ORDER BY v.name
        """,
        (city_id,),
    ).fetchall()
    venues = [_row_to_venue(r) for r in rows]
    return VenuesPayload(
        fetched_at=meta["venues_fetched_at"],
        total_venues=meta["total_venues"] or 0,
        venues_with_coords=meta["venues_with_coords"] or 0,
        tier_config={"private": {}, "corporate": {}},  # handler overwrites
        venues=venues,
    )


def _row_to_venue(row: sqlite3.Row) -> Venue:
    visit_limits = None
    if row["d_visit_limits_json"]:
        visit_limits = VisitLimits.model_validate_json(row["d_visit_limits_json"])
    return Venue(
        name=row["name"] or "",
        slug=row["slug"] or "",
        url=row["url"] or "",
        tiers_private=json.loads(row["tiers_private_json"] or "[]"),
        tiers_corporate=json.loads(row["tiers_corporate_json"] or "[]"),
        min_tier_private=row["min_tier_private"],
        min_tier_corporate=row["min_tier_corporate"],
        activities=json.loads(row["activities_json"] or "[]"),
        district=row["district"] or "",
        street=row["street"] or "",
        is_plus=bool(row["is_plus"]),
        address_id=row["venue_id"],
        lat=row["lat"],
        lng=row["lng"],
        address=VenueAddress(
            street=row["street"] or "",
            postal_code=row["postal_code"] or "",
            city=row["address_city"] or "",
        ),
        rating=row["rating"],
        review_count=row["review_count"],
        is_online=bool(row["is_online"]),
        has_coordinates=bool(row["has_coordinates"]),
        visit_limits=visit_limits,
        bookingLimitsText=row["d_booking_limits_text"],
    )


# ── Venue details (global, not city-scoped) ────────────────────────────────


def get_venue_detail(venue_id: str) -> VenueDetail | None:
    row = (
        _c()
        .execute(
            "SELECT * FROM venue_details WHERE venue_id = ?",
            (venue_id,),
        )
        .fetchone()
    )
    return _row_to_venue_detail(row) if row else None


def _row_to_venue_detail(row: sqlite3.Row) -> VenueDetail:
    visit_limits = None
    if row["visit_limits_json"]:
        visit_limits = VisitLimits.model_validate_json(row["visit_limits_json"])
    return VenueDetail(
        visit_limits=visit_limits,
        bookingLimitsText=row["booking_limits_text"],
        importantInfo=row["important_info"],
        phone=row["phone"],
        website=row["website"],
        description=row["description"],
        fetched_at=row["fetched_at"],
    )


def upsert_venue_detail(venue_id: str, detail: VenueDetail) -> None:
    with _write_lock:
        _c().execute(
            """
            INSERT INTO venue_details (
                venue_id, visit_limits_json, booking_limits_text,
                important_info, phone, website, description, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue_id) DO UPDATE SET
                visit_limits_json   = excluded.visit_limits_json,
                booking_limits_text = excluded.booking_limits_text,
                important_info      = excluded.important_info,
                phone               = excluded.phone,
                website             = excluded.website,
                description         = excluded.description,
                fetched_at          = excluded.fetched_at
            """,
            (
                venue_id,
                detail.visit_limits.model_dump_json() if detail.visit_limits else None,
                detail.bookingLimitsText,
                detail.importantInfo,
                detail.phone,
                detail.website,
                detail.description,
                detail.fetched_at,
            ),
        )


def reparse_visit_limits(parse_fn) -> int:
    """Re-derive visit_limits_json for every venue_details row from its cached
    booking_limits_text using the provided parser. Used when the parser logic
    changes so we don't need to re-fetch from USC. Returns row count updated.
    """
    conn = _c()
    rows = conn.execute("SELECT venue_id, booking_limits_text FROM venue_details").fetchall()
    with _write_lock:
        conn.execute("BEGIN")
        try:
            for r in rows:
                limits = parse_fn(r["booking_limits_text"])
                conn.execute(
                    "UPDATE venue_details SET visit_limits_json = ? WHERE venue_id = ?",
                    (
                        limits.model_dump_json() if limits else None,
                        r["venue_id"],
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(rows)


def list_venue_ids_needing_details(city_id: int, max_age_seconds: float) -> list[str]:
    cutoff = time.time() - max_age_seconds
    rows = (
        _c()
        .execute(
            """
        SELECT v.venue_id
        FROM venues v
        LEFT JOIN venue_details d ON d.venue_id = v.venue_id
        WHERE v.city_id = ?
          AND (d.venue_id IS NULL OR d.fetched_at < ?)
        """,
            (city_id, cutoff),
        )
        .fetchall()
    )
    return [r["venue_id"] for r in rows]


# ── Courses ─────────────────────────────────────────────────────────────────


def get_course_fetches(city_id: int, dates: list[str]) -> dict[str, float]:
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = (
        _c()
        .execute(
            f"""
        SELECT date, fetched_at FROM course_fetches
        WHERE city_id = ? AND date IN ({placeholders})
        """,
            (city_id, *dates),
        )
        .fetchall()
    )
    return {r["date"]: r["fetched_at"] for r in rows}


def get_courses_for_dates(city_id: int, dates: list[str]) -> dict[str, list[Course]]:
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = (
        _c()
        .execute(
            f"""
        SELECT * FROM courses
        WHERE city_id = ? AND date IN ({placeholders})
        ORDER BY date, start_time
        """,
            (city_id, *dates),
        )
        .fetchall()
    )
    out: dict[str, list[Course]] = {d: [] for d in dates}
    for r in rows:
        out[r["date"]].append(_row_to_course(r))
    return out


def _row_to_course(row: sqlite3.Row) -> Course:
    return Course(
        id=row["course_id"],
        date=row["date"],
        title=row["title"] or "",
        start_time=row["start_time"] or "",
        end_time=row["end_time"] or "",
        venue_id=row["venue_id"] or "",
        venue_name=row["venue_name"] or "",
        lat=row["lat"],
        lng=row["lng"],
        district=row["district"] or "",
        category=row["category"] or "",
        category_id=row["category_id"],
        teacher=row["teacher"] or "",
        free_spots=row["free_spots"],
        max_spots=row["max_spots"],
        is_online=bool(row["is_online"]),
        is_plus=bool(row["is_plus"]),
    )


def upsert_courses_for_date(
    city_id: int,
    date: str,
    courses: list[Course],
    fetched_at: float,
) -> None:
    """Atomic replace of all courses for (city_id, date) + fetch ledger entry.

    USC's /courses endpoint occasionally returns the same course_id twice for
    a single day (observed: byte-identical duplicates). Dedupe by course_id
    here so the (city_id, course_id, date) primary key holds.
    """
    seen_ids: set[int] = set()
    deduped: list[Course] = []
    for c in courses:
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        deduped.append(c)
    courses = deduped

    with _write_lock:
        conn = _c()
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM courses WHERE city_id = ? AND date = ?",
                (city_id, date),
            )
            if courses:
                conn.executemany(
                    """
                    INSERT INTO courses (
                        city_id, course_id, date, title, start_time, end_time,
                        venue_id, venue_name, lat, lng, district,
                        category, category_id, teacher, free_spots, max_spots,
                        is_online, is_plus
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            city_id,
                            c.id,
                            c.date,
                            c.title,
                            c.start_time,
                            c.end_time,
                            c.venue_id,
                            c.venue_name,
                            c.lat,
                            c.lng,
                            c.district,
                            c.category,
                            c.category_id,
                            c.teacher,
                            c.free_spots,
                            c.max_spots,
                            int(c.is_online),
                            int(c.is_plus),
                        )
                        for c in courses
                    ],
                )
            conn.execute(
                """
                INSERT INTO course_fetches (city_id, date, fetched_at, course_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(city_id, date) DO UPDATE SET
                    fetched_at   = excluded.fetched_at,
                    course_count = excluded.course_count
                """,
                (city_id, date, fetched_at, len(courses)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def purge_stale_courses(max_age_seconds: float) -> int:
    """Delete course rows + fetch ledger entries older than max_age_seconds."""
    cutoff = time.time() - max_age_seconds
    with _write_lock:
        conn = _c()
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                DELETE FROM courses
                WHERE (city_id, date) IN (
                    SELECT city_id, date FROM course_fetches WHERE fetched_at < ?
                )
                """,
                (cutoff,),
            )
            cur = conn.execute(
                "DELETE FROM course_fetches WHERE fetched_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount or 0
            conn.execute("COMMIT")
            return deleted
        except Exception:
            conn.execute("ROLLBACK")
            raise


# ── Categories (global) ─────────────────────────────────────────────────────


def get_categories(max_age_seconds: float) -> dict | None:
    row = (
        _c().execute("SELECT categories_fetched_at, categories_payload_json FROM global_state WHERE id = 1").fetchone()
    )
    if not row or row["categories_fetched_at"] is None:
        return None
    if (time.time() - row["categories_fetched_at"]) > max_age_seconds:
        return None
    return json.loads(row["categories_payload_json"])


def set_categories(payload: dict, fetched_at: float) -> None:
    with _write_lock:
        _c().execute(
            """
            UPDATE global_state
            SET categories_payload_json = ?, categories_fetched_at = ?
            WHERE id = 1
            """,
            (json.dumps(payload, ensure_ascii=False), fetched_at),
        )
