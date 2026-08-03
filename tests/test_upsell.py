"""Tests for the gallery's upsell tiles (api/upsell.py).

Pure parsing + URL templating, so these are plain assertions. What matters
operationally: a typo in ``$UPSELL_TILES`` must degrade to *fewer tiles*, never to a
broken customer page, and a tile must never render as a dead link.
"""

from __future__ import annotations

import pytest

from api.config import get_settings
from api.upsell import DEFAULT_TILES, UpsellTile, link_tiles, parse_tiles

_SPEC = "raw:Raw Footage:Every unedited minute:$29|photos:Photo Pack:32 stills:$19"


def test_unset_spec_falls_back_to_the_design_defaults() -> None:
    assert parse_tiles(None) == DEFAULT_TILES
    assert parse_tiles("") == DEFAULT_TILES
    assert parse_tiles("   ") == DEFAULT_TILES


@pytest.mark.parametrize("off", ["off", "none", "-", "0", "false", "OFF", " off "])
def test_spec_can_switch_the_row_off(off: str) -> None:
    assert parse_tiles(off) == ()


def test_parses_tiles_in_order() -> None:
    tiles = parse_tiles(_SPEC)
    assert [t.key for t in tiles] == ["raw", "photos"]
    assert tiles[0] == UpsellTile("raw", "Raw Footage", "Every unedited minute", "$29")
    assert tiles[1].blurb == "32 stills"


def test_fields_are_trimmed_and_an_empty_blurb_is_allowed() -> None:
    (tile,) = parse_tiles(" merch : T-Shirt : : $25 ")
    assert tile == UpsellTile("merch", "T-Shirt", "", "$25")


def test_price_may_contain_a_colon() -> None:
    (tile,) = parse_tiles("bundle:Bundle:Two jumps:2 for: $50")
    assert tile.price == "2 for: $50"


@pytest.mark.parametrize(
    "bad",
    [
        "raw:Raw Footage:$29",          # too few fields
        ":Raw Footage:blurb:$29",       # no key
        "raw::blurb:$29",               # no title
    ],
)
def test_a_malformed_tile_is_skipped_not_fatal(bad: str) -> None:
    """A bad env var must cost a tile, never the customer's page."""
    tiles = parse_tiles(f"{bad}|photos:Photo Pack:32 stills:$19")
    assert [t.key for t in tiles] == ["photos"]


def test_all_tiles_malformed_yields_an_empty_row() -> None:
    assert parse_tiles("nonsense|also:nonsense") == ()


def test_link_tiles_fills_the_item_placeholder() -> None:
    linked = link_tiles(
        parse_tiles(_SPEC),
        template="https://pay.test/co?job={job_id}&b={booking_id}&item={item}",
        job_id="j1",
        booking_id="BK-9",
    )
    assert linked[0].url == "https://pay.test/co?job=j1&b=BK-9&item=raw"
    assert linked[1].url == "https://pay.test/co?job=j1&b=BK-9&item=photos"


def test_link_tiles_without_a_template_leaves_tiles_as_text() -> None:
    """No CHECKOUT_URL_TEMPLATE yet → text tiles, never a dead link."""
    assert all(t.url is None for t in link_tiles(DEFAULT_TILES, template=None, job_id="j1"))


def test_link_tiles_survives_an_unknown_placeholder() -> None:
    linked = link_tiles(DEFAULT_TILES, template="https://pay/{nope}", job_id="j1")
    assert all(t.url is None for t in linked)


def test_settings_read_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPSELL_TILES", _SPEC)
    get_settings.cache_clear()
    try:
        assert [t.key for t in get_settings().upsell_tiles] == ["raw", "photos"]
    finally:
        get_settings.cache_clear()
