"""Calls an LLM (Claude or DeepSeek) to run the jd-resume-matcher two-factor
matching analysis (see jd-resume-matcher SKILL.md) against a job's JD text and
the user's base resume, and returns a structured result ready to write into
the xlsx tracker.

定制简历改写和 cover letter 的生成是**另一次调用**（generate_materials），不在匹配
分析里。以前两件事揉在同一个 prompt 里，匹配度一到 70% 就顺手把简历和 cover letter
一起生成了——但那会儿职位还躺在"待审核"里，用户根本还没决定要不要投，钱先花了、
简历文件先落盘了。拆开之后分析只回答"这个职位值不值得看"，材料等用户点按钮再生成。
"""
import logging

import llm
import resume_edits

REQUIRED_ANALYZE_KEYS = (
    "company_overview", "job_content_bullets", "requirement_items", "skill_matched_bullets",
    "skill_gap_bullets", "experience_years", "industry_bullets", "salary", "team_bullets",
    "location", "company_origin", "cognitive_match", "content_match",
)

PROMPT_TEMPLATE = """你是一个JD-简历匹配分析助手，严格按以下规则分析。

## 简历原文（每行前面的 [数字] 是该段落在原始docx文件中的索引，仅供你在需要修改该段落时引用，不要在输出的文本里保留这个索引标记）：
{resume_text}

## 职位信息
公司：{company}
职位名称：{title}
JD正文：
{jd_text}
{preference_profile_block}
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
   为了让上面这条规则可以被程序核验，requirement_items 每条还要给两个字段：
   - is_mandatory：JD原文有没有把这一条明确标注为强制性（true/false）
   - mandatory_evidence：is_mandatory=true 时，把JD原文里标注强制性的**那一小段原话逐字照抄**过来（保持JD的原始语言，不要翻译、不要转述）；is_mandatory=false 时填空字符串
   **照抄不到原话就把 is_mandatory 设成 false**——程序会拿 mandatory_evidence 回JD原文里核对，对不上的一律不当强制要求处理。
   - 职级错配拖累 content_match：如果JD要求的相关经验年限明显低于候选人简历体现的实际经验（比如只要求4-6年），且职位title不带Senior/Staff/Principal/Director/Head/VP等资深字样，这通常意味着职责范围是初级/中级IC岗位，跟候选人现在的资历定位不符——即使技能条目表面都对得上，也要在 content_match 上体现这层"职级偏低"的错配，往下调，不能只看技能清单、忽略候选人可能"高配低就"这个问题。
   - 如果上面提供了"用户偏好档案"，且这个职位明显撞上档案里反复出现的排斥点，可以酌情在 content_match 上体现（往下调），但不能仅凭一次不完全匹配就一票否决——档案是参考信号，不是硬性排除规则，不确定时不要过度套用。
## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "company_overview": "...",
  "job_content_bullets": ["..."],
  "requirement_items": [{{"text": "...", "is_gap": false, "is_mandatory": false, "mandatory_evidence": ""}}],
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
2. 如果 needs_customization=true：给出 resume_paragraph_edits（只列出需要改动的段落，每条是 {{"index": 原文中的段落索引数字, "original": "该段的原文（照抄，不含索引标记）", "text": "修改后的完整段落文本（不要包含索引标记）"}}，改动要基于简历里真实存在的经历和数据，不能编造未发生的经历或夸大数据），以及 resume_optimization_bullets（用中文列出改了哪些地方，要点式）。needs_customization=false 时这两个字段留空数组。
   **注意 text 是替换掉整个段落的，不是只替换你想改的那一句**——所以 original 必须是那一段的完整原文，text 也必须是改写后的完整段落。只摘抄半段会导致该段其余内容丢失。
3. 不管 needs_customization 是 true 还是 false，都要生成 cover_letter（英文，专业简短4-5段以内，开头点明意向和当前角色，中间对应JD强调的匹配点，如果存在明显能力/领域缺口要主动坦诚说明并给出可迁移能力的说法，结尾简短表达期待沟通）——这次生成是用户明确点按钮要的，不要因为匹配度不高就拒绝产出。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "needs_customization": false,
  "resume_paragraph_edits": [{{"index": 0, "original": "...", "text": "..."}}],
  "resume_optimization_bullets": ["..."],
  "cover_letter": "..."
}}
"""


