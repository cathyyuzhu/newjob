"""题库「跟 AI 对话完善答案」测试：临时库 + 临时 config，LLM 全程 mock。

最重要的一条性质在第 4 节：**对话本身绝不写库**。AI 给的改写版只是候选，用户点「采用」
把它填进输入框、再点「保存」才落库——聊崩了也毁不掉已经写好的答案。
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

import llm

calls = []
reply_payload = {"reply": "把背景压成一句，数据提前。", "answer": "改好的第一段\n\n改好的第二段"}
raise_error = None


def fake_chat(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    calls.append({"messages": messages, "system": system, "max_tokens": max_tokens})
    if raise_error:
        raise RuntimeError(raise_error)
    return json.dumps(reply_payload, ensure_ascii=False)


llm.chat = fake_chat

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理，做过XX项目"

# 题库对话读简历，而"简历"现在是用户上传的文件（不再回退到某个硬编码路径），
# 所以得先造一份出来。内容无所谓——上面已经把 read_resume_text 换掉了。
import config as _cfg

_fake_resume = os.path.join(tmpdir, "base.docx")
open(_fake_resume, "wb").close()
_cfg.save_config({**_cfg.DEFAULT_CONFIG, "base_resume_path": _fake_resume})

import interview
import pipeline  # noqa: F401  （让 app.py 里的 pipeline 引用拿到同一个已 patch 的 llm）
import app as flask_app

models.init_db()
flask_app.app.config["TESTING"] = True
client = flask_app.app.test_client()

item_id = models.add_bank_item(
    "common", "为什么离开上一家？", answer="原来的中文答案", answer_en="Original English answer"
)
story_id = models.add_bank_item("star_story", "讲一个从0到1的例子", answer="情境：…")

# ---- 1. 每题对话：正常一轮
calls.clear()
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "太长了，压到 60 秒"})
assert r.status_code == 200, r.get_json()
data = r.get_json()
assert data["reply"] == reply_payload["reply"]
assert data["answer"] == reply_payload["answer"]
assert "\n\n" in data["answer"], "改写版的分段不能在传输过程中丢掉"

system = calls[0]["system"]
assert "为什么离开上一家？" in system, "system prompt 里要带上题目原文"
assert "原来的中文答案" in system, "system prompt 里要带上这一版的当前答案"
assert "中文版" in system, "system prompt 里要说清这轮改的是哪一版"
assert "Original English answer" not in system, "改中文版时不该把英文版也塞进去"
assert calls[0]["messages"] == [{"role": "user", "content": "太长了，压到 60 秒"}]
print("item chat ok:", data["reply"])

# ---- 1b. 改英文版时，喂进去的当前答案换成英文那一版
calls.clear()
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "en", "message": "make it shorter"})
assert r.status_code == 200
system = calls[0]["system"]
assert "Original English answer" in system and "原来的中文答案" not in system
assert "英文版" in system
print("lang switch feeds the right version ok")

# ---- 2. 纯问答轮次：answer 给 null，前端据此不显示"采用"按钮
reply_payload = {"reply": "这版已经挺好了，不用改。", "answer": None}
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "这样行吗？"})
assert r.status_code == 200 and r.get_json()["answer"] is None
# 空字符串也要归一成 None，不然前端会显示一个空的"采用"按钮
reply_payload = {"reply": "同上", "answer": "   "}
assert client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "?"}).get_json()["answer"] is None
reply_payload = {"reply": "把背景压成一句，数据提前。", "answer": "改好的第一段\n\n改好的第二段"}
print("null answer round ok")

# ---- 3. 参数校验
assert client.post("/api/interview/bank/99999/chat", json={"lang": "zh", "message": "x"}).status_code == 404
assert client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "fr", "message": "x"}).status_code == 400
assert client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "  "}).status_code == 400
print("validation ok")

# ---- 4. 对话绝不写库（这套设计最关键的安全性质）
before = models.get_bank_item(item_id)
for msg in ["再短一点", "把数据放前面", "换个开头"]:
    assert client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": msg}).status_code == 200
after = models.get_bank_item(item_id)
assert after == before, "聊天不能改动库里的任何字段（答案、updated_at、user_edited 都不行）"
print("chat never writes to the db ok")

# ---- 5. 历史处理：脏数据滤掉、超长截断
calls.clear()
history = [
    {"role": "user", "content": "第一句"},
    {"role": "assistant", "content": '{"reply": "好的", "answer": null}'},
    {"role": "system", "content": "偷偷塞的 system"},   # 非法 role，滤掉
    {"role": "user", "content": 12345},                  # content 不是字符串，滤掉
    {"role": "user", "content": "   "},                  # 空白，滤掉
    "不是字典",                                           # 滤掉
]
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "接着改"})
assert r.status_code == 200
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "接着改", "history": history})
sent = calls[-1]["messages"]
assert [m["role"] for m in sent] == ["user", "assistant", "user"], sent
assert sent[0]["content"] == "第一句" and sent[-1]["content"] == "接着改"
assert all(m["role"] in ("user", "assistant") for m in sent)
print("history sanitized ok")

# 聊得越久历史越长，不截断的话 token 会一路涨上去
long_history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}句"} for i in range(30)]
calls.clear()
client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "最后一句", "history": long_history})
sent = calls[-1]["messages"]
assert len(sent) == interview.BANK_CHAT_HISTORY_LIMIT + 1, len(sent)
assert sent[0]["content"] == "第10句", sent[0]  # 30 条只留最后 20 条
assert sent[-1]["content"] == "最后一句"
print(f"history capped at {interview.BANK_CHAT_HISTORY_LIMIT} ok")

# ---- 6. LLM 报错要带原因传到前端（前端会把它渲染成一条红色气泡，不打断已有对话）
raise_error = "模拟LLM超时"
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "x"})
assert r.status_code == 500 and "模拟LLM超时" in r.get_json()["error"]
raise_error = None
# 返回的 JSON 缺 reply 时也要当成错误，而不是把 undefined 显示到页面上
reply_payload = {"answer": "只有答案没有说明"}
r = client.post(f"/api/interview/bank/{item_id}/chat", json={"lang": "zh", "message": "x"})
assert r.status_code == 500 and "reply" in r.get_json()["error"]
reply_payload = {"reply": "好的", "answer": "新版本"}
print("errors surface with a reason ok")

# ---- 7. 全局助手：看得到整个题库，但不给改写
calls.clear()
reply_payload = {"reply": "你这两个故事讲的是同一件事，建议合并。"}
r = client.post("/api/interview/bank/chat", json={"message": "我的故事有重复吗？"})
assert r.status_code == 200
data = r.get_json()
assert data["reply"] == reply_payload["reply"]
assert "answer" not in data, "全局助手不改写具体答案，响应里不该有 answer（否则前端会长出采用按钮）"

system = calls[0]["system"]
for q in ["为什么离开上一家？", "讲一个从0到1的例子"]:
    assert q in system, f"题库里的题没喂给助手：{q}"
assert "原来的中文答案" in system, "助手要看得到答案才能判断哪几题答得空"
assert "不负责改写具体答案" in system
print("assistant sees whole bank, gives advice only ok")

assert client.post("/api/interview/bank/chat", json={"message": " "}).status_code == 400
raise_error = "助手也会炸"
assert client.post("/api/interview/bank/chat", json={"message": "x"}).status_code == 500
raise_error = None
print("assistant validation + error ok")

# ---- 8. 助手的题库上下文来自后端自己查库，不信任前端传什么
calls.clear()
client.post("/api/interview/bank/chat", json={"message": "x", "bank_items": [{"question": "伪造的题"}]})
assert "伪造的题" not in calls[0]["system"], "题库上下文必须由后端自己 list_bank_items()"
print("bank context comes from the server ok")

# ---- 9. 长答案在喂给助手前会截断（控制 token）
models.update_bank_item(story_id, answer="很长的答案" * 500)
calls.clear()
client.post("/api/interview/bank/chat", json={"message": "x"})
assert "已截断" in calls[0]["system"], "超长答案应该被截断"
assert len(calls[0]["system"]) < 20000, "助手的 system prompt 不该无限长"
print("long answers truncated for the assistant ok")

print("\nALL PASS")
