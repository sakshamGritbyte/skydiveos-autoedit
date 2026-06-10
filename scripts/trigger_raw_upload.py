"""Fire ONE auto-discovery hand-off at SkydiveOS — for manual end-to-end testing.

Does exactly what the discovery loop does for a single file, on demand: uploads the
file to S3 (the bucket from your env) and POSTs the JSON notification
``{s3_key, camera_id, instructor_id}`` to ``{SKYDIVEOS_API_BASE}/api/media/raw-upload``
— so you can verify your SkydiveOS receiver + media module without a GoPro or waiting
for the BLE scan.

Usage (from the repo root)::

    python scripts/trigger_raw_upload.py \
        --camera TESTGOPRO001 \
        --instructor 6a16d38603b4c98fa2a9cd14 \
        [--file sample-data/discovery_sample.mp4]

Reads S3 + SkydiveOS config from the same env/.env the API uses (S3_BUCKET /
AWS_S3_BUCKET_NAME, AWS_REGION, SKYDIVEOS_API_BASE).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable when run as `python scripts/trigger_raw_upload.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import get_settings  # noqa: E402
from ingest.discovery import RAW_UPLOAD_PATH, s3_notify_uploader  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python scripts/trigger_raw_upload.py",
        description="Upload one file to S3 and notify SkydiveOS, like auto-discovery does.",
    )
    parser.add_argument("--camera", default="TESTGOPRO001", help="camera serial (camera_id)")
    parser.add_argument(
        "--instructor", default=None, help="owning instructor id (omit for unassigned)"
    )
    parser.add_argument(
        "--file",
        default=settings.discovery_sample_mp4 or "sample-data/discovery_sample.mp4",
        help="file to upload (default: the discovery sample)",
    )
    args = parser.parse_args(argv)

    # Fail early with a clear message rather than a deep traceback.
    if not settings.skydiveos_api_base:
        print("error: SKYDIVEOS_API_BASE is not set (nothing to notify)")
        return 1
    if not settings.s3_bucket:
        print("error: S3 bucket not set (S3_BUCKET or AWS_S3_BUCKET_NAME)")
        return 1
    if not Path(args.file).is_file():
        print(f"error: file not found: {args.file}")
        return 1

    target = f"{settings.skydiveos_api_base.rstrip('/')}{RAW_UPLOAD_PATH}"
    print(f"uploading {args.file}")
    print(f"  -> s3://{settings.s3_bucket}/raw/{args.camera}/{Path(args.file).name}")
    print(f"  -> notify {target}  (camera={args.camera}, instructor={args.instructor})")

    upload = s3_notify_uploader(
        settings.skydiveos_api_base,
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
    )
    try:
        upload(args.file, args.camera, args.instructor)
    except Exception as e:  # noqa: BLE001 - surface a friendly message for the operator
        print(f"FAILED: {e!r}")
        print(
            "  hints: is SkydiveOS running and does it expose "
            f"POST {RAW_UPLOAD_PATH}? are the AWS creds valid?"
        )
        return 1

    print("OK: file uploaded to S3 and SkydiveOS notified. Check your media module.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
