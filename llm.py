"""统一的 LLM 调用层：屏蔽 Anthropic / DeepSeek 两家 provider 的接口差异，对上层
只暴露"给一段对话、拿一段回复"。

原来这段适配器代码住在 analyzer.py 里，只支持"单条 prompt 字符串"——因为当时唯一的
用途就是 JD-简历匹配分析（一问一答）。面试准备模块里的模拟面试需要多轮对话
（messages 数组 + system prompt），而且 analyzer.py 从命名到文档都是"简历匹配"专属的，
不适合再往里塞第三方消费者，所以整体抽到这里，analyzer.py / interview.py / pipeline.py
共用。行为跟搬运前一致（包括报错文案）。
"""
import contextlib
import contextvars
import json
import logging
import os
import time
import urllib.error
import urllib.request

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

# _call_deepseek_tools() 专用的默认模型（2026-08-30）。实测确认 DeepSeek 全系 v4
# 模型（pro/flash 都一样，deepseek-reasoner 也一样）只要带 tools 就默认开 thinking
# mode，这时候不允许强制 tool_choice="required"（400: "Thinking mode does not
# support this tool_choice"）——一开始误以为只有 v4-pro 这种标"推理模型"的才会这样，
# 实测发现 v4-flash 同样会报，得靠下面 _call_deepseek_tools() 里显式传
# `"thinking": {"type": "disabled"}` 关掉（跟 Anthropic 那边的 no_thinking 是同一个
# 思路），换模型本身并不能解决问题。选 flash 而不是继续用 DEFAULT_DEEPSEEK_MODEL
# （v4-pro）单纯是因为 flash 支持采样参数，配合 TASK_TEMPERATURE["how_you_fit_agent"]=0.0
# 更贴合"导航动作要确定性"的要求，且更便宜。
DEFAULT_DEEPSEEK_TOOL_MODEL = "deepseek-v4-flash"

# Anthropic 的 messages API 强制要求传 max_tokens，没得选，只能给个默认值。
# DeepSeek 不要求，所以默认压根不传（见 _call_deepseek 里的说明）。
DEFAULT_ANTHROPIC_MAX_TOKENS = 8192

TRUNCATED_HINT = (
    "LLM 输出在写完之前就被 max_tokens 截断了（返回的内容不完整，没法解析）。"
    "如果用的是推理模型（如 deepseek-v4-pro），注意 max_tokens 的额度是"
    "「内部推理 + 正文输出」共用的，推理很容易把额度吃掉一大半——把 max_tokens 调大，或者干脆不传（None）。"
)

# 可选模型清单。前端下拉、后端校验、provider 推断都读这一份，避免"界面上能选、后端不认"。
#
# max_tokens / no_thinking 只对 Anthropic 有意义：
# - Claude Sonnet 5 默认就开自适应思考，而 max_tokens 是「思考 + 正文」共用的硬上限。
#   本项目一次要出十几道题的 JSON，8192 大概率不够，会在写完之前被截断
#   （跟 _call_deepseek 里记的那个推理模型的坑同源）。所以显式关掉思考、把上限抬到 16000
#   ——16000 是非流式请求的安全值，再往上容易顶到 SDK 的 HTTP 超时。
# - Haiku 4.5 默认不思考，不用传 thinking，给同样的上限就够。
#
# price_in / price_out 是每百万 token 的美元单价，给 llm_calls 流水算成本用。原来这两个
# 数字是写在 note 里的自然语言（"约 $3/$15 每百万 token"），既算不了账，也已经漂了——
# sonnet-5 那条抄的其实是 Sonnet 4.6 的价格。现在价格只有这一处来源，note 只留定性描述，
# 前端下拉（static/common.js）渲染时自己从这两个字段拼。
MODELS = [
    {
        "id": "claude-sonnet-5",
        "label": "Claude Sonnet 5",
        "provider": "anthropic",
        "note": "质量最好，贵",
        "max_tokens": 16000,
        "no_thinking": True,
        "price_in": 2.0,
        "price_out": 10.0,
        # ★不支持采样参数★ Claude Sonnet 5 起 temperature/top_p/top_k 已从 API 移除，
        # 传了直接 400。这是项目的默认模型，所以"给两家都加个 temperature"这种改法
        # 会让全线分析当场崩掉——采样只能按模型开关，见 TASK_TEMPERATURE 上面的说明。
        "supports_sampling": False,
    },
    {
        "id": "claude-haiku-4-5",
        "label": "Claude Haiku 4.5",
        "provider": "anthropic",
        "note": "快且便宜",
        "max_tokens": 16000,
        "price_in": 1.0,
        "price_out": 5.0,
        "supports_sampling": True,
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek V4 Pro",
        "provider": "deepseek",
        # 原来这条 note 写"最省钱"是没查过真实定价时的想当然（"推理模型=省钱"的刻板印象）；
        # 2026-09-08 查了 DeepSeek 官方定价页（下面 price_in/price_out 的说明）后发现
        # 恰恰相反——Pro 比 Flash 贵 3 倍，改成如实描述，不再误导用户选贵的以为在省钱。
        "note": "推理模型，质量更好但比 Flash 贵",
        # 推理模型通常忽略甚至拒绝 temperature。没有实测过，先按不支持处理——
        # 宁可控不了温，也不要为了一个收益不确定的参数换来一个 400。
        "supports_sampling": False,
        # DeepSeek 定价比 Anthropic 复杂：区分 cache hit/miss 输入价，还有 UTC 高峰/非高峰
        # 两档（高峰 01:00-04:00 + 06:00-10:00，其余是非高峰，官方峰值价是非高峰的 2 倍）。
        # 这里跟 Anthropic 那几条一样只存一个数，本来就是估算不是账单（见 estimate_cost()
        # 的说明），选非高峰、cache miss 这一档——非高峰占一天 17/24 小时，是更常见的情况；
        # cache miss 是没命中缓存时的价格，用它做估算不会把成本算低于实际。命中缓存时
        # 实际花费会比这个估算数低（Pro 命中价 $0.022/M，只有 miss 价的 1/30）。价格来源：
        # https://api-docs.deepseek.com/quick_start/pricing/ （2026-09-08 查证，生效于
        # 2026-08-16 16:00 UTC 的 V4 价目表）。
        "price_in": 0.66,
        "price_out": 1.98,
    },
    {
        "id": "deepseek-v4-flash",
        "label": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "note": "更快更便宜，质量略低",
        "supports_sampling": True,
        # 同上一条 Pro 的说明：非高峰、cache miss 档，来源同一张官方价目表。
        "price_in": 0.22,
        "price_out": 0.66,
    },
]

