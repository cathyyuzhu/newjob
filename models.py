import sqlite3
from datetime import datetime

from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            site TEXT,
            job_url TEXT,
            date_posted TEXT,
            keyword TEXT,
            first_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            dedupe_key TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT NOT NULL,
            keywords TEXT,
            found INTEGER,
            added INTEGER,
            skipped_duplicate INTEGER,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def normalize(s):
    return (s or "").strip().lower()


def make_dedupe_key(company, title):
    return f"{normalize(company)}::{normalize(title)}"


def job_exists(conn, dedupe_key):
    row = conn.execute("SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    return row is not None


def insert_job(conn, job):
    dedupe_key = make_dedupe_key(job["company"], job["title"])
    if job_exists(conn, dedupe_key):
        return False
    conn.execute(
        """
        INSERT INTO jobs (title, company, location, site, job_url, date_posted, keyword, first_seen, status, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (
            job["title"],
            job["company"],
            job.get("location", ""),
            job.get("site", ""),
            job.get("job_url", ""),
            job.get("date_posted", ""),
            job.get("keyword", ""),
            datetime.now().isoformat(timespec="seconds"),
            dedupe_key,
        ),
    )
    return True


def log_run(conn, keywords, found, added, skipped_duplicate, error=None):
    conn.execute(
        """
        INSERT INTO search_runs (ran_at, keywords, found, added, skipped_duplicate, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            ", ".join(keywords),
            found,
            added,
            skipped_duplicate,
            error,
        ),
    )


def list_jobs(status=None):
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY first_seen DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY first_seen DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_job_status(job_id, status):
    conn = get_conn()
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    conn.close()


def list_runs(limit=20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM search_runs ORDER BY ran_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
