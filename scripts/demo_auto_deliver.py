"""No-camera end-to-end check of the fully-automatic delivery flow.

Proves the whole chain a real jump takes — *without* a GoPro and *without* a real
mail server — by feeding a sample MP4 through the exact same task a camera pull
enqueues (:func:`api.tasks.process_job`), with ``AUTO_DELIVER=1`` so the render is
auto-approved and delivered the moment it finishes:

    sample.mp4 -> segment -> score -> house-cut EDL -> render final.mp4
               -> AUTO-APPROVE (skip the review gate)
               -> upload to s3://$S3_BUCKET/deliveries/{job_id}/
               -> presign download links -> email the customer -> status: delivered

The renders really are uploaded to S3 (needs the same AWS creds + ``S3_BUCKET`` the
rest of the pipeline uses); by default they're deleted again at the end so the demo
leaves nothing behind (``--keep`` to retain them).

Email: with no ``--smtp-host`` given, the script starts a throwaway in-process SMTP
sink (needs ``aiosmtpd``) and prints the message it catches — so you see the exact
mail a customer would get without configuring a provider. Pass ``--smtp-host`` (etc.)
to send through a real server instead, or ``--no-email`` to only generate + print
the S3 links.

Usage:
    python scripts/demo_auto_deliver.py                       # sink + sample footage
    python scripts/demo_auto_deliver.py --email you@you.com   # to your real inbox,
        --smtp-host smtp.gmail.com --smtp-user ... --smtp-password ...
    python scripts/demo_auto_deliver.py --no-email            # just print the links
    python scripts/demo_auto_deliver.py --source path/to/jump.mp4 --keep
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Run as a file (per CLAUDE.md), not just as a module: put the repo root on
# sys.path so the pipeline packages import. Also insert it *before* this script's
# own dir so ``import api`` finds the package, not shadowed by scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if sys.path and sys.path[0] != str(_REPO_ROOT):
    sys.path.insert(0, str(_REPO_ROOT))

#: Default sample footage shipped for no-hardware runs (same file the static
#: camera scanner stages). Overridable with --source.
_DEFAULT_SOURCE = _REPO_ROOT / "templates" / "GL010652.mp4"
if not _DEFAULT_SOURCE.exists():  # fall back to the discovery sample if present
    _alt = _REPO_ROOT / "sample-data" / "discovery_sample.mp4"
    if _alt.exists():
        _DEFAULT_SOURCE = _alt


class _MailSink:
    """A tiny in-process SMTP server that captures one message and prints it.

    Saves configuring a real provider just to see the delivery mail: start it,
    point ``SMTP_HOST``/``SMTP_PORT`` at it, and the caught ``EmailMessage`` is
    available on :attr:`caught`. Needs ``aiosmtpd`` (``pip install aiosmtpd``).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 1025) -> None:
        self.host = host
        self.port = port
        self.caught: list[object] = []
        self._controller = None

    def __enter__(self) -> _MailSink:
        from email import message_from_bytes
        from email.policy import default as _default_policy

        from aiosmtpd.controller import Controller

        sink = self

        class _Handler:
            async def handle_DATA(self, server, session, envelope):  # noqa: ANN001
                # Parse with the modern policy so get_content() is available.
                sink.caught.append(
                    message_from_bytes(envelope.content, policy=_default_policy)
                )
                return "250 OK"

        self._controller = Controller(_Handler(), hostname=self.host, port=self.port)
        self._controller.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._controller is not None:
            self._controller.stop()


