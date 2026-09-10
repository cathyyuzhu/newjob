"""面试相关三个子模块：单职位面试准备、通用面试题库、语音练习。三者共用"页面 → 生成
→ 状态轮询"的模式，且都只依赖 pipeline.py 的面试相关函数，放在同一个文件里。
"""
import logging
import threading

from flask import Blueprint, abort, jsonify, render_template, request

import llm
import resume_store
from job_state import (
    bank_error,
    bank_generating,
    finish_bank_generation,
    interview_prep_in_progress,
    practice_generation_in_progress,
    start_bank_generation,
)
from models import (
    BANK_CATEGORIES,
    add_bank_item,
    add_notification,
    delete_bank_item,
    delete_interview_doc,
    delete_interview_prep,
    get_bank_item,
    get_interview_doc,
    get_job,
    get_latest_interview_prep,
    get_practice_set,
    list_bank_items,
    list_interview_docs,
    list_interview_preps,
    list_latest_practice_answers,
    list_practice_sets,
    update_bank_item,
)
from pipeline import (
    chat_bank_answer,
    chat_bank_assistant,
    generate_bank_draft,
    generate_interview_prep_safe,
    generate_practice_set_for_doc_safe,
    save_uploaded_interview_doc,
    score_practice_answer,
)
from resume_store import ResumeMissingError
from web_helpers import need_resume_response, usage_notification_message

interview_bp = Blueprint("interview", __name__)


# 面试相关的两块内容各自是独立页面，不再是主页上的弹窗：都属于"坐下来看很久 / 一边看一边改"
# 的场景，而弹窗有三个硬伤——生成是后台跑的但轮询跟弹窗生命周期绑死、内容长却被塞进
# max-height:92vh 的内滚容器、没有 URL 没法单独开一个标签页挂着。理由详见
# spec/tech-solution.md。「匹配分析」相反，是在列表里扫一眼就关，继续留在弹窗里。
@interview_bp.route("/interview")
def interview_bank_page():
    return render_template("interview.html")


# 面试语音练习：独立于具体职位的练习模块（2026-08-23）——上传的准备文档往往不对应库里
# 任何一条职位（比如用户自己整理的复合准备材料），不挂在 /jobs/<id> 下面。同样是坐下来
# 长时间交互的场景（录音、逐题练习），独立页面而不是弹窗，理由跟上面两处一致。
@interview_bp.route("/interview/practice")
def interview_practice_page():
    return render_template("interview_practice.html")


@interview_bp.route("/jobs/<int:job_id>/interview")
def job_interview_page(job_id):
    if not get_job(job_id):
        abort(404)
    return render_template("job_interview.html", job_id=job_id)


# ---------------------------------------------------------------- 单职位面试准备


def _interview_prep_background(job_id, round_label=None):
    job = get_job(job_id)
    job_label = f"{job['company']} · {job['title']}" if job else f"职位 #{job_id}"
    llm.start_usage_tracking()
    try:
        result = generate_interview_prep_safe(job_id, round_label=round_label)
        logging.info("interview prep generated for job %s: %s", job_id, result)
        add_notification(
            "interview_prep", "面试准备生成完成", usage_notification_message(job_label),
            level="success", link=f"/jobs/{job_id}",
        )
    except Exception:
        # 失败原因已经由 generate_interview_prep_safe() 写进 interview_preps 表了，
        # 前端读那一行就能看到，这里只记日志。
        logging.exception("interview prep generation failed for job %s", job_id)
        add_notification(
            "interview_prep", "面试准备生成失败", usage_notification_message(job_label),
            level="error", link=f"/jobs/{job_id}",
        )


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


@interview_bp.route("/api/jobs/<int:job_id>/interview_prep", methods=["POST"])
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


@interview_bp.route("/api/jobs/<int:job_id>/interview_prep", methods=["GET"])
def get_interview_prep_route(job_id):
    """默认返回最新一份（含失败记录，前端据此显示失败原因）；?all=1 返回全部历史版本。"""
    if request.args.get("all"):
        return jsonify(list_interview_preps(job_id))
    prep = get_latest_interview_prep(job_id)
    return jsonify(prep or {})


