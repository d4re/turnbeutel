# USC Berlin Venue Explorer

An interactive map app that shows all Urban Sports Club venues in Berlin, filterable by membership type (Corporate / Private) and tier level. Built to answer: *"What venues would I gain or lose by changing my membership tier?"*

## Quick Start

### 1. Set up the environment

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
source .venv/Scripts/activate    # Windows (Git Bash)
pip install -r scraper/requirements.txt
```

### 2. Scrape venue data

```bash
# Step 1: Scrape all venue listings (~2 min)
python scraper/scrape_listings.py

# Step 2: Scrape individual venue pages for coordinates + visit limits (~30 min)
python scraper/scrape_details.py --all

# Step 3: Merge into final JSON
python scraper/merge_data.py
```

### 3. Run the frontend

```bash
python -m http.server 8080
```

Open `http://localhost:8080/frontend/index.html` in your browser.

## How to Use

- **Corporate / Private toggle** — switches between corporate tier names (S, M Pro, L Pro, XL Pro) and private ones (Essential, Classic, Premium, Max). Pick whichever matches your membership.
- **Tier range slider** — has two handles. Drag both to the same tier to see only that tier's exclusive venues. Example: both handles on "L Pro" shows the ~340 venues you'd gain by upgrading from M Pro.
- **District / Activity / Search** — narrow down further.
- **Map markers** are color-coded by minimum required tier (green → red).
- **Click a venue** on the map or in the list to see visit limits per tier.

## Project Structure

```
scraper/
  scrape_listings.py    # Paginate through listing pages, extract venue cards
  scrape_details.py     # Fetch individual venue pages for coords + visit limits
  merge_data.py         # Combine listing + detail data into venues_final.json
  requirements.txt      # Python dependencies

frontend/
  index.html            # Single-page app
  app.js                # Map, filters, rendering logic
  style.css             # Styling

data/                   # Generated (gitignored)
  venues_listing.json   # Raw listing scrape output
  venues_details.json   # Raw detail scrape output
  venues_final.json     # Merged final dataset used by the frontend
```

## Re-scraping

Venue data changes over time. To refresh, re-run the three scraper steps. The detail scrape supports incremental runs (without `--all` it only scrapes non-Classic venues, ~3-4 min), but for full accuracy use `--all`.
