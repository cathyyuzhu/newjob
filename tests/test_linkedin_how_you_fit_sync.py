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
import routes_search

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


# ---- 3b. 登录态熔断：连续 2 次真正判定登录态失效后，第 3 次直接快速失败，不再开浏览器
#          （2026-08-29，见 job_state.py 顶部说明）----
job_state._linkedin_auth = {"consecutive_failures": 0, "opened_until": None}  # 隔离本测试

scan_calls.clear()
for _ in range(2):
    try:
        h.fetch_search_job_ids(TEST_URL)
        assert False
    except h.HowYouFitSyncError:
        pass
assert job_state.linkedin_auth_breaker_open() is True
scan_calls.clear()
try:
    h.fetch_search_job_ids(TEST_URL)
    assert False, "熔断打开时应该直接快速失败"
except h.HowYouFitAuthError as e:
    assert "暂停自动化" in str(e), e
assert scan_calls == [], "熔断打开时不应该真的去扫描页面"
print("fetch_search_job_ids trips the LinkedIn auth breaker after repeated auth failures and fast-fails ok")

# 成功一次清零熔断，不影响后面的测试
job_state._linkedin_auth = {"consecutive_failures": 0, "opened_until": None}
linkedin_list_scan.scan_job_list = lambda url, headless: {"9999999999"}
got = h.fetch_search_job_ids(TEST_URL)
assert got == {"9999999999"}
assert job_state.linkedin_auth_breaker_open() is False
print("fetch_search_job_ids clears the auth breaker counter on success ok")


# ---- 4. sync_search()：确定性扫描命中足够多职位时不触发 agent ----
job_link.add_jobs_from_urls = lambda urls: {
    "results": [{"url": u, "status": "added", "job_id": 1000 + i} for i, u in enumerate(urls)],
    "added_ids": [1000 + i for i in range(len(urls))],
}

real_scan_with_agent = h._scan_with_agent  # 后面几个测试要换回真实实现直接测它
agent_calls = []
h._scan_with_agent = lambda url, headless=False: (agent_calls.append(url), set())[1]

enough_ids = {f"111111{i:04d}" for i in range(h.MIN_DETERMINISTIC_JOB_IDS)}
linkedin_list_scan.scan_job_list = lambda url, headless: set(enough_ids)
set_searches([{"id": "s1", "name": "PM", "url": TEST_URL, "enabled": True}])
result = h.sync_search("s1")
assert agent_calls == [], "确定性扫描命中足够职位时不该触发 agent 兜底"
assert result["total_found"] == len(enough_ids)
print("sync_search skips agent fallback when deterministic scan finds enough jobs ok")


# ---- 4b. sync_search(force_agent=True)：设置页"用 agent 测试"按钮的入口，跳过
#          确定性扫描直接强制走 agent，即使确定性扫描本来能扫到足够职位 ----
scan_job_list_calls = []
linkedin_list_scan.scan_job_list = lambda url, headless: (scan_job_list_calls.append(url), set(enough_ids))[1]
agent_calls.clear()
h._scan_with_agent = lambda url, headless=False: (agent_calls.append(url), {"6666666666"})[1]
result = h.sync_search("s1", force_agent=True)
assert scan_job_list_calls == [], "force_agent=True 应该完全跳过确定性扫描"
assert agent_calls == [TEST_URL]
assert result["total_found"] == 1 and result["degraded"] is None
print("sync_search(force_agent=True) skips deterministic scan and forces the agent path ok")
agent_calls.clear()


# ---- 5. sync_search()：确定性扫描抱空时升级给 agent ----
linkedin_list_scan.scan_job_list = lambda url, headless: set()
h._scan_with_agent = lambda url, headless=False: (agent_calls.append(url), {"2222222222"})[1]
result = h.sync_search("s1")
assert agent_calls == [TEST_URL], "确定性扫描抱空时应该升级给 agent"
assert result["total_found"] == 1
print("sync_search escalates to agent fallback when deterministic scan finds nothing ok")


