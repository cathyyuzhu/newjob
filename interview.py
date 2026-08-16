"""面试准备：根据职位JD、简历、以及已经跑过的AI匹配分析结果，生成一份针对这家公司/
这个岗位的面试准备材料（公司背景研究、预测面试题+建议答法、缺口应对话术、反问清单）。

跟 analyzer.py 同层定位：只负责 prompt 和调 LLM，不碰数据库——读职位/读简历/写库都在
pipeline.py 里编排。
"""
import json

import llm

# 面试准备/题库的输出量都明显大于匹配分析（十几道题各带答题要点，或者几个完整的 STAR 故事）。
# 这里刻意不设 max_tokens 上限（None = 用各家 provider 自己的上限）——踩过的坑详见
# llm._call_deepseek 里的说明：推理模型的 max_tokens 是「推理 + 输出」共用额度，
# 设一个看起来很宽裕的 8192 反而会让推理吃光额度、正文只剩几十个 token。
PREP_MAX_TOKENS = None

PREP_PROMPT = """你是一个资深的面试辅导教练，帮候选人准备一场具体的面试。

## 候选人简历原文（每行前面的 [数字] 是段落索引，忽略即可，不要在输出里保留）：
{resume_text}

## 目标职位
公司：{company}
职位名称：{title}
面试轮次：{round_label}
JD正文：
{jd_text}

## 已有的JD-简历匹配分析结论（之前另一次分析已经算好的，直接采信，不要推翻重算）
{analysis_block}

## 任务
基于以上信息，生成这场面试的准备材料。要求：

1. **company_research 公司/业务背景研究**：基于你对这家公司的知识 + JD线索。
   - business：主营业务、所在赛道、商业模式，2-4句
   - role_context：这个岗位大概处在组织的什么位置、公司为什么现在要招这个人（从JD职责反推）
   - pain_points：面试中可以主动提及、显示你理解他们业务处境的痛点或机会点，3-5条
   - talking_points：能体现你做过功课的具体切入点（可以是产品、近期动向、行业变化），3-5条
   - 对这家公司确实不了解时，如实写"未找到该公司的可靠信息"，并把 pain_points/talking_points
     改成基于JD本身能推断出来的内容，不要编造公司近况、融资、财报之类的具体事实。

2. **questions 预测面试题**：10-15 道这场面试大概率会被问到的问题，覆盖以下 category 且分布合理：
   "行为面"、"业务/领域"、"技术/方法论"、"职业动机"。每题给出：
   - question：面试官会怎么问（用面试官的口吻）
   - why_asked：为什么这家公司/这个岗位大概率会问这题（结合JD里的职责或要求）
   - answer_points：答题要点列表。行为面的题按"情境 → 任务 → 行动 → 结果"的顺序组织要点，
     结果要点尽量落到简历里真实存在的数据上
   - resume_evidence：简历里可以直接拿来回答这题的那段经历/那个具体数字（一句话点명，
     没有合适的就填"简历中无直接对应经历，建议用可迁移经历回答"）

3. **gap_scripts 缺口应对话术**：针对上面匹配分析里所有 is_gap=true 的任职要求、以及
   "未达标的技能"，每一条都要有对应的应对话术（不要遗漏硬性要求的缺口）。每条给出：
   - gap：这个缺口是什么
   - likely_question：面试官大概率会怎么问到它
   - script：建议的回答话术。原则是——坦诚承认能力边界，不要假装有这个经验；给出最接近的
     可迁移经历；给出具体的补齐计划。绝对不要建议候选人编造或含糊其辞地蒙混过去
   - transferable：简历里能拿来顶这个缺口的最接近的经历

4. **questions_to_ask 反问面试官的问题**：5-8 个高质量的反问（团队构成、这个岗位的成功标准、
   决策链路、这个坑为什么空着、未来半年最重要的目标等），每条附 intent 说明问这题想探到什么。

5. **prep_checklist**：面试前要做的具体准备动作清单，5-8条，要可执行（"重读XX项目的数据"
   比"熟悉自己的项目"好）。

## 硬性约束
- 所有内容一律用中文输出。JD原文是英文时翻译成通顺自然的中文，不要逐字机翻；公司名、产品名、
  技术/工具名、职级缩写等专有名词保留英文原文。
- **答题要点和话术必须基于简历里真实存在的经历和数据，不能编造未发生的经历、不能夸大数据。**
  简历里没有的就明说没有，给可迁移的说法，而不是替候选人虚构一段经历。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "company_research": {{
    "business": "...",
    "role_context": "...",
    "pain_points": ["..."],
    "talking_points": ["..."]
  }},
  "questions": [
    {{"category": "行为面", "question": "...", "why_asked": "...", "answer_points": ["..."], "resume_evidence": "..."}}
  ],
  "gap_scripts": [
    {{"gap": "...", "likely_question": "...", "script": "...", "transferable": "..."}}
  ],
  "questions_to_ask": [{{"question": "...", "intent": "..."}}],
  "prep_checklist": ["..."]
}}
"""

