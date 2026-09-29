"""Insomnia.gr Marketplace — the "latest adverts" feed (all categories) plus a
classifieds search per product query (sale ads only, newest first)."""
import re
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import urlencode

from selectolax.parser import HTMLParser

from ..config import get_settings
from .base import BACKFILL, DELTA, FULL, RawListing, Scraper, parse_price

BASE = "https://www.insomnia.gr/classifieds/"
_ID_RE = re.compile(r"/classifieds/item/(\d+)-")


def page_url(page: int) -> str:
    return BASE if page <= 1 else f"{BASE}page/{page}/"


def category_url(slug: str, page: int = 1) -> str:
    """A category's sale ads, most recently updated first."""
    params = {"filter": "classifieds_type_1", "sortby": "classifieds_adverts.cl_a_date_updated",
              "sortdirection": "desc"}
    if page > 1:
        params["page"] = page
    return f"{BASE}category/{slug}/?{urlencode(params)}"


def search_url(q: str, page: int = 1) -> str:
    params = {"q": q, "updated_after": "any", "sortby": "newest", "filter": "classifieds_type_1"}
    if page > 1:
        params["page"] = page
    return f"{BASE}search/?{urlencode(params)}"


def _https(src: str | None) -> str | None:
    return "https:" + src if src and src.startswith("//") else src


def parse_listings(html: str) -> list[RawListing]:
    """Cards of the latest-adverts feed and of category pages."""
    tree = HTMLParser(html)
    out: list[RawListing] = []
    for art in tree.css("article.insAdvert, li.insAdvertsList"):
        link = art.css_first("h4 a[href*='/classifieds/item/']")
        if link is None:
            continue
        url = link.attributes.get("href") or ""
        m = _ID_RE.search(url)
        if not m:
            continue
        price_node = art.css_first("p.cFilePrice span.cFilePrice") or art.css_first(".cFilePrice")
        badges = [b.text(strip=True).upper() for b in art.css("li.insAdvTypeBadge")]
        wanted = any("ΖΗΤ" in b for b in badges) or art.css_first("li.insRequest") is not None
        cond = art.css_first("span[data-ipsTooltip-label]")
        img = art.css_first(".classified-product-thumb img")
        seller = art.css_first(".classifieds-data-item-author strong a")
        out.append(
            RawListing(
                source="insomnia",
                external_id=m.group(1),
                url=url,
                title=link.text(strip=True),
                price=parse_price(price_node.text(strip=True) if price_node else None),
                image_url=_https(img.attributes.get("src") if img else None),
                raw_condition=cond.text(strip=True).capitalize() if cond else None,
                is_wanted_ad=wanted,
                seller=seller.text(strip=True) if seller else None,
            )
        )
    return out


def parse_search(html: str) -> list[RawListing]:
    """Cards of /classifieds/search/ results (these also carry a snippet and date)."""
    tree = HTMLParser(html)
    out: list[RawListing] = []
    for card in tree.css("li.insSrCard"):
        link = card.css_first(".insSrCard__title a")
        if link is None:
            continue
        url = link.attributes.get("href") or ""
        m = _ID_RE.search(url)
        if not m:
            continue
        price = card.css_first(".insSrCard__price")
        pill = card.css_first(".insSrCard__pill")
        img = card.css_first("img.insSrCard__img")
        seller = card.css_first(".insSrCard__seller")
        snippet = card.css_first(".insSrCard__snippet")
        time_node = card.css_first("time[datetime]")
        posted = None
        if time_node is not None:
            try:
                posted = datetime.fromisoformat(time_node.attributes["datetime"].replace("Z", "+00:00")).replace(tzinfo=None)
            except (KeyError, ValueError):
                pass
        seller_name = None
        if seller is not None:
            seller_name = seller.text(deep=False, strip=True) or None
        out.append(
            RawListing(
                source="insomnia",
                external_id=m.group(1),
                url=url,
                title=link.text(strip=True),
                price=parse_price(price.text(strip=True) if price else None),
                image_url=_https(img.attributes.get("src") if img else None),
                raw_condition=pill.text(strip=True).capitalize() if pill else None,
                seller=seller_name,
                description=snippet.text(strip=True) if snippet else None,
                posted_at=posted,
            )
        )
    return out


class InsomniaScraper(Scraper):
    name = "insomnia"
    supports_full = True

    def pages(self, queries: list[str], mode: str = DELTA) -> Iterator[list[RawListing]]:
        cfg = get_settings()
        # 1) the "latest adverts" feed covers every category; FULL walks all of it (initial database)
        yield from self.crawl_feed(page_url, parse_listings, mode,
                                   delta_max=cfg.insomnia_pages, backfill_pages=cfg.insomnia_backfill_pages)
        if mode == FULL:
            return
        # 2) category feeds (GPU, CPU, RAM, ...) — deeper than the mixed latest feed
        for slug in cfg.csv(cfg.insomnia_feed_categories):
            yield from self.crawl_feed(lambda page, slug=slug: category_url(slug, page), parse_listings, mode,
                                       delta_max=cfg.feed_pages, backfill_pages=cfg.feed_backfill_pages)
        # 3) product searches; results carry more detail (snippet, date), stored over feed cards
        search_pages = 2 if mode == BACKFILL else 1
        for q in queries:
            for page in range(1, search_pages + 1):
                items = parse_search(self.client.get(search_url(q, page)).text)
                if items:
                    yield items
                if len(items) < 25:
                    break
