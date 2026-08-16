import sqlite3
from datetime import datetime

from config import DB_PATH


def get_conn():
    # timeout=30：搜索（run_search_once）和后台自动分析可能同时想写库，SQLite 默认只给 5 秒
    # 就报 "database is locked"；WAL 模式让读不阻塞写、多个连接更容易并存，两者一起用
    # 基本能扛住这个程序里"边搜索边后台分析"的并发写场景，不需要引入额外的锁或队列。
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
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
            jd_text TEXT,
            first_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            dedupe_key TEXT NOT NULL UNIQUE,
            overall_match REAL,
            resume_path TEXT,
            analysis_error TEXT,
            company_origin TEXT,
            application_status TEXT NOT NULL DEFAULT 'not_applied',
            starred INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, ddl in (
        ("jd_text", "ALTER TABLE jobs ADD COLUMN jd_text TEXT"),
        ("overall_match", "ALTER TABLE jobs ADD COLUMN overall_match REAL"),
        ("resume_path", "ALTER TABLE jobs ADD COLUMN resume_path TEXT"),
        ("analysis_error", "ALTER TABLE jobs ADD COLUMN analysis_error TEXT"),
        # AI分析时顺带判断公司国籍归属（foreign/domestic/unknown），用于设置页"只看外企"过滤——
        # 见 analyzer.py 的 company_origin 字段。没有可靠的公司国籍数据源，只能靠LLM知识+JD线索判断，
        # 不保证100%准确。
        ("company_origin", "ALTER TABLE jobs ADD COLUMN company_origin TEXT"),
        # 投递状态跟踪（待投/已投递/面试中/已拒绝/Offer/已婉拒），跟 status（new/reviewed/
        # dismissed 审核工作流字段）是两个独立维度，不复用同一列——status 被
        # list_jobs_needing_analysis()/queue_pending_jobs() 等分析资格判断依赖，混用会
        # 破坏这些查询。NOT NULL DEFAULT 让 SQLite 自动把历史行回填成 'not_applied'。
        ("application_status", "ALTER TABLE jobs ADD COLUMN application_status TEXT NOT NULL DEFAULT 'not_applied'"),
        # "重点关注"标记（0/1），跟 status（新/已收藏/已忽略）也是独立维度：收藏是"这个职位
        # 我要留着"，重点关注是"这几条要优先盯"，一条职位可以只收藏不关注，也可以还在待审核
        # 阶段就先标上关注。SQLite 没有布尔类型，用 INTEGER 0/1，NOT NULL DEFAULT 0 让历史行
        # 自动回填成"未关注"。
        ("starred", "ALTER TABLE jobs ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in existing_cols:
            conn.execute(ddl)
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
    existing_run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(search_runs)")}
    if "skipped_irrelevant" not in existing_run_cols:
        # 标题/地点粗筛在入库前就跳过的噪音结果计数（见 scraper.py run_search_once()、
        # relevance.py），跟"去重跳过"（skipped_duplicate）是两回事，分开记录方便区分
        # "这次搜索有多少是真重复"和"有多少是Indeed/LinkedIn匹配太宽松的噪音"。
        conn.execute("ALTER TABLE search_runs ADD COLUMN skipped_irrelevant INTEGER")
    # 面试准备材料：一条职位可以有多份（比如二面前换个角度重新生成一份，历史保留可对照），
    # 所以是独立的表而不是 jobs 表上的列。存 SQLite 而不是追加到 JD匹配追踪表.xlsx，是因为
    # 那张表跟 jd-resume-matcher 技能共享，而且面试题/话术都是长文本列表，塞进单元格没法看。
    # 跟 jobs 表的关联沿用本项目一贯做法：只存 job_id，不建外键。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_preps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            round_label TEXT,
            content_json TEXT,
            error TEXT,
            llm_provider TEXT,
            llm_model TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interview_preps_job ON interview_preps(job_id)")
    # 通用面试题库：跟具体职位无关的个人标准答案（自我介绍、离职原因这类通用问题、
    # 可反复复用的 STAR 故事），一次准备好之后每场面试都能用，所以不挂在 job_id 上。
    # user_edited 是这张表的关键：AI 起草只是初稿，用户改过的答案才是"我的标准答案"，
    # 重新起草时绝不能覆盖（见 replace_ai_bank_items）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_bank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT,
            answer_en TEXT,
            user_edited INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
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
    """插入成功返回新记录的 id，重复（已存在）返回 None。"""
    dedupe_key = make_dedupe_key(job["company"], job["title"])
    if job_exists(conn, dedupe_key):
        return None
    cur = conn.execute(
        """
        INSERT INTO jobs (title, company, location, site, job_url, date_posted, keyword, jd_text, first_seen, status, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (
            job["title"],
            job["company"],
            job.get("location", ""),
            job.get("site", ""),
            job.get("job_url", ""),
            job.get("date_posted", ""),
            job.get("keyword", ""),
            job.get("jd_text", ""),
            datetime.now().isoformat(timespec="seconds"),
            dedupe_key,
        ),
    )
    return cur.lastrowid


def get_job(job_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_job_analysis(job_id, overall_match=None, resume_path=None, error=None, company_origin=None):
    """只更新分析结果字段，不碰 status（new/reviewed/dismissed 是用户的审核状态，
    跟"有没有分析成功"是两件事，不应该被分析结果覆盖掉）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE jobs SET overall_match = ?, resume_path = ?, analysis_error = ?, company_origin = ? WHERE id = ?",
        (overall_match, resume_path, error, company_origin, job_id),
    )
    conn.commit()
    conn.close()


def update_job_error(job_id, error):
    """只更新 analysis_error，不碰 overall_match/resume_path/company_origin——
    用于"重试一次已经成功过的分析，这次失败了"的场景：如果沿用 update_job_analysis()
    （它会把没传的字段一并写成 NULL），会把上一次成功分析的匹配度/简历路径/公司归属
    全部冲掉，用户点"AI 分析"重试一次网络抖动导致的失败，反而把已有的好结果弄丢了。"""
    conn = get_conn()
    conn.execute("UPDATE jobs SET analysis_error = ? WHERE id = ?", (error, job_id))
    conn.commit()
    conn.close()


def update_job_jd_text(job_id, jd_text):
    """"重新获取"JD正文成功后写入新正文，顺带清掉上一次残留的 analysis_error
    （比如"未获取到JD正文"这类过期提示，抓到正文后不该还显示）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE jobs SET jd_text = ?, analysis_error = NULL WHERE id = ?",
        (jd_text, job_id),
    )
    conn.commit()
    conn.close()


def log_run(conn, keywords, found, added, skipped_duplicate, skipped_irrelevant=0, error=None):
    conn.execute(
        """
        INSERT INTO search_runs (ran_at, keywords, found, added, skipped_duplicate, skipped_irrelevant, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            ", ".join(keywords),
            found,
            added,
            skipped_duplicate,
            skipped_irrelevant,
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


def list_jobs_needing_analysis():
    """待审核（status='new'）且还没有分析成功过（overall_match为空）的职位，最新的排最前面——
    包括从没分析过的和之前分析失败过的，都会被自动分析重试。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'new' AND overall_match IS NULL ORDER BY first_seen DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_jobs_missing_company_origin():
    """所有还没判断过公司国籍归属（company_origin为空）的职位，不限状态——批量轻量
    分类（见 pipeline.classify_company_origins）不需要等完整AI匹配分析，处理范围
    比"待审核"更宽，已收藏/已忽略的职位如果之前没分析过也会一起补上。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE company_origin IS NULL ORDER BY first_seen DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_job_company_origin(job_id, company_origin):
    """只更新 company_origin，不碰匹配度/简历路径等完整分析才会产出的字段——用于轻量
    批量分类（见 pipeline.classify_company_origins），跟完整AI分析是两条独立的写入路径。"""
    conn = get_conn()
    conn.execute("UPDATE jobs SET company_origin = ? WHERE id = ?", (company_origin, job_id))
    conn.commit()
    conn.close()


def list_jobs_missing_jd():
    """待审核（status='new'）且还没成功分析过、JD正文为空的职位，最新的排最前面——
    用于"重新获取JD正文"批量入口（见 pipeline.refetch_missing_jd_jobs）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'new' AND overall_match IS NULL "
        "AND (jd_text IS NULL OR TRIM(jd_text) = '') ORDER BY first_seen DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_job_status(job_id, status):
    conn = get_conn()
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    conn.close()


def set_job_starred(job_id, starred):
    conn = get_conn()
    conn.execute("UPDATE jobs SET starred = ? WHERE id = ?", (1 if starred else 0, job_id))
    conn.commit()
    conn.close()


def set_application_status(job_id, application_status):
    conn = get_conn()
    conn.execute("UPDATE jobs SET application_status = ? WHERE id = ?", (application_status, job_id))
    conn.commit()
    conn.close()


def delete_jobs(job_ids):
    """按 id 批量删除职位记录（不可逆）。返回实际删除的行数。"""
    if not job_ids:
        return 0
    conn = get_conn()
    placeholders = ",".join("?" for _ in job_ids)
    cur = conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", list(job_ids))
    conn.commit()
    conn.close()
    return cur.rowcount


def list_runs(limit=20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM search_runs ORDER BY ran_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 面试准备


def insert_interview_prep(job_id, content_json=None, round_label=None, error=None, provider=None, model=None):
    """写入一份面试准备材料。失败时也写一行（content_json 为空、error 有值），而不是
    什么都不留——不然用户点了"生成"之后页面永远是空态，看不出到底是还在跑还是失败了。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO interview_preps (job_id, created_at, round_label, content_json, error, llm_provider, llm_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            datetime.now().isoformat(timespec="seconds"),
            round_label,
            content_json,
            error,
            provider,
            model,
        ),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def list_interview_preps(job_id):
    """这条职位的全部面试准备材料，最新的排最前面（含失败记录）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interview_preps WHERE job_id = ? ORDER BY created_at DESC, id DESC", (job_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_interview_prep(job_id, success_only=False):
    """最新的一份。success_only=True 时只看生成成功的那些——用于"改成面试中要不要
    自动生成"的判断：上次失败过不算已经有材料，应该再试一次。"""
    conn = get_conn()
    sql = "SELECT * FROM interview_preps WHERE job_id = ?"
    if success_only:
        sql += " AND error IS NULL AND content_json IS NOT NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
    row = conn.execute(sql, (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def job_ids_with_interview_prep():
    """已经有成功生成过面试准备的职位id集合，供 /api/jobs 一次性附带给前端做卡片标记，
    避免前端逐条职位发一次请求（N+1）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT job_id FROM interview_preps WHERE error IS NULL AND content_json IS NOT NULL"
    ).fetchall()
    conn.close()
    return {r["job_id"] for r in rows}


def delete_interview_prep(prep_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM interview_preps WHERE id = ?", (prep_id,))
    conn.commit()
    conn.close()
    return cur.rowcount


# ---------------------------------------------------------------- 通用面试题库

BANK_CATEGORIES = ("self_intro", "common", "star_story")


def list_bank_items():
    """全部题库条目，按 类别 → sort_order → id 排序，前端直接分组渲染。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interview_bank ORDER BY "
        "CASE category WHEN 'self_intro' THEN 0 WHEN 'common' THEN 1 ELSE 2 END, sort_order, id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_bank_item(category, question, answer=None, answer_en=None, user_edited=1, sort_order=None):
    """新增一条。默认 user_edited=1——手动加的题就是用户自己的内容，
    不该被之后的 AI 起草覆盖掉。"""
    conn = get_conn()
    if sort_order is None:
        row = conn.execute(
            "SELECT MAX(sort_order) AS m FROM interview_bank WHERE category = ?", (category,)
        ).fetchone()
        sort_order = (row["m"] or 0) + 1
    cur = conn.execute(
        "INSERT INTO interview_bank (category, question, answer, answer_en, user_edited, sort_order, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            category,
            question,
            answer,
            answer_en,
            1 if user_edited else 0,
            sort_order,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def update_bank_item(item_id, question=None, answer=None, answer_en=None):
    """用户在页面上改答案时调用，顺带把这条标成 user_edited=1，之后 AI 重新起草
    会跳过它（见 replace_ai_bank_items）。只更新传进来的字段，None 表示不动。"""
    sets = ["user_edited = 1", "updated_at = ?"]
    params = [datetime.now().isoformat(timespec="seconds")]
    for col, val in (("question", question), ("answer", answer), ("answer_en", answer_en)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    params.append(item_id)
    conn = get_conn()
    cur = conn.execute(f"UPDATE interview_bank SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return cur.rowcount


def delete_bank_item(item_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM interview_bank WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return cur.rowcount


def replace_ai_bank_items(items):
    """把一批 AI 起草的条目合并进题库。items: [{category, question, answer, answer_en}]。

    合并规则（这张表最重要的一条逻辑）：
    - 同类别下问题文字相同的已有条目，只有在 user_edited=0（用户没改过）时才更新答案；
      用户手改过的一律跳过——AI 初稿只是起点，改过的才是"我的标准答案"，重新起草
      是为了补充没想到的问题，不是把用户的心血冲掉。
    - 没有对应条目的直接新增。
    - 不删除任何已有条目（AI 这次没生成到的题可能是用户手动加的，不能当成"过期"清掉）。
    返回 {"updated": n, "added": n, "skipped": n}——skipped 是被 user_edited 保护住的条数，
    调用方会把它告诉用户，让"改过的没被覆盖"这件事可见。
    """
    conn = get_conn()
    existing = {}
    for row in conn.execute("SELECT id, category, question, user_edited FROM interview_bank"):
        existing[(row["category"], (row["question"] or "").strip())] = row

    now = datetime.now().isoformat(timespec="seconds")
    stats = {"updated": 0, "added": 0, "skipped": 0}
    next_order = {}
    for item in items:
        category = item.get("category")
        question = (item.get("question") or "").strip()
        if category not in BANK_CATEGORIES or not question:
            continue
        row = existing.get((category, question))
        if row is not None:
            if row["user_edited"]:
                stats["skipped"] += 1
                continue
            conn.execute(
                "UPDATE interview_bank SET answer = ?, answer_en = ?, updated_at = ? WHERE id = ?",
                (item.get("answer"), item.get("answer_en"), now, row["id"]),
            )
            stats["updated"] += 1
            continue
        if category not in next_order:
            r = conn.execute(
                "SELECT MAX(sort_order) AS m FROM interview_bank WHERE category = ?", (category,)
            ).fetchone()
            next_order[category] = (r["m"] or 0) + 1
        conn.execute(
            "INSERT INTO interview_bank (category, question, answer, answer_en, user_edited, sort_order, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (category, question, item.get("answer"), item.get("answer_en"), next_order[category], now),
        )
        next_order[category] += 1
        stats["added"] += 1
    conn.commit()
    conn.close()
    return stats
