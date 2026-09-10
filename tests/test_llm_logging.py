"""LLM 调用流水（llm_calls 表）回归测试。全程 mock HTTP，不产生真实 API 费用。

覆盖两件事：
1. 埋点记的东西对不对（task/provider/model/token/耗时/成本、失败也记、解析失败回填）
2. **埋点绝不能成为新的故障源**——第 6 段那条是核心回归锁：注入一个必然抛错的
   recorder，主流程仍然要正常返回。
"""
import json
import os
import sys
import tempfile
import types
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

tmpdir = tempfile.mkdtemp()
config.DB_PATH = os.path.join(tmpdir, "test.db")

import models

models.DB_PATH = config.DB_PATH
models.init_db()

import llm

llm.set_recorder(models.insert_llm_call, models.update_llm_call_error)


def rows():
    conn = models.get_conn()
    out = [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()]
    conn.close()
    return out


# ---- Anthropic 假客户端（带 usage，这是全项目第一次读 resp.usage）
class FakeBlock:
    def __init__(self, t):
        self.text = t


class FakeUsage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class FakeMsg:
    def __init__(self, text, stop_reason="end_turn", usage=(1000, 500)):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason
        self.usage = FakeUsage(*usage) if usage else None


class FakeAnthropic:
    OUT = '{"a": 1}'
    STOP = "end_turn"

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kw):
        return FakeMsg(FakeAnthropic.OUT, FakeAnthropic.STOP)


fake_mod = types.ModuleType("anthropic")
fake_mod.Anthropic = FakeAnthropic
sys.modules["anthropic"] = fake_mod
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

# ---- 1. 成功调用落一行，字段齐全
CFG = {"llm_provider": "anthropic", "anthropic_model": "claude-sonnet-5"}
llm.resolve_task(CFG, "analysis")          # 顺手设好 task ContextVar
out = llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
assert out == {"a": 1}
r = rows()
assert len(r) == 1, r
call = r[0]
assert call["task"] == "analysis", call["task"]
assert call["provider"] == "anthropic" and call["model"] == "claude-sonnet-5"
assert call["ok"] == 1 and call["error"] is None and call["error_type"] is None
assert call["input_tokens"] == 1000 and call["output_tokens"] == 500
assert call["max_tokens"] == 16000, call["max_tokens"]
assert call["prompt_chars"] == 2, call["prompt_chars"]        # "hi"
assert call["duration_ms"] is not None and call["duration_ms"] >= 0
# sonnet-5：$2/$10 每百万 → 1000/1e6*2 + 500/1e6*10 = 0.002 + 0.005
assert abs(call["cost_usd"] - 0.007) < 1e-9, call["cost_usd"]
assert json.loads(call["usage_json"])["cache_read_input_tokens"] == 0
print("successful call recorded with tokens + cost ok")

# ---- 2. task 跟着 resolve_task 走，换一个功能位就换一个标签
llm.resolve_task({**CFG, "llm_tasks": {"job_chat": "claude-haiku-4-5"}}, "job_chat")
llm.ask("hi there", provider="anthropic", model="claude-haiku-4-5")
call = rows()[-1]
assert call["task"] == "job_chat", call["task"]
assert call["model"] == "claude-haiku-4-5"
# haiku：$1/$5 → 1000/1e6*1 + 500/1e6*5 = 0.001 + 0.0025
assert abs(call["cost_usd"] - 0.0035) < 1e-9, call["cost_usd"]
print("task label follows resolve_task ok")

# ---- 3. 注册表外的模型：token 照记，成本记 NULL（算不出来就别瞎猜）
llm.ask("hi", provider="anthropic", model="some-other-model")
call = rows()[-1]
assert call["model"] == "some-other-model"
assert call["input_tokens"] == 1000 and call["cost_usd"] is None, call
print("unknown model records tokens but no cost ok")

# ---- 4. 截断也要记一行（ok=0），而且原异常照常抛、文案不变
before = len(rows())
FakeAnthropic.STOP = "max_tokens"
try:
    llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
    raise AssertionError("截断时应该抛错")
except RuntimeError as e:
    assert "截断" in str(e), str(e)
FakeAnthropic.STOP = "end_turn"
r = rows()
assert len(r) == before + 1
assert r[-1]["ok"] == 0 and r[-1]["error_type"] == "RuntimeError"
assert "截断" in r[-1]["error"]
print("truncation recorded as failure, original error preserved ok")

