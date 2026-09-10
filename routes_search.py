"""职位入库与批量分析：定时/手动搜索、贴链接、LinkedIn 各类列表同步、批量 AI 分析、公司国籍分类。

这些路由共用同一套"入库后台线程 → 排队分析 → 分类国籍"收尾模式，所以放在一起，而不是
按"搜索"和"AI分析"拆成两个文件——它们的收尾逻辑本来就是同一段代码在多处复用。
"""
import logging
import threading
import time
from datetime import datetime

import collect_errors
import resume_store
from config import load_config
from job_state import (
    acquire_linkedin_browser,
    finish_how_you_fit_batch,
    finish_how_you_fit_sync,
    finish_tracker_sync,
    how_you_fit_batch_error,
    how_you_fit_batch_queued_behind,
    how_you_fit_batch_result,
    how_you_fit_batch_syncing,
    how_you_fit_queued_behind,
    how_you_fit_sync_error,
    how_you_fit_sync_result,
    how_you_fit_syncing,
    release_linkedin_browser,
    request_stop,
    set_how_you_fit_batch_queued_behind,
    set_how_you_fit_queued_behind,
    set_tracker_queued_behind,
    start_how_you_fit_batch,
    start_how_you_fit_sync,
    start_tracker_sync,
    tracker_queued_behind,
    tracker_sync_error,
    tracker_sync_result,
    tracker_syncing,
)
from job_link import MAX_URLS, add_jobs_from_urls
from models import add_notification, insert_collect_run, set_application_status
from pipeline import analyze_pending_jobs, classify_company_origins, queue_pending_jobs
from resume_store import ResumeMissingError
from scraper import run_search_once
from web_helpers import need_resume_response

from flask import Blueprint, jsonify, request

search_bp = Blueprint("search", __name__)


# 程序启动时，数据库里可能积压着这个"自动分析"功能上线之前留下的一大批历史"待审核"职位
# （远超一次搜索通常会新增的量），一次性全部自动跑完可能要跑好几个小时。启动时的补跑先限制
# 只跑最近的这么多条，想继续清历史积压就再重启一次程序，或者把这个数字改大/改成 None。
STARTUP_BACKLOG_LIMIT = 5


def _analyze_pending_jobs_background(job_ids=None, limit=None, jobs=None):
    try:
        count = analyze_pending_jobs(job_ids=job_ids, limit=limit, jobs=jobs)
        logging.info("auto-analyzed %s pending job(s)", count)
        if count:
            add_notification("analysis", "AI 匹配分析完成", f"成功分析 {count} 条职位", level="success")
    except Exception:
        logging.exception("background auto-analyze failed")
        add_notification("analysis", "AI 匹配分析失败", level="error")


def _classify_company_origins_background(job_ids=None):
    try:
        result = classify_company_origins(job_ids=job_ids)
        logging.info("company origin classify done: %s", result)
        if result.get("classified"):
            add_notification(
                "company_origin", "公司国籍识别完成",
                f"{result['companies']} 家公司，判断出 {result['classified']} 条职位的归属",
                level="success",
            )
    except Exception:
        logging.exception("background company origin classify failed")
        add_notification("company_origin", "公司国籍识别失败", level="error")


def _has_enabled_how_you_fit_searches():
    cfg = load_config()
    return any(s.get("enabled", True) for s in (cfg.get("linkedin_how_you_fit_searches") or []))