# ---- 5b. sync_search()：确定性扫描收集到但数量明显偏少时，也升级给 agent，并集去重 ----
agent_calls.clear()
linkedin_list_scan.scan_job_list = lambda url, headless: {"3333333333", "4444444444"}
h._scan_with_agent = lambda url, headless=False: (agent_calls.append(url), {"4444444444", "5555555555"})[1]
result = h.sync_search("s1")
assert agent_calls == [TEST_URL], "确定性扫描数量偏少（低于 MIN_DETERMINISTIC_JOB_IDS）时也应该升级给 agent"
assert result["total_found"] == 3, "偏少的确定性结果应该跟 agent 结果取并集去重，而不是被丢弃"
print("sync_search escalates to agent fallback and merges results when deterministic scan finds too few ok")


# ---- 5c. sync_search()：agent 兜底本身失败（判定 stuck/超步数）不该连累确定性扫描
#          已经拿到的结果——2026-08-29 修复的设计级 bug，见 sync_search() 顶部说明 ----
agent_calls.clear()
linkedin_list_scan.scan_job_list = lambda url, headless: {"3333333333", "4444444444"}


def failing_agent(url, headless=False):
    agent_calls.append(url)
    raise h.HowYouFitSyncError("AI 判定导航卡住：遇到验证码")


h._scan_with_agent = failing_agent
result = h.sync_search("s1")
assert agent_calls == [TEST_URL]
assert result["total_found"] == 2, "agent 兜底失败时应该保留确定性扫描已收集到的职位，而不是整条同步失败"
assert result["degraded"] and "卡住" in result["degraded"], result["degraded"]
print("sync_search keeps deterministic results and reports degraded when agent fallback fails ok")


# ---- 5d. sync_search()：agent 步数耗尽但异常带了 partial_job_ids 时，要并进最终结果
#          （2026-08-30 修复——之前只保留确定性扫描的结果，agent 自己已经收集到的
#          真实数据会被无声丢弃，见 _scan_with_agent() 步数上限那段说明）----
agent_calls.clear()
linkedin_list_scan.scan_job_list = lambda url, headless: {"3333333333"}


def step_limit_agent_with_partial_data(url, headless=False):
    agent_calls.append(url)
    err = h.HowYouFitSyncError("AI 导航超过步数上限（12轮）仍未确定结束，本次同步中止")
    err.partial_job_ids = {"3333333333", "7777777777", "8888888888"}
    raise err


h._scan_with_agent = step_limit_agent_with_partial_data
result = h.sync_search("s1")
assert agent_calls == [TEST_URL]
assert result["total_found"] == 3, "步数耗尽但 agent 已经收集到的真实职位id应该并进最终结果，不能只剩确定性扫描那 1 条"
assert result["degraded"] and "步数上限" in result["degraded"], result["degraded"]
print("sync_search merges partial_job_ids from a step-limit failure into the final result ok")

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

    def _fake(messages, tools, provider="anthropic", system=None, model=None, max_tokens=None):
        tu = queue.pop(0)
        return {"stop_reason": "tool_use", "assistant_message": {"role": "assistant", "content": []}, "tool_use": tu}

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


