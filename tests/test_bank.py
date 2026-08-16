"""P2 通用题库测试：临时库 + 临时 config，LLM 全程 mock，不产生真实 API 费用。

起草分三次 LLM 调用（自我介绍 / 通用问题 / STAR 故事库），每段跑完立刻入库，
所以这里的 mock 要按 prompt 内容判断这一次问的是哪一段。
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

# 三段各自的假返回。答案里刻意带 \n\n，用来验证分段一路存到库里都没被吃掉。
DRAFT_V1 = {
    "self_intro": {"answer_zh": "我是Cathy，产品经理…\n\n做过XX项目…", "answer_en": "I'm Cathy, a PM…\n\nI built XX…"},
    "common": {
        "items": [
            {"question": "为什么离开上一家？", "answer_zh": "初稿答案A\n\n第二段", "answer_en": "Draft A\n\nSecond"},
            {"question": "职业规划是什么？", "answer_zh": "初稿答案B", "answer_en": "Draft B"},
        ]
    },
    "star_story": {
        "items": [{"question": "讲一个从0到1的例子", "answer_zh": "情境：…\n\n任务：…", "answer_en": "Situation: …\n\nTask: …"}]
    },
}
# 第二次起草：同样的题给了不同答案，外加一道新题
DRAFT_V2 = {
    "self_intro": {"answer_zh": "第二版自我介绍", "answer_en": "Second version"},
    "common": {
        "items": [
            {"question": "为什么离开上一家？", "answer_zh": "初稿答案A-改", "answer_en": "Draft A-v2"},
            {"question": "职业规划是什么？", "answer_zh": "初稿答案B-改", "answer_en": "Draft B-v2"},
            {"question": "你最大的短板是什么？", "answer_zh": "新题答案", "answer_en": "New answer"},
        ]
    },
    "star_story": {"items": [{"question": "讲一个从0到1的例子", "answer_zh": "STAR 初稿-改", "answer_en": "STAR v2"}]},
}

current_draft = DRAFT_V1
calls = []

# prompt 里各段独有的句子，用来认出这一次问的是哪一段
SECTION_MARKERS = {
    "self_intro": "60-90 秒口播的自我介绍",
    "common": "几乎每场面试都会遇到的通用问题",
    "star_story": "可以反复复用的完整故事",
}
# 让某一段直接抛异常（测"一段失败不拖累另外两段"）
fail_sections = set()


def section_of(prompt):
    for key, marker in SECTION_MARKERS.items():
        if marker in prompt:
            return key
    raise AssertionError(f"认不出这是哪一段的 prompt：{prompt[:200]}")


def fake_chat(messages, provider="anthropic", model=None, system=None, max_tokens=4096):
    prompt = messages[0]["content"]
    key = section_of(prompt)
    calls.append({"section": key, "provider": provider, "model": model, "max_tokens": max_tokens, "prompt": prompt})
    if key in fail_sections:
        raise RuntimeError(f"模拟 {key} 段失败")
    return json.dumps(current_draft[key], ensure_ascii=False)


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
assert stats == {"updated": 0, "added": 4, "skipped": 0, "failed_sections": []}, stats
items = models.list_bank_items()
assert len(items) == 4
# 排序：self_intro 在最前，然后 common，最后 star_story
assert [i["category"] for i in items] == ["self_intro", "common", "common", "star_story"], [i["category"] for i in items]
intro = items[0]
assert intro["answer"].startswith("我是Cathy") and intro["answer_en"].startswith("I'm Cathy")
assert all(i["user_edited"] == 0 for i in items)
print("first draft ok:", stats)

# 三次调用，一段一次（不是一次性出完——双语+分段之后单次输出会顶到 max_tokens 上限）
assert len(calls) == 3, [c["section"] for c in calls]
assert [c["section"] for c in calls] == ["self_intro", "common", "star_story"], [c["section"] for c in calls]
# prompt 里带上了搜索关键词作为目标岗位
assert all("AI产品经理、Senior Product Manager" in c["prompt"] for c in calls)
# max_tokens 必须是 None（不设上限）。设了具体数值会踩推理模型的坑：deepseek-v4-pro 的
# max_tokens 是「内部推理 + 正文输出」共用额度，之前设 8192 时推理一口气吃光全部额度，
# 正文返回空字符串，报出来是一句看不懂的 JSONDecodeError。
assert all(c["max_tokens"] is None for c in calls), [c["max_tokens"] for c in calls]
print("three section calls, prompt uses config keywords, max_tokens 不设上限")

# ---- 1b. 每道题都要有中英文两版；分段（\n\n）不能在存取过程中被吃掉
by_q = {i["question"]: i for i in items}
assert all(i["answer_en"] for i in items), "每一条都该有英文版，不只是自我介绍"
assert "\n\n" in by_q["为什么离开上一家？"]["answer"], "中文答案的分段丢了"
assert "\n\n" in by_q["为什么离开上一家？"]["answer_en"], "英文答案的分段丢了"
assert "\n\n" in by_q["讲一个从0到1的例子"]["answer"]
print("bilingual + paragraph breaks preserved ok")

# ---- 1c. 每段只把**自己这一类**的已有题目喂回去（拆成三次调用后模型压根看不到别的类别，
#          原来那条"不要串类别抄"的约束就没必要了）
import interview as interview_mod

assert all(interview_mod._NO_EXISTING in c["prompt"] for c in calls), "第一次起草时题库还是空的"

# ---- 2. 用户手改两条
leave = by_q["为什么离开上一家？"]
models.update_bank_item(leave["id"], answer="这是我自己写的答案，不许覆盖")
models.update_bank_item(intro["id"], answer="我自己的自我介绍", answer_en="My own intro")
after = {i["id"]: i for i in models.list_bank_items()}
assert after[leave["id"]]["user_edited"] == 1
assert after[intro["id"]]["answer_en"] == "My own intro"
print("manual edit marks user_edited ok")

# ---- 3. 再次起草：改过的不能被覆盖，没改过的更新，新题新增
current_draft = DRAFT_V2
calls.clear()
stats = pipeline.generate_bank_draft()
assert stats == {"updated": 2, "added": 1, "skipped": 2, "failed_sections": []}, stats

after = {i["question"]: i for i in models.list_bank_items()}
# 改过的两条原样保留
assert after["为什么离开上一家？"]["answer"] == "这是我自己写的答案，不许覆盖"
assert after["自我介绍（60-90秒）"]["answer"] == "我自己的自我介绍"
assert after["自我介绍（60-90秒）"]["answer_en"] == "My own intro"
# 没改过的两条被更新，中英文一起更新
assert after["职业规划是什么？"]["answer"] == "初稿答案B-改"
assert after["职业规划是什么？"]["answer_en"] == "Draft B-v2"
assert after["讲一个从0到1的例子"]["answer"] == "STAR 初稿-改"
# 新题被加进来
assert after["你最大的短板是什么？"]["answer"] == "新题答案"
assert len(after) == 5
print("re-draft protects user edits ok:", stats)

# 这一轮题库非空了：每段的 prompt 里只该出现自己那一类的题
common_prompt = next(c["prompt"] for c in calls if c["section"] == "common")
star_prompt = next(c["prompt"] for c in calls if c["section"] == "star_story")
assert "为什么离开上一家？" in common_prompt, "已有的通用题没喂回给模型"
assert "讲一个从0到1的例子" not in common_prompt, "别的类别的题不该出现在这一段的 prompt 里"
assert "讲一个从0到1的例子" in star_prompt and "为什么离开上一家？" not in star_prompt
assert "一字不差地" in common_prompt, "prompt 里必须要求复用已有题目的原文措辞"
print("existing items fed back per-category ok")

# ---- 4. 手动加的题默认就受保护，不会被之后的起草覆盖
manual_id = models.add_bank_item("common", "手动加的题", answer="我的答案")
manual = models.get_bank_item(manual_id)
assert manual["user_edited"] == 1, "手动加的题应该默认 user_edited=1"
current_draft = {
    "self_intro": {"answer_zh": "占位", "answer_en": "placeholder"},
    "common": {"items": [{"question": "手动加的题", "answer_zh": "AI想覆盖"}]},
    "star_story": {"items": [{"question": "讲一个从0到1的例子", "answer_zh": "STAR 原样"}]},
}
stats = pipeline.generate_bank_draft()
# 手动加的题 + 用户改过的自我介绍 = 两条被 user_edited 保护住，一条都没被覆盖
assert stats["added"] == 0 and stats["skipped"] == 2, stats
assert models.get_bank_item(manual_id)["answer"] == "我的答案"
print("manually added items are protected ok")

# ---- 5. 起草不会删除已有条目（AI 这轮没生成到的题保留）
assert len(models.list_bank_items()) == 6
print("re-draft never deletes ok")

# ---- 5b. 措辞飘动不能堆出重复题（真实踩过的坑：起草两次 16 条变 28 条，只有 4 条对上）
#          模型每轮自己起的标题都不一样，只差空格/标点/「0到1」写法也算不同字符串。
before = len(models.list_bank_items())
current_draft = {
    "self_intro": {"answer_zh": "占位", "answer_en": "placeholder"},
    # 已有 "职业规划是什么？"（user_edited=0），这里换成加空格 + 换标点的写法
    "common": {"items": [{"question": " 职业规划是什么 ! ", "answer_zh": "措辞飘了但还是同一题"}]},
    # 已有 "讲一个从0到1的例子"（user_edited=0），这里把 0到1 写成 0 到 1 并加句号
    "star_story": {"items": [{"question": "讲一个从 0 到 1 的例子。", "answer_zh": "STAR 措辞飘了"}]},
}
stats = pipeline.generate_bank_draft()
assert stats["added"] == 0, f"措辞飘动不该新增条目，实际 {stats}"
assert stats["updated"] == 2, stats
assert len(models.list_bank_items()) == before, "题库条数不该变"
after = {i["question"]: i for i in models.list_bank_items()}
# 库里存的还是原来那句问题原文，答案被更新
assert "职业规划是什么？" in after and " 职业规划是什么 ! " not in after
assert after["职业规划是什么？"]["answer"] == "措辞飘了但还是同一题"
assert after["讲一个从0到1的例子"]["answer"] == "STAR 措辞飘了"
print("normalized matching absorbs wording drift ok:", stats)

# 同一批里出现两道归一化后相同的题，只认第一道，不能自己插两条重复的
current_draft = {
    "self_intro": {"answer_zh": "占位", "answer_en": "placeholder"},
    "common": {
        "items": [
            {"question": "全新的题？", "answer_zh": "第一个"},
            {"question": "全新的题", "answer_zh": "第二个（重复）"},
        ]
    },
    "star_story": {"items": []},
}
stats = pipeline.generate_bank_draft()
assert stats["added"] == 1, f"同一批里的重复题只该加一条，实际 {stats}"
assert len([i for i in models.list_bank_items() if "全新的题" in i["question"]]) == 1
print("dedupes within a single batch ok")

# 归一化只用于匹配，不能把不同的题误判成同一题
assert models.normalize_bank_question("为什么离开上一家？") != models.normalize_bank_question(
    "为什么离开上一家 / 这次为什么想看外部机会？"
), "真正的改写归一化后仍应是两道题（这一层靠 prompt 复用措辞解决）"
print("normalization does not over-merge ok")

# ---- 5c. 一段失败不拖累另外两段：前两段照样入库，失败原因单独收进 failed_sections
current_draft = DRAFT_V1
fail_sections = {"star_story"}
before_items = {i["id"]: dict(i) for i in models.list_bank_items()}
stats = pipeline.generate_bank_draft()
assert len(stats["failed_sections"]) == 1, stats
assert "STAR 故事库" in stats["failed_sections"][0] and "模拟 star_story 段失败" in stats["failed_sections"][0]
# 自我介绍那条是 user_edited=1 被跳过的，通用题里没改过的被更新了 —— 总之前两段确实跑到了
assert stats["skipped"] + stats["updated"] > 0, stats
assert len(models.list_bank_items()) == len(before_items), "失败的那一段不该动到已有条目"
print("one failed section does not block the others ok:", stats["failed_sections"])

# 三段全失败才抛异常（走整体失败那条路径）
fail_sections = {"self_intro", "common", "star_story"}
try:
    pipeline.generate_bank_draft()
    raise AssertionError("三段全失败时应该抛异常")
except RuntimeError as e:
    assert "自我介绍" in str(e) and "STAR 故事库" in str(e), str(e)
print("all sections failing raises ok")
fail_sections = set()

# ---- 5d. build_existing_block 的分组/去重/空态
import interview as interview_mod

assert interview_mod.build_existing_block([]) == interview_mod._NO_EXISTING
assert interview_mod.build_existing_block(None) == interview_mod._NO_EXISTING
block = interview_mod.build_existing_block(
    [
        {"category": "star_story", "question": "故事题"},
        {"category": "common", "question": "通用题A"},
        {"category": "common", "question": "通用题A"},  # 重复的只列一次
        {"category": "common", "question": "  "},        # 空的跳过
        {"category": "self_intro", "question": "自我介绍（60-90秒）"},
    ]
)
# 顺序固定成 self_intro → common → star_story，跟三段任务的顺序一致
assert block == (
    "【self_intro 自我介绍】\n- 自我介绍（60-90秒）\n\n"
    "【items 通用问题】\n- 通用题A\n\n"
    "【star_stories 核心故事库】\n- 故事题"
), repr(block)
print("existing block grouping/dedupe/empty ok")

# ---- 6. 接口层
import app as flask_app

flask_app.app.config["TESTING"] = True
client = flask_app.app.test_client()

# 不写死条数：上面每加一段测试都会改变库里的条目数，写死的话每次都得回来改这个数字。
baseline = len(models.list_bank_items())
r = client.get("/api/interview/bank")
data = r.get_json()
assert len(data["items"]) == baseline and data["generating"] is False

# 新增 / 校验
assert client.post("/api/interview/bank", json={"category": "bogus", "question": "x"}).status_code == 400
assert client.post("/api/interview/bank", json={"category": "common", "question": "  "}).status_code == 400
r = client.post("/api/interview/bank", json={"category": "common", "question": "接口加的题"})
new_id = r.get_json()["id"]

# 编辑
r = client.put(f"/api/interview/bank/{new_id}", json={"answer": "接口写的答案", "answer_en": "via API"})
assert r.status_code == 200
saved = models.get_bank_item(new_id)
assert saved["answer"] == "接口写的答案" and saved["answer_en"] == "via API"
assert client.put("/api/interview/bank/99999", json={"answer": "x"}).status_code == 404

# 删除
assert client.delete(f"/api/interview/bank/{new_id}").status_code == 200
assert client.delete("/api/interview/bank/99999").status_code == 404
assert len(models.list_bank_items()) == baseline, "增删一条之后应该回到原来的条数"
print("bank CRUD endpoints ok")

# ---- 7. 起草接口：后台跑 + 并发保护
current_draft = DRAFT_V1
import job_state

slow_calls = {"n": 0}
orig_chat = llm.chat


def slow_chat(messages, **kw):
    time.sleep(0.2)
    slow_calls["n"] += 1
    return json.dumps(current_draft[section_of(messages[0]["content"])], ensure_ascii=False)


llm.chat = slow_chat
r = client.post("/api/interview/bank/generate")
assert r.status_code == 200 and r.get_json()["started"] is True
# 正在跑的时候再点一次应该 409
r2 = client.post("/api/interview/bank/generate")
assert r2.status_code == 409, r2.status_code
assert client.get("/api/interview/bank").get_json()["generating"] is True
for _ in range(100):
    if not job_state.bank_generating():
        break
    time.sleep(0.1)
assert slow_calls["n"] == 3 and not job_state.bank_generating()
assert client.get("/api/interview/bank").get_json()["generating"] is False
print("generate endpoint: background + 409 guard + state cleared ok")

# ---- 8. 起草失败：generating 标志要清掉（否则按钮永远灰着），失败原因要能传到前端
#         （踩过的坑：失败只写服务端日志，前端看到 generating 变 false 就弹绿色的
#          "起草完成"，用户对着一个空题库以为是自己等得不够久）
llm.chat = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟LLM报错"))
r = client.post("/api/interview/bank/generate")
assert r.status_code == 200
for _ in range(50):
    if not job_state.bank_generating():
        break
    time.sleep(0.1)
assert not job_state.bank_generating(), "生成失败后 generating 标志必须清掉"
data = client.get("/api/interview/bank").get_json()
assert data["generating"] is False
assert "模拟LLM报错" in (data["error"] or ""), f"失败原因没传给前端：{data.get('error')!r}"
print("failure surfaces to frontend ok:", data["error"])

# 只挂一段时，前端也要看得到——不能因为另外两段成功了就一声不吭
llm.chat = fake_chat
fail_sections = {"star_story"}
r = client.post("/api/interview/bank/generate")
for _ in range(50):
    if not job_state.bank_generating():
        break
    time.sleep(0.1)
data = client.get("/api/interview/bank").get_json()
assert "STAR 故事库" in (data["error"] or ""), f"部分失败没传给前端：{data.get('error')!r}"
print("partial failure surfaces to frontend ok")
fail_sections = set()

# 重新起草成功后，上一轮的报错必须消失，不能一直挂在界面上
current_draft = DRAFT_V1
r = client.post("/api/interview/bank/generate")
assert r.status_code == 200
for _ in range(50):
    if not job_state.bank_generating():
        break
    time.sleep(0.1)
assert client.get("/api/interview/bank").get_json()["error"] is None, "成功一轮之后旧报错要清掉"
print("error cleared on next successful draft ok")

# ---- 9. 前端接线
bankjs = open(os.path.join(BASE, "static", "bank.js"), encoding="utf-8").read()
commonjs = open(os.path.join(BASE, "static", "common.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "templates", "interview.html"), encoding="utf-8").read()
index_html = open(os.path.join(BASE, "templates", "index.html"), encoding="utf-8").read()
css = open(os.path.join(BASE, "static", "style.css"), encoding="utf-8").read()

# 题库是独立页面了，不再是主页上的弹窗
assert 'id="bankRoot"' in html and 'id="bankAssistant"' in html
assert 'href="/interview"' in index_html, "主页的「面试题库」按钮要跳到独立页面"
assert "interviewModalOverlay" not in index_html, "旧的题库弹窗应该已经删掉"
assert "openInterviewModal" not in bankjs and "openInterviewModal" not in index_html
assert html.index("/static/common.js") < html.index("/static/bank.js")

import re

defined = set(re.findall(r"function\s+(\w+)", bankjs + commonjs))
for fn in ["loadBank", "renderBank", "generateBankDraft", "saveBankItem", "addBankItem",
           "removeBankItem", "startBankPoll", "stopBankPoll", "autoGrow",
           "toggleItemChat", "sendItemChat", "applyChatAnswer", "setChatLang",
           "toggleAssistant", "sendAssistantChat"]:
    assert fn in defined, f"缺少 {fn}"
inline = set(re.findall(r'onclick="[^"]*?(\w+)\(', bankjs)) | set(re.findall(r'onclick="(\w+)\(', html))
missing = {f for f in inline if f not in defined and f != "stopPropagation"}
assert not missing, f"内联 onclick 引用了不存在的函数：{missing}"
for cls in [".bank-count", ".bank-badge", ".bank-answer", ".bank-actions", ".bank-label",
            ".bank-item", ".bank-item-head", ".bank-chat", ".bank-chat-draft", ".bank-assistant"]:
    assert cls in css, f"缺少样式 {cls}"
# 答案框不能再有内部滚动条 / 固定行数：高度由 autoGrow() 跟着内容算
assert "overflow-y: hidden" in css.split(".bank-answer {")[1].split("}")[0]
assert 'rows="6"' not in bankjs, "答案框不该再写死行数"
# 用户输入和 AI 回复必须转义（都会原样回填到页面上）
for bad in ["${item.answer}", "${item.question}", "${msg.content}", "${msg.answer}"]:
    assert bad not in bankjs, f"未转义的插值：{bad}"
# 起草失败要走失败的分支：读到 error 就报错，而不是一律弹"起草完成"
assert "data.error" in bankjs, "前端没有读取 /api/interview/bank 的 error 字段"
assert "题库起草失败" in bankjs, "前端没有把起草失败提示出来"
assert "escapeHtml(bankError)" in bankjs, "错误信息回填到页面时必须转义"
# 删除按钮要说清后果（真删库 + AI 可能再生成），不能只是一句"确定删除吗"
assert "永久删掉" in bankjs and "可能会再生成" in bankjs
print("frontend wiring ok")

r = client.get("/interview")
assert r.status_code == 200 and "通用面试题库" in r.data.decode("utf-8")
print("page renders ok")

print("\nALL PASS")
