"""同步 LinkedIn jobs-tracker 列表（"已收藏"/"已投递"）的冒烟测试（见 linkedin_tracker.py）。

覆盖三块：
1. 列表页扫描的核心逻辑（滚动收集职位链接/稳定后停止/登录墙识别/无头撞墙后带界面
   重试）——用一个 fake Playwright page 模拟，不开真实浏览器、不碰网络。这部分对
   stage 不敏感（两个 stage 共用同一套扫描代码），只测一遍。
2. Flask 路由（POST 启动 + GET 轮询状态 + 并发保护 409 + 完成后自动排队分析 + 不支持
   的 stage 返回 404）——mock 掉 linkedin_tracker.sync_tracker_stage，跟
   test_add_by_url.py 一样不产生真实网络/LLM 调用。
3. "已投递"这个 stage 特有的行为：新入库的职位要自动打上 application_status='applied'，
   "已收藏"不受影响。
"""
import os
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH

import llm

llm.chat = lambda messages, provider="anthropic", model=None, system=None, max_tokens=None: "{}"

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理"

fake_resume = os.path.join(tmpdir, "base.docx")
open(fake_resume, "wb").close()
# tracker_xlsx_path 必须指到隔离的临时文件，不能留空——留空会解析成真实的
# ~/Downloads/JD匹配追踪表.xlsx，见 test_add_by_url.py 同一处注释里的线上事故说明。
config.save_config({
    **config.DEFAULT_CONFIG,
    "base_resume_path": fake_resume,
    "tracker_xlsx_path": os.path.join(tmpdir, "tracker.xlsx"),
})

import linkedin_tracker
import linkedin_list_scan
import job_state
import app as flask_app

models.init_db()
flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()


# ---- 1. _scan_tracker_jobs：滚动收集职位id + 出现新id才继续滚 + 连续稳定后停止 ----
class FakeLocator:
    def __init__(self, visible=False):
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self._visible

    def click(self, timeout=None):
        pass


class FakePage:
    """模拟 jobs-tracker 列表页：eval_on_selector_all 按"当前滚到第几轮"返回这一轮能
    看到的链接，没有"加载更多"按钮（get_by_text 永远不可见），靠 mouse.wheel 推进轮次。"""

    def __init__(self, rounds, url="https://www.linkedin.com/jobs-tracker/?stage=saved"):
        self.url = url
        self._rounds = rounds
        self._round_idx = 0
        self.wheel_count = 0

    def goto(self, url, wait_until=None, timeout=None):
        # 不用请求的 url 覆盖 self.url——真实 Playwright 里 page.url 是导航结束后的最终地址，
        # 可能因为登录墙重定向跟传入的 url 不一样，这里用构造函数传的 url 模拟"最终落地页"。
        pass

    def wait_for_timeout(self, ms):
        pass

    def eval_on_selector_all(self, selector, js_fn):
        idx = min(self._round_idx, len(self._rounds) - 1)
        return self._rounds[idx]

    def get_by_text(self, text, exact=False):
        return FakeLocator(visible=False)

    @property
    def mouse(self):
        page = self

        class _Mouse:
            def wheel(self, x, y):
                page.wheel_count += 1
                page._round_idx += 1

        return _Mouse()


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class FakePlaywrightCM:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


linkedin_list_scan.sync_playwright = lambda: FakePlaywrightCM()

ROUNDS = [
    ["https://www.linkedin.com/jobs/view/1111111111/"],
    ["https://www.linkedin.com/jobs/view/1111111111/", "https://www.linkedin.com/jobs/view/2222222222/"],
    ["https://www.linkedin.com/jobs/view/1111111111/", "https://www.linkedin.com/jobs/view/2222222222/",
     "https://www.linkedin.com/jobs/view/3333333333/?currentJobId=3333333333"],
]

page = FakePage(ROUNDS)
contexts_created = []


def fake_launch_context(p, headless):
    ctx = FakeContext(page)
    contexts_created.append(ctx)
    return ctx


linkedin_list_scan._launch_context = fake_launch_context

