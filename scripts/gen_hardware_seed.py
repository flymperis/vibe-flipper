"""Generate seed_hardware.yaml: every desktop GPU / CPU model of the families below.

    python -m scripts.gen_hardware_seed          # writes seed_hardware.yaml
    python -m scripts.sync_seed                  # apply to an existing DB

Each entry is (model, typical used price in EUR). The typical price only sets the
min/max sanity range (accessories / complete PCs fall outside it); the real market
price is always computed from the listings. Adjust prices or add models here and
regenerate. Matching is done on normalized titles (see matching/rules.py):
"RTX4070Ti" -> "rtx 4070 ti", "i7-12700KF" -> "i 7 12700 kf", "5800X3D" -> "5800 x 3 d".

GPUs/CPUs have no search queries: they are picked up by the GPU/CPU category
feeds of each site (config *_FEED_*), which is far cheaper than ~150 searches.
"""
from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parents[1] / "seed_hardware.yaml"

# Complete systems / laptops that merely contain the part.
SYSTEM_WORDS = ["laptop", "λαπτοπ", "φορητ*", "notebook", "pc", "desktop", "υπολογιστ*", "συστημα", "σετ",
                "gaming pc", "mobile"]
# Also hints of a whole build listed together (CPU + board + RAM + GPU).
BUILD_WORDS = ["ddr 3", "ddr 4", "ddr 5", "μητρικ*", "motherboard", "x 3 d"]
GPU_EXCLUDE = SYSTEM_WORDS + BUILD_WORDS + ["ryzen", "i 3", "i 5", "i 7", "i 9", "intel core"]
CPU_EXCLUDE = SYSTEM_WORDS + ["rtx", "gtx", "rx", "μητρικ*", "motherboard", "combo", "bundle", "cooler master"]
# Mobile CPU suffixes: "13900HX" must not match the desktop 13900.
MOBILE_SUFFIXES = ["h", "hx", "hk", "hs", "u", "p", "t", "te", "m"]

# ---------------- GPUs: (family prefix, number, suffix, typical €, VRAM variants?) ----------------
NVIDIA = [
    # GTX 900
    ("GTX", "950", "", 30), ("GTX", "960", "", 40), ("GTX", "970", "", 55), ("GTX", "980", "", 70),
    ("GTX", "980", "Ti", 100),
    # GT/GTX 10
    ("GT", "1030", "", 40), ("GTX", "1050", "", 50), ("GTX", "1050", "Ti", 70), ("GTX", "1060", "", 80),
    ("GTX", "1070", "", 110), ("GTX", "1070", "Ti", 130), ("GTX", "1080", "", 150), ("GTX", "1080", "Ti", 210),
    # GTX 16 (Turing, same era as RTX 20)
    ("GTX", "1650", "", 90), ("GTX", "1650", "Super", 100), ("GTX", "1660", "", 110),
    ("GTX", "1660", "Super", 125), ("GTX", "1660", "Ti", 125),
    # RTX 20
    ("RTX", "2060", "", 140), ("RTX", "2060", "Super", 170), ("RTX", "2070", "", 180),
    ("RTX", "2070", "Super", 210), ("RTX", "2080", "", 240), ("RTX", "2080", "Super", 260),
    ("RTX", "2080", "Ti", 330),
    # RTX 30
    ("RTX", "3050", "", 160), ("RTX", "3060", "", 230), ("RTX", "3060", "Ti", 250), ("RTX", "3070", "", 290),
    ("RTX", "3070", "Ti", 320), ("RTX", "3080", "", 400), ("RTX", "3080", "Ti", 480), ("RTX", "3090", "", 650),
    ("RTX", "3090", "Ti", 750),
    # RTX 40
    ("RTX", "4060", "", 260), ("RTX", "4060", "Ti", 330), ("RTX", "4070", "", 470), ("RTX", "4070", "Super", 530),
    ("RTX", "4070", "Ti", 600), ("RTX", "4070", "Ti Super", 680), ("RTX", "4080", "", 850),
    ("RTX", "4080", "Super", 920), ("RTX", "4090", "", 1600),
    # RTX 50
    ("RTX", "5050", "", 230), ("RTX", "5060", "", 280), ("RTX", "5060", "Ti", 380), ("RTX", "5070", "", 520),
    ("RTX", "5070", "Ti", 750), ("RTX", "5080", "", 1050), ("RTX", "5090", "", 2400),
]
AMD_GPU = [
    # RX 6000
    ("RX", "6400", "", 90), ("RX", "6500", "XT", 110), ("RX", "6600", "", 170), ("RX", "6600", "XT", 200),
    ("RX", "6650", "XT", 210), ("RX", "6700", "", 230), ("RX", "6700", "XT", 260), ("RX", "6750", "XT", 290),
    ("RX", "6800", "", 330), ("RX", "6800", "XT", 380), ("RX", "6900", "XT", 430), ("RX", "6950", "XT", 480),
    # RX 7000
    ("RX", "7600", "", 220), ("RX", "7600", "XT", 270), ("RX", "7700", "", 330), ("RX", "7700", "XT", 340),
    ("RX", "7800", "XT", 430), ("RX", "7900", "GRE", 480), ("RX", "7900", "XT", 600), ("RX", "7900", "XTX", 750),
    # RX 9000
    ("RX", "9060", "XT", 320), ("RX", "9070", "", 560), ("RX", "9070", "XT", 650),
]
# Models sold with different VRAM sizes -> price variants by "ram" (= VRAM).
VRAM_VARIANTS = {"GTX 1060", "RTX 2060", "RTX 3050", "RTX 3060", "RTX 3080", "RTX 4060 Ti", "RTX 5060 Ti",
                 "RX 9060 XT", "RX 7600 XT"}
