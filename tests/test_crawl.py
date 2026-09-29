"""Feed crawling modes, on a fake site (no network)."""
from vibe_flipper.scrapers.base import BACKFILL, DELTA, FULL, RawListing, Scraper


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeSite:
    """Page N holds ids "N-0".."N-3"; past `last` it keeps serving the last page (like Insomnia)."""

    def __init__(self, last: int):
        self.last = last
        self.requested: list[int] = []

    def get(self, url):
        page = int(url)
        self.requested.append(page)
        return FakeResp(str(min(page, self.last)))


def parse(text: str) -> list[RawListing]:
    n = int(text)
    return [RawListing("fake", f"{n}-{i}", f"u{n}-{i}", f"t{n}-{i}", 10.0) for i in range(4)]


def crawl(site, mode, known=frozenset(), delta_max=30, backfill_pages=5):
    scraper = Scraper(site, known=lambda ids: {i for i in ids if i in known})
    pages = list(scraper.crawl_feed(lambda p: str(p), parse, mode, delta_max=delta_max,
                                    backfill_pages=backfill_pages, full_max=100))
    return [p[0].external_id.split("-")[0] for p in pages]


def test_full_walks_to_the_end_and_detects_the_repeat():
    site = FakeSite(last=7)
    assert crawl(site, FULL) == ["1", "2", "3", "4", "5", "6", "7"]
    assert site.requested[-1] == 8  # page 8 repeated page 7 -> stop


def test_delta_stops_at_first_fully_known_page():
    site = FakeSite(last=50)
    known = {f"{p}-{i}" for p in range(4, 51) for i in range(4)}  # pages 1-3 are new
    assert crawl(site, DELTA, known) == ["1", "2", "3", "4"]  # page 4 is all known -> stop after it


def test_delta_reads_one_page_when_nothing_is_new():
    site = FakeSite(last=50)
    known = {f"{p}-{i}" for p in range(1, 51) for i in range(4)}
    assert crawl(site, DELTA, known) == ["1"]


def test_delta_is_capped():
    site = FakeSite(last=500)
    assert len(crawl(site, DELTA, delta_max=30)) == 30  # everything new: stop at the cap


def test_backfill_reads_a_fixed_number_of_pages():
    site = FakeSite(last=50)
    assert crawl(site, BACKFILL, backfill_pages=5) == ["1", "2", "3", "4", "5"]


def test_mode_selection(monkeypatch):
    """Empty DB: Insomnia gets the FULL initial crawl, others BACKFILL; afterwards DELTA."""
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from vibe_flipper import jobs
    from vibe_flipper.db import Base
    from vibe_flipper.models import Listing
    from vibe_flipper.scrapers import SCRAPERS

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def scope():
        s = Session()
        yield s
        s.commit()
        s.close()

    monkeypatch.setattr(jobs, "session_scope", scope)
    ins, ven = SCRAPERS["insomnia"], SCRAPERS["vendora"]
    assert jobs._pick_mode("insomnia", ins, None) == FULL
    assert jobs._pick_mode("vendora", ven, None) == BACKFILL
    assert jobs._pick_mode("vendora", ven, FULL) == BACKFILL  # no full crawl for search-based sites
    with scope() as s:
        s.add(Listing(source="insomnia", external_id="1", url="u", title="t"))
    assert jobs._pick_mode("insomnia", ins, None) == DELTA
    assert jobs._pick_mode("insomnia", ins, FULL) == FULL     # forced from the Settings button
