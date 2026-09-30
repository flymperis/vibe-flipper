import re
import threading
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from . import changes, jobs, pricing, runtime_settings
from .config import get_settings
from .db import get_session
from .matching.llm import OllamaClassifier
from .matching.rules import normalize
from .matching.specs import RAM_SIZES, SPEC_KEYS, SPEC_LABELS, STORAGE_SIZES, fmt_gb, parse_label, variant_label
from .scrapers.base import FULL
from .models import Listing, Product, ScrapeRun, utcnow

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
router = APIRouter()
PAGE_SIZE = 50
FLAGS = ("excluded", "is_bundle", "is_broken", "is_wanted_ad")
SOURCES = ("insomnia", "vendora", "vinted")


_NUM = re.compile(r"(\d+)")
_TB = re.compile(r"(\d+)\s*tb", re.I)


def natural_key(name: str) -> list:
    """Alphabetical, but numbers by value and TB in GB: "iPad 9" < "iPad 10", "HDD 500GB" < "HDD 1TB"."""
    s = _TB.sub(lambda m: f"{int(m.group(1)) * 1024}gb", name.casefold())
    return [int(part) if part.isdigit() else part for part in _NUM.split(s)]


def _mount_static(app):
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# ---------- template helpers ----------

def eur(v, signed=False):
    if v is None:
        return "–"
    s = f"{v:+,.0f}" if signed else f"{v:,.0f}"
    return s.replace(",", ".") + " €"


def ago(dt):
    if dt is None:
        return ""
    secs = (utcnow() - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}λ"
    if secs < 86400:
        return f"{int(secs // 3600)}ω"
    return f"{int(secs // 86400)}μ"


def sparkline(points: list[float], w=110, h=26) -> str:
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    step = w / (len(points) - 1)
    coords = " ".join(f"{i * step:.1f},{h - 2 - (p - lo) / span * (h - 4):.1f}" for i, p in enumerate(points))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{coords}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>')


try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Athens")
except Exception:  # noqa: BLE001 — no tz database: show UTC
    LOCAL_TZ = None


def local_dt(dt, fmt="%d/%m %H:%M"):
    """Stored datetimes are naive UTC; show them in Greek time."""
    if dt is None:
        return ""
    if LOCAL_TZ is not None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    return dt.strftime(fmt)


templates.env.filters["eur"] = eur
templates.env.filters["local_dt"] = local_dt
templates.env.filters["ago"] = ago
templates.env.globals["SOURCES"] = SOURCES
# cache-busting for static files: /static/style.css?v=<mtime>
templates.env.globals["static_v"] = lambda name: int((HERE / "static" / name).stat().st_mtime)
templates.env.globals["SPEC_KEYS"] = SPEC_KEYS
templates.env.globals["SPEC_LABELS"] = SPEC_LABELS


def with_query(request: Request, **changes) -> str:
    """The current query string with some parameters replaced (None removes one)."""
    items = [(k, v) for k, v in request.query_params.multi_items() if k not in changes]
    items += [(k, v) for k, v in changes.items() if v is not None]
    return "?" + urlencode(items)


templates.env.globals["with_query"] = with_query
templates.env.filters["gb"] = fmt_gb


def _render(request: Request, name: str, **ctx):
    ctx.setdefault("running", jobs.is_running())
    from .db import session_scope
    with session_scope() as s:
        ctx.setdefault("enabled_sources", runtime_settings.load(s).sources)
    return templates.TemplateResponse(request, name, ctx)


def _rows(session: Session, listings: list[Listing]) -> list[dict]:
    rs = runtime_settings.load(session)
    min_samples = get_settings().min_samples
    return [{"l": l, "p": l.product, "profit": pricing.listing_profit(l, l.product, rs.fee_pct, rs.fee_fixed, min_samples)}
            for l in listings]


def _add_was(s: Session, rows: list[dict]) -> list[dict]:
    """Attach the original price to rows whose price changed since first seen."""
    first = changes.first_prices(s, [r["l"].id for r in rows])
    for r in rows:
        was = first.get(r["l"].id)
        r["was"] = was if was is not None and was != r["l"].price else None
    return rows


