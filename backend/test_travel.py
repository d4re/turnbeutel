"""Outcome-based tests for travel.py (OSRM/VBB helpers and the travel worker)."""

from datetime import datetime, timedelta

import travel
from travel import (
    BERLIN,
    Transit,
    in_vbb_area,
    next_reference_departure,
    origin_key,
    parse_journey,
    pick_exact,
    pick_typical,
)

# ── VBB journey fixtures ───────────────────────────────────────────────────


def _t(hhmm: str, day: str = "2026-10-20") -> str:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=BERLIN).isoformat()


def _journey(dep: str, arr: str, lines=("M4", "U8"), walk=(600, 177)) -> dict:
    """VBB-shaped journey: walk → one leg per line → walk, from `dep` to `arr`."""
    legs = [{"departure": _t(dep), "arrival": _t(dep), "walking": True, "distance": walk[0]}]
    for name in lines:
        legs.append({"departure": _t(dep), "arrival": _t(arr), "line": {"name": name}})
    legs.append({"departure": _t(arr), "arrival": _t(arr), "walking": True, "distance": walk[1]})
    return {"legs": legs}


# ── Pure helpers ───────────────────────────────────────────────────────────


def test_origin_key_rounds_to_4_decimals():
    assert origin_key(52.5579489, 13.4856965) == "52.5579,13.4857"


def test_in_vbb_area():
    assert in_vbb_area(52.52, 13.40)  # Berlin
    assert in_vbb_area(52.39, 13.06)  # Potsdam
    assert not in_vbb_area(53.55, 9.99)  # Hamburg


def test_next_reference_departure_from_monday():
    now = datetime(2026, 10, 19, 10, 0, tzinfo=BERLIN)
    assert next_reference_departure(now) == datetime(2026, 10, 20, 17, 30, tzinfo=BERLIN)
    assert next_reference_departure(now).utcoffset() == timedelta(hours=2)


def test_next_reference_departure_tuesday_evening_rolls_to_next_week():
    # DST ends on 25 Oct 2026, so the next reference time is in CET.
    now = datetime(2026, 10, 20, 17, 0, tzinfo=BERLIN)
    ref = next_reference_departure(now)
    assert (ref.year, ref.month, ref.day, ref.hour, ref.minute) == (2026, 10, 27, 17, 30)
    assert ref.utcoffset() == timedelta(hours=1)


def test_parse_journey_lines_walk_and_minutes():
    t = parse_journey(_journey("17:31", "18:09"))
    assert t == Transit(
        pt_min=38,
        lines=["M4", "U8"],
        walk_m=777,
        depart_at=datetime.fromisoformat(_t("17:31")),
        arrive_at=datetime.fromisoformat(_t("18:09")),
    )


def test_parse_journey_falls_back_to_planned_times():
    j = _journey("17:31", "18:09")
    first, last = j["legs"][0], j["legs"][-1]
    first["plannedDeparture"], first["departure"] = first["departure"], None
    last["plannedArrival"], last["arrival"] = last["arrival"], None
    assert parse_journey(j).pt_min == 38


def test_pick_typical_earliest_arrival():
    journeys = [_journey("17:34", "18:17"), _journey("17:31", "18:09"), _journey("17:38", "18:18")]
    assert pick_typical(journeys).arrive_at == datetime.fromisoformat(_t("18:09"))


def test_pick_exact_latest_departure_arriving_in_time():
    journeys = [_journey("18:00", "18:40"), _journey("18:12", "18:50"), _journey("18:20", "19:00")]
    arrive_by = datetime.fromisoformat(_t("18:55"))
    assert pick_exact(journeys, arrive_by).depart_at == datetime.fromisoformat(_t("18:12"))


def test_pick_returns_none_for_no_journeys():
    assert pick_typical([]) is None
    assert pick_exact([], datetime.fromisoformat(_t("18:55"))) is None
    # Nothing arrives in time.
    assert pick_exact([_journey("18:20", "19:00")], datetime.fromisoformat(_t("18:55"))) is None


def test_user_agent_names_the_project():
    assert "turnbeutel" in travel.USER_AGENT
