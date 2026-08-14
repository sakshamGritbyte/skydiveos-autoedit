"""Foundations for a MIXED job: one jump, one gallery link, two media refs.

The case this exists for: a customer buys the instructor's handcam edit (``selfie``,
paid) and the desk *also* manifests a speculative camera-flyer edit (``external``, spec)
on the same jumper. Both sets of footage land on ONE job so the customer gets ONE link —
so the job's single ``entitlement`` scalar can no longer answer "is this locked?".
``Job.deliverable_access`` answers it per deliverable instead.

Four contracts are pinned here, and the first is the most important:

* **An empty ``deliverable_access`` map is byte-identical to the old behaviour.** Every
  job written before this field, and every ordinary single-ref job, must resolve exactly
  as it did — a lock is never introduced or removed by the mere existence of the field.
* A second render pass is **additive**: it must not delete the first pass's outputs
  (the gallery lists ``outputs`` keys, so a wiped key is a deliverable that vanishes
  while its bytes linger).
* Previews are rendered for the **locked** deliverables, which on a mixed job is a
  subset — the job-level question would render none (the job is ``edited_download``
  because the handcam was bought) and the spec edit would then be served clean.
* A presigned URL is minted only for a deliverable the customer **owns**. A presigned
  URL carries no entitlement check and is persisted/archived/forwarded, so this is the
  paywall's outer boundary, not a display concern.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app, get_queue, get_store
from api.config import get_settings
from api.delivery import upload_and_link
from api.jobs import (
    DeliverableAccess,
    Entitlement,
    Job,
    JobStatus,
    JobStore,
    MediaRef,
    Package,
    all_locked,
    any_locked,
    deliverable_name,
    deliverable_names,
    entitlement_for,
    locked_deliverables,
    unlockable_group,
)
from api.preview import PREVIEW_PREFIX, render_job_previews

from .test_api import FakeQueue
from .test_delivery import FakeS3, _settings
from .test_preview import FakeFFmpeg


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def client(tmp_path: Path, queue: FakeQueue) -> Iterator[TestClient]:
    """The same app/store/queue wiring every other endpoint test uses."""
    app = create_app()
    store = JobStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    with TestClient(app) as c:
        c.jobs_root = tmp_path  # stash the root for assertions
        yield c
    app.dependency_overrides.clear()


#: The shape a mixed job has once both passes have run: the paid handcam edit under its
#: plain names, the spec camera-flyer edit under ``external_``-prefixed ones.
PAID_NAMES = ("full_video", "highlights", "freefall")
SPEC_NAMES = ("external_full_video", "external_highlights", "external_freefall")


def _mixed_job(store: JobStore, tmp_path: Path, *, spec_locked: bool = True) -> Job:
    """A job whose handcam edit is owned and whose camera-flyer edit is spec."""
    outputs: dict[str, str] = {}
    for name in (*PAID_NAMES, *SPEC_NAMES):
        path = store.dir("j1") / f"{name}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-master")
        outputs[name] = str(path)
    spec_entitlement = (
        Entitlement.preview_only if spec_locked else Entitlement.edited_download
    )
    return store.create(
        Job(
            job_id="j1",
            customer_name="Priya",
            # The job's own entitlement is the PAID ref's: the customer bought the
            # handcam edit, so anything without an explicit entry is theirs.
            entitlement=Entitlement.edited_download,
            outputs=outputs,
            deliverable_access={
                name: DeliverableAccess(entitlement=spec_entitlement, born_locked=True)
                for name in SPEC_NAMES
            },
        )
    )


# --------------------------------------------------------------------------- #
# entitlement_for — the resolver, and its back-compatibility
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "job_entitlement", [Entitlement.edited_download, Entitlement.preview_only]
)
def test_empty_access_map_resolves_to_the_job_entitlement(
    tmp_path: Path, job_entitlement: Entitlement
) -> None:
    """The back-compat contract: no entries → every name inherits the job's own state.

    This is what makes the field safe to add to a live system: a ``job.json`` written
    before it exists loads with ``{}`` and resolves identically for both values.
    """
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            entitlement=job_entitlement,
            outputs={"full_video": "/x/full_video.mp4", "photos": "/x/photos"},
        )
    )
    assert job.deliverable_access == {}
    assert entitlement_for(job, "full_video") is job_entitlement
    # A name that isn't even a deliverable resolves the same way — the resolver is a
    # lookup with a default, never a membership test.
    assert entitlement_for(job, "anything") is job_entitlement
    assert all_locked(job) is (job_entitlement is Entitlement.preview_only)


def test_explicit_entry_wins_over_the_job_default(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = _mixed_job(store, tmp_path)

    for name in PAID_NAMES:
        assert entitlement_for(job, name) is Entitlement.edited_download
    for name in SPEC_NAMES:
        assert entitlement_for(job, name) is Entitlement.preview_only

    assert locked_deliverables(job) == frozenset(SPEC_NAMES)
    assert any_locked(job) is True
    assert all_locked(job) is False


def test_deliverable_names_excludes_photos_and_falls_back_to_final(
    tmp_path: Path,
) -> None:
    """``photos`` is a directory of stills, and the classic pipeline records no map."""
    store = JobStore(tmp_path)
    with_map = store.create(
        Job(job_id="j1", outputs={"full_video": "/x/a.mp4", "photos": "/x/photos"})
    )
    assert deliverable_names(with_map) == ["full_video"]

    classic = store.create(Job(job_id="j2"))
    assert deliverable_names(classic) == ["final"]


def test_unlockable_group_is_the_born_locked_set_still_locked(tmp_path: Path) -> None:
    """What a group unlock buys — and why a re-run of it is a no-op.

    ``born_locked`` is immutable, so after the spec edit is paid for the group is empty:
    a repeated unlock finds nothing left to flip rather than re-writing state.
    """
    store = JobStore(tmp_path)
    locked_job = _mixed_job(store, tmp_path)
    assert unlockable_group(locked_job) == frozenset(SPEC_NAMES)

    unlocked_job = _mixed_job(JobStore(tmp_path / "b"), tmp_path, spec_locked=False)
    assert unlockable_group(unlocked_job) == frozenset()
    # …and the born_locked flag survives the unlock, so the audit trail does too.
    assert all(e.born_locked for e in unlocked_job.deliverable_access.values())


# --------------------------------------------------------------------------- #
# set_pipeline_outputs — the merge seam
# --------------------------------------------------------------------------- #


def test_outputs_replace_by_default_matching_the_old_behaviour(tmp_path: Path) -> None:
    """``owns=None`` is a wholesale replace — what the four render sites always did."""
    store = JobStore(tmp_path)
    store.create(Job(job_id="j1", outputs={"stale": "/x/stale.mp4"}))

    job = store.set_pipeline_outputs(
        "j1", {"full_video": "/x/fv.mp4"}, status=JobStatus.ready
    )
    assert job.outputs == {"full_video": "/x/fv.mp4"}
    assert job.status is JobStatus.ready


def test_second_pass_preserves_the_first_passs_outputs(tmp_path: Path) -> None:
    """The bug this seam exists to prevent: pass two must not wipe pass one.

    Without it the camera-flyer render would delete the handcam edit from ``outputs``,
    and the gallery — which lists ``outputs`` keys — would stop showing a video the
    customer had already been emailed a link to.
    """
    store = JobStore(tmp_path)
    store.create(Job(job_id="j1"))

    store.set_pipeline_outputs("j1", {n: f"/x/{n}.mp4" for n in PAID_NAMES})
    job = store.set_pipeline_outputs(
        "j1", {n: f"/x/{n}.mp4" for n in SPEC_NAMES}, owns=SPEC_NAMES
    )

    assert set(job.outputs or {}) == {*PAID_NAMES, *SPEC_NAMES}


def test_a_pass_drops_only_its_own_stale_names(tmp_path: Path) -> None:
    """``owns`` is what keeps a vanished deliverable from lingering as a broken card."""
    store = JobStore(tmp_path)
    store.create(
        Job(
            job_id="j1",
            outputs={
                "full_video": "/x/fv.mp4",
                "external_full_video": "/x/efv.mp4",
                "external_highlights": "/x/eh.mp4",
            },
        )
    )

    # The spec pass re-runs and this time produces only one video.
    job = store.set_pipeline_outputs(
        "j1", {"external_full_video": "/x/efv2.mp4"}, owns=SPEC_NAMES
    )

    assert job.outputs == {
        "full_video": "/x/fv.mp4",  # not this pass's to touch
        "external_full_video": "/x/efv2.mp4",  # re-pointed
    }  # external_highlights dropped: owned by this pass, not produced


def test_deliverable_access_merges_rather_than_replaces(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.create(Job(job_id="j1"))

    store.set_deliverable_access(
        "j1", {"external_full_video": DeliverableAccess(entitlement=Entitlement.preview_only)}
    )
    job = store.set_deliverable_access(
        "j1", {"external_highlights": DeliverableAccess(entitlement=Entitlement.preview_only)}
    )

    assert set(job.deliverable_access) == {"external_full_video", "external_highlights"}


# --------------------------------------------------------------------------- #
# Previews — rendered for the locked subset
# --------------------------------------------------------------------------- #


def test_mixed_job_previews_only_the_locked_deliverables(tmp_path: Path) -> None:
    """The leak this closes: the job is ``edited_download``, so the old job-level check
    returned early and the spec edit had NO watermarked bytes to serve."""
    store = JobStore(tmp_path)
    job = _mixed_job(store, tmp_path)
    ffmpeg = FakeFFmpeg()

    rendered = render_job_previews(job, store, _settings(), runner=ffmpeg)

    assert set(rendered) == set(SPEC_NAMES)
    job_dir = store.dir("j1")
    for name in SPEC_NAMES:
        assert (job_dir / f"{PREVIEW_PREFIX}{name}.mp4").is_file()
    for name in PAID_NAMES:
        assert not (job_dir / f"{PREVIEW_PREFIX}{name}.mp4").exists()


def test_wholly_owned_job_still_renders_no_previews(tmp_path: Path) -> None:
    """Path A gains no new work and no new failure mode (unchanged contract)."""
    store = JobStore(tmp_path)
    job = _mixed_job(store, tmp_path, spec_locked=False)
    ffmpeg = FakeFFmpeg()

    assert render_job_previews(job, store, _settings(), runner=ffmpeg) == {}
    assert ffmpeg.commands == []


# --------------------------------------------------------------------------- #
# Delivery — presign only what the customer owns
# --------------------------------------------------------------------------- #


def test_presign_accepts_a_name_collection(tmp_path: Path) -> None:
    """Every file uploads (the durable copy is what makes ``/unlock`` instant); only
    the named ones get a URL."""
    files = {}
    for name in (*PAID_NAMES, *SPEC_NAMES):
        p = tmp_path / f"{name}.mp4"
        p.write_bytes(b"x")
        files[name] = p
    s3 = FakeS3()

    links = upload_and_link(
        files,
        job_id="j1",
        settings=_settings(),
        s3_client=s3,
        presign=set(PAID_NAMES),
    )

    assert set(links) == set(PAID_NAMES)
    assert len(s3.uploads) == len(files)


def test_presign_true_and_false_keep_their_meaning(tmp_path: Path) -> None:
    """The two existing callers (Path A, and the wholly locked Path B) are untouched."""
    p = tmp_path / "full_video.mp4"
    p.write_bytes(b"x")
    files = {"full_video": p}

    assert set(
        upload_and_link(
            files, job_id="j1", settings=_settings(), s3_client=FakeS3(), presign=True
        )
    ) == {"full_video"}
    assert (
        upload_and_link(
            files, job_id="j1", settings=_settings(), s3_client=FakeS3(), presign=False
        )
        == {}
    )


def test_empty_collection_presigns_nothing(tmp_path: Path) -> None:
    """Fail-closed: an empty allow-set must not be read as "no filter, allow all"."""
    p = tmp_path / "full_video.mp4"
    p.write_bytes(b"x")
    s3 = FakeS3()

    links = upload_and_link(
        {"full_video": p},
        job_id="j1",
        settings=_settings(),
        s3_client=s3,
        presign=set(),
    )

    assert links == {}
    assert len(s3.uploads) == 1


# --------------------------------------------------------------------------- #
# Deliverable naming — which ref owns the plain names
# --------------------------------------------------------------------------- #


def test_primary_ref_keeps_plain_names_and_the_other_is_namespaced(
    tmp_path: Path,
) -> None:
    """A mixed job's PAID half must be indistinguishable from a single-product job.

    That is what keeps every existing consumer — the gallery's video meta, the music
    selectors, the archive, `replay_*` — working on the customer's own edit without
    learning about refs.
    """
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            package=Package.selfie,
            entitlement=Entitlement.edited_download,
            media_refs=[
                MediaRef(
                    role="instructor",
                    package=Package.selfie,
                    entitlement=Entitlement.edited_download,
                ),
                MediaRef(
                    role="external",
                    package=Package.external,
                    entitlement=Entitlement.preview_only,
                ),
            ],
        )
    )

    assert job.is_multi_ref is True
    assert job.primary_ref is not None and job.primary_ref.role == "instructor"
    assert deliverable_name(job, "instructor", "full_video") == "full_video"
    assert deliverable_name(job, "external", "full_video") == "external_full_video"
    # Raw clips must be kept apart: the role is what decides which product a clip feeds.
    assert job.staged_by_camera_role is True


def test_a_single_ref_job_is_not_multi_ref_and_uses_plain_names(tmp_path: Path) -> None:
    """One ref carries nothing new, so nothing about the job changes."""
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            package=Package.selfie,
            media_refs=[
                MediaRef(
                    role="instructor",
                    package=Package.selfie,
                    entitlement=Entitlement.edited_download,
                )
            ],
        )
    )
    assert job.is_multi_ref is False
    assert job.staged_by_camera_role is False
    assert deliverable_name(job, "instructor", "freefall") == "freefall"


def test_primary_is_the_paid_ref_whichever_role_it_is(tmp_path: Path) -> None:
    """The customer's own product leads the gallery even when it's the outside camera.

    Deterministic order matters beyond presentation: which ref is primary decides
    deliverable NAMING, so it must not change between the two renders of one job.
    """
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            package=Package.external,
            entitlement=Entitlement.edited_download,
            media_refs=[
                MediaRef(
                    role="instructor",
                    package=Package.selfie,
                    entitlement=Entitlement.preview_only,
                ),
                MediaRef(
                    role="external",
                    package=Package.external,
                    entitlement=Entitlement.edited_download,
                ),
            ],
        )
    )
    assert job.primary_ref is not None and job.primary_ref.role == "external"
    assert deliverable_name(job, "external", "full_video") == "full_video"
    assert deliverable_name(job, "instructor", "full_video") == "instructor_full_video"


def test_all_spec_job_falls_back_to_the_instructor_ref_as_primary(tmp_path: Path) -> None:
    """No purchase at all: the handcam edit still leads, since it films every tandem."""
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            package=Package.selfie,
            entitlement=Entitlement.preview_only,
            media_refs=[
                MediaRef(
                    role="external",
                    package=Package.external,
                    entitlement=Entitlement.preview_only,
                ),
                MediaRef(
                    role="instructor",
                    package=Package.selfie,
                    entitlement=Entitlement.preview_only,
                ),
            ],
        )
    )
    assert job.primary_ref is not None and job.primary_ref.role == "instructor"


# --------------------------------------------------------------------------- #
# Seeding lock state — and never re-locking something paid for
# --------------------------------------------------------------------------- #


def test_seeding_never_relocks_a_paid_deliverable(tmp_path: Path) -> None:
    """The money bug this guards: a replay must not take back a bought edit.

    A re-render (an instructor tweak, a replay) re-runs the seed, and the ref's BIRTH
    entitlement is still ``preview_only`` — writing that back would re-lock a video the
    customer already paid for. ``born_locked`` is preserved either way, so the audit trail
    and the purchasable group survive.
    """
    from api.selfie import _seed_deliverable_access

    store = JobStore(tmp_path)
    store.create(
        Job(
            job_id="j1",
            deliverable_access={
                "external_full_video": DeliverableAccess(
                    entitlement=Entitlement.edited_download,
                    born_locked=True,
                    paid_at=1_700_000_000.0,
                    payment_reference="clover_txn_9f21c7",
                ),
                "external_highlights": DeliverableAccess(
                    entitlement=Entitlement.preview_only, born_locked=True
                ),
            },
        )
    )
    spec_ref = MediaRef(
        role="external", package=Package.external, entitlement=Entitlement.preview_only
    )

    _seed_deliverable_access(
        store, "j1", {"external_full_video", "external_highlights"}, spec_ref
    )

    access = store.load("j1").deliverable_access
    paid = access["external_full_video"]
    assert paid.entitlement is Entitlement.edited_download  # NOT walked back
    assert paid.payment_reference == "clover_txn_9f21c7"  # audit trail intact
    assert paid.born_locked is True
    # The one that was never bought is re-asserted as locked, as it should be.
    assert access["external_highlights"].entitlement is Entitlement.preview_only


def test_seeding_marks_a_spec_refs_deliverables_born_locked(tmp_path: Path) -> None:
    from api.selfie import _seed_deliverable_access

    store = JobStore(tmp_path)
    store.create(Job(job_id="j1"))
    _seed_deliverable_access(
        store,
        "j1",
        {"external_full_video"},
        MediaRef(
            role="external", package=Package.external, entitlement=Entitlement.preview_only
        ),
    )
    entry = store.load("j1").deliverable_access["external_full_video"]
    assert entry.entitlement is Entitlement.preview_only
    assert entry.born_locked is True


def test_seeding_a_paid_ref_records_it_unlocked_and_not_born_locked(tmp_path: Path) -> None:
    """Written for every ref, not just the spec one, so the map is a complete statement —
    and so a paid deliverable can never be swept into an unlock group."""
    from api.selfie import _seed_deliverable_access

    store = JobStore(tmp_path)
    store.create(Job(job_id="j1"))
    _seed_deliverable_access(
        store,
        "j1",
        {"full_video"},
        MediaRef(
            role="instructor", package=Package.selfie, entitlement=Entitlement.edited_download
        ),
    )
    entry = store.load("j1").deliverable_access["full_video"]
    assert entry.entitlement is Entitlement.edited_download
    assert entry.born_locked is False


# --------------------------------------------------------------------------- #
# The REST surface — creating a mixed job and attaching each camera
# --------------------------------------------------------------------------- #

MIXED_REFS = [
    {"role": "instructor", "package": "selfie", "entitlement": "edited_download"},
    {"role": "external", "package": "external", "entitlement": "preview_only"},
]


def test_create_persists_media_refs(client) -> None:
    r = client.post(
        "/jobs",
        json={
            "customer_name": "Priya",
            "package": "selfie",
            "entitlement": "edited_download",
            "media_refs": MIXED_REFS,
        },
    )
    assert r.status_code == 201, r.text
    job = JobStore(client.jobs_root).load(r.json()["job_id"])
    assert [(x.role, x.package.value, x.entitlement.value) for x in job.media_refs] == [
        ("instructor", "selfie", "edited_download"),
        ("external", "external", "preview_only"),
    ]
    # The job's own two fields still mirror the PAID ref, so nothing that reads them
    # needs to know refs exist.
    assert job.package is Package.selfie
    assert job.entitlement is Entitlement.edited_download


def test_create_refuses_a_primary_ref_that_contradicts_the_top_level_fields(client) -> None:
    """Two answers to "what did the customer buy?" is a 422, not a guess."""
    r = client.post(
        "/jobs",
        json={
            "package": "selfie",
            "entitlement": "edited_download",
            # Both refs speculative → the primary is spec → contradicts entitlement.
            "media_refs": [
                {"role": "instructor", "package": "selfie", "entitlement": "preview_only"},
                {"role": "external", "package": "external", "entitlement": "preview_only"},
            ],
        },
    )
    assert r.status_code == 422
    assert "must mirror the primary ref" in r.text


def test_create_refuses_two_refs_on_one_role(client) -> None:
    r = client.post(
        "/jobs",
        json={
            "package": "selfie",
            "entitlement": "edited_download",
            "media_refs": [
                {"role": "external", "package": "external", "entitlement": "edited_download"},
                {"role": "external", "package": "video_only", "entitlement": "preview_only"},
            ],
        },
    )
    assert r.status_code == 422
    assert "one ref per camera role" in r.text


def _mixed_job_id(client) -> str:
    r = client.post(
        "/jobs",
        json={
            "customer_name": "Priya",
            "package": "selfie",
            "entitlement": "edited_download",
            "media_refs": MIXED_REFS,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["job_id"])


def test_upload_stages_per_role_and_dispatches_only_that_product(client, queue) -> None:
    """The instructor's card must not wait on the cameraman's.

    This is the flow's whole point: the paid edit ships as soon as its own clips land, and
    the speculative one joins the same gallery whenever (or if) its card turns up.
    """
    job_id = _mixed_job_id(client)

    r = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"fake-mp4-bytes", "video/mp4"))],
        data={"camera_role": "instructor"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["camera_role"] == "instructor"
    assert ("media_ref", (job_id, "instructor")) in queue.calls
    # Nothing was enqueued for the camera that hasn't arrived.
    assert ("media_ref", (job_id, "external")) not in queue.calls

    staged = JobStore(client.jobs_root).camera_raw_dir(job_id, "instructor")
    assert [p.name for p in staged.glob("*.MP4")] == ["GH010001.MP4"]

    # The cameraman's card lands later — its own product, its own dispatch.
    r = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GX010007.MP4", b"fake-mp4-bytes", "video/mp4"))],
        data={"camera_role": "external"},
    )
    assert r.status_code == 200, r.text
    assert ("media_ref", (job_id, "external")) in queue.calls


def test_upload_to_a_mixed_job_requires_a_camera_role(client) -> None:
    """Without a role there is no way to tell which product the footage feeds — and
    therefore whether the resulting edit should be watermarked. Refuse."""
    job_id = _mixed_job_id(client)
    r = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"fake-mp4-bytes", "video/mp4"))],
    )
    assert r.status_code == 422
    assert "camera_role" in r.text


def test_upload_refuses_a_role_this_job_has_no_product_for(client) -> None:
    job_id = _mixed_job_id(client)
    # Swap the external ref out so only the instructor product exists…
    store = JobStore(client.jobs_root)
    job = store.load(job_id)
    store.update(job_id, media_refs=[job.media_refs[0], job.media_refs[1]])
    # …then ask for a role that is not a role at all.
    r = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"fake-mp4-bytes", "video/mp4"))],
        data={"camera_role": "ground"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# The paywall boundary: what /j/{code} actually streams, per deliverable
# --------------------------------------------------------------------------- #


def _rendered_mixed(client, *, job_id: str) -> str:
    """Give a mixed job both halves' renders, previews for the locked half, and a token."""
    store = JobStore(client.jobs_root)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name in ("full_video", "external_full_video"):
        (jd / f"{name}.mp4").write_bytes(b"CLEAN-MASTER")
        outputs[name] = str(jd / f"{name}.mp4")
    # Only the locked half has watermarked bytes — that is what the render pass produces.
    (jd / "preview_external_full_video.mp4").write_bytes(b"WATERMARKED")
    store.set_pipeline_outputs(job_id, outputs, status=JobStatus.ready)
    store.set_deliverable_access(
        job_id,
        {
            "external_full_video": DeliverableAccess(
                entitlement=Entitlement.preview_only, born_locked=True
            )
        },
    )
    return str(store.load(job_id).gallery_token)


