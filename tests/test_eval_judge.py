"""evals/judge.py 的测试：prompt 拼装、judge 返回解析、跨厂商 judge 目标解析、
judge_output 的集成路径（monkeypatch llm.chat_json）。

全部离线，不调真实 LLM、不花钱。"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evals"))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import judge  # noqa: E402
import llm  # noqa: E402

ANALYZE_OUTPUT = {
    "company_overview": "一家做仓储机器人的公司",
    "job_content_bullets": ["负责仓储调度系统开发", "优化分拣算法"],
    "skill_matched_bullets": ["5年Python经验"],
    "skill_gap_bullets": ["没有物流行业背景"],
    "requirement_items": [{"text": "需要PMP认证", "is_gap": True, "is_mandatory": True, "mandatory_evidence": "PMP required"}],
    "cognitive_match": 0.8,
    "content_match": 0.7,
}

MATERIALS_OUTPUT = {
    "needs_customization": True,
    "resume_paragraph_edits": [{"index": 3, "original": "原文段落", "text": "改写后的段落"}],
    "resume_optimization_bullets": ["把A段改写成B段"],
    "cover_letter": "Dear Hiring Manager, ...",
}

# ---------------------------------------------------------------- build_judge_messages

msgs = judge.build_judge_messages(
    "analyze", company="Acme", title="高级工程师",
    jd_text="JD正文" * 5000, resume_text="简历正文" * 3000, output=ANALYZE_OUTPUT,
)
assert len(msgs) == 1 and msgs[0]["role"] == "user", msgs
text = msgs[0]["content"]
assert "Acme" in text and "高级工程师" in text
for dim, _, _ in judge.JUDGE_RUBRICS["analyze"]:
    assert dim in text, dim
assert "0.8" in text and "需要PMP认证" in text, "模型输出应被渲染进 prompt"
# 截断生效：拼接结果应远小于原始输入总长（JD 10000 + 简历 12000 字符）
assert len(text) < judge.JD_LIMIT + judge.RESUME_LIMIT + 6000, len(text)

msgs_m = judge.build_judge_messages(
    "materials", company="Acme", title="产品经理",
    jd_text="jd", resume_text="r", output=MATERIALS_OUTPUT,
)
tm = msgs_m[0]["content"]
assert "Dear Hiring Manager" in tm and "改写后的段落" in tm
assert "relevance" in tm and "authenticity" in tm and "professionalism" in tm

# ---------------------------------------------------------------- parse_judge_verdict

v = judge.parse_judge_verdict({
    "dimensions": [
        {"name": "evidence_grounding", "score": 4, "reason": "条条有据"},
        {"name": "score_consistency", "score": 9, "reason": None},   # 越界 -> clamp 到 5
        {"name": "honesty", "score": "3", "reason": ""},             # 字符串数字也接
    ],
    "concerns": ["  理由略泛  ", "", 123],  # 空串/非字符串丢弃，其余去空白
    "overall": 4,
}, "analyze")
assert v["dimensions"]["evidence_grounding"]["score"] == 4
assert v["dimensions"]["evidence_grounding"]["reason"] == "条条有据"
assert v["dimensions"]["score_consistency"]["score"] == 5
assert v["dimensions"]["score_consistency"]["reason"] == ""
assert v["dimensions"]["honesty"]["score"] == 3
assert v["concerns"] == ["理由略泛"], v["concerns"]
assert v["overall"] == 4

# 缺期望维度 -> 记进 concerns（缺评可见，不静默按满分）
v2 = judge.parse_judge_verdict(
    {"dimensions": [{"name": "honesty", "score": 5, "reason": "ok"}], "concerns": [], "overall": 5},
    "analyze",
)
assert "judge 未返回维度 evidence_grounding，按缺评处理" in v2["concerns"], v2["concerns"]
assert "judge 未返回维度 score_consistency，按缺评处理" in v2["concerns"], v2["concerns"]

# overall 缺失 -> 用维度均值补
v3 = judge.parse_judge_verdict({
    "dimensions": [
        {"name": "relevance", "score": 5, "reason": "r"},
        {"name": "authenticity", "score": 3, "reason": "r"},
        {"name": "professionalism", "score": 4, "reason": "r"},
    ],
}, "materials")
assert v3["overall"] == 4.0, v3["overall"]

# 结构不合规 -> LLMJsonError（跟其它"模型输出不合规"是同一类失败）
for bad in ({"foo": 1}, {"dimensions": []}, {"dimensions": [{"name": "", "score": 3}]}, "not a dict"):
    try:
        judge.parse_judge_verdict(bad, "analyze")
        raise AssertionError(f"应当抛 LLMJsonError：{bad!r}")
    except llm.LLMJsonError:
        pass

# ---------------------------------------------------------------- judge_output（monkeypatch llm.chat_json）

seen = {}


def fake_chat_json(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    seen["provider"] = provider
    seen["model"] = model
    seen["messages"] = messages
    return {
        "dimensions": [
            {"name": "relevance", "score": 4, "reason": "ok"},
            {"name": "authenticity", "score": 4, "reason": "ok"},
            {"name": "professionalism", "score": 4, "reason": "ok"},
        ],
        "concerns": [],
        "overall": 4,
    }


_orig = llm.chat_json
llm.chat_json = fake_chat_json
try:
    v4 = judge.judge_output(
        "materials", company="c", title="t", jd_text="j", resume_text="r",
        output=MATERIALS_OUTPUT, provider="deepseek", model="deepseek-v4-flash",
    )
finally:
    llm.chat_json = _orig
assert v4["overall"] == 4
assert seen["provider"] == "deepseek" and seen["model"] == "deepseek-v4-flash", seen
assert seen["messages"] == judge.build_judge_messages(
    "materials", company="c", title="t", jd_text="j", resume_text="r", output=MATERIALS_OUTPUT,
)

# ---------------------------------------------------------------- resolve_judge_target

CFG = {"anthropic_model": "claude-sonnet-5", "deepseek_model": "deepseek-v4-pro"}
_key_names = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")
_saved = {k: os.environ.get(k) for k in _key_names}
try:
    for k in _key_names:
        os.environ.pop(k, None)
    # 显式指定模型：注册表里的按注册表反查 provider（不要求调用方保证 provider 对得上）
    assert judge.resolve_judge_target("anthropic", "deepseek-v4-flash", CFG) == ("deepseek", "deepseek-v4-flash")
    # 不在注册表里的手写模型名：按当前 provider 传
    assert judge.resolve_judge_target("anthropic", "my-custom-model", CFG) == ("anthropic", "my-custom-model")
    # 两家 key 都没有：退回同厂商默认（model=None 由 llm 层取该厂商默认）
    jp, jm = judge.resolve_judge_target("deepseek", None, CFG)
    assert jp == "deepseek" and jm is None, (jp, jm)
    # 有另一家 key：默认跨厂商，模型取 config 里那家的默认
    os.environ["DEEPSEEK_API_KEY"] = "x"
    assert judge.resolve_judge_target("anthropic", None, CFG) == ("deepseek", "deepseek-v4-pro")
    os.environ["ANTHROPIC_API_KEY"] = "x"
    os.environ.pop("DEEPSEEK_API_KEY", None)
    assert judge.resolve_judge_target("deepseek", None, CFG) == ("anthropic", "claude-sonnet-5")
finally:
    for k, val in _saved.items():
        if val is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = val

print("ALL PASS")
