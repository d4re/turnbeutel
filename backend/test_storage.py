"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import threading
import time

import pytest

import storage
from models import City, Course, CourseDetail, Plan, Settings, Venue, VenueAddress, VenueDetail, VisitLimits


@pytest.fixture
def db(tmp_path):
    storage.close()
    storage.init(tmp_path / "test.db")
    yield storage
    storage.close()


# ── Factories ──────────────────────────────────────────────────────────────


def make_city(city_id: int = 1, name: str = "Berlin", **overrides) -> City:
    base = {
        "id": city_id,
        "name": name,
        "country_code": "DE",
        "centroid_lat": 52.52,
        "centroid_lng": 13.405,
        "venue_address_count": 1985,
    }
    base.update(overrides)
    return City(**base)


def make_venue(venue_id: str = "v1", **overrides) -> Venue:
    base = dict(
        name=f"Venue {venue_id}",
        slug=f"venue-{venue_id}",
        url=f"https://urbansportsclub.com/en/venues/venue-{venue_id}",
        tiers_private=["Classic"],
        tiers_corporate=["M"],
        min_tier_private="Classic",
        min_tier_corporate="M",
        activities=["Yoga"],
        district="Mitte",
        street="Street 1",
        is_plus=False,
        address_id=venue_id,
        lat=52.5,
        lng=13.4,
        address=VenueAddress(street="Street 1", postal_code="10115", city="Berlin, DE"),
        rating=4.5,
        review_count=100,
        is_online=False,
        has_coordinates=True,
    )
    base.update(overrides)
    return Venue(**base)


def make_course(course_id: int = 1, date: str = "2026-04-10", **overrides) -> Course:
    base = dict(
        id=course_id,
        date=date,
        title=f"Course {course_id}",
        start_time="09:00",
        end_time="10:00",
        venue_id="v1",
        venue_name="Venue v1",
        lat=52.5,
        lng=13.4,
        district="Mitte",
        category="Yoga",
        category_id=6,
        teacher="Alice",
        free_spots=10,
        max_spots=20,
        is_online=False,
        is_plus=False,
    )
    base.update(overrides)
    return Course(**base)


def make_detail(**overrides) -> VenueDetail:
    base = dict(
        visit_limits=VisitLimits(
            private={"Essential": None, "Classic": 4, "Premium": None, "Max": 8},
            corporate={"S": None, "M": 4, "L": None, "XL": 8},
        ),
        importantInfo="important",
        phone="+49 30 12345",
        website="https://example.com",
        description="a gym",
        fetched_at=time.time(),
    )
    base.update(overrides)
    return VenueDetail(**base)


# ── 1. Init ─────────────────────────────────────────────────────────────────


def test_init_creates_schema_idempotent(tmp_path):
    storage.close()
    storage.init(tmp_path / "a.db")
    storage.init(tmp_path / "a.db")  # second call must not fail
    storage.close()


# ── 2-4. Cities ─────────────────────────────────────────────────────────────


def test_upsert_cities_bulk_and_idempotent(db):
    cities = [make_city(1, "Berlin"), make_city(2, "Hamburg"), make_city(3, "Munich")]
    db.upsert_cities(cities, fetched_at=1000.0)
    assert len(db.list_cities()) == 3
    db.upsert_cities(cities, fetched_at=2000.0)  # re-upsert
    assert len(db.list_cities()) == 3
    assert db.get_cities_fetched_at() == 2000.0


def test_upsert_cities_preserves_existing_bbox(db):
    db.upsert_cities([make_city(1, "Berlin")], fetched_at=1000.0)
    # Derive bbox via upsert_venues
    venues = [
        make_venue("a", lat=52.4, lng=13.3),
        make_venue("b", lat=52.6, lng=13.5),
    ]
    db.upsert_venues(city_id=1, venues=venues, fetched_at=1000.0, total=2, with_coords=2)
    city_before = db.get_city(1)
    assert city_before.lat_min == 52.4
    assert city_before.lat_max == 52.6

    # Re-upsert from /cities payload — bbox must survive.
    db.upsert_cities([make_city(1, "Berlin")], fetched_at=3000.0)
    city_after = db.get_city(1)
    assert city_after.lat_min == 52.4
    assert city_after.lat_max == 52.6
    assert city_after.lng_min == 13.3
    assert city_after.lng_max == 13.5


def test_get_city_unknown_returns_none(db):
    assert db.get_city(999) is None


# ── 5-8. Venues ─────────────────────────────────────────────────────────────


