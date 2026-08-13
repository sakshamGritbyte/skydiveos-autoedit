"""The card-status PUSH: what makes the operator banner work in production.

The registry is in-memory and per-process, so ``GET /ingest/cards`` answers only on the box
running discovery — the dropzone machine with the reader. Production deliberately puts the
renderer on another host with discovery off, and SkydiveOS holds one auto-edit base URL
pointing there, so a *pull* reads an empty list forever while the dropzone box sits behind
NAT and cannot be dialled in to. Hence a push, outbound, like every other hand-off that box
originates.

Four properties are pinned here because each one, broken, is a distinct failure the operator
sees or pays for:

* **It never raises.** Discovery and the pull are the product; the banner is cosmetic.
* **It pushes ONE final empty snapshot.** Otherwise the consumer's cache holds the last
  non-empty snapshot until its TTL expires, and a removed card's row lingers on screen still
  reading "copying".
* **A failing push warns on the transition, not every tick.** At a 2 s cadence a
  per-attempt warning buries the log it exists to help read.
* **It carries the service token.** The receiver is a public app-to-app endpoint whose body
  tells an operator to act on hardware.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ingest.cardstatus import CardStatusRegistry
from ingest.discovery import CARD_STATUS_PATH, publish_card_status

_URL = "http://skydiveos:8001"


class _Recorder:
    """Stands in for ``httpx.AsyncClient``, recording every POST body."""

    def __init__(self, posts: list[dict[str, Any]], fail: Exception | None = None) -> None:
        self._posts = posts
        self._fail = fail

    async def __aenter__(self) -> _Recorder:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(self, url: str, *, json: Any, headers: Any) -> Any:
        if self._fail is not None:
            raise self._fail
        self._posts.append({"url": url, "body": json, "headers": headers})
        return type("_R", (), {"raise_for_status": lambda s: None})()


def _drive(
    registry: CardStatusRegistry,
    posts: list[dict[str, Any]],
    monkeypatch: Any,
    *,
    ticks: int,
    client_factory: Any = None,
    on_tick: Any = None,
) -> None:
    """Run the publisher for exactly ``ticks`` loop iterations, then stop it.

    ``asyncio.sleep`` is the loop's only await point, so replacing it both removes the real
    delay and gives a deterministic hook between iterations. The injected cancellation is
    swallowed here, so a test that reaches its assertions has proved nothing else escaped.
    """
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient",
        client_factory or (lambda **_kw: _Recorder(posts)),
        raising=False,
    )
    state = {"n": 0}
    real_sleep = asyncio.sleep

    async def _fake_sleep(_seconds: float) -> None:
        state["n"] += 1
        if on_tick is not None:
            on_tick(state["n"])
        if state["n"] >= ticks:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _run() -> None:
        try:
            await publish_card_status(registry, _URL, interval=0)
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def _pulling_card() -> CardStatusRegistry:
    registry = CardStatusRegistry()
    registry.detected("4313")
    registry.totals("4313", 11, 31_000_000_000)
    registry.file_started("4313", "GX010042.MP4")
    return registry


def test_it_posts_the_snapshot_to_the_status_path(monkeypatch: Any) -> None:
    posts: list[dict[str, Any]] = []
    _drive(_pulling_card(), posts, monkeypatch, ticks=1)

    assert posts[0]["url"] == f"{_URL}{CARD_STATUS_PATH}"
    card = posts[0]["body"]["cards"][0]
    assert card["camera_id"] == "4313"
    assert card["state"] == "pulling"
    assert card["files_total"] == 11
    assert card["current_file"] == "GX010042.MP4"


def test_an_idle_registry_pushes_nothing(monkeypatch: Any) -> None:
    """No card in the reader is the resting state for most of the day.

    Pushing an empty body every 2 s all day would be noise on the consumer and in its
    logs — and there is nothing there for it to learn.
    """
    posts: list[dict[str, Any]] = []
    _drive(CardStatusRegistry(), posts, monkeypatch, ticks=3)
    assert posts == []


def test_removal_pushes_exactly_one_empty_snapshot(monkeypatch: Any) -> None:
    """The card is out — the screen must clear now, not when a TTL expires.

    The consumer holds the snapshot behind a short TTL, so a dead ingest box cannot freeze
    "DO NOT REMOVE" on screen forever. Going silent on removal would then leave the last
    non-empty snapshot up for the rest of that TTL, still reading "copying" for a card
    already in someone's pocket.
    """
    registry = _pulling_card()
    posts: list[dict[str, Any]] = []

    def _remove_after_first(n: int) -> None:
        if n == 1:
            registry.safe_to_remove("4313")
            registry.observe([])  # the card left the reader

    _drive(registry, posts, monkeypatch, ticks=4, on_tick=_remove_after_first)

    bodies = [p["body"]["cards"] for p in posts]
    assert len(bodies) == 2, bodies  # the card, then one empty — then silence
    assert bodies[0][0]["camera_id"] == "4313"
    assert bodies[1] == []


def test_a_failing_push_never_raises_and_keeps_trying(monkeypatch: Any) -> None:
    """The pull is the product. A status banner may not cost a customer their footage."""
    posts: list[dict[str, Any]] = []
    _drive(
        _pulling_card(), posts, monkeypatch, ticks=3,
        client_factory=lambda **_kw: _Recorder(posts, RuntimeError("connection refused")),
    )
    assert posts == []  # every attempt failed, and none of them escaped


def test_a_failing_push_warns_once_not_every_tick(
    monkeypatch: Any, caplog: Any
) -> None:
    """At a 2 s cadence a per-attempt warning buries the log it exists to help read."""
    posts: list[dict[str, Any]] = []
    with caplog.at_level("WARNING", logger="ingest.discovery"):
        _drive(
            _pulling_card(), posts, monkeypatch, ticks=5,
            client_factory=lambda **_kw: _Recorder(
                posts, RuntimeError("connection refused")
            ),
        )

    warnings = [r for r in caplog.records if "status push" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_recovery_is_logged_once_so_the_operator_knows_it_came_back(
    monkeypatch: Any, caplog: Any
) -> None:
    posts: list[dict[str, Any]] = []
    fail = {"now": True}

    class _Flaky(_Recorder):
        async def post(self, url: str, *, json: Any, headers: Any) -> Any:
            if fail["now"]:
                raise RuntimeError("connection refused")
            return await _Recorder.post(self, url, json=json, headers=headers)

    def _clear_after_second(n: int) -> None:
        if n == 2:
            fail["now"] = False

    with caplog.at_level("INFO", logger="ingest.discovery"):
        _drive(
            _pulling_card(), posts, monkeypatch, ticks=4,
            client_factory=lambda **_kw: _Flaky(posts),
            on_tick=_clear_after_second,
        )

    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1
    assert posts, "it resumed pushing once the failure cleared"


def test_the_push_carries_the_service_token(monkeypatch: Any) -> None:
    """The receiver is a public app-to-app endpoint, and this body tells an operator to act
    on hardware — a spoofed ``safe_to_remove`` during a retention sweep (the one moment the
    card is being WRITTEN to) invites a yank that corrupts it."""
    from api.config import get_settings

    monkeypatch.setenv("AUTO_EDIT_API_KEY", "s3cr3t")
    get_settings.cache_clear()
    posts: list[dict[str, Any]] = []
    try:
        _drive(_pulling_card(), posts, monkeypatch, ticks=1)
    finally:
        get_settings.cache_clear()

    assert posts[0]["headers"]["Authorization"] == "Bearer s3cr3t"
