"""Run one scrape cycle now (outside the scheduler).

    python -m scripts.scrape_now                      # all enabled sources, automatic mode (delta)
    python -m scripts.scrape_now --backfill           # deeper crawl (after adding many products)
    python -m scripts.scrape_now --full --source insomnia
                                                      # every Insomnia page (~19k ads, ~45-60 min)
    podman exec -it vibe-flipper python -m scripts.scrape_now --full --source insomnia

Don't run it while the app's own scheduled scrape is running.
"""
import argparse
import logging

from vibe_flipper import jobs
from vibe_flipper.db import init_db
from vibe_flipper.scrapers.base import BACKFILL, FULL

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--backfill", action="store_true", help="crawl more pages per feed/query")
    group.add_argument("--full", action="store_true",
                       help="crawl every page (Insomnia); other sources fall back to --backfill")
    ap.add_argument("--source", action="append", help="limit to these sources")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db()
    mode = FULL if args.full else BACKFILL if args.backfill else None
    if not jobs.run_scrape(args.source, mode=mode):
        print("another scrape is running in this process")
