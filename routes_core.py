"""首页 + 全局配置（/api/config、/api/models）。"""
import logging
import uuid

from flask import Blueprint, jsonify, render_template, request

from config import load_config, save_config
from job_state import discard_how_you_fit_state
from linkedin_company import resolve_company_ids
from llm import LLM_TASKS, MODELS, get_model
from llm import resolve as llm_resolve
from scheduler import reschedule

core_bp = Blueprint("core", __name__)


@core_bp.route("/")
def index():
    return render_template("index.html")


@core_bp.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@core_bp.route("/api/models", methods=["GET"])
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


@core_bp.route("/api/config", methods=["POST"])
def update_config():
    cfg = load_config()
    data = request.get_json(force=True)

    if "country_indeed" in data:
        # 不能允许空串：scrape_jobs() 的 country_indeed 参数一旦是空字符串，jobspy 对每个
        # 关键词×城市组合的请求都会直接抛"Invalid country string"，整次抓取found=0——
        # 之前设置页没做非空校验，被悄悄清空过一次导致抓取静默失效了一整天
        # （2026-08-24 实测踩过，见 spec/roadmap.md）。
        raw = str(data["country_indeed"] or "").strip()
        if not raw:
            return jsonify({"error": "国家不能留空（决定去 Indeed 抓哪个国家的职位，清空会导致抓取整体失败）"}), 400
        cfg["country_indeed"] = raw

    for key in (
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
    for key in ("results_wanted", "days_old", "schedule_hour", "schedule_minute", "email_scan_interval_days", "stale_application_reminder_days"):
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
    if "linkedin_how_you_fit_searches" in data:
        from linkedin_how_you_fit import MAX_HOW_YOU_FIT_SEARCHES, HowYouFitSyncError, _validate_search_url

        items = data["linkedin_how_you_fit_searches"] or []
        if len(items) > MAX_HOW_YOU_FIT_SEARCHES:
            return jsonify({"error": f"最多配置 {MAX_HOW_YOU_FIT_SEARCHES} 条 LinkedIn 智能匹配推荐搜索"}), 400
        existing = {s["id"]: s for s in (cfg.get("linkedin_how_you_fit_searches") or []) if s.get("id")}
        new_list = []
        for item in items:
            name = (item.get("name") or "").strip()
            url = (item.get("url") or "").strip()
            if not name or not url:
                continue
            try:
                _validate_search_url(url)
            except HowYouFitSyncError as e:
                return jsonify({"error": f"「{name}」：{e}"}), 400
            item_id = item.get("id") or ""
            enabled = bool(item.get("enabled", True))
            if item_id in existing:
                new_list.append({"id": item_id, "name": name, "url": url, "enabled": enabled})
            else:
                new_list.append({"id": uuid.uuid4().hex[:12], "name": name, "url": url, "enabled": enabled})
        removed_ids = set(existing) - {s["id"] for s in new_list}
        for search_id in removed_ids:
            discard_how_you_fit_state(search_id)
        cfg["linkedin_how_you_fit_searches"] = new_list
    if "linkedin_how_you_fit_delay" in data:
        raw = data["linkedin_how_you_fit_delay"]
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            cfg["linkedin_how_you_fit_delay"] = 0
        else:
            try:
                cfg["linkedin_how_you_fit_delay"] = int(raw)
            except (TypeError, ValueError):
                return jsonify({"error": "linkedin_how_you_fit_delay 必须是数字"}), 400
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