# 每个功能位的采样温度。只对 supports_sampling=True 的模型生效。
#
# 先说清楚这一层的定位：**降温对"事实性幻觉"基本没用**，它管的是输出的稳定性和可复现性
# ——同一份 JD 分析两次给出的分数别差太多。真正防编造靠的是第4层那些确定性核查
# （resume_edits.py 的段落原文比对、analyzer.verify_mandatory_items 的 JD 原话核验）。
#
# 值本身还没有用 eval 量过，先按任务性质给一档保守的初值。验证方法见
# evals/run_analyzer_eval.py 的 --repeats（比较改动前后的分数离散度），注意 Sonnet 5
# 上做不了这个实验，结论只对 haiku / deepseek-flash 成立。
#
# 配置放在这里而不是 config.json：这是一个没人会去调、调错了还会悄悄变差的旋钮，
# 跟 max_tokens / no_thinking 硬编码在 MODELS 里是同一个先例。
TASK_TEMPERATURE = {
    "analysis": 0.0,             # 打分/抽取，要的是可复现的粗排
    "resume_review": 0.2,        # 诊断类；低温同时提高"照抄原文"的通过率（见 resume_edits）
    "preference_profile": 0.2,   # 归纳已有的否决理由，不需要创造
    "materials": 0.3,            # 一半事实改写、一半 cover letter 写作，压太低会写得干瘪
    "interview_prep": 0.4,
    "interview_practice": 0.3,   # 出题和评分共用这一个功能位，只能取折中值
    "interview_bank": 0.7,       # 创作性输出，压低会让十几道题的答案互相雷同
    "how_you_fit_agent": 0.0,    # 导航动作选择，要确定性
    # job_chat 刻意不设：自由对话降温只会让它更像机器人，这里的幻觉靠
    # ANTI_FABRICATION_NOTE 管，不靠采样参数。
}

# 可以各自配模型的功能位。分开配是因为它们的成本和质量要求差很多：匹配分析每条职位都要跑
# 一次（量大、便宜优先），面试准备、题库、简历体检都是一次生成看很久（质量优先）。
# materials（定制简历+cover letter）跟分析同源但拆成了单独一次调用（见 analyzer.generate_materials
# 顶部的说明），是用户点按钮才触发的一次性生成，质量优先，不该继续沾"分析"那档便宜模型的光。
# job_chat（职位详情页的自由问答）单独一档：追问式的小问题，回复要快、聊起来不心疼调用次数。
LLM_TASKS = (
    "analysis", "materials", "interview_prep", "interview_bank", "resume_review", "job_chat",
    "preference_profile", "interview_practice",
)

MODELS_BY_ID = {m["id"]: m for m in MODELS}


