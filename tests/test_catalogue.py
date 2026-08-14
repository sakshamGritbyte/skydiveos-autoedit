"""The operator's admin price catalogue, and the tiles it prices.

The bug these pin down was live on 2026-08-13: the gallery advertised raw footage at
``$29`` while SkydiveOS's catalogue charged ``$15``, and offered a Photo Pack and a
rebook discount the catalogue had no price for at all — both of which dead-ended the
customer on "No price is configured for media item …". One price, one place.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.catalogue import (
    CONFIG_ID,
    PriceCatalogue,
    clear_cache,
    load_price_catalogue,
    parse_catalogue,
)
from api.upsell import DEFAULT_TILES, UpsellTile, priced_tiles, repriced_from

# The shape the SkydiveOS admin UI actually writes (read off the live config,
# 2026-08-13): minor units, a currency, and a labels map it leaves empty.
LIVE_PRICING = {
    "items": {"unlock": 3900, "unlock_external": 2900, "raw": 1500},
    "currency": "usd",
    "labels": {},
}


# --------------------------------------------------------------------------- #
# parse_catalogue — pure, and forgiving: one bad row must not cost the page the
# other prices in a shared config document.
# --------------------------------------------------------------------------- #


def test_parses_the_live_admin_document() -> None:
    cat = parse_catalogue(LIVE_PRICING)

    assert cat is not None
    assert cat.items == {"unlock": 3900, "unlock_external": 2900, "raw": 1500}
    assert cat.currency == "usd"


def test_a_bad_row_is_dropped_not_raised() -> None:
    """A typo in one price must not take every other price down with it."""
    cat = parse_catalogue(
        {"items": {"raw": 1500, "photos": "nineteen", "rebook": None, "x": -5, "y": True}}
    )

    assert cat is not None
    assert cat.items == {"raw": 1500}  # the others are unusable, so unpriced


def test_nothing_priced_is_the_same_as_no_catalogue() -> None:
    for pricing in ({}, {"items": {}}, {"items": None}, None, "nope", {"items": {"a": "b"}}):
        assert parse_catalogue(pricing) is None, pricing


def test_currency_defaults_to_usd_when_absent_or_junk() -> None:
    assert parse_catalogue({"items": {"raw": 1}}).currency == "usd"  # type: ignore[union-attr]
    assert parse_catalogue({"items": {"raw": 1}, "currency": ""}).currency == "usd"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# display — what the customer reads
# --------------------------------------------------------------------------- #


def test_whole_amounts_drop_the_cents() -> None:
    cat = parse_catalogue(LIVE_PRICING)
    assert cat is not None

    assert cat.display("unlock") == "$39"
    assert cat.display("unlock_external") == "$29"
    assert cat.display("raw") == "$15"


def test_part_amounts_keep_the_cents() -> None:
    cat = PriceCatalogue(items={"raw": 2950, "photos": 5, "free": 0})

    assert cat.display("raw") == "$29.50"
    assert cat.display("photos") == "$0.05"
    assert cat.display("free") == "$0"


def test_an_unpriced_item_has_no_display() -> None:
    cat = parse_catalogue(LIVE_PRICING)
    assert cat is not None
    assert cat.display("photos") is None
    assert "photos" not in cat


def test_an_unknown_currency_is_labelled_never_silently_dollars() -> None:
    """Printing ``$39`` for 39 francs is a mis-sold product, not a rendering detail."""
    assert PriceCatalogue(items={"raw": 3900}, currency="chf").display("raw") == "CHF 39"
    assert PriceCatalogue(items={"raw": 3900}, currency="eur").display("raw") == "€39"


def test_labels_override_the_title_only_when_the_operator_sets_one() -> None:
    cat = PriceCatalogue(items={"raw": 100}, labels={"raw": "Your unedited card", "x": "  "})

    assert cat.label("raw") == "Your unedited card"
    assert cat.label("x") is None  # blank is not a label
    assert cat.label("missing") is None


# --------------------------------------------------------------------------- #
# priced_tiles — the catalogue owns price AND existence
# --------------------------------------------------------------------------- #


def test_the_default_row_is_repriced_and_the_unpriced_tiles_disappear() -> None:
    """The exact live failure: $29-vs-$15, plus two tiles that cannot be bought."""
    cat = parse_catalogue(LIVE_PRICING)

    tiles = priced_tiles(DEFAULT_TILES, cat)

    assert [(t.key, t.price) for t in tiles] == [("raw", "$15")]
    # photos and rebook are gone — the catalogue prices neither, so the checkout would
    # have answered "No price is configured for media item …".
    assert {t.key for t in tiles} == {"raw"}


def test_the_operators_label_wins_over_the_configured_title() -> None:
    cat = PriceCatalogue(items={"raw": 1500}, labels={"raw": "Your whole card"})

    (tile,) = priced_tiles(DEFAULT_TILES, cat)

    assert (tile.title, tile.blurb, tile.price) == (
        "Your whole card",
        "Every unedited minute",  # blurb stays ours — the admin UI has no field for it
        "$15",
    )


def test_no_catalogue_leaves_every_tile_exactly_as_configured() -> None:
    """No shared database, or it didn't answer: the pre-catalogue page, byte for byte."""
    assert priced_tiles(DEFAULT_TILES, None) == DEFAULT_TILES


