"""Customer gallery page: one branded link showing all the edit's videos + photos.

Instead of emailing the customer a handful of raw S3 links, delivery builds a single
self-contained HTML **gallery page** (this module renders it) — the design doc's
"Delivery Landing Page" (Frame 03): the deliverables playing inline, one primary
action, and the "Add to your day" upsell row. Two hosts use it:

* the legacy S3 path (:mod:`api.delivery` uploads the HTML and emails a presigned
  link) — the defaults reproduce that page's *shape* (no tabs, no badges, no paywall);
* the live ``GET /j/{code}`` route (:mod:`api.app`), which re-renders per request —
  ``tabbed=True`` for the Video/Photos tabs, and ``locked=True`` for a
  ``preview_only`` job (design doc Path B): watermarked players, a
  "Unlock full video" CTA, and a watermarked photo-preview grid with its own
  "unlock your photos" offer.

Two rules from the design notes shape the layout:

* **The two entitlement states share one layout.** Only the player treatment (quality
  badge, ``nodownload``) and the primary action (Download vs Unlock) change, so the
  paid path never feels like a different product.
* **The upsell row is entitlement-independent** — it is the operator's second revenue
  line whether or not the video was pre-purchased, so it renders in both states.

:func:`render_gallery_html` is **pure** (strings in, one HTML string out, no I/O) so
it stays trivially testable; hosting lives in :mod:`api.delivery` / :mod:`api.app`.
"""

from __future__ import annotations

import base64
import html
import mimetypes
from collections.abc import Mapping, Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path

from .upsell import UpsellTile

#: Nice per-deliverable video labels + subtitle (Shred-style "Vidéo Tandem 001").
_VIDEO_META: dict[str, tuple[str, str]] = {
    "full_video": ("Full Video", "The complete edit"),
    "highlights": ("Highlights", "The best moments"),
    "freefall": ("Freefall", "The freefall"),
    "external_freefall": ("Freefall — Outside Camera", "Cameraman angle"),
    "chute_libre_selfie": ("Freefall — Selfie Camera", "Instructor angle"),
    "final": ("Your Skydive Edit", "The complete edit"),
    # A MIXED jump's secondary product, namespaced `<role>_<name>` by
    # `api.jobs.deliverable_name`. Named the same way round as the Ultimate product's
    # per-camera cuts above ("Freefall — Outside Camera") so the customer reads a
    # product, not a filename: without these the fallback yields "External Full Video".
    "external_full_video": ("Full Video — Outside Camera", "Cameraman angle"),
    "external_highlights": ("Highlights — Outside Camera", "Cameraman angle"),
    "instructor_full_video": ("Full Video — Selfie Camera", "Instructor angle"),
    "instructor_highlights": ("Highlights — Selfie Camera", "Instructor angle"),
    "instructor_freefall": ("Freefall — Selfie Camera", "Instructor angle"),
}

#: Parachute Montréal redesign palette (2026-08 mockup): near-black base, the brand
#: red as the ONE accent in both entitlement states — the design keeps the paid and
#: locked page visually identical (Frame 03's "one layout" rule taken further), so the
#: lock now reads from the badge/CTA copy, not from a page-wide colour swap. Amber
#: survives only as the ``720P PREVIEW`` badge text, the at-a-glance lock signal on a
#: mixed jump where clean and watermarked cards sit side by side.
_BG = "#0a0a0a"
_SURFACE = "#141414"
_SURFACE_2 = "#1c1c1c"
_RED = "#d50000"
_RED_DIM = "#9e0000"
_LOCKED_BADGE = "#e2a13f"

#: The platform mark in the footer ("Powered by UltimateDZM · <dropzone brand>").
_PLATFORM_MARK = "UltimateDZM"

#: Header tagline (the redesign's top-right line).
_TAGLINE = "Your souvenir is ready"


