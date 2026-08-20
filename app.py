import json
import logging
import os
import threading
from datetime import datetime

from flask import Flask, abort, jsonify, render_template, request, send_file

from config import load_config, save_config
from easy_apply import EasyApplyError, EasyApplyInProgress, run_easy_apply
from llm import LLM_TASKS, MODELS, get_model
from llm import resolve as llm_resolve
from job_link import MAX_URLS, add_jobs_from_urls
from linkedin_company import resolve_company_ids
from job_state import (
    bank_error,
    bank_generating,
    clear_discard,
    discard_job,
    easy_apply_opening,
    finish_bank_generation,
    finish_easy_apply,
    finish_resume_review,
    get_easy_apply_error,
    get_easy_apply_states,
    get_interview_prep_states,
    get_materials_states,
    get_states,
    interview_prep_in_progress,
    materials_in_progress,
    request_materials_stop,
    request_stop,
    resume_review_error,
    resume_review_generating,
    start_bank_generation,
    start_easy_apply,
    start_resume_review,
)
from models import (
    BANK_CATEGORIES,
    add_bank_item,
    add_checklist_item,
    add_dismiss_reason,
    add_job_note,
    annotate_similar_groups,
    delete_bank_item,
    delete_checklist_item,
    delete_interview_prep,
    delete_job_note,
    get_bank_item,
    get_job,
    get_latest_interview_prep,
    get_latest_preference_profile,
    get_latest_resume_review,
    init_db,
    job_ids_with_dismiss_reason,
    job_ids_with_interview_prep,
    list_bank_items,
    list_checklist_items,
    list_interview_preps,
    list_job_notes,
    list_jobs,
    list_jobs_missing_cover_letter,
    list_jobs_with_tailored_resume,
    list_runs,
    list_stale_applications,
    make_dedupe_key,
    note_counts,
    set_application_status,
    set_job_starred,
    set_job_status,
    set_job_tags,
    update_bank_item,
    update_job_materials,
)
from pipeline import (
    analyze_and_record_safe,
    analyze_pending_jobs,
    build_optimized_resume,
    chat_about_job,
    chat_bank_answer,
    chat_bank_assistant,
    classify_company_origins,
    find_tracker_entry,
    generate_bank_draft,
    generate_interview_prep_safe,
    generate_materials_batch,
    generate_materials_for_job_safe,
    maybe_refresh_preference_profile,
    queue_pending_jobs,
    refetch_jd,
    refetch_missing_jd_jobs,
    run_resume_review,
)
import resume_store
from resume_store import ResumeMissingError, ResumeUploadError
from scheduler import start_scheduler, reschedule
from scraper import run_search_once
from tracker_utils import list_entries

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
# 简历上传的大小上限。超过这个数 Flask 会在读请求体之前就返回 413，不会先把几百MB
# 读进内存再让我们自己判断。resume_store 里还有一道同样数值的校验，因为那边是"文件已
# 经落到临时文件了"的最后一关（比如以后有别的入口不走 HTTP）。
app.config["MAX_CONTENT_LENGTH"] = resume_store.MAX_RESUME_BYTES

init_db()


def need_resume_response(e):
    """把"还没上传简历"翻译成一个前端能识别的响应。

    用 409 而不是 400：这不是请求本身写错了，是服务端当前状态不满足前置条件，重试也没用，
    得先去做另一件事（上传简历）。need_resume 这个标记让前端能弹"去上传"而不是一句
    干巴巴的错误 toast——参见 static/common.js 的 handleNeedResume()。
    """
    return jsonify({"error": str(e), "need_resume": True}), 409


@app.route("/")
def index():
    return render_template("index.html")


# 面试相关的两块内容各自是独立页面，不再是主页上的弹窗：都属于"坐下来看很久 / 一边看一边改"
# 的场景，而弹窗有三个硬伤——生成是后台跑的但轮询跟弹窗生命周期绑死、内容长却被塞进
# max-height:92vh 的内滚容器、没有 URL 没法单独开一个标签页挂着。理由详见
# spec/tech-solution.md。「匹配分析」相反，是在列表里扫一眼就关，继续留在弹窗里。
@app.route("/interview")
def interview_bank_page():
    return render_template("interview.html")


@app.route("/jobs/<int:job_id>/interview")
def job_interview_page(job_id):
    if not get_job(job_id):
        abort(404)
    return render_template("job_interview.html", job_id=job_id)


# 职位详情页：以前是主页上一个 680px 的弹窗（只放「匹配分析」），现在多了 AI 对话和备注，
# 两样都需要长时间挂着交互，弹窗的三个硬伤（轮询跟弹窗生命周期绑死、内容被塞进内滚容器、
# 没有独立URL）跟面试准备当初搬出弹窗是同一个理由，详见 spec/tech-solution.md。
@app.route("/jobs/<int:job_id>")
def job_detail_page(job_id):
    if not get_job(job_id):
        abort(404)
    return render_template("job_detail.html", job_id=job_id)


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/models", methods=["GET"])
def get_models():
    """可选模型清单 + 每个功能位当前选的是谁。

    前端下拉直接照这份渲染，不在 JS 里再抄一份模型名单——抄了就一定会出现"界面上多了一个
    选项、后端不认"或者反过来的情况。
    """
    cfg = load_config()
    tasks = cfg.get("llm_tasks") or {}
    return jsonify(
        {
            "models": MODELS,
            "tasks": LLM_TASKS,
            # 留空的功能位在这里补上它实际会用的那个模型（全局默认），前端不用自己算一遍回退逻辑
            "llm_tasks": {t: tasks.get(t) or "" for t in LLM_TASKS},
            "fallback": llm_resolve(cfg)[1],
        }
    )


