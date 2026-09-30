"""Keyword/regex matcher. Works on a normalized title: lowercase, no accents,
final sigma folded, punctuation collapsed to single spaces."""
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from .specs import variant_label

_NON_ALNUM = re.compile(r"[^0-9a-zα-ω]+")
# Split every letter/digit boundary so "ps5" == "ps 5", "rtx3070ti" == "rtx 3070 ti",
# "5800X3D" == "5800 x 3 d". Keywords and regexes are written against this normalized form.
_LETTER_DIGIT = re.compile(r"(?<=[a-zα-ω])(?=\d)|(?<=\d)(?=[a-zα-ω])")

WANTED_PATTERNS = ["ζητω", "ζητειται", "ζητουνται", "ζηταω", "ψαχνω", "αγοραζω", "wtb", "looking for", "wanted"]
BROKEN_PATTERNS = [
    "χαλασμεν", "ανταλλακτικ", "ανταλακτικ", "επισκευ", "δεν λειτουργ", "δεν δουλευ", "δεν ανοιγει",
    "δεν βγαζει εικονα", "χωρισ εικονα", "σπασμεν", "κλειδωμεν", "icloud lock", "broken", "not working",
    "for parts", "for repair", "repair", "defect", "faulty", "artifact", "no display", "ελαττωματικ",
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("ς", "σ")
    text = _NON_ALNUM.sub(" ", text)
    text = _LETTER_DIGIT.sub(" ", text)
    return " ".join(text.split())


@lru_cache(maxsize=4096)
def _norm_phrase(p: str) -> str:
    return normalize(p)


def contains(norm_text: str, phrase: str) -> bool:
    """Whole-word phrase containment on normalized text. A trailing '*' on the
    phrase allows prefix matching of the last word (e.g. 'χαλασμεν*')."""
    prefix = phrase.endswith("*")
    p = _norm_phrase(phrase.rstrip("*"))
    if not p:
        return False
    if prefix:
        return re.search(rf"(?:^| ){re.escape(p)}", norm_text) is not None
    return f" {p} " in f" {norm_text} "


# A title that *starts* with one of these sells an accessory/game FOR the product
# ("Θήκη iPhone 13", "Χειριστήριο PS5", "Παιχνίδια Nintendo Switch"), not the product.
ACCESSORY_PREFIXES = [
    "θηκη", "θηκεσ", "καλυμμα", "καλυμματα", "χειριστηριο", "χειριστηρια", "παιχνιδι", "παιχνιδια",
    "βαση", "φορτιστη", "φορτιστησ", "καλωδιο", "τζαμακι", "μπαταρια", "κουτι",
    "case", "cover", "covers", "skin", "controller", "charger", "cable", "stand", "dock", "game", "games",
    "husa", "carcasa", "joc", "jocuri", "manete", "hulle", "funda",
]


def is_accessory_title(norm_title: str) -> bool:
    first = norm_title.split(" ", 1)[0] if norm_title else ""
    return first in ACCESSORY_PREFIXES or any(first.startswith(p) for p in ("προστατευτικ", "ανταλλακτικ"))


def detect_flags(title: str) -> dict[str, bool]:
    """Cheap heuristics; the LLM refines these when enabled."""
    n = normalize(title)
    padded = f" {n}"
    return {
        # "Ζητώ PS5" — only when the title *starts* with a wanted-word
        "is_wanted_ad": any(n.startswith(normalize(p)) for p in WANTED_PATTERNS),
        "is_broken": any(f" {normalize(p)}" in padded for p in BROKEN_PATTERNS),
    }


# Bundles: a second *major* item sold together with the product, so the price is not the product's.
# Games, controllers, cases, chargers... are what consoles/handhelds normally come with: not bundles.
_PC_PARTS = {"GPU", "CPU"}
# chipset of a motherboard: "z 590", "b 760 m", "x 570" (normalized: letters and digits split)
_MOTHERBOARD = re.compile(r"(?:^| )(?:[abhxz] [1-9]\d0(?= |$))|μητρικ|motherboard")
_GPU_MODEL = re.compile(r"(?:^| )(?:rtx|gtx|rx|radeon(?: rx)?) (\d{3,4})(?= |$)")
_DISCRETE_GPU = re.compile(r"(?:^| )(?:rtx|gtx|rx) \d{3,4}(?= |$)")  # not "Radeon 780M" graphics of a CPU
_CPU = re.compile(r"ryzen|(?:^| )i [3579] \d{4,5}|core ultra|(?:^| )intel core")
_PSU = re.compile(r"τροφοδοτικ|(?:^| )psu(?= |$)|(?:^| )\d{3,4} w(?= |$)")
_RAM = re.compile(r"(?:^| )ddr [345](?= |$).*(?:^| )\d{1,3} gb|(?:^| )\d{1,3} gb.*(?:^| )ddr [345](?= |$)")
# another device, unless it is the listing's own kind: (phrase, categories it belongs to)
_DEVICES = [
    ("monitor", ()), ("λαπτοπ", ("MacBook",)), ("laptop", ("MacBook",)), ("apple watch", ("Smartwatch",)),
    ("airpods", ("Ακουστικά",)), ("macbook", ("MacBook",)), ("ipad", ("iPad",)), ("iphone", ("iPhone",)),
    ("magic keyboard", ()), ("ps 5", ("PlayStation",)), ("playstation 5", ("PlayStation",)),
    ("ps 4", ("PlayStation",)), ("playstation 4", ("PlayStation",)), ("xbox", ("Xbox",)),
    ("nintendo", ("Nintendo",)), ("steam deck", ("Handheld PC",)), ("rog ally", ("Handheld PC",)),
]

_TRADE = re.compile(r"ανταλλαγ|αλλαγη|(?:^| )(?:trade|swap)(?= |$)")


def is_bundle(title: str, category: str | None) -> bool:
    """Title sells the product together with another major item: "i7-11700K + Z590 Aorus",
    "RTX 2060 μαζί με RX 590", "Mac mini M4 + Samsung monitor", "iPhone 16 Pro + Apple Watch"."""
    n = normalize(title)
    if category in _PC_PARTS:
        gpus = set(_GPU_MODEL.findall(n))
        if (_MOTHERBOARD.search(n) or _PSU.search(n) or _RAM.search(n)
                or (category == "GPU" and (len(gpus) > 1 or _CPU.search(n)))
                or (category == "CPU" and _DISCRETE_GPU.search(n))):
            return True
    # "ανταλλαγή με iPad" offers a trade, "παιχνίδια για PS4" are games: neither is sold along
    offered = _TRADE.split(n, 1)[0]
    return any(category not in own and re.search(rf"(?:^| )(?<!για )(?<!for ){re.escape(_norm_phrase(phrase))}(?= |$)",
                                                 offered)
               for phrase, own in _DEVICES)


@dataclass
class ProductRule:
    id: int
    name: str
    include: list[str]
    exclude: list[str] = field(default_factory=list)
    regex: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    spec_filter: dict[str, int] = field(default_factory=dict)
    spec_keys: list[str] = field(default_factory=list)
    variant_ranges: dict[str, dict] = field(default_factory=dict)
    spec_values: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def from_product(cls, p) -> "ProductRule":
        return cls(p.id, p.name, [str(k) for k in p.include_keywords or []],
                   [str(k) for k in p.exclude_keywords or []],
                   p.regex or None, p.min_price, p.max_price, dict(p.spec_filter or {}),
                   list(p.spec_keys or []), dict(p.variant_ranges or {}), dict(p.spec_values or {}))

    def specs_ok(self, specs: dict | None) -> bool:
        """All required specs must be known and equal (e.g. storage == 1024 for an "SSD 1TB")."""
        return all((specs or {}).get(k) == v for k, v in self.spec_filter.items())

    def price_range(self, specs: dict | None = None) -> tuple[float | None, float | None]:
        """The variant's own range if one is set (e.g. iPhone "1TB"), else the product's."""
        if self.variant_ranges and specs:
            label = variant_label(self.spec_keys, specs.get("ram"), specs.get("storage"), self.spec_values)
            vr = self.variant_ranges.get(label or "")
            if vr:
                return vr.get("min"), vr.get("max")
        return self.min_price, self.max_price

    def price_ok(self, price: float | None, specs: dict | None = None) -> bool:
        if price is None:
            return True
        lo, hi = self.price_range(specs)
        if lo is not None and price < lo:
            return False
        if hi is not None and price > hi:
            return False
        return True


@dataclass
class RuleResult:
    product_id: int | None
    confidence: float
    candidates: list[int]  # all products whose keywords matched (before tie-break)
    price_rejected: list[int]  # keyword match but price outside bounds
    note: str = ""

    @property
    def confident(self) -> bool:
        return self.product_id is not None and self.confidence >= 0.8


def match(title: str, price: float | None, rules: list[ProductRule], specs: dict | None = None) -> RuleResult:
    n = normalize(title)
    if is_accessory_title(n):
        return RuleResult(None, 0.0, [], [], "accessory (title prefix)")
    scored: list[tuple[str, ProductRule]] = []  # (longest matched phrase, rule)
    price_rejected: list[int] = []
    for r in rules:
        hits = [_norm_phrase(k.rstrip("*")) for k in r.include if contains(n, k)]
        if not hits:
            continue
        if any(contains(n, k) for k in r.exclude):
            continue
        if r.regex:
            try:
                if not re.search(r.regex, n):
                    continue
            except re.error:
                continue
        if not r.specs_ok(specs):
            continue
        if not r.price_ok(price, specs):
            price_rejected.append(r.id)
            continue
        scored.append((max(hits, key=len), r))

    if not scored:
        return RuleResult(None, 0.0, [], price_rejected,
                          "price outside product range" if price_rejected else "no keyword match")
    scored.sort(key=lambda t: len(t[0]), reverse=True)
    best_phrase, best = scored[0]
    candidates = [r.id for _, r in scored]
    if len(scored) == 1:
        return RuleResult(best.id, 0.9, candidates, price_rejected, f"rule: {best.name}")
    # The more specific phrase wins only over phrases it contains ("ps 5 pro" over "ps 5").
    # Unrelated models in one title ("6600 xt ... 1660 super") are genuinely ambiguous.
    if all(f" {p} " in f" {best_phrase} " and p != best_phrase for p, _ in scored[1:]):
        return RuleResult(best.id, 0.7, candidates, price_rejected, f"rule (most specific of {len(scored)})")
    return RuleResult(None, 0.0, candidates, price_rejected, f"ambiguous: {len(scored)} products")
