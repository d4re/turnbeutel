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

**Response** (truncated, real sample from `/venues?cityId=1&page=1&pageSize=3`):
```json
{
  "success": "true",
  "data": [
    {
      "id": 27355,
      "name": "Brooklyn Fitboxing Potsdamer-Str",
      "urlSlug": "brooklyn-fitboxing-potsdamer-str",
      "location": {
        "displayAddress": "Berlin - Schöneberg",
        "city": { "id": 1, "name": "Berlin" },
        "district": { "id": 87, "name": "Schöneberg", "area": "Berlin" },
        "latitude": 52.4985601,
        "longitude": 13.3626739,
        "postalCode": "10783",
        "address": "Potsdamer Straße 125",
        "additionalInformation": "",
        "country": { "code": "DE" }
      },
      "planTypes": ["M", "L", "XL"],
      "planTypesB2B": ["M", "L", "XL"],
      "allowedBusinessTypes": ["b2c", "b2b"],
      "appointmentTypes": ["onsite"],
      "isOnline": 0,
      "isPlusCheckin": 0,
      "isMyClubs": false,
      "isFavorited": 0,
      "highlight": 1,
      "deleted": 0,
      "policy": { "type": null },
      "covers": [
        {
          "id": 12345,
          "sortOrder": 0,
          "title": "",
          "cover150": "https://...",
          "cover311": "https://...",
          "cover720": "https://...",
          "cover1024": "https://..."
        }
      ],
      "categories": [
        {
          "id": 1,
          "key": "40001",
          "name": "Fitness",
          "icon": "https://...",
          "category_group_id": 2,
          "allowed_plan_types": [2, 3, 6],
          "allowed_plan_types_b2b": [2, 3, 6],
          "translations": {
            "en_GB": "Fitness", "de_DE": "Fitness", "fr_FR": "Fitness",
            "es_ES": "Fitness", "pt_PT": "Fitness", "nl_NL": "Fitness"
          }
        }
      ],
      "ratings": { "averageScore": 4.9, "totalRatings": 7134 }
    }
  ]
}
```

