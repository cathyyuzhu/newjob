"""不属于上述任何一个业务域的收尾接口：搜索运行记录、每日任务清单、通知、追踪表只读接口，
以及一次性的历史数据回填。
"""
import json
import logging
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from config import load_config
from models import (
    add_checklist_item,
    add_job_note,
    collect_run_stats,
    collect_run_totals,
    delete_checklist_item,
    get_latest_resume_review,
    last_email_scan_run,
    list_checklist_items,
    list_jobs_missing_cover_letter,
    list_notifications,
    list_pending_rejections,
    list_runs,
    list_stale_applications,
    llm_call_stats,
    llm_call_totals,
    make_dedupe_key,
    mark_all_notifications_read,
    remove_pending_rejection,
    set_application_status,
    unread_notification_count,
    update_job_materials,
)
import resume_store
from tracker_utils import list_entries

misc_bp = Blueprint("misc", __name__)


@misc_bp.route("/api/runs", methods=["GET"])
def get_runs():
    return jsonify(list_runs())


# ---------------------------------------------------------------- 每日任务清单
MAX_CHECKLIST_ITEM_LENGTH = 200


@misc_bp.route("/api/checklist", methods=["GET"])
def get_checklist():
    """待审核/待投递两项前端已经有 allJobs 全量数据，直接在 static/app.js 里现算，
    不占这个接口的字段——这里只负责后端才算得出来的部分：超过N天没跟进的投递（N由设置页
    stale_application_reminder_days 配置，0表示用户关掉了这条提醒）、用户自建的待办条目、
    简历有没有体检过、体检给出的建议是不是还没去优化、距上次邮件拒信扫描是不是已经超过
    设置页配置的提醒间隔。"""
    stale_days = load_config().get("stale_application_reminder_days", 7)
    followups = [
        {"job_id": j["id"], "title": j["title"], "company": j["company"], "applied_at": j["applied_at"]}
        for j in list_stale_applications(days=stale_days)
    ] if stale_days else []

    # 邮件拒信扫描提醒：app.py 本身够不到 Gmail（见 email_rejection_scan.py 顶部说明），
    # 这里只算"距上次 Claude 帮你查过一轮已经过去几天了、够不够到设置页配的提醒间隔"，
    # 不做任何真实扫描。interval=0 表示用户在设置页关掉了这条提醒。
    interval_days = load_config().get("email_scan_interval_days", 1)
    last_scan = last_email_scan_run()
    email_scan_days_since = None
    if last_scan:
        try:
            email_scan_days_since = (datetime.now() - datetime.fromisoformat(last_scan["run_at"])).days
        except ValueError:
            email_scan_days_since = None
    email_scan_due = bool(interval_days) and (last_scan is None or (email_scan_days_since or 0) >= interval_days)

    latest_review = get_latest_resume_review()
    resume_review_ready = False
    resume_review_id = None
    if latest_review and latest_review.get("content_json") and not latest_review.get("error"):
        resume_review_id = latest_review.get("id")
        # 不用"今天完成"这种按日期收敛的提醒——用户明确要求这条要一直留到真的处理完
        # （生成过优化版）或者自己主动点掉，跨天也不该凭空消失。用「优化版文件的 mtime
        # 有没有晚于这次体检」判断"处理完"了没有：optimized.docx 不存在，或者存在但是
        # 上一次体检之前生成的，都算这次体检的建议还没被采纳过。
        optimized_path = resume_store.optimized_path()
        if not os.path.exists(optimized_path):
            resume_review_ready = True
        else:
            try:
                optimized_at = datetime.fromtimestamp(os.path.getmtime(optimized_path))
                reviewed_at = datetime.fromisoformat(latest_review["created_at"])
                resume_review_ready = optimized_at < reviewed_at
            except (OSError, ValueError):
                resume_review_ready = True
    return jsonify(
        {
            "followups": followups,
            "custom_items": list_checklist_items(),
            "resume_review_done": bool(latest_review),
            "resume_review_ready": resume_review_ready,
            "resume_review_id": resume_review_id,
            "email_scan_due": email_scan_due,
            "email_scan_days_since": email_scan_days_since,
            "pending_rejections": list_pending_rejections(),
        }
    )


@misc_bp.route("/api/pending-rejections/<int:pending_id>/confirm", methods=["POST"])
def confirm_pending_rejection(pending_id):
    """无人值守扫描排进队列的疑似拒信，人工在网页上确认——这时候才真的改
    application_status，走跟交互式扫描（email_rejection_scan.py apply）一样的落库路径。"""
    pending = next((p for p in list_pending_rejections() if p["id"] == pending_id), None)
    if not pending:
        return jsonify({"error": "待确认记录不存在"}), 404
    set_application_status(pending["job_id"], "rejected")
    add_job_note(pending["job_id"], pending["note"] or "邮件扫描识别为拒信", source="email_scan")
    remove_pending_rejection(pending_id)
    return jsonify({"ok": True})


