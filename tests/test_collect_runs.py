"""职位收集运行流水（collect_runs 表）+ 生产级加固原语（分层重试/墙钟deadline/空
结果健康检查/故障现场取证）的回归测试。见 collect_errors.py / models.py 里
collect_run_*/recent_found_counts 函数 / routes_misc.py 的 /api/collect/stats
（对标 llm_calls 的 test_llm_logging.py）。

覆盖：
1. collect_errors.classify()：能从异常类型/HTTP状态码/文案判断出来的几类
   （auth/locked/rate_limited/transient），包括"被包了一层再 from e 抛出"的
   __cause__ 链路；分不出来的一律归 bug。
2. models.insert_collect_run() 记的字段对不对、collect_run_stats/totals 聚合对不对。
3. collect_errors.with_retry()：只重试 transient/rate_limited，重试次数有上限，
   auth 这类不该重试的第一次失败就直接抛出；note_retry() 手动计数。
4. collect_errors 的墙钟 deadline（start_run_deadline/check_deadline/
   clear_run_deadline）。
5. collect_errors.is_suspicious_drop()：跟历史中位数比的断崖判定。
6. collect_errors.save_debug_snapshot()：写 html+截图、按 label 滚动保留最近 N 份、
   page 对象本身坏掉时不该抛出。
7. models.recent_found_counts()：只看成功运行。
8. GET /api/collect/stats 路由能正常拼出 by_source/by_error_kind/totals。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH
models.init_db()

import job_state
from easy_apply import EasyApplyInProgress

import collect_errors


# ==================== 1. collect_errors.classify() ====================

assert collect_errors.classify(job_state.LinkedInAuthRequired("登录态失效")) == "auth"
print("classify recognizes LinkedInAuthRequired as auth ok")

assert collect_errors.classify(EasyApplyInProgress("profile 被占用")) == "locked"
print("classify recognizes EasyApplyInProgress as locked ok")

# __cause__ 链路：tracker/how_you_fit 捕获 EasyApplyInProgress 后包成自己的
# XxxSyncError 再 `from e` 重新抛出，包完的类型不再是 EasyApplyInProgress。
try:
    try:
        raise EasyApplyInProgress("原始异常")
    except EasyApplyInProgress as inner:
        raise RuntimeError("包了一层") from inner
except RuntimeError as wrapped:
    assert collect_errors.classify(wrapped) == "locked", "应该顺着 __cause__ 找到原始的 EasyApplyInProgress"
print("classify looks through __cause__ to find the original EasyApplyInProgress ok")

assert collect_errors.classify(RuntimeError("HTTP 429 Too Many Requests")) == "rate_limited"
assert collect_errors.classify(RuntimeError("some error"), http_status=429) == "rate_limited"
print("classify recognizes rate limiting from status code or message ok")

assert collect_errors.classify(RuntimeError("connection timed out")) == "transient"
assert collect_errors.classify(TimeoutError("timeout")) == "transient"
assert collect_errors.classify(RuntimeError("server error"), http_status=502) == "transient"
print("classify recognizes transient network failures ok")

assert collect_errors.classify(ValueError("完全不认识的错误")) == "bug"
assert collect_errors.classify(None) == "bug"
print("classify defaults unrecognized failures to bug (conservative: don't retry blindly) ok")

for kind in collect_errors.KINDS:
    assert kind in collect_errors.RETRY_POLICY, kind
print("every kind has a retry policy entry ok")


# ==================== 2. models.insert_collect_run / stats ====================

models.insert_collect_run(
    source="jobspy", started_at="2026-08-29T10:00:00", duration_ms=1200,
    found=20, added=5, skipped_duplicate=10, skipped_irrelevant=5, failed=0,
    ok=1, error_kind=None, error_detail=None, retries=0, agent_used=0, suspicious=0,
)
models.insert_collect_run(
    source="jobspy", started_at="2026-08-29T11:00:00", duration_ms=800,
    found=0, added=0, skipped_duplicate=0, skipped_irrelevant=0, failed=1,
    ok=0, error_kind="transient", error_detail="connection reset", retries=0,
    agent_used=0, suspicious=0,
)
models.insert_collect_run(
    source="tracker_saved", started_at="2026-08-29T12:00:00", duration_ms=30000,
    found=12, added=3, skipped_duplicate=9, skipped_irrelevant=0, failed=0,
    ok=0, error_kind="auth", error_detail="登录态已失效", retries=0,
    agent_used=0, suspicious=0,
)
models.insert_collect_run(
    source="hyf_s1", started_at="2026-08-29T13:00:00", duration_ms=45000,
    found=4, added=4, skipped_duplicate=0, skipped_irrelevant=0, failed=0,
    ok=1, error_kind="structure", error_detail="AI 判定导航卡住", retries=0,
    agent_used=1, suspicious=0,
)

conn = models.get_conn()
rows = [dict(r) for r in conn.execute("SELECT * FROM collect_runs ORDER BY id").fetchall()]
conn.close()
assert len(rows) == 4
assert rows[0]["source"] == "jobspy" and rows[0]["ok"] == 1 and rows[0]["found"] == 20
assert rows[1]["ok"] == 0 and rows[1]["error_kind"] == "transient"
assert rows[3]["agent_used"] == 1 and rows[3]["error_kind"] == "structure" and rows[3]["ok"] == 1
print("insert_collect_run stores fields (including the degraded-but-ok hyf row) ok")

by_source = {r["key"]: r for r in models.collect_run_stats(group_by="source")}
assert by_source["jobspy"]["runs"] == 2 and by_source["jobspy"]["failures"] == 1
assert by_source["tracker_saved"]["runs"] == 1 and by_source["tracker_saved"]["failures"] == 1
assert by_source["hyf_s1"]["runs"] == 1 and by_source["hyf_s1"]["failures"] == 0
print("collect_run_stats group_by=source aggregates runs/failures correctly ok")

by_kind = {r["key"]: r for r in models.collect_run_stats(group_by="error_kind")}
assert set(by_kind.keys()) == {"transient", "auth", "structure"}, by_kind.keys()
print("collect_run_stats group_by=error_kind excludes rows with no error_kind ok")

totals = models.collect_run_totals()
assert totals["runs"] == 4 and totals["failures"] == 2
assert totals["found"] == 20 + 0 + 12 + 4 and totals["added"] == 5 + 0 + 3 + 4
print("collect_run_totals sums across all sources ok")

since_totals = models.collect_run_totals(since="2026-08-29T12:00:00")
assert since_totals["runs"] == 2, since_totals
print("collect_run_totals respects the since filter ok")

try:
    models.collect_run_stats(group_by="bogus")
    assert False
except RuntimeError as e:
    assert "bogus" in str(e)
print("collect_run_stats rejects unknown group_by ok")


# ==================== 3. collect_errors.with_retry / 重试计数 ====================

# collect_errors.time 就是进程里唯一那份 time 模块，这里改的是全局 time.sleep，
# 不是 collect_errors.py 私有的一份——本文件后面没有任何代码依赖真实的 time.sleep
# 计时（不像 test_add_by_url.py 有轮询后台线程结果的循环），所以不需要用完再复原，
# 但如果以后要往这个文件加类似的轮询测试，记得先把这一行挪到需要重试的调用附近、
# 跑完立刻复原。
collect_errors.time.sleep = lambda s: None  # 测试不真的等待退避

calls = []


import requests


def flaky_then_ok():
    calls.append(1)
    if len(calls) < 3:
        raise requests.exceptions.ConnectionError("connection reset")  # 归类成 transient
    return "ok"


collect_errors.reset_retry_count()
result = collect_errors.with_retry(flaky_then_ok)
assert result == "ok" and len(calls) == 3
assert collect_errors.get_retry_count() == 2, collect_errors.get_retry_count()
print("with_retry retries transient failures until success, counting retries ok")

calls.clear()


def always_fails():
    calls.append(1)
    raise requests.exceptions.ConnectionError("connection reset")


collect_errors.reset_retry_count()
try:
    collect_errors.with_retry(always_fails)
    assert False
except requests.exceptions.ConnectionError:
    pass
assert len(calls) == collect_errors.RETRY_POLICY["transient"]["max_retries"] + 1, len(calls)
print("with_retry gives up after max_retries and re-raises the last exception ok")

calls.clear()


def auth_failure():
    calls.append(1)
    raise job_state.LinkedInAuthRequired("登录态失效")


try:
    collect_errors.with_retry(auth_failure)
    assert False
except job_state.LinkedInAuthRequired:
    pass
assert len(calls) == 1, "auth 不重试，第一次失败就该直接抛出（继续撞只会加深账号风险）"
print("with_retry does not retry non-retryable kinds like auth ok")

before = collect_errors.get_retry_count()
collect_errors.note_retry()
assert collect_errors.get_retry_count() == before + 1
print("note_retry manually bumps the retry counter (for retry paths outside with_retry) ok")


# ==================== 4. collect_errors 墙钟 deadline ====================

collect_errors.clear_run_deadline()
collect_errors.check_deadline()  # 没设过 deadline，什么都不做，不该抛
print("check_deadline is a no-op before start_run_deadline() is ever called ok")

collect_errors.start_run_deadline(seconds=-1)  # 负数=已经过期
try:
    collect_errors.check_deadline("测试超时")
    assert False
except TimeoutError as e:
    assert "测试超时" in str(e)
collect_errors.clear_run_deadline()
collect_errors.check_deadline()  # clear 之后应该恢复成空操作
print("start_run_deadline/check_deadline/clear_run_deadline round-trip correctly ok")


# ==================== 5. collect_errors.is_suspicious_drop ====================

assert collect_errors.is_suspicious_drop(0, [10, 12, 11]) is True
assert collect_errors.is_suspicious_drop(10, [10, 12, 11]) is False
assert collect_errors.is_suspicious_drop(0, [10]) is False, "历史数据不够时不该判可疑"
assert collect_errors.is_suspicious_drop(2, [3, 3, 3]) is False, "历史本来就低时，绝对值低不该单独判可疑"
print("is_suspicious_drop only flags a drop with enough history AND a low absolute count ok")


# ==================== 6. collect_errors.save_debug_snapshot ====================

snapshot_root = tempfile.mkdtemp()
collect_errors.DEBUG_SNAPSHOT_DIR = snapshot_root


class FakePage:
    def content(self):
        return "<html>fake</html>"

    def screenshot(self, path=None, full_page=None):
        with open(path, "wb") as f:
            f.write(b"fake-png")


for _ in range(collect_errors.MAX_DEBUG_SNAPSHOTS_PER_LABEL + 2):
    collect_errors.save_debug_snapshot("test_label", FakePage())

saved = sorted(d for d in os.listdir(snapshot_root) if d.startswith("test_label_"))
assert len(saved) == collect_errors.MAX_DEBUG_SNAPSHOTS_PER_LABEL, saved
html_path = os.path.join(snapshot_root, saved[-1], "page.html")
png_path = os.path.join(snapshot_root, saved[-1], "screenshot.png")
assert os.path.exists(html_path) and open(html_path, encoding="utf-8").read() == "<html>fake</html>"
assert os.path.exists(png_path)
print("save_debug_snapshot writes html+screenshot and prunes down to the most recent N ok")


class BrokenPage:
    def content(self):
        raise RuntimeError("page already closed")

    def screenshot(self, **kwargs):
        raise RuntimeError("page already closed")


collect_errors.save_debug_snapshot("broken_label", BrokenPage())  # 不该抛出
print("save_debug_snapshot never raises even when the page object is unusable ok")


# ==================== 7. models.recent_found_counts ====================

for i, found in enumerate([5, 8, 6]):
    models.insert_collect_run(
        source="tracker_applied", started_at=f"2026-08-29T0{i}:00:00", duration_ms=100,
        found=found, added=0, skipped_duplicate=0, skipped_irrelevant=0, failed=0,
        ok=1, error_kind=None, error_detail=None, retries=0, agent_used=0, suspicious=0,
    )
models.insert_collect_run(
    source="tracker_applied", started_at="2026-08-29T04:00:00", duration_ms=100,
    found=0, added=0, skipped_duplicate=0, skipped_irrelevant=0, failed=1,
    ok=0, error_kind="transient", error_detail="boom", retries=0, agent_used=0, suspicious=0,
)
history = models.recent_found_counts("tracker_applied", limit=10)
assert sorted(history) == [5, 6, 8], history  # 只看成功（ok=1）的运行
print("recent_found_counts only returns found values from successful runs ok")


# ==================== 8. GET /api/collect/stats ====================

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理"

fake_resume = os.path.join(tmpdir, "base.docx")
open(fake_resume, "wb").close()
config.save_config({
    **config.DEFAULT_CONFIG,
    "base_resume_path": fake_resume,
    "tracker_xlsx_path": os.path.join(tmpdir, "tracker.xlsx"),
})

import app as flask_app

flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()

r = c.get("/api/collect/stats?days=0")
assert r.status_code == 200, r.get_json()
body = r.get_json()
assert body["days"] == 0
# 累计到这里的 collect_runs：section 2 插的 4 行（jobspy×2/tracker_saved/hyf_s1）+
# section 7 插的 4 行（tracker_applied×4，其中 1 行失败、kind=transient）。
assert {row["key"] for row in body["by_source"]} == {"jobspy", "tracker_saved", "hyf_s1", "tracker_applied"}
assert {row["key"] for row in body["by_error_kind"]} == {"transient", "auth", "structure"}
assert body["totals"]["runs"] == 8
print("GET /api/collect/stats returns by_source/by_error_kind/totals ok")

r = c.get("/api/collect/stats?days=notanumber")
assert r.status_code == 400
print("GET /api/collect/stats rejects non-integer days ok")

print("\nALL PASS")
