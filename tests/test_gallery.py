"""Tests for the customer gallery page (api/gallery.py).

:func:`render_gallery_html` is pure, so these are plain string assertions — no S3,
no HTTP, no browser. What matters is the *contract with the customer*: an unlocked
(Path A) page offers the clean videos and downloads; a locked (Path B) page offers
watermarked previews, the unlock CTA, and no way to download anything.
"""

from __future__ import annotations

import pytest

from api.gallery import _display_date, render_gallery_html
from api.upsell import DEFAULT_TILES, UpsellTile

_BASE = {
    "brand": "Ultimate DZ",
    "customer_name": "Sophie Lavoie",
    "jump_date": "2026-07-30",
    "location": "Parachute Montréal",
}


def _page(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        **_BASE,
        "videos": [("full_video", "/j/tok/media/full_video")],
        "photos": [],
    }
    kwargs.update(overrides)
    return render_gallery_html(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The legacy (default) page — the S3-hosted gallery must be unchanged in shape
# --------------------------------------------------------------------------- #


def test_defaults_render_the_plain_gallery() -> None:
    """No tabs, no badges, no CTA, no download links — exactly the legacy page."""
    html = _page(photos=["/p/1.jpg", "/p/2.jpg"], download_all_url="/all.zip")
    assert "Sophie Lavoie" in html
    # The design's hero meta typography (Frame 03), on both hosts.
    assert "Parachute Montréal" in html and "30 JUL 2026" in html
    assert 'class="tabbtn"' not in html  # tabs are opt-in
    assert 'class="ctabtn"' not in html  # no paywall CTA
    assert 'class="pbadge"' not in html  # no preview badge
    assert 'class="vdl"' not in html  # per-video downloads are opt-in
    assert "Download all photos" in html  # the photo zip button survives
    assert "/p/1.jpg" in html and "/p/2.jpg" in html


def test_customer_name_is_escaped() -> None:
    html = _page(customer_name='Bobby <script>alert("x")</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------- #
# Path A — unlocked
# --------------------------------------------------------------------------- #


def test_unlocked_served_page_offers_downloads_and_photos() -> None:
    html = _page(
        photos=["/j/tok/photos/a.jpg"],
        tabbed=True,
        show_downloads=True,
        photo_count_teaser=32,
    )
    assert "Your jump is ready" in html
    assert 'class="vdl"' in html and ">Download<" in html
    assert "/j/tok/photos/a.jpg" in html  # the real grid, not a teaser
    assert 'class="tabbtn"' in html and 'href="#tab-video"' in html
    assert 'href="#tab-photos"' in html
    assert 'class="ctabtn"' not in html  # nothing to unlock
    assert "nodownload" not in html


# --------------------------------------------------------------------------- #
# Path B — locked
# --------------------------------------------------------------------------- #


def test_locked_page_shows_preview_badge_cta_and_no_downloads() -> None:
    html = _page(
        photos=[],
        locked=True,
        unlock_url="https://pay.test/unlock/j1",
        price_display="$39",
        photo_count_teaser=32,
        tabbed=True,
    )
    assert "We filmed it anyway" in html
    assert "720P PREVIEW" in html
    assert "Unlock full video — $39" in html
    assert 'href="https://pay.test/unlock/j1"' in html
    assert "nodownload" in html  # the player can't offer a save
    assert 'class="vdl"' not in html  # no per-video download link
    assert "32 photos included" in html  # teaser, not the grid


def test_locked_page_without_checkout_url_renders_text_not_a_dead_link() -> None:
    """No CHECKOUT_URL_TEMPLATE yet → the CTA must not become a broken anchor."""
    html = _page(locked=True, unlock_url=None, tabbed=True)
    assert "Unlock full video" in html
    assert "ask at the desk" in html
    assert '<a class="ctabtn"' not in html


def test_locked_page_ignores_a_photo_zip_url() -> None:
    """A locked customer must not get the "download all photos" escape hatch."""
    html = _page(locked=True, photos=[], download_all_url="/all.zip", tabbed=True)
    assert "/all.zip" not in html
    assert "Download all photos" not in html


def test_locked_and_unlocked_share_the_same_layout() -> None:
    """Design doc: only the player treatment + primary action change."""
    common = {"videos": [("full_video", "u")], "photos": [], "tabbed": True}
    locked = _page(locked=True, photo_count_teaser=1, **common)  # type: ignore[arg-type]
    unlocked = _page(show_downloads=True, **common)  # type: ignore[arg-type]
    for marker in ('class="hero"', 'class="vgrid"', 'class="tabs"', "Powered by"):
        assert marker in locked and marker in unlocked


# --------------------------------------------------------------------------- #
# Frame 03 — the delivery landing page's hero, primary action, and upsell row
# --------------------------------------------------------------------------- #


def test_hero_meta_reads_date_product_instructor() -> None:
    """`14 AUG 2026 · TANDEM … · INSTRUCTOR MARC TREMBLAY` (upper-cased by CSS)."""
    html = _page(
        jump_date="2026-08-14",
        instructor_name="Marc Tremblay",
        product_label="Tandem · Handcam",
        tabbed=True,
    )
    assert "14 AUG 2026" in html
    assert "Tandem · Handcam" in html
    assert "Instructor Marc Tremblay" in html


def test_hero_meta_skips_what_it_does_not_know() -> None:
    """An unknown instructor/product must leave no empty separator behind."""
    html = _page(jump_date=None, location=None, instructor_name=None, product_label=None)
    assert '<div class="sub"></div>' in html


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-14", "14 AUG 2026"),
        ("2026-08-14T13:05:00Z", "14 AUG 2026"),  # tolerates a full ISO datetime
        ("later today", "later today"),  # unparseable: shown, not dropped
        (None, ""),
    ],
)
def test_display_date(value: str | None, expected: str) -> None:
    assert _display_date(value) == expected


