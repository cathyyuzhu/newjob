"""P1 面试准备冒烟测试：临时库 + 临时 config，LLM 全程 mock，不产生真实 API 费用。"""
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

FAKE = {
    "company_research": {
        "business": "测试公司做AI基础设施",
        "role_context": "这个岗位在平台产品线",
        "pain_points": ["增长放缓"],
        "talking_points": ["最近发布的Agent平台"],
    },
    "questions": [
        {
            "category": "行为面",
            "question": "讲一个你推动跨部门协作的例子",
            "why_asked": "JD强调需要横向拉通",
            "answer_points": ["情境：…", "任务：…", "行动：…", "结果：转化率+18%"],
            "resume_evidence": "简历里XX项目那段",
        }
    ],
    "gap_scripts": [
        {
            "gap": "无ITIL认证",
            "likely_question": "你有ITIL认证吗？",
            "script": "坦诚没有，但…",
            "transferable": "做过服务台流程重构",
        }
    ],
    "questions_to_ask": [{"question": "这个岗位的成功标准是什么？", "intent": "探期望"}],
    "prep_checklist": ["重读XX项目的数据"],
}

calls = []


def fake_chat(messages, provider="anthropic", model=None, system=None, max_tokens=4096):
    calls.append({"provider": provider, "model": model, "max_tokens": max_tokens, "prompt": messages[0]["content"]})
    return "```json\n" + json.dumps(FAKE, ensure_ascii=False) + "\n```"


llm.chat = fake_chat

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理，做过XX项目，转化率+18%"

import pipeline

# 追踪表读不到就返回 None，这里模拟"已经有匹配分析结论"的情况
pipeline.find_tracker_entry = lambda company, title: {
    "company_overview": "一家做AI基础设施的公司",
    "overall_match": 0.78,
    "requirement_items": [
        {"text": "5年以上B端产品经验", "is_gap": False},
        {"text": "必须持有ITIL认证", "is_gap": True},
    ],
    "skill_gap_bullets": ["缺少ITIL认证"],
    "skill_matched_bullets": ["B端产品全流程"],
}

models.init_db()

conn = models.get_conn()
job_id = models.insert_job(
    conn,
    {
        "title": "Senior Product Manager",
        "company": "TestCorp",
        "location": "Shanghai",
        "site": "linkedin",
        "job_url": "https://example.com/1",
        "jd_text": "We are looking for a senior PM. Must have ITIL certification.",
        "keyword": "product manager",
    },
)
conn.commit()
conn.close()
print("created job", job_id)

# ---- 1. 正常生成
result = pipeline.generate_interview_prep_safe(job_id)
print("generate result:", result)
assert result["questions"] == 1

prep = models.get_latest_interview_prep(job_id)
assert prep["error"] is None, prep["error"]
content = json.loads(prep["content_json"])
assert content["gap_scripts"][0]["gap"] == "无ITIL认证"
print("stored prep ok, round_label =", prep["round_label"], "provider =", prep["llm_provider"])

# ---- 2. prompt 里确实带上了已有分析结论（复用而不是重算）
p = calls[0]["prompt"]
assert "【任职要求逐条评估】" in p
assert "【未达标】必须持有ITIL认证" in p
assert "【已达标】5年以上B端产品经验" in p
assert "【公司简介】一家做AI基础设施的公司" in p
assert "【总体匹配度】78%" in p
assert calls[0]["max_tokens"] == 8192, calls[0]["max_tokens"]
print("prompt reuses existing analysis ok, max_tokens =", calls[0]["max_tokens"])

# ---- 3. 多份历史 + 轮次标签
pipeline.generate_interview_prep_safe(job_id, round_label="二面")
preps = models.list_interview_preps(job_id)
assert len(preps) == 2, len(preps)
assert preps[0]["round_label"] == "二面", preps[0]["round_label"]
assert "面试轮次：二面" in calls[-1]["prompt"]
print("multi-version + round_label ok, latest =", preps[0]["round_label"])

# ---- 4. 失败也落库，且不冲掉成功的那些
def boom(*a, **kw):
    raise RuntimeError("模拟LLM报错")


llm.chat = boom
try:
    pipeline.generate_interview_prep_safe(job_id)
    raise AssertionError("应该抛异常")
except RuntimeError as e:
    assert "模拟LLM报错" in str(e)
preps = models.list_interview_preps(job_id)
assert len(preps) == 3
assert preps[0]["error"] and preps[0]["content_json"] is None
# success_only 应该跳过失败那行，拿到上一份成功的
latest_ok = models.get_latest_interview_prep(job_id, success_only=True)
assert latest_ok["error"] is None and latest_ok["round_label"] == "二面"
print("failure recorded without clobbering success ok")

