"""
Scrape all Urban Sports Club venue listings for Berlin.
Paginates through all pages and extracts venue cards with tier info.
Output: data/venues_listing.json
"""

import json
import os
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

BASE_URL = "https://urbansportsclub.com/en/venues/berlin/berlin"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def parse_venue_card(card):
    """Extract venue data from a single venue card element."""
    # Name and slug
    link = card.find("a", class_="smm-studio-snippet__studio-link")
    if not link:
        return None
    name = link.get_text(strip=True)
    href = link.get("href", "")
    slug = href.rstrip("/").split("/")[-1] if href else ""

    # Tiers
    plan_spans = card.find_all("span", class_="smm-studio-snippet__studio-plan")
    tiers = [span.get_text(strip=True) for span in plan_spans]

    # Activities
    disciplines_div = card.find("div", class_="disciplines")
    activities = []
    if disciplines_div:
        raw = disciplines_div.get_text(strip=True)
        activities = [a.strip() for a in raw.replace("·", ",").split(",") if a.strip()]

    # Address and district
    address_p = card.find("p", class_="smm-studio-snippet__address")
    district = ""
    street = ""
    if address_p:
        # District is the text before the street span
        street_span = address_p.find("span", class_="smm-studio-snippet__address-street")
        if street_span:
            street = street_span.get_text(strip=True)
        # District is the direct text of the <p> minus the street
        full_text = address_p.get_text(strip=True)
        if street:
            district = full_text.replace(street, "").strip().rstrip(",").strip()
        else:
            district = full_text.strip()

    # PLUS badge
    details_link = card.find("a", class_="smm-studio-snippet__image-link")
    is_plus = False
    if details_link:
        status_labels = details_link.find_all("span", class_="label")
        for label in status_labels:
            if "plus" in label.get_text(strip=True).lower():
                is_plus = True
                break
        # Also check the "More details" text
        details_text = details_link.get_text(strip=True)
        if "PLUS" in details_text:
            is_plus = True

    # Address ID (useful for dedup)
    address_id = card.get("data-address-id", "")

    return {
        "name": name,
        "slug": slug,
        "url": f"https://urbansportsclub.com{href}",
        "tiers": tiers,
        "activities": activities,
        "district": district,
        "street": street,
        "is_plus": is_plus,
        "address_id": address_id,
    }


def scrape_all_listings():
    """Scrape all pages of Berlin venue listings."""
    all_venues = []
    seen_slugs = set()
    page = 1

    while True:
        url = f"{BASE_URL}?page={page}"
        print(f"Fetching page {page}... ", end="", flush=True)

        resp = requests.get(url, headers=HEADERS)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.find_all("div", class_="smm-studio-snippet")

        if not cards:
            print("no cards found, done.")
            break

        new_count = 0
        for card in cards:
            venue = parse_venue_card(card)
            if venue and venue["slug"] not in seen_slugs:
                seen_slugs.add(venue["slug"])
                all_venues.append(venue)
                new_count += 1

        print(f"{len(cards)} cards, {new_count} new venues (total: {len(all_venues)})")

        if len(cards) < 32:
            # Last page
            break

        page += 1
        time.sleep(1.5)

    return all_venues


def compute_min_tier(tiers):
    """Determine the minimum tier required for access."""
    tier_order = ["Essential", "Classic", "Premium", "Max"]
    for t in tier_order:
        if t in tiers:
            return t
    return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"Starting USC Berlin venue listing scrape...")
    print(f"Base URL: {BASE_URL}")
    print()

    venues = scrape_all_listings()

    # Add computed fields
    for v in venues:
        v["min_tier"] = compute_min_tier(v["tiers"])

    # Stats
    tier_counts = {}
    for v in venues:
        key = " + ".join(v["tiers"]) if v["tiers"] else "None"
        tier_counts[key] = tier_counts.get(key, 0) + 1

    print(f"\n=== Results ===")
    print(f"Total venues: {len(venues)}")
    print(f"\nTier combinations:")
    for combo, count in sorted(tier_counts.items(), key=lambda x: -x[1]):
        print(f"  {combo}: {count}")

    # Count upgrade-worthy venues
    l_only = [v for v in venues if "Premium" in v["tiers"] and "Classic" not in v["tiers"]]
    print(f"\nL Pro only (Premium but not Classic): {len(l_only)}")
    for v in l_only[:10]:
        print(f"  - {v['name']} ({v['district']})")
    if len(l_only) > 10:
        print(f"  ... and {len(l_only) - 10} more")

    # Save
    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_venues": len(venues),
        "venues": venues,
    }

    output_path = os.path.join(DATA_DIR, "venues_listing.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nSaved to {output_path}")


if __name__ == "__main__":
    main()