COMPANY_ORIGIN_PROMPT = """判断下面这些公司分别属于："foreign"（总部/主体在中国大陆以外，含其在华子公司/办公室，如跨国企业、外资在华机构）、"domestic"（总部/主体在中国大陆，含大陆互联网大厂、国企、本土创业公司等）、还是"unknown"（公司名称信息不足以判断，比如从未听说过、名称过于通用）。只依据你对这些公司的知识判断，不确定时选"unknown"，不要瞎猜。

公司列表：
{companies}

只输出一个JSON对象，key是公司名（跟上面列表里的原文一字不差），value是"foreign"/"domestic"/"unknown"，不要有任何其他文字、不要用markdown代码块包裹。
"""


class AnalysisContractError(RuntimeError):
    """LLM 返回的分析结果不满足 PROMPT_TEMPLATE 规定的输出契约（缺字段/分数越界/
    is_gap 类型不对）。是 RuntimeError 的子类，所以 pipeline.py 和各 routes 里已有的
    `except RuntimeError`/`except Exception` 兜底一个都不用改。

    单独成一个类是给 evals/run_analyzer_eval.py 用的：它需要把"模型没遵守输出格式"
    （这正是 eval 要测的东西，该判 FAIL）和"调用本身失败"（网络抖动/欠费/限流，该判
    ERROR）区分开——两者以前都只是裸 RuntimeError，没法从异常类型上分辨，一次真正的
    prompt 漂移会跟一次网络抖动被同等对待。"""


def validate_analysis_result(result):
    """结构校验：保证 analyze_job() 从 LLM 拿到的 JSON 长得跟 PROMPT_TEMPLATE 要求的一样
    （字段齐全、分数在 [0,1] 范围内、requirement_items 里每条都有 is_gap 布尔值），不满足
    就抛错——不能让缺字段或越界分数悄悄写进追踪表/数据库。以前这份校验只存在于
    evals/run_analyzer_eval.py 的离线评测里，没接入这里的生产调用路径。"""
    problems = []
    for key in REQUIRED_ANALYZE_KEYS:
        if key not in result:
            problems.append(f"缺字段 {key}")
    if problems:
        raise AnalysisContractError(f"AI 返回的分析结果结构不完整：{'; '.join(problems)}")

    for score_key in ("cognitive_match", "content_match"):
        v = result[score_key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 1):
            problems.append(f"{score_key}={v!r} 不是 [0,1] 范围内的数")
    if not isinstance(result["requirement_items"], list):
        problems.append("requirement_items 不是数组")
    else:
        # is_mandatory / mandatory_evidence 刻意**不进这里的必填校验**：模型偶尔漏掉
        # 这两个字段的话，整条分析就会抛错——为了一条兜底规则牺牲主流程可用性不划算。
        # 缺了就当 false 处理（见 verify_mandatory_items），退回没有这条规则时的行为。
        for item in result["requirement_items"]:
            if not isinstance(item, dict) or not isinstance(item.get("is_gap"), bool):
                problems.append(f"requirement_items 里有条目缺 is_gap 或不是 bool：{item!r}")
                break
    if problems:
        raise AnalysisContractError(f"AI 返回的分析结果不符合规范：{'; '.join(problems)}")


# 未覆盖的强制性要求条数 → cognitive_match 上限。数值跟 PROMPT_TEMPLATE 里那条规则
# （0.5 / 0.3）保持一致：prompt 负责让模型自己打对，这张表负责在它没打对时兜底。
MANDATORY_GAP_CAPS = {1: 0.5, 2: 0.3}
MANDATORY_GAP_CAP_FLOOR = 0.3


def verify_mandatory_items(requirement_items, jd_text):
    """核验每条任职要求的 is_mandatory 标注，就地写回。返回确认成立的硬缺口条数。

    核验方式：拿模型给的 mandatory_evidence 回 JD 原文里找。找不到就把 is_mandatory
    降成 False（fail-open，退回没有这条规则时的行为）。

    为什么必须核验：这条规则会把分数从 0.8 直接压到 0.3，而模型只要把 "preferred"
    也标成强制，大批职位就会被误杀，比不做还糟。要求它照抄原话、程序回原文比对，
    是纯 Python、零成本的确定性核查——跟 resume_edits.py 核查段落原文是同一个路子。
    """
    if not isinstance(requirement_items, list):
        return 0
    haystack = resume_edits.normalize_for_compare(jd_text or "")
    gaps = 0
    for item in requirement_items:
        if not isinstance(item, dict):
            continue
        evidence = resume_edits.normalize_for_compare(item.get("mandatory_evidence") or "")
        confirmed = bool(item.get("is_mandatory")) and bool(evidence) and evidence in haystack
        item["is_mandatory"] = confirmed
        if confirmed and item.get("is_gap") is True:
            gaps += 1
    return gaps