# 有失败记录也不影响"已有材料"的集合判断
assert models.job_ids_with_interview_prep() == {job_id}
llm.chat = fake_chat

# ---- 5. JD 为空的职位应该在调 LLM 之前就被挡住
conn = models.get_conn()
empty_id = models.insert_job(
    conn,
    {"title": "No JD Role", "company": "EmptyCo", "site": "indeed", "job_url": "x", "jd_text": ""},
)
conn.commit()
conn.close()
before = len(calls)
try:
    pipeline.generate_interview_prep_safe(empty_id)
    raise AssertionError("应该抛异常")
except ValueError as e:
    assert "JD" in str(e)
assert len(calls) == before, "JD为空时不应该调用LLM"
print("empty-JD guard ok (no LLM call)")

# ---- 6. 接口层：改成"面试中"自动触发，且生成失败不影响状态更新
import app as flask_app

flask_app.app.config["TESTING"] = True
client = flask_app.app.test_client()

fresh_conn = models.get_conn()
new_job_id = models.insert_job(
    fresh_conn,
    {
        "title": "Another PM",
        "company": "OtherCorp",
        "site": "linkedin",
        "job_url": "y",
        "jd_text": "Some real JD text here.",
    },
)
fresh_conn.commit()
fresh_conn.close()

r = client.post(f"/api/jobs/{new_job_id}/application_status", json={"application_status": "interviewing"})
assert r.status_code == 200, r.data
assert r.get_json()["interview_prep_started"] is True, r.get_json()
for _ in range(50):
    if models.get_latest_interview_prep(new_job_id):
        break
    time.sleep(0.1)
assert models.get_latest_interview_prep(new_job_id, success_only=True), "自动触发应生成成功"
print("auto-trigger on 面试中 ok")

# 再切一次不应该重复生成
r = client.post(f"/api/jobs/{new_job_id}/application_status", json={"application_status": "interviewing"})
assert r.get_json().get("interview_prep_started") is False, r.get_json()
print("no duplicate auto-generation ok")

# 生成路径抛异常时，状态更新照样 200
orig = flask_app._maybe_start_interview_prep
flask_app._maybe_start_interview_prep = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("炸了"))
r = client.post(f"/api/jobs/{new_job_id}/application_status", json={"application_status": "interviewing"})
assert r.status_code == 200, r.data
assert models.get_job(new_job_id)["application_status"] == "interviewing"
flask_app._maybe_start_interview_prep = orig
print("status update survives prep failure ok")

# ---- 7. GET 接口 + /api/jobs 附带字段
r = client.get(f"/api/jobs/{job_id}/interview_prep")
assert r.get_json()["id"] == preps[0]["id"]
r = client.get(f"/api/jobs/{job_id}/interview_prep?all=1")
assert len(r.get_json()) == 3
jobs = client.get("/api/jobs").get_json()
by_id = {j["id"]: j for j in jobs}
assert by_id[job_id]["has_interview_prep"] is True
assert by_id[empty_id]["has_interview_prep"] is False
assert by_id[job_id]["interview_prep_state"] is None
print("GET endpoints + /api/jobs enrichment ok")

# ---- 8. 删除
r = client.delete(f"/api/interview_preps/{preps[0]['id']}")
assert r.status_code == 200
assert len(models.list_interview_preps(job_id)) == 2
assert client.delete("/api/interview_preps/99999").status_code == 404
print("delete ok")

# ---- 9. llm.py 重构后 analyzer 行为不变
import analyzer

captured = {}


def fake_chat2(messages, provider="anthropic", model=None, system=None, max_tokens=4096):
    captured["messages"] = messages
    captured["provider"] = provider
    return '{"Acme": "foreign", "字节跳动": "domestic", "Weird": "nope"}'


llm.chat = fake_chat2
out = analyzer.classify_companies(["Acme", "字节跳动", "Weird"], provider="deepseek", model="deepseek-v4-pro")
assert out == {"Acme": "foreign", "字节跳动": "domestic", "Weird": "unknown"}, out
assert captured["provider"] == "deepseek"
try:
    llm.chat = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x"))
    analyzer.classify_companies(["A"], provider="bogus")
except RuntimeError:
    pass
# 未知 provider 的报错文案保留在 llm.chat 里
llm.chat = llm.__dict__["chat"]
import importlib

importlib.reload(llm)
try:
    llm.chat([{"role": "user", "content": "x"}], provider="bogus")
    raise AssertionError("应该抛异常")
except RuntimeError as e:
    assert "未知的 llm_provider：bogus" in str(e), str(e)
print("analyzer behavior preserved after llm.py refactor ok")

print("\nALL PASS")