def _background(fn, *args, session: Session | None = None, **kwargs):
    """Run fn in a thread, after committing the request's pending changes."""
    if session is not None:
        session.commit()
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def _redirect(url: str, session: Session | None = None) -> RedirectResponse:
    """Commit before redirecting: the dependency's own commit runs after the
    response is sent, so the next page could otherwise read stale data."""
    if session is not None:
        session.commit()
    return RedirectResponse(url, status_code=303)


def _back(request: Request, default: str = "/", session: Session | None = None):
    return _redirect(request.headers.get("referer") or default, session)


# Filters are remembered per page in a cookie: opening the page without a query
# string restores the last one, "?reset=1" clears it.
STICKY_MAX_AGE = 365 * 86400


def _restore_filters(request: Request, cookie: str) -> RedirectResponse | None:
    if not request.query_params and (saved := request.cookies.get(cookie)):
        return RedirectResponse(f"{request.url.path}?{unquote(saved)}", status_code=303)
    return None


def _remember_filters(request: Request, response, cookie: str):
    if "reset" in request.query_params:
        response.delete_cookie(cookie)
    else:
        qs = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "page"])
        if qs:  # stored percent-encoded: "=" and "&" are not valid in a cookie value
            response.set_cookie(cookie, quote(qs, safe=""), max_age=STICKY_MAX_AGE, samesite="lax", httponly=True)
    return response


def _visible_listings(query):
    """Hide wanted ads ("ζητείται") and listings of disabled products from the tables."""
    return query.filter(Listing.is_wanted_ad.isnot(True),
                        or_(Listing.product_id.is_(None), Listing.product.has(Product.active.is_(True))))


# ---------- sorting of listing tables ----------

# sort value -> label of the sort menu
SORTS = {
    "new": "Νεότερες", "old": "Παλαιότερες",
    "price": "Φθηνότερες", "price_desc": "Ακριβότερες",
    "margin": "Περιθώριο % ↓", "margin_asc": "Περιθώριο % ↑",
    "profit": "Κέρδος € ↓", "profit_asc": "Κέρδος € ↑",
    "market": "Αγοραία ↓", "market_asc": "Αγοραία ↑",
    "product": "Προϊόν Α→Ω",
}
# table column -> (first click, second click)
SORT_COLUMNS = {"listing": ("new", "old"), "product": ("product", "product"), "price": ("price", "price_desc"),
                "market": ("market", "market_asc"), "profit": ("profit", "profit_asc"),
                "margin": ("margin", "margin_asc")}
ASC_SORTS = {"old", "price", "market_asc", "profit_asc", "margin_asc", "product"}
_PROFIT_ATTR = {"margin": "margin_pct", "profit": "profit", "market": "market"}
templates.env.globals.update(SORTS=SORTS, SORT_COLUMNS=SORT_COLUMNS, ASC_SORTS=ASC_SORTS)


def sort_rows(rows: list[dict], sort: str) -> list[dict]:
    """Sort listing rows ({"l", "p", "profit"}) by one of SORTS; unknown values = newest first."""
    column, _, direction = sort.partition("_")
    if column in _PROFIT_ATTR:
        attr = _PROFIT_ATTR[column]
        # broken / wanted ads and listings without a market price can't be flipped: always last
        ok = [r for r in rows if r["profit"] is not None and not (r["l"].is_broken or r["l"].is_wanted_ad)]
        rest = [r for r in rows if r["profit"] is None or r["l"].is_broken or r["l"].is_wanted_ad]
        return sorted(ok, key=lambda r: getattr(r["profit"], attr), reverse=direction != "asc") + rest
    if column == "price":
        sign = -1 if direction == "desc" else 1
        return sorted(rows, key=lambda r: (r["l"].price is None, sign * (r["l"].price or 0)))
    if column == "product":
        return sorted(rows, key=lambda r: (r["p"] is None, natural_key(r["p"].name) if r["p"] else [],
                                           r["l"].price or 0))
    return sorted(rows, key=lambda r: r["l"].first_seen, reverse=sort != "old")


