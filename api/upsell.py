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

Tiles link through the same ``CHECKOUT_URL_TEMPLATE`` as the unlock CTA, with an
extra ``{item}`` placeholder carrying the tile's key (:func:`link_tiles`). As with
the unlock CTA, an unset template renders the tile as plain text — the page must
never dead-link a customer.

This module is **pure** (parsing + string formatting, no I/O, no ``api.*`` imports
beyond nothing at all) so :mod:`api.gallery` can stay a pure renderer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

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