# GPU numbers that are also Ryzen CPU numbers: require "rx"/"radeon" before a bare number.
RYZEN_CLASH = {"7600", "7700", "7900", "9600", "9700", "9900"}


def gpu_entries(models, vendor: str) -> list[dict]:
    by_num: dict[str, list[str]] = {}
    for _, num, sfx, _ in models:
        by_num.setdefault(num, []).append(sfx.lower())
    out = []
    for fam, num, sfx, typ in models:
        name = f"{fam} {num}{(' ' + sfx) if sfx else ''}"
        phrase = f"{num} {sfx.lower()}".strip()
        # longer siblings that start with this phrase ("4070 ti" -> "4070 ti super"), or all siblings for the base
        siblings = [f"{num} {o}".strip() for o in by_num[num] if o != sfx.lower() and (not sfx or o.startswith(sfx.lower()))]
        if vendor == "amd" and not sfx and num in RYZEN_CLASH:
            include = [f"rx {num}", f"radeon {num}"]
        elif vendor == "amd":
            include = [phrase, f"rx {phrase}"]
        elif len(num) == 3 or fam == "GT":
            # "980" / "970" are also Samsung SSDs, "1030" is too generic -> need the family prefix
            include = [f"{fam.lower()} {phrase}"]
        else:
            include = [phrase]
        e = {
            "name": name,
            "category": "GPU",
            "include": include,
            # "1080p" (resolution) must not look like a GTX 1080
            "exclude": siblings + GPU_EXCLUDE + ([f"{num} p", f"{num} i"] if num == "1080" else []),
            "min_price": round(typ * 0.4),
            "max_price": round(typ * 1.8),
            "queries": [],
        }
        if name in VRAM_VARIANTS:
            e["specs"] = ["ram"]
        out.append(e)
    return out


