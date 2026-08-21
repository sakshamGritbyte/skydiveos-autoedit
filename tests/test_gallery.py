"""Tests for the customer gallery page (api/gallery.py).

:func:`render_gallery_html` is pure, so these are plain string assertions — no S3,
no HTTP, no browser. What matters is the *contract with the customer*: an unlocked
(Path A) page offers the clean videos and downloads; a locked (Path B) page offers
watermarked previews, the unlock CTA, and no way to download anything.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from api.gallery import _display_date, brand_logo_data_uri, render_gallery_html
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


# --------------------------------------------------------------------------- #
# C-4 — Frame 03 conformance, asserted per state.
#
# The mockup is the contract here: headline, badge, primary action (in caps), the
# upsell row on both paths, one layout skeleton, and a locked page that flips itself
# when payment lands instead of waiting for a manual refresh.
# --------------------------------------------------------------------------- #

_FRAME03 = {
    "jump_date": "2026-08-14",
    "customer_name": "Sophie Lavoie",
    "instructor_name": "Marc Tremblay",
    "product_label": "Tandem · Handcam",
    "tabbed": True,
    "upsells": DEFAULT_TILES,
}


def _state_1_unlocked() -> str:
    return _page(
        show_downloads=True,
        # A selfie package has stills in BOTH states — locked shows the teaser,
        # unlocked shows the grid. Same section, same place.
        photos=["/j/tok/photos/a.jpg"],
        primary_download_url="/j/tok/media/full_video",
        primary_download_note="1080p MP4  ·  214 MB  ·  yours to keep",
        **_FRAME03,  # type: ignore[arg-type]
    )


def _state_2_locked() -> str:
    return _page(
        locked=True,
        unlock_url="https://pay.test/u",
        price_display="$39",
        photo_count_teaser=32,
        poll_token="tok",
        **_FRAME03,  # type: ignore[arg-type]
    )


def test_frame03_state_1_unlocked() -> None:
    html = _state_1_unlocked()
    assert "Your jump is ready" in html
    assert "1080P · FULL QUALITY" in html
    assert "Download video" in html
    assert "1080p MP4" in html and "214 MB" in html and "yours to keep" in html
    assert "14 AUG 2026" in html and "Instructor Marc Tremblay" in html
    assert "Unlock full video" not in html and "720P PREVIEW" not in html


def test_frame03_state_2_locked() -> None:
    html = _state_2_locked()
    assert "We filmed it anyway" in html
    assert "720P PREVIEW" in html
    assert "Unlock full video — $39" in html
    assert 'href="https://pay.test/u"' in html
    assert "nodownload" in html  # watermarked player, no save affordance
    assert "1080P · FULL QUALITY" not in html and "Download video" not in html


def test_frame03_primary_action_is_set_in_caps() -> None:
    """The mockup's CTA is upper-case. Done in CSS so the label stays translatable."""
    for html in (_state_1_unlocked(), _state_2_locked()):
        cta = html.split(".ctabtn {", 1)[1].split("}", 1)[0]
        assert "text-transform:uppercase" in cta.replace(" ", "")


def test_frame03_upsell_row_on_both_paths() -> None:
    for html in (_state_1_unlocked(), _state_2_locked()):
        assert "Add to your day" in html
        for title in ("Raw Footage", "Photo Pack", "Book Again"):
            assert title in html


def test_frame03_states_share_one_layout_skeleton() -> None:
    """Only the player treatment and the primary action may differ."""
    skeleton = (
        "<header>", 'class="hero"', 'class="eyebrow"', 'class="sub"', 'class="tabs"',
        'class="cta"', 'id="tab-video"', 'class="vgrid"', 'class="vcard"',
        'id="tab-photos"', 'class="upsell"', 'class="urow"', "<footer>",
    )
    unlocked, locked = _state_1_unlocked(), _state_2_locked()
    for marker in skeleton:
        assert marker in unlocked, marker
        assert marker in locked, marker


def test_posters_are_per_card_and_optional() -> None:
    """A still from that video on that card — and no attribute at all without one."""
    html = _page(
        videos=[("full_video", "/j/tok/media/full_video"), ("highlights", "/j/tok/m/h")],
        posters={"full_video": "/j/tok/poster/full_video"},
    )
    assert (
        '<video controls preload="metadata" playsinline'
        ' poster="/j/tok/poster/full_video"'
    ) in html
    # The card with no poster keeps exactly the markup it had before the feature: an
    # empty poster="" reads as a failed image in some browsers, which is worse than none.
    assert 'poster=""' not in html
    assert html.count("poster=") == 1


def test_a_page_with_no_posters_is_byte_identical_to_before() -> None:
    assert _page(posters={}) == _page()


def test_locked_page_flips_itself_when_payment_lands() -> None:
    """Frame 03: "on payment the page re-renders in place"."""
    html = _state_2_locked()
    assert "/j/tok/state" in html
    assert "location.reload()" in html
    # The poll compares the full purchase signature (lock + addon keys), so an
    # add-on purchase re-renders the page exactly like the paywall unlock does.
    assert "s.locked?'locked':'open'" in html and "s.addons" in html