# ---------- dashboard ----------

# Tile order / icons for known categories; unknown categories follow alphabetically.
CATEGORY_ORDER = ["GPU", "CPU", "RAM", "NVMe", "SSD", "HDD", "iPhone", "iPad", "Samsung", "MacBook", "Mac",
                  "PlayStation", "Xbox", "Nintendo",
                  "Handheld PC", "VR", "Ακουστικά", "Smartwatch", "Drones & Κάμερες"]
CATEGORY_ICONS = {"GPU": "🖥️", "CPU": "🧠", "RAM": "🧩", "NVMe": "⚡", "SSD": "💾", "HDD": "🗄️", "iPhone": "📱", "iPad": "📲", "Samsung": "📱", "MacBook": "💻", "Mac": "🖥️",
                  "PlayStation": "🎮", "Xbox": "🎮", "Nintendo": "🕹️", "Handheld PC": "🕹️", "VR": "🥽",
                  "Ακουστικά": "🎧", "Smartwatch": "⌚", "Drones & Κάμερες": "📷"}


def _category_tiles(rows: list[dict], categories: list[str]) -> list[dict]:
    """Per-category counts of listings and deals, for the dashboard tiles."""
    stats = {c: {"count": 0, "deals": 0} for c in categories}
    for r in rows:
        c = r["p"].category if r["p"] else None
        if c in stats:
            stats[c]["count"] += 1
            stats[c]["deals"] += bool(r["profit"] and r["profit"].is_deal)
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    return [{"name": c, "icon": CATEGORY_ICONS.get(c, "📦"), **stats[c]}
            for c in sorted(categories, key=lambda c: (order.get(c, len(order)), c))]


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, source: str = "", category: list[str] = Query([]), product_id: str = "",
              deals: bool = False, sort: str = "new", q: str = "", days: int = 7,
              matched: str = "matched", foreign: bool = False, page: int = 1, s: Session = Depends(get_session)):
    if redirect := _restore_filters(request, "f_dash"):
        return redirect
    cats = [c for c in dict.fromkeys(category) if c]  # selected category tiles (none = all)
    base = _visible_listings(s.query(Listing).options(joinedload(Listing.product))
                             .filter(Listing.first_seen >= utcnow() - timedelta(days=days)))
    if not foreign:
        rs = runtime_settings.load(s)
        base = pricing.visible_filter(base, rs.countries, rs.sources)
    if source:
        base = base.filter(Listing.source == source)
    if q:
        base = base.filter(Listing.title.ilike(f"%{q}%"))

    # tiles: every matched listing of the window, before category/product/deal filters
    matched_rows = _rows(s, base.filter(Listing.product_id.isnot(None)).order_by(Listing.first_seen.desc()).all())
    products = s.query(Product).order_by(Product.category, Product.name).all()
    categories = sorted({p.category for p in products if p.category})
    tiles = _category_tiles(matched_rows, sorted({p.category for p in products if p.category and p.active}))
    all_tile = {"count": len(matched_rows),
                "deals": sum(1 for r in matched_rows if r["profit"] and r["profit"].is_deal)}

    if matched == "matched" or cats or product_id.isdigit():
        rows = matched_rows
        if cats:
            rows = [r for r in rows if r["p"].category in cats]
        if product_id.isdigit():
            rows = [r for r in rows if r["p"].id == int(product_id)]
    else:
        q_rows = base if matched == "all" else base.filter(Listing.product_id.is_(None))
        rows = _rows(s, q_rows.order_by(Listing.first_seen.desc()).all())
    if deals:
        rows = [r for r in rows if r["profit"] and r["profit"].is_deal]
    rows = sort_rows(rows, sort)
    total = len(rows)
    rows = _add_was(s, rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])

    last_runs = s.query(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(len(SOURCES)).all()
    counts = {
        "listings": s.query(func.count(Listing.id)).scalar(),
        "matched": s.query(func.count(Listing.id)).filter(Listing.product_id.isnot(None)).scalar(),
        "today": s.query(func.count(Listing.id)).filter(Listing.first_seen >= utcnow() - timedelta(days=1)).scalar(),
    }
    f = {"source": source, "category": cats, "product_id": product_id, "deals": deals,
         "sort": sort, "q": q, "days": days, "matched": matched, "foreign": foreign}
    cat_products = sorted((p for p in products if p.active and (not cats or p.category in cats)),
                          key=lambda p: natural_key(p.name))
    resp = _render(request, "dashboard.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                   products=cat_products, categories=categories, last_runs=last_runs, counts=counts, f=f,
                   tiles=tiles, all_tile=all_tile, countries=runtime_settings.load(s).countries,
                   tile_qs=_tile_qs(f), tile_href=lambda name: _tile_href(_tile_qs(f), cats, name))
    return _remember_filters(request, resp, "f_dash")


def _tile_href(qs: str, selected: list[str], name: str) -> str:
    """Link of a category tile: toggles `name` in the selected categories and keeps
    the other filters. `qs` is never empty (days/sort), so "/" + saved filters is
    not restored by mistake when the last tile is switched off."""
    cats = [c for c in selected if c != name] if name in selected else [*selected, name]
    return "/?" + "&".join([qs, *(urlencode({"category": c}) for c in cats)] if qs else
                           [urlencode({"category": c}) for c in cats])


def _tile_qs(f: dict) -> str:
    """Query string that keeps the current filters but not category/product/page."""
    keep = {k: v for k, v in f.items() if k not in ("category", "product_id") and v not in ("", False, None)}
    if keep.get("matched") == "matched":
        keep.pop("matched")
    return urlencode(keep)


# ---------- products ----------

@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, category: str = "", q: str = "", s: Session = Depends(get_session)):
    if redirect := _restore_filters(request, "f_products"):
        return redirect
    categories = sorted({c for (c,) in s.query(Product.category).distinct() if c})
    query = s.query(Product)
    if category:
        query = query.filter(Product.category == category)
    if q:
        query = query.filter(Product.name.ilike(f"%{q}%"))
    products = query.order_by(Product.category, Product.name).all()
    since = utcnow() - timedelta(days=30)
    counts = dict(s.query(Listing.product_id, func.count(Listing.id))
                  .filter(Listing.product_id.isnot(None), Listing.first_seen >= utcnow() - timedelta(days=7))
                  .group_by(Listing.product_id).all())
    sparks = {}
    for p in products:
        ls = s.query(Listing).filter(Listing.product_id == p.id, Listing.first_seen >= since).all()
        sparks[p.id] = sparkline([pt["median"] for pt in pricing.daily_series(ls, 30)])
    resp = _render(request, "products.html", products=products, week_counts=counts, sparks=sparks,
                   categories=categories, f={"category": category, "q": q})
    return _remember_filters(request, resp, "f_products")