def get_model(model_id):
    """按 id 取模型定义，不认识就抛错。

    刻意不做"不认识就当成 anthropic 硬传过去"的兜底：模型名打错时，那样会一路走到
    API 才报一个看不懂的 404，而这里报错能直接告诉用户是配置写错了。
    """
    model = MODELS_BY_ID.get(model_id)
    if not model:
        raise RuntimeError(
            f"未知的模型：{model_id}（可选：{'、'.join(MODELS_BY_ID)}）"
        )
    return model


# ---------------------------------------------------------------- 调用流水埋点
#
# 目标：回答"这个月花了多少钱""哪个任务最容易失败""哪个模型慢"。埋点打在下面
# _call_anthropic / _call_deepseek / _call_anthropic_tools 三个函数里——它们是全项目
# 所有 LLM 调用的唯一收口点，一处埋点全覆盖。副作用是把 llm.chat 整个 mock 掉的测试
# 走不到这里，天然不写库，测试环境不用额外隔离。

# 本次调用属于哪个功能位（analysis / interview_prep / ...）。
#
# 为什么用 ContextVar 而不是给 chat()/ask() 加 task= 参数：那样要改 4 个对外入口 +
# 各功能模块约 10 个函数签名 + pipeline 里 11 个调用点，而且会打断 6 个测试文件——
# test_linkedin_tracker_sync.py 那些把 llm.chat 换成固定签名 lambda 的 monkeypatch
# 多一个 kwarg 就 TypeError。ContextVar 方案下那些测试一行都不用改。
#
# 线程安全：threading.Thread 起的新线程拿到的是全新 context，默认值 None，不会跨线程
# 串味；后台线程都会在自己线程里调 resolve_task()，标签正确。
_current_task = contextvars.ContextVar("llm_task", default=None)
_last_call_id = contextvars.ContextVar("llm_last_call_id", default=None)

# 单次用户操作（一次 HTTP 请求/一个后台线程任务）期间发生的调用，供路由层/后台任务
# 结束时取走汇总，回显给用户"这次操作花了多少钱"（见 start_usage_tracking /
# pop_usage_summary）。跟上面 _last_call_id 不是一回事：那个只记"最近一次"，用于
# JSON 解析失败时回填错误；这个是"从开始跟踪以来的全部"，因为一次操作经常不止一次
# LLM 调用（比如批量分类）。同样用 ContextVar：Flask 每个请求、每个后台线程都是
# 独立线程/独立 context，天然互不串味，不需要显式清理跨请求状态。
_usage_log = contextvars.ContextVar("llm_usage_log", default=None)

# 写流水的两个回调，由 app.py 启动时注册。
# 刻意不在这里 import models：llm.py 是"第5层地基"，全项目唯一不依赖任何项目内模块的
# LLM 适配器（见 spec/architecture.md），直接 import 会造出第5层反向依赖第4层。
_RECORDER = None
_ERROR_UPDATER = None


def set_recorder(fn, error_updater=None):
    """注册流水回调。fn 写新行，error_updater 把已有行改判为失败（JSON 解析失败时用）。
    都传 None 就是关掉，测试里这么用。"""
    global _RECORDER, _ERROR_UPDATER
    _RECORDER = fn
    _ERROR_UPDATER = error_updater


def current_task():
    return _current_task.get()


def temperature_for(model_id):
    """本次调用该传什么 temperature；None 表示**不传这个参数**（不是传 0）。

    两个前提都要成立才传：模型支持采样，且当前功能位在 TASK_TEMPERATURE 里配了温度。
    注册表里没有的模型（config.json 手写的）一律不传——宁可控不了温，也不要为一个
    收益不确定的参数换来一个 400。
    """
    if not MODELS_BY_ID.get(model_id or "", {}).get("supports_sampling"):
        return None
    return TASK_TEMPERATURE.get(_current_task.get())


@contextlib.contextmanager
def task_context(name):
    """显式标注一段代码属于哪个功能位。

    正常路径不需要用它——resolve_task() 会顺手设好。这个上下文管理器是给不走
    resolve_task 的调用留的（目前只有 linkedin_how_you_fit.py 的 agent 兜底，
    它用默认模型、不查 llm_tasks）。
    """
    token = _current_task.set(name)
    try:
        yield
    finally:
        _current_task.reset(token)


def estimate_cost(model_id, input_tokens, output_tokens):
    """按注册表里的单价估算本次调用的美元成本。注册表里没有的模型（config.json 手写的）
    返回 None，token 数照记不误。

    是"估算"不是账单：DeepSeek 有缓存命中折扣价，按单价直算会高估。原始 usage 整块
    存在 llm_calls.usage_json 里，将来要精算可以回头重算。
    """
    spec = MODELS_BY_ID.get(model_id or "", {})
    price_in, price_out = spec.get("price_in"), spec.get("price_out")
    if price_in is None or price_out is None:
        return None
    return round(
        (input_tokens or 0) / 1_000_000 * price_in + (output_tokens or 0) / 1_000_000 * price_out,
        6,
    )