def test_the_load_video_tile_is_repriced_but_never_dropped() -> None:
    """It is generated per job, so an unpriced one keeps its configured price.

    Dropping it would silently remove a feature nobody asked to remove — unlike an
    operator-listed tile, whose absence from the catalogue IS the operator's decision.
    """
    tile = UpsellTile("load_video", "Your Load 2 aerial video", "Filmed from the air", "$39")

    assert repriced_from(tile, parse_catalogue(LIVE_PRICING)) == tile  # unpriced → kept
    assert repriced_from(tile, PriceCatalogue(items={"load_video": 4500})).price == "$45"
    assert repriced_from(tile, None) == tile


# --------------------------------------------------------------------------- #
# load_price_catalogue — cached, never raises, off unless configured
# --------------------------------------------------------------------------- #


def test_no_mongo_url_means_no_catalogue() -> None:
    clear_cache()
    assert load_price_catalogue(SimpleNamespace(mongo_url=None, mongo_db="x")) is None


def test_an_unreachable_database_degrades_to_configured_prices(monkeypatch) -> None:
    """A price lookup must never take the customer's page down."""
    clear_cache()
    import pymongo

    def _boom(*a, **k):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(pymongo, "MongoClient", _boom)
    settings = SimpleNamespace(mongo_url="mongodb://nope", mongo_db="skydiveos")

    assert load_price_catalogue(settings) is None  # not an exception


def test_the_catalogue_is_cached_so_a_polled_public_route_costs_one_read(
    monkeypatch,
) -> None:
    """``GET /j/{code}`` is anonymous and polled; Atlas must not be on its hot path."""
    clear_cache()
    import pymongo

    calls: list[dict] = []

    class _Coll:
        def find_one(self, query):
            calls.append(query)
            return {"pricing": LIVE_PRICING}

    class _Client:
        def __init__(self, *a, **k) -> None: ...
        def __getitem__(self, _name):
            return {"mediaconfigs": _Coll()}

    monkeypatch.setattr(pymongo, "MongoClient", _Client)
    settings = SimpleNamespace(mongo_url="mongodb://x", mongo_db="skydiveos")

    first = load_price_catalogue(settings)
    for _ in range(20):
        assert load_price_catalogue(settings) == first

    assert calls == [{"_configId": CONFIG_ID}]  # one read, not twenty-one
    assert first is not None and first.display("raw") == "$15"


def test_a_zero_ttl_re_reads(monkeypatch) -> None:
    """The cache is a TTL, not a freeze — an operator's edit lands within the minute."""
    clear_cache()
    import pymongo

    prices = {"items": {"raw": 1500}}
    calls: list[dict] = []

    class _Coll:
        def find_one(self, query):
            calls.append(query)
            return {"pricing": prices}

    class _Client:
        def __init__(self, *a, **k) -> None: ...
        def __getitem__(self, _name):
            return {"mediaconfigs": _Coll()}

    monkeypatch.setattr(pymongo, "MongoClient", _Client)
    settings = SimpleNamespace(mongo_url="mongodb://x", mongo_db="skydiveos")

    assert load_price_catalogue(settings, ttl=0).display("raw") == "$15"  # type: ignore[union-attr]
    prices["items"]["raw"] = 2500
    assert load_price_catalogue(settings, ttl=0).display("raw") == "$25"  # type: ignore[union-attr]
    assert len(calls) == 2