@search_bp.route("/api/search/run", methods=["POST"])
def trigger_search():
    result = run_search_once()

    # 手动点「智能抓取」顺带同步 How You Fit（2026-08-23 用户明确要求）：跟每日定时
    # 任务的取舍一致——两个风险源互相独立，jobspy 搜索已经跑完不受影响，How You Fit
    # 批量同步单独起后台线程跑，慢（要开登录态浏览器逐条扫）也不卡这次请求的响应。
    # 没配置任何已启用的搜索、或批量同步已经在别处跑着，都不算错误，静默跳过——
    # 跟 sync_how_you_fit_all_route 的 409 不同，这里只是「顺带」触发，不是用户主动
    # 点的这个按钮，没必要因为撞车就报错。
    #
    # 跟专门按钮一样过 acquire_linkedin_browser 排队（2026-09-08）：这是"顺带"触发，
    # 不代表当下 LinkedIn 登录态浏览器一定空闲——用户完全可能刚点完「同步收藏」又点
    # 「智能抓取」，如果这里直接起线程 launch 浏览器，会跟正在跑的收藏同步在 Chromium
    # 的 profile 独占锁上撞车（这正是本来要修的问题，不能因为是"顺带"触发就绕开）。
    # 排上队跟单独点按钮没有区别，只是不需要用户等它排到再手动点一次。
    result["how_you_fit_started"] = False
    if _has_enabled_how_you_fit_searches() and start_how_you_fit_batch():
        label = "同步 LinkedIn 智能匹配推荐全部搜索"
        acquired, occupant = acquire_linkedin_browser(label, _sync_how_you_fit_all_background)
        if acquired:
            threading.Thread(target=_sync_how_you_fit_all_background, daemon=True).start()
        else:
            set_how_you_fit_batch_queued_behind(occupant)
        result["how_you_fit_started"] = True

    # 搜索本身不需要简历，所以没上传简历也照常抓——只是抓完不排队分析，在响应里带一个
    # need_resume 让前端提示"职位搜到了，想看匹配度得先上传简历"。这里如果跟着 409 掉，
    # 用户连职位列表都拿不到，等于因为一个下游功能的前置条件把上游功能也废了。
    if not resume_store.has_base_resume():
        result["need_resume"] = True
        result["need_resume_message"] = "职位已抓取，但还没上传简历，暂时无法自动分析匹配度。"
        return jsonify(result)

    # 先同步筛选+标记排队（纯本地DB/内存操作，很快），确保这次请求的响应返回时排队
    # 状态已经写好——前端拿到响应后会立刻刷新一次职位列表，如果排队状态是在后台线程里
    # 才标记的，容易跟这次刷新产生时序竞态，导致前端误判"当前没有职位在分析"从而不
    # 安排轮询，往后也不会再自动刷新，看起来像是分析没有自动开始（2026-08-15 实测踩过）。
    # 真正调用LLM的分析循环仍然放后台线程跑，不卡住这次请求的返回。
    to_analyze = queue_pending_jobs(job_ids=result.get("new_job_ids"))
    threading.Thread(
        target=_analyze_pending_jobs_background, kwargs={"jobs": to_analyze}, daemon=True
    ).start()
    # 公司国籍分类不需要等JD/完整分析，独立跑一遍，让"外企/国内公司"筛选尽快对新职位可用。
    threading.Thread(
        target=_classify_company_origins_background, kwargs={"job_ids": result.get("new_job_ids")}, daemon=True
    ).start()
    return jsonify(result)


