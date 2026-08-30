"""llm.chat_tool_step() 回归测试：项目里第一个 LLM 工具调用循环的底层调用封装
（见 linkedin_how_you_fit.py 的 agent 兜底路径）。全程 mock anthropic.Anthropic 客户端，
不产生真实 API 费用、不需要真实 ANTHROPIC_API_KEY。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import llm

os.environ["ANTHROPIC_API_KEY"] = "test-key"

TOOLS = [{"name": "finish", "description": "结束", "input_schema": {"type": "object", "properties": {}}}]


class FakeBlock:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeAnthropic:
    def __init__(self, api_key=None):
        self.api_key = api_key


# ---- 1. 正常路径：tools/tool_choice 正确传下去，tool_use block 被正确解析
resp = FakeResponse([FakeBlock("tool_use", id="t1", name="finish", input={"status": "reached_end", "reason": "done"})])
fake_messages = FakeMessages(resp)
FakeAnthropic.messages = fake_messages
anthropic.Anthropic = FakeAnthropic

result = llm.chat_tool_step([{"role": "user", "content": "hi"}], TOOLS, system="SYS")
sent = fake_messages.calls[-1]
assert sent["tools"] == TOOLS
assert sent["tool_choice"] == {"type": "any"}
assert sent["system"] == "SYS"
assert result["stop_reason"] == "tool_use"
assert result["tool_use"] == {"id": "t1", "name": "finish", "input": {"status": "reached_end", "reason": "done"}}
assert result["assistant_message"] == {"role": "assistant", "content": resp.content}
print("chat_tool_step sends tools/tool_choice and parses tool_use ok")

# ---- 2. 模型没按预期调用工具（content 里没有 tool_use block）：tool_use 应为 None，不能瞎猜
resp2 = FakeResponse([FakeBlock("text", text="我不太确定")], stop_reason="end_turn")
fake_messages._response = resp2
result2 = llm.chat_tool_step([{"role": "user", "content": "hi"}], TOOLS)
assert result2["tool_use"] is None, "没有 tool_use block 时应该返回 None，不能假装有"
print("chat_tool_step returns tool_use=None when model didn't call a tool ok")

# ---- 3. 没有 ANTHROPIC_API_KEY 时直接抛错，不静默用别的 provider 兜底
old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
try:
    try:
        llm.chat_tool_step([{"role": "user", "content": "hi"}], TOOLS)
        raise AssertionError("没有 ANTHROPIC_API_KEY 时应该抛错")
    except RuntimeError as e:
        assert "ANTHROPIC_API_KEY" in str(e)
        print("chat_tool_step raises without ANTHROPIC_API_KEY ok")
finally:
    if old_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = old_key


# ---- 4. provider="deepseek"：OpenAI 兼容 function calling，tools/tool_choice 转换正确
class FakeHttpResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


sent = {}


def make_urlopen(payload):
    def _urlopen(req, timeout=None):
        sent.clear()
        sent.update(json.loads(req.data.decode("utf-8")))
        return FakeHttpResp(payload)

    return _urlopen


os.environ["DEEPSEEK_API_KEY"] = "test-key"

DEEPSEEK_TOOL_CALL_PAYLOAD = {
    "choices": [{
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "finish", "arguments": '{"status": "reached_end", "reason": "done"}'}},
            ],
        },
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}
urllib.request.urlopen = make_urlopen(DEEPSEEK_TOOL_CALL_PAYLOAD)

result3 = llm.chat_tool_step(
    [{"role": "user", "content": "hi"}], TOOLS, provider="deepseek", model="deepseek-v4-pro", system="SYS"
)
assert sent["tool_choice"] == "required"
assert sent["thinking"] == {"type": "disabled"}, "DeepSeek v4 系列带 tools 时默认开思考模式，不显式关掉会跟 tool_choice=required 冲突报 400"
assert sent["tools"] == [{
    "type": "function",
    "function": {"name": "finish", "description": "结束", "parameters": {"type": "object", "properties": {}}},
}]
assert sent["messages"][0] == {"role": "system", "content": "SYS"}
assert result3["tool_use"] == {"id": "call_1", "name": "finish", "input": {"status": "reached_end", "reason": "done"}}
assert result3["assistant_message"] == DEEPSEEK_TOOL_CALL_PAYLOAD["choices"][0]["message"]
print("chat_tool_step(provider='deepseek') converts tools and parses tool_calls ok")

# ---- 5. deepseek 侧模型没调用任何工具时 tool_use 应为 None，不能瞎猜
NO_TOOL_CALL_PAYLOAD = {
    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "不确定"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}
urllib.request.urlopen = make_urlopen(NO_TOOL_CALL_PAYLOAD)
result4 = llm.chat_tool_step([{"role": "user", "content": "hi"}], TOOLS, provider="deepseek")
assert result4["tool_use"] is None
print("chat_tool_step(provider='deepseek') returns tool_use=None when model didn't call a tool ok")

# ---- 6. tool_result_message()：两家 provider 的回填形状不一样
assert llm.tool_result_message("anthropic", "t1", "desc") == {
    "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "desc"}],
}
assert llm.tool_result_message("deepseek", "t1", "desc") == {
    "role": "tool", "tool_call_id": "t1", "content": "desc",
}
print("tool_result_message shapes results per provider ok")

print("\nALL PASS")