def test_mixed_gallery_streams_clean_bytes_for_the_bought_edit_only(client) -> None:
    """The leak this closes.

    The job's own entitlement is ``edited_download`` (the handcam edit was bought), so a
    job-level check served the CLEAN external master to a customer who never paid for it.
    The lock is asked per deliverable instead.
    """
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    bought = client.get(f"/j/{token}/media/full_video")
    assert bought.status_code == 200
    assert bought.content == b"CLEAN-MASTER"

    spec = client.get(f"/j/{token}/media/external_full_video")
    assert spec.status_code == 200
    assert spec.content == b"WATERMARKED"  # never the clean master


def test_group_unlock_flips_only_the_born_locked_deliverables(client) -> None:
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_9f21c7", "item": "unlock_external"},
    )
    assert r.status_code == 200, r.text

    # Same URL, clean bytes now — no re-render, no new link.
    assert client.get(f"/j/{token}/media/external_full_video").content == b"CLEAN-MASTER"

    job = JobStore(client.jobs_root).load(job_id)
    entry = job.deliverable_access["external_full_video"]
    assert entry.entitlement is Entitlement.edited_download
    assert entry.payment_reference == "clover_txn_9f21c7"
    assert entry.paid_at is not None
    assert entry.born_locked is True  # the audit trail survives the purchase
    # The job's own fields were NOT touched: the paid half was never in question, and
    # `status` must stay clear of the review/delivery machine.
    assert job.entitlement is Entitlement.edited_download
    assert job.paid_at is None


