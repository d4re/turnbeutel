"""Travel times from the user's home to venues: bike distance (OSRM) and public
transport (VBB, Berlin/Brandenburg only).

Bike: only OSRM's *distance* is used — its durations assume ~13 km/h, which
proved too slow; the frontend turns km into minutes at the user's chosen speed.
Transit: the "typical" time is the earliest-arriving journey when leaving at a
fixed reference time (next Tuesday 17:30), measured from first departure to
last arrival (walking and transfers included, waiting at home not). The public
VBB instance allows 100 req/min and is sometimes down, so requests are strictly
sequential and spaced (see `Pacer`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

USER_AGENT = "turnbeutel (+https://github.com/d4re/turnbeutel)"
OSRM_TABLE_URL = "https://routing.openstreetmap.de/routed-bike/table/v1/driving/"
VBB_JOURNEYS_URL = "https://v6.vbb.transport.rest/journeys"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
BERLIN = ZoneInfo("Europe/Berlin")
# (lat_min, lat_max, lng_min, lng_max) — generous box around Berlin/Brandenburg.
VBB_BBOX = (51.3, 53.6, 11.2, 14.8)

BIKE_TTL = 30 * 24 * 3600
PT_TTL = 7 * 24 * 3600
PT_RETRY_AFTER = 3600
EXACT_TTL = 24 * 3600
GEOCODE_TTL = 30 * 24 * 3600

REFERENCE_WEEKDAY = 1  # Tuesday
REFERENCE_TIME = (17, 30)


def origin_key(lat: float, lng: float) -> str:
    """Cache key for an origin, rounded to ~10 m."""
    return f"{lat:.4f},{lng:.4f}"


def in_vbb_area(lat: float, lng: float) -> bool:
    lat_min, lat_max, lng_min, lng_max = VBB_BBOX
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def next_reference_departure(now: datetime) -> datetime:
    """The first Tuesday 17:30 (Europe/Berlin) more than an hour after `now`."""
    earliest = now.astimezone(BERLIN) + timedelta(hours=1)
    day = earliest.date()
    while True:
        if day.weekday() == REFERENCE_WEEKDAY:
            candidate = datetime(day.year, day.month, day.day, *REFERENCE_TIME, tzinfo=BERLIN)
            if candidate > earliest:
                return candidate
        day += timedelta(days=1)


@dataclass
class Transit:
    pt_min: int
    lines: list[str]
    walk_m: int
    depart_at: datetime
    arrive_at: datetime


def _leg_time(leg: dict, key: str) -> datetime:
    planned = "planned" + key[0].upper() + key[1:]
    return datetime.fromisoformat(leg.get(key) or leg[planned])


def parse_journey(j: dict) -> Transit:
    legs = j["legs"]
    depart_at = _leg_time(legs[0], "departure")
    arrive_at = _leg_time(legs[-1], "arrival")
    return Transit(
        pt_min=round((arrive_at - depart_at).total_seconds() / 60),
        lines=[leg["line"]["name"] for leg in legs if leg.get("line")],
        walk_m=sum(leg.get("distance") or 0 for leg in legs if leg.get("walking")),
        depart_at=depart_at,
        arrive_at=arrive_at,
    )


def pick_typical(journeys: list[dict]) -> Transit | None:
    """The earliest-arriving journey — what a journey planner shows first."""
    parsed = [parse_journey(j) for j in journeys]
    return min(parsed, key=lambda t: t.arrive_at, default=None)


def pick_exact(journeys: list[dict], arrive_by: datetime) -> Transit | None:
    """The latest-departing journey that still arrives by `arrive_by`."""
    in_time = [t for t in (parse_journey(j) for j in journeys) if t.arrive_at <= arrive_by]
    return max(in_time, key=lambda t: t.depart_at, default=None)