# ---------------- Intel: (tier, number, group, typical €) ----------------
# group "K" = K and KF, "" = plain and F (price-equivalent pairs are one product), "KS" separate.
INTEL = [
    # 10th gen
    ("i3", "10100", "", 50), ("i3", "10105", "", 55), ("i5", "10400", "", 80), ("i5", "10500", "", 95),
    ("i5", "10600", "", 100), ("i5", "10600", "K", 110), ("i7", "10700", "", 130), ("i7", "10700", "K", 150),
    ("i9", "10850", "K", 170), ("i9", "10900", "", 170), ("i9", "10900", "K", 190),
    # 11th gen
    ("i5", "11400", "", 90), ("i5", "11500", "", 105), ("i5", "11600", "K", 120), ("i7", "11700", "", 140),
    ("i7", "11700", "K", 160), ("i9", "11900", "", 170), ("i9", "11900", "K", 190),
    # 12th gen
    ("i3", "12100", "", 75), ("i5", "12400", "", 110), ("i5", "12500", "", 130), ("i5", "12600", "", 140),
    ("i5", "12600", "K", 160), ("i7", "12700", "", 190), ("i7", "12700", "K", 210), ("i9", "12900", "", 260),
    ("i9", "12900", "K", 290), ("i9", "12900", "KS", 330),
    # 13th gen
    ("i3", "13100", "", 90), ("i5", "13400", "", 150), ("i5", "13500", "", 180), ("i5", "13600", "K", 230),
    ("i7", "13700", "", 260), ("i7", "13700", "K", 290), ("i9", "13900", "", 360), ("i9", "13900", "K", 400),
    ("i9", "13900", "KS", 450),
    # 14th gen
    ("i3", "14100", "", 100), ("i5", "14400", "", 160), ("i5", "14500", "", 190), ("i5", "14600", "K", 240),
    ("i7", "14700", "", 290), ("i7", "14700", "K", 320), ("i9", "14900", "", 400), ("i9", "14900", "K", 440),
    ("i9", "14900", "KS", 500),
]
# Core Ultra 200S (Arrow Lake desktop): (tier, number, group, typical €)
INTEL_ULTRA = [
    ("5", "225", "", 180), ("5", "245", "K", 240), ("7", "265", "", 280), ("7", "265", "K", 300),
    ("9", "285", "", 450), ("9", "285", "K", 480),
]


def _intel_entry(name, include, siblings, typ):
    return {
        "name": name,
        "category": "CPU",
        "include": include,
        "exclude": siblings + CPU_EXCLUDE,
        "min_price": round(typ * 0.4),
        "max_price": round(typ * 1.8),
        "queries": [],
    }


def intel_entries() -> list[dict]:
    groups = {"": ["", "f"], "K": ["k", "kf"], "KS": ["ks"]}
    by_num: dict[str, set[str]] = {}
    for _, num, grp, _ in INTEL + INTEL_ULTRA:
        by_num.setdefault(num, set()).update(groups[grp])
    out = []
    for tier, num, grp, typ in INTEL:
        own = groups[grp]
        name = f"Intel {tier}-{num}" + {"": "/F", "K": "K/KF", "KS": "KS"}[grp]
        include = [f"{num} {s}".strip() for s in own]
        others = sorted(by_num[num] - set(own))
        siblings = [f"{num} {o}" for o in others if o] + [f"{num} {m}" for m in MOBILE_SUFFIXES]
        out.append(_intel_entry(name, include, siblings, typ))
    for tier, num, grp, typ in INTEL_ULTRA:
        own = groups[grp]
        name = f"Intel Core Ultra {tier} {num}" + {"": "/F", "K": "K/KF"}[grp]
        # 3-digit numbers are ambiguous alone -> require "ultra" context or the K suffix
        include = [f"ultra {tier} {num} {s}".strip() for s in own] + [f"{num} {s}" for s in own if s]
        others = sorted(by_num[num] - set(own))
        siblings = [f"{num} {o}" for o in others if o] + [f"{num} {m}" for m in MOBILE_SUFFIXES]
        out.append(_intel_entry(name, include, siblings, typ))
    return out


