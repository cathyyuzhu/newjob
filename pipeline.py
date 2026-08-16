import logging
import os
import time
from datetime import date

import interview
import llm
from analyzer import analyze_job, classify_companies
from config import load_config
from job_state import (
    clear_batch_current,
    clear_discard,
    clear_queued,
    finish_analyzing,
    finish_interview_prep,
    in_progress_ids,
    mark_queued,
    reset_stop,
    set_batch_current,
    should_discard,
    start_analyzing,
    start_interview_prep,
    stop_requested,
)
from models import (
    get_job,
    insert_interview_prep,
    list_bank_items,
    list_jobs_missing_company_origin,
    list_jobs_missing_jd,
    list_jobs_needing_analysis,
    make_dedupe_key,
    replace_ai_bank_items,
    update_job_analysis,
    update_job_company_origin,
    update_job_error,
    update_job_jd_text,
)
from relevance import title_looks_relevant as _title_looks_relevant
from relevance import location_looks_relevant as _location_looks_relevant
from resume_docx import read_resume_text, write_tailored_resume
from scraper import refetch_job_jd
from tracker_utils import add_entry, list_entries

JD_MISSING_ERROR = "未获取到JD正文，已跳过AI分析（可点击「重新获取」重试抓取）"


def _safe_filename_part(s):
    return "".join(c for c in s if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")


def analyze_and_record(job_id):
    job = get_job(job_id)
    if not job:
        raise ValueError(f"job {job_id} not found")
    if not (job.get("jd_text") or "").strip():
        # 没有JD正文，LLM看不到任何真实职位内容，跑分析纯属白花钱（也是"nan"那次踩过的坑
        # 的另一面）。留给用户点"重新获取"重新抓一次，抓到了会自动接着分析。
        raise ValueError(JD_MISSING_ERROR)

    cfg = load_config()
    base_resume_path = cfg.get("base_resume_path") or os.path.expanduser(
        "~/Downloads/Cathy_Yang_Resume_EN_AI.docx"
    )
    tracker_path = cfg.get("tracker_xlsx_path") or os.path.expanduser(
        "~/Downloads/JD匹配追踪表.xlsx"
    )
    resume_output_dir = cfg.get("resume_output_dir") or os.path.dirname(base_resume_path)
    provider, model = llm.resolve(cfg)

    resume_text = read_resume_text(base_resume_path)

    result = analyze_job(
        company=job["company"],
        title=job["title"],
        jd_text=job.get("jd_text") or "",
        resume_text=resume_text,
        model=model,
        provider=provider,
    )

    if should_discard(job_id):
        # 这条职位是批量分析循环当前正在跑的那一条，LLM调用还没返回时用户就点了"停止分析"——
        # 网络请求没法从外部强行中断，只能等它自然跑完，跑完了就在这里把结果丢掉：不写库、
        # 不写追踪表、不生成定制简历，跟没跑过一样（那次LLM调用的钱已经花出去了，省不回来，
        # 但至少不会让用户没料到的分析结果突然冒出来）。丢弃标记本身的清理放在
        # analyze_and_record_safe() 的 finally 里统一做（保证LLM调用失败抛异常时也不会漏清）。
        return {"discarded": True}

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
        company_overview=result.get("company_overview"),
    )

    company_origin = result.get("company_origin")
    update_job_analysis(job_id, overall_match=overall, resume_path=resume_path, company_origin=company_origin)
    return {"overall_match": overall, "resume_path": resume_path, "status": status, "company_origin": company_origin}


def analyze_and_record_safe(job_id):
    start_analyzing(job_id)
    try:
        return analyze_and_record(job_id)
    except Exception as e:
        # 只写 analysis_error，不touch overall_match/resume_path/company_origin——
        # 否则"重试一次已经成功分析过的职位，这次失败了"会把之前的好结果冲掉（见
        # models.update_job_error 的说明）。
        update_job_error(job_id, str(e))
        raise
    finally:
        finish_analyzing(job_id)
        # 不管这次是正常完成、被丢弃还是报错，都清掉这条职位的"丢弃"标记（如果有的话）——
        # 否则LLM调用失败没走到 analyze_and_record() 里 should_discard() 检查那一步，
        # 标记会一直留着，导致这条职位下次重新分析（哪怕早就没人再点停止了）也被误判丢弃。
        clear_discard(job_id)