@search_bp.route("/api/jobs/add_by_url", methods=["POST"])
def add_jobs_by_url_route():
    """手动贴 LinkedIn 职位链接入库到待审核。

    抓取是同步做的（不像"立即搜索一次"那样一股脑丢后台）：用户贴完链接就是要立刻知道
    每一条到底进没进库、没进是因为重复还是抓不到，这个结果没法用一句"已在后台开始"
    代替。条数上限见 job_link.MAX_URLS。入库之后接上的自动分析仍然是后台线程，跟
    /api/search/run 一条路。
    """
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("urls")
    if isinstance(raw, list):
        urls = [str(u).strip() for u in raw if str(u).strip()]
    else:
        urls = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
    if not urls:
        return jsonify({"error": "请先贴至少一条 LinkedIn 职位链接"}), 400
    if len(urls) > MAX_URLS:
        return jsonify({"error": f"一次最多 {MAX_URLS} 条链接，请分批提交"}), 400

    # 成败都往 collect_runs 记一行（2026-08-29，见 spec/roadmap.md「职位收集链路的
    # 错误处理生产级加固」缺口7），source 固定 manual_urls——tracker/how_you_fit
    # 内部批量调用 add_jobs_from_urls() 时不会走到这里（各自在自己的 sync 函数里
    # 记一行 tracker_<stage>/hyf_<id>），只有用户直接贴链接这个入口才算 manual_urls。
    run_started_iso = datetime.now().isoformat(timespec="seconds")
    run_started_at = time.time()
    collect_errors.reset_retry_count()
    try:
        result = add_jobs_from_urls(urls)
    except Exception as e:
        insert_collect_run(
            source="manual_urls", started_at=run_started_iso,
            duration_ms=int((time.time() - run_started_at) * 1000),
            found=len(urls), added=0, skipped_duplicate=0, skipped_irrelevant=0, failed=0,
            ok=0, error_kind=collect_errors.classify(e), error_detail=str(e) or e.__class__.__name__,
            retries=collect_errors.get_retry_count(), agent_used=0, suspicious=0,
        )
        raise
    insert_collect_run(
        source="manual_urls", started_at=run_started_iso,
        duration_ms=int((time.time() - run_started_at) * 1000),
        found=len(urls), added=len(result.get("added_ids") or []),
        skipped_duplicate=len([r for r in result.get("results") or [] if r.get("status") == "duplicate"]),
        skipped_irrelevant=0,
        failed=len([r for r in result.get("results") or [] if r.get("status") == "failed"]),
        ok=1, error_kind=None, error_detail=None,
        retries=collect_errors.get_retry_count(), agent_used=0, suspicious=0,
    )
    added_ids = result.get("added_ids") or []
    if added_ids and resume_store.has_base_resume():
        # enforce_relevance=False：手动贴进来的职位是用户自己挑的，不该再被当前搜索
        # 关键词/城市的粗筛挡在分析之外（见 pipeline.queue_pending_jobs 的说明）。
        to_analyze = queue_pending_jobs(job_ids=added_ids, enforce_relevance=False)
        threading.Thread(
            target=_analyze_pending_jobs_background, kwargs={"jobs": to_analyze}, daemon=True
        ).start()
        threading.Thread(
            target=_classify_company_origins_background, kwargs={"job_ids": added_ids}, daemon=True
        ).start()
    elif added_ids:
        # 跟 /api/search/run 同样的取舍：职位已经入库了，只是没法自动算匹配度，
        # 不该因为这个下游前置条件把入库结果也一起 409 掉。
        result["need_resume"] = True
        result["need_resume_message"] = "职位已入库，但还没上传简历，暂时无法自动分析匹配度。"
    return jsonify(result)


@search_bp.route("/api/jobs/sync_tracker/<stage>", methods=["GET"])
def get_tracker_sync_status_route(stage):
    """轮询这次同步的状态。跟"添加链接"不一样，这个操作没法在一次 HTTP 请求里同步等完
    （要开一次真实浏览器扫列表，慢且不确定要多久），所以拆成"POST 启动 + GET 查状态"
    两个接口，跟体检（resume_review）、题库起草（bank）同一个模式。

    stage 是"saved"（已收藏）/"applied"（已投递）/"interview"（面试）：几个列表结构
    一样，状态和路由合并成一组按 stage 参数区分，不为每个 stage 各写一套几乎相同的
    路由/状态代码。"""
    from linkedin_tracker import SUPPORTED_STAGES

    if stage not in SUPPORTED_STAGES:
        return jsonify({"error": f"暂不支持同步这个列表：{stage}"}), 404
    return jsonify({
        "syncing": tracker_syncing(stage),
        "result": tracker_sync_result(stage),
        "error": tracker_sync_error(stage),
        "queued_behind": tracker_queued_behind(stage),
    })


# stage 中文名，跟前端 TRACKER_STAGE_META（static/app.js）里的 label 保持一致，
# 通知文案和 toast 文案说的是同一件事。
_TRACKER_STAGE_LABELS = {"saved": "收藏列表", "applied": "已投递列表", "interview": "面试列表"}

