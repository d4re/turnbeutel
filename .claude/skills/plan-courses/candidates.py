"""Candidate courses for a /plan-courses run, from a Turnbeutel instance's course cache.

Downloads /api/courses (plus /api/venues for tiers and visit limits), joins
them, applies the hard filters, saves the full joined list as JSON, and prints
either per-day category counts (default) or a compact table (--list).
Stdlib only; run with `python3 -I`.

Examples:
  python3 -I candidates.py --out /tmp/c.json                    # counts per category and day
  python3 -I candidates.py --out /tmp/c.json --list --category Dance --grep 'salsa|tango'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

CORPORATE = ["S", "M", "L", "XL"]
PRIVATE = ["Essential", "Classic", "Premium", "Max"]


def get_json(base: str, path: str, params: list[tuple[str, str]]) -> dict:
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("TURNBEUTEL_URL", "http://localhost:8000"))
    ap.add_argument("--from", dest="start", default=(date.today() + timedelta(days=1)).isoformat())
    ap.add_argument("--days", type=int, default=13, help="1-14 (the course cache covers today..today+13)")
    ap.add_argument("--city-id", type=int, action="append", help="repeatable; default 1 (Berlin)")
    ap.add_argument("--membership", choices=["corporate", "private"], default="corporate")
    ap.add_argument("--tier", default="M", help="user's tier, e.g. M (corporate) or Classic (private)")
    ap.add_argument("--exclude", help="case-insensitive regex on title; matching courses are dropped")
    ap.add_argument("--all", action="store_true", help="keep online, full, Plus and above-tier courses")
    ap.add_argument("--out", help="write the joined, filtered candidates as JSON here")
    ap.add_argument("--list", action="store_true", help="print a table instead of category counts")
    ap.add_argument("--category", action="append", help="--list only: keep these categories (repeatable)")
    ap.add_argument("--grep", help="--list only: case-insensitive regex on title, venue or category")
    ap.add_argument("--max-rows", type=int, default=300)
    args = ap.parse_args()

    cities = args.city_id or [1]
    order = CORPORATE if args.membership == "corporate" else PRIVATE
    if args.tier not in order:
        ap.error(f"--tier must be one of {order}")

    city_params = [("city_ids", str(c)) for c in cities]
    courses_payload = get_json(
        args.url, "/api/courses", [("start_date", args.start), ("days", str(args.days)), *city_params]
    )
    venues_payload = get_json(args.url, "/api/venues", city_params)
    for err in courses_payload.get("errors", []):
        print(f"warning: courses for {err['date']} (city {err.get('city_id')}) failed: {err['reason']}", file=sys.stderr)

    venues = {}
    for city in venues_payload["cities"]:
        for v in city["venues"]:
            venues[str(v["address_id"])] = v

    exclude = re.compile(args.exclude, re.I) if args.exclude else None
    candidates = []
    for city in courses_payload["cities"]:
        for c in city["courses"]:
            v = venues.get(c["venue_id"])
            tiers = (v or {}).get(f"tiers_{args.membership}") or []
            limits = ((v or {}).get("visit_limits") or {}).get(args.membership) or {}
            row = {
                **c,
                "city_id": city["city_id"],
                "venue_tiers": tiers,
                "in_tier": args.tier in tiers if v else None,
                "monthly_limit": limits.get(args.tier),
                "venue_rating": (v or {}).get("rating"),
                "venue_url": (v or {}).get("url"),
                "full": c["free_spots"] == 0,
            }
            if exclude and exclude.search(c["title"]):
                continue
            if not args.all and (c["is_online"] or c["is_plus"] or row["full"] or row["in_tier"] is False):
                continue
            candidates.append(row)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(candidates, f, ensure_ascii=False)

    if not args.list:
        per_day = defaultdict(Counter)
        for c in candidates:
            per_day[c["date"]][c["category"]] += 1
        totals = Counter(c["category"] for c in candidates)
        print(f"{len(candidates)} candidates, {courses_payload['date_from']}..{courses_payload['date_to']}")
        print("categories (total):", ", ".join(f"{k} {n}" for k, n in totals.most_common()))
        for d in sorted(per_day):
            print(f"{d}: {sum(per_day[d].values())} courses")
        return 0

    grep = re.compile(args.grep, re.I) if args.grep else None
    rows = [
        c
        for c in candidates
        if (not args.category or c["category"] in args.category)
        and (not grep or grep.search(f"{c['title']} {c['venue_name']} {c['category']}"))
    ]
    rows.sort(key=lambda c: (c["date"], c["start_time"]))
    last_date = None
    for c in rows[: args.max_rows]:
        if c["date"] != last_date:
            last_date = c["date"]
            print(f"\n## {c['date']}")
        spots = "?" if c["free_spots"] is None else f"{c['free_spots']}/{c['max_spots']}"
        flags = []
        if c["is_plus"]:
            flags.append("PLUS")
        if c["in_tier"] is False:
            flags.append("above-tier")
        if c["monthly_limit"] is not None and c["monthly_limit"] < 30:
            flags.append(f"{c['monthly_limit']}/mo")
        rating = f"★{c['venue_rating']}" if c["venue_rating"] else ""
        print(
            f"{c['id']} | {c['start_time']}-{c['end_time']} | {c['title']} | {c['venue_name']} "
            f"(venue {c['venue_id']}) | {c['district']} | {c['category']} | {spots} {rating} {' '.join(flags)}".rstrip()
        )
    if len(rows) > args.max_rows:
        print(f"\n… {len(rows) - args.max_rows} more rows; narrow with --category/--grep or raise --max-rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
