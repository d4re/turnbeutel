# Agent Guide: USC Venue Explorer

Design decisions and things to know when working on this project.

## Architecture Overview

This is a two-part app: a Python scraper pipeline that produces a static JSON file, and a vanilla JS frontend that reads it. There is no backend server — the frontend loads `data/venues_final.json` directly.

```
USC Website  →  scrape_listings.py  →  venues_listing.json  ─┐
                scrape_details.py   →  venues_details.json   ─┤→  merge_data.py  →  venues_final.json  →  frontend
                                                               └──────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Private vs Corporate tiers are NOT interchangeable

The USC website has two parallel tier systems:

| Corporate | Private   |
|-----------|-----------|
| S         | Essential |
| M (Pro)   | Classic   |
| L (Pro)   | Premium   |
| XL (Pro)  | Max       |

**These do NOT always map 1:1.** We verified that out of 1,931 venues:
- 3 venues have inclusion mismatches (e.g., Fenriz Gym is excluded from Classic/private but included in M/corporate)
- 21 venues have different visit counts between the "equivalent" tiers

This is why the app stores **both** `tiers_private` and `tiers_corporate` per venue, computed from the actual visit limit tables on each venue's detail page — not from a naive name mapping.

### Listing tiers are private-only, detail tiers are authoritative

The listing page (`/en/venues/berlin/berlin?page=N`) only shows private tier badges (Essential/Classic/Premium/Max). The individual venue pages have a tabbed visit-limit table with both private and corporate data. The merge script uses detail-page data as the source of truth and only falls back to listing-page tiers when detail data is missing.

### No API — HTML scraping only

USC has no public API. Pagination works via `?page=N` query parameter on the listing URL (returns ~30 venues per page). Individual venue pages embed JSON-LD (`LocalBusiness` schema) with coordinates and address, and have visit-limit tables in the HTML.

### Frontend is intentionally zero-build

Vanilla HTML + JS + CSS. Leaflet.js and MarkerCluster loaded from CDN. No npm, no bundler, no framework. Serve with any static file server.

## Things to Watch Out For

### HTML structure is fragile

The scraper relies on specific CSS classes from the USC website:
- `smm-studio-snippet` for venue cards
- `smm-studio-snippet__studio-plan` for tier badges
- `smm-studio-snippet__studio-link` for venue name/link
- `div[role="tabpanel"]` with `private`/`corporate` in the ID for visit limits
- `script[type="application/ld+json"]` for JSON-LD coordinates

If USC redesigns their frontend, these selectors will break. The JSON-LD schema is the most stable part.

### Rate limiting

The scraper uses 1–1.5 second delays between requests. The full detail scrape (1,931 pages) takes ~30 minutes. Don't reduce the delay — USC could start blocking.

### Coordinate gaps

~13 out of 1,931 venues had no coordinates in their JSON-LD. These show in the venue list but not on the map. The `has_coordinates` field tracks this.

### The slider filters on `min_tier`

The dual-handle tier slider filters venues by their **minimum required tier** (the lowest tier that grants access). A venue available on "M, L, XL" has `min_tier_corporate = "M"`. Setting both slider handles to "L" means: show venues where the minimum tier is exactly L (i.e., L-exclusive venues not available on M or S).

## Data Schema

Each venue in `venues_final.json` has:

```json
{
  "name": "Fenriz Gym",
  "slug": "fenriz-trainingszentrum",
  "url": "https://urbansportsclub.com/en/venues/fenriz-trainingszentrum",
  "tiers_private": ["Premium", "Max"],
  "tiers_corporate": ["M", "L", "XL"],
  "min_tier_private": "Premium",
  "min_tier_corporate": "M",
  "activities": ["Fitness", "Mixed Martial Arts"],
  "district": "Kreuzberg",
  "street": "Lobeckstr. 36",
  "is_plus": false,
  "lat": 52.5036,
  "lng": 13.4078,
  "has_coordinates": true,
  "visit_limits": {
    "private": { "Essential": null, "Classic": null, "Premium": 8, "Max": 8 },
    "corporate": { "S": null, "M": 4, "L": 8, "XL": 8 }
  },
  "rating": 4.9,
  "review_count": 3874
}
```

Note how Fenriz has `min_tier_private = "Premium"` but `min_tier_corporate = "M"` — the two systems genuinely differ.

## Common Tasks

### Refresh the data
```bash
python scraper/scrape_listings.py
python scraper/scrape_details.py --all
python scraper/merge_data.py
```

### Quick refresh (upgrade venues only)
```bash
python scraper/scrape_details.py   # without --all, only scrapes non-Classic venues
python scraper/merge_data.py
```

### Add a new filter to the frontend
1. Add the UI element in `index.html` inside `#filters`
2. Bind the change event in `populateFilters()` in `app.js`
3. Add the filter logic in `applyFilters()`
4. The venue data schema is in `venues_final.json` — check what fields are available

### Change the city
The scraper is hardcoded to Berlin. To change:
1. Update `BASE_URL` in `scrape_listings.py` (e.g., `/en/venues/munich/munich`)
2. Update the map center coordinates in `app.js` (`setView([lat, lng], zoom)`)