def test_unlocked_page_shows_the_quality_badge_and_download_action() -> None:
    html = _page(
        tabbed=True,
        show_downloads=True,
        primary_download_url="/j/tok/media/full_video",
        primary_download_note="1080p MP4  ·  214 MB  ·  yours to keep",
    )
    assert "1080P · FULL QUALITY" in html
    assert "720P PREVIEW" not in html
    assert 'class="ctabtn dl"' in html and "Download video" in html
    assert 'href="/j/tok/media/full_video"' in html
    assert "214 MB" in html


def test_locked_page_ignores_a_download_action() -> None:
    """The paywall owns the primary action while locked — no download escape hatch."""
    html = _page(
        locked=True,
        tabbed=True,
        unlock_url="https://pay.test/u",
        primary_download_url="/j/tok/media/full_video",
        primary_download_note="1080p MP4",
    )
    assert "Download video" not in html
    assert 'class="ctabtn dl"' not in html and "download>" not in html
    assert "Unlock full video" in html  # the only primary action while locked


def test_upsell_row_renders_in_both_entitlement_states() -> None:
    """Design note: the row is the operator's second revenue line either way."""
    tiles = (
        UpsellTile("raw", "Raw Footage", "Every unedited minute", "$29", "https://pay/raw"),
        UpsellTile("photos", "Photo Pack", "32 stills, full res", "$19"),
    )
    for kwargs in ({"show_downloads": True}, {"locked": True, "photo_count_teaser": 3}):
        html = _page(tabbed=True, upsells=tiles, **kwargs)  # type: ignore[arg-type]
        assert "Add to your day" in html
        assert "Raw Footage" in html and "$29" in html
        assert 'href="https://pay/raw"' in html  # linked tile
        # A tile with no checkout URL stays text — never a dead link.
        assert '<div class="utile"><div class="utitle">Photo Pack' in html


def test_upsell_row_is_absent_when_no_tiles_are_configured() -> None:
    html = _page(tabbed=True, upsells=())
    assert "Add to your day" not in html
    assert 'class="utile"' not in html


def test_upsell_tile_text_is_escaped() -> None:
    html = _page(upsells=[UpsellTile("x", "<b>Raw</b>", "a & b", "$1", 'javascript:"')])
    assert "<b>Raw</b>" not in html
    assert "&lt;b&gt;Raw&lt;/b&gt;" in html and "a &amp; b" in html


def test_default_tiles_match_the_design() -> None:
    """The three tiles a fresh deployment shows, with no UPSELL_TILES set."""
    assert [(t.title, t.price) for t in DEFAULT_TILES] == [
        ("Raw Footage", "$29"),
        ("Photo Pack", "$19"),
        ("Book Again", "-15%"),
    ]