# 跟 static/app.js 的 APPLICATION_STATUS_LABELS 保持一致——那边是职位卡片上显示用的，
# 这边是通知文案拼"从什么状态变成什么状态"用的，同一套中文措辞不能各写一份不同的。
_APPLICATION_STATUS_LABELS = {
    "not_applied": "待投", "applied": "已投递", "interviewing": "面试中",
    "rejected": "已拒绝", "declined": "已婉拒", "offer": "Offer",
}

# 通知/toast 里最多列几条明细，多了直接堆成一大段反而没法看，见下面
# _format_reconciled_note() 的说明。
_RECONCILED_DETAIL_LIMIT = 5


def _format_reconciled_note(reconciled_total, reconciled_details):
    """把 models.reconcile_application_status_from_linkedin() 的明细列表拼成通知/toast
    里"投递状态核对更新"那截文案。起因：2026-09-08 用户反馈只报一个"更新 2 条"看不出
    具体是哪两条、改了什么，得把明细摊开——每条职位标"公司·职位名：旧状态→新状态"；
    如果这条职位这次调用里 application_status 没变（只是被
    models._promote_reviewed_for_applied_jobs() 顺带补了 status，见
    reconcile_application_status_from_linkedin() 的说明），改标"标记为已审核"，不然
    "旧状态→旧状态"看着像没变化、容易让人以为是 bug。超过 _RECONCILED_DETAIL_LIMIT
    条只列前几条，后面折算成"等 N 条"，避免同步顺带修了一批历史积压时通知/toast被撑得
    很长。"""
    if not reconciled_total:
        return ""
    parts = []
    for detail in reconciled_details[:_RECONCILED_DETAIL_LIMIT]:
        before, after = detail["application_status_before"], detail["application_status_after"]
        if before != after:
            change = f"{_APPLICATION_STATUS_LABELS.get(before, before)}→{_APPLICATION_STATUS_LABELS.get(after, after)}"
        else:
            change = "标记为已审核"
        parts.append(f"{detail['company']}·{detail['title']}：{change}")
    if len(reconciled_details) > _RECONCILED_DETAIL_LIMIT:
        parts.append(f"等 {reconciled_total} 条")
    detail_text = "；".join(parts)
    return f" · 投递状态核对更新 {reconciled_total} 条（{detail_text}）"


