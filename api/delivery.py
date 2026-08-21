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
import time
from collections.abc import Callable, Collection
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import Settings
from .jobs import (
    Entitlement,
    Job,
    JobKind,
    JobStore,
    any_locked,
    entitlement_for,
    locked_deliverables,
)

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


def delivery_s3_key(job_id: str, filename: str) -> str:
    """The S3 key ``deliver_job`` uses (or will use) for one deliverable file.

    The ONE authority on the ``deliveries/{job_id}/{filename}`` convention, so
    consumers (notably ``GET /jobs/{id}/deliverables``, which advertises the key
    to SkydiveOS for server-side S3→S3 copies) can never drift from what
    ``upload_and_link`` actually writes. A photo belongs under the ``photos/``
    sub-prefix — pass ``photos/<name>`` as the filename.

    The key names where the object *lands at delivery*; before ``deliver_job``
    has run it may not exist yet, so consumers must HeadObject before use.
    """
    return f"{DELIVERY_KEY_PREFIX}/{job_id}/{filename}"


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
    presign: bool | Collection[str] = True,
) -> dict[str, str]:
    """Upload each deliverable to S3; return a presigned URL per name.

    Keys are ``deliveries/{job_id}/{filename}``; URLs expire after
    ``delivery_link_ttl_days`` (≤ 7, the SigV4 maximum). ``ContentType`` is set so
    browsers stream the videos instead of prompting a raw download.

    ``presign`` decides which names get a URL — every file uploads either way, because
    the durable copy is what makes ``/unlock`` instant:

    * ``True`` — a URL for every name (the customer owns the whole set).
    * ``False`` — no URLs at all, returning ``{}``. The wholly ``preview_only``
      (Path B) case.
    * a **collection of names** — a URL for those names only. This is the *mixed* job:
      a paid handcam edit alongside a spec external one. Naming the allowed set here,
      rather than filtering afterwards, is deliberate — a presigned URL carries no
      entitlement check (unlike the ``/j/{code}`` route), it is persisted on the job,
      mirrored into the archive manifest and forwarded to SkydiveOS, so a locked
      deliverable must never have one minted in the first place.
    """
    if not settings.s3_bucket:
        raise RuntimeError(
            "delivery needs S3_BUCKET (or AWS_S3_BUCKET_NAME) to host the customer links"
        )
    client = s3_client if s3_client is not None else _default_s3_client(settings)
    ttl_seconds = int(settings.delivery_link_ttl_days * 86400)
    allowed: Collection[str] | None = None if isinstance(presign, bool) else presign
    links: dict[str, str] = {}
    for name, path in files.items():
        key = delivery_s3_key(job_id, path.name)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.upload_file(
            str(path), settings.s3_bucket, key, ExtraArgs={"ContentType": content_type}
        )
        if presign is True or (allowed is not None and name in allowed):
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
        key = delivery_s3_key(job_id, f"photos/{p.name}")
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


def send_gallery_email_once(
    job: Job,
    store: JobStore,
    gallery_url: str,
    settings: Settings,
    *,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
    link_expires: bool = True,
) -> bool:
    """Send the gallery email **at most once per job**, across retries and workers.

    The idempotency guard for delivery. Celery runs ``task_acks_late=True``, so a worker
    killed after the SMTP send but before the ack re-runs ``deliver_job`` from the top —
    and the ``status != approved`` guard cannot catch it, because the status is still
    ``approved`` for the whole run. Two workers draining a re-queued delivery hit the same
    window. Either way the customer got a second "your video is ready" (the failure class
    of the 2026-08-06 four-emails incident, from a different direction).

    Two records, deliberately:

    * :meth:`JobStore.claim_email_send` — an ``O_EXCL`` create, so the *filesystem*
      arbitrates the race that a read-modify-write ``job.json`` field cannot.
    * :attr:`api.jobs.Job.email_sent_at` — the durable, inspectable answer to "has this
      customer been emailed?", written only after a send actually succeeded.

    Returns **"the customer has their link"** — True when an email went out now *or* on an
    earlier run. That is what the caller's reachability check needs: a retry must not
    conclude the gallery reached nobody and fail a job that was, in fact, delivered.
    Returns False only in the pre-existing skip cases (no ``customer_email``, no SMTP),
    and releases the claim then so configuring SMTP and re-queueing still sends.
    """
    if settings.customer_email_sender == "skydiveos":
        # SkydiveOS owns the customer email (branded HTML + the dropzone Cc from
        # MediaConfig, neither reachable from here). It learns the gallery URL
        # from the status callback, which `deliver_job` fires either way — so the
        # customer IS reachable and this must report True, not fail the job.
        # Nothing is stamped: `email_sent_at` means "THIS service emailed them",
        # and flipping the flag back must not look like an email already went.
        logger.info(
            "job %s: customer email delegated to SkydiveOS (CUSTOMER_EMAIL_SENDER=skydiveos)",
            job.job_id,
        )
        return True
    if job.email_sent_at is not None:
        logger.info(
            "job %s was already emailed (at %.0f) — not sending a second time",
            job.job_id, job.email_sent_at,
        )
        return True
    if not store.claim_email_send(job.job_id):
        # Another worker holds the claim: it will either send (and stamp) or release.
        # Reporting "reachable" here is the safe side — the alternative is failing a job
        # whose email is in flight.
        logger.warning(
            "job %s: another delivery run holds the email claim — not sending again",
            job.job_id,
        )
        return True
    try:
        sent = send_gallery_email(
            job, gallery_url, settings,
            smtp_factory=smtp_factory, link_expires=link_expires,
        )
    except Exception:
        store.release_email_claim(job.job_id)  # a transient SMTP failure must be retryable
        raise
    if not sent:
        store.release_email_claim(job.job_id)
        return False
    store.update(job.job_id, email_sent_at=time.time())
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


