from pathlib import Path

import pytest
import yaml

from vibe_flipper.matching.rules import ProductRule, contains, detect_flags, match, normalize

ROOT = Path(__file__).parents[1]
SEED = [item for f in ("seed_products.yaml", "seed_hardware.yaml")
        for item in yaml.safe_load((ROOT / f).read_text(encoding="utf-8"))]
RULES = [
    ProductRule(i, p["name"], [str(k) for k in p.get("include", [])], [str(k) for k in p.get("exclude", [])],
                p.get("regex"), p.get("min_price"), p.get("max_price"), dict(p.get("require") or {}))
    for i, p in enumerate(SEED)
]
NAMES = {r.id: r.name for r in RULES}


def product_for(title, price):
    return NAMES.get(match(title, price, RULES).product_id)


def test_normalize():
    assert normalize("Sony PlayStation®5 Ψηφιακή Έκδοσης") == "sony playstation 5 ψηφιακη εκδοσησ"
    assert normalize("RTX3070Ti") == "rtx 3070 ti"
    assert normalize("MacBook Air M1, 8GB") == "macbook air m 1 8 gb"


def test_contains_is_whole_word_and_prefix():
    assert contains("rtx 3070 ti", "3070")
    assert not contains("rtx 30700", "3070")
    assert contains("υπολογιστησ gaming", "υπολογιστ*")
    assert not contains("υπολογιστησ gaming", "υπολογιστ")


@pytest.mark.parametrize("title,price,expected", [
    ("Sony PlayStation 5 Slim Digital Edition - εντός εγγύησης", 450, "PlayStation 5 Digital"),
    ("PS5 Slim με δίσκο + 2 χειριστήρια", 420, "PlayStation 5 (Disc)"),
    ("PS5 Pro 2TB σαν καινούργιο", 700, "PlayStation 5 Pro"),
    ("Χειριστήριο PS5, καινούργιο", 50, None),              # accessory: price below range
    ("God of War Ragnarok παιχνίδι PS5", 35, None),
    ("NVIDIA GeForce RTX 3070 Founders Edition 8GB", 277, "RTX 3070"),
    ("Gigabyte 3060 ti Aorus Master 8 GB", 260, "RTX 3060 Ti"),
    ("Lenovo Legion 5 Pro, i9-12900H, RTX 3070, 32GB RAM", 1006, None),  # laptop, not GPU
    ("Gaming Pc με i7, RTX 3070 8GB", 900, None),
    ("RTX 3070 MSI Gaming Z Trio/RTX 3080 MSI Gaming Z Trio", 330, None),  # ambiguous
    ("Gigabyte RTX 4070 Super Gaming OC", 650, "RTX 4070 Super"),
    ("Apple iPhone 14 Pro Max 256GB", 600, "iPhone 14 Pro Max"),
    ("iPhone 14 Pro 128GB μαύρο", 520, "iPhone 14 Pro"),
    ("iPhone 13 128gb", 300, "iPhone 13"),
    ("Apple MacBook Air 13 M1 2020 8/256", 480, "MacBook Air M1"),
    ("Steam Deck OLED 512GB", 480, "Steam Deck OLED"),
    ("Steam Deck 256GB LCD", 330, "Steam Deck LCD"),
    ("Nintendo Switch OLED λευκό", 230, "Nintendo Switch OLED"),
    ("Xbox Series X 1TB", 380, "Xbox Series X"),
])
def test_seed_rules(title, price, expected):
    assert product_for(title, price) == expected


def test_most_specific_phrase_wins():
    rules = [ProductRule(1, "A", ["ps 5"]), ProductRule(2, "B", ["ps 5 pro"])]
    res = match("PS5 Pro", 700, rules)
    assert res.product_id == 2 and not res.confident


