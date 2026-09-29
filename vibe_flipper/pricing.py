"""Market price statistics and per-listing profit."""
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .matching import specs
from .models import Listing, Product, utcnow


@dataclass
class Stats:
    median: float
    mean: float
    p25: float
    p75: float
    count: int


def _quantile(sorted_vals: list[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def remove_outliers(prices: list[float]) -> list[float]:
    """Tukey fences (1.5×IQR). Needs a few points to be meaningful."""
    vals = sorted(prices)
    if len(vals) < 4:
        return vals
    q1, q3 = _quantile(vals, 0.25), _quantile(vals, 0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [v for v in vals if lo <= v <= hi]


def compute_stats(prices: list[float]) -> Stats | None:
    vals = remove_outliers([p for p in prices if p is not None])
    if not vals:
        return None
    return Stats(
        median=statistics.median(vals),
        mean=statistics.fmean(vals),
        p25=_quantile(vals, 0.25),
        p75=_quantile(vals, 0.75),
        count=len(vals),
    )


def visible_filter(query, countries: set[str] | frozenset[str] = frozenset(), sources: list[str] | None = None):
    """Restrict a Listing query to enabled marketplaces and allowed seller
    countries (empty countries = no restriction). Listings whose country is not
    known yet are left out until resolved."""
    if sources is not None:
        query = query.filter(Listing.source.in_(sources))
    return query.filter(Listing.country.in_(countries)) if countries else query


def eligible_listings(session: Session, product_id: int, since: datetime,
                      countries: set[str] | frozenset[str] = frozenset(),
                      sources: list[str] | None = None) -> list[Listing]:
    q = session.query(Listing).filter(Listing.product_id == product_id, Listing.last_seen >= since,
                                      Listing.price.isnot(None))
    return [l for l in visible_filter(q, countries, sources).all() if l.counts_for_stats]


MIN_VARIANT_SAMPLES = 3


def recompute_all(session: Session, window_days: int, countries: set[str] | frozenset[str] = frozenset(),
                  sources: list[str] | None = None) -> None:
    """Refresh each product's market stats, its listings' variant labels and the
    per-variant stats (e.g. MacBook "8GB / 256GB" vs "16GB / 512GB")."""
    since = utcnow() - timedelta(days=window_days)
    for p in session.query(Product).all():
        keys = [k for k in (p.spec_keys or []) if k in specs.SPEC_KEYS]
        for l in p.listings:
            l.variant = specs.variant_label(keys, l.ram_gb, l.storage_gb)
        eligible = eligible_listings(session, p.id, since, countries, sources)
        st = compute_stats([l.price for l in eligible])
        p.market_median = st.median if st else None
        p.market_mean = st.mean if st else None
        p.market_p25 = st.p25 if st else None
        p.market_p75 = st.p75 if st else None
        p.sample_count = st.count if st else 0

        by_variant: dict[str, list[float]] = {}
        for l in eligible:
            if l.variant:
                by_variant.setdefault(l.variant, []).append(l.price)
        vstats = {}
        for v, prices in by_variant.items():
            vs = compute_stats(prices)
            if vs:
                vstats[v] = {"median": vs.median, "mean": round(vs.mean, 2), "p25": vs.p25, "p75": vs.p75,
                             "count": vs.count}
        p.variant_stats = vstats
        p.stats_updated_at = utcnow()


# When a variant has too few ads of its own, its price is estimated from a
# well-sampled neighbour: +STEP per doubling of RAM/storage. STEP is measured
# from the product's own variants when two of them differ in only that spec.
DEFAULT_STEP = 0.10
MIN_STEP = 0.03  # a bigger disk/RAM never costs less
MAX_STEP = 0.60


@dataclass
class Market:
    price: float
    samples: int
    variant: str | None = None  # variant whose price is used; None = whole product
    assumed: bool = False  # listing's variant unknown -> compared to the cheapest variant
    estimated: bool = False  # sparse variant: estimated from `basis` (+ its own few ads)
    basis: str | None = None


def _steps(keys: list[str], reliable: dict[str, tuple[dict, float]]) -> dict[str, float]:
    """log price change per doubling of each spec, from pairs of reliable variants
    that differ only in that spec (e.g. 128GB vs 256GB, or 8GB/256GB vs 16GB/256GB)."""
    out = {}
    items = list(reliable.values())
    for k in keys:
        slopes = []
        for i, (a, pa) in enumerate(items):
            for b, pb in items[i + 1:]:
                if a[k] != b[k] and all(a[o] == b[o] for o in keys if o != k):
                    slopes.append(math.log(pb / pa) / math.log2(b[k] / a[k]))
        step = statistics.fmean(slopes) if slopes else math.log1p(DEFAULT_STEP)
        out[k] = min(max(step, math.log1p(MIN_STEP)), math.log1p(MAX_STEP))
    return out


def variant_market(product: Product, label: str) -> Market | None:
    """Reference price of one variant of `product`:
    - enough ads of its own -> their median;
    - otherwise estimated from the nearest well-sampled variant, scaled per
      doubling of RAM/storage, blended with its own few ads (if any), and kept
      between the well-sampled variants below and above it."""
    keys = [k for k in (product.spec_keys or []) if k in specs.SPEC_KEYS]
    target = specs.parse_label(keys, label)
    vstats = product.variant_stats or {}
    own = vstats.get(label)
    if own and own["count"] >= MIN_VARIANT_SAMPLES:
        return Market(own["median"], own["count"], label)
    if target is None:
        return None
    reliable = {}
    for v, s in vstats.items():
        vec = specs.parse_label(keys, v)
        if vec and s["count"] >= MIN_VARIANT_SAMPLES and s["median"] > 0:
            reliable[v] = (vec, s["median"])
    if not reliable:
        return Market(own["median"], own["count"], label) if own else None

    steps = _steps(keys, reliable)

    def dist(vec):  # doublings away from the target, over all specs
        return sum(abs(math.log2(target[k] / vec[k])) for k in keys)

    anchor = min(reliable, key=lambda v: (dist(reliable[v][0]), reliable[v][1]))
    vec, price = reliable[anchor]
    est = price * math.exp(sum(steps[k] * math.log2(target[k] / vec[k]) for k in keys))
    n = own["count"] if own else 0
    if n:
        est = (n * own["median"] + (MIN_VARIANT_SAMPLES - n) * est) / MIN_VARIANT_SAMPLES
    below = [p for vec, p in reliable.values() if all(vec[k] <= target[k] for k in keys)]
    above = [p for vec, p in reliable.values() if all(vec[k] >= target[k] for k in keys)]
    lo, hi = max(below, default=None), min(above, default=None)
    if lo is not None and (hi is None or lo <= hi):
        est = max(est, lo)
    if hi is not None and (lo is None or lo <= hi):
        est = min(est, hi)
    samples = n + reliable_count(vstats, anchor)
    return Market(round(est, 2), samples, label, estimated=True, basis=anchor)


def reliable_count(vstats: dict, label: str) -> int:
    return vstats.get(label, {}).get("count", 0)


def market_for(listing: Listing, product: Product) -> Market | None:
    """Pick the reference price for a listing:
    1. its own variant: its median, or an estimate when it has few ads (see variant_market);
    2. variant unknown, on a product with variants -> the cheapest well-sampled
       variant (conservative: "a deal even if it's the base model");
    3. otherwise the whole product's median."""
    if product.spec_keys and listing.variant:
        m = variant_market(product, listing.variant)
        if m:
            return m
    vstats = {v: s for v, s in (product.variant_stats or {}).items() if s["count"] >= MIN_VARIANT_SAMPLES}
    if product.spec_keys and vstats:
        v, s = min(vstats.items(), key=lambda kv: kv[1]["median"])
        return Market(s["median"], s["count"], v, assumed=True)
    if product.market_median is None:
        return None
    return Market(product.market_median, product.sample_count)


@dataclass
class Profit:
    market: float
    cost: float
    profit: float
    margin_pct: float
    is_deal: bool
    low_data: bool
    variant: str | None = None
    assumed: bool = False
    estimated: bool = False
    basis: str | None = None


def listing_profit(listing: Listing, product: Product | None, fee_pct: float, fee_fixed: float,
                   min_samples: int) -> Profit | None:
    """profit = resale at market median minus selling fees, minus what we pay."""
    if product is None or listing.price is None:
        return None
    m = market_for(listing, product)
    if m is None:
        return None
    cost = listing.buy_cost or listing.price
    resale = m.price * (1 - fee_pct / 100) - fee_fixed
    profit = resale - cost
    margin = profit / cost * 100 if cost else 0.0
    usable = not (listing.is_wanted_ad or listing.is_broken)
    return Profit(
        market=m.price,
        cost=cost,
        profit=profit,
        margin_pct=margin,
        is_deal=usable and margin >= product.target_margin_pct,
        low_data=m.samples < (MIN_VARIANT_SAMPLES if m.variant else min_samples),
        variant=m.variant,
        assumed=m.assumed,
        estimated=m.estimated,
        basis=m.basis,
    )


def daily_series(listings: list[Listing], days: int, rolling: int = 7) -> list[dict]:
    """Rolling-window median/p25/p75 per day, by the date each ad was listed."""
    today = utcnow().date()
    pts = sorted((l.listed_at.date(), l.price) for l in listings if l.counts_for_stats)
    out = []
    for i in range(days, -1, -1):
        day = today - timedelta(days=i)
        start = day - timedelta(days=rolling - 1)
        st = compute_stats([p for d, p in pts if start <= d <= day])
        if st:
            out.append({"date": day.isoformat(), "median": round(st.median, 2),
                        "p25": round(st.p25, 2), "p75": round(st.p75, 2), "count": st.count})
    return out
