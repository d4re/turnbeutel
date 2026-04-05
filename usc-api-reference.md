# Urban Sports Club API Reference

Unofficial documentation of the USC consumer API, reverse-engineered from the mobile app and the [usc-auto-book](https://github.com/Anroc/usc-auto-book) project.

**Base URL**: `https://api.urbansportsclub.com/api/v6`

**Authentication**: Not required for read-only endpoints (`/venues`, `/courses`, `/categories`, `/cities`, `/districts`). Required for booking (`/bookings`) via OAuth 2.0.

**Headers** (recommended):
```
User-Agent: USCAPP/4.0.8 (android; 28; Scale/2.75)
Accept-Encoding: gzip, deflate
Accept-Language: en-US;q=1.0
```

## Read-Only Endpoints

### GET /venues

List venues with filtering and pagination.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `cityId` | int | yes | City ID (Berlin = 1). Without this, the request may time out. |
| `page` | int | yes | Page number, starts at 1 |
| `pageSize` | int | yes | Results per page (max 100) |
| `planType` | string | no | Filter by private plan: `S`, `M`, `L`, `XL` |
| `categoryId` | int | no | Filter by activity category (see `/categories`) |

**Response** (truncated):
```json
{
  "success": "true",
  "data": [
    {
      "id": 4926,
      "name": "Yellow Yoga - Studio Sonne",
      "location": {
        "displayAddress": "Berlin - Neukölln",
        "city": { "id": 1, "name": "Berlin" },
        "district": { "id": 88, "name": "Neukölln", "area": "Berlin" },
        "latitude": 52.48444,
        "longitude": 13.43498,
        "postalCode": "12045",
        "address": "Sonnenallee 67",
        "country": { "code": "DE" }
      },
      "planTypes": ["M", "L", "XL"],
      "planTypesB2B": ["M", "L", "XL"],
      "allowedBusinessTypes": ["b2c", "b2b"],
      "appointmentTypes": ["onsite"],
      "isOnline": 0,
      "isPlusCheckin": 0,
      "deleted": 0,
      "covers": [ { "cover150": "https://...", "cover1024": "https://..." } ],
      "categories": [
        {
          "id": 6,
          "name": "Yoga",
          "allowed_plan_types": [1, 3, 6],
          "allowed_plan_types_b2b": [1, 3, 6],
          "category_group_id": 2,
          "translations": { "en_GB": "Yoga", "de_DE": "Yoga" }
        }
      ],
      "urlSlug": "yellow-yoga-studio-sonne",
      "ratings": { "averageScore": 4.9, "totalRatings": 9777 }
    }
  ]
}
```

**Notes:**
- Berlin returns 2,639 venues across 27 pages (at pageSize=100). This is more than the 1,931 from the website — likely includes online/partner venues.
- `planTypes` = private tiers, `planTypesB2B` = corporate tiers. These can differ per venue.
- Each category within a venue has its own `allowed_plan_types` and `allowed_plan_types_b2b`, meaning tier access can vary by activity at the same venue.
- The `id` field matches the `data-address-id` attribute in the website HTML.
- Pagination returns an empty `data` array when past the last page.

### GET /venues/{id}

Single venue detail. Returns the same structure as a single item from `/venues`, plus additional fields:

```
GET /venues/4926
```

Extra fields include:
- `bookingLimitsText` — human-readable visit limits per tier (e.g., "M-Mitglieder können 4x pro Monat...")
- `importantInfo` — venue-specific notes
- `phone`, `website`, `openingHoursText`

### GET /courses

List classes/courses with filtering. Supports city-wide search (not just per-venue).

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `locationId` | int | no | Venue ID (same as venue `id`). Omit for city-wide search. |
| `cityId` | int | no | City ID for city-wide search. Use this OR `locationId`. |
| `districtId` | int | no | District ID for district-wide search. |
| `categoryId` | int | no | Activity category filter (see `/categories`) |
| `startDate` | string | yes | Date in `YYYY-MM-DD` format |
| `forDurationOfDays` | int | yes | Number of days to search (1 = single day) |
| `pageSize` | int | yes | Results per page (max 100) |
| `page` | int | yes | Page number, starts at 1 |
| `query` | string | no | Free-text search |

**Response:**
```json
{
  "success": "true",
  "data": {
    "classes": [
      {
        "id": 99051489,
        "date": "2026-04-05",
        "title": "Kundalini IN STUDIO SONNE with Paula (all levels/english)",
        "startTime": "12:15:00",
        "startDateTimeUTC": "2026-04-05T12:15:00+02:00",
        "endTime": "13:30:00",
        "endDateTimeUTC": "2026-04-05T13:30:00+02:00",
        "venue": {
          "id": 4926,
          "name": "Yellow Yoga - Studio Sonne",
          "location": { "displayAddress": "Berlin - Neukölln", "latitude": 52.48444, "longitude": 13.43498, ... },
          "bookingLimitsText": "M-Mitglieder können 4x pro Monat...",
          "importantInfo": "..."
        },
        "category": { "id": 6, "name": "Yoga" },
        "teacherName": "Paula",
        "bookable": 1,
        "freeSpots": 32,
        "maximumNumber": 40,
        "minimumNumber": 0,
        "planTypes": [],
        "planTypesB2B": [],
        "serviceType": "classes",
        "bookingType": "instant",
        "isOnline": 0,
        "isPlusCheckin": 0,
        "deleted": 0,
        "booking": null
      }
    ]
  }
}
```

**Notes:**
- City-wide search is the killer feature: `cityId=1&categoryId=7` returns all Dance classes in Berlin in a single paginated request.
- `planTypes` and `planTypesB2B` at the class level are populated for some classes but empty for others. Venue-level tier info is the more reliable and consistent source.
- `venue` is embedded in each class object, so you get full location data without a separate lookup.
- `freeSpots` shows real-time availability.
- `booking` is `null` when not authenticated. When logged in, shows booking status.
- The `startDate` parameter limits how far ahead you can search — max observed is ~13 days out.

### GET /categories

Full list of activity categories with sub-categories.

**Response:**
```json
{
  "success": "true",
  "data": [
    {
      "id": 7,
      "name": "Dance",
      "icon": "https://...",
      "sub-categories": [
        { "id": 345, "name": "African Dance" },
        { "id": 101, "name": "Ballett" },
        { "id": 330, "name": "Hip Hop" },
        { "id": 102, "name": "Pole Dance" },
        { "id": 100, "name": "Salsa" }
      ]
    }
  ]
}
```

Selected category IDs:
- 1 = Fitness, 4 = Pilates, 6 = Yoga, 7 = Dance, 9 = Massage
- 17 = Aqua, 135 = Pole Dance, 173 = Sauna, 174 = Spa
- 232 = Bouldering, 233 = Functional Training, 253 = MMA
- 262 = Padel, 271 = Swimming, 286 = Cryotherapy

### GET /cities

All cities where USC operates.

**Response:**
```json
{
  "success": "true",
  "data": [
    { "id": 1, "defaultName": "Berlin", "lat": 52.52, "lon": 13.405, "countryCode": "DE" },
    { "id": 2, "defaultName": "München", "lat": 48.137, "lon": 11.575, "countryCode": "DE" }
  ]
}
```

### GET /districts

All districts, optionally filtered by city.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `cityId` | int | no | Filter districts by city |

**Response:**
```json
{
  "success": "true",
  "data": [
    {
      "id": 276,
      "name": "Berlin",
      "sub-districts": [
        { "id": 60, "name": "Adlershof" },
        { "id": 14, "name": "Kreuzberg" },
        { "id": 88, "name": "Neukölln" }
      ]
    }
  ]
}
```

## Authenticated Endpoints

These require a bearer token obtained via `/auth/token`. Only needed for booking, not for browsing.

### POST /auth/token

OAuth 2.0 password grant. Returns a bearer token for authenticated requests.

**Body:**
```json
{
  "username": "your-email@example.com",
  "password": "your-password",
  "client_id": "86093282310",
  "client_secret": "1BJX3V5HWUYVCZ77S1TY9L1PSWAXA3K95ZMUC3ZRBAP3M696ZF4SD3QW5VBNU81H",
  "grant_type": "password"
}
```

**Response:**
```json
{ "data": { "access_token": "<bearer-token>" } }
```

**Note:** The `client_id` and `client_secret` above are from the usc-auto-book repo (extracted from the Android app). They may be rotated.

### POST /bookings

Book a class. Requires authentication.

**Headers:**
```
Authorization: Bearer <token>
```

**Body:**
```json
{ "courseId": 99051489 }
```

## Plan Type ID Mapping

Plan types in the API are represented as integers in category-level `allowed_plan_types`:

| ID | Private Name | Corporate Name |
|----|-------------|----------------|
| 0  | Essential   | S              |
| 1  | Classic     | M              |
| 3  | Premium     | L              |
| 6  | Max         | XL             |

At the venue level, `planTypes` and `planTypesB2B` use string names ("S", "M", "L", "XL") instead of IDs.

## Comparison: API vs HTML Scraping

| | API | HTML Scraping |
|---|---|---|
| Venues in Berlin | 2,639 | 1,931 |
| Time to get all venues | ~5 sec (27 API calls) | ~2 min (64 pages) |
| Time to get all venue details | Not needed (included in /venues) | ~30 min (1,931 pages) |
| Tier data accuracy | Separate `planTypes`/`planTypesB2B` | Private tiers only on listing; detail pages needed for corporate |
| Coordinates | Included in venue response | Requires detail page scrape or geocoding |
| Class schedules | City-wide search in 1 call | Must scrape each venue page individually |
| Visit limits | Human-readable text only (`bookingLimitsText`) | Structured table with exact counts per tier |
| Rate limiting | Unknown, but fast responses observed | 1-2 sec delay recommended |
| Stability | API contracts tend to be more stable | HTML selectors break on redesign |

**Recommendation:** Use the API for venues, coordinates, tiers, and class schedules. Fall back to HTML scraping only for structured visit-limit tables (exact counts per tier per venue), which the API only provides as free-text.

## API Versioning: v5 vs v6

Both v5 and v6 are active. v7+ returns `success: false` with no data. **Use v6** — it is a strict superset of v5 with these improvements:

| Feature | v5 | v6 |
|---|---|---|
| Ratings in `/venues` listing | Always `{averageScore: 0, totalRatings: 0}` | Returns actual ratings (e.g., `{averageScore: 4.9, totalRatings: 49}`) |
| Ratings in `/venues/{id}` detail | Works correctly | Works correctly (same as v5) |
| `isFavorited` field in `/venues` listing | Not present | Present (always `0` when unauthenticated) |
| `isPlusCheckin` in `/venues` listing | May differ from v6 | Appears more accurate |
| `/courses`, `/categories`, `/cities`, `/districts` | Identical | Identical |
| Total item counts | Same | Same |

**Recommendation:** Use v6 for all endpoints. The key win is that venue ratings are populated in listing responses, eliminating the need to fetch each venue detail individually just to get ratings.

## Geo/Distance-Based Search

**The API does not support distance-based search or sorting.** This was tested extensively:

- `latitude`, `longitude`, `radius` parameters on `/venues` are silently ignored — results are always alphabetical within a `cityId`.
- `sort=distance`, `sortBy=distance`, `orderBy=distance` have no effect on `/venues`.
- Without `cityId`, `/venues` returns all venues globally (still alphabetical), ignoring any geo params.
- `/venues/search`, `/venues/nearby` return empty `data` arrays.
- The `/courses` endpoint also ignores `latitude`/`longitude` — tested with coordinates in different parts of Berlin with identical results.

**The website** (`urbansportsclub.com/en/venues?city_id=1&sort=distance&lat=...&lng=...`) accepts distance-sort params in the URL, but the actual sorting appears to be done client-side via browser geolocation JavaScript, not server-side.

### Cross-City Limitations

Each city is a separate silo:
- Berlin (id=1) has ~2,600 venues, Potsdam (id=10) has ~78 venues.
- There is no way to query "all venues within X km" across city boundaries in a single request.
- Venues just outside a city's administrative boundary only appear under their own city ID.

### Recommended Approach: Client-Side Distance Filtering

Since every venue response includes `latitude` and `longitude`, compute distance client-side:

1. **Fetch venues from multiple nearby cities** (e.g., Berlin + Potsdam) in parallel.
2. **Compute distance** using the Haversine formula from the user's coordinates.
3. **Sort and filter** by the computed distance.

This matches what the mobile app likely does — the API provides coordinates, the app sorts by device GPS position.

```python
import math

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
```

## Other APIs (Not Useful for Members)

### Partner API (`connect.urbansportsclub.io`)
Official API for studio/venue operators. Documented at [docs.urbansportsclub.io](https://docs.urbansportsclub.io/). Requires onboarding credentials. Endpoints include `/endpoint/bookings`, `/endpoint/access-control`, `/endpoint/locations`. Not accessible to regular members.

### Known Integrations
Studios can connect via bsport, Magicline, Eversports, Virtuagym, and CodexFit — all venue-side integrations using the Partner API.
