"""The matcher's mixed-jump half: a jumper holding TWO media products.

Two things are pinned here, and they are the two ways this can go wrong at a dropzone:

1. **A mixed jumper must never resolve to ``ultimum``.** Their jumper doc's derived
   ``videoType`` is the UNION of the two add-ons — ``'both'`` — which is byte-identical
   to a genuine Ultimate booking. ``ultimum`` *merges* the two cameras into shared
   deliverables, and a merged clip cannot be half-locked: the unpaid camera-flyer footage
   would be cut clean into the edit the customer bought. So the per-add-on docs
   (``mediaAddOnRefs``) win over the union whenever they resolve.
2. **Everything else must be byte-identical to before.** No ``mediaAddOnRefs`` (every
   jumper manifested before the field shipped), one ref, two refs on the same camera —
   all stay on the single-product union path, because ``media_refs`` empty is the only
   thing the pipeline reads.

Identification is structural, mirroring SkydiveOS's ``utils/autoEditPackage.js`` applied
one doc at a time: ``mediaType``, ``videoAngle`` (+ scene ``cameraSource``),
``isTwoCameraVideo``, ``specOf``. Never by name (their BUG 156).
"""

from __future__ import annotations

from typing import Any

import pytest

from ingest.match import (
    MediaRefSpec,
    is_spec_addon,
    media_refs_for_jumper,
    package_for,
    package_for_addon,
    primary_media_ref,
)

# --------------------------------------------------------------------------- #
# Add-on docs, as the BookingPackage catalogue stores them.
# --------------------------------------------------------------------------- #


def _selfie_addon(**over: Any) -> dict[str, Any]:
    """The paid handcam product: video+photos, filmed from the inside."""
    return {
        "category": "media", "mediaType": "video-photos",
        "videoAngle": "inside", "specOf": None, **over,
    }


def _external_addon(**over: Any) -> dict[str, Any]:
    """The camera-flyer product, filmed from the outside."""
    return {
        "category": "media", "mediaType": "video",
        "videoAngle": "outside", "specOf": None, **over,
    }


def _spec(doc: dict[str, Any]) -> dict[str, Any]:
    """The same product as its $0 spec twin (``specOf`` set → ``preview_only``)."""
    return {**doc, "specOf": "parent-package-id"}


# --------------------------------------------------------------------------- #
# package_for_addon — one doc, structurally.
# --------------------------------------------------------------------------- #


class TestOneAddOn:
    def test_inside_video_is_the_selfie_product_on_the_instructor_camera(self) -> None:
        assert package_for_addon(_selfie_addon()) == ("selfie", "instructor")

    def test_outside_video_is_the_external_product_on_the_cameraman(self) -> None:
        assert package_for_addon(_external_addon()) == ("external", "external")

    def test_photos_only_are_shot_by_the_handcam(self) -> None:
        assert package_for_addon({"mediaType": "photos"}) == ("photo_only", "instructor")

    def test_the_two_camera_flag_is_ultimum_and_has_no_single_role(self) -> None:
        """Ultimate spans both cameras — it is one product, never half of a pair."""
        assert package_for_addon(
            {"mediaType": "video-photos", "videoAngle": "both", "isTwoCameraVideo": True}
        ) == ("ultimum", None)

    def test_both_angles_without_the_two_camera_flag_is_refused_not_guessed(self) -> None:
        """SkydiveOS's contract: ambiguous → flag, because a wrong ultimum hangs forever."""
        assert package_for_addon(
            {"mediaType": "video", "videoAngle": "both"}
        ) == (None, None)

    def test_video_with_no_angle_at_all_is_refused(self) -> None:
        assert package_for_addon({"mediaType": "video"}) == (None, None)

    def test_a_non_media_addon_contributes_nothing(self) -> None:
        assert package_for_addon({"category": "general", "mediaType": None}) == (None, None)

    def test_the_angle_falls_back_to_the_scene_camera_source(self) -> None:
        """``videoAngle`` absent → the shot list decides, exactly as deriveMediaFields does."""
        assert package_for_addon(
            {"mediaType": "video", "scenes": [{"cameraSource": "external"}]}
        ) == ("external", "external")
        assert package_for_addon(
            {"mediaType": "video", "scenes": [{"cameraSource": "selfie"}]}
        ) == ("selfie", "instructor")

    def test_an_explicit_angle_overrides_the_scenes(self) -> None:
        """The per-package angle is authoritative; only ``any``/absent defers."""
        assert package_for_addon(
            {"mediaType": "video", "videoAngle": "outside",
             "scenes": [{"cameraSource": "selfie"}]}
        ) == ("external", "external")

    def test_the_name_is_never_read(self) -> None:
        """BUG 156: names are staff-editable, so an 'External' add-on must not need one."""
        assert package_for_addon(
            {"name": "Ultimum Deluxe Selfie Combo", "mediaType": "video",
             "videoAngle": "outside"}
        ) == ("external", "external")

    def test_spec_ness_is_the_relationship_not_a_flag(self) -> None:
        assert is_spec_addon(_spec(_external_addon())) is True
        assert is_spec_addon(_external_addon()) is False


