"""Background jobs: scrape → upsert → match → recompute stats."""
import logging
import re
import threading
from datetime import timedelta
import traceback

import yaml
from pathlib import Path
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from . import pricing, runtime_settings
from .config import get_settings
from .db import session_scope
from .matching import Matcher
from .models import Listing, ListingPrice, Product, ScrapeRun, VintedUser, utcnow
from .scrapers import SCRAPERS, PoliteClient, RawListing
from .scrapers.base import BACKFILL, DELTA, FULL
from .scrapers import vinted as vinted_scraper

log = logging.getLogger(__name__)
_lock = threading.Lock()  # one scrape at a time
_match_lock = threading.Lock()  # matching from scrape + UI must not overlap


def is_running() -> bool:
    return _lock.locked()


_GREEK = re.compile(r"[α-ωά-ώΑ-ΩΆ-Ώ]")
DOMESTIC_SOURCES = ("insomnia", "vendora")  # Greek-only marketplaces


def resolve_countries(budget: int | None = None) -> int:
    """Fill Listing.country:
    - Insomnia / Vendora are Greek marketplaces -> GR;
    - Vinted title written in Greek -> GR (no lookup needed);
    - other Vinted listings that matched a product -> seller country via the
      users API, cached per seller, newest first, limited per run.
    Returns the number of API lookups made."""
    cfg = get_settings()
    budget = cfg.vinted_user_lookups_per_run if budget is None else budget
    with session_scope() as s:
        s.query(Listing).filter(Listing.source.in_(DOMESTIC_SOURCES), Listing.country.is_(None))             .update({Listing.country: "GR"}, synchronize_session=False)
        pending = (s.query(Listing).filter(Listing.source == "vinted", Listing.country.is_(None))
                   .order_by(Listing.product_id.is_(None), Listing.first_seen.desc()).all())
        cache = {u.id: u for u in s.query(VintedUser).all()}
        need_lookup: list[str] = []
        for l in pending:
            if _GREEK.search(l.title or ""):
                l.country = "GR"
            elif l.seller in cache:
                l.country, l.location = cache[l.seller].country, l.location or cache[l.seller].city
            elif l.seller and l.product_id is not None and l.seller not in need_lookup:
                need_lookup.append(l.seller)
    if not need_lookup or budget <= 0:
        return 0

    client = PoliteClient(delay=1.0)  # small JSON calls
    looked = 0
    try:
        client.get(vinted_scraper.BASE + "/")  # session cookies
        for uid in need_lookup[:budget]:
            res = vinted_scraper.lookup_user(client, uid)
            looked += 1
            if res is None:
                continue
            country, city = res
            with session_scope() as s:
                s.merge(VintedUser(id=uid, country=country, city=city, fetched_at=utcnow()))
                s.query(Listing).filter(Listing.source == "vinted", Listing.seller == uid)                     .update({Listing.country: country, Listing.location: city}, synchronize_session=False)
    finally:
        client.close()
    log.info("vinted: looked up %d sellers (%d still pending)", looked, max(0, len(need_lookup) - looked))
    return looked


def purge_old_listings(session: Session, days: int, countries: set[str] | frozenset[str] = frozenset(),
                       dry_run: bool = False) -> int:
    """Delete listings that are of no use and have not been seen for `days`:
    unmatched ones and ones from sellers outside the allowed countries. Kept:
    anything matched to a product from an allowed/unknown country (price history),
    and every manual decision. Returns how many were (or would be) deleted."""
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    useless = Listing.product_id.is_(None)
    if countries:
        useless = or_(useless, and_(Listing.country.isnot(None), Listing.country.notin_(countries)))
    q = session.query(Listing.id).filter(
        Listing.last_seen < cutoff,
        useless,
        or_(Listing.match_method.is_(None), Listing.match_method != "manual"),
    )
    ids = [i for (i,) in q.all()]
    if dry_run or not ids:
        return len(ids)
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        session.query(ListingPrice).filter(ListingPrice.listing_id.in_(part)).delete(synchronize_session=False)
        session.query(Listing).filter(Listing.id.in_(part)).delete(synchronize_session=False)
    return len(ids)


def run_purge() -> int:
    with session_scope() as s:
        rs = runtime_settings.load(s)
        n = purge_old_listings(s, rs.retention_days, rs.countries)
    if n:
        log.info("purged %d old unmatched/foreign listings (older than %d days)", n, rs.retention_days)
    return n


