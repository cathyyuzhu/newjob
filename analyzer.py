"""Calls an LLM (Claude or DeepSeek) to run the jd-resume-matcher two-factor
matching analysis (see jd-resume-matcher SKILL.md) against a job's JD text and
the user's base resume, and returns a structured result ready to write into
the xlsx tracker.

定制简历改写和 cover letter 的生成是**另一次调用**（generate_materials），不在匹配
分析里。以前两件事揉在同一个 prompt 里，匹配度一到 70% 就顺手把简历和 cover letter
一起生成了——但那会儿职位还躺在"待审核"里，用户根本还没决定要不要投，钱先花了、
简历文件先落盘了。拆开之后分析只回答"这个职位值不值得看"，材料等用户点按钮再生成。
"""
import llm

PROMPT_TEMPLATE = """你是一个JD-简历匹配分析助手，严格按以下规则分析。

## 简历原文（每行前面的 [数字] 是该段落在原始docx文件中的索引，仅供你在需要修改该段落时引用，不要在输出的文本里保留这个索引标记）：
{resume_text}

## 职位信息
公司：{company}
职位名称：{title}
JD正文：
{jd_text}

## 任务
1. 从JD中提取：职位内容要点、任职要求（拆成一条条，标注每条是否在简历中已达标 is_gap=false / 未达标 is_gap=true）、相关经验年限要求、行业背景要求、薪资范围（JD未提及则填"JD未公开，需进一步询问"）、团队规模或汇报线（JD未提及则如实说明未提及）、地理位置/远程政策。
   以上所有提取内容（职位内容要点、任职要求、相关经验年限、行业背景、薪资范围、团队规模、地理位置等）一律用中文输出：如果JD原文是英文，请翻译成通顺自然的中文，不要逐字机翻；公司名、产品名、技术/工具名、职级缩写等专有名词可保留英文原文。
1.5. 基于你对这家公司的知识（不依赖JD正文），用中文写一段简要的公司简介（company_overview，2-4句话即可）：主营业务/行业赛道、大致规模或知名度、其它有助于候选人了解这家公司的背景信息。如果你对这家公司完全没有可靠认知（比如从未听说过、名称过于通用无法确定具体是哪家），如实填"未找到该公司的相关信息"，不要编造。
2. 对比简历，找出技能匹配度里"匹配的"和"未达标的"具体条目。
2.5. 判断公司国籍归属（company_origin），基于你对该公司的知识 + JD正文里的线索：
   - "foreign"：总部在中国大陆以外的公司（含其在华子公司/办公室），如跨国企业、外资在华机构
   - "domestic"：总部/主体在中国大陆的公司（含大陆互联网大厂、国企、本土创业公司等）
   - "unknown"：公司名称/JD内容都不足以判断（例如从未听说过、名称过于通用）
   不确定时倾向选"unknown"而不是瞎猜。
3. 按双因子模型打分（都是0~1之间的小数）：
   - cognitive_match：候选人是否具备完成这份工作所需的硬技能/方法论 + 领域知识（行业背景缺口也算在这里）
   - content_match：JD描述的日常职责/工作性质/角色范围是否和候选人过去/现在实际做的、想做的工作内容相符
   （不要把经验年限、薪资、团队规模、地理位置这些因素混入这两个分数）
   - 硬性门槛拖累总分：任职要求里如果有条目被JD原文明确标注为强制性（如"required"、"must have"、"mandatory"、"必须"、"强制要求"等措辞，常见于certification/资质类要求），并且该条目 is_gap=true（简历未覆盖），cognitive_match 最高不能超过0.5——不能仅凭"迁移技能可以覆盖精神"这类理由把分数打高来掩盖这个硬缺口；如果同时有两条以上这类未覆盖的强制性要求，cognitive_match 要进一步下调（比如0.3左右），如实反映硬门槛不满足的严重程度。
## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "company_overview": "...",
  "job_content_bullets": ["..."],
  "requirement_items": [{{"text": "...", "is_gap": false}}],
  "skill_matched_bullets": ["..."],
  "skill_gap_bullets": ["..."],
  "experience_years": "...",
  "industry_bullets": ["..."],
  "salary": "...",
  "team_bullets": ["..."],
  "location": "...",
  "company_origin": "foreign|domestic|unknown",
  "cognitive_match": 0.0,
  "content_match": 0.0
}}
"""


