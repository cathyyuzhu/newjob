"""简历体检：对整份简历做一次不针对具体职位的诊断，给出评分、问题清单和逐段改写建议。

跟 analyzer.py 的分工：analyzer 回答"我跟**这一个**职位有多匹配"，每条职位都要跑一次；
这里回答"我这份简历**本身**写得怎么样"，跟投哪家无关，只跟目标岗位方向有关，一份简历
跑一次就够。所以它不进职位分析的批量队列，是「我的简历」页上一个手动按钮。

输出里的 paragraph_edits 刻意跟 analyzer 的 resume_paragraph_edits 用同一个形状
（{"index": 段落索引, "text": 改写后的整段}），因为两者最终都喂给
resume_docx.write_tailored_resume() —— 用户勾选哪几条，就把哪几条原样传进去。
"""
import logging

import llm
import resume_edits

REVIEW_PROMPT = """你是一位资深的简历顾问，正在帮一位求职者做简历体检。

## 求职者的目标岗位方向
{target_roles}

## 简历原文（每行前面的 [数字] 是该段落在原始docx文件中的索引，仅供你在需要修改该段落时引用，不要在输出的文本里保留这个索引标记）：
{resume_text}

## 任务
只针对上面这份简历本身做诊断，**不要**假设某个具体公司或某条具体JD。全部用中文输出
（公司名、产品名、技术/工具名、职级缩写等专有名词可保留英文原文）。

1. 打四个维度的分（都是0~1之间的小数，实事求是，不要一律给高分）：
   - structure：结构与排版。信息层级是否清楚、篇幅是否合理、关键信息是否在显眼位置、有没有该有的模块缺失。
   - impact：成果的说服力。是在写"我负责什么"还是"我做成了什么"；有没有量化数据；数据是否可信、是否说明了自己的贡献而不是团队的。
   - keyword：跟目标岗位方向的关键词覆盖。目标岗位通常会考察的能力项、领域词、方法论，简历里有没有体现。
   - clarity：表达质量。有没有空话套话（"良好的沟通能力"这类）、动词是否有力、有没有前后矛盾或含糊其辞的地方。
   overall_score 用这四项的平均值。

2. strengths：这份简历确实写得好的地方，2-4条，要具体（指出是哪一段的什么写法好），不要泛泛夸奖。

3. issues：具体问题清单，按严重程度排序，一般5-10条。每条：
   - severity："high"（会直接导致被筛掉）/ "medium"（明显拉低印象）/ "low"（锦上添花）
   - title：一句话点出问题
   - detail：为什么是问题、建议怎么改（说清楚方向即可，具体改写放到 paragraph_edits）
   - paragraph_index：如果这条问题能定位到某个具体段落，填该段落的索引数字；如果是整体性问题（比如"缺少技能模块"），填 null

4. keyword_coverage：对照目标岗位方向，covered 列出简历里已经体现的关键能力/领域词，
   missing 列出目标岗位通常会看、但这份简历里没有体现的。missing 里只列**求职者有可能
   真的具备、只是没写出来**的，不要列他明显不具备的东西（那是要去补经历，不是改简历）。

5. paragraph_edits：逐段改写建议，挑最值得改的3-8段。每条：
   - index：原文中的段落索引数字
   - original：该段的原文（照抄，不含索引标记）
   - text：改写后的完整段落文本（不含索引标记）
   - reason：为什么这么改，一句话
   **硬约束：改写只能基于简历里真实存在的经历和数据，可以换措辞、调结构、把已有信息
   表达得更有力，但绝对不能编造未发生的经历、不能凭空添加数字、不能夸大已有数据。**
   如果某段的问题是"缺少数据支撑"，改写里要用占位提示（比如"（此处建议补充具体数字：
   例如用户量/转化率提升幅度）"）让用户自己填，而不是替他编一个。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹，字段如下：
{{
  "overall_score": 0.0,
  "dimension_scores": {{"structure": 0.0, "impact": 0.0, "keyword": 0.0, "clarity": 0.0}},
  "summary": "两三句话的总体评价",
  "strengths": ["..."],
  "issues": [{{"severity": "high", "title": "...", "detail": "...", "paragraph_index": 3}}],
  "keyword_coverage": {{"covered": ["..."], "missing": ["..."]}},
  "paragraph_edits": [{{"index": 3, "original": "...", "text": "...", "reason": "..."}}]
}}
"""

DIMENSIONS = ("structure", "impact", "keyword", "clarity")

SEVERITIES = ("high", "medium", "low")


