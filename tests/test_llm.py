"""llm.py 回归测试：max_tokens 处理 + 截断识别。全程 mock HTTP，不产生真实 API 费用。

背景（2026-08-16 线上故障）：把 provider 适配器从 analyzer.py 抽到 llm.py 时，顺手给
DeepSeek 加了 max_tokens 参数（原来不传）。deepseek-v4-pro 是推理模型，max_tokens 是
「推理 + 输出」共用额度，8192 被推理吃掉 8143，正文只剩 49 token，返回半截 JSON，
报错是看不懂的「Unterminated string」。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm

sent = {}


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_urlopen(payload):
    def _urlopen(req, timeout=None):
        sent.clear()
        sent.update(json.loads(req.data.decode("utf-8")))
        sent["_timeout"] = timeout
        return FakeResp(payload)

    return _urlopen


OK_PAYLOAD = {
    "choices": [{"finish_reason": "stop", "message": {"content": '{"a": 1}'}}],
    "usage": {"completion_tokens": 100},
}
TRUNC_PAYLOAD = {
    "choices": [{"finish_reason": "length", "message": {"content": '{"a": "半截字符'}}],
    "usage": {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8143}},
}


os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

# ---- 1. 默认不给 DeepSeek 发 max_tokens（这就是那次故障的直接修复）
urllib.request.urlopen = make_urlopen(OK_PAYLOAD)
out = llm.ask_json("hi", provider="deepseek", model="deepseek-v4-pro")
assert out == {"a": 1}
assert "max_tokens" not in sent, f"默认不该给 DeepSeek 传 max_tokens，实际传了 {sent.get('max_tokens')}"
assert sent["model"] == "deepseek-v4-pro" and sent["stream"] is False
print("deepseek omits max_tokens by default ok")

# ---- 2. 显式传了才发
llm.ask_json("hi", provider="deepseek", model="m", max_tokens=32768)
assert sent["max_tokens"] == 32768
print("deepseek honors explicit max_tokens ok")

# ---- 3. 截断要报清楚的错，而不是让 json.loads 抛 Unterminated string
urllib.request.urlopen = make_urlopen(TRUNC_PAYLOAD)
try:
    llm.ask_json("hi", provider="deepseek", model="m")
    raise AssertionError("截断时应该抛错")
except RuntimeError as e:
    msg = str(e)
    assert "截断" in msg, msg
    assert "推理" in msg, msg
    assert "8143" in msg, msg           # 带上实际用量，方便判断该调多大
    assert "Unterminated" not in msg
    print("deepseek truncation raises actionable error ok:", msg[:56] + "…")

# ---- 4. system prompt 拼成 messages[0]（OpenAI 兼容接口的形状）
urllib.request.urlopen = make_urlopen(OK_PAYLOAD)
llm.chat([{"role": "user", "content": "u"}], provider="deepseek", model="m", system="SYS")
assert sent["messages"][0] == {"role": "system", "content": "SYS"}
assert sent["messages"][1] == {"role": "user", "content": "u"}
# 不传 system 时不该凭空多一条
llm.chat([{"role": "user", "content": "u"}], provider="deepseek", model="m")
assert len(sent["messages"]) == 1 and sent["messages"][0]["role"] == "user"
print("deepseek system prompt placement ok")

# ---- 5. 多轮 messages 原样透传（P3 模拟面试要靠这个）
llm.chat(
    [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}],
    provider="deepseek", model="m",
)
assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
print("multi-turn messages pass through ok")

# ---- 6. Anthropic：API 强制要 max_tokens，None 时退到默认值；stop_reason 截断也要报错
class FakeBlock:
    def __init__(self, t):
        self.text = t


class FakeMsg:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason


captured = {}


class FakeAnthropic:
    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kw):
        captured.clear()
        captured.update(kw)
        return FakeMsg(captured.pop("_out", '{"a": 2}'), captured.get("_stop", "end_turn"))


import types

fake_mod = types.ModuleType("anthropic")
fake_mod.Anthropic = FakeAnthropic
sys.modules["anthropic"] = fake_mod
os.environ["ANTHROPIC_API_KEY"] = "test-key"

# 注册表里没有的模型名（config.json 手写的）退到全局默认值
out = llm.ask_json("hi", provider="anthropic", model="some-other-model")
assert out == {"a": 2}
assert captured["max_tokens"] == llm.DEFAULT_ANTHROPIC_MAX_TOKENS
assert "system" not in captured and "thinking" not in captured
llm.ask_json("hi", provider="anthropic", max_tokens=1234, system="SYS")
assert captured["max_tokens"] == 1234 and captured["system"] == "SYS"
print("anthropic max_tokens fallback + system param ok")

# 注册表里的模型用自己那一档 max_tokens。Claude Sonnet 5 起，不传 thinking 就是**默认开着**
# 思考，而 max_tokens 是「思考 + 正文」共用的——8192 不够写完一份十几道题的面试准备，
# 会在写到一半时被截断（跟 deepseek 推理模型那个坑同源），所以显式关掉思考并抬高上限。
sonnet = llm.get_model("claude-sonnet-5")
assert sonnet["provider"] == "anthropic" and sonnet["max_tokens"] == 16000
llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
assert captured["max_tokens"] == 16000, captured["max_tokens"]
assert captured["thinking"] == {"type": "disabled"}, captured.get("thinking")
# Haiku 默认就不思考，不用传这个参数
llm.ask_json("hi", provider="anthropic", model="claude-haiku-4-5")
assert captured["max_tokens"] == 16000 and "thinking" not in captured
print("model registry drives max_tokens + thinking ok")

# ---- 6b. 按功能位取模型：配了用配的，没配回退到全局，模型名打错当场报错
BASE_CFG = {"llm_provider": "deepseek", "deepseek_model": "deepseek-v4-pro",
            "anthropic_model": "claude-sonnet-5"}
assert llm.resolve_task(BASE_CFG, "interview_prep") == ("deepseek", "deepseek-v4-pro")
assert llm.resolve_task({**BASE_CFG, "llm_tasks": {}}, "analysis") == ("deepseek", "deepseek-v4-pro")
assert llm.resolve_task({**BASE_CFG, "llm_tasks": {"analysis": ""}}, "analysis") == ("deepseek", "deepseek-v4-pro")
# provider 从注册表反查，不需要用户自己保证 llm_provider 和模型名对得上
assert llm.resolve_task(
    {**BASE_CFG, "llm_tasks": {"interview_bank": "claude-haiku-4-5"}}, "interview_bank"
) == ("anthropic", "claude-haiku-4-5")
# 只影响自己那一个功能位
assert llm.resolve_task(
    {**BASE_CFG, "llm_tasks": {"interview_bank": "claude-haiku-4-5"}}, "analysis"
) == ("deepseek", "deepseek-v4-pro")
try:
    llm.resolve_task({**BASE_CFG, "llm_tasks": {"analysis": "gpt-9"}}, "analysis")
    raise AssertionError("未知模型应该当场报错，而不是一路走到 API 才 404")
except RuntimeError as e:
    assert "未知的模型：gpt-9" in str(e), str(e)
assert set(llm.LLM_TASKS) == {
    "analysis", "materials", "interview_prep", "interview_bank", "resume_review", "job_chat",
    "preference_profile",
}
print("per-task model resolution ok")


class TruncAnthropic(FakeAnthropic):
    def create(self, **kw):
        return FakeMsg('{"a": "半截', stop_reason="max_tokens")


fake_mod.Anthropic = TruncAnthropic
try:
    llm.ask_json("hi", provider="anthropic")
    raise AssertionError("截断时应该抛错")
except RuntimeError as e:
    assert "截断" in str(e), str(e)
print("anthropic truncation raises actionable error ok")

# ---- 7. 未知 provider 的报错文案保持不变
try:
    llm.chat([{"role": "user", "content": "x"}], provider="bogus")
    raise AssertionError("应该抛错")
except RuntimeError as e:
    assert "未知的 llm_provider：bogus" in str(e), str(e)
print("unknown provider error preserved ok")

# ---- 8. interview.py 不再写死 8192
import interview

assert interview.PREP_MAX_TOKENS is None, interview.PREP_MAX_TOKENS
assert interview.BANK_MAX_TOKENS is None, interview.BANK_MAX_TOKENS
print("interview.py no longer caps max_tokens ok")

print("\nALL PASS")
