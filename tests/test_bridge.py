"""Route contract of the SkydiveOS raw-upload bridge (scripts/skydiveos_bridge.py).

The bridge's whole job is to answer ONE endpoint that a machine on another host
POSTs to unattended. If that endpoint's signature degrades, nothing tells us —
discovery just logs a rejection per clip and the day's footage never becomes jobs.
That happened: the handler took a ``Request`` parameter imported *inside*
``create_app``, and because this module is ``from __future__ import annotations``
FastAPI resolved the annotation against module globals, failed, and demoted it to a
required **query** parameter — so every notify 422'd while ``/healthz`` stayed green.

These tests pin the contract with a stub bridge (no Mongo, no S3): the decision
logic itself is exercised through ``ingest.match``.

Two dev-tooling behaviours are pinned further down, both of which exist to make the
*production* behaviour survivable rather than to soften it: the settle-window
override (off by default, shorten-only, loud) and clearing a terminal flag so a
fixed-data clip can be re-notified (``handled`` keys stay refused).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.skydiveos_bridge import (
    DEV_DEBOUNCE_ENV,
    PRODUCTION_DEBOUNCE_S,
    Bridge,
    clear_flagged,
    create_app,
    load_state,
    resolve_debounce,
    save_state,
)


class _StubBridge:
    """Enough of ``Bridge`` for the routes: records what the handler passed on."""

    def __init__(self) -> None:
        from api.config import get_settings

        self.settings: Any = get_settings()
        self.pending: dict[str, Any] = {}
        self.state: dict[str, dict[str, Any]] = {"handled": {}, "flagged": {}}
        self.seen: list[dict[str, Any]] = []

    async def raw_upload(self, notice: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(notice)
        return {"status": "accepted", "s3_key": notice.get("s3_key")}

    def ownership_audit_count(self) -> int:
        """The /healthz out-of-window counter (see Bridge.ownership_audit_count)."""
        return 0


def test_notify_body_reaches_the_bridge_as_the_posted_json() -> None:
    """The JSON body — not a query string — is what the handler consumes."""
    stub = _StubBridge()
    client = TestClient(create_app(stub))

    payload = {
        "s3_key": "raw/4313/GX010042.MP4",
        "camera_id": "4313",
        "captured_at": "2026-08-06T14:12:00+00:00",
        "staff_id": "6a16d38603b4c98fa2a9cd14",
        "staff_source": "qr",
    }
    resp = client.post("/api/media/raw-upload", json=payload)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "accepted", "s3_key": payload["s3_key"]}
    assert stub.seen == [payload]


def test_an_empty_notify_is_answered_not_rejected_as_a_missing_query_param() -> None:
    """The regression: ``{}`` must reach the bridge (which ignores it), never 422.

    This is the exact smoke test run against a fresh deployment, so it must stay
    truthful — a 422 here means every real notify would 422 too.
    """
    stub = _StubBridge()
    client = TestClient(create_app(stub))

    resp = client.post("/api/media/raw-upload", json={})

    assert resp.status_code == 200, resp.text
    assert stub.seen == [{}]


def test_healthz_reports_the_bridge_counters() -> None:
    stub = _StubBridge()
    stub.pending["load-7/2"] = object()
    stub.state["handled"]["raw/4313/GX010042.MP4"] = "job-1"
    client = TestClient(create_app(stub))

    assert client.get("/healthz").json() == {
        "ok": True,
        "pending_jumps": 1,
        "handled": 1,
        "flagged": 0,
        # The temporary compatibility path's counter (ownership accepted on
        # same-day-lone-candidate rather than a flight window) — reviewed after a week.
        "out_of_window_accepts": 0,
    }


def test_notify_requires_the_service_token_when_one_is_configured(
    monkeypatch: Any,
) -> None:
    """With AUTO_EDIT_API_KEY set, an unauthenticated notify is refused.

    The bridge is reachable from the public internet and one accepted notification
    creates a job, renders it and emails a customer — so the security group must not
    be its only gate (a dropzone's IP changes; a console mistake opens 0.0.0.0/0).
    """
    from api.config import get_settings

    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cr3t")
    get_settings.cache_clear()
    try:
        stub = _StubBridge()
        stub.settings = get_settings()
        client = TestClient(create_app(stub), raise_server_exceptions=False)

        assert client.post("/api/media/raw-upload", json={"s3_key": "k"}).status_code == 401
        assert client.post(
            "/api/media/raw-upload", json={"s3_key": "k"},
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        assert stub.seen == []  # nothing reached the matcher

        ok = client.post(
            "/api/media/raw-upload", json={"s3_key": "k"},
            headers={"Authorization": "Bearer s3cr3t"},
        )
        assert ok.status_code == 200, ok.text
        assert stub.seen == [{"s3_key": "k"}]

        # /healthz stays open: it is how an operator checks the bridge is alive.
        assert client.get("/healthz").status_code == 200
    finally:
        get_settings.cache_clear()


def test_the_gate_is_off_until_a_key_is_configured(monkeypatch: Any) -> None:
    """No AUTO_EDIT_API_KEY -> unchanged behaviour, same opt-in as the API's gate."""
    from api.config import get_settings

    monkeypatch.delenv("AUTO_EDIT_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        stub = _StubBridge()
        stub.settings = get_settings()
        client = TestClient(create_app(stub))
        assert client.post("/api/media/raw-upload", json={"s3_key": "k"}).status_code == 200
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The dev-only settle-window override
#
# The 900 s default is a scar: at 20 s a card pulled over a dropzone uplink split
# one jump into four jobs and emailed one customer four times (2026-08-06). The
# override exists so a laptop test cycle isn't 15 min — so what these tests pin is
# that it stays OFF unless asked for, can only ever shorten, and is never quiet.
# --------------------------------------------------------------------------- #


def test_the_settle_window_is_the_production_default_with_no_override() -> None:
    assert PRODUCTION_DEBOUNCE_S == 900.0
    assert resolve_debounce() == 900.0
    assert resolve_debounce(900.0, flag_s=None, env_s=None) == 900.0
    assert resolve_debounce(900.0, flag_s=None, env_s="") == 900.0  # exported but empty


def test_the_flag_shortens_the_window_and_says_so_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skydiveos_bridge"):
        assert resolve_debounce(900.0, flag_s=10.0) == 10.0

    banner = "\n".join(r.getMessage() for r in caplog.records)
    assert "DEV DEBOUNCE ACTIVE" in banner
    assert "10s" in banner and "900s" in banner
    assert "--dev-debounce" in banner
    assert "2026-08-06" in banner  # the incident, so nobody has to go find it
    assert "production" in banner


def test_the_env_var_shortens_the_window_and_the_flag_wins_over_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skydiveos_bridge"):
        assert resolve_debounce(900.0, env_s="15") == 15.0
    assert f"${DEV_DEBOUNCE_ENV}" in "\n".join(r.getMessage() for r in caplog.records)

    assert resolve_debounce(900.0, flag_s=5.0, env_s="15") == 5.0


@pytest.mark.parametrize("value", [0.0, -1.0, 900.0, 1800.0])
def test_the_override_may_only_shorten_never_lengthen_or_zero(
    value: float, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused value keeps the production window — it must never be applied quietly."""
    with caplog.at_level(logging.WARNING, logger="skydiveos_bridge"):
        assert resolve_debounce(900.0, flag_s=value) == 900.0
    assert "ignoring" in caplog.text


def test_a_junk_env_value_is_refused_not_crashed_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="skydiveos_bridge"):
        assert resolve_debounce(900.0, env_s="soon") == 900.0
    assert "not a number" in caplog.text


def test_main_wires_the_flag_and_env_through_without_starting_a_server(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """``--dev-debounce`` reaches the Bridge; ``--debounce`` alone stays production."""
    import scripts.skydiveos_bridge as mod

    built: list[float] = []

    class _FakeBridge:
        def __init__(self, api: str, *, debounce_s: float) -> None:
            built.append(debounce_s)
            self.settings = None

    monkeypatch.setattr(mod, "Bridge", _FakeBridge)
    monkeypatch.setattr(mod, "create_app", lambda bridge: object())
    monkeypatch.setitem(
        __import__("sys").modules, "uvicorn",
        type("_M", (), {"run": staticmethod(lambda *a, **k: None)}),
    )

    monkeypatch.delenv(DEV_DEBOUNCE_ENV, raising=False)
    assert mod.main([]) == 0
    assert built == [900.0]

    assert mod.main(["--dev-debounce", "8"]) == 0
    assert built[-1] == 8.0

    monkeypatch.setenv(DEV_DEBOUNCE_ENV, "12")
    assert mod.main([]) == 0
    assert built[-1] == 12.0


# --------------------------------------------------------------------------- #
# Clearing a terminal flag (scripts/unflag_bridge_key.py)
# --------------------------------------------------------------------------- #


def _seeded_state(tmp_path: Path) -> Path:
    path = tmp_path / "_bridge_state.json"
    save_state(path, {
        "handled": {"raw/4313/GX010001.MP4": "job-abc"},
        "flagged": {
            "raw/4313/GX010042.MP4": "NoLoadError: no load fits 2026-08-10T14:12",
            "raw/4313/GX010043.MP4": "customer 'Ada Byron' has no email",
        },
    })
    return path


def test_clear_flagged_removes_only_the_named_flag() -> None:
    state = {"handled": {}, "flagged": {"a": "why-a", "b": "why-b"}}

    cleared, unknown, handled = clear_flagged(state, ["a"])

    assert cleared == {"a": "why-a"}
    assert (unknown, handled) == ([], [])
    assert state["flagged"] == {"b": "why-b"}


def test_clear_flagged_refuses_a_key_that_already_became_a_job() -> None:
    """Clearing a handled key would invite a SECOND job (and a second email)."""
    state = {"handled": {"a": "job-1"}, "flagged": {"b": "why-b"}}

    cleared, unknown, handled = clear_flagged(state, ["a", "c"])

    assert cleared == {}
    assert handled == ["a"]
    assert unknown == ["c"]
    assert state == {"handled": {"a": "job-1"}, "flagged": {"b": "why-b"}}


def test_unflag_cli_lists_flags_when_given_no_target(
    tmp_path: Path, capsys: Any
) -> None:
    """A bare run must be read-only — the destructive path needs a key or --all."""
    from scripts.unflag_bridge_key import main as unflag

    path = _seeded_state(tmp_path)
    before = path.read_text()

    assert unflag(["--state", str(path)]) == 0

    out = capsys.readouterr().out
    assert "2 flagged key(s)" in out
    assert "raw/4313/GX010042.MP4" in out
    assert "no load fits" in out  # the reason, so the operator knows what to fix
    assert path.read_text() == before


def test_unflag_cli_clears_one_key_and_leaves_the_rest_alone(tmp_path: Path) -> None:
    from scripts.unflag_bridge_key import main as unflag

    path = _seeded_state(tmp_path)

    assert unflag(["--state", str(path), "raw/4313/GX010042.MP4"]) == 0

    state = json.loads(path.read_text())
    assert state["flagged"] == {"raw/4313/GX010043.MP4": "customer 'Ada Byron' has no email"}
    assert state["handled"] == {"raw/4313/GX010001.MP4": "job-abc"}  # untouched


def test_unflag_cli_all_and_dry_run(tmp_path: Path, capsys: Any) -> None:
    from scripts.unflag_bridge_key import main as unflag

    path = _seeded_state(tmp_path)

    assert unflag(["--state", str(path), "--all", "--dry-run"]) == 0
    assert len(json.loads(path.read_text())["flagged"]) == 2  # nothing written
    assert "would clear" in capsys.readouterr().out

    assert unflag(["--state", str(path), "--all"]) == 0
    state = json.loads(path.read_text())
    assert state["flagged"] == {}
    assert state["handled"] == {"raw/4313/GX010001.MP4": "job-abc"}


def test_unflag_cli_refuses_a_handled_key_with_a_nonzero_exit(
    tmp_path: Path, capsys: Any
) -> None:
    from scripts.unflag_bridge_key import main as unflag

    path = _seeded_state(tmp_path)

    assert unflag(["--state", str(path), "raw/4313/GX010001.MP4"]) == 1

    assert "REFUSED" in capsys.readouterr().out
    assert json.loads(path.read_text())["handled"] == {"raw/4313/GX010001.MP4": "job-abc"}


def test_unflag_cli_on_a_missing_state_file_is_a_no_op(tmp_path: Path) -> None:
    from scripts.unflag_bridge_key import main as unflag

    assert unflag(["--state", str(tmp_path / "nope.json"), "--all"]) == 0


# --------------------------------------------------------------------------- #
# The retry loop end to end: flag → duplicate → unflag → matched again.
# The production behaviour (flag is terminal for as long as it is recorded) is
# pinned here too, since that is what must NOT change.
# --------------------------------------------------------------------------- #


def _bridge_without_mongo(
    tmp_path: Path, match: Any, load_match: Any = None
) -> Bridge:
    """A real ``Bridge`` with its Mongo/S3 construction skipped.

    ``match`` is what the jumper-keyed resolve returns — an exception instance is *raised*
    instead, which is how the spec-flight fallback is reached. ``load_match`` is what the
    load-keyed resolve then returns (or raises).
    """

    def _answer(value: Any) -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

    bridge = object.__new__(Bridge)
    bridge.api = "http://localhost:8000"
    bridge.debounce_s = 900.0
    bridge.dev_debounce = False
    bridge.matcher = type(
        "_M",
        (),
        {
            # ``clip_ref`` is the matcher's decision-record label (the S3 key), passed
            # by the bridge so an out-of-window acceptance names its clip.
            "resolve": lambda self, cam, at, *, clip_ref=None: _answer(match),
            "resolve_for_staff": lambda self, s, at, *, clip_ref=None: _answer(match),
            "resolve_load": lambda self, cam, at, *, clip_ref=None: _answer(load_match),
            "resolve_load_for_staff": (
                lambda self, s, at, *, clip_ref=None: _answer(load_match)
            ),
        },
    )()
    bridge.pending = {}
    bridge.state_path = tmp_path / "_bridge_state.json"
    bridge.state = load_state(bridge.state_path)
    bridge._s3 = None
    return bridge


def _match() -> Any:
    from ingest.match import MatchResult

    return MatchResult(
        role="instructor", staff_id="staff-1", staff_name="Marc Tremblay",
        load_id="load-7", load_number=7, jumper_index=2,
        customer_email="ada@example.com", customer_name="Ada Byron",
        package="selfie", entitlement="edited_download",
    )


def test_a_flagged_key_stays_a_duplicate_until_it_is_cleared(tmp_path: Path) -> None:
    from scripts.unflag_bridge_key import main as unflag

    notice = {
        "s3_key": "raw/4313/GX010042.MP4",
        "camera_id": "4313",
        "captured_at": "2026-08-10T14:12:00+00:00",
    }

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _match())
        bridge._flag(notice["s3_key"], "NoLoadError: no load fits")

        # Production behaviour, unchanged: re-notifying never retries.
        assert await bridge.raw_upload(notice) == {
            "status": "duplicate", "s3_key": notice["s3_key"]
        }
        assert bridge.pending == {}

        # Operator fixes the load, clears the flag, restarts the bridge.
        assert unflag(["--state", str(bridge.state_path), notice["s3_key"]]) == 0
        restarted = _bridge_without_mongo(tmp_path, _match())
        assert restarted.state["flagged"] == {}

        result = await restarted.raw_upload(notice)
        assert result["status"] == "accepted"
        assert ("load-7", 2) in restarted.pending
        restarted.pending[("load-7", 2)].timer.cancel()  # don't fire during teardown

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# The spec-flight branch: a camera flyer with no assigned customer becomes ONE
# load master for the whole load, instead of a flagged clip nobody edits.
# --------------------------------------------------------------------------- #


def _load_match() -> Any:
    from ingest.match import LoadJumper, LoadMatchResult

    return LoadMatchResult(
        staff_id="staff-9", staff_name="Marc Tremblay",
        load_id="load-17", load_number=17, business_day="2026-08-10",
        jumpers=[
            LoadJumper(jumper_index=0, customer_name="Daniel",
                       customer_email="dan@x.test", media_package="video"),
            LoadJumper(jumper_index=1, customer_name="Priya",
                       customer_email="priya@x.test", media_package="none"),
        ],
    )


_SPEC_NOTICE = {
    "s3_key": "raw/4313/GX010099.MP4",
    "camera_id": "4313",
    "captured_at": "2026-08-10T14:12:00+00:00",
}


def test_a_spec_flight_becomes_a_load_master_instead_of_a_flag(tmp_path: Path) -> None:
    """No jumper slot → the jumper match raises → the load match takes it."""
    from ingest.match import NoBookingMatch

    async def scenario() -> None:
        bridge = _bridge_without_mongo(
            tmp_path, NoBookingMatch("no matchable load-jumper"), _load_match()
        )
        result = await bridge.raw_upload(_SPEC_NOTICE)

        assert result["status"] == "accepted"
        # Keyed on the LOAD, with a string where a jumper key holds its index — so a spec
        # flyer's clips can never be folded into a customer's job on the same load.
        assert ("load-17", "load") in bridge.pending
        jump = bridge.pending[("load-17", "load")]
        assert jump.is_load_master is True
        assert bridge.state["flagged"] == {}
        jump.timer.cancel()

    asyncio.run(scenario())


def test_an_assigned_flyer_is_flagged_not_turned_into_a_load_master(tmp_path: Path) -> None:
    """v1 is spec flights only. Both refusals are named so the cause is diagnosable."""
    from ingest.match import NoBookingMatch, NotSpecFlight

    async def scenario() -> None:
        bridge = _bridge_without_mongo(
            tmp_path,
            NoBookingMatch("no matchable load-jumper"),
            NotSpecFlight("staff is the assignedCameraman of jumper 0 on load 14"),
        )
        result = await bridge.raw_upload(_SPEC_NOTICE)

        assert result["status"] == "flagged"
        assert bridge.pending == {}
        reason = bridge.state["flagged"][_SPEC_NOTICE["s3_key"]]
        assert "NoBookingMatch" in reason and "NotSpecFlight" in reason

    asyncio.run(scenario())


def test_a_load_master_job_payload_carries_the_roster_and_no_customer(
    tmp_path: Path,
) -> None:
    """The ``POST /jobs`` body: no email, video_only + preview_only, the whole roster."""

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, None, _load_match())
        from scripts.skydiveos_bridge import PendingJump

        jump = PendingJump(match=_load_match(), captured_at="x", is_load_master=True)
        payload = bridge._job_payload(jump, "2026-08-10")

        assert payload["job_kind"] == "load_master"
        assert payload["package"] == "video_only"  # house cut, no photo set of strangers
        assert payload["entitlement"] == "preview_only"  # nobody bought it
        assert "customer_email" not in payload  # nothing is ever sent to a load master
        assert payload["customer_name"] == payload["load_label"] == "Load 17"
        assert payload["load_id"] == "load-17"
        # A spec flight was resolved from the timestamp alone — the fan-out's freefall
        # guard stays mandatory for it.
        assert payload["load_evidence"] == "flight_window"
        assert [r["bought_media"] for r in payload["load_roster"]] == [True, False]
        assert [r["customer_name"] for r in payload["load_roster"]] == ["Daniel", "Priya"]

    asyncio.run(scenario())


def test_a_normal_jump_payload_is_unchanged_and_now_carries_its_load_slot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _match())
        from scripts.skydiveos_bridge import PendingJump

        payload = bridge._job_payload(
            PendingJump(match=_match(), captured_at="x"), "2026-08-10"
        )

        assert payload["customer_email"] == "ada@example.com"
        assert payload["package"] == "selfie"
        assert "job_kind" not in payload  # omitted → the Job default ("jump")
        # New: the load slot rides along, so the fan-out can find this job later.
        assert (payload["load_id"], payload["jumper_index"]) == ("load-7", 2)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# clip → job grouping (audit §3-E / §3-K / ⚠️-4).
#
# The last hop of the ownership chain: several clips of ONE jump must become ONE job,
# and clips of different jumps must never share one. The grouping key is the matched
# slot — (load_id, jumper_index) for a customer, (load_id, "load") for a master — so
# these pin the isolation the render-once/one-email design depends on.
# --------------------------------------------------------------------------- #


def _slot_match(load_id: str, jumper_index: int, customer: str) -> Any:
    from ingest.match import MatchResult

    return MatchResult(
        role="instructor", staff_id="staff-1", staff_name="Marc Tremblay",
        load_id=load_id, load_number=7, jumper_index=jumper_index,
        customer_email=f"{customer.lower()}@example.com", customer_name=customer,
        package="selfie", entitlement="edited_download",
    )


def _clip_notice(key: str, at: str = "2026-08-11T14:12:00+00:00") -> dict[str, Any]:
    return {"s3_key": key, "camera_id": "4313", "captured_at": at}


def test_several_clips_of_one_jump_become_one_pending_job(tmp_path: Path) -> None:
    """Three clips, one customer → one pending jump holding all three."""

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        for n in (1, 2, 3):
            assert (await bridge.raw_upload(_clip_notice(f"raw/4313/2026-08-11/GX01000{n}.MP4")))[
                "status"
            ] == "accepted"

        assert list(bridge.pending) == [("load-7", 2)]
        pending = bridge.pending[("load-7", 2)]
        assert len(pending.clips) == 3
        assert pending.match.customer_name == "Ada"

    asyncio.run(scenario())


def test_two_customers_on_one_card_never_share_a_job(tmp_path: Path) -> None:
    """The isolation guarantee for a card holding several customers (audit §3-E).

    Each clip is matched independently, so the pending map is keyed per slot: two
    customers' clips can never land in one job dir, one render, or one email.
    """

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010001.MP4"))
        # Same card, next jump: the matcher resolves a different slot.
        bridge.matcher = type(
            "_M", (),
            {
                "resolve": lambda self, c, a, *, clip_ref=None: _slot_match("load-8", 5, "Grace"),
                "resolve_for_staff": (
                    lambda self, s, a, *, clip_ref=None: _slot_match("load-8", 5, "Grace")
                ),
            },
        )()
        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010002.MP4"))

        assert sorted(bridge.pending) == [("load-7", 2), ("load-8", 5)]
        for key, expected in ((("load-7", 2), "Ada"), (("load-8", 5), "Grace")):
            jump = bridge.pending[key]
            assert len(jump.clips) == 1
            assert jump.match.customer_name == expected

    asyncio.run(scenario())


def test_a_spec_flight_master_never_folds_into_a_customer_job_on_the_same_load(
    tmp_path: Path,
) -> None:
    """``(load_id, "load")`` is a string where a jumper key holds an int index."""
    from ingest.match import LoadMatchResult, NoBookingMatch

    roster = LoadMatchResult(
        staff_id="staff-9", staff_name="Lena", load_id="load-7", load_number=7, jumpers=[]
    )

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010001.MP4"))
        # The flyer's card: jumper-keyed match refuses, the spec-flight path answers.
        bridge = _bridge_without_mongo(
            tmp_path, NoBookingMatch("no slot"), load_match=roster
        )
        await bridge.raw_upload(_clip_notice("raw/9999/2026-08-11/GX010001.MP4"))

        assert list(bridge.pending) == [("load-7", "load")]  # not (load-7, 2)

    asyncio.run(scenario())


def test_a_clip_arriving_after_the_flush_joins_the_JOB_the_jump_already_has(
    tmp_path: Path,
) -> None:
    """KNOWN GAP ⚠️-4 closed (audit §3-K) — the per-jump idempotency key.

    The bridge's durable dedupe used to be keyed by ``s3_key`` alone, with nothing keyed
    by the *jump*. So a clip whose S3 upload outran the 15-minute settle window arrived
    after the job was created and started a fresh pending jump for the SAME customer — a
    second job, a second render and a second "your video is ready" email.

    A second pending jump is still opened (each notification is matched on its own), but
    ``state["jumps"]`` now records what job that slot already has, so the flush attaches
    to it instead of creating another. The mixed jump made this load-bearing rather than
    merely tidy: a jumper's two cards are plugged minutes or hours apart *by design*, so
    the late-arriving card is the normal case, not the pathological one.

    The attach path itself is pinned in ``test_bridge_mixed.py``.
    """

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010001.MP4"))
        # The settle window expired and _flush created the job.
        bridge.pending.clear()
        bridge.state["handled"]["raw/4313/2026-08-11/GX010001.MP4"] = "job-abc"
        bridge.state["jumps"]["load-7:2"] = "job-abc"

        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010002.MP4"))

        assert list(bridge.pending) == [("load-7", 2)]
        jump = bridge.pending[("load-7", 2)]
        assert len(jump.clips) == 1
        # The key the flush will find — the same job, not a new one.
        assert bridge._jump_state_key(jump) == "load-7:2"
        assert bridge.state["jumps"]["load-7:2"] == "job-abc"

    asyncio.run(scenario())


def test_a_clip_with_no_capture_time_is_flagged_not_guessed(tmp_path: Path) -> None:
    """No timestamp → no match is even attempted (the input the whole chain rests on)."""

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        result = await bridge.raw_upload(
            {"s3_key": "raw/4313/2026-08-11/GX010001.MP4", "camera_id": "4313"}
        )
        assert result["status"] == "flagged"
        assert "captured_at" in result["reason"]
        assert bridge.pending == {}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# The out_of_window_same_day audit trail (approved 2026-08-11, review after ~1 week).
#
# The compatibility path keeps a rescheduled customer's early interview attached to
# their jump, so it must be *countable and investigable* rather than silent — that is
# the whole basis on which it was allowed to stay.
# --------------------------------------------------------------------------- #


def test_an_out_of_window_acceptance_is_recorded_with_everything_needed_to_investigate(
    tmp_path: Path,
) -> None:
    from ingest.match import EVIDENCE_OUT_OF_WINDOW_SAME_DAY

    match = _slot_match("load-7", 2, "Ada")
    match = match.model_copy(update={
        "evidence": EVIDENCE_OUT_OF_WINDOW_SAME_DAY,
        "evidence_detail": {
            "event": "ownership_decision",
            "evidence": EVIDENCE_OUT_OF_WINDOW_SAME_DAY,
            "clip_ref": "raw/4313/2026-08-11/GX010001.MP4",
            "captured_local": "2026-08-11T10:15:00",
            "load_id": "load-7",
            "load_number": 7,
            "departure_local": "2026-08-11T14:00:00",
            "window_local": ["2026-08-11T13:30:00", "2026-08-11T16:30:00"],
            "window_source": "scheduled_departure",
            "seconds_outside_window": -11700.0,
            "jumper_index": 2,
            "role": "instructor",
            "booking_id": "bk-1",
            "customer_id": "cust-1",
            "candidates_considered": 1,
        },
    })

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, match)
        assert bridge.ownership_audit_count() == 0
        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010001.MP4"))

        path = bridge.state_path.parent / "_ownership_audit.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert len(rows) == 1
        row = rows[0]
        # Everything an investigation starts from, per the approval conditions.
        assert row["evidence"] == EVIDENCE_OUT_OF_WINDOW_SAME_DAY
        assert row["s3_key"] == "raw/4313/2026-08-11/GX010001.MP4"
        assert row["clip_ref"] == "raw/4313/2026-08-11/GX010001.MP4"
        assert row["captured_local"] == "2026-08-11T10:15:00"
        assert row["load_id"] == "load-7" and row["load_number"] == 7
        assert row["departure_local"] == "2026-08-11T14:00:00"
        assert row["seconds_outside_window"] == -11700.0
        assert row["jumper_index"] == 2 and row["booking_id"] == "bk-1"
        assert row["customer_name"] == "Ada" and row["staff_id"] == "staff-1"
        assert row["package"] == "selfie"
        # And the counter /healthz exposes is just this file's line count.
        assert bridge.ownership_audit_count() == 1

    asyncio.run(scenario())


def test_a_window_established_match_writes_no_audit_row(tmp_path: Path) -> None:
    """On a healthy dropzone the file stays empty, so its line count IS the metric."""

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        await bridge.raw_upload(_clip_notice("raw/4313/2026-08-11/GX010001.MP4"))
        assert not (bridge.state_path.parent / "_ownership_audit.jsonl").exists()
        assert bridge.ownership_audit_count() == 0

    asyncio.run(scenario())


def test_the_clip_key_is_passed_to_the_matcher_as_clip_ref(tmp_path: Path) -> None:
    """So a decision record can be traced back to the clip it was about."""
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        bridge = _bridge_without_mongo(tmp_path, _slot_match("load-7", 2, "Ada"))
        original = bridge.matcher.resolve_for_staff

        def _spy(staff_id, captured_at, *, clip_ref=None):
            seen["clip_ref"] = clip_ref
            return original(staff_id, captured_at, clip_ref=clip_ref)

        bridge.matcher.resolve_for_staff = _spy
        notice = {**_clip_notice("raw/4313/2026-08-11/GX010009.MP4"), "staff_id": "staff-1"}
        await bridge.raw_upload(notice)
        assert seen["clip_ref"] == "raw/4313/2026-08-11/GX010009.MP4"

    asyncio.run(scenario())