# --------------------------------------------------------------------------- #
# media_refs_for_jumper — the whole set.
# --------------------------------------------------------------------------- #


class TestTheRefSet:
    def test_the_paid_selfie_plus_spec_external_pair_is_the_mixed_jump(self) -> None:
        refs = media_refs_for_jumper([_selfie_addon(), _spec(_external_addon())])
        assert refs == [
            MediaRefSpec(role="instructor", package="selfie",
                         entitlement="edited_download"),
            MediaRefSpec(role="external", package="external",
                         entitlement="preview_only"),
        ]

    def test_a_single_addon_is_not_a_mixed_set(self) -> None:
        """One product → the ordinary job, and ``media_refs`` empty keeps it that way."""
        assert media_refs_for_jumper([_selfie_addon()]) == []

    def test_no_addons_at_all_is_not_a_mixed_set(self) -> None:
        assert media_refs_for_jumper([]) == []

    def test_ultimum_is_never_half_of_a_pair(self) -> None:
        """It merges the cameras itself; splitting it into refs would render it twice."""
        ultimate = {"mediaType": "video-photos", "isTwoCameraVideo": True}
        assert media_refs_for_jumper([ultimate, _spec(_external_addon())]) == []

    def test_two_products_on_one_camera_are_refused(self) -> None:
        """One role is one raw folder, one render pass and ONE lock state.

        A photo add-on alongside a selfie package is the common shape of this and is
        deliberately not a mixed set — photos come from the paid ref only.
        """
        assert media_refs_for_jumper([_selfie_addon(), {"mediaType": "photos"}]) == []

    def test_an_unclassifiable_addon_refuses_the_whole_set(self) -> None:
        """Never guess a package for footage — the caller flags it for a human."""
        assert media_refs_for_jumper(
            [_selfie_addon(), {"category": "media", "mediaType": "video"}]
        ) == []

    def test_two_paid_products_are_a_valid_mixed_set(self) -> None:
        """Both bought → both clean, still two renders on one link (no lock at all)."""
        refs = media_refs_for_jumper([_selfie_addon(), _external_addon()])
        assert [(r.role, r.entitlement) for r in refs] == [
            ("instructor", "edited_download"), ("external", "edited_download"),
        ]

    def test_a_spec_handcam_with_a_paid_external_is_the_inverted_pair(self) -> None:
        refs = media_refs_for_jumper([_spec(_selfie_addon()), _external_addon()])
        assert {r.role: r.entitlement for r in refs} == {
            "instructor": "preview_only", "external": "edited_download",
        }

    def test_a_falsy_doc_in_the_list_is_skipped_not_crashed_on(self) -> None:
        refs = media_refs_for_jumper([_selfie_addon(), None, _spec(_external_addon())])  # type: ignore[list-item]
        assert len(refs) == 2


