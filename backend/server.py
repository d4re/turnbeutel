"""
FastAPI backend proxy for USC API.
Fetches venue data from api.urbansportsclub.com, transforms it to match
the frontend's expected format, and caches results to disk.
"""

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load caches from disk on startup."""
    global _venues_data, _details_data

    cached_details = read_cache(DETAILS_CACHE_FILE, DETAILS_TTL_HOURS)
    if cached_details:
        _details_data = {k: v for k, v in cached_details.items() if k != "_cached_at"}

    cached_venues = read_cache(VENUES_CACHE_FILE, VENUES_TTL_HOURS)
    if cached_venues:
        _venues_data = cached_venues
        _merge_details_into_venues()

    yield


app = FastAPI(title="USC Venue Explorer API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Config ──

USC_API = "https://api.urbansportsclub.com/api/v6"
USC_HEADERS = {
    "User-Agent": "USCAPP/4.0.8 (android; 28; Scale/2.75)",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US;q=1.0",
}
CITY_ID = 1  # Berlin
PAGE_SIZE = 100

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

VENUES_CACHE_FILE = CACHE_DIR / "venues.json"
DETAILS_CACHE_FILE = CACHE_DIR / "venue_details.json"
CATEGORIES_CACHE_FILE = CACHE_DIR / "categories.json"

VENUES_TTL_HOURS = 24
DETAILS_TTL_HOURS = 7 * 24  # 7 days
CATEGORIES_TTL_HOURS = 7 * 24

# ── Tier mappings (from merge_data.py) ──

PRIVATE_TIER_ORDER = ["Essential", "Classic", "Premium", "Max"]
CORPORATE_TIER_ORDER = ["S", "M", "L", "XL"]
CORP_TO_PRIVATE = {"S": "Essential", "M": "Classic", "L": "Premium", "XL": "Max"}

TIER_CONFIG = {
    "private": {
        "order": PRIVATE_TIER_ORDER,
        "colors": {"Essential": "#27ae60", "Classic": "#2980b9", "Premium": "#e67e22", "Max": "#c0392b"},
    },
    "corporate": {
        "order": CORPORATE_TIER_ORDER,
        "display": {"S": "S", "M": "M Pro", "L": "L Pro", "XL": "XL Pro"},
        "colors": {"S": "#27ae60", "M": "#2980b9", "L": "#e67e22", "XL": "#c0392b"},
    },
}

# ── In-memory state ──

_venues_data: dict | None = None
_details_data: dict = {}
_enrichment_running = False


# ── Cache helpers ──


def read_cache(path: Path, max_age_hours: float) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = data.get("_cached_at", 0)
        if (time.time() - cached_at) > max_age_hours * 3600:
            return None
        return data
    except (json.JSONDecodeError, KeyError):
        return None


def write_cache(path: Path, data: dict) -> None:
    out = {**data, "_cached_at": time.time()}
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


# ── Visit limits parsing ──

VISIT_LIMIT_RE = re.compile(
    r"(S|M|L|XL)-Mitglieder\s+können.*?(\d+)\s*(?:x|Mal)\s*pro\s*Monat",
    re.IGNORECASE,
)


def parse_visit_limits(text: str | None) -> dict | None:
    """Parse bookingLimitsText into structured visit limits."""
    if not text:
        return None
    matches = VISIT_LIMIT_RE.findall(text)
    if not matches:
        return None

    corporate = {}
    private = {}
    for tier_letter, count in matches:
        tier_letter = tier_letter.upper()
        count = int(count)
        # Keep first match per tier (general limit, not per-activity limit)
        if tier_letter not in corporate:
            corporate[tier_letter] = count
            private_name = CORP_TO_PRIVATE.get(tier_letter)
            if private_name:
                private[private_name] = count

    if not corporate:
        return None

    # Fill in nulls for tiers not mentioned
    for t in CORPORATE_TIER_ORDER:
        corporate.setdefault(t, None)
    for t in PRIVATE_TIER_ORDER:
        private.setdefault(t, None)

    return {"private": private, "corporate": corporate}


# ── Venue transformation ──


def min_tier(tiers: list[str], order: list[str]) -> str | None:
    for t in order:
        if t in tiers:
            return t
    return None


def transform_venue(raw: dict, detail: dict | None = None) -> dict:
    """Transform a USC API venue object into the frontend format."""
    loc = raw.get("location", {})
    plan_types = raw.get("planTypes", [])
    plan_types_b2b = raw.get("planTypesB2B", [])

    tiers_private = [CORP_TO_PRIVATE[t] for t in plan_types if t in CORP_TO_PRIVATE]
    tiers_corporate = [t for t in plan_types_b2b if t in CORPORATE_TIER_ORDER]

    lat = loc.get("latitude")
    lng = loc.get("longitude")
    slug = raw.get("urlSlug", "")

    categories = raw.get("categories", [])
    activities = []
    for cat in categories:
        name = (cat.get("translations") or {}).get("en_GB") or cat.get("name", "")
        if name and name not in activities:
            activities.append(name)

    ratings = raw.get("ratings", {})
    district_obj = loc.get("district", {})

    visit_limits = None
    booking_limits_text = None
    if detail:
        visit_limits = detail.get("visit_limits")
        booking_limits_text = detail.get("bookingLimitsText")

    return {
        "name": raw.get("name", "").strip(),
        "slug": slug,
        "url": f"https://urbansportsclub.com/en/venues/{slug}" if slug else "",
        "tiers_private": tiers_private,
        "tiers_corporate": tiers_corporate,
        "min_tier_private": min_tier(tiers_private, PRIVATE_TIER_ORDER),
        "min_tier_corporate": min_tier(tiers_corporate, CORPORATE_TIER_ORDER),
        "activities": activities,
        "district": district_obj.get("name", ""),
        "street": loc.get("address", ""),
        "is_plus": raw.get("isPlusCheckin", 0) == 1,
        "address_id": str(raw.get("id", "")),
        "lat": lat,
        "lng": lng,
        "address": {
            "street": loc.get("address", ""),
            "postal_code": loc.get("postalCode", ""),
            "city": f"{loc.get('city', {}).get('name', '')}, {loc.get('country', {}).get('code', '')}",
        },
        "rating": ratings.get("averageScore"),
        "review_count": ratings.get("totalRatings"),
        "visit_limits": visit_limits,
        "bookingLimitsText": booking_limits_text,
        "is_online": raw.get("isOnline", 0) == 1,
        "has_coordinates": bool(lat and lng),
    }


# ── API fetching ──


async def fetch_all_venue_pages() -> list[dict]:
    """Fetch all venue pages from the USC API concurrently."""
    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
        # First request to determine total pages
        first = await client.get(f"{USC_API}/venues", params={"cityId": CITY_ID, "page": 1, "pageSize": PAGE_SIZE})
        first.raise_for_status()
        first_data = first.json().get("data", [])
        if not first_data:
            return []

        all_venues = list(first_data)

        if len(first_data) < PAGE_SIZE:
            return all_venues

        # Calculate remaining pages needed (add 1 extra to be safe)
        estimated_total = len(first_data) * 30  # rough upper bound
        total_pages = (estimated_total // PAGE_SIZE) + 2
        tasks = [
            client.get(f"{USC_API}/venues", params={"cityId": CITY_ID, "page": p, "pageSize": PAGE_SIZE})
            for p in range(2, total_pages + 1)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for resp in responses:
            if isinstance(resp, Exception):
                continue
            data = resp.json().get("data", [])
            if not data:
                break
            all_venues.extend(data)

        return all_venues


async def fetch_venue_detail(venue_id: int) -> dict:
    """Fetch a single venue detail from the USC API."""
    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
        resp = await client.get(f"{USC_API}/venues/{venue_id}")
        resp.raise_for_status()
        return resp.json().get("data", {})


# ── Background enrichment ──


async def enrich_venue_details() -> None:
    """Background task: fetch details for all venues that lack cached detail data."""
    global _details_data, _enrichment_running
    if _enrichment_running:
        return
    _enrichment_running = True

    try:
        # Load existing detail cache
        cached = read_cache(DETAILS_CACHE_FILE, DETAILS_TTL_HOURS)
        if cached:
            _details_data = {k: v for k, v in cached.items() if k != "_cached_at"}

        if _venues_data is None:
            return

        venue_ids = [v["address_id"] for v in _venues_data["venues"]]
        semaphore = asyncio.Semaphore(5)

        async def fetch_one(vid: str) -> None:
            if vid in _details_data:
                return
            async with semaphore:
                try:
                    raw = await fetch_venue_detail(int(vid))
                    limits_text = raw.get("bookingLimitsText")
                    _details_data[vid] = {
                        "visit_limits": parse_visit_limits(limits_text),
                        "bookingLimitsText": limits_text,
                        "importantInfo": raw.get("importantInfo"),
                        "phone": raw.get("phone"),
                        "website": raw.get("website"),
                        "description": raw.get("description"),
                        "fetched_at": time.time(),
                    }
                except Exception:
                    pass  # skip failures, will retry on next enrichment cycle

        # Process in batches to periodically save progress
        batch_size = 50
        for i in range(0, len(venue_ids), batch_size):
            batch = venue_ids[i : i + batch_size]
            await asyncio.gather(*[fetch_one(vid) for vid in batch])
            # Save progress to disk
            write_cache(DETAILS_CACHE_FILE, dict(_details_data))

        # Final merge into in-memory venues data
        _merge_details_into_venues()

    finally:
        _enrichment_running = False


def _merge_details_into_venues() -> None:
    """Merge cached detail data into the in-memory venues list."""
    if _venues_data is None:
        return
    for venue in _venues_data["venues"]:
        detail = _details_data.get(venue["address_id"])
        if detail:
            venue["visit_limits"] = detail.get("visit_limits")
            venue["bookingLimitsText"] = detail.get("bookingLimitsText")


# ── Endpoints ──


@app.get("/api/venues")
async def get_venues():
    global _venues_data

    # Return from cache if fresh
    if _venues_data and read_cache(VENUES_CACHE_FILE, VENUES_TTL_HOURS):
        return _venues_data

    # Fetch from USC API
    raw_venues = await fetch_all_venue_pages()

    # Transform
    venues = [transform_venue(v, _details_data.get(str(v.get("id", "")))) for v in raw_venues]
    # Filter out deleted/empty venues and venues with no tier access
    venues = [v for v in venues if v["name"] and (v["tiers_private"] or v["tiers_corporate"])]
    venues.sort(key=lambda v: v["name"])

    _venues_data = {
        "fetched_at": time.time(),
        "total_venues": len(venues),
        "venues_with_coords": sum(1 for v in venues if v["has_coordinates"]),
        "tier_config": TIER_CONFIG,
        "venues": venues,
    }

    write_cache(VENUES_CACHE_FILE, _venues_data)

    # Kick off background enrichment
    asyncio.create_task(enrich_venue_details())

    return _venues_data


@app.get("/api/venues/{venue_id}")
async def get_venue_detail(venue_id: int):
    vid = str(venue_id)

    # Check detail cache
    if vid in _details_data:
        return _details_data[vid]

    # Fetch from API
    raw = await fetch_venue_detail(venue_id)
    limits_text = raw.get("bookingLimitsText")
    detail = {
        "visit_limits": parse_visit_limits(limits_text),
        "bookingLimitsText": limits_text,
        "importantInfo": raw.get("importantInfo"),
        "phone": raw.get("phone"),
        "website": raw.get("website"),
        "description": raw.get("description"),
        "fetched_at": time.time(),
    }

    # Cache
    _details_data[vid] = detail
    write_cache(DETAILS_CACHE_FILE, dict(_details_data))

    return detail


@app.get("/api/categories")
async def get_categories():
    cached = read_cache(CATEGORIES_CACHE_FILE, CATEGORIES_TTL_HOURS)
    if cached:
        return cached

    async with httpx.AsyncClient(headers=USC_HEADERS, timeout=30) as client:
        resp = await client.get(f"{USC_API}/categories")
        resp.raise_for_status()
        data = resp.json()

    write_cache(CATEGORIES_CACHE_FILE, data)
    return data


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
