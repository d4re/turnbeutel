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
import itertools
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi.concurrency import run_in_threadpool

import storage
from models import GeocodeResult

logger = logging.getLogger(__name__)

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
BIKE_RETRY_AFTER = 600
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


# ── Background worker ──────────────────────────────────────────────────────

PRIORITY_REQUEST = 1  # venues the app or planner asked about
PRIORITY_WARM = 2  # background warm of the prefetch cities


class TransitUnavailable(Exception):
    """No exact journey could be found (VBB down, or nothing arrives in time)."""


# Within a priority, bike batches (a fraction of a second per 100 venues) run
# before any transit lookup (one paced VBB request each).
_KIND_RANK = {"bike": 0, "transit": 1}


@dataclass(order=True)
class _Job:
    priority: int
    rank: int
    seq: int
    kind: str = field(compare=False)  # "bike" | "transit"
    origin: tuple[float, float] = field(compare=False)
    dests: list[tuple[str, float, float]] = field(compare=False)  # bike: a batch; transit: one
    key: tuple = field(compare=False)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class TravelWorker:
    """Computes travel times one job at a time, most urgent first.

    Bike distances go to OSRM in batches; every transit lookup is one paced VBB
    request. Results land in the travel cache tables, which the endpoints read.
    Warm jobs queued for an earlier home are dropped once the home changes.
    Exact lookups (someone is waiting on them) bypass the queue but share the
    VBB pacer, so a slow background request never holds them up.
    """

    def __init__(self, client: httpx.AsyncClient, vbb_pacer: Pacer, now: Callable[[], float] = time.time):
        self._client = client
        self._pacer = vbb_pacer
        self._now = now
        self._queue: asyncio.PriorityQueue[_Job] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._queued: dict[tuple, _Job] = {}
        self._pending: Counter[str] = Counter()
        self._bike_failed_at: dict[str, float] = {}
        self._exact_tasks: dict[tuple, asyncio.Task] = {}
        self.origin: tuple[float, float] | None = None

    def set_origin(self, origin: tuple[float, float] | None) -> None:
        self.origin = origin

    def is_pending(self, venue_id: str) -> bool:
        return self._pending[venue_id] > 0

    async def request(self, venue_ids: list[str], origin: tuple[float, float] | None = None) -> None:
        """Queue whatever is missing or expired for these venues (looked up in the venues table)."""
        origin = origin or self.origin
        if origin is None:
            return
        points = await run_in_threadpool(storage.get_venue_points, venue_ids)
        await self._enqueue(PRIORITY_REQUEST, origin, [(vid, *points[vid]) for vid in venue_ids if vid in points])

    async def warm(self, points: list[tuple[str, float, float]]) -> None:
        """Queue the background warm for `points`, nearest to the current home first."""
        if self.origin is None:
            return
        origin = self.origin
        ordered = sorted(points, key=lambda p: _haversine_km(origin, (p[1], p[2])))
        await self._enqueue(PRIORITY_WARM, origin, ordered)

    async def exact(self, venue_id: str, dest: tuple[float, float], arrive_by: datetime) -> dict:
        """Journey arriving by `arrive_by`; raises TransitUnavailable."""
        if self.origin is None:
            raise TransitUnavailable("no home set")
        origin = self.origin
        key = (origin_key(*origin), venue_id, arrive_by.isoformat())
        task = self._exact_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._lookup_exact(origin, venue_id, dest, arrive_by))
            self._exact_tasks[key] = task

            def _forget(t: asyncio.Task) -> None:
                self._exact_tasks.pop(key, None)
                if not t.cancelled():
                    t.exception()  # mark retrieved even if every caller timed out

            task.add_done_callback(_forget)
        # Shielded: a caller timing out must not cancel the lookup others share.
        return await asyncio.shield(task)

    async def _lookup_exact(
        self, origin: tuple[float, float], venue_id: str, dest: tuple[float, float], arrive_by: datetime
    ) -> dict:
        try:
            journeys = await fetch_journeys(origin, dest, self._client, self._pacer, arrival=arrive_by)
            best = pick_exact(journeys, arrive_by)
        except Exception as e:
            # Only the type: httpx messages carry the URL, i.e. the home coordinates.
            logger.warning("Exact transit lookup failed: %s", type(e).__name__)
            raise TransitUnavailable("lookup failed") from e
        if best is None:
            raise TransitUnavailable("no journey arrives in time")
        result = {
            "pt_min": best.pt_min,
            "depart_at": best.depart_at.isoformat(),
            "arrive_at": best.arrive_at.isoformat(),
            "lines": best.lines,
            "walk_m": best.walk_m,
        }
        await run_in_threadpool(
            storage.set_exact, origin_key(*origin), venue_id, arrive_by.isoformat(), result, self._now()
        )
        return result

    async def _enqueue(
        self, priority: int, origin: tuple[float, float], points: list[tuple[str, float, float]]
    ) -> None:
        okey = origin_key(*origin)
        cached = await run_in_threadpool(storage.get_travel, okey, [p[0] for p in points])
        now = self._now()
        bike_ok = now - self._bike_failed_at.get(okey, 0) >= BIKE_RETRY_AFTER
        need_bike, need_transit = [], []
        for p in points:
            entry = cached.get(p[0])
            bike_stale = entry is None or entry.bike_at is None or now - entry.bike_at >= BIKE_TTL
            if bike_ok and bike_stale and ("bike", okey, p[0]) not in self._queued:
                need_bike.append(p)
            if not (in_vbb_area(*origin) and in_vbb_area(p[1], p[2])):
                continue
            fresh = entry is not None and entry.pt_at is not None and now - entry.pt_at < PT_TTL
            failed_recently = entry is not None and entry.pt_failed_at and now - entry.pt_failed_at < PT_RETRY_AFTER
            if not fresh and not failed_recently and ("transit", okey, p[0]) not in self._queued:
                need_transit.append(p)
        for i in range(0, len(need_bike), OSRM_BATCH):
            batch = need_bike[i : i + OSRM_BATCH]
            self._put(priority, "bike", origin, batch, ("bike-batch", okey, next(self._seq)), batch_keys=True)
        for p in need_transit:
            self._put(priority, "transit", origin, [p], ("transit", okey, p[0]))

    def _put(self, priority, kind, origin, dests, key, *, batch_keys=False) -> _Job:
        job = _Job(priority, _KIND_RANK[kind], next(self._seq), kind, origin, dests, key)
        keys = [("bike", origin_key(*origin), d[0]) for d in dests] if batch_keys else [key]
        for k in keys:
            self._queued[k] = job
        for d in dests:
            self._pending[d[0]] += 1
        self._queue.put_nowait(job)
        return job

    def _done(self, job: _Job) -> None:
        okey = origin_key(*job.origin)
        keys = [("bike", okey, d[0]) for d in job.dests] if job.kind == "bike" else [job.key]
        for k in keys:
            if self._queued.get(k) is job:
                del self._queued[k]
        for d in job.dests:
            self._pending[d[0]] -= 1
            if self._pending[d[0]] <= 0:
                del self._pending[d[0]]

    async def run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.priority == PRIORITY_WARM and job.origin != self.origin:
                    continue  # home changed since this warm was queued
                await self._process(job)
            except Exception as e:
                # Only the type: httpx messages carry the URL, i.e. the home coordinates.
                logger.warning("Travel %s job failed: %s", job.kind, type(e).__name__)
            finally:
                self._done(job)

    async def _process(self, job: _Job) -> None:
        okey = origin_key(*job.origin)
        if job.kind == "bike":
            try:
                km = await fetch_bike_km(job.origin, job.dests, self._client)
            except Exception:
                self._bike_failed_at[okey] = self._now()
                raise
            # Unroutable venues are stored as None so they aren't re-asked every time.
            values = {vid: km.get(vid) for vid, _, _ in job.dests}
            await run_in_threadpool(storage.set_bike_km, okey, values, self._now())
        else:
            vid, lat, lng = job.dests[0]
            try:
                departure = next_reference_departure(datetime.fromtimestamp(self._now(), BERLIN))
                journeys = await fetch_journeys(job.origin, (lat, lng), self._client, self._pacer, departure=departure)
                best = pick_typical(journeys)
            except Exception:
                await run_in_threadpool(storage.mark_transit_failed, okey, vid, self._now())
                raise
            if best is None:
                await run_in_threadpool(storage.mark_transit_failed, okey, vid, self._now())
            else:
                await run_in_threadpool(
                    storage.set_transit, okey, vid, best.pt_min, best.lines, best.walk_m, self._now()
                )
