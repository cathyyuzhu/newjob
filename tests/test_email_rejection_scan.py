"""邮件拒信扫描的本地落库半段冒烟测试（见 email_rejection_scan.py 顶部说明）。

Gmail 搜索/读信/判断"是不是拒信"由 Claude Code 在对话里用 Gmail MCP 连接器完成，
不在这个项目的代码里、也没法在自动化测试里跑真实网络请求。这里只锁住本地这一半：
1. list_applied_jobs() 只返回 application_status='applied' 的职位，其它状态不混进来
2. apply 子命令能把确认列表批量落库：改状态为 rejected + 记一条 source='email_scan' 的备注
3. queue 子命令（本机计划任务无人值守扫描用）把疑似拒信排进 pending_rejections，
   不直接改状态；网页端确认/忽略分别对应 models.list_pending_rejections()/
   remove_pending_rejection()（真正的落库路径复用 app.py 里跟 apply 一样的调用）
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH
models.init_db()

import email_rejection_scan


def _add(company, title, application_status="not_applied"):
    conn = models.get_conn()
    job_id = models.insert_job(conn, {"company": company, "title": title})
    conn.commit()
    conn.close()
    if application_status != "not_applied":
        models.set_application_status(job_id, application_status)
    return job_id


# ---- 1. list_applied_jobs() 只挑出 application_status='applied' 的职位 ----
applied_id = _add("Amazon", "AI Product Manager", "applied")
_add("Google", "Senior PM", "not_applied")
rejected_id = _add("Meta", "PM", "rejected")

applied = models.list_applied_jobs()
applied_ids = {j["id"] for j in applied}
assert applied_id in applied_ids
assert rejected_id not in applied_ids
assert all(j["company"] != "Google" for j in applied)
print("list_applied_jobs() 只返回已投递职位 ok")


# ---- 2. apply 子命令：批量改状态 + 记备注 ----
confirm_file = os.path.join(tmpdir, "confirmed.json")
with open(confirm_file, "w", encoding="utf-8") as f:
    json.dump([{"job_id": applied_id, "note": "Gmail来信主题《Update on your application》：感谢申请，决定推进其他候选人"}], f)

email_rejection_scan.cmd_apply(type("Args", (), {"file": confirm_file})())

job = models.get_job(applied_id)
assert job["application_status"] == "rejected"
notes = models.list_job_notes(applied_id)
assert len(notes) == 1
assert notes[0]["source"] == "email_scan"
assert "Update on your application" in notes[0]["content"]
print("apply 子命令：状态改为 rejected + 备注留痕 ok")


# ---- 3. record-run 子命令 + last_email_scan_run()：记一次扫描时间戳 ----
assert models.last_email_scan_run() is None
email_rejection_scan.cmd_record_run(type("Args", (), {"checked": 18, "rejections": 0})())
last_run = models.last_email_scan_run()
assert last_run is not None
assert last_run["jobs_checked"] == 18
assert last_run["rejections_found"] == 0
print("record-run 子命令：写入扫描记录 ok")


# ---- 4. queue 子命令：排进待确认队列，不直接改状态 ----
queued_id = _add("Netflix", "PM", "applied")
candidates_file = os.path.join(tmpdir, "candidates.json")
with open(candidates_file, "w", encoding="utf-8") as f:
    json.dump([{"job_id": queued_id, "note": "Gmail来信主题《Thank you for your interest》：暂不推进"}], f)

email_rejection_scan.cmd_queue(type("Args", (), {"file": candidates_file})())

job = models.get_job(queued_id)
assert job["application_status"] == "applied"  # queue 不改状态，只有 apply/网页确认才改
pending = models.list_pending_rejections()
pending_for_job = [p for p in pending if p["job_id"] == queued_id]
assert len(pending_for_job) == 1
assert "Thank you for your interest" in pending_for_job[0]["note"]
assert pending_for_job[0]["company"] == "Netflix"

models.remove_pending_rejection(pending_for_job[0]["id"])
assert not [p for p in models.list_pending_rejections() if p["job_id"] == queued_id]
print("queue 子命令：排队不改状态 + 确认/忽略后从队列移除 ok")


print("ALL PASS")
