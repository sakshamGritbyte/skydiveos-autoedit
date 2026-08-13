#!/usr/bin/env python3
"""Generate the printable QR codes instructors film to claim an SD-card session.

The SD-card ingest flow (``CAMERA_SCANNER=sdcard``) attributes footage to whoever
filmed a short clip of their QR code during the recording session — at the start,
at the end, or anywhere in between; attribution is card-level, not sequential
(:mod:`ingest.qr`). Each code's payload is ``skydiveos-staff:<staffs._id>`` — the
SkydiveOS staff document id, NOT the auto-edit camera registry's instructor id
(the two differ; see ``ingest/match.py``). This script writes one captioned,
print-ready PNG per instructor.

Usage::

    # One code, id in hand (no DB needed)
    python scripts/make_instructor_qr.py --staff-id 665f1c0a2ab79c0012345678 --name "Marc Tremblay"

    # Every staff member with a name, from the shared DB ($MONGO_URL)
    python scripts/make_instructor_qr.py --all

Print each PNG at least ~10 cm wide and laminate it. Filming guidance for the
instructor: hold the code steady and roughly a third of the frame for 3–5 seconds,
once per session — before the first jump or after the last, whichever is easier.
That clip becomes the session marker and is dropped from the edit. Only when one
card carries TWO OR MORE people's sessions does consistency matter: then everyone
should film on the same side of their session (all at the start, or all at the end).

Exits non-zero on an unknown staff id or an empty staff collection.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ingest.qr import QR_STAFF_PREFIX  # noqa: E402

#: Rendered QR edge in pixels (before the caption strip is added below).
_QR_SIZE_PX = 1200
#: White quiet-zone border, in modules (spec minimum is 4).
_QUIET_MODULES = 6


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "instructor"


def _render(
    staff_id: str, name: str, out_dir: Path, *, payload: str | None = None,
    file_stem: str | None = None,
) -> Path:
    """Encode + caption one QR card; returns the written PNG path.

    ``payload`` defaults to the instructor payload ``skydiveos-staff:<staff_id>``.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise SystemExit(f"error: opencv-python is required to encode QR codes: {e!r}") from e
    from PIL import Image, ImageDraw, ImageFont

    # H-level error correction: the code is decoded off handheld fisheye footage,
    # so it needs every bit of redundancy the format offers.
    params = cv2.QRCodeEncoder.Params()
    params.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_H
    encoder = cv2.QRCodeEncoder.create(params)
    code = encoder.encode(payload or f"{QR_STAFF_PREFIX}{staff_id}")

    module_px = max(1, _QR_SIZE_PX // code.shape[0])
    quiet = _QUIET_MODULES * module_px
    scaled = cv2.resize(
        code,
        (code.shape[1] * module_px, code.shape[0] * module_px),
        interpolation=cv2.INTER_NEAREST,
    )
    bordered = cv2.copyMakeBorder(
        scaled, quiet, quiet, quiet, quiet, cv2.BORDER_CONSTANT, value=255
    )

    # Caption strip below the code: the instructor's name, so the right laminate
    # gets grabbed off the wall. Pillow is already a project dep (render/watermark).
    qr_img = Image.fromarray(np.asarray(bordered)).convert("L").convert("RGB")
    caption_h = max(90, qr_img.height // 8)
    page = Image.new("RGB", (qr_img.width, qr_img.height + caption_h), "white")
    page.paste(qr_img, (0, 0))
    draw = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(caption_h * 0.5))
    except OSError:
        font = ImageFont.load_default()
    text = name.strip()
    box = draw.textbbox((0, 0), text, font=font)
    x = (page.width - (box[2] - box[0])) // 2
    y = qr_img.height + (caption_h - (box[3] - box[1])) // 2
    draw.text((x, y), text, fill="black", font=font)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Two staff docs can share a display name (real DBs have duplicates); the id
    # suffix keeps one from silently overwriting the other's code — handing the
    # wrong laminate to an instructor mis-attributes every session they film.
    out = out_dir / f"{file_stem or _slug(name)}.png"
    if file_stem is None and out.exists():
        out = out_dir / f"{_slug(name)}-{staff_id[-6:]}.png"
    page.save(out)
    return out


def _staff_from_db() -> list[tuple[str, str]]:
    """Every named staff member from the shared DB as ``(staff_id, name)``."""
    from api.config import get_settings

    settings = get_settings()
    if not settings.mongo_url:
        raise SystemExit("error: --all needs MONGO_URL set (the shared SkydiveOS DB).")
    try:
        from pymongo import MongoClient
    except ImportError as e:  # pragma: no cover - only without the driver
        raise SystemExit(
            "error: pymongo is required for --all; install with 'uv pip install \"pymongo[srv]\"'."
        ) from e

    client = MongoClient(settings.mongo_url)
    try:
        staff: list[tuple[str, str]] = []
        for doc in client[settings.mongo_db]["staffs"].find():
            name = " ".join(
                p for p in (doc.get("firstName"), doc.get("lastName")) if p
            ).strip()
            if name:
                staff.append((str(doc["_id"]), name))
        return staff
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staff-id", help="SkydiveOS staffs._id to encode")
    parser.add_argument("--name", help="instructor display name for the caption")
    parser.add_argument("--all", action="store_true", help="one code per named staff in $MONGO_URL")
    parser.add_argument(
        "--out-dir", default="qr-codes", help="output directory (default: qr-codes/)"
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if args.all:
        staff = _staff_from_db()
        if not staff:
            print("error: no named staff found in the DB", file=sys.stderr)
            return 1
        for staff_id, name in staff:
            print(f"  {_render(staff_id, name, out_dir)}  <- {name} ({staff_id})")
        print(f"\n{len(staff)} QR codes written to {out_dir}/ — print big (>=10 cm) and laminate.")
        return 0

    if not args.staff_id or not args.name:
        parser.error("either --all, or both --staff-id and --name")
    out = _render(args.staff_id, args.name, out_dir)
    print(f"{out}  <- {args.name} ({args.staff_id})")
    print(
        "Print at >=10 cm wide; film it steady for 3-5 s once per session — start "
        "or end, either works."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
