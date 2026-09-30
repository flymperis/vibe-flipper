"""Extract RAM / storage from listing text, and build a product "variant" label
from the specs that matter for that product (e.g. MacBook: ram+storage,
iPhone: storage, RTX 3060: ram = VRAM)."""
import re

SPEC_KEYS = ("ram", "storage")
SPEC_LABELS = {"ram": "RAM", "storage": "Αποθήκευση"}

RAM_SIZES = {2, 3, 4, 6, 8, 10, 12, 16, 18, 20, 24, 32, 36, 48, 64, 96, 128, 192, 256}
TB = 1024
# Canonical capacity classes: marketing sizes map onto them (500GB ~ 512GB, 1000GB ~ 1TB, ...).
STORAGE_SIZES = {16, 32, 64, 128, 256, 512, 1 * TB, 2 * TB, 3 * TB, 4 * TB, 6 * TB, 8 * TB, 10 * TB, 12 * TB,
                 14 * TB, 16 * TB, 18 * TB, 20 * TB, 22 * TB, 24 * TB}
_STORAGE_ALIASES = {120: 128, 240: 256, 250: 256, 275: 256, 480: 512, 500: 512, 525: 512, 960: TB, 1000: TB,
                    1920: 2 * TB, 2000: 2 * TB, 3000: 3 * TB, 3840: 4 * TB, 4000: 4 * TB, 6000: 6 * TB,
                    8000: 8 * TB, 10000: 10 * TB, 12000: 12 * TB}

# "8/256", "16GB/512GB", "8+256", "16/1TB"
_PAIR = re.compile(r"(?<![\d.])(\d{1,3})\s*(?:gb)?\s*[/+|]\s*(\d{1,4})\s*(gb|tb)?(?![\d])", re.I)
# "16GB", "512 GB", "1TB", "1,5 TB", "256G"
_CAP = re.compile(r"(?<![\d.])(\d{1,4}(?:[.,]5)?)\s*(gb|tb|g)(?![a-zα-ω])", re.I)
# "256 SSD", "512 nvme" (unit omitted)
_BARE_STORAGE = re.compile(r"(?<![\d.])(\d{3,4})\s*(?:ssd|nvme|m\.?2)\b", re.I)
_RAM_HINT = re.compile(r"^\s*(?:of\s+)?(?:ram|ddr\d?|unified|μνημη|μνήμη|memory|vram|gddr\d?x?)", re.I)
_STORAGE_HINT = re.compile(r"^\s*(?:ssd|nvme|hdd|storage|αποθηκ|δισκ|δίσκ|ssd)", re.I)
# RAM kits: "2x8GB", "2 x 16 GB", "4×8G"
_KIT = re.compile(r"(?<![\d.])([1-8])\s*[x×]\s*(\d{1,3})\s*(?:gb|g)(?![a-zα-ω])", re.I)
_DDR = re.compile(r"(?<![a-z])ddr\s?[2-5]", re.I)  # not GDDR
_DISK_WORDS = re.compile(r"ssd|nvme|hdd|m\.2|σκληρ|δισκ|δίσκ", re.I)


def _to_gb(value: str, unit: str | None) -> float:
    v = float(value.replace(",", "."))
    return v * 1024 if unit and unit.lower() == "tb" else v


def norm_storage(gb: float) -> int | None:
    g = int(round(gb))
    g = _STORAGE_ALIASES.get(g, g)
    return g if g in STORAGE_SIZES else None


def _ram_module(t: str) -> int | None:
    """Total capacity of a RAM listing: a kit "2x16GB" -> 32, else the largest RAM size mentioned."""
    if len({g.lower().replace(" ", "") for g in _DDR.findall(t)}) > 1:
        return None  # a lot mixing DDR generations ("DDR3 / DDR4 sodimm 16GB 8GB 4GB ...")
    kits = _KIT.findall(t)
    if len(kits) == 1:
        total = int(kits[0][0]) * int(kits[0][1])
        if total in RAM_SIZES:
            return total
    elif len(kits) > 1:
        return None  # several kits in one listing
    sizes = [int(float(m.group(1).replace(",", "."))) for m in _CAP.finditer(t) if m.group(2).lower() != "tb"]
    sizes = sorted({x for x in sizes if x in RAM_SIZES})
    # one size, or "32GB (2x16GB)"-style pairs are handled above; 3+ sizes = a lot of mixed modules
    return sizes[-1] if 0 < len(sizes) <= 2 else None


def extract(text: str) -> dict[str, int | None]:
    """Best-effort RAM and storage (in GB) from free text."""
    ram: int | None = None
    storage: int | None = None
    if not text:
        return {"ram": None, "storage": None}
    t = text.replace("\xa0", " ")

    # A memory module/kit listing ("DDR4 32GB (2x16GB) 3600MHz"): every GB figure is RAM.
    if _DDR.search(t) and not _DISK_WORDS.search(t):
        return {"ram": _ram_module(t), "storage": None}

    m = _PAIR.search(t)
    if m:
        a, b, unit = int(m.group(1)), m.group(2), m.group(3)
        st = norm_storage(_to_gb(b, unit))
        if a in RAM_SIZES and st and st >= 64 and a < st:
            ram, storage = a, st

    for m in _CAP.finditer(t):
        unit = m.group(2).lower()
        gb = _to_gb(m.group(1), "tb" if unit == "tb" else "gb")
        after = t[m.end(): m.end() + 12]
        if _RAM_HINT.match(after) and int(gb) in RAM_SIZES:
            ram = ram or int(gb)
        elif _STORAGE_HINT.match(after) or unit == "tb" or gb >= 64:
            if gb >= 64 or unit == "tb":
                storage = storage or norm_storage(gb)
        elif int(gb) in RAM_SIZES and gb <= 48:
            ram = ram or int(gb)
    if storage is None and (m := _BARE_STORAGE.search(t)):
        storage = norm_storage(int(m.group(1)))
    return {"ram": ram, "storage": storage}


def fmt_gb(gb: int) -> str:
    return f"{gb // 1024}TB" if gb >= 1024 and gb % 1024 == 0 else f"{gb}GB"


def variant_label(keys: list[str], ram: int | None, storage: int | None,
                  allowed: dict[str, list[int]] | None = None) -> str | None:
    """'16GB / 512GB' for keys [ram, storage]; None if a required spec is unknown,
    or is not one of the product's `allowed` sizes (e.g. an RTX 2060 "8GB")."""
    if not keys:
        return None
    parts = []
    for k in keys:
        v = ram if k == "ram" else storage if k == "storage" else None
        if v is None or ((allowed or {}).get(k) and v not in allowed[k]):
            return None
        parts.append(fmt_gb(v))
    return " / ".join(parts)


_LABEL_PART = re.compile(r"^(\d+)(GB|TB)$")


def parse_label(keys: list[str], label: str | None) -> dict[str, int] | None:
    """Inverse of variant_label: ("ram", "storage"), "16GB / 1TB" -> {"ram": 16, "storage": 1024}."""
    if not label or not keys:
        return None
    parts = [x.strip() for x in label.split("/")]
    if len(parts) != len(keys):
        return None
    out = {}
    for k, part in zip(keys, parts):
        m = _LABEL_PART.match(part)
        if not m:
            return None
        out[k] = int(m.group(1)) * (TB if m.group(2) == "TB" else 1)
    return out