def close_interrupted_runs(session: Session) -> None:
    """Runs left open by a restart/crash would otherwise show as running forever."""
    for run in session.query(ScrapeRun).filter(ScrapeRun.finished_at.is_(None)):
        run.ok, run.finished_at, run.error = False, utcnow(), "interrupted (app restarted)"


def search_queries(session: Session) -> list[str]:
    seen: dict[str, str] = {}
    for p in session.query(Product).filter(Product.active.is_(True)):
        for q in p.search_queries or []:
            seen.setdefault(q.strip().lower(), q.strip())
    return list(seen.values())


def upsert(session: Session, raw: RawListing) -> tuple[Listing, bool, bool, bool]:
    """Returns (listing, is_new, changed, price_changed); `changed` = price or title
    changed (needs re-matching). Every new price is kept in listing_prices."""
    now = utcnow()
    listing = session.query(Listing).filter_by(source=raw.source, external_id=raw.external_id).one_or_none()
    if listing is None:
        listing = Listing(
            source=raw.source, external_id=raw.external_id, url=raw.url, title=raw.title[:500],
            description=raw.description, price=raw.price, buy_cost=raw.buy_cost, currency=raw.currency,
            image_url=raw.image_url, location=raw.location, seller=raw.seller,
            raw_condition=raw.raw_condition, posted_at=raw.posted_at, is_wanted_ad=raw.is_wanted_ad,
            first_seen=now, last_seen=now, country="GR" if raw.source in DOMESTIC_SOURCES else None,
        )
        session.add(listing)
        if raw.price is not None:
            listing.prices.append(ListingPrice(price=raw.price, seen_at=now))
        return listing, True, False, False

    listing.last_seen = now
    listing.is_active = True
    # the same listing may come from a richer source page later (e.g. search vs feed)
    listing.description = listing.description or raw.description
    listing.posted_at = listing.posted_at or raw.posted_at
    listing.raw_condition = listing.raw_condition or raw.raw_condition
    price_changed = raw.price is not None and raw.price != listing.price
    changed = price_changed
    if price_changed:
        listing.price = raw.price
        listing.buy_cost = raw.buy_cost
        listing.prices.append(ListingPrice(price=raw.price, seen_at=now))
    if raw.title and raw.title != listing.title:
        listing.title = raw.title[:500]
        listing.llm_checked = False
        changed = True
    return listing, False, changed, price_changed


def run_scrape(sources: list[str] | None = None, mode: str | None = None) -> bool:
    """Run one scrape cycle. Returns False if one was already running.
    `mode`: None = automatic (first run of a source: FULL for Insomnia if enabled,
    else BACKFILL; afterwards DELTA), or force DELTA / BACKFILL / FULL."""
    if not _lock.acquire(blocking=False):
        log.info("scrape already running, skipping")
        return False
    try:
        if sources is None:
            with session_scope() as s:
                sources = runtime_settings.load(s).sources
        _run_scrape(sources, mode)
        return True
    finally:
        _lock.release()


