"""Outcome-based tests for server pure functions."""

import asyncio
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import storage
from models import City, Course, Venue, VenueAddress, VenueDetail, VisitLimits
from server import (
    CORPORATE_TIER_ORDER,
    PRIVATE_TIER_ORDER,
    min_tier,
    parse_extra_fee,
    parse_venue_limits,
    transform_city,
    transform_course,
    transform_venue,
)

# ── parse_venue_limits ──


def _plan(name, venue=-1, address=-1):
    """One plan entry as returned by USC's /venues/{id}/limits."""
    return {
        "id": 1,
        "name": name,
        "planName": name,
        "priority": 1,
        "monthlyLimitVenue": venue,
        "monthlyUsageVenue": -1,
        "monthlyLimitVenueAddress": address,
        "monthlyUsageVenueAddress": -1,
        "categoryLimits": [],
    }


def test_parse_venue_limits_none_or_empty():
    assert parse_venue_limits(None) is None
    assert parse_venue_limits({}) is None
    assert parse_venue_limits({"b2c": [], "b2e": []}) is None


def test_parse_venue_limits_maps_b2c_to_private_and_b2e_to_corporate():
    # Fenriz-style mismatch: corporate M is included, private Classic is not.
    result = parse_venue_limits(
        {
            "b2c": [_plan("L", address=8), _plan("XL", address=8)],
            "b2e": [_plan("M", address=4), _plan("L", address=8), _plan("XL", address=8)],
        }
    )
    assert result.private == {"Essential": None, "Classic": None, "Premium": 8, "Max": 8}
    assert result.corporate == {"S": None, "M": 4, "L": 8, "XL": 8}


def test_parse_venue_limits_unlimited_is_daily_cap():
    # -1 on both levels = no monthly limit, only the 1-visit-per-day cap.
    result = parse_venue_limits({"b2c": [_plan("XL")], "b2e": [_plan("XL")]})
    assert result.private["Max"] == 30
    assert result.corporate["XL"] == 30


def test_parse_venue_limits_takes_stricter_of_venue_and_address_limit():
    # Holmes Place S: 3/month across the chain, 1/month at this address.
    result = parse_venue_limits({"b2c": [_plan("M", venue=4, address=8)], "b2e": [_plan("S", venue=3, address=1)]})
    assert result.private["Classic"] == 4
    assert result.corporate["S"] == 1


def test_parse_venue_limits_venue_wide_limit_only():
    result = parse_venue_limits({"b2c": [_plan("M", venue=8, address=-1)], "b2e": []})
    assert result.private["Classic"] == 8


def test_parse_venue_limits_plan_without_values_is_not_included():
    result = parse_venue_limits({"b2c": [_plan("S", venue=None, address=None), _plan("M", address=4)]})
    assert result.private["Essential"] is None
    assert result.private["Classic"] == 4


def test_parse_venue_limits_ignores_unknown_plans():
    result = parse_venue_limits({"b2c": [_plan("XXL", address=4), _plan("M", address=4)]})
    assert set(result.private) == set(PRIVATE_TIER_ORDER)
    assert set(result.corporate) == set(CORPORATE_TIER_ORDER)


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
        fetched_at=time.time(),
    )
    result = transform_venue(_make_raw(), detail=detail)
    assert result.visit_limits is not None
    assert result.visit_limits.corporate["M"] == 8


def test_transform_venue_without_detail():
    result = transform_venue(_make_raw(), detail=None)
    assert result.visit_limits is None


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
    assert c.tiers_corporate == []
    assert c.min_tier_corporate is None
    assert c.extra_fee_eur is None


# ── fetch_all_venue_pages ───────────────────────────────────────────────────


def _raw_venue_page(page: int, n: int) -> list[dict]:
    return [
        {
            "name": f"Venue {page}-{i}",
            "urlSlug": f"venue-{page}-{i}",
            "planTypes": ["S"],
            "planTypesB2B": ["S"],
            "categories": [],
            "ratings": {},
            "id": page * 1000 + i,
            "location": {"latitude": 52.5, "longitude": 13.4},
            "isOnline": 0,
            "isPlusCheckin": 0,
        }
        for i in range(n)
    ]


