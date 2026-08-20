import re
import sqlite3
from datetime import datetime, timedelta

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
            starred INTEGER NOT NULL DEFAULT 0,
            tags TEXT,
            cover_letter TEXT,
            resume_bullets TEXT
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
        # 用户自定义标签，逗号分隔存一列（如 "AI,remote"）。没有做成 tags/job_tags 关联表：
        # 单用户本地库、一条职位撑死挂几个标签，筛选是在前端内存里对 allJobs 做的，关联表
        # 换来的只有多一次 JOIN 和一套增删同步逻辑。标签文本本身不允许含逗号（见 app.py
        # 的 /api/jobs/<id>/tags 校验），所以 split(",") 就是可靠的解析方式。
        ("tags", "ALTER TABLE jobs ADD COLUMN tags TEXT"),
        # Cover letter 全文和简历优化要点（JSON数组）。以前这两样只写进 JD匹配追踪表.xlsx，
        # 网页要知道某条职位有没有 cover letter，只能把整张 Excel 拉下来重新解析一遍——列表页
        # 每 4 秒轮询一次，代价完全不成比例。材料改成按需生成之后更需要落库：生成完要立刻
        # 反映到界面上，不能依赖 Excel 有没有被别的程序占用。追踪表照旧写，那是给
        # jd-resume-matcher 技能和用户自己看的另一份产物。
        ("cover_letter", "ALTER TABLE jobs ADD COLUMN cover_letter TEXT"),
        ("resume_bullets", "ALTER TABLE jobs ADD COLUMN resume_bullets TEXT"),
        # 投递时间：只在 application_status 变成 'applied' 时顺带记一笔（见 set_application_status），
        # 用于"每日任务清单"里"投了超过7天该跟进"这一项。不是完整的投递自动化（那需要 Easy Apply
        # 走完自动置状态、列表页"我投了"一键按钮，是另一件更大的事，这里只补最小的时间戳）。
        ("applied_at", "ALTER TABLE jobs ADD COLUMN applied_at TEXT"),
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
    # 职位备注：一条职位可以有很多条，来源分"手写"和"从AI对话里一键记下来的"。做成表而不是
    # jobs 表上一个 notes 大文本字段，是因为这几件事塞进一个字段就都做不了：单条删除、按时间
    # 倒序、标出哪条是AI说的（AI的话不该跟自己的判断混成一段无从分辨的文本）。职位AI对话本身
    # 不落库（跟题库对话保持同一个决策），notes 就是那场对话唯一的沉淀出口——用户觉得有用的
    # 那一段点一下存下来，面试准备页也读同一份。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_notes_job ON job_notes(job_id)")
    # 简历体检结果：整份简历的诊断 + 逐段改写建议。跟 interview_preps 同一个模式（含失败
    # 也落一行），但不挂 job_id——体检是针对简历本身的，跟具体投哪家无关。
    # resume_fingerprint 存体检那一刻简历文件的 mtime+size：用户换了简历之后，旧体检结论
    # 里的段落索引就对不上新文件了，前端靠它提示"简历已更新，建议重新体检"，而不是拿着
    # 过期建议去改一份不存在的段落。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS resume_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            resume_fingerprint TEXT,
            content_json TEXT,
            error TEXT,
            llm_provider TEXT,
            llm_model TEXT
        )
        """
    )
    # 忽略某条职位时顺手记一笔原因（预设标签+自由文本）。一条职位可以有多行——忽略/收藏/
    # 再忽略反复横跳时，每次的原因都是信号，不做成"每条职位只留一行"的覆盖式存储。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_dismiss_reasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            tags TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dismiss_reasons_job ON job_dismiss_reasons(job_id)")
    # 偏好档案：攒够一批忽略原因后一次 LLM 调用总结出来的一段话，全局单例（不挂 job_id），
    # 跟 resume_reviews 同一个模式——失败也落一行，source_reason_count 记录"生成这份档案时
    # 一共有多少条原因记录"，用于判断攒够新原因时要不要重新生成（见 pipeline.py）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS preference_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source_reason_count INTEGER NOT NULL DEFAULT 0,
            content_text TEXT,
            error TEXT,
            llm_provider TEXT,
            llm_model TEXT
        )
        """
    )
    # 每日任务清单里用户自己加的待办条目（跟自动生成的几项不同，这是真正需要持久化的
    # 待办，勾掉即删除，不需要"已完成"归档状态）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checklist_custom_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    _migrate_dedupe_keys(conn)
    _merge_cross_source_duplicates(conn)
    conn.close()