# ---------------- AMD Ryzen: (tier, number, group, typical €) ----------------
# group "X" = plain + X + XT (one product), "X3D", "G" = G + GT.
RYZEN = [
    # 3000
    ("3", "3100", "X", 45), ("3", "3300", "X", 70), ("3", "3200", "G", 50), ("5", "3400", "G", 70),
    ("5", "3600", "X", 65), ("7", "3700", "X", 95), ("7", "3800", "X", 110), ("9", "3900", "X", 160),
    ("9", "3950", "X", 230),
    # 4000
    ("3", "4100", "X", 45), ("5", "4500", "X", 55), ("5", "4600", "G", 70),
    # 5000
    ("5", "5500", "X", 65), ("5", "5600", "X", 85), ("5", "5600", "G", 90), ("7", "5700", "X", 120),
    ("7", "5700", "G", 120), ("7", "5700", "X3D", 170), ("7", "5800", "X", 130), ("7", "5800", "X3D", 250),
    ("9", "5900", "X", 190), ("9", "5950", "X", 270),
    # 7000
    ("5", "7500", "X", 130), ("5", "7600", "X", 150), ("7", "7700", "X", 200), ("7", "7800", "X3D", 330),
    ("9", "7900", "X", 260), ("9", "7900", "X3D", 330), ("9", "7950", "X", 380), ("9", "7950", "X3D", 450),
    # 9000
    ("5", "9600", "X", 190), ("7", "9700", "X", 260), ("7", "9800", "X3D", 470), ("9", "9900", "X", 340),
    ("9", "9900", "X3D", 450), ("9", "9950", "X", 480), ("9", "9950", "X3D", 620),
]


# Display names matching the models that actually exist (matching still accepts X/XT/F).
RYZEN_NAMES = {
    ("3100", "X"): "Ryzen 3 3100", ("3300", "X"): "Ryzen 3 3300X", ("3600", "X"): "Ryzen 5 3600/3600X",
    ("3700", "X"): "Ryzen 7 3700X", ("3800", "X"): "Ryzen 7 3800X/XT", ("3900", "X"): "Ryzen 9 3900X/XT",
    ("3950", "X"): "Ryzen 9 3950X", ("4100", "X"): "Ryzen 3 4100", ("4500", "X"): "Ryzen 5 4500",
    ("5500", "X"): "Ryzen 5 5500", ("5600", "X"): "Ryzen 5 5600/5600X/XT", ("5700", "X"): "Ryzen 7 5700/5700X",
    ("5800", "X"): "Ryzen 7 5800X/XT", ("5900", "X"): "Ryzen 9 5900X/XT", ("5950", "X"): "Ryzen 9 5950X",
    ("7500", "X"): "Ryzen 5 7500F", ("7600", "X"): "Ryzen 5 7600/7600X", ("7700", "X"): "Ryzen 7 7700/7700X",
    ("7900", "X"): "Ryzen 9 7900/7900X", ("7950", "X"): "Ryzen 9 7950X", ("9600", "X"): "Ryzen 5 9600/9600X",
    ("9700", "X"): "Ryzen 7 9700X", ("9900", "X"): "Ryzen 9 9900X", ("9950", "X"): "Ryzen 9 9950X",
    ("3200", "G"): "Ryzen 3 3200G", ("3400", "G"): "Ryzen 5 3400G", ("4600", "G"): "Ryzen 5 4600G",
    ("5600", "G"): "Ryzen 5 5600G/GT", ("5700", "G"): "Ryzen 7 5700G",
}