def _run_fetch_all_venue_pages(page_sizes: dict[int, int], expected_count=None, record=None):
    """Drive fetch_all_venue_pages against a mock USC with the given page sizes."""
    import asyncio

    import httpx

    state = {"in_flight": 0, "max_in_flight": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if record is not None:
            record.append(page)
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["in_flight"] -= 1
        return httpx.Response(200, json={"data": _raw_venue_page(page, page_sizes.get(page, 0))})

    async def run():
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def patched_client(**kwargs):
            kwargs["transport"] = transport
            return real_client(**kwargs)

        import unittest.mock

        with unittest.mock.patch.object(httpx, "AsyncClient", patched_client):
            return await server.fetch_all_venue_pages(1, expected_count=expected_count)

    venues = asyncio.run(run())
    return venues, state["max_in_flight"]


def test_fetch_all_venue_pages_known_count_fetches_exact_pages_in_one_round():
    """With venueAddressCount known (230 -> 3 pages), all pages are requested
    together and no speculative pages beyond the expected ones are fetched."""
    record: list[int] = []
    venues, max_in_flight = _run_fetch_all_venue_pages({1: 100, 2: 100, 3: 30}, expected_count=230, record=record)
    assert len(venues) == 230
    assert sorted(record) == [1, 2, 3]  # no wasted requests past page 3
    assert max_in_flight == 3  # page 1 fetched together with 2 and 3


def test_fetch_all_venue_pages_stale_count_still_fetches_everything():
    """If the city grew past the cached venueAddressCount, discovery continues."""
    venues, _ = _run_fetch_all_venue_pages({1: 100, 2: 20}, expected_count=100)
    assert len(venues) == 120


def test_fetch_all_venue_pages_no_count_falls_back_to_discovery():
    venues, _ = _run_fetch_all_venue_pages({1: 100, 2: 50}, expected_count=None)
    assert len(venues) == 150


# ── fetch_courses_for_date ──────────────────────────────────────────────────


def test_fetch_courses_for_date_fetches_pages_concurrently():
    """A day with many pages must be fetched in parallel batches, not one page
    at a time — serial pagination made big cities take ~14 round trips."""
    import asyncio

    import httpx

    in_flight = 0
    max_in_flight = 0
    # 6 full pages + 1 short page = 650 courses.
    page_sizes = {1: 100, 2: 100, 3: 100, 4: 100, 5: 100, 6: 100, 7: 50}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        page = int(request.url.params["page"])
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        n = page_sizes.get(page, 0)
        classes = [_make_raw_course(id=(page * 1000 + i)) for i in range(n)]
        return httpx.Response(200, json={"data": {"classes": classes}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await server.fetch_courses_for_date("2026-04-05", 1, client, asyncio.Semaphore(5))

    courses = asyncio.run(run())

    assert len(courses) == 650
    assert len({c.id for c in courses}) == 650
    # With serial pagination max_in_flight is 1; batched fetching overlaps pages.
    assert max_in_flight > 1


def test_fetch_courses_for_date_single_short_page():
    """A single short page must not trigger extra page requests beyond the first batch."""
    import asyncio

    import httpx

    requested_pages: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested_pages.append(page)
        classes = [_make_raw_course(id=i) for i in range(3)] if page == 1 else []
        return httpx.Response(200, json={"data": {"classes": classes}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await server.fetch_courses_for_date("2026-04-05", 1, client, asyncio.Semaphore(5))

    courses = asyncio.run(run())
    assert len(courses) == 3
    # Speculative fetching may cover the first batch, but must stop there.
    assert max(requested_pages) <= 5


# ── Server endpoint fixtures ───────────────────────────────────────────────


@pytest.fixture
def seeded_client(tmp_path: Path, monkeypatch):
    """FastAPI TestClient with a temp SQLite DB pre-seeded with two cities.

    - `fetch_all_cities`, `fetch_venue_detail` and `fetch_venue_limits` are monkeypatched to no-ops so
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
    # Never start the background prefetch loop from the fixture's lifespan.
    monkeypatch.setattr(server, "PREFETCH_CITIES", "")

    # Reset module-level caches so state doesn't leak between tests.
    server._venues_response_cache.clear()
    server._enrichment_cities.clear()
    server._venue_refresh_cities.clear()
    server._course_refresh_keys.clear()

    async def _fake_cities():
        return []

    async def _fake_venue_detail(venue_id):
        return {}

    async def _fake_venue_limits(venue_id):
        return {}

    async def _fake_enrich(city_id):
        # No-op: the fire-and-forget enrichment task would otherwise outlive the
        # SQLite connection and touch it after fixture teardown (segfault).
        return None

    monkeypatch.setattr(server, "fetch_all_cities", _fake_cities)
    monkeypatch.setattr(server, "fetch_venue_detail", _fake_venue_detail)
    monkeypatch.setattr(server, "fetch_venue_limits", _fake_venue_limits)
    monkeypatch.setattr(server, "enrich_venue_details", _fake_enrich)

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


def test_get_venue_detail_combines_detail_and_limits(seeded_client, monkeypatch):
    async def fake_detail(venue_id):
        return {"importantInfo": "Bring a towel", "website": "https://example.com"}

    async def fake_limits(venue_id):
        return {"b2c": [_plan("M", address=4)], "b2e": [_plan("M", address=8)]}

    monkeypatch.setattr(server, "fetch_venue_detail", fake_detail)
    monkeypatch.setattr(server, "fetch_venue_limits", fake_limits)

    resp = seeded_client.get("/api/venues/42")
    assert resp.status_code == 200
    data = resp.json()
    assert data["importantInfo"] == "Bring a towel"
    assert data["visit_limits"]["private"]["Classic"] == 4
    assert data["visit_limits"]["corporate"]["M"] == 8
    assert data["visit_limits"]["corporate"]["S"] is None
    assert storage.get_venue_detail("42").visit_limits.corporate["M"] == 8


def test_get_venues_requires_city_ids(seeded_client):
    resp = seeded_client.get("/api/venues")
    assert resp.status_code == 400
    assert "city_ids" in resp.json()["detail"].lower()


def test_get_venues_multi_city_grouped(seeded_client, monkeypatch):
    calls: list[int] = []

    async def fake_fetch(usc_city_id: int, expected_count=None):
        calls.append(usc_city_id)
        if usc_city_id == 1:
            return [
                {
                    "name": "Berlin Studio",
                    "urlSlug": "berlin-studio",
                    "planTypes": ["S"],
                    "planTypesB2B": ["S"],
                    "categories": [],
                    "ratings": {},
                    "id": "b1",
                    "location": {"latitude": 52.52, "longitude": 13.4},
                    "isOnline": 0,
                    "isPlusCheckin": 0,
                }
            ]
        return [
            {
                "name": "Hamburg Studio",
                "urlSlug": "hamburg-studio",
                "planTypes": ["S"],
                "planTypesB2B": ["S"],
                "categories": [],
                "ratings": {},
                "id": "h1",
                "location": {"latitude": 53.55, "longitude": 9.99},
                "isOnline": 0,
                "isPlusCheckin": 0,
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

    async def fake_fetch(usc_city_id: int, expected_count=None):
        calls.append(usc_city_id)
        return [
            {
                "name": "Studio A",
                "urlSlug": "studio-a",
                "planTypes": ["S"],
                "planTypesB2B": ["S"],
                "categories": [],
                "ratings": {},
                "id": "a1",
                "location": {"latitude": 52.52, "longitude": 13.4},
                "isOnline": 0,
                "isPlusCheckin": 0,
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


def test_large_responses_are_gzip_compressed(seeded_client, monkeypatch):
    """Venue payloads run to megabytes; the server must honor Accept-Encoding: gzip."""

    async def fake_fetch(usc_city_id: int, expected_count=None):
        return [
            {
                "name": f"Studio {i}",
                "urlSlug": f"studio-{i}",
                "planTypes": ["S"],
                "planTypesB2B": ["S"],
                "categories": [],
                "ratings": {},
                "id": f"v{i}",
                "location": {"latitude": 52.52, "longitude": 13.4},
                "isOnline": 0,
                "isPlusCheckin": 0,
            }
            for i in range(20)
        ]

    monkeypatch.setattr(server, "fetch_all_venue_pages", fake_fetch)

    resp = seeded_client.get("/api/venues?city_ids=1", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    # The body still decodes transparently.
    assert len(resp.json()["cities"][0]["venues"]) == 20


def _make_course(course_id: int, date: str, title: str) -> Course:
    return Course(
        id=course_id,
        date=date,
        title=title,
        start_time="09:00",
        end_time="10:00",
        venue_id="v1",
        venue_name="Venue",
        lat=52.52,
        lng=13.4,
        district="",
        category="Yoga",
        category_id=1,
        teacher="",
        free_spots=5,
        max_spots=10,
        is_online=False,
        is_plus=False,
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_get_venues_stale_cache_served_immediately_then_refreshed(seeded_client, monkeypatch):
    """An expired-but-present venue snapshot must be returned right away, with
    the live refetch happening in the background — not blocking the request."""
    stale_at = time.time() - server.VENUES_TTL - 3600
    storage.upsert_venues(1, [_make_venue("v1", "Old Studio", 52.5, 13.4)], stale_at, 1, 1)

    calls: list[int] = []

    async def fake_fetch(usc_city_id: int, expected_count=None):
        calls.append(usc_city_id)
        return [
            {
                "name": "New Studio",
                "urlSlug": "new-studio",
                "planTypes": ["S"],
                "planTypesB2B": ["S"],
                "categories": [],
                "ratings": {},
                "id": "v2",
                "location": {"latitude": 52.52, "longitude": 13.4},
                "isOnline": 0,
                "isPlusCheckin": 0,
            }
        ]

    monkeypatch.setattr(server, "fetch_all_venue_pages", fake_fetch)

    resp = seeded_client.get("/api/venues?city_ids=1")
    assert resp.status_code == 200
    venues = resp.json()["cities"][0]["venues"]
    # The stale snapshot is served, not the live refetch.
    assert [v["name"] for v in venues] == ["Old Studio"]

    # The background refresh runs to completion and lands in storage.
    assert _wait_until(lambda: calls == [1])
    assert _wait_until(lambda: (storage.get_venues_fetched_at(1) or 0) > stale_at)
    refreshed = storage.get_venues_payload(1)
    assert [v.name for v in refreshed.venues] == ["New Studio"]


def test_get_courses_stale_cache_served_immediately_then_refreshed(seeded_client, monkeypatch):
    """Expired-but-present course days are served from cache with a background refresh."""
    stale_at = time.time() - server.COURSES_TTL - 3600
    storage.upsert_courses_for_date(1, "2026-04-11", [_make_course(1, "2026-04-11", "Old Class")], stale_at)

    calls: list[tuple[str, int]] = []

    async def fake_fetch(date_str, usc_city_id, client, semaphore):
        calls.append((date_str, usc_city_id))
        return [_make_course(2, date_str, "New Class")]

    monkeypatch.setattr(server, "fetch_courses_for_date", fake_fetch)

    resp = seeded_client.get("/api/courses?start_date=2026-04-11&days=1&city_ids=1")
    assert resp.status_code == 200
    titles = [c["title"] for c in resp.json()["cities"][0]["courses"]]
    assert titles == ["Old Class"]

    assert _wait_until(lambda: calls == [("2026-04-11", 1)])
    assert _wait_until(lambda: (storage.get_course_fetches(1, ["2026-04-11"]).get("2026-04-11") or 0) > stale_at)
    refreshed = storage.get_courses_for_dates(1, ["2026-04-11"])["2026-04-11"]
    assert [c.title for c in refreshed] == ["New Class"]


# ── Background prefetch ──


def test_resolve_prefetch_cities_accepts_ids_and_names():
    cities = [City(id=1, name="Berlin"), City(id=2, name="Hamburg")]
    resolved = server.resolve_prefetch_cities(" berlin, 2,1, Atlantis ,", cities)
    assert [c.id for c in resolved] == [1, 2]
    assert server.resolve_prefetch_cities("", cities) == []


def test_resolve_prefetch_cities_includes_nearby_cities():
    cities = [
        City(id=1, name="Berlin", centroid_lat=52.5167, centroid_lng=13.3833),
        City(id=10, name="Potsdam", centroid_lat=52.3989, centroid_lng=13.0657),  # ~25 km
        City(id=6448, name="Glienicke/Nordbahn", centroid_lat=52.6333, centroid_lng=13.3167),  # ~14 km
        City(id=2, name="Hamburg", centroid_lat=53.55, centroid_lng=9.99),
        City(id=99, name="Nowhere"),  # no centroid
    ]
    assert [c.id for c in server.resolve_prefetch_cities("Berlin", cities, radius_km=30)] == [1, 6448, 10]
    assert [c.id for c in server.resolve_prefetch_cities("Berlin", cities, radius_km=20)] == [1, 6448]
    assert [c.id for c in server.resolve_prefetch_cities("Berlin", cities)] == [1]


def _fake_raw_venue(venue_id: str, name: str) -> dict:
    return {
        "name": name,
        "urlSlug": name.lower(),
        "planTypes": ["S"],
        "planTypesB2B": ["S"],
        "categories": [],
        "ratings": {},
        "id": venue_id,
        "location": {"latitude": 52.52, "longitude": 13.4},
        "isOnline": 0,
        "isPlusCheckin": 0,
    }


def test_prefetch_cycle_warms_empty_cache(seeded_client, monkeypatch):
    """A cold city gets its venues and the whole date strip fetched."""
    venue_calls: list[int] = []
    course_calls: list[tuple[str, int]] = []

    async def fake_venues(usc_city_id: int, expected_count=None):
        venue_calls.append(usc_city_id)
        return [_fake_raw_venue("v1", "Studio")]

    async def fake_courses(date_str, usc_city_id, client, semaphore):
        course_calls.append((date_str, usc_city_id))
        return [_make_course(1, date_str, "Class")]

    monkeypatch.setattr(server, "fetch_all_venue_pages", fake_venues)
    monkeypatch.setattr(server, "fetch_courses_for_date", fake_courses)

    berlin = next(c for c in server._cities_index if c.id == 1)
    asyncio.run(server.prefetch_cycle([berlin], interval=3600))

    assert venue_calls == [1]
    today = date.today()
    expected_dates = [(today + timedelta(days=i)).isoformat() for i in range(server.MAX_COURSE_DAYS)]
    assert sorted(course_calls) == [(d, 1) for d in expected_dates]
    assert set(storage.get_course_fetches(1, expected_dates)) == set(expected_dates)

    # Requests are now served entirely from the warm cache.
    monkeypatch.setattr(server, "fetch_all_venue_pages", None)
    monkeypatch.setattr(server, "fetch_courses_for_date", None)
    resp = seeded_client.get(f"/api/courses?start_date={expected_dates[0]}&days=14&city_ids=1")
    assert resp.status_code == 200
    assert resp.json()["cities"][0]["total"] == server.MAX_COURSE_DAYS
    resp = seeded_client.get("/api/venues?city_ids=1")
    assert [v["name"] for v in resp.json()["cities"][0]["venues"]] == ["Studio"]


def test_prefetch_cycle_only_refreshes_what_expires_before_next_cycle(seeded_client, monkeypatch):
    now = time.time()
    interval = 3600
    today = date.today()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(server.MAX_COURSE_DAYS)]
    # Venues fresh well beyond the next cycle; courses fresh except two days:
    # one that will expire before the next cycle and one never fetched.
    storage.upsert_venues(1, [_make_venue("v1", "Studio", 52.5, 13.4)], now, 1, 1)
    expiring = dates[3]
    for d in dates[:-1]:
        fetched_at = now - server.COURSES_TTL + interval / 2 if d == expiring else now
        storage.upsert_courses_for_date(1, d, [], fetched_at)

    venue_calls: list[int] = []
    course_calls: list[str] = []

    async def fake_venues(usc_city_id: int, expected_count=None):
        venue_calls.append(usc_city_id)
        return []

    async def fake_courses(date_str, usc_city_id, client, semaphore):
        course_calls.append(date_str)
        return []

    monkeypatch.setattr(server, "fetch_all_venue_pages", fake_venues)
    monkeypatch.setattr(server, "fetch_courses_for_date", fake_courses)

    berlin = next(c for c in server._cities_index if c.id == 1)
    asyncio.run(server.prefetch_cycle([berlin], interval=interval))

    assert venue_calls == []
    assert sorted(course_calls) == sorted([expiring, dates[-1]])


def test_course_refresh_due_staggers_days_and_respects_ttl():
    """Simulate hourly cycles over 3 weeks for a fixed strip that was cold-
    fetched all at once: refreshes spread out to at most one day per cycle,
    each day is refreshed about every 2 days, and nothing outlives the TTL."""
    interval = 3600.0
    days = [date(2026, 10, 18) + timedelta(days=i) for i in range(server.MAX_COURSE_DAYS)]
    t0 = 1_800_000_000.0
    fetched = dict.fromkeys(days, t0)
    refresh_times: dict[date, list[float]] = {d: [] for d in days}
    per_cycle: list[int] = []

    now = t0
    while now < t0 + 21 * 24 * 3600:
        now += interval
        due = [d for d in days if server.course_refresh_due(d, fetched[d], now, interval)]
        per_cycle.append(len(due))
        for d in due:
            assert now - fetched[d] <= server.COURSES_TTL
            fetched[d] = now
            refresh_times[d].append(now)

    # After the first period (one catch-up refresh per day), never more than one day per cycle.
    assert max(per_cycle[48:]) == 1
    for times in refresh_times.values():
        gaps = [b - a for a, b in zip(times[1:], times[2:], strict=False)]
        assert gaps and all(server.COURSES_TTL - 2 * interval <= g <= server.COURSES_TTL for g in gaps)


def test_course_refresh_due_when_never_fetched():
    assert server.course_refresh_due(date(2026, 10, 18), None, time.time(), 3600)


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

    resp = seeded_client.get("/api/courses?start_date=2026-04-11&days=1&city_ids=1&city_ids=2")
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


def test_get_courses_days_clamped_to_date_strip(seeded_client, monkeypatch):
    """`days` must clamp to 14, matching the frontend's 14-chip date strip."""
    calls: list[str] = []

    async def fake_fetch(date_str, usc_city_id, client, semaphore):
        calls.append(date_str)
        return []

    monkeypatch.setattr(server, "fetch_courses_for_date", fake_fetch)

    resp = seeded_client.get("/api/courses?start_date=2026-04-11&days=30&city_ids=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(calls) == 14
    assert data["date_from"] == "2026-04-11"
    assert data["date_to"] == "2026-04-24"


# ── transform_city ──


def test_transform_city_uses_default_name():
    """USC's /cities rows carry the name under `defaultName`, not `name`."""
    raw = {
        "id": 93,
        "defaultName": "Aachen",
        "lat": 50.780658,
        "lon": 6.083815,
        "countryCode": "DE",
        "venueAddressCount": 122,
    }
    c = transform_city(raw)
    assert c.id == 93
    assert c.name == "Aachen"
    assert c.country_code == "DE"
    assert c.centroid_lat == 50.780658
    assert c.centroid_lng == 6.083815
    assert c.venue_address_count == 122
