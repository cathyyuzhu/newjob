"""职位详情页的 AI 对话——针对某一条具体职位自由提问（"这家公司最近有裁员新闻吗"
"这个职级大概对应我现在的什么级别"之类），跟题库对话（interview.py）是姊妹功能，但
不套 JSON 输出：这里只需要一段回复文本，用户觉得有用会直接点「记进备注」把这段话原样
存下来（见 models.add_job_note），JSON 包一层反而要多一次序列化/反序列化。

对话本身不落库（跟题库对话同一个决策，见 spec/tech-solution.md）：刷新页面就清空，
notes 才是这场对话唯一的沉淀出口。
"""
import llm
from interview import sanitize_chat_history

MAX_CONTEXT_CHARS = 6000  # JD/简历原文喂进 system prompt 前的截断长度，控制 token 消耗

SYSTEM_TEMPLATE = """你是求职者的私人顾问，帮他更了解下面这条具体职位、判断要不要投、怎么投。

## 职位信息
公司：{company}
职位名称：{title}
JD正文：
{jd_text}

## 之前对这条职位做过的匹配分析结论（如果有）
{analysis_context}

## 求职者的简历（供你判断他的背景和这条职位的关系）
{resume_text}

## 你的任务
基于以上信息回答求职者关于这条职位的问题——可以是追问JD里没说清楚的地方、这家公司的
背景、这个职级大概什么水平、怎么准备这次投递等等。如果问题超出你的知识范围（比如需要
实时信息你答不上来），如实说不知道，不要编造。

用中文回答，直接给结论和依据，不要写成客套的开场白。回复保持简短，除非用户明确要求
展开讲。"""


def _truncate(text, limit=MAX_CONTEXT_CHARS):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def _format_analysis_context(tracker_entry):
    if not tracker_entry:
        return "（还没有做过匹配分析）"
    parts = []
    if tracker_entry.get("company_overview"):
        parts.append(f"公司简介：{tracker_entry['company_overview']}")
    if tracker_entry.get("skill_gap_bullets"):
        parts.append("技能缺口：" + "；".join(tracker_entry["skill_gap_bullets"]))
    if tracker_entry.get("requirement_items"):
        gaps = [i["text"] for i in tracker_entry["requirement_items"] if i.get("is_gap")]
        if gaps:
            parts.append("未达标的任职要求：" + "；".join(gaps))
    return "\n".join(parts) if parts else "（已分析，但没有明显的缺口/结论可摘要）"


def chat_about_job(job, tracker_entry, resume_text, message, history=None, model=None, provider="anthropic"):
    """跟职位AI对话一轮，返回纯文本回复。history/message 都是不可信的前端输入，
    走 interview.sanitize_chat_history 同一套清洗（角色校验 + 截断轮数）。"""
    message = (message or "").strip()
    if not message:
        raise RuntimeError("说点什么吧")

    system = SYSTEM_TEMPLATE.format(
        company=job.get("company") or "",
        title=job.get("title") or "",
        jd_text=_truncate(job.get("jd_text")) or "(未获取到JD正文)",
        analysis_context=_format_analysis_context(tracker_entry),
        resume_text=_truncate(resume_text) or "(未能读取简历内容)",
    )
    messages = sanitize_chat_history(history) + [{"role": "user", "content": message}]
    reply = llm.chat(messages, provider=provider, model=model, system=system)
    reply = (reply or "").strip()
    if not reply:
        raise RuntimeError("LLM 返回了空回复，请再问一次。")
    return reply