def test_group_unlock_is_idempotent(client) -> None:
    """SkydiveOS may retry a captured-payment webhook; the second call must be a no-op."""
    job_id = _mixed_job_id(client)
    _rendered_mixed(client, job_id=job_id)
    body = {"payment_reference": "clover_txn_9f21c7", "item": "unlock_external"}

    assert client.post(f"/jobs/{job_id}/unlock", json=body).status_code == 200
    first = JobStore(client.jobs_root).load(job_id).deliverable_access[
        "external_full_video"
    ]
    assert client.post(
        f"/jobs/{job_id}/unlock",
        json={**body, "payment_reference": "a-different-retry-id"},
    ).status_code == 200

    again = JobStore(client.jobs_root).load(job_id).deliverable_access[
        "external_full_video"
    ]
    assert again.paid_at == first.paid_at
    assert again.payment_reference == "clover_txn_9f21c7"  # the original capture stands


def test_legacy_unlock_does_not_open_a_mixed_jobs_spec_half(client) -> None:
    """The reason the group item exists at all.

    The legacy no-target ``unlock`` moves the job's DEFAULT. On a mixed job every locked
    deliverable carries an explicit entry, so that path would take a payment and open
    nothing — it must not be the route SkydiveOS wires the mixed offer to.
    """
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    client.post(f"/jobs/{job_id}/unlock", json={"payment_reference": "clover_txn_1"})

    assert client.get(f"/j/{token}/media/external_full_video").content == b"WATERMARKED"