def _cleanup_s3(job_id: str, settings) -> None:  # noqa: ANN001
    """Delete the demo's uploaded objects so the bucket is left as it was."""
    if not settings.s3_bucket:
        return
    import boto3

    client = boto3.client(
        "s3", endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region
    )
    from api.delivery import DELIVERY_KEY_PREFIX

    # The key layout is deliveries/{job_id}/{filename}; list + delete the whole prefix.
    prefix = f"{DELIVERY_KEY_PREFIX}/{job_id}/"
    resp = client.list_objects_v2(Bucket=settings.s3_bucket, Prefix=prefix)
    for obj in resp.get("Contents", []):
        client.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
        print(f"  cleaned up s3://{settings.s3_bucket}/{obj['Key']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(_DEFAULT_SOURCE), help="sample MP4 to edit")
    parser.add_argument("--email", default="customer@example.com", help="customer email")
    parser.add_argument("--customer", default="Test Customer", help="customer name")
    parser.add_argument("--smtp-host", default=None, help="real SMTP host (else a local sink)")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-user", default=None)
    parser.add_argument("--smtp-password", default=None)
    parser.add_argument("--from-email", default="videos@dropzone.local")
    parser.add_argument("--no-email", action="store_true", help="only generate S3 links")
    parser.add_argument("--keep", action="store_true", help="don't delete the S3 uploads")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        parser.error(f"source footage not found: {source}")

    jobs_root = Path(tempfile.mkdtemp(prefix="autodeliver-demo-"))

    # Configure the run entirely through the environment the pipeline already reads,
    # so this exercises the real settings path (not a hand-built Settings).
    os.environ["JOBS_ROOT"] = str(jobs_root)
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"  # run tasks inline, no worker/broker
    os.environ["AUTO_DELIVER"] = "1"  # the flag under test: skip the review gate
    os.environ["SKYDIVEOS_API_BASE"] = ""  # no web layer in this demo
    os.environ.setdefault("DELIVERY_FROM_EMAIL", args.from_email)

    sink: _MailSink | None = None
    if args.no_email:
        # Leave SMTP_HOST unset → delivery generates links, skips the email, and
        # (with no SKYDIVEOS_API_BASE) would normally error. Point it at SkydiveOS
        # so link-only delivery is allowed.
        os.environ["SKYDIVEOS_API_BASE"] = "http://demo.local"
        os.environ.pop("SMTP_HOST", None)
    elif args.smtp_host:
        os.environ["SMTP_HOST"] = args.smtp_host
        os.environ["SMTP_PORT"] = str(args.smtp_port)
        if args.smtp_user:
            os.environ["SMTP_USER"] = args.smtp_user
        if args.smtp_password:
            os.environ["SMTP_PASSWORD"] = args.smtp_password
    else:
        try:
            import aiosmtpd  # noqa: F401
        except ImportError:
            parser.error(
                "no --smtp-host given and aiosmtpd isn't installed for the local mail "
                "sink. Install it (pip install aiosmtpd) or pass --no-email."
            )
        sink = _MailSink()
        os.environ["SMTP_HOST"] = sink.host
        os.environ["SMTP_PORT"] = str(sink.port)
        os.environ["SMTP_STARTTLS"] = "0"  # a plain local sink, no TLS

    # Import after the env is set so get_settings() (lru_cached) snapshots our config.
    from api.config import get_settings
    from api.jobs import Job, JobStore
    from api.tasks import process_job

    get_settings.cache_clear()
    settings = get_settings()
    if not settings.s3_bucket:
        print(
            "ERROR: S3_BUCKET (or AWS_S3_BUCKET_NAME) isn't set — delivery needs a "
            "bucket to host the customer links. Set it in .env and retry.",
            file=sys.stderr,
        )
        return 2

    store = JobStore(settings.jobs_root)
    job = store.create(
        Job(
            job_id="demo-autodeliver",
            customer_name=args.customer,
            customer_email=None if args.no_email else args.email,
            jump_date="2026-07-27",
            source_path=str(source),
        )
    )
    print(f"→ job {job.job_id} created (status={job.status.value}); editing {source.name} ...")

    ctx = sink if sink is not None else _nullcontext()
    with ctx:
        process_job(job.job_id)  # the exact task a camera pull enqueues
        final = store.load(job.job_id)

        print(f"\n✓ FINAL STATUS: {final.status.value}")
        print("\nDownload links (what the customer receives):")
        for name, url in (final.delivery_links or {}).items():
            print(f"  • {name}: {url[:100]}...")

        if sink is not None:
            print("\n── Caught delivery email ─────────────────────────────")
            for msg in sink.caught:
                print(f"  Subject: {msg['Subject']}")
                print(f"  From:    {msg['From']}")
                print(f"  To:      {msg['To']}")
                print("  ---")
                for line in msg.get_content().splitlines():
                    print(f"  {line}")
            if not sink.caught:
                print("  (no email captured — check the customer_email / SMTP settings)")

    if not args.keep:
        print("\nCleaning up demo uploads ...")
        _cleanup_s3(job.job_id, settings)

    print(f"\nDone. Job artifacts under {jobs_root}")
    return 0


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
