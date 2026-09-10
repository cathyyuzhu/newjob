"""LLM-as-judge 质性评审：规则断言（run_analyzer_eval.py 的主流程）只能查"有没有
遵守明文规则"——阈值、分类标签、短语出现、数字集合差，全是机械可查的。查不了
"分析理由讲得通不通""cover letter 写得好不好"这类质性质量，那部分原来只能靠人翻
reports/*.md 里的原始输出。这个模块把"人翻报告"的第一遍预读自动化：拿另一个模型
按维度 rubric 打分并列关注点，产出的仍然是信息性结果，最终判断权在人。

设计决策（都刻意为之）：

1. **judge 默认跨厂商**：被评模型是 anthropic 就用 deepseek 当 judge，反之亦然——
   模型给自家输出打分存在自评偏差。没有另一家 API key 时退回同厂商默认模型（调用
   方打印提醒），配 --judge-model 可显式指定。

2. **每条 fixture 只评第 1 次成功输出**：repeats + 离散度闸门已经在规则层面过滤了
   抽样噪声，judge 只做质性抽查；每条都评会把 judge 调用费翻 repeats 倍。

3. **只评 analyze / materials 两种 kind**：classify_companies（分类对错）和
   overview_honesty（特定短语出现与否）已经被确定性断言完整覆盖，judge 再评一遍
   只会增加成本和噪声。

4. **纯信息性，不进 exit code**：judge 本身也是 LLM，它的评分同样有噪声和偏差，
   拿它当硬闸门等于用未经校准的模型去校准另一个模型。它该做的是把"值得人看的
   输出"顶到显眼位置。

5. **输入截断**：JD/简历全文加模型输出可能很长，按字符数截断（llm.truncate）——
   judge 评的是质性印象，不是逐字核对（逐字核对是 checks_fabrication 那类确定性
   检查的活），截断损失的边界信息不影响 1-5 分的判断。

fail-open 精神跟项目其它核查一致：judge 失败/返回不合规只在结果里记 ERROR，绝不
影响 fixture 的规则判定。
"""
import json
import os

import llm

# 可评审的 fixture kind。JUDGE_KINDS 之外的（classify_companies / overview_honesty）
# 已有确定性断言覆盖，不浪费 judge 调用。
JUDGE_KINDS = ("analyze", "materials")

# 各 kind 的评分维度：(维度名, 判据, 打分锚点)。judge 返回缺哪个维度都会被记进
# concerns（见 parse_judge_verdict）——宁可标注缺评，也不要静默按满分算。
JUDGE_RUBRICS = {
    "analyze": (
        ("evidence_grounding",
         "分析列出的匹配/未达标条目是否能在JD和简历原文里找到具体依据，而不是泛泛而谈",
         "5=条条有据可查；3=多数有据但夹带空泛归纳；1=大量凭空断言"),
        ("score_consistency",
         "分数与列出的证据是否自洽（缺口多却打高分、缺口少却打极低分都算不自洽）",
         "5=分数与证据完全自洽；3=方向对但幅度可疑；1=明显矛盾"),
        ("honesty",
         "有无无依据断言或夸大，含公司简介是否在对公司无可靠认知时编造",
         "5=全部断言有依据或如实说不知道；1=多处无依据断言"),
    ),
    "materials": (
        ("relevance",
         "cover letter 与段落改写是否紧扣JD的具体要求，而不是通用套话",
         "5=逐点对应JD要求；3=部分对应、有明显套话；1=基本是通用模板"),
        ("authenticity",
         "是否只基于简历真实经历陈述，无夸大或无中生有的经历描述（数字编造另有确定性"
         "程序检查，这里查措辞层面的吹嘘）",
         "5=全部陈述有简历依据；3=个别措辞拔高；1=编造经历/能力"),
        ("professionalism",
         "语言专业度与结构（英文表达、段落组织、简洁度）",
         "5=专业简洁结构清晰；3=可用但有冗余或生硬；1=明显不通顺"),
    ),
}