def test_unlocked_page_does_not_poll() -> None:
    """Nothing to wait for once the customer owns the edit."""
    assert "/state" not in _state_1_unlocked()


def test_legacy_s3_page_emits_no_script() -> None:
    """The static S3 fallback can't reload into a fresh render — so it doesn't try."""
    html = _page(locked=True, photo_count_teaser=1)  # no poll_token, no tabs
    assert "location.reload()" not in html
    assert "/state" not in html


def test_a_jump_with_no_stills_shows_no_photos_tab_in_either_state() -> None:
    """A locked page must not advertise photos the package never shot (video_only)."""
    common = {"tabbed": True, "photos": []}
    locked = _page(locked=True, photo_count_teaser=0, **common)  # type: ignore[arg-type]
    unlocked = _page(show_downloads=True, **common)  # type: ignore[arg-type]
    for html in (locked, unlocked):
        assert 'id="tab-photos"' not in html
    assert "Photos unlock with the full video" not in locked


# --------------------------------------------------------------------------- #
# The locked photo-preview grid (BUG 350)
# --------------------------------------------------------------------------- #


def test_locked_page_with_photo_urls_renders_the_grid_not_the_teaser() -> None:
    """Locked + preview URLs → the watermarked grid and the photo set's own offer."""
    html = _page(
        locked=True,
        tabbed=True,
        photos=["/j/tok/photos/a.jpg", "/j/tok/photos/b.jpg"],
        photos_unlocked=False,
        photos_unlock_url="https://dz.example/checkout?item=photos",
        photos_unlock_price="$19",
    )
    assert "/j/tok/photos/a.jpg" in html and 'class="pgrid"' in html
    assert "unlock to see them all" not in html  # the teaser is the no-URLs fallback
    assert "Unlock your photos — $19" in html
    assert "item=photos" in html
    assert "Download all photos" not in html  # still no zip escape hatch
    assert "Photos <span>(2)</span>" in html  # count comes from the URLs


def test_locked_photo_offer_without_checkout_url_is_text_not_a_dead_link() -> None:
    html = _page(
        locked=True,
        photos=["/j/tok/photos/a.jpg"],
        photos_unlocked=False,
    )
    assert "Unlock your photos · ask at the desk" in html
    assert 'href=""' not in html


def test_locked_page_without_photo_urls_keeps_the_teaser() -> None:
    """Legacy callers (no preview URLs) still get the count line, not an empty grid."""
    html = _page(locked=True, photos=[], photo_count_teaser=32, tabbed=True)
    assert "32 photos included — unlock to see them all." in html
    assert "Unlock your photos" not in html


# --------------------------------------------------------------------------- #
# The Parachute Montréal skin (2026-08 mockup)
# --------------------------------------------------------------------------- #


def test_header_shows_the_logo_when_one_is_inlined() -> None:
    html = _page(logo_data_uri="data:image/png;base64,AAAA")
    assert '<div class="logo"><img src="data:image/png;base64,AAAA"' in html
    assert 'alt="Ultimate DZ"' in html  # the brand is still readable without images


def test_header_falls_back_to_the_brand_in_text_without_a_logo() -> None:
    """A deployment with no logo configured must still get a finished header."""
    html = _page()
    assert "<img" not in html.split("</header>", 1)[0]
    assert '<span class="brand">Ultimate DZ</span>' in html


def test_brand_logo_data_uri_reads_a_real_file_and_never_raises(tmp_path: Path) -> None:
    png = tmp_path / "logo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    assert brand_logo_data_uri(str(png)) == (
        "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    )
    # The three ways it can have nothing to inline — none of them may raise, all of
    # them fall back to the text header.
    assert brand_logo_data_uri(None) is None
    assert brand_logo_data_uri(str(tmp_path / "absent.png")) is None
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert brand_logo_data_uri(str(empty)) is None


def test_the_skin_is_the_brand_red_in_both_states() -> None:
    """The redesign drops the green/amber accent swap: one accent, two copies."""
    unlocked, locked = _state_1_unlocked(), _state_2_locked()
    for html in (unlocked, locked):
        assert "--red:#d50000" in html and "--bg:#0a0a0a" in html
    # Amber survives only as the locked badge's colour, which is what makes a mixed
    # jump's clean and watermarked cards distinguishable at a glance.
    assert "--locked:#e2a13f" in unlocked
    assert "720P PREVIEW" in locked and "720P PREVIEW" not in unlocked


def test_upsell_price_carries_the_catalogue_string_verbatim() -> None:
    """"Add" is CSS decoration — the price text stays exactly what was priced."""
    html = _page(upsells=[UpsellTile("raw", "Raw Footage", "Every minute", "$15", None)])
    assert '<div class="uprice">$15</div>' in html
    assert 'content:"Add "' in html


def test_platform_mark_and_tagline() -> None:
    html = _page()
    assert 'Powered by <span class="fmark">UltimateDZM</span> · Ultimate DZ' in html
    assert "Your souvenir is ready" in html
