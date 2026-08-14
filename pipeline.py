import os
from datetime import date

from analyzer import analyze_job
from config import load_config
from models import get_job, update_job_analysis
from resume_docx import read_resume_text, write_tailored_resume
from tracker_utils import add_entry


def _safe_filename_part(s):
    return "".join(c for c in s if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")


def analyze_and_record(job_id):
    job = get_job(job_id)
    if not job:
        raise ValueError(f"job {job_id} not found")

    cfg = load_config()
    base_resume_path = cfg.get("base_resume_path") or os.path.expanduser(
        "~/Downloads/Cathy_Yang_Resume_EN_AI.docx"
    )
    tracker_path = cfg.get("tracker_xlsx_path") or os.path.expanduser(
        "~/Downloads/JD匹配追踪表.xlsx"
    )
    resume_output_dir = cfg.get("resume_output_dir") or os.path.dirname(base_resume_path)
    model = cfg.get("anthropic_model")

    resume_text = read_resume_text(base_resume_path)

    result = analyze_job(
        company=job["company"],
        title=job["title"],
        jd_text=job.get("jd_text") or "",
        resume_text=resume_text,
        model=model,
    )

    overall = result["overall_match"]
    requirement_items = [(item["text"], bool(item["is_gap"])) for item in result.get("requirement_items", [])]

    resume_path = None
    cover_letter = result.get("cover_letter") or None
    resume_bullets = result.get("resume_optimization_bullets") or None
    status = f"自动分析完成，匹配度{overall:.0%}"

    if overall >= 0.7 and result.get("needs_customization") and result.get("resume_paragraph_edits"):
        if resume_text is None:
            status += "；匹配度达标但未找到基础简历文件，未生成定制简历"
        else:
            fname = f"Cathy_Yang_Resume_EN_{_safe_filename_part(job['company'])}_{_safe_filename_part(job['title'])[:40]}.docx"
            resume_path = os.path.join(resume_output_dir, fname)
            write_tailored_resume(base_resume_path, resume_path, result["resume_paragraph_edits"])
    elif overall >= 0.7:
        status += "；判定不需要生成定制简历"
    else:
        status += "；低于70%阈值，未自动生成简历"

    add_entry(
        path=tracker_path,
        job_title=job["title"],
        job_url=job["job_url"],
        company=job["company"],
        overall_match=overall,
        job_content_bullets=result.get("job_content_bullets", []),
        requirement_items=requirement_items,
        skill_matched_bullets=result.get("skill_matched_bullets", []),
        skill_gap_bullets=result.get("skill_gap_bullets", []),
        experience_years=result.get("experience_years", ""),
        industry_bullets=result.get("industry_bullets", []),
        salary=result.get("salary", ""),
        team_bullets=result.get("team_bullets", []),
        location=result.get("location", ""),
        status=status,
        apply_date=date.today(),
        resume_optimization_bullets=resume_bullets,
        resume_path=resume_path,
        cover_letter=cover_letter,
    )

    update_job_analysis(job_id, status="analyzed", overall_match=overall, resume_path=resume_path)
    return {"overall_match": overall, "resume_path": resume_path, "status": status}


def analyze_and_record_safe(job_id):
    try:
        return analyze_and_record(job_id)
    except Exception as e:
        update_job_analysis(job_id, status="analysis_failed", error=str(e))
        raise