# 去重键归一化的噪声字符：跟题库去重（normalize_bank_question）思路一致——职位名/公司名
# 只差标点、空格、大小写的两条记录，业务含义上是完全一样的（2026-08-18 实测案例：
# "Associate Director, Product Development" vs "Associate Director Product Development"，
# 一个逗号让原来只做大小写折叠的 normalize() 判断成两条不同职位）。不做词根收敛（比如
# 去掉"Senior"/"资深"）——那类词承载真实的职级差异，收敛了会把本该分开的职位悄悄合并。
_DEDUPE_NOISE_RE = re.compile(
    r"[\s　。，、；：？！…—～·「」『』（）【】《》"
    r"\.,;:?!\-–—_/\\|<>\[\]\(\)\{\}'\"`~@#$%^&*+=]+"
)

# 公司名常见法律实体后缀：同一家公司在不同职位描述里抬头带不带这些后缀并不一致
# （2026-08-18 实测：库里 "Amazon.com" 和 "Amazon" 被当成两家不同公司，导致同一雇主的
# 职位完全分不到一组）。只去后缀本身，不做更激进的公司名归一（比如缩写展开）——缩写
# 映射要维护一张长期会漂移的表，收益不确定，先只处理这个已验证存在的具体问题。
_COMPANY_SUFFIX_RE = re.compile(
    r"[,，]?\s*(\.com|\.cn|inc|ltd|llc|corp|corporation|limited|company|co)\.?\s*$",
    re.IGNORECASE,
)
_COMPANY_SUFFIX_CN_RE = re.compile(r"(股份有限公司|有限公司|集团|公司)$")


def normalize(s):
    s = (s or "").strip().lower()
    return _DEDUPE_NOISE_RE.sub("", s)


