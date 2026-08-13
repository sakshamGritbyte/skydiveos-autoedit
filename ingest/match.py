"""Footage → jump match: resolve a clip's camera + capture time to a load's jumper.

This is the *SkydiveOS-side* footage↔booking match, mirrored into the auto-edit repo
so the whole camera→customer flow can run end to end without the SkydiveOS backend
in the loop (see :doc:`SKYDIVEOS_INTEGRATION.md` — SkydiveOS owns this match in
production; this module lets a dropzone run it locally against the shared DB).

Why it exists: the two-camera products need to know, for **each clip**, whether it is
the instructor's handcam (``instructor`` role) or the cameraman's outside cam
(``external`` role) — and at real dropzones **one staff member can be the instructor
on one jump and the cameraman on the next**, on the *same* physical GoPro. So the role
is **not** a static property of the camera; it is decided **per jump** by which slot
that staff filled on the matched load-jumper:

    * the jumper's ``instructor``        → role ``instructor``  (handcam / inside)
    * the jumper's ``assignedCameraman`` → role ``external``    (cameraman / outside)

The camera registry's static ``role`` (:mod:`ingest.registry`) is therefore only a
*hint*; this resolver is the authority.

Match key, in order:

    1. ``staffs.goproSerial`` == the clip's camera serial  → the owning staff member
       (a serial maps to exactly one staff; that is the reliable bridge — the SkydiveOS
       ``staffs`` ``_id`` differs from the auto-edit registry's owner id, so we never
       match on the registry).
    2. the clip's **true-UTC** ``captured_at`` → the dropzone-local day + flight window
       → the ``loads`` whose ``businessDate`` is that day and whose ``departureTime`` is
       within the window.
    3. among those loads, the jumper(s) whose ``instructor`` **or** ``assignedCameraman``
       is that staff member → the (load, jumper, role) triple.

**Refuse and flag on ambiguity.** If zero or more than one jumper survives, we raise
rather than guess — mis-assigning here emails customer A's video to customer B. The
caller flags the footage for a human instead of picking "the nearest one".

**The spec-flight match** (:meth:`FootageMatcher.resolve_load_for_staff`) is the second,
deliberately separate entry point. When a camera flyer goes up with **no assigned
customer** — ops filling an open seat on spec — he holds no jumper slot, so the
jumper-keyed query above returns nothing at all and step 3 can never succeed. That
footage is still worth an edit: it becomes one *load master* offered to everybody on the
manifest (see ``CLAUDE.md``). Resolving it needs a load looked up **by time**, which the
jumper-keyed path never does, and there is no crew field on a load document to confirm he
was aboard — so the flight window is mandatory there (:func:`select_load`) and a staff
member who *does* hold a slot is refused with :class:`NotSpecFlight`, because that
footage belongs to their customer. ``MatchResult`` and :func:`select_match` are untouched
by this: the load path has its own result shape (:class:`LoadMatchResult`) so nothing
that consumes a jumper-keyed match has to learn about jumper-less ones.

Design mirrors :mod:`ingest.registry`: lazy ``pymongo`` import, ``MONGO_URL`` /
``MONGO_DB`` from the environment, disabled (every lookup raises
:class:`RegistryUnavailable`) when no URL is configured. The *decision* logic
(:func:`select_match`, :func:`package_for`) is **pure** — plain dicts in, result out,
no I/O — so it unit-tests without a database, exactly like :mod:`edl.validate`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Default database + collections (override the DB with ``MONGO_DB``).
DEFAULT_DB = "skydiveos"
STAFFS = "staffs"
LOADS = "loads"
CUSTOMERS = "customers"
#: The media add-on catalogue (mongoose pluralises ``BookingPackage``). Read only to
#: resolve a jumper's ``mediaAddOnRefs`` into per-product media refs — see
#: :func:`media_refs_for_jumper`.
BOOKING_PACKAGES = "bookingpackages"

#: Camera roles, as plain strings equal to ``api.jobs``'s (never imported here).
CAMERA_ROLE_INSTRUCTOR = "instructor"
CAMERA_ROLE_EXTERNAL = "external"
#: Canonical ref order — handcam first, cameraman second. Media refs are emitted in this
#: order rather than in add-on order so a re-created job can't reorder them (which would
#: not change the *primary* ref, but would make the wire payload gratuitously unstable).
MEDIA_REF_ROLES = (CAMERA_ROLE_INSTRUCTOR, CAMERA_ROLE_EXTERNAL)

#: Flight-window tolerance around a load's **scheduled** ``departureTime`` that a clip's
#: capture instant may fall in and still belong to that load. Footage is captured *after*
#: departure (plane climbs, then the jump), so the window is asymmetric: a little
#: before (early departure / clock skew) and generously after (climb + freefall +
#: 3–5 min canopy ride + landing + buffer).
#:
#: This is the FALLBACK window, used only when the load has no recorded actual flight
#: times. ``WINDOW_POST`` is 2.5 h — far wider than a tandem load's ~25 min of flight —
#: so it overlaps 5–6 consecutive loads, which is why a clip filmed *between* jumps used
#: to be claimed by whichever load had most recently departed (the audit's §8 worst case:
#: one customer's interview landing in another customer's edit). Prefer
#: :func:`flight_window`, which narrows to the load's real times when ops recorded them.
WINDOW_PRE = timedelta(minutes=30)
WINDOW_POST = timedelta(minutes=150)

#: Grace either side of a load's **recorded** flight when ``actualTakeoffTime`` (and
#: optionally ``landingTime``) are present: boarding/taxi before, gear-off after. Mirrors
#: the production serial-path window (``capturedWithinLoadWindow`` in
#: ``autoEditOrchestrationService.js``), which has always used the real times.
TAKEOFF_PRE = timedelta(minutes=20)
LANDING_POST = timedelta(minutes=10)
#: Upper bound on a flight whose ``landingTime`` isn't recorded yet.
MAX_FLIGHT = timedelta(minutes=60)

#: Two departures closer together than this are treated as indistinguishable: the causal
#: tie-break ("the latest departure at or before the clip") is a *guess* at that spacing,
#: and it put customer A's clip in customer B's job when two loads left 5 min apart
#: (audit §3-D). Shorter than any realistic load turnaround, so a normal 15–25 min
#: cadence still resolves causally.
MIN_DEPARTURE_GAP = timedelta(minutes=20)

# --- how a match was established (``MatchResult.evidence``) ----------------- #
#: The clip fell inside the chosen candidate's flight window. The only clean answer.
EVIDENCE_WINDOW = "window"
#: TEMPORARY COMPATIBILITY PATH (approved 2026-08-11, review after ~1 week in
#: production): the clip is OUTSIDE its candidate's window, but that candidate is the
#: only slot this staff member holds on the capture day, so there is nothing it could be
#: confused with. This is what keeps a rescheduled customer's early interview attached to
#: their jump (audit §3-A) — every acceptance is logged at WARNING with the full decision
#: record and counted, so the real population can be reviewed before the window is
#: enforced strictly.
EVIDENCE_OUT_OF_WINDOW_SAME_DAY = "out_of_window_same_day"
#: Several candidates were in-window and the causal rule (latest departure at or before
#: the clip) chose between them, with the departures at least
#: :data:`MIN_DEPARTURE_GAP` apart.
EVIDENCE_CAUSAL_TIEBREAK = "causal_tiebreak"

#: Load statuses a jump can be matched against. A ``planned`` load may still be
#: matched (footage can land before manifest closes it); a cancelled one never.
_MATCHABLE_STATUSES = {"planned", "closed", "landed", "completed"}

#: Candidate types :func:`_narrow_by_time` accepts — both expose ``business_day`` and
#: ``departure_local``, which is all the time narrowing reads.
_Timed = TypeVar("_Timed", "Candidate", "LoadCandidate")


class FootageMatchError(Exception):
    """Base for a match that could not be made safely."""


class RegistryUnavailable(FootageMatchError):
    """No ``MONGO_URL`` configured, so the shared DB can't be read."""