def test_detect_flags():
    assert detect_flags("Ζητώ PS5 digital")["is_wanted_ad"]
    assert not detect_flags("PS5 - δεν ζητώ ανταλλαγές")["is_wanted_ad"]
    assert detect_flags("iPhone 13 για ανταλλακτικά")["is_broken"]
    assert detect_flags("RTX 3080 χαλασμένη")["is_broken"]
    assert not detect_flags("RTX 3080 άψογη")["is_broken"]
    assert detect_flags("RTX 3080 Eagle 10GB κάρτα γραφικών για επισκευή ή ανταλλακτικά")["is_broken"]
    assert detect_flags("GTX 1080 artifacts")["is_broken"]
    assert not detect_flags("PS5 δεκτές ανταλλαγές")["is_broken"]


@pytest.mark.parametrize("title,price,expected", [
    ("Θήκη iPhone 13 σιλικόνης", 300, None),   # price in range, still an accessory
    ("Χειριστήριο PS5 DualSense Edge", 260, None),
    ("Παιχνίδια Nintendo Switch με Mario, Sonic", 120, None),
    ("Xbox series x memorie", 287, None),
    ("PS5 Pro Console Covers – Wolverine", 500, None),
    ("Ochelari Vr Meta quest 3 S", 287, "Meta Quest 3S"),
    ("Meta Quest 3 512GB", 420, "Meta Quest 3"),
])
def test_accessories_and_lookalikes(title, price, expected):
    assert product_for(title, price) == expected


@pytest.mark.parametrize("title,price,expected", [
    ("AMD Ryzen 7 5800X3D σφραγισμένος", 260, "Ryzen 7 5800X3D"),
    ("Ryzen 7 7800 X3D", 330, "Ryzen 7 7800X3D"),
    ("B650 + 7800X3D + 32GB DDR5 combo", 620, None),            # combo above max
    ("Gaming PC Ryzen 7 5800X3D RTX 3080", 1100, None),
    ("RTX 4070 Ti Super Gigabyte Gaming OC", 720, "RTX 4070 Ti Super"),
    ("MSI RTX 4070 Ti Ventus 3X", 620, "RTX 4070 Ti"),
    ("Sapphire Pulse RX 7900XTX 24GB", 780, "RX 7900 XTX"),
    ("iPhone 16e 128GB", 500, None),
    ("iPhone 16 128GB Black", 650, "iPhone 16"),
    ("iPhone 16 Pro Max 256GB Desert", 1000, "iPhone 16 Pro Max"),
    ("iPhone 15 Pro Max 256", 850, "iPhone 15 Pro Max"),
    ("Samsung Galaxy S24 Ultra 512GB", 800, "Galaxy S24 Ultra"),
    ("Sony WH-1000XM5 μαύρα", 220, "Sony WH-1000XM5"),
    ("Sony WH1000XM4", 150, "Sony WH-1000XM4"),
    ("PS VR2 + Horizon", 320, "PlayStation VR2"),
    ("PlayStation 5 + VR2", 700, None),                          # PS5 excludes vr, VR2 above max
    ("ROG Ally X 1TB", 600, "ASUS ROG Ally X"),
    ("Asus ROG Ally Z1 Extreme", 380, "ASUS ROG Ally"),
    ("DJI Mini 4 Pro Fly More Combo", 750, "DJI Mini 4 Pro"),
    ("iPad mini 4 128GB", 150, "iPad mini 4"),
    ("MacBook Pro 14 M1 Pro 16/512", 1000, "MacBook Pro M1 Pro/Max"),
    ("Mac mini M4 16/256", 520, "Mac mini M4"),
    ("GoPro Hero12 Black", 250, "GoPro Hero 12"),
])
def test_v2_products(title, price, expected):
    assert product_for(title, price) == expected


