import re
import threading
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from . import jobs, pricing, runtime_settings
from .config import get_settings
from .db import get_session
from .matching.llm import OllamaClassifier
from .matching.rules import normalize
from .matching.specs import SPEC_KEYS, SPEC_LABELS
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
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline points="{coords}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>')


templates.env.filters["eur"] = eur
templates.env.filters["ago"] = ago
templates.env.globals["SOURCES"] = SOURCES
# cache-busting for static files: /static/style.css?v=<mtime>
templates.env.globals["static_v"] = lambda name: int((HERE / "static" / name).stat().st_mtime)
templates.env.globals["SPEC_KEYS"] = SPEC_KEYS
templates.env.globals["SPEC_LABELS"] = SPEC_LABELS


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
def dashboard(request: Request, source: str = "", category: str = "", product_id: str = "",
              deals: bool = False, sort: str = "new", q: str = "", days: int = 7,
              matched: str = "matched", foreign: bool = False, page: int = 1, s: Session = Depends(get_session)):
    base = (s.query(Listing).options(joinedload(Listing.product))
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

    if matched == "matched" or category or product_id.isdigit():
        rows = matched_rows
        if category:
            rows = [r for r in rows if r["p"].category == category]
        if product_id.isdigit():
            rows = [r for r in rows if r["p"].id == int(product_id)]
    else:
        q_rows = base if matched == "all" else base.filter(Listing.product_id.is_(None))
        rows = _rows(s, q_rows.order_by(Listing.first_seen.desc()).all())
    if deals:
        rows = [r for r in rows if r["profit"] and r["profit"].is_deal]
    if sort in ("margin", "profit"):
        key = "margin_pct" if sort == "margin" else "profit"
        # broken / wanted ads can't be flipped: keep them below everything else
        rows = sorted(rows, key=lambda r: (not (r["l"].is_broken or r["l"].is_wanted_ad) and r["profit"] is not None,
                                           getattr(r["profit"], key) if r["profit"] else 0), reverse=True)
    total = len(rows)
    rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    last_runs = s.query(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(len(SOURCES)).all()
    counts = {
        "listings": s.query(func.count(Listing.id)).scalar(),
        "matched": s.query(func.count(Listing.id)).filter(Listing.product_id.isnot(None)).scalar(),
        "today": s.query(func.count(Listing.id)).filter(Listing.first_seen >= utcnow() - timedelta(days=1)).scalar(),
    }
    f = {"source": source, "category": category, "product_id": product_id, "deals": deals,
         "sort": sort, "q": q, "days": days, "matched": matched, "foreign": foreign}
    cat_products = sorted((p for p in products if not category or p.category == category),
                          key=lambda p: natural_key(p.name))
    return _render(request, "dashboard.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                   products=cat_products, categories=categories, last_runs=last_runs, counts=counts, f=f,
                   tiles=tiles, all_tile=all_tile, countries=runtime_settings.load(s).countries,
                   tile_qs=_tile_qs(f))


def _tile_qs(f: dict) -> str:
    """Query string that keeps the current filters but not category/product/page."""
    from urllib.parse import urlencode
    keep = {k: v for k, v in f.items() if k not in ("category", "product_id") and v not in ("", False, None)}
    if keep.get("matched") == "matched":
        keep.pop("matched")
    return urlencode(keep)


# ---------- products ----------

@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, category: str = "", q: str = "", s: Session = Depends(get_session)):
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
    return _render(request, "products.html", products=products, week_counts=counts, sparks=sparks,
                   categories=categories, f={"category": category, "q": q})


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
    q = s.query(Listing).filter(Listing.product_id == pid, Listing.first_seen >= utcnow() - timedelta(days=days))
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
                   s: Session = Depends(get_session)):
    p = s.get(Product, pid) or _404()
    listings = _product_listings(s, pid, days, variant, foreign)
    categories = sorted({c for (c,) in s.query(Product.category).distinct() if c})
    variants = sorted((p.variant_stats or {}).items(), key=lambda kv: kv[1]["median"])
    unknown = (s.query(func.count(Listing.id)).filter(Listing.product_id == pid, Listing.variant.is_(None)).scalar()
               if p.spec_keys else 0)
    return _render(request, "product.html", p=p, rows=_rows(s, listings), days=days, categories=categories,
                   min_samples=get_settings().min_samples, variants=variants, variant=variant,
                   unknown_variant=unknown, min_variant_samples=pricing.MIN_VARIANT_SAMPLES, foreign=foreign)


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


# ---------- unmatched / listing actions ----------

@router.get("/unmatched", response_class=HTMLResponse)
def unmatched(request: Request, source: str = "", q: str = "", show_rejected: bool = False,
              days: int = 14, page: int = 1, s: Session = Depends(get_session)):
    query = s.query(Listing).filter(Listing.product_id.is_(None), Listing.match_method.is_(None),
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
    products = sorted(s.query(Product).all(), key=lambda p: natural_key(p.name))
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
