"""Candidate courses for a /plan-courses run, from a Turnbeutel instance's course cache.

Downloads /api/courses (plus /api/venues for visit limits, /api/settings for
the user's plan, and with --travel /api/travel for travel times from home),
joins them, applies the hard filters, saves the full joined list as JSON, and
prints either per-day category counts (default) or a compact table (--list).
Stdlib only; run with `python3 -I`.

Examples:
  python3 -I candidates.py --out /tmp/c.json                    # counts per category and day
  python3 -I candidates.py --out /tmp/c.json --list --travel --category Dance --grep 'salsa|tango'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

CORPORATE = ["S", "M", "L", "XL"]
PRIVATE = ["Essential", "Classic", "Premium", "Max"]


def get_json(base: str, path: str, params: list[tuple[str, str]] | None = None) -> dict:
    url = f"{base.rstrip('/')}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.load(r)


def post_json(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def fetch_travel(base: str, venue_ids: list[str], wait_s: float) -> dict[str, dict]:
    """Travel from the stored home per venue; re-polls pending ids for up to `wait_s` seconds."""
    results: dict[str, dict] = {}
    todo = list(dict.fromkeys(venue_ids))
    deadline = time.monotonic() + wait_s
    while todo:
        pending: list[str] = []
        for i in range(0, len(todo), 500):
            data = post_json(base, "/api/travel", {"venue_ids": todo[i : i + 500]})
            results.update(data["results"])
            pending += data["pending"]
        if not pending or time.monotonic() > deadline:
            if pending:
                print(f"warning: travel still computing for {len(pending)} venues", file=sys.stderr)
            break
        todo = pending
        time.sleep(3)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("TURNBEUTEL_URL", "http://localhost:8000"))
    ap.add_argument("--from", dest="start", default=(date.today() + timedelta(days=1)).isoformat())
    ap.add_argument("--days", type=int, default=13, help="1-14 (the course cache covers today..today+13)")
    ap.add_argument("--city-id", type=int, action="append", help="repeatable; default 1 (Berlin)")
    ap.add_argument("--membership", choices=["corporate", "private"], help="default: the plan in /api/settings")
    ap.add_argument("--tier", help="user's tier, e.g. M or Classic; default: the plan in /api/settings")
    ap.add_argument("--exclude", help="case-insensitive regex on title; matching courses are dropped")
    ap.add_argument("--all", action="store_true", help="keep online, full, Plus and above-tier courses")
    ap.add_argument("--out", help="write the joined, filtered candidates as JSON here")
    ap.add_argument("--list", action="store_true", help="print a table instead of category counts")
    ap.add_argument("--category", action="append", help="--list only: keep these categories (repeatable)")
    ap.add_argument("--grep", help="--list only: case-insensitive regex on title, venue or category")
    ap.add_argument("--max-rows", type=int, default=300)
    ap.add_argument(
        "--travel", action="store_true", help="add travel minutes from the stored home (with --list: listed rows only)"
    )
    ap.add_argument("--travel-wait", type=float, default=60, help="seconds to wait for travel still computing")
    args = ap.parse_args()

    cities = args.city_id or [1]
    settings = get_json(args.url, "/api/settings")
    plan = settings.get("plan") or {}
    args.membership = args.membership or plan.get("type")
    args.tier = args.tier or plan.get("tier")
    if not args.membership or not args.tier:
        ap.error("no plan in /api/settings: set it in the app's You panel or pass --membership and --tier")
    order = CORPORATE if args.membership == "corporate" else PRIVATE
    if args.tier not in order:
        ap.error(f"--tier must be one of {order}")
    if args.travel and not settings.get("home"):
        ap.error("--travel needs a home: set it in the app's You panel")
    bike_kmh = settings.get("bike_kmh") or 18

    city_params = [("city_ids", str(c)) for c in cities]
    courses_payload = get_json(
        args.url, "/api/courses", [("start_date", args.start), ("days", str(args.days)), *city_params]
    )
    venues_payload = get_json(args.url, "/api/venues", city_params)
    for err in courses_payload.get("errors", []):
        print(
            f"warning: courses for {err['date']} (city {err.get('city_id')}) failed: {err['reason']}", file=sys.stderr
        )

    venues = {}
    for city in venues_payload["cities"]:
        for v in city["venues"]:
            venues[str(v["address_id"])] = v

    exclude = re.compile(args.exclude, re.I) if args.exclude else None
    candidates = []
    for city in courses_payload["cities"]:
        for c in city["courses"]:
            v = venues.get(c["venue_id"])
            # Class-level tiers can be stricter than the venue's (e.g. intro aerial classes).
            tiers = c.get(f"tiers_{args.membership}") or (v or {}).get(f"tiers_{args.membership}") or []
            limits = ((v or {}).get("visit_limits") or {}).get(args.membership) or {}
            row = {
                **c,
                "city_id": city["city_id"],
                "class_tiers": tiers,
                "in_tier": args.tier in tiers if tiers else None,
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

    def add_travel(rows: list[dict]) -> None:
        travel = fetch_travel(args.url, [c["venue_id"] for c in rows], args.travel_wait)
        for c in rows:
            t = travel.get(c["venue_id"]) or {}
            c["bike_min"] = round(t["bike_km"] / bike_kmh * 60 + 2) if t.get("bike_km") is not None else None
            c["pt_min"] = t.get("pt_min")
            c["pt_lines"] = t.get("pt_lines") or []

    # Transit lookups are paced (~1/s), so only fetch travel for what is shown:
    # every candidate in summary mode, only the listed rows with --list.
    if args.travel and not args.list:
        add_travel(candidates)

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
    if args.travel:
        add_travel(rows[: args.max_rows])
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
            flags.append(f"needs {c['min_tier_' + args.membership] or 'higher tier'}")
        if c.get("extra_fee_eur") is not None:
            flags.append(f"+{c['extra_fee_eur']}€ on site")
        if c["monthly_limit"] is not None and c["monthly_limit"] < 30:
            flags.append(f"{c['monthly_limit']}/mo")
        rating = f"★{c['venue_rating']}" if c["venue_rating"] else ""
        if args.travel:
            pt = f"{c['pt_min']}min" if c["pt_min"] is not None else "-"
            bike = f"{c['bike_min']}min" if c["bike_min"] is not None else "-"
            rating += f" | bike {bike} pt {pt}"
        print(
            f"{c['id']} | {c['start_time']}-{c['end_time']} | {c['title']} | {c['venue_name']} "
            f"(venue {c['venue_id']}) | {c['district']} | {c['category']} | {spots} {rating} {' '.join(flags)}".rstrip()
        )
    if len(rows) > args.max_rows:
        print(f"\n… {len(rows) - args.max_rows} more rows; narrow with --category/--grep or raise --max-rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
