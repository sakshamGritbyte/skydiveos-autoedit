"""Customer delivery: upload finals to S3, presign download links, email them.

Pipeline stage 7. Until now :func:`api.tasks.deliver_job` only flipped the job's
status and left the actual hand-off to the SkydiveOS web layer; this module makes
the pipeline able to complete the loop itself so a jump can go camera → edit →
customer inbox with no human step (``AUTO_DELIVER=1``).

The flow: collect the job's rendered deliverables (``final.mp4`` for the classic
single-master pipeline, the ``outputs`` map for the selfie/ultimum packages — a
photos *directory* is zipped first), upload each to
``s3://{bucket}/deliveries/{job_id}/``, presign a download URL per file, and send
one email listing them. Links are returned to the caller so they're persisted on
the job (``delivery_links``) and forwarded to SkydiveOS in the status callback.

Follows the project's injectable-dependency style (``Camera``, ``EventEmitter``,
``s3_notify_uploader``): pass ``s3_client`` / ``smtp_factory`` to substitute fakes
in tests; ``boto3`` is imported lazily so importing this module stays cheap.
"""

from __future__ import annotations

import logging
import mimetypes
import shutil
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import Settings
from .jobs import Job, JobStore

logger = logging.getLogger(__name__)

#: S3 key prefix for customer-facing renders: ``deliveries/{job_id}/{filename}``.
#: Distinct from ``raw/`` (ingest masters) so bucket lifecycle rules can differ.
DELIVERY_KEY_PREFIX = "deliveries"

#: Human labels for the email body, keyed by deliverable name.
_LABELS = {
    "final": "Your skydive edit",
    "full_video": "Full video",
    "highlights": "Highlights",
    "freefall": "Freefall",
    "external_freefall": "Freefall — outside camera",
    "chute_libre_selfie": "Freefall — selfie camera",
    "photos": "Photos (zip)",
}


def _label(name: str) -> str:
    return _LABELS.get(name, name.replace("_", " ").capitalize())


def collect_deliverables(job: Job, store: JobStore) -> dict[str, Path]:
    """The files to hand to the customer, keyed by deliverable name.

    Package jobs deliver their ``outputs`` map; the classic single-master pipeline
    delivers ``final.mp4``. A photos entry points at a *directory* — it's zipped
    (idempotently, beside itself) so the customer downloads one file. Paths that
    no longer exist are skipped with a warning rather than failing the whole
    delivery — better the customer gets four of five files than none.
    """
    files: dict[str, Path] = {}
    if job.outputs:
        for name, raw in job.outputs.items():
            path = Path(raw)
            if path.is_dir():
                path = Path(shutil.make_archive(str(path), "zip", root_dir=path))
            if path.is_file():
                files[name] = path
            else:
                logger.warning(
                    "job %s deliverable %s missing at %s — skipping it",
                    job.job_id,
                    name,
                    raw,
                )
    else:
        final = store.final_path(job.job_id)
        if final.is_file():
            files["final"] = final
    return files


def _default_s3_client(settings: Settings) -> Any:
    import boto3  # deferred: only the delivery path needs it

    return boto3.client(
        "s3", endpoint_url=settings.s3_endpoint_url, region_name=settings.s3_region
    )


def upload_and_link(
    files: dict[str, Path],
    *,
    job_id: str,
    settings: Settings,
    s3_client: Any | None = None,
) -> dict[str, str]:
    """Upload each deliverable to S3 and return a presigned URL per name.

    Keys are ``deliveries/{job_id}/{filename}``; URLs expire after
    ``delivery_link_ttl_days`` (≤ 7, the SigV4 maximum). ``ContentType`` is set so
    browsers stream the videos instead of prompting a raw download.
    """
    if not settings.s3_bucket:
        raise RuntimeError(
            "delivery needs S3_BUCKET (or AWS_S3_BUCKET_NAME) to host the customer links"
        )
    client = s3_client if s3_client is not None else _default_s3_client(settings)
    ttl_seconds = int(settings.delivery_link_ttl_days * 86400)
    links: dict[str, str] = {}
    for name, path in files.items():
        key = f"{DELIVERY_KEY_PREFIX}/{job_id}/{path.name}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.upload_file(
            str(path), settings.s3_bucket, key, ExtraArgs={"ContentType": content_type}
        )
        links[name] = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        logger.info("job %s: uploaded %s → s3://%s/%s", job_id, name, settings.s3_bucket, key)
    return links