class UnknownCamera(FootageMatchError):
    """No staff member owns the clip's camera serial (``staffs.goproSerial``)."""


class NoBookingMatch(FootageMatchError):
    """The camera's owner isn't in any matchable load-jumper for that capture time."""


class NoLoadMatch(FootageMatchError):
    """No matchable load's flight window contains the capture instant."""


class NotSpecFlight(FootageMatchError):
    """The staff member holds a jumper slot on the resolved load, so this is not a
    speculative ("spec") flight.

    A *spec flight* is a camera flyer going up with **no assigned customer** — the only
    case v1 builds a load master for. When the flyer is somebody's ``instructor`` or
    ``assignedCameraman`` on that load, the footage is that customer's product and
    :meth:`FootageMatcher.resolve` already routes it; a load master would additionally
    need to exclude the customer's personal scenes from everyone else's video, which
    scene labels cannot guarantee (see ``AUDIT_SCENE_LABELS.md``).
    """


class AmbiguousMatch(FootageMatchError):
    """More than one jumper matches — refuse and flag rather than guess."""

    def __init__(self, reason: str, candidates: list[Candidate]) -> None:
        super().__init__(reason)
        self.candidates = candidates


@dataclass
class Candidate:
    """One (load, jumper, role) the camera's owner could belong to."""

    load_id: str
    load_number: int | None
    departure_local: datetime | None  # naive DZ-local wall clock, as stored
    business_day: str | None  # ``YYYY-MM-DD`` DZ-local
    jumper_index: int
    jumper: dict[str, Any] = field(repr=False)
    role: str  # ``"instructor"`` | ``"external"``
    #: The load's RECORDED flight, when ops entered it (naive DZ-local, as stored).
    #: Present → :func:`flight_window` uses it instead of the scheduled ±2.5 h window,
    #: which is what lets a clip filmed between loads be recognised as belonging to
    #: neither. Absent → the scheduled fallback, and §3-C stays unresolvable for that
    #: load (see :func:`flight_window`).
    actual_takeoff_local: datetime | None = None
    landing_local: datetime | None = None

    @property
    def booking_ref(self) -> str | None:
        """The stable identity the rest of the chain joins on — the jumper's booking.

        Check T3. A slot with no booking cannot be reconciled to a job, gallery, tile or
        email later: ``api.tasks._is_own_job`` prefers it precisely because
        ``jumper_index`` is a manifest *position* that shifts when the manifest is edited
        (audit ⚠️-7). So ownership is refused rather than resolved to something
        unreconcilable.

        Booking only, deliberately: this mirrors the production matcher, which has always
        answered ``unmatched: "matched exactly one jump but it has no booking — link
        manually"``. Accepting a customer id instead would make this side create jobs that
        production would have refused.
        """
        value = self.jumper.get("booking")
        return str(value) if value is not None and str(value).strip() else None


@dataclass
class LoadCandidate:
    """One load a clip could belong to, resolved WITHOUT a jumper.

    The jumper-keyed :class:`Candidate` cannot represent a spec flight: the flyer holds
    no slot on the load, so the jumper predicate yields nothing. This carries only what
    the time narrowing needs (the same two fields :class:`Candidate` exposes, so
    :func:`_narrow_by_time` serves both) plus the raw load document for enrichment.
    """

    load_id: str
    load_number: int | None
    departure_local: datetime | None  # naive DZ-local wall clock, as stored
    business_day: str | None  # ``YYYY-MM-DD`` DZ-local
    load: dict[str, Any] = field(repr=False)


class MatchResult(BaseModel):
    """The resolved jump for one clip (what the caller needs to create/route a job)."""

    role: str  #: ``"instructor"`` (handcam) or ``"external"`` (cameraman)
    staff_id: str
    staff_name: str | None = None
    load_id: str
    load_number: int | None = None
    jumper_index: int
    booking_id: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    media_package: str | None = None
    video_type: str | None = None
    #: Auto-edit package name (``selfie``/``external``/``video_only``/``photo_only``/
    #: ``ultimum``) or ``None`` when a purchased add-on can't be mapped. A booking
    #: that buys NO media now still gets a package — the role-default speculative one
    #: — with ``entitlement="preview_only"`` (design doc Path B: "film it anyway").
    package: str | None = None
    #: ``"edited_download"`` (media purchased) or ``"preview_only"`` (speculative
    #: capture — watermarked preview behind the paywall). Plain string equal to
    #: ``api.jobs.Entitlement`` values; this module never imports ``api``.
    entitlement: str = "edited_download"
    #: The jumper's media products, one per camera role — **only** when they hold more
    #: than one (a paid handcam package plus a speculative camera-flyer twin). Empty for
    #: every ordinary jump, which is what keeps a single-product job byte-identical:
    #: nothing branches on this except ``len(...) > 1``. When non-empty, :attr:`package`
    #: and :attr:`entitlement` above mirror the **primary** ref
    #: (:func:`primary_media_ref`), as ``POST /jobs`` requires.
    media_refs: list[MediaRefSpec] = []
    #: HOW ownership was established (an ``EVIDENCE_*`` constant). Informational — no
    #: pipeline logic branches on it — and the hook monitoring reads: anything other than
    #: :data:`EVIDENCE_WINDOW` was a judgement call, and
    #: :data:`EVIDENCE_OUT_OF_WINDOW_SAME_DAY` is the temporary compatibility path under
    #: review. Defaults to ``window`` so a hand-built result stays valid.
    evidence: str = EVIDENCE_WINDOW
    #: The full decision record behind :attr:`evidence` (see ``_decision_record``): clip,
    #: capture instant, load, departure/recorded flight, window, delta, jumper slot,
    #: booking/customer ids. Carries no name or email — it is written to logs and an audit
    #: file. Empty when the result was constructed directly rather than resolved.
    evidence_detail: dict[str, Any] = {}


class LoadJumper(BaseModel):
    """One jumper on a load, as the fan-out roster needs them.

    A flattened, ``api``-free projection of a ``loads.jumpers[]`` entry plus its
    ``customers`` doc: who they are, what they bought, and therefore which tier of the
    load-video offer they fall into. ``package``/``entitlement`` come from the same pure
    :func:`package_and_entitlement_for` a normal match uses, so a jumper's tier here can
    never disagree with the job their own footage would create.
    """

    jumper_index: int
    booking_id: str | None = None
    customer_id: str | None = None
    customer_email: str | None = None
    customer_name: str | None = None
    media_package: str | None = None
    video_type: str | None = None
    #: Auto-edit package the jumper's OWN footage would run through, or ``None`` when a
    #: purchase can't be mapped. Not the load master's package.
    package: str | None = None
    entitlement: str = "edited_download"

    @property
    def bought_media(self) -> bool:
        """Whether this jumper purchased media (so they already receive a gallery).

        The fan-out tier test: ``True`` → they get a load-video *tile* in the gallery
        they're already opening (never a second email); ``False`` → they get their own
        locked child gallery. Keyed on the *purchase* (``mediaPackage``), not on
        ``package``, which is non-``None`` for a speculative capture too.
        """
        return (self.media_package or "").strip().lower() not in ("", "none")


class LoadMatchResult(BaseModel):
    """A clip resolved to a LOAD rather than to a jumper — the spec-flight match.

    What the caller needs to open a load-master job and fan it out: which load, which
    staff member filmed it, and the whole manifest roster (:attr:`jumpers`) so the
    fan-out never has to re-read the database.
    """

    staff_id: str
    staff_name: str | None = None
    load_id: str
    load_number: int | None = None
    business_day: str | None = None
    #: Naive DZ-local departure wall clock, ISO-formatted (``None`` if the load has none).
    departure_local: str | None = None
    #: Every jumper manifested on the load, in manifest order.
    jumpers: list[LoadJumper] = []

    @property
    def label(self) -> str:
        """Human name for the load (``"Load 14"``), used for folders and intro cards."""
        return f"Load {self.load_number}" if self.load_number is not None else "Load"