# ---- 5. JSON 解析失败：API 返回 200 记成功，之后要被回填改判成 ok=0
FakeAnthropic.OUT = "对不起，我不能回答这个问题。"
try:
    llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
    raise AssertionError("非 JSON 应该抛错")
except llm.LLMJsonError as e:
    assert "对不起" in e.raw, e.raw
    assert "对不起" in str(e), str(e)        # 报错文案带上模型实际吐了什么
    # 继承 ValueError，pipeline.py 里到处是的 except Exception 照旧接得住
    assert isinstance(e, ValueError)
FakeAnthropic.OUT = '{"a": 1}'
call = rows()[-1]
assert call["ok"] == 0 and call["error_type"] == "LLMJsonError", call
assert "对不起" in call["error"], call["error"]
print("json parse failure back-filled as failure ok")

# ---- 6. ★核心回归锁★ 埋点写库炸了，主流程必须照常返回
def exploding_recorder(**kw):
    raise RuntimeError("模拟写库炸了")


llm.set_recorder(exploding_recorder, exploding_recorder)
out = llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
assert out == {"a": 1}, "观测代码不能成为新的故障源"
print("exploding recorder does not break the call ok")

# ---- 7. 没注册 recorder 时不报错、不写库
llm.set_recorder(None, None)
before = len(rows())
assert llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5") == {"a": 1}
assert len(rows()) == before
llm.set_recorder(models.insert_llm_call, models.update_llm_call_error)
print("no recorder registered is a no-op ok")

# ---- 8. DeepSeek 侧：usage 归一成同样的字段名；截断时 usage 也要记下来
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
        return FakeResp(payload)

    return _urlopen


urllib.request.urlopen = make_urlopen(
    {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"a": 1}'}}],
        "usage": {"prompt_tokens": 2000, "completion_tokens": 800},
    }
)
llm.resolve_task({"llm_provider": "deepseek", "deepseek_model": "deepseek-v4-pro"}, "materials")
llm.ask_json("hi", provider="deepseek", model="deepseek-v4-pro")
call = rows()[-1]
assert call["provider"] == "deepseek" and call["task"] == "materials"
assert call["input_tokens"] == 2000 and call["output_tokens"] == 800
# deepseek-v4-pro：非高峰 cache-miss 价 $0.66/$1.98 每百万 → 2000/1e6*0.66 + 800/1e6*1.98
assert abs(call["cost_usd"] - 0.002904) < 1e-9, call["cost_usd"]
assert json.loads(call["usage_json"])["prompt_tokens"] == 2000
print("deepseek usage normalized ok")

# 截断：这是本项目历史上最容易踩的坑，之前完全没有可查询的数据
urllib.request.urlopen = make_urlopen(
    {
        "choices": [{"finish_reason": "length", "message": {"content": '{"a": "半截'}}],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 8192,
            "completion_tokens_details": {"reasoning_tokens": 8143},
        },
    }
)
try:
    llm.ask_json("hi", provider="deepseek", model="deepseek-v4-pro")
    raise AssertionError("截断时应该抛错")
except RuntimeError as e:
    assert "截断" in str(e) and "8143" in str(e), str(e)
call = rows()[-1]
assert call["ok"] == 0 and call["output_tokens"] == 8192
assert json.loads(call["usage_json"])["completion_tokens_details"]["reasoning_tokens"] == 8143
print("deepseek truncation records usage ok")

# ---- 9. 聚合查询能回答那三个问题
by_task = {r["key"]: r for r in models.llm_call_stats(group_by="task")}
assert by_task["analysis"]["calls"] >= 1
assert by_task["materials"]["failures"] == 1, by_task["materials"]
totals = models.llm_call_totals()
assert totals["calls"] == len(rows())
assert totals["failures"] >= 3
assert totals["cost_usd"] > 0
by_model = {r["key"]: r for r in models.llm_call_stats(group_by="model")}
assert by_model["claude-sonnet-5"]["avg_ms"] is not None
try:
    models.llm_call_stats(group_by="; DROP TABLE llm_calls")
    raise AssertionError("非法聚合维度应该报错")
except RuntimeError as e:
    assert "未知的聚合维度" in str(e)
print("stats aggregation ok")

# ---- 10. app.py 必须记得注册 recorder（忘了就悄悄没数据）
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"),
           encoding="utf-8").read()
assert "llm.set_recorder(insert_llm_call, update_llm_call_error)" in src, \
    "app.py 忘了注册 llm 流水回调，线上会悄悄没有任何调用记录"
