"""Render the preview watermark as a transparent PNG overlay.

A ``preview_only`` job (design doc Path B — "we filmed it anyway") streams a
watermarked 720p preview behind the paywall, so the watermark's job is to make the
preview obviously not-the-product while leaving the footage watchable enough to sell
the unlock. Like :mod:`render.caption`, the mark is drawn with Pillow into a
full-frame RGBA PNG and composited by FFmpeg's always-available ``overlay`` filter —
**never** ``drawtext``, which needs an FFmpeg built ``--enable-libfreetype`` that the
deploy machines don't have.

The layout is a tile of translucent ``{brand} • PREVIEW`` rows (every other row
offset half a tile, so no crop or letterbox dodges the mark) plus a solid lower-third
strip telling the customer how to get the clean file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .caption import CaptionError, resolve_font

if TYPE_CHECKING:  # annotation-only; the real imports happen lazily in render_watermark
    from PIL.ImageFont import FreeTypeFont, ImageFont

    _Font = FreeTypeFont | ImageFont

#: Alpha of the tiled brand rows (0-255). ~28% — visible on any footage, not opaque.
_TILE_ALPHA = 72
#: Alpha of the lower-third message text.
_STRIP_TEXT_ALPHA = 230
#: Alpha of the lower-third backing band.
_STRIP_BAND_ALPHA = 110


def render_watermark(
    out_path: str | Path,
    *,
    width: int,
    height: int,
    brand: str,
    label: str = "PREVIEW",
    message: str = "Preview — unlock the full video",
    font_path: str | None = None,
) -> Path:
    """Draw the tiled preview watermark to a transparent PNG of the frame size.

    Args:
        out_path: Where to write the RGBA PNG.
        width / height: Output frame geometry — the PNG overlays at ``0:0``.
        brand: Dropzone brand shown in every tile (``"{brand} • {label}"``).
        label: Tile suffix, default ``PREVIEW``.
        message: Lower-third strip text (the unlock nudge).
        font_path: Explicit TrueType font; otherwise :func:`render.caption.resolve_font`.

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

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Tiled brand rows: every other row shifted half a tile so no crop escapes them.
    text = f"{brand} • {label}"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=tile_font)
    text_w, text_h = int(right - left), int(bottom - top)
    step_x = text_w + max(width // 10, text_w // 2)
    step_y = max(text_h * 4, height // 6)
    fill = (255, 255, 255, _TILE_ALPHA)
    for row, y in enumerate(range(step_y // 2, height, step_y)):
        offset = (step_x // 2) if row % 2 else 0
        for x in range(-step_x + offset, width, step_x):
            draw.text((x, y), text, font=tile_font, fill=fill)

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