def _sync_tracker_background(stage):
    from linkedin_tracker import sync_tracker_stage

    # 不管是刚拿到浏览器直接开跑、还是排队等前一个用完后才轮到自己，这一刻都已经不再
    # "排队中"了，清掉 queued_behind 免得轮询接口一直显示"还在等 XX"（见 job_state.py
    # 「LinkedIn 登录态浏览器排队」的说明）。
    set_tracker_queued_behind(stage, None)
    error = None
    result = None
    try:
        result = sync_tracker_stage(stage)
    except Exception as e:
        logging.exception("sync linkedin tracker (%s) failed", stage)
        error = str(e) or e.__class__.__name__
        add_notification(
            "sync_tracker", f"同步 LinkedIn {_TRACKER_STAGE_LABELS.get(stage, stage)}失败", error, level="error",
        )
    finally:
        added_ids = (result or {}).get("added_ids") or []
        # "已投递"列表同步进来的职位，LinkedIn 上已经是投过的了，职达这边也该同步反映成
        # 「已投递」，不然会一直显示"待投"——跟职位卡片上手动点"我投了"走的是同一个函数
        # （models.set_application_status），会顺带记 applied_at；这个时间戳只能是"同步
        # 这一刻"，不是真实投递日期（LinkedIn 页面上没有稳定可解析的投递日期字段），
        # 「超7天该跟进」提醒的判断基准会因此从这一刻开始算，不是真实投递时间，
        # 这点在 README 里向用户说明。"已收藏"列表不做这个处理，沿用原来入库即"新"的状态。
        #
        # "面试"列表同步进来的职位同理要标记，但标的是「面试中」不是「已投递」——LinkedIn
        # 把职位挪进"面试"列表后就不再出现在"已投递"列表里了，这些职位本身已经过了投递
        # 阶段，跟 application_status 的进度模型（applied < interviewing < offer，见
        # models._APPLICATION_PROGRESS_RANK）保持一致，不倒退成"已投递"。
        if stage == "applied" and added_ids:
            for job_id in added_ids:
                set_application_status(job_id, "applied")
        elif stage == "interview" and added_ids:
            for job_id in added_ids:
                set_application_status(job_id, "interviewing")
        if added_ids and not resume_store.has_base_resume():
            # 跟 add_jobs_by_url_route 同样的取舍：职位已经入库了，只是没法自动算匹配度，
            # 用一个字段告诉前端，不要因为这个下游前置条件把入库结果本身也当成失败。
            result["need_resume"] = True
            result["need_resume_message"] = "已同步入库，但还没上传简历，暂时无法自动分析匹配度。"
        finish_tracker_sync(stage, result=result, error=error)
        if result is not None:
            results_list = result.get("results") or []
            failed = len([r for r in results_list if r.get("status") == "failed"])
            reconciled_note = _format_reconciled_note(result.get("reconciled"), result.get("reconciled_details") or [])
            add_notification(
                "sync_tracker",
                f"同步 LinkedIn {_TRACKER_STAGE_LABELS.get(stage, stage)}完成",
                f"共找到 {result.get('total_found', 0)} 条 · 新入库 {len(added_ids)} 条 · 失败 {failed} 条{reconciled_note}",
                level="error" if failed else "success",
            )
        if added_ids and resume_store.has_base_resume():
            to_analyze = queue_pending_jobs(job_ids=added_ids, enforce_relevance=False)
            threading.Thread(
                target=_analyze_pending_jobs_background, kwargs={"jobs": to_analyze}, daemon=True
            ).start()
            threading.Thread(
                target=_classify_company_origins_background, kwargs={"job_ids": added_ids}, daemon=True
            ).start()
        # 让出 LinkedIn 登录态浏览器——如果排队里还有人等着（比如同时点的另一个同步
        # 按钮），这一句会直接把使用权交给它、在新线程里自动跑起来，不需要用户重新点。
        release_linkedin_browser()


@search_bp.route("/api/jobs/sync_tracker/<stage>", methods=["POST"])
def sync_tracker_route(stage):
    """同步 LinkedIn jobs-tracker 列表（stage="saved" 已收藏 / "applied" 已投递 / "interview" 面试）到职达。

    整个流程（开浏览器扫列表 + 逐条抓详情入库）放后台线程跑，请求立刻返回——跟
    "添加链接"（同步等待、逐条报告结果）不同，这里没法给用户一个"贴完立刻看到结果"的
    体验：列表可能有几十上百条，光是扫这个列表滚动加载就可能要一两分钟，再加上逐条抓
    详情，同步等待会让请求挂太久。前端改用轮询 GET 同一路径查进度，参照体检
    （resume_review）的既有模式。

    跟另一个也要用 LinkedIn 登录态浏览器的同步（另一个 stage、How You Fit 单条/批量）
    撞车时不再直接报错：`start_tracker_sync` 这把"这个 stage 是否已经在同步"的锁照常
    立刻生效（同一个 stage 重复点还是 409），但真正要用浏览器时会先排队，如果当下正被
    别的同步占用，就把这次请求存进队列、稍后占用者跑完自动接着跑，见 job_state.py
    「LinkedIn 登录态浏览器排队」。
    """
    from linkedin_tracker import SUPPORTED_STAGES

    if stage not in SUPPORTED_STAGES:
        return jsonify({"error": f"暂不支持同步这个列表：{stage}"}), 404
    if not start_tracker_sync(stage):
        return jsonify({"error": "上一次同步还在进行中，请稍等它完成"}), 409

    label = f"同步 LinkedIn {_TRACKER_STAGE_LABELS.get(stage, stage)}"

    def _start():
        _sync_tracker_background(stage)

    acquired, occupant = acquire_linkedin_browser(label, _start)
    if acquired:
        threading.Thread(target=_start, daemon=True).start()
        return jsonify({"started": True})
    set_tracker_queued_behind(stage, occupant)
    return jsonify({"started": True, "queued": True, "queued_behind": occupant})