_NO_ANALYSIS = "（这条职位还没有可用的匹配分析结果，请直接基于JD和简历自行判断哪些是缺口。）"


def build_analysis_block(analysis):
    """把追踪表里已有的匹配分析结论压成一段喂给 prompt 的纯文本。

    面试准备不重新做一遍JD解析：任职要求逐条是否达标、技能缺口、公司简介这些，AI匹配分析
    已经算过并存在追踪表里了。复用它既省 token，也保证两处结论不打架——不会出现匹配分析说
    "缺ITIL认证"、面试准备却当没这回事（用户会在同一个弹窗的两个 tab 里对着看）。
    """
    if not analysis:
        return _NO_ANALYSIS

    parts = []
    overview = analysis.get("company_overview")
    if overview:
        parts.append(f"【公司简介】{overview}")

    match = analysis.get("overall_match")
    if match is not None:
        try:
            parts.append(f"【总体匹配度】{round(float(match) * 100)}%")
        except (TypeError, ValueError):
            pass

    items = analysis.get("requirement_items") or []
    if items:
        lines = [
            f"- {'【未达标】' if item.get('is_gap') else '【已达标】'}{item.get('text', '')}"
            for item in items
            if item.get("text")
        ]
        if lines:
            parts.append("【任职要求逐条评估】\n" + "\n".join(lines))

    for key, label in (
        ("skill_matched_bullets", "【已匹配的技能】"),
        ("skill_gap_bullets", "【未达标的技能】"),
    ):
        bullets = [b for b in (analysis.get(key) or []) if b]
        if bullets:
            parts.append(label + "\n" + "\n".join(f"- {b}" for b in bullets))

    return "\n\n".join(parts) if parts else _NO_ANALYSIS


def generate_prep(
    company,
    title,
    jd_text,
    resume_text,
    analysis=None,
    round_label=None,
    model=None,
    provider="anthropic",
):
    if not resume_text:
        raise RuntimeError("未能读取基础简历文件，请确认 config.json 中 base_resume_path 是否正确。")
    if not (jd_text or "").strip():
        raise RuntimeError("这条职位没有JD正文，无法生成面试准备（可先点「重新获取」抓取JD）。")

    prompt = PREP_PROMPT.format(
        resume_text=resume_text,
        company=company,
        title=title,
        round_label=round_label or "未指定（按第一轮技术/业务面准备）",
        jd_text=jd_text,
        analysis_block=build_analysis_block(analysis),
    )
    result = llm.ask_json(prompt, provider=provider, model=model, max_tokens=PREP_MAX_TOKENS)
    if not isinstance(result, dict) or not result.get("questions"):
        raise RuntimeError("LLM 返回的面试准备内容不完整（缺少面试题列表），请重试一次。")
    return result


def dumps(content):
    return json.dumps(content, ensure_ascii=False)


# ---------------------------------------------------------------- 通用题库

# 同 PREP_MAX_TOKENS：不设上限，理由见上面那条注释。
BANK_MAX_TOKENS = None

# 起草和对话改写共用的写作规范。抽成常量是为了让"AI 起草出来的答案"和"跟 AI 聊完改写出来的
# 答案"长一个样——两边各写一份的话，聊几轮之后答案的结构就会跟题库里其它条目对不上。
_BANK_ANSWER_RULES = """- **中英文各一版**：`answer_zh` 是中文版，`answer_en` 是面向外企面试的
  自然英文表达（按英文母语者的说法组织，不是中文版的逐字直译，允许两版详略略有不同）。
- **必须分段**：答案不能是一整坨。段落之间空一行，在 JSON 字符串里就是写成 `\\n\\n`
  （JSON 里的换行必须用 `\\n` 转义，绝对不能直接敲真实换行，否则整段 JSON 解析不了）。
- **不能编造**：所有答案必须基于简历里真实存在的经历和数据，不能编造未发生的经历、
  不能夸大数据。简历信息不足以支撑某道题时，在答案里如实留出待补充的部分
  （用「（此处需你补充：…）」标注），不要虚构。
- 除了 `answer_en` 之外，其余内容一律用中文；公司名、产品名、技术/工具名等专有名词保留英文原文。"""

