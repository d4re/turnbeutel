"""
FastAPI backend proxy for USC API.

Fetches venue, course, and category data from api.urbansportsclub.com,
transforms it to the frontend's expected format, and caches results in a
single SQLite database (see storage.py).
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import storage
from models import (
    CitiesResponse,
    City,
    CityCoursesEntry,
    CityVenuesEntry,
    Course,
    CourseFetchError,
    MultiCityCoursesResponse,
    MultiCityVenuesPayload,
    TierConfig,
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

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
DB_PATH = CACHE_DIR / "usc.db"

VENUES_TTL = 24 * 3600
DETAILS_TTL = 7 * 24 * 3600
CATEGORIES_TTL = 7 * 24 * 3600
COURSES_TTL = 48 * 3600
COURSES_STALE = 3 * 24 * 3600
CITIES_TTL = 7 * 24 * 3600

# ── Tier mappings ──

PRIVATE_TIER_ORDER = ["Essential", "Classic", "Premium", "Max"]
CORPORATE_TIER_ORDER = ["S", "M", "L", "XL"]
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


def _invalidate_venues_cache(city_id: int) -> None:
    _venues_response_cache.pop(city_id, None)


# ── Lifespan ──


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite storage, sync /cities index, and purge stale courses."""
    global _cities_index

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

    # Re-derive visit_limits from cached booking_limits_text on every startup.
    # Cheap (~ms per row) and ensures parser changes take effect without a refetch.
    reparsed = await run_in_threadpool(storage.reparse_visit_limits, parse_visit_limits)
    if reparsed:
        logger.info("Re-parsed visit limits for %d cached venue_details rows", reparsed)

    yield

    storage.close()


