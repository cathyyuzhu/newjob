"""Runs one search pass across Indeed + LinkedIn (via python-jobspy, unofficial
scraping — LinkedIn has no public job-search API) for every configured keyword,
dedupes against the local queue and the jd-resume-matcher xlsx tracker, and
stores new postings in the SQLite queue for manual review.
"""
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from config import load_config
from models import get_conn, init_db, insert_job, log_run, job_exists, make_dedupe_key, upgrade_to_linkedin_if_needed
from relevance import location_looks_relevant, title_looks_relevant
from tracker_xlsx import existing_keys_from_tracker

logger = logging.getLogger(__name__)


def _clean(val):
    """pandas 里缺失值是 float('nan')，是 truthy 的，`val or ""` 接不住它——
    直接 str() 会变成字面量 "nan" 存进数据库/发给LLM，而不是真正的空字符串。
    统一在这里把 nan/None 转成空字符串。"""
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s


def _ingest_df(df, conn, keyword, tracker_keys, configured_locations, stats, new_job_ids):
    """把一次 scrape_jobs() 返回的结果落库：清洗 → 去重键判断 → 标题/地点粗筛 → 入库。
    关键词×城市的常规搜索、和下面按公司定向的搜索共用同一套处理逻辑，避免两处各写
    一份、其中一处漏改标准（粗筛/去重规则）而悄悄跑偏。stats 在调用方之间累加，
    直接原地更新（found/added/skipped/skipped_irrelevant 四个 key 必须已存在）。
    """
    if df is None or df.empty:
        return
    stats["found"] += len(df)
    for _, row in df.iterrows():
        title = _clean(row.get("title"))
        company = _clean(row.get("company"))
        if not title or not company:
            continue

        dedupe_key = make_dedupe_key(company, title)
        job = {
            "title": title,
            "company": company,
            "location": _clean(row.get("location")),
            "site": _clean(row.get("site")),
            "job_url": _clean(row.get("job_url")),
            "date_posted": _clean(row.get("date_posted")),
            "keyword": keyword,
            "jd_text": _clean(row.get("description")),
        }

        if dedupe_key in tracker_keys or job_exists(conn, dedupe_key):
            stats["skipped"] += 1
            # 已经在库里的这条如果是 Indeed 版本、这次抓到的是同一条的 LinkedIn 版本，
            # 把库里那行的来源换成 LinkedIn（「只留 LinkedIn」，2026-08-18），不新插入一行。
            upgrade_to_linkedin_if_needed(conn, dedupe_key, job)
            continue

        # 标题/地点粗筛：Indeed/LinkedIn 自己的搜索匹配比较宽松（比如搜"Senior
        # Product Manager"会混进"Senior Premier Relationship Manager"这类只是
        # 碰巧共享"Senior"/"Manager"的不相关结果），跟关键词、配置城市完全不沾边
        # 的直接不入库，而不是等它们堆在待审核列表里靠人工/AI分析再筛一遍——复用
        # pipeline.py 批量分析前用的同一套判断逻辑（见 relevance.py），保证两处
        # 标准一致。
        if not title_looks_relevant(job) or not location_looks_relevant(job, configured_locations):
            stats["skipped_irrelevant"] += 1
            continue

        new_id = insert_job(conn, job)
        if new_id is not None:
            stats["added"] += 1
            new_job_ids.append(new_id)