def test_mixed_state_endpoint_names_the_locked_cards(client) -> None:
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    state = client.get(f"/j/{token}/state").json()
    assert state["locked"] is True  # something is still behind the paywall…
    assert state["locked_deliverables"] == ["external_full_video"]  # …namely this


def test_mixed_page_offers_the_group_and_downloads_the_bought_edit(client) -> None:
    """One page, both states — the customer must see which video is theirs."""
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    page = client.get(f"/j/{token}").text

    assert "720P PREVIEW" in page  # the spec card
    assert "1080P · FULL QUALITY" in page  # the bought card
    # The primary action is a download of THEIR edit, not the offer's.
    assert f"/j/{token}/media/full_video" in page
    # …and the offer for the other edit is on the same page.
    assert "Unlock the outside-camera video" in page


def test_mixed_group_offer_is_text_when_no_checkout_is_configured(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as the unlock CTA and the upsell tiles: never a dead link."""
    monkeypatch.delenv("CHECKOUT_URL_TEMPLATE", raising=False)
    get_settings.cache_clear()
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    page = client.get(f"/j/{token}").text

    assert "Unlock the outside-camera video" in page
    assert "ask at the desk" in page
    assert "<a" not in page.split("Unlock the outside-camera video")[0].rsplit("<div", 1)[-1]


# --------------------------------------------------------------------------- #
# The wire contract SkydiveOS codes against
# --------------------------------------------------------------------------- #


def test_status_callback_carries_the_resolved_per_deliverable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this SkydiveOS gives the speculative edit away for free.

    Its offer page falls back to the job's single ``entitlement`` when it has no
    per-deliverable state — and on a mixed job that reads ``edited_download``, because the
    handcam edit WAS bought. It would conclude the camera-flyer edit is already owned and
    charge nothing for it. Sent fully resolved so nothing over there has to reimplement
    the inherit-from-job rule.
    """
    import api.tasks as tasks

    store = JobStore(tmp_path)
    job = _mixed_job(store, tmp_path)
    sent: dict[str, object] = {}

    class _FakeHTTPX:
        @staticmethod
        def post(url: str, json: dict[str, object], headers: dict[str, str], timeout: float):
            sent.update(json)

    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(skydiveos_api_base="http://sos.test")
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", _FakeHTTPX)

    tasks._notify_skydiveos(job)

    assert sent["entitlement"] == "edited_download"  # the job's own default, as before
    assert sent["deliverable_entitlements"] == {
        "full_video": "edited_download",
        "highlights": "edited_download",
        "freefall": "edited_download",
        "external_full_video": "preview_only",
        "external_highlights": "preview_only",
        "external_freefall": "preview_only",
    }


def test_status_callback_is_unchanged_for_a_single_product_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary job's payload must stay byte-identical to what SkydiveOS receives
    today — the new keys appear only when there is something new to say."""
    import api.tasks as tasks

    store = JobStore(tmp_path)
    job = store.create(
        Job(job_id="j1", outputs={"full_video": "/x/fv.mp4"}, customer_name="Ana")
    )
    sent: dict[str, object] = {}

    class _FakeHTTPX:
        @staticmethod
        def post(url: str, json: dict[str, object], headers: dict[str, str], timeout: float):
            sent.update(json)

    monkeypatch.setattr(
        tasks, "get_settings", lambda: _settings(skydiveos_api_base="http://sos.test")
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", _FakeHTTPX)

    tasks._notify_skydiveos(job)

    assert "deliverable_entitlements" not in sent
    assert "media_refs" not in sent


def test_job_response_projects_the_refs_and_the_resolved_lock(client) -> None:
    """SkydiveOS reconciles what it manifested against what we recorded."""
    job_id = _mixed_job_id(client)
    _rendered_mixed(client, job_id=job_id)

    body = client.get(f"/jobs/{job_id}").json()

    assert body["media_refs"] == [
        {"role": "instructor", "package": "selfie", "entitlement": "edited_download"},
        {"role": "external", "package": "external", "entitlement": "preview_only"},
    ]
    assert body["deliverable_entitlements"] == {
        "full_video": "edited_download",
        "external_full_video": "preview_only",
    }
    # And a single-product job says nothing new.
    plain = client.post("/jobs", json={"customer_name": "Ana", "package": "selfie"}).json()
    assert plain["job"]["media_refs"] == []
    assert plain["job"]["deliverable_entitlements"] == {}


def test_primary_ref_is_order_independent(tmp_path: Path) -> None:
    """Which ref leads decides deliverable NAMING, so array order must not change it.

    Two products with the same lock state (two paid, or two speculative) is where a
    first-match rule went wrong: the same jumper re-created with the array the other way
    round would rename its deliverables, and the gallery — which lists ``outputs`` keys —
    would lose the ones it had already emailed a link to.
    """
    from api.jobs import primary_ref_of

    handcam = MediaRef(
        role="instructor", package=Package.selfie, entitlement=Entitlement.edited_download
    )
    outside = MediaRef(
        role="external", package=Package.external, entitlement=Entitlement.edited_download
    )
    spec_outside = MediaRef(
        role="external", package=Package.external, entitlement=Entitlement.preview_only
    )

    # Both paid: the tie-break is instructor, whichever order they arrive in.
    assert primary_ref_of([handcam, outside]) == primary_ref_of([outside, handcam]) == handcam
    # Paid beats speculative regardless of role or order.
    assert primary_ref_of([spec_outside, handcam]) == handcam
    assert primary_ref_of([outside, MediaRef(
        role="instructor", package=Package.selfie, entitlement=Entitlement.preview_only
    )]) == outside
    assert primary_ref_of([]) is None


def test_the_mixed_page_names_both_products_readably(client) -> None:
    """The customer must read a PRODUCT on every card, not a filename.

    The secondary ref's deliverables are namespaced ``<role>_<name>``, and without a
    label the gallery's fallback renders "External Full Video" — which reads like an
    internal key. Named the same way round as the Ultimate product's per-camera cuts.
    """
    job_id = _mixed_job_id(client)
    store = JobStore(client.jobs_root)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name in ("full_video", "highlights", "freefall",
                 "external_full_video", "external_highlights", "external_freefall"):
        (jd / f"{name}.mp4").write_bytes(b"CLEAN")
        outputs[name] = str(jd / f"{name}.mp4")
        if name.startswith("external_"):
            (jd / f"{PREVIEW_PREFIX}{name}.mp4").write_bytes(b"WM")
    store.set_pipeline_outputs(job_id, outputs, status=JobStatus.ready)
    store.set_deliverable_access(job_id, {
        n: DeliverableAccess(entitlement=Entitlement.preview_only, born_locked=True)
        for n in outputs if n.startswith("external_")
    })
    token = str(store.load(job_id).gallery_token)

    page = client.get(f"/j/{token}").text

    for label in ("Full Video", "Highlights", "Freefall"):
        assert label in page
    for label in ("Full Video — Outside Camera", "Highlights — Outside Camera",
                  "Freefall — Outside Camera"):
        assert label in page
    # The filename-ish fallback must not appear anywhere.
    assert "External Full Video" not in page


# --------------------------------------------------------------------------- #
# Per-camera unlock groups — the two angles are priced and sold separately.
#
# The unscoped group was right while one half was always PAID: only the spec ref's
# deliverables were ever born locked, so "the whole locked group" and "that camera's
# group" named the same set. A jump where NOTHING was bought breaks that — both cameras
# are born locked — and one payment must not hand over both.
# --------------------------------------------------------------------------- #

#: A jump nobody bought media for: the handcam filmed it anyway (every tandem is), and a
#: camera flyer went up on the open seat. Both edits are born locked, independently.
ALL_SPEC_REFS = [
    {"role": "instructor", "package": "selfie", "entitlement": "preview_only"},
    {"role": "external", "package": "external", "entitlement": "preview_only"},
]
HANDCAM_NAMES = ("full_video", "highlights", "freefall")


def _all_spec_job_id(client) -> str:
    r = client.post(
        "/jobs",
        json={
            "customer_name": "Nadia",
            "package": "selfie",
            "entitlement": "preview_only",
            "media_refs": ALL_SPEC_REFS,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["job_id"])


def _rendered_all_spec(client, *, job_id: str) -> str:
    """Both halves rendered, both locked, watermarked bytes for each."""
    store = JobStore(client.jobs_root)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name in (*HANDCAM_NAMES, *SPEC_NAMES):
        (jd / f"{name}.mp4").write_bytes(b"CLEAN-" + name.encode())
        (jd / f"{PREVIEW_PREFIX}{name}.mp4").write_bytes(b"WM-" + name.encode())
        outputs[name] = str(jd / f"{name}.mp4")
    store.set_pipeline_outputs(job_id, outputs, status=JobStatus.ready)
    store.set_deliverable_access(
        job_id,
        {
            name: DeliverableAccess(
                entitlement=Entitlement.preview_only, born_locked=True
            )
            for name in (*HANDCAM_NAMES, *SPEC_NAMES)
        },
    )
    return str(store.load(job_id).gallery_token)


def test_role_for_deliverable_inverts_the_naming_convention(tmp_path: Path) -> None:
    """Derived, not stored, so it cannot drift from ``deliverable_name``."""
    from api.jobs import role_for_deliverable

    store = JobStore(tmp_path)
    job = store.create(
        Job(job_id="j1", media_refs=[MediaRef(**r) for r in ALL_SPEC_REFS])
    )
    assert role_for_deliverable(job, "full_video") == "instructor"  # the primary
    assert role_for_deliverable(job, "photos") == "instructor"
    assert role_for_deliverable(job, "external_full_video") == "external"
    # An ordinary job has no refs, so the question has no answer.
    assert role_for_deliverable(store.create(Job(job_id="j2")), "full_video") is None


def test_unlockable_group_splits_by_camera(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = store.create(
        Job(
            job_id="j1",
            entitlement=Entitlement.preview_only,
            outputs={n: "x" for n in (*HANDCAM_NAMES, *SPEC_NAMES)},
            media_refs=[MediaRef(**r) for r in ALL_SPEC_REFS],
            deliverable_access={
                n: DeliverableAccess(
                    entitlement=Entitlement.preview_only, born_locked=True
                )
                for n in (*HANDCAM_NAMES, *SPEC_NAMES)
            },
        )
    )
    assert unlockable_group(job, role="instructor") == frozenset(HANDCAM_NAMES)
    assert unlockable_group(job, role="external") == frozenset(SPEC_NAMES)
    # Unscoped still means "everything purchasable", which is what the mixed page's
    # single offer relied on and what a bundle item would buy.
    assert unlockable_group(job) == frozenset((*HANDCAM_NAMES, *SPEC_NAMES))


def test_unlocking_one_camera_leaves_the_other_locked(client) -> None:
    """Buying the outside angle must not hand over the handcam's edit for free."""
    job_id = _all_spec_job_id(client)
    token = _rendered_all_spec(client, job_id=job_id)

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_ext_1", "item": "unlock_external"},
    )
    assert r.status_code == 200, r.text

    assert client.get(f"/j/{token}/media/external_full_video").content == (
        b"CLEAN-external_full_video"
    )
    assert client.get(f"/j/{token}/media/full_video").content == b"WM-full_video"

    job = JobStore(client.jobs_root).load(job_id)
    assert unlockable_group(job, role="external") == frozenset()
    assert unlockable_group(job, role="instructor") == frozenset(HANDCAM_NAMES)
    # The job's own fields stay out of it, exactly as for the mixed case.
    assert job.entitlement is Entitlement.preview_only
    assert job.paid_at is None


