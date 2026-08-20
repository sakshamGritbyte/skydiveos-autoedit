"""Every paid+spec COMBINATION, end to end: match → render → gallery → unlock → payout.

``test_match_media_refs`` pins the matcher's *rules* and ``test_mixed_entitlement`` pins
the paywall's *mechanics*, both on the canonical ``selfie + external-spec`` jump. What
neither covers is the combination matrix a real desk actually manifests, and three of the
four shapes below exercise seams the canonical case never touches:

* ``selfie + external_spec`` — the canonical mixed jump (primary = instructor).
* ``external + selfie_spec`` — **the inverted pair**: the paid product is the CAMERAMAN's,
  so the primary ref is ``external``. That inverts every naming decision on the job — the
  outside camera takes the plain deliverable names, the handcam's are namespaced
  ``instructor_*`` — and it inverts the photo owner. It is also the one shape where the
  namespaced half is the one the AI editor runs on.
* ``photo_only + external_spec`` — a **video-less primary**. The paid ref renders no
  videos at all, so every video on the page belongs to the locked camera while the photo
  grid is the customer's own. Nothing else in the suite runs a ``photo_only`` media ref.
* ``photo_only + selfie_spec`` — **two products on ONE camera**, which by contract is not
  a mixed set at all: it defers to the jumper's derived union. Pinned here because the
  deferral has a customer-visible consequence (see the test).

Each combination is driven through the same five layers the footage does:

1. the matcher (add-on docs → ``(package, entitlement, media_refs)``),
2. the **SD-card entry point** — ``resolve_for_staff``, where a filmed QR marker is the
   only identity the box has — so the "insert a card and walk away" flow is proven to
   reach the same answer the serial-keyed pull does,
3. ``POST /jobs`` + the per-role render pass (naming, photo ownership, AI-vs-house cut),
4. the gallery: which bytes each deliverable actually streams,
5. unlock + payout: the checkout item, the group it opens, and what it must leave alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import (
    UNLOCK_GROUP_ITEM_BY_ROLE,
    UNLOCK_ITEM,
    create_app,
    get_queue,
    get_store,
)
from api.jobs import (
    DeliverableAccess,
    Entitlement,
    JobStatus,
    JobStore,
    Package,
    all_locked,
    any_locked,
    deliverable_name,
    entitlement_for,
    locked_deliverables,
    photos_locked,
    role_for_deliverable,
    unlockable_group,
)
from ingest.match import FootageMatcher, MediaRefSpec

from .test_api import FakeQueue

# --------------------------------------------------------------------------- #
# The add-on catalogue docs, as SkydiveOS stores them (structural, never by name).
# --------------------------------------------------------------------------- #


def _inside_video(**over: Any) -> dict[str, Any]:
    """The handcam product: video+photos shot from inside (the instructor's GoPro)."""
    return {"category": "media", "mediaType": "video-photos",
            "videoAngle": "inside", "specOf": None, **over}


def _outside_video(**over: Any) -> dict[str, Any]:
    """The camera-flyer product: video shot from outside."""
    return {"category": "media", "mediaType": "video",
            "videoAngle": "outside", "specOf": None, **over}


def _photos(**over: Any) -> dict[str, Any]:
    """A photos-only add-on — shot by whoever is closest, i.e. the handcam."""
    return {"category": "media", "mediaType": "photos", "specOf": None, **over}


def _spec(doc: dict[str, Any]) -> dict[str, Any]:
    """The $0 spec twin of a product (``specOf`` set → born ``preview_only``)."""
    return {**doc, "specOf": "parent-package-id"}


# --------------------------------------------------------------------------- #
# The combination matrix.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Combo:
    """One booking shape, with everything the five layers need to assert on it."""

    key: str
    addons: tuple[dict[str, Any], ...]
    #: The union SkydiveOS derives and stores on the jumper doc from those add-ons.
    media_package: str
    video_type: str | None
    #: What the matcher must resolve to.
    package: str
    entitlement: str
    refs: tuple[tuple[str, str, str], ...]  # (role, package, entitlement)
    #: Which camera's clips the customer actually paid for (``None`` for the deferred one).
    paid_role: str | None
    spec_role: str | None
    #: The deliverables each side owns once both passes have rendered.
    paid_names: tuple[str, ...] = ()
    spec_names: tuple[str, ...] = ()
    photos_owner: str | None = None
    notes: str = ""

    @property
    def is_mixed(self) -> bool:
        return len(self.refs) > 1

    @property
    def paid(self) -> str:
        """The camera the customer paid for (mixed combos only)."""
        assert self.paid_role is not None
        return self.paid_role

    @property
    def spec(self) -> str:
        """The camera behind the paywall (mixed combos only)."""
        assert self.spec_role is not None
        return self.spec_role


VIDEO_BASES = ("full_video", "highlights", "freefall")

COMBOS: tuple[Combo, ...] = (
    Combo(
        key="selfie+external_spec",
        addons=(_inside_video(), _spec(_outside_video())),
        media_package="video-photos", video_type="both",
        package="selfie", entitlement="edited_download",
        refs=(("instructor", "selfie", "edited_download"),
              ("external", "external", "preview_only")),
        paid_role="instructor", spec_role="external",
        paid_names=VIDEO_BASES,
        spec_names=tuple(f"external_{b}" for b in VIDEO_BASES),
        photos_owner="instructor",
        notes="the canonical mixed jump",
    ),
    Combo(
        key="external+selfie_spec",
        addons=(_outside_video(), _spec(_inside_video())),
        media_package="video-photos", video_type="both",
        # The PAID ref leads regardless of which camera it is — so the outside camera
        # takes the plain names here and the handcam is the namespaced one.
        package="external", entitlement="edited_download",
        refs=(("instructor", "selfie", "preview_only"),
              ("external", "external", "edited_download")),
        paid_role="external", spec_role="instructor",
        paid_names=VIDEO_BASES,
        spec_names=tuple(f"instructor_{b}" for b in VIDEO_BASES),
        photos_owner="external",
        notes="the inverted pair — every naming decision flips",
    ),
    Combo(
        key="photo_only+external_spec",
        addons=(_photos(), _spec(_outside_video())),
        media_package="video-photos", video_type="both",
        package="photo_only", entitlement="edited_download",
        refs=(("instructor", "photo_only", "edited_download"),
              ("external", "external", "preview_only")),
        paid_role="instructor", spec_role="external",
        paid_names=(),  # a photo_only ref renders no videos at all
        spec_names=tuple(f"external_{b}" for b in VIDEO_BASES),
        photos_owner="instructor",
        notes="a video-less primary: every video on the page is the locked one",
    ),
    Combo(
        key="photo_only+selfie_spec",
        addons=(_photos(), _spec(_inside_video())),
        media_package="video-photos", video_type="inside",
        # NOT a mixed set: both add-ons want the instructor's camera, and one camera
        # cannot feed two products. Contract says defer to the union — which reads
        # (video-photos, inside) as a bought selfie package.
        package="selfie", entitlement="edited_download",
        refs=(),
        paid_role="instructor", spec_role=None,
        paid_names=VIDEO_BASES, photos_owner="instructor",
        notes="two products on ONE camera → deferred to the union",
    ),
)

BY_KEY = {c.key: c for c in COMBOS}
MIXED = tuple(c for c in COMBOS if c.is_mixed)


def _combo_id(c: Combo) -> str:
    return c.key


# --------------------------------------------------------------------------- #
# Layer 1 — the matcher: add-on docs → (package, entitlement, media_refs)
# --------------------------------------------------------------------------- #


class _Collection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def find(self, query: dict[str, Any], _projection: Any = None) -> Any:
        """Only ``_id`` is honoured — every other query is a prefilter the code re-checks."""
        wanted = (query or {}).get("_id")
        if not isinstance(wanted, dict) or "$in" not in wanted:
            return iter(list(self._docs))
        ids = set(wanted["$in"])
        return iter([d for d in self._docs if d.get("_id") in ids])

    def find_one(self, q: dict[str, Any]) -> dict[str, Any] | None:
        wanted = q.get("_id")
        ids: list[Any] = list(wanted["$in"]) if isinstance(wanted, dict) else [wanted]
        return next((d for d in self._docs if d.get("_id") in ids), None)


def _jumper_for(combo: Combo, *, with_cameraman: bool = True) -> dict[str, Any]:
    """The ``loads.jumpers[]`` entry this booking produces."""
    jumper: dict[str, Any] = {
        "instructor": "staff-1",
        "customer": "cust-1",
        "booking": "bk-1",
        "mediaPackage": combo.media_package,
        "videoType": combo.video_type,
        "mediaAddOnRefs": [f"addon-{i}" for i in range(len(combo.addons))],
    }
    if with_cameraman:
        jumper["assignedCameraman"] = "staff-2"
    return jumper


def _catalogue_for(combo: Combo) -> list[dict[str, Any]]:
    return [{"_id": f"addon-{i}", **doc} for i, doc in enumerate(combo.addons)]


@pytest.mark.parametrize("combo", COMBOS, ids=_combo_id)
def test_the_addons_resolve_to_the_expected_products(combo: Combo) -> None:
    """The one database read, per combination.

    ``(None, None, [])`` is the deliberate "I can't classify this — use the union" answer,
    and it is what ``photo_only + selfie_spec`` returns because both add-ons want the same
    camera. Everything else must name its refs exactly, in canonical role order.
    """
    package, entitlement, refs = FootageMatcher._media_products_for(
        {"bookingpackages": _Collection(_catalogue_for(combo))}, _jumper_for(combo)
    )
    if not combo.is_mixed:
        assert (package, entitlement, refs) == (None, None, [])
        return
    assert (package, entitlement) == (combo.package, combo.entitlement)
    assert [(r.role, r.package, r.entitlement) for r in refs] == list(combo.refs)


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_primary_ref_mirrors_the_jobs_own_fields(combo: Combo) -> None:
    """``POST /jobs`` refuses any set whose primary disagrees — so the matcher must not
    emit one. The primary is the PAID ref whichever camera it is (the inverted pair is
    the case that proves it isn't just "the instructor")."""
    _p, _e, refs = FootageMatcher._media_products_for(
        {"bookingpackages": _Collection(_catalogue_for(combo))}, _jumper_for(combo)
    )
    from ingest.match import primary_media_ref

    primary = primary_media_ref(refs)
    assert primary is not None
    assert (primary.package, primary.entitlement) == (combo.package, combo.entitlement)
    assert primary.role == combo.paid


def test_two_products_on_one_camera_hand_the_spec_twin_over_clean() -> None:
    """``photo_only + selfie_spec``: the deferral's customer-visible consequence.

    One camera is one raw folder and one render pass carrying one lock state, so a
    photos add-on beside a handcam VIDEO twin cannot be expressed as two refs — the
    contract defers to the jumper's union. The union is ``(video-photos, inside)``, which
    reads as a *bought* selfie package: the customer paid for photos and receives the
    speculative video clean, unwatermarked and downloadable.

    This is pinned, not fixed, deliberately: the alternative — inferring the split from
    two same-camera add-ons — is exactly the guess the module refuses to make everywhere
    else. It is a **revenue** miss (an unbought edit given away), never a delivery
    failure, and never the reverse (a paid customer is never left locked).
    """
    combo = BY_KEY["photo_only+selfie_spec"]
    assert FootageMatcher._media_products_for(
        {"bookingpackages": _Collection(_catalogue_for(combo))}, _jumper_for(combo)
    ) == (None, None, [])

    from ingest.match import package_and_entitlement_for

    # …and this is what the job is then created as.
    assert package_and_entitlement_for(
        combo.media_package, combo.video_type, "instructor"
    ) == ("selfie", "edited_download")


# --------------------------------------------------------------------------- #
# Layer 2 — the SD-card entry point: a filmed QR is the only identity the box has
# --------------------------------------------------------------------------- #


def _sdcard_matcher(combo: Combo, *, with_cameraman: bool = True) -> FootageMatcher:
    """A matcher over a one-load fake DB, reached the way an inserted card reaches it."""
    matcher = FootageMatcher("mongodb://unused-in-test")
    matcher._db = {
        "staffs": _Collection(
            [{"_id": "staff-1", "firstName": "Marc", "lastName": "Tremblay"},
             {"_id": "staff-2", "firstName": "Lena", "lastName": "Ortiz"}]
        ),
        "loads": _Collection(
            [{
                "_id": "load-7", "status": "planned", "loadNumber": 7,
                "businessDate": datetime(2026, 8, 12),
                "departureTime": datetime(2026, 8, 12, 14, 0),
                "jumpers": [_jumper_for(combo, with_cameraman=with_cameraman)],
            }]
        ),
        "customers": _Collection(
            [{"_id": "cust-1", "email": "ada@example.test",
              "firstName": "Ada", "lastName": "Byron"}]
        ),
        "bookingpackages": _Collection(_catalogue_for(combo)),
    }
    return matcher


@pytest.mark.parametrize("combo", COMBOS, ids=_combo_id)
@pytest.mark.parametrize("staff_id", ["staff-1", "staff-2"])
def test_an_inserted_card_resolves_to_the_same_products_either_camera(
    combo: Combo, staff_id: str
) -> None:
    """Insert the card, film a QR, walk away — both cards reach one answer.

    ``resolve_for_staff`` is the QR flow's entry point and shares ``_build_result`` with
    the serial-keyed pull, so the two cards of one jump must agree on the whole product
    set and differ **only** in ``role`` — which is what decides where the footage stages
    and therefore whether that half is watermarked.
    """
    r = _sdcard_matcher(combo).resolve_for_staff(staff_id, "2026-08-12T14:12:00")

    assert r.role == ("instructor" if staff_id == "staff-1" else "external")
    assert r.customer_email == "ada@example.test"
    assert r.load_id == "load-7" and r.jumper_index == 0
    assert (r.package, r.entitlement) == (combo.package, combo.entitlement)
    assert [(x.role, x.package, x.entitlement) for x in r.media_refs] == list(combo.refs)


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_a_card_with_no_cameraman_assigned_never_invents_the_second_camera(
    combo: Combo,
) -> None:
    """The filming roles are read off the SLOTS. With no ``assignedCameraman`` the
    outside product cannot have been filmed, so nothing is synthesised for it — but a
    real outside ADD-ON still resolves, because the customer bought it."""
    r = _sdcard_matcher(combo, with_cameraman=False).resolve_for_staff(
        "staff-1", "2026-08-12T14:12:00"
    )
    # Every ref still comes from a purchased add-on; none was synthesised from a slot.
    assert [x.role for x in r.media_refs] == [role for role, _p, _e in combo.refs]


def test_a_no_purchase_jumper_with_a_flyer_gets_both_cameras_locked() -> None:
    """The SD-card flow's zero-booking case: nothing bought, two cards inserted.

    Both edits are born locked on ONE link, which is what stops the flyer's footage
    landing in the handcam's raw folder and rendering as one edit of two mixed cameras.
    """
    combo = Combo(
        key="nothing-bought", addons=(), media_package="none", video_type=None,
        package="selfie", entitlement="preview_only",
        refs=(("instructor", "selfie", "preview_only"),
              ("external", "external", "preview_only")),
        paid_role=None, spec_role=None,
    )
    r = _sdcard_matcher(combo).resolve_for_staff("staff-2", "2026-08-12T14:12:00")
    assert r.role == "external"
    assert [(x.role, x.package, x.entitlement) for x in r.media_refs] == list(combo.refs)
    # The primary is the instructor's when neither was bought — the handcam is the
    # product a tandem customer recognises, and it exists on every jump.
    assert (r.package, r.entitlement) == ("selfie", "preview_only")


# --------------------------------------------------------------------------- #
# Layer 3 — POST /jobs and the per-role render pass
# --------------------------------------------------------------------------- #


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


def _create(client: TestClient, combo: Combo, *, customer: str = "Ada Byron") -> str:
    body: dict[str, Any] = {
        "customer_name": customer,
        "customer_email": "ada@example.test",
        "package": combo.package,
        "entitlement": combo.entitlement,
    }
    if combo.is_mixed:
        body["media_refs"] = [
            {"role": r, "package": p, "entitlement": e} for r, p, e in combo.refs
        ]
    r = client.post("/jobs", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["job_id"])


@pytest.mark.parametrize("combo", COMBOS, ids=_combo_id)
def test_the_matchers_answer_is_accepted_by_post_jobs(
    client: TestClient, combo: Combo
) -> None:
    """Whatever the matcher resolves must be creatable — the wire validator and the
    matcher's primary-ref rule are two implementations of one contract."""
    job_id = _create(client, combo)
    job = JobStore(client.jobs_root).load(job_id)
    assert job.package is Package(combo.package)
    assert job.entitlement is Entitlement(combo.entitlement)
    assert job.is_multi_ref is combo.is_mixed
    if combo.is_mixed:
        assert job.primary_ref is not None
        assert job.primary_ref.role == combo.paid


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_each_camera_stages_and_dispatches_on_its_own(
    client: TestClient, queue: FakeQueue, combo: Combo
) -> None:
    """The paid edit must never wait on a speculative card that may never arrive —
    and on the inverted pair the paid card is the CAMERAMAN's."""
    job_id = _create(client, combo)
    store = JobStore(client.jobs_root)

    paid, spec = combo.paid, combo.spec
    r = client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GH010001.MP4", b"fake-mp4", "video/mp4"))],
        data={"camera_role": paid},
    )
    assert r.status_code == 200, r.text
    assert ("media_ref", (job_id, paid)) in queue.calls
    assert ("media_ref", (job_id, spec)) not in queue.calls
    assert [p.name for p in store.camera_raw_dir(job_id, paid).glob("*.MP4")] == [
        "GH010001.MP4"
    ]

    client.post(
        f"/jobs/{job_id}/upload",
        files=[("files", ("GX010007.MP4", b"fake-mp4", "video/mp4"))],
        data={"camera_role": spec},
    )
    assert ("media_ref", (job_id, spec)) in queue.calls
    # Two GoPros emit colliding filenames — the roles must be separate folders.
    assert [p.name for p in store.camera_raw_dir(job_id, spec).glob("*.MP4")] == [
        "GX010007.MP4"
    ]


