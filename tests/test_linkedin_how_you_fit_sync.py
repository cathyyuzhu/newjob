"""同步 LinkedIn "How You Fit" 匹配推荐搜索的冒烟测试（见 linkedin_how_you_fit.py）。

覆盖四块：
1. URL 校验 + 确定性优先路径（fetch_search_job_ids）——跟 tracker 同步同样的
   "无头撞墙带界面重试"逻辑，这里只测差异部分（不重复测滚动收集本身，那部分已经在
   test_linkedin_tracker_sync.py 里通过共享的 linkedin_list_scan 测过）。
2. sync_search()：确定性扫描命中时不触发 agent；扫描抱空时升级给 agent。
3. agent 兜底路径（_scan_with_agent）：正常完成、候选元素排除职位链接（安全网1）、
   页面被导航离开时中止（安全网2）、无效候选编号不崩溃、步数超限、AI判定卡住、
   没有 ANTHROPIC_API_KEY。全程 mock Playwright + llm.chat_tool_step，不开真实
   浏览器、不产生真实 API 费用。
4. sync_all_enabled_searches() 的节流/异常隔离/enabled过滤，以及 Flask 路由
   （单条同步 POST/GET/409/404、批量同步、app.py update_config() 对新字段的处理）。
"""
import os
import sys
import tempfile
import threading
import time as time_module

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
config.save_config({
    **config.DEFAULT_CONFIG,
    "base_resume_path": fake_resume,
    "tracker_xlsx_path": os.path.join(tmpdir, "tracker.xlsx"),
})

import linkedin_how_you_fit as h
import linkedin_list_scan
import job_link
import job_state
import app as flask_app

models.init_db()
flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()

TEST_URL = "https://www.linkedin.com/jobs/search-results/?showHowYouFit=HOW_YOU_FIT&keywords=PM&geoId=1"


def set_searches(searches):
    cfg = config.load_config()
    cfg["linkedin_how_you_fit_searches"] = searches
    config.save_config(cfg)


# ---- 1. _validate_search_url ----
h._validate_search_url("https://www.linkedin.com/jobs/search-results/?x=1")  # 不抛就是通过
try:
    h._validate_search_url("https://example.com/jobs")
    assert False, "应该报错：域名/路径不对"
except h.HowYouFitSyncError as e:
    assert "search-results" in str(e), e
print("_validate_search_url accepts/rejects urls ok")


# ---- 2. fetch_search_job_ids：没保存过登录态直接报错，不会尝试开浏览器 ----
import easy_apply

missing_profile_dir = os.path.join(tmpdir, "no-such-profile")
easy_apply.PROFILE_DIR = missing_profile_dir
h.PROFILE_DIR = missing_profile_dir
try:
    h.fetch_search_job_ids(TEST_URL)
    assert False, "应该报错：还没有保存过登录态"
except h.HowYouFitSyncError as e:
    assert "ensure_logged_in" in str(e), e
print("fetch_search_job_ids without a saved profile raises ok")

os.makedirs(missing_profile_dir, exist_ok=True)  # 假装 profile 目录已存在（之前登录过）


# ---- 3. fetch_search_job_ids：无头撞墙先带界面重试一次；两次都撞墙才真报错 ----
scan_calls = []


def fake_scan_always_wall(url, headless):
    scan_calls.append((url, headless))
    return None


linkedin_list_scan.scan_job_list = fake_scan_always_wall
try:
    h.fetch_search_job_ids(TEST_URL)
    assert False
except h.HowYouFitSyncError as e:
    assert "登录态已失效" in str(e), e
assert scan_calls == [(TEST_URL, True), (TEST_URL, False)], scan_calls
print("fetch_search_job_ids retries visible after headless hits a login wall ok")


# ---- 4. sync_search()：确定性扫描命中时不触发 agent ----
job_link.add_jobs_from_urls = lambda urls: {
    "results": [{"url": u, "status": "added", "job_id": 1000 + i} for i, u in enumerate(urls)],
    "added_ids": [1000 + i for i in range(len(urls))],
}

real_scan_with_agent = h._scan_with_agent  # 后面几个测试要换回真实实现直接测它
agent_calls = []
h._scan_with_agent = lambda url, headless=False: (agent_calls.append(url), set())[1]

