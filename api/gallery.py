"""Customer gallery page: one branded link showing all the edit's videos + photos.

Instead of emailing the customer a handful of raw S3 links, delivery builds a single
self-contained HTML **gallery page** (this module renders it; :mod:`api.delivery`
hosts it on S3 and emails its link) — the Shred-style "one link, everything on one
page" experience: inline video players for each deliverable and a photo grid, branded
for the dropzone.

:func:`render_gallery_html` is **pure** (strings in, one HTML string out, no I/O) so it
stays trivially testable; the S3 upload + email live in :mod:`api.delivery`.
"""

from __future__ import annotations

import html

#: Nice per-deliverable video labels + subtitle (Shred-style "Vidéo Tandem 001").
_VIDEO_META: dict[str, tuple[str, str]] = {
    "full_video": ("Full Video", "The complete edit"),
    "highlights": ("Highlights", "The best moments"),
    "freefall": ("Freefall", "The freefall"),
    "external_freefall": ("Freefall — Outside Camera", "Cameraman angle"),
    "chute_libre_selfie": ("Freefall — Selfie Camera", "Instructor angle"),
    "final": ("Your Skydive Edit", "The complete edit"),
}


def _video_label(name: str) -> tuple[str, str]:
    return _VIDEO_META.get(name, (name.replace("_", " ").title(), ""))


def render_gallery_html(
    *,
    brand: str,
    customer_name: str,
    jump_date: str | None,
    location: str | None,
    videos: list[tuple[str, str]],
    photos: list[str],
    download_all_url: str | None = None,
) -> str:
    """Render the customer gallery page as one self-contained HTML string.

    ``videos`` is a list of ``(deliverable_name, url)``; ``photos`` a list of image
    URLs. All URLs are embedded as-is (presigned S3 links). No external assets — the
    page is fully inline so it renders straight from an S3 object.
    """
    e = html.escape
    brand_e = e(brand)
    title = e(customer_name)
    meta_bits = [b for b in (e(location) if location else None, e(jump_date) if jump_date else None) if b]
    subtitle = "  •  ".join(meta_bits)

    video_cards = []
    for name, url in videos:
        label, sub = _video_label(name)
        video_cards.append(f"""
        <div class="vcard">
          <video controls preload="metadata" playsinline src="{e(url)}"></video>
          <div class="vlabel">{e(label)}<span>{e(sub)}</span></div>
        </div>""")

    photo_tiles = "".join(
        f'<a class="ptile" href="{e(u)}" target="_blank" rel="noopener">'
        f'<img loading="lazy" src="{e(u)}" alt="photo"></a>'
        for u in photos
    )

    dl_btn = (
        f'<a class="btn" href="{e(download_all_url)}" download>Download all photos (.zip)</a>'
        if download_all_url else ""
    )
    photos_section = (
        f"""
      <section class="photos">
        <div class="shead"><h2>Photos <span>({len(photos)})</span></h2>{dl_btn}</div>
        <div class="pgrid">{photo_tiles}</div>
      </section>"""
        if photos else ""
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {brand_e}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background:#0d0d0d; color:#f2f2f2; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  header {{ background:#e2231a; padding:18px; text-align:center; }}
  header .brand {{ font-weight:800; letter-spacing:3px; font-size:22px; color:#fff; text-transform:uppercase; }}
  .hero {{ padding:34px 20px 10px; text-align:center; }}
  .hero h1 {{ font-size:34px; font-weight:800; }}
  .hero .sub {{ color:#b7b7b7; margin-top:8px; font-size:14px; letter-spacing:1px; text-transform:uppercase; }}
  main {{ max-width:1100px; margin:0 auto; padding:20px; }}
  h2 {{ font-size:18px; letter-spacing:2px; text-transform:uppercase; margin:26px 0 14px; border-left:4px solid #e2231a; padding-left:10px; }}
  h2 span {{ color:#e2231a; }}
  .vgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }}
  .vcard {{ background:#161616; border-radius:10px; overflow:hidden; }}
  .vcard video {{ width:100%; display:block; background:#000; aspect-ratio:16/9; }}
  .vlabel {{ padding:12px 14px; font-weight:700; }}
  .vlabel span {{ display:block; font-weight:400; color:#8f8f8f; font-size:12px; margin-top:3px; }}
  .shead {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }}
  .btn {{ background:#e2231a; color:#fff; text-decoration:none; padding:10px 18px; border-radius:8px; font-weight:700; font-size:13px; }}
  .pgrid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:8px; }}
  .ptile {{ display:block; aspect-ratio:1/1; overflow:hidden; border-radius:6px; background:#161616; }}
  .ptile img {{ width:100%; height:100%; object-fit:cover; transition:transform .2s; }}
  .ptile:hover img {{ transform:scale(1.05); }}
  footer {{ text-align:center; color:#666; padding:30px; font-size:12px; }}
</style></head>
<body>
  <header><div class="brand">{brand_e}</div></header>
  <div class="hero"><h1>{title}</h1><div class="sub">{subtitle}</div></div>
  <main>
    <section>
      <h2>Videos <span>({len(videos)})</span></h2>
      <div class="vgrid">{"".join(video_cards)}</div>
    </section>{photos_section}
  </main>
  <footer>Powered by {brand_e} · Blue skies! 🪂</footer>
</body></html>"""
