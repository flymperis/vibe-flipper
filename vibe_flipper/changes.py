"""Price changes of listings, as seen between scrapes (from the listing_prices history)."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Listing, ListingPrice


@dataclass
class PriceChange:
    listing: Listing
    old: float
    new: float
    at: datetime
    first: float  # price when the listing was first seen
    history: list[float]  # every price, oldest first

    @property
    def diff(self) -> float:
        return self.new - self.old

    @property
    def pct(self) -> float:
        return self.diff / self.old * 100 if self.old else 0.0

    @property
    def total_pct(self) -> float:
        """Change since the listing was first seen."""
        return (self.new - self.first) / self.first * 100 if self.first else 0.0


def price_changes(session: Session, since: datetime, listing_filter=None) -> list[PriceChange]:
    """Every price change recorded since `since`, newest first. `listing_filter`
    narrows the listings (a function taking and returning a Listing query)."""
    ids_q = (session.query(ListingPrice.listing_id).group_by(ListingPrice.listing_id)
             .having(func.count(ListingPrice.id) > 1).having(func.max(ListingPrice.seen_at) >= since))
    ids = [i for (i,) in ids_q]
    if not ids:
        return []
    lq = session.query(Listing).filter(Listing.id.in_(ids))
    if listing_filter is not None:
        lq = listing_filter(lq)
    listings = {l.id: l for l in lq}
    prices: dict[int, list[ListingPrice]] = defaultdict(list)
    for p in (session.query(ListingPrice).filter(ListingPrice.listing_id.in_(list(listings)))
              .order_by(ListingPrice.listing_id, ListingPrice.seen_at, ListingPrice.id)):
        prices[p.listing_id].append(p)
    out = []
    for lid, ps in prices.items():
        history = [p.price for p in ps]
        for prev, cur in zip(ps, ps[1:]):
            if cur.seen_at >= since and cur.price != prev.price:
                out.append(PriceChange(listings[lid], prev.price, cur.price, cur.seen_at, history[0], history))
    out.sort(key=lambda c: c.at, reverse=True)
    return out


def first_prices(session: Session, listing_ids: list[int]) -> dict[int, float]:
    """Original price of the listings whose price has changed since first seen."""
    if not listing_ids:
        return {}
    rows = (session.query(ListingPrice.listing_id, ListingPrice.price)
            .filter(ListingPrice.listing_id.in_(listing_ids))
            .order_by(ListingPrice.listing_id, ListingPrice.seen_at, ListingPrice.id))
    out: dict[int, float] = {}
    for lid, price in rows:
        out.setdefault(lid, price)
    return out
