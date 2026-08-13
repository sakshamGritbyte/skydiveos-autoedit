"""The spec-flight load master and its fan-out — the Load 17 scenario.

One camera flyer goes up on an open seat with no assigned customer. His card becomes ONE
load master; everybody manifested on that load is then offered its video, in two tiers:

* bought no media  → their own locked child gallery (own name, own link, own unlock)
* bought media     → a load-video *tile* in the gallery they were already getting,
                     never a second page and never a second email

What these tests pin down is the invariant the whole feature rests on: **the files come
from the load master, the lock state comes from the requesting job.** Get that wrong in
either direction and you either hand the clean video to someone who hasn't paid, or you
unlock four strangers when one of them does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app, get_store
from api.jobs import (
    Entitlement,
    Job,
    JobKind,
    JobStatus,
    JobStore,
    LoadEvidence,
    LoadRosterEntry,
    Package,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prune_jobs  # noqa: E402

MASTER = "master17"

#: Load 17: two media buyers (Daniel, Frank) and two who bought nothing (Priya, Kevin).
ROSTER = [
    LoadRosterEntry(jumper_index=0, customer_name="Daniel", customer_email="dan@x.test",
                    booking_id="b0", bought_media=True),
    LoadRosterEntry(jumper_index=1, customer_name="Priya", customer_email="priya@x.test",
                    booking_id="b1", bought_media=False),
    LoadRosterEntry(jumper_index=2, customer_name="Kevin", customer_email="kev@x.test",
                    booking_id="b2", bought_media=False),
    LoadRosterEntry(jumper_index=3, customer_name="Frank", customer_email="frank@x.test",
                    booking_id="b3", bought_media=True),
]


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(str(tmp_path / "jobs"))


def _master(store: JobStore, *, freefall: bool = True, rendered: bool = True) -> Job:
    """A load master as ``fan_out_load_job`` finds it: approved, rendered, with a roster."""
    job = store.create(
        Job(
            job_id=MASTER,
            status=JobStatus.approved,
            job_kind=JobKind.load_master,
            load_id="L17",
            load_label="Load 17",
            customer_name="Load 17",
            instructor_name="Marc Tremblay",
            entitlement=Entitlement.preview_only,
            jump_date="2026-07-21",
            load_roster=ROSTER,
        )
    )
    jd = store.dir(MASTER)
    if rendered:
        (jd / "full_video.mp4").write_bytes(b"CLEAN-MASTER")
        (jd / "preview_full_video.mp4").write_bytes(b"WATERMARKED")
        job = store.update(MASTER, outputs={"full_video": str(jd / "full_video.mp4")})
    # Shaped like api.selfie.build_scenes writes it: scene entries keyed ``name``.
    scenes = [{"name": "boarding", "duration": 40.0}, {"name": "plane", "duration": 60.0}]
    if freefall:
        scenes.append({"name": "freefall", "duration": 55.0, "exit_offset": 8.0})
    (jd / "scene_manifest.json").write_text(json.dumps({"scenes": scenes, "flagged": []}))
    return job


def _buyer(store: JobStore, job_id: str, jumper_index: int, name: str) -> Job:
    """A media buyer's own job on the same load — already delivered, gallery and all."""
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"THEIR-OWN-EDIT")
    return store.create(
        Job(
            job_id=job_id,
            status=JobStatus.delivered,
            job_kind=JobKind.jump,
            load_id="L17",
            load_label="Load 17",
            jumper_index=jumper_index,
            customer_name=name,
            customer_email=f"{name.lower()}@x.test",
            outputs={"full_video": str(jd / "full_video.mp4")},
        )
    )


@pytest.fixture()
def fan_out(monkeypatch: pytest.MonkeyPatch, store: JobStore):
    """``fan_out_load_job`` with S3 and the delivery queue stubbed out.

    Returns ``(run, uploads, delivered)``: the callable, the recorded S3 uploads, and the
    job ids handed to ``deliver_job``.
    """
    from api import tasks

    uploads: list[tuple[dict, bool]] = []
    delivered: list[str] = []

    monkeypatch.setattr(tasks, "_store", lambda: store)
    monkeypatch.setattr(tasks, "_notify_skydiveos", lambda job: None)
    monkeypatch.setattr(
        "api.delivery.upload_and_link",
        lambda files, *, job_id, settings, s3_client=None, presign=True: (
            uploads.append((dict(files), presign)) or {}
        ),
    )
    monkeypatch.setattr(tasks.deliver_job, "delay", lambda job_id: delivered.append(job_id))
    return tasks.fan_out_load_job, uploads, delivered


# --------------------------------------------------------------------------- #
# Fan-out: who gets what
# --------------------------------------------------------------------------- #


