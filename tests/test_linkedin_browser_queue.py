"""LinkedIn 登录态浏览器排队的冒烟测试（见 job_state.py「LinkedIn 登录态浏览器排队」、
routes_search.py 里 sync_tracker_route/sync_how_you_fit_route/sync_how_you_fit_all_route
的说明）。

起因：tracker 同步（收藏/已投递/面试）、How You Fit 单条/批量同步共用同一个持久化登录
profile，同一时刻只能开一个真实浏览器。2026-09-08 之前不同类型的同步前后脚点，完全靠
Chromium 对 profile 目录的独占锁"谁后 launch 谁失败"，用户体验是"莫名其妙报错、通知
还说成功"。改成显式排队后，这里覆盖：
1. job_state 层的 acquire/release 原语本身：占用/排队/释放后自动交接给队首。
2. 通过 Flask 路由端到端验证：占用中的一方触发另一方时，返回 queued=True 且带
   queued_behind；占用方跑完后，排队的一方不需要用户重新点击就自动开始并完成。
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

import job_state
import linkedin_tracker
import linkedin_how_you_fit as h
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


# ==================== 1. job_state 层的 acquire/release 原语 ====================

acquired, occupant = job_state.acquire_linkedin_browser("A", lambda: None)
assert acquired is True and occupant is None, (acquired, occupant)

acquired2, occupant2 = job_state.acquire_linkedin_browser("B", lambda: None)
assert acquired2 is False and occupant2 == "A", (acquired2, occupant2)

started = threading.Event()


def _start_c():
    started.set()


acquired3, occupant3 = job_state.acquire_linkedin_browser("C", _start_c)
assert acquired3 is False and occupant3 == "A", "A 仍然占用中，B 排在 C 前面"

job_state.release_linkedin_browser()  # A 用完，应该自动把使用权交给排在最前面的 B（不是 C）
assert not started.is_set(), "队首是 B 不是 C，C 的 start_fn 不应该被调用"

job_state.release_linkedin_browser()  # B 用完，轮到 C
started.wait(timeout=2)
assert started.is_set(), "B 用完后应该自动把使用权交给排队里的 C，不需要重新调用 acquire"

job_state.release_linkedin_browser()  # 队列空了，占用标记清空，下一次 acquire 应该立刻成功
acquired4, occupant4 = job_state.acquire_linkedin_browser("D", lambda: None)
assert acquired4 is True and occupant4 is None, (acquired4, occupant4)
job_state.release_linkedin_browser()
print("acquire_linkedin_browser/release_linkedin_browser: FIFO 排队 + 自动交接 ok")


# ==================== 2. 端到端：不同类型的同步撞车时排队，占用方跑完自动接力 ====================

tracker_gate = threading.Event()
tracker_finish_gate = threading.Event()


def fake_sync_tracker_stage(stage):
    tracker_gate.set()  # 告诉主测试线程：tracker 同步已经真的拿到浏览器、开始跑了
    tracker_finish_gate.wait(timeout=5)
    return {"results": [], "added_ids": [], "total_found": 0}


linkedin_tracker.sync_tracker_stage = fake_sync_tracker_stage

hyf_batch_started = threading.Event()


def fake_sync_all_enabled_searches(delay_seconds=None):
    hyf_batch_started.set()
    return {}


h.sync_all_enabled_searches = fake_sync_all_enabled_searches
set_searches([{"id": "s1", "name": "远程产品经理", "url": TEST_URL, "enabled": True}])

# 先点「同步收藏」，卡在 fake_sync_tracker_stage 里不放，模拟它正占着 LinkedIn 浏览器。
r1 = c.post("/api/jobs/sync_tracker/saved")
assert r1.status_code == 200 and r1.get_json() == {"started": True}, r1.get_json()
tracker_gate.wait(timeout=5)
assert tracker_gate.is_set(), "tracker 同步应该已经真的开始跑（拿到了浏览器）"

# 这时候再点「同步全部搜索」：不应该真的去 launch 浏览器（fake_sync_all_enabled_searches
# 不会被调用），而是排队，响应里明确告诉调用方"正被谁占用"。
r2 = c.post("/api/jobs/sync_how_you_fit_all")
assert r2.status_code == 200, r2.get_json()
body2 = r2.get_json()
assert body2["started"] is True and body2.get("queued") is True, body2
assert body2.get("queued_behind") == "同步 LinkedIn 收藏列表", body2
assert not hyf_batch_started.is_set(), "占用中的时候不应该真的起线程去跑批量同步"

# 轮询批量同步状态也应该能看到"正在排队"，而不是一个查不出原因的"syncing: true"。
status = c.get("/api/jobs/sync_how_you_fit_all").get_json()
assert status["syncing"] is True and status["queued_behind"] == "同步 LinkedIn 收藏列表", status

# 放行 tracker 同步，让它跑完——完成后应该自动把浏览器使用权交给排队的批量同步，
# 不需要用户重新点一次「同步全部搜索」。
tracker_finish_gate.set()
for _ in range(300):
    if hyf_batch_started.is_set():
        break
    time_module.sleep(0.02)
assert hyf_batch_started.is_set(), "tracker 同步跑完后，排队的批量同步应该自动开始，不需要用户重新点击"

for _ in range(300):
    if not job_state.how_you_fit_batch_syncing():
        break
    time_module.sleep(0.02)
final_status = c.get("/api/jobs/sync_how_you_fit_all").get_json()
assert final_status["syncing"] is False and final_status["queued_behind"] is None, final_status
print("同时点「同步收藏」和「同步全部搜索」：后一个排队等前一个用完自动开始 ok")

print()
print("ALL PASS")