@interview_bp.route("/api/interview_preps/<int:prep_id>", methods=["DELETE"])
def delete_interview_prep_route(prep_id):
    deleted = delete_interview_prep(prep_id)
    if not deleted:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 通用面试题库


@interview_bp.route("/api/interview/bank", methods=["GET"])
def get_bank_route():
    # error 是上一次起草的失败原因：起草在后台线程里跑，失败了只有这一条路能告诉前端，
    # 否则前端只看到 generating 变 false，会把失败渲染成"起草完成"。
    return jsonify(
        {"items": list_bank_items(), "generating": bank_generating(), "error": bank_error()}
    )


def _bank_generation_background():
    error = None
    llm.start_usage_tracking()
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
        add_notification(
            "bank", "题库 AI 起草失败" if error else "题库 AI 起草完成",
            usage_notification_message(error), level="error" if error else "success",
        )


@interview_bp.route("/api/interview/bank/generate", methods=["POST"])
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


@interview_bp.route("/api/interview/bank", methods=["POST"])
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


@interview_bp.route("/api/interview/bank/<int:item_id>", methods=["PUT"])
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


@interview_bp.route("/api/interview/bank/<int:item_id>", methods=["DELETE"])
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


@interview_bp.route("/api/interview/bank/<int:item_id>/chat", methods=["POST"])
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
    llm.start_usage_tracking()
    try:
        result = chat_bank_answer(item, lang, message, history=data.get("history"))
        result["llm_usage_text"] = llm.usage_text(llm.pop_usage_summary())
        return jsonify(result)
    except Exception as e:
        logging.exception("bank item chat failed")
        return jsonify({
            "error": str(e) or e.__class__.__name__,
            "llm_usage_text": llm.usage_text(llm.pop_usage_summary()),
        }), 500


@interview_bp.route("/api/interview/bank/chat", methods=["POST"])
def bank_assistant_chat_route():
    """全局题库助手：只做跨题诊断，响应里刻意没有 answer 字段——它不改写具体答案，
    改写走上面那个按条目的接口（那边才知道要回填哪一条）。"""
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "说点什么吧"}), 400
    llm.start_usage_tracking()
    try:
        result = chat_bank_assistant(message, history=data.get("history"))
        result["llm_usage_text"] = llm.usage_text(llm.pop_usage_summary())
        return jsonify(result)
    except Exception as e:
        logging.exception("bank assistant chat failed")
        return jsonify({
            "error": str(e) or e.__class__.__name__,
            "llm_usage_text": llm.usage_text(llm.pop_usage_summary()),
        }), 500


# ---------------------------------------------------------------- 面试语音练习
#
# 上传准备文档 → 生成一套练习题 → 逐题录音作答 → 打分反馈。独立于具体职位（见
# interview_practice_page 路由上的注释），数据模型/状态管理详见 models.py 里
# interview_docs/interview_practice_sets/interview_practice_answers 表上方的注释。


@interview_bp.route("/api/interview/practice/docs", methods=["GET"])
def list_interview_docs_route():
    """文档列表，每条附带当前是否正在生成题目——用于文档库页面渲染"生成中"态并安排轮询，
    跟 /api/jobs 附带 interview_prep_state 是同一个用意。"""
    docs = list_interview_docs()
    for d in docs:
        d["generating"] = practice_generation_in_progress(d["id"])
    return jsonify(docs)


@interview_bp.route("/api/interview/practice/docs/upload", methods=["POST"])
def upload_interview_doc_route():
    file_storage = request.files.get("file")
    if not file_storage:
        return jsonify({"error": "没有收到文件。"}), 400
    try:
        doc_id = save_uploaded_interview_doc(file_storage)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.exception("interview doc upload failed")
        return jsonify({"error": str(e) or e.__class__.__name__}), 500
    return jsonify({"id": doc_id})


