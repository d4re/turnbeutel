"""Outcome-based tests for server pure functions."""

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
    assert result["corporate"]["S"] == 4
    assert result["private"]["Essential"] == 4
    # Other tiers should be None
    assert result["corporate"]["M"] is None
    assert result["corporate"]["L"] is None
    assert result["corporate"]["XL"] is None


def test_parse_visit_limits_multiple_tiers():
    text = (
        "S-Mitglieder können 4x pro Monat trainieren. "
        "M-Mitglieder können 8x pro Monat trainieren. "
        "L-Mitglieder können 12x pro Monat trainieren. "
        "XL-Mitglieder können 16x pro Monat trainieren."
    )
    result = parse_visit_limits(text)
    assert result is not None
    assert result["corporate"] == {"S": 4, "M": 8, "L": 12, "XL": 16}
    assert result["private"] == {"Essential": 4, "Classic": 8, "Premium": 12, "Max": 16}


def test_parse_visit_limits_mal_variant():
    result = parse_visit_limits("M-Mitglieder können 8 Mal pro Monat trainieren.")
    assert result is not None
    assert result["corporate"]["M"] == 8
    assert result["private"]["Classic"] == 8


def test_parse_visit_limits_unmentioned_tiers_are_none():
    result = parse_visit_limits("L-Mitglieder können 5x pro Monat trainieren.")
    assert result is not None
    for tier in CORPORATE_TIER_ORDER:
        assert tier in result["corporate"]
    for tier in PRIVATE_TIER_ORDER:
        assert tier in result["private"]
    assert result["corporate"]["L"] == 5
    assert result["corporate"]["S"] is None


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
    assert result["name"] == "Test Gym"
    assert result["slug"] == "test-gym"
    assert result["url"] == "https://urbansportsclub.com/en/venues/test-gym"
    assert result["tiers_corporate"] == ["M"]
    assert result["tiers_private"] == ["Classic"]
    assert result["min_tier_corporate"] == "M"
    assert result["min_tier_private"] == "Classic"
    assert result["has_coordinates"] is True
    assert result["district"] == "Mitte"
    assert result["visit_limits"] is None


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
    assert result["has_coordinates"] is False


def test_transform_venue_with_detail():
    detail = {"visit_limits": {"corporate": {"M": 8}}, "bookingLimitsText": "some text"}
    result = transform_venue(_make_raw(), detail=detail)
    assert result["visit_limits"] == {"corporate": {"M": 8}}
    assert result["bookingLimitsText"] == "some text"


def test_transform_venue_without_detail():
    result = transform_venue(_make_raw(), detail=None)
    assert result["visit_limits"] is None
    assert result["bookingLimitsText"] is None


def test_transform_venue_activities():
    raw = _make_raw(
        categories=[
            {"translations": {"en_GB": "Yoga"}, "name": "yoga_fallback"},
            {"translations": {"en_GB": "Swimming"}, "name": "swimming_fallback"},
        ]
    )
    result = transform_venue(raw)
    assert result["activities"] == ["Yoga", "Swimming"]


def test_transform_venue_empty_slug():
    result = transform_venue(_make_raw(urlSlug=""))
    assert result["url"] == ""


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
    assert result["id"] == 99051489
    assert result["date"] == "2026-04-05"
    assert result["title"] == "Kundalini with Paula"
    assert result["start_time"] == "12:15"
    assert result["end_time"] == "13:30"
    assert result["venue_id"] == "4926"
    assert result["venue_name"] == "Yellow Yoga - Studio Sonne"
    assert result["lat"] == 52.48444
    assert result["lng"] == 13.43498
    assert result["district"] == "Neukölln"
    assert result["category"] == "Yoga"
    assert result["category_id"] == 6
    assert result["teacher"] == "Paula"
    assert result["free_spots"] == 32
    assert result["max_spots"] == 40
    assert result["is_online"] is False
    assert result["is_plus"] is False


def test_transform_course_time_truncation():
    result = transform_course(_make_raw_course(startTime="09:00:00", endTime="10:00:00"))
    assert result["start_time"] == "09:00"
    assert result["end_time"] == "10:00"


def test_transform_course_missing_optional_fields():
    raw = _make_raw_course(teacherName=None, freeSpots=None)
    result = transform_course(raw)
    assert result["teacher"] == ""
    assert result["free_spots"] is None


def test_transform_course_missing_venue_location():
    raw = _make_raw_course(venue={"id": 1, "name": "Nowhere", "location": {}})
    result = transform_course(raw)
    assert result["lat"] is None
    assert result["lng"] is None
    assert result["district"] == ""


def test_transform_course_plus_and_online_flags():
    raw = _make_raw_course(isOnline=1, isPlusCheckin=1)
    result = transform_course(raw)
    assert result["is_online"] is True
    assert result["is_plus"] is True