@dataclass
class _RenderSpy:
    """Records what each ``run_media_ref_pipeline`` pass asked the editor for."""

    compose: list[dict[str, Any]] = field(default_factory=list)
    photos: list[dict[str, Any]] = field(default_factory=list)


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, job_dir: Path) -> _RenderSpy:
    """Replace the ffmpeg/MediaPipe half of the ref pipeline, keep every decision."""
    from api import selfie

    spy = _RenderSpy()
    monkeypatch.setattr(selfie, "_require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        selfie, "_build_role_scene_set",
        lambda job_id, role, raw_dir, jobs_root: ({"scenes": [], "role": role}, []),  # noqa: ARG005
    )

    def _compose(*args: Any, **kw: Any) -> dict[str, Any]:
        spy.compose.append({"use_ai": kw.get("use_ai"), "prefix": kw.get("prefix")})
        return {f"{kw.get('prefix', '')}{b}": {"clips": []} for b in VIDEO_BASES}

    def _render(job_id: str, edls: dict[str, Any], *args: Any, **kw: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in edls:
            p = job_dir / f"{name}.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"CLEAN-MASTER")
            out[name] = str(p)
        return out

    def _extract(*args: Any, **kw: Any) -> list[Any]:
        spy.photos.append(kw)
        (job_dir / "photos").mkdir(parents=True, exist_ok=True)
        return []

    monkeypatch.setattr(selfie, "compose_edls", _compose)
    monkeypatch.setattr(selfie, "render_outputs", _render)
    monkeypatch.setattr(selfie, "extract_photos", _extract)
    monkeypatch.setattr(selfie, "apply_exclusions", lambda edls, _ex: edls)
    monkeypatch.setattr(selfie, "load_exclusions", lambda *_a, **_kw: {})
    monkeypatch.setattr(selfie, "_music_paths", lambda *_a, **_kw: {})
    monkeypatch.setattr(selfie, "_ensure_default_music", lambda b, *a, **kw: b)
    return spy


def _render_both_refs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> tuple[str, _RenderSpy]:
    """Create the job, stage both cards, and run BOTH ref passes for real (minus ffmpeg)."""
    from api.selfie import run_media_ref_pipeline

    job_id = _create(client, combo)
    store = JobStore(client.jobs_root)
    for role in (combo.paid, combo.spec):
        d = store.camera_raw_dir(job_id, role)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"G{role[:2].upper()}010001.MP4").write_bytes(b"fake-mp4")
    store.write_booking(job_id, {"customer_name": "Ada Byron"})

    spy = _stub_pipeline(monkeypatch, store.dir(job_id))
    for role in (combo.paid, combo.spec):
        run_media_ref_pipeline(job_id, role, store=store, jobs_root=client.jobs_root)
    return job_id, spy


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_both_passes_produce_the_expected_deliverable_names(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """The primary ref keeps the plain names; every other ref is namespaced.

    On the inverted pair that means the OUTSIDE camera owns ``full_video`` and the
    handcam is ``instructor_full_video`` — the gallery reads the same either way, but a
    render that got this backwards would silently overwrite the other camera's edit.
    """
    job_id, _spy = _render_both_refs(client, monkeypatch, combo)
    job = JobStore(client.jobs_root).load(job_id)

    expected = {*combo.paid_names, *combo.spec_names}
    if combo.photos_owner is not None:
        expected.add("photos")
    assert set(job.outputs or {}) == expected
    # …and the naming authority agrees, read back both ways.
    for base in combo.paid_names:
        assert deliverable_name(job, combo.paid, base) == base
    for name in combo.spec_names:
        assert role_for_deliverable(job, name) == combo.spec


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_second_pass_never_deletes_the_first_passs_deliverables(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """``outputs`` is merged, never replaced — the gallery lists its keys, so a wiped
    key is a deliverable that vanishes while its bytes linger."""
    job_id, _spy = _render_both_refs(client, monkeypatch, combo)
    outputs = JobStore(client.jobs_root).load(job_id).outputs or {}
    for name in (*combo.paid_names, *combo.spec_names):
        assert name in outputs, f"{name} was dropped by the other ref's pass"


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_photos_come_from_the_paid_camera_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """One ``photos`` key, one directory, one grid — therefore exactly one lock state.

    The owner is the PRIMARY ref, so on the inverted pair the stills come off the
    cameraman's card, and on the photo_only pair the spec camera contributes none at all.
    """
    job_id, spy = _render_both_refs(client, monkeypatch, combo)
    job = JobStore(client.jobs_root).load(job_id)

    assert len(spy.photos) == 1, "exactly one camera may build the photo set"
    assert "photos" in (job.outputs or {})
    assert entitlement_for(job, "photos") is Entitlement.edited_download
    assert photos_locked(job) is False  # the customer bought this half

    # photo_only asks for a fuller set from a wider pool than selfie/external do.
    paid_pkg = Package(dict((r, p) for r, p, _e in combo.refs)[combo.paid])
    from api.selfie import PHOTO_ONLY_TARGET, SELFIE_PHOTO_TARGET

    assert spy.photos[0]["target"] == (
        PHOTO_ONLY_TARGET if paid_pkg is Package.photo_only else SELFIE_PHOTO_TARGET
    )


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_ai_editor_runs_per_ref_not_per_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """Distant camera-flyer footage scores too few faces for the AI editor to sequence,
    so the ``external`` package composes deterministically (the house cut) while a
    handcam ref gets the AI edit — judged per REF, so a mixed job runs both editors.

    The inverted pair is the proof this isn't keyed on "is it the primary": there the
    house cut is the PAID product and the AI edit is the speculative one.
    """
    _job_id, spy = _render_both_refs(client, monkeypatch, combo)
    by_prefix = {c["prefix"]: c["use_ai"] for c in spy.compose}

    for role, package, _ent in combo.refs:
        pkg = Package(package)
        if not pkg.makes_videos:
            continue
        prefix = "" if role == combo.paid else f"{role}_"
        assert by_prefix[prefix] is (pkg is not Package.external), (
            f"{role}/{package} composed with use_ai={by_prefix[prefix]}"
        )


# --------------------------------------------------------------------------- #
# Layer 4 — the gallery: which bytes each deliverable streams
# --------------------------------------------------------------------------- #


def _rendered(client: TestClient, combo: Combo) -> tuple[str, str]:
    """A job with both halves rendered, previews for the locked half, and a token."""
    store = JobStore(client.jobs_root)
    job_id = _create(client, combo)
    jd = store.dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, str] = {}
    for name in (*combo.paid_names, *combo.spec_names):
        (jd / f"{name}.mp4").write_bytes(b"CLEAN-MASTER")
        outputs[name] = str(jd / f"{name}.mp4")
    for name in combo.spec_names:
        (jd / f"preview_{name}.mp4").write_bytes(b"WATERMARKED")
    if combo.photos_owner is not None:
        (jd / "photos").mkdir(exist_ok=True)
        (jd / "photos" / "0001.jpg").write_bytes(b"JPEG")
        # The grid is driven by the extractor's index, not a directory listing.
        (jd / "photos" / "index.json").write_text(
            '[{"filename": "0001.jpg", "ts": 12.0, "scene": "freefall", "score": 0.9}]'
        )
        outputs["photos"] = str(jd / "photos")
    store.set_pipeline_outputs(job_id, outputs, status=JobStatus.ready)

    access = {
        name: DeliverableAccess(entitlement=Entitlement.preview_only, born_locked=True)
        for name in combo.spec_names
    }
    access.update(
        {
            name: DeliverableAccess(entitlement=Entitlement.edited_download)
            for name in (*combo.paid_names, *(("photos",) if combo.photos_owner else ()))
        }
    )
    if access:
        store.set_deliverable_access(job_id, access)
    return job_id, str(store.load(job_id).gallery_token)


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_entitlement_never_the_url_picks_the_bytes(
    client: TestClient, combo: Combo
) -> None:
    """The paywall's core invariant, per combination: while locked, the clean master is
    unreachable at ANY address — and the customer's own edit is never withheld."""
    _job_id, token = _rendered(client, combo)

    for name in combo.paid_names:
        r = client.get(f"/j/{token}/media/{name}")
        assert r.status_code == 200 and r.content == b"CLEAN-MASTER", name
    for name in combo.spec_names:
        r = client.get(f"/j/{token}/media/{name}")
        assert r.status_code == 200 and r.content == b"WATERMARKED", name


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_lock_is_asked_per_deliverable_not_per_job(
    client: TestClient, combo: Combo
) -> None:
    job_id, _token = _rendered(client, combo)
    job = JobStore(client.jobs_root).load(job_id)

    assert locked_deliverables(job) == frozenset(combo.spec_names)
    assert any_locked(job) is True
    # A photo_only primary renders no videos of its own, so EVERY video is the locked
    # camera's — the page is wholly locked even though the customer bought the photos.
    assert all_locked(job) is (not combo.paid_names)
    assert photos_locked(job) is False


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_previews_are_rendered_for_exactly_the_locked_half(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """The job-level question renders none on a paid-primary job (it reads
    ``edited_download``) and the spec edit would then be served clean."""
    from api.preview import PREVIEW_PREFIX, render_job_previews

    from .test_delivery import _settings
    from .test_preview import FakeFFmpeg

    job_id, _token = _rendered(client, combo)
    store = JobStore(client.jobs_root)
    for name in combo.spec_names:  # start from nothing so the render is observable
        (store.dir(job_id) / f"{PREVIEW_PREFIX}{name}.mp4").unlink()

    ffmpeg = FakeFFmpeg()
    made = render_job_previews(store.load(job_id), store, _settings(), runner=ffmpeg)

    assert sorted(made) == sorted(combo.spec_names)
    for name in combo.paid_names:  # the customer's own edit is never watermarked
        assert not (store.dir(job_id) / f"{PREVIEW_PREFIX}{name}.mp4").exists()


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_page_offers_the_locked_camera_and_never_a_whole_job_cta(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """One CTA per still-locked camera; the whole-job ``unlock`` CTA is suppressed
    whenever a per-camera offer exists — leaving it would take a payment and open
    nothing."""
    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/{job_id}?item={item}")
    from api.config import get_settings

    get_settings.cache_clear()
    try:
        job_id, token = _rendered(client, combo)
        page = client.get(f"/j/{token}").text
    finally:
        get_settings.cache_clear()

    assert f"item={UNLOCK_GROUP_ITEM_BY_ROLE[combo.spec]}" in page
    assert f"item={UNLOCK_ITEM}\"" not in page  # never the unscoped one
    assert f"item={UNLOCK_GROUP_ITEM_BY_ROLE[combo.paid]}" not in page
    # The customer's own half is never offered for sale back to them.
    for name in combo.paid_names:
        assert f"/j/{token}/media/{name}" in page


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_state_endpoint_names_exactly_the_locked_cards(
    client: TestClient, combo: Combo
) -> None:
    _job_id, token = _rendered(client, combo)
    state = client.get(f"/j/{token}/state").json()
    assert state["locked"] is True
    assert state["locked_deliverables"] == sorted(combo.spec_names)


# --------------------------------------------------------------------------- #
# Layer 5 — unlock and payout
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_group_unlock_opens_that_camera_and_only_that_camera(
    client: TestClient, combo: Combo
) -> None:
    job_id, token = _rendered(client, combo)
    item = UNLOCK_GROUP_ITEM_BY_ROLE[combo.spec]

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_9f21c7", "item": item},
    )
    assert r.status_code == 200, r.text

    # Same URLs, clean bytes now — no re-render, no new link.
    for name in combo.spec_names:
        assert client.get(f"/j/{token}/media/{name}").content == b"CLEAN-MASTER"

    job = JobStore(client.jobs_root).load(job_id)
    for name in combo.spec_names:
        entry = job.deliverable_access[name]
        assert entry.entitlement is Entitlement.edited_download
        assert entry.payment_reference == "clover_txn_9f21c7"
        assert entry.paid_at is not None
        assert entry.born_locked is True  # the audit trail survives the purchase
    # The paywall machine never touches the review/delivery machine.
    assert job.status is JobStatus.ready
    assert job.entitlement is Entitlement(combo.entitlement)
    assert job.paid_at is None
    assert not locked_deliverables(job)


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_paid_cameras_item_buys_nothing_because_nothing_is_locked(
    client: TestClient, combo: Combo
) -> None:
    """A payment scoped to the camera the customer already owns must be a no-op, not a
    state flip: the group is "born locked AND still locked", and their own edit was
    never born locked."""
    job_id, _token = _rendered(client, combo)
    before = JobStore(client.jobs_root).load(job_id).deliverable_access

    r = client.post(
        f"/jobs/{job_id}/unlock",
        json={"payment_reference": "clover_txn_oops",
              "item": UNLOCK_GROUP_ITEM_BY_ROLE[combo.paid]},
    )
    assert r.status_code == 200
    after = JobStore(client.jobs_root).load(job_id).deliverable_access
    assert {k: v.model_dump() for k, v in after.items()} == {
        k: v.model_dump() for k, v in before.items()
    }


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_a_retried_payment_webhook_keeps_the_original_capture(
    client: TestClient, combo: Combo
) -> None:
    job_id, _token = _rendered(client, combo)
    item = UNLOCK_GROUP_ITEM_BY_ROLE[combo.spec]
    body = {"payment_reference": "clover_txn_9f21c7", "item": item}

    assert client.post(f"/jobs/{job_id}/unlock", json=body).status_code == 200
    first = JobStore(client.jobs_root).load(job_id).deliverable_access
    assert client.post(
        f"/jobs/{job_id}/unlock", json={**body, "payment_reference": "a-retry-id"}
    ).status_code == 200
    again = JobStore(client.jobs_root).load(job_id).deliverable_access

    for name in combo.spec_names:
        assert again[name].paid_at == first[name].paid_at
        assert again[name].payment_reference == "clover_txn_9f21c7"


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_legacy_whole_job_unlock_opens_nothing_on_a_mixed_job(
    client: TestClient, combo: Combo
) -> None:
    """It moves the job's DEFAULT, and every locked deliverable here has an explicit
    entry — so wiring the mixed offer to it would take the money and open nothing."""
    job_id, token = _rendered(client, combo)
    client.post(f"/jobs/{job_id}/unlock", json={"payment_reference": "clover_txn_1"})
    for name in combo.spec_names:
        assert client.get(f"/j/{token}/media/{name}").content == b"WATERMARKED"


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_group_is_scoped_by_the_deliverable_name_convention(
    client: TestClient, combo: Combo
) -> None:
    """``role_for_deliverable`` is the inverse of ``deliverable_name`` — derived, never
    stored, so the two cannot drift and a payment cannot reach the wrong camera."""
    job_id, _token = _rendered(client, combo)
    job = JobStore(client.jobs_root).load(job_id)

    assert unlockable_group(job, role=combo.spec) == frozenset(combo.spec_names)
    assert unlockable_group(job, role=combo.paid) == frozenset()
    assert unlockable_group(job) == frozenset(combo.spec_names)


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_each_angle_carries_its_own_price_from_the_admin_catalogue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """One ``PREVIEW_PRICE_DISPLAY`` cannot speak for two independently-priced angles,
    and a figure shown here must be the figure the checkout charges."""
    import sys

    from api.catalogue import PriceCatalogue

    app_mod = sys.modules["api.app"]

    prices = {"unlock_instructor": 3900, "unlock_external": 4900, "photos": 2500}
    monkeypatch.setattr(
        app_mod, "load_price_catalogue",
        lambda _s: PriceCatalogue(items=prices, currency="usd", labels={}),
    )
    monkeypatch.setenv("CHECKOUT_URL_TEMPLATE", "https://pay.test/{job_id}?item={item}")
    from api.config import get_settings

    get_settings.cache_clear()
    try:
        _job_id, token = _rendered(client, combo)
        page = client.get(f"/j/{token}").text
    finally:
        get_settings.cache_clear()

    from api.app import UNLOCK_GROUP_LABEL_BY_ROLE

    expected, unexpected = ("$39", "$49") if combo.spec == "instructor" else ("$49", "$39")
    # The figure must be ON the locked camera's own CTA, not merely somewhere on the page.
    assert f"{UNLOCK_GROUP_LABEL_BY_ROLE[combo.spec]} — {expected}" in page
    assert unexpected not in page  # the other angle is not on sale here


# --------------------------------------------------------------------------- #
# Layer 6 — the SD-card bridge: two cards, one job, one link, per combination
# --------------------------------------------------------------------------- #


def _match_for(combo: Combo, role: str) -> Any:
    from ingest.match import MatchResult

    return MatchResult(
        role=role, staff_id="staff-1", staff_name="Marc Tremblay",
        load_id="load-7", load_number=7, jumper_index=2, booking_id="bk-1",
        customer_email="ada@example.test", customer_name="Ada Byron",
        media_package=combo.media_package, video_type=combo.video_type,
        package=combo.package, entitlement=combo.entitlement,
        media_refs=[
            MediaRefSpec(role=r, package=p, entitlement=e) for r, p, e in combo.refs
        ],
    )


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_the_second_card_joins_the_job_the_first_one_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """Insert one card, then the other — ONE job, ONE gallery link, ONE email.

    The bridge's only other dedupe key is the ``s3_key``, so without the per-jump record
    the later card starts a fresh job: a second render, a second link and a second "your
    video is ready" email to one customer.
    """
    from .test_bridge_mixed import _Bridge

    b = _Bridge(tmp_path, monkeypatch)
    b.flush(_match_for(combo, combo.paid), ["raw/4313/2026-08-12/GH010001.MP4"])
    b.flush(_match_for(combo, combo.spec), ["raw/5150/2026-08-12/GX010007.MP4"])

    assert len(b.creates) == 1, "the cameraman's card must not open a second job"
    payload = b.creates[0]["json"]
    assert payload["package"] == combo.package
    assert payload["entitlement"] == combo.entitlement
    assert [(r["role"], r["package"], r["entitlement"]) for r in payload["media_refs"]] \
        == list(combo.refs)
    # Each upload names the role its own clip resolved to — that is what decides which
    # product the footage feeds and therefore whether the edit is watermarked.
    assert [u["data"]["camera_role"] for u in b.uploads] == [
        combo.paid, combo.spec
    ]


def test_a_same_camera_pair_sends_no_media_refs_and_no_camera_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``photo_only + selfie_spec`` deferred to the union, so the bridge must open the
    ordinary single-product job — one flat ``raw/``, no ``camera_role``, byte-identical
    to every jump manifested before media refs existed."""
    from .test_bridge_mixed import _Bridge

    combo = BY_KEY["photo_only+selfie_spec"]
    b = _Bridge(tmp_path, monkeypatch)
    b.flush(_match_for(combo, "instructor"), ["raw/4313/2026-08-12/GH010001.MP4"])

    assert "media_refs" not in b.creates[0]["json"]
    assert "camera_role" not in (b.uploads[0]["data"] or {})


# --------------------------------------------------------------------------- #
# Layer 7 — delivery: what is presigned, and how many emails the customer gets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_delivery_presigns_only_what_the_customer_owns(
    client: TestClient, combo: Combo
) -> None:
    """The paywall's OUTER boundary, per combination.

    A presigned URL answers to whoever holds it — there is no entitlement check on a
    URL — and these links are persisted on the job, mirrored into the archive manifest
    and forwarded to SkydiveOS. So every file uploads (durable; what ``/unlock`` serves
    instantly) but only the bought ones get a link.
    """
    from api.delivery import deliver_to_customer

    from .test_delivery import FakeS3, FakeSMTP, _settings

    job_id, _token = _rendered(client, combo)
    store = JobStore(client.jobs_root)
    store.update(job_id, status=JobStatus.approved, customer_email="ada@example.test")
    s3, smtp = FakeS3(), FakeSMTP()

    links = deliver_to_customer(
        store.load(job_id), store, _settings(public_base_url="https://gallery.test"),
        s3_client=s3, smtp_factory=lambda: smtp,  # type: ignore[arg-type,return-value]
    )

    # Every deliverable reached S3 — locked ones included.
    uploaded = {Path(key).stem for _f, _b, key, _e in s3.uploads}
    for name in (*combo.paid_names, *combo.spec_names):
        assert name in uploaded, f"{name} was not uploaded to S3"
    # …but not one locked deliverable was linked.
    for name in combo.spec_names:
        assert name not in links, f"{name} was presigned while still behind the paywall"
    for name in combo.paid_names:
        assert name in links
    if combo.photos_owner is not None:
        assert "photos" in links  # the paid ref's stills
    # One customer, one link, one email.
    assert links["gallery"].startswith("https://gallery.test/j/")
    assert len(smtp.sent) == 1


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_a_locked_half_refuses_the_legacy_s3_gallery(
    client: TestClient, combo: Combo
) -> None:
    """With no ``PUBLIC_BASE_URL`` the fallback page embeds presigned CLEAN masters —
    the paywall bypass. Delivery must fail with an actionable error instead."""
    from api.delivery import deliver_to_customer

    from .test_delivery import FakeS3, _settings

    job_id, _token = _rendered(client, combo)
    store = JobStore(client.jobs_root)
    store.update(job_id, status=JobStatus.approved, customer_email="ada@example.test")

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        deliver_to_customer(
            store.load(job_id), store, _settings(public_base_url=None), s3_client=FakeS3()
        )


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_skydiveos_is_told_the_resolved_per_deliverable_lock(
    client: TestClient, combo: Combo
) -> None:
    """Fully resolved — every video deliverable, not just the explicit entries — so
    nothing over there reimplements the inherit-from-job rule and reads
    ``edited_download`` for a speculative edit."""
    job_id, _token = _rendered(client, combo)
    resp = client.get(f"/jobs/{job_id}").json()

    states = resp["deliverable_entitlements"]
    assert {n: states[n] for n in combo.spec_names} == dict.fromkeys(
        combo.spec_names, "preview_only"
    )
    assert {n: states[n] for n in combo.paid_names} == dict.fromkeys(
        combo.paid_names, "edited_download"
    )


# --------------------------------------------------------------------------- #
# Layer 8 — "insert the card and walk away", with nothing else hand-fed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("combo", MIXED, ids=_combo_id)
def test_two_qr_marked_cards_become_one_job_with_no_operator_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combo: Combo
) -> None:
    """The whole automatic flow, from the only two facts an inserted card supplies.

    Everything else in this module hands the bridge a ``MatchResult``. Here the *real*
    ``FootageMatcher`` runs against the fake load: the notify carries only the filmed
    QR's ``staff_id`` and the clip's capture instant — no package, no role, no customer,
    no camera registry — and the two cards must still converge on ONE job carrying both
    products, each clip staged under the role its own match resolved.
    """
    import asyncio

    from .test_bridge_mixed import _Bridge

    b = _Bridge(tmp_path, monkeypatch)
    b.bridge.matcher = _sdcard_matcher(combo)
    b.bridge.debounce_s = 0.01

    async def _drive() -> None:
        # The instructor drops their card; the cameraman's turns up afterwards.
        for staff, key in (
            ("staff-1", "raw/4313/2026-08-12/GH010001.MP4"),
            ("staff-2", "raw/5150/2026-08-12/GX010007.MP4"),
        ):
            r = await b.bridge.raw_upload(
                {"s3_key": key, "staff_id": staff,
                 "captured_at": "2026-08-12T14:12:00+00:00"}
            )
            assert r["status"] not in ("flagged", "ignored"), r
            await asyncio.sleep(0.05)  # let the settle timer fire

    asyncio.run(_drive())

    assert len(b.creates) == 1, "two cards, two matches, ONE job"
    payload = b.creates[0]["json"]
    assert payload["customer_email"] == "ada@example.test"
    assert payload["package"] == combo.package
    assert payload["entitlement"] == combo.entitlement
    assert [(r["role"], r["package"], r["entitlement"]) for r in payload["media_refs"]] \
        == list(combo.refs)
    # Each card staged under the role ITS clip resolved to — the two GoPros emit
    # colliding filenames, and the role is what decides which half is watermarked.
    assert {u["data"]["camera_role"] for u in b.uploads} == {"instructor", "external"}


def test_a_qr_card_on_a_same_camera_pair_opens_the_ordinary_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``photo_only + selfie_spec`` again, through the real matcher: one card, one
    ordinary single-product job, no ``media_refs``, no ``camera_role``."""
    import asyncio

    from .test_bridge_mixed import _Bridge

    combo = BY_KEY["photo_only+selfie_spec"]
    b = _Bridge(tmp_path, monkeypatch)
    b.bridge.matcher = _sdcard_matcher(combo)
    b.bridge.debounce_s = 0.01

    async def _drive() -> None:
        r = await b.bridge.raw_upload(
            {"s3_key": "raw/4313/2026-08-12/GH010001.MP4", "staff_id": "staff-1",
             "captured_at": "2026-08-12T14:12:00+00:00"}
        )
        assert r["status"] not in ("flagged", "ignored"), r
        await asyncio.sleep(0.05)

    asyncio.run(_drive())

    payload = b.creates[0]["json"]
    assert "media_refs" not in payload
    assert payload["package"] == "selfie"  # the union's answer, not the add-ons'
    assert "camera_role" not in (b.uploads[0]["data"] or {})


def test_a_video_less_primary_still_serves_the_photos_the_customer_bought(
    client: TestClient,
) -> None:
    """``photo_only + external_spec``: EVERY video is the locked camera's, so the page
    takes the locked treatment — but the photo set is the customer's own product and its
    lock is its own question (``photos_locked``, not the page's video-derived
    ``all_locked``). Asking two different questions there is what rendered a grid of
    404ing tiles (BUG 350), so the grid must serve clean bytes on a wholly-locked page.
    """
    combo = BY_KEY["photo_only+external_spec"]
    job_id, token = _rendered(client, combo)
    job = JobStore(client.jobs_root).load(job_id)

    assert all_locked(job) is True  # the page's own treatment is the locked one…
    assert photos_locked(job) is False  # …but the stills were paid for

    r = client.get(f"/j/{token}/photos/0001.jpg")
    assert r.status_code == 200
    assert r.content == b"JPEG"  # the master still, not a watermarked preview
    assert f"/j/{token}/photos/0001.jpg" in client.get(f"/j/{token}").text