@interview_bp.route("/api/interview/practice/docs/<int:doc_id>", methods=["DELETE"])
def delete_interview_doc_route(doc_id):
    if not delete_interview_doc(doc_id):
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


def _practice_generation_background(doc_id, round_label_hint=None):
    llm.start_usage_tracking()
    try:
        result = generate_practice_set_for_doc_safe(doc_id, round_label_hint=round_label_hint)
        logging.info("practice set generated for doc %s: %s", doc_id, result)
        add_notification(
            "practice", "面试语音练习出题完成", usage_notification_message(None),
            level="success", link="/interview/practice",
        )
    except Exception:
        # 失败原因已经由 generate_practice_set_for_doc_safe() 写进
        # interview_practice_sets 表了，前端读那一行就能看到，这里只记日志。
        logging.exception("practice set generation failed for doc %s", doc_id)
        add_notification(
            "practice", "面试语音练习出题失败", usage_notification_message(None),
            level="error", link="/interview/practice",
        )


@interview_bp.route("/api/interview/practice/docs/<int:doc_id>/generate", methods=["POST"])
def generate_practice_set_route(doc_id):
    # 一次要吃下整份文档、出 8-15 道结构化题目，比匹配分析慢得多，必须后台跑、立刻返回，
    # 前端轮询 GET /api/interview/practice/docs 的 generating 字段看进度。
    if not get_interview_doc(doc_id):
        return jsonify({"error": "文档不存在"}), 404
    if practice_generation_in_progress(doc_id):
        return jsonify({"error": "这份文档的题目正在生成中，请稍等"}), 409
    data = request.get_json(silent=True) or {}
    round_label_hint = (data.get("round_label") or "").strip() or None
    threading.Thread(
        target=_practice_generation_background,
        args=(doc_id,),
        kwargs={"round_label_hint": round_label_hint},
        daemon=True,
    ).start()
    return jsonify({"started": True})


@interview_bp.route("/api/interview/practice/sets", methods=["GET"])
def list_practice_sets_route():
    """某份文档的全部练习题版本（可重新生成，历史保留，同 interview_preps 的多版本模式）。"""
    doc_id = request.args.get("doc_id", type=int)
    if not doc_id:
        return jsonify({"error": "缺少 doc_id"}), 400
    return jsonify(list_practice_sets(doc_id))


@interview_bp.route("/api/interview/practice/sets/<int:set_id>", methods=["GET"])
def get_practice_set_route(set_id):
    """一套练习题的详情，连同这套题里每一题**最新**的作答记录一起返回——练习页需要
    同时知道题目内容和当前进度，拆成两次请求没有实际好处。"""
    practice_set = get_practice_set(set_id)
    if not practice_set:
        return jsonify({"error": "记录不存在"}), 404
    practice_set["answers"] = list_latest_practice_answers(set_id)
    return jsonify(practice_set)


@interview_bp.route("/api/interview/practice/sets/<int:set_id>/questions/<question_id>/answer", methods=["POST"])
def answer_practice_question_route(set_id, question_id):
    # 同步返回，不走后台线程+轮询：单次打分是十几秒到一分钟量级的一次 LLM 调用，
    # 跟题库单题对话（bank_item_chat_route）同一档，app.run(threaded=True) 本来就
    # 能并发处理，再套一层状态机是过度设计。
    data = request.get_json(force=True, silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "还没有作答内容，请先录音或手动输入回答。"}), 400
    llm.start_usage_tracking()
    try:
        result = score_practice_answer(set_id, question_id, transcript)
        result["llm_usage_text"] = llm.usage_text(llm.pop_usage_summary())
        return jsonify(result)
    except ValueError as e:
        llm.pop_usage_summary()
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logging.exception("practice answer scoring failed")
        return jsonify({
            "error": str(e) or e.__class__.__name__,
            "llm_usage_text": llm.usage_text(llm.pop_usage_summary()),
        }), 500