def start_usage_tracking():
    """开始收集"从现在起这个 context 里发生的 LLM 调用"，配合 pop_usage_summary() 用，
    给路由/后台任务结束时回显"这次操作花了多少钱"。

    调用方（路由函数、后台线程的入口函数）在触发业务逻辑之前调一次，业务逻辑内部
    不管调几次 LLM、经过多少层函数，都会被 _CallRecord.__exit__ 记下来——不需要
    每个功能模块自己知道"我被谁跟踪了"。忘记配对调用 pop_usage_summary() 没有副作用，
    只是这次的记录取不出来，不会内存泄漏或串到下一次请求（下一个请求/线程是全新
    context，默认值天然是 None）。"""
    _usage_log.set([])


def pop_usage_summary():
    """取走并清空自 start_usage_tracking() 以来记录的调用，按模型/费用汇总返回；
    没开跟踪、或跟踪期间一次 LLM 都没调用，返回 None（调用方应把 None 当"没有可展示的
    用量"处理，而不是当成 0 花费——两者含义不同，别混在一起显示成 $0.00）。"""
    log = _usage_log.get()
    _usage_log.set(None)
    if not log:
        return None
    input_tokens = sum(c["input_tokens"] or 0 for c in log)
    output_tokens = sum(c["output_tokens"] or 0 for c in log)
    costs = [c["cost_usd"] for c in log if c["cost_usd"] is not None]
    models = sorted({c["model"] for c in log if c["model"]})
    return {
        "calls": len(log),
        "ok": all(c["ok"] for c in log),
        "provider": log[0]["provider"] if len({c["provider"] for c in log}) == 1 else None,
        "models": models,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        # 只要有一次调用没定价（比如 DeepSeek），总成本就说不准，宁可报"未知"也不要
        # 悄悄把那次调用当 0 元算，展示层看到 None 应该显示"成本未知"而不是省略。
        "cost_usd": round(sum(costs), 6) if len(costs) == len(log) else None,
    }


def usage_text(summary):
    """把 pop_usage_summary() 的结果拼成一行人类可读文案（模型 + tokens + 成本），
    给 toast/通知文案直接拼接用。summary 为 None 时返回 None——调用方据此判断
    "这次操作根本没有可展示的 LLM 用量"，不要拼出"None"字样的文案。"""
    if not summary:
        return None
    if len(summary["models"]) == 1:
        model_label = MODELS_BY_ID.get(summary["models"][0], {}).get("label", summary["models"][0])
    elif summary["models"]:
        model_label = "/".join(summary["models"])
    else:
        model_label = "未知模型"
    tokens = summary["input_tokens"] + summary["output_tokens"]
    cost = f"${summary['cost_usd']:.4f}" if summary["cost_usd"] is not None else "成本未知"
    return f"{model_label} · {tokens:,} tokens · {cost}"


class _CallRecord:
    """包住一次真实 API 调用，退出时无论成功失败都落一行流水。

    硬约束：__exit__ 整段 try/except 且 return False。观测代码绝不能成为新的故障源——
    写库失败只打日志，原异常照常往上抛，调用方感知不到这里存在过。
    """

    def __init__(self, provider, model, max_tokens=None, prompt_chars=None):
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.prompt_chars = prompt_chars
        self.usage = None

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            usage = self.usage or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            cost_usd = estimate_cost(self.model, input_tokens, output_tokens)
            call_id = None
            if _RECORDER:
                call_id = _RECORDER(
                    task=_current_task.get(),
                    provider=self.provider,
                    model=self.model,
                    ok=0 if exc_type else 1,
                    error_type=exc_type.__name__ if exc_type else None,
                    error=str(exc) if exc else None,
                    duration_ms=int((time.monotonic() - self._t0) * 1000),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    max_tokens=self.max_tokens,
                    prompt_chars=self.prompt_chars,
                    usage_json=json.dumps(usage, ensure_ascii=False) if usage else None,
                )
            _last_call_id.set(call_id)
            log = _usage_log.get()
            if log is not None:
                log.append({
                    "provider": self.provider,
                    "model": self.model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost_usd,
                    "ok": not exc_type,
                })
        except Exception:
            logging.exception("llm_calls 埋点写库失败（不影响本次调用结果）")
        return False  # 绝不吞掉原异常