# 输入截断上限（字符）。judge 评质性印象，不需要全文逐字。
JD_LIMIT = 4000
RESUME_LIMIT = 4000
# 模型输出里列表字段的条数/长度上限，防个别 fixture 的输出把 prompt 撑爆。
BULLET_LIMIT = 12
BULLET_CHAR_LIMIT = 200
COVER_LETTER_LIMIT = 6000


def _clip_bullets(items):
    """列表字段统一裁剪：只留字符串、截断每条、限条数。非字符串条目（模型偶尔
    会输出数字/嵌套对象）直接丢弃——judge 不需要它们。"""
    out = []
    for item in (items or [])[:BULLET_LIMIT]:
        if isinstance(item, str):
            out.append(llm.truncate(item, BULLET_CHAR_LIMIT))
    return out


def _render_output(kind, output):
    """从模型原始输出里挑 judge 需要看的字段，裁剪后序列化。不是把整个 result
    dict 原样丢过去：prompt 模板要求的字段里混着 salary/location 这类 judge 用不上
    的长文本，挑字段能让评审聚焦在质性维度上。"""
    output = output or {}
    if kind == "materials":
        payload = {
            "needs_customization": output.get("needs_customization"),
            "resume_paragraph_edits": [
                {
                    "index": e.get("index"),
                    "original": llm.truncate(e.get("original"), BULLET_CHAR_LIMIT),
                    "text": llm.truncate(e.get("text"), BULLET_CHAR_LIMIT * 2),
                }
                for e in (output.get("resume_paragraph_edits") or [])[:BULLET_LIMIT]
                if isinstance(e, dict)
            ],
            "resume_optimization_bullets": _clip_bullets(output.get("resume_optimization_bullets")),
            "cover_letter": llm.truncate(output.get("cover_letter"), COVER_LETTER_LIMIT),
        }
    else:
        payload = {
            "company_overview": llm.truncate(output.get("company_overview"), 600),
            "job_content_bullets": _clip_bullets(output.get("job_content_bullets")),
            "skill_matched_bullets": _clip_bullets(output.get("skill_matched_bullets")),
            "skill_gap_bullets": _clip_bullets(output.get("skill_gap_bullets")),
            "requirement_items": [
                {"text": llm.truncate(i.get("text"), BULLET_CHAR_LIMIT), "is_gap": i.get("is_gap")}
                for i in (output.get("requirement_items") or [])[:BULLET_LIMIT]
                if isinstance(i, dict)
            ],
            "cognitive_match": output.get("cognitive_match"),
            "content_match": output.get("content_match"),
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_judge_messages(kind, *, company, title, jd_text, resume_text, output):
    """拼 judge 的输入。单条 user 消息（judge 是一问一答，不需要 system/multi-turn）。"""
    rubric_lines = []
    for name, criteria, anchors in JUDGE_RUBRICS[kind]:
        rubric_lines.append(f"- {name}：{criteria}（{anchors}）")
    rubric = "\n".join(rubric_lines)
    dim_names = "、".join(name for name, _, _ in JUDGE_RUBRICS[kind])
    text = f"""你是一个严格但公允的输出质量评审员。下面是一次 LLM 任务的全部输入与它的输出，请按给定维度逐项打 1-5 分并列出关注点。只依据给出的输入与输出本身判断，不要臆测输入之外的上下文。

## 评分维度（{kind}）
{rubric}

## 任务输入
公司：{company}
职位：{title}
JD正文（可能被截断）：
{llm.truncate(jd_text, JD_LIMIT)}

简历原文（可能被截断）：
{llm.truncate(resume_text, RESUME_LIMIT)}

## 被评审的模型输出（JSON，字段可能被截断/精简）
{_render_output(kind, output)}

## 输出格式
只输出一个JSON对象，不要有任何其他文字：
{{"dimensions": [{{"name": "<{dim_names} 之一>", "score": 1到5的整数, "reason": "一句话依据"}}], "concerns": ["具体问题，没有则空数组"], "overall": 1到5的整数}}
overall 是综合分。reason 和 concerns 用中文。
"""
    return [{"role": "user", "content": text}]


def parse_judge_verdict(raw, kind):
    """把 judge 返回解析归一成 {"dimensions": {name: {"score","reason"}},
    "concerns": [...], "overall": float}。

    宽进严出：分数越界/字符串数字都 clamp 进 [1,5]；缺 overall 用维度均值补；
    缺期望维度记进 concerns（缺评可见，不静默按满分）。所有解析失败（含顶层不是
    dict）统一抛 llm.LLMJsonError——require_dict 原生抛 RuntimeError，这里转一层，
    让"judge 返回不合规"跟其它"模型输出不合规"是同一个可捕获的异常类型。"""
    try:
        result = llm.require_dict(raw)
    except RuntimeError as e:
        raise llm.LLMJsonError(str(e), raw=str(raw)[:500]) from e
    dims_raw = result.get("dimensions")
    if not isinstance(dims_raw, list) or not dims_raw:
        raise llm.LLMJsonError(
            f"judge 返回缺少 dimensions 数组：{str(raw)[:200]}", raw=str(raw)[:500]
        )
    dimensions = {}
    for d in dims_raw:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        dimensions[name] = {
            "score": round(llm.clamp(d.get("score"), lo=1, hi=5, default=3), 3),
            "reason": str(d.get("reason") or "")[:300],
        }
    if not dimensions:
        raise llm.LLMJsonError(
            f"judge 返回的 dimensions 里没有任何有效条目：{str(raw)[:200]}", raw=str(raw)[:500]
        )

    concerns = []
    for c in result.get("concerns") or []:
        if isinstance(c, str) and c.strip():
            concerns.append(c.strip()[:300])
    expected = {name for name, _, _ in JUDGE_RUBRICS[kind]}
    for missing in sorted(expected - set(dimensions)):
        concerns.append(f"judge 未返回维度 {missing}，按缺评处理")

    overall = result.get("overall")
    if not isinstance(overall, (int, float)) or isinstance(overall, bool):
        overall = sum(d["score"] for d in dimensions.values()) / len(dimensions)
    return {
        "dimensions": dimensions,
        "concerns": concerns,
        "overall": round(llm.clamp(overall, lo=1, hi=5, default=3), 3),
    }


def judge_output(kind, *, company, title, jd_text, resume_text, output, provider, model):
    """调一次 judge 并解析。重试不在这里做——调用方（run_analyzer_eval）用
    collect_errors.with_retry 包住本函数，transient/rate_limited 才值得重试。

    task_context("judge")：llm_calls 流水的 task 标签记成 "judge"，跟被评审的那次
    调用区分开；"judge" 不在 TASK_TEMPERATURE 里，temperature 自然不会传。"""
    messages = build_judge_messages(
        kind, company=company, title=title, jd_text=jd_text, resume_text=resume_text, output=output,
    )
    with llm.task_context("judge"):
        raw = llm.chat_json(messages, provider=provider, model=model)
    return parse_judge_verdict(raw, kind)


def resolve_judge_target(provider, judge_model_arg, cfg):
    """决定 judge 用哪家 provider/哪个模型，返回 (provider, model)。

    - 显式 --judge-model：注册表里的模型按注册表反查 provider（跟 llm.resolve_task
      的思路一致，用户不用保证 provider 和 model 对得上）；不在注册表里的手写模型名
      按当前 provider 传。
    - 默认跨厂商（anthropic↔deepseek）避免自评偏差；缺另一家 API key 时退回同厂商
      默认模型（model 返回 None，由 llm 层取该厂商默认），调用方负责打印提醒。"""
    if judge_model_arg:
        spec = llm.MODELS_BY_ID.get(judge_model_arg)
        if spec:
            return spec["provider"], judge_model_arg
        return provider, judge_model_arg
    judge_provider = "deepseek" if provider == "anthropic" else "anthropic"
    key_var = "ANTHROPIC_API_KEY" if judge_provider == "anthropic" else "DEEPSEEK_API_KEY"
    if not os.environ.get(key_var):
        return provider, None
    judge_model = cfg.get("deepseek_model") if judge_provider == "deepseek" else cfg.get("anthropic_model")
    return judge_provider, judge_model