# 三份起草 prompt 共用的开头（简历 + 目标岗位 + 已有题目）。
_BANK_HEADER = """你是一个资深的面试辅导教练，帮候选人准备**跨公司通用**的面试答案库
（不针对某一家具体公司，是每场面试都用得上的那些标准问题）。

## 候选人简历原文（每行前面的 [数字] 是段落索引，忽略即可，不要在输出里保留）：
{resume_text}

## 候选人正在找的岗位方向
{target_roles}

## 这一类里题库已经有的题目
{existing_block}
"""

# 复用已有措辞的约束。只保留"照抄原文"这一条——原来还有一条"必须抄同一个类别下的那一条"，
# 是单次调用同时出三类题时模型串类别才需要的；现在一次调用只出一类，模型压根看不到别的
# 类别的题目，那条约束没有存在意义了。
_BANK_REUSE_RULE = """- **你要出的题只要跟上面「题库已经有的题目」是同一个意思，`question` 就必须
  一字不差地照抄那一条的原文**（连标点、空格、有没有句号、有没有"你"字都不能改）。系统靠问题
  文字判断这是不是同一道题，措辞变一点就会被当成新题、在题库里堆出两条意思重复的。真正的新题
  才自己起标题。"""

BANK_INTRO_PROMPT = _BANK_HEADER + """
## 任务
写一段 60-90 秒口播的自我介绍。结构分三段：

1. 当前角色、年限、擅长的方向
2. 最能代表能力的 1-2 段经历，带具体成果数据
3. 为什么在看上面那个方向的机会

要口语化、能直接念出来，不要写成书面的简历摘要。

## 硬性约束
{answer_rules}

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "answer_zh": "第一段…\\n\\n第二段…\\n\\n第三段…",
  "answer_en": "First paragraph…\\n\\nSecond paragraph…\\n\\nThird paragraph…"
}}
"""

BANK_COMMON_PROMPT = _BANK_HEADER + """
## 任务
出 8-12 道几乎每场面试都会遇到的通用问题，结合这份简历给出候选人**自己的**答案
（不是通用模板套话）。要覆盖这些方向（可按简历情况增减）：
为什么离开上一家 / 为什么想来这个方向 / 未来3-5年职业规划 / 最大的优势 / 最大的短板 /
讲一次失败或做错的决策 / 你怎么定义这个岗位做得好 / 期望薪资怎么谈 / 还有哪些在看的机会。

每道题的答案分 2-4 段：第一段先把结论或态度说清楚，后面几段展开具体经历和数据。
整体长度控制在口头回答 60-90 秒的量。

## 硬性约束
{answer_rules}
{reuse_rule}

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "items": [
    {{"question": "为什么离开上一家？", "answer_zh": "…\\n\\n…", "answer_en": "…\\n\\n…"}}
  ]
}}
"""

BANK_STAR_PROMPT = _BANK_HEADER + """
## 任务
从简历里提炼 3-5 个可以反复复用的完整故事。尽量覆盖不同类型：从0到1做成一件事 /
推动跨部门协作或说服他人 / 处理冲突或危机 / 数据驱动的决策 / 一次失败和从中学到什么。

`question` 写"面试官通常会用什么问题引出这个故事"，答案写故事本身。

每个故事**固定分成四段**，每段以下面的标签开头：
- 中文版：「情境：」「任务：」「行动：」「结果：」
- 英文版：`Situation:` `Task:` `Action:` `Result:`

结果那一段尽量落到简历里真实存在的数据上。

## 硬性约束
{answer_rules}
{reuse_rule}

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "items": [
    {{"question": "讲一个你从0到1做成一件事的例子",
      "answer_zh": "情境：…\\n\\n任务：…\\n\\n行动：…\\n\\n结果：…",
      "answer_en": "Situation: …\\n\\nTask: …\\n\\nAction: …\\n\\nResult: …"}}
  ]
}}
"""

