"""BUGS 417 / 418 / 419 — the pipeline half.

* §1 ``ingest.match`` reads SkydiveOS's flight window: a load is matchable once its
  window has closed (``flightWindow.closedAt``, or ``closesAt`` already passed), whatever
  its status — the same rule as SkydiveOS's ``isLoadFootageEligible``. Without it a clip
  from a load staff left ``in-air`` is invisible, which held the reported edit until a
  human stepped in.
* §2 the bridge HOLDS such a clip instead of flagging it terminally, re-matches it on a
  timer, accepts it once the window closes, and flags it with a reason past the cap.
* §3 the SkydiveOS status callback is no longer fire-and-forget: a non-2xx is recorded on
  the job and retried on a schedule; only the CURRENT status is ever retried; the poll
  re-arms a due retry; the owed state is visible on ``GET /jobs/{id}``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from api.jobs import Job, JobStatus, JobStore, PendingCallback
from ingest.match import (
    FootageMatcher,
    NoBookingMatch,
    NoLoadMatch,
    flight_window_closed,
    load_is_matchable,
    window_still_open,
)
from tests.test_delivery import _settings

NOW = datetime(2026, 10, 21, 15, 30)  # naive UTC, as pymongo returns stored instants


# --------------------------------------------------------------------------- #
# §1 the shared eligibility rule
# --------------------------------------------------------------------------- #


class TestEligibility:
    def test_no_window_keeps_the_status_rule(self) -> None:
        assert load_is_matchable({"status": "landed"}, NOW)
        assert not load_is_matchable({"status": "in-air"}, NOW)
        assert not flight_window_closed({"status": "in-air"}, NOW)

    def test_a_claimed_close_makes_an_in_air_load_matchable(self) -> None:
        load = {"status": "in-air", "flightWindow": {"closedAt": NOW - timedelta(minutes=1)}}
        assert load_is_matchable(load, NOW)
        assert not window_still_open(load, NOW)

    def test_a_passed_closes_at_counts_even_before_the_sweep_claims_it(self) -> None:
        load = {"status": "boarding", "flightWindow": {"closesAt": NOW - timedelta(seconds=1)}}
        assert load_is_matchable(load, NOW)
        future = {"status": "boarding", "flightWindow": {"closesAt": NOW + timedelta(minutes=4)}}
        assert not load_is_matchable(future, NOW)
        assert window_still_open(future, NOW)

    def test_aware_instants_are_compared_in_utc(self) -> None:
        aware = (NOW - timedelta(minutes=1)).replace(tzinfo=UTC).astimezone(
            __import__("zoneinfo").ZoneInfo("America/Toronto")
        )
        assert flight_window_closed({"flightWindow": {"closesAt": aware}}, NOW)

    def test_a_cancelled_load_is_never_matchable(self) -> None:
        assert not load_is_matchable(
            {"status": "cancelled", "flightWindow": {"closedAt": NOW}}, NOW
        )

    def test_garbage_window_is_not_closed(self) -> None:
        assert not flight_window_closed({"flightWindow": "nope"}, NOW)
        assert not flight_window_closed({"flightWindow": {"closesAt": "2026-10-21"}}, NOW)


class _Coll:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def find(self, _q: Any) -> list[dict[str, Any]]:
        return list(self._docs)

    def find_one(self, q: dict[str, Any]) -> dict[str, Any] | None:
        wanted = q.get("_id")
        ids = wanted.get("$in") if isinstance(wanted, dict) else [wanted]
        return next((d for d in self._docs if d.get("_id") in ids), None)


def _matcher(loads: list[dict[str, Any]]) -> FootageMatcher:
    m = FootageMatcher("mongodb://unused-in-test")
    m._db = {
        "staffs": _Coll([{"_id": "s1", "firstName": "Marc", "lastName": "T"}]),
        "loads": _Coll(loads),
        "customers": _Coll([{"_id": "c1", "email": "c@x.test", "firstName": "Ada"}]),
        "bookingpackages": _Coll([]),
    }
    return m


def _in_air_load(flight_window: dict[str, Any] | None = None) -> dict[str, Any]:
    load: dict[str, Any] = {
        "_id": "L7",
        "status": "in-air",
        "loadNumber": 7,
        "businessDate": datetime(2026, 7, 21),
        "departureTime": datetime(2026, 7, 21, 13, 0),
        "jumpers": [
            {"instructor": "s1", "customer": "c1", "booking": "b1",
             "mediaPackage": "video", "videoType": "inside"},
        ],
    }
    if flight_window is not None:
        load["flightWindow"] = flight_window
    return load


class TestMatcherReadsTheWindow:
    def test_in_air_load_is_refused_while_the_window_is_open(self) -> None:
        open_window = {"closesAt": datetime(2999, 1, 1)}
        with pytest.raises(NoBookingMatch):
            _matcher([_in_air_load(open_window)]).resolve_for_staff("s1", "2026-07-21T13:05:00")

    def test_and_matched_once_skydiveos_closed_it(self) -> None:
        at = datetime(2026, 7, 21, 13, 5)
        closed = {"closedAt": at, "closesAt": at}
        r = _matcher([_in_air_load(closed)]).resolve_for_staff("s1", "2026-07-21T13:05:00")
        assert (r.load_id, r.role, r.customer_email) == ("L7", "instructor", "c@x.test")

    def test_the_spec_flight_path_reads_it_too(self) -> None:
        load = _in_air_load({"closedAt": datetime(2026, 7, 21, 13, 5)})
        load["jumpers"] = [{"customer": "c1", "booking": "b1"}]  # flyer holds no slot
        r = _matcher([load]).resolve_load_for_staff("s1", "2026-07-21T13:10:00")
        assert r.load_id == "L7"
        load.pop("flightWindow")
        with pytest.raises(NoLoadMatch):
            _matcher([load]).resolve_load_for_staff("s1", "2026-07-21T13:10:00")

    def test_open_window_hold_names_the_in_air_load(self) -> None:
        closes = datetime(2999, 1, 1)
        hold = _matcher([_in_air_load({"closesAt": closes})]).open_window_hold(
            "2026-07-21T13:05:00", staff_id="s1"
        )
        assert hold == {"load_id": "L7", "load_number": 7, "closes_at": closes}

    def test_no_hold_once_closed_or_when_the_clip_is_far_from_the_load(self) -> None:
        closed = _matcher([_in_air_load({"closedAt": datetime(2026, 7, 21, 13, 5)})])
        assert closed.open_window_hold("2026-07-21T13:05:00", staff_id="s1") is None
        far = _matcher([_in_air_load({"closesAt": datetime(2999, 1, 1)})])
        assert far.open_window_hold("2026-07-21T20:00:00", staff_id="s1") is None

    def test_a_landed_load_is_not_a_hold(self) -> None:
        load = _in_air_load()
        load["status"] = "landed"
        assert _matcher([load]).open_window_hold("2026-07-21T13:05:00", staff_id="s1") is None


# --------------------------------------------------------------------------- #
# §2 the bridge holds, retries, accepts — instead of a terminal flag
# --------------------------------------------------------------------------- #


class _ScriptedMatcher:
    """resolve_for_staff raises NoBookingMatch until ``closed`` flips True."""

    def __init__(self) -> None:
        self.closed = False
        self.holds = True

    def resolve_for_staff(self, staff_id: Any, captured_at: Any, *, clip_ref: Any = None) -> Any:
        if not self.closed:
            raise NoBookingMatch("staff is in no matchable load-jumper")
        from types import SimpleNamespace

        return SimpleNamespace(
            load_id="L7", jumper_index=0, load_number=7, role="instructor",
            package="video_only", entitlement="edited_download", customer_email="c@x.test",
            customer_name="Ada", evidence="window", staff_id=staff_id, staff_name="Marc T",
        )

    def resolve_load_for_staff(self, staff_id: Any, captured_at: Any) -> Any:
        raise NoLoadMatch("no matchable load's window contains the clip")

    def open_window_hold(
        self, captured_at: Any, *, staff_id: Any = None, camera_id: Any = None
    ) -> Any:
        if self.closed or not self.holds:
            return None
        return {"load_id": "L7", "load_number": 7, "closes_at": datetime(2026, 7, 21, 13, 5)}


def _bridge(tmp_path: Path) -> Any:
    from scripts.skydiveos_bridge import Bridge, load_state

    b = Bridge.__new__(Bridge)
    b.api = "http://api.test"
    b.debounce_s = 900.0
    b.dev_debounce = False
    b.settings = _settings(jobs_root=str(tmp_path))
    b.matcher = _ScriptedMatcher()
    b.pending = {}
    b.state_path = tmp_path / "_bridge_state.json"
    b.state = load_state(b.state_path)
    b._s3 = None
    return b


NOTICE = {"s3_key": "raw/4313/GX010001.MP4", "captured_at": "2026-07-21T13:05:00",
          "staff_id": "s1", "camera_id": "4313"}


def test_bridge_holds_an_in_air_clip_instead_of_flagging_it(tmp_path: Path) -> None:
    from scripts.skydiveos_bridge import load_state

    b = _bridge(tmp_path)
    r = asyncio.run(b.raw_upload(dict(NOTICE)))
    assert r["status"] == "deferred" and r["load_number"] == 7
    assert NOTICE["s3_key"] not in b.state["flagged"]
    # Durable: a restarted bridge still holds it.
    assert NOTICE["s3_key"] in load_state(b.state_path)["deferred"]
    # A re-notify from discovery while it waits is a duplicate, not a second attempt.
    assert asyncio.run(b.raw_upload(dict(NOTICE)))["status"] == "duplicate"


def test_held_clip_is_accepted_once_the_window_closes(tmp_path: Path) -> None:
    b = _bridge(tmp_path)

    async def scenario() -> tuple[dict[str, int], dict[str, int]]:
        await b.raw_upload(dict(NOTICE))
        first = await b.retry_deferred(now=10**12)  # still open → re-held
        b.matcher.closed = True  # SkydiveOS closed the window
        second = await b.retry_deferred(now=10**12)
        for jump in b.pending.values():
            jump.timer.cancel()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == {"accepted": 0, "flagged": 0, "deferred": 1, "other": 0}
    assert second == {"accepted": 1, "flagged": 0, "deferred": 0, "other": 0}
    assert b.state["deferred"] == {}
    assert ("L7", 0) in b.pending


def test_a_retry_is_not_due_before_next_at(tmp_path: Path) -> None:
    b = _bridge(tmp_path)
    asyncio.run(b.raw_upload(dict(NOTICE)))
    idle = {"accepted": 0, "flagged": 0, "deferred": 0, "other": 0}
    assert asyncio.run(b.retry_deferred(now=0)) == idle


def test_a_hold_that_never_closes_is_flagged_with_the_reason(tmp_path: Path) -> None:
    from scripts import skydiveos_bridge as mod

    b = _bridge(tmp_path)
    asyncio.run(b.raw_upload(dict(NOTICE)))
    b.state["deferred"][NOTICE["s3_key"]]["since"] -= mod.DEFER_MAX_S + 1
    out = asyncio.run(b.retry_deferred(now=10**12))
    assert out["flagged"] == 1
    reason = b.state["flagged"][NOTICE["s3_key"]]
    assert "flight window to close and it never did" in reason
    assert b.state["deferred"] == {}


def test_nothing_to_wait_for_still_flags_as_before(tmp_path: Path) -> None:
    b = _bridge(tmp_path)
    b.matcher.holds = False
    r = asyncio.run(b.raw_upload(dict(NOTICE)))
    assert r["status"] == "flagged"


def test_weak_same_day_ownership_waits_for_an_in_air_load(tmp_path: Path) -> None:
    """The wrong-customer hazard: a lone landed candidate matched outside its window
    while the flyer's CURRENT load is still in the air. Hold — don't attach."""
    b = _bridge(tmp_path)
    b.matcher.closed = True  # resolve succeeds…
    original = b.matcher.resolve_for_staff

    def weak(*a: Any, **k: Any) -> Any:
        m = original(*a, **k)
        m.evidence = "out_of_window_same_day"
        return m

    b.matcher.resolve_for_staff = weak
    b.matcher.open_window_hold = lambda *a, **k: {
        "load_id": "L8", "load_number": 8, "closes_at": None,
    }
    r = asyncio.run(b.raw_upload(dict(NOTICE)))
    assert r["status"] == "deferred" and r["load_number"] == 8
    assert b.pending == {}


# --------------------------------------------------------------------------- #
# §3 the status callback is recorded and retried
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, code: int, text: str = "") -> None:
        self.status_code = code
        self.text = text


@pytest.fixture
def cb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import httpx

    from api import tasks

    store = JobStore(tmp_path)
    store.create(Job(job_id="j1", status=JobStatus.delivered, gallery_token="tok123"))
    monkeypatch.setattr(
        tasks, "get_settings",
        lambda: _settings(
            jobs_root=str(tmp_path), skydiveos_api_base="http://sos.test",
            public_base_url="https://edit.test",
        ),
    )
    state: dict[str, Any] = {"responses": [], "posts": [], "scheduled": []}

    def _post(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        state["posts"].append(json)
        nxt = state["responses"].pop(0) if state["responses"] else _Resp(200)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(
        tasks, "_schedule_callback_retry",
        lambda job_id, status, countdown: state["scheduled"].append((job_id, status, countdown)),
    )
    state["store"] = store
    state["tasks"] = tasks
    return state


def test_a_failed_delivered_callback_is_recorded_and_scheduled(cb: dict[str, Any]) -> None:
    cb["responses"] = [_Resp(500, "boom")]
    cb["tasks"]._notify_skydiveos(cb["store"].load("j1"))
    pc = cb["store"].load("j1").pending_callback
    assert pc is not None and (pc.status, pc.attempts) == ("delivered", 1)
    assert pc.last_error == "HTTP 500: boom"
    assert cb["scheduled"] == [("j1", "delivered", 60)]
    assert cb["posts"][0]["gallery_url"] == "https://edit.test/j/tok123"


def test_network_errors_and_401_are_failures_with_actionable_text(cb: dict[str, Any]) -> None:
    import httpx

    cb["responses"] = [httpx.ConnectError("refused"), _Resp(401, "Invalid callback token")]
    cb["tasks"]._notify_skydiveos(cb["store"].load("j1"))
    assert cb["store"].load("j1").pending_callback.last_error.startswith("network: ")
    cb["tasks"]._notify_skydiveos(cb["store"].load("j1"))
    pc = cb["store"].load("j1").pending_callback
    assert pc.attempts == 2 and "AUTO_EDIT_CALLBACK_TOKEN" in pc.last_error
    assert [c[2] for c in cb["scheduled"]] == [60, 120]


def test_the_retry_task_resends_and_clears_on_2xx(cb: dict[str, Any]) -> None:
    cb["responses"] = [_Resp(503)]
    cb["tasks"]._notify_skydiveos(cb["store"].load("j1"))
    cb["store"].update("j1", pending_callback=cb["store"].load("j1").pending_callback.model_copy(
        update={"next_at": 0.0}))
    assert cb["tasks"].retry_status_callback_job("j1", "delivered") == "delivered"
    assert cb["store"].load("j1").pending_callback is None
    assert len(cb["posts"]) == 2


def test_a_duplicate_or_early_retry_message_is_a_noop(cb: dict[str, Any]) -> None:
    cb["responses"] = [_Resp(503)]
    cb["tasks"]._notify_skydiveos(cb["store"].load("j1"))
    assert cb["tasks"].retry_status_callback_job("j1", "delivered") == "not due"
    assert cb["tasks"].retry_status_callback_job("j1", "processing") == "nothing owed"
    assert len(cb["posts"]) == 1


def test_an_older_status_is_never_replayed_over_a_newer_one(cb: dict[str, Any]) -> None:
    store, tasks = cb["store"], cb["tasks"]
    store.update("j1", status=JobStatus.ready_for_review)
    cb["responses"] = [_Resp(500)]
    tasks._notify_skydiveos(store.load("j1"))
    assert store.load("j1").pending_callback.status == "ready_for_review"
    # The job moves on; its own callback lands and supersedes the owed one.
    store.update("j1", status=JobStatus.delivered)
    tasks._notify_skydiveos(store.load("j1"))
    assert store.load("j1").pending_callback is None
    # And a stale retry message for the old status sends nothing.
    store.update("j1", pending_callback=PendingCallback(
        status="ready_for_review", first_failed_at=0.0, attempts=1, next_at=0.0))
    assert tasks.retry_status_callback_job("j1", "ready_for_review") == "superseded"
    assert store.load("j1").pending_callback is None
    assert len(cb["posts"]) == 2


def test_retries_are_bounded_and_the_give_up_stays_visible(cb: dict[str, Any]) -> None:
    store, tasks = cb["store"], cb["tasks"]
    store.update("j1", pending_callback=PendingCallback(
        status="delivered", first_failed_at=0.0, attempts=3, next_at=0.0))
    cb["responses"] = [_Resp(500)]
    tasks._notify_skydiveos(store.load("j1"))
    pc = store.load("j1").pending_callback
    assert pc.gave_up_at is not None and pc.next_at is None
    assert cb["scheduled"] == []
    assert tasks.retry_status_callback_job("j1", "delivered") == "gave up"


def test_the_poll_rearms_a_due_retry_and_shows_the_owed_state(
    cb: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from api.app import create_app

    store, tasks = cb["store"], cb["tasks"]
    store.update("j1", pending_callback=PendingCallback(
        status="delivered", first_failed_at=0.0, attempts=1, next_at=0.0, last_error="HTTP 502: x"))
    assert tasks.kick_due_status_callback(store.load("j1")) is True
    assert cb["scheduled"] == [("j1", "delivered", 0)]
    # Not due → no re-arm.
    store.update("j1", pending_callback=store.load("j1").pending_callback.model_copy(
        update={"next_at": 10**12}))
    assert tasks.kick_due_status_callback(store.load("j1")) is False

    from api.app import get_queue, get_store
    from tests.test_api import FakeQueue

    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: FakeQueue()
    with TestClient(app) as client:
        body = client.get("/jobs/j1").json()
    assert body["status_callback"]["status"] == "delivered"
    assert body["status_callback"]["last_error"] == "HTTP 502: x"


def test_a_delivered_callback_without_a_gallery_url_is_logged_as_an_error(
    cb: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tasks = cb["tasks"]
    monkeypatch.setattr(
        tasks, "get_settings",
        lambda: _settings(jobs_root=str(cb["store"].root), skydiveos_api_base="http://sos.test"),
    )
    with caplog.at_level("ERROR"):
        tasks._notify_skydiveos(cb["store"].load("j1"))
    assert "no gallery_url was sent" in caplog.text
    assert "gallery_url" not in cb["posts"][0]


def test_recorded_takeoff_is_read_as_a_true_instant_not_local_wall_clock() -> None:
    """SkydiveOS stamps actualTakeoffTime with `new Date()` — a UTC instant. Read as local
    wall clock it put the recorded window 4 h off in Toronto/Montreal, so an on-flight clip
    only survived on the lone-candidate fallback (found in the BUG 417 cross-system QA)."""
    load = _in_air_load({"closedAt": datetime(2026, 7, 21, 17, 6)})
    load["status"] = "landed"
    load["departureTime"] = datetime(2026, 7, 21, 13, 0)        # naive DZ-local, as stored
    load["actualTakeoffTime"] = datetime(2026, 7, 21, 17, 1)    # 13:01 EDT as a UTC instant
    load["landingTime"] = datetime(2026, 7, 21, 17, 25)
    m = _matcher([load])
    m._tz = __import__("zoneinfo").ZoneInfo("America/Toronto")
    r = m.resolve_for_staff("s1", "2026-07-21T17:12:00+00:00")  # 13:12 local, mid-flight
    assert r.evidence == "window"