MATERIALS_PROMPT = """你是一个简历定制助手。下面给你一份基础简历、一个具体职位的JD，以及之前对这个职位做过的匹配分析结论。请据此产出投递这个职位要用的两份材料：一份定制简历的改动方案，一份 cover letter。

## 简历原文（每行前面的 [数字] 是该段落在原始docx文件中的索引，仅供你在需要修改该段落时引用，不要在输出的文本里保留这个索引标记）：
{resume_text}

## 职位信息
公司：{company}
职位名称：{title}
JD正文：
{jd_text}

## 之前的匹配分析结论（供参考，重点关注"未达标"的部分）
{analysis_context}

## 任务
1. 判断是否需要定制简历（needs_customization: true/false）。如果简历已经覆盖JD要求只是措辞不同，可以判定false。
2. 如果 needs_customization=true：给出 resume_paragraph_edits（只列出需要改动的段落，每条是 {{"index": 原文中的段落索引数字, "text": "修改后的完整段落文本（不要包含索引标记）"}}，改动要基于简历里真实存在的经历和数据，不能编造未发生的经历或夸大数据），以及 resume_optimization_bullets（用中文列出改了哪些地方，要点式）。needs_customization=false 时这两个字段留空数组。
3. 不管 needs_customization 是 true 还是 false，都要生成 cover_letter（英文，专业简短4-5段以内，开头点明意向和当前角色，中间对应JD强调的匹配点，如果存在明显能力/领域缺口要主动坦诚说明并给出可迁移能力的说法，结尾简短表达期待沟通）——这次生成是用户明确点按钮要的，不要因为匹配度不高就拒绝产出。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "needs_customization": false,
  "resume_paragraph_edits": [{{"index": 0, "text": "..."}}],
  "resume_optimization_bullets": ["..."],
  "cover_letter": "..."
}}
"""


COMPANY_ORIGIN_PROMPT = """判断下面这些公司分别属于："foreign"（总部/主体在中国大陆以外，含其在华子公司/办公室，如跨国企业、外资在华机构）、"domestic"（总部/主体在中国大陆，含大陆互联网大厂、国企、本土创业公司等）、还是"unknown"（公司名称信息不足以判断，比如从未听说过、名称过于通用）。只依据你对这些公司的知识判断，不确定时选"unknown"，不要瞎猜。

公司列表：
{companies}

只输出一个JSON对象，key是公司名（跟上面列表里的原文一字不差），value是"foreign"/"domestic"/"unknown"，不要有任何其他文字、不要用markdown代码块包裹。
"""


def classify_companies(companies, model=None, provider="anthropic"):
    """轻量批量判断一批公司名的国籍归属，只需要公司名（不需要JD/简历），比完整的
    analyze_job() 匹配分析快得多、几乎不花钱——用于在职位还没跑完整AI匹配分析之前，
    就能提前给"外企/国内公司"筛选填上判断结果。返回 {{公司名: "foreign"/"domestic"/"unknown"}}，
    LLM 没给出有效值的公司归为 "unknown"。"""
    if not companies:
        return {}

    prompt = COMPANY_ORIGIN_PROMPT.format(companies="\n".join(f"- {c}" for c in companies))
    result = llm.ask_json(prompt, provider=provider, model=model)
    return {c: (result.get(c) if result.get(c) in ("foreign", "domestic") else "unknown") for c in companies}


def analyze_job(company, title, jd_text, resume_text, model=None, provider="anthropic"):
    if not resume_text:
        raise RuntimeError("读不到简历内容，请在「我的简历」页重新上传一份 .docx 简历。")

    prompt = PROMPT_TEMPLATE.format(
        resume_text=resume_text, company=company, title=title, jd_text=jd_text or "(未获取到JD正文)"
    )
    result = llm.ask_json(prompt, provider=provider, model=model)

    cognitive = float(result.get("cognitive_match", 0))
    content = float(result.get("content_match", 0))
    result["overall_match"] = round(0.5 * cognitive + 0.5 * content, 4)
    if result.get("company_origin") not in ("foreign", "domestic"):
        result["company_origin"] = "unknown"
    return result


def generate_materials(company, title, jd_text, resume_text, analysis_context="", model=None, provider="anthropic"):
    """生成投递这个职位要用的定制简历改动方案 + cover letter。跟 analyze_job() 是两次
    独立的LLM调用：分析回答"值不值得看"，这里回答"决定投了，材料怎么写"，后者由用户
    点按钮触发（见 pipeline.generate_materials_for_job）。

    analysis_context 是之前那次匹配分析的结论摘要（技能缺口、任职要求等），纯参考——
    传空字符串也能跑，只是LLM少了一点"哪里需要补"的提示。"""
    if not resume_text:
        raise RuntimeError("读不到简历内容，请在「我的简历」页重新上传一份 .docx 简历。")

    prompt = MATERIALS_PROMPT.format(
        resume_text=resume_text,
        company=company,
        title=title,
        jd_text=jd_text or "(未获取到JD正文)",
        analysis_context=analysis_context or "(无)",
    )
    result = llm.ask_json(prompt, provider=provider, model=model)
    result["needs_customization"] = bool(result.get("needs_customization"))
    result["resume_paragraph_edits"] = result.get("resume_paragraph_edits") or []
    result["resume_optimization_bullets"] = result.get("resume_optimization_bullets") or []
    result["cover_letter"] = result.get("cover_letter") or None
    return result