# 起草分三次调用，一次只出一个类别。这么拆的原因见 spec/tech-solution.md：双语+分段之后
# 单次输出量会翻倍，一次性出完容易顶到 max_tokens 上限被截断；分开跑还有两个好处——
# 一段失败不影响另外两段，而且每段跑完能立刻入库让用户看到进度（见 pipeline.generate_bank_draft）。
BANK_SECTIONS = (
    {"key": "self_intro", "label": "自我介绍", "prompt": BANK_INTRO_PROMPT},
    {"key": "common", "label": "通用问题", "prompt": BANK_COMMON_PROMPT},
    {"key": "star_story", "label": "STAR 故事库", "prompt": BANK_STAR_PROMPT},
)

SELF_INTRO_QUESTION = "自我介绍（60-90秒）"

_NO_EXISTING = "（题库现在是空的，这是第一次起草，下面那条「照抄原文」的约束不用管。）"

# 喂回给 prompt 时每个 category 的标题，要跟"任务"那三段的字段名对得上，模型才知道
# 哪一条属于哪个字段。
_BANK_CATEGORY_LABELS = (
    ("self_intro", "self_intro 自我介绍"),
    ("common", "items 通用问题"),
    ("star_story", "star_stories 核心故事库"),
)


def build_existing_block(items):
    """把题库里已有的问题**按类别分组**列成一段喂给 prompt。

    为什么要喂回去：合并是按问题文字判重的（见 models.replace_ai_bank_items），而模型每次
    自己起的标题都不一样——「为什么离开上一家？」下一轮就写成「为什么离开上一家 / 这次为什么
    想看外部机会？」，归一化也救不了这种改写，结果"补充新题"变成"堆重复题"。把已有题目连
    原文措辞一起给它看、让它复用，是唯一能从源头解决改写的办法。

    为什么要分组：实测只给一个不分类别的大列表时，模型会**串类别**——把 common 里那条
    「讲一次你失败或做错决策的经历。」的措辞拿去当 star_story 的标题，同时给 common 另起
    一个「讲一次失败或做错决策的经历。」（少一个"你"），于是两个类别各多出一条重复。
    "失败/做错决策"这道题本来就同时存在于通用题和故事库里，不告诉它哪条属于哪个字段，
    它就只能猜。

    items 接受 [{category, question}]（直接喂 models.list_bank_items() 的结果即可）。
    """
    grouped = {}
    for it in items or []:
        q = ((it.get("question") if isinstance(it, dict) else it) or "").strip()
        cat = it.get("category") if isinstance(it, dict) else None
        if q:
            grouped.setdefault(cat, [])
            if q not in grouped[cat]:
                grouped[cat].append(q)

    parts = []
    for key, label in _BANK_CATEGORY_LABELS:
        if grouped.get(key):
            parts.append(f"【{label}】\n" + "\n".join(f"- {q}" for q in grouped[key]))
    # 兜底：category 不在已知三类里的（理论上不会有），单独列出来别丢了
    for key, qs in grouped.items():
        if key not in dict(_BANK_CATEGORY_LABELS) and qs:
            parts.append("【其它】\n" + "\n".join(f"- {q}" for q in qs))
    return "\n\n".join(parts) if parts else _NO_EXISTING


def _format_roles(target_roles):
    return "、".join(r for r in (target_roles or []) if r) or "（未指定，请从简历本身推断）"


def get_bank_section(section_key):
    for section in BANK_SECTIONS:
        if section["key"] == section_key:
            return section
    raise RuntimeError(f"未知的题库类别：{section_key}")