@pytest.mark.parametrize("text,ram,storage", [
    ("Apple MacBook Air M1, 8GB RAM και 256GB SSD", 8, 256),
    ("MacBook Air M2 16/512 midnight", 16, 512),
    ("Macbook Air 13.6inch M2 16GB RAM 1TB SSD", 16, 1024),
    ("MacBook Air M2 8GB 512GB SSD", 8, 512),
    ("iPhone 13 128GB σαν καινούργιο", None, 128),
    ("Samsung S24 Ultra 12/256", 12, 256),
    ("RTX 3060 Ventus 2X 8G OC", 8, None),
    ("Gigabyte RTX 3060 12 GB GDDR6", 12, None),
    ("Steam Deck OLED 1TB", None, 1024),
    ("MacBook Air M1 2020 13,3 ιντσών", None, None),
    ("Macbook Air M1 8GB 256 SSD 2020", 8, 256),
])
def test_spec_extraction(text, ram, storage):
    from vibe_flipper.matching.specs import extract
    assert extract(text) == {"ram": ram, "storage": storage}


def test_variant_label():
    from vibe_flipper.matching.specs import variant_label
    assert variant_label(["ram", "storage"], 16, 1024) == "16GB / 1TB"
    assert variant_label(["storage"], 8, 256) == "256GB"
    assert variant_label(["ram", "storage"], None, 256) is None
    assert variant_label([], 8, 256) is None



@pytest.mark.parametrize("title,price,expected", [
    # NVIDIA siblings
    ("Gigabyte RTX 4070 Ti Super Gaming OC 16G", 700, "RTX 4070 Ti Super"),
    ("MSI RTX 4070 Super Ventus", 520, "RTX 4070 Super"),
    ("RTX4070 Dual", 450, "RTX 4070"),
    ("Asus GTX 1080 Ti Strix", 200, "GTX 1080 Ti"),
    ("GTX 1660 Super 6GB", 120, "GTX 1660 Super"),
    ("MSI GTX 980 4GB", 70, "GTX 980"),
    ("Samsung 980 Pro 1TB NVMe", 80, None),            # SSD, not a GPU
    ("RTX 5090 Founders Edition", 2300, "RTX 5090"),
    ("Κάρτα γραφικών Palit RTX 3060 12GB", 220, "RTX 3060"),
    # AMD GPUs vs Ryzen numbers
    ("Sapphire Pulse RX 7600 8GB", 210, "RX 7600"),
    ("RX 7600 XT 16GB", 280, "RX 7600 XT"),
    ("PowerColor 7900 XTX Red Devil", 800, "RX 7900 XTX"),
    ("RX 6700 10GB", 230, "RX 6700"),
    ("Radeon RX 9070 XT Taichi", 650, "RX 9070 XT"),
    # Ryzen
    ("AMD Ryzen 5 7600 tray", 140, "Ryzen 5 7600/7600X"),
    ("Ryzen 5 7600X box", 170, "Ryzen 5 7600/7600X"),
    ("R5 5600 + stock cooler", 80, "Ryzen 5 5600/5600X/XT"),
    ("AMD Ryzen 5 5600G (Tray)", 99, "Ryzen 5 5600G/GT"),
    ("Ryzen 7 5700X3D", 180, "Ryzen 7 5700X3D"),
    ("Ryzen 7 5800X", 130, "Ryzen 7 5800X/XT"),
    ("Ryzen 5 7500F", 120, "Ryzen 5 7500F"),
    ("Ryzen 7 5800H laptop board", 150, None),
    ("B550 + Ryzen 5 5600X combo", 180, None),
    # Intel
    ("πωλειται Intel Core i5-12400", 90, "Intel i5-12400/F"),
    ("i5 12400F", 95, "Intel i5-12400/F"),
    ("Intel i7-12700KF", 200, "Intel i7-12700K/KF"),
    ("i9-13900KS", 450, "Intel i9-13900KS"),
    ("i9 13900K", 400, "Intel i9-13900K/KF"),
    ("I7 7700k", 70, None),                             # 7th gen: not tracked, and not Ryzen 7700
    ("Intel Core Ultra 7 265K", 300, "Intel Core Ultra 7 265K/KF"),
    ("Lenovo Legion i7-13700HX RTX 4060", 1100, None),
    ("5700xt  6600xt  1660super", 160, None),
    ("Κονσόλα Xbox Series S, 500GB, 120fps 1080p, 4K 60fps", 250, "Xbox Series S"),
    ("MSI GTX 1080 Gaming X 8G", 150, "GTX 1080"),             # several unrelated models
    ("9800X3D, ROG STRIX X870-A GAMING, Trident Z5 RGB DDR5, RX 7800XT", 500, None),  # a whole build
])
def test_hardware_products(title, price, expected):
    assert product_for(title, price) == expected


