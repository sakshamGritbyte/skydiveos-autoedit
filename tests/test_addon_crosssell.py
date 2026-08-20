"""Cross-selling a deliverable TYPE the job's package never produced.

Two questions a desk asks constantly, and they have different answers:

1. "They bought ``video_only`` — can they add the photos?"  The ``photos`` add-on is
   purchasable and the Photo Pack tile is one of the three defaults on every gallery,
   so the *checkout* says yes. The pipeline says no: ``video_only`` skips
   ``extract_photos`` entirely, so there are no stills to unlock. The purchase is
   recorded and the customer receives nothing.
2. "They bought ``photo_only`` — can they add the AI-edited videos?"  There is no
   purchasable item that could: ``PURCHASABLE_ADDONS`` is ``{raw, photos, load_video}``
   and the unlock items move lock STATE, never render anything. A ``photo_only`` job has
   no EDL, so ``/tweak`` and the replay scripts have nothing to re-render either.

Both are the same underlying shape — **the paywall unlocks bytes that already exist**,
and the "film it anyway" doctrine was applied to the CAMERA (whoever filmed gets a
locked edit) but never to the deliverable TYPE. These tests pin the current behaviour so
the gap is visible and a fix has a baseline to move.

The one cross-sell that fully works is **``raw``**: the camera masters exist on every
package (they are the input, not a chosen deliverable), so a ``photo_only`` customer can
buy the raw footage and stream it from the same link. And the gallery now offers **only
what the job can fulfil** (``api.upsell.offerable_tiles``): the Photo Pack tile appears
only for stills that exist and are still locked, the Raw Footage tile only when masters
are staged to stream — so the mis-sell in question 1 can no longer be *advertised*,
even though the unlock endpoint itself still accepts the item.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import PURCHASABLE_ADDONS, create_app, get_queue, get_store
from api.jobs import Entitlement, JobStatus, JobStore, Package, deliverable_names

from .test_api import FakeQueue


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def client(tmp_path: Path, queue: FakeQueue) -> Iterator[TestClient]:
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    with TestClient(app) as c:
        c.jobs_root = tmp_path
        yield c
    app.dependency_overrides.clear()


VIDEO_BASES = ("full_video", "highlights", "freefall")


def _job_with(
    client: TestClient, package: str, *, videos: bool, photos: bool
) -> tuple[str, str]:
    """A rendered job carrying exactly the deliverables its package emits."""
    r = client.post(
        "/jobs",
        json={"customer_name": "Ada Byron", "customer_email": "ada@example.test",
              "package": package, "entitlement": "edited_download"},
    )
    assert r.status_code == 201, r.text
    job_id = str(r.json()["job_id"])

    store = JobStore(client.jobs_root)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    if videos:
        for name in VIDEO_BASES:
            (jd / f"{name}.mp4").write_bytes(b"CLEAN-MASTER")
            outputs[name] = str(jd / f"{name}.mp4")
    if photos:
        (jd / "photos").mkdir(exist_ok=True)
        (jd / "photos" / "0001.jpg").write_bytes(b"JPEG")
        (jd / "photos" / "index.json").write_text(
            '[{"filename": "0001.jpg", "ts": 12.0, "scene": "freefall", "score": 0.9}]'
        )
        outputs["photos"] = str(jd / "photos")
    store.set_pipeline_outputs(job_id, outputs, status=JobStatus.ready)
    return job_id, str(store.load(job_id).gallery_token)


# --------------------------------------------------------------------------- #
# 1. video_only + the photos add-on
# --------------------------------------------------------------------------- #


def test_video_only_never_extracts_a_single_still() -> None:
    """The pipeline's own switch: ``if package.makes_photos`` gates ``extract_photos``,
    and ``video_only`` is the one video package that answers False."""
    assert Package.video_only.makes_videos is True
    assert Package.video_only.makes_photos is False
    assert Package.photo_only.makes_photos is True
    assert Package.photo_only.makes_videos is False


def test_the_photos_addon_is_purchasable_on_a_job_that_has_no_photos(
    client: TestClient,
) -> None:
    """The checkout seam accepts the money.

    ``photos`` is in ``PURCHASABLE_ADDONS``, and the unlock endpoint validates the ITEM,
    never whether this job could fulfil it. So the purchase is recorded, audited against
    a real payment reference, and returns 200 — on a job that extracted no stills.
    """
    assert "photos" in PURCHASABLE_ADDONS
    job_id, _token = _job_with(client, "video_only", videos=True, photos=False)

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_photos_1", "item": "photos"},
    )
    assert r.status_code == 200, r.text
    job = JobStore(client.jobs_root).load(job_id)
    assert job.addons["photos"] == "clover_txn_photos_1"


def test_the_purchased_photos_never_appear_because_none_were_ever_made(
    client: TestClient,
) -> None:
    """…and this is what the customer sees for that payment: nothing.

    The grid is driven by ``photos/index.json``, which ``video_only`` never writes, so the
    Photos tab is omitted from the page entirely — the same page they saw before paying.
    """
    job_id, token = _job_with(client, "video_only", videos=True, photos=False)
    before = client.get(f"/j/{token}").text

    client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_photos_1", "item": "photos"},
    )
    after = client.get(f"/j/{token}").text

    assert 'id="tab-photos"' not in after  # no Photos SECTION is rendered
    assert f"/j/{token}/photos/" not in after  # and no tile URLs
    # Worse: the tab NAV button is unconditional, so the page shows a Photos tab that
    # opens an empty panel — before the purchase and after it, identically.
    assert 'href="#tab-photos"' in before and 'href="#tab-photos"' in after
    # And the endpoint that would serve a still has nothing to serve.
    assert client.get(f"/j/{token}/photos/0001.jpg").status_code == 404


def test_the_gallery_never_advertises_the_photo_pack_it_cannot_deliver(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tile filter's third leg: existence in the JOB, not just in the catalogue.

    ``priced_tiles`` asks the catalogue "will the checkout accept this item?";
    ``offerable_tiles`` asks the job "do the bytes behind it exist?". A ``video_only``
    job extracted no stills, so even an operator-priced, checkout-linked Photo Pack is
    dropped from its gallery — the checkout would take $19 and deliver nothing.
    """
    import sys

    from api.catalogue import PriceCatalogue
    from api.config import get_settings

    app_mod = sys.modules["api.app"]
    monkeypatch.setattr(
        app_mod, "load_price_catalogue",
        lambda _s: PriceCatalogue(items={"photos": 1900}, currency="usd", labels={}),
    )
    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/{job_id}?item={item}")
    get_settings.cache_clear()
    try:
        _job_id, token = _job_with(client, "video_only", videos=True, photos=False)
        page = client.get(f"/j/{token}").text
    finally:
        get_settings.cache_clear()

    assert "Photo Pack" not in page
    assert "item=photos" not in page  # no checkout link for stills that don't exist
    assert 'id="tab-photos"' not in page  # …and no photos section anywhere on the page