def apply_score_rules(result, jd_text):
    """把 PROMPT_TEMPLATE 里那条"硬门槛拖累总分"的规则落成代码，就地改写 result。

    在这之前这条规则只存在于 prompt 自然语言里，Python 侧一行都没有——模型返回 0.9
    照样入库，唯一的执行力在离线 eval（evals/run_analyzer_eval.py），而那是抽样、事后、
    不阻断的。spec/product-review.md 记的"11/37 高分职位被人工否决"里大概率有这一类。

    保留 raw_cognitive_match（模型原始输出），这样 eval 仍然能检测 prompt 漂移——
    封顶落成代码之后，eval 再去查 cognitive_match 就必然通过，等于失去了这项能力。
    """
    gaps = verify_mandatory_items(result.get("requirement_items"), jd_text)
    raw = float(result.get("cognitive_match", 0))
    result["raw_cognitive_match"] = raw
    result["mandatory_gap_count"] = gaps
    result["score_adjustments"] = []
    if gaps:
        cap = MANDATORY_GAP_CAPS.get(gaps, MANDATORY_GAP_CAP_FLOOR)
        if raw > cap:
            result["cognitive_match"] = cap
            result["score_adjustments"].append(
                f"JD 有 {gaps} 条明确标注为强制性的要求未被简历覆盖，"
                f"cognitive_match 由 {raw} 封顶到 {cap}"
            )
    return result


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


def analyze_job(company, title, jd_text, resume_text, model=None, provider="anthropic", preference_profile_text=None):
    if not resume_text:
        raise RuntimeError("读不到简历内容，请在「我的简历」页重新上传一份 .docx 简历。")

    preference_profile_block = (
        f"\n## 用户偏好档案（供参考，用法见下面任务3的说明）\n{preference_profile_text}\n"
        if preference_profile_text else ""
    )
    prompt = PROMPT_TEMPLATE.format(
        resume_text=resume_text, company=company, title=title, jd_text=jd_text or "(未获取到JD正文)",
        preference_profile_block=preference_profile_block,
    )
    result = llm.ask_json(prompt, provider=provider, model=model)
    validate_analysis_result(result)
    # 顺序不能反：overall_match 必须基于封顶**之后**的 cognitive_match 算，
    # 否则硬门槛不满足的职位总分照样虚高，封顶等于白做。
    apply_score_rules(result, jd_text)

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
    # 段落改写建议要对着简历原文核查一遍（见 resume_edits.py）。这里用 DROP_HARMFUL：
    # 简历体检那边核查不过的会保留下来标记成"不可应用"让用户自己看，而这条路径
    # （pipeline.generate_materials_for_job）拿到结果直接 write_tailored_resume 落盘，
    # 没有人工复核环节，也没有 UI 展示警告，所以会损坏内容的只能丢。
    paragraphs = resume_edits.parse_indexed_paragraphs(resume_text)
    edits, dropped = resume_edits.annotate_edits(
        result.get("resume_paragraph_edits"), paragraphs, drop=resume_edits.DROP_HARMFUL
    )
    if dropped:
        # 不记一笔的话，表现出来就是"AI 说要定制简历，却没有生成定制简历"——
        # 这种静默行为最难查（pipeline.py 那边判断 edits 非空才生成文件）。
        logging.warning(
            "%s / %s 的定制简历丢弃了 %s 条改写建议：%s", company, title, len(dropped), dropped
        )
    result["resume_paragraph_edits"] = edits
    # 只给 evals/run_analyzer_eval.py 的编造检查器看的诊断字段——annotate_edits 丢弃的
    # 条目（越界/摘抄不全/原文对不上）默认不会出现在返回结果里，调用方（pipeline.py）
    # 也从不读这个 key，所以加它不影响生产路径，只是让离线 eval 能看见"这次有没有
    # 条目被悄悄丢掉"。
    result["resume_paragraph_edits_dropped"] = dropped
    result["resume_optimization_bullets"] = result.get("resume_optimization_bullets") or []
    result["cover_letter"] = result.get("cover_letter") or None
    return result