@lru_cache(maxsize=4)
def brand_logo_data_uri(path_str: str | None) -> str | None:
    """The header logo as a ``data:`` URI, or ``None`` for the text-brand fallback.

    The gallery must stay a single self-contained document (it is uploaded to S3
    verbatim on the legacy path), so the logo is inlined rather than linked. Cached —
    the file is read once per process; swap the logo, restart the API. Never raises:
    a missing/unreadable file simply renders the brand name as text, exactly the
    pre-logo page. Kept OUT of :func:`render_gallery_html` so that stays pure.
    """
    if not path_str:
        return None
    p = Path(path_str)
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

#: How the locked page notices it has been paid for (Frame 03's "re-renders in
#: place"). Slow enough to be free at dropzone volume, bounded so a page left open
#: overnight stops asking: 6 s x 200 = 20 minutes, then the customer reloads.
_FLIP_POLL_MS = 6000
_FLIP_POLL_LIMIT = 200


def _video_label(name: str) -> tuple[str, str]:
    return _VIDEO_META.get(name, (name.replace("_", " ").title(), ""))


def _display_date(value: str | None) -> str:
    """``2026-08-14`` → ``14 AUG 2026`` (the design's hero meta). Passes junk through.

    Tolerates a full ISO datetime; anything unparseable is shown as given rather than
    dropped — a customer would rather see an odd date than none.
    """
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).strftime("%d %b %Y").upper()
    except ValueError:
        return value