def _product_from_form(p: Product, form) -> None:
    def lines(v: str) -> list[str]:
        return [x.strip() for x in v.replace(",", "\n").splitlines() if x.strip()]

    def num(v: str):
        v = (v or "").strip().replace(",", ".")
        return float(v) if v else None

    p.name = form["name"].strip()
    p.category = form.get("category", "").strip()
    p.include_keywords = lines(form.get("include_keywords", ""))
    p.exclude_keywords = lines(form.get("exclude_keywords", ""))
    p.regex = form.get("regex", "").strip() or None
    p.search_queries = lines(form.get("search_queries", ""))
    p.min_price = num(form.get("min_price", ""))
    p.max_price = num(form.get("max_price", ""))
    p.target_margin_pct = num(form.get("target_margin_pct", "")) or 20.0
    p.active = form.get("active") == "on"
    p.spec_keys = [k for k in SPEC_KEYS if form.get(f"spec_{k}") == "on"]
    p.spec_filter = {k: int(v) for k in SPEC_KEYS if (v := (form.get(f"require_{k}") or "").strip()).isdigit()}


@router.get("/products/new", response_class=HTMLResponse)
def product_new(request: Request, title: str = "", s: Session = Depends(get_session)):
    p = Product(name="", category="", include_keywords=[normalize(title)] if title else [], exclude_keywords=[],
                search_queries=[], target_margin_pct=20.0, active=True, spec_keys=[], spec_filter={})
    categories = sorted({c for (c,) in s.query(Product.category).distinct() if c})
    return _render(request, "product_form.html", p=p, categories=categories, is_new=True)