def test_the_handcam_angle_sells_on_its_own_item(client) -> None:
    job_id = _all_spec_job_id(client)
    token = _rendered_all_spec(client, job_id=job_id)

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_hc_1", "item": "unlock_instructor"},
    )
    assert r.status_code == 200, r.text

    assert client.get(f"/j/{token}/media/full_video").content == b"CLEAN-full_video"
    assert client.get(f"/j/{token}/media/external_full_video").content == (
        b"WM-external_full_video"
    )


def test_both_angles_can_be_bought_in_two_payments(client) -> None:
    """Each capture is recorded against its own deliverables — the audit trail."""
    job_id = _all_spec_job_id(client)
    token = _rendered_all_spec(client, job_id=job_id)

    for item, ref in (("unlock_external", "txn_ext"), ("unlock_instructor", "txn_hc")):
        assert client.post(
            f"/jobs/{job_id}/unlock",
            json={"payment_reference": ref, "item": item},
        ).status_code == 200

    job = JobStore(client.jobs_root).load(job_id)
    assert job.deliverable_access["external_full_video"].payment_reference == "txn_ext"
    assert job.deliverable_access["full_video"].payment_reference == "txn_hc"
    assert unlockable_group(job) == frozenset()  # nothing left to sell
    for name in (*HANDCAM_NAMES, *SPEC_NAMES):
        assert client.get(f"/j/{token}/media/{name}").content.startswith(b"CLEAN-")