# --------------------------------------------------------------------------- #
# primary_media_ref — which ref owns the job's top-level fields.
# --------------------------------------------------------------------------- #


class TestOneProductOnly:
    """A single add-on: no refs, but its entitlement still has to be right.

    This is where the paywall was leaking. A spec twin is the SAME product as its
    parent at $0, so it carries the same ``mediaType`` and the jumper's derived union is
    indistinguishable from a real purchase — ``package_and_entitlement_for`` reads
    ``edited_download`` and the unpaid edit is served clean, no watermark, downloads on.
    Only the add-on doc's ``specOf`` can tell them apart.
    """

    def test_a_lone_spec_external_is_locked_not_given_away(self) -> None:
        from ingest.match import resolve_media_products

        assert resolve_media_products([_spec(_external_addon())]) == (
            "external", "preview_only", [],
        )

    def test_a_lone_spec_selfie_is_locked_too(self) -> None:
        from ingest.match import resolve_media_products

        assert resolve_media_products([_spec(_selfie_addon())]) == (
            "selfie", "preview_only", [],
        )

    def test_a_lone_paid_addon_is_owned(self) -> None:
        from ingest.match import resolve_media_products

        assert resolve_media_products([_selfie_addon()]) == (
            "selfie", "edited_download", [],
        )

    def test_a_lone_spec_addon_also_fixes_the_package(self) -> None:
        """``external``, not ``video_only`` — the union's answer for a lone outside cam.

        It matters beyond tidiness: ``video_only`` runs the AI editor, and distant
        cameraman footage scores too few faces for it to sequence reliably. ``external``
        composes the deterministic house cut, which is what that footage needs.
        """
        from ingest.match import resolve_media_products

        package, _entitlement, _refs = resolve_media_products([_spec(_external_addon())])
        assert package == "external"
        assert package_for("video", "outside") == "video_only"  # what the union says

    def test_a_lone_ultimum_twin_is_locked_and_still_ultimum(self) -> None:
        """Ultimate can be twinned too; it stays one merged two-camera product."""
        from ingest.match import resolve_media_products

        ultimate = {"mediaType": "video-photos", "videoAngle": "both",
                    "isTwoCameraVideo": True}
        assert resolve_media_products([_spec(ultimate)]) == (
            "ultimum", "preview_only", [],
        )

    def test_an_unclassifiable_lone_addon_defers_to_the_union(self) -> None:
        from ingest.match import resolve_media_products

        assert resolve_media_products([{"category": "media", "mediaType": "video"}]) == (
            None, None, [],
        )


class TestPrimaryRef:
    def test_the_paid_product_wins_over_the_spec_twin(self) -> None:
        refs = media_refs_for_jumper([_spec(_selfie_addon()), _external_addon()])
        primary = primary_media_ref(refs)
        assert primary is not None
        assert (primary.role, primary.entitlement) == ("external", "edited_download")

    def test_it_does_not_depend_on_array_order(self) -> None:
        """Order-dependence would rename the deliverables on a re-created job.

        The primary ref keeps the plain names while every other ref is namespaced
        ``<role>_<name>``, so a reordered set would move the gallery's video keys and the
        page would lose them while the bytes lingered.
        """
        a = MediaRefSpec(role="instructor", package="selfie",
                         entitlement="preview_only")
        b = MediaRefSpec(role="external", package="external",
                         entitlement="edited_download")
        assert primary_media_ref([a, b]) == primary_media_ref([b, a]) == b

    def test_the_instructor_wins_when_both_are_the_same_entitlement(self) -> None:
        refs = media_refs_for_jumper([_selfie_addon(), _external_addon()])
        primary = primary_media_ref(refs)
        assert primary is not None and primary.role == "instructor"

    def test_an_empty_set_has_no_primary(self) -> None:
        assert primary_media_ref([]) is None


