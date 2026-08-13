#!/usr/bin/env python3
"""Local stand-in for the SkydiveOS raw-upload consumer: notify → match → job.

The production contract (SKYDIVEOS_INTEGRATION.md) is that discovery uploads each
pulled clip to S3 and POSTs ``{s3_key, camera_id, captured_at?, staff_id?, ...}``
to ``{SKYDIVEOS_API_BASE}/api/media/raw-upload`` — and *SkydiveOS* then matches the
footage to a booking and creates the auto-edit job. Until the SkydiveOS backend
implements that (see the contract doc), this bridge closes the loop locally so the
fully hands-off flow works end to end on one machine:

    insert SD card → discovery pulls + decodes QR + uploads to S3 → THIS BRIDGE
    → FootageMatcher (staff_id from the QR, else camera serial) → POST /jobs
    → attach footage → auto-edit renders → AUTO_DELIVER emails the customer.

It is also the executable reference for the SkydiveOS implementation: same match
rules (``ingest.match``), same refuse-and-flag behavior, same job-creation calls.

Behaviour:

* **Dedupe** — a notification for an s3_key already handled is acknowledged and
  ignored (discovery retries on our non-2xx, so failures raise and retry instead).
* **Debounce** — clips are grouped per matched jump ``(load_id, jumper_index)``;
  the job is created once no new clip has arrived for that jump for
  ``--debounce`` seconds, so a session's clips land in ONE job. The default is
  deliberately long (15 min): the gap between two notifications is really the
  *S3 upload time of the next clip*, and at 20 s a card pulled over a dropzone
  uplink split one jump into four jobs — four renders, four "your video is
  ready" emails to the same customer (observed 2026-08-06). Waiting is cheap;
  a partial edit delivered to a customer is not.

  Waiting 15 min per test cycle on a laptop *is* expensive, though, so there is a
  separate, deliberately conspicuous dev shortcut: ``--dev-debounce <s>`` or
  ``BRIDGE_DEV_DEBOUNCE_SECONDS=<s>``. It is off by default, may only *shorten*
  the window, and logs a warning banner naming the incident above for as long as
  it is active. Never set it on a dropzone.
* **Spec-flight load master** — a camera flyer sent up on an open seat with no assigned
  customer holds no slot on the manifest, so the jumper-keyed match cannot resolve his
  card. Rather than flag it, the bridge then asks whether a load's flight window contains
  the clips with him absent from its jumpers; if so his card becomes ONE ``load_master``
  job (no customer, no email) that the pipeline fans out to everybody on that load. A
  flyer who *did* have an assigned customer is refused (``NotSpecFlight``) on this path —
  that footage is their product. See ``CLAUDE.md``.
* **Refuse-and-flag** — an unmatchable clip (no load fits, ambiguous, unmappable
  purchase) is logged + recorded and acknowledged with 200: mis-delivery is worse
  than a human follow-up, and a 5xx would make discovery retry forever. A flag is
  *terminal* on purpose — every later notify for that key is a duplicate, so a
  clip is never silently retried into a mis-matched job. Once the underlying data
  is fixed (the load manifested, ``goproSerial`` set, the purchase mapped), clear
  the key with ``python scripts/unflag_bridge_key.py`` and re-notify.

Run::

    python scripts/skydiveos_bridge.py [--port 9000] [--api http://localhost:8000]

and point discovery at it: ``SKYDIVEOS_API_BASE=http://localhost:9000``. It binds
localhost by default; when the notifier is a *different* machine (the dropzone Mac
POSTing to this box) pass ``--host 0.0.0.0``. ``/api/*`` then requires the same
``Authorization: Bearer $AUTO_EDIT_API_KEY`` the auto-edit API does (discovery sends
it automatically) — one accepted notify creates a job and emails a customer, so the
firewall must not be its only gate. Needs ``MONGO_URL`` (the match) and ``S3_BUCKET``
(to fetch the clips back).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("skydiveos_bridge")

STATE_FILENAME = "_bridge_state.json"

#: Append-only record of ownership decisions that were NOT established by the flight
#: window — today only ``out_of_window_same_day``, the temporary compatibility path
#: approved on 2026-08-11. Lives beside the bridge state so counting the population is
#: ``wc -l`` and does not depend on log retention: the whole point of the one-week review
#: is to read the real distribution before the window is enforced strictly.
OWNERSHIP_AUDIT_FILENAME = "_ownership_audit.jsonl"

#: The production settle window. Long on purpose — see the module docstring.
PRODUCTION_DEBOUNCE_S = 900.0

#: Dev-only escape hatch for the settle window (see ``resolve_debounce``).
DEV_DEBOUNCE_ENV = "BRIDGE_DEV_DEBOUNCE_SECONDS"


# -- state file: shared with scripts/unflag_bridge_key.py ------------------- #


def default_state_path(settings: Any | None = None) -> Path:
    """Where the bridge keeps its dedupe/flag record (``jobs/_bridge_state.json``)."""
    if settings is None:
        from api.config import get_settings

        settings = get_settings()
    return Path(settings.jobs_root or "jobs") / STATE_FILENAME


def load_state(path: Path) -> dict[str, Any]:
    """Read the state file, tolerating absent/corrupt content (start empty)."""
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("handled", {})
    state.setdefault("flagged", {})
    # ``"{load_id}:{jumper_index}" -> job_id``: the job a jump ALREADY has. A mixed
    # jumper's two cards are plugged in minutes or hours apart, so the second card
    # arrives long after the first flushed — without this record it would create a
    # second job, and the customer would get two gallery links and two emails.
    state.setdefault("jumps", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write the state file. Raises — callers that must not fail catch it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def clear_flagged(
    state: dict[str, Any], keys: list[str]
) -> tuple[dict[str, str], list[str], list[str]]:
    """Drop ``keys`` from ``state["flagged"]`` so a re-notify is matched afresh.

    Mutates ``state`` and returns ``(cleared {key: reason}, unknown, handled)``.
    Only the flag record is touched: a key in ``handled`` already became a job, so
    clearing it would invite a *second* job for the same footage — those keys are
    refused and reported instead.
    """
    flagged: dict[str, str] = state["flagged"]
    handled: dict[str, Any] = state["handled"]
    cleared: dict[str, str] = {}
    unknown: list[str] = []
    already_handled: list[str] = []
    for key in keys:
        if key in handled:
            already_handled.append(key)
        elif key in flagged:
            cleared[key] = flagged.pop(key)
        else:
            unknown.append(key)
    return cleared, unknown, already_handled


# -- the dev-only settle-window override ------------------------------------ #


def resolve_debounce(
    base_s: float = PRODUCTION_DEBOUNCE_S,
    *,
    flag_s: float | None = None,
    env_s: str | None = None,
) -> float:
    """Apply the dev-only settle-window override to ``base_s``, loudly.

    ``flag_s`` is ``--dev-debounce`` and wins over ``env_s`` (``$BRIDGE_DEV_DEBOUNCE_SECONDS``);
    with neither set the production window is returned untouched. The override may
    only *shorten* the window — a value that is not a positive number below
    ``base_s`` is refused with a warning rather than silently applied, because the
    one thing this knob must never do is quietly become the dropzone's setting.
    """
    candidate: float | None = flag_s
    source = "--dev-debounce"
    if candidate is None and env_s is not None and env_s.strip():
        source = f"${DEV_DEBOUNCE_ENV}"
        try:
            candidate = float(env_s)
        except ValueError:
            logger.warning(
                "ignoring %s=%r: not a number — settle window stays %.0fs",
                DEV_DEBOUNCE_ENV, env_s, base_s,
            )
            return base_s
    if candidate is None:
        return base_s
    if not 0 < candidate < base_s:
        logger.warning(
            "ignoring %s=%s: the dev override may only SHORTEN the settle window "
            "(0 < s < %.0f) — settle window stays %.0fs",
            source, candidate, base_s, base_s,
        )
        return base_s

    bar = "!" * 72
    logger.warning(bar)
    logger.warning(
        "DEV DEBOUNCE ACTIVE: clip settle window is %gs, not the %.0fs default (%s)",
        candidate, base_s, source,
    )
    logger.warning(
        "This is a LOCAL-TESTING shortcut. On a real dropzone uplink the gap between"
    )
    logger.warning(
        "two notifications is the next clip's S3 upload time: at 20s one jump split"
    )
    logger.warning(
        "into four jobs and emailed one customer four times (2026-08-06). Do NOT set"
    )
    logger.warning("this in production.")
    logger.warning(bar)
    return candidate


@dataclass
class PendingJump:
    """Clips accumulating for one matched jump until the debounce expires."""

    match: Any  # ingest.match.MatchResult, or LoadMatchResult when is_load_master
    captured_at: str
    clips: list[dict[str, Any]] = field(default_factory=list)  # raw notifications
    #: ``s3_key -> camera role`` from that clip's OWN match. :attr:`match` holds only the
    #: first clip's, and on a mixed jumper the two cards resolve to the same jumper with
    #: *different* roles — so when both are plugged inside one debounce window they land
    #: in this same pending jump and the role must be remembered per clip, or the
    #: cameraman's footage stages under the instructor's folder and is edited (and
    #: entitled) as the wrong product.
    clip_roles: dict[str, str | None] = field(default_factory=dict)
    timer: asyncio.TimerHandle | None = None
    #: True when ``match`` is a :class:`~ingest.match.LoadMatchResult` — a camera
    #: flyer's card becoming ONE load master for the whole load instead of one
    #: customer's jump.
    is_load_master: bool = False
    #: How the master's load was resolved (``api.jobs.LoadEvidence`` values):
    #: ``flight_window`` for a spec flight (timestamp-only — the fan-out keeps its
    #: freefall guard).
    load_evidence: str = "flight_window"


class Bridge:
    def __init__(self, api: str, *, debounce_s: float = PRODUCTION_DEBOUNCE_S) -> None:
        from api.config import get_settings
        from ingest.match import FootageMatcher

        self.api = api.rstrip("/")
        self.debounce_s = debounce_s
        # Marks every per-clip log line while a shortened window is in force, so the
        # startup banner isn't the only trace once it has scrolled away.
        self.dev_debounce = debounce_s < PRODUCTION_DEBOUNCE_S
        self.settings = get_settings()
        if not self.settings.mongo_url:
            raise SystemExit("error: the bridge needs MONGO_URL set (the shared SkydiveOS DB).")
        if not self.settings.s3_bucket:
            raise SystemExit("error: the bridge needs S3_BUCKET set (to fetch clips back).")
        self.matcher = FootageMatcher(
            self.settings.mongo_url,
            db_name=self.settings.mongo_db,
            clock_tz=self.settings.camera_clock_tz,
        )
        # Keyed per matched jump. A load master keys on ``(load_id, "load")`` — a string
        # where a jumper key holds its integer index, so a spec flyer's clips can never
        # be folded into a customer's job on the same load.
        self.pending: dict[tuple[str, int | str], PendingJump] = {}
        # Durable next to the jobs it created — survives a bridge restart, so a
        # re-notified clip never becomes a second job.
        self.state_path = default_state_path(self.settings)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, Any] = load_state(self.state_path)
        self._s3: Any | None = None

    # -- durable dedupe / flag record ------------------------------------- #

    def _save_state(self) -> None:
        try:
            save_state(self.state_path, self.state)
        except OSError as e:  # pragma: no cover - disk trouble must not 500 the notify
            logger.warning("could not persist bridge state: %r", e)

    # -- the notify endpoint ---------------------------------------------- #

    async def raw_upload(self, notice: dict[str, Any]) -> dict[str, Any]:
        from ingest.match import FootageMatchError, NoBookingMatch

        s3_key = notice.get("s3_key")
        if not s3_key:
            return {"status": "ignored", "reason": "no s3_key"}
        if s3_key in self.state["handled"] or s3_key in self.state["flagged"]:
            return {"status": "duplicate", "s3_key": s3_key}

        captured_at = notice.get("captured_at")
        if not captured_at:
            return self._flag(s3_key, "no captured_at — cannot match to a load")

        staff_id = notice.get("staff_id")
        try:
            # ``clip_ref`` labels the matcher's decision record with the clip this was
            # about, so an out-of-window acceptance can be investigated from either end.
            if staff_id:
                match = self.matcher.resolve_for_staff(
                    staff_id, captured_at, clip_ref=s3_key
                )
            else:
                match = self.matcher.resolve(
                    str(notice.get("camera_id", "")), captured_at, clip_ref=s3_key
                )
        except NoBookingMatch as e:
            # A camera flyer sent up on SPEC fills no slot on the manifest, so the
            # jumper-keyed match above can never succeed for him — this is exactly the
            # error it raises. Before flagging, ask the other question: does a load's
            # flight window contain this clip, with him absent from its jumpers? If so his
            # card is one load master for the whole load (the upsell engine). Anything
            # else — including NotSpecFlight, i.e. he DID have an assigned customer — is
            # flagged as before, with both refusals named so the cause is diagnosable.
            try:
                load_match = (
                    self.matcher.resolve_load_for_staff(staff_id, captured_at)
                    if staff_id
                    else self.matcher.resolve_load(
                        str(notice.get("camera_id", "")), captured_at
                    )
                )
            except FootageMatchError as load_e:
                return self._flag(
                    s3_key,
                    f"{type(e).__name__}: {e}; not a spec flight either "
                    f"({type(load_e).__name__}: {load_e})",
                )
            return self._accept(
                s3_key, notice, captured_at, load_match,
                key=(load_match.load_id, "load"),
                is_load_master=True,
                what=f"load {load_match.load_number} SPEC FLIGHT "
                     f"({len(load_match.jumpers)} on the manifest)",
                staff_id=staff_id,
            )
        except FootageMatchError as e:
            return self._flag(s3_key, f"{type(e).__name__}: {e}")

        if match.package is None:
            return self._flag(
                s3_key, f"unmappable purchase (mediaPackage={match.media_package!r})"
            )
        if not match.customer_email:
            return self._flag(s3_key, f"customer {match.customer_name!r} has no email")

        # Ownership that rested on the temporary compatibility path is recorded durably,
        # not only logged: the one-week review needs to count and inspect the real
        # population before the window is enforced strictly.
        self._record_ownership_evidence(s3_key, match)

        return self._accept(
            s3_key, notice, captured_at, match,
            key=(match.load_id, match.jumper_index),
            is_load_master=False,
            what=f"load {match.load_number} jumper {match.customer_name} ({match.package})",
            staff_id=staff_id,
        )

    def _accept(
        self,
        s3_key: str,
        notice: dict[str, Any],
        captured_at: str,
        match: Any,
        *,
        key: tuple[str, int | str],
        is_load_master: bool,
        what: str,
        staff_id: Any,
        load_evidence: str = "flight_window",
    ) -> dict[str, Any]:
        """Add a matched clip to its pending jump and (re)arm the settle timer."""
        jump = self.pending.get(key)
        if jump is None:
            jump = self.pending[key] = PendingJump(
                match=match, captured_at=captured_at, is_load_master=is_load_master,
                load_evidence=load_evidence,
            )
        jump.clips.append(notice)
        jump.clip_roles[s3_key] = None if is_load_master else match.role
        logger.info(
            "clip %s -> %s (%s); %d clip(s) pending, job in %gs%s",
            s3_key, what, "QR" if staff_id else "serial", len(jump.clips), self.debounce_s,
            "  [DEV DEBOUNCE — not a production setting]" if self.dev_debounce else "",
        )

        # (Re)arm the debounce: the job is created once this jump goes quiet.
        loop = asyncio.get_running_loop()
        if jump.timer is not None:
            jump.timer.cancel()
        jump.timer = loop.call_later(
            self.debounce_s, lambda: asyncio.ensure_future(self._flush(key))
        )
        return {"status": "accepted", "s3_key": s3_key, "pending": len(jump.clips)}

    def _record_ownership_evidence(self, s3_key: str, match: Any) -> None:
        """Append a non-``window`` ownership decision to the audit file. Never raises.

        One JSON object per line in ``jobs/_ownership_audit.jsonl``, carrying the matcher's
        whole decision record (clip, capture instant, load, departure and recorded flight,
        the window it missed and by how much, jumper slot, booking/customer ids) plus the
        customer name and the staff member — the fields an investigation starts from.

        Only written for evidence other than :data:`ingest.match.EVIDENCE_WINDOW`, so on a
        healthy dropzone this file stays empty and its line count IS the
        ``out_of_window_accept`` count. Best-effort: a full disk must not cost a customer
        their video, exactly like the archive.
        """
        from ingest.match import EVIDENCE_WINDOW

        evidence = getattr(match, "evidence", EVIDENCE_WINDOW)
        if evidence == EVIDENCE_WINDOW:
            return
        record = {
            **(getattr(match, "evidence_detail", None) or {}),
            "s3_key": s3_key,
            "staff_id": match.staff_id,
            "staff_name": match.staff_name,
            "customer_name": match.customer_name,
            "package": match.package,
            "entitlement": match.entitlement,
        }
        logger.warning(
            "%s: clip %s attached to %r on load %s (%s) — %s",
            evidence, s3_key, match.customer_name, match.load_number,
            record.get("departure_local"),
            "outside the load's flight window; review this before the window is enforced",
        )
        try:
            path = self.state_path.parent / OWNERSHIP_AUDIT_FILENAME
            with path.open("a") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as e:  # pragma: no cover - disk trouble must not fail a notify
            logger.warning("could not append to the ownership audit file: %r", e)

    def ownership_audit_count(self) -> int:
        """How many non-``window`` acceptances have been recorded (0 if none ever)."""
        path = self.state_path.parent / OWNERSHIP_AUDIT_FILENAME
        try:
            with path.open() as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    def _flag(self, s3_key: str, reason: str) -> dict[str, Any]:
        logger.warning("FLAGGED %s: %s", s3_key, reason)
        self.state["flagged"][s3_key] = reason
        self._save_state()
        return {"status": "flagged", "s3_key": s3_key, "reason": reason}

    # -- job creation ------------------------------------------------------ #

    def _s3_client(self) -> Any:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client(
                "s3",
                endpoint_url=self.settings.s3_endpoint_url,
                region_name=self.settings.s3_region,
            )
        return self._s3

    async def _flush(self, key: tuple[str, int | str]) -> None:
        jump = self.pending.pop(key, None)
        if jump is None:
            return
        try:
            await asyncio.to_thread(self._create_and_attach, jump)
        except Exception as e:  # noqa: BLE001 - log; the clips stay unhandled for a re-notify
            logger.exception("job creation failed for load %s: %r", key[0], e)

    def _job_payload(self, jump: PendingJump, jump_date: str) -> dict[str, Any]:
        """The ``POST /jobs`` body for this pending jump — a customer's, or a load's.

        A **load master** has no customer by construction: no email (so nothing is ever
        sent to it), and ``customer_name`` set to the load's label so the archive files it
        as ``{date}/{flyer}/Load 14/`` and the intro card burns an honest "Load 14". It
        runs ``video_only`` (the flyer's house cut, no photo set — a load's stills are of
        strangers) and ``preview_only``, which is both true (nobody bought it) and what
        makes the watermarked previews every locked child streams get rendered at all.
        """
        m = jump.match
        if jump.is_load_master:
            label = m.label
            return {
                "customer_name": label,
                "jump_date": jump_date,
                "package": "video_only",
                "entitlement": "preview_only",
                "instructor_name": m.staff_name,
                "job_kind": "load_master",
                "load_id": m.load_id,
                "load_label": label,
                # How the load was resolved: what the fan-out's freefall guard keys on.
                "load_evidence": jump.load_evidence,
                "load_roster": [
                    {
                        "jumper_index": j.jumper_index,
                        "customer_name": j.customer_name,
                        "customer_email": j.customer_email,
                        "booking_id": j.booking_id,
                        "bought_media": j.bought_media,
                    }
                    for j in m.jumpers
                ],
            }
        payload: dict[str, Any] = {
            "customer_name": m.customer_name or "Valued Skydiver",
            "customer_email": m.customer_email,
            "jump_date": jump_date,
            "package": m.package,
            "entitlement": m.entitlement,
            "booking_id": m.booking_id,
            "instructor_name": m.staff_name,
            "load_id": m.load_id,
            "jumper_index": m.jumper_index,
        }
        # A jumper holding TWO media products (a paid handcam package plus a speculative
        # camera-flyer twin) gets both on ONE job and ONE link. Sent only when the
        # matcher resolved more than one, so an ordinary jump's body is unchanged; the
        # ``package``/``entitlement`` above already mirror the primary ref, which
        # ``POST /jobs`` validates.
        if len(m.media_refs) > 1:
            payload["media_refs"] = [r.model_dump() for r in m.media_refs]
        return payload

    def _jump_state_key(self, jump: PendingJump) -> str | None:
        """The durable ``jumps`` key for this jump, or ``None`` for a load master.

        A load master is keyed by its load and has no second camera by construction, so
        it is deliberately excluded — the two-card reuse below is a mixed-jump rule.
        """
        if jump.is_load_master:
            return None
        m = jump.match
        return f"{m.load_id}:{m.jumper_index}"

    def _create_and_attach(self, jump: PendingJump) -> None:
        import httpx

        from api.auth import service_auth_headers

        m = jump.match
        package = "video_only" if jump.is_load_master else m.package
        multi_ref = not jump.is_load_master and len(m.media_refs) > 1
        jump_date = str(self.matcher._to_local(jump.captured_at).date())  # noqa: SLF001
        state_key = self._jump_state_key(jump)
        existing = self.state["jumps"].get(state_key) if state_key else None

        with httpx.Client(headers=service_auth_headers(), timeout=60.0) as client:
            if existing:
                # The jumper already has a job — this is their SECOND camera arriving
                # (a mixed jumper's spec twin, plugged in minutes or hours later). It
                # joins the same job, so the gallery grows a deliverable and the
                # customer keeps ONE link and ONE email.
                job_id = existing
                logger.info(
                    "job %s already exists for load %s jumper %s — attaching this "
                    "card's %d clip(s) as the %r camera",
                    job_id, m.load_id, m.jumper_index, len(jump.clips), m.role,
                )
            else:
                resp = client.post(
                    f"{self.api}/jobs", json=self._job_payload(jump, jump_date)
                )
                resp.raise_for_status()
                job_id = resp.json()["job_id"]
                if jump.is_load_master:
                    logger.info(
                        "load master %s created for %s (%d on the manifest, %d without media)",
                        job_id, m.label, len(m.jumpers),
                        sum(1 for j in m.jumpers if not j.bought_media),
                    )
                else:
                    logger.info(
                        "job %s created: %s <%s> %s/%s%s",
                        job_id, m.customer_name, m.customer_email, m.package,
                        m.entitlement,
                        (
                            " [mixed: "
                            + ", ".join(f"{r.role}={r.package}/{r.entitlement}"
                                        for r in m.media_refs)
                            + "]"
                        ) if multi_ref else "",
                    )
                if state_key:
                    self.state["jumps"][state_key] = job_id
                    self._save_state()

            # Fetch the masters back from S3 and attach them in ONE multipart POST
            # per camera role (the multi-file byte path dispatches processing once,
            # which the per-key s3_key path can't do for a multi-clip jump).
            #
            # On a mixed job the role comes from the MATCH, not from the notification:
            # one card is one camera, and the matcher already decided which slot that
            # staff member filled on this jumper (instructor vs assignedCameraman). It
            # is what decides whether this camera's edit is watermarked, so it must not
            # be re-guessed from a discovery hint.
            by_role: dict[str | None, list[dict[str, Any]]] = {}
            for notice in jump.clips:
                if multi_ref:
                    role: str | None = jump.clip_roles.get(notice["s3_key"], m.role)
                elif package == "ultimum":
                    role = notice.get("camera_role")
                else:
                    role = None
                by_role.setdefault(role, []).append(notice)

            with tempfile.TemporaryDirectory(prefix="bridge-") as tmp:
                for role, notices in by_role.items():
                    paths = []
                    for notice in notices:
                        dest = Path(tmp) / Path(notice["s3_key"]).name
                        self._s3_client().download_file(
                            self.settings.s3_bucket, notice["s3_key"], str(dest)
                        )
                        paths.append(dest)
                    handles = [p.open("rb") for p in paths]
                    try:
                        resp = client.post(
                            f"{self.api}/jobs/{job_id}/upload",
                            files=[("files", (p.name, fh, "video/mp4"))
                                   for p, fh in zip(paths, handles, strict=True)],
                            data={"camera_role": role} if role else None,
                            timeout=None,
                        )
                    finally:
                        for fh in handles:
                            fh.close()
                    resp.raise_for_status()
                    logger.info("attached %d clip(s)%s to job %s",
                                len(paths), f" [{role}]" if role else "", job_id)

        for notice in jump.clips:
            self.state["handled"][notice["s3_key"]] = job_id
        self._save_state()
        logger.info("job %s underway — auto-edit takes it from here", job_id)


def create_app(bridge: Bridge) -> Any:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from api.auth import service_token_allows

    app = FastAPI(title="SkydiveOS raw-upload bridge (local stand-in)")

    # This endpoint is reachable from the public internet (the dropzone's ingest
    # machine POSTs to it), and ONE accepted notification is enough to create a job,
    # render it and email a customer. A security-group /32 was the only gate, which
    # (a) breaks whenever the dropzone's IP changes and (b) is one console mistake
    # away from 0.0.0.0/0. So the same service token that protects the auto-edit API
    # protects this too — off until AUTO_EDIT_API_KEY is set, same opt-in as the gate.
    @app.middleware("http")
    async def _service_token_gate(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/api/") and not service_token_allows(
            request.url.path, request.method, request.headers.get("authorization"),
            bridge.settings,
        ):
            return JSONResponse({"detail": "service token required"}, status_code=401)
        return await call_next(request)

    # The notify body is taken as a plain dict, NOT via a `Request` parameter: this
    # module is `from __future__ import annotations`, so FastAPI resolves annotations
    # as strings against the MODULE globals — and a `Request` imported inside this
    # function isn't there. It silently degraded to "required query param `request`",
    # so every notify 422'd. `dict` is a builtin, so it always resolves.
    @app.post("/api/media/raw-upload")
    async def raw_upload(notice: dict[str, Any]) -> dict[str, Any]:
        return await bridge.raw_upload(notice)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "pending_jumps": len(bridge.pending),
            "handled": len(bridge.state["handled"]),
            "flagged": len(bridge.state["flagged"]),
            # The temporary-compatibility-path counter: how many clips were attached on
            # same-day-lone-candidate evidence rather than a flight window. Reviewed after
            # ~a week to decide whether the window becomes strict.
            "out_of_window_accepts": bridge.ownership_audit_count(),
        }

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address (default 127.0.0.1 — same-machine only). Use 0.0.0.0 when "
             "the bridge runs in a container or the notifier is another host; the "
             "notify carries no auth header, so restrict access at the firewall/proxy",
    )
    parser.add_argument("--api", default="http://localhost:8000", help="auto-edit API base")
    parser.add_argument(
        "--debounce", type=float, default=PRODUCTION_DEBOUNCE_S,
        help="seconds a jump waits for more clips before its job is created "
             f"(default {PRODUCTION_DEBOUNCE_S:.0f} — a shorter window split one jump "
             "into four jobs on a real uplink; see the module docstring)",
    )
    parser.add_argument(
        "--dev-debounce", type=float, default=None, metavar="SECONDS",
        help="DEV ONLY: shorten the settle window for local test cycles (e.g. 10). "
             f"Also settable as ${DEV_DEBOUNCE_ENV}. Off by default, may only shorten, "
             "and logs a warning banner the whole time it is active. Never use on a dropzone",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import uvicorn

    debounce_s = resolve_debounce(
        args.debounce,
        flag_s=args.dev_debounce,
        env_s=os.environ.get(DEV_DEBOUNCE_ENV),
    )
    bridge = Bridge(args.api, debounce_s=debounce_s)
    logger.info(
        "bridge on %s:%d → auto-edit at %s  (point discovery at SKYDIVEOS_API_BASE="
        "http://<this-host>:%d)", args.host, args.port, args.api, args.port,
    )
    uvicorn.run(create_app(bridge), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
