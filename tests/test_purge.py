"""Retention: which listings the daily purge deletes (runs on an in-memory DB)."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vibe_flipper.db import Base
from vibe_flipper.jobs import purge_old_listings
from vibe_flipper.models import Listing, ListingPrice, Product, utcnow


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def add(s, ext, *, days_ago, product=None, country="GR", method=None):
    seen = utcnow() - timedelta(days=days_ago)
    l = Listing(source="vinted", external_id=ext, url=f"u{ext}", title=ext, price=10, first_seen=seen,
                last_seen=seen, product=product, country=country, match_method=method)
    l.prices.append(ListingPrice(price=10, seen_at=seen))
    s.add(l)
    return l


def test_purge_rules(session):
    p = Product(name="RTX 3070", include_keywords=["3070"])
    session.add(p)
    add(session, "old-unmatched", days_ago=40)
    add(session, "recent-unmatched", days_ago=5)
    add(session, "old-matched-gr", days_ago=400, product=p)
    add(session, "old-matched-foreign", days_ago=40, product=p, country="RO")
    add(session, "old-matched-unknown-country", days_ago=40, product=p, country=None)
    add(session, "old-manual-not-a-product", days_ago=400, method="manual")
    add(session, "old-manual-foreign", days_ago=400, product=p, country="RO", method="manual")
    session.commit()

    assert purge_old_listings(session, 30, {"GR"}, dry_run=True) == 2
    assert purge_old_listings(session, 30, {"GR"}) == 2
    session.commit()
    left = {l.external_id for l in session.query(Listing)}
    assert left == {"recent-unmatched", "old-matched-gr", "old-matched-unknown-country",
                    "old-manual-not-a-product", "old-manual-foreign"}
    # price history of deleted listings is gone too
    assert session.query(ListingPrice).count() == 5


def test_purge_disabled_and_no_country_filter(session):
    p = Product(name="PS5", include_keywords=["ps 5"])
    session.add(p)
    add(session, "old-unmatched", days_ago=40)
    add(session, "old-matched-foreign", days_ago=40, product=p, country="RO")
    session.commit()
    assert purge_old_listings(session, 0, {"GR"}) == 0            # 0 = keep forever
    assert purge_old_listings(session, 30, set(), dry_run=True) == 1  # no country filter: foreign matched kept
