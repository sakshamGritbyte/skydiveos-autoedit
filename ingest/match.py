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

Design mirrors :mod:`ingest.registry`: lazy ``pymongo`` import, ``MONGO_URL`` /
``MONGO_DB`` from the environment, disabled (every lookup raises
:class:`RegistryUnavailable`) when no URL is configured. The *decision* logic
(:func:`select_match`, :func:`package_for`) is **pure** — plain dicts in, result out,
no I/O — so it unit-tests without a database, exactly like :mod:`edl.validate`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Default database + collections (override the DB with ``MONGO_DB``).
DEFAULT_DB = "skydiveos"
STAFFS = "staffs"
LOADS = "loads"
CUSTOMERS = "customers"

#: Flight-window tolerance around a load's ``departureTime`` that a clip's capture
#: instant may fall in and still belong to that load. Footage is captured *after*
#: departure (plane climbs, then the jump), so the window is asymmetric: a little
#: before (early departure / clock skew) and generously after (climb + freefall +
#: 3–5 min canopy ride + landing + buffer).
WINDOW_PRE = timedelta(minutes=30)
WINDOW_POST = timedelta(minutes=150)

#: Load statuses a jump can be matched against. A ``planned`` load may still be
#: matched (footage can land before manifest closes it); a cancelled one never.
_MATCHABLE_STATUSES = {"planned", "closed", "landed", "completed"}


class FootageMatchError(Exception):
    """Base for a match that could not be made safely."""


class RegistryUnavailable(FootageMatchError):
    """No ``MONGO_URL`` configured, so the shared DB can't be read."""


class UnknownCamera(FootageMatchError):
    """No staff member owns the clip's camera serial (``staffs.goproSerial``)."""


class NoBookingMatch(FootageMatchError):
    """The camera's owner isn't in any matchable load-jumper for that capture time."""


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
    #: ``ultimum``) or ``None`` when the booking buys no media / can't be mapped.
    package: str | None = None


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


def _in_window(captured_local: datetime, departure_local: datetime | None) -> bool:
    """Whether a capture instant falls in a load's flight window (both DZ-local naive)."""
    if departure_local is None:
        return False
    return (departure_local - WINDOW_PRE) <= captured_local <= (departure_local + WINDOW_POST)


def select_match(
    candidates: list[Candidate],
    captured_local: datetime,
    *,
    captured_day: str,
) -> Candidate:
    """Pick the single (load, jumper, role) a clip belongs to, or raise.

    Pure decision logic (no I/O), so it unit-tests without a database:

    * 0 candidates → :class:`NoBookingMatch`.
    * 1 candidate  → it (day already constrained the query).
    * >1 → narrow to the same DZ-local business day, then to the flight window
      around each load's ``departureTime``.
    * Still >1 → the windows OVERLAP, because ``WINDOW_POST`` (2.5 h) is far wider
      than the gap between loads: a clip shot at 12:05 sits inside the windows of the
      12:00, 11:00 *and* 10:00 loads at once. That is the normal case for a staff
      member flying 4–5 loads a day, so refusing here would automate only their first
      jump. Resolve it causally instead of guessing: footage cannot belong to a flight
      that had not departed yet, so the clip belongs to the LATEST departure at or
      before it. Only a tie *within one departure instant* (two jumpers on the same
      load, or two loads sharing a departure time) is real ambiguity —
      :class:`AmbiguousMatch` is still raised there rather than picking one.
    """
    if not candidates:
        raise NoBookingMatch("no matchable load-jumper for this camera + capture time")
    if len(candidates) == 1:
        return candidates[0]

    same_day = [c for c in candidates if c.business_day == captured_day]
    pool = same_day or candidates
    if len(pool) == 1:
        return pool[0]

    in_window = [c for c in pool if _in_window(captured_local, c.departure_local)]
    if len(in_window) == 1:
        return in_window[0]
    if not in_window:
        raise AmbiguousMatch(
            f"{len(pool)} jumpers match this camera, none within a flight window; "
            "refusing to guess",
            pool,
        )

    # Already departed at capture time (allowing WINDOW_PRE for an early takeoff or a
    # little clock skew); prefer those, newest first. If the clip predates every
    # departure, fall back to the earliest — it can only belong to the next flight up.
    # (_in_window already excluded a None departure, so every one here is set.)
    departures = [c.departure_local for c in in_window if c.departure_local is not None]
    departed = [d for d in departures if d - WINDOW_PRE <= captured_local]
    chosen_departure = max(departed) if departed else min(departures)
    on_that_flight = [c for c in in_window if c.departure_local == chosen_departure]
    if len(on_that_flight) == 1:
        return on_that_flight[0]

    raise AmbiguousMatch(
        f"{len(on_that_flight)} jumpers share the same departure "
        f"({chosen_departure:%Y-%m-%d %H:%M}); refusing to guess",
        on_that_flight,
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
        self._db_name = db_name or os.environ.get("MONGO_DB") or DEFAULT_DB
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

    def resolve(self, gopro_serial: str, captured_at: datetime | str) -> MatchResult:
        """Resolve one clip to its jump. Raises a :class:`FootageMatchError` subclass.

        See the module docstring for the match key. On success the returned
        :class:`MatchResult` carries the per-jump ``role`` (the authority over the
        registry's static hint), the customer's email/name, and the mapped package.
        """
        if not self.enabled:
            raise RegistryUnavailable("MONGO_URL unset; cannot read the shared DB")

        db = self._database()
        staff = self._staff_for_camera(db, gopro_serial)
        staff_id = staff["_id"]
        staff_name = " ".join(
            p for p in (staff.get("firstName"), staff.get("lastName")) if p
        ) or None

        captured_local = self._to_local(captured_at)
        captured_day = captured_local.date().isoformat()

        # Every load where this staff is a jumper's instructor or cameraman.
        cursor = db[LOADS].find(
            {
                "$or": [
                    {"jumpers.instructor": staff_id},
                    {"jumpers.assignedCameraman": staff_id},
                ]
            }
        )
        candidates: list[Candidate] = []
        for load in cursor:
            if str(load.get("status", "")).lower() not in _MATCHABLE_STATUSES:
                continue
            biz = self._naive_local(load.get("businessDate"))
            dep = self._naive_local(load.get("departureTime"))
            for idx, jumper in enumerate(load.get("jumpers", [])):
                if jumper.get("instructor") == staff_id:
                    role = "instructor"
                elif jumper.get("assignedCameraman") == staff_id:
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
                    )
                )

        match = select_match(candidates, captured_local, captured_day=captured_day)
        return self._build_result(db, staff_id, staff_name, match)

    def _build_result(
        self, db: Any, staff_id: Any, staff_name: str | None, match: Candidate
    ) -> MatchResult:
        """Enrich the chosen candidate with customer email/name + package."""
        j = match.jumper
        customer_email = customer_name = None
        cust_id = j.get("customer")
        if cust_id is not None:
            cust = db[CUSTOMERS].find_one({"_id": cust_id})
            if cust:
                customer_email = cust.get("email")
                customer_name = " ".join(
                    p for p in (cust.get("firstName"), cust.get("lastName")) if p
                ) or None

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
            package=package_for(j.get("mediaPackage"), j.get("videoType")),
        )

    def close(self) -> None:
        """Close the Mongo client if one was opened."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
