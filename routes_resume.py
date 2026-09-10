"""「我的简历」模块：基础简历上传/下载、整份简历体检、优化版简历生成、各职位定制简历列表。"""
import logging
import os
import threading

from flask import Blueprint, abort, jsonify, render_template, request, send_file

import llm
import resume_store
from config import load_config
from job_state import finish_resume_review, resume_review_error, resume_review_generating, start_resume_review
from models import add_notification, get_latest_resume_review, list_jobs_with_tailored_resume
from pipeline import build_optimized_resume, run_resume_review
from resume_store import ResumeMissingError, ResumeUploadError
from web_helpers import need_resume_response, usage_notification_message

resume_bp = Blueprint("resume", __name__)


@resume_bp.route("/resume")
def resume_page():
    return render_template("resume.html")


@resume_bp.route("/api/resume", methods=["GET"])
def get_resume_route():
    return jsonify(resume_store.get_meta())


@resume_bp.route("/api/resume/upload", methods=["POST"])
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


@resume_bp.route("/api/resume", methods=["DELETE"])
def delete_resume_route():
    return jsonify(resume_store.delete_base_resume())


@resume_bp.route("/api/resume/download", methods=["GET"])
def download_base_resume_route():
    path = resume_store.get_base_resume_path()
    if not path:
        abort(404, description="还没有上传简历")
    meta = load_config().get("base_resume_meta") or {}
    return send_file(
        path, as_attachment=True, download_name=meta.get("original_filename") or os.path.basename(path)
    )


@resume_bp.route("/api/resume/review", methods=["GET"])
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
    llm.start_usage_tracking()
    try:
        run_resume_review()
    except Exception as e:
        logging.exception("resume review failed")
        error = str(e) or e.__class__.__name__
    finally:
        finish_resume_review(error)
        # 正常的LLM调用失败落在 resume_reviews 表自己的 error 列里（不是这里的 error
        # 变量，那个只覆盖体检还没跑到能落库那一步就整个挂了的边缘情况），两处都要看。
        latest = get_latest_resume_review()
        review_error = error or (latest or {}).get("error")
        add_notification(
            "resume_review", "简历体检失败" if review_error else "简历体检完成",
            usage_notification_message(review_error), level="error" if review_error else "success", link="/resume",
        )


@resume_bp.route("/api/resume/review", methods=["POST"])
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


@resume_bp.route("/api/resume/optimize", methods=["POST"])
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


@resume_bp.route("/api/resume/optimized", methods=["GET"])
def download_optimized_resume_route():
    path = resume_store.optimized_path()
    if not os.path.isfile(path):
        abort(404, description="还没有生成优化版简历")
    return send_file(path, as_attachment=True, download_name=resume_store.optimized_download_name())


@resume_bp.route("/api/resume/tailored", methods=["GET"])
def list_tailored_resumes_route():
    """AI 分析给各职位生成过的定制简历。file_exists 单独算一遍：这些文件存在用户自己的
    磁盘上，可能已经被移走或删掉了，前端据此把下载按钮置灰而不是点了才 404。"""
    rows = list_jobs_with_tailored_resume()
    for row in rows:
        row["file_exists"] = bool(row.get("resume_path") and os.path.isfile(row["resume_path"]))
    return jsonify(rows)
