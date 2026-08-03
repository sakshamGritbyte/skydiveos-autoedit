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
  "Unlock full video" CTA, and a photo teaser instead of the grid.

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

import html
from collections.abc import Sequence
from datetime import date

from .upsell import UpsellTile

#: Nice per-deliverable video labels + subtitle (Shred-style "Vidéo Tandem 001").
_VIDEO_META: dict[str, tuple[str, str]] = {
    "full_video": ("Full Video", "The complete edit"),
    "highlights": ("Highlights", "The best moments"),
    "freefall": ("Freefall", "The freefall"),
    "external_freefall": ("Freefall — Outside Camera", "Cameraman angle"),
    "chute_libre_selfie": ("Freefall — Selfie Camera", "Instructor angle"),
    "final": ("Your Skydive Edit", "The complete edit"),
}

#: Design-doc palette (Frame 03 notes). The accent is the only thing the lock state
#: changes: green when the customer owns the edit, amber while it's behind the paywall.
_BG = "#0c1218"
_SURFACE = "#131b24"
_ACCENT_UNLOCKED = "#5bbd84"
_ACCENT_LOCKED = "#e2a13f"


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
    """
    e = html.escape
    brand_e = e(brand)
    title = e(customer_name)
    accent = _ACCENT_LOCKED if locked else _ACCENT_UNLOCKED

    # Hero meta: date · product · instructor · location, skipping whatever is unknown.
    meta_bits = [
        b
        for b in (
            e(_display_date(jump_date)) if jump_date else None,
            e(product_label) if product_label else None,
            f"Instructor {e(instructor_name)}" if instructor_name else None,
            e(location) if location else None,
        )
        if b
    ]
    subtitle = "  ·  ".join(meta_bits)

    if locked:
        badge = '<div class="pbadge">720P PREVIEW</div>'
    elif show_downloads:
        badge = '<div class="pbadge ok">1080P · FULL QUALITY</div>'
    else:
        badge = ""
    guard = ' controlsList="nodownload"' if locked else ""
    video_cards = []
    for name, url in videos:
        label, sub = _video_label(name)
        dl = (
            f'<a class="vdl" href="{e(url)}" download>Download</a>'
            if show_downloads and not locked else ""
        )
        video_cards.append(f"""
        <div class="vcard">{badge}
          <video controls preload="metadata" playsinline{guard} src="{e(url)}"></video>
          <div class="vlabel">{e(label)}<span>{e(sub)}</span>{dl}</div>
        </div>""")

    photo_tiles = "".join(
        f'<a class="ptile" href="{e(u)}" target="_blank" rel="noopener">'
        f'<img loading="lazy" src="{e(u)}" alt="photo"></a>'
        for u in photos
    )

    dl_btn = (
        f'<a class="btn" href="{e(download_all_url)}" download>Download all photos (.zip)</a>'
        if download_all_url and not locked else ""
    )
    n_photos = photo_count_teaser if locked else len(photos)
    if locked:
        photo_body = (
            f'<p class="teaser">{n_photos} photos included — unlock to see them all.</p>'
            if n_photos else '<p class="teaser">Photos unlock with the full video.</p>'
        )
    else:
        photo_body = f'<div class="pgrid">{photo_tiles}</div>'
    photos_section = (
        f"""
      <section class="photos" id="tab-photos">
        <div class="shead"><h2>Photos <span>({n_photos})</span></h2>{dl_btn}</div>
        {photo_body}
      </section>"""
        if (photos or (locked and tabbed) or (locked and n_photos)) else ""
    )

    # The primary action. Locked → the paywall CTA (an anchor when SkydiveOS's
    # checkout URL is known, plain text otherwise: the page must never dead-link the
    # customer). Unlocked → the download button, when the host gave us a URL for it.
    cta_bar = ""
    if locked:
        cta_line = f"Unlock full video — {e(price_display)}"
        cta_action = (
            f'<a class="ctabtn" href="{e(unlock_url)}">🔒 {cta_line}</a>'
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
    if upsells:
        tiles = []
        for t in upsells:
            body = (
                f'<div class="utitle">{e(t.title)}</div>'
                f'<div class="ublurb">{e(t.blurb)}</div>'
                f'<div class="uprice">{e(t.price)}</div>'
            )
            tiles.append(
                f'<a class="utile" href="{e(t.url)}">{body}</a>'
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

    panel_cls = ' class="tab-panel"' if tabbed else ""
    videos_section = f"""<section id="tab-video"{panel_cls}>
      <h2>Videos <span>({len(videos)})</span></h2>
      <div class="vgrid">{"".join(video_cards)}</div>
    </section>"""
    if tabbed and photos_section:
        photos_section = photos_section.replace(
            'class="photos" id="tab-photos"', 'class="photos tab-panel" id="tab-photos"'
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {brand_e}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{ --accent:{accent}; --bg:{_BG}; --surface:{_SURFACE}; --line:#22303d; --muted:#8fa0b0; }}
  body {{ background:var(--bg); color:#eef3f7; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  header {{ background:var(--surface); padding:16px; text-align:center; border-bottom:1px solid var(--line); }}
  header .brand {{ font-weight:800; letter-spacing:3px; font-size:15px; color:#fff; text-transform:uppercase; }}
  .hero {{ padding:28px 20px 6px; text-align:center; }}
  .hero h1 {{ font-size:34px; font-weight:800; letter-spacing:-.5px; }}
  .hero .sub {{ color:var(--muted); margin-top:8px; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; }}
  .hero .eyebrow {{ color:var(--accent); font-size:12px; letter-spacing:2px; text-transform:uppercase; font-weight:700; margin-bottom:8px; }}
  main {{ max-width:1100px; margin:0 auto; padding:14px 20px 20px; }}
  h2 {{ font-size:15px; letter-spacing:2px; text-transform:uppercase; margin:24px 0 14px; border-left:3px solid var(--accent); padding-left:10px; }}
  h2 span {{ color:var(--accent); }}
  .vgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }}
  .vcard {{ background:var(--surface); border-radius:10px; overflow:hidden; position:relative; border:1px solid var(--line); }}
  .vcard video {{ width:100%; display:block; background:#000; aspect-ratio:16/9; }}
  .vlabel {{ padding:12px 14px; font-weight:700; }}
  .vlabel span {{ display:block; font-weight:400; color:var(--muted); font-size:12px; margin-top:3px; }}
  .vdl {{ display:inline-block; margin-top:8px; color:#0c1218; background:{_ACCENT_UNLOCKED}; text-decoration:none; padding:7px 14px; border-radius:7px; font-size:12px; font-weight:800; }}
  .pbadge {{ position:absolute; top:10px; left:10px; z-index:2; background:rgba(0,0,0,.72); color:{_ACCENT_LOCKED}; font-size:10px; font-weight:800; letter-spacing:1px; padding:5px 9px; border-radius:5px; }}
  .pbadge.ok {{ color:{_ACCENT_UNLOCKED}; }}
  .shead {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }}
  .btn {{ background:var(--accent); color:#0c1218; text-decoration:none; padding:10px 18px; border-radius:8px; font-weight:800; font-size:13px; }}
  .pgrid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:8px; }}
  .ptile {{ display:block; aspect-ratio:1/1; overflow:hidden; border-radius:6px; background:var(--surface); }}
  .ptile img {{ width:100%; height:100%; object-fit:cover; transition:transform .2s; }}
  .ptile:hover img {{ transform:scale(1.05); }}
  .teaser {{ color:var(--muted); padding:14px 0; }}
  .tabs {{ display:flex; gap:8px; justify-content:center; padding:14px 20px 0; }}
  .tabbtn {{ color:var(--muted); text-decoration:none; padding:9px 22px; border-radius:8px; background:var(--surface); font-weight:700; font-size:12px; letter-spacing:1px; text-transform:uppercase; }}
  .tabbtn.active {{ background:var(--accent); color:#0c1218; }}
  .tab-panel {{ display:none; }}
  .tab-panel.active {{ display:block; }}
  .cta {{ max-width:1100px; margin:0 auto; padding:14px 20px 4px; text-align:center; }}
  .ctabtn {{ display:block; background:var(--accent); color:#0c1218; text-decoration:none; padding:15px 30px; border-radius:9px; font-weight:800; font-size:15px; letter-spacing:.5px; }}
  .ctasub {{ color:var(--muted); font-size:12px; margin-top:8px; }}
  .upsell {{ max-width:1100px; margin:0 auto; padding:26px 20px 8px; border-top:1px solid var(--line); }}
  .ulabel {{ color:var(--muted); font-size:11px; letter-spacing:2px; text-transform:uppercase; font-weight:700; margin-bottom:10px; }}
  .urow {{ display:flex; gap:10px; overflow-x:auto; padding-bottom:6px; }}
  .utile {{ flex:0 0 auto; min-width:150px; background:var(--surface); border:1px solid var(--line); border-radius:9px; padding:13px 15px; text-decoration:none; color:inherit; }}
  .utile .utitle {{ font-size:12px; font-weight:800; letter-spacing:1px; text-transform:uppercase; }}
  .utile .ublurb {{ color:var(--muted); font-size:12px; margin-top:5px; }}
  .utile .uprice {{ font-weight:800; font-size:15px; margin-top:12px; }}
  footer {{ text-align:center; color:#5c6b78; padding:30px; font-size:12px; }}
</style></head>
<body>
  <header><div class="brand">{brand_e}</div></header>
  <div class="hero">{eyebrow_html}<h1>{title}</h1><div class="sub">{subtitle}</div></div>
  {cta_bar}{tab_nav}
  <main>
    {videos_section}{photos_section}
  </main>
  {upsell_section}
  <footer>Powered by {brand_e} · Blue skies! 🪂</footer>
{tab_js}</body></html>"""