def _get_how_you_fit_search(search_id):
    cfg = load_config()
    return next((s for s in (cfg.get("linkedin_how_you_fit_searches") or []) if s.get("id") == search_id), None)


def _how_you_fit_search_exists(search_id):
    return _get_how_you_fit_search(search_id) is not None


@search_bp.route("/api/jobs/sync_how_you_fit/<search_id>", methods=["GET"])
def get_how_you_fit_sync_status_route(search_id):
    """轮询单条 How You Fit 搜索的同步状态，跟 get_tracker_sync_status_route 同一个模式。
    search_id 不在当前配置里返回404（比如另一个标签页早已把它删了）。"""
    if not _how_you_fit_search_exists(search_id):
        return jsonify({"error": "这条 LinkedIn 智能匹配推荐搜索不存在（可能已被删除）"}), 404
    return jsonify({
        "syncing": how_you_fit_syncing(search_id),
        "result": how_you_fit_sync_result(search_id),
        "error": how_you_fit_sync_error(search_id),
        "queued_behind": how_you_fit_queued_behind(search_id),
    })


def _finish_how_you_fit_added(added_ids, result):
    """单条/批量同步共用的收尾：入库后排队分析+公司分类，跟 _sync_tracker_background
    一样，但没有"已投递"那种业务分支——How You Fit 结果就是普通候选职位，入库后状态
    照常是"新/待审核"。"""
    if added_ids and not resume_store.has_base_resume():
        result["need_resume"] = True
        result["need_resume_message"] = "已同步入库，但还没上传简历，暂时无法自动分析匹配度。"
    if added_ids and resume_store.has_base_resume():
        to_analyze = queue_pending_jobs(job_ids=added_ids, enforce_relevance=False)
        threading.Thread(
            target=_analyze_pending_jobs_background, kwargs={"jobs": to_analyze}, daemon=True
        ).start()
        threading.Thread(
            target=_classify_company_origins_background, kwargs={"job_ids": added_ids}, daemon=True
        ).start()


def _sync_how_you_fit_background(search_id, force_agent=False):
    from linkedin_how_you_fit import sync_search

    # 清掉"排队中"标记，理由跟 _sync_tracker_background 开头那句一致，见 job_state.py
    # 「LinkedIn 登录态浏览器排队」的说明。
    set_how_you_fit_queued_behind(search_id, None)
    error = None
    result = None
    try:
        result = sync_search(search_id, force_agent=force_agent)
    except Exception as e:
        logging.exception("sync linkedin how-you-fit search (%s) failed", search_id)
        error = str(e) or e.__class__.__name__
        add_notification("sync_how_you_fit", "同步 LinkedIn 智能匹配推荐搜索失败", error, level="error")
    finally:
        added_ids = (result or {}).get("added_ids") or []
        if result is not None:
            _finish_how_you_fit_added(added_ids, result)
            results_list = result.get("results") or []
            failed = len([r for r in results_list if r.get("status") == "failed"])
            skipped_irrelevant = len([r for r in results_list if r.get("status") == "skipped_irrelevant"])
            irrelevant_note = f" · 标题不符跳过 {skipped_irrelevant} 条" if skipped_irrelevant else ""
            add_notification(
                "sync_how_you_fit", "同步 LinkedIn 智能匹配推荐搜索完成",
                f"共找到 {result.get('total_found', 0)} 条 · 新入库 {len(added_ids)} 条 · 失败 {failed} 条{irrelevant_note}",
                level="error" if failed else "success",
            )
        finish_how_you_fit_sync(search_id, result=result, error=error)
        release_linkedin_browser()