def test_a_package_that_DID_shoot_stills_fulfils_the_same_purchase(
    client: TestClient,
) -> None:
    """The contrast that shows the gap is the package, not the add-on machinery.

    On ``selfie`` — identical pipeline, one extra ``extract_photos`` call — the very same
    ``photos`` purchase lands on a real grid.
    """
    job_id, token = _job_with(client, "selfie", videos=True, photos=True)
    client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_photos_1", "item": "photos"},
    )
    page = client.get(f"/j/{token}").text

    assert 'id="tab-photos"' in page
    assert client.get(f"/j/{token}/photos/0001.jpg").content == b"JPEG"


# --------------------------------------------------------------------------- #
# 2. photo_only + the AI-edited videos
# --------------------------------------------------------------------------- #


def test_a_photo_only_job_has_no_video_deliverable_to_sell(client: TestClient) -> None:
    """Nothing was composed and nothing was rendered, so there is no name to unlock."""
    job_id, token = _job_with(client, "photo_only", videos=False, photos=True)
    job = JobStore(client.jobs_root).load(job_id)

    assert set(job.outputs or {}) == {"photos"}
    # `deliverable_names` falls back to the classic single-master name for a job with no
    # video outputs — a name that does not exist on disk here.
    assert deliverable_names(job) == ["final"]
    assert client.get(f"/j/{token}/media/full_video").status_code == 404


