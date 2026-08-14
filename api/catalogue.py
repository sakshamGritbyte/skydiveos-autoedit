"""SkydiveOS's admin-set media price catalogue (``mediaconfigs.pricing``).

**Why this exists.** The gallery's offers — the unlock CTA and the "Add to your day"
tiles — are *displayed* here and *charged* by SkydiveOS. Until this module the two
numbers came from two places: the tile's price string from ``$UPSELL_TILES`` on this
box, the amount from the operator's Media-settings catalogue over there. Nothing
reconciled them, so on 2026-08-13 a live gallery advertised raw footage at **$29**
while the catalogue said **$15**, and offered a Photo Pack and a rebook discount that
the catalogue had no price for at all — both of which dead-ended the customer on
"No price is configured for media item …". The operator sets prices in one admin
screen; the page must read that screen, not a second copy of it.

So the catalogue is the authority for **price and existence**: an item the admin has
not priced is not offered. The tile's *copy* (title, blurb) stays in code — the admin
UI writes ``pricing.items`` and ``pricing.currency`` but leaves ``pricing.labels``
empty today, so a label here is used only when the operator actually sets one.

Three rules, the same ones every other cross-system read in this repo follows:

* **It never raises.** A gallery is a customer-facing page; an unreachable Mongo, a
  malformed document or a missing collection degrades to "no catalogue" and the page
  renders with its configured prices exactly as it did before this module existed.
  A pricing lookup must not be able to take the page down.
* **It is cached.** ``GET /j/{code}`` is public and polled (the re-render poller hits
  it after a purchase), so a per-request round trip to Atlas would put a database on
  the hot path of an anonymous route. One read per :data:`CACHE_TTL_S` is plenty —
  prices change with the season, not with the request.
* **It is off unless configured.** No ``MONGO_URL`` → ``None`` → legacy behaviour,
  byte-identical. That is what keeps the test suite and any deployment without the
  shared database working unchanged.

Parsing and formatting are pure (:class:`PriceCatalogue`); only :func:`load_price_catalogue`
touches the database, and it imports ``pymongo`` lazily like :mod:`ingest.match` does.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The singleton config document the SkydiveOS admin UI writes.
COLLECTION = "mediaconfigs"
CONFIG_ID = "media-global"

#: How long a fetched catalogue is reused. Long enough that a polled public route
#: costs no database traffic, short enough that an operator editing a price sees it
#: on the page within a minute.
CACHE_TTL_S = 60.0

#: Minor units per major unit. SkydiveOS stores ``3900`` for $39.00.
_MINOR_PER_MAJOR = 100

#: Symbols for the currencies a dropzone actually bills in; anything else falls back
#: to the uppercase ISO code so an unexpected currency is *labelled*, never silently
#: rendered as dollars.
_SYMBOLS = {"usd": "$", "cad": "$", "eur": "€", "gbp": "£", "aud": "$", "nzd": "$"}


@dataclass(frozen=True, slots=True)
class PriceCatalogue:
    """What the operator has priced, in minor units, keyed by checkout item.

    ``items`` maps a checkout item key (``raw``, ``photos``, ``unlock_external``, …)
    to its price in minor units. ``labels`` is the operator's override for an item's
    display title, empty in practice today.
    """

    items: Mapping[str, int]
    currency: str = "usd"
    labels: Mapping[str, str] = field(default_factory=dict)

    def __contains__(self, key: str) -> bool:
        return key in self.items

    def display(self, key: str) -> str | None:
        """The price to print for ``key``, or ``None`` when it isn't priced.

        Whole amounts drop the cents (``3900`` → ``$39``) because that is how a
        dropzone writes a price on a board; anything else keeps them (``2950`` →
        ``$29.50``).
        """
        minor = self.items.get(key)
        if minor is None:
            return None
        symbol = _SYMBOLS.get(self.currency.lower(), self.currency.upper() + " ")
        major, cents = divmod(int(minor), _MINOR_PER_MAJOR)
        return f"{symbol}{major}" if cents == 0 else f"{symbol}{major}.{cents:02d}"

    def label(self, key: str) -> str | None:
        """The operator's display override for ``key``, if they set one."""
        value = self.labels.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None


def parse_catalogue(pricing: Any) -> PriceCatalogue | None:
    """Build a catalogue from a raw ``mediaconfigs.pricing`` sub-document.

    Pure, and forgiving by design: an entry whose price isn't a non-negative number is
    dropped rather than raising, because one bad row in a shared config document must
    not cost the page every other price in it. Returns ``None`` when nothing is priced
    — indistinguishable, to the caller, from having no catalogue at all.
    """
    if not isinstance(pricing, Mapping):
        return None
    raw_items = pricing.get("items")
    if not isinstance(raw_items, Mapping):
        return None

    items: dict[str, int] = {}
    for key, value in raw_items.items():
        if not isinstance(key, str) or not key:
            continue
        # bool is an int subclass; `True` is not a price.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.warning("price catalogue: skipping %r — not a number (%r)", key, value)
            continue
        if value < 0:
            logger.warning("price catalogue: skipping %r — negative price (%r)", key, value)
            continue
        items[key] = int(value)
    if not items:
        return None

    currency = pricing.get("currency")
    raw_labels = pricing.get("labels")
    labels = (
        {k: v for k, v in raw_labels.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_labels, Mapping)
        else {}
    )
    return PriceCatalogue(
        items=items,
        currency=currency if isinstance(currency, str) and currency else "usd",
        labels=labels,
    )


#: ``(fetched_at, catalogue)`` per ``(mongo_url, db_name)``, so several settings objects
#: in one process (the test suite makes many) never share a cache entry.
_cache: dict[tuple[str, str], tuple[float, PriceCatalogue | None]] = {}


def clear_cache() -> None:
    """Forget every cached catalogue. For tests, and for an operator-triggered reload."""
    _cache.clear()


def load_price_catalogue(settings: Any, *, ttl: float = CACHE_TTL_S) -> PriceCatalogue | None:
    """The operator's priced items, or ``None`` when unavailable.

    ``None`` means "no catalogue" for every reason — no ``MONGO_URL``, no document,
    nothing priced, a database that won't answer — and every caller must treat it as
    "price things the way you did before". This function never raises.
    """
    url = getattr(settings, "mongo_url", None)
    if not url:
        return None
    db_name = getattr(settings, "mongo_db", None) or "skydiveos"
    cache_key = (str(url), str(db_name))

    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1]

    catalogue: PriceCatalogue | None = None
    try:
        from pymongo import MongoClient  # noqa: PLC0415 - optional dep, lazy like ingest.match

        client: Any = MongoClient(str(url), serverSelectionTimeoutMS=3000)
        doc = client[str(db_name)][COLLECTION].find_one({"_configId": CONFIG_ID})
        catalogue = parse_catalogue((doc or {}).get("pricing"))
    except Exception as e:  # noqa: BLE001 - a price lookup must never fail a gallery
        # Logged at warning, not error: the page still renders, just with its
        # configured prices. Cached as None below so a down database is asked for at
        # most once per TTL instead of on every request.
        logger.warning("price catalogue unavailable (%r) — using configured prices", e)

    _cache[cache_key] = (now, catalogue)
    return catalogue