@search_bp.route("/api/jobs/sync_how_you_fit/<search_id>", methods=["POST"])
def sync_how_you_fit_route(search_id):
    """同步单条 LinkedIn How You Fit 搜索到职达，结构跟 sync_tracker_route 一致
    （后台线程跑 + 轮询查进度，撞上别的 LinkedIn 浏览器同步时排队而不是直接报错，
    见 sync_tracker_route 的说明）。

    ?force_agent=1（设置页"用 agent 测试"按钮，2026-08-29）：跳过确定性扫描直接
    强制走 agent 兜底导航，用来肉眼观察 agent 打开的浏览器实际怎么操作——仍然是
    一次真实同步，收集到的职位照常入库，只是不通过 fetch_search_job_ids 那条路径
    收集，跟平时"确定性扫描数量不够才升级给 agent"的触发条件不同，其它都一样。"""
    search = _get_how_you_fit_search(search_id)
    if search is None:
        return jsonify({"error": "这条 LinkedIn 智能匹配推荐搜索不存在（可能已被删除）"}), 404
    if not start_how_you_fit_sync(search_id):
        return jsonify({"error": "上一次同步还在进行中，请稍等它完成"}), 409
    force_agent = request.args.get("force_agent") == "1"

    label = f"同步 LinkedIn 智能匹配推荐搜索：{search.get('name') or search_id}"

    def _start():
        _sync_how_you_fit_background(search_id, force_agent=force_agent)

    acquired, occupant = acquire_linkedin_browser(label, _start)
    if acquired:
        threading.Thread(target=_start, daemon=True).start()
        return jsonify({"started": True})
    set_how_you_fit_queued_behind(search_id, occupant)
    return jsonify({"started": True, "queued": True, "queued_behind": occupant})


@search_bp.route("/api/jobs/sync_how_you_fit_all", methods=["GET"])
def get_how_you_fit_batch_status_route():
    return jsonify({
        "syncing": how_you_fit_batch_syncing(),
        "result": how_you_fit_batch_result(),
        "error": how_you_fit_batch_error(),
        "queued_behind": how_you_fit_batch_queued_behind(),
    })


def _sync_how_you_fit_all_background():
    from linkedin_how_you_fit import sync_all_enabled_searches

    set_how_you_fit_batch_queued_behind(None)
    error = None
    summary = None
    try:
        summary = sync_all_enabled_searches()
    except Exception as e:
        logging.exception("sync all linkedin how-you-fit searches failed")
        error = str(e) or e.__class__.__name__
        add_notification("sync_how_you_fit", "同步 LinkedIn 智能匹配推荐全部搜索失败", error, level="error")
    finally:
        all_added_ids = []
        all_failed = 0
        all_skipped_irrelevant = 0
        search_errors = []
        for entry in (summary or {}).values():
            entry_result = entry.get("result") or {}
            all_added_ids += entry_result.get("added_ids") or []
            entry_results_list = entry_result.get("results") or []
            all_failed += len([r for r in entry_results_list if r.get("status") == "failed"])
            all_skipped_irrelevant += len([r for r in entry_results_list if r.get("status") == "skipped_irrelevant"])
            # entry["error"] 是某条搜索整个跑失败（sync_search 直接抛异常，比如登录
            # profile 被占用、登录态失效），跟上面 entry_results_list 里单条 URL
            # 入库失败的 "failed" 状态是两回事——之前这里完全没读这个字段，导致"这
            # 条搜索从头到尾就没跑起来"被悄悄漏计，通知永远显示"失败 0 条 · 成功"，
            # 掩盖了真实原因（2026-09-08 用户反馈"每次都抓不到内容"但通知一直显示
            # 成功，排查发现就是这里）。
            if entry.get("error"):
                search_errors.append(entry["error"])
        placeholder_result = {"added_ids": all_added_ids, "summary": summary}
        if all_added_ids:
            _finish_how_you_fit_added(all_added_ids, placeholder_result)
        if summary is not None:
            irrelevant_note = f" · 标题不符跳过 {all_skipped_irrelevant} 条" if all_skipped_irrelevant else ""
            search_error_note = (
                f" · {len(search_errors)} 条搜索整体失败：{'; '.join(search_errors)}" if search_errors else ""
            )
            add_notification(
                "sync_how_you_fit", "同步 LinkedIn 智能匹配推荐全部搜索完成",
                f"共 {len(summary)} 条搜索 · 新入库 {len(all_added_ids)} 条 · 失败 {all_failed} 条"
                f"{irrelevant_note}{search_error_note}",
                level="error" if (all_failed or search_errors) else "success",
            )
        finish_how_you_fit_batch(result=placeholder_result, error=error)
        release_linkedin_browser()