def ryzen_entries() -> list[dict]:
    suffix_phrases = {"X": ["x", "xt", "f"], "X3D": ["x 3 d"], "G": ["g", "gt", "ge"]}
    by_num: dict[str, list[str]] = {}
    for _, num, grp, _ in RYZEN:
        by_num.setdefault(num, []).append(grp)
    out = []
    for tier, num, grp, typ in RYZEN:
        name = RYZEN_NAMES.get((num, grp), f"Ryzen {tier} {num}{grp}")
        if grp == "X":
            # the bare number clashes with RX GPUs -> needs "ryzen"/"r5" context or an X suffix
            include = [f"ryzen {tier} {num}", f"r {tier} {num}", f"ryzen {num}"] + [f"{num} {s}" for s in suffix_phrases["X"]]
        else:
            include = [f"{num} {s}" for s in suffix_phrases[grp]]
        siblings = [f"{num} {s}" for g in by_num[num] if g != grp for s in suffix_phrases[g]]
        # "5800 x" is a prefix of our own "5800 x 3 d": excluding it would exclude ourselves
        siblings = [x for x in siblings if not any(inc.startswith(x + " ") for inc in include)]
        siblings += [f"{num} {m}" for m in MOBILE_SUFFIXES if m not in ("t", "te", "m")]
        out.append({
            "name": name,
            "category": "CPU",
            "include": include,
            "exclude": siblings + CPU_EXCLUDE,
            "min_price": round(typ * 0.4),
            "max_price": round(typ * 1.8),
            "queries": [],
        })
    return out


# ---------------- Storage & memory: one product per capacity (brand-agnostic) ----------------
# The capacity comes from the spec extractor (matching/specs.py), which folds marketing sizes onto
# classes (480/500/512GB -> 512GB, 960/1000GB -> 1TB) and reads RAM kits as totals ("2x8GB" -> 16GB).
TB = 1024
NOT_A_PART = SYSTEM_WORDS + ["ps 5", "playstation", "xbox", "switch", "steam deck", "macbook", "iphone", "ipad",
                             "rtx", "gtx", "ryzen", "i 3", "i 5", "i 7", "i 9", "μητρικ*", "motherboard", "combo"]
# NVMe (M.2 PCIe) vs SATA SSDs: explicit words or well-known model lines. A bare "SSD" counts as SATA;
# a bare "M.2 SSD" (type unknown) matches both and is left to the LLM.
NVME_MARKERS = ["nvme", "pcie", "pci e", "gen 3", "gen 4", "gen 5", "990 pro", "980 pro", "970 evo", "960 evo",
                "sn 850", "sn 850 x", "sn 770", "sn 580", "sn 570", "sn 5000", "crucial p 1", "crucial p 3",
                "crucial p 5", "crucial t 500", "crucial t 700", "kc 3000", "nv 2", "nv 3", "firecuda 530", "mp 600",
                "sx 8200", "fury renegade"]
NVME_WORDS = NVME_MARKERS + ["m 2"]
# Not "sata" alone: hard disks are SATA too. A SATA SSD needs the word "SSD" or a known SSD model.
SATA_WORDS = ["ssd", "870 evo", "860 evo", "850 evo", "870 qvo", "860 qvo", "mx 500", "bx 500", "a 400", "kc 600",
              "sa 510", "ultra 3 d", "wd blue 3 d", "ssd plus", "a 55"]
# USB/portable SSDs are a different market: in neither category.
EXTERNAL_WORDS = ["portable", "external", "εξωτερικ*", "t 7", "t 9", "usb"]
HDD_WORDS = ["hdd", "σκληρ*", "hard disk", "hard drive", "harddisk", "barracuda", "ironwolf", "rpm", "scorpio",
             "caviar", "wd red", "wd purple", "wd green", "exos", "skyhawk", "toshiba n 300", "toshiba p 300",
             "wd elements", "my passport", "my book", "seagate expansion"]
# Signs that an "SSD" listing is (also) a hard disk, e.g. a lot of mixed drives.
HDD_MARKERS = ["hdd", "σκληρ*", "rpm", "scorpio", "caviar", "barracuda", "3 5", "my book", "my passport",
               "wd elements"]
MEDIA_WORDS = ["flash drive", "usb stick", "stick", "sd card", "micro sd", "microsd", "καρτα μνημης",
               "enclosure", "θηκη", "adapter", "ανταπτορ*", "docking"]