def queue_pending_jobs(job_ids=None, limit=None):
    """筛选出一批需要自动分析的职位并标成"排队中"（见 job_state.mark_queued），不实际
    调用LLM——纯本地DB读取+内存操作，很快，特意跟真正的分析循环（analyze_pending_jobs）
    拆开，好让调用方（app.py 的 /api/search/run）能在HTTP请求线程里同步调用它，在响应
    返回前就把排队状态写好。

    背景：如果"标记排队"和"真正分析"揉在一起放去后台线程跑，会有时序竞态——前端
    拿到搜索接口的响应后立刻刷新一次职位列表，这时后台线程可能还没来得及标记排队，
    前端这次刷新看到的职位没有任何 analysis_state，判断"当前没有职位在分析"就不会
    安排轮询（见 static/app.js scheduleAnalyzingPoll），之后也没有别的代码会再触发
    刷新——分析其实在后台正常跑完了，但页面上的按钮会一直停在"AI 分析"没反应，
    看起来像是"没有自动开始分析"（2026-08-15 实测复现）。

    标题跟搜索关键词、地点跟配置的城市列表完全不沾边的、或者没有JD正文的，会被跳过，
    不参与排队、不调用LLM（见 _title_looks_relevant / _location_looks_relevant）。

    job_ids：只考虑这些指定 id 的职位（用于"这次搜索新增的职位"这种场景，不会牵连
        数据库里其它还没分析的历史积压）。传 None 则考虑"待审核"（status='new'）里
        所有还没分析成功过的职位——包括历史积压，用于程序启动时的一次性补跑（配合
        limit）或顶部"AI分析"按钮手动触发全量分析（不传 limit）。
    limit：最多排队几条，None 表示不限。
    返回待分析的职位（dict）列表，供调用方传给 analyze_pending_jobs 执行真正的分析。

    调用即代表"要开始一个新批次"，顺带把上一次可能残留的"停止分析"标志清掉（见
    job_state.reset_stop()），并排除掉已经在排队/分析中的职位（见 job_state.in_progress_ids()）——
    后者是为了防止不同入口（"立即搜索一次"自动触发 vs 顶部"AI分析"按钮手动触发）在同一条
    职位还没跑完时重复排队，导致对同一条职位并发调用两次LLM。"""
    reset_stop()
    if job_ids is not None:
        candidates = [get_job(jid) for jid in job_ids]
        candidates = [j for j in candidates if j and j["status"] == "new" and j["overall_match"] is None]
    else:
        candidates = list_jobs_needing_analysis()

    in_progress = in_progress_ids()
    configured_locations = load_config().get("locations") or []
    relevant = [
        j for j in candidates
        if j["id"] not in in_progress
        and (j.get("jd_text") or "").strip()
        and _title_looks_relevant(j) and _location_looks_relevant(j, configured_locations)
    ]
    skipped = len(candidates) - len(relevant)
    if skipped:
        logging.info("skipped %s job(s) already in progress, missing JD text, or whose title/location doesn't match current search settings", skipped)

    to_analyze = relevant if limit is None else relevant[:limit]
    # 整批一次性标成"排队中"——分析是串行跑的（见下面"是否可以并发"的讨论，没做），
    # 排在后面的职位在真正轮到它之前跟"完全没提交"没法区分，先标一下，前端能提示用户
    # 这条已经排上队了，不是没反应。轮到某一条时 analyze_and_record_safe() 会把它从
    # queued 转成 analyzing。
    mark_queued([job["id"] for job in to_analyze])
    return to_analyze