def test_a_per_camera_unlock_is_idempotent(client) -> None:
    job_id = _all_spec_job_id(client)
    _rendered_all_spec(client, job_id=job_id)
    body = {"payment_reference": "txn_ext", "item": "unlock_external"}

    assert client.post(f"/jobs/{job_id}/unlock", json=body).status_code == 200
    first = JobStore(client.jobs_root).load(job_id).deliverable_access[
        "external_full_video"
    ]
    assert client.post(
        f"/jobs/{job_id}/unlock", json={**body, "payment_reference": "retry"}
    ).status_code == 200

    again = JobStore(client.jobs_root).load(job_id).deliverable_access[
        "external_full_video"
    ]
    assert (again.paid_at, again.payment_reference) == (
        first.paid_at, "txn_ext",
    )


def test_an_all_spec_page_offers_BOTH_cameras_and_no_whole_job_cta(client) -> None:
    """The bug the per-camera offers also fix.

    Every deliverable here carries an explicit entry, so the whole-job ``unlock`` item
    moves only the job's DEFAULT and buys nothing at all. That CTA must therefore not be
    on this page — and the two real offers must be.
    """
    job_id = _all_spec_job_id(client)
    token = _rendered_all_spec(client, job_id=job_id)

    page = client.get(f"/j/{token}").text
    assert "Unlock the handcam video" in page
    assert "Unlock the outside-camera video" in page
    assert "Unlock full video" not in page  # the whole-job CTA is suppressed