def _known_ids(source: str):
    def known(ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with session_scope() as s:
            rows = s.query(Listing.external_id).filter(Listing.source == source, Listing.external_id.in_(ids)).all()
        return {r[0] for r in rows}
    return known


def _pick_mode(source: str, cls, forced: str | None) -> str:
    if forced:
        return FULL if forced == FULL and cls.supports_full else (BACKFILL if forced == FULL else forced)
    with session_scope() as s:
        empty = s.query(Listing.id).filter_by(source=source).first() is None
    if not empty:
        return DELTA
    return FULL if cls.supports_full and get_settings().insomnia_full_initial else BACKFILL


def _run_scrape(sources: list[str], forced_mode: str | None = None) -> None:
    client = PoliteClient()
    try:
        with session_scope() as s:
            queries = search_queries(s)
        for name in sources:
            cls = SCRAPERS.get(name)
            if cls is None:
                log.warning("unknown source %s", name)
                continue
            mode = _pick_mode(name, cls, forced_mode)
            with session_scope() as s:
                run = ScrapeRun(source=name)
                s.add(run)
                s.flush()
                run_id = run.id
            log.info("%s: scraping (%s)", name, mode)
            found = new = repriced = 0
            to_match: list[int] = []
            try:
                # store page by page: survives interruptions, shows progress, and lets DELTA
                # feeds stop as soon as a page holds only listings we already have
                for page in cls(client, known=_known_ids(name)).pages(queries, mode):
                    with session_scope() as s:
                        for raw in page:
                            listing, is_new, changed, price_changed = upsert(s, raw)
                            s.flush()
                            if is_new or changed:
                                to_match.append(listing.id)
                            new += is_new
                            repriced += price_changed
                        found += len(page)
                        run = s.get(ScrapeRun, run_id)
                        run.found, run.new, run.price_changes = found, new, repriced
                with session_scope() as s:
                    run = s.get(ScrapeRun, run_id)
                    run.ok, run.finished_at = True, utcnow()
                log.info("%s: %d listings seen, %d new, %d price changes (%s)", name, found, new, repriced, mode)
            except Exception as e:  # noqa: BLE001 — one broken source must not stop the others
                log.exception("scraper %s failed", name)
                with session_scope() as s:
                    run = s.get(ScrapeRun, run_id)
                    run.ok, run.finished_at = False, utcnow()
                    run.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"[:4000]
            # match whatever was stored, even after a failure half-way
            if to_match:
                try:
                    match_listings(to_match, include_pending=True)  # per source, so results show up early
                except Exception:  # noqa: BLE001
                    log.exception("matching after %s failed", name)
    finally:
        client.close()

    if "vinted" in sources:
        try:
            if resolve_countries():
                match_listings([])  # refresh stats with the newly known countries
        except Exception:  # noqa: BLE001
            log.exception("resolving vinted countries failed")


def match_listings(ids: list[int] | None = None, reset_llm: bool = False, include_pending: bool = False) -> int:
    """Match the given listings (or all non-manual ones) and refresh stats.
    `include_pending` also retries listings the LLM skipped earlier (budget)."""
    cfg = get_settings()
    with _match_lock, session_scope() as s:
        rs = runtime_settings.load(s)
        products = s.query(Product).all()
        matcher = Matcher(products, rs, llm_budget=cfg.llm_max_calls_per_run)
        q = s.query(Listing).filter(or_(Listing.match_method.is_(None), Listing.match_method != "manual"))
        if ids is not None:
            cond = Listing.id.in_(ids)
            if include_pending and matcher.llm:
                cond = or_(cond, Listing.match_note.like("%(llm pending)"))
            q = q.filter(cond)
        n = 0
        # newest first so the LLM budget goes to what the dashboard shows
        for listing in q.order_by(Listing.first_seen.desc()):
            if reset_llm:
                listing.llm_checked = False
            matcher.apply(listing)
            n += 1
        s.flush()
        pricing.recompute_all(s, rs.stats_window_days, rs.countries, rs.sources)
        if n:
            log.info("matched %d listings (%d llm calls)", n, matcher.llm_calls)
        return n


def _seed_fields(item: dict) -> dict:
    return dict(
        category=item.get("category", ""),
        include_keywords=[str(k) for k in item.get("include", [])],
        exclude_keywords=[str(k) for k in item.get("exclude", [])],
        regex=item.get("regex"),
        search_queries=[str(q) for q in item.get("queries", [])],
        min_price=item.get("min_price"),
        max_price=item.get("max_price"),
        target_margin_pct=item.get("target_margin_pct", 20),
        spec_keys=[str(k) for k in item.get("specs", [])],
        spec_filter={str(k): int(v) for k, v in (item.get("require") or {}).items()},
        spec_values={str(k): [int(x) for x in v] for k, v in (item.get("sizes") or {}).items()},
    ) | ({"variant_ranges": {str(k): {"min": v[0], "max": v[1]} for k, v in item["variant_prices"].items()}}
         if item.get("variant_prices") else {})


def load_seed(session: Session, sync: bool = False) -> tuple[int, int]:
    """Load seed_products.yaml. By default only into an empty DB; with
    `sync=True`, add missing products and overwrite the rule fields of existing
    ones (matched by name). Returns (added, updated)."""
    cfg = get_settings()
    paths = [Path(p) for p in cfg.csv(cfg.seed_files) if Path(p).exists()]
    if not paths or (not sync and session.query(Product.id).first() is not None):
        return 0, 0
    data = [item for p in paths for item in (yaml.safe_load(p.read_text(encoding="utf-8")) or [])]
    added = updated = 0
    for item in data:
        p = session.query(Product).filter_by(name=item["name"]).one_or_none()
        if p is None:
            session.add(Product(name=item["name"], **_seed_fields(item)))
            added += 1
        else:
            for k, v in _seed_fields(item).items():
                setattr(p, k, v)
            updated += 1
    log.info("seed: %d added, %d updated", added, updated)
    return added, updated
