"""Outcome-based tests for server pure functions."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import storage
from models import City, Venue, VenueAddress, VenueDetail, VisitLimits
from server import (
    CORPORATE_TIER_ORDER,
    PRIVATE_TIER_ORDER,
    min_tier,
    parse_visit_limits,
    transform_course,
    transform_venue,
)

# ── parse_visit_limits ──


def test_parse_visit_limits_none_input():
    assert parse_visit_limits(None) is None


def test_parse_visit_limits_empty_string():
    assert parse_visit_limits("") is None


def test_parse_visit_limits_no_match():
    assert parse_visit_limits("No limits apply to this venue.") is None


def test_parse_visit_limits_single_tier():
    result = parse_visit_limits("S-Mitglieder können 4x pro Monat trainieren.")
    assert result is not None
    assert result.corporate["S"] == 4
    assert result.private["Essential"] == 4
    assert result.corporate["M"] is None
    assert result.corporate["L"] is None
    assert result.corporate["XL"] is None


def test_parse_visit_limits_multiple_tiers():
    text = (
        "S-Mitglieder können 4x pro Monat trainieren. "
        "M-Mitglieder können 8x pro Monat trainieren. "
        "L-Mitglieder können 12x pro Monat trainieren. "
        "XL-Mitglieder können 16x pro Monat trainieren."
    )
    result = parse_visit_limits(text)
    assert result is not None
    assert result.corporate == {"S": 4, "M": 8, "L": 12, "XL": 16}
    assert result.private == {"Essential": 4, "Classic": 8, "Premium": 12, "Max": 16}


def test_parse_visit_limits_mal_variant():
    result = parse_visit_limits("M-Mitglieder können 8 Mal pro Monat trainieren.")
    assert result is not None
    assert result.corporate["M"] == 8
    assert result.private["Classic"] == 8


def test_parse_visit_limits_grouped_tier_prefix():
    """USC frequently groups tiers like 'L- & XL-Mitglieder können 8 Mal pro Monat'."""
    text = (
        "M-Mitglieder können 4 Mal pro Monat CORE Training besuchen.\r\n\r\n"
        "L- & XL-Mitglieder können 8 Mal pro Monat CORE Training besuchen."
    )
    result = parse_visit_limits(text)
    assert result is not None
    assert result.corporate["M"] == 4
    assert result.corporate["L"] == 8
    assert result.corporate["XL"] == 8
    assert result.corporate["S"] is None
    assert result.private["Premium"] == 8
    assert result.private["Max"] == 8


def test_parse_visit_limits_grouped_with_und():
    result = parse_visit_limits("M, L und XL-Mitglieder können 8x pro Monat trainieren.")
    assert result is not None
    assert result.corporate["M"] == 8
    assert result.corporate["L"] == 8
    assert result.corporate["XL"] == 8
    assert result.corporate["S"] is None


def test_parse_visit_limits_im_monat_variant():
    result = parse_visit_limits("S-Mitglieder können 4x im Monat trainieren.")
    assert result is not None
    assert result.corporate["S"] == 4


def test_parse_visit_limits_per_day_converted_to_month():
    """'1 Mal pro Tag' should be converted to 30 / month (30-day month assumption)."""
    text = "M, L & XL-Mitglieder können diesen Standort 1 Mal pro Tag besuchen"
    result = parse_visit_limits(text)
    assert result is not None
    assert result.corporate["M"] == 30
    assert result.corporate["L"] == 30
    assert result.corporate["XL"] == 30
    assert result.corporate["S"] is None


def test_parse_visit_limits_per_day_multi_visit():
    result = parse_visit_limits("XL-Mitglieder können 2x pro Tag trainieren.")
    assert result is not None
    assert result.corporate["XL"] == 60


def test_parse_visit_limits_duerfen_variant():
    """USC also uses 'dürfen' instead of 'können' (e.g. Berliner Bäderbetriebe)."""
    result = parse_visit_limits("M, L & XL-Mitglieder dürfen 1 x am Tag schwimmen gehen")
    assert result is not None
    assert result.corporate["M"] == 30
    assert result.corporate["L"] == 30
    assert result.corporate["XL"] == 30
    assert result.corporate["S"] is None


def test_parse_visit_limits_filler_between_count_and_preposition():
    """Real text has noun fillers between the count and 'pro/im Monat'."""
    result = parse_visit_limits(
        "S-Mitglieder können im Rahmen ihres Kontingents von insgesamt 4 x Sport im Monat trainieren"
    )
    assert result is not None
    assert result.corporate["S"] == 4


def test_parse_visit_limits_hyphen_mal_form():
    """'8-Mal pro Monat' (hyphenated) should parse the same as '8 Mal pro Monat'."""
    result = parse_visit_limits("M-Mitglieder können diesen Standort insgesamt 8-Mal pro Monat besuchen")
    assert result is not None
    assert result.corporate["M"] == 8


def test_parse_visit_limits_baederbetriebe_full_text():
    """Both sentences in a real Berliner Bäderbetriebe text should parse together."""
    text = (
        "M, L & XL-Mitglieder\xa0dürfen\xa01 x am Tag\xa0bei den Berliner Bäderbetrieben schwimmen gehen\r\n\r\n"
        "S-Mitglieder\xa0können im Rahmen ihres\xa0Kontingents von insgesamt\xa04 x Sport im Monat\xa0die Berliner Bäderbetriebe\xa0besuchen"
    )
    result = parse_visit_limits(text)
    assert result is not None
    assert result.corporate["S"] == 4
    assert result.corporate["M"] == 30
    assert result.corporate["L"] == 30
    assert result.corporate["XL"] == 30


def test_parse_visit_limits_unmentioned_tiers_are_none():
    result = parse_visit_limits("L-Mitglieder können 5x pro Monat trainieren.")
    assert result is not None
    for tier in CORPORATE_TIER_ORDER:
        assert tier in result.corporate
    for tier in PRIVATE_TIER_ORDER:
        assert tier in result.private
    assert result.corporate["L"] == 5
    assert result.corporate["S"] is None


# ── min_tier ──


def test_min_tier_returns_lowest():
    assert min_tier(["Premium", "Essential"], PRIVATE_TIER_ORDER) == "Essential"


def test_min_tier_single():
    assert min_tier(["Max"], PRIVATE_TIER_ORDER) == "Max"


def test_min_tier_empty():
    assert min_tier([], PRIVATE_TIER_ORDER) is None


def test_min_tier_corporate():
    assert min_tier(["XL", "M"], CORPORATE_TIER_ORDER) == "M"


# ── transform_venue ──


def _make_raw(**overrides):
    """Build a minimal raw venue dict."""
    raw = {
        "name": "Test Gym",
        "urlSlug": "test-gym",
        "location": {
            "latitude": 52.52,
            "longitude": 13.405,
            "address": "Street 1",
            "postalCode": "10115",
            "city": {"name": "Berlin"},
            "country": {"code": "DE"},
            "district": {"name": "Mitte"},
        },
        "planTypes": ["M"],
        "planTypesB2B": ["M"],
        "categories": [],
        "ratings": {},
        "isPlusCheckin": 0,
        "isOnline": 0,
        "id": 123,
    }
    raw.update(overrides)
    return raw


def test_transform_venue_basic():
    result = transform_venue(_make_raw())
    assert result.name == "Test Gym"
    assert result.slug == "test-gym"
    assert result.url == "https://urbansportsclub.com/en/venues/test-gym"
    assert result.tiers_corporate == ["M"]
    assert result.tiers_private == ["Classic"]
    assert result.min_tier_corporate == "M"
    assert result.min_tier_private == "Classic"
    assert result.has_coordinates is True
    assert result.district == "Mitte"
    assert result.visit_limits is None


def test_transform_venue_missing_coordinates():
    result = transform_venue(
        _make_raw(
            location={
                "latitude": None,
                "longitude": None,
                "address": "",
                "postalCode": "",
                "city": {"name": ""},
                "country": {"code": ""},
                "district": {"name": ""},
            }
        )
    )
    assert result.has_coordinates is False


def test_transform_venue_with_detail():
    detail = VenueDetail(
        visit_limits=VisitLimits(
            private={"Essential": None, "Classic": 8, "Premium": None, "Max": None},
            corporate={"S": None, "M": 8, "L": None, "XL": None},
        ),
        bookingLimitsText="some text",
        fetched_at=time.time(),
    )
    result = transform_venue(_make_raw(), detail=detail)
    assert result.visit_limits is not None
    assert result.visit_limits.corporate["M"] == 8
    assert result.bookingLimitsText == "some text"


def test_transform_venue_without_detail():
    result = transform_venue(_make_raw(), detail=None)
    assert result.visit_limits is None
    assert result.bookingLimitsText is None


def test_transform_venue_activities():
    raw = _make_raw(
        categories=[
            {"translations": {"en_GB": "Yoga"}, "name": "yoga_fallback"},
            {"translations": {"en_GB": "Swimming"}, "name": "swimming_fallback"},
        ]
    )
    result = transform_venue(raw)
    assert result.activities == ["Yoga", "Swimming"]


def test_transform_venue_empty_slug():
    result = transform_venue(_make_raw(urlSlug=""))
    assert result.url == ""


# ── transform_course ──


def _make_raw_course(**overrides):
    raw = {
        "id": 99051489,
        "date": "2026-04-05",
        "title": "Kundalini with Paula",
        "startTime": "12:15:00",
        "endTime": "13:30:00",
        "venue": {
            "id": 4926,
            "name": "Yellow Yoga - Studio Sonne",
            "location": {
                "latitude": 52.48444,
                "longitude": 13.43498,
                "district": {"name": "Neukölln"},
            },
        },
        "category": {"id": 6, "name": "Yoga"},
        "teacherName": "Paula",
        "freeSpots": 32,
        "maximumNumber": 40,
        "isOnline": 0,
        "isPlusCheckin": 0,
    }
    raw.update(overrides)
    return raw


def test_transform_course_basic():
    result = transform_course(_make_raw_course())
    assert result.id == 99051489
    assert result.date == "2026-04-05"
    assert result.title == "Kundalini with Paula"
    assert result.start_time == "12:15"
    assert result.end_time == "13:30"
    assert result.venue_id == "4926"
    assert result.venue_name == "Yellow Yoga - Studio Sonne"
    assert result.lat == 52.48444
    assert result.lng == 13.43498
    assert result.district == "Neukölln"
    assert result.category == "Yoga"
    assert result.category_id == 6
    assert result.teacher == "Paula"
    assert result.free_spots == 32
    assert result.max_spots == 40
    assert result.is_online is False
    assert result.is_plus is False


def test_transform_course_time_truncation():
    result = transform_course(_make_raw_course(startTime="09:00:00", endTime="10:00:00"))
    assert result.start_time == "09:00"
    assert result.end_time == "10:00"


def test_transform_course_missing_optional_fields():
    raw = _make_raw_course(teacherName=None, freeSpots=None)
    result = transform_course(raw)
    assert result.teacher == ""
    assert result.free_spots is None


def test_transform_course_missing_venue_location():
    raw = _make_raw_course(venue={"id": 1, "name": "Nowhere", "location": {}})
    result = transform_course(raw)
    assert result.lat is None
    assert result.lng is None
    assert result.district == ""


def test_transform_course_plus_and_online_flags():
    raw = _make_raw_course(isOnline=1, isPlusCheckin=1)
    result = transform_course(raw)
    assert result.is_online is True
    assert result.is_plus is True


# ── Server endpoint fixtures ───────────────────────────────────────────────


@pytest.fixture
def seeded_client(tmp_path: Path, monkeypatch):
    """FastAPI TestClient with a temp SQLite DB pre-seeded with two cities.

    - `fetch_all_cities` and `fetch_venue_detail` are monkeypatched to no-ops so
      neither lifespan nor background enrichment hits USC.
    - `fetch_all_venue_pages` and `fetch_courses_for_date` are left as stubs
      that individual tests override (see `monkeypatch.setattr` calls inside
      each test).
    - The temp DB is seeded with cities 1 ("Berlin") and 2 ("Hamburg") so
      `_cities_index` has content after lifespan runs `storage.list_cities()`.
    - Module-level caches (`_venues_response_cache`, `_enrichment_cities`) are
      cleared on setup so tests don't leak state into each other.
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


def test_seeded_client_boots_and_has_cities_index(seeded_client):
    # Lifespan ran; _cities_index should be populated from the seeded DB.
    assert len(server._cities_index) == 2
    assert {c.id for c in server._cities_index} == {1, 2}


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