def _upload_photos_individually(
    client: Any, photos_dir: Path, job_id: str, settings: Settings, ttl: int
) -> list[str]:
    """Upload each still to ``deliveries/{job}/photos/`` and return presigned URLs.

    The gallery's photo grid needs a URL per image (the zip is only for "download
    all"). Ordered by filename so the grid is stable.
    """
    urls: list[str] = []
    for p in sorted(photos_dir.glob("*.jpg")):
        key = f"{DELIVERY_KEY_PREFIX}/{job_id}/photos/{p.name}"
        client.upload_file(str(p), settings.s3_bucket, key, ExtraArgs={"ContentType": "image/jpeg"})
        urls.append(
            client.generate_presigned_url(
                "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=ttl
            )
        )
    return urls


def _upload_gallery_html(
    client: Any, html_str: str, job_id: str, settings: Settings, ttl: int
) -> str:
    """Put the gallery page as an S3 ``text/html`` object and presign it (the customer link)."""
    key = f"{DELIVERY_KEY_PREFIX}/{job_id}/gallery.html"
    client.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=html_str.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
    )
    return client.generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=ttl
    )


def send_gallery_email(
    job: Job,
    gallery_url: str,
    settings: Settings,
    *,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
) -> bool:
    """Email the customer ONE gallery link (all videos + photos on one page).

    Same skip-not-fail contract as :func:`send_delivery_email`: returns False (with a
    warning) when there's no ``customer_email`` / SMTP, so the caller can fall back to
    the SkydiveOS hand-off.
    """
    if not job.customer_email:
        logger.warning("job %s has no customer_email — not emailing", job.job_id)
        return False
    if not settings.smtp_host:
        logger.warning("SMTP_HOST not set — not emailing job %s", job.job_id)
        return False
    sender = settings.delivery_from_email or settings.smtp_user
    if not sender:
        logger.warning("no DELIVERY_FROM_EMAIL / SMTP_USER — not emailing job %s", job.job_id)
        return False

    expiry_days = int(settings.delivery_link_ttl_days)
    lines = [
        f"Hi {job.customer_name},",
        "",
        "Your skydive video is ready! 🪂",
        "",
        "View and download all your videos and photos on one page here:",
        "",
        f"  {gallery_url}",
        "",
        f"The link is valid for {expiry_days} days — save your files soon.",
        "",
        "Blue skies!",
    ]
    msg = EmailMessage()
    msg["Subject"] = f"Your skydive video is ready, {job.customer_name}!"
    msg["From"] = sender
    msg["To"] = job.customer_email
    msg.set_content("\n".join(lines))

    factory = smtp_factory or (
        lambda: smtplib.SMTP(settings.smtp_host or "", settings.smtp_port, timeout=30)
    )
    with factory() as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    logger.info("job %s: gallery email sent to %s", job.job_id, job.customer_email)
    return True


