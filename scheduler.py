import logging

from apscheduler.schedulers.background import BackgroundScheduler

from config import load_config
from pipeline import analyze_pending_jobs, classify_company_origins
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
        return

    try:
        analyzed_count = analyze_pending_jobs(job_ids=result.get("new_job_ids"))
        logger.info("auto-analyzed %s pending job(s)", analyzed_count)
    except Exception:
        logger.exception("auto-analyze after daily search failed")

    try:
        classify_result = classify_company_origins(job_ids=result.get("new_job_ids"))
        logger.info("company origin classify after daily search: %s", classify_result)
    except Exception:
        logger.exception("company origin classify after daily search failed")


def start_scheduler():
    cfg = load_config()
    reschedule(cfg["schedule_hour"], cfg["schedule_minute"], cfg.get("schedule_enabled", True))
    if not _scheduler.running:
        _scheduler.start()


def reschedule(hour, minute, enabled=True):
    if _scheduler.get_job(_job_id):
        _scheduler.remove_job(_job_id)
    if not enabled:
        return
    _scheduler.add_job(_run_job, "cron", hour=hour, minute=minute, id=_job_id)