def package_for(media_package: str | None, video_type: str | None) -> str | None:
    """Map a jumper's ``mediaPackage`` + ``videoType`` to an auto-edit package name.

    Pure. Returns one of ``selfie``/``external``/``video_only``/``photo_only``/
    ``ultimum``, or ``None`` when the booking buys no media (``mediaPackage`` ``none``)
    or when a video add-on is missing the ``videoType`` needed to pick a camera (the
    caller treats ``None`` as "flag — can't map"). Returns the plain string that equals
    ``api.jobs.Package``'s values, so this module never imports ``api`` (kept
    dependency-light, like :mod:`edl.validate`).
    """
    mp = (media_package or "").strip().lower()
    vt = (video_type or "").strip().lower()
    if mp in ("", "none"):
        return None
    if mp in ("photos", "photo", "photo-only", "photos-only", "photo_only"):
        return "photo_only"
    has_photos = "photo" in mp  # e.g. "video-photos"
    if vt == "both":
        return "ultimum"
    if not has_photos:  # plain video add-on, single camera either way
        return "video_only"
    if vt == "outside":
        return "external"
    if vt == "inside":
        return "selfie"
    return None  # video+photos but no camera side named → caller flags it


#: Package a speculative ("film it anyway") capture runs through, by camera role.
#: The instructor's handcam edits like a selfie booking; a cameraman's footage like
#: the camera-flyer product. Both always make videos — a preview needs something
#: watchable behind the paywall.
SPECULATIVE_PACKAGE_BY_ROLE = {"instructor": "selfie", "external": "external"}


def package_and_entitlement_for(
    media_package: str | None, video_type: str | None, role: str
) -> tuple[str | None, str]:
    """Map a jumper's booking to ``(package, entitlement)`` — the Path A/B fork.

    Pure, like :func:`package_for` (which it wraps and leaves untouched):

    * purchase mappable → ``(package, "edited_download")`` — Path A.
    * no purchase (``mediaPackage`` empty/``none``) → the role-default speculative
      package + ``"preview_only"`` — Path B, the "we filmed it anyway" job whose
      gallery is watermarked behind the paywall.
    * a purchase that can't be mapped (e.g. video+photos with no ``videoType``) →
      ``(None, "edited_download")`` so the caller still flags it for a human.

    Strings equal ``api.jobs.Package`` / ``api.jobs.Entitlement`` values; no ``api``
    import (dependency-light, like :mod:`edl.validate`).
    """
    mapped = package_for(media_package, video_type)
    if mapped is not None:
        return mapped, "edited_download"
    if (media_package or "").strip().lower() in ("", "none"):
        return SPECULATIVE_PACKAGE_BY_ROLE.get(role, "selfie"), "preview_only"
    return None, "edited_download"


# -- per-add-on media refs (the mixed jump) ---------------------------------- #
#
# ``package_for`` above reads the jumper's DERIVED UNION (``mediaPackage`` +
# ``videoType``), which is all a single-product jumper needs. It cannot describe a jumper
# holding TWO media products — a paid handcam package plus a speculative camera-flyer
# twin — because the union of "inside" and "outside" is ``videoType: 'both'``, which is
# indistinguishable from the genuine two-camera ``ultimum`` product. Those are opposite
# things: ``ultimum`` *merges* the two angles into shared deliverables, and a merged clip
# cannot be half-locked, which is exactly what a paid + spec pair needs.
#
# So a mixed jumper is resolved from ``jumper.mediaAddOnRefs`` — the BookingPackage docs
# actually sold — one ref per product, mirroring SkydiveOS's
# ``utils/autoEditPackage.resolveAutoEditPackage`` applied to ONE doc at a time.
# Identification is **structural, never by name** (their BUG 156): ``mediaType``,
# ``videoAngle`` (+ scene ``cameraSource``), ``isTwoCameraVideo``, and ``specOf``.
#
# When ``mediaAddOnRefs`` is empty — every jumper manifested before it shipped — nothing
# here fires and the union path above is used unchanged. That leaves one knowingly
# unresolvable case: a legacy row whose union is ``both`` is still read as ``ultimum``,
# because without the per-add-on docs there is no signal that separates the two.


class MediaRefSpec(BaseModel):
    """One media product on a jump: which camera films it, and whether it was bought.

    The matcher's projection of ``api.jobs.MediaRef`` (plain strings; this module never
    imports ``api``). ``POST /jobs`` takes a list of these as ``media_refs``.
    """

    role: str  #: ``"instructor"`` (handcam) or ``"external"`` (cameraman)
    package: str  #: auto-edit package name for THIS product
    entitlement: str  #: ``"edited_download"`` (paid) or ``"preview_only"`` (spec twin)


def _addon_angle(doc: dict[str, Any]) -> tuple[bool, bool]:
    """``(has_inside, has_outside)`` for one add-on — its explicit angle, else its scenes.

    Mirrors SkydiveOS ``utils/mediaDerivation.deriveMediaFields``: ``videoAngle`` is
    authoritative for a package and only ``any``/absent defers to ``scenes[].cameraSource``.
    """
    angle = (doc.get("videoAngle") or "").strip().lower()
    if angle in ("inside", "outside", "both"):
        return angle in ("inside", "both"), angle in ("outside", "both")
    inside = outside = False
    for scene in doc.get("scenes") or []:
        source = ((scene or {}).get("cameraSource") or "").strip().lower()
        if source in ("selfie", "instructor"):
            inside = True
        elif source in ("external", "ground"):
            outside = True
    return inside, outside


