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
  ``--debounce`` seconds, so a session's clips land in ONE job.
* **Refuse-and-flag** — an unmatchable clip (no load fits, ambiguous, unmappable
  purchase) is logged + recorded and acknowledged with 200: mis-delivery is worse
  than a human follow-up, and a 5xx would make discovery retry forever.

Run::

    python scripts/skydiveos_bridge.py [--port 9000] [--api http://localhost:8000]

and point discovery at it: ``SKYDIVEOS_API_BASE=http://localhost:9000``.
Needs ``MONGO_URL`` (the match) and ``S3_BUCKET`` (to fetch the clips back).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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


@dataclass
class PendingJump:
    """Clips accumulating for one matched jump until the debounce expires."""

    match: Any  # ingest.match.MatchResult
    captured_at: str
    clips: list[dict[str, Any]] = field(default_factory=list)  # raw notifications
    timer: asyncio.TimerHandle | None = None


class Bridge:
    def __init__(self, api: str, *, debounce_s: float = 20.0) -> None:
        from api.config import get_settings
        from ingest.match import FootageMatcher

        self.api = api.rstrip("/")
        self.debounce_s = debounce_s
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
        self.pending: dict[tuple[str, int], PendingJump] = {}
        # Durable next to the jobs it created — survives a bridge restart, so a
        # re-notified clip never becomes a second job.
        jobs_root = Path(self.settings.jobs_root or "jobs")
        jobs_root.mkdir(parents=True, exist_ok=True)
        self.state_path = jobs_root / STATE_FILENAME
        self.state: dict[str, Any] = self._load_state()
        self._s3: Any | None = None

    # -- durable dedupe / flag record ------------------------------------- #

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"handled": {}, "flagged": {}}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")
        except OSError as e:  # pragma: no cover - disk trouble must not 500 the notify
            logger.warning("could not persist bridge state: %r", e)

    # -- the notify endpoint ---------------------------------------------- #

    async def raw_upload(self, notice: dict[str, Any]) -> dict[str, Any]:
        from ingest.match import FootageMatchError

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
            if staff_id:
                match = self.matcher.resolve_for_staff(staff_id, captured_at)
            else:
                match = self.matcher.resolve(str(notice.get("camera_id", "")), captured_at)
        except FootageMatchError as e:
            return self._flag(s3_key, f"{type(e).__name__}: {e}")
        if match.package is None:
            return self._flag(
                s3_key, f"unmappable purchase (mediaPackage={match.media_package!r})"
            )
        if not match.customer_email:
            return self._flag(s3_key, f"customer {match.customer_name!r} has no email")

        key = (match.load_id, match.jumper_index)
        jump = self.pending.get(key)
        if jump is None:
            jump = self.pending[key] = PendingJump(match=match, captured_at=captured_at)
        jump.clips.append(notice)
        logger.info(
            "clip %s -> load %s jumper %s (%s, %s); %d clip(s) pending, job in %ss",
            s3_key, match.load_number, match.customer_name, match.package,
            "QR" if staff_id else "serial", len(jump.clips), self.debounce_s,
        )

        # (Re)arm the debounce: the job is created once this jump goes quiet.
        loop = asyncio.get_running_loop()
        if jump.timer is not None:
            jump.timer.cancel()
        jump.timer = loop.call_later(
            self.debounce_s, lambda: asyncio.ensure_future(self._flush(key))
        )
        return {"status": "accepted", "s3_key": s3_key, "pending": len(jump.clips)}

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

    async def _flush(self, key: tuple[str, int]) -> None:
        jump = self.pending.pop(key, None)
        if jump is None:
            return
        try:
            await asyncio.to_thread(self._create_and_attach, jump)
        except Exception as e:  # noqa: BLE001 - log; the clips stay unhandled for a re-notify
            logger.exception("job creation failed for load %s: %r", key[0], e)

    def _create_and_attach(self, jump: PendingJump) -> None:
        import httpx

        from api.auth import service_auth_headers

        m = jump.match
        jump_date = str(self.matcher._to_local(jump.captured_at).date())  # noqa: SLF001

        with httpx.Client(headers=service_auth_headers(), timeout=60.0) as client:
            resp = client.post(
                f"{self.api}/jobs",
                json={
                    "customer_name": m.customer_name or "Valued Skydiver",
                    "customer_email": m.customer_email,
                    "jump_date": jump_date,
                    "package": m.package,
                    "entitlement": m.entitlement,
                    "booking_id": m.booking_id,
                    "instructor_name": m.staff_name,
                },
            )
            resp.raise_for_status()
            job_id = resp.json()["job_id"]
            logger.info(
                "job %s created: %s <%s> %s/%s",
                job_id, m.customer_name, m.customer_email, m.package, m.entitlement,
            )

            # Fetch the masters back from S3 and attach them in ONE multipart POST
            # per camera role (the multi-file byte path dispatches processing once,
            # which the per-key s3_key path can't do for a multi-clip jump).
            by_role: dict[str | None, list[dict[str, Any]]] = {}
            for notice in jump.clips:
                role = notice.get("camera_role") if m.package == "ultimum" else None
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

    app = FastAPI(title="SkydiveOS raw-upload bridge (local stand-in)")

    @app.post("/api/media/raw-upload")
    async def raw_upload(request: Request) -> dict[str, Any]:
        return await bridge.raw_upload(dict(await request.json()))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "pending_jumps": len(bridge.pending),
            "handled": len(bridge.state["handled"]),
            "flagged": len(bridge.state["flagged"]),
        }

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--api", default="http://localhost:8000", help="auto-edit API base")
    parser.add_argument(
        "--debounce", type=float, default=20.0,
        help="seconds a jump waits for more clips before its job is created (default 20)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import uvicorn

    bridge = Bridge(args.api, debounce_s=args.debounce)
    logger.info(
        "bridge on :%d → auto-edit at %s  (point discovery at SKYDIVEOS_API_BASE="
        "http://localhost:%d)", args.port, args.api, args.port,
    )
    uvicorn.run(create_app(bridge), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
