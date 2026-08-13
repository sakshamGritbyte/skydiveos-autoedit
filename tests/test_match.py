"""Pure-function tests for the footage→jump resolver (:mod:`ingest.match`).

The DB-touching :class:`~ingest.match.FootageMatcher.resolve` is exercised live
against the shared DB by hand; here we lock down the two *pure* decision points —
:func:`package_for` (add-on → package) and :func:`select_match` (which jumper a clip
belongs to, and when to refuse) — because that is where a mistake mis-delivers a
customer's video.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ingest.match import (
    AmbiguousMatch,
    Candidate,
    LoadCandidate,
    NoBookingMatch,
    NoLoadMatch,
    NotSpecFlight,
    package_and_entitlement_for,
    package_for,
    select_load,
    select_match,
)


def _cand(
    *,
    load_id: str = "L1",
    role: str = "external",
    day: str | None = "2026-07-21",
    departure: datetime | None = None,
    idx: int = 0,
) -> Candidate:
    return Candidate(
        load_id=load_id,
        load_number=1,
        departure_local=departure,
        business_day=day,
        jumper_index=idx,
        jumper={"booking": "b", "customer": "c"},
        role=role,
    )


def _load(
    *,
    load_id: str = "L1",
    number: int = 1,
    day: str | None = "2026-07-21",
    departure: datetime | None = None,
    jumpers: list[dict] | None = None,
) -> LoadCandidate:
    return LoadCandidate(
        load_id=load_id,
        load_number=number,
        departure_local=departure,
        business_day=day,
        load={"_id": load_id, "loadNumber": number, "jumpers": jumpers or []},
    )


class TestSelectLoad:
    """The spec-flight load pick: same causal rule as jumpers, but the window is a MUST.

    A spec flyer holds no slot on the manifest, so the capture timestamp is the only
    evidence tying his card to a load. That is why these tests care so much about the
    window: relaxing it is what would turn footage shot on the ground between loads into
    a "load video" offered to five customers who were never on it.
    """

    def test_no_candidates_refuses(self) -> None:
        with pytest.raises(NoLoadMatch):
            select_load([], datetime(2026, 7, 21, 13, 5), captured_day="2026-07-21")

    def test_lone_load_must_still_contain_the_clip(self) -> None:
        """The one behavioural difference from select_match, and the reason it exists.

        A single jumper-keyed candidate is accepted without a window check (the jumper
        predicate is itself evidence). A single *load* is not: a clip captured at 22:00
        does not belong to the day's only 09:00 load just because nothing else is on offer.
        """
        far_off = [_load(departure=datetime(2026, 7, 21, 9, 0))]
        with pytest.raises(NoLoadMatch):
            select_load(far_off, datetime(2026, 7, 21, 22, 0), captured_day="2026-07-21")

        # …and the same lone load DOES match a clip inside its window.
        got = select_load(far_off, datetime(2026, 7, 21, 9, 20), captured_day="2026-07-21")
        assert got.load_id == "L1"

    def test_overlapping_windows_resolve_to_the_latest_departure(self) -> None:
        """WINDOW_POST is 2.5 h, so a 12:05 clip sits in the 10:00, 11:00 and 12:00 windows."""
        loads = [
            _load(load_id="L10", number=1, departure=datetime(2026, 7, 21, 10, 0)),
            _load(load_id="L11", number=2, departure=datetime(2026, 7, 21, 11, 0)),
            _load(load_id="L12", number=3, departure=datetime(2026, 7, 21, 12, 0)),
        ]
        got = select_load(loads, datetime(2026, 7, 21, 12, 5), captured_day="2026-07-21")
        assert got.load_id == "L12"  # footage can't precede its own flight

    def test_clip_before_every_departure_takes_the_earliest(self) -> None:
        loads = [
            _load(load_id="L11", departure=datetime(2026, 7, 21, 11, 0)),
            _load(load_id="L12", departure=datetime(2026, 7, 21, 12, 0)),
        ]
        got = select_load(loads, datetime(2026, 7, 21, 10, 45), captured_day="2026-07-21")
        assert got.load_id == "L11"  # can only be the next flight up

    def test_business_day_narrows_before_the_window(self) -> None:
        loads = [
            _load(load_id="Lyest", day="2026-07-20", departure=datetime(2026, 7, 21, 13, 0)),
            _load(load_id="Ltoday", day="2026-07-21", departure=datetime(2026, 7, 21, 13, 0)),
        ]
        got = select_load(loads, datetime(2026, 7, 21, 13, 5), captured_day="2026-07-21")
        assert got.load_id == "Ltoday"

    def test_two_loads_sharing_a_departure_refuse(self) -> None:
        dep = datetime(2026, 7, 21, 13, 0)
        loads = [_load(load_id="La", departure=dep), _load(load_id="Lb", departure=dep)]
        with pytest.raises(AmbiguousMatch):
            select_load(loads, datetime(2026, 7, 21, 13, 5), captured_day="2026-07-21")

    def test_a_load_with_no_departure_never_matches(self) -> None:
        with pytest.raises(NoLoadMatch):
            select_load(
                [_load(departure=None)],
                datetime(2026, 7, 21, 13, 5),
                captured_day="2026-07-21",
            )


class TestPackageFor:
    @pytest.mark.parametrize(
        "media,video,expected",
        [
            ("video-photos", "both", "ultimum"),
            ("video-photos", "outside", "external"),
            ("video-photos", "inside", "selfie"),
            ("video", "both", "ultimum"),
            ("video", "outside", "video_only"),
            ("video", "inside", "video_only"),
            ("photos", None, "photo_only"),
            ("none", "both", None),
            ("", None, None),
            (None, None, None),
            ("video-photos", None, None),  # video+photos but no side named → flag
            ("video-photos", "", None),
        ],
    )
    def test_mapping(self, media, video, expected):
        assert package_for(media, video) == expected

    def test_case_insensitive(self):
        assert package_for("Video-Photos", "BOTH") == "ultimum"


class TestSelectMatch:
    _day = "2026-07-21"
    _cap = datetime(2026, 7, 21, 13, 15)  # 15 min after a 13:00 departure

    def test_none_raises(self):
        with pytest.raises(NoBookingMatch):
            select_match([], self._cap, captured_day=self._day)

    def test_single_returns_it(self):
        c = _cand()
        assert select_match([c], self._cap, captured_day=self._day) is c

    def test_two_same_day_narrows_by_window(self):
        dep = datetime(2026, 7, 21, 13, 0)
        hit = _cand(load_id="L1", departure=dep, idx=0)
        # a load the same day but hours away from the capture instant
        miss = _cand(load_id="L2", departure=datetime(2026, 7, 21, 8, 0), idx=1)
        got = select_match([hit, miss], self._cap, captured_day=self._day)
        assert got.load_id == "L1"

    def test_two_in_window_refuses(self):
        dep = datetime(2026, 7, 21, 13, 0)
        a = _cand(load_id="L1", departure=dep, idx=0)
        b = _cand(load_id="L2", departure=dep, idx=1)
        with pytest.raises(AmbiguousMatch) as ei:
            select_match([a, b], self._cap, captured_day=self._day)
        assert len(ei.value.candidates) == 2

    def test_different_day_dropped_leaving_one(self):
        # Yesterday's load departs YESTERDAY: its window is a day away from the clip, so
        # the day check drops it. (The previous day is only eligible when its flight
        # window still contains the clip — the just-after-midnight case, covered by
        # ``test_a_load_that_departed_before_midnight_still_owns_a_post_midnight_clip``.)
        today = _cand(load_id="L1", day="2026-07-21", departure=datetime(2026, 7, 21, 13, 0))
        other = _cand(load_id="L2", day="2026-07-20", departure=datetime(2026, 7, 20, 13, 0))
        got = select_match([today, other], self._cap, captured_day=self._day)
        assert got.load_id == "L1"

    def test_capture_outside_window_refuses(self):
        # two same-day loads, neither within the flight window of the capture instant
        far = datetime(2026, 7, 21, 3, 0)
        a = _cand(load_id="L1", departure=far, idx=0)
        b = _cand(load_id="L2", departure=far, idx=1)
        with pytest.raises(AmbiguousMatch):
            select_match([a, b], self._cap, captured_day=self._day)

    def test_busy_day_each_jump_matches_its_own_load(self):
        """A staff member flying 5 loads a day: every clip must find ITS jump.

        ``WINDOW_POST`` (2.5 h) is far wider than the gap between loads, so a clip is
        inside several loads' windows at once. Resolving that by departure order is
        what makes the unattended BLE flow usable — refusing would automate only the
        first jump of the day and strand the other four.
        """
        cands = [
            _cand(load_id=f"L{i}", departure=datetime(2026, 7, 21, hour, 0), idx=i)
            for i, hour in enumerate((10, 11, 12, 13, 14))
        ]
        for i, hour in enumerate((10, 11, 12, 13, 14)):
            captured = datetime(2026, 7, 21, hour, 5)  # 5 min after that load left
            got = select_match(cands, captured, captured_day=self._day)
            assert got.load_id == f"L{i}", f"clip at {hour}:05 matched {got.load_id}"

    def test_clip_belongs_to_the_flight_that_already_departed(self):
        """Never attribute footage to a load that had not taken off yet."""
        earlier = _cand(load_id="L1", departure=datetime(2026, 7, 21, 12, 0), idx=0)
        later = _cand(load_id="L2", departure=datetime(2026, 7, 21, 13, 0), idx=1)
        # 12:20 — after L1 left, still inside L2's pre-window. It is L1's footage.
        got = select_match([earlier, later], datetime(2026, 7, 21, 12, 20),
                           captured_day=self._day)
        assert got.load_id == "L1"

    def test_same_departure_still_refuses(self):
        """Two jumpers on the SAME flight is real ambiguity — never guess."""
        dep = datetime(2026, 7, 21, 13, 0)
        a = _cand(load_id="L1", departure=dep, idx=0)
        b = _cand(load_id="L2", departure=dep, idx=1)
        other = _cand(load_id="L3", departure=datetime(2026, 7, 21, 11, 0), idx=2)
        with pytest.raises(AmbiguousMatch) as ei:
            select_match([a, b, other], self._cap, captured_day=self._day)
        assert len(ei.value.candidates) == 2  # only the tied pair, not the 11:00 load

    def test_window_boundaries(self):
        from ingest.match import WINDOW_POST, WINDOW_PRE, _in_window

        dep = datetime(2026, 7, 21, 13, 0)
        assert _in_window(dep + WINDOW_POST, dep)
        assert _in_window(dep - WINDOW_PRE, dep)
        assert not _in_window(dep + WINDOW_POST + timedelta(seconds=1), dep)
        assert not _in_window(dep, None)


class TestStaffLookupBySerialSuffix:
    """A camera id is the TRAILING serial digits; staffs.goproSerial holds the full one.

    A GoPro named ``GoPro 4313`` scans as ``4313``, but the staff record carries
    ``C3504224544313``. Exact-match-only silently owned no camera at a real dropzone.
    """

    class _FakeStaffs:
        def __init__(self, docs): self._docs = docs

        def find_one(self, q):
            return next((d for d in self._docs if d.get("goproSerial") == q["goproSerial"]), None)

        def find(self, q):
            import re as _re
            pat = _re.compile(q["goproSerial"]["$regex"], _re.IGNORECASE)
            return [d for d in self._docs if pat.search(str(d.get("goproSerial") or ""))]

    def _db(self, *serials):
        docs = [{"_id": i, "goproSerial": s} for i, s in enumerate(serials)]
        return {"staffs": self._FakeStaffs(docs)}

    def test_exact_serial_still_wins(self):
        from ingest.match import FootageMatcher

        db = self._db("C3504224544313")
        got = FootageMatcher._staff_for_camera(db, "C3504224544313")
        assert got["goproSerial"] == "C3504224544313"

    def test_ble_short_id_matches_full_serial(self):
        from ingest.match import FootageMatcher

        db = self._db("C3504224544313")
        got = FootageMatcher._staff_for_camera(db, "4313")
        assert got["goproSerial"] == "C3504224544313"

    def test_unknown_camera_raises(self):
        from ingest.match import FootageMatcher, UnknownCamera

        with pytest.raises(UnknownCamera):
            FootageMatcher._staff_for_camera(self._db("C3504224544313"), "9999")

    def test_suffix_shared_by_two_staff_refuses(self):
        from ingest.match import AmbiguousMatch, FootageMatcher

        db = self._db("C3504224544313", "C9999999994313")
        with pytest.raises(AmbiguousMatch, match="refusing to guess the owner"):
            FootageMatcher._staff_for_camera(db, "4313")

    def test_suffix_only_never_matches_a_prefix(self):
        """``4313`` must not match ``43134444`` — it is a suffix, anchored."""
        from ingest.match import FootageMatcher, UnknownCamera

        with pytest.raises(UnknownCamera):
            FootageMatcher._staff_for_camera(self._db("C350431344449"), "4313")


# --------------------------------------------------------------------------- #
# package_and_entitlement_for — the Path A / Path B fork
# --------------------------------------------------------------------------- #


class TestPackageAndEntitlement:
    """A purchase is Path A; no purchase is now Path B ("we filmed it anyway")."""

    @pytest.mark.parametrize(
        ("media", "video", "role", "expected"),
        [
            # Path A: anything actually bought keeps its mapped package, unlocked.
            ("video", "inside", "instructor", ("video_only", "edited_download")),
            ("video-photos", "inside", "instructor", ("selfie", "edited_download")),
            ("video-photos", "outside", "external", ("external", "edited_download")),
            ("video-photos", "both", "instructor", ("ultimum", "edited_download")),
            ("photos", None, "instructor", ("photo_only", "edited_download")),
            # Path B: nothing bought → the role's default package, preview-only.
            (None, None, "instructor", ("selfie", "preview_only")),
            ("", None, "instructor", ("selfie", "preview_only")),
            ("none", None, "instructor", ("selfie", "preview_only")),
            ("none", None, "external", ("external", "preview_only")),
            ("None", "inside", "external", ("external", "preview_only")),
        ],
    )
    def test_mapping(
        self, media: str | None, video: str | None, role: str, expected: tuple[str | None, str]
    ) -> None:
        assert package_and_entitlement_for(media, video, role) == expected

    def test_unmappable_purchase_still_flags_rather_than_previewing(self) -> None:
        """Video+photos with no camera side named is a data problem, not a Path-B job.

        Silently downgrading it to a watermarked preview would quietly under-deliver
        media the customer PAID for, so it stays ``None`` for the caller to flag.
        """
        assert package_and_entitlement_for("video-photos", None, "instructor") == (
            None,
            "edited_download",
        )

    def test_unknown_role_falls_back_to_selfie(self) -> None:
        assert package_and_entitlement_for("none", None, "wingsuit") == (
            "selfie",
            "preview_only",
        )

    def test_package_for_contract_is_unchanged(self) -> None:
        """The older helper still means "unmappable/unbought" — callers rely on it."""
        assert package_for("none", None) is None
        assert package_for("video-photos", "inside") == "selfie"

    def test_values_match_the_api_enums(self) -> None:
        """The strings must equal api.jobs' enum values (this module can't import api)."""
        from api.jobs import Entitlement, Package

        pkg, ent = package_and_entitlement_for("none", None, "external")
        assert Package(pkg) is Package.external
        assert Entitlement(ent) is Entitlement.preview_only


# --------------------------------------------------------------------------- #
# resolve_for_staff — the QR session flow's entry point
# --------------------------------------------------------------------------- #


class TestResolveForStaff:
    """The QR supplies WHO (a staffs._id string); the loads still decide WHICH jump.

    Load-bearing detail: a QR payload is a *string* while the loads store raw
    ObjectIds, so ``_staff_id_variants`` must bridge the two or the QR flow
    matches nothing against a real DB.
    """

    class _Coll:
        def __init__(self, docs):
            self._docs = docs

        def find(self, _q):
            # The Mongo query is only a prefilter; the code re-checks per jumper.
            return list(self._docs)

        def find_one(self, q):
            wanted = q.get("_id")
            ids = wanted.get("$in") if isinstance(wanted, dict) else [wanted]
            return next((d for d in self._docs if d.get("_id") in ids), None)

    def _matcher(self, staff_oid, *, departures=(datetime(2026, 7, 21, 13, 0),)):
        from ingest.match import FootageMatcher

        loads = [
            {
                "_id": f"L{i}",
                "status": "planned",
                "loadNumber": i + 1,
                "businessDate": datetime(2026, 7, 21),
                "departureTime": dep,
                "jumpers": [
                    {
                        "instructor": staff_oid,
                        "customer": "c1",
                        "booking": "b1",
                        "mediaPackage": "video",
                        "videoType": "inside",
                    }
                ],
            }
            for i, dep in enumerate(departures)
        ]
        matcher = FootageMatcher("mongodb://unused-in-test")
        matcher._db = {
            "staffs": self._Coll([{"_id": staff_oid, "firstName": "Marc", "lastName": "T"}]),
            "loads": self._Coll(loads),
            "customers": self._Coll([{"_id": "c1", "email": "c@x.test", "firstName": "Cus"}]),
        }
        return matcher

    def test_staff_id_variants_bridges_string_and_objectid(self) -> None:
        from bson import ObjectId

        from ingest.match import _staff_id_variants

        hex_id = "665f1c0a2ab79c0012345678"
        variants = _staff_id_variants(hex_id)
        assert variants[0] == hex_id and ObjectId(hex_id) in variants
        # Anything else passes through untouched.
        assert _staff_id_variants("not-hex") == ["not-hex"]
        oid = ObjectId(hex_id)
        assert _staff_id_variants(oid) == [oid]

    def test_resolve_for_staff_matches_objectid_loads_from_string_id(self) -> None:
        """A QR's string staff id finds loads keyed by the raw ObjectId."""
        from bson import ObjectId

        hex_id = "665f1c0a2ab79c0012345678"
        matcher = self._matcher(ObjectId(hex_id))
        r = matcher.resolve_for_staff(hex_id, "2026-07-21T13:05:00")
        assert r.role == "instructor"
        assert r.staff_id == hex_id
        assert r.staff_name == "Marc T"  # looked up when not supplied
        assert r.customer_email == "c@x.test"
        assert (r.package, r.entitlement) == ("video_only", "edited_download")

    def test_resolve_delegates_to_resolve_for_staff(self, monkeypatch) -> None:
        """The serial path is now a thin shim over the staff path — one code path."""
        matcher = self._matcher("s1")
        matcher._db["staffs"] = self._SerialStaffs()
        seen: dict[str, object] = {}

        def _spy(staff_id, captured_at, *, staff_name=None, clip_ref=None):
            seen.update(staff_id=staff_id, staff_name=staff_name)
            return "sentinel"

        monkeypatch.setattr(matcher, "resolve_for_staff", _spy)
        assert matcher.resolve("4313", "2026-07-21T13:05:00") == "sentinel"
        assert seen == {"staff_id": "s1", "staff_name": "Greg B"}

    class _SerialStaffs:
        def find_one(self, q):
            if q.get("goproSerial") == "4313":
                return {"_id": "s1", "goproSerial": "4313", "firstName": "Greg", "lastName": "B"}
            return None

        def find(self, _q):
            return []

    def test_resolve_for_staff_refuses_ambiguity_like_resolve(self) -> None:
        """Two jumpers at the same departure instant → refuse, exactly as the serial path."""
        dep = datetime(2026, 7, 21, 13, 0)
        matcher = self._matcher("s1", departures=(dep, dep))
        with pytest.raises(AmbiguousMatch):
            matcher.resolve_for_staff("s1", "2026-07-21T13:05:00")

    def test_resolve_for_staff_requires_db(self, monkeypatch) -> None:
        from ingest.match import FootageMatcher, RegistryUnavailable

        # mongo_url=None defers to $MONGO_URL — clear it so the matcher is disabled.
        monkeypatch.delenv("MONGO_URL", raising=False)
        with pytest.raises(RegistryUnavailable):
            FootageMatcher(None).resolve_for_staff("s1", "2026-07-21T13:05:00")

    # -- the spec-flight (load master) path -------------------------------------- #

    def _spec_matcher(self, *, flyer="marc", jumpers=None):
        """A matcher whose one load carries ``jumpers`` and (by default) NOT the flyer."""
        from ingest.match import FootageMatcher

        load = {
            "_id": "L17",
            "status": "planned",
            "loadNumber": 17,
            "businessDate": datetime(2026, 7, 21),
            "departureTime": datetime(2026, 7, 21, 13, 0),
            "jumpers": jumpers
            if jumpers is not None
            else [
                # Two media buyers and two who bought nothing — the fan-out's two tiers.
                {"instructor": "i1", "customer": "c1", "booking": "b1",
                 "mediaPackage": "video", "videoType": "inside"},
                {"instructor": "i1", "customer": "c2", "booking": "b2",
                 "mediaPackage": "none"},
                {"instructor": "i2", "customer": "c3", "booking": "b3",
                 "mediaPackage": None},
                {"instructor": "i2", "customer": "c4", "booking": "b4",
                 "mediaPackage": "photos"},
            ],
        }
        # clock_tz pinned to UTC so the naive capture strings below are taken at face
        # value: the load path insists the clip fall inside a flight window, so a dev
        # box's real $CAMERA_CLOCK_TZ would silently shift these clips out of it.
        matcher = FootageMatcher("mongodb://unused-in-test", clock_tz="UTC")
        matcher._db = {
            "staffs": self._Coll([{"_id": flyer, "firstName": "Marc", "lastName": "T"}]),
            "loads": self._Coll([load]),
            "customers": self._Coll([
                {"_id": "c1", "email": "one@x.test", "firstName": "Daniel"},
                {"_id": "c2", "email": "two@x.test", "firstName": "Priya"},
                {"_id": "c3", "email": "three@x.test", "firstName": "Kevin"},
                {"_id": "c4", "email": "four@x.test", "firstName": "Aisha"},
            ]),
        }
        return matcher

    def test_resolve_load_returns_the_whole_manifest_roster(self) -> None:
        """A spec flight resolves to the LOAD, with every jumper's tier already decided."""
        r = self._spec_matcher().resolve_load_for_staff("marc", "2026-07-21T13:05:00")

        assert (r.load_id, r.load_number, r.label) == ("L17", 17, "Load 17")
        assert r.staff_name == "Marc T"
        assert [j.customer_name for j in r.jumpers] == [
            "Daniel", "Priya", "Kevin", "Aisha",
        ]
        # bought_media is the fan-out's fork: a tile for buyers, a child gallery for the
        # rest. "photos" counts as a purchase; "none"/None/"" do not.
        assert [j.bought_media for j in r.jumpers] == [True, False, False, True]
        assert [j.jumper_index for j in r.jumpers] == [0, 1, 2, 3]
        assert r.jumpers[1].customer_email == "two@x.test"

    def test_resolve_load_refuses_when_the_flyer_has_an_assigned_customer(self) -> None:
        """v1 is spec flights only: an assigned flyer's footage is his customer's product.

        Building a load master from it would need the customer's personal scenes excluded
        from four strangers' videos, which scene labels cannot guarantee (the
        clip-position heuristic in AUDIT_SCENE_LABELS.md). So refuse rather than promise it.
        """
        matcher = self._spec_matcher(
            jumpers=[
                {"instructor": "i1", "assignedCameraman": "marc", "customer": "c1",
                 "booking": "b1", "mediaPackage": "video-photos", "videoType": "both"},
            ]
        )
        with pytest.raises(NotSpecFlight, match="assignedCameraman"):
            matcher.resolve_load_for_staff("marc", "2026-07-21T13:05:00")

    def test_resolve_load_refuses_footage_outside_every_window(self) -> None:
        """Ground footage shot between loads must resolve to nothing, not the nearest load."""
        with pytest.raises(NoLoadMatch):
            self._spec_matcher().resolve_load_for_staff("marc", "2026-07-21T22:00:00")

    def test_resolve_load_skips_cancelled_loads(self) -> None:
        matcher = self._spec_matcher()
        matcher._db["loads"]._docs[0]["status"] = "cancelled"
        with pytest.raises(NoLoadMatch):
            matcher.resolve_load_for_staff("marc", "2026-07-21T13:05:00")

    def test_resolve_load_serial_path_delegates(self, monkeypatch) -> None:
        """``resolve_load`` is the serial shim over ``resolve_load_for_staff``."""
        matcher = self._spec_matcher()
        matcher._db["staffs"] = self._SerialStaffs()
        seen: dict[str, object] = {}

        def _spy(staff_id, captured_at, *, staff_name=None, clip_ref=None):
            seen.update(staff_id=staff_id, staff_name=staff_name)
            return "sentinel"

        monkeypatch.setattr(matcher, "resolve_load_for_staff", _spy)
        assert matcher.resolve_load("4313", "2026-07-21T13:05:00") == "sentinel"
        assert seen == {"staff_id": "s1", "staff_name": "Greg B"}

    def test_resolve_load_requires_db(self, monkeypatch) -> None:
        from ingest.match import FootageMatcher, RegistryUnavailable

        monkeypatch.delenv("MONGO_URL", raising=False)
        with pytest.raises(RegistryUnavailable):
            FootageMatcher(None).resolve_load_for_staff("s1", "2026-07-21T13:05:00")