ids = linkedin_tracker._scan_tracker_jobs("saved", headless=True)
assert ids == {"1111111111", "2222222222", "3333333333"}, ids
assert page.wheel_count >= linkedin_list_scan.STABLE_ROUNDS_TO_STOP, "该滚动直到连续几轮都没有新id才停"
assert contexts_created and contexts_created[0].closed, "扫描完要关掉浏览器上下文"
print("_scan_tracker_jobs collects ids across scroll rounds and stops once stable ok")

login_wall_page = FakePage([[]], url="https://www.linkedin.com/authwall?x=1")
linkedin_list_scan._launch_context = lambda p, headless: FakeContext(login_wall_page)
assert linkedin_tracker._scan_tracker_jobs("saved", headless=True) is None
print("_scan_tracker_jobs returns None on login wall ok")


# ---- 2. fetch_tracker_job_ids：不支持的 stage 直接报错，不会尝试开浏览器/查登录态 ----
try:
    linkedin_tracker.fetch_tracker_job_ids("archived")
    assert False, "应该报错：不支持的 stage"
except linkedin_tracker.TrackerSyncError as e:
    assert "archived" in str(e), e
print("fetch_tracker_job_ids rejects unsupported stage ok")


# ---- 3. fetch_tracker_job_ids：没保存过登录态直接报错，不会尝试开浏览器 ----
import easy_apply

missing_profile_dir = os.path.join(tmpdir, "no-such-profile")
easy_apply.PROFILE_DIR = missing_profile_dir
linkedin_tracker.PROFILE_DIR = missing_profile_dir
try:
    linkedin_tracker.fetch_tracker_job_ids("saved")
    assert False, "应该报错：还没有保存过登录态"
except linkedin_tracker.TrackerSyncError as e:
    assert "ensure_logged_in" in str(e), e
print("fetch_tracker_job_ids without a saved profile raises ok")


# ---- 4. fetch_tracker_job_ids：无头撞登录墙先带界面重试一次；两次都撞墙才真报错 ----
os.makedirs(missing_profile_dir, exist_ok=True)  # 假装 profile 目录已存在（之前登录过）
calls = []


def fake_scan_always_wall(stage, headless):
    calls.append((stage, headless))
    return None


linkedin_tracker._scan_tracker_jobs = fake_scan_always_wall
try:
    linkedin_tracker.fetch_tracker_job_ids("applied")
    assert False
except linkedin_tracker.TrackerSyncError as e:
    assert "登录态已失效" in str(e), e
assert calls == [("applied", True), ("applied", False)], calls
print("fetch_tracker_job_ids retries visible after headless hits a login wall ok")

calls.clear()


def fake_scan_headless_ok(stage, headless):
    calls.append((stage, headless))
    return {"9999999999"} if headless else None


linkedin_tracker._scan_tracker_jobs = fake_scan_headless_ok
got = linkedin_tracker.fetch_tracker_job_ids("saved")
assert got == {"9999999999"} and calls == [("saved", True)], (got, calls)
print("fetch_tracker_job_ids doesn't retry visible when headless already found jobs ok")


# ---- 5. Flask 路由：不支持的 stage 返回 404 ----
r = c.get("/api/jobs/sync_tracker/archived")
assert r.status_code == 404, r.get_json()
r = c.post("/api/jobs/sync_tracker/archived")
assert r.status_code == 404, r.get_json()
print("sync_tracker_route rejects unsupported stage with 404 ok")


# ---- 6. Flask 路由：POST 启动 / 并发 409 / GET 轮询状态 / 完成后自动排队分析（stage=saved）----
sync_gate = threading.Event()
added_job_id = {}


def fake_sync_saved():
    sync_gate.wait(timeout=5)
    conn = models.get_conn()
    job_id = models.insert_job(conn, {
        "title": "Synced PM",
        "company": "Synced Co",
        "location": "Shenzhen",
        "site": "linkedin",
        "job_url": "https://www.linkedin.com/jobs/view/5555555555",
        "date_posted": "",
        "keyword": "Synced PM",
        "jd_text": "We need a product manager to own the roadmap end to end for our platform.",
    })
    conn.commit()
    conn.close()
    added_job_id["id"] = job_id
    return {
        "results": [{"url": "...", "status": "added", "job_id": job_id, "title": "Synced PM", "company": "Synced Co"}],
        "added_ids": [job_id],
        "total_found": 1,
    }