def test_no_purchasable_item_can_add_a_video(client: TestClient) -> None:
    """``PURCHASABLE_ADDONS`` is ``{raw, photos, load_video}`` — none of them a video, and
    none of them a RENDER. Every unlock item moves lock state over existing bytes."""
    assert PURCHASABLE_ADDONS == frozenset({"raw", "photos", "load_video"})

    job_id, _token = _job_with(client, "photo_only", videos=False, photos=True)
    for item in ("video", "videos", "edit", "full_video"):
        r = client.post(
            f"/jobs/{job_id}/unlock",
            json={"payment_reference": "clover_txn_1", "item": item},
        )
        assert r.status_code == 400, f"{item!r} was accepted"
        assert "unknown purchasable item" in r.text


def test_the_whole_job_unlock_flips_state_but_renders_nothing(
    client: TestClient,
) -> None:
    """The closest thing to "buy the video": it moves ``entitlement`` and produces no
    file. A ``photo_only`` job unlocked this way still has an empty video grid."""
    job_id, token = _job_with(client, "photo_only", videos=False, photos=True)
    store = JobStore(client.jobs_root)
    store.update(job_id, entitlement=Entitlement.preview_only)

    r = client.post(
        f"/jobs/{job_id}/unlock", json={"payment_reference": "clover_txn_1"}
    )
    assert r.status_code == 200
    job = store.load(job_id)
    assert job.entitlement is Entitlement.edited_download
    assert set(job.outputs or {}) == {"photos"}  # no video appeared
    page = client.get(f"/j/{token}").text
    assert f"/j/{token}/media/" not in page  # not one video card on the page


def test_there_is_no_endpoint_that_changes_a_jobs_package(client: TestClient) -> None:
    """The recovery path is not exposed over the API.

    Re-running the pipeline under a wider package WOULD work — the raw masters are staged
    in ``jobs/<id>/raw/`` and mirrored into the jump archive, so the footage is still
    there — but ``POST /jobs`` only CREATES, ``/tweak`` re-renders an existing EDL (a
    ``photo_only`` job has none), and ``/reject`` re-queues the SAME package. Fixing one
    of these jumps today means editing ``job.json`` by hand and re-queueing.
    """
    job_id, _token = _job_with(client, "photo_only", videos=False, photos=True)

    # No PATCH/PUT on a job at all.
    assert client.patch(f"/jobs/{job_id}", json={"package": "selfie"}).status_code in (
        404, 405,
    )
    assert client.put(f"/jobs/{job_id}", json={"package": "selfie"}).status_code in (
        404, 405,
    )
    # /tweak needs an EDL this job never produced.
    r = client.post(f"/jobs/{job_id}/tweak", json={"clips": []})
    assert r.status_code >= 400