@router.post("/products/new")
async def product_create(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    p = Product()
    _product_from_form(p, form)
    if not p.name or s.query(Product).filter_by(name=p.name).first():
        raise HTTPException(400, "Χρειάζεται μοναδικό όνομα")
    s.add(p)
    s.flush()
    _background(jobs.match_listings, session=s)
    return _redirect(f"/products/{p.id}", s)


@router.post("/products/active")
async def products_set_active(request: Request, s: Session = Depends(get_session)):
    """Bulk enable/disable from the products list checkboxes. `ids` = products shown
    on the page, `active` = the ticked ones; one re-match for all changes."""
    form = await request.form()
    shown = {int(x) for x in form.getlist("ids") if x.isdigit()}
    ticked = {int(x) for x in form.getlist("active") if x.isdigit()}
    changed = 0
    for p in s.query(Product).filter(Product.id.in_(shown)):
        if p.active != (p.id in ticked):
            p.active = p.id in ticked
            changed += 1
    if changed:
        _background(jobs.match_listings, session=s)
    return _back(request, "/products", s)


def _product_listings(s: Session, pid: int, days: int, variant: str, foreign: bool = False) -> list[Listing]:
    q = s.query(Listing).filter(Listing.product_id == pid, Listing.first_seen >= utcnow() - timedelta(days=days),
                                Listing.is_wanted_ad.isnot(True))
    if not foreign:
        rs = runtime_settings.load(s)
        q = pricing.visible_filter(q, rs.countries, rs.sources)
    if variant == "?":
        q = q.filter(Listing.variant.is_(None))
    elif variant:
        q = q.filter(Listing.variant == variant)
    return q.order_by(Listing.first_seen.desc()).all()


@router.get("/products/{pid}", response_class=HTMLResponse)
def product_detail(request: Request, pid: int, days: int = 90, variant: str = "", foreign: bool = False,
                   sort: str = "new", s: Session = Depends(get_session)):
    p = s.get(Product, pid) or _404()
    listings = _product_listings(s, pid, days, variant, foreign)
    categories = sorted({c for (c,) in s.query(Product.category).distinct() if c})
    variants = variant_table(p)
    unknown = (s.query(func.count(Listing.id)).filter(Listing.product_id == pid, Listing.variant.is_(None)).scalar()
               if p.spec_keys else 0)
    return _render(request, "product.html", p=p, rows=_add_was(s, sort_rows(_rows(s, listings), sort)), days=days,
                   categories=categories, sort=sort,
                   min_samples=get_settings().min_samples, variants=variants, variant=variant,
                   selected=next((v for v in variants if v["label"] == variant), None),
                   unknown_variant=unknown, min_variant_samples=pricing.MIN_VARIANT_SAMPLES, foreign=foreign,
                   size_options={"ram": sorted(RAM_SIZES), "storage": sorted(STORAGE_SIZES)})


def variant_table(p: Product) -> list[dict]:
    """Every variant with ads or with its own price range: stats, the market price
    used for its listings (measured or estimated) and the range. Sorted by specs."""
    keys = [k for k in (p.spec_keys or []) if k in SPEC_KEYS]
    stats, ranges = p.variant_stats or {}, p.variant_ranges or {}
    rows = []
    for label in set(stats) | set(ranges):
        vec = parse_label(keys, label)
        if vec is None:
            continue
        rows.append({"label": label, "st": stats.get(label), "range": ranges.get(label),
                     "market": pricing.variant_market(p, label), "key": [vec[k] for k in keys]})
    return sorted(rows, key=lambda r: r["key"])


@router.post("/products/{pid}/ranges")
async def product_ranges(request: Request, pid: int, s: Session = Depends(get_session)):
    """Save per-variant price ranges (rows `range_min_<label>` / `range_max_<label>`,
    plus an optional new row built from the `new_<key>` selects)."""
    p = s.get(Product, pid) or _404()
    form = await request.form()
    keys = [k for k in (p.spec_keys or []) if k in SPEC_KEYS]

    def num(v):
        v = (v or "").strip().replace(",", ".")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    ranges = {}
    for label in form.getlist("label"):
        lo, hi = num(form.get(f"range_min_{label}")), num(form.get(f"range_max_{label}"))
        if parse_label(keys, label) and (lo is not None or hi is not None):
            ranges[label] = {"min": lo, "max": hi}
    new = {k: form.get(f"new_{k}", "") for k in keys}
    if keys and all(v.isdigit() for v in new.values()):
        label = variant_label(keys, int(new["ram"]) if "ram" in new else None,
                              int(new["storage"]) if "storage" in new else None)
        lo, hi = num(form.get("new_min")), num(form.get("new_max"))
        if label and (lo is not None or hi is not None):
            ranges[label] = {"min": lo, "max": hi}
    p.variant_ranges = ranges
    _background(jobs.match_listings, session=s)
    return _redirect(f"/products/{pid}", s)


@router.post("/products/{pid}")
async def product_update(request: Request, pid: int, s: Session = Depends(get_session)):
    p = s.get(Product, pid) or _404()
    _product_from_form(p, await request.form())
    _background(jobs.match_listings, session=s)
    return _redirect(f"/products/{pid}", s)


@router.post("/products/{pid}/toggle")
def product_toggle(request: Request, pid: int, s: Session = Depends(get_session)):
    """Enable / disable a product: disabled products are not searched or matched."""
    p = s.get(Product, pid) or _404()
    p.active = not p.active
    _background(jobs.match_listings, session=s)
    return _back(request, "/products", s)


@router.post("/products/{pid}/delete")
def product_delete(pid: int, s: Session = Depends(get_session)):
    p = s.get(Product, pid) or _404()
    s.query(Listing).filter(Listing.product_id == pid).update(
        {Listing.product_id: None, Listing.match_method: None, Listing.llm_checked: False})
    s.delete(p)
    return _redirect("/products", s)


@router.get("/api/products/{pid}/chart")
def product_chart(pid: int, days: int = 90, variant: str = "", foreign: bool = False,
                  s: Session = Depends(get_session)):
    s.get(Product, pid) or _404()
    listings = _product_listings(s, pid, days, variant, foreign)
    points = [{"x": l.listed_at.isoformat(), "y": l.price, "source": l.source, "title": l.title,
               "variant": l.variant,
               "url": l.url, "used": l.counts_for_stats} for l in listings if l.price is not None]
    return JSONResponse({"points": points, "series": pricing.daily_series(listings, days)})


# ---------- price changes ----------

@router.get("/price-changes", response_class=HTMLResponse)
def price_changes_page(request: Request, days: int = 7, direction: str = "all", category: str = "",
                       source: str = "", matched: str = "all", sort: str = "new", deals: bool = False,
                       page: int = 1, s: Session = Depends(get_session)):
    """Listings whose price changed between scrapes, grouped by the scrape that saw it."""
    if redirect := _restore_filters(request, "f_changes"):
        return redirect
    rs = runtime_settings.load(s)
    min_samples = get_settings().min_samples

    def narrow(q):
        q = _visible_listings(pricing.visible_filter(q, rs.countries, rs.sources))
        if source:
            q = q.filter(Listing.source == source)
        if matched == "matched" or category:
            q = q.filter(Listing.product_id.isnot(None))
        if category:
            q = q.filter(Listing.product.has(Product.category == category))
        return q

    since = utcnow() - timedelta(days=days)
    events = changes.price_changes(s, since, narrow)
    if direction == "down":
        events = [e for e in events if e.diff < 0]
    elif direction == "up":
        events = [e for e in events if e.diff > 0]
    rows = []
    for e in events:
        profit = pricing.listing_profit(e.listing, e.listing.product, rs.fee_pct, rs.fee_fixed, min_samples)
        rows.append({"c": e, "l": e.listing, "p": e.listing.product, "profit": profit})
    if deals:
        rows = [r for r in rows if r["profit"] and r["profit"].is_deal]
    if sort == "pct":
        rows.sort(key=lambda r: r["c"].pct)
    elif sort == "eur":
        rows.sort(key=lambda r: r["c"].diff)
    summary = {"down": sum(1 for r in rows if r["c"].diff < 0), "up": sum(1 for r in rows if r["c"].diff > 0),
               "deals": sum(1 for r in rows if r["profit"] and r["profit"].is_deal)}
    total = len(rows)
    rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    # the scrape run that saw each change (same source, started before it)
    runs = (s.query(ScrapeRun).filter(ScrapeRun.started_at >= since - timedelta(hours=2))
            .order_by(ScrapeRun.started_at.desc()).all())
    last_start = {}
    for run in runs:
        last_start.setdefault(run.source, run.started_at)
    for r in rows:
        c = r["c"]
        r["run"] = next((run for run in runs if run.source == r["l"].source and run.started_at <= c.at), None)
        r["latest"] = c.at >= last_start.get(r["l"].source, c.at)
    groups: list[dict] = []
    for r in rows:
        key = r["run"].id if (sort == "new" and r["run"]) else None
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "run": r["run"] if sort == "new" else None, "rows": []})
        groups[-1]["rows"].append(r)

    categories = sorted({c for (c,) in s.query(Product.category).filter(Product.active.is_(True)).distinct() if c})
    f = {"days": days, "direction": direction, "category": category, "source": source, "matched": matched,
         "sort": sort, "deals": deals}
    resp = _render(request, "price_changes.html", groups=groups, total=total, page=page,
                   pages=max(1, -(-total // PAGE_SIZE)), summary=summary, categories=categories, f=f)
    return _remember_filters(request, resp, "f_changes")


# ---------- unmatched / listing actions ----------

@router.get("/unmatched", response_class=HTMLResponse)
def unmatched(request: Request, source: str = "", q: str = "", show_rejected: bool = False,
              days: int = 14, page: int = 1, s: Session = Depends(get_session)):
    query = s.query(Listing).filter(Listing.product_id.is_(None), Listing.match_method.is_(None),
                                    Listing.is_wanted_ad.isnot(True),
                                    Listing.first_seen >= utcnow() - timedelta(days=days))
    # Vinted countries are only resolved for matched listings, so unknown is allowed here
    rs = runtime_settings.load(s)
    query = query.filter(Listing.source.in_(rs.sources))
    if rs.countries:
        query = query.filter(or_(Listing.country.is_(None), Listing.country.in_(rs.countries)))
    if source:
        query = query.filter(Listing.source == source)
    if q:
        query = query.filter(Listing.title.ilike(f"%{q}%"))
    if not show_rejected:
        query = query.filter(or_(Listing.match_note.is_(None),
                                 ~(Listing.match_note.like("price outside%") | Listing.match_note.like("accessory%"))))
    total = query.count()
    listings = query.order_by(Listing.first_seen.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    products = sorted(s.query(Product).filter(Product.active.is_(True)), key=lambda p: natural_key(p.name))
    return _render(request, "unmatched.html", listings=listings, products=products, total=total, page=page,
                   pages=max(1, -(-total // PAGE_SIZE)),
                   f={"source": source, "q": q, "show_rejected": show_rejected, "days": days})


@router.post("/listings/{lid}/assign")
def listing_assign(request: Request, lid: int, product_id: str = Form(""), s: Session = Depends(get_session)):
    l = s.get(Listing, lid) or _404()
    if product_id == "auto":
        l.match_method, l.llm_checked = None, False
        s.flush()
        _background(jobs.match_listings, [lid], session=s)
    else:
        l.product_id = int(product_id) if product_id.isdigit() else None
        l.match_method = "manual"
        l.match_confidence = 1.0
        l.match_note = "manual"
        _background(jobs.match_listings, [], session=s)  # refresh stats
    return _back(request)


@router.post("/listings/{lid}/toggle/{flag}")
def listing_toggle(request: Request, lid: int, flag: str, s: Session = Depends(get_session)):
    if flag not in FLAGS:
        raise HTTPException(400)
    l = s.get(Listing, lid) or _404()
    setattr(l, flag, not getattr(l, flag))
    l.manual_flags = {**(l.manual_flags or {}), flag: getattr(l, flag)}  # re-matching keeps it
    _background(jobs.match_listings, [], session=s)
    return _back(request)


# ---------- settings / jobs ----------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_session)):
    rs = runtime_settings.load(s)
    runs = s.query(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(30).all()
    llm_status = OllamaClassifier(rs.ollama_url, rs.ollama_model).ping() if rs.ollama_url else (None, "δεν έχει οριστεί")
    country_counts = (s.query(Listing.country, func.count(Listing.id)).filter(Listing.source == "vinted")
                      .group_by(Listing.country).order_by(func.count(Listing.id).desc()).all())
    pending_matched = (s.query(func.count(Listing.id))
                       .filter(Listing.source == "vinted", Listing.country.is_(None), Listing.product_id.isnot(None))
                       .scalar())
    purgeable = jobs.purge_old_listings(s, rs.retention_days, rs.countries, dry_run=True)
    return _render(request, "settings.html", rs=rs, runs=runs, llm_status=llm_status, purgeable=purgeable,
                   modes=runtime_settings.MATCHING_MODES, country_counts=country_counts,
                   pending_matched=pending_matched)


@router.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    before = runtime_settings.load(s)
    values = {k: v for k, v in form.items()}
    values["enabled_sources"] = ",".join(src for src in SOURCES if form.get(f"source_{src}") == "on")
    runtime_settings.save(s, **values)
    s.flush()
    after = runtime_settings.load(s)
    if after.scrape_interval_minutes != before.scrape_interval_minutes:
        from .main import reschedule
        reschedule(after.scrape_interval_minutes)
    if ((after.fee_pct, after.fee_fixed, after.stats_window_days, after.countries, after.sources)
            != (before.fee_pct, before.fee_fixed, before.stats_window_days, before.countries, before.sources)):
        pricing.recompute_all(s, after.stats_window_days, after.countries, after.sources)
    return _redirect("/settings", s)


@router.post("/scrape")
def scrape_now(request: Request):
    if not jobs.is_running():
        _background(jobs.run_scrape)
    return _back(request)


@router.post("/scrape/full")
def scrape_full(request: Request):
    """Full crawl of Insomnia (every page) in the background — the initial-database option."""
    if not jobs.is_running():
        _background(jobs.run_scrape, ["insomnia"], FULL)
    return _back(request, "/settings")


@router.post("/purge")
def purge_now(request: Request):
    _background(jobs.run_purge)
    return _back(request, "/settings")


@router.post("/rematch")
def rematch(request: Request, reset_llm: bool = Form(False)):
    _background(jobs.match_listings, None, reset_llm)
    return _back(request, "/settings")


@router.get("/status")
def status(s: Session = Depends(get_session)):
    last = s.query(ScrapeRun).order_by(ScrapeRun.id.desc()).first()
    return {"running": jobs.is_running(), "last_run": last.finished_at.isoformat() if last and last.finished_at else None}


@router.get("/healthz")
def healthz():
    return {"ok": True}


def _404():
    raise HTTPException(404)
