"""Render the preview watermark as a transparent PNG overlay.

A ``preview_only`` job (design doc Path B — "we filmed it anyway") streams a
watermarked 720p preview behind the paywall, so the watermark's job is to make the
preview obviously not-the-product: dense enough that keeping the preview is never a
substitute for buying the unlock, while the moment itself stays recognisable enough
to sell. Like :mod:`render.caption`, the mark is drawn with Pillow into a full-frame
RGBA PNG and composited by FFmpeg's always-available ``overlay`` filter — **never**
``drawtext``, which needs an FFmpeg built ``--enable-libfreetype`` that the deploy
machines don't have.

The layout is three stacked layers:

* a dense tile of translucent ``{brand} • PREVIEW`` rows (every other row offset half
  a tile, so no crop or letterbox dodges the mark),
* when a ``logo_path`` is given, the dropzone logo tiled between the text rows and
  once large across the centre of frame — the brand is on every part of the image,
* a solid lower-third strip telling the customer how to get the clean file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .caption import CaptionError, resolve_font

if TYPE_CHECKING:  # annotation-only; the real imports happen lazily in render_watermark
    from PIL.Image import Image as PILImage
    from PIL.ImageFont import FreeTypeFont, ImageFont

    _Font = FreeTypeFont | ImageFont

#: Alpha of the tiled brand rows (0-255). ~43% — reads on any footage without
#: flattening it to a title card.
_TILE_ALPHA = 110
#: Alpha of the tiled logo marks between the text rows.
_LOGO_TILE_ALPHA = 92
#: Alpha of the single large centre logo.
_CENTER_LOGO_ALPHA = 72
#: Alpha of the lower-third message text.
_STRIP_TEXT_ALPHA = 230
#: Alpha of the lower-third backing band.
_STRIP_BAND_ALPHA = 110


def _faded(logo: "PILImage", alpha: int) -> "PILImage":
    """Return a copy of ``logo`` with its alpha channel scaled to ``alpha``/255."""
    faded = logo.copy()
    faded.putalpha(faded.getchannel("A").point(lambda v: v * alpha // 255))
    return faded


def render_watermark(
    out_path: str | Path,
    *,
    width: int,
    height: int,
    brand: str,
    label: str = "PREVIEW",
    message: str = "Preview — unlock the full video",
    font_path: str | None = None,
    logo_path: str | Path | None = None,
) -> Path:
    """Draw the tiled preview watermark to a transparent PNG of the frame size.

    Args:
        out_path: Where to write the RGBA PNG.
        width / height: Output frame geometry — the PNG overlays at ``0:0``.
        brand: Dropzone brand shown in every tile (``"{brand} • {label}"``).
        label: Tile suffix, default ``PREVIEW``.
        message: Lower-third strip text (the unlock nudge).
        font_path: Explicit TrueType font; otherwise :func:`render.caption.resolve_font`.
        logo_path: Optional transparent-background logo PNG, tiled between the text
            rows and stamped large across the centre. Unreadable/missing → text-only
            mark (the preview must never fail for a branding asset).

    Returns:
        The path written.

    Raises:
        CaptionError: Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:  # pragma: no cover - Pillow ships with MoviePy
        raise CaptionError("Pillow not installed; required to render the watermark") from e

    resolved = resolve_font(font_path)
    tile_size = max(height // 24, 12)
    strip_size = max(height // 30, 10)
    if resolved is not None:
        tile_font: _Font = ImageFont.truetype(resolved, tile_size)
        strip_font: _Font = ImageFont.truetype(resolved, strip_size)
    else:  # pragma: no cover - bitmap fallback on a bare box
        tile_font = ImageFont.load_default()
        strip_font = ImageFont.load_default()

    logo: PILImage | None = None
    if logo_path is not None:
        try:
            with Image.open(logo_path) as raw:
                logo = raw.convert("RGBA")
            logo = logo.crop(logo.getbbox() or (0, 0, *logo.size))
        except OSError:
            logo = None  # never fail the preview over a branding asset

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # One large centre logo first, under the tiles: the frame itself carries the brand.
    if logo is not None:
        big_h = max(height // 2, 1)
        big = logo.resize((max(big_h * logo.width // logo.height, 1), big_h))
        img.alpha_composite(
            _faded(big, _CENTER_LOGO_ALPHA),
            ((width - big.width) // 2, (height - big.height) // 2),
        )

    # Tiled brand rows: dense, every other row shifted half a tile so no crop
    # escapes them.
    text = f"{brand} • {label}"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=tile_font)
    text_w, text_h = int(right - left), int(bottom - top)
    step_x = text_w + max(width // 14, text_w // 3)
    step_y = max(text_h * 2, height // 10)
    fill = (255, 255, 255, _TILE_ALPHA)
    small: PILImage | None = None
    if logo is not None:
        small_h = max(height // 8, 1)
        small = _faded(
            logo.resize((max(small_h * logo.width // logo.height, 1), small_h)),
            _LOGO_TILE_ALPHA,
        )
    for row, y in enumerate(range(step_y // 2, height, step_y)):
        offset = (step_x // 2) if row % 2 else 0
        for x in range(-step_x + offset, width, step_x):
            draw.text((x, y), text, font=tile_font, fill=fill)
        # A logo mark between every text row, on the opposite half-tile offset.
        if small is not None:
            logo_offset = 0 if row % 2 else (step_x // 2)
            for x in range(-step_x + logo_offset, width, step_x):
                img.alpha_composite(small, (x + (step_x - small.width) // 2, y + text_h))

    # Lower-third strip: a dark band + the unlock message, centred.
    band_h = strip_size * 3
    band_top = height - band_h - height // 20
    draw.rectangle(
        (0, band_top, width, band_top + band_h), fill=(0, 0, 0, _STRIP_BAND_ALPHA)
    )
    m_left, m_top, m_right, m_bottom = draw.textbbox((0, 0), message, font=strip_font)
    m_w, m_h = int(m_right - m_left), int(m_bottom - m_top)
    draw.text(
        ((width - m_w) // 2, band_top + (band_h - m_h) // 2),
        message,
        font=strip_font,
        fill=(255, 255, 255, _STRIP_TEXT_ALPHA),
    )

    out = Path(out_path)
    img.save(out)
    return out
