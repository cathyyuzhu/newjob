"""调度器加固的回归测试（见 spec/roadmap.md「职位收集链路的错误处理生产级加固」
缺口6/⑧）：进程启动时的错过检测（_maybe_catch_up）+ reschedule() 真的把
misfire_grace_time/coalesce/max_instances 传给了 APScheduler。

不测每日定时任务本身四个子步骤的隔离逻辑（scraper 搜索/how-you-fit同步/自动分析/
公司分类互不阻断）——那部分只是普通函数顺序调用+各自 try/except，读代码就能确认，
真起 APScheduler 等实际触发一次没有额外信号量。
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH
models.init_db()

import scheduler

run_job_calls = []
scheduler._run_job = lambda: run_job_calls.append(1)

_db_counter = [0]


def _fresh_db():
    """每个场景各用一个全新的空库——_maybe_catch_up 只看"最近一次成功记录"，同一个
    库里插入一条更早的历史记录不会改变"最近一次"，场景之间必须互相隔离，不能在同
    一条时间线上累加。"""
    _db_counter[0] += 1
    models.DB_PATH = os.path.join(tmpdir, f"scenario{_db_counter[0]}.db")
    models.init_db()


def _insert(hours_ago, ok, **overrides):
    fields = dict(
        source="jobspy", started_at=(datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
        duration_ms=100, found=5, added=1, skipped_duplicate=0, skipped_irrelevant=0, failed=0,
        ok=ok, error_kind=None, error_detail=None, retries=0, agent_used=0, suspicious=0,
    )
    fields.update(overrides)
    models.insert_collect_run(**fields)


# ---- 1. 没有任何历史成功记录：不该补跑（全新安装，不算"错过"） ----
_fresh_db()
scheduler._maybe_catch_up({"schedule_enabled": True})
time.sleep(0.05)
assert run_job_calls == [], "从没成功跑过不该被判定成错过，不该补跑"
print("_maybe_catch_up does nothing when there is no successful history ok")


# ---- 2. 定时任务被用户关掉：不检查、不补跑（即使历史记录早就过期） ----
_fresh_db()
_insert(hours_ago=48, ok=1)
scheduler._maybe_catch_up({"schedule_enabled": False})
time.sleep(0.05)
assert run_job_calls == [], "用户关掉每日定时任务时不该补跑"
print("_maybe_catch_up skips the check entirely when schedule_enabled is False ok")


# ---- 3. 距上次成功还在阈值内：不补跑 ----
_fresh_db()
_insert(hours_ago=2, ok=1)
scheduler._maybe_catch_up({"schedule_enabled": True})
time.sleep(0.05)
assert run_job_calls == [], "最近成功跑过时不该补跑"
print("_maybe_catch_up does nothing when the last success is within the threshold ok")


# ---- 4. 距上次成功超过阈值（比如笔记本合盖休眠错过了昨天的触发）：立刻补跑一次 ----
_fresh_db()
_insert(hours_ago=scheduler.CATCHUP_THRESHOLD_HOURS + 1, ok=1)
scheduler._maybe_catch_up({"schedule_enabled": True})
deadline = time.time() + 2
while not run_job_calls and time.time() < deadline:
    time.sleep(0.01)
assert run_job_calls == [1], "距上次成功超过阈值应该立刻在后台补跑一次"
print("_maybe_catch_up triggers a background run when the last success is stale ok")


# ---- 5. 失败的运行不算"成功过"——只看 ok=1 的记录 ----
run_job_calls.clear()
_fresh_db()
_insert(hours_ago=0, ok=0, found=0, added=0, failed=1, error_kind="transient", error_detail="boom")
scheduler._maybe_catch_up({"schedule_enabled": True})
time.sleep(0.05)
assert run_job_calls == [], "只有失败记录、从没成功过时应该等同于'从没跑过'，不算错过"
print("_maybe_catch_up treats a history of only failed runs as never-succeeded ok")


# ---- 6. reschedule() 真的把 misfire_grace_time/coalesce/max_instances 传给了 APScheduler ----
scheduler.reschedule(9, 0, enabled=True)
job = scheduler._scheduler.get_job(scheduler._job_id)
assert job is not None
assert job.misfire_grace_time == 3 * 3600, job.misfire_grace_time
assert job.coalesce is True
assert job.max_instances == 1
print("reschedule() configures misfire_grace_time/coalesce/max_instances ok")

scheduler.reschedule(9, 0, enabled=False)
assert scheduler._scheduler.get_job(scheduler._job_id) is None
print("reschedule() removes the job when disabled ok")

print("\nALL PASS")
