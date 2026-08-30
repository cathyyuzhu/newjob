import logging
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from config import load_config
from models import add_notification, last_successful_collect_run_at
from pipeline import analyze_pending_jobs, classify_company_origins
from scraper import run_search_once

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()
_job_id = "daily_job_search"

# 距上次成功抓取超过这么多小时，进程启动时判定"错过了至少一次定时触发"，立刻补跑
# 一次——比严格的 24 小时略宽松，日常调度抖动（提前/推迟几分钟触发、系统时间有小
# 误差）不该被误判成"错过了"。见 start_scheduler() 的 _maybe_catch_up() 说明。
CATCHUP_THRESHOLD_HOURS = 25


def _run_job():
    # jobspy 搜索（访客身份 HTTP 请求）和 How You Fit 同步（登录态浏览器会话）是两个
    # 独立的风险源，互相没有因果关系——前者失败不该连累后者，所以这里不再像原来那样
    # jobspy 一失败就直接 return 跳过后面所有步骤，而是各自 try/except、用
    # new_job_ids 累积两边找到的新职位，一起喂给后面的分析/公司分类。
    #
    # 这是唯一一个完全没人盯着屏幕的触发路径（每天定时跑），最需要"回来看板"，所以在
    # 最后统一发一条汇总通知，不像其它后台任务那样逐个环节各发一条——四个子步骤本来就
    # 各自独立 try/except、互不阻断，汇总成一条比四条更适合这个"事后回顾"场景。
    new_job_ids = []
    search_ok = True
    try:
        result = run_search_once()
        new_job_ids += result.get("new_job_ids") or []
        logger.info("daily search done: %s", result)
    except Exception:
        logger.exception("daily search failed")
        search_ok = False

    try:
        from linkedin_how_you_fit import sync_all_enabled_searches

        hyf_summary = sync_all_enabled_searches()
        for entry in hyf_summary.values():
            new_job_ids += (entry.get("result") or {}).get("added_ids") or []
        logger.info("how-you-fit sync after daily search: %s", hyf_summary)
    except Exception:
        logger.exception("how-you-fit sync after daily search failed")

    analyzed_count = 0
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

    add_notification(
        "search", "每日定时抓取完成" if search_ok else "每日定时抓取部分失败",
        f"新增 {len(new_job_ids)} 条职位 · 自动分析 {analyzed_count} 条",
        level="success" if search_ok else "error",
    )


def start_scheduler():
    cfg = load_config()
    reschedule(cfg["schedule_hour"], cfg["schedule_minute"], cfg.get("schedule_enabled", True))
    if not _scheduler.running:
        _scheduler.start()
    _maybe_catch_up(cfg)


def _maybe_catch_up(cfg):
    """进程启动时检查有没有错过至少一次定时触发，错过就立刻在后台补跑一次
    （2026-08-29，见 spec/roadmap.md「职位收集链路的错误处理生产级加固」缺口6/⑧）。

    最常见的场景是笔记本合盖休眠、跨过了昨天设定的触发时刻——APScheduler 的
    BackgroundScheduler 靠进程内的定时器，进程/系统一停就完全不知道"错过了"，
    重新唤醒后只会乖乖等下一次预定时间，不会自己补上；`misfire_grace_time` 只能
    处理"进程一直活着但被别的任务卡住几分钟"这种短暂延误，处理不了"整台机器睡了
    一晚上"这种量级的错过。

    每日定时任务被用户关掉（`schedule_enabled=False`）时不检查——那是用户主动选的
    状态，不该在"关掉"的情况下还偷偷跑一次。只看 `collect_runs` 里 source="jobspy"
    最近一次成功记录的时间：How You Fit 同步是 `_run_job()` 里紧跟在 jobspy 之后的
    一步，正常情况下两者总是一起触发，用其中一个作代表足够，不需要每个 source 各查
    一次。从没成功跑过（全新安装、数据库刚建好）不算"错过"，等第一次正常触发就行，
    不在这里抢跑。
    """
    if not cfg.get("schedule_enabled", True):
        return
    last_ok = last_successful_collect_run_at("jobspy")
    if last_ok is None:
        return
    try:
        last_ok_at = datetime.fromisoformat(last_ok)
    except ValueError:
        return
    if datetime.now() - last_ok_at < timedelta(hours=CATCHUP_THRESHOLD_HOURS):
        return
    logger.info(
        "距上次成功抓取已超过 %d 小时（上次：%s），判定错过了至少一次定时触发，启动时补跑一次",
        CATCHUP_THRESHOLD_HOURS, last_ok,
    )
    threading.Thread(target=_run_job, daemon=True).start()


def reschedule(hour, minute, enabled=True):
    if _scheduler.get_job(_job_id):
        _scheduler.remove_job(_job_id)
    if not enabled:
        return
    _scheduler.add_job(
        _run_job, "cron", hour=hour, minute=minute, id=_job_id,
        # 三个都是 2026-08-29 加固补的（见 spec/roadmap.md 缺口6/⑧），APScheduler
        # 默认值对"笔记本经常休眠"这个本项目最常见的运行环境不够安全：
        # - misfire_grace_time：进程活着但触发时刻被别的任务/系统负载卡住的话，
        #   多久以内补跑仍然算数（超过这个宽限期 APScheduler 默认直接放弃，不跑
        #   也不报错，表现上就是"今天什么都没发生"）。跟 CATCHUP_THRESHOLD_HOURS
        #   处理的是同一类问题的两个尺度：这个管"进程没停、只是晚了几分钟到几小时"，
        #   _maybe_catch_up() 管"进程/系统整个停过一段时间"。
        # - coalesce=True：如果因为长时间没醒过来、一次积压了好几个错过的触发点，
        #   醒来后只补一次，不要把攒下来的都补跑一遍。
        # - max_instances=1：正常的每日一次不该重叠，但防止 misfire 补跑撞上下一次
        #   正常触发时段的边界情况，两次 _run_job() 绝不同时跑（互相没有并发安全性）。
        misfire_grace_time=3 * 3600, coalesce=True, max_instances=1,
    )
