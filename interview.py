"""面试准备：根据职位JD、简历、以及已经跑过的AI匹配分析结果，生成一份针对这家公司/
这个岗位的面试准备材料（公司背景研究、预测面试题+建议答法、缺口应对话术、反问清单）。

跟 analyzer.py 同层定位：只负责 prompt 和调 LLM，不碰数据库——读职位/读简历/写库都在
pipeline.py 里编排。
"""
import json

import llm

# 面试准备一次要出 10-15 道题（每题还带答题要点和简历依据）+ 缺口话术 + 反问清单，
# 输出量明显大于匹配分析，用默认的 4096 会被截断成不完整的 JSON 直接解析失败。
PREP_MAX_TOKENS = 8192

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

# 题库一次要出自我介绍中英双版 + 8-12 道通用题 + 3-5 个 STAR 故事（故事都是完整段落），
# 输出量跟单职位准备材料差不多，同样不能用默认的 4096。
BANK_MAX_TOKENS = 8192

BANK_PROMPT = """你是一个资深的面试辅导教练，帮候选人准备**跨公司通用**的面试答案库
（不针对某一家具体公司，是每场面试都用得上的那些标准问题）。

## 候选人简历原文（每行前面的 [数字] 是段落索引，忽略即可，不要在输出里保留）：
{resume_text}

## 候选人正在找的岗位方向
{target_roles}

## 任务

1. **self_intro 自我介绍**：写一段 60-90 秒口播的自我介绍（`zh` 中文版 + `en` 英文版，
   英文版是面向外企面试的自然英文表达，不是中文版的逐字直译）。结构：当前角色和年限 →
   最能代表能力的 1-2 段经历（带具体成果数据）→ 为什么在看上面这个方向的机会。
   要口语化、能念出来，不要写成书面简历摘要。

2. **items 通用问题**：8-12 道几乎每场面试都会遇到的通用问题，结合这份简历给出候选人
   自己的答案（不是通用模板套话）。要覆盖这些方向（可按简历情况增减）：
   为什么离开上一家 / 为什么想来这个方向 / 未来3-5年职业规划 / 最大的优势 /
   最大的短板 / 讲一次失败或做错的决策 / 你怎么定义这个岗位做得好 / 期望薪资怎么谈 /
   还有哪些在看的机会。
   答案要具体到简历里的真实经历，长度控制在口头回答 60-90 秒的量。

3. **star_stories 核心故事库**：从简历里提炼 3-5 个可以反复复用的完整故事，用
   情境 → 任务 → 行动 → 结果 的结构完整写出来（每个故事一段完整的话，不是要点）。
   尽量覆盖不同类型：从0到1做成一件事 / 推动跨部门协作或说服他人 / 处理冲突或危机 /
   数据驱动的决策 / 一次失败和从中学到什么。
   `question` 字段写"面试官通常会用什么问题引出这个故事"，`answer` 写故事本身。

## 硬性约束
- 全部用中文输出（只有 self_intro 的 `en` 字段是英文）。
- **所有答案必须基于简历里真实存在的经历和数据，不能编造未发生的经历、不能夸大数据。**
  简历信息不足以支撑某道题时，答案里如实留出待用户补充的部分（用「（此处需你补充：…）」标注），
  不要虚构。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "self_intro": {{"zh": "...", "en": "..."}},
  "items": [{{"question": "为什么离开上一家？", "answer": "..."}}],
  "star_stories": [{{"question": "讲一个你从0到1做成一件事的例子", "answer": "..."}}]
}}
"""

SELF_INTRO_QUESTION = "自我介绍（60-90秒）"


def generate_bank(resume_text, target_roles=None, model=None, provider="anthropic"):
    """生成通用题库初稿。返回可以直接喂给 models.replace_ai_bank_items() 的扁平列表
    [{category, question, answer, answer_en}]——三种 category 在 prompt 输出里是三个
    不同形状的字段，这里统一拍平，让存储层只认一种结构。"""
    if not resume_text:
        raise RuntimeError("未能读取基础简历文件，请确认 config.json 中 base_resume_path 是否正确。")

    roles = "、".join(r for r in (target_roles or []) if r) or "（未指定，请从简历本身推断）"
    prompt = BANK_PROMPT.format(resume_text=resume_text, target_roles=roles)
    result = llm.ask_json(prompt, provider=provider, model=model, max_tokens=BANK_MAX_TOKENS)
    if not isinstance(result, dict):
        raise RuntimeError("LLM 返回的题库内容格式不对，请重试一次。")

    items = []
    intro = result.get("self_intro") or {}
    if intro.get("zh") or intro.get("en"):
        items.append(
            {
                "category": "self_intro",
                "question": SELF_INTRO_QUESTION,
                "answer": intro.get("zh"),
                "answer_en": intro.get("en"),
            }
        )
    for it in result.get("items") or []:
        if (it.get("question") or "").strip():
            items.append({"category": "common", "question": it["question"].strip(), "answer": it.get("answer")})
    for st in result.get("star_stories") or []:
        if (st.get("question") or "").strip():
            items.append({"category": "star_story", "question": st["question"].strip(), "answer": st.get("answer")})

    if not items:
        raise RuntimeError("LLM 没有返回任何题库内容，请重试一次。")
    return items