def _messages_chars(messages, system=None):
    """prompt 长度（字符数）。刻意只记长度不记原文：一次匹配分析的 prompt 是简历全文+
    JD全文，10-20KB，每天几十次调用，一年几百MB，而这份数据99%的时间没人看。长度足够
    回答"是不是 prompt 变长导致变贵/被截断"这类问题。"""
    total = len(system or "")
    for m in messages or ():
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def resolve(cfg):
    """从配置里解析出 (provider, model)。原来这两行在 pipeline.py 里重复了好几处，
    每新增一个调用 LLM 的功能就要再抄一遍，容易出现某处漏改导致 provider 和 model
    对不上（比如用 deepseek 的 provider 配 anthropic 的模型名）。

    这是**全局默认值**，功能位没单独配模型时回退到它（见 resolve_task）。
    """
    provider = cfg.get("llm_provider") or "anthropic"
    model = cfg.get("deepseek_model") if provider == "deepseek" else cfg.get("anthropic_model")
    return provider, model


def resolve_task(cfg, task):
    """解析某个功能位用的 (provider, model)。

    cfg["llm_tasks"][task] 有值就用它（provider 从模型注册表反查，不用用户自己保证两者
    对得上）；留空则回退到全局的 llm_provider/anthropic_model/deepseek_model，这样老的
    config.json 一个字不改也能照常跑。

    **刻意的副作用**：顺手把 task 记进 ContextVar，供调用流水和采样温度查（见上面
    _current_task 那段的说明）。一个 getter 带副作用是坏味道，这里认下来是因为替代方案
    要改十几个函数签名并打断 6 个测试。前提是"调 resolve_task 就等于马上要调 LLM"——
    目前 12 个调用点全部成立。如果将来有地方只是想知道用哪个模型、并不真的要调
    （比如设置页回显），标签会脏，那时候要么换个只读函数、要么显式清掉。
    """
    _current_task.set(task)
    model_id = ((cfg.get("llm_tasks") or {}).get(task) or "").strip()
    if not model_id:
        return resolve(cfg)
    model = get_model(model_id)
    return model["provider"], model["id"]


class LLMJsonError(ValueError):
    """模型返回的文本不是合法 JSON。

    继承 ValueError（json.JSONDecodeError 的父类）是为了兼容：上层 pipeline.py 到处是
    `except Exception` 兜底，原来接住的是 json.JSONDecodeError，换成这个照样接得住，
    调用方一行都不用改。

    比原来多带了 .raw（模型实际返回的前 500 字符）——原来只有 json 模块自带的
    "Expecting value: line 1 column 1 (char 0)" 这种，看不出模型到底吐了什么，
    排查只能靠猜。
    """

    def __init__(self, message, raw=None):
        super().__init__(message)
        self.raw = raw


def extract_json(text):
    """把模型回复解析成 JSON（容忍它习惯性套上的 ``` 代码块围栏）。

    解析失败时除了抛 LLMJsonError，还会把本次调用的流水行从 ok=1 改判成 ok=0：
    API 本身返回 200，埋点已经记成功了，但"返回的根本不是 JSON"恰恰是最该统计的
    失败模式，不回填的话"哪个任务最容易失败"就漏掉了最大的一类。
    """
    raw = text
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        # 只有开围栏没有闭围栏时 parts 只有 2 段，[1] 拿到的是围栏之后的全部内容，
        # 正好是想要的；这里只是别让段数不足时抛 IndexError。
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except ValueError as e:
        _mark_last_call_failed("LLMJsonError", f"{e}｜模型返回：{(raw or '')[:500]}")
        raise LLMJsonError(
            f"模型返回的不是合法 JSON：{e}｜实际返回（前500字）：{(raw or '')[:500]}",
            raw=raw,
        ) from e


def _mark_last_call_failed(error_type, error):
    """把本 context 里最近一次调用的流水改判为失败。找不到 recorder / 没有 call_id 就
    静默跳过；跟埋点本身一样，绝不因为观测失败影响主流程。"""
    call_id = _last_call_id.get()
    if not call_id or not _ERROR_UPDATER:
        return
    try:
        _ERROR_UPDATER(call_id, error_type=error_type, error=error)
    except Exception:
        logging.exception("llm_calls 失败回填写库失败（不影响本次调用结果）")


# 反幻觉标准文案：允许模型说"不知道"，而不是编答案。job_chat.py/analyzer.py 之类
# 通用场景直接引用这一句；interview.py/resume_review.py 里那些针对具体子任务的
# 更详细的"不要编造XX"提示（比如"不要编造候选人没有的经历"）比这句更精确，留在
# 各自的 PROMPT_TEMPLATE 里不动——通用文案负责兜底，不负责替换已经调好的专用提示。
ANTI_FABRICATION_NOTE = (
    "如果某个信息你不确定或者没有可靠依据，如实说不知道/未找到相关信息，"
    "不要编造或凭空猜测细节。"
)


