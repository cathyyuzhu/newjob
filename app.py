import logging
import os
import threading

from flask import Flask, abort, jsonify, render_template, request, send_file

from config import load_config, save_config
from easy_apply import EasyApplyError, EasyApplyInProgress, run_easy_apply
from job_state import (
    bank_error,
    bank_generating,
    easy_apply_opening,
    finish_bank_generation,
    finish_easy_apply,
    get_easy_apply_error,
    get_easy_apply_states,
    get_interview_prep_states,
    get_states,
    interview_prep_in_progress,
    request_stop,
    start_bank_generation,
    start_easy_apply,
)
from models import (
    BANK_CATEGORIES,
    add_bank_item,
    delete_bank_item,
    delete_interview_prep,
    get_bank_item,
    get_job,
    get_latest_interview_prep,
    init_db,
    job_ids_with_interview_prep,
    list_bank_items,
    list_interview_preps,
    list_jobs,
    list_runs,
    set_application_status,
    set_job_starred,
    set_job_status,
    update_bank_item,
)
from pipeline import (
    analyze_and_record_safe,
    analyze_pending_jobs,
    chat_bank_answer,
    chat_bank_assistant,
    classify_company_origins,
    find_tracker_entry,
    generate_bank_draft,
    generate_interview_prep_safe,
    queue_pending_jobs,
    refetch_jd,
    refetch_missing_jd_jobs,
)
from scheduler import start_scheduler, reschedule
from scraper import run_search_once
from tracker_utils import list_entries

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
init_db()


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


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    cfg = load_config()
    data = request.get_json(force=True)

    for key in (
        "country_indeed",
        "tracker_xlsx_path",
        "base_resume_path",
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
    if "easy_apply_profile" in data:
        # 前端一次性提交整份 profile（三个固定字段 + extra_answers 列表），直接整体替换，
        # 不做逐字段合并——设置页每次保存都是带着当前完整表单内容提交的，不存在"只改一个
        # 字段、其它字段要保留旧值"的场景。
        cfg["easy_apply_profile"] = data["easy_apply_profile"]

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


@app.route("/api/search/run", methods=["POST"])
def trigger_search():
    result = run_search_once()
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


@app.route("/api/jobs/analyze_all", methods=["POST"])
def analyze_all_route():
    # 顶部"AI分析"按钮：跟 /api/search/run 一样，先同步筛选+标记排队（见 queue_pending_jobs
    # 的注释），响应返回时前端就能立刻看到"排队中"状态；真正调用LLM的分析循环放后台线程跑。
    # 不传 job_ids/limit：处理"待审核"里所有还没分析成功过的职位，包括历史积压
    # （对应 roadmap 里"历史积压批量清理入口"这条）。
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
    # 一次查询取回"哪些职位已经有面试准备"的集合，而不是逐条职位查一次库（N+1）。
    prep_job_ids = job_ids_with_interview_prep()
    for job in jobs:
        job["analysis_state"] = states.get(job["id"])
        job["easy_apply_state"] = easy_apply_states.get(job["id"])
        if job["easy_apply_state"] == "error":
            job["easy_apply_error"] = get_easy_apply_error(job["id"])
        job["interview_prep_state"] = prep_states.get(job["id"])
        job["has_interview_prep"] = job["id"] in prep_job_ids
    return jsonify(jobs)


@app.route("/api/jobs/<int:job_id>", methods=["GET"])
def get_job_route(job_id):
    """单条职位。面试准备页只关心一条职位，没必要跟主页一样把整个列表拉回来再 find——
    尤其是生成期间每隔几秒就要查一次状态。字段跟列表接口保持一致。"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "职位不存在"}), 404
    job["interview_prep_state"] = get_interview_prep_states().get(job_id)
    job["has_interview_prep"] = job_id in job_ids_with_interview_prep()
    return jsonify(job)


@app.route("/api/jobs/<int:job_id>/status", methods=["POST"])
def update_job_status(job_id):
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("new", "reviewed", "dismissed"):
        return jsonify({"error": "invalid status"}), 400
    set_job_status(job_id, status)
    return jsonify({"ok": True})


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


def _find_cover_letter(job):
    """从追踪表里按 公司+职位名 找回这条职位对应的 cover letter 全文（如果生成过），
    用于 Easy Apply 尽力而为填写 cover letter 字段；找不到就返回 None，不影响其它步骤。"""
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


@app.route("/api/runs", methods=["GET"])
def get_runs():
    return jsonify(list_runs())


@app.route("/api/tracker", methods=["GET"])
def get_tracker_entries():
    cfg = load_config()
    tracker_path = cfg.get("tracker_xlsx_path") or os.path.expanduser("~/Downloads/JD匹配追踪表.xlsx")
    return jsonify(list_entries(tracker_path))


if __name__ == "__main__":
    start_scheduler()
    # 启动时顺带把历史积压里最新的 STARTUP_BACKLOG_LIMIT 条自动分析一遍（后台跑，不卡启动）。
    threading.Thread(
        target=_analyze_pending_jobs_background, kwargs={"limit": STARTUP_BACKLOG_LIMIT}, daemon=True
    ).start()
    # 公司国籍分类很便宜，不像完整分析那样需要限量，启动时直接把所有历史积压里还没
    # 判断过的一次性处理完，让"外企/国内公司"筛选马上就有历史数据可看。
    threading.Thread(target=_classify_company_origins_background, daemon=True).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
