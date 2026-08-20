"""忽略原因收集 → 偏好档案冒烟测试（P0-3，2026-08-18）：预设标签+自由文本落库、
攒够阈值后台自动总结、手动强制重新生成、偏好档案被真的塞进匹配分析的 prompt 里。

临时库 + 临时 config，LLM 全程 mock，不产生真实 API 费用。
"""
import json
import os
import sys
import tempfile
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

FAKE_SUMMARY = "反复因为薪资不符和层级不匹配忽略职位，看起来倾向更高职级、薪资更有竞争力的机会。"


def fake_chat_profile(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    return json.dumps({"summary": FAKE_SUMMARY}, ensure_ascii=False)


llm.chat = fake_chat_profile

# pipeline.py 在模块加载时用 `from resume_docx import read_resume_text` 把函数绑到自己
# 命名空间里，之后再改 resume_docx.read_resume_text 对 pipeline 已经不起作用了——
# 必须在第一次 import pipeline 之前就把假的实现装上（跟 test_dismiss_abort.py 同一个坑）。
import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理"

import job_state
import pipeline
import app as flask_app

models.init_db()
flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()


def insert_job(title, url):
    conn = models.get_conn()
    job_id = models.insert_job(conn, {
        "title": title,
        "company": "TestCo",
        "location": "Beijing",
        "site": "linkedin",
        "job_url": url,
        "date_posted": "2026-08-01",
        "keyword": title,
        "jd_text": f"We need a {title} who ships.",
    })
    conn.commit()
    conn.close()
    return job_id


job_ids = [insert_job(f"Job {i}", f"https://example.com/{i}") for i in range(6)]

# ---- 1. 输入校验：非法标签、原因全空
r = c.post(f"/api/jobs/{job_ids[0]}/dismiss_reason", json={"tags": ["瞎编的标签"]})
assert r.status_code == 400, r.get_json()
r = c.post(f"/api/jobs/{job_ids[0]}/dismiss_reason", json={"tags": [], "note": ""})
assert r.status_code == 400, r.get_json()
r = c.post("/api/jobs/999999/dismiss_reason", json={"tags": ["薪资不符"]})
assert r.status_code == 404
print("dismiss_reason validation ok")

# ---- 2. 记 4 条原因，不该攒够阈值（PREFERENCE_PROFILE_THRESHOLD=5），不触发生成
for i in range(4):
    r = c.post(f"/api/jobs/{job_ids[i]}/dismiss_reason", json={"tags": ["薪资不符"], "note": f"太低了 {i}"})
    assert r.status_code == 200, r.get_json()
time.sleep(0.2)  # 后台线程即使触发了也会在这段时间内跑完/跑到检查阈值那一步
assert models.count_dismiss_reasons() == 4
assert models.get_latest_preference_profile() is None, "还没攒够阈值，不该生成"
print("below threshold: no generation")

# ---- 3. has_dismiss_reason 反映在 /api/jobs 里
jobs = c.get("/api/jobs").get_json()
by_id = {j["id"]: j for j in jobs}
assert by_id[job_ids[0]]["has_dismiss_reason"] is True
assert by_id[job_ids[5]]["has_dismiss_reason"] is False
assert models.latest_dismiss_reason_for_job(job_ids[0])["note"] == "太低了 0"
print("has_dismiss_reason flag ok")

# ---- 4. 第 5 条原因攒够阈值，后台自动生成一份偏好档案
r = c.post(f"/api/jobs/{job_ids[4]}/dismiss_reason", json={"tags": ["层级不匹配"], "note": ""})
assert r.status_code == 200
for _ in range(200):
    profile = models.get_latest_preference_profile()
    if profile and not job_state.profile_generating():
        break
    time.sleep(0.02)
else:
    raise AssertionError("偏好档案一直没生成出来")
assert profile["content_text"] == FAKE_SUMMARY, profile
assert profile["source_reason_count"] == 5, profile
assert profile["error"] is None
print("threshold reached: auto-generated ok")

# ---- 5. GET /api/preferences 能读到同一份
row = c.get("/api/preferences").get_json()
assert row["content_text"] == FAKE_SUMMARY, row

# ---- 6. 再记一条（第6条，还没到下一个5的倍数），不该再自动触发
r = c.post(f"/api/jobs/{job_ids[5]}/dismiss_reason", json={"tags": ["地点"], "note": ""})
assert r.status_code == 200
time.sleep(0.2)
assert models.get_latest_preference_profile()["source_reason_count"] == 5, "不该在还没攒够新一批之前又生成一次"
print("no premature regeneration ok")

# ---- 7. 手动强制重新生成：跳过阈值检查
r = c.post("/api/preferences/regenerate")
assert r.status_code == 200 and r.get_json()["started"] is True
for _ in range(200):
    if not job_state.profile_generating():
        latest = models.get_latest_preference_profile()
        if latest["source_reason_count"] == 6:
            break
    time.sleep(0.02)
else:
    raise AssertionError("强制重新生成一直没跑完")
print("force regenerate ok")

# ---- 8. 生成失败也要落一行（不是静默吞掉），跟 resume_review/interview_prep 同一个规矩
def fake_chat_broken(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    return "不是JSON"


llm.chat = fake_chat_broken
pipeline.maybe_refresh_preference_profile(force=True)
failed = models.get_latest_preference_profile()
assert failed["error"], "生成失败应该落一行 error，而不是什么都不留"
print("failure recorded ok")

llm.chat = fake_chat_profile

# ---- 9. analyzer.analyze_job()：传了偏好档案文本要出现在 prompt 里，不传就不出现
import analyzer

captured = {}


def capture_chat(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    captured["prompt"] = messages[0]["content"]
    return json.dumps({
        "company_overview": "x", "job_content_bullets": [], "requirement_items": [],
        "skill_matched_bullets": [], "skill_gap_bullets": [], "experience_years": "",
        "industry_bullets": [], "salary": "", "team_bullets": [], "location": "",
        "company_origin": "unknown", "cognitive_match": 0.7, "content_match": 0.7,
    })


llm.chat = capture_chat
analyzer.analyze_job("Co", "PM", "JD text", "[0] resume", preference_profile_text="讨厌频繁出差")
assert "## 用户偏好档案" in captured["prompt"] and "讨厌频繁出差" in captured["prompt"], captured["prompt"]

captured.clear()
analyzer.analyze_job("Co", "PM", "JD text", "[0] resume")
# 任务3的评分规则里固定提到"用户偏好档案"这个词（说明"如果提供了就怎么用"），
# 那句话不受影响；真正判断"这次有没有注入"要看区块本身的标题（## 开头）出不出现。
assert "## 用户偏好档案" not in captured["prompt"], "没传档案就不该出现这个区块"
print("analyzer prompt injection ok")

# ---- 10. pipeline.analyze_and_record() 真的会把最新档案喂进去
fake_resume = os.path.join(tmpdir, "base.docx")
open(fake_resume, "wb").close()
config.save_config({**config.DEFAULT_CONFIG, "base_resume_path": fake_resume})

pipeline.analyze_and_record(job_ids[5])
assert "## 用户偏好档案" in captured["prompt"] and FAKE_SUMMARY in captured["prompt"], \
    "pipeline 应该把最新的偏好档案传给 analyzer"
print("pipeline wiring ok")

print("\nALL PASS")
