"""
Scrape individual venue pages for coordinates, visit limits, and ratings.
By default, only scrapes venues not available on Classic tier (the "upgrade" venues).
Pass --all to scrape all venues.
Output: data/venues_details.json
"""

import json
import os
import sys
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def parse_venue_detail(html, slug):
    """Extract detailed info from an individual venue page."""
    soup = BeautifulSoup(html, "lxml")
    detail = {"slug": slug}

    # JSON-LD for coordinates, address, etc.
    jsonld_tag = soup.find("script", type="application/ld+json")
    if jsonld_tag and jsonld_tag.string:
        try:
            ld = json.loads(jsonld_tag.string)
            geo = ld.get("geo", {})
            detail["lat"] = float(geo.get("latitude", 0)) or None
            detail["lng"] = float(geo.get("longitude", 0)) or None

            addr = ld.get("address", {})
            detail["address"] = {
                "street": addr.get("streetAddress", ""),
                "postal_code": addr.get("postalCode", ""),
                "city": addr.get("addressLocality", ""),
            }
            detail["description"] = ld.get("description", "")
            detail["image_url"] = ld.get("image", "")
            detail["telephone"] = ld.get("telephone", "")
        except (json.JSONDecodeError, ValueError):
            pass

    # Visit limits - parse both private and corporate tabs
    visit_limits = {"private": {}, "corporate": {}}

    panels = soup.find_all("div", role="tabpanel")
    for panel in panels:
        panel_id = panel.get("id", "")
        if "private" in panel_id:
            tab_key = "private"
        elif "corporate" in panel_id:
            tab_key = "corporate"
        else:
            continue

        rows = panel.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) == 2:
                tier_name = cells[0].get_text(strip=True)
                limit_text = cells[1].get_text(strip=True)
                is_inactive = "inactive" in (row.get("class", []) or [])

                if is_inactive or "not included" in limit_text.lower():
                    visit_limits[tab_key][tier_name] = None
                else:
                    # Parse "8 / month" -> 8
                    try:
                        visits = int(limit_text.split("/")[0].strip())
                    except ValueError:
                        visits = limit_text
                    visit_limits[tab_key][tier_name] = visits

    detail["visit_limits"] = visit_limits

    # Rating from JSON-LD
    if jsonld_tag and jsonld_tag.string:
        try:
            ld = json.loads(jsonld_tag.string)
            agg = ld.get("aggregateRating", {})
            if agg:
                detail["rating"] = float(agg.get("ratingValue", 0)) or None
                detail["review_count"] = int(agg.get("reviewCount", 0)) or None
        except (json.JSONDecodeError, ValueError):
            pass

    return detail


def scrape_venue_details(venues, scrape_all=False):
    """Scrape detail pages for selected venues."""
    if scrape_all:
        to_scrape = venues
        print(f"Scraping ALL {len(to_scrape)} venues...")
    else:
        # Only scrape venues not available on Classic (upgrade venues)
        to_scrape = [v for v in venues if "Classic" not in v.get("tiers", [])]
        print(f"Scraping {len(to_scrape)} non-Classic venues (upgrade venues)...")

    details = []
    errors = []

    for i, venue in enumerate(to_scrape):
        slug = venue["slug"]
        url = venue["url"]
        safe_name = venue['name'].encode('ascii', 'replace').decode()
        print(f"  [{i + 1}/{len(to_scrape)}] {safe_name}... ", end="", flush=True)

        try:
            resp = requests.get(url, headers=HEADERS)
            resp.raise_for_status()
            detail = parse_venue_detail(resp.text, slug)
            details.append(detail)
            has_coords = bool(detail.get("lat") and detail.get("lng"))
            print(f"OK (coords: {'yes' if has_coords else 'no'})")
        except Exception as e:
            print(f"ERROR: {e}")
            errors.append({"slug": slug, "error": str(e)})

        time.sleep(1.0)

    return details, errors


def main():
    scrape_all = "--all" in sys.argv

    listing_path = os.path.join(DATA_DIR, "venues_listing.json")
    if not os.path.exists(listing_path):
        print(f"Error: {listing_path} not found. Run scrape_listings.py first.")
        sys.exit(1)

    with open(listing_path, "r", encoding="utf-8") as f:
        listing_data = json.load(f)

    venues = listing_data["venues"]
    print(f"Loaded {len(venues)} venues from listing data.")

    details, errors = scrape_venue_details(venues, scrape_all=scrape_all)

    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_scraped": len(details),
        "errors": len(errors),
        "details": details,
        "error_list": errors,
    }

    output_path = os.path.join(DATA_DIR, "venues_details.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(details)} venue details to {output_path}")
    if errors:
        print(f"Errors: {len(errors)}")
        for e in errors:
            print(f"  - {e['slug']}: {e['error']}")


if __name__ == "__main__":
    main()
