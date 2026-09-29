"""Decides a listing's product using rules, the LLM, or both (per matching_mode).

hybrid: confident rule match wins; otherwise the LLM decides (ambiguous titles,
        low-confidence rule hits, or titles sharing words with some product).
llm:    the LLM decides everything it can (falls back to rules if unavailable).
rules:  rules only.
Manual assignments are never touched. LLM answers are cached per listing
(`llm_checked`) so each listing costs at most one call."""
import logging

from ..models import Listing, Product
from ..runtime_settings import RuntimeSettings
from . import rules as R
from . import specs
from .llm import OllamaClassifier

log = logging.getLogger(__name__)

# Site condition labels -> our condition scale
CONDITION_MAP = {
    "νεο με ετικετεσ": "new", "νεο χωρισ ετικετεσ": "like_new", "καινουργιο": "new",
    "πολυ καλο": "like_new", "καλο": "good", "ικανοποιητικο": "fair", "μεταχειρισμενο": "unknown",
}
_GENERIC = {"edition", "digital", "pro", "max", "mini", "plus", "slim", "gb", "tb", "the", "and", "with",
            "series", "new", "lite", "ti", "super", "oled", "air"}


def map_condition(raw: str | None) -> str | None:
    if not raw:
        return None
    return CONDITION_MAP.get(R.normalize(raw), "unknown")


class Matcher:
    def __init__(self, products: list[Product], rs: RuntimeSettings, llm_budget: int = 0,
                 classifier: OllamaClassifier | None = None):
        active = [p for p in products if p.active]
        self.rules = [R.ProductRule.from_product(p) for p in active]
        self.by_id = {r.id: r for r in self.rules}
        self.catalog = [(p.id, p.name, p.category) for p in active]
        self.mode = rs.matching_mode
        self.llm = classifier or (OllamaClassifier(rs.ollama_url, rs.ollama_model) if rs.llm_enabled else None)
        self.llm_budget = llm_budget
        self.llm_calls = 0
        self.vocab = self._vocab(active)

    @staticmethod
    def _vocab(products: list[Product]) -> set[str]:
        words: set[str] = set()
        for p in products:
            for phrase in [p.name, *(p.include_keywords or [])]:
                for w in R.normalize(phrase.rstrip("*")).split():
                    if len(w) >= 3 and w not in _GENERIC:
                        words.add(w)
        return words

    def _worth_llm(self, listing: Listing, rr: R.RuleResult) -> bool:
        if self.mode == "llm":
            return True
        if rr.confident:
            return False
        if rr.candidates:  # ambiguous or low-confidence rule hit
            return True
        if rr.price_rejected or rr.note.startswith("accessory"):  # clearly not a product -> skip
            return False
        return bool(self.vocab & set(R.normalize(listing.title).split()))

    def apply(self, listing: Listing) -> None:
        found = specs.extract(f"{listing.title} {listing.description or ''}")
        if listing.llm_checked:  # keep what the LLM read when the text parser finds nothing
            listing.ram_gb = found["ram"] or listing.ram_gb
            listing.storage_gb = found["storage"] or listing.storage_gb
        else:
            listing.ram_gb, listing.storage_gb = found["ram"], found["storage"]

        if listing.match_method == "manual":
            return

        if not listing.llm_checked:
            flags = R.detect_flags(listing.title)
            listing.is_wanted_ad = listing.is_wanted_ad or flags["is_wanted_ad"]
            listing.is_broken = flags["is_broken"]
            listing.condition = map_condition(listing.raw_condition)

        specs_now = {"ram": listing.ram_gb, "storage": listing.storage_gb}
        rr = R.match(listing.title, listing.price, self.rules, specs_now)

        use_llm = self.mode in ("llm", "hybrid") and self._worth_llm(listing, rr)
        if use_llm and listing.llm_checked:
            return  # keep cached LLM decision
        if use_llm and self.llm and self.llm_calls < self.llm_budget:
            self.llm_calls += 1
            res = self.llm.classify(listing.title, listing.price, listing.raw_condition,
                                    listing.description, self.catalog)
            if res is not None:
                listing.llm_checked = True
                listing.condition = res.condition
                listing.is_bundle = res.is_bundle
                listing.is_wanted_ad = listing.is_wanted_ad or res.is_wanted_ad
                listing.is_broken = res.is_broken
                listing.ram_gb = listing.ram_gb or res.ram_gb
                listing.storage_gb = listing.storage_gb or specs.norm_storage(res.storage_gb or 0)
                pid, note = res.product_id, "llm"
                if pid is not None and not self.by_id[pid].price_ok(listing.price):
                    pid, note = None, f"llm said {self.by_id[pid].name}, but price outside range"
                elif pid is not None and not self.by_id[pid].specs_ok(
                        {"ram": listing.ram_gb, "storage": listing.storage_gb}):
                    pid, note = None, f"llm said {self.by_id[pid].name}, but specs differ"
                listing.product_id = pid
                listing.match_method = "llm" if pid else None
                listing.match_confidence = res.confidence if pid else None
                listing.match_note = note
                return

        # rules decide (mode=rules, confident rule hit, or LLM unavailable/over budget)
        listing.product_id = rr.product_id
        listing.match_method = "rule" if rr.product_id else None
        listing.match_confidence = rr.confidence if rr.product_id else None
        listing.match_note = rr.note + (" (llm pending)" if use_llm and not rr.product_id and self.llm else "")