# --------------------------------------------------------------------------- #
# The union path is untouched — the back-compat contract.
# --------------------------------------------------------------------------- #


def test_the_union_package_contract_is_unchanged() -> None:
    """``package_for`` still reads the jumper's derived fields exactly as before.

    A legacy row (no ``mediaAddOnRefs``) whose union is ``'both'`` is still read as
    ``ultimum`` — knowingly, because without the per-add-on docs there is no signal that
    separates a genuine Ultimate booking from a mixed pair. Every new mixed pair carries
    the refs, so this is a legacy-row limitation, not a live one.
    """
    assert package_for("video-photos", "both") == "ultimum"
    assert package_for("video-photos", "inside") == "selfie"
    assert package_for("video-photos", "outside") == "external"
    assert package_for("video", "inside") == "video_only"
    assert package_for("photos", None) == "photo_only"
    assert package_for("none", None) is None


# --------------------------------------------------------------------------- #
# The one database read: mediaAddOnRefs -> the catalogue -> the refs.
# --------------------------------------------------------------------------- #


class _Collection:
    def __init__(self, docs: list[dict[str, Any]] | Exception) -> None:
        self._docs = docs

    def find(self, query: dict[str, Any], _projection: Any = None) -> Any:
        if isinstance(self._docs, Exception):
            raise self._docs
        wanted = set(query["_id"]["$in"])
        return iter([d for d in self._docs if d["_id"] in wanted])


def _products_for(
    jumper: dict[str, Any], catalogue: list[dict[str, Any]] | Exception
) -> tuple[str | None, str | None, list[MediaRefSpec]]:
    from ingest.match import FootageMatcher

    return FootageMatcher._media_products_for(
        {"bookingpackages": _Collection(catalogue)}, jumper
    )


def _refs_for(
    jumper: dict[str, Any], catalogue: list[dict[str, Any]] | Exception
) -> list[MediaRefSpec]:
    return _products_for(jumper, catalogue)[2]


class TestTheCatalogueRead:
    def test_two_refs_are_resolved_into_a_mixed_set(self) -> None:
        refs = _refs_for(
            {"mediaAddOnRefs": ["a", "b"]},
            [{"_id": "a", **_selfie_addon()}, {"_id": "b", **_spec(_external_addon())}],
        )
        assert [(r.role, r.package, r.entitlement) for r in refs] == [
            ("instructor", "selfie", "edited_download"),
            ("external", "external", "preview_only"),
        ]

    def test_a_legacy_row_never_touches_the_catalogue(self) -> None:
        """No ``mediaAddOnRefs`` at all → nothing to resolve, and no lookup to pay for."""
        boom = RuntimeError("the catalogue must not be read")
        assert _products_for({"mediaAddOnRefs": []}, boom) == (None, None, [])
        assert _products_for({}, boom) == (None, None, [])

    def test_a_single_ref_IS_read_because_only_the_doc_shows_spec_ness(self) -> None:
        """One indexed lookup against handing an unpaid edit over clean.

        The union sees a twin's ``mediaType`` — the same one its paid parent carries —
        and reads it as a purchase. The doc is the only place ``specOf`` lives.
        """
        assert _products_for(
            {"mediaAddOnRefs": ["a"]}, [{"_id": "a", **_spec(_external_addon())}]
        ) == ("external", "preview_only", [])

    def test_an_unreadable_catalogue_falls_back_to_the_union_path(self) -> None:
        """No job shape is worse than the wrong one — but a traceback is worse than both."""
        assert _products_for(
            {"mediaAddOnRefs": ["a", "b"]}, RuntimeError("mongo is down")
        ) == (None, None, [])

    def test_a_ref_pointing_at_a_deleted_addon_falls_back(self) -> None:
        """Half a set is not a set: resolving one of two would silently drop a product."""
        assert _products_for(
            {"mediaAddOnRefs": ["a", "b"]}, [{"_id": "a", **_selfie_addon()}]
        ) == (None, None, [])


