from .base import PoliteClient, RawListing, Scraper
from .insomnia import InsomniaScraper
from .vendora import VendoraScraper
from .vinted import VintedScraper

SCRAPERS: dict[str, type[Scraper]] = {
    s.name: s for s in (InsomniaScraper, VendoraScraper, VintedScraper)
}

__all__ = ["SCRAPERS", "PoliteClient", "RawListing", "Scraper"]
