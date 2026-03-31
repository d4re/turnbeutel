"""
Merge listing data with detail data to produce the final venues JSON.
Output: data/venues_final.json
"""

import json
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

TIER_ORDER = ["Essential", "Classic", "Premium", "Max"]
CORPORATE_TO_PRIVATE = {"S": "Essential", "M": "Classic", "L": "Premium", "XL": "Max"}


def main():
    listing_path = os.path.join(DATA_DIR, "venues_listing.json")
    details_path = os.path.join(DATA_DIR, "venues_details.json")

    with open(listing_path, "r", encoding="utf-8") as f:
        listing_data = json.load(f)

    details_by_slug = {}
    if os.path.exists(details_path):
        with open(details_path, "r", encoding="utf-8") as f:
            details_data = json.load(f)
        for d in details_data["details"]:
            details_by_slug[d["slug"]] = d
        print(f"Loaded {len(details_by_slug)} venue details.")
    else:
        print("No details file found, proceeding with listing data only.")

    venues = []
    for v in listing_data["venues"]:
        slug = v["slug"]
        detail = details_by_slug.get(slug, {})

        venue = {
            "name": v["name"],
            "slug": slug,
            "url": v["url"],
            "tiers": v["tiers"],
            "min_tier": v.get("min_tier"),
            "activities": v["activities"],
            "district": v["district"],
            "street": v["street"],
            "is_plus": v["is_plus"],
            "address_id": v["address_id"],
            # From detail scrape
            "lat": detail.get("lat"),
            "lng": detail.get("lng"),
            "address": detail.get("address"),
            "rating": detail.get("rating"),
            "review_count": detail.get("review_count"),
            "visit_limits": detail.get("visit_limits"),
            "description": detail.get("description"),
            "image_url": detail.get("image_url"),
            "has_coordinates": bool(detail.get("lat") and detail.get("lng")),
        }
        venues.append(venue)

    # Sort: Premium-only first, then by name
    def sort_key(v):
        is_upgrade = "Premium" in v["tiers"] and "Classic" not in v["tiers"]
        return (0 if is_upgrade else 1, v["name"])

    venues.sort(key=sort_key)

    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_venues": len(venues),
        "venues_with_coords": sum(1 for v in venues if v["has_coordinates"]),
        "tier_mapping": {
            "corporate_to_private": CORPORATE_TO_PRIVATE,
            "tier_order": TIER_ORDER,
        },
        "venues": venues,
    }

    output_path = os.path.join(DATA_DIR, "venues_final.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Stats
    with_coords = sum(1 for v in venues if v["has_coordinates"])
    upgrade = [v for v in venues if "Premium" in v["tiers"] and "Classic" not in v["tiers"]]
    print(f"\n=== Final Data ===")
    print(f"Total venues: {len(venues)}")
    print(f"With coordinates: {with_coords}")
    print(f"Upgrade venues (L Pro only): {len(upgrade)}")
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()