# --------------------------------------------------------------------------- #
# _build_result: the mixed jumper must not open an `ultimum` job.
# --------------------------------------------------------------------------- #


class _Db:
    def __init__(self, catalogue: list[dict[str, Any]]) -> None:
        self._catalogue = catalogue

    def __getitem__(self, name: str) -> Any:
        if name == "bookingpackages":
            return _Collection(self._catalogue)
        return type("_C", (), {"find_one": lambda self, q: None})()


@pytest.fixture
def mixed_jumper() -> dict[str, Any]:
    """A jumper holding a paid handcam package and a spec camera-flyer twin.

    ``mediaPackage``/``videoType`` are the UNION SkydiveOS derives and stores — note the
    ``'both'``, which is exactly what a genuine Ultimate booking also carries.
    """
    return {
        "mediaPackage": "video-photos",
        "videoType": "both",
        "mediaAddOnRefs": ["paid-selfie", "spec-external"],
    }


def _result_for(jumper: dict[str, Any], catalogue: list[dict[str, Any]]) -> Any:
    from ingest.match import Candidate, FootageMatcher, OwnershipDecision

    candidate = Candidate(
        role="instructor", load_id="load-7", load_number=7, jumper_index=2,
        jumper=jumper, business_day="2026-08-12", departure_local=None,
    )
    matcher = object.__new__(FootageMatcher)
    return FootageMatcher._build_result(
        matcher, _Db(catalogue), "staff-1", "Marc Tremblay",
        OwnershipDecision(candidate=candidate, evidence="window", detail={}),
    )


def test_a_mixed_jumper_does_not_become_an_ultimum_job(
    mixed_jumper: dict[str, Any],
) -> None:
    """The whole point: the union says ``ultimum``, the add-on docs say otherwise.

    ``ultimum`` merges both cameras into shared deliverables, so its clips cannot be
    half-locked — the unpaid camera-flyer footage would be cut clean into the edit the
    customer paid for. The per-add-on docs are the more specific signal and win.
    """
    result = _result_for(
        mixed_jumper,
        [
            {"_id": "paid-selfie", **_selfie_addon()},
            {"_id": "spec-external", **_spec(_external_addon())},
        ],
    )
    assert package_for(result.media_package, result.video_type) == "ultimum"  # the union
    assert result.package == "selfie"  # what we actually open
    assert result.entitlement == "edited_download"
    assert [(r.role, r.package, r.entitlement) for r in result.media_refs] == [
        ("instructor", "selfie", "edited_download"),
        ("external", "external", "preview_only"),
    ]


def test_the_top_level_fields_mirror_the_primary_ref(
    mixed_jumper: dict[str, Any],
) -> None:
    """``POST /jobs`` rejects a body whose ``package``/``entitlement`` disagree.

    The paid product is the primary one, so a spec handcam + paid cameraman inverts
    which ref the job's top-level fields describe.
    """
    result = _result_for(
        mixed_jumper,
        [
            {"_id": "paid-selfie", **_spec(_selfie_addon())},
            {"_id": "spec-external", **_external_addon()},
        ],
    )
    assert (result.package, result.entitlement) == ("external", "edited_download")


def test_a_single_product_jumper_is_byte_identical(
    mixed_jumper: dict[str, Any],
) -> None:
    """No refs → the union path, empty ``media_refs``, nothing downstream branches."""
    result = _result_for(
        {"mediaPackage": "video-photos", "videoType": "inside"}, []
    )
    assert (result.package, result.entitlement) == ("selfie", "edited_download")
    assert result.media_refs == []


def test_a_genuine_ultimate_booking_still_opens_one_ultimum_job() -> None:
    """The two-camera flag is not a mixed pair — it must keep merging the angles."""
    result = _result_for(
        {"mediaPackage": "video-photos", "videoType": "both",
         "mediaAddOnRefs": ["ultimate", "photos"]},
        [
            {"_id": "ultimate", "mediaType": "video-photos", "isTwoCameraVideo": True},
            {"_id": "photos", "mediaType": "photos"},
        ],
    )
    assert result.package == "ultimum"
    assert result.media_refs == []


