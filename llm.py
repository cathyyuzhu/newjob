"""统一的 LLM 调用层：屏蔽 Anthropic / DeepSeek 两家 provider 的接口差异，对上层
只暴露"给一段对话、拿一段回复"。

原来这段适配器代码住在 analyzer.py 里，只支持"单条 prompt 字符串"——因为当时唯一的
用途就是 JD-简历匹配分析（一问一答）。面试准备模块里的模拟面试需要多轮对话
（messages 数组 + system prompt），而且 analyzer.py 从命名到文档都是"简历匹配"专属的，
不适合再往里塞第三方消费者，所以整体抽到这里，analyzer.py / interview.py / pipeline.py
共用。行为跟搬运前一致（包括报错文案）。
"""
import json
import os
import urllib.error
import urllib.request

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

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
MODELS = [
    {
        "id": "claude-sonnet-5",
        "label": "Claude Sonnet 5",
        "provider": "anthropic",
        "note": "质量最好，贵（约 $3/$15 每百万 token）",
        "max_tokens": 16000,
        "no_thinking": True,
    },
    {
        "id": "claude-haiku-4-5",
        "label": "Claude Haiku 4.5",
        "provider": "anthropic",
        "note": "快且便宜（约 $1/$5 每百万 token）",
        "max_tokens": 16000,
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek V4 Pro",
        "provider": "deepseek",
        "note": "推理模型，最省钱",
    },
    {
        "id": "deepseek-v4-flash",
        "label": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "note": "更快更便宜，质量略低",
    },
]

# 可以各自配模型的功能位。分开配是因为它们的成本和质量要求差很多：匹配分析每条职位都要跑
# 一次（量大、便宜优先），面试准备、题库、简历体检都是一次生成看很久（质量优先）。
# materials（定制简历+cover letter）跟分析同源但拆成了单独一次调用（见 analyzer.generate_materials
# 顶部的说明），是用户点按钮才触发的一次性生成，质量优先，不该继续沾"分析"那档便宜模型的光。
# job_chat（职位详情页的自由问答）单独一档：追问式的小问题，回复要快、聊起来不心疼调用次数。
LLM_TASKS = (
    "analysis", "materials", "interview_prep", "interview_bank", "resume_review", "job_chat",
    "preference_profile",
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
    """
    model_id = ((cfg.get("llm_tasks") or {}).get(task) or "").strip()
    if not model_id:
        return resolve(cfg)
    model = get_model(model_id)
    return model["provider"], model["id"]


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


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
    # Anthropic 的 system prompt 是独立的顶层参数，不像 OpenAI 兼容接口那样放在
    # messages 里当第一条消息。
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise RuntimeError(TRUNCATED_HINT + f"（本次 max_tokens={kwargs['max_tokens']}）")
    return text


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
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DeepSeek API调用失败（HTTP {e.code}）：{e.read().decode('utf-8', 'ignore')}")
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        # 显式识别截断，而不是把半截内容丢给 json.loads 去报一个看不懂的解析错。
        usage = data.get("usage") or {}
        detail = (
            f"（completion_tokens={usage.get('completion_tokens')}，"
            f"其中推理 {(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}）"
        )
        raise RuntimeError(TRUNCATED_HINT + detail)
    return choice["message"]["content"]


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
