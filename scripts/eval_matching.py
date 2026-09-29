"""Compare matching modes (rules / llm / hybrid) against your manual labels.

Label listings from the UI first ("Χωρίς αντιστοίχιση" → pick product, or
"Λάθος προϊόν" on a product page); every manual choice — including
"not a product" — is ground truth here.

    python -m scripts.eval_matching                  # uses DB settings for Ollama
    python -m scripts.eval_matching --ollama-url http://server:11434 --model qwen2.5:14b
"""
import argparse
import time

from vibe_flipper import runtime_settings
from vibe_flipper.db import init_db, session_scope
from vibe_flipper.matching import Matcher
from vibe_flipper.matching.llm import OllamaClassifier
from vibe_flipper.models import Listing, Product


def evaluate(mode, labeled, products, rs, classifier):
    rs.matching_mode = mode
    matcher = Matcher(products, rs, llm_budget=10**6, classifier=classifier if mode != "rules" else None)
    correct = assigned = assigned_ok = 0
    errors = []
    t0 = time.monotonic()
    for src in labeled:
        l = Listing(title=src.title, price=src.price, raw_condition=src.raw_condition,
                    description=src.description, is_wanted_ad=False, llm_checked=False)
        matcher.apply(l)
        ok = l.product_id == src.product_id
        correct += ok
        if l.product_id is not None:
            assigned += 1
            assigned_ok += ok
        if not ok:
            errors.append((src.title, src.product_id, l.product_id))
    return {
        "accuracy": correct / len(labeled),
        "precision": assigned_ok / assigned if assigned else 0.0,
        "llm_calls": matcher.llm_calls,
        "seconds": time.monotonic() - t0,
        "errors": errors,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ollama-url")
    ap.add_argument("--model")
    ap.add_argument("--show-errors", type=int, default=10)
    args = ap.parse_args()

    init_db()
    with session_scope() as s:
        rs = runtime_settings.load(s)
        products = s.query(Product).all()
        labeled = s.query(Listing).filter(Listing.match_method == "manual").all()
        names = {p.id: p.name for p in products}
        s.expunge_all()

    if not labeled:
        print("No manual labels yet — assign some listings in the UI first.")
        return
    url = args.ollama_url or rs.ollama_url
    model = args.model or rs.ollama_model
    classifier = OllamaClassifier(url, model) if url else None
    modes = ["rules"] + (["hybrid", "llm"] if classifier else [])
    print(f"{len(labeled)} labeled listings, {len(products)} products, LLM: {model if classifier else 'off'}\n")
    print(f"{'mode':8} {'accuracy':>9} {'precision':>10} {'llm calls':>10} {'time':>8}")
    for mode in modes:
        r = evaluate(mode, labeled, products, rs, classifier)
        print(f"{mode:8} {r['accuracy']:9.1%} {r['precision']:10.1%} {r['llm_calls']:10d} {r['seconds']:7.1f}s")
        for title, want, got in r["errors"][: args.show_errors]:
            print(f"    ✗ {title[:60]:60}  want={names.get(want, '—')}  got={names.get(got, '—')}")


if __name__ == "__main__":
    main()