def clamp(value, lo=0.0, hi=1.0, default=0.0):
    """把 LLM 给的分数裁到 [lo, hi] 范围内；不是数字（缺失/字符串/None）就返回
    default，不抛错——分数这类校验不该因为一次脏数据打断整个流程。

    interview.py 和 resume_review.py 原来各自定义了一份几乎一样的 _clamp/_clamp01，
    这里收成共享版本，default 参数支持 resume_review.py 那种"给 -1 当哨兵值，
    判断出来后再决定要不要用维度均值回填"的用法。
    """
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def require_dict(result, context="LLM 返回结果"):
    """校验 LLM 解析出来的 JSON 顶层是个 dict，不是就抛错。只兜底这一层最基础的
    形状；字段级的必填/取值范围校验业务差异太大（各模块需要哪些字段、报什么错
    文案都不一样），没有强行收进来，留在各自模块里写。"""
    if not isinstance(result, dict):
        raise RuntimeError(f"{context}不是有效的 JSON 对象：{result!r}")
    return result


def truncate(text, limit):
    """按字符数截断，超出部分换成省略提示。job_chat.py 原来自己发明了一份
    一模一样的实现，这里收成共享版本。sanitize_chat_history() 按对话轮数截断、
    linkedin_how_you_fit.py 按候选数量截断，跟这个不是同一个形状，不适合塞进
    同一个函数，各自留在原地。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def _call_anthropic(messages, model, system=None, max_tokens=None):
    from anthropic import Anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 ANTHROPIC_API_KEY 环境变量，无法调用Claude API做自动匹配分析。"
        )
    client = Anthropic(api_key=api_key)
    model_id = model or DEFAULT_ANTHROPIC_MODEL
    # 注册表里没有的模型名也放行（config.json 里可以手写别的），只是拿不到下面这两项调优。
    spec = MODELS_BY_ID.get(model_id, {})
    kwargs = {
        "model": model_id,
        # 调用方传 None 表示"不指定上限"，但 Anthropic 的 API 强制要求这个参数，只能给个值：
        # 优先用模型自己那档（见 MODELS 上面的注释），否则退到全局默认。
        "max_tokens": max_tokens or spec.get("max_tokens") or DEFAULT_ANTHROPIC_MAX_TOKENS,
        "messages": messages,
    }
    # 关掉自适应思考。Claude Sonnet 5 起，不传 thinking 就是**默认开着**思考，而 max_tokens
    # 是「思考 + 正文」共用的，思考吃掉一大半之后正文写到一半就被截断
    # ——和 _call_deepseek 里记的那个坑一模一样。本项目要的是一整段 JSON，不需要它边想边写。
    if spec.get("no_thinking"):
        kwargs["thinking"] = {"type": "disabled"}
    # 只有注册表里标了 supports_sampling 的模型才带这个参数。Sonnet 5 传了会 400，
    # 而它正是默认模型——这行判断是防 400 的关键，不是可选优化。
    temperature = temperature_for(model_id)
    if temperature is not None:
        kwargs["temperature"] = temperature
    # Anthropic 的 system prompt 是独立的顶层参数，不像 OpenAI 兼容接口那样放在
    # messages 里当第一条消息。
    if system:
        kwargs["system"] = system
    with _CallRecord(
        "anthropic", model_id, kwargs["max_tokens"], _messages_chars(messages, system)
    ) as rec:
        resp = client.messages.create(**kwargs)
        rec.usage = _anthropic_usage(resp)
        text = "".join(block.text for block in resp.content if hasattr(block, "text"))
        # 截断也要记一行（ok=0）：这是本项目历史上最容易踩的坑，之前完全没有可查询的数据。
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise RuntimeError(TRUNCATED_HINT + f"（本次 max_tokens={kwargs['max_tokens']}）")
    return text


def _anthropic_usage(resp):
    """从 Anthropic 响应里取 token 用量。resp.usage 原来全项目从来没被读过。

    缓存相关的两个字段现在恒为 0（项目没开 prompt caching），但一并存进 usage_json，
    将来开了缓存不用回填历史数据。
    """
    usage = getattr(resp, "usage", None)
    if not usage:
        return None
    out = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    for extra in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, extra, None)
        if value is not None:
            out[extra] = value
    return out


def _call_deepseek(messages, model, system=None, max_tokens=None):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY 环境变量，无法调用DeepSeek API做自动匹配分析。"
        )
    # OpenAI 兼容接口：system 作为 messages 的第一条。
    full_messages = ([{"role": "system", "content": system}] if system else []) + list(messages)
    payload = {
        "model": model or DEFAULT_DEEPSEEK_MODEL,
        "messages": full_messages,
        "stream": False,
    }
    # 默认不传 max_tokens，让 DeepSeek 用它自己的上限。这不是省事，是踩过的坑：
    # deepseek-v4-pro 是推理模型，max_tokens 的额度是「内部推理 + 正文输出」共用的，
    # 实测一次面试准备生成光推理就烧掉 6675 token、正文还要 5878——传 8192 的话
    # 推理直接吃掉 8143，正文只剩 49 个 token，返回一段截断的半截 JSON，
    # 报出来是「Unterminated string」这种跟真实原因八竿子打不着的错。
    # 不传的时候实测 finish_reason=stop、完整输出，跟这个模块从 analyzer.py 抽出来
    # 之前的原始行为一致。
    if max_tokens:
        payload["max_tokens"] = max_tokens
    temperature = temperature_for(payload["model"])
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with _CallRecord(
        "deepseek",
        payload["model"],
        payload.get("max_tokens"),
        _messages_chars(messages, system),
    ) as rec:
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"DeepSeek API调用失败（HTTP {e.code}）：{e.read().decode('utf-8', 'ignore')}")
        rec.usage = _deepseek_usage(data)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            # 显式识别截断，而不是把半截内容丢给 json.loads 去报一个看不懂的解析错。
            # usage 现在同时喂给流水（上面那行），"推理烧光额度"这个坑第一次有数据可查。
            usage = data.get("usage") or {}
            detail = (
                f"（completion_tokens={usage.get('completion_tokens')}，"
                f"其中推理 {(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}）"
            )
            raise RuntimeError(TRUNCATED_HINT + detail)
        content = choice["message"]["content"]
    return content


def _deepseek_usage(data):
    """DeepSeek 的 usage 整块存下来（含 completion_tokens_details.reasoning_tokens 和
    缓存命中数），另外把两个 token 数归一成跟 Anthropic 一样的字段名，方便统一聚合。"""
    usage = (data or {}).get("usage")
    if not usage:
        return None
    out = dict(usage)
    out["input_tokens"] = usage.get("prompt_tokens")
    out["output_tokens"] = usage.get("completion_tokens")
    return out


def _call_anthropic_tools(messages, tools, model, system=None, max_tokens=None):
    from anthropic import Anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 ANTHROPIC_API_KEY 环境变量，无法调用Claude API做自动匹配分析。"
        )
    client = Anthropic(api_key=api_key)
    model_id = model or DEFAULT_ANTHROPIC_MODEL
    spec = MODELS_BY_ID.get(model_id, {})
    kwargs = {
        "model": model_id,
        "max_tokens": max_tokens or spec.get("max_tokens") or DEFAULT_ANTHROPIC_MAX_TOKENS,
        "messages": messages,
        "tools": tools,
        # 强制模型每轮必须选一个工具，不允许只回文字不调用——这个循环里"什么都不做
        # 只说话"不是一个有意义的状态，agent 存在的意义就是每轮都要给出下一步动作。
        "tool_choice": {"type": "any"},
    }
    if spec.get("no_thinking"):
        kwargs["thinking"] = {"type": "disabled"}
    temperature = temperature_for(model_id)
    if temperature is not None:
        kwargs["temperature"] = temperature
    if system:
        kwargs["system"] = system
    with _CallRecord(
        "anthropic", model_id, kwargs["max_tokens"], _messages_chars(messages, system)
    ) as rec:
        resp = client.messages.create(**kwargs)
        rec.usage = _anthropic_usage(resp)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise RuntimeError(TRUNCATED_HINT + f"（本次 max_tokens={kwargs['max_tokens']}）")
    return resp


def _tools_to_openai(tools):
    """把 Anthropic 风格的 tools schema（{name, description, input_schema}）转成
    DeepSeek/OpenAI 兼容 function calling 要的形状（{type:function, function:{...,
    parameters}}）。两边字段名的差异收在这里，调用方只需要写一份 tools 定义。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _call_deepseek_tools(messages, tools, model, system=None, max_tokens=None):
    """DeepSeek 版本的单轮工具调用。tool_choice="required" 对应 Anthropic 那边的
    tool_choice={"type":"any"}——同样强制模型每轮必须选一个工具。返回原始的
    assistant message dict（含 tool_calls），交给 chat_tool_step() 统一解析。

    thinking 显式关掉（2026-08-30，实测踩出来的坑）：DeepSeek 的 v4 系列模型只要带
    tools 就默认开 thinking mode，这时候 API 直接拒绝 tool_choice="required"（400:
    "Thinking mode does not support this tool_choice"），跟具体哪个 v4 模型无关，
    pro/flash 都一样会报。不传这个参数就没法用强制工具调用，等价于 Anthropic 那边的
    no_thinking，理由相同：这里要的是确定性的下一步动作，不需要模型边想边写。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY 环境变量，无法调用DeepSeek API做工具调用。"
        )
    full_messages = ([{"role": "system", "content": system}] if system else []) + list(messages)
    payload = {
        "model": model or DEFAULT_DEEPSEEK_TOOL_MODEL,
        "messages": full_messages,
        "tools": _tools_to_openai(tools),
        "tool_choice": "required",
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    temperature = temperature_for(payload["model"])
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with _CallRecord(
        "deepseek", payload["model"], payload.get("max_tokens"), _messages_chars(messages, system)
    ) as rec:
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"DeepSeek API调用失败（HTTP {e.code}）：{e.read().decode('utf-8', 'ignore')}")
        rec.usage = _deepseek_usage(data)
        message = data["choices"][0]["message"]
    return message


def chat_tool_step(messages, tools, provider="anthropic", model=None, system=None, max_tokens=None):
    """单轮工具调用。默认 provider="anthropic"；provider="deepseek" 时走 DeepSeek
    的 OpenAI 兼容 function calling（唯一消费者 linkedin_how_you_fit.py 的 agent
    兜底按当前有哪个 API key 来选，见调用方）。

    messages 是完整对话历史，tools 统一用 Anthropic 风格的
    {name, description, input_schema} 定义——provider="deepseek" 时内部会转换成
    对方要的形状，调用方不用为每家 provider 各写一份 tools。返回：
    {"stop_reason": str, "assistant_message": dict, "tool_use": {"id","name","input"} | None}
    - assistant_message 是这一轮模型的原始回复，调用方原样存回 messages 才能续接
      下一轮对话（Anthropic/DeepSeek 内部形状不同，调用方不需要关心，也不要自己拼）。
    - tool_use 是本轮模型选中的工具调用；两家都强制模型每轮必须选一个工具，正常
      情况下不会是 None——为 None 说明模型没有按预期调用工具，调用方应该当成异常
      处理，不能假设一定有。
    """
    if provider == "deepseek":
        message = _call_deepseek_tools(messages, tools, model, system=system, max_tokens=max_tokens)
        tool_calls = message.get("tool_calls") or []
        tool_use = None
        if tool_calls:
            call = tool_calls[0]
            tool_use = {
                "id": call["id"],
                "name": call["function"]["name"],
                "input": json.loads(call["function"]["arguments"] or "{}"),
            }
        return {
            "stop_reason": "tool_calls" if tool_calls else "stop",
            "assistant_message": message,
            "tool_use": tool_use,
        }
    if provider != "anthropic":
        raise RuntimeError(f"chat_tool_step 不支持的 provider：{provider}（应为 anthropic 或 deepseek）")
    resp = _call_anthropic_tools(messages, tools, model, system=system, max_tokens=max_tokens)
    tool_use = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            tool_use = {"id": block.id, "name": block.name, "input": block.input}
            break
    return {
        "stop_reason": resp.stop_reason,
        "assistant_message": {"role": "assistant", "content": resp.content},
        "tool_use": tool_use,
    }


def tool_result_message(provider, tool_use_id, content):
    """把一次工具执行结果包成下一轮要塞回 messages 的那条消息。Anthropic 用
    tool_result content block，DeepSeek/OpenAI 兼容用 role="tool"——两家字段名
    的差异收在这里，调用方不用关心当前 provider 具体是哪家。"""
    if provider == "deepseek":
        return {"role": "tool", "tool_call_id": tool_use_id, "content": content}
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}


def chat(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    """messages: [{"role": "user"|"assistant", "content": str}]，返回模型回复的纯文本。

    max_tokens=None 表示"不指定上限"：DeepSeek 直接不传这个参数（用它自己的上限），
    Anthropic 因为 API 强制要求，退到 DEFAULT_ANTHROPIC_MAX_TOKENS。
    """
    if provider == "deepseek":
        return _call_deepseek(messages, model, system=system, max_tokens=max_tokens)
    if provider == "anthropic":
        return _call_anthropic(messages, model, system=system, max_tokens=max_tokens)
    raise RuntimeError(f"未知的 llm_provider：{provider}（应为 anthropic 或 deepseek）")


def chat_json(messages, provider="anthropic", model=None, system=None, max_tokens=None):
    """同 chat()，但把回复按 JSON 解析后返回（容忍模型习惯性套上的 ``` 代码块围栏）。"""
    raw = chat(messages, provider=provider, model=model, system=system, max_tokens=max_tokens)
    return extract_json(raw)


def ask(prompt, provider="anthropic", model=None, system=None, max_tokens=None):
    """单轮便捷包装：一条 user 消息进、纯文本出。"""
    return chat(
        [{"role": "user", "content": prompt}],
        provider=provider,
        model=model,
        system=system,
        max_tokens=max_tokens,
    )


def ask_json(prompt, provider="anthropic", model=None, system=None, max_tokens=None):
    """单轮便捷包装：一条 user 消息进、解析好的 JSON 出。"""
    return extract_json(ask(prompt, provider=provider, model=model, system=system, max_tokens=max_tokens))