def _deliver_load_child(
    job: Job,
    store: JobStore,
    settings: Settings,
    *,
    smtp_factory: Callable[[], smtplib.SMTP] | None = None,
) -> dict[str, str]:
    """Deliver a child gallery: email its link, upload nothing.

    A ``load_child`` owns no files — it is a customer-named *view* of its load master's
    renders, which :func:`api.tasks.fan_out_load_job` already uploaded to S3. So this
    skips :func:`collect_deliverables` and every upload: re-uploading the same bytes once
    per customer is exactly the per-customer cost the render-once design exists to avoid.

    A child is ``preview_only`` by construction, so the served ``/j/{code}`` gallery is
    the only safe address for it (a presigned URL carries no entitlement check) — hence
    the hard requirement on ``PUBLIC_BASE_URL`` rather than a fall back to the legacy S3
    page, which would embed presigned clean masters.
    """
    served_url = gallery_link(job, store, settings)
    if served_url is None:
        raise RuntimeError(
            f"load child {job.job_id} needs PUBLIC_BASE_URL set: its gallery is served "
            "live at {PUBLIC_BASE_URL}/j/{code}, and the legacy S3 page would hand out "
            "presigned clean masters of a video this customer has not bought."
        )
    emailed = send_gallery_email_once(
        job,
        store,
        f"{served_url}?s=e#tab-video",
        settings,
        smtp_factory=smtp_factory,
        link_expires=False,
    )
    if not emailed and not settings.skydiveos_api_base:
        raise RuntimeError(
            f"load child {job.job_id}: gallery generated but unreachable — no "
            "customer_email/SMTP and no SKYDIVEOS_API_BASE to forward it to"
        )
    # Only the gallery link: a locked job contributes no per-deliverable URLs, and the
    # master's files are not this job's to hand out.
    return {"gallery": served_url}


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

    if job.job_kind is JobKind.load_child:
        return _deliver_load_child(job, store, settings, smtp_factory=smtp_factory)

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
    # deliverable mints none: the masters and the photo zip still upload (durable, and
    # what /unlock serves instantly), but the customer's only address for a locked file
    # is the lock-aware `/j/{code}` route, which picks preview-vs-master per request.
    #
    # Asked per deliverable, so a MIXED job (paid handcam + spec external) links the
    # edit the customer bought and withholds the one they haven't — the job-level
    # question would have to choose between leaking the spec edit and withholding the
    # paid one.
    locked = any_locked(job)
    if locked and served_url is None:
        raise RuntimeError(
            f"job {job.job_id} has locked deliverable(s) "
            f"({', '.join(sorted(locked_deliverables(job)))}) but PUBLIC_BASE_URL is not "
            "set: the only safe customer link is the served /j/{code} gallery (the legacy "
            "S3 gallery hands out presigned clean masters, which would bypass the "
            "paywall). Set PUBLIC_BASE_URL and re-queue delivery."
        )

    # Videos → durable copy in S3, presigned only for the ones the customer owns.
    video_files = {n: p for n, p in files.items() if n != "photos"}
    unlocked_videos = {
        n for n in video_files if entitlement_for(job, n) is Entitlement.edited_download
    }
    video_links = upload_and_link(
        video_files,
        job_id=job.job_id,
        settings=settings,
        s3_client=client,
        presign=unlocked_videos,
    )

    # Photos zip → durable copy + "download all" (locked: no link; the gallery
    # shows watermarked previews instead of the stills). The photo set is produced from the
    # PAID ref on a mixed job, so it inherits the job's own entitlement unless an
    # explicit access entry says otherwise.
    zip_url: str | None = None
    if "photos" in files:
        photos_owned = entitlement_for(job, "photos") is Entitlement.edited_download
        zip_url = upload_and_link(
            {"photos": files["photos"]},
            job_id=job.job_id,
            settings=settings,
            s3_client=client,
            presign=photos_owned,
        ).get("photos")

    if served_url is not None:
        # Served gallery: the page and its media stream live from this API, so no
        # gallery.html / per-photo uploads. The email carries the source-tagged link.
        gallery_url = served_url
        emailed = send_gallery_email_once(
            job,
            store,
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
        emailed = send_gallery_email_once(
            job, store, gallery_url, settings, smtp_factory=smtp_factory
        )

    if not emailed and not settings.skydiveos_api_base:
        raise RuntimeError(
            f"job {job.job_id}: gallery generated but unreachable — no customer_email/SMTP "
            "and no SKYDIVEOS_API_BASE to forward it to"
        )
    # gallery is the customer link; individual links ride along for SkydiveOS.
    # Only UNLOCKED deliverables appear here: these links are persisted on the job,
    # mirrored into the archive manifest and forwarded to SkydiveOS, so one
    # clean-master URL for a locked deliverable would leak the paywalled edit through
    # any of those. A wholly locked job therefore contributes none, and a mixed one
    # contributes exactly the edits the customer bought.
    links = {"gallery": gallery_url, **video_links}
    if zip_url:
        links["photos"] = zip_url
    return links
