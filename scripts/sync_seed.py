"""Apply seed_products.yaml to an existing DB (adds new products, overwrites
rule fields of products with the same name), then re-match.

    python -m scripts.sync_seed
    podman exec -it vibe-flipper python -m scripts.sync_seed
"""
from vibe_flipper import jobs
from vibe_flipper.db import init_db, session_scope

if __name__ == "__main__":
    init_db()
    with session_scope() as s:
        added, updated = jobs.load_seed(s, sync=True)
    print(f"{added} added, {updated} updated; re-matching…")
    print(f"{jobs.match_listings()} listings matched")