# (capacity in GB, typical used €)
SSD_SATA = [(128, 12), (256, 18), (512, 30), (1 * TB, 55), (2 * TB, 110), (4 * TB, 220)]
SSD_NVME = [(256, 20), (512, 35), (1 * TB, 65), (2 * TB, 120), (4 * TB, 250)]
HDD = [(512, 12), (1 * TB, 20), (2 * TB, 35), (3 * TB, 45), (4 * TB, 60), (6 * TB, 90), (8 * TB, 120),
       (10 * TB, 150), (12 * TB, 170), (14 * TB, 190), (16 * TB, 220), (18 * TB, 250), (20 * TB, 280)]
RAM = {
    "DDR3": [(4, 5), (8, 10), (16, 18)],
    "DDR4": [(4, 6), (8, 15), (16, 30), (32, 60), (64, 120)],
    "DDR5": [(16, 45), (32, 90), (48, 130), (64, 180), (96, 280)],
}


def _size(gb: int) -> str:
    return f"{gb // TB}TB" if gb >= TB else f"{gb}GB"


def storage_entries() -> list[dict]:
    out = []
    for gb, typ in SSD_SATA:
        out.append({"name": f"SSD {_size(gb)}", "category": "SSD", "include": SATA_WORDS,
                    "exclude": NVME_MARKERS + HDD_MARKERS + NOT_A_PART + MEDIA_WORDS + EXTERNAL_WORDS,
                    "require": {"storage": gb}, "min_price": round(typ * 0.35), "max_price": round(typ * 2.2),
                    "queries": []})
    for gb, typ in SSD_NVME:
        out.append({"name": f"NVMe {_size(gb)}", "category": "NVMe", "include": NVME_WORDS,
                    "exclude": ["sata"] + HDD_MARKERS + NOT_A_PART + MEDIA_WORDS + EXTERNAL_WORDS,
                    "require": {"storage": gb}, "min_price": round(typ * 0.35), "max_price": round(typ * 2.2),
                    "queries": []})
    for gb, typ in HDD:
        out.append({"name": "HDD 500GB" if gb == 512 else f"HDD {_size(gb)}", "category": "HDD", "include": HDD_WORDS,
                    "exclude": ["ssd", "nvme", "m 2"] + NOT_A_PART + MEDIA_WORDS,
                    "require": {"storage": gb}, "min_price": round(typ * 0.35), "max_price": round(typ * 2.2),
                    "queries": []})
    for gen, sizes in RAM.items():
        for gb, typ in sizes:
            out.append({"name": f"RAM {gen} {gb}GB", "category": "RAM", "include": [gen.lower()],
                        "exclude": ["ssd", "nvme", "hdd"] + NOT_A_PART + ["cpu", "επεξεργαστ*", "gpu"],
                        "require": {"ram": gb}, "min_price": max(2, round(typ * 0.3)),
                        "max_price": round(typ * 2.2), "queries": []})
    return out


def main() -> None:
    entries = (gpu_entries(NVIDIA, "nvidia") + gpu_entries(AMD_GPU, "amd") + intel_entries() + ryzen_entries()
               + storage_entries())
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names)), "duplicate product names"
    header = ("# GENERATED by scripts/gen_hardware_seed.py -- edit that script and regenerate.\n"
              "# Desktop GPUs (GTX 900 -> RTX 50, RX 6000 -> 9000) and CPUs (Intel 10th gen -> Core Ultra 200S,\n"
              "# Ryzen 3000 -> 9000), SATA SSD / NVMe / HDD per capacity and RAM per DDR generation + capacity.\n"
              "# Found via the category feeds, so no per-model search queries.\n\n")
    OUT.write_text(header + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False, width=140), encoding="utf-8")
    print(f"wrote {len(entries)} products to {OUT.name}")


if __name__ == "__main__":
    main()