app = FastAPI(title="USC Venue Explorer API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Visit limits parsing ──

# Matches the body of a "<tiers>-Mitglieder können|dürfen ... N x/Mal pro/im/am (Monat|Tag)" sentence.
# Tier names live in the prefix immediately before "Mitglieder" — extracted separately
# so we can handle grouped forms like "L- & XL-Mitglieder" or "M, L und XL-Mitglieder".
# Per-day limits are converted to a 30-day month equivalent.
#
# Filler words may appear both before the count ("im Rahmen ihres Kontingents von insgesamt 4 x")
# and between the count and the preposition ("4 x Sport im Monat", "8x Padel pro Monat"), so the
# pattern allows lazy non-period stretches on either side. Requiring `x|Mal` after the digit biases
# the capture toward genuine visit counts rather than incidental numbers in the same sentence.
VISIT_LIMIT_SEGMENT_RE = re.compile(
    r"Mitglieder\s+(?:können|dürfen)[^.]*?(\d+)\s*-?\s*(?:x|mal)\b[^.]*?\b(?:pro|im|am|/)\s*(Monat|Tag)",
    re.IGNORECASE,
)
CORPORATE_TIER_TOKEN_RE = re.compile(r"\b(XL|S|M|L)\b", re.IGNORECASE)
PRIVATE_TIER_TOKEN_RE = re.compile(r"\b(Essential|Classic|Premium|Max)\b", re.IGNORECASE)
PRIVATE_TO_CORP = {v: k for k, v in CORP_TO_PRIVATE.items()}
DAYS_PER_MONTH = 30


def parse_visit_limits(text: str | None) -> VisitLimits | None:
    """Parse bookingLimitsText into a structured VisitLimits model.

    Both corporate (S/M/L/XL) and private (Essential/Classic/Premium/Max) tier
    names in the source text are matched directly. Whichever side is parsed gets
    the other side derived via the CORP_TO_PRIVATE mapping.

    "pro Monat" is taken at face value; "pro Tag" is converted to a monthly
    figure assuming a 30-day month (e.g. "1 Mal pro Tag" → 30 / month).
    """
    if not text:
        return None

    corporate: dict[str, int | None] = {}
    private: dict[str, int | None] = {}
    saw_segment = False

    for m in VISIT_LIMIT_SEGMENT_RE.finditer(text):
        saw_segment = True
        count = int(m.group(1))
        if m.group(2).lower() == "tag":
            count *= DAYS_PER_MONTH
        # Look at the text just before "Mitglieder" for the tier prefix.
        # Trim to the most recent sentence/line break so we don't pick up
        # tier names belonging to a previous sentence.
        prefix = text[max(0, m.start() - 80) : m.start()]
        for sep in (".", "\n", "\r"):
            idx = prefix.rfind(sep)
            if idx >= 0:
                prefix = prefix[idx + 1 :]
        for tier in CORPORATE_TIER_TOKEN_RE.findall(prefix):
            tier = tier.upper()
            if tier in CORPORATE_TIER_ORDER and tier not in corporate:
                corporate[tier] = count
        for tier in PRIVATE_TIER_TOKEN_RE.findall(prefix):
            tier = tier.capitalize()
            if tier in PRIVATE_TIER_ORDER and tier not in private:
                private[tier] = count

    if not corporate and not private:
        logger.warning(
            "parse_visit_limits: could not extract any tier limits from text: %r",
            text,
        )
        return None

    # In practice USC's text always uses corporate-style letters, so a missing
    # direct private match is the norm — log at debug, not warning.
    if saw_segment and not corporate:
        logger.debug(
            "parse_visit_limits: no corporate tiers parsed (only private) from text: %r",
            text,
        )
    if saw_segment and not private:
        logger.debug(
            "parse_visit_limits: no private tiers parsed (only corporate) from text: %r",
            text,
        )

    # Cross-fill: USC almost always uses corporate-style letters, but if either
    # side is populated, derive the other via the 1:1 mapping.
    for corp, priv in CORP_TO_PRIVATE.items():
        if corp in corporate and priv not in private:
            private[priv] = corporate[corp]
        if priv in private and corp not in corporate:
            corporate[corp] = private[priv]

    for t in CORPORATE_TIER_ORDER:
        corporate.setdefault(t, None)
    for t in PRIVATE_TIER_ORDER:
        private.setdefault(t, None)

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
        bookingLimitsText=detail.bookingLimitsText if detail else None,
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


async def fetch_all_venue_pages(usc_city_id: int) -> list[dict]:
    """Fetch all venue pages from the USC API in bounded batches.

    Pages are fetched VENUE_PAGE_BATCH at a time until a short page signals the
    end (capped at MAX_VENUE_PAGES as a runaway guard). A failed page raises so
    a venue list with a silent gap is never cached.
    """
    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:

        async def fetch_page(page: int) -> list[dict]:
            resp = await client.get(
                f"{USC_API}/venues",
                params={"cityId": usc_city_id, "page": page, "pageSize": PAGE_SIZE},
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

        all_venues = await fetch_page(1)
        if len(all_venues) < PAGE_SIZE:
            return all_venues

        page = 2
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


# ── Course transformation and fetching ──


def transform_course(raw: dict) -> Course:
    """Transform a USC API course object into a Course model."""
    venue = raw.get("venue") or {}
    loc = venue.get("location") or {}
    district = (loc.get("district") or {}).get("name", "")
    category = raw.get("category") or {}
    start_time = raw.get("startTime") or ""
    end_time = raw.get("endTime") or ""
    return Course(
        id=raw.get("id"),
        date=raw.get("date", ""),
        title=raw.get("title", ""),
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
    )


async def fetch_courses_for_date(
    date_str: str,
    usc_city_id: int,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> list[Course]:
    """Fetch all courses for a single date from USC (no caching — the caller handles storage)."""
    all_raw: list[dict] = []
    page = 1
    while True:
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
        classes = data.get("classes") or []
        if not classes:
            break
        all_raw.extend(classes)
        if len(classes) < PAGE_SIZE:
            break
        page += 1
        if page > 50:
            break

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
                    raw = await fetch_venue_detail(int(vid), client)
                    limits_text = raw.get("bookingLimitsText")
                    detail = VenueDetail(
                        visit_limits=parse_visit_limits(limits_text),
                        bookingLimitsText=limits_text,
                        importantInfo=raw.get("importantInfo"),
                        phone=raw.get("phone"),
                        website=raw.get("website"),
                        description=raw.get("description"),
                        fetched_at=time.time(),
                    )
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


async def _load_venues_for_city(city_id: int) -> CityVenuesEntry | None:
    """Resolve one city's venues, honoring memory and DB caches.

    Returns None if the city is not in `_cities_index` (unknown id).
    """
    city = next((c for c in _cities_index if c.id == city_id), None)
    if city is None:
        return None

    payload = _fresh_cached_payload(city_id)

    if payload is None:
        fetched_at = await run_in_threadpool(storage.get_venues_fetched_at, city_id)
        if fetched_at is not None and (time.time() - fetched_at) < VENUES_TTL:
            payload = await run_in_threadpool(storage.get_venues_payload, city_id)
            if payload is not None:
                payload.tier_config = TIER_CONFIG
                _venues_response_cache[city_id] = (time.time(), payload)

    if payload is None:
        # Bound concurrent live USC pulls globally. The cache reads above are
        # unthrottled; only the cold fetch acquires the semaphore.
        async with _venue_fetch_semaphore:
            # Re-check: another request may have fetched this same city while we
            # were queued on the semaphore.
            payload = _fresh_cached_payload(city_id)
            if payload is None:
                raw_venues = await fetch_all_venue_pages(usc_city_id=city_id)
                venues = [transform_venue(v) for v in raw_venues]
                venues = [v for v in venues if v.name and (v.tiers_private or v.tiers_corporate)]
                venues.sort(key=lambda v: v.name)

                total = len(venues)
                with_coords = sum(1 for v in venues if v.has_coordinates)
                now = time.time()
                await run_in_threadpool(storage.upsert_venues, city_id, venues, now, total, with_coords)

                payload = await run_in_threadpool(storage.get_venues_payload, city_id)
                assert payload is not None
                payload.tier_config = TIER_CONFIG
                _venues_response_cache[city_id] = (now, payload)
                asyncio.create_task(enrich_venue_details(city_id))

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
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Venue {venue_id} not found") from e
        raise
    limits_text = raw.get("bookingLimitsText")
    detail = VenueDetail(
        visit_limits=parse_visit_limits(limits_text),
        bookingLimitsText=limits_text,
        importantInfo=raw.get("importantInfo"),
        phone=raw.get("phone"),
        website=raw.get("website"),
        description=raw.get("description"),
        fetched_at=time.time(),
    )
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

    # Partition per (city, date): fresh from DB vs stale -> needs fetch.
    # All stale fetches share one Semaphore so fan-out stays bounded.
    semaphore = asyncio.Semaphore(5)
    per_city_merged: dict[int, dict[str, list[Course]]] = {cid: {} for cid in ordered_cities}
    stale_work: list[tuple[int, str]] = []

    for cid in ordered_cities:
        fetches = await run_in_threadpool(storage.get_course_fetches, cid, date_list)
        fresh_dates = [d for d in date_list if d in fetches and (now - fetches[d]) < COURSES_TTL]
        stale_dates = [d for d in date_list if d not in fresh_dates]
        if fresh_dates:
            per_city_merged[cid].update(await run_in_threadpool(storage.get_courses_for_dates, cid, fresh_dates))
        for d in stale_dates:
            stale_work.append((cid, d))

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