# ---- 6b. 安全网1 也要拦住 componentkey 版职位卡片（2026-08-30）：LinkedIn 改版后
#          职位卡片变成 <div role="button" componentkey="job-card-component-ref-ID">，
#          没有 href，只靠 parse_linkedin_job_id() 完全拦不住——实测会被 agent 当成
#          普通可点击候选逐个点开，点开只是切到详情面板、不算"导航离开"，那道安全网
#          也拦不住，纯粹浪费步数一个新职位都收集不到 ----
CLICK_CANDIDATE_WITH_COMPONENTKEY_JOB_CARD = {
    "items": [
        {"raw_index": 0, "kind": "click", "tag": "button", "role": "", "text": "加载更多",
         "href": None, "componentKey": None},
        {"raw_index": 1, "kind": "click", "tag": "div", "role": "button", "text": "某职位标题",
         "href": None, "componentKey": "job-card-component-ref-4455933085"},
    ]
}
collect_seq = [{"1"}, {"1", "2"}, {"1", "2", "3"}]
linkedin_list_scan.collect_job_ids = fake_collect_ok
llm.chat_tool_step = make_scripted_tool_step([
    {"id": "t1", "name": "click_element", "input": {"index": 0}},
    {"id": "t2", "name": "scroll", "input": {"index": 0}},
    {"id": "t3", "name": "finish", "input": {"status": "reached_end", "reason": "没有更多了"}},
])
page = FakeAgentPage(
    TEST_URL, snapshots=[CLICK_CANDIDATE_WITH_COMPONENTKEY_JOB_CARD, SCROLL_CANDIDATE, EMPTY_CANDIDATES]
)
context6b = FakeContext(page)
h._launch_context = lambda p, headless: context6b

result = h._scan_with_agent(TEST_URL, headless=False)
assert result == {"1", "2", "3"}, result
# componentkey 版职位卡片（raw_index 1）应该被排除，只有 raw_index 0 幸存并打上正式编号
assert page.tag_calls[0] == [0], page.tag_calls
print("_scan_with_agent excludes componentkey-based job card candidates (safety net 1b) ok")


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


# ---- 10. 超过步数上限仍未 finish：抛错，但异常上要带上已收集到的 job_ids（2026-08-30
#          修复的真实数据丢失 bug——排查"用 agent 测试"报 0 条时发现，collect_job_ids()
#          明明每轮都收集到了完整数据，只是模型没调 finish，之前直接抛错会把这些真实
#          数据一起扔掉，见 _scan_with_agent() 步数上限那段说明）----
llm.chat_tool_step = lambda *a, **kw: {
    "stop_reason": "tool_use", "assistant_message": {"role": "assistant", "content": []},
    "tool_use": {"id": "t", "name": "click_element", "input": {"index": 0}},
}
overflow_page = FakeAgentPage(TEST_URL, snapshots=[], default_snapshot=SCROLL_CANDIDATE)
h._launch_context = lambda p, headless: FakeContext(overflow_page)
linkedin_list_scan.collect_job_ids = lambda page: {"1", "2", "3"}
try:
    h._scan_with_agent(TEST_URL, headless=False)
    assert False
except h.HowYouFitSyncError as e:
    assert "步数上限" in str(e), e
    assert e.partial_job_ids == {"1", "2", "3"}, "步数耗尽不该把已经收集到的真实职位id也一起扔掉"
print("_scan_with_agent raises after exceeding MAX_AGENT_STEPS but preserves collected job_ids ok")
linkedin_list_scan.collect_job_ids = lambda page: {"1"}


# ---- 11. 两个 API key 都没有：直接抛错，不尝试开浏览器 ----
old_anthropic_key = os.environ.pop("ANTHROPIC_API_KEY", None)
old_deepseek_key = os.environ.pop("DEEPSEEK_API_KEY", None)
try:
    try:
        h._scan_with_agent(TEST_URL, headless=False)
        assert False
    except h.HowYouFitSyncError as e:
        assert "ANTHROPIC_API_KEY" in str(e) and "DEEPSEEK_API_KEY" in str(e), e
    print("_scan_with_agent raises without ANTHROPIC_API_KEY or DEEPSEEK_API_KEY ok")

    # ---- 11b. 没有 ANTHROPIC_API_KEY 但有 DEEPSEEK_API_KEY：退到 deepseek，正常跑完 ----
    os.environ["DEEPSEEK_API_KEY"] = "test-key"
    seen_providers = []
    real_chat_tool_step = make_scripted_tool_step([
        {"id": "t1", "name": "finish", "input": {"status": "reached_end", "reason": "没有更多了"}},
    ])

    def _capture_provider(messages, tools, provider="anthropic", system=None, model=None, max_tokens=None):
        seen_providers.append(provider)
        return real_chat_tool_step(messages, tools, provider=provider, system=system, model=model, max_tokens=max_tokens)

    llm.chat_tool_step = _capture_provider
    linkedin_list_scan.collect_job_ids = lambda page: {"1"}
    fallback_page = FakeAgentPage(TEST_URL, snapshots=[EMPTY_CANDIDATES])
    h._launch_context = lambda p, headless: FakeContext(fallback_page)
    result_fallback = h._scan_with_agent(TEST_URL, headless=False)
    assert result_fallback == {"1"}, result_fallback
    assert seen_providers == ["deepseek"], seen_providers
    print("_scan_with_agent falls back to deepseek when only DEEPSEEK_API_KEY is set ok")