def test_load_17_fans_out_to_children_and_tiles(store: JobStore, fan_out) -> None:
    """The headline scenario: 2 child galleries, 2 tiles, 4 offers, one render."""
    run, uploads, delivered = fan_out
    _master(store)
    daniel = _buyer(store, "danieljob", 0, "Daniel")
    frank = _buyer(store, "frankjob", 3, "Frank")

    run(MASTER)

    children = [j for j in store.list_jobs() if j.job_kind is JobKind.load_child]
    assert sorted(c.customer_name for c in children) == ["Kevin", "Priya"]
    # Every child is locked, points at the master, and owns no files of its own.
    for child in children:
        assert child.entitlement is Entitlement.preview_only
        assert child.source_job_id == MASTER
        assert child.outputs is None
        assert child.gallery_token  # its own link, minted at creation
        assert child.instructor_name == "Marc Tremblay"

    # The two buyers keep their own job untouched except for the load-video pointer —
    # no child, no second email, no status change.
    for buyer in (daniel, frank):
        reloaded = store.load(buyer.job_id)
        assert reloaded.source_job_id == MASTER
        assert reloaded.job_kind is JobKind.jump
        assert reloaded.status is JobStatus.delivered
        assert reloaded.outputs == buyer.outputs  # their own edit, not the load's

    # Only the two children are delivered (i.e. emailed). The buyers are not re-delivered.
    assert sorted(delivered) == sorted(c.job_id for c in children)
    assert store.load(MASTER).status is JobStatus.delivered


def test_master_uploads_durably_but_never_presigned(store: JobStore, fan_out) -> None:
    """Every gallery streaming these bytes is locked, and a presigned URL has no lock."""
    run, uploads, _ = fan_out
    _master(store)

    run(MASTER)

    assert len(uploads) == 1
    files, presign = uploads[0]
    assert "full_video" in files
    assert presign is False


def test_no_freefall_means_no_fan_out(store: JobStore, fan_out) -> None:
    """The guard on a timestamp-only match: no jump in the footage, no offer to the load.

    The load was resolved from the capture instant alone (there is no crew field on a load
    document), so without freefall in the scenes this could be a card filmed on the ground
    between loads — and every customer on that load would be sold a video of nothing.
    """
    run, uploads, delivered = fan_out
    _master(store, freefall=False)

    with pytest.raises(RuntimeError, match="no freefall scene"):
        run(MASTER)

    master = store.load(MASTER)
    assert master.status is JobStatus.failed
    assert "freefall" in (master.error or "")
    assert not [j for j in store.list_jobs() if j.job_kind is JobKind.load_child]
    assert uploads == [] and delivered == []


def test_fan_out_is_idempotent(store: JobStore, fan_out) -> None:
    """A re-run must not open a second gallery — or email one customer twice."""
    run, _, delivered = fan_out
    _master(store)
    _buyer(store, "danieljob", 0, "Daniel")

    run(MASTER)
    first = {j.job_id for j in store.list_jobs()}
    store.update(MASTER, status=JobStatus.approved)  # as a re-queue would leave it
    run(MASTER)

    assert {j.job_id for j in store.list_jobs()} == first
    assert len(delivered) == 2  # the two children, once each


def test_buyer_is_found_by_booking_id_alone(store: JobStore, fan_out) -> None:
    """The PRODUCTION case: SkydiveOS creates jump jobs with no load_id/jumper_index.

    Its ``createJob`` payload carries `booking_id` but neither positional field, so a
    positional-only join would leave every media buyer untiled — a missed sale whose only
    trace is a WARNING line.
    """
    run, _, delivered = fan_out
    _master(store)
    # Daniel's job as SkydiveOS would have created it: booking id, no load slot at all.
    store.create(
        Job(
            job_id="danielprod",
            status=JobStatus.delivered,
            job_kind=JobKind.jump,
            booking_id="b0",
            customer_name="Daniel",
        )
    )

    run(MASTER)

    assert store.load("danielprod").source_job_id == MASTER  # tiled
    children = [j for j in store.list_jobs() if j.job_kind is JobKind.load_child]
    assert sorted(c.customer_name for c in children) == ["Kevin", "Priya"]
    assert "Daniel" not in [c.customer_name for c in children]  # no second link
    assert len(delivered) == 2


def test_buyer_is_found_positionally_when_booking_ids_do_not_match(
    store: JobStore, fan_out
) -> None:
    """The other half: the two sides spell booking ids differently, so position saves it.

    SkydiveOS puts ``booking.bookingNumber`` ("BK-1001") on a jump job while a roster built
    from the raw jumper subdoc would hold the booking ObjectId — they never compare equal.
    """
    run, _, _ = fan_out
    _master(store)
    store.create(
        Job(
            job_id="danielprod",
            status=JobStatus.delivered,
            job_kind=JobKind.jump,
            booking_id="BK-1001",  # ≠ roster's "b0"
            load_id="L17",
            jumper_index=0,
            customer_name="Daniel",
        )
    )

    run(MASTER)

    assert store.load("danielprod").source_job_id == MASTER