def render_gallery_html(
    *,
    brand: str,
    customer_name: str,
    jump_date: str | None,
    location: str | None,
    videos: list[tuple[str, str]],
    photos: list[str],
    download_all_url: str | None = None,
    locked: bool = False,
    unlock_url: str | None = None,
    price_display: str = "$39",
    photo_count_teaser: int = 0,
    tabbed: bool = False,
    show_downloads: bool = False,
    instructor_name: str | None = None,
    product_label: str | None = None,
    primary_download_url: str | None = None,
    primary_download_note: str | None = None,
    upsells: Sequence[UpsellTile] = (),
    poll_token: str | None = None,
    photos_unlocked: bool | None = None,
    photos_unlock_url: str | None = None,
    photos_unlock_price: str | None = None,
    raw_videos: list[tuple[str, str]] | None = None,
    load_videos: list[tuple[str, str]] | None = None,
    purchased_addons: Sequence[str] = (),
    locked_videos: Sequence[str] = (),
    group_unlocks: Sequence[tuple[str, str | None]] = (),
    posters: Mapping[str, str] | None = None,
    logo_data_uri: str | None = None,
) -> str:
    """Render the customer gallery page as one self-contained HTML string.

    ``videos`` is a list of ``(deliverable_name, url)``; ``photos`` a list of image
    URLs. All URLs are embedded as-is. No external assets — the page is fully inline
    so it renders straight from an S3 object or a FastAPI response.

    The defaults reproduce the legacy S3 gallery's shape. The served route
    additionally uses:

    * ``tabbed`` — Video/Photos tabs (``#tab-video`` / ``#tab-photos`` fragments).
    * ``locked`` — Path B: ``720P PREVIEW`` badges, download-suppressed players, the
      unlock CTA (an anchor to ``unlock_url`` when set, plain text otherwise), and a
      ``photo_count_teaser`` line in place of the photo grid.
    * ``show_downloads`` — the served *unlocked* state: per-video download links and
      the ``1080P · FULL QUALITY`` badge.
    * ``instructor_name`` / ``product_label`` — the hero's meta line
      (``14 AUG 2026 · TANDEM · INSTRUCTOR MARC TREMBLAY``).
    * ``primary_download_url`` / ``primary_download_note`` — the unlocked page's
      primary action (the green "Download video" button and its "1080p MP4 · 214 MB ·
      yours to keep" line). Ignored while ``locked``, whose primary action is the
      unlock CTA.
    * ``upsells`` — the "Add to your day" tiles, rendered in *both* states.
    * ``poll_token`` — the gallery short code. When given, the page polls
      ``/j/{code}/state`` and reloads itself the moment the paywall lifts **or an
      add-on purchase lands**, so paying in another tab flips this page without a
      manual refresh. Omit it (the legacy S3 page) and no script is emitted.
    * ``photos_unlocked`` — decouples the photo grid from the paywall: a locked job
      that bought the ``photos`` add-on shows the grid while the videos stay
      watermarked. ``None`` (legacy callers) keeps the old rule: photos follow the lock.
      When ``photos`` URLs are passed while NOT unlocked, the grid renders anyway —
      the host serves watermarked preview bytes at those URLs (BUG 350) — followed by
      its own unlock offer (``photos_unlock_url`` / ``photos_unlock_price``, text when
      no checkout URL exists: never a dead link). Only a locked page with NO photo
      URLs falls back to the old ``photo_count_teaser`` line.
    * ``raw_videos`` — the purchased Raw Footage section: ``(label, url)`` per camera
      master, rendered under the Video tab with download links. Empty/None → no section.
    * ``posters`` — ``{deliverable or label: poster image URL}``. A card whose video has
      one opens on a real frame of that edit instead of the browser's generic
      placeholder tile (:mod:`api.thumbnail`); a card with no entry is rendered exactly
      as before, which is the whole fallback story. Keyed by deliverable name for the
      main grid and by *label* for the load/raw sections, since those cards have no
      deliverable name of their own on this page.
    * ``logo_data_uri`` — the dropzone logo for the header, already inlined as a
      ``data:`` URI by :func:`brand_logo_data_uri` (the I/O is the caller's, so this
      function stays pure). ``None`` → the brand name in letter-spaced caps, which is
      the header this page had before the logo existed.
    * ``load_videos`` — the purchased spec-flight load video: ``(label, url)``, rendered
      under the Video tab for a customer who already had a gallery and bought the load
      video as an add-on. Empty/None → no section. (A no-media customer's *child* gallery
      shows the load video as its main ``videos`` instead — it's the only video they have.)
    """
    e = html.escape
    brand_e = e(brand)
    title = e(customer_name)
    if photos_unlocked is None:
        photos_unlocked = not locked
    raw_videos = raw_videos or []
    load_videos = load_videos or []
    posters = posters or {}

    # The header shows the dropzone's logo when the host inlined one, and the brand set
    # in letter-spaced caps otherwise — the pre-logo header, unchanged, so a deployment
    # with no logo configured still gets a finished-looking page. The image carries the
    # brand as its alt text for the same reason.
    brand_mark = (
        f'<div class="logo"><img src="{e(logo_data_uri)}" alt="{brand_e}"></div>'
        if logo_data_uri
        else f'<div class="logo"><span class="brand">{brand_e}</span></div>'
    )

    def poster_attr(key: str) -> str:
        """``poster="…"`` for a card, or nothing at all.

        Emitting an empty ``poster=""`` would be worse than omitting it — some
        browsers treat it as a failed image and paint a broken tile — so a card with
        no still keeps the exact markup it had before posters existed.
        """
        url = posters.get(key)
        return f' poster="{e(url)}"' if url else ""

    # Hero meta: date · product · instructor · location, skipping whatever is unknown.
    # The date is set in <strong> — the redesign's meta line leads with a bold date.
    meta_bits = [
        b
        for b in (
            f"<strong>{e(_display_date(jump_date))}</strong>" if jump_date else None,
            e(product_label) if product_label else None,
            f"Instructor {e(instructor_name)}" if instructor_name else None,
            e(location) if location else None,
        )
        if b
    ]
    subtitle = "  ·  ".join(meta_bits)

    # Lock treatment is per CARD, not per page. On a mixed jump — the handcam edit bought,
    # a camera-flyer edit filmed on spec — one page carries both states at once, and the
    # customer must be able to tell at a glance which video is theirs and which is an
    # offer. ``locked_videos`` names the locked ones; a wholly locked page passes them all
    # (or just sets ``locked``), which is the single-product behaviour unchanged.
    locked_set = set(locked_videos or ())
    video_cards = []
    for name, url in videos:
        label, sub = _video_label(name)
        card_locked = locked or name in locked_set
        if card_locked:
            badge = '<div class="pbadge">720P PREVIEW</div>'
        elif show_downloads:
            badge = '<div class="pbadge ok">1080P · FULL QUALITY</div>'
        else:
            badge = ""
        guard = ' controlsList="nodownload"' if card_locked else ""
        dl = (
            f'<a class="vdl" href="{e(url)}" download>Download</a>'
            if show_downloads and not card_locked else ""
        )
        video_cards.append(f"""
        <div class="vcard">{badge}
          <video controls preload="metadata" playsinline{guard}{poster_attr(name)} src="{e(url)}"></video>
          <div class="vlabel"><div class="titles">{e(label)}<span>{e(sub)}</span></div>{dl}</div>
        </div>""")

    # One call to action PER CAMERA whose edit is still locked. Two angles are priced and
    # sold separately, so a jump where nothing was bought shows two offers and the customer
    # can take either — one button that bought both would be the wrong product, and on such
    # a job the plain ``unlock`` CTA buys nothing at all (every deliverable carries an
    # explicit per-deliverable entry, which the job-level default cannot move). Each is
    # rendered as text when it has no URL, exactly like the unlock CTA — the page must
    # never hand out a dead link.
    group_unlock_html = "".join(
        '<div class="cta">'
        + (
            f'<a class="ctabtn" rel="noreferrer" href="{e(url)}">{e(label)}</a>'
            if url
            else f'<span class="ctabtn">{e(label)} · ask at the desk</span>'
        )
        + '<div class="ctasub">Watermark removed. Instant download.</div></div>'
        for label, url in group_unlocks
    )

    photo_tiles = "".join(
        f'<a class="ptile" href="{e(u)}" target="_blank" rel="noopener noreferrer">'
        f'<img loading="lazy" src="{e(u)}" alt="photo"></a>'
        for u in photos
    )

    dl_btn = (
        f'<a class="btn" href="{e(download_all_url)}" download>Download all photos (.zip)</a>'
        if download_all_url and photos_unlocked else ""
    )
    n_photos = len(photos) if photos else photo_count_teaser
    if photos_unlocked:
        photo_body = f'<div class="pgrid">{photo_tiles}</div>'
    elif photos:
        # Locked, with previewable stills: the watermarked grid (the host serves
        # preview bytes at these URLs) and the photo set's OWN unlock offer — a photos
        # purchase is an add-on, separate from the video paywall. Same dead-link rule
        # as every CTA: text when there is no checkout URL.
        line = "Unlock your photos" + (
            f" — {e(photos_unlock_price)}" if photos_unlock_price else ""
        )
        action = (
            f'<a class="ctabtn" rel="noreferrer" href="{e(photos_unlock_url)}">🔒 {line}</a>'
            if photos_unlock_url
            else f'<span class="ctabtn">{line} · ask at the desk</span>'
        )
        photo_body = (
            f'<div class="pgrid">{photo_tiles}</div>'
            f'<div class="cta">{action}'
            '<div class="ctasub">Full resolution, watermark-free. Instant download.</div></div>'
        )
    else:
        photo_body = f'<p class="teaser">{n_photos} photos included — unlock to see them all.</p>'
    # The section appears when this jump HAS stills — locked or not, so the two states
    # keep one layout (Frame 03). A package that shot none (video_only) gets no Photos
    # tab in either state: a locked page must not advertise photos that don't exist.
    photos_section = (
        f"""
      <section class="photos" id="tab-photos">
        <div class="shead"><h2>Photos <span>({n_photos})</span></h2>{dl_btn}</div>
        {photo_body}
      </section>"""
        if n_photos else ""
    )

    # The primary action. Locked → the paywall CTA (an anchor when SkydiveOS's
    # checkout URL is known, plain text otherwise: the page must never dead-link the
    # customer). Unlocked → the download button, when the host gave us a URL for it.
    # Suppressed when there are per-camera offers: on a job with media refs every locked
    # deliverable carries an explicit entry, and this CTA's ``unlock`` item moves only the
    # job's DEFAULT — it would take the payment and open nothing.
    cta_bar = ""
    if locked and not group_unlocks:
        cta_line = f"Unlock full video — {e(price_display)}"
        cta_action = (
            f'<a class="ctabtn" rel="noreferrer" href="{e(unlock_url)}">🔒 {cta_line}</a>'
            if unlock_url
            else f'<span class="ctabtn">{cta_line} · ask at the desk</span>'
        )
        cta_bar = (
            f'<div class="cta">{cta_action}'
            '<div class="ctasub">Watermark removed. Instant download.</div></div>'
        )
    elif primary_download_url:
        note = (
            f'<div class="ctasub">{e(primary_download_note)}</div>'
            if primary_download_note else ""
        )
        cta_bar = (
            '<div class="cta">'
            f'<a class="ctabtn dl" href="{e(primary_download_url)}" download>⬇ Download video</a>'
            f"{note}</div>"
        )

    # "Add to your day" — the same row in both entitlement states (design notes).
    # The redesign renders each tile as a wide card: label/title/blurb on the left, a red
    # "Add $15" button on the right. The DOM order stays title → blurb → price (a
    # two-column grid places the price, and CSS supplies the word "Add") so the markup
    # keeps its shape and the price text stays exactly the catalogue's string.
    if upsells:
        tiles = []
        for t in upsells:
            body = (
                f'<div class="utitle">{e(t.title)}</div>'
                f'<div class="ublurb">{e(t.blurb)}</div>'
                f'<div class="uprice">{e(t.price)}</div>'
            )
            tiles.append(
                f'<a class="utile" rel="noreferrer" href="{e(t.url)}">{body}</a>'
                if t.url else f'<div class="utile">{body}</div>'
            )
        upsell_section = (
            '<section class="upsell">'
            '<div class="ulabel">Add to your day</div>'
            f'<div class="urow">{"".join(tiles)}</div>'
            "</section>"
        )
    else:
        upsell_section = ""

    # Frame 03: "on payment the page re-renders in place — watermark drops, badge
    # flips to 1080p, CTA becomes Download". The page is rendered server-side per
    # request, so a reload IS that re-render: it asks for the state fresh and comes
    # back unlocked. This poll only removes the need for the customer to do it by
    # hand after paying in another tab. It compares the whole purchase signature
    # (lock state + purchased add-on keys), so a raw/photos purchase re-renders an
    # already-unlocked page too — and it reads state, never the media.
    # Must reproduce /j/{code}/state's shape exactly (lock + sorted addon keys) or a
    # stale-looking signature would reload the page in a loop.
    #
    # And "exactly" means the same PREDICATE, not just the same shape. ``/state``
    # reports ``any_locked`` — a mixed jump whose speculative half is unpaid is still
    # "locked" for the purpose of re-rendering when that half is bought. This page's
    # ``locked`` flag is ``all_locked``, because it drives the *treatment* (badges,
    # download suppression, the primary action). On a mixed jump the two disagree, and
    # a baseline built from the treatment flag can NEVER match what /state answers: the
    # page reloaded every 6 s, forever, the moment a spec camera's locked edit joined a
    # paid one (observed live 2026-08-13). ``locked or locked_set`` is any-locked here.
    poll_locked = bool(locked or locked_set)
    init_sig = ("locked" if poll_locked else "open") + "|" + ",".join(sorted(purchased_addons))
    flip_js = (
        "<script>(function(){var n=0;"
        f"var init='{{sig}}';"
        "var t=setInterval(function(){"
        f"if(++n>{_FLIP_POLL_LIMIT}){{clearInterval(t);return;}}"
        f"fetch('/j/{{token}}/state',{{cache:'no-store'}}).then(function(r){{return r.json();}})"
        ".then(function(s){if(!s)return;"
        "var sig=(s.locked?'locked':'open')+'|'+((s.addons||[]).join(','));"
        "if(sig!==init){clearInterval(t);location.reload();}})"
        "['catch'](function(){});"
        f"}},{_FLIP_POLL_MS});}})();</script>"
    ).replace("{token}", html.escape(poll_token or "", quote=True)).replace(
        "{sig}", html.escape(init_sig, quote=True)
    ) if poll_token else ""

    if tabbed:
        eyebrow = "We filmed it anyway" if locked else "Your jump is ready"
        eyebrow_html = f'<div class="eyebrow">{eyebrow}</div>'
        tab_nav = (
            '<nav class="tabs">'
            '<a class="tabbtn" href="#tab-video">Video</a>'
            '<a class="tabbtn" href="#tab-photos">Photos</a>'
            "</nav>"
        )
        tab_js = (
            "<script>function _t(){var h=location.hash==='#tab-photos'?'tab-photos':'tab-video';"
            "document.querySelectorAll('.tab-panel').forEach(function(p){p.classList.toggle('active',p.id===h);});"
            "document.querySelectorAll('.tabbtn').forEach(function(b){b.classList.toggle('active',b.getAttribute('href')==='#'+h);});}"
            "window.addEventListener('hashchange',_t);_t();</script>"
        )
    else:
        eyebrow_html = tab_nav = tab_js = ""

    # The purchased Raw Footage section: camera masters, always downloadable (the
    # customer bought exactly these bytes), badged as-filmed so they aren't mistaken
    # for the edit. Lives in the Video tab — one layout, one place for moving pictures.
    raw_cards = "".join(
        f"""
        <div class="vcard"><div class="pbadge ok">RAW · AS FILMED</div>
          <video controls preload="metadata" playsinline src="{e(url)}"></video>
          <div class="vlabel"><div class="titles">{e(label)}<span>Camera master</span></div>
          <a class="vdl" href="{e(url)}" download>Download</a></div>
        </div>"""
        for label, url in raw_videos
    )
    raw_section = (
        f"""
      <h2>Raw Footage <span>({len(raw_videos)})</span></h2>
      <div class="vgrid">{raw_cards}</div>"""
        if raw_videos else ""
    )

    # The purchased spec-flight load video: filmed from the air on the customer's jump
    # day, but by a flyer who exited with somebody else. Badged and captioned as the
    # group/aerial angle so it is never mistaken for their own freefall — the one promise
    # this product must not make (design doc Stage 7).
    load_cards = "".join(
        f"""
        <div class="vcard"><div class="pbadge ok">FROM THE AIR</div>
          <video controls preload="metadata" playsinline{poster_attr(label)} src="{e(url)}"></video>
          <div class="vlabel"><div class="titles">{e(label)}<span>Your jump day from the air</span></div>
          <a class="vdl" href="{e(url)}" download>Download</a></div>
        </div>"""
        for label, url in load_videos
    )
    load_section = (
        f"""
      <h2>Load Video <span>({len(load_videos)})</span></h2>
      <div class="vgrid">{load_cards}</div>"""
        if load_videos else ""
    )

    panel_cls = ' class="tab-panel"' if tabbed else ""
    videos_section = f"""<section id="tab-video"{panel_cls}>
      <h2>Videos <span>({len(videos)})</span></h2>
      <div class="vgrid">{"".join(video_cards)}</div>{group_unlock_html}{load_section}{raw_section}
    </section>"""
    if tabbed and photos_section:
        photos_section = photos_section.replace(
            'class="photos" id="tab-photos"', 'class="photos tab-panel" id="tab-photos"'
        )

    # `referrer: no-referrer` is load-bearing, not hygiene: this page's URL *is* the
    # customer's credential, so it must never travel in a Referer header. Without it,
    # following the unlock CTA or an upsell tile hands the short code to the checkout
    # host — harmless while that's SkydiveOS's own domain, a credential leak the moment
    # CHECKOUT_URL_TEMPLATE points at a payment provider. The outbound anchors carry
    # rel="noreferrer" too, so the page is covered even if the meta tag is ever dropped.
    # (Kept as a source comment: an HTML comment would ship to the customer.)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title} — {brand_e}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg:{_BG}; --surface:{_SURFACE}; --surface-2:{_SURFACE_2};
    --red:{_RED}; --red-dim:{_RED_DIM}; --locked:{_LOCKED_BADGE};
    --white:#f5f5f5; --gray:#9a9a9a; --line:#2a2a2a;
  }}
  body {{
    background:var(--bg); color:var(--white); line-height:1.5;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  }}
  a {{ color:inherit; text-decoration:none; }}

  header {{
    display:flex; align-items:center; justify-content:space-between;
    padding:14px 32px; border-bottom:1px solid var(--line);
    position:sticky; top:0; z-index:10;
    background:rgba(10,10,10,.9); backdrop-filter:blur(8px);
  }}
  header .logo {{ display:flex; align-items:center; }}
  header .logo img {{ height:100px; width:auto; display:block; }}
  header .brand {{ font-weight:800; letter-spacing:3px; font-size:15px; color:#fff; text-transform:uppercase; }}
  header .tagline {{ color:var(--gray); font-size:13px; }}

  .hero {{ max-width:980px; margin:0 auto; padding:56px 32px 30px; }}
  .hero .eyebrow {{ color:var(--red); font-weight:700; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:14px; }}
  .hero h1 {{ font-size:clamp(32px,5vw,52px); font-weight:800; letter-spacing:-.5px; margin-bottom:12px; }}
  .hero .sub {{ color:var(--gray); font-size:15px; }}
  .hero .sub strong {{ color:var(--white); font-weight:600; }}

  main {{ max-width:980px; margin:0 auto; padding:0 32px 10px; }}
  main section {{ padding-top:36px; }}
  h2 {{ font-size:20px; font-weight:700; margin-bottom:20px; }}
  h2 span {{ color:var(--gray); font-weight:500; }}

  .tabs {{ max-width:980px; margin:0 auto; padding:0 32px; display:flex; gap:32px; border-bottom:1px solid var(--line); }}
  .tabbtn {{
    padding:14px 2px; font-weight:700; font-size:15px; color:var(--gray);
    border-bottom:3px solid transparent; transition:color .15s ease,border-color .15s ease;
  }}
  .tabbtn.active {{ color:var(--white); border-bottom-color:var(--red); }}
  .tab-panel {{ display:none; }}
  .tab-panel.active {{ display:block; }}

  .vgrid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:18px; }}
  .vcard {{
    position:relative; background:var(--surface); border:1px solid var(--line);
    border-radius:14px; overflow:hidden; transition:border-color .15s ease,transform .15s ease;
  }}
  .vcard:hover {{ border-color:var(--red-dim); transform:translateY(-2px); }}
  .vcard video {{ width:100%; display:block; background:#000; aspect-ratio:16/9; }}
  .vlabel {{ padding:14px 16px 16px; display:flex; align-items:center; justify-content:space-between; gap:12px; }}
  .vlabel .titles {{ min-width:0; font-weight:700; font-size:14.5px; }}
  .vlabel span {{ display:block; font-weight:400; color:var(--gray); font-size:12.5px; margin-top:2px; }}
  .vdl {{
    flex-shrink:0; background:transparent; border:1px solid var(--red); color:var(--red);
    font-weight:700; font-size:12.5px; padding:8px 14px; border-radius:8px;
    transition:background .15s ease,color .15s ease;
  }}
  .vdl:hover {{ background:var(--red); color:#fff; }}
  .pbadge {{
    position:absolute; top:10px; left:10px; z-index:2;
    background:rgba(0,0,0,.65); border:1px solid rgba(255,255,255,.15); color:var(--locked);
    font-size:10px; font-weight:700; letter-spacing:.5px; padding:4px 8px; border-radius:6px;
  }}
  .pbadge.ok {{ color:#fff; }}

  .shead {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; margin-bottom:20px; }}
  .shead h2 {{ margin-bottom:0; }}
  .btn {{ background:var(--red); color:#fff; padding:10px 18px; border-radius:9px; font-weight:700; font-size:13px; }}
  .pgrid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
  .ptile {{ display:block; aspect-ratio:1/1; overflow:hidden; border-radius:10px; border:1px solid var(--line); background:var(--surface); }}
  .ptile img {{ width:100%; height:100%; object-fit:cover; transition:transform .2s; }}
  .ptile:hover img {{ transform:scale(1.05); }}
  .teaser {{ color:var(--gray); padding:14px 0; }}

  /* The primary action. Set in caps in CSS, not by upper-casing the string, so the
     label stays one readable sentence for translation and for the tests/scripts that
     match on it (Frame 03). */
  .cta {{ max-width:980px; margin:0 auto; padding:0 32px 34px; }}
  /* The per-camera unlock offers and the photo-grid offer sit INSIDE main, which is
     already inset — so they carry only the vertical rhythm, not a second gutter. */
  main .cta {{ max-width:none; padding:20px 0 0; }}
  .ctabtn {{
    display:inline-flex; align-items:center; gap:14px;
    background:var(--red); color:#fff; padding:16px 26px; border-radius:10px;
    font-weight:700; font-size:16px; letter-spacing:.5px; text-transform:uppercase;
    box-shadow:0 8px 24px rgba(213,0,0,.35); transition:transform .15s ease,box-shadow .15s ease;
  }}
  .ctabtn:hover {{ transform:translateY(-2px); box-shadow:0 12px 30px rgba(213,0,0,.45); }}
  .ctasub {{ display:block; color:var(--gray); font-size:13px; margin-top:12px; }}

  .upsell {{ max-width:980px; margin:44px auto 0; padding:0 32px 24px; }}
  .ulabel {{ color:var(--red); font-weight:700; font-size:12px; letter-spacing:1px; text-transform:uppercase; margin-bottom:12px; }}
  .urow {{ display:grid; gap:14px; }}
  .utile {{
    display:grid; grid-template-columns:1fr auto; column-gap:20px; align-items:center;
    background:linear-gradient(120deg,var(--surface-2),#150505);
    border:1px solid var(--red-dim); border-radius:16px; padding:24px 26px; color:inherit;
  }}
  .utile .utitle {{ grid-column:1; font-size:20px; font-weight:700; margin-bottom:6px; }}
  .utile .ublurb {{ grid-column:1; color:var(--gray); font-size:14px; }}
  .utile .uprice {{
    grid-column:2; grid-row:1 / span 2; justify-self:end; white-space:nowrap;
    background:var(--red); color:#fff; font-weight:700; font-size:26px;
    padding:11px 22px; border-radius:9px;
  }}
  /* "Add $15" — the word is decoration, so it lives here and the price text stays
     exactly the catalogue's string. */
  .utile .uprice::before {{ content:"Add "; font-size:14px; font-weight:700; margin-right:6px; }}

  footer {{ text-align:center; padding:30px 20px 50px; color:var(--gray); font-size:12.5px; border-top:1px solid var(--line); }}
  footer .fmark {{ color:var(--red); font-weight:700; }}

  @media (max-width:620px) {{
    header {{ padding:10px 20px; }}
    header .logo img {{ height:68px; }}
    .hero, .tabs, main, .cta, .upsell {{ padding-left:20px; padding-right:20px; }}
    .hero {{ padding-top:34px; }}
    .tabs {{ gap:22px; }}
    .vgrid {{ grid-template-columns:1fr; }}
    .pgrid {{ grid-template-columns:repeat(3,1fr); }}
    .utile {{ grid-template-columns:1fr; row-gap:14px; }}
    .utile .uprice {{ grid-column:1; grid-row:auto; justify-self:start; }}
  }}
</style></head>
<body>
  <header>{brand_mark}<div class="tagline">{_TAGLINE}</div></header>
  <div class="hero">{eyebrow_html}<h1>{title}</h1><div class="sub">{subtitle}</div></div>
  {cta_bar}{tab_nav}
  <main>
    {videos_section}{photos_section}
  </main>
  {upsell_section}
  <footer>Powered by <span class="fmark">{_PLATFORM_MARK}</span> · {brand_e}</footer>
{tab_js}{flip_js}</body></html>"""
