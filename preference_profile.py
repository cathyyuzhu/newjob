"""偏好档案：把用户忽略职位时随手记的原因（预设标签 + 自由文本）攒够一批后，
一次 LLM 调用总结成一段供以后判断新职位参考的"这个人大概不想要什么样的工作"。

跟 resume_review.py 同一类"全局单例、不挂 job_id"的产物——偏好是跟人相关的，
不是跟某条职位相关的。样本不够时如实说样本少，不能没有信号硬编一份看似言之凿凿的总结。
"""
import llm

PROFILE_PROMPT = """你是一个求职顾问，正在帮用户从他忽略过的职位里总结出求职偏好。

## 用户忽略过的职位及原因（最近的排最前面）
{reasons_block}

## 任务
只依据上面这些真实记录做总结，不要过度推断或脑补没有证据支撑的结论。用中文写一段
（4-8句话即可）"偏好档案"：反复出现的排斥点（比如反复因为同一类原因忽略）、可能倾向
的方向。如果样本太少或原因很分散、看不出明显规律，如实写"目前样本较少/原因比较分散，
暂无明显规律"，不要为了显得有内容而编造规律。

## 输出格式
只输出一个JSON对象，不要有任何其他文字、不要用markdown代码块包裹：
{{
  "summary": "..."
}}
"""


def _format_reason(r):
    parts = []
    company = r.get("company")
    title = r.get("title")
    if company or title:
        parts.append(f"《{title or '未知职位'}》@ {company or '未知公司'}")
    tags = r.get("tags")
    if tags:
        parts.append(f"原因标签：{tags}")
    note = r.get("note")
    if note:
        parts.append(f"备注：{note}")
    return "- " + "；".join(parts) if parts else "- （未填写具体原因）"


def summarize_preferences(reasons, model=None, provider="anthropic"):
    """reasons：models.list_dismiss_reasons() 返回的列表。返回 {"summary": "..."}。"""
    if not reasons:
        raise RuntimeError("还没有任何忽略原因记录，无法生成偏好档案。")

    reasons_block = "\n".join(_format_reason(r) for r in reasons)
    prompt = PROFILE_PROMPT.format(reasons_block=reasons_block)
    result = llm.ask_json(prompt, provider=provider, model=model)
    return {"summary": (result.get("summary") or "").strip()}