def test_a_child_is_never_mistaken_for_a_buyers_own_job(store: JobStore, fan_out) -> None:
    """A child shares its load slot AND booking id with nothing — but guard the kind anyway.

    A re-run must not treat an already-created child as the buyer's own job and stamp a
    pointer onto it.
    """
    run, _, _ = fan_out
    _master(store)
    run(MASTER)
    kinds = {j.job_id: j.job_kind for j in store.list_jobs()}
    for job in store.list_jobs():
        if kinds[job.job_id] is JobKind.load_child:
            assert job.source_job_id == MASTER  # a pointer, not a tile stamp
            assert job.outputs is None


def test_a_nomedia_jumper_who_already_has_a_gallery_gets_a_tile_not_a_second_link(
    store: JobStore, fan_out
) -> None:
    """The case that breaks "one customer, one link" if the tier test is `bought_media`.

    A jumper who bought NOTHING still normally has a gallery: their instructor's handcam
    films every tandem anyway, so `package_and_entitlement_for(None, …, 'instructor')`
    gives them a speculative `selfie` + `preview_only` job with its own token and unlock.
    Branching the fan-out on "did they buy media?" then creates a load_child ON TOP of
    that — two links and two emails to one customer, the same failure class as the
    2026-08-06 four-emails incident.

    The tier test is therefore "do they already have a job of their own?", not "did they
    pay?".
    """
    run, _, delivered = fan_out
    _master(store)
    # B bought nothing, but his instructor's handcam already made him a locked gallery.
    store.create(
        Job(
            job_id="bselfie",
            status=JobStatus.delivered,
            job_kind=JobKind.jump,
            package=Package.selfie,
            entitlement=Entitlement.preview_only,   # speculative — he bought nothing
            load_id="L17",
            jumper_index=1,
            booking_id="b1",
            customer_name="Priya",
            customer_email="priya@x.test",
        )
    )

    run(MASTER)

    # He gets the load video as a TILE in the gallery he already has…
    assert store.load("bselfie").source_job_id == MASTER
    # …and NOT a second gallery.
    children = [j for j in store.list_jobs() if j.job_kind is JobKind.load_child]
    assert [c.customer_name for c in children] == ["Kevin"]  # only the un-filmed one
    assert "bselfie" not in delivered  # and no second email to him


def test_buyer_without_a_job_yet_is_skipped_not_given_a_child(
    store: JobStore, fan_out
) -> None:
    """A media buyer whose own job hasn't appeared gets nothing — a child would be a 2nd link."""
    run, _, delivered = fan_out
    _master(store)  # Daniel and Frank bought media but have no job on disk

    run(MASTER)

    children = [j for j in store.list_jobs() if j.job_kind is JobKind.load_child]
    assert sorted(c.customer_name for c in children) == ["Kevin", "Priya"]
    assert len(delivered) == 2


# --------------------------------------------------------------------------- #
# The gallery invariant: master's files, requester's lock
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(store: JobStore) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


def _child(store: JobStore, job_id: str, name: str, index: int) -> str:
    store.create(
        Job(
            job_id=job_id,
            status=JobStatus.delivered,
            job_kind=JobKind.load_child,
            source_job_id=MASTER,
            load_id="L17",
            load_label="Load 17",
            jumper_index=index,
            customer_name=name,
            customer_email=f"{name.lower()}@x.test",
            entitlement=Entitlement.preview_only,
        )
    )
    return store.ensure_gallery_token(job_id)


def test_child_streams_the_masters_preview_and_unlock_is_isolated(
    store: JobStore, client: TestClient
) -> None:
    """One render, two customers, independent paywalls — the feature in one test."""
    _master(store)
    priya = _child(store, "priyachild", "Priya", 1)
    kevin = _child(store, "kevinchild", "Kevin", 2)

    # Both locked: each streams the MASTER's watermarked preview, not its clean master.
    for token in (priya, kevin):
        resp = client.get(f"/j/{token}/media/full_video")
        assert resp.status_code == 200
        assert resp.content == b"WATERMARKED"

    # Priya pays. Her page flips; Kevin's does not, though both read the same file.
    store.update("priyachild", entitlement=Entitlement.edited_download)
    assert client.get(f"/j/{priya}/media/full_video").content == b"CLEAN-MASTER"
    assert client.get(f"/j/{kevin}/media/full_video").content == b"WATERMARKED"
    # And the master itself was never touched.
    assert store.load(MASTER).entitlement is Entitlement.preview_only


def test_child_page_renders_the_masters_video_under_its_own_name(
    store: JobStore, client: TestClient
) -> None:
    _master(store)
    token = _child(store, "priyachild", "Priya", 1)

    page = client.get(f"/j/{token}").text

    assert "Priya" in page  # her name, not the load's
    assert "Tandem · Jump Day" in page  # honest product line: her jump DAY, from the air
    assert "We filmed it anyway" in page  # the locked eyebrow
    assert "720P PREVIEW" in page
    assert f"/j/{token}/media/full_video" in page