@search_bp.route("/api/jobs/sync_how_you_fit_all", methods=["POST"])
def sync_how_you_fit_all_route():
    """手动"立即同步全部"入口：跟每日定时任务用的是同一个
    linkedin_how_you_fit.sync_all_enabled_searches()，用户配好搜索不用等到第二天
    定时任务才能验证生效。`start_how_you_fit_batch()` 这把锁只挡"另一次批量同步也在
    跑"（同类型重复点，409）；跟单条同步、tracker 同步撞车（不同类型）现在走排队
    （见 sync_tracker_route 的说明），不再是"不强行互斥、指望不要撞上"——2026-09-08
    之前两者确实可能同时真的 launch 浏览器，撞在 Chromium 的 profile 独占锁上失败。"""
    if not start_how_you_fit_batch():
        return jsonify({"error": "上一次批量同步还在进行中，请稍等它完成"}), 409

    label = "同步 LinkedIn 智能匹配推荐全部搜索"
    acquired, occupant = acquire_linkedin_browser(label, _sync_how_you_fit_all_background)
    if acquired:
        threading.Thread(target=_sync_how_you_fit_all_background, daemon=True).start()
        return jsonify({"started": True})
    set_how_you_fit_batch_queued_behind(occupant)
    return jsonify({"started": True, "queued": True, "queued_behind": occupant})


@search_bp.route("/api/jobs/analyze_all", methods=["POST"])
def analyze_all_route():
    # 顶部"AI分析"按钮：跟 /api/search/run 一样，先同步筛选+标记排队（见 queue_pending_jobs
    # 的注释），响应返回时前端就能立刻看到"排队中"状态；真正调用LLM的分析循环放后台线程跑。
    # 不传 job_ids/limit：处理"待审核"里所有还没分析成功过的职位，包括历史积压
    # （对应 roadmap 里"历史积压批量清理入口"这条）。
    #
    # 简历检查必须在 queue_pending_jobs 之前：排队标记一旦打上，前端就会开始轮询"分析中"，
    # 而后台线程会立刻对每条职位抛"没有简历"并把错误写进 analysis_error——等于一次点击
    # 就给几十条职位刷上一片失败记录，还得手动清。
    try:
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        return need_resume_response(e)

    to_analyze = queue_pending_jobs()
    threading.Thread(
        target=_analyze_pending_jobs_background, kwargs={"jobs": to_analyze}, daemon=True
    ).start()
    return jsonify({"started": True, "count": len(to_analyze)})


@search_bp.route("/api/jobs/analyze_stop", methods=["POST"])
def analyze_stop_route():
    # 设置停止标志，正在跑的分析循环（analyze_pending_jobs）每跑完一条职位会检查一次，
    # 之后不再继续下一条；当前正在分析的这一条LLM调用没法中途打断，会自然跑完。
    request_stop()
    return jsonify({"stopping": True})


@search_bp.route("/api/jobs/classify_origin", methods=["POST"])
def classify_origin_route():
    # 批量给数据库里所有还没判断过公司国籍的职位（不限状态）补分类，后台跑、立刻返回；
    # 用于本次功能刚上线时回填历史积压，跑完刷新页面就能看到"国内公司"筛选下有结果了。
    threading.Thread(target=_classify_company_origins_background, daemon=True).start()
    return jsonify({"started": True})
