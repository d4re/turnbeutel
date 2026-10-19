"""Outcome-based tests for travel.py (OSRM/VBB helpers and the travel worker)."""

import asyncio
import contextlib
import time
from datetime import datetime, timedelta

import httpx
import pytest

import storage
import travel
from models import City, Venue, VenueAddress
from travel import (
    BERLIN,
    Pacer,
    Transit,
    TransitUnavailable,
    TravelWorker,
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


# ── TravelWorker ───────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    storage.close()
    storage.init(tmp_path / "travel.db")
    storage.upsert_cities([City(id=1, name="Berlin")], fetched_at=time.time())
    yield storage
    storage.close()


def _seed_venues(points: dict[str, tuple[float, float]]) -> None:
    venues = [
        Venue(
            name=vid,
            slug=vid,
            url="",
            tiers_private=[],
            tiers_corporate=[],
            min_tier_private=None,
            min_tier_corporate=None,
            activities=[],
            district="",
            street="",
            is_plus=False,
            address_id=vid,
            lat=lat,
            lng=lng,
            address=VenueAddress(),
            rating=None,
            review_count=None,
            is_online=False,
            has_coordinates=True,
        )
        for vid, (lat, lng) in points.items()
    ]
    storage.upsert_venues(1, venues, time.time(), len(venues), len(venues))


class _Fakes:
    """Records outgoing OSRM/VBB calls; journeys/bike results are configurable per destination."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.fail_transit: set[tuple[float, float]] = set()
        self.no_journeys: set[tuple[float, float]] = set()

        async def fake_bike(origin, dests, client):
            self.calls.append(("bike", origin, tuple(d[0] for d in dests)))
            return {vid: 5.0 for vid, _, _ in dests}

        async def fake_journeys(origin, dest, client, pacer, *, departure=None, arrival=None):
            kind = "exact" if arrival else "transit"
            self.calls.append((kind, origin, dest))
            if dest in self.fail_transit:
                raise httpx.ConnectError("down")
            if dest in self.no_journeys:
                return []
            if arrival:
                return [_journey("18:12", "18:50")]
            return [_journey("17:31", "18:09")]

        monkeypatch.setattr(travel, "fetch_bike_km", fake_bike)
        monkeypatch.setattr(travel, "fetch_journeys", fake_journeys)

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def _drive(worker: TravelWorker, until, setup=None, max_wait: float = 2.0) -> None:
    """Run `setup`, start the worker, wait until `until()` holds (or `max_wait`), stop it."""

    async def main():
        if setup:
            await setup()
        task = asyncio.create_task(worker.run())
        for _ in range(int(max_wait / 0.01)):
            if until():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())


def _worker(origin=HOME) -> TravelWorker:
    w = TravelWorker(client=None, vbb_pacer=_no_pacing())
    w.set_origin(origin)
    return w


A, B, C = (52.53, 13.40), (52.50, 13.45), (52.48, 13.43)
KEY = origin_key(*HOME)


def test_worker_bike_then_transit_stored(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    _seed_venues({"a": A})
    w = _worker()
    _drive(
        w,
        lambda: "a" in storage.get_travel(KEY, ["a"]) and storage.get_travel(KEY, ["a"])["a"].pt_min,
        lambda: w.request(["a"]),
    )
    entry = storage.get_travel(KEY, ["a"])["a"]
    assert entry.bike_km == 5.0
    assert (entry.pt_min, entry.pt_lines, entry.walk_m) == (38, ["M4", "U8"], 777)
    assert [c[0] for c in fakes.calls] == ["bike", "transit"]
    assert not w.is_pending("a")


def test_worker_priority_order(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    _seed_venues({"a": A, "b": B, "c": C})
    w = _worker()
    results = {}

    async def setup():
        await w.warm([("a", *A)])
        await w.request(["b"])
        results["exact"] = asyncio.create_task(w.exact("c", C, datetime(2026, 10, 22, 18, 55, tzinfo=BERLIN)))
        await asyncio.sleep(0)  # let exact() enqueue before the worker starts

    _drive(w, lambda: len(fakes.of("transit")) == 2, setup)
    order = [(c[0], c[2]) for c in fakes.calls if c[0] != "bike"]
    assert order == [("exact", C), ("transit", B), ("transit", A)]


def test_worker_exact_result_and_cache(db, monkeypatch):
    _Fakes(monkeypatch)
    w = _worker()
    arrive_by = datetime(2026, 10, 22, 18, 55, tzinfo=BERLIN)
    out = {}

    async def setup():
        out["task"] = asyncio.create_task(w.exact("c", C, arrive_by))

    _drive(w, lambda: out["task"].done(), setup)
    result = out["task"].result()
    assert result["pt_min"] == 38
    assert datetime.fromisoformat(result["depart_at"]).strftime("%H:%M") == "18:12"
    assert result["lines"] == ["M4", "U8"]
    assert storage.get_exact(KEY, "c", arrive_by.isoformat(), max_age=60) == result


def test_worker_exact_failure_raises_transit_unavailable(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    fakes.no_journeys.add(C)
    w = _worker()
    out = {}

    async def setup():
        out["task"] = asyncio.create_task(w.exact("c", C, datetime(2026, 10, 22, 18, 55, tzinfo=BERLIN)))

    _drive(w, lambda: out["task"].done(), setup)
    assert isinstance(out["task"].exception(), TransitUnavailable)


def test_worker_drops_jobs_for_stale_origin(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    _seed_venues({"a": A})
    other = (52.40, 13.10)
    w = _worker()

    async def setup():
        await w.warm([("a", *A)])
        w.set_origin(other)

    _drive(w, lambda: False, setup, max_wait=0.2)
    assert all(c[1] != HOME for c in fakes.calls)
    assert storage.get_travel(KEY, ["a"]) == {}


def test_worker_transit_failure_marks_failed_and_continues(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    fakes.fail_transit.add(A)
    _seed_venues({"a": A, "b": B})
    w = _worker()
    _drive(w, lambda: len(fakes.of("transit")) == 2 and not w.is_pending("b"), lambda: w.request(["a", "b"]))
    got = storage.get_travel(KEY, ["a", "b"])
    assert got["a"].pt_failed_at is not None
    assert got["a"].pt_min is None
    assert got["b"].pt_min == 38


def test_worker_empty_journeys_marks_failed(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    fakes.no_journeys.add(A)
    _seed_venues({"a": A})
    w = _worker()
    _drive(w, lambda: len(fakes.of("transit")) == 1 and not w.is_pending("a"), lambda: w.request(["a"]))
    assert storage.get_travel(KEY, ["a"])["a"].pt_failed_at is not None


def test_worker_failed_transit_not_retried_within_back_off(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    _seed_venues({"a": A})
    storage.set_bike_km(KEY, {"a": 5.0}, at=time.time())
    storage.mark_transit_failed(KEY, "a", at=time.time())
    w = _worker()
    _drive(w, lambda: False, lambda: w.request(["a"]), max_wait=0.2)
    assert fakes.calls == []


def test_worker_skips_outside_vbb_area(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    _seed_venues({"hh": (53.55, 9.99)})
    w = _worker()
    _drive(w, lambda: bool(fakes.of("bike")) and not w.is_pending("hh"), lambda: w.request(["hh"]))
    assert fakes.of("transit") == []
    assert storage.get_travel(KEY, ["hh"])["hh"].bike_km == 5.0


def test_worker_request_skips_fresh_entries(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    _seed_venues({"a": A})
    storage.set_bike_km(KEY, {"a": 5.0}, at=time.time())
    storage.set_transit(KEY, "a", pt_min=20, lines=[], walk_m=0, at=time.time())
    w = _worker()

    async def setup():
        await w.request(["a"])
        assert not w.is_pending("a")

    _drive(w, lambda: False, setup, max_wait=0.2)
    assert fakes.calls == []


def test_worker_unknown_venue_is_ignored(db, monkeypatch):
    fakes = _Fakes(monkeypatch)
    w = _worker()
    _drive(w, lambda: False, lambda: w.request(["nope"]), max_wait=0.2)
    assert fakes.calls == []