def test_child_gallery_never_leaks_the_master_via_the_load_route(
    store: JobStore, client: TestClient
) -> None:
    """``/load/`` serves the CLEAN cut, so a child must never be able to reach it."""
    _master(store)
    token = _child(store, "priyachild", "Priya", 1)

    # Not even with the add-on recorded: a child's load video is its own main video,
    # gated by its own entitlement at /media/.
    store.update("priyachild", addons={"load_video": "txn-1"})
    assert client.get(f"/j/{token}/load/full_video").status_code == 404


def test_buyer_sees_a_load_video_tile_then_the_section_once_purchased(
    store: JobStore, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daniel: one customer, one link — the load video arrives as a tile in his own page."""
    from api.config import get_settings

    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/{job_id}/{item}")
    get_settings.cache_clear()
    _master(store)
    _buyer(store, "danieljob", 0, "Daniel")
    store.update("danieljob", source_job_id=MASTER, load_label="Load 17")
    token = store.ensure_gallery_token("danieljob")

    page = client.get(f"/j/{token}").text
    assert "Your Load 17 aerial video" in page
    assert "https://pay.test/danieljob/load_video" in page
    assert "Load Video" not in page  # not fulfilled yet — no section
    # His own edit is still the page's main video.
    assert f"/j/{token}/media/full_video" in page

    # Purchase lands (SkydiveOS → POST /unlock item=load_video). The tile becomes a section.
    store.update("danieljob", addons={"load_video": "txn-9"})
    page = client.get(f"/j/{token}").text
    assert "Load Video" in page
    assert f"/j/{token}/load/full_video" in page
    assert "Your Load 17 aerial video" not in page  # purchased tiles leave the row

    # And it serves the master's CLEAN cut — that is exactly what he bought.
    resp = client.get(f"/j/{token}/load/full_video")
    assert resp.status_code == 200 and resp.content == b"CLEAN-MASTER"
    get_settings.cache_clear()


def test_load_video_route_is_gated_on_the_purchase(
    store: JobStore, client: TestClient
) -> None:
    _master(store)
    _buyer(store, "danieljob", 0, "Daniel")
    store.update("danieljob", source_job_id=MASTER)
    token = store.ensure_gallery_token("danieljob")

    assert client.get(f"/j/{token}/load/full_video").status_code == 404  # not bought
    store.update("danieljob", addons={"load_video": "txn-9"})
    assert client.get(f"/j/{token}/load/full_video").status_code == 200
    # An unknown deliverable is refused even with the purchase recorded.
    assert client.get(f"/j/{token}/load/highlights").status_code == 404


def test_a_child_cannot_be_given_footage(store: JobStore, client: TestClient) -> None:
    """Attaching clips to a child would render the load video once per customer."""
    _master(store)
    _child(store, "priyachild", "Priya", 1)

    resp = client.post(
        "/jobs/priyachild/upload", files={"files": ("GX01.MP4", b"data", "video/mp4")}
    )
    assert resp.status_code == 409
    assert "load_child" in resp.json()["detail"]


def test_the_bridges_load_master_payload_is_accepted_by_post_jobs(
    store: JobStore, client: TestClient
) -> None:
    """The cross-service contract, end to end — the bridge's exact body must not 422.

    ``CreateJobRequest`` is ``extra="forbid"``, so an undeclared field here is a 422 per
    clip with nothing but a discovery log line to show for it. The bridge has been bitten
    by exactly that class of silent-422 before (see its module docstring), so the body it
    builds is asserted against the real endpoint rather than only against itself.
    """
    from scripts.skydiveos_bridge import Bridge, PendingJump
    from tests.test_bridge import _load_match

    bridge = object.__new__(Bridge)
    payload = bridge._job_payload(
        PendingJump(match=_load_match(), captured_at="x", is_load_master=True),
        "2026-08-10",
    )

    resp = client.post("/jobs", json=payload)

    assert resp.status_code == 201, resp.text
    created = store.load(resp.json()["job_id"])
    assert created.job_kind is JobKind.load_master
    assert created.load_evidence is LoadEvidence.flight_window
    assert created.load_label == "Load 17"
    assert [r.customer_name for r in created.load_roster] == ["Daniel", "Priya"]
    assert [r.bought_media for r in created.load_roster] == [True, False]
    assert created.customer_email is None  # nothing is ever emailed to a master
    assert created.gallery_token  # minted at birth like any job (never handed out)
    # The roster carries other customers' emails, so it must not be projected outward.
    assert "load_roster" not in resp.json()["job"]


def test_a_child_needs_a_real_source_job(client: TestClient) -> None:
    for body, expected in (
        ({"job_kind": "load_child"}, "source_job_id"),
        ({"job_kind": "load_child", "source_job_id": "nope"}, "not a known job"),
    ):
        resp = client.post("/jobs", json=body)
        assert resp.status_code == 422 and expected in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Delivery: the email Priya actually receives
# --------------------------------------------------------------------------- #


def test_child_delivery_emails_the_link_and_uploads_nothing(
    store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child's delivery is one email. Re-uploading the master per customer is the
    per-customer cost the render-once design exists to avoid."""
    from api.delivery import deliver_to_customer
    from tests.test_delivery import FakeSMTP, _settings

    _master(store)
    _child(store, "priyachild", "Priya", 1)
    job = store.load("priyachild")
    settings = _settings(public_base_url="https://gallery.test")
    smtp = FakeSMTP()

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("a child must not upload anything")

    monkeypatch.setattr("api.delivery.upload_and_link", _boom)
    monkeypatch.setattr("api.delivery._default_s3_client", _boom)

    links = deliver_to_customer(
        job, store, settings, smtp_factory=lambda: smtp  # type: ignore[arg-type,return-value]
    )

    token = job.gallery_token
    assert links == {"gallery": f"https://gallery.test/j/{token}"}
    (msg,) = smtp.sent
    assert msg["To"] == "priya@x.test"
    assert f"https://gallery.test/j/{token}?s=e#tab-video" in msg.get_content()


def test_child_delivery_refuses_without_a_served_gallery(
    store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PUBLIC_BASE_URL → refuse. The legacy S3 page would presign the clean master."""
    from api.delivery import deliver_to_customer
    from tests.test_delivery import FakeSMTP, _settings

    _master(store)
    _child(store, "priyachild", "Priya", 1)

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        deliver_to_customer(
            store.load("priyachild"),
            store,
            _settings(public_base_url=None),
            smtp_factory=FakeSMTP,  # type: ignore[arg-type]
        )


def test_a_load_master_is_never_delivered_to_a_customer(
    store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has no customer. The roster's first name must never receive the load's video."""
    from api import tasks

    _master(store)
    monkeypatch.setattr(tasks, "_store", lambda: store)

    with pytest.raises(RuntimeError, match="fans out"):
        tasks.deliver_job(MASTER)


# --------------------------------------------------------------------------- #
# The status callback (build spec C3) — how SkydiveOS groups a load's jobs
# --------------------------------------------------------------------------- #


def test_callback_carries_job_kind_and_the_load_grouping_fields(
    store: JobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child has no booking, so ``load_id`` is SkydiveOS's ONLY handle on it.

    Without these fields the receiver adopts a child as a booking-less orphan: it can't
    group it under its load, can't render a per-jumper media chip, and can't tell it from
    a jump that lost its booking.
    """
    import httpx

    from api import tasks
    from tests.test_delivery import _settings

    _master(store)
    _child(store, "priyachild", "Priya", 1)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: _settings(jobs_root=str(store.root), skydiveos_api_base="http://skydiveos.test"),
    )
    posted: dict[str, object] = {}

    def _fake_post(url, *, json, headers, timeout):  # noqa: ANN001
        posted.update(json)

        class _R:
            def raise_for_status(self) -> None: ...

        return _R()

    monkeypatch.setattr(httpx, "post", _fake_post)

    tasks._notify_skydiveos(store.load("priyachild"))
    assert posted["job_kind"] == "load_child"
    assert posted["load_id"] == "L17"
    assert posted["jumper_index"] == 1
    assert posted["source_job_id"] == MASTER
    assert "booking_id" not in posted  # nothing was purchased

    posted.clear()
    tasks._notify_skydiveos(store.load(MASTER))
    assert posted["job_kind"] == "load_master"
    assert posted["load_id"] == "L17"
    assert "source_job_id" not in posted  # the master IS the source

    # An ordinary jump still reports the default kind explicitly, so SkydiveOS can
    # branch on presence rather than absence.
    _buyer(store, "danieljob", 0, "Daniel")
    posted.clear()
    tasks._notify_skydiveos(store.load("danieljob"))
    assert posted["job_kind"] == "jump"


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


def test_prune_guard_keeps_a_streamed_masters_renders_and_previews(
    store: JobStore,
) -> None:
    """A locked child's ONLY watchable media is the master's local preview."""
    _master(store)
    master = store.update(MASTER, status=JobStatus.delivered)
    _child(store, "priyachild", "Priya", 1)
    jd = store.dir(MASTER)

    class Confirms:
        """S3 confirms every render — so only the guard can save these files."""

        def head_object(self, Bucket: str, Key: str) -> dict[str, int]:  # noqa: N803
            return {"ContentLength": (jd / Path(Key).name).stat().st_size}

    pointers = {MASTER: [j for j in store.list_jobs() if j.source_job_id == MASTER]}
    freed = prune_jobs.prune_job_renders(
        store, master, Confirms(), "bkt", dry_run=False, pointers=pointers
    )

    assert freed == 0
    assert (jd / "full_video.mp4").exists()  # a child may unlock at any moment
    assert (jd / "preview_full_video.mp4").exists()  # the locked child's only media

    # With nobody streaming it, the master prunes like any other delivered job.
    freed = prune_jobs.prune_job_renders(
        store, master, Confirms(), "bkt", dry_run=False, pointers={}
    )
    assert freed > 0 and not (jd / "full_video.mp4").exists()


# =========================================================================== #
# AUDIT SCENARIO — LOAD 14
#
#   Sophie → Ultimate    B → Selfie    C → Video Only    D → Photo Only
#   E → No media         Cameraman → Marc
#
# Marc has no assigned customer (a spec flight) — the shipped happy path.
# =========================================================================== #

LOAD14_ROSTER = [
    LoadRosterEntry(jumper_index=0, customer_name="Sophie", customer_email="sophie@x.test",
                    booking_id="bk-sophie", bought_media=True),   # ultimum
    LoadRosterEntry(jumper_index=1, customer_name="B", customer_email="b@x.test",
                    booking_id="bk-b", bought_media=True),        # selfie
    LoadRosterEntry(jumper_index=2, customer_name="C", customer_email="c@x.test",
                    booking_id="bk-c", bought_media=True),        # video_only
    LoadRosterEntry(jumper_index=3, customer_name="D", customer_email="d@x.test",
                    booking_id="bk-d", bought_media=True),        # photo_only
    LoadRosterEntry(jumper_index=4, customer_name="E", customer_email="e@x.test",
                    booking_id="bk-e", bought_media=False),       # nothing
]

#: Each non-Ultimate customer's OWN job on Load 14, as their own footage would create it.
LOAD14_OWN_JOBS = [
    ("sophiejob", 0, "Sophie", Package.ultimum, Entitlement.edited_download),
    ("bjob", 1, "B", Package.selfie, Entitlement.edited_download),
    ("cjob", 2, "C", Package.video_only, Entitlement.edited_download),
    ("djob", 3, "D", Package.photo_only, Entitlement.edited_download),
    # E has NO job — nobody filmed E individually.
]


def _load14_master(store: JobStore) -> Job:
    job = store.create(
        Job(
            job_id=MASTER, status=JobStatus.approved, job_kind=JobKind.load_master,
            load_id="L14", load_label="Load 14", customer_name="Load 14",
            instructor_name="Marc Tremblay", entitlement=Entitlement.preview_only,
            jump_date="2026-08-14", load_roster=LOAD14_ROSTER, package=Package.video_only,
        )
    )
    jd = store.dir(MASTER)
    (jd / "full_video.mp4").write_bytes(b"LOAD14-CLEAN")
    (jd / "preview_full_video.mp4").write_bytes(b"LOAD14-WATERMARKED")
    job = store.update(MASTER, outputs={"full_video": str(jd / "full_video.mp4")})
    (jd / "scene_manifest.json").write_text(json.dumps({
        "scenes": [{"name": "boarding"}, {"name": "plane"},
                   {"name": "freefall", "exit_offset": 8.0}], "flagged": [],
    }))
    return job


def _load14_own_jobs(store: JobStore) -> None:
    for job_id, idx, name, pkg, ent in LOAD14_OWN_JOBS:
        jd = store.dir(job_id)
        jd.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, str] = {}
        if pkg.makes_videos or pkg.is_ultimum:
            (jd / "full_video.mp4").write_bytes(f"{name}-OWN-EDIT".encode())
            outputs["full_video"] = str(jd / "full_video.mp4")
        if pkg.makes_photos:
            (jd / "photos").mkdir(exist_ok=True)
            (jd / "photos" / "p1.jpg").write_bytes(b"JPEG")
            (jd / "photos" / "index.json").write_text('[{"filename": "p1.jpg"}]')
            outputs["photos"] = str(jd / "photos")
        store.create(Job(
            job_id=job_id, status=JobStatus.delivered, job_kind=JobKind.jump,
            package=pkg, entitlement=ent, load_id="L14", jumper_index=idx,
            booking_id=f"bk-{name.lower()}", customer_name=name,
            customer_email=f"{name.lower()}@x.test", outputs=outputs or None,
        ))


def test_load14b_spec_flight_fans_out_to_all_five_tiers(store: JobStore, fan_out) -> None:
    """14b — a spec flyer on Load 14: four tiles, one child, ONE render, five customers."""
    run, uploads, delivered = fan_out
    _load14_master(store)
    _load14_own_jobs(store)

    run(MASTER)

    # ── Sophie / B / C / D: a TILE on the gallery they already have ──────────
    for job_id, _idx, name, pkg, _ent in LOAD14_OWN_JOBS:
        j = store.load(job_id)
        assert j.source_job_id == MASTER, f"{name} ({pkg.value}) got no load-video pointer"
        assert j.job_kind is JobKind.jump, f"{name} changed kind"
        assert j.status is JobStatus.delivered, f"{name}'s status was disturbed"
        assert j.entitlement is Entitlement.edited_download, f"{name}'s paywall moved"
        # Their OWN deliverables are theirs — the master's render is not copied in.
        if pkg.makes_videos or pkg.is_ultimum:
            assert (store.dir(job_id) / "full_video.mp4").read_bytes() \
                == f"{name}-OWN-EDIT".encode()

    # ── E: the only child gallery, locked, owning no files ──────────────────
    children = [j for j in store.list_jobs() if j.job_kind is JobKind.load_child]
    assert [c.customer_name for c in children] == ["E"]
    e = children[0]
    assert e.entitlement is Entitlement.preview_only
    assert e.source_job_id == MASTER
    assert e.outputs is None
    assert e.gallery_token

    # ── Emails: E only. Nobody else gets a second link. ─────────────────────
    assert delivered == [e.job_id]

    # ── Rendering economics: ONE master render, ONE upload, no per-customer work ──
    assert len(uploads) == 1
    files, presign = uploads[0]
    assert list(files) == ["full_video"] and presign is False
    # No child or tiled job gained an output of its own.
    assert all(
        j.outputs is None or "LOAD14" not in Path(next(iter(j.outputs.values()))).name
        for j in store.list_jobs() if j.job_id != MASTER
    )


def test_load14b_master_is_single_camera_and_cannot_reach_merge_multicam(
    store: JobStore, fan_out
) -> None:
    """Structural proof, not a doc claim: the master's package can't enter the combo path.

    ``_merge_multicam`` is reachable ONLY from ``compose_combo_edls``, which is called ONLY
    inside ``run_ultimum_pipeline``, which is entered ONLY when ``package.is_ultimum``
    (api/selfie.py:2670). A ``video_only`` master therefore cannot reach it by any route.
    """
    run, _, _ = fan_out
    master = _load14_master(store)

    assert master.package is Package.video_only
    assert master.package.is_ultimum is False
    assert master.package.makes_photos is False  # no stills of strangers
    assert master.package.makes_videos is True

    run(MASTER)

    # The master's outputs are its own single-camera renders; no per-camera scene sets
    # (scenes_instructor/ scenes_external/) were ever built for it.
    jd = store.dir(MASTER)
    assert not (jd / "scene_manifest_instructor.json").exists()
    assert not (jd / "scene_manifest_external.json").exists()
    assert not list(jd.glob("scenes_*"))


def test_load14b_photo_only_customer_gets_tile_then_section(
    store: JobStore, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D bought photos only — no videos of their own. The tile and section must still work."""
    from api.config import get_settings

    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/{job_id}/{item}")
    get_settings.cache_clear()
    _load14_master(store)
    _load14_own_jobs(store)
    store.update("djob", source_job_id=MASTER, load_label="Load 14")
    token = store.ensure_gallery_token("djob")

    page = client.get(f"/j/{token}").text
    assert "Your Load 14 aerial video" in page       # the tile
    assert "https://pay.test/djob/load_video" in page
    assert "Photos" in page                           # their own product still there

    store.update("djob", addons={"load_video": "txn-d"})
    page = client.get(f"/j/{token}").text
    assert "Load Video" in page
    resp = client.get(f"/j/{token}/load/full_video")
    assert resp.status_code == 200 and resp.content == b"LOAD14-CLEAN"
    get_settings.cache_clear()


def test_load14b_unlock_isolation_across_the_whole_load(
    store: JobStore, client: TestClient
) -> None:
    """E unlocks; the master and every other customer's state are untouched."""
    _load14_master(store)
    _load14_own_jobs(store)
    e_token = _child(store, "echild", "E", 4)

    assert client.get(f"/j/{e_token}/media/full_video").content == b"LOAD14-WATERMARKED"
    store.update("echild", entitlement=Entitlement.edited_download)
    assert client.get(f"/j/{e_token}/media/full_video").content == b"LOAD14-CLEAN"

    assert store.load(MASTER).entitlement is Entitlement.preview_only
    for job_id, *_ in LOAD14_OWN_JOBS:
        assert store.load(job_id).paid_at is None
        assert "load_video" not in store.load(job_id).addons


# =========================================================================== #
# THE GALLERY RACE — fixed at the token-minting boundary (api.app.create_job)
# =========================================================================== #


def test_load14_race_customer_job_arriving_after_the_child_ADOPTS_its_gallery(
    store: JobStore, client: TestClient, fan_out, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flyer's card first → E gets a child gallery + email. E's own footage lands
    later → the NEW job adopts the child's token: one customer, ONE link, and the
    already-emailed URL now serves their own gallery (plus the load tile)."""
    from api.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gallery.test")
    get_settings.cache_clear()
    try:
        run, _, _ = fan_out
        _load14_master(store)
        _load14_own_jobs(store)
        run(MASTER)

        child = next(j for j in store.list_jobs() if j.job_kind is JobKind.load_child)
        child_token = child.gallery_token
        assert child.customer_name == "E"

        # E's instructor's card is ingested afterwards → SkydiveOS/bridge POSTs the job.
        resp = client.post("/jobs", json={
            "customer_name": "E", "customer_email": "e@x.test",
            "package": "selfie", "entitlement": "preview_only",
            "load_id": "L14", "jumper_index": 4, "booking_id": "bk-e",
        })
        assert resp.status_code == 201, resp.text
        new_id = resp.json()["job_id"]

        # ONE link: the new job owns the child's token; no second token was minted.
        new_job = store.load(new_id)
        assert new_job.gallery_token == child_token
        assert new_job.source_job_id == MASTER  # the load-video tile rides along
        assert new_job.load_label == "Load 14"  # tile text filled from the child
        retired = store.load(child.job_id)
        assert retired.gallery_token is None
        assert retired.superseded_by == new_id
        live = [j for j in store.list_jobs()
                if j.customer_name == "E" and j.gallery_token]
        assert [j.job_id for j in live] == [new_id]

        # The link E was already emailed resolves to the NEW job's page.
        assert store.find_by_gallery_token(child_token).job_id == new_id
        page = client.get(f"/j/{child_token}")
        assert page.status_code == 200  # still being edited — but E's page, one URL

        # E's entitlement is their own (selfie preview path unchanged): still locked.
        assert new_job.entitlement is Entitlement.preview_only
    finally:
        get_settings.cache_clear()


def test_race_adoption_matches_by_booking_id_alone_the_skydiveos_shape(
    store: JobStore, client: TestClient, fan_out, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production: SkydiveOS creates the jump job with booking_id but no load slot."""
    from api.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gallery.test")
    get_settings.cache_clear()
    try:
        run, _, _ = fan_out
        _load14_master(store)
        run(MASTER)
        child = next(j for j in store.list_jobs()
                     if j.job_kind is JobKind.load_child and j.customer_name == "E")

        resp = client.post("/jobs", json={
            "customer_name": "E", "customer_email": "e@x.test",
            "package": "selfie", "entitlement": "preview_only",
            "booking_id": "bk-e",  # no load_id, no jumper_index
        })
        assert resp.status_code == 201
        new_job = store.load(resp.json()["job_id"])
        assert new_job.gallery_token == child.gallery_token
        assert new_job.source_job_id == MASTER
        # Positional keys back-filled from the child so later fan-out re-runs join.
        assert (new_job.load_id, new_job.jumper_index) == ("L14", 4)
    finally:
        get_settings.cache_clear()


def test_race_adoption_preserves_a_purchase_made_on_the_child(
    store: JobStore, client: TestClient, fan_out, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E PAID on the child's unlock before their own footage landed: the purchase
    must survive adoption as the fulfilled load-video section of the one gallery."""
    from api.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://gallery.test")
    get_settings.cache_clear()
    try:
        run, _, _ = fan_out
        _load14_master(store)
        run(MASTER)
        child = next(j for j in store.list_jobs() if j.job_kind is JobKind.load_child)
        store.update(child.job_id, entitlement=Entitlement.edited_download,
                     paid_at=123.0, payment_reference="txn-e-unlock")

        resp = client.post("/jobs", json={
            "customer_name": "E", "customer_email": "e@x.test",
            "package": "selfie", "entitlement": "preview_only",
            "load_id": "L14", "jumper_index": 4, "booking_id": "bk-e",
        })
        new_job = store.load(resp.json()["job_id"])

        assert new_job.addons["load_video"] == "txn-e-unlock"  # the sale survives
        assert new_job.entitlement is Entitlement.preview_only  # their OWN video: unpaid
        # The purchased load video is reachable through the adopted link.
        token = new_job.gallery_token
        assert client.get(f"/j/{token}/load/full_video").content == b"LOAD14-CLEAN"
    finally:
        get_settings.cache_clear()


def test_adoption_never_fires_for_masters_children_or_unrelated_jobs(
    store: JobStore, client: TestClient, fan_out
) -> None:
    """Adoption is for a customer's own jump job only — and only on a real match."""
    run, _, _ = fan_out
    _load14_master(store)
    run(MASTER)
    child = next(j for j in store.list_jobs() if j.job_kind is JobKind.load_child)

    # A job for a DIFFERENT customer (other booking, other slot) mints its own token.
    resp = client.post("/jobs", json={
        "customer_name": "Zoe", "booking_id": "bk-zoe", "load_id": "L14",
        "jumper_index": 9,
    })
    new_job = store.load(resp.json()["job_id"])
    assert new_job.gallery_token and new_job.gallery_token != child.gallery_token
    assert store.load(child.job_id).superseded_by is None  # child untouched


def test_a_superseded_child_is_never_delivered(
    store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow race inside the race: adoption lands while the child's delivery
    task is still queued. Delivering it would email a dead link — skip instead."""
    from api import tasks

    _master(store)
    _child(store, "priyachild", "Priya", 1)
    store.update("priyachild", status=JobStatus.approved, gallery_token=None,
                 superseded_by="her-real-job")
    monkeypatch.setattr(tasks, "_store", lambda: store)

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("a superseded child must never be delivered")

    monkeypatch.setattr("api.delivery.deliver_to_customer", _boom, raising=False)
    assert tasks.deliver_job("priyachild") == "priyachild"
    assert store.load("priyachild").status is JobStatus.approved  # untouched
