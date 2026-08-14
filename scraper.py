"""Runs one search pass across Indeed + LinkedIn (via python-jobspy, unofficial
scraping — LinkedIn has no public job-search API) for every configured keyword,
dedupes against the local queue and the jd-resume-matcher xlsx tracker, and
stores new postings in the SQLite queue for manual review.
"""
import logging

from config import load_config
from models import get_conn, init_db, insert_job, log_run, job_exists, make_dedupe_key
from tracker_xlsx import existing_keys_from_tracker

logger = logging.getLogger(__name__)


def run_search_once():
    init_db()
    cfg = load_config()
    keywords = cfg["keywords"]
    locations = cfg["locations"] or [""]
    sites = cfg["sites"]
    results_wanted = cfg["results_wanted"]
    country_indeed = cfg["country_indeed"]
    tracker_keys = existing_keys_from_tracker(cfg.get("tracker_xlsx_path", ""))

    try:
        from jobspy import scrape_jobs
    except ImportError as e:
        conn = get_conn()
        log_run(conn, keywords, 0, 0, 0, error=f"python-jobspy not installed: {e}")
        conn.commit()
        conn.close()
        raise

    total_found = 0
    total_added = 0
    total_skipped = 0
    errors = []

    conn = get_conn()
    for keyword in keywords:
        for location in locations:
            try:
                df = scrape_jobs(
                    site_name=sites,
                    search_term=keyword,
                    location=location,
                    results_wanted=results_wanted,
                    country_indeed=country_indeed,
                )
            except Exception as e:
                logger.exception("search failed for %s / %s", keyword, location)
                errors.append(f"{keyword}/{location}: {e}")
                continue

            if df is None or df.empty:
                continue

            total_found += len(df)
            for _, row in df.iterrows():
                title = str(row.get("title") or "").strip()
                company = str(row.get("company") or "").strip()
                if not title or not company:
                    continue

                dedupe_key = make_dedupe_key(company, title)
                if dedupe_key in tracker_keys or job_exists(conn, dedupe_key):
                    total_skipped += 1
                    continue

                job = {
                    "title": title,
                    "company": company,
                    "location": str(row.get("location") or ""),
                    "site": str(row.get("site") or ""),
                    "job_url": str(row.get("job_url") or ""),
                    "date_posted": str(row.get("date_posted") or ""),
                    "keyword": keyword,
                    "jd_text": str(row.get("description") or ""),
                }
                if insert_job(conn, job):
                    total_added += 1

    log_run(
        conn,
        keywords,
        total_found,
        total_added,
        total_skipped,
        error="; ".join(errors) if errors else None,
    )
    conn.commit()
    conn.close()

    return {
        "found": total_found,
        "added": total_added,
        "skipped_duplicate": total_skipped,
        "errors": errors,
    }