def generate_bank_section(
    section_key, resume_text, target_roles=None, existing_items=None, model=None, provider="anthropic"
):
    """起草**一个类别**的题库条目。返回可以直接喂给 models.replace_ai_bank_items() 的扁平
    列表 [{category, question, answer, answer_en}]——自我介绍在 prompt 输出里是单个对象、
    另外两类是数组，这里统一拍平，让存储层只认一种结构。

    existing_items 应该只传**这个类别**已有的条目（调用方过滤好），用来让模型复用已有措辞、
    不要每轮换个说法（见 build_existing_block）。
    """
    if not resume_text:
        raise RuntimeError("未能读取基础简历文件，请确认 config.json 中 base_resume_path 是否正确。")

    section = get_bank_section(section_key)
    prompt = section["prompt"].format(
        resume_text=resume_text,
        target_roles=_format_roles(target_roles),
        existing_block=build_existing_block(existing_items),
        answer_rules=_BANK_ANSWER_RULES,
        reuse_rule=_BANK_REUSE_RULE,
    )
    result = llm.ask_json(prompt, provider=provider, model=model, max_tokens=BANK_MAX_TOKENS)
    if not isinstance(result, dict):
        raise RuntimeError(f"LLM 返回的「{section['label']}」内容格式不对，请重试一次。")

    items = []
    if section_key == "self_intro":
        if result.get("answer_zh") or result.get("answer_en"):
            items.append(
                {
                    "category": "self_intro",
                    "question": SELF_INTRO_QUESTION,
                    "answer": result.get("answer_zh"),
                    "answer_en": result.get("answer_en"),
                }
            )
    else:
        for it in result.get("items") or []:
            question = (it.get("question") or "").strip()
            if question:
                items.append(
                    {
                        "category": section_key,
                        "question": question,
                        "answer": it.get("answer_zh"),
                        "answer_en": it.get("answer_en"),
                    }
                )

    if not items:
        raise RuntimeError(f"LLM 没有返回任何「{section['label']}」内容，请重试一次。")
    return items


# ------------------------------------------------------- 题库：跟 AI 对话完善答案

# 对话是同步返回的（不像起草那样后台跑 + 轮询）：单轮只改一道题的一个语言版本，输出量比
# 起草小一个数量级，等待在十几秒到一分钟量级，再套一层后台线程 + 状态轮询是过度设计。
BANK_CHAT_MAX_TOKENS = None

# 前端不落库、每轮把完整历史发回来，所以这里必须自己设上限，否则聊得越久 token 烧得越多。
BANK_CHAT_HISTORY_LIMIT = 20

_LANG_LABELS = {"zh": "中文版", "en": "英文版（English）"}

BANK_CHAT_SYSTEM = """你是一个资深的面试辅导教练，正在陪候选人逐字打磨面试题库里的**某一道题**的答案。

## 候选人简历原文（每行前面的 [数字] 是段落索引，忽略即可，不要在输出里保留）：
{resume_text}

## 候选人正在找的岗位方向
{target_roles}

## 正在打磨的题目
{question}

## 这道题当前的{lang_label}答案
{current_answer}

## 你的工作方式
- 候选人会告诉你哪里不满意（太长、太空、想突出某段经历、想换个角度…）。你要**改写整版答案**，
  而不是只给零散建议——他会一键把你给的版本替换进去。
- 每轮都基于「当前答案」和之前几轮的共识来改，不要推翻重来，也不要把上一轮已经改好的地方改回去。
- 这一轮只需要改**{lang_label}**这一版，不要输出另一种语言的版本。
- 如果候选人这轮只是在问问题、或者你认为不需要改动，就把 `answer` 给 null，只在 `reply` 里回答他。

## 硬性约束
- **必须分段**：段落之间空一行，在 JSON 字符串里写成 `\\n\\n`（JSON 里的换行必须用 `\\n` 转义，
  绝对不能直接敲真实换行，否则整段 JSON 解析不了）。
- **不能编造**：只能用简历里真实存在的经历和数据，不能编造未发生的经历、不能夸大数据。
  需要候选人自己补充的信息，用「（此处需你补充：…）」标注出来问他，不要替他虚构。
- 英文版要写成英文母语者的自然表达，不是中文的逐字直译。
- `reply` 用中文写，一到两句话说清这轮改了什么、为什么这么改。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "reply": "把开头的背景压成一句，把三个结果数据提到前面，整体从 100 秒压到 70 秒左右。",
  "answer": "改写后的完整答案…\\n\\n…（不需要改写时给 null）"
}}
"""

BANK_ASSISTANT_SYSTEM = """你是一个资深的面试辅导教练，正在帮候选人**整体检查**他的通用面试题库。

## 候选人简历原文（每行前面的 [数字] 是段落索引，忽略即可，不要在输出里保留）：
{resume_text}

## 候选人正在找的岗位方向
{target_roles}

## 题库现状（每条答案可能被截断，够你判断即可）
{bank_block}

## 你的工作方式
你负责的是**跨题目的诊断**，典型问题：几个 STAR 故事是不是在讲同一件事、覆盖面缺哪一类
（比如没有"处理冲突"的故事）、哪几道题答得空洞没有数据、自我介绍和通用题的说法有没有互相打架、
按他找的岗位方向还缺哪些常见题。

**你不负责改写具体答案**。需要动某一道题时，明确点出是哪一道题（用题目原文），告诉他去那道题
自己的对话框里改——那边的教练看得到完整答案，改出来的版本可以一键替换。

## 硬性约束
- 只能基于上面的简历和题库现状判断，不要编造候选人没有的经历。
- 用中文回答，说话直接、给具体的下一步动作，不要写成泛泛的鼓励。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{"reply": "你的诊断和建议"}}
"""

