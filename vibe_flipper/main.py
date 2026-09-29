import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from . import jobs, runtime_settings
from .config import get_settings
from .db import init_db, session_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vibe_flipper")
logging.getLogger("httpx").setLevel(logging.WARNING)

scheduler = BackgroundScheduler(timezone="UTC")
JOB_ID = "scrape"


def reschedule(minutes: int) -> None:
    job = scheduler.get_job(JOB_ID)
    if job:
        job.reschedule("interval", minutes=max(5, minutes))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as s:
        jobs.load_seed(s)
        jobs.close_interrupted_runs(s)
        interval = runtime_settings.load(s).scrape_interval_minutes
    first = datetime.utcnow() + timedelta(seconds=5) if get_settings().scrape_on_startup else None
    scheduler.add_job(jobs.run_scrape, "interval", minutes=max(5, interval), id=JOB_ID,
                      next_run_time=first, max_instances=1, coalesce=True)
    scheduler.add_job(jobs.run_purge, "interval", hours=24, id="purge",
                      next_run_time=datetime.utcnow() + timedelta(minutes=10), max_instances=1, coalesce=True)
    scheduler.start()
    log.info("scheduler started, every %d min", interval)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Vibe Flipper", lifespan=lifespan)

from . import web  # noqa: E402  (routes need `app`)

app.include_router(web.router)
web._mount_static(app)
