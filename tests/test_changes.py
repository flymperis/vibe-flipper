"""Price changes between scrapes: recorded by upsert, listed by changes.price_changes."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vibe_flipper import changes
from vibe_flipper.db import Base
from vibe_flipper.jobs import upsert
from vibe_flipper.models import Listing, ListingPrice, utcnow
from vibe_flipper.scrapers.base import RawListing


@pytest.fixture
def s():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def raw(price, title="iPhone 13 128GB", ext="1"):
    return RawListing("insomnia", ext, f"u{ext}", title, price)


def test_upsert_reports_price_changes(s):
    _, is_new, changed, repriced = upsert(s, raw(400))
    assert (is_new, changed, repriced) == (True, False, False)
    s.flush()
    assert upsert(s, raw(400))[1:] == (False, False, False)
    assert upsert(s, raw(380))[1:] == (False, True, True)
    assert upsert(s, raw(380, title="iPhone 13 128GB μπλε"))[1:] == (False, True, False)  # title only
    s.flush()
    assert [p.price for p in s.query(ListingPrice).order_by(ListingPrice.id)] == [400, 380]


def _history(s, ext, prices_days_ago):
    l = Listing(source="insomnia", external_id=ext, url="u" + ext, title="t" + ext, price=prices_days_ago[-1][0])
    s.add(l)
    s.flush()
    for price, days in prices_days_ago:
        s.add(ListingPrice(listing_id=l.id, price=price, seen_at=utcnow() - timedelta(days=days)))
    s.flush()
    return l


def test_price_changes_in_window(s):
    _history(s, "a", [(500, 10), (450, 2), (400, 1)])  # two drops inside the window
    _history(s, "b", [(300, 10), (350, 9)])  # change too old
    _history(s, "c", [(200, 5)])  # never changed
    _history(s, "d", [(100, 3), (100, 2)])  # seen twice, same price
    got = changes.price_changes(s, utcnow() - timedelta(days=7))
    assert [(c.listing.external_id, c.old, c.new) for c in got] == [("a", 450, 400), ("a", 500, 450)]
    newest = got[0]
    assert newest.diff == -50 and newest.pct == pytest.approx(-11.11, abs=0.01)
    assert newest.first == 500 and newest.total_pct == -20 and newest.history == [500, 450, 400]


def test_price_changes_respect_listing_filter(s):
    _history(s, "a", [(500, 3), (450, 2)])
    _history(s, "b", [(300, 3), (250, 2)])
    got = changes.price_changes(s, utcnow() - timedelta(days=7), lambda q: q.filter(Listing.external_id == "b"))
    assert [c.listing.external_id for c in got] == ["b"]


def test_first_prices(s):
    a = _history(s, "a", [(500, 3), (450, 2)])
    b = _history(s, "b", [(300, 3)])
    assert changes.first_prices(s, [a.id, b.id]) == {a.id: 500, b.id: 300}
    assert changes.first_prices(s, []) == {}