def product_with_specs(title, price):
    from vibe_flipper.matching.specs import extract
    e = extract(title)
    return NAMES.get(match(title, price, RULES, {"ram": e["ram"], "storage": e["storage"]}).product_id)


@pytest.mark.parametrize("title,price,expected", [
    # SSD / HDD by capacity class, any brand
    # NVMe vs SATA
    ("Samsung 990 Pro 2TB NVMe", 140, "NVMe 2TB"),
    ("Samsung 990 Pro 2TB", 140, "NVMe 2TB"),              # known NVMe line, no "NVMe" word
    ("WD Black SN850X 1TB M.2", 75, "NVMe 1TB"),
    ("Adata XPG SX8200 Pro 2TB NVMe PCIe 3.0", 120, "NVMe 2TB"),
    ("Crucial T700 4TB PCIe Gen5 NVMe M.2 SSD", 450, "NVMe 4TB"),
    ("M.2 NVMe SSD 512GB", 30, "NVMe 512GB"),
    ("Kingston A400 480GB SSD", 25, "SSD 512GB"),
    ("Crucial MX500 250GB", 20, "SSD 256GB"),
    ("Samsung 870 EVO 1TB SATA", 60, "SSD 1TB"),
    ("Ssd 2.5'' 512gb", 25, "SSD 512GB"),
    ("WD Blue SA510 M.2 SATA 500GB", 30, "SSD 512GB"),     # M.2 form factor, SATA protocol
    ("M.2 SSD 1TB", 60, None),                             # type unknown -> ambiguous (LLM)
    ("Samsung T7 1TB portable SSD", 70, None),             # external USB SSD
    ("SSD 1TB + HDD 2TB", 90, None),                      # two drives
    ("WD Red Plus 4TB NAS", 70, "HDD 4TB"),
    ("Seagate Barracuda 2TB 7200rpm", 40, "HDD 2TB"),
    ("Σκληρός δίσκος 1TB 2.5", 20, "HDD 1TB"),
    ("Seagate Exos 16TB", 220, "HDD 16TB"),
    ("PS5 Slim 1TB SSD", 420, "PlayStation 5 (Disc)"),   # the console, not a drive
    ("USB stick 256GB", 15, None),
    # RAM by generation + total kit capacity
    ("Corsair Vengeance 32GB (2x16GB) DDR4 3600MHz", 60, "RAM DDR4 32GB"),
    ("Kingston Fury 2x8GB DDR4 3200", 30, "RAM DDR4 16GB"),
    ("G.Skill Trident Z5 64GB DDR5 6000", 180, "RAM DDR5 64GB"),
    ("DDR3 8GB 1600MHz", 10, "RAM DDR3 8GB"),
    ("RTX 3060 12GB GDDR6", 230, "RTX 3060"),             # GDDR is not DDR
    ("Laptop i7 16GB DDR4 512GB SSD", 400, None),
    # iPad
    ("iPad Air M2 11 128GB", 550, "iPad Air M2"),
    ("Apple iPad Air (5th generation) 64GB", 380, "iPad Air M1"),
    ("iPad Pro 12.9 M2 256GB", 850, "iPad Pro 12.9 M2"),
    ("iPad Pro 11 M2 128GB", 700, "iPad Pro 11 M2"),
    ("iPad Pro M4 13\" 512GB", 1200, "iPad Pro 13 M4"),
    ("iPad 9th gen 64GB", 200, "iPad 9 (2021)"),
    ("iPad 10.2 2021 64GB", 200, "iPad 9 (2021)"),
    ("Apple iPad 8th generation 32GB", 170, "iPad 8 (2020)"),
    ("iPad 7ης γενιάς 32GB", 140, "iPad 7 (2019)"),
    ("iPad 10.2 2019 128GB", 150, "iPad 7 (2019)"),
    ("iPad 6th gen 2018 32GB", 110, "iPad 6 (2018)"),
    ("Apple iPad 5 32GB", 90, "iPad 5 (2017)"),
    ("iPad 4 16GB", 45, "iPad 4 (2012)"),
    ("iPad 4G 64GB", 150, None),                              # cellular, generation unknown
    ("iPad mini 4 128GB", 90, "iPad mini 4"),
    ("iPad mini 5 64GB Wi-Fi", 190, "iPad mini 5"),
    ("iPad Pro 2018 11 64GB", 300, None),                     # not the base 2018 iPad
    ("iPad Air 5 64GB", 380, "iPad Air M1"),
    ("Apple iPad 10th generation 64GB", 280, "iPad 10 (2022)"),
    ("iPad mini 6 64GB", 330, "iPad mini 6"),
    ("Θήκη για iPad Pro 11", 20, None),
    ("Apple ipad Pro 2024 12.9” M4 Wi-Fi", 1050, "iPad Pro 13 M4"),     # 12.9 written for the 13" M4
    ("Μνήμες RAM DDR3 DDR3L DDR2 DDR SODIMM - 16GB 8GB 4GB 2GB 1GB", 5, None),  # mixed lot
    ("Sodimm Kingston DDR4 32GB/Mushkin DDR3 2x8GB", 40, None),
    ("Kingston DDR4 16GB 2666 (8GB+8GB)", 35, "RAM DDR4 16GB"),
])
def test_storage_ram_ipad(title, price, expected):
    assert product_with_specs(title, price) == expected


