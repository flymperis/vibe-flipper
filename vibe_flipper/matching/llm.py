"""Ollama-backed classifier (e.g. Qwen). Returns the product a listing sells
plus condition/bundle/wanted/broken flags, using a JSON-schema constrained reply."""
import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

CONDITIONS = ["new", "like_new", "good", "fair", "unknown"]

SCHEMA = {
    "type": "object",
    "properties": {
        "product_id": {"type": ["integer", "null"]},
        "confidence": {"type": "number"},
        "condition": {"type": "string", "enum": CONDITIONS},
        "is_bundle": {"type": "boolean"},
        "is_wanted_ad": {"type": "boolean"},
        "is_broken": {"type": "boolean"},
        "ram_gb": {"type": ["integer", "null"]},
        "storage_gb": {"type": ["integer", "null"]},
    },
    "required": ["product_id", "confidence", "condition", "is_bundle", "is_wanted_ad", "is_broken",
                 "ram_gb", "storage_gb"],
}

SYSTEM = """You classify second-hand marketplace listings (Greek and other European languages) \
for a price tracker. Given a listing and a numbered list of products, decide which product the \
listing is SELLING as its main item.

Rules:
- product_id must be one of the listed ids, or null if none fits exactly.
- A game, controller, case, cable, box, charger, or other accessory FOR a console/device is NOT \
that console/device -> null (unless that accessory is itself in the list).
- A complete PC or laptop that contains a graphics card is NOT the graphics card -> null.
- Different variants are different products (e.g. PS5 vs PS5 Digital vs PS5 Pro, iPhone 13 vs \
13 Pro vs 13 Pro Max, RTX 3070 vs 3070 Ti). If the variant is unclear, pick the base model only \
when nothing suggests another variant.
- is_bundle: the main item is sold together with significant extras (extra controllers, several \
games, other devices) that materially raise the price.
- is_wanted_ad: the poster wants to BUY (ζητώ, ζητείται, αγοράζω, WTB), not sell.
- is_broken: damaged, not working, for parts, locked/blacklisted.
- condition: new (sealed/unused), like_new (σαν καινούργιο, άριστη), good, fair (φθορές, γρατζουνιές), \
or unknown.
- ram_gb / storage_gb: memory and storage of the main item in GB if stated (1TB = 1024), else null. For graphics cards ram_gb is the VRAM. Never guess a value that is not written.
- confidence: 0..1, your certainty about product_id.
Reply with JSON only."""


@dataclass
class LLMResult:
    product_id: int | None
    confidence: float
    condition: str
    is_bundle: bool
    is_wanted_ad: bool
    is_broken: bool
    ram_gb: int | None = None
    storage_gb: int | None = None


class OllamaClassifier:
    def __init__(self, url: str, model: str, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.model = model
        self.http = httpx.Client(timeout=timeout)

    def ping(self) -> tuple[bool, str]:
        try:
            r = self.http.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])]
            if self.model not in names and not any(n and n.split(":")[0] == self.model for n in names):
                return False, f"model '{self.model}' not found; available: {', '.join(filter(None, names))}"
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def classify(self, title: str, price: float | None, raw_condition: str | None,
                 description: str | None, products: list[tuple[int, str, str]]) -> LLMResult | None:
        """products: (id, name, category)."""
        catalog = "\n".join(f"{pid}: {name}" + (f" [{cat}]" if cat else "") for pid, name, cat in products)
        listing = f"Title: {title}\nPrice: {price if price is not None else 'n/a'} EUR"
        if raw_condition:
            listing += f"\nSite condition label: {raw_condition}"
        if description:
            listing += f"\nDescription: {description[:1500]}"
        body = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "think": False,  # qwen3.x: answer straight away, no reasoning tokens
            # the whole product catalog goes into the prompt: keep it inside the context window
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Products:\n{catalog}\n\nListing:\n{listing}"},
            ],
        }
        try:
            r = self.http.post(f"{self.url}/api/chat", json=body)
            r.raise_for_status()
            data = json.loads(r.json()["message"]["content"])
        except Exception as e:  # noqa: BLE001
            log.warning("ollama classify failed for %r: %s", title, e)
            return None
        valid_ids = {p[0] for p in products}
        pid = data.get("product_id")
        if pid not in valid_ids:
            pid = None
        cond = data.get("condition") if data.get("condition") in CONDITIONS else "unknown"
        return LLMResult(
            product_id=pid,
            confidence=float(data.get("confidence") or 0),
            condition=cond,
            is_bundle=bool(data.get("is_bundle")),
            is_wanted_ad=bool(data.get("is_wanted_ad")),
            is_broken=bool(data.get("is_broken")),
            ram_gb=_int_or_none(data.get("ram_gb")),
            storage_gb=_int_or_none(data.get("storage_gb")),
        )


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None
