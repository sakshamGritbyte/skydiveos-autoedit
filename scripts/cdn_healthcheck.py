"""Answer "is CloudFront delivery actually live, and is it caching?" (Bug 373).

The code side of Bug 373 degrades silently on purpose: with ``CDN_BASE_URL`` and its two
key settings unset — or the PEM unreadable — the gallery falls back to the pre-CDN
behaviour and logs a warning. That is the right failure mode for a customer, and a
terrible one for an operator: a stack that was never configured looks exactly like a
stack that was, and the only difference is a header nobody reads.

So this script proves the chain end to end, in five isolated steps, and says which one
broke:

1. **Config** — all three settings present, and the private key actually loads into a
   signer (a wrong path or a non-RSA key fails here, not in front of a customer).
2. **Object** — the deliverable is in S3 under ``deliveries/{job}/{name}.mp4``, with its
   ``Cache-Control`` and average bitrate reported. A missing lifetime is the thing
   ``scripts/backfill_delivery_cache_headers.py`` fixes; a bitrate far above ~10 Mbit/s
   is a render that predates the VBV cap.
3. **Determinism** — two signed URLs minted a moment apart are byte-identical. This is
   what makes a replay cheap: a per-request presigned URL never repeats, which was the
   bug. A ``CDN_URL_TTL_S`` of 0/negative, or a clock jumping a window boundary between
   the two calls, shows up here.
4. **Edge** — a range request over that URL returns ``206`` with ``Accept-Ranges``, and a
   REPEAT of it reports ``X-Cache: Hit from cloudfront``. A Miss on the second try means
   the distribution is caching nothing (usually a cache policy forwarding all query
   strings, so the signature is part of the cache key).
5. **Paywall** — the same URL with its signature stripped is refused (``403``). If this
   step passes traffic, the CDN is an open bucket and every unlisted key is public.

Read-only: it fetches one kilobyte and writes nothing, anywhere. Run it on the box that
serves the galleries, with the deployment's env loaded::

    cd /opt/skydiveos-autoedit && set -a && . ./.env && set +a && \
        .venv/bin/python scripts/cdn_healthcheck.py

With no ``--job`` it picks the most recently updated **delivered** job that has a video
deliverable, which is the one a customer is most likely watching right now.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import cdn  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.delivery import DELIVERY_CACHE_CONTROL, delivery_s3_key  # noqa: E402
from api.jobs import JobStatus, JobStore  # noqa: E402

OK = "PASS"
BAD = "FAIL"
MEH = "WARN"

#: Above this, a 1080p render is heavy for web playback even from an edge — it predates
#: the VBV cap (``render.render.MAXRATE``) or was produced by a path without one.
BITRATE_WARN_MBPS = 10.0


def _say(verdict: str, step: str, detail: str) -> None:
    print(f"[{verdict}] {step}: {detail}")


def _pick_job(store: JobStore) -> tuple[str, str] | None:
    """The newest delivered job with a video deliverable, as ``(job_id, name)``."""
    best: tuple[float, str, str] | None = None
    root = store.dir("_probe").parent
    if not root.is_dir():
        return None
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        try:
            job = store.load(entry.name)
        except (FileNotFoundError, ValueError):
            continue
        if job.status is not JobStatus.delivered:
            continue
        names = [n for n in (job.outputs or {}) if n != "photos"]
        if not names:
            continue
        # Prefer the full edit — the biggest file, so the worst case for buffering.
        name = "full_video" if "full_video" in names else sorted(names)[0]
        if best is None or job.updated_at > best[0]:
            best = (job.updated_at, job.job_id, name)
    return (best[1], best[2]) if best else None


def _strip_signature(url: str) -> str:
    """The same URL with CloudFront's signing params removed (step 5's probe)."""
    parts = urlsplit(url)
    kept = [
        kv
        for kv in parts.query.split("&")
        if kv and kv.split("=")[0] not in {"Expires", "Signature", "Key-Pair-Id", "Policy"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), ""))


def check_config(settings: Any) -> bool:
    if not cdn.cdn_enabled(settings):
        missing = [
            n
            for n, v in (
                ("CDN_BASE_URL", settings.cdn_base_url),
                ("CDN_KEY_PAIR_ID", settings.cdn_key_pair_id),
                ("CDN_PRIVATE_KEY_PATH", settings.cdn_private_key_path),
            )
            if not v
        ]
        _say(
            BAD,
            "config",
            f"CDN delivery is OFF — unset: {', '.join(missing)}. Galleries are streaming "
            "through this process (or via per-request presigned URLs after pruning), "
            "which is the pre-Bug-373 behaviour. See deploy/CLOUDFRONT.md §2.",
        )
        return False
    signer = cdn._signer(str(settings.cdn_key_pair_id), str(settings.cdn_private_key_path))
    if signer is None:
        _say(
            BAD,
            "config",
            f"the private key at {settings.cdn_private_key_path} could not be loaded as an "
            "RSA key — every sign attempt will degrade to non-CDN delivery.",
        )
        return False
    _say(
        OK,
        "config",
        f"base={settings.cdn_base_url} key={settings.cdn_key_pair_id} "
        f"ttl={settings.cdn_url_ttl_s}s, signer loaded",
    )
    return True


def check_object(settings: Any, job_id: str, name: str, duration: float | None) -> bool:
    key = delivery_s3_key(job_id, f"{name}.mp4")
    if not settings.s3_bucket:
        _say(BAD, "object", "S3_BUCKET is not configured, so nothing can be checked.")
        return False
    try:
        from api.delivery import _default_s3_client

        head = _default_s3_client(settings).head_object(Bucket=settings.s3_bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - report, never traceback
        _say(
            BAD,
            "object",
            f"s3://{settings.s3_bucket}/{key} unreadable: {type(exc).__name__}: {exc}",
        )
        return False
    size = int(head.get("ContentLength") or 0)
    cache = (head.get("CacheControl") or "").strip()
    _say(
        OK,
        "object",
        f"s3://{settings.s3_bucket}/{key} — {size / 1e6:.1f} MB, "
        f"type={head.get('ContentType')}",
    )
    if cache != DELIVERY_CACHE_CONTROL:
        _say(
            MEH,
            "object cache-control",
            f"{cache or '(unset)'} — expected {DELIVERY_CACHE_CONTROL!r}. The edge still "
            "caches it, but the VIEWER's browser is told nothing, so a replay re-fetches. "
            "Fix: scripts/backfill_delivery_cache_headers.py --apply",
        )
    else:
        _say(OK, "object cache-control", cache)
    if duration and duration > 0:
        mbps = (size * 8) / duration / 1e6
        verdict = MEH if mbps > BITRATE_WARN_MBPS else OK
        _say(
            verdict,
            "object bitrate",
            f"{mbps:.1f} Mbit/s over {duration:.0f}s"
            + (
                " — heavy for web playback even from an edge; this render predates the "
                "VBV cap (render.render.MAXRATE)."
                if verdict is MEH
                else ""
            ),
        )
    return True


def check_determinism(settings: Any, job_id: str, name: str) -> str | None:
    first = cdn.signed_delivery_url(job_id, f"{name}.mp4", settings)
    time.sleep(1.0)
    second = cdn.signed_delivery_url(job_id, f"{name}.mp4", settings)
    if not first or not second:
        _say(BAD, "signing", "signed_delivery_url returned None — see the log for the reason.")
        return None
    if first != second:
        _say(
            BAD,
            "determinism",
            "two URLs minted a second apart DIFFER, so a replay cannot reuse the browser's "
            "cache — check CDN_URL_TTL_S (it must be a positive number of seconds).",
        )
        return first
    _say(OK, "determinism", "two signed URLs a second apart are byte-identical")
    return first


def check_edge(url: str) -> bool:
    import httpx

    headers = {"Range": "bytes=0-1023"}
    try:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            first = client.get(url, headers=headers)
            second = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        _say(BAD, "edge", f"could not reach the CDN: {type(exc).__name__}: {exc}")
        return False
    if first.status_code != 206:
        _say(
            BAD,
            "edge",
            f"range request answered {first.status_code} (expected 206). Body: "
            f"{first.text[:200]!r}",
        )
        return False
    _say(
        OK,
        "edge range",
        f"206, content-range={first.headers.get('content-range')}, "
        f"accept-ranges={first.headers.get('accept-ranges')}",
    )
    x1 = first.headers.get("x-cache", "?")
    x2 = second.headers.get("x-cache", "?")
    if "Hit" not in x2:
        _say(
            MEH,
            "edge cache",
            f"repeat request reported X-Cache: {x2} (first: {x1}). The edge is caching "
            "nothing — check the distribution's cache policy (query strings must be "
            "'whitelist: v' only, so the signature is not part of the cache key).",
        )
        return True
    _say(OK, "edge cache", f"first={x1}, repeat={x2}")
    return True


def check_paywall(url: str) -> bool:
    import httpx

    bare = _strip_signature(url)
    try:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            resp = client.get(bare, headers={"Range": "bytes=0-1023"})
    except Exception as exc:  # noqa: BLE001
        _say(MEH, "paywall", f"probe failed: {type(exc).__name__}: {exc}")
        return True
    if resp.status_code in (401, 403):
        _say(OK, "paywall", f"unsigned request refused ({resp.status_code})")
        return True
    _say(
        BAD,
        "paywall",
        f"unsigned request answered {resp.status_code} — the distribution is serving "
        "objects to anyone who knows a path. Set 'Restrict viewer access' to the trusted "
        "key group (deploy/CLOUDFRONT.md §1.3).",
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/cdn_healthcheck.py",
        description="Prove CloudFront delivery is live, caching, and still gated (Bug 373).",
    )
    parser.add_argument("--job", help="job id to probe (default: newest delivered job)")
    parser.add_argument("--name", help="deliverable name (default: full_video, else the first)")
    args = parser.parse_args(argv)

    settings = get_settings()
    store = JobStore(settings.jobs_root)

    job_id, name = args.job, args.name
    duration: float | None = None
    if not job_id:
        picked = _pick_job(store)
        if picked is None:
            print(
                "error: no delivered job with a video deliverable in this store — pass "
                "--job JOB_ID --name full_video.",
                file=sys.stderr,
            )
            return 2
        job_id, name = picked
    if not name:
        name = "full_video"
    # Duration comes from the LOCAL render when it is still on disk, purely so the
    # bitrate line can be printed; its absence is never an error.
    local = store.dir(job_id) / f"{name}.mp4"
    if local.is_file():
        try:
            import subprocess

            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(local)],
                capture_output=True, text=True, check=False, timeout=30,
            ).stdout.strip()
            duration = float(out)
        except Exception:  # noqa: BLE001 - decoration only
            duration = None

    print(f"probing job {job_id}, deliverable {name!r}\n")
    ok = check_config(settings)
    if not ok:
        print("\nVERDICT: CDN delivery is not active on this deployment.")
        return 1
    check_object(settings, job_id, name, duration)
    url = check_determinism(settings, job_id, name)
    if url is None:
        print("\nVERDICT: signing is broken — galleries are falling back to the origin.")
        return 1
    edge_ok = check_edge(url)
    gate_ok = check_paywall(url)
    print(
        "\nVERDICT: "
        + (
            "CDN delivery is live."
            if edge_ok and gate_ok
            else "CDN delivery is reachable but something above needs attention."
        )
    )
    return 0 if (edge_ok and gate_ok) else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