_EMPTY_ANSWER = "（这道题现在还没有答案，需要你从零帮他写一版。）"


def sanitize_chat_history(history):
    """把前端传来的对话历史洗成 llm.chat() 能吃的样子。

    对话不落库、每轮由前端把完整历史发回来，所以这里既要防脏数据（角色写错、content 不是
    字符串），也要卡长度——聊得越久历史越长，不截断的话 token 会一路涨上去。只留最后
    BANK_CHAT_HISTORY_LIMIT 条：面试答案的打磨基本都在最近几轮里收敛，更早的上下文
    在 system prompt 里的「当前答案」中已经体现了。
    """
    clean = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            clean.append({"role": role, "content": content})
    return clean[-BANK_CHAT_HISTORY_LIMIT:]


def _chat(system, history, message, model, provider):
    messages = sanitize_chat_history(history) + [{"role": "user", "content": message}]
    result = llm.chat_json(
        messages, provider=provider, model=model, system=system, max_tokens=BANK_CHAT_MAX_TOKENS
    )
    if not isinstance(result, dict) or not (result.get("reply") or "").strip():
        raise RuntimeError("LLM 返回的对话内容不完整（缺少 reply），请再说一次。")
    return result


def chat_bank_answer(
    question,
    current_answer,
    lang,
    message,
    history=None,
    resume_text=None,
    target_roles=None,
    model=None,
    provider="anthropic",
):
    """陪聊一轮，返回 {"reply": str, "answer": str|None}。

    answer 为 None 表示这轮没有给出新版本（候选人只是在问问题，或者教练认为不用改），
    前端据此决定要不要显示「用这版替换答案」按钮。
    """
    if lang not in _LANG_LABELS:
        raise RuntimeError(f"不支持的语言：{lang}（只能是 zh 或 en）")
    if not resume_text:
        raise RuntimeError("未能读取基础简历文件，请确认 config.json 中 base_resume_path 是否正确。")

    system = BANK_CHAT_SYSTEM.format(
        resume_text=resume_text,
        target_roles=_format_roles(target_roles),
        question=question,
        lang_label=_LANG_LABELS[lang],
        current_answer=(current_answer or "").strip() or _EMPTY_ANSWER,
    )
    result = _chat(system, history, message, model, provider)
    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        answer = None
    return {"reply": result["reply"], "answer": answer}


def build_bank_block(items, answer_limit=500):
    """把整个题库压成一段喂给全局助手。答案截断是为了控制 token——助手做的是跨题诊断
    （故事重不重复、覆盖面缺什么），看开头几百字足够判断，不需要每条都读全。"""
    parts = []
    for key, label in _BANK_CATEGORY_LABELS:
        rows = [i for i in (items or []) if i.get("category") == key]
        if not rows:
            continue
        lines = []
        for row in rows:
            answer = (row.get("answer") or "").strip() or "（还没有答案）"
            if len(answer) > answer_limit:
                answer = answer[:answer_limit] + "…（已截断）"
            lines.append(f"- 【题】{row.get('question', '')}\n  【答】{answer}")
        parts.append(f"【{label}】\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else "（题库现在是空的。）"


def chat_bank_assistant(
    bank_items, message, history=None, resume_text=None, target_roles=None, model=None, provider="anthropic"
):
    """全局题库助手：只做跨题诊断，不改写具体答案（改写走 chat_bank_answer）。
    返回 {"reply": str}——刻意不带 answer 字段，前端也就不会出现"采用"按钮。"""
    if not resume_text:
        raise RuntimeError("未能读取基础简历文件，请确认 config.json 中 base_resume_path 是否正确。")

    system = BANK_ASSISTANT_SYSTEM.format(
        resume_text=resume_text,
        target_roles=_format_roles(target_roles),
        bank_block=build_bank_block(bank_items),
    )
    result = _chat(system, history, message, model, provider)
    return {"reply": result["reply"]}