def package_for_addon(doc: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve ONE media add-on to ``(package, role)``, or ``(None, None)``.

    Pure. The per-product half of :func:`package_for`, keyed on the non-editable
    BookingPackage fields rather than on the jumper's union:

    * ``isTwoCameraVideo`` → ``("ultimum", None)``. It has no single role — Ultimate is
      one product spanning both cameras — and a caller building media refs treats that
      as "not a mixed set" (see :func:`media_refs_for_jumper`).
    * photos only → ``photo_only``, filmed by the handcam (``instructor``).
    * video (± photos) + ``inside`` → ``selfie``; + ``outside`` → ``external``.
    * anything else — no media signal, an unclassifiable legacy add-on, or a ``both``
      angle without the two-camera flag — → ``(None, None)``. Never guess a package for
      footage (their contract: refuse and let a human look).
    """
    if doc.get("isTwoCameraVideo") is True:
        return "ultimum", None
    media_type = (doc.get("mediaType") or "").strip().lower()
    has_video = media_type in ("video", "video-photos")
    has_photo = media_type in ("photos", "video-photos")
    if not has_video:
        # A photo add-on is shot by whoever is closest — the instructor's handcam.
        return ("photo_only", CAMERA_ROLE_INSTRUCTOR) if has_photo else (None, None)
    inside, outside = _addon_angle(doc)
    if inside and not outside:
        return "selfie", CAMERA_ROLE_INSTRUCTOR
    if outside and not inside:
        return "external", CAMERA_ROLE_EXTERNAL
    # Both angles with no two-camera flag, or no angle at all: ambiguous by contract.
    return None, None


def is_spec_addon(doc: dict[str, Any]) -> bool:
    """Whether this add-on is a spec ("we filmed it anyway") twin — ``specOf`` set.

    Spec-ness is a RELATIONSHIP on the SkydiveOS side, and the ``preview_only``
    entitlement is *derived* from it rather than stored, precisely so the two cannot
    drift. This mirrors that derivation.
    """
    return doc.get("specOf") is not None


def resolve_media_products(
    addon_docs: Sequence[dict[str, Any]],
    *,
    filming_roles: Sequence[str] | None = None,
) -> tuple[str | None, str | None, list[MediaRefSpec]]:
    """Resolve a jumper's media add-ons to ``(package, entitlement, media_refs)``.

    Pure, and the authority whenever it answers: ``(None, None, [])`` means "these
    add-ons don't describe a product I can classify — use the jumper's derived union",
    which is what keeps a pre-``mediaAddOnRefs`` row byte-identical to before.

    ``filming_roles`` names the cameras that actually rolled on this jump — the
    instructor's handcam (always, on a tandem) and the cameraman's, when one was
    assigned. It exists because of the module's central rule, applied here per camera:

        **Whoever filmed gets a deliverable. Covered by an add-on → that add-on's
        entitlement. Not covered → locked.**

    That is the "film it anyway" doctrine (design doc Path B) resolved per camera instead
    of per job, and it is what a jumper who bought nothing but had a camera flyer sent up
    needs: the handcam's edit AND the cameraman's, both watermarked, on one link. It
    fails closed by construction — an uncovered camera is never given away, only offered.
    Pass ``None`` (the default) to take the add-ons exactly as they are, with no
    synthesis, which is what the pure unit tests and :func:`media_refs_for_jumper` use.

    Shapes that come back:

    * **no add-ons at all, but cameras that filmed** → every one of them, locked. This is
      the jumper who bought nothing: the classic Path B job, plus the cameraman's angle
      when ops sent a flyer up on the seat anyway.
    * **one product** → ``(package, entitlement, [])``. Nothing downstream branches; an
      ordinary single-product job. This case matters even though it looks trivial: it is
      the only place a lone **spec twin** is recognised. The union cannot see spec-ness —
      a twin carries the same ``mediaType`` as the product it twins (it *is* that
      product, at $0), so the union reads it as a purchase and hands the unpaid edit over
      clean.
    * **two products on distinct cameras** → the mixed pair, in canonical
      :data:`MEDIA_REF_ROLES` order, with ``package``/``entitlement`` mirroring the
      **primary** ref as ``POST /jobs`` requires.
    * **``(None, None, [])``** → defer to the union:

      - no add-on resolves at all,
      - any add-on can't be classified (refuse, never guess),
      - ``ultimum`` alongside another product — Ultimate IS the two-camera product, it
        merges the angles, and is never half of a pair,
      - two add-ons wanting the SAME camera role. One role is one raw folder and one
        render pass carrying one lock state, so two products on one camera cannot be told
        apart downstream; ``photo_only`` beside a ``selfie`` package is the common shape
        of this and is *deliberately* not a mixed set — photos come from the paid ref only.
    """
    resolved: list[tuple[str, str | None, bool]] = []  # (package, role, is_spec)
    for doc in addon_docs:
        if not doc:
            continue
        package, role = package_for_addon(doc)
        if package is None:
            return None, None, []  # unclassifiable → the union decides, or a human does
        resolved.append((package, role, is_spec_addon(doc)))

    def _entitlement(is_spec: bool) -> str:
        return "preview_only" if is_spec else "edited_download"

    # Ultimate spans both cameras, so it has no single role and no other product can sit
    # beside it — and no camera is "uncovered", so nothing is synthesised for it either.
    if any(role is None for _p, role, _s in resolved):
        if len(resolved) > 1:
            return None, None, []
        package, _role, is_spec = resolved[0]
        return package, _entitlement(is_spec), []

    refs: dict[str, MediaRefSpec] = {}
    for package, role, is_spec in resolved:
        assert role is not None  # the ultimum branch above took every None
        if role in refs:
            return None, None, []
        refs[role] = MediaRefSpec(
            role=role, package=package, entitlement=_entitlement(is_spec)
        )

    # A camera that rolled but sold nothing still gets an edit — behind the paywall.
    for role in filming_roles or ():
        if role in refs or role not in SPECULATIVE_PACKAGE_BY_ROLE:
            continue
        refs[role] = MediaRefSpec(
            role=role,
            package=SPECULATIVE_PACKAGE_BY_ROLE[role],
            entitlement="preview_only",
        )

    ordered = [refs[r] for r in MEDIA_REF_ROLES if r in refs]
    if not ordered:  # no add-ons AND no filming roles given → the union decides
        return None, None, []
    if len(ordered) == 1:
        return ordered[0].package, ordered[0].entitlement, []
    primary = primary_media_ref(ordered)
    assert primary is not None  # non-empty by construction
    return primary.package, primary.entitlement, ordered


def media_refs_for_jumper(addon_docs: Sequence[dict[str, Any]]) -> list[MediaRefSpec]:
    """Just the ref set from :func:`resolve_media_products` (``[]`` unless mixed)."""
    return resolve_media_products(addon_docs)[2]


def primary_media_ref(refs: Sequence[MediaRefSpec]) -> MediaRefSpec | None:
    """The ref whose package/entitlement the JOB's top-level fields must mirror.

    Order-independent, and the same rule as ``api.jobs.primary_ref_of``: the paid
    product wins, then the instructor's. It must not depend on array order — the
    primary ref keeps the plain deliverable names while every other ref is namespaced
    ``<role>_<name>``, so a re-created job that reordered its refs would rename its
    deliverables and the gallery would lose them.
    """
    if not refs:
        return None
    return min(
        refs,
        key=lambda r: (
            r.entitlement != "edited_download",
            r.role != CAMERA_ROLE_INSTRUCTOR,
        ),
    )


def _staff_id_variants(staff_id: Any) -> list[Any]:
    """Both representations of a Mongo id, for equality matching.

    A QR payload (or a :class:`MatchResult`) carries an ``_id`` as a *string*,
    while the documents store raw ObjectIds — a bare equality match on the string
    finds nothing. Returns the value as given plus, for a 24-hex string, its
    ObjectId form (``bson`` ships with pymongo; imported lazily so the pure half
    of this module stays dependency-light).
    """
    variants: list[Any] = [staff_id]
    if isinstance(staff_id, str) and re.fullmatch(r"[0-9a-fA-F]{24}", staff_id):
        try:
            from bson import ObjectId

            variants.append(ObjectId(staff_id))
        except (ImportError, ValueError):
            pass  # no driver / not a valid ObjectId — the string variant stands alone
    return variants


def _full_name(doc: dict[str, Any] | None) -> str | None:
    """``{firstName, lastName}`` → ``"Marc Tremblay"``, or ``None`` if neither is set.

    Both ``staffs`` and ``customers`` docs carry the name this way.
    """
    if not doc:
        return None
    return " ".join(p for p in (doc.get("firstName"), doc.get("lastName")) if p) or None


def _in_window(captured_local: datetime, departure_local: datetime | None) -> bool:
    """Whether a capture instant falls in a load's SCHEDULED window (DZ-local naive).

    The fallback test, kept for :func:`select_load` (spec flights, which have only a
    scheduled time to go on) and for candidates with no recorded flight. Jumper-keyed
    ownership goes through :func:`flight_window`.
    """
    if departure_local is None:
        return False
    return (departure_local - WINDOW_PRE) <= captured_local <= (departure_local + WINDOW_POST)


def flight_window(candidate: _Timed) -> tuple[datetime, datetime] | None:
    """The window a clip must fall in to belong to this load, or ``None`` if unknowable.

    **Recorded flight preferred, scheduled window as fallback.** When the load carries
    ``actualTakeoffTime`` the window is ``[takeoff − 20 min, (landing or takeoff + 60 min)
    + 10 min]`` — the real ~25 min a tandem load is airborne, plus boarding and gear-off
    grace. Otherwise it falls back to ``departureTime ± (30 min, 150 min)``.

    Why this distinction carries the audit's §8 case: a 09:30 load that landed at 09:58
    has a window ending 10:08, so a 10:15 clip belongs to *neither* it nor a 14:00 load
    and ownership is refused. Under the scheduled fallback that same clip is 45 min into a
    150-minute window and is claimed — which is how one customer's interview reached
    another customer's edit. **So §3-C is only closed for loads whose actual flight times
    are recorded**; where ops leaves them blank the fallback window still overlaps
    neighbouring loads and the clip is claimed as before. That is a data dependency, not
    a code one, and it is deliberately not papered over with a narrower guess.

    ``None`` (no recorded flight AND no scheduled departure) means the load offers no
    timing evidence at all — such a candidate can never satisfy the window check.
    """
    takeoff = getattr(candidate, "actual_takeoff_local", None)
    if takeoff is not None:
        landing = getattr(candidate, "landing_local", None) or (takeoff + MAX_FLIGHT)
        return takeoff - TAKEOFF_PRE, landing + LANDING_POST
    if candidate.departure_local is not None:
        return (
            candidate.departure_local - WINDOW_PRE,
            candidate.departure_local + WINDOW_POST,
        )
    return None


def _captured_in_flight_window(captured_local: datetime, candidate: _Timed) -> bool:
    window = flight_window(candidate)
    return window is not None and window[0] <= captured_local <= window[1]


def _previous_day(day: str) -> str:
    """``YYYY-MM-DD`` of the day before ``day`` (``day`` itself on a parse failure)."""
    try:
        return (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat()
    except ValueError:
        return day


@dataclass(frozen=True)
class OwnershipDecision:
    """A resolved owner plus *how* it was established — the audit record of a match."""

    candidate: Candidate
    evidence: str  #: one of the ``EVIDENCE_*`` constants
    detail: dict[str, Any] = field(default_factory=dict)


def _decision_record(
    captured_local: datetime,
    candidate: Candidate,
    *,
    evidence: str,
    considered: int,
    clip_ref: str | None,
) -> dict[str, Any]:
    """Everything needed to investigate this decision later, as one flat record.

    Deliberately complete for the ``out_of_window_same_day`` review (approved
    2026-08-11): clip, capture instant, chosen load + its departure and recorded flight,
    the jumper slot and its booking/customer, and the signed delta between the clip and
    the window it missed. Contains no customer *name* or email — this is written to logs
    and an audit file, and PII does not belong in either.
    """
    window = flight_window(candidate)
    delta_s: float | None = None
    if window is not None:
        if captured_local < window[0]:
            delta_s = (captured_local - window[0]).total_seconds()
        elif captured_local > window[1]:
            delta_s = (captured_local - window[1]).total_seconds()
        else:
            delta_s = 0.0
    return {
        "event": "ownership_decision",
        "evidence": evidence,
        "clip_ref": clip_ref,
        "captured_local": captured_local.isoformat(),
        "load_id": candidate.load_id,
        "load_number": candidate.load_number,
        "departure_local": (
            candidate.departure_local.isoformat() if candidate.departure_local else None
        ),
        "actual_takeoff_local": (
            candidate.actual_takeoff_local.isoformat()
            if candidate.actual_takeoff_local
            else None
        ),
        "landing_local": (
            candidate.landing_local.isoformat() if candidate.landing_local else None
        ),
        "window_local": [window[0].isoformat(), window[1].isoformat()] if window else None,
        "window_source": (
            "recorded_flight" if candidate.actual_takeoff_local else "scheduled_departure"
        ),
        "seconds_outside_window": delta_s,
        "jumper_index": candidate.jumper_index,
        "role": candidate.role,
        "booking_id": (
            str(candidate.jumper.get("booking"))
            if candidate.jumper.get("booking") is not None
            else None
        ),
        "customer_id": (
            str(candidate.jumper.get("customer"))
            if candidate.jumper.get("customer") is not None
            else None
        ),
        "media_package": candidate.jumper.get("mediaPackage"),
        "candidates_considered": considered,
    }


def evaluate_ownership(
    candidates: list[Candidate],
    captured_local: datetime,
    *,
    captured_day: str,
    clip_ref: str | None = None,
) -> OwnershipDecision:
    """Establish which jumper owns a clip, or raise. Pure (logging aside).

    The Phase 3 rule (``DESIGN_MATCH_OWNERSHIP.md``): a candidate is returned only when
    ownership is **established**, never merely **unopposed**. Before this, two
    short-circuits accepted a lone candidate with no time check at all and a third
    re-admitted candidates from other days — between them the cause of every confirmed
    cross-customer leak in ``AUDIT_MEDIA_MATCH_ISOLATION.md``.

    Four checks, in order:

    **T1 — day.** The candidate's ``businessDate`` must be the clip's DZ-local capture
    day, or the day *before* it with the clip still inside the load's window (a jump that
    took off before local midnight; :meth:`resolve_load_for_staff` has always handled
    this for spec flights and the jumper path never did).

    **T2 — window.** The clip must fall inside the chosen candidate's
    :func:`flight_window`. One exception, and it is a temporary compatibility path:
    a **single same-day candidate** is accepted out-of-window as
    :data:`EVIDENCE_OUT_OF_WINDOW_SAME_DAY` — logged at WARNING with the full decision
    record and counted — because that is a rescheduled customer's early interview with
    nothing on the day to confuse it with (§3-A). It is not the final rule.

    **T3 — identity.** The chosen slot must carry a booking or customer reference, or it
    cannot be reconciled downstream.

    **T4 — no contradiction.** Several in-window candidates are resolved causally (latest
    departure at or before the clip) only when the two best departures are at least
    :data:`MIN_DEPARTURE_GAP` apart; closer than that is a guess and refuses.

    Raises :class:`NoBookingMatch` or :class:`AmbiguousMatch` — the same exceptions as
    before, so every refuse-and-flag caller is unchanged.
    """
    if not candidates:
        raise NoBookingMatch("no matchable load-jumper for this camera + capture time")

    # ── T1: the capture day (or the previous day, still in flight) ───────────
    previous_day = _previous_day(captured_day)
    eligible = [
        c
        for c in candidates
        if c.business_day == captured_day
        or (
            c.business_day == previous_day
            and _captured_in_flight_window(captured_local, c)
        )
    ]
    if not eligible:
        raise NoBookingMatch(
            f"{len(candidates)} candidate jump(s) for this staff member, none on the "
            f"capture day ({captured_day}); refusing to attach the clip to another day's "
            "customer"
        )

    # ── T2: the flight window ───────────────────────────────────────────────
    in_window = [c for c in eligible if _captured_in_flight_window(captured_local, c)]
    evidence = EVIDENCE_WINDOW

    if not in_window:
        # The temporary compatibility path. Deliberately narrow: exactly ONE slot on the
        # capture day, so there is no other customer this clip could belong to.
        same_day = [c for c in eligible if c.business_day == captured_day]
        if len(same_day) != 1:
            raise AmbiguousMatch(
                f"{len(eligible)} candidate jump(s) on {captured_day}, none whose flight "
                f"window contains the clip ({captured_local:%Y-%m-%d %H:%M}); refusing to "
                "attach it to the nearest one",
                eligible,
            )
        in_window = same_day
        evidence = EVIDENCE_OUT_OF_WINDOW_SAME_DAY

    # ── T4: no contradictory candidate ──────────────────────────────────────
    if len(in_window) > 1:
        dated = [c for c in in_window if c.departure_local is not None]
        if len(dated) != len(in_window):
            raise AmbiguousMatch(
                f"{len(in_window)} candidate jumps are in-window and at least one has no "
                "departure time to order them by; refusing to guess",
                in_window,
            )
        departed = [c for c in dated if c.departure_local <= captured_local]  # type: ignore[operator]
        pool = departed or dated
        ordered = sorted(
            pool, key=lambda c: c.departure_local, reverse=bool(departed)  # type: ignore[arg-type,return-value]
        )
        best = ordered[0]
        rivals = [c for c in ordered[1:] if c.departure_local != best.departure_local]
        if rivals:
            gap = abs(best.departure_local - rivals[0].departure_local)  # type: ignore[operator]
            if gap < MIN_DEPARTURE_GAP:
                raise AmbiguousMatch(
                    f"two candidate flights depart {gap} apart (closer than "
                    f"{MIN_DEPARTURE_GAP}), so which one this clip belongs to is a guess; "
                    "refusing",
                    in_window,
                )
        tied = [c for c in pool if c.departure_local == best.departure_local]
        if len(tied) > 1:
            raise AmbiguousMatch(
                f"{len(tied)} jumpers share the same departure "
                f"({best.departure_local:%Y-%m-%d %H:%M}); refusing to guess",
                tied,
            )
        in_window = [best]
        evidence = EVIDENCE_CAUSAL_TIEBREAK

    chosen = in_window[0]
    record = _decision_record(
        captured_local, chosen, evidence=evidence,
        considered=len(candidates), clip_ref=clip_ref,
    )

    # ── T3: the identity the rest of the chain joins on ─────────────────────
    if chosen.booking_ref is None:
        raise NoBookingMatch(
            f"resolved load {chosen.load_number} jumper {chosen.jumper_index} but it has "
            "no booking to attach a job to; link manually"
        )

    if evidence == EVIDENCE_OUT_OF_WINDOW_SAME_DAY:
        # The temporary compatibility path, made visible: one WARNING per acceptance,
        # carrying the whole decision record, plus the counter the review will read.
        logger.warning("out_of_window_accept %s", json.dumps(record, sort_keys=True))
    else:
        logger.info("ownership_decision %s", json.dumps(record, sort_keys=True))
    return OwnershipDecision(candidate=chosen, evidence=evidence, detail=record)


def _narrow_by_time(
    pool: list[_Timed],
    captured_local: datetime,
    *,
    captured_day: str,
) -> tuple[list[_Timed], list[_Timed]]:
    """Narrow LOAD candidates to the one flight a clip belongs to. Pure; never raises.

    Returns ``(survivors, day_pool)``: ``survivors`` is empty when nothing fell in a
    flight window, one element on a clean resolution, and >1 only on a genuine tie
    *within a single departure instant*. ``day_pool`` is the day-narrowed input, which the
    caller quotes in its error message.

    Now used only by :func:`select_load` (the spec-flight path). The jumper-keyed path
    moved to :func:`evaluate_ownership`, which enforces the day and the window rather than
    treating them as tie-breakers — a customer's video may not rest on "nothing else
    claimed it". Here the window is unconditional anyway (a load-only match has no
    evidence *but* the timestamp), and ``same_day or pool`` is safe because of that: it
    exists so a clip captured just after local midnight can still reach the previous day's
    last load.
    """
    same_day = [c for c in pool if c.business_day == captured_day]
    narrowed = same_day or pool

    in_window = [c for c in narrowed if _in_window(captured_local, c.departure_local)]
    if len(in_window) <= 1:
        return in_window, narrowed

    # Windows OVERLAP by design (WINDOW_POST is 2.5 h, far wider than the gap between
    # loads), so resolve causally rather than refusing: the clip belongs to the latest
    # departure at or before it. A clip predating every departure falls back to the
    # earliest — it can only belong to the next flight up. (_in_window already excluded
    # a None departure, so every one here is set.)
    departures = [c.departure_local for c in in_window if c.departure_local is not None]
    departed = [d for d in departures if d - WINDOW_PRE <= captured_local]
    chosen = max(departed) if departed else min(departures)
    return [c for c in in_window if c.departure_local == chosen], narrowed


def select_match(
    candidates: list[Candidate],
    captured_local: datetime,
    *,
    captured_day: str,
    clip_ref: str | None = None,
) -> Candidate:
    """Pick the single (load, jumper, role) a clip belongs to, or raise.

    Thin wrapper over :func:`evaluate_ownership` — see it for the rule. Kept as the
    module's entry point so every existing caller and test is unchanged; use
    :func:`evaluate_ownership` directly when the *evidence* matters (as
    :meth:`FootageMatcher.resolve_for_staff` does, to stamp
    :attr:`MatchResult.evidence`).
    """
    return evaluate_ownership(
        candidates, captured_local, captured_day=captured_day, clip_ref=clip_ref
    ).candidate


def select_load(
    candidates: list[LoadCandidate],
    captured_local: datetime,
    *,
    captured_day: str,
) -> LoadCandidate:
    """Pick the single load a clip belongs to with no jumper to key on, or raise.

    The spec-flight counterpart of :func:`select_match`, and pure for the same reason.
    A camera flyer sent up on spec holds no slot on the load, so the jumper predicate
    yields nothing and the *timestamp is the only evidence available*. That makes the
    flight window **mandatory** here (unlike :func:`select_match`, where a lone
    jumper-keyed candidate is accepted on the strength of the jumper predicate alone):
    footage shot between loads must resolve to no load rather than to the nearest one.

    Ambiguity between overlapping windows is resolved causally, exactly as for jumpers.
    Only two loads sharing one departure instant is real ambiguity, and that raises.
    """
    if not candidates:
        raise NoLoadMatch("no matchable load on this capture day")

    survivors, pool = _narrow_by_time(
        candidates, captured_local, captured_day=captured_day
    )
    if len(survivors) == 1:
        return survivors[0]
    if not survivors:
        raise NoLoadMatch(
            f"{len(pool)} load(s) that day, none whose flight window contains the clip "
            f"({captured_local:%Y-%m-%d %H:%M}); refusing to attach it to the nearest one"
        )
    raise AmbiguousMatch(
        f"{len(survivors)} loads share the same departure "
        f"({survivors[0].departure_local:%Y-%m-%d %H:%M}); refusing to guess",
        [],
    )


class FootageMatcher:
    """Resolve a clip (camera serial + capture time) to its load-jumper, role, booking.

    Reads the shared SkydiveOS DB (``staffs``, ``loads``, ``customers``). Connection is
    lazy and disabled when ``MONGO_URL`` is unset (every :meth:`resolve` raises
    :class:`RegistryUnavailable`), so importing this never requires the driver or a DB.
    ``clock_tz`` (default ``$CAMERA_CLOCK_TZ``) converts the clip's true-UTC
    ``captured_at`` to the dropzone-local day/time the loads are stored in.
    """

    def __init__(
        self,
        mongo_url: str | None = None,
        *,
        db_name: str | None = None,
        clock_tz: str | None = None,
    ) -> None:
        self._mongo_url = (
            mongo_url if mongo_url is not None else (os.environ.get("MONGO_URL") or None)
        )
        # DB_NAME is the SkydiveOS Node backend's name for the same database, and both
        # services share one .env — see api.config for the incident this alias closes.
        self._db_name = (
            db_name or os.environ.get("MONGO_DB") or os.environ.get("DB_NAME") or DEFAULT_DB
        )
        tz_name = clock_tz if clock_tz is not None else os.environ.get("CAMERA_CLOCK_TZ")
        self._tz = ZoneInfo(tz_name) if tz_name else None
        self._client: Any | None = None
        self._db: Any | None = None

    @property
    def enabled(self) -> bool:
        """True when a Mongo URL is configured."""
        return self._mongo_url is not None

    def _database(self) -> Any:
        if self._db is None:
            try:
                from pymongo import MongoClient
            except ImportError as e:  # pragma: no cover - only without the driver
                raise RuntimeError(
                    "pymongo is required for footage matching; install it with "
                    "'uv pip install \"pymongo[srv]\"'."
                ) from e
            self._client = MongoClient(self._mongo_url)
            self._db = self._client[self._db_name]
        return self._db

    def _to_local(self, captured_at: datetime | str) -> datetime:
        """A clip's true-UTC capture instant → naive DZ-local wall clock.

        Loads store ``businessDate``/``departureTime`` as the dropzone's local wall
        clock; ``captured_at`` from discovery is a real UTC instant. We convert with
        ``clock_tz`` and drop the tzinfo so both sides compare like-for-like.
        """
        if isinstance(captured_at, str):
            captured_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        if self._tz is not None:
            captured_at = captured_at.astimezone(self._tz)
        return captured_at.replace(tzinfo=None)

    @staticmethod
    def _staff_for_camera(db: Any, camera_id: str) -> dict[str, Any]:
        """The staff member who owns this camera, by exact serial or serial suffix.

        A "camera id" in /ingest is the **trailing serial digits** the camera advertises
        over BLE — a GoPro named ``GoPro 4313`` scans as ``4313`` — while
        ``staffs.goproSerial`` normally holds the full printed serial
        (``C3504224544313``). An exact-match-only lookup therefore finds nothing for
        every real camera, silently degrading to the registry's static role hint. So we
        fall back to a suffix match, which is exactly what the two representations
        share.

        Refuses on a suffix that fits more than one staff member (two cameras whose
        serials end the same way) rather than picking one — mis-owning a camera
        mis-delivers a customer's video.
        """
        exact = db[STAFFS].find_one({"goproSerial": camera_id})
        if exact:
            return dict(exact)
        # ``$options: "i"`` because serials are quoted inconsistently by hand.
        pattern = f"{re.escape(camera_id)}$"
        matches = list(db[STAFFS].find({"goproSerial": {"$regex": pattern, "$options": "i"}}))
        if not matches:
            raise UnknownCamera(f"no staff owns camera serial {camera_id!r}")
        if len(matches) > 1:
            owners = [str(m.get("goproSerial")) for m in matches]
            raise AmbiguousMatch(
                f"camera id {camera_id!r} is the tail of {len(matches)} staff serials "
                f"({owners}); refusing to guess the owner",
                [],
            )
        logger.info(
            "camera %s matched staff serial %r by suffix", camera_id, matches[0].get("goproSerial")
        )
        return dict(matches[0])

    @staticmethod
    def _naive_local(value: Any) -> datetime | None:
        """A stored Mongo datetime → naive local (best effort; ``None`` if not a date)."""
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return None

    def resolve(
        self,
        gopro_serial: str,
        captured_at: datetime | str,
        *,
        clip_ref: str | None = None,
    ) -> MatchResult:
        """Resolve one clip to its jump. Raises a :class:`FootageMatchError` subclass.

        See the module docstring for the match key. On success the returned
        :class:`MatchResult` carries the per-jump ``role`` (the authority over the
        registry's static hint), the customer's email/name, the mapped package, and the
        ``evidence`` that established ownership.

        ``clip_ref`` (the S3 key or filename) is carried into the decision record purely
        so an investigation can start from "which clip?" — the matcher never keys on it.
        """
        if not self.enabled:
            raise RegistryUnavailable("MONGO_URL unset; cannot read the shared DB")

        db = self._database()
        staff = self._staff_for_camera(db, gopro_serial)
        return self.resolve_for_staff(
            staff["_id"], captured_at, staff_name=_full_name(staff), clip_ref=clip_ref
        )

    def resolve_load(self, gopro_serial: str, captured_at: datetime | str) -> LoadMatchResult:
        """Resolve one clip to its LOAD (spec flight), by camera serial.

        The :meth:`resolve` counterpart for a camera flyer with no assigned customer.
        Raises a :class:`FootageMatchError` subclass — notably :class:`NotSpecFlight`
        when the camera's owner *does* hold a jumper slot on the resolved load, which
        means :meth:`resolve` is the right entry point for that footage.
        """
        if not self.enabled:
            raise RegistryUnavailable("MONGO_URL unset; cannot read the shared DB")

        db = self._database()
        staff = self._staff_for_camera(db, gopro_serial)
        return self.resolve_load_for_staff(
            staff["_id"], captured_at, staff_name=_full_name(staff)
        )

    def resolve_for_staff(
        self,
        staff_id: Any,
        captured_at: datetime | str,
        *,
        staff_name: str | None = None,
        clip_ref: str | None = None,
    ) -> MatchResult:
        """Resolve one clip to its jump when the staff member is already known.

        The QR session-marker flow (:mod:`ingest.qr`) lands here: the filmed QR
        supplies *who* (the SkydiveOS ``staffs._id``), and this still decides
        *which jump and which role* from the loads — a staff member can be the
        tandem instructor on one jump and the cameraman on the next.
        ``staff_id`` may be the raw ObjectId or its string form (a QR payload is
        a string; the loads store ObjectIds) — both are matched.

        Ownership goes through :func:`evaluate_ownership`, so a clip whose owner cannot be
        established is refused rather than attached to the nearest jump; ``clip_ref``
        labels the decision record for later investigation.
        """
        if not self.enabled:
            raise RegistryUnavailable("MONGO_URL unset; cannot read the shared DB")

        db = self._database()
        staff_ids = _staff_id_variants(staff_id)
        if staff_name is None:
            staff_name = _full_name(db[STAFFS].find_one({"_id": {"$in": staff_ids}}))

        captured_local = self._to_local(captured_at)
        captured_day = captured_local.date().isoformat()

        # Every load where this staff is a jumper's instructor or cameraman.
        cursor = db[LOADS].find(
            {
                "$or": [
                    {"jumpers.instructor": {"$in": staff_ids}},
                    {"jumpers.assignedCameraman": {"$in": staff_ids}},
                ]
            }
        )
        candidates: list[Candidate] = []
        for load in cursor:
            if str(load.get("status", "")).lower() not in _MATCHABLE_STATUSES:
                continue
            biz = self._naive_local(load.get("businessDate"))
            dep = self._naive_local(load.get("departureTime"))
            # The load's RECORDED flight, when ops entered it: it narrows the ownership
            # window from the scheduled ±2.5 h to the ~25 min the plane was actually up,
            # which is what stops a clip filmed between loads being claimed by one of
            # them (see ``flight_window``).
            takeoff = self._naive_local(load.get("actualTakeoffTime"))
            landing = self._naive_local(load.get("landingTime"))
            for idx, jumper in enumerate(load.get("jumpers", [])):
                if jumper.get("instructor") in staff_ids:
                    role = "instructor"
                elif jumper.get("assignedCameraman") in staff_ids:
                    role = "external"
                else:
                    continue
                candidates.append(
                    Candidate(
                        load_id=str(load["_id"]),
                        load_number=load.get("loadNumber"),
                        departure_local=dep,
                        business_day=biz.date().isoformat() if biz else None,
                        jumper_index=idx,
                        jumper=jumper,
                        role=role,
                        actual_takeoff_local=takeoff,
                        landing_local=landing,
                    )
                )

        decision = evaluate_ownership(
            candidates, captured_local, captured_day=captured_day, clip_ref=clip_ref
        )
        return self._build_result(db, staff_id, staff_name, decision)

    def resolve_load_for_staff(
        self,
        staff_id: Any,
        captured_at: datetime | str,
        *,
        staff_name: str | None = None,
    ) -> LoadMatchResult:
        """Resolve one clip to its LOAD, for a camera flyer with no assigned customer.

        This is the **spec-flight** match, and the one place in this module that reads a
        load *by time* rather than by jumper. :meth:`resolve_for_staff` filters the
        ``loads`` query by "this staff is some jumper's ``instructor`` or
        ``assignedCameraman``" before timestamps are consulted at all, so on a spec flight
        it returns zero candidates and raises :class:`NoBookingMatch`. There is no crew
        field on a load document to key on either, so the clip's capture instant is the
        only available evidence — hence the query below is a window around it, and
        :func:`select_load` insists the window actually contains the clip.

        On success the whole manifest roster rides along (:attr:`LoadMatchResult.jumpers`)
        so the fan-out never has to re-read the database.

        Raises :class:`NoLoadMatch` when no load's window fits, :class:`AmbiguousMatch`
        on two loads sharing a departure instant, and :class:`NotSpecFlight` when this
        staff member *does* hold a jumper slot on the resolved load — that footage is a
        paying customer's product and :meth:`resolve_for_staff` owns it.
        """
        if not self.enabled:
            raise RegistryUnavailable("MONGO_URL unset; cannot read the shared DB")

        db = self._database()
        staff_ids = _staff_id_variants(staff_id)
        if staff_name is None:
            staff_name = _full_name(db[STAFFS].find_one({"_id": {"$in": staff_ids}}))

        captured_local = self._to_local(captured_at)
        captured_day = captured_local.date().isoformat()

        # Loads whose flight window COULD contain this clip, straight from the index:
        # captured_local ∈ [dep − WINDOW_PRE, dep + WINDOW_POST]  ⟺
        # dep ∈ [captured_local − WINDOW_POST, captured_local + WINDOW_PRE].
        # Deliberately not a whole-day scan — this also picks up the previous day's last
        # load for a clip captured just after local midnight. (Loads store DZ-local naive
        # wall clock, which is what ``captured_local`` now is.)
        cursor = db[LOADS].find(
            {
                "departureTime": {
                    "$gte": captured_local - WINDOW_POST,
                    "$lte": captured_local + WINDOW_PRE,
                }
            }
        )
        candidates: list[LoadCandidate] = []
        for load in cursor:
            if str(load.get("status", "")).lower() not in _MATCHABLE_STATUSES:
                continue
            biz = self._naive_local(load.get("businessDate"))
            candidates.append(
                LoadCandidate(
                    load_id=str(load["_id"]),
                    load_number=load.get("loadNumber"),
                    departure_local=self._naive_local(load.get("departureTime")),
                    business_day=biz.date().isoformat() if biz else None,
                    load=load,
                )
            )

        chosen = select_load(candidates, captured_local, captured_day=captured_day)

        # The spec-flight test: a flyer going up on spec fills no slot on the manifest.
        # If he is somebody's instructor or cameraman here, this footage is that
        # customer's product — refuse rather than ALSO offering it to the whole load.
        for idx, jumper in enumerate(chosen.load.get("jumpers") or []):
            for slot in ("instructor", "assignedCameraman"):
                if jumper.get(slot) in staff_ids:
                    raise NotSpecFlight(
                        f"staff is the {slot} of jumper {idx} on load "
                        f"{chosen.load_number}; not a spec flight"
                    )

        return self._build_load_result(db, staff_id, staff_name, chosen)

    def _build_load_result(
        self,
        db: Any,
        staff_id: Any,
        staff_name: str | None,
        match: LoadCandidate,
    ) -> LoadMatchResult:
        """Enrich a chosen load with its jumper roster (the fan-out list)."""
        jumpers: list[LoadJumper] = []
        for idx, j in enumerate(match.load.get("jumpers") or []):
            cust_id = j.get("customer")
            cust = (
                db[CUSTOMERS].find_one({"_id": cust_id}) if cust_id is not None else None
            )
            # The jumper's own role decides their speculative package; every jumper on a
            # tandem load is filmed from the inside by their instructor's handcam. The
            # add-on docs win where they classify, for the same reason as in
            # :meth:`_build_result` — otherwise a roster row can disagree with the job
            # that jumper's own footage creates, which this class promises it never does.
            package, entitlement = package_and_entitlement_for(
                j.get("mediaPackage"), j.get("videoType"), "instructor"
            )
            ref_package, ref_entitlement, _refs = self._media_products_for(db, j)
            if ref_package is not None and ref_entitlement is not None:
                package, entitlement = ref_package, ref_entitlement
            jumpers.append(
                LoadJumper(
                    jumper_index=idx,
                    booking_id=str(j["booking"]) if j.get("booking") is not None else None,
                    customer_id=str(cust_id) if cust_id is not None else None,
                    customer_email=(cust or {}).get("email"),
                    customer_name=_full_name(cust),
                    media_package=j.get("mediaPackage"),
                    video_type=j.get("videoType"),
                    package=package,
                    entitlement=entitlement,
                )
            )
        return LoadMatchResult(
            staff_id=str(staff_id),
            staff_name=staff_name,
            load_id=match.load_id,
            load_number=match.load_number,
            business_day=match.business_day,
            departure_local=(
                match.departure_local.isoformat() if match.departure_local else None
            ),
            jumpers=jumpers,
        )

    def _build_result(
        self, db: Any, staff_id: Any, staff_name: str | None, decision: OwnershipDecision
    ) -> MatchResult:
        """Enrich the chosen candidate with customer email/name + package."""
        match = decision.candidate
        j = match.jumper
        customer_email = customer_name = None
        cust_id = j.get("customer")
        if cust_id is not None:
            cust = db[CUSTOMERS].find_one({"_id": cust_id})
            if cust:
                customer_email = cust.get("email")
                customer_name = _full_name(cust)

        package, entitlement = package_and_entitlement_for(
            j.get("mediaPackage"), j.get("videoType"), match.role
        )
        # The add-on docs the jumper actually holds beat the union above whenever they
        # classify, for two independent reasons:
        #   * a PAIR would read `videoType: 'both'` and open an `ultimum` job — the
        #     MERGED two-camera product, whose clips cannot be half-locked;
        #   * a lone SPEC TWIN is invisible to the union, which sees the same
        #     `mediaType` the paid product carries and reads it as a purchase — handing
        #     the unpaid edit over clean, with no watermark and downloads enabled.
        ref_package, ref_entitlement, media_refs = self._media_products_for(db, j)
        if ref_package is not None and ref_entitlement is not None:
            package, entitlement = ref_package, ref_entitlement
        return MatchResult(
            role=match.role,
            staff_id=str(staff_id),
            staff_name=staff_name,
            load_id=match.load_id,
            load_number=match.load_number,
            jumper_index=match.jumper_index,
            booking_id=str(j["booking"]) if j.get("booking") is not None else None,
            customer_id=str(cust_id) if cust_id is not None else None,
            customer_email=customer_email,
            customer_name=customer_name,
            media_package=j.get("mediaPackage"),
            video_type=j.get("videoType"),
            package=package,
            entitlement=entitlement,
            media_refs=media_refs,
            evidence=decision.evidence,
            evidence_detail=decision.detail,
        )

    @staticmethod
    def _media_products_for(
        db: Any, jumper: dict[str, Any]
    ) -> tuple[str | None, str | None, list[MediaRefSpec]]:
        """Read the jumper's ``mediaAddOnRefs`` and resolve them.

        The one database read behind :func:`resolve_media_products`. Never raises: a
        catalogue that can't be read leaves the caller on the union path, which is the
        pre-existing behaviour — the jumper then opens the wrong *shape* of job, which an
        operator can see, rather than no job at all.

        A jumper with **no** refs (every row manifested before the field shipped) skips
        the lookup entirely, so legacy behaviour is untouched. A jumper with ONE ref does
        pay for the read: it is the only way to see that the add-on is a spec twin, and a
        single indexed ``_id`` lookup is cheap against handing an unpaid edit over clean.
        """
        # Which cameras actually rolled. Deliberately read off the SLOTS, not off the
        # products: an assigned cameraman filmed whether or not anybody bought his angle,
        # and that is precisely the footage the paywall exists to offer.
        filming = [
            role
            for role, field in (
                (CAMERA_ROLE_INSTRUCTOR, "instructor"),
                (CAMERA_ROLE_EXTERNAL, "assignedCameraman"),
            )
            if jumper.get(field) is not None
        ]
        refs = jumper.get("mediaAddOnRefs") or []
        if not refs:
            # Nothing to resolve. A jumper who bought NOTHING is still answered here, from
            # the slots alone: every camera that rolled gets a locked edit, which is the
            # same rule as below and the reason a manually-assigned flyer's footage no
            # longer lands in the handcam's raw folder. A jumper who *did* buy but has no
            # ``mediaAddOnRefs`` is a legacy row: nothing here can improve on the union,
            # and guessing would demote a real ``ultimum`` booking (``videoType: 'both'``,
            # no refs) to two speculative products — so those defer, untouched.
            if (jumper.get("mediaPackage") or "").strip().lower() not in ("", "none"):
                return None, None, []
            return resolve_media_products([], filming_roles=filming)
        try:
            docs = list(
                db[BOOKING_PACKAGES].find(
                    {"_id": {"$in": list(refs)}},
                    {"mediaType": 1, "videoAngle": 1, "scenes": 1,
                     "isTwoCameraVideo": 1, "specOf": 1},
                )
            )
        except Exception as e:  # noqa: BLE001 - fall back to the union path
            logger.warning("could not resolve mediaAddOnRefs %r: %r", refs, e)
            return None, None, []
        if len(docs) != len(refs):
            logger.warning(
                "jumper names %d media add-ons but only %d resolved — falling back to "
                "the jumper's derived union", len(refs), len(docs),
            )
            return None, None, []
        return resolve_media_products(docs, filming_roles=filming)

    def close(self) -> None:
        """Close the Mongo client if one was opened."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
