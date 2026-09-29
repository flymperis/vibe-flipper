from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Static configuration from environment / .env. Runtime-editable values
    (interval, matching mode, fees, Ollama) live in the DB `settings` table and
    fall back to these defaults."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/vibe_flipper.db"
    seed_files: str = "seed_products.yaml,seed_hardware.yaml"

    scrape_interval_minutes: int = 20
    scrape_on_startup: bool = True
    enabled_sources: str = "insomnia,vendora,vinted"
    request_delay_seconds: float = 2.0
    request_timeout_seconds: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
    # Delta runs read newest-first feeds until a page holds only known listings, capped at:
    insomnia_pages: int = 30
    insomnia_backfill_pages: int = 15
    # First run on an empty DB: crawl every page of Insomnia's feed (~19k ads, ~45-60 min).
    insomnia_full_initial: bool = True
    vinted_catalog_ids: str = "2994"  # Ηλεκτρονικά είδη
    # Category feeds (newest first) crawled every run, independent of product queries.
    # Covers whole families (all GPUs / CPUs) without one search per model.
    insomnia_feed_categories: str = "11-kartes-grafikon,9-epexergastes,47-mnimes,89-ssd,10-hdd-optika-mesa,15-ipad"
    vendora_feed_categories: str = "6ljwnv,qrzg1v"  # κονσόλες, εξαρτήματα υπολογιστών
    vinted_feed_catalogs: str = "3602,3599,3603,3607,3728"  # GPU, CPU, RAM, εσωτερική αποθήκευση, τάμπλετ
    feed_pages: int = 10  # cap for category feeds in delta runs
    feed_backfill_pages: int = 12
    vinted_user_lookups_per_run: int = 300
    allowed_countries: str = "GR"  # comma separated ISO codes; empty = all

    matching_mode: str = "hybrid"  # rules | llm | hybrid
    ollama_url: str = ""
    ollama_model: str = "qwen2.5:7b"
    llm_max_calls_per_run: int = 150

    stats_window_days: int = 30
    # Unmatched / foreign listings not seen for this many days are deleted (0 = keep forever).
    retention_days: int = 30
    min_samples: int = 5
    fee_pct: float = 0.0
    fee_fixed: float = 0.0

    @staticmethod
    def csv(value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    @property
    def sources(self) -> list[str]:
        return [s.strip() for s in self.enabled_sources.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