def test_upsert_venues_then_get_payload_roundtrip(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    venues = [make_venue("a"), make_venue("b"), make_venue("c")]
    db.upsert_venues(city_id=1, venues=venues, fetched_at=1234.0, total=3, with_coords=3)

    payload = db.get_venues_payload(1)
    assert payload is not None
    assert payload.fetched_at == 1234.0
    assert payload.total_venues == 3
    assert payload.venues_with_coords == 3
    assert len(payload.venues) == 3
    assert {v.address_id for v in payload.venues} == {"a", "b", "c"}
    # Core fields survive the roundtrip.
    v = next(v for v in payload.venues if v.address_id == "a")
    assert v.tiers_private == ["Classic"]
    assert v.tiers_corporate == ["M"]
    assert v.activities == ["Yoga"]
    assert v.address.postal_code == "10115"


def test_upsert_venues_does_not_touch_venue_details(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    db.upsert_venues(city_id=1, venues=[make_venue("a")], fetched_at=1000.0, total=1, with_coords=1)
    detail = make_detail(description="original")
    db.upsert_venue_detail("a", detail)

    # Re-run venue upsert and confirm details still present.
    db.upsert_venues(city_id=1, venues=[make_venue("a")], fetched_at=2000.0, total=1, with_coords=1)
    stored = db.get_venue_detail("a")
    assert stored is not None
    assert stored.description == "original"


def test_upsert_venues_derives_city_bbox(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    venues = [
        make_venue("a", lat=52.3, lng=13.1),
        make_venue("b", lat=52.7, lng=13.6),
        make_venue("c", lat=52.5, lng=13.4),
    ]
    db.upsert_venues(city_id=1, venues=venues, fetched_at=1000.0, total=3, with_coords=3)
    city = db.get_city(1)
    assert city.lat_min == 52.3
    assert city.lat_max == 52.7
    assert city.lng_min == 13.1
    assert city.lng_max == 13.6


def test_venue_details_are_shared_across_cities(db):
    db.upsert_cities([make_city(1, "Berlin"), make_city(2, "Hamburg")], fetched_at=1000.0)
    db.upsert_venues(city_id=1, venues=[make_venue("shared")], fetched_at=1000.0, total=1, with_coords=1)
    db.upsert_venues(city_id=2, venues=[make_venue("shared")], fetched_at=1000.0, total=1, with_coords=1)
    db.upsert_venue_detail("shared", make_detail(description="global detail"))

    for city_id in (1, 2):
        payload = db.get_venues_payload(city_id)
        assert payload is not None
        assert len(payload.venues) == 1
        assert payload.venues[0].visit_limits is not None
        assert payload.venues[0].visit_limits.corporate["M"] == 4


# ── 9-10. Venue details listing ─────────────────────────────────────────────


def test_get_venues_fetched_at_none_then_recent(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    assert db.get_venues_fetched_at(1) is None
    db.upsert_venues(city_id=1, venues=[make_venue("a")], fetched_at=1234.0, total=1, with_coords=1)
    assert db.get_venues_fetched_at(1) == 1234.0


def test_list_venue_ids_needing_details(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    venues = [make_venue("a"), make_venue("b"), make_venue("c")]
    db.upsert_venues(city_id=1, venues=venues, fetched_at=1000.0, total=3, with_coords=3)

    # None enriched yet → all 3 returned.
    assert set(db.list_venue_ids_needing_details(1, max_age_seconds=3600)) == {"a", "b", "c"}

    # Enrich one → only 2 remaining.
    db.upsert_venue_detail("b", make_detail(fetched_at=time.time()))
    assert set(db.list_venue_ids_needing_details(1, max_age_seconds=3600)) == {"a", "c"}

    # Age the enriched row past the max_age cutoff → all 3 returned again.
    db.upsert_venue_detail("b", make_detail(fetched_at=time.time() - 7200))
    assert set(db.list_venue_ids_needing_details(1, max_age_seconds=3600)) == {"a", "b", "c"}


# ── 11-14. Courses ──────────────────────────────────────────────────────────


def test_upsert_courses_for_date_replaces_previous(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    first = [make_course(i) for i in range(1, 6)]
    db.upsert_courses_for_date(1, "2026-04-10", first, fetched_at=1000.0)
    replacement = [make_course(i) for i in range(100, 103)]
    db.upsert_courses_for_date(1, "2026-04-10", replacement, fetched_at=2000.0)

    got = db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"]
    assert {c.id for c in got} == {100, 101, 102}


def test_upsert_courses_for_date_dedupes_within_payload(db):
    """USC sometimes returns the same course id twice in one day's payload."""
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    courses = [
        make_course(1, date="2026-04-10"),
        make_course(2, date="2026-04-10"),
        make_course(1, date="2026-04-10"),  # duplicate
    ]
    db.upsert_courses_for_date(1, "2026-04-10", courses, fetched_at=1000.0)
    got = db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"]
    assert {c.id for c in got} == {1, 2}


def test_upsert_courses_same_id_on_different_dates(db):
    """Defensive: if USC ever reuses a course id across dates, both rows must coexist."""
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    db.upsert_courses_for_date(1, "2026-04-10", [make_course(42, date="2026-04-10")], fetched_at=1000.0)
    db.upsert_courses_for_date(1, "2026-04-11", [make_course(42, date="2026-04-11")], fetched_at=1000.0)
    got = db.get_courses_for_dates(1, ["2026-04-10", "2026-04-11"])
    assert [c.id for c in got["2026-04-10"]] == [42]
    assert [c.id for c in got["2026-04-11"]] == [42]


def test_get_courses_for_dates_returns_only_requested_dates(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    db.upsert_courses_for_date(1, "2026-04-10", [make_course(1, date="2026-04-10")], fetched_at=1000.0)
    db.upsert_courses_for_date(1, "2026-04-11", [make_course(2, date="2026-04-11")], fetched_at=1000.0)
    db.upsert_courses_for_date(1, "2026-04-12", [make_course(3, date="2026-04-12")], fetched_at=1000.0)

    got = db.get_courses_for_dates(1, ["2026-04-10", "2026-04-12"])
    assert set(got.keys()) == {"2026-04-10", "2026-04-12"}
    assert [c.id for c in got["2026-04-10"]] == [1]
    assert [c.id for c in got["2026-04-12"]] == [3]


def test_get_course_fetches_distinguishes_missing_vs_empty(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    db.upsert_courses_for_date(1, "2026-04-10", [], fetched_at=1500.0)  # fetched, empty

    fetches = db.get_course_fetches(1, ["2026-04-10", "2026-04-11"])
    assert fetches == {"2026-04-10": 1500.0}  # never-fetched date is absent
    # Empty-but-fetched day reads back as an empty list, not a cache miss.
    assert db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"] == []


def test_purge_stale_courses_deletes_old_fetches_and_rows(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    now = time.time()
    db.upsert_courses_for_date(1, "2026-04-10", [make_course(1, date="2026-04-10")], fetched_at=now - 10 * 24 * 3600)
    db.upsert_courses_for_date(1, "2026-04-11", [make_course(2, date="2026-04-11")], fetched_at=now)

    deleted = db.purge_stale_courses(max_age_seconds=3 * 24 * 3600)
    assert deleted == 1
    fetches = db.get_course_fetches(1, ["2026-04-10", "2026-04-11"])
    assert set(fetches.keys()) == {"2026-04-11"}
    assert db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"] == []


# ── 15. Categories ──────────────────────────────────────────────────────────


def test_categories_roundtrip_and_ttl(db):
    assert db.get_categories(max_age_seconds=3600) is None
    db.set_categories({"data": [{"id": 1, "name": "Yoga"}]}, fetched_at=time.time())
    got = db.get_categories(max_age_seconds=3600)
    assert got == {"data": [{"id": 1, "name": "Yoga"}]}
    # Past the TTL the helper returns None.
    db.set_categories({"data": []}, fetched_at=time.time() - 7200)
    assert db.get_categories(max_age_seconds=3600) is None


# ── 16. Multi-city isolation ────────────────────────────────────────────────


def test_multi_city_isolation_for_venues_and_courses(db):
    db.upsert_cities([make_city(1, "Berlin"), make_city(2, "Hamburg")], fetched_at=1000.0)
    db.upsert_venues(city_id=1, venues=[make_venue("berlin-1")], fetched_at=1000.0, total=1, with_coords=1)
    db.upsert_venues(
        city_id=2, venues=[make_venue("hh-1"), make_venue("hh-2")], fetched_at=1000.0, total=2, with_coords=2
    )
    db.upsert_courses_for_date(
        1,
        "2026-04-10",
        [make_course(1, date="2026-04-10"), make_course(2, date="2026-04-10")],
        fetched_at=1000.0,
    )
    db.upsert_courses_for_date(2, "2026-04-10", [make_course(99, date="2026-04-10")], fetched_at=1000.0)

    berlin = db.get_venues_payload(1)
    hamburg = db.get_venues_payload(2)
    assert [v.address_id for v in berlin.venues] == ["berlin-1"]
    assert {v.address_id for v in hamburg.venues} == {"hh-1", "hh-2"}

    b_courses = db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"]
    h_courses = db.get_courses_for_dates(2, ["2026-04-10"])["2026-04-10"]
    assert {c.id for c in b_courses} == {1, 2}
    assert {c.id for c in h_courses} == {99}


# ── 17. Concurrent writes ───────────────────────────────────────────────────


def test_concurrent_writes_do_not_corrupt(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    venues = [make_venue(f"v{i}") for i in range(10)]
    db.upsert_venues(city_id=1, venues=venues, fetched_at=1000.0, total=10, with_coords=10)

    def worker(i: int) -> None:
        db.upsert_venue_detail(f"v{i}", make_detail(description=f"desc-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(10):
        detail = db.get_venue_detail(f"v{i}")
        assert detail is not None
        assert detail.description == f"desc-{i}"


def test_concurrent_reads_do_not_raise(db):
    # The connection is shared across threadpool workers; unserialized reads
    # raised sqlite3.InterfaceError ("bad parameter or other API misuse") on
    # Python 3.14.
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    venues = [make_venue(f"v{i}") for i in range(50)]
    db.upsert_venues(city_id=1, venues=venues, fetched_at=1000.0, total=50, with_coords=50)
    db.upsert_courses_for_date(1, "2026-04-10", [make_course(i) for i in range(50)], fetched_at=1000.0)

    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(100):
                assert db.get_venues_payload(1) is not None
                assert len(db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"]) == 50
                db.get_course_fetches(1, ["2026-04-10"])
                db.get_venues_fetched_at(1)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


# ── 18. Pydantic rehydration ────────────────────────────────────────────────


def test_storage_roundtrip_preserves_pydantic_types(db):
    db.upsert_cities([make_city(1)], fetched_at=1000.0)
    db.upsert_venues(city_id=1, venues=[make_venue("a")], fetched_at=1000.0, total=1, with_coords=1)
    db.upsert_venue_detail("a", make_detail())

    payload = db.get_venues_payload(1)
    assert payload is not None
    venue = payload.venues[0]
    assert isinstance(venue, Venue)
    assert isinstance(venue.address, VenueAddress)
    assert isinstance(venue.visit_limits, VisitLimits)
    assert venue.visit_limits.corporate["M"] == 4

    detail = db.get_venue_detail("a")
    assert isinstance(detail, VenueDetail)
    assert isinstance(detail.visit_limits, VisitLimits)


# ── Course tiers / fee + migration ─────────────────────────────────────────


def test_courses_roundtrip_keeps_tiers_and_fee(db):
    db.upsert_cities([make_city()], fetched_at=time.time())
    course = make_course(
        tiers_private=["Premium", "Max"],
        tiers_corporate=["L", "XL"],
        min_tier_private="Premium",
        min_tier_corporate="L",
        extra_fee_text="+ 10€",
        extra_fee_eur=10,
    )
    db.upsert_courses_for_date(1, "2026-04-10", [course], fetched_at=time.time())
    [got] = db.get_courses_for_dates(1, ["2026-04-10"])["2026-04-10"]
    assert got.tiers_private == ["Premium", "Max"]
    assert got.tiers_corporate == ["L", "XL"]
    assert got.min_tier_private == "Premium"
    assert got.min_tier_corporate == "L"
    assert got.extra_fee_text == "+ 10€"
    assert got.extra_fee_eur == 10


def test_migrate_drops_courses_table_without_fee_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cities (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country_code TEXT,
            centroid_lat REAL, centroid_lng REAL, venue_address_count INTEGER,
            lat_min REAL, lat_max REAL, lng_min REAL, lng_max REAL, fetched_at REAL NOT NULL);
        INSERT INTO cities (id, name, fetched_at) VALUES (1, 'Berlin', 0);
        CREATE TABLE courses (
            city_id INTEGER NOT NULL REFERENCES cities(id), course_id INTEGER NOT NULL, date TEXT NOT NULL,
            title TEXT, start_time TEXT, end_time TEXT, venue_id TEXT, venue_name TEXT, lat REAL, lng REAL,
            district TEXT, category TEXT, category_id INTEGER, teacher TEXT, free_spots INTEGER,
            max_spots INTEGER, is_online INTEGER, is_plus INTEGER,
            PRIMARY KEY (city_id, course_id, date)
        );
        INSERT INTO courses (city_id, course_id, date) VALUES (1, 5, '2026-04-10');
        CREATE TABLE course_fetches (city_id INTEGER NOT NULL, date TEXT NOT NULL,
            fetched_at REAL NOT NULL, course_count INTEGER NOT NULL, PRIMARY KEY (city_id, date));
        INSERT INTO course_fetches VALUES (1, '2026-04-10', 0, 1);
    """)
    conn.commit()
    conn.close()

    storage.close()
    storage.init(path)
    try:
        cols = {r["name"] for r in storage._fetchall("PRAGMA table_info(courses)")}
        assert "extra_fee_eur" in cols
        assert storage._fetchall("SELECT * FROM courses") == []
        assert storage._fetchall("SELECT * FROM course_fetches") == []
    finally:
        storage.close()


def test_course_detail_roundtrip(db):
    detail = CourseDetail(
        description="Hang on",
        rating=4.8,
        rating_count=120,
        free_cancellation_hours=12,
        booking_type="instant",
        fetched_at=123.0,
    )
    db.upsert_course_detail(7, detail)
    assert db.get_course_detail(7) == detail
    assert db.get_course_detail(8) is None


def test_settings_roundtrip(db):
    assert db.get_settings() == Settings()
    s = Settings(plan=Plan(type="corporate", tier="L"))
    db.put_settings(s)
    assert db.get_settings() == s


# ── Travel cache ───────────────────────────────────────────────────────────

ORIGIN = "52.5579,13.4857"


def test_travel_bike_then_transit_merge(db):
    db.set_bike_km(ORIGIN, {"v1": 7.93}, at=100.0)
    db.set_transit(ORIGIN, "v1", pt_min=38, lines=["M4", "M8"], walk_m=777, at=200.0)
    entry = db.get_travel(ORIGIN, ["v1", "v2"])["v1"]
    assert (entry.bike_km, entry.bike_at) == (7.93, 100.0)
    assert (entry.pt_min, entry.pt_lines, entry.walk_m, entry.pt_at) == (38, ["M4", "M8"], 777, 200.0)
    assert "v2" not in db.get_travel(ORIGIN, ["v1", "v2"])
    # Re-setting bike keeps the transit columns.
    db.set_bike_km(ORIGIN, {"v1": 8.0}, at=300.0)
    assert db.get_travel(ORIGIN, ["v1"])["v1"].pt_min == 38


def test_mark_transit_failed_then_success_clears_it(db):
    db.mark_transit_failed(ORIGIN, "v1", at=50.0)
    entry = db.get_travel(ORIGIN, ["v1"])["v1"]
    assert entry.pt_failed_at == 50.0
    assert entry.pt_min is None
    db.set_transit(ORIGIN, "v1", pt_min=20, lines=[], walk_m=0, at=60.0)
    assert db.get_travel(ORIGIN, ["v1"])["v1"].pt_failed_at is None


def test_travel_is_keyed_by_origin(db):
    db.set_bike_km(ORIGIN, {"v1": 7.93}, at=100.0)
    assert db.get_travel("52.0000,13.0000", ["v1"]) == {}


def test_exact_respects_max_age(db):
    result = {
        "pt_min": 31,
        "depart_at": "2026-10-22T18:22:00+02:00",
        "arrive_at": "2026-10-22T18:53:00+02:00",
        "lines": ["M4"],
        "walk_m": 300,
    }
    db.set_exact(ORIGIN, "v1", "2026-10-22T18:55:00+02:00", result, at=time.time() - 10)
    assert db.get_exact(ORIGIN, "v1", "2026-10-22T18:55:00+02:00", max_age=60) == result
    assert db.get_exact(ORIGIN, "v1", "2026-10-22T18:55:00+02:00", max_age=5) is None
    assert db.get_exact(ORIGIN, "v1", "2026-10-23T18:55:00+02:00", max_age=60) is None


def test_venue_points_skip_missing_coordinates(db):
    db.upsert_cities([make_city(1), make_city(2, "Potsdam")], fetched_at=time.time())
    db.upsert_venues(
        1,
        [make_venue("a", lat=52.5, lng=13.4), make_venue("b", lat=None, lng=None, has_coordinates=False)],
        time.time(),
        2,
        1,
    )
    db.upsert_venues(2, [make_venue("c", lat=52.4, lng=13.06), make_venue("a", lat=52.5, lng=13.4)], time.time(), 2, 2)
    assert db.get_venue_points(["a", "b", "zz"]) == {"a": (52.5, 13.4)}
    assert sorted(db.list_city_venue_points([1, 2])) == [("a", 52.5, 13.4), ("c", 52.4, 13.06)]
    assert db.list_city_venue_points([]) == []