linkedin_list_scan.scan_job_list = lambda url, headless: {"1111111111"}
set_searches([{"id": "s1", "name": "PM", "url": TEST_URL, "enabled": True}])
result = h.sync_search("s1")
assert agent_calls == [], "确定性扫描命中职位时不该触发 agent 兜底"
assert result["total_found"] == 1
print("sync_search skips agent fallback when deterministic scan finds jobs ok")


# ---- 5. sync_search()：确定性扫描抱空时升级给 agent ----
linkedin_list_scan.scan_job_list = lambda url, headless: set()
h._scan_with_agent = lambda url, headless=False: (agent_calls.append(url), {"2222222222"})[1]
result = h.sync_search("s1")
assert agent_calls == [TEST_URL], "确定性扫描抱空时应该升级给 agent"
assert result["total_found"] == 1
print("sync_search escalates to agent fallback when deterministic scan finds nothing ok")

# search_id 不存在于配置
try:
    h.sync_search("no-such-id")
    assert False
except h.HowYouFitSyncError as e:
    assert "no-such-id" in str(e), e
print("sync_search raises for unknown search_id ok")


# ==================== agent 兜底路径（_scan_with_agent）====================
# 换回真实实现——上面几个 sync_search() 测试把它替身成了一个假函数。

h._scan_with_agent = real_scan_with_agent

os.environ["ANTHROPIC_API_KEY"] = "test-key"


class FakeAgentLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.page.click_calls.append(self.selector)
        override = self.page.click_url_override.get(self.selector)
        if override:
            self.page.url = override

    def evaluate(self, js):
        self.page.scroll_container_calls.append(self.selector)


