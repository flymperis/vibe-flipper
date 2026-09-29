from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str] = mapped_column(String(100), default="")
    # Each entry is a phrase; a listing matches if ANY phrase appears in its normalized title.
    include_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Optional extra condition, applied to the normalized title.
    regex: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Queries used on search-based sources (Vendora, Vinted).
    search_queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_margin_pct: Mapped[float] = mapped_column(Float, default=20.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Specs that split this product into price variants: subset of ["ram", "storage"].
    spec_keys: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)
    # Match only listings whose extracted spec equals this, e.g. {"storage": 1024} for "SSD 1TB".
    spec_filter: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    # Price range per variant, overriding min/max_price for that variant: {"1TB": {"min": 500, "max": 900}}.
    variant_ranges: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)

    # Cached market stats (recomputed after each scrape).
    market_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_p25: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_p75: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    # {"16GB / 512GB": {"median":..,"mean":..,"p25":..,"p75":..,"count":..}, ...}
    variant_stats: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)
    stats_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listings: Mapped[list["Listing"]] = relationship(back_populates="product")


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30), index=True)
    external_id: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # What a buyer actually pays (e.g. Vinted adds a buyer protection fee).
    buy_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(5), default="EUR")
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)  # ISO code, None = not known yet
    seller: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw_condition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    match_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # rule | llm | manual
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    llm_checked: Mapped[bool] = mapped_column(Boolean, default=False)

    ram_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. "16GB / 512GB"

    condition: Mapped[str | None] = mapped_column(String(30), nullable=True)  # new | like_new | good | fair | unknown
    is_wanted_ad: Mapped[bool] = mapped_column(Boolean, default=False)
    is_broken: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bundle: Mapped[bool] = mapped_column(Boolean, default=False)
    # Manual override: never use this listing in market stats.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped[Product | None] = relationship(back_populates="listings")
    prices: Mapped[list["ListingPrice"]] = relationship(back_populates="listing", cascade="all, delete-orphan")

    @property
    def listed_at(self) -> datetime:
        """When the ad was posted, if the site tells us; otherwise when we first saw it."""
        return self.posted_at or self.first_seen

    @property
    def counts_for_stats(self) -> bool:
        return (
            self.price is not None
            and not (self.is_wanted_ad or self.is_broken or self.is_bundle or self.excluded)
        )


class ListingPrice(Base):
    __tablename__ = "listing_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    price: Mapped[float] = mapped_column(Float)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="prices")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))


class VintedUser(Base):
    """Cache of Vinted seller locations (one API lookup per seller)."""
    __tablename__ = "vinted_users"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
