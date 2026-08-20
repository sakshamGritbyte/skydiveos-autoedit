"""The gallery's "Add to your day" upsell tiles (design doc Frame 03).

The customer landing page carries a row of add-on offers — raw footage, a photo
pack, a repeat-jumper discount — under the primary action. Two properties come
straight from the design notes:

* **Entitlement-independent.** The row renders identically on the unlocked (Path A)
  and locked (Path B) page: it's the operator's *second* revenue line whether or not
  the video was pre-purchased. Only the player treatment and the primary action
  differ between the two states.
* **Operator-configurable.** Prices at a dropzone change with the season, so the
  tiles come from ``$UPSELL_TILES`` rather than from code:

      UPSELL_TILES=raw:Raw Footage:Every unedited minute:$29|photos:Photo Pack:...

  one tile per ``|``, four ``:``-separated fields per tile
  (``key:title:blurb:price`` — the blurb may be empty, the price is the remainder so
  it can contain a colon). Unset → :data:`DEFAULT_TILES` (the three in the design).
  ``off`` / ``none`` → no row at all.

  The *price* on a tile, and whether the tile is offered at all, come from the
  operator's admin catalogue when one is reachable (:func:`priced_tiles`); the
  spec above then supplies only the key, title and blurb. Two copies of one price
  is how a live gallery came to advertise $29 for a $15 item.

Tiles link through the same ``CHECKOUT_URL_TEMPLATE`` as the unlock CTA, with an
extra ``{item}`` placeholder carrying the tile's key (:func:`link_tiles`). As with
the unlock CTA, an unset template renders the tile as plain text — the page must
never dead-link a customer.

This module is **pure** (parsing + string formatting, no I/O, no ``api.*`` imports
beyond nothing at all) so :mod:`api.gallery` can stay a pure renderer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

#: Values of ``$UPSELL_TILES`` that switch the row off entirely.
_DISABLED = frozenset({"off", "none", "-", "0", "false"})

#: Field/tile separators in the ``$UPSELL_TILES`` spec.
_TILE_SEP = "|"
_FIELD_SEP = ":"
_FIELDS = 4


@dataclass(frozen=True, slots=True)
class UpsellTile:
    """One add-on offer shown on the customer gallery.

    ``key`` is the stable machine name SkydiveOS's checkout receives (via the
    template's ``{item}`` placeholder); the rest is display text. ``url`` is
    ``None`` until :func:`link_tiles` resolves it for a given job — a tile with no
    URL renders as text, never as a broken link.
    """

    key: str
    title: str
    blurb: str
    price: str
    url: str | None = None


#: The three tiles from the design (Frame 03). Used when ``$UPSELL_TILES`` is unset,
#: so a fresh deployment shows the intended row without configuration.
DEFAULT_TILES: tuple[UpsellTile, ...] = (
    UpsellTile("raw", "Raw Footage", "Every unedited minute", "$29"),
    UpsellTile("photos", "Photo Pack", "32 stills, full res", "$19"),
    UpsellTile("rebook", "Book Again", "Repeat jumper discount", "-15%"),
)


#: Tile key for the spec-flight load video. Must match ``api.app.PURCHASABLE_ADDONS`` and
#: SkydiveOS's priced item keys, since it is what ``POST /jobs/{id}/unlock`` records.
LOAD_VIDEO_KEY = "load_video"


def load_video_tile(load_label: str | None, price: str) -> UpsellTile:
    """The "your load's aerial video" tile, for a media buyer on a spec-flight load.

    A customer who already bought media gets **no second gallery and no second email** —
    the load video is one more offer in the "Add to your day" row of the page they were
    already opening. Named after their actual load ("Your Load 14 aerial video") so it
    reads as a thing that happened on their day, not a generic upsell.

    The blurb is deliberately honest about what a load video is (design doc Stage 7: "your
    jump day", never "your jump") — the flyer exited with somebody else, so this is the
    group and the aerials, not their own freefall.
    """
    label = load_label or "load"
    return UpsellTile(
        key=LOAD_VIDEO_KEY,
        title=f"Your {label} aerial video",
        blurb="Filmed from the air on your jump day",
        price=price,
    )


def parse_tiles(spec: str | None) -> tuple[UpsellTile, ...]:
    """Parse ``$UPSELL_TILES`` into tiles. Never raises — a bad tile is skipped.

    ``None``/empty → :data:`DEFAULT_TILES`; ``off``/``none`` → ``()``. A malformed
    entry (wrong field count, blank key or title) is logged and dropped rather than
    failing the page: a typo in an env var must not take the customer's video away.
    """
    if spec is None or not spec.strip():
        return DEFAULT_TILES
    if spec.strip().lower() in _DISABLED:
        return ()

    tiles: list[UpsellTile] = []
    for entry in spec.split(_TILE_SEP):
        raw = entry.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(_FIELD_SEP, _FIELDS - 1)]
        if len(parts) != _FIELDS:
            logger.warning(
                "UPSELL_TILES: skipping %r — expected key:title:blurb:price", raw
            )
            continue
        key, title, blurb, price = parts
        if not key or not title:
            logger.warning("UPSELL_TILES: skipping %r — key and title are required", raw)
            continue
        tiles.append(UpsellTile(key=key, title=title, blurb=blurb, price=price))
    return tuple(tiles)


def priced_tiles(
    tiles: Sequence[UpsellTile], catalogue: Any | None
) -> tuple[UpsellTile, ...]:
    """Re-price ``tiles`` from the operator's admin catalogue, dropping the unpriced.

    **The catalogue owns price and existence; code owns the copy.** A dropzone sets
    prices in one admin screen, and until this function the page carried a second,
    unreconciled copy of them: a live gallery advertised raw footage at ``$29`` while
    the catalogue charged ``$15``, and offered two tiles the catalogue had no price for
    at all, each of which dead-ended the customer on "No price is configured…"
    (2026-08-13). Both failures are the same mistake — offering something this page
    cannot know the price of — so both are fixed the same way: the price is read from
    the catalogue, and a tile the operator has not priced is **not offered**.

    That is the ``never dead-link a customer`` rule of :func:`link_tiles` applied one
    level earlier. :func:`link_tiles` can only tell whether *this* box knows how to
    build a checkout URL; only the catalogue knows whether that checkout will accept
    the item.

    ``catalogue`` is a :class:`api.catalogue.PriceCatalogue` (typed loosely so this
    module stays import-free and pure). ``None`` — no shared database configured, or it
    didn't answer — returns ``tiles`` unchanged, which is the pre-catalogue behaviour
    every existing deployment keeps.
    """
    if catalogue is None:
        return tuple(tiles)
    repriced: list[UpsellTile] = []
    for tile in tiles:
        price = catalogue.display(tile.key)
        if price is None:
            logger.info(
                "upsell: dropping tile %r — the operator has not priced it", tile.key
            )
            continue
        title = catalogue.label(tile.key) or tile.title
        repriced.append(replace(tile, title=title, price=price))
    return tuple(repriced)


def offerable_tiles(
    tiles: Sequence[UpsellTile], availability: Mapping[str, bool]
) -> tuple[UpsellTile, ...]:
    """Drop each tile whose key THIS job cannot fulfil — offer only what can be served.

    :func:`priced_tiles` asks the catalogue "will the checkout accept this item?";
    this asks the job "do the bytes behind this item exist?" — the third leg of the
    ``never dead-link a customer`` rule, one level deeper than a link. A ``video_only``
    job extracted no stills, so a Photo Pack tile on its gallery is a checkout that
    takes $19 and delivers nothing; a pruned or child job has no ``raw/`` masters to
    stream. Selling media the page cannot serve is worse than a dead link — the link at
    least fails *before* the payment.

    ``availability`` maps a tile key to whether this job can fulfil it. Keys absent
    from the map pass through untouched: only the caller knows the media tiles it can
    reason about, and a non-media tile (``rebook``) or an operator's custom key is
    fulfilled outside this system entirely.
    """
    kept: list[UpsellTile] = []
    for tile in tiles:
        if availability.get(tile.key) is False:
            logger.info(
                "upsell: dropping tile %r — this job cannot fulfil it", tile.key
            )
            continue
        kept.append(tile)
    return tuple(kept)


def repriced_from(tile: UpsellTile, catalogue: Any | None) -> UpsellTile:
    """``tile`` with the catalogue's price when it has one, otherwise unchanged.

    For the per-job load-video tile, which is *generated* rather than operator-listed:
    an unpriced one keeps its configured price instead of vanishing, because dropping
    it would silently remove a feature nobody asked to remove. Its price still comes
    from the admin screen the moment ``load_video`` appears there.
    """
    if catalogue is None:
        return tile
    price = catalogue.display(tile.key)
    return tile if price is None else replace(tile, price=price)


def link_tiles(
    tiles: Sequence[UpsellTile],
    *,
    template: str | None,
    job_id: str,
    booking_id: str | None = None,
) -> tuple[UpsellTile, ...]:
    """Resolve each tile's checkout URL from ``template``, or leave it as text.

    ``template`` is ``$CHECKOUT_URL_TEMPLATE`` — the same one the unlock CTA uses,
    with ``{item}`` additionally available. An unset template, or one whose
    placeholders don't resolve, yields URL-less tiles (rendered as plain text).
    """
    if not template:
        return tuple(tiles)
    linked: list[UpsellTile] = []
    for tile in tiles:
        try:
            url = template.format(job_id=job_id, booking_id=booking_id or "", item=tile.key)
        except (KeyError, IndexError) as e:
            logger.warning("CHECKOUT_URL_TEMPLATE has an unknown placeholder (%r)", e)
            url = None
        linked.append(replace(tile, url=url))
    return tuple(linked)
