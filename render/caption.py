"""Render the customer caption (name + date) as a transparent PNG overlay.

The Render spec calls for the customer's name and jump date burned onto the intro
card. The obvious tool is FFmpeg's ``drawtext`` — but it is only present when
FFmpeg was built ``--enable-libfreetype``, which many distro/Homebrew builds (and
this dev machine) are not. Rather than depend on an optional FFmpeg feature, we
draw the caption with Pillow — already a dependency via MoviePy — into an RGBA PNG
and let :mod:`render.builder` composite it with the always-available ``overlay``
filter. Same result, no build-flag lottery.

The PNG is the full output frame so it can be overlaid at ``0:0``; the text sits in
the lower third with a soft shadow for legibility over any footage.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only; the real imports happen lazily in render_caption
    from PIL.ImageDraw import ImageDraw
    from PIL.ImageFont import FreeTypeFont, ImageFont

    _Font = FreeTypeFont | ImageFont

# Font resolution order: explicit path > $RENDER_FONT > first existing candidate.
# Covers macOS (dev) and common Linux GPU-worker images; falls back to Pillow's
# bundled bitmap font so a caption always renders, even on a bare box.
FONT_ENV_VAR = "RENDER_FONT"
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


class CaptionError(RuntimeError):
    """Raised when the caption overlay cannot be rendered (e.g. Pillow missing)."""


def resolve_font(override: str | None = None) -> str | None:
    """Return a usable font file path, or ``None`` to fall back to Pillow's default.

    See the module docstring for the resolution order. An explicit ``override`` that
    does not exist raises, so a misconfigured path fails loudly rather than silently
    dropping to the bitmap font.
    """
    import os

    if override is not None:
        if not Path(override).exists():
            raise CaptionError(f"font override does not exist: {override}")
        return override
    env = os.environ.get(FONT_ENV_VAR)
    if env:
        if not Path(env).exists():
            raise CaptionError(f"{FONT_ENV_VAR} points at a missing file: {env}")
        return env
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def render_caption(
    out_path: str | Path,
    *,
    customer_name: str,
    jump_date: str,
    width: int,
    height: int,
    font_path: str | None = None,
) -> Path:
    """Draw ``customer_name`` (large) over ``jump_date`` (small) to a transparent PNG.

    Args:
        out_path: Where to write the RGBA PNG.
        customer_name: Burned-in headline (the customer).
        jump_date: Burned-in subline (the jump date, already formatted).
        width / height: Frame size — the PNG matches the output geometry so it can
            be overlaid at ``0:0``.
        font_path: Explicit TrueType font; otherwise resolved via :func:`resolve_font`.

    Returns:
        The path written.

    Raises:
        CaptionError: Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:  # pragma: no cover - Pillow ships with MoviePy
        raise CaptionError("Pillow not installed; required to render the caption") from e

    resolved = resolve_font(font_path)
    name_size = max(height // 14, 12)
    date_size = max(height // 28, 10)
    if resolved is not None:
        name_font: _Font = ImageFont.truetype(resolved, name_size)
        date_font: _Font = ImageFont.truetype(resolved, date_size)
    else:
        name_font = ImageFont.load_default()
        date_font = ImageFont.load_default()

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Stack the two lines, centred horizontally, anchored in the lower third.
    gap = name_size // 4
    name_w, name_h = _text_size(draw, customer_name, name_font)
    date_w, date_h = _text_size(draw, jump_date, date_font)
    block_h = name_h + gap + date_h
    top = int(height * 0.68) - block_h // 2

    _draw_centred(draw, customer_name, name_font, width, top, name_w)
    _draw_centred(draw, jump_date, date_font, width, top + name_h + gap, date_w)

    out = Path(out_path)
    img.save(out)
    return out


def _text_size(draw: ImageDraw, text: str, font: _Font) -> tuple[int, int]:
    """Width/height of ``text`` in ``font`` via ``textbbox``."""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left), int(bottom - top)


def _draw_centred(
    draw: ImageDraw, text: str, font: _Font, width: int, y: int, text_w: int
) -> None:
    """Draw ``text`` horizontally centred at vertical ``y`` with a soft shadow."""
    x = (width - text_w) // 2
    shadow = max(width // 960, 1)
    draw.text((x + shadow, y + shadow), text, font=font, fill=(0, 0, 0, 160))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
