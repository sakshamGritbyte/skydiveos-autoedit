"""MongoDB-backed registry of paired GoPro cameras — the auto-discovery allow-list.

Auto-discovery needs to know *which* cameras are ours: a BLE scan surfaces every
GoPro in range, but we only ever pull the ones we have paired with. This module is
that allow-list — a small ``cameras`` collection in MongoDB, written when
``python -m ingest.pull --pair`` succeeds and read on every scan tick by
:class:`~ingest.discovery.CameraDiscoveryService`.

It is deliberately the *only* part of the system that uses a database: jobs and
media stay file-based (see :mod:`api.jobs` — "a file, not a DB, keeps a job
self-contained"). MongoDB is reached via ``MONGO_URL`` (``mongodb+srv://...``);
``pymongo`` is imported lazily so importing this module — and therefore the whole
``ingest`` package — never requires the driver until a registry method is actually
called against a configured URL.

With no ``MONGO_URL`` configured the registry is **disabled**: every read degrades
to an empty result and a write is a logged no-op, so the rest of the pipeline runs
exactly as before when discovery is off (it never raises just because Mongo is
absent).

The document shape (one per camera)::

    {camera_id: "1234", name: "Tandem cam A", paired_at: 1.7e9, active: true}

``active`` is the discovery filter: ``DELETE /cameras/{id}`` flips it to ``false``
(a soft delete) so a camera stops being auto-pulled without losing its pairing
history; re-pairing flips it back to ``true``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Default Mongo database holding the camera registry (override with ``MONGO_DB``).
DEFAULT_DB = "skydiveos"
#: Collection name within the database.
COLLECTION = "cameras"


class CameraRecord(BaseModel):
    """One paired camera in the registry (the public, DB-agnostic shape)."""

    camera_id: str
    name: str | None = None
    paired_at: float
    active: bool = True
    #: Instructor (SkydiveOS account) this camera belongs to. Jobs auto-pulled from
    #: it are stamped with this id, so the footage lands in that instructor's account.
    instructor_id: str | None = None
    #: Which camera this is in a two-camera (Ultimate) jump: ``"instructor"`` (selfie
    #: cam) or ``"external"`` (cameraman). ``None`` for a single-camera setup. Sent to
    #: SkydiveOS as ``camera_role`` so both angles land under the right ``raw/<role>/``.
    role: str | None = None


class CameraRegistry:
    """File-free, Mongo-backed CRUD for the paired-camera allow-list.

    Connection is lazy: the first method that needs the database opens the client
    and ensures a unique index on ``camera_id``. When ``mongo_url`` is ``None`` (env
    ``MONGO_URL`` unset) the registry is :attr:`enabled` ``False`` and every method
    degrades safely — reads return empty, writes log and no-op — so discovery being
    misconfigured never takes the API or the pipeline down.
    """

    def __init__(
        self,
        mongo_url: str | None = None,
        *,
        db_name: str = DEFAULT_DB,
        clock: Callable[[], float] = time.time,
    ) -> None:
        #: ``None`` (no explicit url and no ``MONGO_URL``) disables the registry.
        self._mongo_url = (
            mongo_url if mongo_url is not None else (os.environ.get("MONGO_URL") or None)
        )
        self._db_name = db_name
        self._clock = clock
        self._client: Any | None = None
        self._coll: Any | None = None

    @property
    def enabled(self) -> bool:
        """True when a Mongo URL is configured (writes persist, reads hit the DB)."""
        return self._mongo_url is not None

    def _collection(self) -> Any:
        """Lazily connect, ensure the unique ``camera_id`` index, return the collection."""
        if self._coll is None:
            try:
                from pymongo import MongoClient
            except ImportError as e:  # pragma: no cover - exercised only without the driver
                raise RuntimeError(
                    "pymongo is required for the camera registry; install it with "
                    "'uv pip install \"pymongo[srv]\"' (the [srv] extra is needed for "
                    "mongodb+srv:// URLs)."
                ) from e
            self._client = MongoClient(self._mongo_url)
            self._coll = self._client[self._db_name][COLLECTION]
            # Idempotent: one camera == one document, so upserts are unambiguous.
            self._coll.create_index("camera_id", unique=True)
        return self._coll

    def upsert_paired(
        self,
        camera_id: str,
        name: str | None = None,
        instructor_id: str | None = None,
        role: str | None = None,
    ) -> CameraRecord:
        """Record (or refresh) a successful pairing; marks the camera active.

        Called from the ``--pair`` flow. Refreshes ``paired_at`` and re-activates the
        camera; only overwrites ``name``/``instructor_id``/``role`` when one is supplied
        (so a re-pair without them keeps the existing values). When the registry is
        disabled this logs a warning and returns the would-be record without persisting
        (so ``--pair`` still succeeds offline).
        """
        now = self._clock()
        if not self.enabled:
            logger.warning(
                "camera registry disabled (MONGO_URL unset); not recording pairing of %s",
                camera_id,
            )
            return CameraRecord(
                camera_id=camera_id, name=name, paired_at=now, active=True,
                instructor_id=instructor_id, role=role,
            )

        set_fields: dict[str, Any] = {"camera_id": camera_id, "paired_at": now, "active": True}
        if name is not None:
            set_fields["name"] = name
        if instructor_id is not None:
            set_fields["instructor_id"] = instructor_id
        if role is not None:
            set_fields["role"] = role
        coll = self._collection()
        coll.update_one({"camera_id": camera_id}, {"$set": set_fields}, upsert=True)
        return self._record(coll.find_one({"camera_id": camera_id}) or set_fields)

    def _record(self, doc: dict[str, Any]) -> CameraRecord:
        """Build a :class:`CameraRecord` from a raw Mongo document."""
        return CameraRecord(
            camera_id=str(doc["camera_id"]),
            name=doc.get("name"),
            paired_at=float(doc.get("paired_at", 0.0)),
            active=bool(doc.get("active", True)),
            instructor_id=doc.get("instructor_id"),
            role=doc.get("role"),
        )

    def get(self, camera_id: str) -> CameraRecord | None:
        """The registry entry for one camera, or ``None`` if unknown/disabled."""
        if not self.enabled:
            return None
        doc = self._collection().find_one({"camera_id": camera_id})
        return self._record(doc) if doc else None

    def instructor_for(self, camera_id: str) -> str | None:
        """The instructor that owns ``camera_id`` (``None`` if unknown/unassigned)."""
        record = self.get(camera_id)
        return record.instructor_id if record else None

    def role_for(self, camera_id: str) -> str | None:
        """The two-camera role of ``camera_id`` (``instructor``/``external``/``None``)."""
        record = self.get(camera_id)
        return record.role if record else None

    def assign_instructor(
        self, camera_id: str, instructor_id: str | None, role: str | None = None
    ) -> bool:
        """Set a camera's owning instructor (and optionally its role); create if unknown.

        Registration + assignment in one step: an unknown serial is auto-created
        (active, ``paired_at`` = now) with the given instructor, so an admin can
        register a camera straight from the UI without a separate ``--pair``. An
        existing camera just has its ``instructor_id`` (and ``role`` when supplied)
        updated (other fields kept). Returns ``True`` when persisted, ``False`` only
        when the registry is disabled (``MONGO_URL`` unset).
        """
        if not self.enabled:
            return False
        set_fields: dict[str, Any] = {"instructor_id": instructor_id}
        if role is not None:
            set_fields["role"] = role
        self._collection().update_one(
            {"camera_id": camera_id},
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "camera_id": camera_id,
                    "paired_at": self._clock(),
                    "active": True,
                },
            },
            upsert=True,
        )
        return True

    def list_cameras(
        self, *, active_only: bool = False, instructor_id: str | None = None
    ) -> list[CameraRecord]:
        """Registered cameras (newest pairing first).

        ``active_only`` drops deactivated cameras; ``instructor_id`` (when given)
        restricts to one instructor's cameras (the per-instructor view; ``None`` is the
        admin view of all).
        """
        if not self.enabled:
            return []
        query: dict[str, Any] = {}
        if active_only:
            query["active"] = True
        if instructor_id is not None:
            query["instructor_id"] = instructor_id
        cursor = self._collection().find(query).sort("paired_at", -1)
        return [self._record(d) for d in cursor]

    def known_active_ids(self) -> set[str]:
        """The set of camera ids discovery is allowed to auto-pull (active only)."""
        if not self.enabled:
            return set()
        cursor = self._collection().find({"active": True}, {"camera_id": 1})
        return {str(d["camera_id"]) for d in cursor}

    def deactivate(self, camera_id: str) -> bool:
        """Soft-delete: stop auto-pulling ``camera_id`` (keeps its pairing record).

        Returns ``True`` if a matching camera was found, ``False`` otherwise.
        """
        if not self.enabled:
            return False
        result = self._collection().update_one(
            {"camera_id": camera_id}, {"$set": {"active": False}}
        )
        return result.matched_count > 0

    def close(self) -> None:
        """Close the Mongo client if one was opened (safe to call when never used)."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._coll = None