def fake_sync_tracker_stage(stage):
    assert stage == "saved"
    return fake_sync_saved()


linkedin_tracker.sync_tracker_stage = fake_sync_tracker_stage

status = c.get("/api/jobs/sync_tracker/saved").get_json()
assert status == {"syncing": False, "result": None, "error": None}, status

r = c.post("/api/jobs/sync_tracker/saved")
assert r.status_code == 200 and r.get_json() == {"started": True}, r.get_json()

for _ in range(200):
    status = c.get("/api/jobs/sync_tracker/saved").get_json()
    if status["syncing"]:
        break
    time.sleep(0.02)
assert status["syncing"] is True, "后台线程应该已经开始跑（卡在 sync_gate.wait 上）"

r2 = c.post("/api/jobs/sync_tracker/saved")
assert r2.status_code == 409, r2.get_json()
print("sync_tracker_route rejects concurrent start with 409 ok")

sync_gate.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_tracker/saved").get_json()
    if not status["syncing"]:
        break
    time.sleep(0.02)
assert status["syncing"] is False
assert status["error"] is None, status
assert status["result"]["total_found"] == 1
assert status["result"]["added_ids"] == [added_job_id["id"]]
print("sync_tracker_route reports result once background sync finishes ok")

for _ in range(300):
    if not job_state.in_progress_ids():
        break
    time.sleep(0.02)
saved_job = models.get_job(added_job_id["id"])
assert saved_job["status"] == "new"
assert saved_job["application_status"] == "not_applied", "「已收藏」同步不该动投递状态"
print("newly synced saved-list job gets queued for automatic analysis, application_status untouched ok")


# ---- 7. "已投递" stage 特有行为：新入库职位自动标记 application_status='applied' ----
applied_sync_gate = threading.Event()
applied_job_id = {}


def fake_sync_applied():
    applied_sync_gate.wait(timeout=5)
    conn = models.get_conn()
    job_id = models.insert_job(conn, {
        "title": "Applied PM",
        "company": "Applied Co",
        "location": "Shanghai",
        "site": "linkedin",
        "job_url": "https://www.linkedin.com/jobs/view/6666666666",
        "date_posted": "",
        "keyword": "Applied PM",
        "jd_text": "We need a product manager who already applied via LinkedIn to this role.",
    })
    conn.commit()
    conn.close()
    applied_job_id["id"] = job_id
    return {
        "results": [{"url": "...", "status": "added", "job_id": job_id, "title": "Applied PM", "company": "Applied Co"}],
        "added_ids": [job_id],
        "total_found": 1,
    }


def fake_sync_tracker_stage_applied(stage):
    assert stage == "applied"
    return fake_sync_applied()


linkedin_tracker.sync_tracker_stage = fake_sync_tracker_stage_applied

r = c.post("/api/jobs/sync_tracker/applied")
assert r.status_code == 200, r.get_json()
applied_sync_gate.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_tracker/applied").get_json()
    if not status["syncing"]:
        break
    time.sleep(0.02)
assert status["error"] is None, status
assert status["result"]["added_ids"] == [applied_job_id["id"]]
applied_job = models.get_job(applied_job_id["id"])
assert applied_job["application_status"] == "applied", applied_job
assert applied_job["applied_at"], "同步进来的已投递职位要记一个 applied_at 时间戳"
print("newly synced applied-list job is auto-marked application_status='applied' with applied_at ok")


# ---- 8. 出错路径：sync_tracker_stage 抛错时 GET 状态里能看到 error ----
sync_gate2 = threading.Event()


def fake_sync_tracker_stage_error(stage):
    sync_gate2.wait(timeout=5)
    raise linkedin_tracker.TrackerSyncError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")


linkedin_tracker.sync_tracker_stage = fake_sync_tracker_stage_error
r = c.post("/api/jobs/sync_tracker/saved")
assert r.status_code == 200, r.get_json()
sync_gate2.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_tracker/saved").get_json()
    if not status["syncing"]:
        break
    time.sleep(0.02)
assert status["error"] and "登录态已失效" in status["error"], status
print("sync_tracker_route surfaces TrackerSyncError via GET status ok")

print("\nALL PASS")