def test_an_ordinary_locked_job_keeps_its_single_whole_job_cta(client) -> None:
    """Back-compat: a plain Path-B job has no ``deliverable_access`` and no refs, so it
    has no per-camera groups and its one ``unlock`` CTA is exactly as before."""
    r = client.post(
        "/jobs",
        json={"customer_name": "Solo", "package": "selfie", "entitlement": "preview_only"},
    )
    job_id = str(r.json()["job_id"])
    store = JobStore(client.jobs_root)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "full_video.mp4").write_bytes(b"CLEAN")
    (jd / f"{PREVIEW_PREFIX}full_video.mp4").write_bytes(b"WM")
    store.set_pipeline_outputs(
        job_id, {"full_video": str(jd / "full_video.mp4")}, status=JobStatus.ready
    )
    token = str(store.load(job_id).gallery_token)

    page = client.get(f"/j/{token}").text
    assert "Unlock full video" in page
    assert "Unlock the handcam video" not in page
    assert "Unlock the outside-camera video" not in page


def test_an_unknown_unlock_item_is_refused_and_names_the_real_ones(client) -> None:
    job_id = _all_spec_job_id(client)
    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "x", "item": "unlock_everything"},
    )
    assert r.status_code == 400
    for item in ("unlock_instructor", "unlock_external"):
        assert item in r.text


def test_gallery_media_is_served_INLINE_not_as_a_download(client) -> None:
    """The player route must not hand the browser an attachment.

    ``FileResponse(filename=...)`` sets ``Content-Disposition: attachment``, and a browser
    given that downloads the file instead of playing it — open such a URL in a tab and the
    tab closes onto a download instead of showing the video. The page's Download button
    does not need it (it uses the HTML ``download`` attribute on a same-origin link), and
    on a LOCKED deliverable an attachment is actively wrong: it offers the watermarked
    preview as a file to keep, from a player the design marks ``nodownload``.
    """
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    for name in ("full_video", "external_full_video"):  # owned and locked
        r = client.get(f"/j/{token}/media/{name}")
        assert r.status_code == 200, name
        disposition = r.headers.get("content-disposition", "")
        assert "attachment" not in disposition, (name, disposition)
        assert r.headers["content-type"] == "video/mp4"