class FakeAgentPage:
    """snapshots：每次 page.evaluate(_PAGE_SNAPSHOT_JS) 依次弹出的原始候选快照
    （{"items": [...]}）；弹完了就一直返回 default_snapshot（用于步数超限这类需要
    很多轮的测试，不用手写几十份快照）。"""

    def __init__(self, url, snapshots, default_snapshot=None):
        self.url = url
        self._snapshots = list(snapshots)
        self._default_snapshot = default_snapshot if default_snapshot is not None else {"items": []}
        self.tag_calls = []
        self.click_calls = []
        self.scroll_container_calls = []
        self.click_url_override = {}
        self.wheel_count = 0

    def goto(self, url, wait_until=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        if arg is None:
            return self._snapshots.pop(0) if self._snapshots else self._default_snapshot
        self.tag_calls.append(arg)
        return None

    def locator(self, selector):
        return FakeAgentLocator(self, selector)

    @property
    def mouse(self):
        page = self

        class _Mouse:
            def wheel(self, x, y):
                page.wheel_count += 1

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


def make_scripted_tool_step(tool_uses):
    queue = list(tool_uses)

    def _fake(messages, tools, system=None, model=None, max_tokens=None):
        tu = queue.pop(0)
        return {"stop_reason": "tool_use", "content_blocks": [{"type": "tool_use"}], "tool_use": tu}

    return _fake


h.sync_playwright = lambda: FakePlaywrightCM()

CLICK_CANDIDATE_WITH_JOB_LINK = {
    "items": [
        {"raw_index": 0, "kind": "click", "tag": "button", "role": "", "text": "加载更多", "href": None},
        {"raw_index": 1, "kind": "click", "tag": "a", "role": "", "text": "某职位标题",
         "href": "https://www.linkedin.com/jobs/view/999999/"},
    ]
}
SCROLL_CANDIDATE = {
    "items": [{"raw_index": 0, "kind": "scroll", "tag": "div", "role": "", "text": "(可滚动区域)", "href": None}]
}
EMPTY_CANDIDATES = {"items": []}

# ---- 6. 正常完成：点击 -> 滚动 -> finish(reached_end)，同时验证安全网1（职位链接被排除）----
collect_seq = [{"1"}, {"1", "2"}, {"1", "2", "3"}]


def fake_collect_ok(page):
    return collect_seq.pop(0)


linkedin_list_scan.collect_job_ids = fake_collect_ok
llm.chat_tool_step = make_scripted_tool_step([
    {"id": "t1", "name": "click_element", "input": {"index": 0}},
    {"id": "t2", "name": "scroll", "input": {"index": 0}},
    {"id": "t3", "name": "finish", "input": {"status": "reached_end", "reason": "没有更多了"}},
])
page = FakeAgentPage(TEST_URL, snapshots=[CLICK_CANDIDATE_WITH_JOB_LINK, SCROLL_CANDIDATE, EMPTY_CANDIDATES])
context6 = FakeContext(page)
h._launch_context = lambda p, headless: context6

result = h._scan_with_agent(TEST_URL, headless=False)
assert result == {"1", "2", "3"}, result
assert page.click_calls == ['[data-hyf-idx="0"]'], page.click_calls
assert page.scroll_container_calls == ['[data-hyf-idx="0"]'], page.scroll_container_calls
# 安全网1：候选快照里有2个原始项（raw_index 0/1），职位链接（raw_index 1）应该被排除，
# 第一次打标只对幸存的 raw_index 0 打了正式编号
assert page.tag_calls[0] == [0], page.tag_calls
assert context6.closed, "扫描完要关掉浏览器上下文"
print("_scan_with_agent completes normally and excludes job-link candidates (safety net 1) ok")


# ---- 7. 候选编号无效：不崩溃，只是这一轮不操作 ----
noop_page = FakeAgentPage(TEST_URL, snapshots=[])
h._apply_agent_action(noop_page, "click_element", {"index": 999}, {})
assert noop_page.click_calls == [], "无效编号不该真的点击任何东西"
h._apply_agent_action(noop_page, "scroll", {"index": 999}, {})
assert noop_page.scroll_container_calls == [], "无效编号不该真的滚动任何容器"
print("_apply_agent_action ignores invalid candidate index without crashing ok")


# ---- 8. 安全网2：动作执行后页面被导航离开搜索结果页，立刻判 stuck 中止 ----
linkedin_list_scan.collect_job_ids = lambda page: {"1"}
llm.chat_tool_step = make_scripted_tool_step([
    {"id": "t1", "name": "click_element", "input": {"index": 0}},
])
drift_page = FakeAgentPage(TEST_URL, snapshots=[CLICK_CANDIDATE_WITH_JOB_LINK])
drift_page.click_url_override = {'[data-hyf-idx="0"]': "https://www.linkedin.com/jobs/view/123456/"}
h._launch_context = lambda p, headless: FakeContext(drift_page)
try:
    h._scan_with_agent(TEST_URL, headless=False)
    assert False, "应该报错：页面被导航离开"
except h.HowYouFitSyncError as e:
    assert "导航离开" in str(e), e
print("_scan_with_agent aborts when page navigates away from search results (safety net 2) ok")


# ---- 9. AI 判定卡住：直接抛错，带上模型给出的 reason ----
llm.chat_tool_step = make_scripted_tool_step([
    {"id": "t1", "name": "finish", "input": {"status": "stuck", "reason": "出现了验证码"}},
])
stuck_page = FakeAgentPage(TEST_URL, snapshots=[EMPTY_CANDIDATES])
h._launch_context = lambda p, headless: FakeContext(stuck_page)
try:
    h._scan_with_agent(TEST_URL, headless=False)
    assert False
except h.HowYouFitSyncError as e:
    assert "验证码" in str(e), e
print("_scan_with_agent raises with model's reason when it decides stuck ok")


# ---- 10. 超过步数上限仍未 finish：抛错，不静默返回不完整结果 ----
llm.chat_tool_step = lambda *a, **kw: {
    "stop_reason": "tool_use", "content_blocks": [],
    "tool_use": {"id": "t", "name": "click_element", "input": {"index": 0}},
}
overflow_page = FakeAgentPage(TEST_URL, snapshots=[], default_snapshot=SCROLL_CANDIDATE)
h._launch_context = lambda p, headless: FakeContext(overflow_page)
try:
    h._scan_with_agent(TEST_URL, headless=False)
    assert False
except h.HowYouFitSyncError as e:
    assert "步数上限" in str(e), e
print("_scan_with_agent raises after exceeding MAX_AGENT_STEPS ok")


# ---- 11. 没有 ANTHROPIC_API_KEY：直接抛错，不尝试开浏览器 ----
old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
try:
    try:
        h._scan_with_agent(TEST_URL, headless=False)
        assert False
    except h.HowYouFitSyncError as e:
        assert "ANTHROPIC_API_KEY" in str(e), e
    print("_scan_with_agent raises without ANTHROPIC_API_KEY ok")
finally:
    if old_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = old_key


# ==================== sync_all_enabled_searches ====================

# ---- 12. 节流间隔 + 单条异常不影响其它条 + enabled=False 被跳过 ----
set_searches([
    {"id": "s1", "name": "A", "url": TEST_URL, "enabled": True},
    {"id": "s2", "name": "B", "url": TEST_URL, "enabled": False},
    {"id": "s3", "name": "C", "url": TEST_URL, "enabled": True},
])
cfg = config.load_config()
cfg["linkedin_how_you_fit_delay"] = 7
config.save_config(cfg)

sync_search_calls = []


def fake_sync_search(search_id):
    sync_search_calls.append(search_id)
    if search_id == "s3":
        raise RuntimeError("boom")
    return {"added_ids": [f"job-{search_id}"], "results": [], "total_found": 1}


h.sync_search = fake_sync_search
sleep_calls = []
real_sleep = time_module.sleep
time_module.sleep = lambda s: sleep_calls.append(s)
try:
    summary = h.sync_all_enabled_searches()
finally:
    time_module.sleep = real_sleep

assert sync_search_calls == ["s1", "s3"], sync_search_calls  # s2（enabled=False）被跳过
assert summary["s1"]["error"] is None and summary["s1"]["result"]["added_ids"] == ["job-s1"]
assert summary["s3"]["error"] == "boom" and summary["s3"]["result"] is None
assert sleep_calls == [7], sleep_calls
print("sync_all_enabled_searches throttles, isolates per-search errors, and skips disabled ones ok")


# ==================== Flask 路由 ====================

# ---- 13. 不存在的 search_id 返回 404 ----
set_searches([{"id": "s1", "name": "A", "url": TEST_URL, "enabled": True}])
r = c.get("/api/jobs/sync_how_you_fit/no-such-id")
assert r.status_code == 404, r.get_json()
r = c.post("/api/jobs/sync_how_you_fit/no-such-id")
assert r.status_code == 404, r.get_json()
print("sync_how_you_fit_route rejects unknown search_id with 404 ok")


# ---- 14. POST 启动 / 并发 409 / GET 轮询状态 / 完成后自动排队分析 ----
sync_gate = threading.Event()
added_job_id = {}


def fake_sync_search_route(search_id):
    assert search_id == "s1"
    sync_gate.wait(timeout=5)
    conn = models.get_conn()
    job_id = models.insert_job(conn, {
        "title": "How You Fit PM",
        "company": "Fit Co",
        "location": "Shenzhen",
        "site": "linkedin",
        "job_url": "https://www.linkedin.com/jobs/view/7777777777",
        "date_posted": "",
        "keyword": "How You Fit PM",
        "jd_text": "We think you're a great fit for this product manager role.",
    })
    conn.commit()
    conn.close()
    added_job_id["id"] = job_id
    return {
        "results": [{"url": "...", "status": "added", "job_id": job_id}],
        "added_ids": [job_id],
        "total_found": 1,
    }


h.sync_search = fake_sync_search_route

status = c.get("/api/jobs/sync_how_you_fit/s1").get_json()
assert status == {"syncing": False, "result": None, "error": None}, status

r = c.post("/api/jobs/sync_how_you_fit/s1")
assert r.status_code == 200 and r.get_json() == {"started": True}, r.get_json()

for _ in range(200):
    status = c.get("/api/jobs/sync_how_you_fit/s1").get_json()
    if status["syncing"]:
        break
    time_module.sleep(0.02)
assert status["syncing"] is True, "后台线程应该已经开始跑（卡在 sync_gate.wait 上）"

r2 = c.post("/api/jobs/sync_how_you_fit/s1")
assert r2.status_code == 409, r2.get_json()
print("sync_how_you_fit_route rejects concurrent start with 409 ok")

sync_gate.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_how_you_fit/s1").get_json()
    if not status["syncing"]:
        break
    time_module.sleep(0.02)
assert status["syncing"] is False
assert status["error"] is None, status
assert status["result"]["added_ids"] == [added_job_id["id"]]
print("sync_how_you_fit_route reports result once background sync finishes ok")

for _ in range(300):
    if not job_state.in_progress_ids():
        break
    time_module.sleep(0.02)
synced_job = models.get_job(added_job_id["id"])
assert synced_job["status"] == "new"
assert synced_job["application_status"] == "not_applied", "How You Fit 同步不该动投递状态"
print("newly synced how-you-fit job gets queued for automatic analysis ok")


# ---- 15. 出错路径：sync_search 抛错时 GET 状态里能看到 error ----
sync_gate2 = threading.Event()


def fake_sync_search_error(search_id):
    sync_gate2.wait(timeout=5)
    raise h.HowYouFitSyncError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")


h.sync_search = fake_sync_search_error
r = c.post("/api/jobs/sync_how_you_fit/s1")
assert r.status_code == 200, r.get_json()
sync_gate2.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_how_you_fit/s1").get_json()
    if not status["syncing"]:
        break
    time_module.sleep(0.02)
assert status["error"] and "登录态已失效" in status["error"], status
print("sync_how_you_fit_route surfaces HowYouFitSyncError via GET status ok")


# ---- 16. 批量同步路由：POST 启动 / 409 / GET 轮询 ----
batch_gate = threading.Event()


def fake_sync_all(delay_seconds=None):
    batch_gate.wait(timeout=5)
    return {"s1": {"result": {"added_ids": []}, "error": None}}


h.sync_all_enabled_searches = fake_sync_all
r = c.post("/api/jobs/sync_how_you_fit_all")
assert r.status_code == 200 and r.get_json() == {"started": True}, r.get_json()
r2 = c.post("/api/jobs/sync_how_you_fit_all")
assert r2.status_code == 409, r2.get_json()
batch_gate.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_how_you_fit_all").get_json()
    if not status["syncing"]:
        break
    time_module.sleep(0.02)
assert status["error"] is None, status
print("sync_how_you_fit_all_route starts/blocks-concurrent/reports result ok")


# ==================== app.py update_config() ====================

# ---- 17. 新增项没带 id：后端生成一个 12 位十六进制 id ----
r = c.post("/api/config", json={"linkedin_how_you_fit_searches": [
    {"name": "Director PM", "url": "https://www.linkedin.com/jobs/search-results/?keywords=Director", "enabled": True}
]})
assert r.status_code == 200, r.get_json()
saved = r.get_json()["linkedin_how_you_fit_searches"]
assert len(saved) == 1
new_id = saved[0]["id"]
assert len(new_id) == 12 and all(ch in "0123456789abcdef" for ch in new_id), new_id
print("update_config assigns a stable id to new how-you-fit search entries ok")

# ---- 18. 已有 id 的项：id 保持不变，字段可以更新 ----
r2 = c.post("/api/config", json={"linkedin_how_you_fit_searches": [
    {"id": new_id, "name": "Director PM (改名)",
     "url": "https://www.linkedin.com/jobs/search-results/?keywords=Director", "enabled": False}
]})
saved2 = r2.get_json()["linkedin_how_you_fit_searches"]
assert saved2[0]["id"] == new_id, "已有 id 不该被重新生成"
assert saved2[0]["name"] == "Director PM (改名)" and saved2[0]["enabled"] is False
print("update_config preserves existing id and updates other fields ok")

# ---- 19. 超过数量上限：400 ----
too_many = [
    {"name": f"s{i}", "url": "https://www.linkedin.com/jobs/search-results/?x=1", "enabled": True}
    for i in range(h.MAX_HOW_YOU_FIT_SEARCHES + 1)
]
r3 = c.post("/api/config", json={"linkedin_how_you_fit_searches": too_many})
assert r3.status_code == 400 and "最多配置" in r3.get_json()["error"], r3.get_json()
print("update_config rejects too many how-you-fit searches with 400 ok")

# ---- 20. URL 格式非法：400，报错文案带上这条搜索的名字 ----
r4 = c.post("/api/config", json={"linkedin_how_you_fit_searches": [
    {"name": "坏链接", "url": "https://example.com/not-linkedin", "enabled": True}
]})
assert r4.status_code == 400 and "坏链接" in r4.get_json()["error"], r4.get_json()
print("update_config rejects invalid how-you-fit search url with 400 ok")

# ---- 21. 删除某条搜索时顺带清理 job_state 里的同步状态 ----
job_state.finish_how_you_fit_sync(new_id, result={"total_found": 99})
assert job_state.how_you_fit_sync_result(new_id) == {"total_found": 99}
r5 = c.post("/api/config", json={"linkedin_how_you_fit_searches": []})
assert r5.status_code == 200
assert job_state.how_you_fit_sync_result(new_id) is None, "删除搜索配置时应该顺带清理旧的同步状态"
print("update_config discards job_state for removed how-you-fit searches ok")

print("\nALL PASS")
