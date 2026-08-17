"""标记「忽略」中断分析的冒烟测试：正在跑的那一条结果要被丢弃，但批次里其它职位要
继续正常跑完，而不是像顶部"停止分析"按钮那样让整批都停下来（见 job_state.discard_job
的说明，这是它跟 request_stop() 唯一的区别）。

临时库 + 临时 config + 临时简历目录，LLM 全程 mock（用 sleep 模拟"正在跑"的窗口），
不产生真实 API 费用。
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

FAKE_ANALYSIS = {
    "company_overview": "一家测试公司",
    "job_content_bullets": ["做产品"],
    "requirement_items": [{"text": "5年经验", "is_gap": False}],
    "skill_matched_bullets": ["产品全流程"],
    "skill_gap_bullets": [],
    "experience_years": "5年+",
    "industry_bullets": ["互联网"],
    "salary": "JD未公开，需进一步询问",
    "team_bullets": ["10人团队"],
    "location": "北京",
    "company_origin": "domestic",
    "cognitive_match": 0.8,
    "content_match": 0.8,
}

call_log = []


def slow_chat(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    call_log.append(messages[0]["content"][:80])
    # 第一次调用（对应第一条职位）睡一下，模拟"正在跑"，好让主线程有机会在这期间
    # 把这条职位标记忽略；后面几条职位不睡，批次能很快跑完。
    if len(call_log) == 1:
        time.sleep(0.3)
    return json.dumps(FAKE_ANALYSIS, ensure_ascii=False)


llm.chat = slow_chat

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理"

fake_resume = os.path.join(tmpdir, "base.docx")
open(fake_resume, "wb").close()
config.save_config({**config.DEFAULT_CONFIG, "base_resume_path": fake_resume})

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


job_analyzing_id = insert_job("Product Manager A", "https://example.com/a")
job_queued_id = insert_job("Product Manager B", "https://example.com/b")
job_normal_id = insert_job("Product Manager C", "https://example.com/c")

# ---- 1. 起批：三条一起排队分析
r = c.post("/api/jobs/analyze_all")
assert r.status_code == 200 and r.get_json()["count"] == 3, r.get_json()

# 等第一条真正轮到（进了 slow_chat 的 sleep 窗口），确保接下来的"忽略"命中的是
# "正在分析中"这条，而不是还没轮到的
for _ in range(200):
    if job_state.get_states().get(job_analyzing_id) == "analyzing":
        break
    time.sleep(0.01)
else:
    raise AssertionError("job_analyzing_id 一直没进入 analyzing 状态")

# 这条正在跑的标记忽略——应该触发 job_state.discard_job()
r = c.post(f"/api/jobs/{job_analyzing_id}/status", json={"status": "dismissed"})
assert r.status_code == 200
# 排队中还没轮到的那条也标记忽略——走的是循环内"轮到时发现已忽略就跳过"的老路径
r = c.post(f"/api/jobs/{job_queued_id}/status", json={"status": "dismissed"})
assert r.status_code == 200

# 等整个批次跑完
for _ in range(200):
    if not job_state.in_progress_ids():
        break
    time.sleep(0.02)
else:
    raise AssertionError("批次一直没跑完")
print("batch finished ok")

# ---- 2. 断言：被忽略的这条结果没有保留，另一条正常职位照常跑完（队列没有被整体停掉）
job_analyzing = models.get_job(job_analyzing_id)
job_queued = models.get_job(job_queued_id)
job_normal = models.get_job(job_normal_id)

assert job_analyzing["overall_match"] is None, "正在分析时被忽略，结果不该写进库"
assert job_analyzing["analysis_error"] is None, "被丢弃不是失败，不该留下分析失败的错误"
assert job_queued["overall_match"] is None, "还没轮到就被忽略，应该被跳过、不参与分析"
assert job_normal["overall_match"] is not None, "没被忽略的这条应该正常分析完成——批次没有被整体中断"
print("dismiss discards only the affected job, batch continues ok")

# ---- 3. 丢弃标记不能残留：批次结束后再手动对这条职位点一次"AI 分析"，
#         不该被误判成"还要丢弃"（见 analyze_and_record_safe 开头新增的 clear_discard）
r = c.post(f"/api/jobs/{job_analyzing_id}/analyze")
assert r.status_code == 200, r.get_json()
assert not r.get_json().get("discarded"), "丢弃标记如果没清干净，这次重新分析会被误丢弃"
assert r.get_json()["overall_match"] is not None
assert models.get_job(job_analyzing_id)["overall_match"] is not None
print("discard flag cleared after batch, manual re-analyze works ok")

# ---- 4. 反悔：把状态从"忽略"改回"新"要清掉丢弃标记（对称性检查，不依赖具体丢弃状态，
#         只验证接口不报错、状态改成功）
r = c.post(f"/api/jobs/{job_queued_id}/status", json={"status": "new"})
assert r.status_code == 200
assert models.get_job(job_queued_id)["status"] == "new"
print("undo dismiss ok")

print("\nALL PASS")