@pytest.mark.parametrize("title,price,expected", [
    ("Western Digital WD2500BEVT 250GB SATA 2,5\" Scorpio Blue", 15, None),   # laptop HDD, below our HDD sizes
    ("Σκληροί δίσκοι 3.5\" SATA Western Digital Blue (500GB)", 25, "HDD 500GB"),
    ("Western Digital My Book 1TB, SSD SAMSUNG 870 - 500GB , 860 - 250 GB", 40, None),
    ("Kingston Technology KC600 (512 GB/SATA III)", 50, "SSD 512GB"),
    ("Seagate 2TB 7200rpm", 40, "HDD 2TB"),
])
def test_ssd_vs_hdd(title, price, expected):
    assert product_with_specs(title, price) == expected


def test_variant_price_range_overrides_product_range():
    r = ProductRule(1, "iPhone 13 Pro Max", ["iphone 13 pro max"], min_price=300, max_price=700,
                    spec_keys=["storage"], variant_ranges={"1TB": {"min": 450, "max": 1000}})
    assert r.price_ok(800, {"storage": 1024})       # 1TB: its own range
    assert not r.price_ok(400, {"storage": 1024})
    assert not r.price_ok(800, {"storage": 256})    # other variants: product range
    assert not r.price_ok(800)                      # variant unknown: product range
    assert match("iPhone 13 Pro Max 1TB", 800, [r], {"storage": 1024, "ram": None}).product_id == 1


def test_parse_label_roundtrip():
    from vibe_flipper.matching.specs import parse_label, variant_label
    assert parse_label(["ram", "storage"], "16GB / 1TB") == {"ram": 16, "storage": 1024}
    assert parse_label(["storage"], variant_label(["storage"], None, 512)) == {"storage": 512}
    assert parse_label(["storage"], "16GB / 1TB") is None
    assert parse_label([], "1TB") is None
