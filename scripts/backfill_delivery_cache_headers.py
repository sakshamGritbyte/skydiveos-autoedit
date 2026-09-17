"""Stamp ``Cache-Control`` on delivery objects uploaded before Bug 373 shipped.

Why a backfill exists at all: ``api.delivery.upload_and_link`` now sets
``DELIVERY_CACHE_CONTROL`` on every object it writes, but an object already in the
bucket keeps whatever metadata it was written with — and a live customer's gallery
link never expires, so the galleries most likely to be replayed are exactly the ones
whose objects predate the fix. With no lifetime on the object:

* CloudFront still caches it (the distribution's cache policy has a default TTL), so
  the edge part of the fix works either way;
* the **viewer's browser** is told nothing, so a replay or reload revalidates — or
  re-fetches the whole MP4 — every single time. That is the reported symptom, and on a
  pre-fix object it survives the CDN.

What it does: for each ``deliveries/**`` object whose ``Cache-Control`` differs from
``DELIVERY_CACHE_CONTROL``, a **metadata-only** ``CopyObject`` onto its own key with
``MetadataDirective=REPLACE``. That rewrites the header while preserving the bytes,
the key, the storage class and the content type (re-declared from the object's own
current value, never re-guessed from the name). It is idempotent — a second run finds
nothing to do — and it never deletes, moves or re-encodes anything.

What it deliberately does NOT touch:

* ``raw/**`` — ingest masters, never served to a browser.
* ``gallery.html`` — the legacy S3 gallery page bakes its lock state in at delivery;
  a day of browser caching there could show a stale paywall. Videos and stills are
  immutable bytes; a page is not.
* Object ACLs, encryption, tags, versions, or anything outside ``deliveries/``.

Run it against each media bucket the deployment uses (one per tenant), e.g.::

    cd /opt/skydiveos-autoedit && set -a && . ./.env && set +a && \
        .venv/bin/python scripts/backfill_delivery_cache_headers.py --dry-run

``--dry-run`` is the DEFAULT: it lists what would change and writes nothing. Pass
``--apply`` to perform the copies.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import get_settings  # noqa: E402
from api.delivery import DELIVERY_CACHE_CONTROL, DELIVERY_KEY_PREFIX  # noqa: E402

#: Extensions worth a lifetime: the media a gallery actually re-fetches. Anything else
#: under ``deliveries/`` (``source_usage.json``, ``gallery.html``) is left alone —
#: see the module docstring.
CACHEABLE_SUFFIXES = (".mp4", ".jpg", ".jpeg", ".zip")


def _cacheable(key: str) -> bool:
    """Whether this delivery key is one of the media objects a browser re-fetches."""
    return key.lower().endswith(CACHEABLE_SUFFIXES)


def iter_delivery_objects(client: Any, bucket: str) -> Any:
    """Every object under ``deliveries/`` in ``bucket``, paginated."""
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{DELIVERY_KEY_PREFIX}/"
    ):
        yield from page.get("Contents", []) or []


def needs_stamp(head: dict) -> bool:
    """Whether this object's ``Cache-Control`` differs from what we now write.

    Compared against the constant rather than merely tested for presence, so a bucket
    stamped by an older lifetime is corrected too — the point is that every gallery
    behaves the same, not that some header exists.
    """
    return (head.get("CacheControl") or "").strip() != DELIVERY_CACHE_CONTROL


def restamp(client: Any, bucket: str, key: str, head: dict) -> None:
    """Rewrite one object's metadata in place, preserving its bytes and content type.

    ``MetadataDirective=REPLACE`` drops every header not restated here, which is why
    ``ContentType`` is carried over from the object's OWN current value: re-deriving it
    from the filename would be a second guess, and a video served as
    ``application/octet-stream`` does not play. ``StorageClass`` is restated for the
    same reason — a copy defaults to STANDARD and would silently promote a
    lifecycle-transitioned object.
    """
    extra: dict[str, str] = {}
    if head.get("ContentType"):
        extra["ContentType"] = str(head["ContentType"])
    if head.get("StorageClass"):
        extra["StorageClass"] = str(head["StorageClass"])
    client.copy_object(
        Bucket=bucket,
        Key=key,
        CopySource={"Bucket": bucket, "Key": key},
        MetadataDirective="REPLACE",
        CacheControl=DELIVERY_CACHE_CONTROL,
        **extra,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/backfill_delivery_cache_headers.py",
        description=(
            "Stamp Cache-Control on deliveries/** objects written before Bug 373. "
            "Dry-run by default."
        ),
    )
    parser.add_argument(
        "--bucket",
        action="append",
        default=None,
        help="bucket to sweep (repeatable). Default: the configured S3_BUCKET.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="perform the copies (default: dry-run)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    buckets = args.bucket or ([settings.s3_bucket] if settings.s3_bucket else [])
    if not buckets:
        print(
            "error: no bucket — configure S3_BUCKET or pass --bucket NAME.",
            file=sys.stderr,
        )
        return 2

    from api.delivery import _default_s3_client

    client = _default_s3_client(settings)
    verb = "stamping" if args.apply else "[dry-run] would stamp"
    total_seen = total_changed = 0
    failures = 0

    for bucket in buckets:
        seen = changed = skipped = 0
        for obj in iter_delivery_objects(client, bucket):
            key = obj["Key"]
            if not _cacheable(key):
                skipped += 1
                continue
            seen += 1
            try:
                head = client.head_object(Bucket=bucket, Key=key)
                if not needs_stamp(head):
                    continue
                changed += 1
                print(f"{verb}: s3://{bucket}/{key}")
                if args.apply:
                    restamp(client, bucket, key, head)
            except Exception as exc:  # noqa: BLE001 - one object must not stop the sweep
                failures += 1
                print(f"  FAILED {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            f"{bucket}: {seen} media object(s), {changed} "
            f"{'stamped' if args.apply else 'to stamp'}, {skipped} non-media skipped"
        )
        total_seen += seen
        total_changed += changed

    print(
        f"\n{total_changed}/{total_seen} object(s) "
        f"{'stamped with' if args.apply else 'would be stamped with'} "
        f"Cache-Control: {DELIVERY_CACHE_CONTROL}"
    )
    if failures:
        print(f"{failures} object(s) failed — rerun to retry them.", file=sys.stderr)
    if not args.apply and total_changed:
        print("Nothing was written. Re-run with --apply to perform the copies.")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
