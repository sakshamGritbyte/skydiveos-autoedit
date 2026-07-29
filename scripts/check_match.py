#!/usr/bin/env python3
"""Read-only check: would this dropzone's data actually assign footage to customers?

The camera→customer chain has three links, and two of them are *data*, not code:

    camera serial → staffs.goproSerial → the load that had departed → that jumper
                 → customer email + package

So footage can transfer perfectly and still reach nobody, because a staff member has no
``goproSerial`` recorded or a load has no departure time. This prints exactly what the
matcher would decide, against the live shared DB, without a GoPro and without touching
anything.

Usage::

    # Is the dropzone's data ready at all? (start here)
    python scripts/check_match.py --readiness

    # Replay a whole day: one simulated clip per load, showing who it lands on
    python scripts/check_match.py --day 2026-07-29

    # One specific clip: "a camera with this serial recorded at this DZ-local time"
    python scripts/check_match.py --serial TEST-CAM-SIM-01 --at 2026-07-29T12:20

    # What a real file on disk would resolve to (reads its creation_time)
    python scripts/check_match.py --serial TEST-CAM-SIM-01 --file /path/GX010001.MP4

``--at`` is DROPZONE-LOCAL wall-clock (what the camera's own clock shows); it is
converted to a true UTC instant exactly as the ingest module does, so what you see here
is what production would decide.

Exit code is 0 when every checked clip resolved to a customer, 1 otherwise — so
``--day`` doubles as a pre-flight check before a day's jumping.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _matcher():  # noqa: ANN202 - a thin CLI helper
    from api.config import get_settings
    from ingest.match import FootageMatcher

    settings = get_settings()
    if not settings.mongo_url:
        raise SystemExit("error: MONGO_URL is not set — nothing to check against.")
    return (
        FootageMatcher(
            settings.mongo_url,
            db_name=settings.mongo_db,
            clock_tz=settings.camera_clock_tz,
        ),
        settings,
    )


#: Outcome of one simulated clip: OK (deliverable), SKIP (nothing was bought — not a
#: fault), or FAIL (the booking wants media but the chain can't deliver it).
OK, SKIP, FAIL = "OK  ", "SKIP", "FAIL"


def _resolve(matcher, serial: str, captured_utc: dt.datetime) -> tuple[str, str]:
    """Ask the matcher, and render the answer as one human line."""
    from ingest.match import FootageMatchError

    try:
        r = matcher.resolve(serial, captured_utc)
    except FootageMatchError as e:
        return FAIL, f"{type(e).__name__}: {e}"
    # A jumper who bought no media is a correct outcome, not a broken one.
    if (r.media_package or "none").strip().lower() in ("", "none"):
        return SKIP, f"role={r.role} — booking buys no media (mediaPackage='none')"
    if r.package is None:
        return FAIL, (
            f"role={r.role} customer={r.customer_name!r} — add-on could not be mapped "
            f"to a package (mediaPackage={r.media_package!r}, videoType={r.video_type!r})"
        )
    if not r.customer_email:
        return FAIL, (
            f"role={r.role} pkg={r.package} customer={r.customer_name!r} "
            "— but NO customer_email, so nothing could be delivered"
        )
    return OK, (
        f"role={r.role:<10} pkg={r.package:<10} {r.customer_name!r} <{r.customer_email}>"
    )


def _readiness(matcher, settings) -> int:
    """Report the data prerequisites, which is where this usually fails."""
    db = matcher._database()  # noqa: SLF001 - a diagnostic, deliberately nosy
    staff = list(db["staffs"].find({}))
    with_serial = [s for s in staff if (s.get("goproSerial") or "").strip()]
    cameras = list(db["cameras"].find({}))
    loads = list(db["loads"].find({}))

    print(f"database         : {settings.mongo_db}")
    print(f"camera clock tz  : {settings.camera_clock_tz or '(unset — timestamps stay as-is!)'}")
    print(f"staff            : {len(staff)}")
    print(f"  with goproSerial: {len(with_serial)}")
    for s in with_serial:
        name = s.get("name") or " ".join(
            p for p in (s.get("firstName"), s.get("lastName")) if p
        )
        print(f"      {s.get('goproSerial'):<18} {name}")
    missing = [s for s in staff if s not in with_serial]
    if missing:
        print(f"  WITHOUT goproSerial: {len(missing)} — their footage can never be matched")
        for s in missing[:10]:
            name = s.get("name") or " ".join(
                p for p in (s.get("firstName"), s.get("lastName")) if p
            )
            print(f"      (none)             {name}")
    print(f"paired cameras   : {len(cameras)}  {[c.get('camera_id') for c in cameras]}")
    print(f"loads            : {len(loads)}")

    # A GoPro's BLE name is "GoPro" + the LAST 4 DIGITS of its serial, so two cameras
    # ending the same way advertise identically. BLE then cannot tell them apart at all
    # — the pull targets whichever answers first — and the owner lookup refuses. Catch
    # it here rather than on a jump day.
    by_tail: dict[str, list[str]] = {}
    for s in with_serial:
        serial = str(s.get("goproSerial")).strip()
        by_tail.setdefault(serial[-4:], []).append(serial)
    collisions = {tail: v for tail, v in by_tail.items() if len(v) > 1}
    if collisions:
        print()
        for tail, serials in collisions.items():
            print(
                f"WARNING: {len(serials)} cameras' serials end in {tail!r} ({serials}). "
                "They advertise the SAME BLE name, so the pull cannot distinguish them "
                "and the owner lookup will refuse. Use different cameras at this dropzone."
            )

    # Discovery matches a scanned id against cameras.camera_id EXACTLY, and a GoPro only
    # ever advertises its trailing serial digits. So a registry entry holding the full
    # printed serial is dead weight — BLE will never report it and it is never pulled.
    unpullable = [
        str(c.get("camera_id")) for c in cameras
        if not str(c.get("camera_id")).strip().isdigit()
    ]
    if unpullable:
        print(
            f"\nWARNING: registry entr{'y' if len(unpullable) == 1 else 'ies'} {unpullable} "
            "cannot be discovered: a GoPro advertises only its trailing serial DIGITS "
            "(e.g. 'GoPro 4313' -> '4313'), so this never matches a scan. Pair with the "
            "short id; keep the full serial in staffs.goproSerial."
        )

    registry_serials = {str(c.get("camera_id")) for c in cameras}
    staff_serials = {str(s.get("goproSerial")) for s in with_serial}
    # A registry id is the short BLE id; a staff serial is the full one. A camera is
    # "owned" when its id is the tail of some staff serial (see FootageMatcher).
    orphan = {
        cam for cam in registry_serials
        if not any(full.endswith(cam) or full == cam for full in staff_serials)
    }
    if orphan:
        print(
            f"\nWARNING: paired camera(s) {sorted(orphan)} match no staffs.goproSerial — "
            "their footage resolves as unknown and falls back to the static role hint."
        )
    if not settings.camera_clock_tz:
        print(
            "\nWARNING: CAMERA_CLOCK_TZ is unset. GoPro writes LOCAL time labelled UTC, so "
            "matches will skew by the dropzone's UTC offset."
        )
    return 0 if with_serial and loads else 1


def _replay_day(matcher, settings, day: str) -> int:
    """One simulated clip per load that day: who would each land on?"""
    db = matcher._database()  # noqa: SLF001
    tz = ZoneInfo(settings.camera_clock_tz or "UTC")
    staff = {str(d["_id"]): d for d in db["staffs"].find({})}

    loads = sorted(
        (
            load for load in db["loads"].find({})
            if isinstance(load.get("departureTime"), dt.datetime)
            and load["departureTime"].date().isoformat() == day
        ),
        key=lambda load: load["departureTime"],
    )
    if not loads:
        print(f"no loads with a departureTime on {day}")
        # Save the caller guessing: name the days that DO have loads.
        days = sorted({
            load["departureTime"].date().isoformat()
            for load in db["loads"].find({})
            if isinstance(load.get("departureTime"), dt.datetime)
        })
        if days:
            print(f"days with loads ({len(days)}): {', '.join(days[-15:])}")
            if len(days) > 15:
                print(f"  … and {len(days) - 15} earlier")
        else:
            print("no load in the database has a departureTime — nothing can be matched")
        return 1

    print(f"replaying {len(loads)} load(s) on {day} (clip 5 min after each departure)\n")
    ok = skipped = failed = 0
    for load in loads:
        dep = load["departureTime"]
        print(f"load {dep:%H:%M}  status={load.get('status')}")
        # DZ-local wall clock -> the true UTC instant ingest would send.
        captured_utc = (dep + dt.timedelta(minutes=5)).replace(tzinfo=tz).astimezone(dt.UTC)
        for jumper in load.get("jumpers") or []:
            for slot, label in (("instructor", "handcam"), ("assignedCameraman", "outside")):
                who = staff.get(str(jumper.get(slot)))
                serial = (who or {}).get("goproSerial")
                if not serial:
                    print(f"    {label:<8} — no camera serial on this staff member, skipped")
                    continue
                verdict, detail = _resolve(matcher, serial, captured_utc)
                print(f"    {label:<8} {serial:<18} {verdict} {detail}")
                ok += verdict == OK
                skipped += verdict == SKIP
                failed += verdict == FAIL
        print()
    print(f"deliverable {ok}, no-media {skipped}, FAILED {failed}")
    return 0 if failed == 0 else 1


def _list_owner_loads(matcher, settings, serial: str) -> int:
    """Every load this camera's owner flew — i.e. the days worth replaying."""
    from ingest.match import FootageMatcher, FootageMatchError

    db = matcher._database()  # noqa: SLF001
    try:
        staff = FootageMatcher._staff_for_camera(db, serial)  # noqa: SLF001
    except FootageMatchError as e:
        print(f"{type(e).__name__}: {e}")
        return 1
    name = staff.get("name") or " ".join(
        p for p in (staff.get("firstName"), staff.get("lastName")) if p
    )
    print(f"camera {serial} -> {name} (goproSerial={staff.get('goproSerial')!r})\n")

    staff_id = staff["_id"]
    rows = []
    for load in db["loads"].find(
        {"$or": [{"jumpers.instructor": staff_id}, {"jumpers.assignedCameraman": staff_id}]}
    ):
        dep = load.get("departureTime")
        if not isinstance(dep, dt.datetime):
            continue
        for jumper in load.get("jumpers") or []:
            if jumper.get("instructor") == staff_id:
                role = "instructor"
            elif jumper.get("assignedCameraman") == staff_id:
                role = "external"
            else:
                continue
            rows.append((dep, role, load.get("status"), jumper.get("mediaPackage")))

    if not rows:
        print("this staff member is not on any load — nothing to replay")
        return 1
    print(f"{len(rows)} jump(s) flown:")
    for dep, role, status, media in sorted(rows):
        print(f"  {dep:%Y-%m-%d %H:%M}  {role:<10} status={status:<10} media={media!r}")
    print(f"\nreplay one with:  --day {sorted(rows)[-1][0]:%Y-%m-%d}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python scripts/check_match.py",
        description="Show what the footage→customer matcher would decide. Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--readiness", action="store_true", help="check the data prerequisites")
    p.add_argument("--day", default=None, help="replay every load on this ISO date")
    p.add_argument("--serial", default=None, help="camera serial (staffs.goproSerial)")
    p.add_argument("--at", default=None, help="DZ-LOCAL capture time, e.g. 2026-07-29T12:20")
    p.add_argument("--file", default=None, help="read the capture time from this MP4 instead")
    args = p.parse_args(argv)

    matcher, settings = _matcher()
    try:
        if args.readiness:
            return _readiness(matcher, settings)
        if args.day:
            return _replay_day(matcher, settings, args.day)
        if args.serial and not (args.at or args.file):
            # No time given: list the jumps this camera's owner actually flew, so you
            # know which day is worth replaying instead of guessing.
            return _list_owner_loads(matcher, settings, args.serial)
        if not args.serial:
            p.error("give --readiness, --day, or --serial (optionally with --at/--file)")

        if args.file:
            from ingest.discovery import _probe_capture_time

            iso = _probe_capture_time(args.file, clock_tz=settings.camera_clock_tz)
            if iso is None:
                print(f"could not read a capture time from {args.file}")
                return 1
            captured_utc = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            print(f"{Path(args.file).name}: captured_at (true UTC) = {iso}")
        else:
            tz = ZoneInfo(settings.camera_clock_tz or "UTC")
            captured_utc = dt.datetime.fromisoformat(args.at).replace(tzinfo=tz).astimezone(dt.UTC)
            zone = settings.camera_clock_tz or "UTC"
            print(f"clip at {args.at} {zone} -> {captured_utc:%Y-%m-%dT%H:%M:%SZ}")

        verdict, detail = _resolve(matcher, args.serial, captured_utc)
        print(f"{verdict} {detail}")
        return 0 if verdict != FAIL else 1
    finally:
        matcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