def normalize_company(s):
    """公司名归一化：先剥掉法律实体后缀（可能叠加，如"XYZ Inc."里 . 和 Inc 分两步剥），
    再走跟职位名一样的标点/空白/大小写清理。只用于去重键和职位分组——展示用的公司名
    不经过这个函数，保留原始抓取数据里的写法。"""
    s = (s or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _COMPANY_SUFFIX_RE.sub("", s).strip()
        s = _COMPANY_SUFFIX_CN_RE.sub("", s).strip()
    return normalize(s)


def make_dedupe_key(company, title):
    return f"{normalize_company(company)}::{normalize(title)}"


def _migrate_dedupe_keys(conn):
    """去重键归一化规则加强后（2026-08-18），历史行的 dedupe_key 是用旧规则算的，不重算
    的话新抓到的职位就算标题只差一个逗号也判断不出"这是同一条"。按新规则重算所有行；
    如果两行重算后撞了同一个 key（说明这两行本来就该合并，比如上面那条 HSBC 案例），
    只更新较早那行（first_seen 更早）的 key，另一行的 dedupe_key 保持不变——不删除、
    不合并任何历史数据，只是不让它去抢一个已被占用的新 key，也不悄悄改变你已经对这条
    记录做过的忽略/收藏决定。幂等：已经是新格式的行重算结果不变，重复运行无副作用。
    """
    rows = conn.execute(
        "SELECT id, company, title, dedupe_key FROM jobs ORDER BY first_seen ASC, id ASC"
    ).fetchall()
    claimed = {row["dedupe_key"] for row in rows}
    for row in rows:
        new_key = make_dedupe_key(row["company"], row["title"])
        if new_key == row["dedupe_key"] or new_key in claimed:
            continue
        try:
            conn.execute("UPDATE jobs SET dedupe_key = ? WHERE id = ?", (new_key, row["id"]))
        except sqlite3.IntegrityError:
            continue
        claimed.discard(row["dedupe_key"])
        claimed.add(new_key)
    conn.commit()


# 投递状态的"进度"排序，只用于下面挑「哪一行该留下」——数字越大越靠前。不复用
# job_state.py 那套（那是分析队列的运行时状态，跟这里的"值不值得当保留依据"是两回事）。
_APPLICATION_PROGRESS_RANK = {
    "offer": 4, "interviewing": 3, "applied": 2,
    "not_applied": 1, "rejected": 1, "declined": 1,
}


def _merge_cross_source_duplicates(conn):
    """LinkedIn 和 Indeed 抓到同一条职位时会各生成一行——标题、公司归一化后完全相同，
    不是"近似"（那是 annotate_similar_groups() 管的、跨公司也可能命中的模糊相似度，
    风险和确定性都不一样），是同一条招聘信息被两个源都收录了。2026-08-18 使用者要求
    "只留 LinkedIn"：合并成一行，保留进度更靠前的那行（投递状态 > 是否标星，看
    _APPLICATION_PROGRESS_RANK），进度打平时优先留 LinkedIn 那行；如果留下来的那行
    恰好是 Indeed 来源、但另一行是 LinkedIn，把留下来那行的 site/job_url/jd_text 换成
    LinkedIn 的版本——"留哪行的状态"和"最终链接指向哪个源"是两件事，前者按进度选，
    后者只要有 LinkedIn 版本就优先用。被合并掉那行如果自己名下有备注/面试准备材料，
    先过户给留下来的那行再删除，不丢数据。

    跟 _migrate_dedupe_keys()「绝不删除、绝不合并」的原则刻意不同：那个函数处理的是
    "任意撞车"（任何原因导致新规则算出同一个 key，包括同源、包括还没验证过是不是真的
    同一条），保守处理更安全；这里只处理"新规则算出来 company+title 完全相同、且一边
    linkedin 一边 indeed"这个更窄、更确定的场景，可以放心当成真重复来合并。
    """
    from collections import defaultdict

    rows = conn.execute(
        "SELECT id, company, title, site, application_status, starred, job_url, jd_text "
        "FROM jobs"
    ).fetchall()
    by_key = defaultdict(list)
    for row in rows:
        by_key[make_dedupe_key(row["company"], row["title"])].append(row)

    def progress_score(row):
        rank = _APPLICATION_PROGRESS_RANK.get(row["application_status"], 1)
        return (rank, 1 if row["starred"] else 0)

    for group in by_key.values():
        sites = {row["site"] for row in group}
        if len(group) < 2 or "linkedin" not in sites or "indeed" not in sites:
            continue  # 只处理跨源都出现的情况；同源撞车不是这个函数管的

        ranked = sorted(group, key=lambda r: (progress_score(r), r["site"] == "linkedin"), reverse=True)
        winner, losers = ranked[0], ranked[1:]

        if winner["site"] != "linkedin":
            linkedin_loser = next((r for r in losers if r["site"] == "linkedin"), None)
            if linkedin_loser is not None:
                conn.execute(
                    "UPDATE jobs SET site = 'linkedin', job_url = ?, "
                    "jd_text = COALESCE(NULLIF(?, ''), jd_text) WHERE id = ?",
                    (linkedin_loser["job_url"], linkedin_loser["jd_text"], winner["id"]),
                )

        for loser in losers:
            for table in ("interview_preps", "job_notes", "job_dismiss_reasons"):
                conn.execute(f"UPDATE {table} SET job_id = ? WHERE job_id = ?", (winner["id"], loser["id"]))
            conn.execute("DELETE FROM jobs WHERE id = ?", (loser["id"],))

    conn.commit()


_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")
_TITLE_STOPWORDS = {
    "senior", "sr", "junior", "jr", "manager", "product", "the", "and", "of",
    "for", "a", "an", "to", "in", "on", "at", "with", "tech", "ai", "team",
    "lead", "staff", "principal",
}
# 2026-08-18 用真实数据验证的阈值：能分开 Amazon 一批近似 AI PM 岗位（相似度 0.5~1.0），
# 同时不会误合并产品线完全不同的职位（如 Blizzard 的 Hearthstone/WoW 两个团队经理，
# 相似度 0.33，正确地没被合并）。
_SIMILAR_GROUP_THRESHOLD = 0.5


def _title_tokens(title):
    toks = _TITLE_TOKEN_RE.findall((title or "").lower())
    return {t for t in toks if t not in _TITLE_STOPWORDS and len(t) > 1}


def annotate_similar_groups(jobs):
    """给同公司下标题高度相似的职位（如同一家公司同时开的多个相近方向岗位）打一个共同的
    similar_group_id，供前端折叠展示成一组——不改变、不合并任何数据。同公司真的开了很多
    个不同方向岗位时很常见（如 Amazon 一家占了库里 10 条近似 PM 岗），逐条铺满列表会让
    "这批我已经决定过了"这件事在视觉上被放大成很多条独立的忽略动作，其实是一次判断。

    只在同一家公司内部两两比较标题的词汇 Jaccard 相似度（去掉通用词后），不跨公司比较，
    也不引入 LLM 调用——纯规则、可解释、对这批数据量（单用户本地库）零性能顾虑。
    """
    from collections import defaultdict

    for j in jobs:
        j.setdefault("similar_group_id", None)
        j.setdefault("duplicate_of_applied", None)

    by_company = defaultdict(list)
    for j in jobs:
        by_company[normalize_company(j.get("company"))].append(j)

    group_seq = 0
    for company_key, group_jobs in by_company.items():
        if len(group_jobs) < 2 or not company_key:
            continue
        toksets = [_title_tokens(j.get("title")) for j in group_jobs]
        parent = list(range(len(group_jobs)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        for i in range(len(group_jobs)):
            for j in range(i + 1, len(group_jobs)):
                a, b = toksets[i], toksets[j]
                if not a or not b:
                    continue
                if len(a & b) / len(a | b) >= _SIMILAR_GROUP_THRESHOLD:
                    union(i, j)

        clusters = defaultdict(list)
        for i in range(len(group_jobs)):
            clusters[find(i)].append(i)

        for members in clusters.values():
            if len(members) < 2:
                continue
            group_seq += 1
            gid = f"{company_key}-{group_seq}"
            for idx in members:
                group_jobs[idx]["similar_group_id"] = gid

            # 组里如果已经有一条投递过/在面试，其余几条大概率是同一个岗位换了个标题重新
            # 挂出来的（Amazon 尤其常见）。标星的职位故意不参与上面的折叠展示（避免削弱
            # "我特意标星"这个动作），但这个提示不折叠、只是提醒，标星的也照样打上。
            advanced = [group_jobs[idx] for idx in members
                        if group_jobs[idx].get("application_status") in ("applied", "interviewing")]
            if advanced:
                best = next((j for j in advanced if j.get("application_status") == "interviewing"), advanced[0])
                for idx in members:
                    j = group_jobs[idx]
                    if j is best:
                        continue
                    j["duplicate_of_applied"] = {
                        "id": best.get("id"),
                        "title": best.get("title"),
                        "application_status": best.get("application_status"),
                    }

    return jobs


def job_exists(conn, dedupe_key):
    row = conn.execute("SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    return row is not None


def upgrade_to_linkedin_if_needed(conn, dedupe_key, candidate):
    """入库判重时命中已有的一行：如果那行来自 Indeed、这次新抓到的同一条是 LinkedIn
    版本，把它的 site/job_url/jd_text 换成 LinkedIn 的，其余状态（status/starred/
    application_status/notes 等）原样不动，也不新插入一行。这是「只留 LinkedIn」在
    "以后新抓到的"这一侧的落地；库里已经攒下的历史重复由 _merge_cross_source_
    duplicates() 处理。"""
    if candidate.get("site") != "linkedin":
        return
    row = conn.execute("SELECT id, site FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    if row is None or row["site"] == "linkedin":
        return
    conn.execute(
        "UPDATE jobs SET site = 'linkedin', job_url = ?, "
        "jd_text = COALESCE(NULLIF(?, ''), jd_text) WHERE id = ?",
        (candidate.get("job_url", ""), candidate.get("jd_text", ""), row["id"]),
    )
    conn.commit()


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


def set_job_tags(job_id, tags):
    """写入标签。tags 传字符串列表，空列表写 NULL（而不是空字符串），让"没有标签"在库里
    只有一种表示，前端判断不用同时考虑 '' 和 None。清洗/去重/长度限制在调用方做
    （见 app.py 的 normalize_tags）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE jobs SET tags = ? WHERE id = ?",
        (",".join(tags) if tags else None, job_id),
    )
    conn.commit()
    conn.close()


def update_job_materials(job_id, resume_path=None, cover_letter=None, resume_bullets=None):
    """写入按需生成出来的定制简历路径 / cover letter / 简历优化要点（JSON字符串）。
    跟 update_job_analysis 分开：材料生成已经从AI匹配分析里拆出来了（用户点按钮才跑），
    两条写入路径互不相干，混用会让"重新生成一次材料"顺手把匹配度写成 NULL。"""
    conn = get_conn()
    conn.execute(
        "UPDATE jobs SET resume_path = ?, cover_letter = ?, resume_bullets = ? WHERE id = ?",
        (resume_path, cover_letter, resume_bullets, job_id),
    )
    conn.commit()
    conn.close()


def list_jobs_missing_cover_letter():
    """已经分析过、但库里还没有 cover letter 的职位——只用于 cover_letter/resume_bullets
    两列刚加上时，从历史追踪表里一次性回填（见 app.py 的 _backfill_materials_from_tracker）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE overall_match IS NOT NULL AND cover_letter IS NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_job_note(job_id, content, source="manual"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO job_notes (job_id, content, source, created_at) VALUES (?, ?, ?, ?)",
        (job_id, content, source, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    note_id = cur.lastrowid
    conn.close()
    return note_id


def list_job_notes(job_id):
    """某条职位的全部备注，最新的排最前面——备注是随手记的，刚记的那条最可能是在找的那条。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM job_notes WHERE job_id = ? ORDER BY created_at DESC, id DESC", (job_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_job_note(note_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM job_notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()
    return cur.rowcount


def note_counts():
    """{job_id: 备注条数}，一次查询取回，供职位列表给卡片挂"📝 N"角标——
    跟 job_ids_with_interview_prep() 同样的用意：避免逐条职位查一次库（N+1）。"""
    conn = get_conn()
    rows = conn.execute("SELECT job_id, COUNT(*) AS n FROM job_notes GROUP BY job_id").fetchall()
    conn.close()
    return {r["job_id"]: r["n"] for r in rows}


def set_application_status(job_id, application_status):
    conn = get_conn()
    if application_status == "applied":
        # 只在"变成已投递"这一刻记一次时间，改成其它状态不清空——万一改错又改回来，
        # 不需要精确记录"第几次投的"，只要"最近一次是什么时候投的"够跟进提醒用就行。
        row = conn.execute("SELECT application_status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row and row["application_status"] != "applied":
            conn.execute(
                "UPDATE jobs SET application_status = ?, applied_at = ? WHERE id = ?",
                (application_status, datetime.now().isoformat(timespec="seconds"), job_id),
            )
        else:
            conn.execute("UPDATE jobs SET application_status = ? WHERE id = ?", (application_status, job_id))
    else:
        conn.execute("UPDATE jobs SET application_status = ? WHERE id = ?", (application_status, job_id))
    conn.commit()
    conn.close()


def list_stale_applications(days=7):
    """已投递（application_status='applied'）超过 days 天还没有更新过状态的职位——
    "每日任务清单"里"该跟进了"这一项。只看 applied_at 有值的行：没有时间戳的（比如
    applied_at 这一列刚加上时已经是 applied 状态的历史数据）没法判断投了多久，不武断
    地当成"超过7天"去提醒，避免翻旧账式的误报。"""
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM jobs WHERE application_status = 'applied' AND applied_at IS NOT NULL AND applied_at <= ? "
        "ORDER BY applied_at ASC",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


# ---------------------------------------------------------------- 简历体检


def insert_resume_review(content_json=None, fingerprint=None, error=None, provider=None, model=None):
    """写入一次简历体检结果。跟 insert_interview_prep 一样，失败也写一行（只有 error），
    否则用户点完"开始体检"看到的还是空页面，分不清是没跑过还是跑挂了。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO resume_reviews (created_at, resume_fingerprint, content_json, error, llm_provider, llm_model) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now().isoformat(timespec="seconds"),
            fingerprint,
            content_json,
            error,
            provider,
            model,
        ),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_latest_resume_review(success_only=False):
    conn = get_conn()
    sql = "SELECT * FROM resume_reviews"
    if success_only:
        sql += " WHERE error IS NULL AND content_json IS NOT NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
    row = conn.execute(sql).fetchone()
    conn.close()
    return dict(row) if row else None


def list_resume_reviews(limit=20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM resume_reviews ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_jobs_with_tailored_resume():
    """已经生成过定制简历的职位，最新的排前面。「我的简历」页的"定制简历"列表读这个——
    以前这些文件只在职位详情弹窗里露一次面，关掉就再也找不到了。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, company, title, overall_match, resume_path, job_url FROM jobs "
        "WHERE resume_path IS NOT NULL AND resume_path != '' ORDER BY overall_match DESC, id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 通用面试题库

# work_history（讲述过往工作）是后加的（2026-08-16）。category 是纯 TEXT、没有 CHECK 约束，
# 所以加类别不需要改表结构、也不需要迁移脚本。
BANK_CATEGORIES = ("self_intro", "star_story", "work_history", "common")

# 页面上的区块顺序：先自我介绍，再讲故事，再逐段过往工作，最后才是那些通用套题。
# 排序写在 SQL 里而不是前端，是为了让全局助手喂给 LLM 的题库快照（build_bank_block）
# 和用户在页面上看到的顺序一致。
_BANK_CATEGORY_ORDER = "CASE category " + " ".join(
    f"WHEN '{c}' THEN {i}" for i, c in enumerate(BANK_CATEGORIES)
) + " ELSE 99 END"


def list_bank_items():
    """全部题库条目，按 类别 → sort_order → id 排序，前端直接分组渲染。"""
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM interview_bank ORDER BY {_BANK_CATEGORY_ORDER}, sort_order, id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_bank_item(item_id):
    """单条题库条目，没有就返回 None。对话接口要拿题目原文和当前答案喂给 prompt。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM interview_bank WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


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


# 归一化问题文字时要抹掉的东西：所有空白 + 中英文标点。
# 之所以抹这么狠：合并靠"这道题是不是已经有了"来判断，而 LLM 每次生成的措辞都会飘一点，
# 光是「未来3-5年你的职业规划是什么？」→「未来 3-5 年职业规划是什么？」这种只差空格的
# 改写，就会让完全相等的字符串比较失手，同一道题在题库里堆成两条。实测起草两次，
# 16 条变 28 条、只有 4 条对上了。抹掉标点和空白之后这类飘动就吃掉了。
_BANK_Q_NOISE_RE = re.compile(
    r"[\s　。，、；：？！…—～·「」『』（）【】《》"
    r"\.,;:?!\-–—_/\\|<>\[\]\(\)\{\}'\"`~@#$%^&*+=]+"
)


def normalize_bank_question(text):
    """把问题文字压成用来判重的 key（抹掉空白/标点、英文转小写）。

    只用于匹配，不改动库里存的原文——展示还是用 AI 或用户写的那句原话。
    注意它吃不掉真正的改写（「为什么离开上一家？」vs「为什么离开上一家 / 这次为什么想看
    外部机会？」归一化后仍是两个 key），那一层靠起草 prompt 把已有题目喂回给
    模型、要求复用原措辞来解决。两层配合才能让"重新起草"真的是补充而不是堆重复。
    """
    return _BANK_Q_NOISE_RE.sub("", (text or "")).lower()


def replace_ai_bank_items(items):
    """把一批 AI 起草的条目合并进题库。items: [{category, question, answer, answer_en}]。

    合并规则（这张表最重要的一条逻辑）：
    - 同类别下问题**归一化后**相同的已有条目（见 normalize_bank_question），只有在
      user_edited=0（用户没改过）时才更新答案；用户手改过的一律跳过——AI 初稿只是起点，
      改过的才是"我的标准答案"，重新起草是为了补充没想到的问题，不是把用户的心血冲掉。
    - 没有对应条目的直接新增。
    - 不删除任何已有条目（AI 这次没生成到的题可能是用户手动加的，不能当成"过期"清掉）。
    返回 {"updated": n, "added": n, "skipped": n}——skipped 是被 user_edited 保护住的条数，
    调用方会把它告诉用户，让"改过的没被覆盖"这件事可见。
    """
    conn = get_conn()
    existing = {}
    # 按 id 升序遍历：库里可能已经躺着归一化后重复的历史数据（这个 bug 修之前堆进去的），
    # 那种情况下认最早的那条，行为才是确定的。
    for row in conn.execute(
        "SELECT id, category, question, user_edited FROM interview_bank ORDER BY id"
    ):
        existing.setdefault((row["category"], normalize_bank_question(row["question"])), row)

    now = datetime.now().isoformat(timespec="seconds")
    stats = {"updated": 0, "added": 0, "skipped": 0}
    next_order = {}
    seen_in_batch = set()
    for item in items:
        category = item.get("category")
        question = (item.get("question") or "").strip()
        if category not in BANK_CATEGORIES or not question:
            continue
        key = (category, normalize_bank_question(question))
        if not key[1]:
            continue  # 问题只剩标点，当成空题跳过
        # 同一批里出现两道归一化后一样的题时，只认第一道——否则这一次起草自己就会往库里
        # 插两条重复的。
        if key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        row = existing.get(key)
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


# ---------------------------------------------------------------- 忽略原因 / 偏好档案


def add_dismiss_reason(job_id, tags, note):
    """记一次忽略原因。tags 传列表（沿用 tags 列同款的逗号分隔存法），note 是自由文本，
    两者都可以为空（用户跳过了原因弹窗，只是单纯忽略）——但那种情况下调用方不该调用
    这个函数，见 app.py 的校验。返回新插入行的 id。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO job_dismiss_reasons (job_id, tags, note, created_at) VALUES (?, ?, ?, ?)",
        (job_id, ",".join(tags) if tags else None, note or None, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def count_dismiss_reasons():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM job_dismiss_reasons").fetchone()
    conn.close()
    return row["n"]


def list_dismiss_reasons(limit=None):
    """给偏好档案生成 prompt 用，带上关联职位的公司/职位名（帮助 LLM 看出具体案例，
    而不是只有干巴巴的标签词）。倒序：最近的排最前面。"""
    conn = get_conn()
    sql = (
        "SELECT r.id, r.tags, r.note, r.created_at, j.company, j.title "
        "FROM job_dismiss_reasons r LEFT JOIN jobs j ON j.id = r.job_id "
        "ORDER BY r.created_at DESC, r.id DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_dismiss_reason_for_job(job_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM job_dismiss_reasons WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def job_ids_with_dismiss_reason():
    """已经补过忽略原因的职位id集合，供 /api/jobs 一次性附带（同 job_ids_with_interview_prep
    的用意），前端据此判断"记录忽略原因"这个补录入口要不要显示。"""
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT job_id FROM job_dismiss_reasons").fetchall()
    conn.close()
    return {r["job_id"] for r in rows}


def insert_preference_profile(content_text=None, source_reason_count=0, error=None, provider=None, model=None):
    """写入一次偏好档案生成结果。跟 insert_resume_review 同一个模式：失败也落一行。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO preference_profiles (created_at, source_reason_count, content_text, error, llm_provider, llm_model) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now().isoformat(timespec="seconds"),
            source_reason_count,
            content_text,
            error,
            provider,
            model,
        ),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_latest_preference_profile(success_only=False):
    conn = get_conn()
    sql = "SELECT * FROM preference_profiles"
    if success_only:
        sql += " WHERE error IS NULL AND content_text IS NOT NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
    row = conn.execute(sql).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------- 每日任务清单


def add_checklist_item(content):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO checklist_custom_items (content, created_at) VALUES (?, ?)",
        (content, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def list_checklist_items():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM checklist_custom_items ORDER BY created_at ASC, id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_checklist_item(item_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM checklist_custom_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return cur.rowcount