def test_a_speculative_jumper_with_no_purchase_is_unchanged() -> None:
    """Path B: nobody bought media, the handcam filmed it anyway → locked preview.

    One camera, so no refs and nothing downstream branches — byte-identical to every
    speculative job written before any of this existed.
    """
    result = _result_for(
        {"instructor": "staff-i", "mediaPackage": "none", "videoType": None}, []
    )
    assert (result.package, result.entitlement) == ("selfie", "preview_only")
    assert result.media_refs == []


def test_a_no_purchase_jumper_with_a_flyer_gets_both_cameras_locked() -> None:
    """Bought nothing, no add-on selected, but ops put a flyer on the open seat.

    Both cards resolve to this one jumper, so without per-camera products they staged
    into one flat ``raw/`` and rendered as a single edit made of both cameras' footage
    mixed together. Now each camera is its own locked deliverable — the same shape a
    selected spec twin produces, reached from the slots alone.
    """
    result = _result_for(
        {
            "instructor": "staff-i", "assignedCameraman": "staff-c",
            "mediaPackage": "none", "videoType": None,
        },
        [],
    )
    assert (result.package, result.entitlement) == ("selfie", "preview_only")
    assert [(m.role, m.package, m.entitlement) for m in result.media_refs] == [
        ("instructor", "selfie", "preview_only"),
        ("external", "external", "preview_only"),
    ]


def test_a_legacy_row_that_DID_buy_still_defers_to_the_union() -> None:
    """No ``mediaAddOnRefs`` but a real purchase → the union, untouched.

    Guessing here would demote a genuine Ultimate booking — ``videoType: 'both'`` with no
    refs, which is every one written before the field shipped — to two speculative
    single-camera products, breaking the merged two-camera edit the customer paid for.
    """
    result = _result_for(
        {
            "instructor": "staff-i", "assignedCameraman": "staff-c",
            "mediaPackage": "video-photos", "videoType": "both",
        },
        [],
    )
    assert (result.package, result.entitlement) == ("ultimum", "edited_download")
    assert result.media_refs == []


# --------------------------------------------------------------------------- #
# Four jumpers on one load, four shapes — the whole matrix, end to end.
# --------------------------------------------------------------------------- #


def _jumper(
    *addon_ids: str,
    media: str,
    video_type: str | None,
    cameraman: bool = True,
) -> dict[str, Any]:
    """A load jumper carrying ``addon_ids`` plus the union SkydiveOS derives from them.

    The staff slots matter as much as the add-ons: they are what says which cameras
    actually rolled, and an uncovered camera that rolled gets a locked deliverable.
    """
    jumper: dict[str, Any] = {
        "instructor": "staff-instructor",  # a tandem always has one, always filming
        "mediaPackage": media,
        "videoType": video_type,
        "mediaAddOnRefs": list(addon_ids),
    }
    if cameraman:
        jumper["assignedCameraman"] = "staff-cameraman"
    return jumper