def test_the_manual_recovery_does_work_once_the_package_is_widened(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix that exists today, proven end to end (minus ffmpeg).

    Widen ``photo_only`` → ``selfie`` on the job record and re-run the scene pipeline:
    the same staged masters now compose and render the three videos alongside the stills.
    This is what an operator has to do by hand, and it is the behaviour any future
    "add the video" purchase would need to trigger.
    """
    from api import selfie as selfie_mod
    from api.selfie import run_selfie_pipeline

    job_id, _token = _job_with(client, "photo_only", videos=False, photos=True)
    store = JobStore(client.jobs_root)
    raw = store.raw_dir(job_id)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "GH010001.MP4").write_bytes(b"fake-mp4")
    store.write_booking(job_id, {"customer_name": "Ada Byron"})

    composed: list[dict[str, Any]] = []

    def _compose(*_a: Any, **kw: Any) -> dict[str, Any]:
        composed.append({"use_ai": kw.get("use_ai")})
        return {b: {"clips": []} for b in VIDEO_BASES}

    def _render(job_id_: str, edls: dict[str, Any], *_a: Any, **_kw: Any) -> dict[str, str]:
        out = {}
        for name in edls:
            p = store.dir(job_id_) / f"{name}.mp4"
            p.write_bytes(b"CLEAN-MASTER")
            out[name] = str(p)
        return out

    monkeypatch.setattr(selfie_mod, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(selfie_mod, "classify_files", lambda _d: {})
    monkeypatch.setattr(selfie_mod, "build_scenes", lambda *_a, **_k: {"scenes": []})
    monkeypatch.setattr(selfie_mod, "score_scenes", lambda *_a, **_k: [])
    monkeypatch.setattr(selfie_mod, "compose_edls", _compose)
    monkeypatch.setattr(selfie_mod, "render_outputs", _render)
    monkeypatch.setattr(selfie_mod, "extract_photos", lambda *_a, **_k: [])
    monkeypatch.setattr(selfie_mod, "apply_exclusions", lambda e, _x: e)
    monkeypatch.setattr(selfie_mod, "load_exclusions", lambda *_a, **_k: {})
    monkeypatch.setattr(selfie_mod, "_music_paths", lambda *_a, **_k: {})
    monkeypatch.setattr(selfie_mod, "_ensure_default_music", lambda b, *_a, **_k: b)

    store.update(job_id, package=Package.selfie)  # the one hand edit
    outputs = run_selfie_pipeline(job_id, store=store, jobs_root=client.jobs_root)

    assert set(outputs) >= set(VIDEO_BASES)
    assert composed == [{"use_ai": True}]  # the AI editor, as a selfie booking gets


# --------------------------------------------------------------------------- #
# 3. photo_only + the raw add-on — the one cross-sell that fully works
# --------------------------------------------------------------------------- #


def _stage_raw(client: TestClient, job_id: str, *names: str) -> None:
    raw = JobStore(client.jobs_root).raw_dir(job_id)
    raw.mkdir(parents=True, exist_ok=True)
    for name in names or ("GH010001.MP4",):
        (raw / name).write_bytes(b"RAW-MASTER")


def test_photo_only_can_buy_the_raw_footage_and_it_is_served(
    client: TestClient,
) -> None:
    """Unlike photos-on-video_only, ``raw`` is fulfillable on EVERY package.

    The camera masters exist regardless of what was booked — they are the input, not a
    deliverable the package chose to render. So the purchase lands, the Raw Footage
    section appears on the same link, and the bytes stream.
    """
    job_id, token = _job_with(client, "photo_only", videos=False, photos=True)
    _stage_raw(client, job_id, "GH010001.MP4", "GH010002.MP4")

    # Before the purchase: no section, and the route refuses the bytes.
    assert "Raw footage" not in client.get(f"/j/{token}").text
    assert client.get(f"/j/{token}/raw/GH010001.MP4").status_code == 404

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_raw_1", "item": "raw"},
    )
    assert r.status_code == 200, r.text
    assert JobStore(client.jobs_root).load(job_id).addons["raw"] == "clover_txn_raw_1"

    page = client.get(f"/j/{token}").text
    assert f"/j/{token}/raw/GH010001.MP4" in page
    assert f"/j/{token}/raw/GH010002.MP4" in page

    served = client.get(f"/j/{token}/raw/GH010001.MP4")
    assert served.status_code == 200
    assert served.content == b"RAW-MASTER"


def test_the_purchase_never_the_url_opens_the_raw_files(client: TestClient) -> None:
    """Same shape as the video paywall: without the ``raw`` add-on every path 404s."""
    _job_id, token = _job_with(client, "photo_only", videos=False, photos=True)
    _stage_raw(client, _job_id)
    assert client.get(f"/j/{token}/raw/GH010001.MP4").status_code == 404
    assert client.get(f"/j/{token}/raw/../job.json").status_code in (400, 404)


# --------------------------------------------------------------------------- #
# 4. The gallery offers ONLY what this job can fulfil (offerable_tiles)
# --------------------------------------------------------------------------- #


def _catalogue_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, token: str
) -> str:
    """The gallery with every default tile priced and a live checkout template."""
    import sys

    from api.catalogue import PriceCatalogue
    from api.config import get_settings

    app_mod = sys.modules["api.app"]
    monkeypatch.setattr(
        app_mod, "load_price_catalogue",
        lambda _s: PriceCatalogue(
            items={"photos": 1900, "raw": 2900, "rebook": 1000, "unlock": 3900},
            currency="usd", labels={},
        ),
    )
    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/{job_id}?item={item}")
    get_settings.cache_clear()
    try:
        return client.get(f"/j/{token}").text
    finally:
        get_settings.cache_clear()


def test_the_raw_tile_is_offered_only_when_masters_exist_to_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pruned box (or a job that never staged masters) must not sell a Raw Footage
    section that would render empty: the /j raw route has no S3 fallback."""
    job_id, token = _job_with(client, "selfie", videos=True, photos=True)
    assert "Raw Footage" not in _catalogue_page(client, monkeypatch, token)

    _stage_raw(client, job_id)
    assert "Raw Footage" in _catalogue_page(client, monkeypatch, token)


def test_the_photos_tile_is_offered_only_for_locked_stills(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ways the Photo Pack must vanish, one way it must show.

    * no stills at all (``video_only``) → nothing to sell;
    * stills the customer already OWNS (Path A) → selling them their own photos;
    * stills locked behind the paywall (Path B) → exactly the product the tile is.
    """
    # Path A: photos exist and are the customer's — no tile.
    _job_a, token_a = _job_with(client, "selfie", videos=True, photos=True)
    assert "Photo Pack" not in _catalogue_page(client, monkeypatch, token_a)

    # Path B: same job shape, photos locked — the tile is a real offer.
    job_b, token_b = _job_with(client, "selfie", videos=True, photos=True)
    store = JobStore(client.jobs_root)
    store.update(job_b, entitlement=Entitlement.preview_only)
    page = _catalogue_page(client, monkeypatch, token_b)
    assert "Photo Pack" in page
    assert "item=photos" in page


def test_non_media_tiles_are_never_fulfillability_gated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rebook`` (and any operator-custom key) is fulfilled outside this system —
    only the keys the gallery can reason about are gated."""
    _job_id, token = _job_with(client, "video_only", videos=True, photos=False)
    page = _catalogue_page(client, monkeypatch, token)
    assert "Book Again" in page
    assert "Photo Pack" not in page  # gated
    assert "Raw Footage" not in page  # gated (no masters staged)


def test_a_purchased_tile_still_becomes_a_fulfilled_section_not_an_offer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing rule is untouched: once bought, the tile leaves the row and the
    section appears — fulfillability gating must not resurrect or double-drop it."""
    job_id, token = _job_with(client, "photo_only", videos=False, photos=True)
    _stage_raw(client, job_id)
    client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_raw_1", "item": "raw"},
    )
    page = _catalogue_page(client, monkeypatch, token)
    assert "item=raw" not in page  # no longer offered…
    assert f"/j/{token}/raw/GH010001.MP4" in page  # …because it is now fulfilled


# --------------------------------------------------------------------------- #
# 5. The pruner never deletes a PURCHASED raw section
# --------------------------------------------------------------------------- #


def test_the_pruner_keeps_raw_masters_the_customer_bought(
    client: TestClient,
) -> None:
    """`/j/{code}/raw/…` streams locally with no S3 fallback and the link never
    expires — pruning a purchased job's masters would black out a paid product."""
    from scripts.prune_jobs import prune_job_raw

    store = JobStore(client.jobs_root)
    job_id, _token = _job_with(client, "photo_only", videos=False, photos=True)
    _stage_raw(client, job_id)
    store.update(
        job_id,
        status=JobStatus.delivered,
        addons={"raw": "clover_txn_raw_1"},
        raw_s3_keys={"GH010001.MP4": "raw/4313/2026-08-12/GH010001.MP4"},
    )

    class _ConfirmingS3:
        def head_object(self, *, Bucket: str, Key: str) -> dict[str, int]:
            return {"ContentLength": len(b"RAW-MASTER")}

    freed = prune_job_raw(
        store, store.load(job_id), _ConfirmingS3(), "bucket", dry_run=False
    )

    assert freed == 0
    raw = store.raw_dir(job_id)
    assert (raw / "GH010001.MP4").read_bytes() == b"RAW-MASTER"


def test_the_pruner_still_sweeps_an_unpurchased_jobs_raw(client: TestClient) -> None:
    """The guard is the purchase, not a blanket keep — retention still works."""
    from scripts.prune_jobs import prune_job_raw

    store = JobStore(client.jobs_root)
    job_id, _token = _job_with(client, "photo_only", videos=False, photos=True)
    _stage_raw(client, job_id)
    store.update(
        job_id,
        status=JobStatus.delivered,
        raw_s3_keys={"GH010001.MP4": "raw/4313/2026-08-12/GH010001.MP4"},
    )

    class _ConfirmingS3:
        def head_object(self, *, Bucket: str, Key: str) -> dict[str, int]:
            return {"ContentLength": len(b"RAW-MASTER")}

    freed = prune_job_raw(
        store, store.load(job_id), _ConfirmingS3(), "bucket", dry_run=False
    )

    assert freed > 0
    assert not (store.raw_dir(job_id) / "GH010001.MP4").exists()