def analyze_pending_jobs(job_ids=None, limit=None, jobs=None):
    """自动分析一批职位（最新的先分析）。

    jobs：调用方如果已经算好待分析列表并标记过排队（见 queue_pending_jobs），直接传
        进来，跳过重复筛选/标记；不传则按 job_ids/limit 自己现算一遍（比如程序启动时
        的一次性补跑，不需要抢在任何HTTP响应之前标记排队，没有上面说的竞态问题）。
    job_ids/limit：仅在 jobs=None 时生效，含义同 queue_pending_jobs。
    单条失败不影响其余条目（错误已经在 analyze_and_record_safe 里记录进
    该职位的 analysis_error 字段，这里只是防止异常中断整个批次）。

    每跑完一条就检查一次 job_state.stop_requested()（顶部"AI分析"按钮点"停止分析"时
    设置），设置了就不再继续下一条，并把剩下还没跑到的职位从"排队中"状态里清掉
    （见 job_state.clear_queued()），避免它们一直卡在"排队中"却没有循环会去处理。

    当前正在跑的这一条LLM调用是同步阻塞的网络请求，没法从外部强行中断，会在后台
    自然跑完——但如果用户是在它还没跑完时点的"停止"，跑完后这条职位的结果会被丢弃
    （不写库/不写追踪表，见 job_state.should_discard() 和 analyze_and_record() 里的
    对应检查），而不是正常记录下来；批量循环调用前后用 set_batch_current()/
    clear_batch_current() 标记"现在正在跑哪条"，供 request_stop() 判断该丢弃哪条。

    to_analyze 是排队时的快照，一个批次（尤其是顶部"AI分析"按钮不限量的全量批次）
    可能要跑很久，轮到某条职位真正分析前，重新查一次它现在的状态——如果用户这段
    等待期间已经把它标记"已忽略"，就跳过、不再浪费一次LLM调用（已忽略的职位不需要
    继续参与自动分析；用户如果后悔了，仍可以对着它手动点单条"AI 分析"）。"""
    to_analyze = jobs if jobs is not None else queue_pending_jobs(job_ids=job_ids, limit=limit)
    analyzed = 0
    for i, job in enumerate(to_analyze):
        if stop_requested():
            remaining_ids = [j["id"] for j in to_analyze[i:]]
            clear_queued(remaining_ids)
            logging.info("analysis stopped by user, %s job(s) left unqueued", len(remaining_ids))
            break
        current = get_job(job["id"])
        if not current or current["status"] == "dismissed":
            clear_queued([job["id"]])
            logging.info("skip analyzing job %s: dismissed before its turn", job["id"])
            continue
        set_batch_current(job["id"])
        try:
            result = analyze_and_record_safe(job["id"])
            if not result.get("discarded"):
                analyzed += 1
        except Exception:
            logging.exception("auto-analyze failed for job %s", job["id"])
        finally:
            clear_batch_current()
    return analyzed


_COMPANY_ORIGIN_BATCH_SIZE = 80  # 一次LLM调用里放多少个公司名，避免公司数太多时单次prompt/响应过长


def classify_company_origins(job_ids=None):
    """轻量批量判断一批职位的公司国籍归属（analyzer.classify_companies()），只需要
    公司名，不需要JD/简历，比完整AI匹配分析（analyze_and_record）快得多、几乎不花钱——
    用来让"外企/国内公司"筛选不用等职位排到完整分析才有结果。

    job_ids：只处理这些指定id里还没判断过（company_origin为空）的职位（比如某次搜索
        新增的职位）。传 None 则处理数据库里所有company_origin为空的职位，不限状态——
        因为足够便宜，不需要像完整分析那样限流/限量。
    按公司名去重后批量丢给LLM（每批最多 _COMPANY_ORIGIN_BATCH_SIZE 个），减少调用次数；
    同一批里某个职位判断失败不影响其它职位，只是那批公司会保持company_origin为空，
    之后重新触发（比如下次搜索/程序重启）会再试一次。"""
    if job_ids is not None:
        jobs = [get_job(jid) for jid in job_ids]
        jobs = [j for j in jobs if j and not j.get("company_origin")]
    else:
        jobs = list_jobs_missing_company_origin()
    if not jobs:
        return {"classified": 0, "companies": 0}

    companies = sorted({(j.get("company") or "").strip() for j in jobs if (j.get("company") or "").strip()})
    cfg = load_config()
    provider, model = llm.resolve(cfg)

    origin_by_company = {}
    for i in range(0, len(companies), _COMPANY_ORIGIN_BATCH_SIZE):
        chunk = companies[i : i + _COMPANY_ORIGIN_BATCH_SIZE]
        try:
            origin_by_company.update(classify_companies(chunk, model=model, provider=provider))
        except Exception:
            logging.exception("company origin classification failed for a batch of %s companies", len(chunk))

    classified = 0
    for job in jobs:
        origin = origin_by_company.get((job.get("company") or "").strip())
        if origin:
            update_job_company_origin(job["id"], origin)
            classified += 1
    return {"classified": classified, "companies": len(companies)}


def refetch_jd(job_id):
    """给单条职位重新抓一次JD正文（见 scraper.refetch_job_jd 的原理和局限）；抓到新正文
    就顺带自动跑一次AI分析（复用手动点"AI 分析"同一套 analyze_and_record_safe），抓不到
    就只留一条失败提示、保持原状态，用户可以再点一次重试。"""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"job {job_id} not found")

    jd_text = refetch_job_jd(job)
    if not jd_text:
        update_job_error(job_id, "重新抓取JD正文失败，仍未获取到内容，可稍后再试")
        return {"jd_fetched": False}

    update_job_jd_text(job_id, jd_text)
    analysis = analyze_and_record_safe(job_id)
    return {"jd_fetched": True, **analysis}