finally:
    os.environ.pop("DEEPSEEK_API_KEY", None)
    if old_anthropic_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = old_anthropic_key
    if old_deepseek_key is not None:
        os.environ["DEEPSEEK_API_KEY"] = old_deepseek_key


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


def fake_sync_search(search_id, force_agent=False):
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


# ---- 12b. 登录态失效应该立刻中止整批，不再尝试剩下的搜索——继续跑只是在同一个已经
#           出问题的账号上反复撞墙，跟"这一条搜索恰好有问题"（上面 s3 的场景）性质
#           不同（2026-08-29，缺口5修复）----
set_searches([
    {"id": "a1", "name": "A", "url": TEST_URL, "enabled": True},
    {"id": "a2", "name": "B", "url": TEST_URL, "enabled": True},
])
auth_calls = []


def fake_sync_search_auth(search_id, force_agent=False):
    auth_calls.append(search_id)
    raise h.HowYouFitAuthError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")


h.sync_search = fake_sync_search_auth
summary = h.sync_all_enabled_searches(delay_seconds=0)
assert auth_calls == ["a1"], "登录态失效应该立刻中止整批，不再尝试第二条搜索"
assert summary["a1"]["error"] and "a2" not in summary, summary
print("sync_all_enabled_searches aborts the whole batch when a search hits LinkedInAuthRequired ok")


# ==================== Flask 路由 ====================

# ---- 13. 不存在的 search_id 返回 404 ----
set_searches([{"id": "s1", "name": "A", "url": TEST_URL, "enabled": True}])
r = c.get("/api/jobs/sync_how_you_fit/no-such-id")
assert r.status_code == 404, r.get_json()
r = c.post("/api/jobs/sync_how_you_fit/no-such-id")
assert r.status_code == 404, r.get_json()
print("sync_how_you_fit_route rejects unknown search_id with 404 ok")


# ---- 13b. POST ?force_agent=1 应该原样转发给 sync_search()（设置页"用 agent
#           测试"按钮，2026-08-29）----
force_agent_seen = []
force_gate = threading.Event()


def fake_sync_search_force_agent(search_id, force_agent=False):
    force_agent_seen.append(force_agent)
    force_gate.wait(timeout=5)
    return {"results": [], "added_ids": [], "total_found": 0}


h.sync_search = fake_sync_search_force_agent
r = c.post("/api/jobs/sync_how_you_fit/s1?force_agent=1")
assert r.status_code == 200, r.get_json()
for _ in range(200):
    if force_agent_seen:
        break
    time_module.sleep(0.02)
force_gate.set()
for _ in range(300):
    status = c.get("/api/jobs/sync_how_you_fit/s1").get_json()
    if not status["syncing"]:
        break
    time_module.sleep(0.02)
assert force_agent_seen == [True], force_agent_seen
print("sync_how_you_fit_route forwards ?force_agent=1 to sync_search ok")
job_state.discard_how_you_fit_state("s1")  # 恢复干净状态，不影响下面测试 14 的初始状态断言


