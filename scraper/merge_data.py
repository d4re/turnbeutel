"""
Merge listing data with detail data to produce the final venues JSON.
Output: data/venues_final.json
"""

import json
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

PRIVATE_TIER_ORDER = ["Essential", "Classic", "Premium", "Max"]
CORPORATE_TIER_ORDER = ["S", "M", "L", "XL"]
CORPORATE_TO_PRIVATE = {"S": "Essential", "M": "Classic", "L": "Premium", "XL": "Max"}
PRIVATE_TO_CORPORATE = {v: k for k, v in CORPORATE_TO_PRIVATE.items()}


def tiers_from_visit_limits(limits, tier_order):
    """Return list of tier names that have non-null visit limits."""
    if not limits:
        return []
    return [t for t in tier_order if limits.get(t) is not None]


def min_tier(tiers, tier_order):
    """Return the lowest tier in the list according to tier_order."""
    for t in tier_order:
        if t in tiers:
            return t
    return None


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
        visit_limits = detail.get("visit_limits", {})

        # Compute tier availability from actual visit limit data
        tiers_private = tiers_from_visit_limits(
            visit_limits.get("private", {}), PRIVATE_TIER_ORDER
        )
        tiers_corporate = tiers_from_visit_limits(
            visit_limits.get("corporate", {}), CORPORATE_TIER_ORDER
        )

        # Fallback to listing-page tiers if no detail data
        if not tiers_private:
            tiers_private = v.get("tiers", [])
        if not tiers_corporate:
            # Map from private listing tiers as fallback
            tiers_corporate = [
                PRIVATE_TO_CORPORATE[t] for t in v.get("tiers", [])
                if t in PRIVATE_TO_CORPORATE
            ]

        venue = {
            "name": v["name"],
            "slug": slug,
            "url": v["url"],
            "tiers_private": tiers_private,
            "tiers_corporate": tiers_corporate,
            "min_tier_private": min_tier(tiers_private, PRIVATE_TIER_ORDER),
            "min_tier_corporate": min_tier(tiers_corporate, CORPORATE_TIER_ORDER),
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
            "visit_limits": visit_limits if visit_limits else None,
            "description": detail.get("description"),
            "image_url": detail.get("image_url"),
            "has_coordinates": bool(detail.get("lat") and detail.get("lng")),
        }
        venues.append(venue)

    # Sort by name
    venues.sort(key=lambda v: v["name"])

    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_venues": len(venues),
        "venues_with_coords": sum(1 for v in venues if v["has_coordinates"]),
        "tier_config": {
            "private": {
                "order": PRIVATE_TIER_ORDER,
                "colors": {"Essential": "#27ae60", "Classic": "#2980b9", "Premium": "#e67e22", "Max": "#c0392b"},
            },
            "corporate": {
                "order": CORPORATE_TIER_ORDER,
                "display": {"S": "S", "M": "M Pro", "L": "L Pro", "XL": "XL Pro"},
                "colors": {"S": "#27ae60", "M": "#2980b9", "L": "#e67e22", "XL": "#c0392b"},
            },
        },
        "venues": venues,
    }

    output_path = os.path.join(DATA_DIR, "venues_final.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Stats
    with_coords = sum(1 for v in venues if v["has_coordinates"])
    with_detail = sum(1 for v in venues if v["visit_limits"])
    print(f"\n=== Final Data ===")
    print(f"Total venues: {len(venues)}")
    print(f"With coordinates: {with_coords}")
    print(f"With detail data: {with_detail}")
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()