def run_search_once():
    init_db()
    cfg = load_config()
    keywords = cfg["keywords"]
    locations = cfg["locations"] or [""]
    sites = cfg["sites"]
    results_wanted = cfg["results_wanted"]
    linkedin_results_wanted = cfg.get("linkedin_results_wanted") or results_wanted
    linkedin_request_delay = cfg.get("linkedin_request_delay") or 0
    country_indeed = cfg["country_indeed"]
    hours_old = int(cfg["days_old"]) * 24 if cfg.get("days_old") else None
    tracker_keys = existing_keys_from_tracker(cfg.get("tracker_xlsx_path", ""))
    configured_locations = cfg.get("locations") or []
    # 重点关注公司（2026-08-18）：普通关键词×城市搜索受 LinkedIn 排名和 results_wanted 条数
    # 上限影响，不保证这些公司的新职位每次都能被搜到——见下面 target_company_ids 那段。
    target_companies = [
        c for c in (cfg.get("linkedin_target_companies") or [])
        if c.get("status") == "resolved" and c.get("company_id")
    ]

    try:
        from jobspy import scrape_jobs
    except ImportError as e:
        conn = get_conn()
        log_run(conn, keywords, 0, 0, 0, error=f"python-jobspy not installed: {e}")
        conn.commit()
        conn.close()
        raise

    # LinkedIn 每条职位都要多发一次详情页请求才能拿到 JD 正文，短时间内量一大很容易被限流/
    # 拦截导致整批 JD 全部拿不到。拆成单独一次调用（更小的条数上限）、跑完停顿一下再进下一组，
    # 跟不需要额外请求的站点（Indeed 等）分开抓，互不拖累。
    # LinkedIn 这次调用不传 hours_old：LinkedIn 访客搜索接口一旦带上发布时间过滤参数（f_TPR）
    # 就会直接返回空结果（2026-08-15 抓包实测：加了这个参数后响应页面只有26字节，去掉立刻能
    # 抓到真实职位，任何取值的 days_old 都会导致 LinkedIn 那部分完全抓不到东西）。改成抓回来
    # 不限时间的结果后，在本地按 date_posted 字段自己按 days_old 过滤一遍。
    other_sites = [s for s in sites if s != "linkedin"]
    site_calls = []
    if other_sites:
        site_calls.append({"sites": other_sites, "results_wanted": results_wanted, "hours_old": hours_old, "is_linkedin": False})
    if "linkedin" in sites:
        site_calls.append({"sites": ["linkedin"], "results_wanted": linkedin_results_wanted, "hours_old": None, "is_linkedin": True})

    linkedin_cutoff = pd.Timestamp(datetime.now() - timedelta(hours=hours_old)) if hours_old else None

    stats = {"found": 0, "added": 0, "skipped": 0, "skipped_irrelevant": 0}
    new_job_ids = []
    errors = []

    conn = get_conn()
    for keyword in keywords:
        for location in locations:
            for call in site_calls:
                # LinkedIn 没有独立的国家参数（不像 Indeed 有 country_indeed），纯拿城市名去
                # 匹配它自己的地理位置库，某些城市名会被解析到完全不相关的地方——2026-08-15
                # 实测：location="Shanghai" 被解析成了美国 Richmond, VA，同样的搜索带上国家名
                # "Shanghai, China" 才能正确匹配到真实的上海职位（"Beijing"没这个问题，凑巧没
                # 有歧义，属于走运，不代表所有城市名都安全）。所以只有 LinkedIn 这次请求需要
                # 补上国家名，Indeed 已经有 country_indeed 参数不受影响。
                call_location = location
                if call["is_linkedin"] and location and "," not in location:
                    call_location = f"{location}, {country_indeed.title()}"
                try:
                    df = scrape_jobs(
                        site_name=call["sites"],
                        search_term=keyword,
                        location=call_location,
                        results_wanted=call["results_wanted"],
                        country_indeed=country_indeed,
                        hours_old=call["hours_old"],
                        # jobspy 默认不抓 LinkedIn 的完整职位描述（每条要多发一次请求，比较慢），
                        # 不开的话 description 基本是空的，JD 正文缺失、AI 分析没法提炼要点。
                        linkedin_fetch_description=True,
                    )
                except Exception as e:
                    logger.exception("search failed for %s / %s / %s", keyword, call_location, call["sites"])
                    errors.append(f"{keyword}/{call_location}/{','.join(call['sites'])}: {e}")
                    df = None

                if call["is_linkedin"] and linkedin_cutoff is not None and df is not None and not df.empty and "date_posted" in df.columns:
                    posted = pd.to_datetime(df["date_posted"], errors="coerce")
                    # 拿不到日期的条目不武断过滤掉（跟 relevance.py 地点粗筛同样的保守原则：
                    # 宁可多留、不要悄悄漏掉），只排除明确早于 days_old 窗口的条目。
                    df = df[posted.isna() | (posted >= linkedin_cutoff)]

                _ingest_df(df, conn, keyword, tracker_keys, configured_locations, stats, new_job_ids)

                if call["is_linkedin"] and linkedin_request_delay:
                    time.sleep(linkedin_request_delay)

            # 每个 关键词×城市 组合处理完就提交一次，而不是攒到整个搜索跑完才提交——
            # 一次搜索要挨个调用好几次网络抓取，全程占着写锁的话，跟后台自动分析同时
            # 跑很容易互相"database is locked"（配合 models.get_conn() 的 WAL + timeout）。
            conn.commit()

    # 重点关注公司：额外对每家公司单独跑一次 LinkedIn 定向搜索（linkedin_company_ids，
    # 对应 LinkedIn 官方搜索页的公司过滤器），沿用跟常规搜索一样的关键词列表——普通
    # 关键词×城市搜索受排名和 results_wanted 条数上限影响，不保证这些公司的新职位每次
    # 都能被搜到；这个过滤器让"这家公司下所有匹配关键词的职位"不受排名影响地被看到。
    # 不按城市循环：大型外企职位通常跨多个城市，交给 LinkedIn 自己按公司返回全量结果，
    # 免得再乘一个 locations 维度、额外请求量涨得太快。
    # 复用现有去重（job_exists）：已经在库里的职位自然被挡掉，不需要额外处理。
    if target_companies and "linkedin" in sites:
        for keyword in keywords:
            for company in target_companies:
                try:
                    df = scrape_jobs(
                        site_name=["linkedin"],
                        search_term=keyword,
                        results_wanted=linkedin_results_wanted,
                        country_indeed=country_indeed,
                        linkedin_fetch_description=True,
                        linkedin_company_ids=[int(company["company_id"])],
                    )
                except Exception as e:
                    logger.exception("company-targeted search failed for %s / %s", keyword, company.get("name"))
                    errors.append(f"{keyword}/{company.get('name')}: {e}")
                    df = None

                _ingest_df(df, conn, keyword, tracker_keys, configured_locations, stats, new_job_ids)

                if linkedin_request_delay:
                    time.sleep(linkedin_request_delay)
            conn.commit()

    log_run(
        conn,
        keywords,
        stats["found"],
        stats["added"],
        stats["skipped"],
        skipped_irrelevant=stats["skipped_irrelevant"],
        error="; ".join(errors) if errors else None,
    )
    conn.commit()
    conn.close()

    return {
        "found": stats["found"],
        "added": stats["added"],
        "skipped_duplicate": stats["skipped"],
        "skipped_irrelevant": stats["skipped_irrelevant"],
        "errors": errors,
        "new_job_ids": new_job_ids,
    }


