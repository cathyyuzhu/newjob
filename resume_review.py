"""简历体检：对整份简历做一次不针对具体职位的诊断，给出评分、问题清单和逐段改写建议。

跟 analyzer.py 的分工：analyzer 回答"我跟**这一个**职位有多匹配"，每条职位都要跑一次；
这里回答"我这份简历**本身**写得怎么样"，跟投哪家无关，只跟目标岗位方向有关，一份简历
跑一次就够。所以它不进职位分析的批量队列，是「我的简历」页上一个手动按钮。

输出里的 paragraph_edits 刻意跟 analyzer 的 resume_paragraph_edits 用同一个形状
（{"index": 段落索引, "text": 改写后的整段}），因为两者最终都喂给
resume_docx.write_tailored_resume() —— 用户勾选哪几条，就把哪几条原样传进去。
"""
import re

import llm

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


def _clamp01(value, default=0.0):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


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
    return normalize_result(result, max_index=max_paragraph_index(resume_text))


def max_paragraph_index(resume_text):
    """简历原文里最大的那个 [N] 段落索引。

    不能用行数代替：read_resume_text() 跳过了空段落，所以 [N] 是稀疏的，
    第 3 行完全可能是 [7]。拿不到就返回 None（表示"不知道，别按索引筛"）。
    """
    indexes = [int(m) for m in re.findall(r"^\[(\d+)\]", resume_text or "", re.MULTILINE)]
    return max(indexes) if indexes else None


def normalize_result(result, max_index=None):
    """把 LLM 返回的原始 JSON 收拾成前端可以直接渲染的形状。

    max_index：简历里最大的段落索引。超出它的改写建议会被丢掉——
    write_tailored_resume() 对越界索引是静默跳过的，留在界面上只会让用户勾了一条
    什么都不会发生的建议，还以为是生成功能坏了。
    """
    result = dict(result or {})

    raw_scores = result.get("dimension_scores") or {}
    scores = {}
    for dim in DIMENSIONS:
        value = raw_scores.get(dim)
        # 有的模型会把 0.72 写成 72，超过 1 的一律按百分制回收
        if isinstance(value, (int, float)) and value > 1:
            value = value / 100.0
        scores[dim] = _clamp01(value)
    result["dimension_scores"] = scores

    overall = result.get("overall_score")
    if isinstance(overall, (int, float)) and overall > 1:
        overall = overall / 100.0
    overall = _clamp01(overall, default=-1)
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
                "paragraph_index": item.get("paragraph_index"),
            }
        )
    # high 在前，方便前端直接顺序渲染（LLM 说了按严重程度排，但不能指望它每次都照做）
    issues.sort(key=lambda i: SEVERITIES.index(i["severity"]))
    result["issues"] = issues

    edits = []
    for item in result.get("paragraph_edits") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        text = item.get("text")
        # 没有段落索引、索引越界、或没有改写内容的条目直接丢掉：它们没法喂给
        # write_tailored_resume（越界会被静默跳过），留在列表里只会让用户勾了个不生效的框。
        if not isinstance(index, int) or index < 0 or not (text or "").strip():
            continue
        if max_index is not None and index > max_index:
            continue
        edits.append(
            {
                "index": index,
                "original": item.get("original") or "",
                "text": text,
                "reason": item.get("reason") or "",
            }
        )
    result["paragraph_edits"] = edits

    coverage = result.get("keyword_coverage") or {}
    result["keyword_coverage"] = {
        "covered": [s for s in (coverage.get("covered") or []) if s],
        "missing": [s for s in (coverage.get("missing") or []) if s],
    }
    result["strengths"] = [s for s in (result.get("strengths") or []) if s]
    result["summary"] = result.get("summary") or ""
    return result
