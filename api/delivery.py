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
from .jobs import Entitlement, Job, JobStore

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
    presign: bool = True,
) -> dict[str, str]:
    """Upload each deliverable to S3; return a presigned URL per name.

    Keys are ``deliveries/{job_id}/{filename}``; URLs expire after
    ``delivery_link_ttl_days`` (≤ 7, the SigV4 maximum). ``ContentType`` is set so
    browsers stream the videos instead of prompting a raw download.

    ``presign=False`` uploads without minting any URL and returns ``{}`` — the
    durable copy still lands in S3, but nothing hands out a link to it. That is the
    ``preview_only`` (Path B) case: the clean masters must exist so ``/unlock`` is
    instant, and a presigned URL to one of them **is** the paywall bypass (a URL,
    unlike the gallery route, carries no entitlement check).
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
        if presign:
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


def gallery_link(job: Job, store: JobStore, settings: Settings) -> str | None:
    """The served customer gallery URL (``{PUBLIC_BASE_URL}/j/{code}``), or ``None``.

    ``None`` when ``PUBLIC_BASE_URL`` isn't set — delivery then falls back to the
    legacy S3-hosted gallery. The short code is minted (idempotently) on first use
    so the link is stable across replays. Bare — no ``?s=`` source tag; each channel
    (the email here, SkydiveOS's SMS) appends its own.
    """
    if not settings.public_base_url:
        return None
    token = store.ensure_gallery_token(job.job_id)
    return f"{settings.public_base_url}/j/{token}"


def send_gallery_email(
    job: Job,
    gallery_url: str,
    settings: Settings,
    *,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
    link_expires: bool = True,
) -> bool:
    """Email the customer ONE gallery link (all videos + photos on one page).

    Same skip-not-fail contract as :func:`send_delivery_email`: returns False (with a
    warning) when there's no ``customer_email`` / SMTP, so the caller can fall back to
    the SkydiveOS hand-off. ``link_expires=False`` drops the "valid for N days" line —
    the served ``/j/{code}`` link never expires.
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
        *(
            [f"The link is valid for {expiry_days} days — save your files soon.", ""]
            if link_expires
            else []
        ),
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
    """Run the full hand-off for an approved job; returns the customer links.

    The clean deliverables always upload to S3 (the durable copy — and for a
    ``preview_only`` job, the masters the unlock will serve instantly). The customer
    link depends on ``PUBLIC_BASE_URL``:

    * set → the served ``/j/{code}`` gallery: short, never expires, and re-renders
      locked/unlocked per request. No gallery.html or per-photo uploads needed.
    * unset → the legacy S3-hosted gallery.html with presigned URLs baked in.

    **A ``preview_only`` job may only be delivered as the served gallery.** The
    legacy page embeds presigned URLs to the *clean masters*, and a presigned URL
    answers to whoever holds it — there is no entitlement check on a URL — so that
    page (and any per-deliverable link in the return value, which is persisted on the
    job, mirrored into the archive and forwarded to SkydiveOS) would hand over the
    very file the customer hasn't bought. Locked jobs therefore mint **no** presigned
    URLs, and delivery raises with an actionable error when there's no served gallery
    to point at, instead of falling back to the leaking path.

    Raises when there's nothing to deliver, when S3 isn't configured, when a locked
    job has no served gallery, or when the links reached nobody (no email went out
    *and* no SkydiveOS callback is configured to forward them) — a job must never
    read ``delivered`` when the customer has no way to get the files.
    """
    from . import gallery
    from .upsell import link_tiles

    files = collect_deliverables(job, store)  # videos + photos.zip (photos dir zipped)
    if not files:
        raise RuntimeError(f"job {job.job_id} has no rendered deliverables to deliver")
    if not settings.s3_bucket:
        raise RuntimeError(
            "delivery needs S3_BUCKET (or AWS_S3_BUCKET_NAME) to host the customer gallery"
        )
    client = s3_client if s3_client is not None else _default_s3_client(settings)
    ttl = int(settings.delivery_link_ttl_days * 86400)
    served_url = gallery_link(job, store, settings)

    # ── Path B: the paywall decides what may be LINKED, not just what's shown ──
    # A presigned URL bypasses every entitlement check by construction, so a locked
    # job mints none: the masters and the photo zip still upload (durable, and what
    # /unlock serves instantly), but the customer's only address for this jump is
    # the lock-aware `/j/{code}` route, which picks preview-vs-master per request.
    locked = job.entitlement is Entitlement.preview_only
    if locked and served_url is None:
        raise RuntimeError(
            f"job {job.job_id} is preview_only but PUBLIC_BASE_URL is not set: the only "
            "safe customer link is the served /j/{code} gallery (the legacy S3 gallery "
            "hands out presigned clean masters, which would bypass the paywall). Set "
            "PUBLIC_BASE_URL and re-queue delivery."
        )

    # Videos → durable copy in S3, presigned only when the customer owns the edit.
    video_files = {n: p for n, p in files.items() if n != "photos"}
    video_links = upload_and_link(
        video_files,
        job_id=job.job_id,
        settings=settings,
        s3_client=client,
        presign=not locked,
    )

    # Photos zip → durable copy + "download all" (locked: no link; the gallery
    # shows a count teaser instead of the stills).
    zip_url: str | None = None
    if "photos" in files:
        zip_url = upload_and_link(
            {"photos": files["photos"]},
            job_id=job.job_id,
            settings=settings,
            s3_client=client,
            presign=not locked,
        ).get("photos")

    if served_url is not None:
        # Served gallery: the page and its media stream live from this API, so no
        # gallery.html / per-photo uploads. The email carries the source-tagged link.
        gallery_url = served_url
        emailed = send_gallery_email(
            job,
            f"{served_url}?s=e#tab-video",
            settings,
            smtp_factory=smtp_factory,
            link_expires=False,
        )
    else:
        # Legacy: photos → individual URLs for the grid, then host the static page.
        photo_urls: list[str] = []
        if "photos" in files:
            photos_dir = (
                Path(job.outputs["photos"])
                if job.outputs and job.outputs.get("photos")
                else None
            )
            if photos_dir and photos_dir.is_dir():
                photo_urls = _upload_photos_individually(
                    client, photos_dir, job.job_id, settings, ttl
                )
        page = gallery.render_gallery_html(
            brand=settings.delivery_brand_name,
            customer_name=job.customer_name,
            jump_date=job.jump_date,
            location=settings.delivery_location,
            videos=[(n, video_links[n]) for n in video_files],
            photos=photo_urls,
            download_all_url=zip_url,
            instructor_name=job.instructor_name,
            product_label=job.package.display_label,
            # The upsell row is entitlement-independent, and so is the host: the
            # fallback S3 page carries the same offers as the served gallery.
            upsells=link_tiles(
                settings.upsell_tiles,
                template=settings.checkout_url_template,
                job_id=job.job_id,
                booking_id=job.booking_id,
            ),
        )
        gallery_url = _upload_gallery_html(client, page, job.job_id, settings, ttl)
        emailed = send_gallery_email(job, gallery_url, settings, smtp_factory=smtp_factory)

    if not emailed and not settings.skydiveos_api_base:
        raise RuntimeError(
            f"job {job.job_id}: gallery generated but unreachable — no customer_email/SMTP "
            "and no SKYDIVEOS_API_BASE to forward it to"
        )
    # gallery is the customer link; individual links ride along for SkydiveOS.
    # A locked job contributes NO per-deliverable links: these are persisted on the
    # job, mirrored into the archive manifest and forwarded to SkydiveOS, so one
    # clean-master URL here would leak the paywalled edit through any of those.
    links = {"gallery": gallery_url, **video_links}
    if zip_url:
        links["photos"] = zip_url
    return links