def refetch_missing_jd_jobs():
    """批量给"待审核"里JD正文为空的职位重新抓取一次。每条都是独立的一次搜索请求
    （jobspy没有按job_url直接取详情的接口，参见 scraper.refetch_job_jd），量大会比较慢，
    调用方（app.py）应该放后台线程跑。LinkedIn职位之间按配置的 linkedin_request_delay
    停顿一下，降低触发限流概率，跟 scraper.run_search_once() 是同一个考虑。
    单条失败不影响其余条目。"""
    jobs = list_jobs_missing_jd()
    delay = load_config().get("linkedin_request_delay") or 0
    refetched = 0
    for i, job in enumerate(jobs):
        try:
            result = refetch_jd(job["id"])
            if result.get("jd_fetched"):
                refetched += 1
        except Exception:
            logging.exception("refetch JD failed for job %s", job["id"])
        if delay and (job.get("site") or "").lower() == "linkedin" and i < len(jobs) - 1:
            time.sleep(delay)
    return {"attempted": len(jobs), "refetched": refetched}


# ---------------------------------------------------------------- 面试准备


def find_tracker_entry(company, title):
    """从追踪表里按 公司+职位名 找回这条职位对应的完整分析记录（没有则 None）。

    jobs 表和追踪表 xlsx 之间没有外键，一直是靠 make_dedupe_key(company, title) 关联的
    （前端 dedupeKey() 也是同一套规则）。这段查找原来只在 app.py 的 _find_cover_letter()
    里给 Easy Apply 取 cover letter 用，现在面试准备也要用它取已算好的任职要求/技能缺口/
    公司简介，抽到这里共用，避免两处各写一遍导致关联规则漂移。
    读表失败（文件不存在/被 Excel 独占打开等）不抛异常，返回 None 让调用方降级处理。"""
    try:
        cfg = load_config()
        tracker_path = cfg.get("tracker_xlsx_path") or os.path.expanduser("~/Downloads/JD匹配追踪表.xlsx")
        key = make_dedupe_key(company, title)
        for entry in list_entries(tracker_path):
            if make_dedupe_key(entry.get("company"), entry.get("job_title")) == key:
                return entry
    except Exception:
        logging.exception("读取追踪表失败，跳过（调用方会降级处理）")
    return None


def generate_interview_prep(job_id, round_label=None):
    """给一条职位生成面试准备材料，写入 interview_preps 表。返回新记录的 dict。"""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"job {job_id} not found")
    if not (job.get("jd_text") or "").strip():
        # 跟 analyze_and_record() 同一个理由：没有JD正文，LLM看不到任何真实职位内容，
        # 生成出来的"面试题"只能靠职位名瞎编，白花钱还误导人。
        raise ValueError(JD_MISSING_ERROR)

    cfg = load_config()
    base_resume_path = cfg.get("base_resume_path") or os.path.expanduser(
        "~/Downloads/Cathy_Yang_Resume_EN_AI.docx"
    )
    provider, model = llm.resolve(cfg)
    resume_text = read_resume_text(base_resume_path)

    # 已有的匹配分析结论（任职要求逐条 is_gap、技能缺口、公司简介）作为输入，不重新解析JD
    # ——见 interview.build_analysis_block() 的说明。没分析过的职位拿不到，LLM 会自行判断。
    analysis = find_tracker_entry(job["company"], job["title"])

    content = interview.generate_prep(
        company=job["company"],
        title=job["title"],
        jd_text=job.get("jd_text") or "",
        resume_text=resume_text,
        analysis=analysis,
        round_label=round_label,
        model=model,
        provider=provider,
    )

    prep_id = insert_interview_prep(
        job_id,
        content_json=interview.dumps(content),
        round_label=round_label,
        provider=provider,
        model=model,
    )
    return {"prep_id": prep_id, "questions": len(content.get("questions") or [])}


