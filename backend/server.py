"""
FastAPI backend proxy for USC API.

Fetches venue, course, and category data from api.urbansportsclub.com,
transforms it to the frontend's expected format, and caches results in a
single SQLite database (see storage.py).
"""

import asyncio
import contextlib
import logging
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

import storage
import travel
from models import (
    CORPORATE_TIER_ORDER,
    PRIVATE_TIER_ORDER,
    CitiesResponse,
    City,
    CityCoursesEntry,
    CityVenuesEntry,
    Course,
    CourseDetail,
    CourseFetchError,
    ExactTransit,
    GeocodeResult,
    MultiCityCoursesResponse,
    MultiCityVenuesPayload,
    Settings,
    TierConfig,
    TravelRequest,
    TravelResponse,
    TravelResult,
    Venue,
    VenueAddress,
    VenueDetail,
    VenuesPayload,
    VisitLimits,
)

logger = logging.getLogger(__name__)


# ── Config ──

USC_API = "https://api.urbansportsclub.com/api/v6"
USC_HEADERS = {
    "User-Agent": "USCAPP/4.0.8 (android; 28; Scale/2.75)",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US;q=1.0",
}
DEFAULT_CITY_ID = 1
PAGE_SIZE = 100
MAX_VENUE_PAGES = 100
VENUE_PAGE_BATCH = 5
MAX_CONCURRENT_VENUE_FETCHES = 3
# Must cover the frontend's date strip (today .. today+13 = 14 days).
MAX_COURSE_DAYS = 14
COURSE_PAGE_BATCH = 5
MAX_COURSE_PAGES = 50

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
DB_PATH = CACHE_DIR / "usc.db"

VENUES_TTL = 24 * 3600
DETAILS_TTL = 7 * 24 * 3600
CATEGORIES_TTL = 7 * 24 * 3600
COURSES_TTL = 48 * 3600
COURSE_DETAILS_TTL = 7 * 24 * 3600
COURSES_STALE = 3 * 24 * 3600
CITIES_TTL = 7 * 24 * 3600

# Background prefetch ("warming"): cities listed here (comma-separated USC city
# ids or names, e.g. "Berlin" or "1,Hamburg") have their venues and the full
# course date strip refreshed periodically, so visitors never wait on a live
# USC pull or see expired data. Empty disables prefetching.
PREFETCH_CITIES = os.environ.get("PREFETCH_CITIES", "")
PREFETCH_INTERVAL = float(os.environ.get("PREFETCH_INTERVAL_MINUTES", "60")) * 60
# Also prefetch every city whose centroid lies within this many km of a listed
# one — the map's default Berlin view pulls in ~20 small Brandenburg towns.
PREFETCH_RADIUS_KM = float(os.environ.get("PREFETCH_RADIUS_KM", "0"))

# Travel times (travel.py): the worker starts with the app; tests switch it off.
TRAVEL_ENABLED = True
travel_worker: travel.TravelWorker | None = None
EXACT_TIMEOUT = 10.0  # s an expanded course waits for its exact journey
EXACT_BUFFER = timedelta(minutes=5)  # arrive this long before the class starts
_prefetch_cities: list[City] = []

# ── Tier mappings ──

CORP_TO_PRIVATE = {"S": "Essential", "M": "Classic", "L": "Premium", "XL": "Max"}

TIER_CONFIG = TierConfig(
    private={
        "order": PRIVATE_TIER_ORDER,
        "colors": {"Essential": "#27ae60", "Classic": "#2980b9", "Premium": "#e67e22", "Max": "#c0392b"},
    },
    corporate={
        "order": CORPORATE_TIER_ORDER,
        "display": {"S": "S", "M": "M Pro", "L": "L Pro", "XL": "XL Pro"},
        "colors": {"S": "#27ae60", "M": "#2980b9", "L": "#e67e22", "XL": "#c0392b"},
    },
)

# ── In-memory state ──

_cities_index: list[City] = []
_venues_response_cache: dict[int, tuple[float, VenuesPayload]] = {}
_enrichment_cities: set[int] = set()
_venue_fetch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_VENUE_FETCHES)
# Guards against duplicate stale-while-revalidate refreshes.
_venue_refresh_cities: set[int] = set()
_course_refresh_keys: set[tuple[int, str]] = set()
_course_detail_refresh: set[int] = set()
# One request per click, but still bounded like every other course fetch.
_course_detail_semaphore = asyncio.Semaphore(5)


def _invalidate_venues_cache(city_id: int) -> None:
    _venues_response_cache.pop(city_id, None)


