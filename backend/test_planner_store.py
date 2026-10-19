"""Unit tests for the planner's SQLite store (suggestions, picks, feedback)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import planner_store
from models import Course, Feedback, NewSuggestion, RunCreate, SuggestionPatch

D1 = "2026-10-20"
D2 = "2026-10-21"


@pytest.fixture
def store(tmp_path):
    planner_store.close()
    planner_store.init(tmp_path / "planner.db")
    yield planner_store
    planner_store.close()


def course(course_id: int, date: str = D1, venue_id: str = "v1") -> Course:
    return Course(
        id=course_id,
        date=date,
        title=f"Course {course_id}",
        start_time="18:00",
        end_time="19:00",
        venue_id=venue_id,
        venue_name=f"Venue {venue_id}",
        lat=52.5,
        lng=13.4,
        district="Mitte",
        category="Dance",
        category_id=3,
        teacher="",
        free_spots=4,
        max_spots=12,
        is_online=False,
        is_plus=False,
        tiers_private=["Premium", "Max"],
        tiers_corporate=["L", "XL"],
        min_tier_private="Premium",
        min_tier_corporate="L",
        extra_fee_text="USC Checkin + 15 €",
        extra_fee_eur=15,
    )


def run(store, pairs: list[tuple[int, str]], date_from: str = D1, date_to: str = D2, ranks: dict | None = None):
    """Create a run suggesting each (course_id, date), snapshots from course()."""
    ranks = ranks or {}
    items = [
        NewSuggestion(course_id=cid, date=d, rank=ranks.get(cid, i + 1), reason=f"r{cid}", tags=["new"])
        for i, (cid, d) in enumerate(pairs)
    ]
    snapshots = {(cid, d): (1, course(cid, d)) for cid, d in pairs}
    return store.create_run(
        RunCreate(date_from=date_from, date_to=date_to, summary="s", suggestions=items), snapshots, now=100.0
    )


def by_course(suggestions):
    return {s.course_id: s for s in suggestions}


def test_profile_defaults_empty_then_roundtrips(store):
    p = store.get_profile()
    assert p.text == ""
    assert p.updated_at is None
    store.set_profile("x", 5.0)
    p = store.get_profile()
    assert p.text == "x"
    assert p.updated_at == 5.0


def test_create_run_snapshots_course_fields(store):
    r, suggestions = run(store, [(1, D1)])
    s = suggestions[0]
    assert s.run_id == r.id
    assert (s.title, s.venue_name, s.start_time, s.city_id) == ("Course 1", "Venue v1", "18:00", 1)
    assert s.tags == ["new"]
    assert (s.tiers_corporate, s.min_tier_corporate, s.tiers_private, s.min_tier_private) == (
        ["L", "XL"],
        "L",
        ["Premium", "Max"],
        "Premium",
    )
    assert (s.extra_fee_eur, s.extra_fee_text) == (15, "USC Checkin + 15 €")
    assert store.list_suggestions(D1, D1)[0].tags == ["new"]
    assert store.list_runs()[0].summary == "s"


def test_rerun_replaces_untouched_keeps_touched(store):
    _, sug = run(store, [(1, D1), (2, D1), (3, D1)])
    s = by_course(sug)
    store.update_suggestion(s[1].id, SuggestionPatch(picked=True))
    store.update_suggestion(s[2].id, SuggestionPatch(not_interested=True))
    run(store, [(4, D1)])
    assert set(by_course(store.list_suggestions(D1, D1))) == {1, 2, 4}


def test_rerun_outside_window_keeps_other_dates(store):
    run(store, [(1, D1)])
    run(store, [(2, D2)], date_from=D2, date_to=D2)
    assert set(by_course(store.list_suggestions(D1, D2))) == {1, 2}


def test_rerun_same_course_updates_kept_row(store):
    _, sug = run(store, [(1, D1)], ranks={1: 3})
    store.update_suggestion(sug[0].id, SuggestionPatch(picked=True))
    r2, _ = run(store, [(1, D1)], ranks={1: 1})
    rows = store.list_suggestions(D1, D1)
    assert len(rows) == 1
    assert rows[0].id == sug[0].id
    assert rows[0].rank == 1
    assert rows[0].picked is True
    assert rows[0].run_id == r2.id


def test_pick_unpicks_other_on_same_date(store):
    _, sug = run(store, [(1, D1), (2, D1), (3, D2)])
    s = by_course(sug)
    store.update_suggestion(s[3].id, SuggestionPatch(picked=True))
    store.update_suggestion(s[1].id, SuggestionPatch(picked=True))
    store.update_suggestion(s[2].id, SuggestionPatch(picked=True))
    now = by_course(store.list_suggestions(D1, D2))
    assert now[1].picked is False
    assert now[2].picked is True
    assert now[3].picked is True


def test_delete_touched_raises_conflict(store):
    _, sug = run(store, [(1, D1)])
    store.update_suggestion(sug[0].id, SuggestionPatch(picked=True))
    with pytest.raises(planner_store.ConflictError):
        store.delete_suggestion(sug[0].id)


def test_delete_untouched_removes(store):
    _, sug = run(store, [(1, D1)])
    store.delete_suggestion(sug[0].id)
    assert store.get_suggestion(sug[0].id) is None


def test_feedback_requires_pick_and_past_date(store):
    _, sug = run(store, [(1, D1), (2, D2)])
    s = by_course(sug)
    fb = Feedback(went="yes", rating=5, again="yes", again_after="2-4w", note="great")
    with pytest.raises(planner_store.ConflictError):
        store.set_feedback(s[1].id, fb, today=D1, now=1.0)
    store.update_suggestion(s[2].id, SuggestionPatch(picked=True))
    with pytest.raises(planner_store.ConflictError):
        store.set_feedback(s[2].id, fb, today=D1, now=1.0)
    store.update_suggestion(s[1].id, SuggestionPatch(picked=True))
    saved = store.set_feedback(s[1].id, fb, today=D1, now=1.0)
    assert saved.feedback.rating == 5
    assert saved.feedback.updated_at == 1.0
    history = store.list_history()
    assert [h.course_id for h in history] == [2, 1]  # newest date first
    assert history[1].feedback.note == "great"


def test_history_excludes_untouched(store):
    run(store, [(1, D1)])
    assert store.list_history() == []


def test_unknown_id_raises_not_found(store):
    with pytest.raises(planner_store.NotFoundError):
        store.update_suggestion(999, SuggestionPatch(rank=1))
    with pytest.raises(planner_store.NotFoundError):
        store.delete_suggestion(999)
    with pytest.raises(planner_store.NotFoundError):
        store.set_feedback(999, Feedback(went="came_up"), today=D1, now=1.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"went": "no", "rating": 4},
        {"went": "yes", "no_reason": "full"},
        {"went": "yes", "again": "no", "again_after": "1w"},
        {"went": "yes", "rating": 6},
        {"went": "maybe"},
    ],
)
def test_feedback_model_rejects_contradictions(kwargs):
    with pytest.raises(ValidationError):
        Feedback(**kwargs)
