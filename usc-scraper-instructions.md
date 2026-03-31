# USC Venue Scraper: M Pro vs L Pro Diff for Berlin

## Goal

Scrape all Urban Sports Club venues in Berlin, extract which membership tiers each venue supports, and produce a list of venues that are **only available on L Pro (Premium) but not M Pro (Classic)** — i.e. the venues I'd gain access to by upgrading.

## Context

- I currently have an **M Pro** corporate membership (equivalent to "Classic" in private tier naming)
- I want to see what **L Pro** ("Premium") adds
- The USC website uses two naming conventions:
  - **Corporate**: S, M, L, XL (with "Pro" or "Business" suffix)  
  - **Private**: Essential, Classic, Premium, Max
- On venue pages, tiers are shown as badges like `Classic Premium Max` or `Premium Max` or `L XL` depending on whether you're viewing as private or corporate member

## Website Structure

### Venue listing pages
- Base URL: `https://urbansportsclub.com/en/venues/berlin/berlin`
- District-filtered: `https://urbansportsclub.com/en/venues/berlin/berlin-friedrichshain`
- The page has a toggle: "Private Members" vs "Corporate Members"
- It has plan filter checkboxes (Essential/Classic/Premium/Max for private, or S/M/L/XL for corporate)
- **Critical**: Venues load dynamically via JavaScript — there's a "Show more" button that loads additional venues. A simple HTTP fetch only gets the first ~30 venues.

### Individual venue pages
- URL pattern: `https://urbansportsclub.com/en/venues/{venue-slug}`
- Each venue page shows visit limits per tier, e.g.:
  - "M-Mitglieder können 4x pro Monat..." 
  - "L-Mitglieder können 8x pro Monat..."
- Some venues show "PLUS" badge (these are premium activities: EMS, massage, cryo, etc.)
- The tier availability is visible in the HTML

### What I observed from partial scraping
- Most venues are `Classic Premium Max` (= M L XL) — available to all from M up
- Some venues are **`Premium Max` only** (= L XL only) — these are the upgrade-worthy ones
- Examples found: Fenriz Gym (Kreuzberg), Black Sheep Athletics (Kreuzberg), Slim-Gym Exclusive (Mitte), Auszeit - die Wellnessmassagen (Friedrichshain)

## Recommended Approach

### Option A: Browser automation with Playwright (preferred)
Use Playwright (Python or Node) to:
1. Navigate to `https://urbansportsclub.com/en/venues/berlin/berlin`
2. Select "Corporate Members" toggle (to see M/L/XL tier labels)  
3. Keep clicking "Show more" until all venues are loaded
4. Extract from each venue card: name, district/address, activities, tier badges (S/M/L/XL)
5. Alternatively, iterate through Berlin districts one by one (Friedrichshain, Kreuzberg, Mitte, etc.) to get smaller batches — the district filter URLs are like `/berlin/berlin-friedrichshain`

### Option B: Look for an API
- Check the browser's Network tab equivalent — the "Show more" button likely calls an API endpoint that returns venue JSON. If you can find this API, you can paginate through it directly without browser automation. Inspect the page source or JS bundles for API endpoints.
- The initial page HTML contains a long list of venue names in the source (I saw the full list in the fetched HTML). Check if the full venue list with tier info is embedded in the page source or in a JS data blob.

### Data extraction
For each venue, capture:
- **name**: venue name
- **slug/url**: the venue URL slug
- **district**: neighborhood (e.g., Friedrichshain, Kreuzberg)
- **address**: street address
- **activities**: list of sports/activities offered
- **tiers**: which membership tiers can access it (e.g., ["M", "L", "XL"] or ["L", "XL"])
- **is_plus**: whether it has a "PLUS" badge (indicates premium activities)

### Output
1. A JSON file with ALL Berlin venues and their tier access
2. A filtered report (markdown or CSV) showing **only venues where tier includes L but NOT M** — these are the venues gained by upgrading from M Pro to L Pro
3. Bonus: also show venues where L Pro gets **more visits per month** than M Pro (e.g., 8x vs 4x) — this requires checking individual venue pages

## Important Notes

- The full venue list IS in the initial HTML source — I saw hundreds of venue names when I fetched the page. The tier badges may also be in the source. **Check the raw HTML carefully before resorting to Playwright** — you might be able to parse it all from the static HTML.
- The HTML I fetched showed venues with tier badges like `Classic Premium Max` next to each venue card. So the tier info IS in the HTML, but only for the initially loaded venues (~30). The rest need "Show more" clicks or API calls.
- Berlin districts available as filters (from the HTML): Friedrichshain, Kreuzberg, Mitte, Prenzlauer Berg, Schöneberg, Charlottenburg, Neukölln, Wedding, Moabit, Tempelhof, Steglitz, Lichtenberg, Pankow, Wilmersdorf, Köpenick, Spandau, Reinickendorf, Treptow, Marzahn, Hellersdorf, Zehlendorf, etc.
- Be respectful with rate limiting — add small delays between requests
- The page language can be toggled (en/de) — use English for consistency

## Quick Start

```bash
# If going the Playwright route:
pip install playwright --break-system-packages
playwright install chromium

# If going the requests + BeautifulSoup route:
pip install requests beautifulsoup4 --break-system-packages
```

Start by fetching the raw HTML of the venues page and examining the full source to understand the data structure before deciding on the approach.