print("app.py registers the recorder ok")

# ---- 11. 价格只有注册表这一处来源（note 里不许再写价格，否则必然漂移）
for m in llm.MODELS:
    assert "$" not in (m.get("note") or ""), f"{m['id']} 的 note 里又写了价格：{m['note']}"
assert llm.MODELS_BY_ID["claude-sonnet-5"]["price_in"] == 2.0    # 不是 $3——那是 Sonnet 4.6 的价
assert llm.MODELS_BY_ID["claude-sonnet-5"]["price_out"] == 10.0
# DeepSeek 非高峰 cache-miss 价（2026-09-08 查证官方定价页，见 llm.py MODELS 里的说明）
assert llm.MODELS_BY_ID["deepseek-v4-pro"]["price_in"] == 0.66
assert llm.MODELS_BY_ID["deepseek-v4-pro"]["price_out"] == 1.98
assert llm.MODELS_BY_ID["deepseek-v4-flash"]["price_in"] == 0.22
assert llm.MODELS_BY_ID["deepseek-v4-flash"]["price_out"] == 0.66
assert llm.estimate_cost("some-made-up-model-id", 1000, 1000) is None
print("pricing lives only in the registry ok")

# ---- 12. start_usage_tracking / pop_usage_summary / usage_text：路由层展示"这次操作
# 花了多少"用的就是这三个函数，见 web_helpers.usage_notification_message() 和
# routes_jobs.py 的 analyze_job_route()
assert llm.pop_usage_summary() is None, "没开跟踪时应该返回 None，不是空汇总"

llm.start_usage_tracking()
llm.resolve_task(CFG, "analysis")
llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
summary = llm.pop_usage_summary()
assert summary["calls"] == 1 and summary["ok"] is True
assert summary["provider"] == "anthropic"
assert summary["models"] == ["claude-sonnet-5"]
assert summary["input_tokens"] == 1000 and summary["output_tokens"] == 500
assert abs(summary["cost_usd"] - 0.007) < 1e-9
assert llm.usage_text(summary) == "Claude Sonnet 5 · 1,500 tokens · $0.0070", llm.usage_text(summary)
print("start/pop usage tracking single call ok")

assert llm.pop_usage_summary() is None, "取走之后应该清空，不能重复拿到上一次的"
print("pop_usage_summary clears the log ok")

# 一次操作里不止一次 LLM 调用（比如分析+分类）：按模型汇总，tokens/成本原样累加
llm.start_usage_tracking()
llm.ask("hi there", provider="anthropic", model="claude-haiku-4-5")
llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
summary = llm.pop_usage_summary()
assert summary["calls"] == 2
assert summary["models"] == ["claude-haiku-4-5", "claude-sonnet-5"]
assert abs(summary["cost_usd"] - (0.0035 + 0.007)) < 1e-9
assert llm.usage_text(summary) == "claude-haiku-4-5/claude-sonnet-5 · 3,000 tokens · $0.0105", llm.usage_text(summary)
print("multiple calls accumulate across models ok")

# 只要有一次调用定不了价（注册表外的模型），总成本必须是 None——不能悄悄当 0 元算，
# 那样用户会以为这次操作免费
llm.start_usage_tracking()
llm.ask("hi", provider="anthropic", model="claude-sonnet-5")
llm.ask("hi", provider="anthropic", model="some-other-model")
summary = llm.pop_usage_summary()
assert summary["cost_usd"] is None, summary
assert llm.usage_text(summary) == "claude-sonnet-5/some-other-model · 3,000 tokens · 成本未知", llm.usage_text(summary)
print("unknown-price call makes total cost unknown, not zero ok")

# 调用失败也要被跟踪到（ok=False），且不能影响原异常照常往上抛
llm.start_usage_tracking()
FakeAnthropic.STOP = "max_tokens"
try:
    llm.ask_json("hi", provider="anthropic", model="claude-sonnet-5")
    raise AssertionError("截断时应该抛错")
except RuntimeError:
    pass
FakeAnthropic.STOP = "end_turn"
summary = llm.pop_usage_summary()
assert summary["calls"] == 1 and summary["ok"] is False
print("failed call is still tracked and marked not ok ok")

assert llm.usage_text(None) is None, "没有可展示的用量时不该拼出 None 字样的文案"
print("usage_text(None) is None ok")

print("\nALL PASS")