def _valid_index(index, paragraphs):
    """issues 里的段落锚点：不是合法索引就当"整体性问题"（None）。paragraphs 为空
    （没传简历原文）时不做判断，原样透传。"""
    if not paragraphs:
        return index
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    return index if index in paragraphs else None


def review_resume(resume_text, target_roles=None, model=None, provider="anthropic"):
    """跑一次体检，返回规整过的结果 dict。

    LLM 给的分数字段会做归一化（缺字段、给了 0~100 而不是 0~1、给了字符串都可能发生），
    免得前端画进度条时拿到一个 NaN 或者 87 这样的值。
    """
    if not resume_text:
        # 正常路径上 app.py 已经先拦过一道了，这里是兜底：直接放行会让 LLM 对着空简历
        # 一本正经地编一份体检报告出来。
        raise RuntimeError("还没有上传简历，请先在「我的简历」页上传一份 .docx 简历。")

    roles = "、".join(target_roles) if isinstance(target_roles, (list, tuple)) else (target_roles or "")
    prompt = REVIEW_PROMPT.format(
        resume_text=resume_text,
        target_roles=roles or "（用户没有填写目标岗位方向，请按简历本身体现出的职业方向来判断）",
    )
    result = llm.ask_json(prompt, provider=provider, model=model)
    return normalize_result(result, resume_text=resume_text)


def normalize_result(result, resume_text=None):
    """把 LLM 返回的原始 JSON 收拾成前端可以直接渲染的形状。

    resume_text：简历原文（带 [N] 索引标记的那份）。传了就会对 paragraph_edits 做
    确定性核查——把模型声称"照抄"的 original 拿回真实段落比对，见 resume_edits.py。
    越界的直接丢（write_tailored_resume 对越界索引静默跳过，留在界面上只是一个勾了
    什么都不会发生的框），指错段/只摘抄一部分的保留但标记成不可应用。
    """
    result = dict(result or {})
    paragraphs = resume_edits.parse_indexed_paragraphs(resume_text)

    raw_scores = result.get("dimension_scores") or {}
    scores = {}
    for dim in DIMENSIONS:
        value = raw_scores.get(dim)
        # 有的模型会把 0.72 写成 72，超过 1 的一律按百分制回收
        if isinstance(value, (int, float)) and value > 1:
            value = value / 100.0
        scores[dim] = llm.clamp(value)
    result["dimension_scores"] = scores

    overall = result.get("overall_score")
    if isinstance(overall, (int, float)) and overall > 1:
        overall = overall / 100.0
    overall = llm.clamp(overall, default=-1)
    if overall < 0:
        overall = round(sum(scores.values()) / len(DIMENSIONS), 4)
    result["overall_score"] = round(overall, 4)

    issues = []
    for item in result.get("issues") or []:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        issues.append(
            {
                "severity": severity if severity in SEVERITIES else "medium",
                "title": item.get("title") or "",
                "detail": item.get("detail") or "",
                # 锚点越界就置 None（当成"整体性问题"），但**不丢整条 issue**——问题描述
                # 本身仍然有效，只是定位不可信。跟 paragraph_edits 那边"越界就丢"的差别
                # 在于：那边丢了没有损失（本来就是个不生效的勾选框），这边丢了会损失内容。
                "paragraph_index": _valid_index(item.get("paragraph_index"), paragraphs),
            }
        )
    # high 在前，方便前端直接顺序渲染（LLM 说了按严重程度排，但不能指望它每次都照做）
    issues.sort(key=lambda i: SEVERITIES.index(i["severity"]))
    result["issues"] = issues

    edits, dropped = resume_edits.annotate_edits(result.get("paragraph_edits"), paragraphs)
    result["paragraph_edits"] = edits
    if dropped:
        # 丢弃是静默的降级，不记一笔的话表现出来就是"AI 给的建议怎么这么少"，没法排查。
        logging.warning("简历体检丢弃了 %s 条改写建议：%s", len(dropped), dropped)
    result["edit_warnings"] = [
        {"index": e["index"], "verdict": e["verdict"], "warning": e["warning"]}
        for e in edits
        if e.get("verdict")
    ]

    coverage = result.get("keyword_coverage") or {}
    result["keyword_coverage"] = {
        "covered": [s for s in (coverage.get("covered") or []) if s],
        "missing": [s for s in (coverage.get("missing") or []) if s],
    }
    result["strengths"] = [s for s in (result.get("strengths") or []) if s]
    result["summary"] = result.get("summary") or ""
    return result