@app.route("/api/config", methods=["POST"])
def update_config():
    cfg = load_config()
    data = request.get_json(force=True)

    for key in (
        "country_indeed",
        "tracker_xlsx_path",
        # base_resume_path 刻意不在这里：它现在只由「我的简历」页的上传/删除流程写。
        # 留在白名单里的话，设置页每次保存都会把表单里那个（现在是只读展示的）字段一起
        # 提交回来，一旦它是空串就会把用户刚传的简历悄悄取消引用。
        "resume_output_dir",
        "llm_provider",
        "anthropic_model",
        "deepseek_model",
    ):
        if key in data:
            cfg[key] = data[key]
    if "schedule_enabled" in data:
        cfg["schedule_enabled"] = bool(data["schedule_enabled"])
    for key in ("results_wanted", "days_old", "schedule_hour", "schedule_minute"):
        if key in data:
            raw = data[key]
            # 设置页"只抓取最近几天内发布的职位"的输入框文案就是"留空或0表示不限"，
            # 用户清空该字段保存是预期操作；int("") 会直接抛异常，之前会把整个保存请求
            # 崩成裸的500页面（还会连带其它已经改好的字段一起保存不进去）。
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                cfg[key] = 0
                continue
            try:
                cfg[key] = int(raw)
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} 必须是数字"}), 400
    for key in ("keywords", "locations", "sites"):
        if key in data:
            cfg[key] = [v.strip() for v in data[key] if v and v.strip()]
    if "linkedin_target_companies" in data:
        names = [v.strip() for v in data["linkedin_target_companies"] if v and str(v).strip()]
        existing = {c["name"]: c for c in (cfg.get("linkedin_target_companies") or [])}
        # 只解析新增的名字、或者上次解析失败的名字——已经成功解析过的不用每次保存设置都
        # 重新抓一遍公司主页（解析是真实网络请求，量一大会让保存设置变得很慢）。
        to_resolve = [n for n in names if existing.get(n, {}).get("status") != "resolved"]
        resolved = {}
        if to_resolve:
            try:
                resolved = resolve_company_ids(to_resolve)
            except Exception as e:
                logging.exception("resolve_company_ids failed")
                return jsonify({"error": f"解析公司列表失败：{e}"}), 400
        target_list = []
        for n in names:
            if n in resolved:
                info = resolved[n]
                target_list.append({"name": n, "company_id": info["company_id"], "status": info["status"]})
            elif n in existing:
                target_list.append(existing[n])
            else:
                target_list.append({"name": n, "company_id": None, "status": "failed"})
        cfg["linkedin_target_companies"] = target_list
    if "easy_apply_profile" in data:
        # 前端一次性提交整份 profile（三个固定字段 + extra_answers 列表），直接整体替换，
        # 不做逐字段合并——设置页每次保存都是带着当前完整表单内容提交的，不存在"只改一个
        # 字段、其它字段要保留旧值"的场景。
        cfg["easy_apply_profile"] = data["easy_apply_profile"]
    if "llm_tasks" in data:
        # 这里**按 key 合并**（跟上面的 easy_apply_profile 相反）：面试页顶栏的模型下拉每次
        # 只提交自己那一个功能位，整体替换会把另外两个悄悄清空、回退到全局默认。
        # 先 dict() 拷一份：config.load_config() 是浅合并，没配过 llm_tasks 时这里拿到的
        # 就是 DEFAULT_CONFIG 里那个字典本身，直接改会污染进程内的默认值。
        tasks = dict(cfg.get("llm_tasks") or {})
        for key, value in (data["llm_tasks"] or {}).items():
            if key not in LLM_TASKS:
                return jsonify({"error": f"未知的功能位：{key}"}), 400
            value = (value or "").strip()
            if value:
                try:
                    get_model(value)  # 界面上能选的才准存，免得存进去一个打不通的模型名
                except RuntimeError as e:
                    return jsonify({"error": str(e)}), 400
            tasks[key] = value
        cfg["llm_tasks"] = tasks

    save_config(cfg)
    reschedule(cfg["schedule_hour"], cfg["schedule_minute"], cfg["schedule_enabled"])
    return jsonify(cfg)


# 程序启动时，数据库里可能积压着这个"自动分析"功能上线之前留下的一大批历史"待审核"职位
# （远超一次搜索通常会新增的量），一次性全部自动跑完可能要跑好几个小时。启动时的补跑先限制
# 只跑最近的这么多条，想继续清历史积压就再重启一次程序，或者把这个数字改大/改成 None。
STARTUP_BACKLOG_LIMIT = 5


def _analyze_pending_jobs_background(job_ids=None, limit=None, jobs=None):
    try:
        count = analyze_pending_jobs(job_ids=job_ids, limit=limit, jobs=jobs)
        logging.info("auto-analyzed %s pending job(s)", count)
    except Exception:
        logging.exception("background auto-analyze failed")


def _classify_company_origins_background(job_ids=None):
    try:
        result = classify_company_origins(job_ids=job_ids)
        logging.info("company origin classify done: %s", result)
    except Exception:
        logging.exception("background company origin classify failed")