def generate_interview_prep_safe(job_id, round_label=None):
    """带状态标记和错误落库的外层包装（对应 analyze_and_record_safe 的角色）。
    失败时也往 interview_preps 写一行（只有 error），这样前端能看到"上次生成失败了、
    原因是什么"，而不是停在一个分不清"还没生成"还是"生成炸了"的空态。"""
    start_interview_prep(job_id)
    try:
        return generate_interview_prep(job_id, round_label=round_label)
    except Exception as e:
        # 失败行也记下 provider/model：排查"是不是换了个模型才开始炸"这类问题时，
        # 光有错误信息不够，得知道当时用的是谁。
        provider, model = llm.resolve(load_config())
        insert_interview_prep(
            job_id, error=str(e), round_label=round_label, provider=provider, model=model
        )
        raise
    finally:
        finish_interview_prep(job_id)


def _bank_context():
    """题库相关的三个入口（起草 / 每题对话 / 全局助手）都要的同一套上下文：
    简历原文、目标岗位方向、provider+model。抽出来免得三处各抄一遍解析逻辑。"""
    cfg = load_config()
    base_resume_path = cfg.get("base_resume_path") or os.path.expanduser(
        "~/Downloads/Cathy_Yang_Resume_EN_AI.docx"
    )
    provider, model = llm.resolve(cfg)
    # 找工作的方向直接用搜索关键词——用户搜什么岗位就是在准备什么岗位的面试，
    # 不额外加一个配置项让用户填第二遍。
    return read_resume_text(base_resume_path), cfg.get("keywords"), provider, model


def chat_bank_answer(item, lang, message, history=None):
    """跟 AI 聊一轮、打磨某一道题的答案。同步返回 {"reply", "answer"}。

    刻意**不写库**：AI 给的改写版只是候选，用户在页面上点「采用」再点「保存」才会落库。
    这样聊崩了也不会毁掉已经写好的答案。
    """
    resume_text, target_roles, provider, model = _bank_context()
    return interview.chat_bank_answer(
        item["question"],
        item["answer_en"] if lang == "en" else item["answer"],
        lang,
        message,
        history=history,
        resume_text=resume_text,
        target_roles=target_roles,
        model=model,
        provider=provider,
    )


def chat_bank_assistant(message, history=None):
    """全局题库助手：看整个题库做跨题诊断，返回 {"reply"}。同样不写库。"""
    resume_text, target_roles, provider, model = _bank_context()
    return interview.chat_bank_assistant(
        list_bank_items(),
        message,
        history=history,
        resume_text=resume_text,
        target_roles=target_roles,
        model=model,
        provider=provider,
    )


def generate_bank_draft():
    """给通用题库跑一次 AI 起草，合并进 interview_bank 表。

    调用方（app.py）负责先用 job_state.start_bank_generation() 抢占，跑完
    finish_bank_generation()——起草是全局单例操作，不像面试准备那样按职位区分。

    三个类别（自我介绍 / 通用问题 / STAR 故事库）**分三次调用 LLM、每段跑完立刻入库**：
    - 分三次是因为答案改成中英双语 + 分段之后，一次性出完的输出量会顶到 max_tokens 上限
      被截断（见 interview.BANK_SECTIONS 上面的注释）；
    - 每段单独入库是为了让前端 5 秒一次的轮询能看到题库一段一段填出来，而不是干等
      5-10 分钟什么都没有；
    - 一段炸了不影响另外两段，失败原因收进 failed_sections 一起返回给调用方去提示用户。

    返回 {"updated","added","skipped","failed_sections"}。三段全失败时抛异常，
    让 app.py 走原来那条"起草失败"的路径。
    """
    resume_text, target_roles, provider, model = _bank_context()

    stats = {"updated": 0, "added": 0, "skipped": 0, "failed_sections": []}
    for section in interview.BANK_SECTIONS:
        key = section["key"]
        try:
            items = interview.generate_bank_section(
                key,
                resume_text,
                target_roles=target_roles,
                # 把这一类已有的题目原文喂回去，让模型复用措辞、只补真正的新题。少了这一步，
                # 重新起草会因为模型换了说法而堆出一批意思重复的题（实测 16 条能变 28 条）。
                # 每轮重新查一次库：前一段刚写进去的内容要对后一段可见。
                existing_items=[i for i in list_bank_items() if i["category"] == key],
                model=model,
                provider=provider,
            )
            merged = replace_ai_bank_items(items)
            for k in ("updated", "added", "skipped"):
                stats[k] += merged[k]
        except Exception as e:
            logging.exception("interview bank section %s failed", key)
            stats["failed_sections"].append(f"{section['label']}：{e}")

    if len(stats["failed_sections"]) == len(interview.BANK_SECTIONS):
        raise RuntimeError("；".join(stats["failed_sections"]))
    return stats
