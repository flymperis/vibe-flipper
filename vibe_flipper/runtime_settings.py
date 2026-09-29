"""Settings editable from the UI, stored in the DB, defaulting to env config."""
from dataclasses import dataclass, fields

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Setting

MATCHING_MODES = ("rules", "llm", "hybrid")
# Left empty in the UI -> use the .env value (e.g. OLLAMA_URL added to .env after
# the settings page was first saved with an empty URL).
ENV_IF_EMPTY = {"ollama_url", "ollama_model"}


@dataclass
class RuntimeSettings:
    scrape_interval_minutes: int
    matching_mode: str
    ollama_url: str
    ollama_model: str
    fee_pct: float
    fee_fixed: float
    stats_window_days: int
    allowed_countries: str
    enabled_sources: str
    retention_days: int

    @property
    def llm_enabled(self) -> bool:
        return bool(self.ollama_url) and self.matching_mode in ("llm", "hybrid")

    @property
    def sources(self) -> list[str]:
        """Marketplaces that are scraped and shown; others are ignored everywhere."""
        return [x.strip() for x in self.enabled_sources.split(",") if x.strip()]

    @property
    def countries(self) -> set[str]:
        """Allowed seller countries; empty set = no restriction."""
        return {c.strip().upper() for c in self.allowed_countries.split(",") if c.strip()}


def load(session: Session) -> RuntimeSettings:
    env = get_settings()
    stored = {s.key: s.value for s in session.query(Setting).all()}
    values = {}
    for f in fields(RuntimeSettings):
        raw = stored.get(f.name)
        default = getattr(env, f.name)
        if raw is None or (raw.strip() == "" and f.name in ENV_IF_EMPTY):
            values[f.name] = default
        else:
            try:
                values[f.name] = type(default)(raw)
            except ValueError:
                values[f.name] = default
    if values["matching_mode"] not in MATCHING_MODES:
        values["matching_mode"] = "hybrid"
    return RuntimeSettings(**values)


def save(session: Session, **updates) -> None:
    valid = {f.name for f in fields(RuntimeSettings)}
    for key, value in updates.items():
        if key not in valid:
            continue
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=str(value)))
        else:
            row.value = str(value)
