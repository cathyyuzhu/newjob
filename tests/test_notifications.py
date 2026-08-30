"""通知功能冒烟测试（2026-08-26）。

背景：同步/AI分析这些后台线程任务完成时，之前唯一的反馈通道是 showToast()——只有
用户正好停留在发起操作的那个页面才能看到，跳走/关掉浏览器再回来就完全错过（代码里
static/app.js 的注释已经承认这个缺口）。新增 notifications 表 + 顶栏铃铛，后台任务
完成时落一条持久化记录，跟 /api/checklist 聚合的"当前有哪些条件成立"不同——通知是
"发生过一件事"，看过之前一直在。

覆盖：
1. models 层基本 CRUD（add/list/unread_count/mark_all_read）
2. GET /api/notifications、POST /api/notifications/read_all 两个接口
3. 两个后台完成函数（_refetch_missing_jd_background、_classify_company_origins_background）
   成功/失败分支各自落对了通知——直接同步调用这两个函数（本身是普通 Python 函数，
   不必真的起线程），monkeypatch 掉它们各自依赖的 pipeline 函数，不碰真实网络/LLM。

临时库 + 临时 config，不碰真实 jobs.db。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")
config.save_config({**config.DEFAULT_CONFIG, "tracker_xlsx_path": os.path.join(tmpdir, "tracker.xlsx")})

import models

models.DB_PATH = config.DB_PATH

import resume_store

resume_store.RESUME_DIR = os.path.join(tmpdir, "resumes")

import app as flask_app
import routes_job_actions
import routes_search

models.init_db()
flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()


# ---- 1. models 层基本行为 ----
assert models.unread_notification_count() == 0
n1 = models.add_notification("sync_tracker", "同步完成", "共 3 条", level="success")
n2 = models.add_notification("analysis", "分析失败", level="error")
assert models.unread_notification_count() == 2

items = models.list_notifications()
assert [i["id"] for i in items] == [n2, n1], "最新的排最前面"
assert items[0]["title"] == "分析失败" and items[0]["level"] == "error"
assert items[1]["message"] == "共 3 条"
assert items[1]["link"] is None

models.mark_all_notifications_read()
assert models.unread_notification_count() == 0
assert all(i["read_at"] for i in models.list_notifications())

n3 = models.add_notification("materials", "材料生成完成", link="/jobs/1")
assert models.unread_notification_count() == 1, "已读清零之后新来的一条应该重新计入未读"
print("models CRUD ok")


# ---- 2. GET /api/notifications、POST /api/notifications/read_all ----
data = c.get("/api/notifications").get_json()
assert data["unread"] == 1
assert [i["id"] for i in data["items"]][0] == n3

r = c.post("/api/notifications/read_all")
assert r.status_code == 200
assert c.get("/api/notifications").get_json()["unread"] == 0
print("GET /api/notifications, POST /api/notifications/read_all ok")


# ---- 3. 后台完成函数落对通知：批量重新获取JD ----
# static/app.js:724 的注释明确承认这个任务"跑完没有任何通知"，这是要堵上的缺口之一。
# _refetch_missing_jd_background 现在住在 routes_job_actions.py 里，patch 要打在它
# 实际引用 refetch_missing_jd_jobs 的那个模块上（app.py 已经不再直接定义任何路由）。
before = models.unread_notification_count()
routes_job_actions.refetch_missing_jd_jobs = lambda: {"attempted": 5, "refetched": 3}
routes_job_actions._refetch_missing_jd_background()
assert models.unread_notification_count() == before + 1
latest = models.list_notifications()[0]
assert latest["category"] == "refetch_jd"
assert latest["level"] == "success"
assert "5" in latest["message"] and "3" in latest["message"]

def _raise(*args, **kwargs):
    raise RuntimeError("boom")

routes_job_actions.refetch_missing_jd_jobs = _raise
routes_job_actions._refetch_missing_jd_background()
latest = models.list_notifications()[0]
assert latest["category"] == "refetch_jd" and latest["level"] == "error"
print("_refetch_missing_jd_background notifications ok")


# ---- 4. 后台完成函数落对通知：公司国籍识别 ----
# _classify_company_origins_background 现在住在 routes_search.py 里，同样的道理。
routes_search.classify_company_origins = lambda job_ids=None: {"classified": 0, "companies": 0}
before = models.unread_notification_count()
routes_search._classify_company_origins_background()
assert models.unread_notification_count() == before, "什么都没分类到，不该发一条空话通知"

routes_search.classify_company_origins = lambda job_ids=None: {"classified": 4, "companies": 2}
routes_search._classify_company_origins_background()
assert models.unread_notification_count() == before + 1
latest = models.list_notifications()[0]
assert latest["category"] == "company_origin" and latest["level"] == "success"
assert "4" in latest["message"] and "2" in latest["message"]

routes_search.classify_company_origins = _raise
routes_search._classify_company_origins_background()
latest = models.list_notifications()[0]
assert latest["category"] == "company_origin" and latest["level"] == "error"
print("_classify_company_origins_background notifications ok")

print("\nALL PASS")