# ---- 14. POST 启动 / 并发 409 / GET 轮询状态 / 完成后自动排队分析 ----
sync_gate = threading.Event()
added_job_id = {}


def fake_sync_search_route(search_id, force_agent=False):
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


def fake_sync_search_error(search_id, force_agent=False):
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


# ==================== app.py trigger_search() 顺带触发 How You Fit 批量同步 ====================
# 2026-08-23 用户明确要求：手动点「智能抓取」主按钮时，如果配置了已启用的 How You Fit
# 搜索，顺带在后台跑一次批量同步，不需要用户额外去点下拉菜单里的「同步全部」。跟每日
# 定时任务的取舍一致（scheduler.py），两个风险源互相独立、失败互不牵连。


def fake_run_search_once():
    return {"found": 0, "added": 0, "skipped_duplicate": 0, "skipped_irrelevant": 0, "errors": [], "new_job_ids": []}


# trigger_search() 现在住在 routes_search.py 里，patch 要打在它实际引用 run_search_once
# 的那个模块上（app.py 已经不再直接定义任何路由）。
routes_search.run_search_once = fake_run_search_once

# ---- 17. 没有配置任何已启用的 How You Fit 搜索：不触发批量同步 ----
set_searches([])
r = c.post("/api/search/run")
assert r.status_code == 200, r.get_json()
assert r.get_json()["how_you_fit_started"] is False
assert job_state.how_you_fit_batch_syncing() is False
print("trigger_search skips how-you-fit batch sync when no enabled searches are configured ok")

# ---- 18. 配了已启用的搜索：顺带触发批量同步，跟专门的「同步全部」按钮共用同一把锁 ----
set_searches([{"id": "s1", "name": "PM", "url": TEST_URL, "enabled": True}])
trigger_gate = threading.Event()


def fake_sync_all_2(delay_seconds=None):
    trigger_gate.wait(timeout=5)
    return {"s1": {"result": {"added_ids": []}, "error": None}}


h.sync_all_enabled_searches = fake_sync_all_2
r = c.post("/api/search/run")
assert r.status_code == 200, r.get_json()
assert r.get_json()["how_you_fit_started"] is True
assert job_state.how_you_fit_batch_syncing() is True
# 撞车验证：批量同步正在跑的时候，专门的「同步全部」按钮应该照常收到 409——两个入口
# 共用同一把锁，不能同时各跑一份。
r_conflict = c.post("/api/jobs/sync_how_you_fit_all")
assert r_conflict.status_code == 409, r_conflict.get_json()
trigger_gate.set()
for _ in range(300):
    if not job_state.how_you_fit_batch_syncing():
        break
    time_module.sleep(0.02)
print("trigger_search starts how-you-fit batch sync sharing the same lock as the dedicated button ok")

# ---- 19. 批量同步已经在别处跑着时：顺带触发静默跳过，不报错、不打断已有同步 ----
set_searches([{"id": "s1", "name": "PM", "url": TEST_URL, "enabled": True}])
running_gate = threading.Event()
finish_running_gate = threading.Event()


def fake_sync_all_3(delay_seconds=None):
    running_gate.set()
    finish_running_gate.wait(timeout=5)
    return {"s1": {"result": {"added_ids": []}, "error": None}}


h.sync_all_enabled_searches = fake_sync_all_3
r_started = c.post("/api/jobs/sync_how_you_fit_all")
assert r_started.status_code == 200, r_started.get_json()
running_gate.wait(timeout=5)
r2 = c.post("/api/search/run")
assert r2.status_code == 200, r2.get_json()
assert r2.get_json()["how_you_fit_started"] is False
finish_running_gate.set()
for _ in range(300):
    if not job_state.how_you_fit_batch_syncing():
        break
    time_module.sleep(0.02)
print("trigger_search silently skips how-you-fit batch sync when one is already running ok")


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