class TestTheFourShapes:
    def test_1_paid_selfie_plus_spec_external(self) -> None:
        """The mixed pair. Handcam clean, cameraman watermarked, one link."""
        r = _result_for(
            _jumper("s", "x", media="video-photos", video_type="both"),
            [{"_id": "s", **_selfie_addon()}, {"_id": "x", **_spec(_external_addon())}],
        )
        assert (r.package, r.entitlement) == ("selfie", "edited_download")
        assert [(m.role, m.entitlement) for m in r.media_refs] == [
            ("instructor", "edited_download"), ("external", "preview_only"),
        ]

    def test_2_ultimum_stays_one_merged_job(self) -> None:
        """Not a pair: Ultimate merges the angles into shared deliverables itself."""
        r = _result_for(
            _jumper("u", media="video-photos", video_type="both"),
            [{"_id": "u", "mediaType": "video-photos", "videoAngle": "both",
              "isTwoCameraVideo": True}],
        )
        assert (r.package, r.entitlement) == ("ultimum", "edited_download")
        assert r.media_refs == []

    def test_3_paid_external_plus_spec_selfie_inverts_the_naming(self) -> None:
        """The paid ref is primary whichever camera it is — so EXTERNAL keeps the
        plain deliverable names here, and the handcam twin is the namespaced one."""
        r = _result_for(
            _jumper("x", "s", media="video-photos", video_type="both"),
            [{"_id": "x", **_external_addon()}, {"_id": "s", **_spec(_selfie_addon())}],
        )
        assert (r.package, r.entitlement) == ("external", "edited_download")
        primary = primary_media_ref(r.media_refs)
        assert primary is not None and primary.role == "external"
        assert {m.role: m.entitlement for m in r.media_refs} == {
            "external": "edited_download", "instructor": "preview_only",
        }

    def test_4_spec_external_only_gets_BOTH_cameras_locked(self) -> None:
        """Bought nothing, a flyer went up anyway → two watermarked edits, one link.

        Two bugs met here. The union reads a lone outside-video twin as
        ``mediaPackage: 'video'`` / ``videoType: 'outside'`` — indistinguishable from a
        bought one — so it answered ``('video_only', 'edited_download')`` and served the
        unpaid cameraman edit **clean**, downloads enabled, with no CTA to buy it with.
        And with no refs at all, both cards staged into one flat ``raw/`` and rendered as
        a single edit made of both cameras' footage mixed together.

        Now the handcam — which films every tandem whether or not anybody paid — gets its
        own locked ref, and the cameraman's twin gets its own. Both preview_only, so the
        primary falls to the instructor and keeps the plain deliverable names.
        """
        assert package_for("video", "outside") == "video_only"  # what the union says

        r = _result_for(
            _jumper("x", media="video", video_type="outside"),
            [{"_id": "x", **_spec(_external_addon())}],
        )
        assert (r.package, r.entitlement) == ("selfie", "preview_only")
        assert [(m.role, m.package, m.entitlement) for m in r.media_refs] == [
            ("instructor", "selfie", "preview_only"),
            ("external", "external", "preview_only"),
        ]

    def test_4b_a_paid_external_only_jumper_keeps_the_handcam_locked(self) -> None:
        """The counterfactual: the cameraman's angle is bought, the handcam's is not.

        The paid ref is primary, so ``external`` keeps the plain names — and the handcam
        that filmed anyway is offered, never given away.
        """
        r = _result_for(
            _jumper("x", media="video", video_type="outside"),
            [{"_id": "x", **_external_addon()}],
        )
        assert (r.package, r.entitlement) == ("external", "edited_download")
        assert {m.role: m.entitlement for m in r.media_refs} == {
            "external": "edited_download", "instructor": "preview_only",
        }

    def test_a_selfie_only_jumper_with_no_flyer_is_untouched(self) -> None:
        """Nothing synthesised for a camera that never rolled — there is no footage.

        This is the ordinary tandem, and it must stay a plain single-product job: no
        refs, no per-role staging, no second render pass.
        """
        r = _result_for(
            _jumper("s", media="video-photos", video_type="inside", cameraman=False),
            [{"_id": "s", **_selfie_addon()}],
        )
        assert (r.package, r.entitlement) == ("selfie", "edited_download")
        assert r.media_refs == []

    def test_ultimum_synthesises_nothing_even_with_both_slots_filled(self) -> None:
        """Ultimate already covers both cameras — a synthesised ref would render twice."""
        r = _result_for(
            _jumper("u", media="video-photos", video_type="both"),
            [{"_id": "u", "mediaType": "video-photos", "isTwoCameraVideo": True}],
        )
        assert (r.package, r.entitlement) == ("ultimum", "edited_download")
        assert r.media_refs == []
