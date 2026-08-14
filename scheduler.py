import logging

from apscheduler.schedulers.background import BackgroundScheduler

from config import load_config
from scraper import run_search_once

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()
_job_id = "daily_job_search"


def _run_job():
    try:
        result = run_search_once()
        logger.info("daily search done: %s", result)
    except Exception:
        logger.exception("daily search failed")


def start_scheduler():
    cfg = load_config()
    reschedule(cfg["schedule_hour"], cfg["schedule_minute"])
    if not _scheduler.running:
        _scheduler.start()


def reschedule(hour, minute):
    if _scheduler.get_job(_job_id):
        _scheduler.remove_job(_job_id)
    _scheduler.add_job(_run_job, "cron", hour=hour, minute=minute, id=_job_id)
