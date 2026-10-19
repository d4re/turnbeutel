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

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from models import GeocodeResult

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

OSRM_BATCH = 100
VBB_SPACING = 0.7  # s between VBB requests: stays well under its 100 req/min
VBB_TIMEOUT = 15
RETRY_DELAY = 2.0

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


class Pacer:
    """Serialises callers and spaces their starts at least `min_interval` apart."""

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                delay = self._last + self.min_interval - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            self._last = self._clock()


async def fetch_bike_km(
    origin: tuple[float, float],
    dests: list[tuple[str, float, float]],
    client: httpx.AsyncClient,
) -> dict[str, float]:
    """Bike route distances (km) from `origin` to each `(venue_id, lat, lng)`.

    One OSRM table request per OSRM_BATCH destinations, sent one after another.
    Unroutable destinations (null distance) are left out.
    """
    out: dict[str, float] = {}
    for i in range(0, len(dests), OSRM_BATCH):
        batch = dests[i : i + OSRM_BATCH]
        coords = ";".join([f"{origin[1]},{origin[0]}"] + [f"{lng},{lat}" for _, lat, lng in batch])
        resp = await client.get(
            OSRM_TABLE_URL + coords,
            params={"sources": "0", "annotations": "distance"},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        row = resp.json()["distances"][0]
        for (venue_id, _, _), metres in zip(batch, row[1:], strict=True):
            if metres is not None:
                out[venue_id] = round(metres / 1000, 2)
    return out


async def fetch_journeys(
    origin: tuple[float, float],
    dest: tuple[float, float],
    client: httpx.AsyncClient,
    pacer: Pacer,
    *,
    departure: datetime | None = None,
    arrival: datetime | None = None,
) -> list[dict]:
    """VBB journeys from `origin` to `dest`, leaving at `departure` or arriving by `arrival`.

    Retries once on a network error or 5xx; raises if that fails too.
    """
    if (departure is None) == (arrival is None):
        raise ValueError("pass exactly one of departure / arrival")
    params = {
        "from.latitude": origin[0],
        "from.longitude": origin[1],
        "from.address": "Home",
        "to.latitude": dest[0],
        "to.longitude": dest[1],
        "to.address": "Venue",
        "results": 3,
        "stopovers": "false",
    }
    if departure is not None:
        params["departure"] = departure.isoformat()
    else:
        params["arrival"] = arrival.isoformat()

    for attempt in range(2):
        await pacer.wait()
        try:
            resp = await client.get(
                VBB_JOURNEYS_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=VBB_TIMEOUT
            )
            resp.raise_for_status()
            return resp.json().get("journeys") or []
        except httpx.HTTPError as e:
            client_error = isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
            if client_error or attempt == 1:
                raise
        await asyncio.sleep(RETRY_DELAY)
    raise AssertionError("unreachable")


# Nominatim's usage policy: at most 1 request/s, with an identifying User-Agent.
nominatim_pacer = Pacer(1.0)


async def geocode(q: str, viewbox: str | None, client: httpx.AsyncClient, pacer: Pacer) -> list[GeocodeResult]:
    """Address search via Nominatim; `viewbox` ("lng1,lat1,lng2,lat2") biases, not bounds, results."""
    params = {"q": q, "format": "jsonv2", "addressdetails": 1, "limit": 5, "bounded": 0}
    if viewbox:
        params["viewbox"] = viewbox
    await pacer.wait()
    resp = await client.get(NOMINATIM_SEARCH_URL, params=params, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return [GeocodeResult(label=r["display_name"], lat=float(r["lat"]), lng=float(r["lon"])) for r in resp.json()]