@misc_bp.route("/api/pending-rejections/<int:pending_id>/dismiss", methods=["POST"])
def dismiss_pending_rejection(pending_id):
    """判断为误判，直接从待确认队列移除，不改职位状态。"""
    remove_pending_rejection(pending_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 通知
# 同步/AI分析这些后台线程任务完成时落一条记录（见各 _*_background 函数里的
# add_notification 调用），跟 /api/checklist 聚合的"当前有哪些条件成立"不同——通知是
# "发生过一件事"，看过之前一直在。不做单条已读追踪，打开下拉列表即视为看过、统一清零
# 未读数（见 mark_all_notifications_read），6 个页面的顶栏铃铛共用这两个接口。


@misc_bp.route("/api/notifications", methods=["GET"])
def get_notifications_route():
    return jsonify({"items": list_notifications(50), "unread": unread_notification_count()})


@misc_bp.route("/api/notifications/read_all", methods=["POST"])
def mark_notifications_read_route():
    mark_all_notifications_read()
    return jsonify({"ok": True})


@misc_bp.route("/api/checklist", methods=["POST"])
def add_checklist_item_route():
    data = request.get_json(force=True)
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "内容不能为空"}), 400
    if len(content) > MAX_CHECKLIST_ITEM_LENGTH:
        return jsonify({"error": f"内容太长（超过{MAX_CHECKLIST_ITEM_LENGTH}字符）"}), 400
    item_id = add_checklist_item(content)
    return jsonify({"id": item_id})


@misc_bp.route("/api/checklist/<int:item_id>", methods=["DELETE"])
def delete_checklist_item_route(item_id):
    deleted = delete_checklist_item(item_id)
    if not deleted:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify({"ok": True})


@misc_bp.route("/api/llm/stats", methods=["GET"])
def get_llm_stats():
    """LLM 调用流水的聚合视图：花了多少钱、哪个任务最容易失败、哪个模型慢。

    埋点见 llm.py 的 _CallRecord。默认看最近 30 天——days=0 表示全部历史。

    days=1（"今日"）刻意按**自然日**算（从本地零点到现在），不是"过去24小时"——
    2026-09-09 用户拿真实 DeepSeek 后台账单核对时发现两边对不上，根因就是这里原来
    统一用 `now - timedelta(days=days)` 滚动窗口，"今日"因此会把昨天傍晚以后的调用
    也算进来。DeepSeek 账单后台跟大多数计费面板一样按自然日展示，这里跟着对齐；
    days=7/30 保留滚动窗口语义不变——"过去7天/30天"本来就没有"自然7天"这个概念，
    不存在同样的歧义。
    """
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        return jsonify({"error": "days 必须是整数"}), 400
    since = None
    if days == 1:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    elif days > 0:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return jsonify(
        {
            "days": days,
            "by_task": llm_call_stats(since=since, group_by="task"),
            "by_model": llm_call_stats(since=since, group_by="model"),
            "totals": llm_call_totals(since=since),
        }
    )


@misc_bp.route("/api/collect/stats", methods=["GET"])
def get_collect_stats():
    """职位收集链路（jobspy 搜索 / tracker 同步 / how-you-fit 同步 / 手动贴链接）的
    运行流水聚合视图：每个来源最近跑了几次、找到/入库多少、失败率、失败原因分类。

    埋点见各模块调用 models.insert_collect_run() 的地方（`scraper.run_search_once`、
    `linkedin_tracker.sync_tracker_stage`、`linkedin_how_you_fit.sync_search`、
    `routes_search.add_jobs_by_url_route`），见 spec/roadmap.md「职位收集链路的
    错误处理生产级加固」缺口7。默认看最近 30 天——days=0 表示全部历史。
    """
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        return jsonify({"error": "days 必须是整数"}), 400
    since = None
    if days > 0:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return jsonify(
        {
            "days": days,
            "by_source": collect_run_stats(since=since, group_by="source"),
            "by_error_kind": collect_run_stats(since=since, group_by="error_kind"),
            "totals": collect_run_totals(since=since),
        }
    )


@misc_bp.route("/api/tracker", methods=["GET"])
def get_tracker_entries():
    cfg = load_config()
    tracker_path = cfg.get("tracker_xlsx_path") or os.path.expanduser("~/Downloads/JD匹配追踪表.xlsx")
    return jsonify(list_entries(tracker_path))


def _backfill_materials_from_tracker():
    """一次性回填：cover_letter/resume_bullets 这两列是新加的，之前生成过材料的历史职位
    还只存在追踪表 xlsx 里。按 公司+职位名 匹配一遍，把追踪表里已有的材料抄回 jobs 表，
    这样列表页/详情页不用再依赖 Excel 就能看到这些历史材料。只处理"分析过但库里还没有
    cover letter"的职位（见 models.list_jobs_missing_cover_letter），跑过一次之后这批
    职位就不会再进来，重启不会重复劳动。"""
    jobs = list_jobs_missing_cover_letter()
    if not jobs:
        return
    cfg = load_config()
    tracker_path = cfg.get("tracker_xlsx_path") or os.path.expanduser("~/Downloads/JD匹配追踪表.xlsx")
    entries_by_key = {
        make_dedupe_key(e.get("company"), e.get("job_title")): e for e in list_entries(tracker_path)
    }
    backfilled = 0
    for job in jobs:
        entry = entries_by_key.get(make_dedupe_key(job["company"], job["title"]))
        if not entry or not entry.get("cover_letter"):
            continue
        bullets = entry.get("resume_optimization_bullets")
        update_job_materials(
            job["id"],
            resume_path=job.get("resume_path"),
            cover_letter=entry.get("cover_letter"),
            resume_bullets=json.dumps(bullets, ensure_ascii=False) if bullets else None,
        )
        backfilled += 1
    if backfilled:
        logging.info("backfilled cover letter/resume bullets for %s historical job(s)", backfilled)