# ── Lifespan ──


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite storage, sync /cities index, and purge stale courses."""
    global _cities_index, travel_worker, _prefetch_cities

    await run_in_threadpool(storage.init, DB_PATH)

    cities_fetched_at = await run_in_threadpool(storage.get_cities_fetched_at)
    if cities_fetched_at is None or (time.time() - cities_fetched_at) > CITIES_TTL:
        try:
            raw_cities = await fetch_all_cities()
            cities = [transform_city(c) for c in raw_cities if c.get("id")]
            await run_in_threadpool(storage.upsert_cities, cities, time.time())
        except Exception:
            logger.exception("Failed to sync /cities on startup; continuing with existing cache")

    _cities_index = await run_in_threadpool(storage.list_cities)
    await run_in_threadpool(storage.purge_stale_courses, float(COURSES_STALE))

    prefetch_cities = resolve_prefetch_cities(PREFETCH_CITIES, _cities_index, PREFETCH_RADIUS_KM)
    _prefetch_cities = prefetch_cities

    travel_task = travel_client = None
    if TRAVEL_ENABLED:
        travel_client = httpx.AsyncClient(headers={"User-Agent": travel.USER_AGENT}, timeout=15)
        travel_worker = travel.TravelWorker(travel_client, travel.Pacer(travel.VBB_SPACING))
        settings = await run_in_threadpool(storage.get_settings)
        if settings.home:
            travel_worker.set_origin((settings.home.lat, settings.home.lng))
        travel_task = asyncio.create_task(travel_worker.run())

    prefetch_task = asyncio.create_task(prefetch_loop(prefetch_cities)) if prefetch_cities else None

    yield

    for task in (prefetch_task, travel_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    if travel_client is not None:
        await travel_client.aclose()
        travel_worker = None
    storage.close()


app = FastAPI(title="Turnbeutel API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
# Venue/course payloads are large (Berlin: ~1.4 MB JSON, ~0.16 MB gzipped).
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Visit limits parsing ──

# USC's /venues/{id}/limits returns one entry per plan the venue is available on,
# `b2c` for private and `b2e` for corporate memberships, keyed by the corporate
# letter (S/M/L/XL). Each has a per-location limit (`monthlyLimitVenueAddress`)
# and a venue-wide one (`monthlyLimitVenue`, e.g. across a chain); -1 means
# "no limit at that level". A plan with no monthly limit at all is still capped
# at one visit per day, which we express as a 30-day month equivalent.
DAYS_PER_MONTH = 30


def _plan_monthly_limit(plan: dict) -> int | None:
    values = [v for v in (plan.get("monthlyLimitVenueAddress"), plan.get("monthlyLimitVenue")) if v is not None]
    if not values:
        return None
    positive = [v for v in values if v > 0]
    if positive:
        return min(positive)
    if all(v == -1 for v in values):
        return DAYS_PER_MONTH
    return None


def parse_venue_limits(data: dict | None) -> VisitLimits | None:
    """Turn a /venues/{id}/limits payload into a VisitLimits model.

    Tiers the venue isn't available on are absent from the payload and map to
    None. Private and corporate are read independently (they don't always match).
    """
    if not data:
        return None

    corporate: dict[str, int | None] = dict.fromkeys(CORPORATE_TIER_ORDER)
    private: dict[str, int | None] = dict.fromkeys(PRIVATE_TIER_ORDER)
    found = False
    for plan in data.get("b2c") or []:
        tier = CORP_TO_PRIVATE.get(plan.get("name"))
        if tier:
            private[tier] = _plan_monthly_limit(plan)
            found = True
    for plan in data.get("b2e") or []:
        tier = plan.get("name")
        if tier in corporate:
            corporate[tier] = _plan_monthly_limit(plan)
            found = True

    if not found:
        return None
    return VisitLimits(private=private, corporate=corporate)


# ── Venue transformation ──


def min_tier(tiers: list[str], order: list[str]) -> str | None:
    for t in order:
        if t in tiers:
            return t
    return None


def transform_venue(raw: dict, detail: VenueDetail | None = None) -> Venue:
    """Transform a USC API venue object into a Venue model."""
    loc = raw.get("location", {})
    plan_types = raw.get("planTypes", [])
    plan_types_b2b = raw.get("planTypesB2B", [])

    tiers_private = [CORP_TO_PRIVATE[t] for t in plan_types if t in CORP_TO_PRIVATE]
    tiers_corporate = [t for t in plan_types_b2b if t in CORPORATE_TIER_ORDER]

    lat = loc.get("latitude")
    lng = loc.get("longitude")
    slug = raw.get("urlSlug", "")

    activities: list[str] = []
    for cat in raw.get("categories", []) or []:
        name = (cat.get("translations") or {}).get("en_GB") or cat.get("name", "")
        if name and name not in activities:
            activities.append(name)

    ratings = raw.get("ratings", {}) or {}
    district_obj = loc.get("district", {}) or {}

    return Venue(
        name=(raw.get("name") or "").strip(),
        slug=slug,
        url=f"https://urbansportsclub.com/en/venues/{slug}" if slug else "",
        tiers_private=tiers_private,
        tiers_corporate=tiers_corporate,
        min_tier_private=min_tier(tiers_private, PRIVATE_TIER_ORDER),
        min_tier_corporate=min_tier(tiers_corporate, CORPORATE_TIER_ORDER),
        activities=activities,
        district=district_obj.get("name", ""),
        street=loc.get("address", "") or "",
        is_plus=raw.get("isPlusCheckin", 0) == 1,
        address_id=str(raw.get("id", "")),
        lat=lat,
        lng=lng,
        address=VenueAddress(
            street=loc.get("address", "") or "",
            postal_code=loc.get("postalCode", "") or "",
            city=f"{(loc.get('city') or {}).get('name', '')}, {(loc.get('country') or {}).get('code', '')}",
        ),
        rating=ratings.get("averageScore"),
        review_count=ratings.get("totalRatings"),
        visit_limits=detail.visit_limits if detail else None,
        is_online=raw.get("isOnline", 0) == 1,
        has_coordinates=bool(lat and lng),
    )


# ── City transformation ──


def transform_city(raw: dict) -> City:
    """Transform a USC /cities row into a City model."""
    return City(
        id=int(raw.get("id")),
        # USC's /cities rows carry the name under `defaultName`; fall back to
        # `name` for the venue-embedded city shape ({"id", "name"}).
        name=raw.get("defaultName") or raw.get("name") or "",
        country_code=(raw.get("country") or {}).get("code")
        if isinstance(raw.get("country"), dict)
        else raw.get("countryCode"),
        centroid_lat=raw.get("lat") or raw.get("latitude"),
        centroid_lng=raw.get("lon") or raw.get("lng") or raw.get("longitude"),
        venue_address_count=raw.get("venueAddressCount"),
    )


# ── API fetching ──


async def fetch_all_cities() -> list[dict]:
    """Fetch the USC /cities index (small, ~218 rows)."""
    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
        resp = await client.get(f"{USC_API}/cities")
        resp.raise_for_status()
        data = resp.json()
    # USC wraps lists in {"data": [...]} but older endpoints return raw lists.
    if isinstance(data, dict):
        return data.get("data") or []
    return data or []


async def fetch_all_venue_pages(usc_city_id: int, expected_count: int | None = None) -> list[dict]:
    """Fetch all venue pages from the USC API in bounded batches.

    When `expected_count` (the city's venueAddressCount from /cities) is known,
    exactly the expected pages are enqueued upfront — page 1 no longer costs a
    lone discovery round trip. Discovery batches of VENUE_PAGE_BATCH still
    follow if the city grew past the cached count (or when the count is
    unknown), capped at MAX_VENUE_PAGES as a runaway guard. A failed page
    raises so a venue list with a silent gap is never cached.
    """
    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:

        async def fetch_page(page: int) -> list[dict]:
            resp = await client.get(
                f"{USC_API}/venues",
                params={"cityId": usc_city_id, "page": page, "pageSize": PAGE_SIZE},
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

        all_venues: list[dict] = []

        if expected_count and expected_count > 0:
            expected_pages = min(-(-expected_count // PAGE_SIZE), MAX_VENUE_PAGES)
            page = 1
            while page <= expected_pages:
                batch_end = min(page + VENUE_PAGE_BATCH, expected_pages + 1)
                batch = await asyncio.gather(*[fetch_page(p) for p in range(page, batch_end)])
                for data in batch:
                    all_venues.extend(data)
                    if len(data) < PAGE_SIZE:
                        return all_venues
                page = batch_end
        else:
            all_venues = await fetch_page(1)
            if len(all_venues) < PAGE_SIZE:
                return all_venues
            page = 2

        # Discovery: the expected count was stale (or unknown) — keep fetching
        # batches until a short page appears.
        while page <= MAX_VENUE_PAGES:
            batch = await asyncio.gather(*[fetch_page(p) for p in range(page, page + VENUE_PAGE_BATCH)])
            for data in batch:
                all_venues.extend(data)
                if len(data) < PAGE_SIZE:
                    return all_venues
            page += VENUE_PAGE_BATCH

        return all_venues


async def fetch_venue_detail(venue_id: int, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch a single venue detail from the USC API.

    Pass `client` to reuse connections across many calls (background enrichment);
    without it a one-shot client is created.
    """
    if client is None:
        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as one_shot:
            return await fetch_venue_detail(venue_id, one_shot)
    resp = await client.get(f"{USC_API}/venues/{venue_id}")
    resp.raise_for_status()
    return resp.json().get("data", {})


async def fetch_venue_limits(venue_id: int, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch a venue's per-plan visit limits from the USC API (see parse_venue_limits)."""
    if client is None:
        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as one_shot:
            return await fetch_venue_limits(venue_id, one_shot)
    resp = await client.get(f"{USC_API}/venues/{venue_id}/limits")
    resp.raise_for_status()
    return resp.json().get("data", {})


def build_venue_detail(raw: dict, limits: dict) -> VenueDetail:
    """Combine /venues/{id} and /venues/{id}/limits payloads into a VenueDetail."""
    return VenueDetail(
        visit_limits=parse_venue_limits(limits),
        importantInfo=raw.get("importantInfo"),
        phone=raw.get("phone"),
        website=raw.get("website"),
        description=raw.get("description"),
        fetched_at=time.time(),
    )


# ── Course transformation and fetching ──

# On-site surcharges are often only in the title: "USC Checkin + 15 €",
# "+12 Euros", "EXTRA 5 EUR PAID AT THE DOOR", "+ 10€ Zuzahlung". Requiring a
# leading "+" or "extra" keeps plain prices ("Drop-in 15 €") from matching.
_EXTRA_FEE_RE = re.compile(r"(?:\+|\bextra\b)\s*(\d{1,3})\s*(?:€|eur(?:os?)?\b)", re.IGNORECASE)


def parse_extra_fee(title: str, extra_text: str | None) -> int | None:
    """Euro amount payable on site, from USC's extra-price text or else the title."""
    for text in (extra_text, title):
        m = _EXTRA_FEE_RE.search(text or "")
        if m:
            return int(m.group(1))
    return None


def transform_course(raw: dict) -> Course:
    """Transform a USC API course object into a Course model."""
    venue = raw.get("venue") or {}
    loc = venue.get("location") or {}
    district = (loc.get("district") or {}).get("name", "")
    category = raw.get("category") or {}
    start_time = raw.get("startTime") or ""
    end_time = raw.get("endTime") or ""
    tiers_private = [CORP_TO_PRIVATE[t] for t in raw.get("planTypes") or [] if t in CORP_TO_PRIVATE]
    tiers_corporate = [t for t in raw.get("planTypesB2B") or [] if t in CORPORATE_TIER_ORDER]
    title = raw.get("title", "")
    extra_fee_text = raw.get("extraPriceDescriptionText") or None
    return Course(
        id=raw.get("id"),
        date=raw.get("date", ""),
        title=title,
        start_time=start_time[:5],
        end_time=end_time[:5],
        venue_id=str(venue.get("id", "")),
        venue_name=venue.get("name", ""),
        lat=loc.get("latitude"),
        lng=loc.get("longitude"),
        district=district,
        category=category.get("name", ""),
        category_id=category.get("id"),
        teacher=raw.get("teacherName", "") or "",
        free_spots=raw.get("freeSpots"),
        max_spots=raw.get("maximumNumber"),
        is_online=raw.get("isOnline", 0) == 1,
        is_plus=raw.get("isPlusCheckin", 0) == 1,
        tiers_private=tiers_private,
        tiers_corporate=tiers_corporate,
        min_tier_private=min_tier(tiers_private, PRIVATE_TIER_ORDER),
        min_tier_corporate=min_tier(tiers_corporate, CORPORATE_TIER_ORDER),
        extra_fee_text=extra_fee_text,
        extra_fee_eur=parse_extra_fee(title, extra_fee_text),
    )


async def fetch_courses_for_date(
    date_str: str,
    usc_city_id: int,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> list[Course]:
    """Fetch all courses for a single date from USC (no caching — the caller handles storage).

    Pages are fetched speculatively COURSE_PAGE_BATCH at a time until a short
    page signals the end (capped at MAX_COURSE_PAGES). The shared semaphore
    still bounds total concurrency toward USC, so overlapping batches never
    exceed the global request limit.
    """

    async def fetch_page(page: int) -> list[dict]:
        async with semaphore:
            resp = await client.get(
                f"{USC_API}/courses",
                params={
                    "cityId": usc_city_id,
                    "startDate": date_str,
                    "forDurationOfDays": 1,
                    "pageSize": PAGE_SIZE,
                    "page": page,
                },
            )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("classes") or []

    all_raw: list[dict] = []
    page = 1
    while page <= MAX_COURSE_PAGES:
        batch = await asyncio.gather(*[fetch_page(p) for p in range(page, page + COURSE_PAGE_BATCH)])
        done = False
        for classes in batch:
            all_raw.extend(classes)
            if len(classes) < PAGE_SIZE:
                done = True
                break
        if done:
            break
        page += COURSE_PAGE_BATCH

    # USC occasionally returns the same course_id twice in a single day's payload
    # (observed: byte-identical duplicates). Dedupe at the source so both the
    # response and the storage layer see unique ids per (date, id).
    seen: set = set()
    courses: list[Course] = []
    for raw in all_raw:
        c = transform_course(raw)
        if c.id in seen:
            continue
        seen.add(c.id)
        courses.append(c)
    courses.sort(key=lambda c: (c.date, c.start_time))
    return courses


# ── Background enrichment ──


async def enrich_venue_details(city_id: int) -> None:
    """Background task: fetch details for any venue in `city_id` missing or with stale enrichment."""
    if city_id in _enrichment_cities:
        return
    _enrichment_cities.add(city_id)

    try:
        venue_ids = await run_in_threadpool(storage.list_venue_ids_needing_details, city_id, float(DETAILS_TTL))
        if not venue_ids:
            return

        semaphore = asyncio.Semaphore(5)
        processed = 0

        async def fetch_one(vid: str, client: httpx.AsyncClient) -> None:
            nonlocal processed
            async with semaphore:
                try:
                    # Sequential on purpose: keeps at most 5 requests in flight.
                    raw = await fetch_venue_detail(int(vid), client)
                    limits = await fetch_venue_limits(int(vid), client)
                    detail = build_venue_detail(raw, limits)
                    await run_in_threadpool(storage.upsert_venue_detail, vid, detail)
                    processed += 1
                    if processed % 50 == 0:
                        _invalidate_venues_cache(city_id)
                except Exception:
                    pass  # skip failures, retry next cycle

        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
            await asyncio.gather(*[fetch_one(vid, client) for vid in venue_ids])
        _invalidate_venues_cache(city_id)

    finally:
        _enrichment_cities.discard(city_id)


# ── Endpoints ──


def _fresh_cached_payload(city_id: int) -> VenuesPayload | None:
    """Return the in-memory venues payload for a city if it's within TTL."""
    cached = _venues_response_cache.get(city_id)
    if cached and (time.time() - cached[0]) < VENUES_TTL:
        return cached[1]
    return None


async def _fetch_and_cache_venues(city: City, force: bool = False, enrich: bool = True) -> VenuesPayload:
    """Live-fetch a city's venues from USC, persist them, and prime the caches.

    `force` skips the in-memory freshness re-check (prefetch refreshes a city
    shortly *before* it expires, while the memory cache still counts as fresh).
    `enrich=False` leaves detail enrichment to the caller (prefetch awaits it,
    so enrichment runs one city at a time).
    """
    # Bound concurrent live USC pulls globally. Cache reads elsewhere are
    # unthrottled; only live fetches acquire the semaphore.
    async with _venue_fetch_semaphore:
        # Re-check: another request may have fetched this same city while we
        # were queued on the semaphore.
        payload = None if force else _fresh_cached_payload(city.id)
        if payload is not None:
            return payload

        raw_venues = await fetch_all_venue_pages(usc_city_id=city.id, expected_count=city.venue_address_count)
        venues = [transform_venue(v) for v in raw_venues]
        venues = [v for v in venues if v.name and (v.tiers_private or v.tiers_corporate)]
        venues.sort(key=lambda v: v.name)

        total = len(venues)
        with_coords = sum(1 for v in venues if v.has_coordinates)
        now = time.time()
        await run_in_threadpool(storage.upsert_venues, city.id, venues, now, total, with_coords)

        payload = await run_in_threadpool(storage.get_venues_payload, city.id)
        assert payload is not None
        payload.tier_config = TIER_CONFIG
        _venues_response_cache[city.id] = (now, payload)
        if enrich:
            asyncio.create_task(enrich_venue_details(city.id))
        return payload


async def _refresh_venues_in_background(city: City, force: bool = False, enrich: bool = True) -> None:
    """Stale-while-revalidate worker: refetch a city whose snapshot expired."""
    if city.id in _venue_refresh_cities:
        return
    _venue_refresh_cities.add(city.id)
    try:
        await _fetch_and_cache_venues(city, force=force, enrich=enrich)
    except Exception:
        logger.warning("Background venue refresh failed for city=%s", city.id, exc_info=True)
    finally:
        _venue_refresh_cities.discard(city.id)


async def _load_venues_for_city(city_id: int) -> CityVenuesEntry | None:
    """Resolve one city's venues, honoring memory and DB caches.

    An expired-but-present DB snapshot is served immediately and refreshed in
    the background (stale-while-revalidate); only a city with no snapshot at
    all blocks on the live USC fetch.

    Returns None if the city is not in `_cities_index` (unknown id).
    """
    city = next((c for c in _cities_index if c.id == city_id), None)
    if city is None:
        return None

    payload = _fresh_cached_payload(city_id)

    if payload is None:
        fetched_at = await run_in_threadpool(storage.get_venues_fetched_at, city_id)
        if fetched_at is not None:
            payload = await run_in_threadpool(storage.get_venues_payload, city_id)
            if payload is not None:
                payload.tier_config = TIER_CONFIG
                if (time.time() - fetched_at) < VENUES_TTL:
                    _venues_response_cache[city_id] = (time.time(), payload)
                else:
                    # Stale: serve it now, refresh out-of-band. Deliberately not
                    # put in the memory cache — the refresh will prime it.
                    asyncio.create_task(_refresh_venues_in_background(city))

    if payload is None:
        payload = await _fetch_and_cache_venues(city)

    return CityVenuesEntry(
        city_id=city_id,
        city_name=city.name,
        fetched_at=payload.fetched_at,
        total=payload.total_venues,
        with_coordinates=payload.venues_with_coords,
        venues=payload.venues,
    )


@app.get("/api/venues", response_model=MultiCityVenuesPayload)
async def get_venues(city_ids: list[int] | None = Query(None)):
    if not city_ids:
        raise HTTPException(status_code=400, detail="city_ids query parameter is required")

    # Dedupe while preserving order.
    ordered = list(dict.fromkeys(city_ids))

    # Resolve cities concurrently: cached cities return instantly, while live
    # USC pulls are bounded by `_venue_fetch_semaphore`. gather preserves the
    # input order, so `entries` stays in `ordered` order.
    results = await asyncio.gather(*[_load_venues_for_city(cid) for cid in ordered])
    entries = [e for e in results if e is not None]

    return MultiCityVenuesPayload(cities=entries, tier_config=TIER_CONFIG)


@app.get("/api/venues/{venue_id}", response_model=VenueDetail)
async def get_venue_detail(venue_id: int):
    vid = str(venue_id)
    cached = await run_in_threadpool(storage.get_venue_detail, vid)
    if cached is not None:
        return cached

    try:
        raw = await fetch_venue_detail(venue_id)
        limits = await fetch_venue_limits(venue_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Venue {venue_id} not found") from e
        raise
    detail = build_venue_detail(raw, limits)
    await run_in_threadpool(storage.upsert_venue_detail, vid, detail)
    _venues_response_cache.clear()
    return detail


@app.get("/api/categories")
async def get_categories():
    cached = await run_in_threadpool(storage.get_categories, float(CATEGORIES_TTL))
    if cached is not None:
        return cached

    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
        resp = await client.get(f"{USC_API}/categories")
        resp.raise_for_status()
        data = resp.json()

    await run_in_threadpool(storage.set_categories, data, time.time())
    return data


async def _refresh_courses_in_background(pairs: list[tuple[int, str]]) -> None:
    """Stale-while-revalidate worker: refetch expired (city, date) course days."""
    work = [p for p in pairs if p not in _course_refresh_keys]
    if not work:
        return
    _course_refresh_keys.update(work)
    try:
        semaphore = asyncio.Semaphore(5)

        # Persist each day as soon as it lands: a big city's full date strip
        # takes minutes (Berlin: ~30 s per day), and finished days shouldn't
        # wait on the slowest one.
        async def refresh_one(cid: int, d: str, client: httpx.AsyncClient) -> None:
            try:
                courses = await fetch_courses_for_date(d, cid, client, semaphore)
            except Exception as e:
                logger.warning("Background course refresh failed for city=%s %s: %r", cid, d, e)
                return
            await run_in_threadpool(storage.upsert_courses_for_date, cid, d, courses, time.time())

        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
            await asyncio.gather(*[refresh_one(cid, d, client) for (cid, d) in work])
    finally:
        _course_refresh_keys.difference_update(work)


# ── Background prefetch ──


def _distance_km(a: City, b: City) -> float:
    """Great-circle distance between two city centroids."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a.centroid_lat, a.centroid_lng, b.centroid_lat, b.centroid_lng))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def resolve_prefetch_cities(spec: str, cities: list[City], radius_km: float = 0) -> list[City]:
    """Resolve a PREFETCH_CITIES spec (comma-separated ids or names) to cities.

    With `radius_km`, cities whose centroid lies within that distance of a
    listed city are appended (nearest first), after the listed ones.
    """
    by_id = {c.id: c for c in cities}
    by_name = {c.name.casefold(): c for c in cities}
    resolved: list[City] = []
    for token in (t.strip() for t in spec.split(",")):
        if not token:
            continue
        city = by_id.get(int(token)) if token.isdigit() else by_name.get(token.casefold())
        if city is None:
            logger.warning("PREFETCH_CITIES: unknown city %r, skipping", token)
        elif city not in resolved:
            resolved.append(city)

    if radius_km > 0:
        centers = [c for c in resolved if c.centroid_lat is not None and c.centroid_lng is not None]
        nearby: list[tuple[float, City]] = []
        for city in cities:
            if city in resolved or city.centroid_lat is None or city.centroid_lng is None:
                continue
            dist = min((_distance_km(center, city) for center in centers), default=math.inf)
            if dist <= radius_km:
                nearby.append((dist, city))
        resolved.extend(city for _, city in sorted(nearby, key=lambda x: x[0]))
    return resolved


def course_refresh_due(day: date, fetched_at: float | None, now: float, interval: float) -> bool:
    """Whether a prefetched course day should be re-fetched in this cycle.

    Every date gets a fixed slot in a refresh period of COURSES_TTL - interval
    (so data never outlives its TTL between cycles), offset by its ordinal:
    the 14 days of the strip fall into 14 evenly spaced slots (~3.4 h apart
    with the defaults) instead of all expiring together after a cold start.
    A day is due if it was never fetched or not since its most recent slot.
    """
    if fetched_at is None:
        return True
    period = max(COURSES_TTL - interval, interval)
    phase = (day.toordinal() % MAX_COURSE_DAYS) * period / MAX_COURSE_DAYS
    last_slot = (now + phase) // period * period - phase
    return fetched_at < last_slot or now - fetched_at + interval >= COURSES_TTL


async def prefetch_cycle(cities: list[City], interval: float) -> None:
    """Refresh whatever of `cities`' data is due this cycle.

    Covers each city's venues snapshot and venue details, and every day of the
    frontend's date strip — today's new last day included, so no date ever
    needs a blocking live fetch. Course days are refreshed on staggered slots
    (see `course_refresh_due`) to spread load on USC.
    """
    now = time.time()
    for city in cities:
        try:
            fetched_at = await run_in_threadpool(storage.get_venues_fetched_at, city.id)
            if fetched_at is None or now - fetched_at + interval >= VENUES_TTL:
                await _refresh_venues_in_background(city, force=True, enrich=False)
            # Details have their own (longer) TTL; this is a no-op when all are fresh.
            await enrich_venue_details(city.id)
        except Exception:
            logger.warning("Venue prefetch failed for city=%s", city.id, exc_info=True)

    today = date.today()
    days = [today + timedelta(days=i) for i in range(MAX_COURSE_DAYS)]
    due_by_day: dict[str, list[tuple[int, str]]] = {d.isoformat(): [] for d in days}
    for city in cities:
        fetches = await run_in_threadpool(storage.get_course_fetches, city.id, list(due_by_day))
        for d in days:
            if course_refresh_due(d, fetches.get(d.isoformat()), now, interval):
                due_by_day[d.isoformat()].append((city.id, d.isoformat()))
    # One day at a time, nearest first, all cities of that day sharing one
    # concurrency limit: a big city's day already saturates it, small towns
    # fill the gaps, and today is usable long before day 14.
    for pairs in due_by_day.values():
        if pairs:
            await _refresh_courses_in_background(pairs)

    # Keep travel times from home warm too (only re-fetches what has expired).
    await _warm_travel(cities)


async def _warm_travel(cities: list[City]) -> None:
    if travel_worker is None or travel_worker.origin is None or not cities:
        return
    points = await run_in_threadpool(storage.list_city_venue_points, [c.id for c in cities])
    await travel_worker.warm(points)


async def prefetch_loop(cities: list[City], interval: float | None = None) -> None:
    """Keep `cities` warm forever: one prefetch cycle every `interval` seconds."""
    interval = PREFETCH_INTERVAL if interval is None else interval
    logger.info("Prefetching %s every %.0f min", ", ".join(c.name for c in cities), interval / 60)
    while True:
        try:
            await prefetch_cycle(cities, interval)
            await run_in_threadpool(storage.purge_stale_courses, float(COURSES_STALE))
        except Exception:
            logger.warning("Prefetch cycle failed", exc_info=True)
        await asyncio.sleep(interval)


@app.get("/api/courses", response_model=MultiCityCoursesResponse)
async def get_courses(
    start_date: str,
    days: int = 1,
    city_ids: list[int] | None = Query(None),
):
    """Get courses across one or more cities for a date range.

    Each (city_id, date) pair is cached independently with a 2-day TTL.
    """
    if not city_ids:
        raise HTTPException(status_code=400, detail="city_ids query parameter is required")

    try:
        start = date.fromisoformat(start_date)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail="Invalid start_date, expected YYYY-MM-DD",
        ) from e

    days = max(1, min(days, MAX_COURSE_DAYS))
    date_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    # Dedupe city_ids while preserving order, keep only known ones.
    known_ids = {c.id for c in _cities_index}
    ordered_cities = [cid for cid in dict.fromkeys(city_ids) if cid in known_ids]

    now = time.time()
    entries: list[CityCoursesEntry] = []
    errors: list[CourseFetchError] = []

    # Partition per (city, date): any cached day is served from the DB — expired
    # ones get a background refresh (stale-while-revalidate) — and only days
    # with no cache at all block on a live fetch, sharing one Semaphore so
    # fan-out stays bounded.
    semaphore = asyncio.Semaphore(5)
    per_city_merged: dict[int, dict[str, list[Course]]] = {cid: {} for cid in ordered_cities}
    stale_work: list[tuple[int, str]] = []
    refresh_work: list[tuple[int, str]] = []

    for cid in ordered_cities:
        fetches = await run_in_threadpool(storage.get_course_fetches, cid, date_list)
        cached_dates = [d for d in date_list if d in fetches]
        if cached_dates:
            per_city_merged[cid].update(await run_in_threadpool(storage.get_courses_for_dates, cid, cached_dates))
        for d in cached_dates:
            if (now - fetches[d]) >= COURSES_TTL:
                refresh_work.append((cid, d))
        for d in date_list:
            if d not in fetches:
                stale_work.append((cid, d))

    if refresh_work:
        asyncio.create_task(_refresh_courses_in_background(refresh_work))

    if stale_work:
        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
            results = await asyncio.gather(
                *[fetch_courses_for_date(d, cid, client, semaphore) for (cid, d) in stale_work],
                return_exceptions=True,
            )
        for (cid, d), r in zip(stale_work, results, strict=True):
            if isinstance(r, Exception):
                logger.warning("Failed to fetch courses for city=%s %s: %r", cid, d, r)
                errors.append(CourseFetchError(date=d, reason=str(r), city_id=cid))
                per_city_merged[cid][d] = []
                continue
            per_city_merged[cid][d] = r
            await run_in_threadpool(storage.upsert_courses_for_date, cid, d, r, time.time())

    for cid in ordered_cities:
        city = next(c for c in _cities_index if c.id == cid)
        flat: list[Course] = []
        for d in date_list:
            flat.extend(per_city_merged[cid].get(d, []))
        flat.sort(key=lambda c: (c.date, c.start_time))
        entries.append(
            CityCoursesEntry(
                city_id=cid,
                city_name=city.name,
                date_from=date_list[0],
                date_to=date_list[-1],
                total=len(flat),
                courses=flat,
            )
        )

    return MultiCityCoursesResponse(
        cities=entries,
        date_from=date_list[0],
        date_to=date_list[-1],
        errors=errors,
    )


async def fetch_course_detail(course_id: int, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch a single course from the USC API (description, ratings, cancellation)."""
    if client is None:
        async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as one_shot:
            return await fetch_course_detail(course_id, one_shot)
    resp = await client.get(f"{USC_API}/courses/{course_id}")
    resp.raise_for_status()
    return resp.json().get("data", {})


def build_course_detail(raw: dict) -> CourseDetail:
    ratings = raw.get("ratings") or {}
    return CourseDetail(
        description=raw.get("description") or "",
        rating=ratings.get("averageScore"),
        rating_count=ratings.get("totalRatings"),
        free_cancellation_hours=raw.get("freeCancellationHours"),
        booking_type=raw.get("bookingType"),
        fetched_at=time.time(),
    )


async def _load_course_detail(course_id: int) -> CourseDetail:
    async with _course_detail_semaphore:
        raw = await fetch_course_detail(course_id)
    detail = build_course_detail(raw)
    await run_in_threadpool(storage.upsert_course_detail, course_id, detail)
    return detail


async def _refresh_course_detail_in_background(course_id: int) -> None:
    if course_id in _course_detail_refresh:
        return
    _course_detail_refresh.add(course_id)
    try:
        await _load_course_detail(course_id)
    except Exception as e:
        logger.warning("Background course detail refresh failed for %s: %r", course_id, e)
    finally:
        _course_detail_refresh.discard(course_id)


@app.get("/api/courses/{course_id}", response_model=CourseDetail)
async def get_course_detail(course_id: int):
    """Course description, rating and cancellation window; cached 7 days, stale served while refreshing."""
    cached = await run_in_threadpool(storage.get_course_detail, course_id)
    if cached is not None:
        if time.time() - cached.fetched_at >= COURSE_DETAILS_TTL:
            asyncio.create_task(_refresh_course_detail_in_background(course_id))
        return cached
    try:
        return await _load_course_detail(course_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Course {course_id} not found") from e
        raise HTTPException(status_code=502, detail="USC unavailable") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail="USC unavailable") from e


@app.get("/api/settings", response_model=Settings)
async def get_settings():
    return await run_in_threadpool(storage.get_settings)


@app.put("/api/settings", response_model=Settings)
async def put_settings(settings: Settings):
    """Replace the stored settings (single user, last write wins). A new home re-warms travel times."""
    previous = await run_in_threadpool(storage.get_settings)
    await run_in_threadpool(storage.put_settings, settings)
    home = (settings.home.lat, settings.home.lng) if settings.home else None
    old_home = (previous.home.lat, previous.home.lng) if previous.home else None
    if travel_worker is not None and home != old_home:
        travel_worker.set_origin(home)
        await _warm_travel(_prefetch_cities)
    return settings


@app.get("/api/geocode", response_model=list[GeocodeResult])
async def get_geocode(q: str, viewbox: str | None = None):
    """Address search (Nominatim), cached 30 days per normalised query."""
    query = " ".join(q.split())
    if not 3 <= len(query) <= 200:
        raise HTTPException(status_code=422, detail="q must be 3-200 characters")
    key = query.lower()
    cached = await run_in_threadpool(storage.get_geocode, key, float(travel.GEOCODE_TTL))
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            results = await travel.geocode(query, viewbox, client, travel.nominatim_pacer)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail="Address search unavailable") from e
    payload = [r.model_dump() for r in results]
    await run_in_threadpool(storage.set_geocode, key, payload, time.time())
    return payload


async def _stored_home() -> tuple[float, float] | None:
    settings = await run_in_threadpool(storage.get_settings)
    return (settings.home.lat, settings.home.lng) if settings.home else None


@app.post("/api/travel", response_model=TravelResponse)
async def post_travel(req: TravelRequest):
    """Cached travel data per venue, plus the ids still being computed in the background."""
    origin = (req.origin.lat, req.origin.lng) if req.origin else await _stored_home()
    if origin is None:
        raise HTTPException(status_code=409, detail="Set a home first")
    points = await run_in_threadpool(storage.get_venue_points, list(dict.fromkeys(req.venue_ids)))
    known = [vid for vid in dict.fromkeys(req.venue_ids) if vid in points]
    cached = await run_in_threadpool(storage.get_travel, travel.origin_key(*origin), known)
    if travel_worker is not None and known:
        await travel_worker.request(known, origin=origin)
    results = {
        vid: TravelResult(bike_km=e.bike_km, pt_min=e.pt_min, pt_lines=e.pt_lines, walk_m=e.walk_m)
        for vid, e in cached.items()
        if e.bike_km is not None or e.pt_min is not None
    }
    pending = [vid for vid in known if travel_worker is not None and travel_worker.is_pending(vid)]
    return TravelResponse(results=results, pending=pending)


@app.get("/api/travel/exact", response_model=ExactTransit)
async def get_travel_exact(venue_id: str, date: str, start: str):
    """Transit journey from home arriving EXACT_BUFFER before a class starts (waits up to EXACT_TIMEOUT)."""
    home = await _stored_home()
    if home is None:
        raise HTTPException(status_code=409, detail="Set a home first")
    try:
        starts_at = datetime.fromisoformat(f"{date}T{start}").replace(tzinfo=travel.BERLIN)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="Expected date=YYYY-MM-DD and start=HH:MM") from e
    points = await run_in_threadpool(storage.get_venue_points, [venue_id])
    if venue_id not in points:
        raise HTTPException(status_code=404, detail=f"Venue {venue_id} not found")
    dest = points[venue_id]
    if not (travel.in_vbb_area(*home) and travel.in_vbb_area(*dest)):
        raise HTTPException(status_code=404, detail="No transit data for this area")

    arrive_by = starts_at - EXACT_BUFFER
    cached = await run_in_threadpool(
        storage.get_exact, travel.origin_key(*home), venue_id, arrive_by.isoformat(), float(travel.EXACT_TTL)
    )
    if cached is not None:
        return cached
    if travel_worker is None:
        raise HTTPException(status_code=502, detail="Transit lookup unavailable")
    try:
        return await asyncio.wait_for(travel_worker.exact(venue_id, dest, arrive_by), EXACT_TIMEOUT)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="Transit lookup timed out") from e
    except travel.TransitUnavailable as e:
        raise HTTPException(status_code=502, detail="Transit lookup unavailable") from e


@app.get("/api/cities", response_model=CitiesResponse)
async def get_cities():
    """Return the cached cities index, seeded from USC on startup."""
    return CitiesResponse(cities=list(_cities_index), default_city_id=DEFAULT_CITY_ID)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serve frontend static files — must be last (catch-all mount)
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
