"""The bridge's mixed-jump half: two cards, one job, one link.

At a dropzone the instructor's card goes into the reader first and the cameraman's turns
up minutes or hours later — two separate notifications, two separate settle windows. The
product requirement is ONE gallery link and ONE email, so the second card must join the
job the first one opened. Three things are pinned here:

1. **The second card attaches instead of creating a second job.** Without a per-jump
   record the bridge's only dedupe key is the ``s3_key``, so the later card started a
   fresh job — a second render, a second link and a second "your video is ready" email to
   one customer (the 2026-08-06 incident, in its slower form).
2. **The role comes from each clip's OWN match.** A pending jump keeps only the first
   clip's match, and on a mixed jumper the two cards resolve to the same jumper with
   *different* roles. When both are plugged inside one debounce window they land in the
   same pending jump, so the role has to be remembered per clip — otherwise the
   cameraman's footage stages under the instructor's folder and is edited, and entitled,
   as the wrong product.
3. **An ordinary jump is unchanged.** No ``media_refs`` → no ``camera_role`` on the
   upload, one flat ``raw/``, and the ``POST /jobs`` body carries no new field.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.skydiveos_bridge import Bridge, load_state

# --------------------------------------------------------------------------- #
# A bridge with its HTTP + S3 hops replaced by recorders.
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingClient:
    """Stands in for ``httpx.Client``, recording every call the bridge makes."""

    def __init__(self, calls: list[dict[str, Any]], job_id: str) -> None:
        self._calls = calls
        self._job_id = job_id

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def post(
        self, url: str, *, json: Any = None, files: Any = None,
        data: Any = None, timeout: Any = None,
    ) -> _Response:
        self._calls.append(
            {
                "url": url, "json": json, "data": data,
                "files": [f[1][0] for f in (files or [])],
            }
        )
        return _Response({"job_id": self._job_id})


def _mixed_match(role: str = "instructor") -> Any:
    """A jumper holding a paid handcam package and a spec camera-flyer twin.

    ``role`` is the slot the staff member whose card this is filled — what decides which
    of the two products this footage feeds, and therefore whether its edit is watermarked.
    """
    from ingest.match import MatchResult, MediaRefSpec

    return MatchResult(
        role=role, staff_id="staff-1", staff_name="Marc Tremblay",
        load_id="load-7", load_number=7, jumper_index=2,
        booking_id="bk-1",
        customer_email="ada@example.com", customer_name="Ada Byron",
        media_package="video-photos", video_type="both",
        package="selfie", entitlement="edited_download",
        media_refs=[
            MediaRefSpec(role="instructor", package="selfie",
                         entitlement="edited_download"),
            MediaRefSpec(role="external", package="external",
                         entitlement="preview_only"),
        ],
    )


def _plain_match() -> Any:
    """An ordinary single-product jump — no ``media_refs``."""
    from ingest.match import MatchResult

    return MatchResult(
        role="instructor", staff_id="staff-1", staff_name="Marc Tremblay",
        load_id="load-7", load_number=7, jumper_index=2,
        customer_email="ada@example.com", customer_name="Ada Byron",
        package="selfie", entitlement="edited_download",
    )


class _Bridge:
    """A real :class:`Bridge` with Mongo/S3/HTTP construction skipped."""

    def __init__(self, tmp_path: Path, monkeypatch: Any, job_id: str = "job-abc") -> None:
        import httpx

        self.calls: list[dict[str, Any]] = []
        self.downloaded: list[str] = []

        bridge = object.__new__(Bridge)
        bridge.api = "http://localhost:8000"
        bridge.debounce_s = 900.0
        bridge.dev_debounce = False
        bridge.pending = {}
        bridge.state_path = tmp_path / "_bridge_state.json"
        bridge.state = load_state(bridge.state_path)
        bridge.settings = type("_S", (), {"s3_bucket": "bucket", "jobs_root": str(tmp_path)})()
        # Only ``_to_local`` is reached by _create_and_attach; the notify test installs
        # its own ``resolve`` on this instance.
        bridge.matcher = type(
            "_M", (), {"_to_local": lambda self, at: datetime(2026, 8, 12)}
        )()
        downloaded = self.downloaded

        class _S3:
            def download_file(self, _bucket: str, key: str, dest: str) -> None:
                downloaded.append(key)
                Path(dest).write_bytes(b"\x00")

        bridge._s3 = _S3()
        monkeypatch.setattr(
            httpx, "Client",
            lambda **_kw: _RecordingClient(self.calls, job_id),
        )
        self.bridge = bridge

    # -- convenience ---------------------------------------------------- #

    def flush(self, match: Any, keys: list[str], *, roles: dict[str, str] | None = None) -> None:
        """Run one settle-window flush with ``keys`` as the jump's clips."""
        from scripts.skydiveos_bridge import PendingJump

        jump = PendingJump(
            match=match, captured_at="2026-08-12T14:12:00+00:00",
            clips=[{"s3_key": k, "camera_id": "4313"} for k in keys],
            clip_roles={k: (roles or {}).get(k, match.role) for k in keys},
        )
        self.bridge._create_and_attach(jump)

    @property
    def creates(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["url"].endswith("/jobs")]

    @property
    def uploads(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "/upload" in c["url"]]


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: Any) -> _Bridge:
    return _Bridge(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- #
# 1. Two cards, one job.
# --------------------------------------------------------------------------- #


def test_the_first_card_creates_the_job_with_both_media_refs(bridge: _Bridge) -> None:
    """The job knows a spec twin is coming before its card exists.

    That is what seeds ``deliverable_access`` correctly and makes the paid edit ship
    without waiting: the second product joins the same gallery whenever it turns up.
    """
    bridge.flush(_mixed_match("instructor"), ["raw/4313/2026-08-12/GX010001.MP4"])

    assert len(bridge.creates) == 1
    body = bridge.creates[0]["json"]
    assert body["media_refs"] == [
        {"role": "instructor", "package": "selfie", "entitlement": "edited_download"},
        {"role": "external", "package": "external", "entitlement": "preview_only"},
    ]
    # POST /jobs validates that these mirror the PRIMARY ref (the paid one).
    assert (body["package"], body["entitlement"]) == ("selfie", "edited_download")


def test_the_cameramans_card_attaches_to_the_same_job(bridge: _Bridge) -> None:
    """One customer, one gallery link, one email — the product requirement.

    The two cards are plugged in minutes or hours apart, so the second arrives long after
    the first flushed. Keyed on the *jump*, not the clip, it joins the existing job.
    """
    bridge.flush(_mixed_match("instructor"), ["raw/4313/2026-08-12/GX010001.MP4"])
    bridge.flush(_mixed_match("external"), ["raw/9977/2026-08-12/GX010001.MP4"])

    assert len(bridge.creates) == 1  # NOT two jobs
    assert len(bridge.uploads) == 2
    assert [u["data"]["camera_role"] for u in bridge.uploads] == ["instructor", "external"]
    assert all(u["url"].endswith("/jobs/job-abc/upload") for u in bridge.uploads)


def test_the_jump_record_survives_a_bridge_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The gap between two cards can be hours — longer than the bridge's uptime.

    Held only in memory, a restart between the two cards would lose the job and open a
    second one, which is precisely the failure this record exists to prevent.
    """
    first = _Bridge(tmp_path, monkeypatch, job_id="job-abc")
    first.flush(_mixed_match("instructor"), ["raw/4313/2026-08-12/GX010001.MP4"])

    second = _Bridge(tmp_path, monkeypatch, job_id="job-SHOULD-NOT-BE-CREATED")
    second.flush(_mixed_match("external"), ["raw/9977/2026-08-12/GX010001.MP4"])

    assert second.creates == []
    assert second.uploads[0]["url"].endswith("/jobs/job-abc/upload")
    persisted = json.loads((tmp_path / "_bridge_state.json").read_text())
    assert persisted["jumps"] == {"load-7:2": "job-abc"}


def test_two_customers_on_one_load_keep_separate_jobs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The record is keyed per jump, so it can never fold two customers into one job."""
    b = _Bridge(tmp_path, monkeypatch)
    ada = _mixed_match("instructor")
    grace = _mixed_match("instructor").model_copy(
        update={"jumper_index": 5, "customer_name": "Grace"}
    )
    b.flush(ada, ["raw/4313/2026-08-12/GX010001.MP4"])
    b.flush(grace, ["raw/4313/2026-08-12/GX010002.MP4"])

    assert len(b.creates) == 2
    assert set(b.bridge.state["jumps"]) == {"load-7:2", "load-7:5"}


# --------------------------------------------------------------------------- #
# 2. The role is per clip, not per pending jump.
# --------------------------------------------------------------------------- #


def test_both_cards_inside_one_settle_window_keep_their_own_roles(
    bridge: _Bridge,
) -> None:
    """The 15-minute window is easily long enough to hold both cards.

    They share a pending jump (same load, same jumper), which keeps only the FIRST
    clip's match — so without a per-clip role the cameraman's footage would stage under
    ``raw/instructor/`` and be rendered clean as the edit the customer paid for.
    """
    handcam = "raw/4313/2026-08-12/GX010001.MP4"
    cameraman = "raw/9977/2026-08-12/GX010001.MP4"
    bridge.flush(
        _mixed_match("instructor"), [handcam, cameraman],
        roles={handcam: "instructor", cameraman: "external"},
    )

    assert len(bridge.creates) == 1
    by_role = {u["data"]["camera_role"]: u["files"] for u in bridge.uploads}
    assert by_role == {
        "instructor": ["GX010001.MP4"], "external": ["GX010001.MP4"],
    }
    assert set(bridge.downloaded) == {handcam, cameraman}


def test_several_clips_of_one_camera_are_attached_in_one_call(bridge: _Bridge) -> None:
    """A chaptered 4 GB master is several files but ONE render — one multipart POST."""
    keys = [f"raw/4313/2026-08-12/GX01000{n}.MP4" for n in (1, 2, 3)]
    bridge.flush(_mixed_match("instructor"), keys)

    assert len(bridge.uploads) == 1
    assert bridge.uploads[0]["files"] == ["GX010001.MP4", "GX010002.MP4", "GX010003.MP4"]


# --------------------------------------------------------------------------- #
# 3. An ordinary jump is unchanged.
# --------------------------------------------------------------------------- #


def test_a_single_product_jump_sends_no_media_refs_and_no_camera_role(
    bridge: _Bridge,
) -> None:
    """Byte-identical to before: nothing new in the body, flat ``raw/``."""
    bridge.flush(_plain_match(), ["raw/4313/2026-08-12/GX010001.MP4"])

    assert "media_refs" not in bridge.creates[0]["json"]
    assert bridge.uploads[0]["data"] is None


def test_a_late_clip_of_an_ordinary_jump_no_longer_opens_a_second_job(
    bridge: _Bridge,
) -> None:
    """The 2026-08-06 incident, in its slow form — now closed for every job.

    A clip whose S3 upload outran the settle window used to arrive after the job was
    created and start a fresh one: a second render and a second email. It attaches
    instead; ``Job.processing_dispatched`` then keeps the render exactly-once, so the
    late clip changes nothing rather than duplicating everything.
    """
    bridge.flush(_plain_match(), ["raw/4313/2026-08-12/GX010001.MP4"])
    bridge.flush(_plain_match(), ["raw/4313/2026-08-12/GX010002.MP4"])

    assert len(bridge.creates) == 1
    assert len(bridge.uploads) == 2


def test_a_load_master_is_never_folded_into_the_jump_record(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A master is keyed by its LOAD and has no second camera by construction.

    Recording it under a jump key would let a later customer clip on the same load
    attach to the master — five strangers' footage in one job.
    """
    from ingest.match import LoadJumper, LoadMatchResult
    from scripts.skydiveos_bridge import PendingJump

    b = _Bridge(tmp_path, monkeypatch)
    master = LoadMatchResult(
        staff_id="staff-9", staff_name="Yves", load_id="load-7", load_number=7,
        business_day="2026-08-12",
        jumpers=[LoadJumper(jumper_index=0, customer_name="Ada", package="selfie")],
    )
    b.bridge._create_and_attach(
        PendingJump(
            match=master, captured_at="2026-08-12T14:12:00+00:00",
            clips=[{"s3_key": "raw/9977/2026-08-12/GX010001.MP4", "camera_id": "9977"}],
            is_load_master=True,
        )
    )

    assert b.bridge.state["jumps"] == {}
    assert b.creates[0]["json"]["job_kind"] == "load_master"
    assert b.uploads[0]["data"] is None  # a master's clips are not role-staged


# --------------------------------------------------------------------------- #
# The notify path: a mixed jumper's clips still group by slot.
# --------------------------------------------------------------------------- #


def test_both_cards_notified_land_in_one_pending_jump_with_their_roles(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """End to end from the notify: same slot → one pending jump, roles remembered."""
    b = _Bridge(tmp_path, monkeypatch)
    answers = iter([_mixed_match("instructor"), _mixed_match("external")])
    b.bridge.matcher.resolve = lambda c, a, *, clip_ref=None: next(answers)  # type: ignore[assignment]

    async def scenario() -> None:
        for key in ("raw/4313/2026-08-12/GX010001.MP4", "raw/9977/2026-08-12/GX010001.MP4"):
            r = await b.bridge.raw_upload(
                {"s3_key": key, "camera_id": "4313",
                 "captured_at": "2026-08-12T14:12:00+00:00"}
            )
            assert r["status"] == "accepted"

    asyncio.run(scenario())

    assert list(b.bridge.pending) == [("load-7", 2)]
    jump = b.bridge.pending[("load-7", 2)]
    assert list(jump.clip_roles.values()) == ["instructor", "external"]