def _profile_refresh_background(force=False):
    try:
        maybe_refresh_preference_profile(force=force)
    except Exception:
        # maybe_refresh_preference_profile() 内部已经把"生成失败"落库成 error 行了，
        # 这里兜的是更早的异常（比如攒计数的那次查询本身就出错）。
        logging.exception("background preference profile refresh failed")


@app.route("/api/search/run", methods=["POST"])
def trigger_search():
    result = run_search_once()
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


@app.route("/api/jobs/add_by_url", methods=["POST"])
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

    result = add_jobs_from_urls(urls)
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


@app.route("/api/jobs/analyze_all", methods=["POST"])
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


@app.route("/api/jobs/analyze_stop", methods=["POST"])
def analyze_stop_route():
    # 设置停止标志，正在跑的分析循环（analyze_pending_jobs）每跑完一条职位会检查一次，
    # 之后不再继续下一条；当前正在分析的这一条LLM调用没法中途打断，会自然跑完。
    request_stop()
    return jsonify({"stopping": True})


@app.route("/api/jobs/classify_origin", methods=["POST"])
def classify_origin_route():
    # 批量给数据库里所有还没判断过公司国籍的职位（不限状态）补分类，后台跑、立刻返回；
    # 用于本次功能刚上线时回填历史积压，跑完刷新页面就能看到"国内公司"筛选下有结果了。
    threading.Thread(target=_classify_company_origins_background, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    status = request.args.get("status")
    jobs = list_jobs(status=status)
    states = get_states()
    easy_apply_states = get_easy_apply_states()
    prep_states = get_interview_prep_states()
    materials_states = get_materials_states()
    # 一次查询取回"哪些职位已经有面试准备"/"每条职位有几条备注"，而不是逐条职位查一次库（N+1）。
    prep_job_ids = job_ids_with_interview_prep()
    notes_count_by_job = note_counts()
    dismiss_reason_job_ids = job_ids_with_dismiss_reason()
    for job in jobs:
        job["analysis_state"] = states.get(job["id"])
        job["easy_apply_state"] = easy_apply_states.get(job["id"])
        if job["easy_apply_state"] == "error":
            job["easy_apply_error"] = get_easy_apply_error(job["id"])
        job["interview_prep_state"] = prep_states.get(job["id"])
        job["has_interview_prep"] = job["id"] in prep_job_ids
        job["materials_state"] = materials_states.get(job["id"])
        job["note_count"] = notes_count_by_job.get(job["id"], 0)
        job["has_dismiss_reason"] = job["id"] in dismiss_reason_job_ids
    jobs = annotate_similar_groups(jobs)
    return jsonify(jobs)


@app.route("/api/jobs/<int:job_id>", methods=["GET"])
def get_job_route(job_id):
    """单条职位。面试准备页/职位详情页只关心一条职位，没必要跟主页一样把整个列表拉回来
    再 find——尤其是生成期间每隔几秒就要查一次状态。字段跟列表接口保持一致。"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    job["interview_prep_state"] = get_interview_prep_states().get(job_id)
    job["has_interview_prep"] = job_id in job_ids_with_interview_prep()
    job["materials_state"] = get_materials_states().get(job_id)
    return jsonify(job)


@app.route("/api/jobs/<int:job_id>/status", methods=["POST"])
def update_job_status(job_id):
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("new", "reviewed", "dismissed"):
        return jsonify({"error": "invalid status"}), 400
    set_job_status(job_id, status)
    if status == "dismissed":
        # 标记忽略即中断：如果这条职位正好在跑分析（排队中或LLM调用正在进行），把它的结果
        # 标成作废——批量循环不会因此停下来，会正常轮到下一条（见 job_state.discard_job
        # 的说明，这是它跟顶部"停止分析"按钮唯一的区别）。
        discard_job(job_id)
    else:
        # 用户反悔、把状态改回"新"或"已收藏"：清掉可能残留的作废标记，不然这条职位
        # 下次被排进批次时会莫名其妙被当场丢弃结果。
        clear_discard(job_id)
    return jsonify({"ok": True})


# 预设忽略原因（来自 spec/product-review.md 的 P0-3），用户也可以只填自由文本不选标签，
# 或者两者都填。跟"忽略"本身解耦——见 add_dismiss_reason_route 的说明。
DISMISS_REASON_TAGS = ("薪资不符", "职能不对", "公司不感兴趣", "地点", "行业", "层级不匹配")


@app.route("/api/jobs/<int:job_id>/dismiss_reason", methods=["POST"])
def add_dismiss_reason_route(job_id):
    """记一次忽略原因。刻意不要求这条职位当前状态一定是 dismissed——补录冷启动数据时
    (见 static/app.js 已忽略卡片上的"记录忽略原因"入口) 职位可能早就被忽略过、状态没变化，
    这里只负责存一条原因记录，不去校验/联动 jobs.status。"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    data = request.get_json(force=True)
    tags = data.get("tags") or []
    note = (data.get("note") or "").strip()
    if not isinstance(tags, list) or any(t not in DISMISS_REASON_TAGS for t in tags):
        return jsonify({"error": "invalid tags"}), 400
    if not tags and not note:
        return jsonify({"error": "原因不能为空"}), 400
    add_dismiss_reason(job_id, tags, note)
    # 攒够阈值才会真的触发一次 LLM 调用（见 maybe_refresh_preference_profile），这里无论
    # 有没有攒够都后台线程里查一次，不阻塞保存这个动作本身。
    threading.Thread(target=_profile_refresh_background, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/preferences", methods=["GET"])
def get_preference_profile_route():
    return jsonify(get_latest_preference_profile() or {})


@app.route("/api/preferences/regenerate", methods=["POST"])
def regenerate_preference_profile_route():
    """手动强制重新生成，跳过阈值检查——用于补录完冷启动数据后想立刻验证效果，
    不用真的再攒够 5 条新原因。"""
    threading.Thread(target=_profile_refresh_background, kwargs={"force": True}, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/jobs/<int:job_id>/starred", methods=["POST"])
def update_job_starred(job_id):
    """切换"重点关注"标记。跟 /status 分开是因为两者是独立维度（见 models.py 里
    starred 列的说明），标记关注不应该顺带改动审核状态。"""
    data = request.get_json(force=True)
    starred = data.get("starred")
    if not isinstance(starred, bool):
        return jsonify({"error": "invalid starred"}), 400
    set_job_starred(job_id, starred)
    return jsonify({"ok": True})


# 预设标签，用户可以从这几个里选，也可以自己敲一个新的（见 normalize_tags 的校验）。
PRESET_TAGS = ("AI", "ML", "remote", "tech")
MAX_TAGS_PER_JOB = 10
MAX_TAG_LENGTH = 20


def normalize_tags(raw_tags):
    """清洗前端传来的标签列表：去空白、去空项、限长度、限个数、大小写不敏感去重
    （保留第一次出现时的大小写）。标签文本本身不允许含逗号——库里是逗号分隔存的
    （见 models.set_job_tags），含逗号会在读回来时被错误拆成两个标签。"""
    if not isinstance(raw_tags, list):
        raise ValueError("tags 必须是字符串数组")
    seen = set()
    cleaned = []
    for t in raw_tags:
        if not isinstance(t, str):
            raise ValueError("标签必须是字符串")
        t = t.strip()
        if not t:
            continue
        if "," in t or "，" in t:
            raise ValueError(f"标签不能包含逗号：{t}")
        if len(t) > MAX_TAG_LENGTH:
            raise ValueError(f"标签太长（超过{MAX_TAG_LENGTH}字符）：{t}")
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
    if len(cleaned) > MAX_TAGS_PER_JOB:
        raise ValueError(f"标签最多{MAX_TAGS_PER_JOB}个")
    return cleaned


@app.route("/api/jobs/<int:job_id>/tags", methods=["POST"])
def update_job_tags(job_id):
    data = request.get_json(force=True)
    try:
        tags = normalize_tags(data.get("tags"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    set_job_tags(job_id, tags)
    return jsonify({"ok": True, "tags": tags})


APPLICATION_STATUSES = ("not_applied", "applied", "interviewing", "rejected", "offer", "declined")


@app.route("/api/jobs/<int:job_id>/application_status", methods=["POST"])
def update_application_status(job_id):
    data = request.get_json(force=True)
    application_status = data.get("application_status")
    if application_status not in APPLICATION_STATUSES:
        return jsonify({"error": "invalid application_status"}), 400
    set_application_status(job_id, application_status)

    resp = {"ok": True}
    if application_status == "interviewing":
        # 改成"面试中"就自动开始生成面试准备材料——这是用户真正需要材料的那一刻，
        # 不用再多点一次按钮。整段用 try/except 包住：面试准备生成失败（没配API key、
        # 追踪表被Excel占用、LLM报错等）绝不能连累"改投递状态"这个纯本地操作失败，
        # 那是两件事，状态本身已经写进库了。
        try:
            resp["interview_prep_started"] = _maybe_start_interview_prep(job_id)
        except Exception:
            logging.exception("投递状态改为面试中后，触发面试准备生成失败（不影响状态更新）")
    return jsonify(resp)


@app.route("/api/jobs/<int:job_id>/analyze", methods=["POST"])
def analyze_job_route(job_id):
    try:
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        # 在 analyze_and_record_safe 之前拦：那个 safe 包装会把异常写进 jobs.analysis_error，
        # 于是这条职位会一直挂着"分析失败"的红标，哪怕用户马上就传了简历。"还没上传简历"
        # 不是这条职位的问题，不该记在它头上。
        return need_resume_response(e)
    try:
        result = analyze_and_record_safe(job_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/jobs/<int:job_id>/resume", methods=["GET"])
def download_resume(job_id):
    job = get_job(job_id)
    if not job or not job.get("resume_path"):
        abort(404, description="该职位还没有生成定制简历")
    if not os.path.isfile(job["resume_path"]):
        abort(404, description="简历文件不存在，可能已被移动或删除")
    return send_file(job["resume_path"], as_attachment=False)


@app.route("/api/jobs/<int:job_id>/analysis", methods=["GET"])
def get_job_analysis_route(job_id):
    """职位详情页左栏要展示的完整匹配分析结论（职位内容/任职要求/技能匹配等一整套），
    只存在追踪表 xlsx 里（jobs 表没有完整落库，见 pipeline.find_tracker_entry 的说明）。
    单独开一个接口按 id 查一条，而不是让前端拉 /api/tracker 整表再自己 find——那样每次
    打开一个职位详情页都要把整张 Excel 解析一遍，代价不成比例。"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    entry = find_tracker_entry(job["company"], job["title"])
    return jsonify(entry or {})


# ---------------------------------------------------------------- 材料生成（定制简历 + Cover Letter）


def _materials_background(job_id):
    try:
        result = generate_materials_for_job_safe(job_id)
        logging.info("materials generated for job %s: %s", job_id, {k: bool(v) for k, v in result.items()})
    except Exception:
        logging.exception("materials generation failed for job %s", job_id)


@app.route("/api/jobs/<int:job_id>/generate_materials", methods=["POST"])
def generate_materials_route(job_id):
    # 生成要 30-60 秒（一次简历改写+cover letter的LLM调用），同步等待容易重演 refetch_jd
    # 那次"Failed to fetch"（见 refetch_jd_route 的注释），放后台线程跑，前端轮询
    # /api/jobs 的 materials_state 看进度。
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    if job.get("overall_match") is None:
        return jsonify({"error": "请先完成 AI 分析，再生成定制简历和 Cover Letter"}), 400
    if materials_in_progress(job_id):
        return jsonify({"error": "这条职位的材料正在生成中，请稍等"}), 409
    try:
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        return need_resume_response(e)
    threading.Thread(target=_materials_background, args=(job_id,), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/jobs/generate_materials", methods=["POST"])
def generate_materials_batch_route():
    """批量生成：body {job_ids: [...]}，通常是"当前筛选出来的职位"。已经生成过材料的
    职位会被后端跳过（见 pipeline.generate_materials_batch），响应里的 skipped 让前端能
    告诉用户"跳过了几条已经生成过的"，不用自己先查一遍。"""
    data = request.get_json(force=True)
    job_ids = data.get("job_ids")
    if not isinstance(job_ids, list) or not job_ids:
        return jsonify({"error": "job_ids 不能为空"}), 400
    try:
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        return need_resume_response(e)

    def _batch_background():
        try:
            result = generate_materials_batch(job_ids)
            logging.info("batch materials generation done: %s", result)
        except Exception:
            logging.exception("batch materials generation failed")

    threading.Thread(target=_batch_background, daemon=True).start()
    return jsonify({"started": True, "count": len(job_ids)})


@app.route("/api/jobs/generate_materials_stop", methods=["POST"])
def generate_materials_stop_route():
    request_materials_stop()
    return jsonify({"stopping": True})


# ---------------------------------------------------------------- 职位AI对话 / 备注


@app.route("/api/jobs/<int:job_id>/chat", methods=["POST"])
def job_chat_route(job_id):
    """职位详情页的自由问答，同步返回（跟题库对话同一个考虑：单轮等待在十几秒到一分钟
    量级，threaded=True 本来就能并发处理，不值得为它再搭一套后台状态+轮询）。"""
    if not get_job(job_id):
        return jsonify({"error": "职位不存在"}), 404
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "说点什么吧"}), 400
    try:
        reply = chat_about_job(job_id, message, history=data.get("history"))
        return jsonify({"reply": reply})
    except Exception as e:
        logging.exception("job chat failed")
        return jsonify({"error": str(e) or e.__class__.__name__}), 500


MAX_NOTE_LENGTH = 4000


@app.route("/api/jobs/<int:job_id>/notes", methods=["GET"])
def list_job_notes_route(job_id):
    return jsonify(list_job_notes(job_id))


@app.route("/api/jobs/<int:job_id>/notes", methods=["POST"])
def add_job_note_route(job_id):
    """新增一条备注。source 区分手写（默认）还是从职位AI对话里一键记下来的
    （见 models.job_notes 的说明），前端「📌 记进备注」按钮传 source=chat。"""
    if not get_job(job_id):
        return jsonify({"error": "职位不存在"}), 404
    data = request.get_json(force=True)
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "备注内容不能为空"}), 400
    if len(content) > MAX_NOTE_LENGTH:
        return jsonify({"error": f"备注太长（超过{MAX_NOTE_LENGTH}字符）"}), 400
    source = data.get("source") or "manual"
    if source not in ("manual", "chat"):
        return jsonify({"error": "invalid source"}), 400
    note_id = add_job_note(job_id, content, source=source)
    return jsonify({"id": note_id})


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
def delete_job_note_route(note_id):
    if not delete_job_note(note_id):
        return jsonify({"error": "记录不存在"}), 404
    return jsonify({"ok": True})


def _find_cover_letter(job):
    """这条职位对应的 cover letter 全文（如果生成过），用于 Easy Apply 尽力而为填写
    cover letter 字段；找不到就返回 None，不影响其它步骤。

    优先读 jobs 表自己的 cover_letter 列（材料生成时会直接写进去），追踪表只是
    历史职位的兜底——那些是在 cover_letter 列加上去之前生成的，还没被回填过。"""
    if job.get("cover_letter"):
        return job["cover_letter"]
    entry = find_tracker_entry(job["company"], job["title"])
    return entry.get("cover_letter") if entry else None


def _easy_apply_background(job_id):
    job = get_job(job_id)
    job["cover_letter"] = _find_cover_letter(job)
    job["easy_apply_profile"] = load_config().get("easy_apply_profile") or {}
    try:
        result = run_easy_apply(job)
        logging.info("easy apply opened for review: job %s, %s", job_id, result)
        finish_easy_apply(job_id, True)
    except (EasyApplyError, EasyApplyInProgress) as e:
        logging.info("easy apply failed for job %s: %s", job_id, e)
        finish_easy_apply(job_id, False, str(e))
    except Exception as e:
        logging.exception("easy apply unexpected error for job %s", job_id)
        finish_easy_apply(job_id, False, str(e))


@app.route("/api/jobs/<int:job_id>/easy_apply", methods=["POST"])
def easy_apply_route(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    if (job.get("site") or "").lower() != "linkedin":
        return jsonify({"error": "仅支持 LinkedIn 职位"}), 400
    if not job.get("resume_path") or not os.path.isfile(job["resume_path"]):
        return jsonify({"error": "该职位还没有生成定制简历"}), 400
    if easy_apply_opening():
        return jsonify({"error": "已经有一次 Easy Apply 请求正在启动中，请稍等它完成"}), 409
    start_easy_apply(job_id)
    threading.Thread(target=_easy_apply_background, args=(job_id,), daemon=True).start()
    return jsonify({"started": True})


def _refetch_jd_background(job_id):
    try:
        result = refetch_jd(job_id)
        logging.info("background refetch JD done for job %s: %s", job_id, result)
    except Exception:
        logging.exception("background refetch JD failed for job %s", job_id)


@app.route("/api/jobs/<int:job_id>/refetch_jd", methods=["POST"])
def refetch_jd_route(job_id):
    # 重新搜索+抓LinkedIn详情页可能要一两分钟甚至更久，同步跑在请求里等这么久，容易被
    # 浏览器/网络中间层判定连接失活而中断（实测报"Failed to fetch"）。改成跟批量重新获取
    # 一样放后台线程跑，接口立刻返回，完成后刷新职位列表能看到结果。
    threading.Thread(target=_refetch_jd_background, args=(job_id,), daemon=True).start()
    return jsonify({"started": True})


def _refetch_missing_jd_background():
    try:
        result = refetch_missing_jd_jobs()
        logging.info("background refetch JD done: %s", result)
    except Exception:
        logging.exception("background refetch JD failed")


@app.route("/api/jobs/refetch_jd", methods=["POST"])
def refetch_jd_all_route():
    # 逐条重新搜索，条数多的话会比较慢（跟"立即搜索一次"同理），放后台线程跑，不卡住
    # 这次请求的返回；完成后刷新职位列表能看到抓到JD的职位状态更新。
    threading.Thread(target=_refetch_missing_jd_background, daemon=True).start()
    return jsonify({"started": True})


def _interview_prep_background(job_id, round_label=None):
    try:
        result = generate_interview_prep_safe(job_id, round_label=round_label)
        logging.info("interview prep generated for job %s: %s", job_id, result)
    except Exception:
        # 失败原因已经由 generate_interview_prep_safe() 写进 interview_preps 表了，
        # 前端读那一行就能看到，这里只记日志。
        logging.exception("interview prep generation failed for job %s", job_id)


def _maybe_start_interview_prep(job_id, round_label=None, force=False):
    """需要的话起一个后台线程生成面试准备。返回是否真的启动了。

    force=False（自动触发）：已经有成功生成过的材料就不重复生成——用户可能反复切换
    投递状态，每切一次就重跑一遍LLM太浪费。想换个角度再来一份走手动"重新生成"。
    上次生成失败的不算"已有材料"（get_latest_interview_prep 的 success_only），
    这种情况应该自动再试一次。"""
    if interview_prep_in_progress(job_id):
        return False
    if not force and get_latest_interview_prep(job_id, success_only=True):
        return False
    threading.Thread(
        target=_interview_prep_background, args=(job_id, round_label), daemon=True
    ).start()
    return True


@app.route("/api/jobs/<int:job_id>/interview_prep", methods=["POST"])
def generate_interview_prep_route(job_id):
    # 一次要出十几道题+答法+话术，比匹配分析还慢，必须后台跑、立刻返回，前端轮询
    # /api/jobs 的 interview_prep_state 看进度（同 refetch_jd 那次"Failed to fetch"的教训）。
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    if not (job.get("jd_text") or "").strip():
        return jsonify({"error": "这条职位没有JD正文，无法生成面试准备（可先点「重新获取」抓取JD）"}), 400
    if interview_prep_in_progress(job_id):
        return jsonify({"error": "这条职位的面试准备正在生成中，请稍等"}), 409
    try:
        # 同 analyze 路由：后台线程里才发现没简历的话，失败只会落进 interview_preps 的
        # error 行，用户看到的是"生成失败"而不是"你还没传简历"。
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        return need_resume_response(e)

    data = request.get_json(silent=True) or {}
    round_label = (data.get("round_label") or "").strip() or None
    _maybe_start_interview_prep(job_id, round_label=round_label, force=True)
    return jsonify({"started": True})


@app.route("/api/jobs/<int:job_id>/interview_prep", methods=["GET"])
def get_interview_prep_route(job_id):
    """默认返回最新一份（含失败记录，前端据此显示失败原因）；?all=1 返回全部历史版本。"""
    if request.args.get("all"):
        return jsonify(list_interview_preps(job_id))
    prep = get_latest_interview_prep(job_id)
    return jsonify(prep or {})


@app.route("/api/interview_preps/<int:prep_id>", methods=["DELETE"])
def delete_interview_prep_route(prep_id):
    deleted = delete_interview_prep(prep_id)
    if not deleted:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 通用面试题库


@app.route("/api/interview/bank", methods=["GET"])
def get_bank_route():
    # error 是上一次起草的失败原因：起草在后台线程里跑，失败了只有这一条路能告诉前端，
    # 否则前端只看到 generating 变 false，会把失败渲染成"起草完成"。
    return jsonify(
        {"items": list_bank_items(), "generating": bank_generating(), "error": bank_error()}
    )


def _bank_generation_background():
    error = None
    try:
        stats = generate_bank_draft()
        logging.info("interview bank draft done: %s", stats)
        # 三段里只挂了一两段：另外几段的内容已经入库了，不能当成整体成功一声不吭，
        # 也不该当成整体失败——把挂掉的那几段单独说清楚，用户再点一次就只补这几段。
        if stats.get("failed_sections"):
            error = "部分内容起草失败：" + "；".join(stats["failed_sections"]) + (
                "（其余部分已经生成好了，可以再点一次「AI 起草 / 补充」只补这几段）"
            )
    except Exception as e:
        logging.exception("interview bank draft failed")
        # str(e) 对空消息的异常会是空串，那样前端拿到 error 却没话可说，退到类名。
        error = str(e) or e.__class__.__name__
    finally:
        finish_bank_generation(error)


@app.route("/api/interview/bank/generate", methods=["POST"])
def generate_bank_route():
    # 跟面试准备同理：一次要出自我介绍+十来道通用题+几个完整故事，放后台跑、立刻返回，
    # 前端轮询 GET /api/interview/bank 的 generating 字段看进度。
    # start_bank_generation() 里检查+置位是原子的，连点两下第二下会拿到 409。
    try:
        # 简历检查放在置位之前：先置位再发现没简历的话，得记着把标志位复位，
        # 漏一次就永远卡在"正在生成中"，只能重启进程。
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        return need_resume_response(e)
    if not start_bank_generation():
        return jsonify({"error": "题库正在生成中，请稍等它完成"}), 409
    threading.Thread(target=_bank_generation_background, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/interview/bank", methods=["POST"])
def add_bank_item_route():
    data = request.get_json(force=True)
    category = data.get("category")
    question = (data.get("question") or "").strip()
    if category not in BANK_CATEGORIES:
        return jsonify({"error": "invalid category"}), 400
    if not question:
        return jsonify({"error": "问题不能为空"}), 400
    item_id = add_bank_item(
        category, question, answer=data.get("answer"), answer_en=data.get("answer_en")
    )
    return jsonify({"id": item_id})


@app.route("/api/interview/bank/<int:item_id>", methods=["PUT"])
def update_bank_item_route(item_id):
    data = request.get_json(force=True)
    updated = update_bank_item(
        item_id,
        question=data.get("question"),
        answer=data.get("answer"),
        answer_en=data.get("answer_en"),
    )
    if not updated:
        return jsonify({"error": "条目不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/interview/bank/<int:item_id>", methods=["DELETE"])
def delete_bank_item_route(item_id):
    if not delete_bank_item(item_id):
        return jsonify({"error": "条目不存在"}), 404
    return jsonify({"ok": True})


# ------------------------------------------------- 题库：跟 AI 对话完善答案
#
# 这两个接口**同步返回**，不像起草那样后台线程 + 轮询：单轮只改一道题的一个语言版本，
# 输出量比起草小一个数量级，等待在十几秒到一分钟量级，app.run(threaded=True) 本来就能
# 并发处理。再套一层 job_state 标志和轮询是过度设计。
#
# 对话历史由前端每轮带回来（不落库，见 spec/tech-solution.md），所以要当成不可信输入：
# interview.sanitize_chat_history() 会滤掉脏数据并只保留最后若干条，防止历史无限长把
# token 烧光。


@app.route("/api/interview/bank/<int:item_id>/chat", methods=["POST"])
def bank_item_chat_route(item_id):
    item = get_bank_item(item_id)
    if not item:
        return jsonify({"error": "条目不存在"}), 404
    data = request.get_json(force=True)
    lang = data.get("lang") or "zh"
    message = (data.get("message") or "").strip()
    if lang not in ("zh", "en"):
        return jsonify({"error": "lang 只能是 zh 或 en"}), 400
    if not message:
        return jsonify({"error": "说点什么吧"}), 400
    try:
        return jsonify(chat_bank_answer(item, lang, message, history=data.get("history")))
    except Exception as e:
        logging.exception("bank item chat failed")
        return jsonify({"error": str(e) or e.__class__.__name__}), 500


@app.route("/api/interview/bank/chat", methods=["POST"])
def bank_assistant_chat_route():
    """全局题库助手：只做跨题诊断，响应里刻意没有 answer 字段——它不改写具体答案，
    改写走上面那个按条目的接口（那边才知道要回填哪一条）。"""
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "说点什么吧"}), 400
    try:
        return jsonify(chat_bank_assistant(message, history=data.get("history")))
    except Exception as e:
        logging.exception("bank assistant chat failed")
        return jsonify({"error": str(e) or e.__class__.__name__}), 500


# ---------------------------------------------------------------- 我的简历


@app.route("/resume")
def resume_page():
    return render_template("resume.html")


@app.route("/api/resume", methods=["GET"])
def get_resume_route():
    return jsonify(resume_store.get_meta())


@app.route("/api/resume/upload", methods=["POST"])
def upload_resume_route():
    file_storage = request.files.get("file")
    if not file_storage:
        return jsonify({"error": "没有收到文件。"}), 400
    try:
        return jsonify(resume_store.save_uploaded(file_storage))
    except ResumeUploadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.exception("resume upload failed")
        return jsonify({"error": str(e) or e.__class__.__name__}), 500


@app.route("/api/resume", methods=["DELETE"])
def delete_resume_route():
    return jsonify(resume_store.delete_base_resume())


@app.route("/api/resume/download", methods=["GET"])
def download_base_resume_route():
    path = resume_store.get_base_resume_path()
    if not path:
        abort(404, description="还没有上传简历")
    meta = load_config().get("base_resume_meta") or {}
    return send_file(
        path, as_attachment=True, download_name=meta.get("original_filename") or os.path.basename(path)
    )


@app.route("/api/resume/review", methods=["GET"])
def get_resume_review_route():
    """最近一次体检结果。stale=True 表示这份结果是对着另一个版本的简历跑的——
    它里面的段落索引已经对不上现在这份文件了，照着改会改错段落。

    体检在后台线程里跑（见下面 POST 路由），generating 让前端知道"还在跑"，不然刷新
    页面/从别的页面跳回来只看得到上一次的结果，会误以为这次点的体检被打断了什么都
    没发生。background_error 只覆盖"体检还没跑到能落库那一步就整个挂了"的边缘情况
    （比如简历文件读取失败）——正常的 LLM 调用失败已经落进 resume_reviews 表的 error
    列，直接读 review.error 就够了，不用等这个字段。"""
    review = get_latest_resume_review() or {}
    if review:
        review["stale"] = bool(
            review.get("resume_fingerprint")
            and review["resume_fingerprint"] != resume_store.fingerprint()
        )
    review["generating"] = resume_review_generating()
    review["background_error"] = resume_review_error()
    return jsonify(review)


def _resume_review_background():
    error = None
    try:
        run_resume_review()
    except Exception as e:
        logging.exception("resume review failed")
        error = str(e) or e.__class__.__name__
    finally:
        finish_resume_review(error)


@app.route("/api/resume/review", methods=["POST"])
def review_resume_route():
    # 后台线程里跑，立刻返回——跟题库起草（generate_bank_route）同一个模式。之前是同步
    # 阻塞到 LLM 调用完成才返回，用户跳去别的页面会让浏览器直接取消这个还没返回的请求，
    # 体检等于被打断；现在请求只负责"启动"，真正的生成不挂在这次 HTTP 请求的生死上。
    try:
        resume_store.require_base_resume()
    except ResumeMissingError as e:
        return need_resume_response(e)
    if not start_resume_review():
        return jsonify({"error": "体检正在进行中，请稍等它完成"}), 409
    threading.Thread(target=_resume_review_background, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/resume/optimize", methods=["POST"])
def optimize_resume_route():
    data = request.get_json(force=True)
    try:
        build_optimized_resume(data.get("edits"))
        return jsonify({"ok": True, "download_name": resume_store.optimized_download_name()})
    except ResumeMissingError as e:
        return need_resume_response(e)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.exception("build optimized resume failed")
        return jsonify({"error": str(e) or e.__class__.__name__}), 500


@app.route("/api/resume/optimized", methods=["GET"])
def download_optimized_resume_route():
    path = resume_store.optimized_path()
    if not os.path.isfile(path):
        abort(404, description="还没有生成优化版简历")
    return send_file(path, as_attachment=True, download_name=resume_store.optimized_download_name())


@app.route("/api/resume/tailored", methods=["GET"])
def list_tailored_resumes_route():
    """AI 分析给各职位生成过的定制简历。file_exists 单独算一遍：这些文件存在用户自己的
    磁盘上，可能已经被移走或删掉了，前端据此把下载按钮置灰而不是点了才 404。"""
    rows = list_jobs_with_tailored_resume()
    for row in rows:
        row["file_exists"] = bool(row.get("resume_path") and os.path.isfile(row["resume_path"]))
    return jsonify(rows)


@app.route("/api/runs", methods=["GET"])
def get_runs():
    return jsonify(list_runs())


# ---------------------------------------------------------------- 每日任务清单
MAX_CHECKLIST_ITEM_LENGTH = 200


@app.route("/api/checklist", methods=["GET"])
def get_checklist():
    """待审核/待投递两项前端已经有 allJobs 全量数据，直接在 static/app.js 里现算，
    不占这个接口的字段——这里只负责后端才算得出来的部分：超过7天没跟进的投递、
    用户自建的待办条目、简历有没有体检过、体检给出的建议是不是还没去优化。"""
    followups = [
        {"job_id": j["id"], "title": j["title"], "company": j["company"], "applied_at": j["applied_at"]}
        for j in list_stale_applications(days=7)
    ]
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
        }
    )


@app.route("/api/checklist", methods=["POST"])
def add_checklist_item_route():
    data = request.get_json(force=True)
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "内容不能为空"}), 400
    if len(content) > MAX_CHECKLIST_ITEM_LENGTH:
        return jsonify({"error": f"内容太长（超过{MAX_CHECKLIST_ITEM_LENGTH}字符）"}), 400
    item_id = add_checklist_item(content)
    return jsonify({"id": item_id})


@app.route("/api/checklist/<int:item_id>", methods=["DELETE"])
def delete_checklist_item_route(item_id):
    deleted = delete_checklist_item(item_id)
    if not deleted:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/tracker", methods=["GET"])
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


if __name__ == "__main__":
    start_scheduler()
    # 启动时顺带把历史积压里最新的 STARTUP_BACKLOG_LIMIT 条自动分析一遍（后台跑，不卡启动）。
    threading.Thread(
        target=_analyze_pending_jobs_background, kwargs={"limit": STARTUP_BACKLOG_LIMIT}, daemon=True
    ).start()
    # 公司国籍分类很便宜，不像完整分析那样需要限量，启动时直接把所有历史积压里还没
    # 判断过的一次性处理完，让"外企/国内公司"筛选马上就有历史数据可看。
    threading.Thread(target=_classify_company_origins_background, daemon=True).start()
    threading.Thread(target=_backfill_materials_from_tracker, daemon=True).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
