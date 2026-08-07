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
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from scripts.skydiveos_bridge import create_app


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
