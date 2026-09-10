"""分析完成之后，针对单条职位可以做的动作：生成定制材料、LinkedIn Easy Apply、重新获取JD。
三者都是"职位已经在库里、已经分析过"之后的下一步操作，且都是后台线程+轮询/通知的模式。
"""
import logging
import os
import threading

from flask import Blueprint, jsonify, request

import llm
import resume_store
from config import load_config
from easy_apply import EasyApplyError, EasyApplyInProgress, run_easy_apply
from job_state import (
    easy_apply_opening,
    finish_easy_apply,
    materials_in_progress,
    request_materials_stop,
    start_easy_apply,
)
from models import add_notification, get_job
from pipeline import (
    find_tracker_entry,
    generate_materials_batch,
    generate_materials_for_job_safe,
    refetch_jd,
    refetch_missing_jd_jobs,
)
from resume_store import ResumeMissingError
from web_helpers import need_resume_response, usage_notification_message

job_actions_bp = Blueprint("job_actions", __name__)


# ---------------------------------------------------------------- 材料生成（定制简历 + Cover Letter）


def _materials_background(job_id):
    job = get_job(job_id)
    job_label = f"{job['company']} · {job['title']}" if job else f"职位 #{job_id}"
    llm.start_usage_tracking()
    try:
        result = generate_materials_for_job_safe(job_id)
        logging.info("materials generated for job %s: %s", job_id, {k: bool(v) for k, v in result.items()})
        add_notification(
            "materials", "定制材料生成完成", usage_notification_message(job_label),
            level="success", link=f"/jobs/{job_id}",
        )
    except Exception:
        logging.exception("materials generation failed for job %s", job_id)
        add_notification(
            "materials", "定制材料生成失败", usage_notification_message(job_label),
            level="error", link=f"/jobs/{job_id}",
        )


@job_actions_bp.route("/api/jobs/<int:job_id>/generate_materials", methods=["POST"])
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


@job_actions_bp.route("/api/jobs/generate_materials", methods=["POST"])
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
            add_notification(
                "materials", "批量定制材料生成完成",
                f"成功 {result['generated']} 条 · 跳过 {result['skipped']} 条 · 失败 {result['failed']} 条",
                level="error" if result["failed"] else "success",
            )
        except Exception:
            logging.exception("batch materials generation failed")
            add_notification("materials", "批量定制材料生成失败", level="error")

    threading.Thread(target=_batch_background, daemon=True).start()
    return jsonify({"started": True, "count": len(job_ids)})


@job_actions_bp.route("/api/jobs/generate_materials_stop", methods=["POST"])
def generate_materials_stop_route():
    request_materials_stop()
    return jsonify({"stopping": True})


# ---------------------------------------------------------------- LinkedIn Easy Apply


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


@job_actions_bp.route("/api/jobs/<int:job_id>/easy_apply", methods=["POST"])
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


# ---------------------------------------------------------------- 重新获取JD


def _refetch_jd_background(job_id):
    job = get_job(job_id)
    job_label = f"{job['company']} · {job['title']}" if job else f"职位 #{job_id}"
    try:
        result = refetch_jd(job_id)
        logging.info("background refetch JD done for job %s: %s", job_id, result)
        add_notification(
            "refetch_jd",
            "重新获取JD完成" if result.get("jd_fetched") else "重新获取JD失败，仍未获取到内容",
            job_label,
            level="success" if result.get("jd_fetched") else "error",
            link=f"/jobs/{job_id}",
        )
    except Exception:
        logging.exception("background refetch JD failed for job %s", job_id)
        add_notification("refetch_jd", "重新获取JD失败", job_label, level="error", link=f"/jobs/{job_id}")


@job_actions_bp.route("/api/jobs/<int:job_id>/refetch_jd", methods=["POST"])
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
        add_notification(
            "refetch_jd", "批量重新获取JD完成",
            f"尝试 {result['attempted']} 条 · 成功获取 {result['refetched']} 条",
            level="success",
        )
    except Exception:
        logging.exception("background refetch JD failed")
        add_notification("refetch_jd", "批量重新获取JD失败", level="error")


@job_actions_bp.route("/api/jobs/refetch_jd", methods=["POST"])
def refetch_jd_all_route():
    # 逐条重新搜索，条数多的话会比较慢（跟"立即搜索一次"同理），放后台线程跑，不卡住
    # 这次请求的返回；完成后刷新职位列表能看到抓到JD的职位状态更新。
    threading.Thread(target=_refetch_missing_jd_background, daemon=True).start()
    return jsonify({"started": True})
