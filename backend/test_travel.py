"""Outcome-based tests for travel.py (OSRM/VBB helpers and the travel worker)."""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

import travel
from travel import (
    BERLIN,
    Pacer,
    Transit,
    fetch_bike_km,
    fetch_journeys,
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


# ── OSRM ───────────────────────────────────────────────────────────────────

HOME = (52.5579489, 13.4856965)


def _osrm_handler(paths: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        n_dests = request.url.path.count(";")
        assert request.url.params["sources"] == "0"
        assert request.url.params["annotations"] == "distance"
        return httpx.Response(200, json={"distances": [[0.0] + [1234.0 + i for i in range(n_dests)]]})

    return handler


def test_fetch_bike_km_batches_of_100():
    paths: list[str] = []
    dests = [(f"v{i}", 52.5 + i / 10000, 13.4) for i in range(230)]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_osrm_handler(paths))) as client:
            return await fetch_bike_km(HOME, dests, client)

    result = asyncio.run(run())
    assert [p.count(";") for p in paths] == [100, 100, 30]
    # Origin first (lng,lat order), then the batch's destinations in order.
    assert paths[0].split("/")[-1].startswith(f"{HOME[1]},{HOME[0]};13.4,52.5;13.4,52.5001;")
    assert len(result) == 230
    assert result["v0"] == 1.23
    assert result["v101"] == 1.24  # second batch restarts at index 1 → 1235 m


def test_fetch_bike_km_skips_null_distances():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"distances": [[0.0, 2000.0, None]]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_bike_km(HOME, [("a", 52.5, 13.4), ("b", 52.6, 13.4)], client)

    assert asyncio.run(run()) == {"a": 2.0}


# ── Pacer ──────────────────────────────────────────────────────────────────


def test_pacer_spaces_calls():
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = Pacer(0.7, clock=lambda: now[0], sleep=fake_sleep)

    async def run():
        await asyncio.gather(pacer.wait(), pacer.wait(), pacer.wait())

    asyncio.run(run())
    assert sum(slept) == pytest.approx(1.4)


# ── VBB ────────────────────────────────────────────────────────────────────


def _no_pacing() -> Pacer:
    async def no_sleep(seconds: float) -> None:
        return None

    return Pacer(0, sleep=no_sleep)


def test_fetch_journeys_retries_once_then_raises(monkeypatch):
    monkeypatch.setattr(travel, "RETRY_DELAY", 0)
    responses = [503, 503]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(responses.pop(0), json={"journeys": [_journey("17:31", "18:09")]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_journeys(HOME, (52.5, 13.4), client, _no_pacing(), departure=datetime.now(BERLIN))

    with pytest.raises(httpx.HTTPError):
        asyncio.run(run())

    responses[:] = [503, 200]
    assert len(asyncio.run(run())) == 1
    assert responses == []


def test_fetch_journeys_sends_arrival_param():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"journeys": []})

    arrive_by = datetime(2026, 10, 22, 18, 55, tzinfo=BERLIN)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_journeys(HOME, (52.5, 13.4), client, _no_pacing(), arrival=arrive_by)

    assert asyncio.run(run()) == []
    params = seen[0].params
    assert datetime.fromisoformat(params["arrival"]) == arrive_by
    assert "departure" not in params
    assert params["from.latitude"] == str(HOME[0])
    assert params["to.longitude"] == "13.4"
    assert params["results"] == "3"


# ── Nominatim ──────────────────────────────────────────────────────────────


def test_geocode_parses_nominatim():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"display_name": "143G, Falkenberger Straße, Berlin", "lat": "52.5579489", "lon": "13.4856965"},
                {"display_name": "Falkenberger Straße, Berlin", "lat": "52.556", "lon": "13.48"},
            ],
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await travel.geocode("Falkenberger Str. 143G", "13.3,52.6,13.6,52.4", client, _no_pacing())

    results = asyncio.run(run())
    assert [(r.label, r.lat, r.lng) for r in results] == [
        ("143G, Falkenberger Straße, Berlin", 52.5579489, 13.4856965),
        ("Falkenberger Straße, Berlin", 52.556, 13.48),
    ]
    req = seen[0]
    assert req.headers["User-Agent"] == travel.USER_AGENT
    assert req.url.params["q"] == "Falkenberger Str. 143G"
    assert req.url.params["viewbox"] == "13.3,52.6,13.6,52.4"
    assert req.url.params["bounded"] == "0"
    assert req.url.params["limit"] == "5"
