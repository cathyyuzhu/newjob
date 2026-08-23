"""llm.chat_tool_step() 回归测试：项目里第一个 LLM 工具调用循环的底层调用封装
（见 linkedin_how_you_fit.py 的 agent 兜底路径）。全程 mock anthropic.Anthropic 客户端，
不产生真实 API 费用、不需要真实 ANTHROPIC_API_KEY。
"""
import os
import sys

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
assert result["content_blocks"] == resp.content
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

print("\nALL PASS")
