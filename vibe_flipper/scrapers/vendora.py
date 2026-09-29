"""Vendora.gr — uses the ajax results endpoint (same HTML cards as the site),
sorted by most recent, once per product search query."""
from collections.abc import Iterator
from urllib.parse import urlencode

from selectolax.parser import HTMLParser

from ..config import get_settings
from .base import BACKFILL, DELTA, FULL, RawListing, Scraper, parse_price

RESULTS = "https://vendora.gr/ajax/items/results.html"


def results_url(q: str | None = None, category: str | None = None, page: int = 1) -> str:
    params = {"sort": "recent", "page": page}
    if q:
        params["q"] = q
    if category:
        params["category"] = category
    return f"{RESULTS}?{urlencode(params)}"


def parse_listings(html: str) -> list[RawListing]:
    tree = HTMLParser(html)
    out: dict[str, RawListing] = {}
    for card in tree.css("a.card-product"):
        ext_id = card.attributes.get("data-id")
        url = card.attributes.get("href")
        if not ext_id or not url or ext_id in out:
            continue
        title = card.css_first(".title .body-m") or card.css_first(".title")
        price = card.css_first(".subtitle .label-l")
        img = card.css_first(".card-img img")
        out[ext_id] = RawListing(
            source="vendora",
            external_id=ext_id,
            url=url,
            title=title.text(strip=True) if title else "",
            price=parse_price(price.text(strip=True) if price else None),
            image_url=img.attributes.get("src") if img else None,
        )
    return list(out.values())


class VendoraScraper(Scraper):
    name = "vendora"

    def pages(self, queries: list[str], mode: str = DELTA) -> Iterator[list[RawListing]]:
        cfg = get_settings()
        for c in cfg.csv(cfg.vendora_feed_categories):
            yield from self.crawl_feed(lambda page, c=c: results_url(category=c, page=page), parse_listings,
                                       mode if mode != FULL else BACKFILL,
                                       delta_max=cfg.feed_pages, backfill_pages=cfg.feed_backfill_pages)
        pages = 3 if mode != DELTA else 1
        for q in queries:
            for page in range(1, pages + 1):
                items = parse_listings(self.client.get(results_url(q=q, page=page)).text)
                if not items:
                    break
                yield items
