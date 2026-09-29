"""Vinted — the catalog page is a Next.js app that streams its data inline via
`self.__next_f.push([1, "..."])` chunks; the search results live under
`"items":{"items":[...]}` in the decoded payload. No headless browser needed."""
import json
import logging
import re
from collections.abc import Iterator
from urllib.parse import urlencode

from ..config import get_settings
from .base import BACKFILL, DELTA, FULL, RawListing, Scraper, parse_price

log = logging.getLogger(__name__)

BASE = "https://www.vinted.gr"
_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_ITEMS_KEY = '"items":{"items":['


def catalog_url(q: str, page: int = 1) -> str:
    params = [("search_text", q), ("order", "newest_first"), ("page", page)]
    params += [("catalog[]", c.strip()) for c in get_settings().vinted_catalog_ids.split(",") if c.strip()]
    return f"{BASE}/catalog?{urlencode(params)}"


def feed_url(catalog_id: str, page: int = 1) -> str:
    """Newest items of one catalog (category), no search text."""
    return f"{BASE}/catalog?{urlencode([('order', 'newest_first'), ('catalog[]', catalog_id), ('page', page)])}"


def decode_payload(html: str) -> str:
    parts = []
    for chunk in _CHUNK_RE.findall(html):
        try:
            parts.append(json.loads(f'"{chunk}"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def _amount(d) -> float | None:
    if isinstance(d, dict):
        return parse_price(str(d.get("amount") or ""))
    return None


def parse_listings(html: str) -> list[RawListing]:
    payload = decode_payload(html)
    decoder = json.JSONDecoder()
    out: dict[str, RawListing] = {}
    start = 0
    while (i := payload.find(_ITEMS_KEY, start)) != -1:
        start = i + len(_ITEMS_KEY)
        try:
            arr, _ = decoder.raw_decode(payload, i + len('"items":{"items":'))
        except json.JSONDecodeError:
            continue
        for entry in arr if isinstance(arr, list) else []:
            item = entry.get("productItem") if isinstance(entry, dict) else None
            if not item or not item.get("id") or not item.get("url"):
                continue
            box = item.get("itemBox") or {}
            second = box.get("secondLine")
            ext_id = str(item["id"])
            price = _amount(item.get("priceWithDiscount")) or _amount(item.get("price"))
            out.setdefault(
                ext_id,
                RawListing(
                    source="vinted",
                    external_id=ext_id,
                    url=BASE + item["url"] if item["url"].startswith("/") else item["url"],
                    title=item.get("title") or "",
                    price=price,
                    buy_cost=_amount(item.get("totalItemPrice")) or price,
                    image_url=item.get("thumbnailUrl"),
                    raw_condition=second if isinstance(second, str) and second != "$undefined" else None,
                    seller=str((item.get("user") or {}).get("id") or "") or None,
                ),
            )
    return list(out.values())


class VintedScraper(Scraper):
    name = "vinted"

    def pages(self, queries: list[str], mode: str = DELTA) -> Iterator[list[RawListing]]:
        cfg = get_settings()
        self.client.get(BASE + "/")  # establish session cookies
        for cat in cfg.csv(cfg.vinted_feed_catalogs):
            yield from self.crawl_feed(lambda page, cat=cat: feed_url(cat, page), parse_listings,
                                       mode if mode != FULL else BACKFILL,
                                       delta_max=cfg.feed_pages, backfill_pages=cfg.feed_backfill_pages)
        pages = 2 if mode != DELTA else 1
        for q in queries:
            for page in range(1, pages + 1):
                items = parse_listings(self.client.get(catalog_url(q, page)).text)
                if not items:
                    log.warning("vinted: no items parsed for %r page %d (layout change?)", q, page)
                    break
                yield items


def lookup_user(client, user_id: str) -> tuple[str | None, str | None] | None:
    """Seller (country ISO code, city) from the public users API; None if the
    lookup failed (so it can be retried later)."""
    try:
        r = client.get(f"{BASE}/api/v2/users/{user_id}", headers={"Accept": "application/json"})
        user = r.json().get("user") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("vinted user %s lookup failed: %s", user_id, e)
        return None
    country = (user.get("country_iso_code") or user.get("country_code") or "").upper() or None
    return country, (user.get("city") or None)
