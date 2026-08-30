"""职位列表/详情页与单条职位上的各类操作：状态、标签、忽略原因、偏好档案、AI 分析、
AI 对话、备注。职位详情页本身（/jobs/<id>）也放在这里，跟它读的数据是同一批。
"""
import logging
import os
import threading

from flask import Blueprint, abort, jsonify, render_template, request, send_file

import resume_store
import routes_interview
from models import (
    add_dismiss_reason,
    add_job_note,
    add_notification,
    annotate_similar_groups,
    delete_job_note,
    get_job,
    get_latest_preference_profile,
    job_ids_with_dismiss_reason,
    job_ids_with_interview_prep,
    list_job_notes,
    list_jobs,
    note_counts,
    set_application_status,
    set_job_starred,
    set_job_status,
    set_job_tags,
)
from job_state import (
    clear_discard,
    discard_job,
    get_easy_apply_error,
    get_easy_apply_states,
    get_interview_prep_states,
    get_materials_states,
    get_states,
)
from pipeline import analyze_and_record_safe, chat_about_job, find_tracker_entry, maybe_refresh_preference_profile
from resume_store import ResumeMissingError
from web_helpers import need_resume_response

jobs_bp = Blueprint("jobs", __name__)


# 职位详情页：以前是主页上一个 680px 的弹窗（只放「匹配分析」），现在多了 AI 对话和备注，
# 两样都需要长时间挂着交互，弹窗的三个硬伤（轮询跟弹窗生命周期绑死、内容被塞进内滚容器、
# 没有独立URL）跟面试准备当初搬出弹窗是同一个理由，详见 spec/tech-solution.md。
@jobs_bp.route("/jobs/<int:job_id>")
def job_detail_page(job_id):
    if not get_job(job_id):
        abort(404)
    return render_template("job_detail.html", job_id=job_id)


def _profile_refresh_background(force=False):
    try:
        result = maybe_refresh_preference_profile(force=force)
        # 返回 None 表示没有真的触发生成（攒的新原因还没到阈值），不算一次完成，不发通知；
        # 失败也算"触发过"（返回值非 None，内容里 error 有值），一并落一条错误通知。
        if result is not None:
            if result.get("error"):
                add_notification("preference_profile", "偏好档案生成失败", result["error"], level="error")
            else:
                add_notification("preference_profile", "偏好档案已更新", level="success")
    except Exception:
        # maybe_refresh_preference_profile() 内部已经把"生成失败"落库成 error 行了，
        # 这里兜的是更早的异常（比如攒计数的那次查询本身就出错）。
        logging.exception("background preference profile refresh failed")
        add_notification("preference_profile", "偏好档案生成失败", level="error")


@jobs_bp.route("/api/jobs", methods=["GET"])
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


@jobs_bp.route("/api/jobs/<int:job_id>", methods=["GET"])
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


@jobs_bp.route("/api/jobs/<int:job_id>/status", methods=["POST"])
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


@jobs_bp.route("/api/jobs/<int:job_id>/dismiss_reason", methods=["POST"])
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


@jobs_bp.route("/api/preferences", methods=["GET"])
def get_preference_profile_route():
    return jsonify(get_latest_preference_profile() or {})


@jobs_bp.route("/api/preferences/regenerate", methods=["POST"])
def regenerate_preference_profile_route():
    """手动强制重新生成，跳过阈值检查——用于补录完冷启动数据后想立刻验证效果，
    不用真的再攒够 5 条新原因。"""
    threading.Thread(target=_profile_refresh_background, kwargs={"force": True}, daemon=True).start()
    return jsonify({"started": True})


@jobs_bp.route("/api/jobs/<int:job_id>/starred", methods=["POST"])
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


@jobs_bp.route("/api/jobs/<int:job_id>/tags", methods=["POST"])
def update_job_tags(job_id):
    data = request.get_json(force=True)
    try:
        tags = normalize_tags(data.get("tags"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    set_job_tags(job_id, tags)
    return jsonify({"ok": True, "tags": tags})


APPLICATION_STATUSES = ("not_applied", "applied", "interviewing", "rejected", "offer", "declined")


@jobs_bp.route("/api/jobs/<int:job_id>/application_status", methods=["POST"])
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
            # 模块级引用（而不是 from routes_interview import ...）：测试会 monkeypatch
            # routes_interview._maybe_start_interview_prep，用 from-import 会绑死一份
            # 旧的函数引用，patch 不到这里。
            resp["interview_prep_started"] = routes_interview._maybe_start_interview_prep(job_id)
        except Exception:
            logging.exception("投递状态改为面试中后，触发面试准备生成失败（不影响状态更新）")
    return jsonify(resp)


@jobs_bp.route("/api/jobs/<int:job_id>/analyze", methods=["POST"])
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


@jobs_bp.route("/api/jobs/<int:job_id>/resume", methods=["GET"])
def download_resume(job_id):
    job = get_job(job_id)
    if not job or not job.get("resume_path"):
        abort(404, description="该职位还没有生成定制简历")
    if not os.path.isfile(job["resume_path"]):
        abort(404, description="简历文件不存在，可能已被移动或删除")
    return send_file(job["resume_path"], as_attachment=False)


@jobs_bp.route("/api/jobs/<int:job_id>/analysis", methods=["GET"])
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


# ---------------------------------------------------------------- 职位AI对话 / 备注


@jobs_bp.route("/api/jobs/<int:job_id>/chat", methods=["POST"])
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


@jobs_bp.route("/api/jobs/<int:job_id>/notes", methods=["GET"])
def list_job_notes_route(job_id):
    return jsonify(list_job_notes(job_id))


@jobs_bp.route("/api/jobs/<int:job_id>/notes", methods=["POST"])
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


@jobs_bp.route("/api/notes/<int:note_id>", methods=["DELETE"])
def delete_job_note_route(note_id):
    if not delete_job_note(note_id):
        return jsonify({"error": "记录不存在"}), 404
    return jsonify({"ok": True})
