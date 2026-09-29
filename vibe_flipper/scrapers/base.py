import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


@dataclass
class RawListing:
    source: str
    external_id: str
    url: str
    title: str
    price: float | None
    buy_cost: float | None = None
    currency: str = "EUR"
    image_url: str | None = None
    raw_condition: str | None = None
    is_wanted_ad: bool = False
    location: str | None = None
    seller: str | None = None
    posted_at: datetime | None = None
    description: str | None = None


# Crawl modes
DELTA = "delta"        # regular run: feeds until a page holds nothing new, queries page 1
BACKFILL = "backfill"  # deeper fixed crawl (first run of a source)
FULL = "full"          # every page of the site's feed (initial database; Insomnia)

KnownFn = Callable[[list[str]], set[str]]


class Scraper:
    name: str = ""
    supports_full = False

    def __init__(self, client: "PoliteClient", known: KnownFn | None = None):
        self.client = client
        # external ids already in the DB (checked before each page is stored)
        self.known: KnownFn = known or (lambda ids: set())

    def pages(self, queries: list[str], mode: str = DELTA) -> Iterator[list[RawListing]]:
        """Yield listings one fetched page at a time, so the caller can store them
        as they come. `queries` are product search terms."""
        raise NotImplementedError

    def fetch(self, queries: list[str], mode: str = DELTA) -> list[RawListing]:
        seen: dict[str, RawListing] = {}
        for page in self.pages(queries, mode):
            for it in page:
                seen[it.external_id] = it
        return list(seen.values())

    def crawl_feed(self, url_for_page: Callable[[int], str], parse: Callable[[str], list[RawListing]],
                   mode: str, delta_max: int, backfill_pages: int, full_max: int = 5000,
                   ) -> Iterator[list[RawListing]]:
        """Walk a newest-first feed.
        DELTA: stop after the first page whose listings are all already known.
        BACKFILL: a fixed number of pages. FULL: until the site stops giving new ids
        (Insomnia keeps serving pages past the end, so "nothing new in this crawl" = end)."""
        limit = {DELTA: delta_max, BACKFILL: backfill_pages, FULL: full_max}[mode]
        crawled: set[str] = set()
        for page in range(1, limit + 1):
            items = parse(self.client.get(url_for_page(page)).text)
            ids = [it.external_id for it in items]
            fresh = [i for i in ids if i not in crawled]
            if not items or (mode == FULL and not fresh):
                return
            unknown = set(ids) - self.known(ids) if mode == DELTA else set(ids)
            crawled.update(ids)
            yield items
            if mode == DELTA and not unknown:
                return


class PoliteClient:
    """httpx client with a per-host delay and retry/backoff on 429/5xx."""

    def __init__(self, delay: float | None = None, retries: int = 3):
        cfg = get_settings()
        self.delay = cfg.request_delay_seconds if delay is None else delay
        self.retries = retries
        self._last: dict[str, float] = {}
        self.http = httpx.Client(
            headers={
                "User-Agent": cfg.user_agent,
                "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=cfg.request_timeout_seconds,
            follow_redirects=True,
        )

    def get(self, url: str, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        for attempt in range(self.retries + 1):
            wait = self._last.get(host, 0) + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
            try:
                resp = self.http.get(url, **kwargs)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise
                log.warning("GET %s failed (%s), retrying", url, e)
                time.sleep(self.delay * 2 ** (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.retries:
                    resp.raise_for_status()
                backoff = float(resp.headers.get("Retry-After", 0) or 0) or self.delay * 2 ** (attempt + 2)
                log.warning("GET %s -> %s, backing off %.0fs", url, resp.status_code, backoff)
                time.sleep(min(backoff, 120))
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self.http.close()


_PRICE_RE = re.compile(r"\d[\d.,\s]*")


def parse_price(text: str | None) -> float | None:
    """Parse Greek/EU formatted prices: '€ 1.799', '420 €', '12,50 €', '0.01 €'."""
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    s = m.group(0).strip().replace(" ", "")
    if "," in s and "." in s:
        # whichever comes last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.rsplit(",", 1)[1]) != 3 else s.replace(",", "")
    elif "." in s and len(s.rsplit(".", 1)[1]) == 3:
        s = s.replace(".", "")  # thousands separator: 1.799
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value >= 1 else None  # 0.01 € placeholders are not real prices
