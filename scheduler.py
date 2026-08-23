import logging

from apscheduler.schedulers.background import BackgroundScheduler

from config import load_config
from pipeline import analyze_pending_jobs, classify_company_origins
from scraper import run_search_once

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()
_job_id = "daily_job_search"


def _run_job():
    # jobspy 搜索（访客身份 HTTP 请求）和 How You Fit 同步（登录态浏览器会话）是两个
    # 独立的风险源，互相没有因果关系——前者失败不该连累后者，所以这里不再像原来那样
    # jobspy 一失败就直接 return 跳过后面所有步骤，而是各自 try/except、用
    # new_job_ids 累积两边找到的新职位，一起喂给后面的分析/公司分类。
    new_job_ids = []
    try:
        result = run_search_once()
        new_job_ids += result.get("new_job_ids") or []
        logger.info("daily search done: %s", result)
    except Exception:
        logger.exception("daily search failed")

    try:
        from linkedin_how_you_fit import sync_all_enabled_searches

        hyf_summary = sync_all_enabled_searches()
        for entry in hyf_summary.values():
            new_job_ids += (entry.get("result") or {}).get("added_ids") or []
        logger.info("how-you-fit sync after daily search: %s", hyf_summary)
    except Exception:
        logger.exception("how-you-fit sync after daily search failed")

    try:
        analyzed_count = analyze_pending_jobs(job_ids=new_job_ids)
        logger.info("auto-analyzed %s pending job(s)", analyzed_count)
    except Exception:
        logger.exception("auto-analyze after daily search failed")

    try:
        classify_result = classify_company_origins(job_ids=new_job_ids)
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