def send_delivery_email(
    job: Job,
    links: dict[str, str],
    settings: Settings,
    *,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
) -> bool:
    """Email the download links to the customer. Returns True iff an email went out.

    Skips (False, with a warning) rather than raises when the job has no
    ``customer_email`` or SMTP isn't configured — the caller decides whether a
    link-only delivery (handed to SkydiveOS) is acceptable.
    """
    if not job.customer_email:
        logger.warning("job %s has no customer_email — not emailing", job.job_id)
        return False
    if not settings.smtp_host:
        logger.warning("SMTP_HOST not set — not emailing job %s", job.job_id)
        return False
    sender = settings.delivery_from_email or settings.smtp_user
    if not sender:
        logger.warning("no DELIVERY_FROM_EMAIL / SMTP_USER — not emailing job %s", job.job_id)
        return False

    expiry_days = int(settings.delivery_link_ttl_days)
    lines = [
        f"Hi {job.customer_name},",
        "",
        "Your skydive video is ready! Download your files here:",
        "",
        *(f"  {_label(name)}: {url}" for name, url in links.items()),
        "",
        f"The links are valid for {expiry_days} days — save your files soon.",
        "",
        "Blue skies!",
    ]
    msg = EmailMessage()
    msg["Subject"] = f"Your skydive video is ready, {job.customer_name}!"
    msg["From"] = sender
    msg["To"] = job.customer_email
    msg.set_content("\n".join(lines))

    factory = smtp_factory or (
        lambda: smtplib.SMTP(settings.smtp_host or "", settings.smtp_port, timeout=30)
    )
    with factory() as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    logger.info("job %s: delivery email sent to %s", job.job_id, job.customer_email)
    return True


def deliver_to_customer(
    job: Job,
    store: JobStore,
    settings: Settings,
    *,
    s3_client: Any | None = None,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
) -> dict[str, str]:
    """Run the full hand-off for an approved job; returns the presigned links.

    Raises when there's nothing to deliver, when S3 isn't configured, or when the
    links reached nobody (no email went out *and* no SkydiveOS callback is
    configured to forward them) — a job must never read ``delivered`` when the
    customer has no way to get the files.
    """
    from . import gallery

    files = collect_deliverables(job, store)  # videos + photos.zip (photos dir zipped)
    if not files:
        raise RuntimeError(f"job {job.job_id} has no rendered deliverables to deliver")
    if not settings.s3_bucket:
        raise RuntimeError(
            "delivery needs S3_BUCKET (or AWS_S3_BUCKET_NAME) to host the customer gallery"
        )
    client = s3_client if s3_client is not None else _default_s3_client(settings)
    ttl = int(settings.delivery_link_ttl_days * 86400)

    # Videos → a presigned URL each (the gallery's inline players).
    video_files = {n: p for n, p in files.items() if n != "photos"}
    video_links = upload_and_link(
        video_files, job_id=job.job_id, settings=settings, s3_client=client
    )

    # Photos → individual URLs for the grid, plus the zip for "download all".
    photo_urls: list[str] = []
    zip_url: str | None = None
    if "photos" in files:
        zip_url = upload_and_link(
            {"photos": files["photos"]}, job_id=job.job_id, settings=settings, s3_client=client
        )["photos"]
        photos_dir = (
            Path(job.outputs["photos"])
            if job.outputs and job.outputs.get("photos")
            else None
        )
        if photos_dir and photos_dir.is_dir():
            photo_urls = _upload_photos_individually(client, photos_dir, job.job_id, settings, ttl)

    # Build the one-page gallery and host it; its link is what the customer gets.
    page = gallery.render_gallery_html(
        brand=settings.delivery_brand_name,
        customer_name=job.customer_name,
        jump_date=job.jump_date,
        location=settings.delivery_location,
        videos=[(n, video_links[n]) for n in video_files],
        photos=photo_urls,
        download_all_url=zip_url,
    )
    gallery_url = _upload_gallery_html(client, page, job.job_id, settings, ttl)

    emailed = send_gallery_email(job, gallery_url, settings, smtp_factory=smtp_factory)
    if not emailed and not settings.skydiveos_api_base:
        raise RuntimeError(
            f"job {job.job_id}: gallery generated but unreachable — no customer_email/SMTP "
            "and no SKYDIVEOS_API_BASE to forward it to"
        )
    # gallery is the customer link; individual links ride along for SkydiveOS.
    links = {"gallery": gallery_url, **video_links}
    if zip_url:
        links["photos"] = zip_url
    return links
