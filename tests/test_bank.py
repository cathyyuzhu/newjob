"""P2 通用题库测试：临时库 + 临时 config，LLM 全程 mock，不产生真实 API 费用。"""
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

DRAFT_V1 = {
    "self_intro": {"zh": "我是Cathy，产品经理…", "en": "I'm Cathy, a PM…"},
    "items": [
        {"question": "为什么离开上一家？", "answer": "初稿答案A"},
        {"question": "职业规划是什么？", "answer": "初稿答案B"},
    ],
    "star_stories": [{"question": "讲一个从0到1的例子", "answer": "STAR 初稿"}],
}
# 第二次起草：同样的题给了不同答案，外加一道新题
DRAFT_V2 = {
    "self_intro": {"zh": "第二版自我介绍", "en": "Second version"},
    "items": [
        {"question": "为什么离开上一家？", "answer": "初稿答案A-改"},
        {"question": "职业规划是什么？", "answer": "初稿答案B-改"},
        {"question": "你最大的短板是什么？", "answer": "新题答案"},
    ],
    "star_stories": [{"question": "讲一个从0到1的例子", "answer": "STAR 初稿-改"}],
}

current_draft = DRAFT_V1
calls = []


def fake_chat(messages, provider="anthropic", model=None, system=None, max_tokens=4096):
    calls.append({"provider": provider, "model": model, "max_tokens": max_tokens, "prompt": messages[0]["content"]})
    return json.dumps(current_draft, ensure_ascii=False)


llm.chat = fake_chat

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理，做过XX项目"

import pipeline

models.init_db()

# ---- 1. 首次起草
import config as cfg_mod

cfg = cfg_mod.load_config()
cfg["keywords"] = ["AI产品经理", "Senior Product Manager"]
cfg_mod.save_config(cfg)

stats = pipeline.generate_bank_draft()
assert stats == {"updated": 0, "added": 4, "skipped": 0}, stats
items = models.list_bank_items()
assert len(items) == 4
# 排序：self_intro 在最前，然后 common，最后 star_story
assert [i["category"] for i in items] == ["self_intro", "common", "common", "star_story"], [i["category"] for i in items]
intro = items[0]
assert intro["answer"] == "我是Cathy，产品经理…" and intro["answer_en"] == "I'm Cathy, a PM…"
assert all(i["user_edited"] == 0 for i in items)
print("first draft ok:", stats)

# prompt 里带上了搜索关键词作为目标岗位
assert "AI产品经理、Senior Product Manager" in calls[0]["prompt"]
assert calls[0]["max_tokens"] == 8192
print("prompt uses config keywords ok")

# ---- 2. 用户手改两条
leave = next(i for i in items if i["question"] == "为什么离开上一家？")
models.update_bank_item(leave["id"], answer="这是我自己写的答案，不许覆盖")
models.update_bank_item(intro["id"], answer="我自己的自我介绍", answer_en="My own intro")
after = {i["id"]: i for i in models.list_bank_items()}
assert after[leave["id"]]["user_edited"] == 1
assert after[intro["id"]]["answer_en"] == "My own intro"
print("manual edit marks user_edited ok")

# ---- 3. 再次起草：改过的不能被覆盖，没改过的更新，新题新增
current_draft = DRAFT_V2
stats = pipeline.generate_bank_draft()
assert stats == {"updated": 2, "added": 1, "skipped": 2}, stats

after = {i["question"]: i for i in models.list_bank_items()}
# 改过的两条原样保留
assert after["为什么离开上一家？"]["answer"] == "这是我自己写的答案，不许覆盖"
assert after["自我介绍（60-90秒）"]["answer"] == "我自己的自我介绍"
assert after["自我介绍（60-90秒）"]["answer_en"] == "My own intro"
# 没改过的两条被更新
assert after["职业规划是什么？"]["answer"] == "初稿答案B-改"
assert after["讲一个从0到1的例子"]["answer"] == "STAR 初稿-改"
# 新题被加进来
assert after["你最大的短板是什么？"]["answer"] == "新题答案"
assert len(after) == 5
print("re-draft protects user edits ok:", stats)

# ---- 4. 手动加的题默认就受保护，不会被之后的起草覆盖
manual_id = models.add_bank_item("common", "手动加的题", answer="我的答案")
assert models.list_bank_items()[-1] or True
manual = next(i for i in models.list_bank_items() if i["id"] == manual_id)
assert manual["user_edited"] == 1, "手动加的题应该默认 user_edited=1"
current_draft = {"self_intro": {}, "items": [{"question": "手动加的题", "answer": "AI想覆盖"}], "star_stories": []}
stats = pipeline.generate_bank_draft()
assert stats["skipped"] == 1 and stats["updated"] == 0, stats
assert next(i for i in models.list_bank_items() if i["id"] == manual_id)["answer"] == "我的答案"
print("manually added items are protected ok")

