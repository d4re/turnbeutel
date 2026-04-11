"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import threading
import time

import pytest

import storage
from models import City, Course, Venue, VenueAddress, VenueDetail, VisitLimits


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
        bookingLimitsText="some text",
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
    assert stored.bookingLimitsText == "some text"


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
        assert payload.venues[0].bookingLimitsText == "some text"
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
