"""Calls the Claude API to run the jd-resume-matcher two-factor matching
analysis (see jd-resume-matcher SKILL.md) against a job's JD text and the
user's base resume, and returns a structured result ready to write into the
xlsx tracker (and, if the match is strong, a tailored resume + cover letter).
"""
import json
import os

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
2. 对比简历，找出技能匹配度里"匹配的"和"未达标的"具体条目。
3. 按双因子模型打分（都是0~1之间的小数）：
   - cognitive_match：候选人是否具备完成这份工作所需的硬技能/方法论 + 领域知识（行业背景缺口也算在这里）
   - content_match：JD描述的日常职责/工作性质/角色范围是否和候选人过去/现在实际做的、想做的工作内容相符
   （不要把经验年限、薪资、团队规模、地理位置这些因素混入这两个分数）
4. 如果 (cognitive_match*0.5 + content_match*0.5) >= 0.7：
   - 判断是否需要定制简历（needs_customization: true/false）。如果简历已经覆盖JD要求只是措辞不同，可以判定false。
   - 如果 needs_customization=true：给出 resume_paragraph_edits（只列出需要改动的段落，每条是 {{"index": 原文中的段落索引数字, "text": "修改后的完整段落文本（不要包含索引标记）"}}，改动要基于简历里真实存在的经历和数据，不能编造未发生的经历或夸大数据），以及 resume_optimization_bullets（用中文列出改了哪些地方，要点式）。
   - 生成 cover_letter（英文，专业简短4-5段以内，开头点明意向和当前角色，中间对应JD强调的匹配点，如果存在明显能力/领域缺口要主动坦诚说明并给出可迁移能力的说法，结尾简短表达期待沟通）。
5. 如果总分 < 0.7：resume_paragraph_edits、resume_optimization_bullets、cover_letter 都留空/null，needs_customization=false。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "job_content_bullets": ["..."],
  "requirement_items": [{{"text": "...", "is_gap": false}}],
  "skill_matched_bullets": ["..."],
  "skill_gap_bullets": ["..."],
  "experience_years": "...",
  "industry_bullets": ["..."],
  "salary": "...",
  "team_bullets": ["..."],
  "location": "...",
  "cognitive_match": 0.0,
  "content_match": 0.0,
  "needs_customization": false,
  "resume_paragraph_edits": [{{"index": 0, "text": "..."}}],
  "resume_optimization_bullets": ["..."],
  "cover_letter": "..."
}}
"""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def analyze_job(company, title, jd_text, resume_text, model=None):
    from anthropic import Anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 ANTHROPIC_API_KEY 环境变量，无法调用Claude API做自动匹配分析。"
        )
    if not resume_text:
        raise RuntimeError("未能读取基础简历文件，请确认 config.json 中 base_resume_path 是否正确。")

    client = Anthropic(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(
        resume_text=resume_text, company=company, title=title, jd_text=jd_text or "(未获取到JD正文)"
    )
    resp = client.messages.create(
        model=model or "claude-sonnet-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in resp.content if hasattr(block, "text"))
    result = _extract_json(raw)

    cognitive = float(result.get("cognitive_match", 0))
    content = float(result.get("content_match", 0))
    result["overall_match"] = round(0.5 * cognitive + 0.5 * content, 4)
    return result