# ---- 5. 起草不会删除已有条目（AI 这轮没生成到的题保留）
assert len(models.list_bank_items()) == 6
print("re-draft never deletes ok")

# ---- 6. 接口层
import app as flask_app

flask_app.app.config["TESTING"] = True
client = flask_app.app.test_client()

r = client.get("/api/interview/bank")
data = r.get_json()
assert len(data["items"]) == 6 and data["generating"] is False

# 新增 / 校验
assert client.post("/api/interview/bank", json={"category": "bogus", "question": "x"}).status_code == 400
assert client.post("/api/interview/bank", json={"category": "common", "question": "  "}).status_code == 400
r = client.post("/api/interview/bank", json={"category": "common", "question": "接口加的题"})
new_id = r.get_json()["id"]

# 编辑
r = client.put(f"/api/interview/bank/{new_id}", json={"answer": "接口写的答案"})
assert r.status_code == 200
assert next(i for i in models.list_bank_items() if i["id"] == new_id)["answer"] == "接口写的答案"
assert client.put("/api/interview/bank/99999", json={"answer": "x"}).status_code == 404

# 删除
assert client.delete(f"/api/interview/bank/{new_id}").status_code == 200
assert client.delete("/api/interview/bank/99999").status_code == 404
assert len(models.list_bank_items()) == 6
print("bank CRUD endpoints ok")

# ---- 7. 起草接口：后台跑 + 并发保护
current_draft = DRAFT_V1
import job_state

slow_done = {"v": False}
orig_chat = llm.chat


def slow_chat(*a, **kw):
    time.sleep(0.6)
    slow_done["v"] = True
    return json.dumps(current_draft, ensure_ascii=False)


llm.chat = slow_chat
r = client.post("/api/interview/bank/generate")
assert r.status_code == 200 and r.get_json()["started"] is True
# 正在跑的时候再点一次应该 409
r2 = client.post("/api/interview/bank/generate")
assert r2.status_code == 409, r2.status_code
assert client.get("/api/interview/bank").get_json()["generating"] is True
for _ in range(50):
    if not job_state.bank_generating():
        break
    time.sleep(0.1)
assert slow_done["v"] and not job_state.bank_generating()
assert client.get("/api/interview/bank").get_json()["generating"] is False
print("generate endpoint: background + 409 guard + state cleared ok")

# ---- 8. 起草失败也要把 generating 标志清掉，否则按钮永远灰着
llm.chat = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟LLM报错"))
r = client.post("/api/interview/bank/generate")
assert r.status_code == 200
for _ in range(50):
    if not job_state.bank_generating():
        break
    time.sleep(0.1)
assert not job_state.bank_generating(), "生成失败后 generating 标志必须清掉"
print("generating flag cleared on failure ok")
llm.chat = orig_chat

# ---- 9. 前端接线
ivjs = open(os.path.join(BASE, "static", "interview.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "templates", "index.html"), encoding="utf-8").read()
css = open(os.path.join(BASE, "static", "style.css"), encoding="utf-8").read()
assert 'id="interviewModalOverlay"' in html and 'id="interviewModalBody"' in html
assert 'onclick="openInterviewModal()"' in html
import re

defined = set(re.findall(r"function\s+(\w+)", ivjs))
for fn in ["openInterviewModal", "closeInterviewModal", "loadBank", "renderBank", "generateBankDraft",
           "saveBankItem", "addBankItem", "removeBankItem", "startBankPoll", "stopBankPoll"]:
    assert fn in defined, f"缺少 {fn}"
inline = set(re.findall(r'onclick="[^"]*?(\w+)\(', ivjs))
appjs = open(os.path.join(BASE, "static", "app.js"), encoding="utf-8").read()
all_defined = defined | set(re.findall(r"function\s+(\w+)", appjs))
missing = {f for f in inline if f not in all_defined and f != "stopPropagation"}
assert not missing, f"内联 onclick 引用了不存在的函数：{missing}"
for cls in [".modal-wide", ".bank-count", ".bank-badge", ".bank-answer", ".bank-actions", ".bank-label"]:
    assert cls in css, f"缺少样式 {cls}"
# 用户输入必须转义（题库答案会原样回填到 textarea 里）
assert "${item.answer}" not in ivjs and "${item.question}" not in ivjs
print("frontend wiring ok")

r = client.get("/")
assert r.status_code == 200 and "面试题库" in r.data.decode("utf-8")
print("page renders ok")

print("\nALL PASS")