**Notes:**
- Berlin returns ~2,600 venues across ~27 pages (at pageSize=100). More than the ~1,931 from the website — includes online/partner venues.
- `planTypes` = private/B2C tiers, `planTypesB2B` = corporate/B2B tiers. These can differ per venue. Pure online venues sometimes have an empty `planTypes` and only `planTypesB2B`.
- Each category within a venue has its own `allowed_plan_types` and `allowed_plan_types_b2b` (integer IDs — see [Plan Type ID Mapping](#plan-type-id-mapping)). The venue-level `planTypes` arrays are the union across categories. Tier access can vary by activity at the same venue.
- `appointmentTypes` is one of `["onsite"]`, `["online"]`, or both — describes whether check-in is in person or via the app/web.
- `allowedBusinessTypes` declares which membership families the venue accepts (`"b2c"`, `"b2b"`).
- `covers` is a sorted list with multiple resolutions per image (`cover150`, `cover311`, `cover720`, `cover1024`) plus `id`, `sortOrder`, `title`.
- `highlight` (0/1) and `isMyClubs` are personalization flags; `isFavorited` only varies when authenticated.
- `policy.type` is usually `null` in the listing endpoint.
- The `id` field matches the `data-address-id` attribute in the website HTML.
- Pagination returns an empty `data` array when past the last page.

### GET /venues/{id}

Single venue detail. Returns the same structure as a single item from `/venues`, plus several enrichment fields:

```
GET /venues/30881
```

Extra fields observed in detail responses (not present in listings):

| Field | Type | Description |
|---|---|---|
| `bookingLimitsText` | string | Free-text German visit-limit description per tier. Common forms: `"M-Mitglieder können 4x pro Monat..."`, `"L- & XL-Mitglieder können 8 Mal pro Monat..."`, `"M, L und XL-Mitglieder können 1 Mal pro Tag..."`. May reference `pro Tag` (per day) or `pro Monat` (per month) — there's no separate structured limits field. |
| `importantInfo` | string | Venue-specific booking instructions (e.g., "Buche deinen Kurs direkt über die Urban Sports Club App!..."). |
| `description` | string | Marketing description of the venue/activity. |
| `openingHoursText` | string | Free-text opening hours — not structured. |
| `phone` | string | Contact phone (sometimes `"_"` as a placeholder). |
| `website` | string | External URL. |

The `categories[].allowed_plan_types{,_b2b}` arrays are populated for active venues (e.g. `[2, 3, 6]` for an M/L/XL venue, `[1, 2, 3, 6]` for an S/M/L/XL one).

**Note:** USC's `bookingLimitsText` is the *only* place visit-limit data is exposed in the API — there is no structured equivalent. Parsing it requires handling several variants (grouped tier prefixes, `x`/`Mal`, `pro Monat`/`im Monat`/`/Monat`, `pro Tag`, non-breaking spaces, etc.). See `backend/server.py:parse_visit_limits` for a working parser.

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
        "id": 99087547,
        "date": "2026-04-07",
        "title": "Sakralchakra-Meditation",
        "startTime": "22:30:00",
        "endTime": "22:50:00",
        "startDateTimeUTC": "2026-04-07T22:30:00+02:00",
        "endDateTimeUTC": "2026-04-07T22:50:00+02:00",
        "venue": {
          "id": 15029,
          "name": "Online - Saccidananda Yoga",
          "phone": "_",
          "website": "www.saccidananda-yoga.de",
          "allowedBusinessTypes": ["b2c", "b2b"],
          "openingHoursText": "Bitte informiere dich ...",
          "additionalInformation": "",
          "bookingLimitsText": "S-Mitglieder können bis zu 4 Mal pro Monat ...",
          "importantInfo": "Buche deinen Kurs direkt ...",
          "location": {
            "displayAddress": "Berlin - Friedrichshain",
            "city": { "id": 1, "name": "Berlin" },
            "district": { "id": 13, "name": "Friedrichshain", "area": "Berlin" },
            "latitude": 52.5119121,
            "longitude": 13.466102,
            "postalCode": "10247",
            "address": "Jungstraße 14",
            "country": { "code": "DE" },
            "policy": { "type": null }
          }
        },
        "category": { "id": 13, "name": "Meditation", "icon": "https://..." },
        "teacherName": "Tobias S.",
        "bookable": 1,
        "freeSpots": 93,
        "maximumNumber": 99,
        "minimumNumber": -1,
        "planTypes": ["S", "M", "L", "XL"],
        "planTypesB2B": ["S", "M", "L", "XL"],
        "types": ["live"],
        "serviceType": "event",
        "bookingType": "confirmation_required",
        "covers": [{ "cover150": "...", "cover311": "...", "cover720": "...", "cover1024": "..." }],
        "isOnline": 1,
        "isPlusCheckin": 0,
        "isMyClubs": false,
        "highlight": 0,
        "external": true,
        "extraPriceDescriptionText": "",
        "deleted": 0,
        "booking": null
      }
    ]
  }
}
```

**Notes:**
- City-wide search is the killer feature: `cityId=1&categoryId=7` returns all Dance classes in Berlin in a single paginated request.
- `planTypes` and `planTypesB2B` at the class level *are* populated in current responses (contrary to older observations), and may differ from venue-level tiers when an individual class is restricted.
- `venue` is embedded in each class with the same enrichment fields as `/venues/{id}` (`bookingLimitsText`, `importantInfo`, `phone`, `website`, `openingHoursText`, `allowedBusinessTypes`, `additionalInformation`) — so a city-wide course query also gets you full venue context without an extra round-trip.
- `freeSpots` / `maximumNumber` show real-time availability. `minimumNumber` can be `-1` for events without a minimum.
- `serviceType` is one of `"classes"` or `"event"`. `bookingType` is `"instant"` or `"confirmation_required"`.
- `types` is a list of class characteristics (e.g. `["live"]` for live-online, `["onsite"]` for in-person).
- `external` is `true` when the booking is handled by the venue's own system.
- `booking` is `null` when not authenticated. When logged in, shows booking status.
- The `startDate` parameter limits how far ahead you can search — max observed is ~13 days out.

### GET /categories

Full list of activity categories with sub-categories.

**Response** (real sample, ~72 top-level categories):
```json
{
  "success": "true",
  "data": [
    {
      "id": 92,
      "name": "Aerial",
      "icon": "",
      "sub-categories": [
        { "id": 345, "name": "Aerial Hoop", "icon": "", "sub-categories": [] }
      ]
    },
    {
      "id": 17,
      "name": "Aqua",
      "icon": "",
      "sub-categories": [
        { "id": 352, "name": "Aqua biking", "icon": "", "sub-categories": [] },
        { "id": 351, "name": "Aqua Gym", "icon": "", "sub-categories": [] }
      ]
    }
  ]
}
```

Notes:
- Sub-categories use the same recursive `{id, name, icon, sub-categories}` shape but typically nest only one level deep.
- The `icon` field is often an empty string in the categories endpoint (icons used elsewhere come from the venue category objects).

Selected category IDs (verified against `/categories` and venue samples):
- 1 = Fitness, 4 = Pilates, 6 = Yoga, 7 = Dance, 9 = Massage, 13 = Meditation
- 17 = Aqua, 92 = Aerial, 135 = Pole Dance, 173 = Sauna, 174 = Spa
- 232 = Bouldering, 233 = Functional Training, 253 = MMA
- 262 = Padel, 271 = Swimming, 277 = Archery, 286 = Cryotherapy

### GET /cities

All cities where USC operates (~218 entries, all countries).

**Response** (real sample):
```json
{
  "success": "true",
  "data": [
    {
      "id": 93,
      "defaultName": "Aachen",
      "lat": 50.780658,
      "lon": 6.083815,
      "countryCode": "DE",
      "venueAddressCount": 122,
      "supportEmail": "",
      "supportPhone": ""
    }
  ]
}
```

Fields:
- `id`, `defaultName`, `countryCode` — primary identifiers.
- `lat`, `lon` — geographic centroid (useful for default map zoom).
- `venueAddressCount` — total number of venue addresses USC has in that city. Useful for sorting/filtering inactive cities.
- `supportEmail`, `supportPhone` — frequently empty strings.

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

Plan types in the API are represented as integers in category-level `allowed_plan_types` and `allowed_plan_types_b2b`:

| ID | Corporate (B2B) | Private (B2C) |
|----|-----------------|---------------|
| 1  | S               | Essential     |
| 2  | M               | Classic       |
| 3  | L               | Premium       |
| 6  | XL              | Max           |

Verified against live samples — e.g. a venue with `planTypesB2B: ["S","M","L","XL"]` has `allowed_plan_types_b2b: [1, 2, 3, 6]`, and an `["L","XL"]`-only venue has `[3, 6]`. The IDs `0`, `4`, `5` do not appear in current responses.

At the venue level, `planTypes` and `planTypesB2B` use the string names (`"S"`, `"M"`, `"L"`, `"XL"`) instead of IDs. The string letters are the same for both private and corporate, even though they map to different display names — the distinction is purely which array (`planTypes` vs `planTypesB2B`) the value lives in.

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