def test_gallery_media_answers_HEAD_not_405(client) -> None:
    """A media URL must answer HEAD — players and CDNs probe with it before streaming.

    FastAPI's ``@app.get`` registers ONLY GET (unlike Starlette's plain ``Route``, which
    adds HEAD when GET is present), so this route used to answer **405** and send players
    into a retry loop that looks like a video reloading forever. The body must be empty and
    the metadata headers must still be right, since that is the whole point of the probe.
    """
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    for name in ("full_video", "external_full_video"):  # owned and locked
        r = client.head(f"/j/{token}/media/{name}")
        assert r.status_code == 200, (name, r.status_code)
        assert r.content == b"", name  # headers only
        assert r.headers["content-type"] == "video/mp4"
        assert r.headers["accept-ranges"] == "bytes"
        assert int(r.headers["content-length"]) > 0, name
        assert "attachment" not in r.headers.get("content-disposition", "")

    # The locked one still reports the PREVIEW's size, never the master's — the
    # entitlement picks the file on a HEAD exactly as it does on a GET.
    master = int(client.head(f"/j/{token}/media/full_video").headers["content-length"])
    preview = int(
        client.head(f"/j/{token}/media/external_full_video").headers["content-length"]
    )
    assert (master, preview) == (len(b"CLEAN-MASTER"), len(b"WATERMARKED"))


def _poll_baseline(page: str) -> str:
    """The lock/addons signature the page's re-render poller starts from."""
    m = re.search(r"var init='([^']*)'", page)
    assert m is not None, "the gallery page carries no re-render poller"
    return m.group(1)


def test_mixed_page_poll_baseline_agrees_with_the_state_endpoint(client) -> None:
    """The page must not reload itself forever on a MIXED jump.

    The poller compares a baseline rendered into the page against what ``/state``
    answers, and reloads when they differ. ``/state`` reports ``any_locked`` — the spec
    half being unpaid keeps the jump "locked", which is what makes the page re-render
    when that half is bought. The page's own ``locked`` flag is ``all_locked``, because
    it drives the treatment. Building the baseline from the treatment flag made the two
    permanently disagree on exactly the jump this feature exists for: a paid edit beside
    a locked one reloaded every 6 s, forever (observed live 2026-08-13).
    """
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    page = client.get(f"/j/{token}").text
    state = client.get(f"/j/{token}/state").json()

    # Half locked, half owned: the treatment flag and /state's answer genuinely differ.
    assert "720P PREVIEW" in page and "1080P · FULL QUALITY" in page
    assert state["locked"] is True

    live = ("locked" if state["locked"] else "open") + "|" + ",".join(state["addons"])
    assert _poll_baseline(page) == live


def test_wholly_owned_page_poll_baseline_is_open(client) -> None:
    """The unlocked single-product page keeps its old signature — no spurious reload."""
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)
    client.post(
        f"/jobs/{job_id}/unlock",
        json={"item": "unlock_external", "payment_reference": "clover_txn_poll"},
    )

    page = client.get(f"/j/{token}").text
    state = client.get(f"/j/{token}/state").json()

    assert state["locked"] is False
    assert _poll_baseline(page) == "open|"


# --------------------------------------------------------------------------- #
# Prices on the page come from the operator's admin catalogue, not from a second
# copy of it on this box (see tests/test_catalogue.py for the unit-level rules).
# --------------------------------------------------------------------------- #


def _patch_catalogue(monkeypatch, catalogue) -> None:
    """Swap the gallery route's price-catalogue reader.

    Via ``sys.modules`` because ``api/__init__.py`` re-exports the FastAPI *instance*
    as ``api.app``, so monkeypatch's dotted lookup finds the app, not the module.
    """
    monkeypatch.setattr(
        sys.modules["api.app"], "load_price_catalogue", lambda settings: catalogue
    )


def test_gallery_prices_come_from_the_admin_catalogue(client, monkeypatch) -> None:
    """One price, one place — the figure shown is the figure the checkout charges.

    Live on 2026-08-13 the page advertised raw footage at ``$29`` (this box's default
    tile) while SkydiveOS charged ``$15``, and offered a Photo Pack and a rebook tile
    the catalogue had no price for, each of which dead-ended the customer on "No price
    is configured for media item …".
    """
    from api.catalogue import PriceCatalogue

    _patch_catalogue(
        monkeypatch,
        PriceCatalogue(items={"unlock": 3900, "unlock_external": 2900, "raw": 1500}),
    )
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    page = client.get(f"/j/{token}").text

    assert "$15" in page  # the catalogue's raw price…
    assert "$29" in page  # …and the outside-camera unlock, on its own CTA
    # The tiles the operator never priced are not offered at all.
    assert "item=photos" not in page
    assert "item=rebook" not in page
    assert "item=raw" in page


def test_the_per_camera_cta_carries_that_cameras_own_price(client, monkeypatch) -> None:
    """Two angles sell separately, so each CTA names its own price — only the
    catalogue can do that; a single ``PREVIEW_PRICE_DISPLAY`` cannot."""
    from api.catalogue import PriceCatalogue

    _patch_catalogue(monkeypatch, PriceCatalogue(items={"unlock_external": 2900}))
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    page = client.get(f"/j/{token}").text

    m = re.search(r"item=unlock_external[^>]*>(.*?)</a>", page, re.S)
    assert m is not None, "the mixed page carries no per-camera unlock CTA"
    assert "$29" in m.group(1)


def test_without_a_catalogue_the_page_is_unchanged(client, monkeypatch) -> None:
    """No shared database, or it didn't answer: the configured row, exactly as before."""
    _patch_catalogue(monkeypatch, None)
    job_id = _mixed_job_id(client)
    token = _rendered_mixed(client, job_id=job_id)

    page = client.get(f"/j/{token}").text

    for key in ("raw", "photos", "rebook"):
        assert f"item={key}" in page, key
    assert "$29" in page and "$19" in page  # the DEFAULT_TILES figures