def refetch_job_jd(job):
    """给单条职位重新抓一次JD正文。jobspy 没有"按 job_url 直接取详情"的接口，只能用它
    当初被搜到时的 关键词+城市+来源站点 重新跑一遍搜索，在结果里按 job_url 找回同一条
    （老数据没存 job_url 或站点已经变更过链接格式的话，退化成 company+title 的去重键匹配）。
    局限：如果这条职位现在已经不在该关键词/城市组合的最新一批结果里了（排名掉出去、
    下架又重新上架换了ID等），会找不到匹配、返回 None——调用方保留原状态，用户可以再点
    一次重试。没有记录关键词的老数据无法重新搜索，直接返回 None。
    """
    keyword = (job.get("keyword") or "").strip()
    site = (job.get("site") or "").strip().lower()
    # 没有关键词/站点没法重新定位到同一条搜索（也就无法套用下面 linkedin 专属的更小
    # results_wanted 节流），老数据缺这两个字段之一就直接放弃，不去猜——猜错了会在
    # 多站点搜索里把 linkedin 也套用通用的（更大的）results_wanted，容易触发限流。
    if not keyword or not site:
        return None

    try:
        from jobspy import scrape_jobs
    except ImportError:
        logger.warning("python-jobspy not installed, cannot refetch JD for job %s", job.get("id"))
        return None

    cfg = load_config()
    site_name = [site]
    results_wanted = (cfg.get("linkedin_results_wanted") or cfg["results_wanted"]) if site == "linkedin" else cfg["results_wanted"]

    try:
        df = scrape_jobs(
            site_name=site_name,
            search_term=keyword,
            location=job.get("location") or "",
            results_wanted=results_wanted,
            country_indeed=cfg["country_indeed"],
            # description本来就是要重新获取的东西，LinkedIn不开这个开关description基本是空的。
            linkedin_fetch_description=True,
        )
    except Exception:
        logger.exception("refetch JD search failed for job %s", job.get("id"))
        return None

    if df is None or df.empty:
        return None

    target_url = job.get("job_url") or ""
    if target_url:
        for _, row in df.iterrows():
            if _clean(row.get("job_url")) == target_url:
                return _clean(row.get("description")) or None

    target_key = make_dedupe_key(job.get("company"), job.get("title"))
    for _, row in df.iterrows():
        if make_dedupe_key(_clean(row.get("company")), _clean(row.get("title"))) == target_key:
            return _clean(row.get("description")) or None

    return None
