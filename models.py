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
        # 首页「投递分析」折线图用（2026-09-08）：跟 applied_at 同样的取舍，只在"从别的
        # 状态变成这个状态"那一刻刷新时间戳，改成其它状态不清空——够画"哪一周发生了这件
        # 事"的趋势线就行，不需要完整的状态变更历史。starred/application_status 本身早
        # 就有了，这两列只是补时间戳，不影响原有判断逻辑。
        ("starred_at", "ALTER TABLE jobs ADD COLUMN starred_at TEXT"),
        ("interview_started_at", "ALTER TABLE jobs ADD COLUMN interview_started_at TEXT"),
        # 面试区间的结束时间（2026-09-08）：「面试中」在折线图里是区间状态——从进入
        # 面试到出结果之间每一周都算，不是只算进入那一周（见 set_application_status()
        # 的说明）。这一列只在真正"离开面试中状态"时才写，仍在面试中或从没进过面试的
        # 职位这一列是 NULL。
        ("interview_resolved_at", "ALTER TABLE jobs ADD COLUMN interview_resolved_at TEXT"),
    ):
        if col not in existing_cols:
            conn.execute(ddl)
    # 一次性历史回填（2026-09-08）：上面两列刚上线时，已经存在的星标/面试中记录不会有
    # 时间戳（功能上线前发生的事，没法补出真实时间），「投递分析」图表上会一直显示0，
    # 用户反馈这看起来像是坏的。跟用户确认后回填一次——星标用入库时间兜底，面试中用
    # 投递时间兜底（没有投递时间的边界情况再退到入库时间）。WHERE ... IS NULL 保证
    # 天然幂等：回填过一次之后，同样的 UPDATE 在后续每次启动时都是空操作，不需要额外的
    # "是否已回填过"标记；之后新发生的标星/进入面试依旧走 set_job_starred()/
    # set_application_status() 里的真实时间戳，这里只补历史缺口，不改变长期统计口径。
    conn.execute("UPDATE jobs SET starred_at = first_seen WHERE starred = 1 AND starred_at IS NULL")
    conn.execute(
        "UPDATE jobs SET interview_started_at = COALESCE(applied_at, first_seen) "
        "WHERE application_status = 'interviewing' AND interview_started_at IS NULL"
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
    # 邮件拒信扫描的运行记录（2026-08-23）：只记"什么时候查过一次、查了几条、发现几条
    # 拒信"，不记具体扫了哪些公司/邮件内容——那部分已经作为 job_notes（source='email_scan'）
    # 落在对应职位上了。这张表纯粹是给"每日任务清单"算"该不该提醒你去查邮箱"用的，一条
    # 时间戳就够，不需要外键关联具体职位。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            jobs_checked INTEGER,
            rejections_found INTEGER
        )
        """
    )
    # 无人值守扫描（本机计划任务每天跑一次 headless Claude Code）发现的疑似拒信，先落这张
    # 表等人工确认，不直接改 application_status——邮件措辞模糊时容易误判（见
    # spec/roadmap.md"邮件拒信自动识别"条目），无人值守场景下更没有人在对话里能兜底，所以
    # 比交互式扫描（email_rejection_scan.py apply，人在 Claude Code 对话里当场确认）多一道
    # 网页端确认/忽略的环节。确认后这行删掉，同时正常走 set_application_status+add_job_note。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            note TEXT,
            detected_at TEXT NOT NULL
        )
        """
    )
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
    # 面试语音练习：独立于具体职位的练习模块（2026-08-23）——用户上传的准备文档
    # （比如自己整理的一份复合面试准备材料，不是标准JD）往往不对应库里任何一条职位，
    # 所以不挂 job_id，是三张全新的表而不是往 interview_preps 加字段。
    #
    # interview_docs：上传的原始文档，只存抽取出来的正文——不保留原始 PDF 文件本身
    # （跟简历刻意保留原文件不同：简历要能重新下载原件，这份文档只是喂给 LLM 的素材，
    # 没有"下载回原PDF"的需求，存文件反而多一份要清理的磁盘占用）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            uploaded_at TEXT NOT NULL,
            extracted_text TEXT,
            char_count INTEGER
        )
        """
    )
    # interview_practice_sets：从某份文档生成的一套练习题，可重新生成、保留历史版本
    # （同 interview_preps 的多版本模式），content_json 存整套题（含每题的
    # category/question/why_asked/answer_points/source_hint）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_practice_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            round_label TEXT,
            content_json TEXT,
            error TEXT,
            llm_provider TEXT,
            llm_model TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_sets_doc ON interview_practice_sets(doc_id)")
    # interview_practice_answers：每道题的作答+评分记录。question_id 对应
    # content_json 里题目自己的 id（字符串），不是这张表的自增主键——同一题可以
    # 重新作答多次，保留全部历史（最新一条代表当前状态），跟 job_dismiss_reasons
    # "一条职位可以有多行"是同一个考虑。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_practice_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            transcript TEXT,
            score_json TEXT,
            error TEXT,
            llm_provider TEXT,
            llm_model TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_practice_answers_set ON interview_practice_answers(set_id)")
    # 通知：后台线程跑的同步/AI任务（LinkedIn同步、批量分析、材料生成、体检等，耗时几十秒
    # 到几分钟不等）完成时落一条记录，跟 /api/checklist 聚合的"当前有哪些条件成立"不同——
    # 通知是"发生过一件事"，用户看过之前一直在，不随状态变化消失。不做单条已读追踪，
    # 只有一个全局"未读数"，打开下拉列表即视为看过、整体清零（见 mark_all_notifications_read）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            category TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT,
            link TEXT,
            read_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(read_at)")
    # LLM 调用流水：每次真实打到 API 的调用记一行（成功和失败都记）。埋点打在 llm.py 的
    # _call_anthropic/_call_deepseek 两个收口函数里，所以覆盖全项目所有 LLM 调用。
    #
    # 跟 interview_preps/resume_reviews 那几张表上的 llm_provider/llm_model 两列不是一回事：
    # 那两列只说"这条结果是谁生成的"，这张表回答的是"这个月花了多少钱、哪个任务最容易失败、
    # 哪个模型慢"——原来这些一个都答不上来（Anthropic 的 resp.usage 全项目从来没被读过）。
    #
    # 刻意不存 prompt/响应原文：一次匹配分析的 prompt 是简历全文+JD全文（10-20KB），
    # 每天几十次调用，一年就是几百MB，而这份数据99%的时间没人看。prompt_chars 只记长度，
    # 足够回答"是不是 prompt 变长导致变贵/被截断"这类问题。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            task TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            ok INTEGER NOT NULL,
            error_type TEXT,
            error TEXT,
            duration_ms INTEGER NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL,
            max_tokens INTEGER,
            prompt_chars INTEGER,
            usage_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task, created_at)")

    # 职位收集运行流水（2026-08-29，见 spec/roadmap.md「职位收集链路的错误处理生产级
    # 加固」缺口7）：jobspy 搜索 / tracker 同步 / how-you-fit 同步 / 手动贴链接，每次
    # 运行成败都写一行。跟 llm_calls 同样的取舍：不存页面 HTML/响应原文，只存结构化
    # 统计和 collect_errors.classify() 给出的失败分类，这份数据的用途是看趋势/排错，
    # 不是留档——tracker/how_you_fit 同步之前完全不落库，查不到"最近几次某个来源分别
    # 找到几条"，断崖式下跌（往往意味着 LinkedIn 悄悄改版）只能靠人肉发现。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collect_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            duration_ms INTEGER,
            found INTEGER,
            added INTEGER,
            skipped_duplicate INTEGER,
            skipped_irrelevant INTEGER,
            failed INTEGER,
            ok INTEGER NOT NULL,
            error_kind TEXT,
            error_detail TEXT,
            retries INTEGER,
            agent_used INTEGER,
            suspicious INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collect_runs_source ON collect_runs(source, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collect_runs_started ON collect_runs(started_at)")

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
    "lead", "staff", "principal", "cn",
}
# "cn" 加入于 2026-08-22：AlphaLife Sciences 的「Technical Product Manager (CN)」
# 和「Sr. Product Manager (CN)」被误判成相似职位——"sr"/"product"/"manager" 都是
# 已有的停用词，"Sr..." 那条去掉这些之后就只剩下 "cn"，跟另一条唯一的共同词，
# Jaccard 相似度算出 1/2=0.5 刚好压线。"(CN)" 只是地区标记，不代表岗位方向，
# 不该被当成有区分度的词。
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
    if starred:
        # 只在"从未标星变成标星"（0→1）这一刻刷新 starred_at，给「投递分析」折线图的
        # 「收藏」那条线用；已经是标星状态时再次调用（没有实际变化）不动它。跟
        # set_application_status() 对 applied_at/interview_started_at 的处理是
        # 同一个道理——每次真正"变成"这个状态都刷新时间戳，不是只记第一次。
        row = conn.execute("SELECT starred FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row and not row["starred"]:
            conn.execute(
                "UPDATE jobs SET starred = 1, starred_at = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), job_id),
            )
        else:
            conn.execute("UPDATE jobs SET starred = 1 WHERE id = ?", (job_id,))
    else:
        conn.execute("UPDATE jobs SET starred = 0 WHERE id = ?", (job_id,))
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
    """全项目唯一的 application_status 写入口（手动改状态、
    reconcile_application_status_from_linkedin() 核对推进都走这里），只改这一处
    就能覆盖所有路径。

    「投递分析」折线图的「面试中」是区间状态而不是单周事件（讨论于 2026-09-08）：
    一条职位从进入面试到出结果之间，每一周都该算"面试中"，不是只算进入那一周。
    所以除了 applied_at（进入"已投递"时刷新一次），还要维护面试区间的起止：
    - interview_started_at：从别的状态第一次变成"面试中"时刷新，同时把
      interview_resolved_at 清空——这是一段全新的、还没出结果的面试期，不能让
      上一轮面试遗留的"已出结果"时间戳把这次的区间提前截断。
    - interview_resolved_at：从"面试中"变成别的状态（拒绝/offer/婉拒/甚至手动
      改回待投）时刷新，标记这段面试期到这一刻结束。仍然是"面试中"、或者从来
      没进过面试的职位不碰这个字段。
    这两个字段只做"哪一周该不该算面试中"这个跟踪用途，不影响 application_status
    本身的判断逻辑。
    """
    conn = get_conn()
    row = conn.execute("SELECT application_status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    old_status = row["application_status"] if row else None
    now = datetime.now().isoformat(timespec="seconds")

    updates = {"application_status": application_status}
    if application_status == "applied" and old_status != "applied":
        updates["applied_at"] = now
    if application_status == "interviewing" and old_status != "interviewing":
        updates["interview_started_at"] = now
        updates["interview_resolved_at"] = None
    if old_status == "interviewing" and application_status != "interviewing":
        updates["interview_resolved_at"] = now

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", (*updates.values(), job_id))
    conn.commit()
    conn.close()


# 只有这几个状态允许被"核对"自动推进——"收藏"跟"投递"在 LinkedIn 上不是互斥状态
# （同一条职位可以同时出现在"已收藏"和"已投递"两个列表里），所以"待投"/"已投递"
# 这两个还在正常流程里的状态发现 LinkedIn 上其实进度更靠前时，可以放心推进；
# "已拒绝"/"已婉拒"是流程走完后的终态（可能是用户手动标的，也可能是邮件拒信扫描
# 确认过的，见 email_rejection_scan.py），即使 LinkedIn 的"已投递"列表里暂时还挂着
# 这条（LinkedIn 不会因为被拒就自动把职位从列表里摘掉），也不该被核对逻辑悄悄改回
# "已投递"——这是它俩在 _APPLICATION_PROGRESS_RANK 里跟"待投"同分（都是1分）却不能
# 用同一条规则处理的原因，不能只靠 rank 比较，得显式排除。"Offer"同理不该被降级，
# 但 rank 比较本身已经保护了它（4分，比"已投递""面试中"都高），不用额外排除。
_RECONCILE_ADVANCEABLE_STATUSES = {"not_applied", "applied", "interviewing"}


def reconcile_application_status_from_linkedin(applied_ids, interview_ids):
    """核对库里所有 LinkedIn 职位的投递状态，跟 LinkedIn 官方"已投递"/"面试"两个
    jobs-tracker 列表（这两个列表是判断"这条职位到底投没投"的权威数据源）对齐，把
    落后的状态推进——只升不降，用 _APPLICATION_PROGRESS_RANK 判断。

    起因：用户反馈"同步 LinkedIn 收藏"进来的职位很多其实已经在 LinkedIn 上投递过了，
    职达里却一直卡在"待投"——根因是"收藏"同步一直假设"从收藏列表来的职位=还没投"，
    但 LinkedIn 的"收藏"和"投递"是两个独立维度，不是互斥的，这个假设本来就不成立。
    实测在真实账号上核实过：库里有职位在 LinkedIn 上已经进入"已投递"/"面试"列表，
    职达里却还是"待投"，不是孤例（见 spec/roadmap.md 2026-08-26 的记录）。

    不止应用给"这次同步新入库的职位"——历史上通过任何渠道（收藏同步、关键词自动
    搜索、手动贴链接）入库的 LinkedIn 职位，只要 job_url 能解析出 id 且在传入的两个
    集合里，都会被核对，不局限于本次同步这一批，这样能顺带修好历史积压的错配，不用
    再单独跑一次迁移脚本。

    applied_ids/interview_ids 由调用方传入（linkedin_tracker.fetch_tracker_job_ids()
    扫出来的 LinkedIn 职位 id 集合），这里只管拿去核对入库数据，不关心怎么扫出来的。
    返回被更新的职位明细列表——按"这条职位"去重，不按"改了几个字段"计数：一条职位
    如果 application_status 和 status 在这次调用里都被推进了，只出现一条，不出现两条
    （下面 _promote_reviewed_for_applied_jobs() 顺带修的 status 卡壳，跟这里的
    application_status 推进用同一个 id 去重合并）。调用方要拿"这次核对更新了几条"，
    对返回值 len() 即可。每条明细是 {"id", "title", "company",
    "application_status_before", "application_status_after"}——同步完成后的通知要
    报"具体更新了哪几条、从什么状态变成什么状态"，只给一个数字不够用（2026-09-08
    用户反馈"更新2条"不知道是哪两条、改了什么，见 routes_search.py 里通知文案的
    拼装）。只在核对逻辑本身推进了 application_status 的条目里 before != after；
    只被 _promote_reviewed_for_applied_jobs() 推进 status（application_status 本身
    这次调用没变）的条目，before == after，调用方据此判断该展示"状态推进"还是
    "标记为已审核"。
    """
    from job_link import parse_linkedin_job_id

    touched = {}
    if applied_ids or interview_ids:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, title, company, job_url, application_status FROM jobs WHERE site = 'linkedin'"
        ).fetchall()
        conn.close()

        for row in rows:
            if row["application_status"] not in _RECONCILE_ADVANCEABLE_STATUSES:
                continue
            job_id = parse_linkedin_job_id(row["job_url"] or "")
            if not job_id:
                continue
            if job_id in interview_ids:
                target = "interviewing"
            elif job_id in applied_ids:
                target = "applied"
            else:
                continue
            current_rank = _APPLICATION_PROGRESS_RANK.get(row["application_status"], 1)
            target_rank = _APPLICATION_PROGRESS_RANK.get(target, 1)
            if target_rank <= current_rank:
                continue
            set_application_status(row["id"], target)
            touched[row["id"]] = {
                "id": row["id"], "title": row["title"], "company": row["company"],
                "application_status_before": row["application_status"],
                "application_status_after": target,
            }

    for job_id in _promote_reviewed_for_applied_jobs():
        if job_id in touched:
            continue
        promoted_row = get_job(job_id)
        touched[job_id] = {
            "id": job_id, "title": promoted_row["title"], "company": promoted_row["company"],
            "application_status_before": promoted_row["application_status"],
            "application_status_after": promoted_row["application_status"],
        }

    return list(touched.values())


def _promote_reviewed_for_applied_jobs():
    """把"投递状态不是待投、但审核状态还停在待审核"这种不该出现的组合统一修掉——
    "已经投递"逻辑上必然意味着"已经审核过、决定要投"，不可能还停在"待审核"。

    起因：用户反馈"待审核"列表里有几条职位的投递状态明明是"已投递"，却还停在
    "待审核"。查下来发现根因不止是上面 reconcile_application_status_from_linkedin()
    这个当天新写的函数——`app.py` 的 `_sync_tracker_background()` 从"同步 LinkedIn
    已投递/面试"这个功能（2026-08-22）上线起，同步新入库的职位后就一直只调用
    set_application_status() 打"已投递"/"面试中"，从来没人管过 status 要不要跟着挪；
    新入库的职位 status 默认是 'new'，两个字段从那时起就有可能不一致，只是没人注意到。

    所以这一步扫描范围不限定"这次核对推进了什么"、也不限定 site='linkedin'——是个
    通用不变量修复，跟数据从哪个渠道来的、是不是这次核对推进的无关。故意跳过
    'dismissed'（已忽略）：那是用户显式做过的决定，不该被这里悄悄撤销回"已收藏"。
    返回被推进的职位 id 集合（调用方要跟 application_status 推进那批 id 去重合并，
    所以给集合而不是计数，见 reconcile_application_status_from_linkedin() 的说明）。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status = 'new' AND application_status != 'not_applied'"
    ).fetchall()
    conn.close()
    ids = {row["id"] for row in rows}
    for job_id in ids:
        set_job_status(job_id, "reviewed")
    return ids


def list_applied_jobs():
    """当前"已投递"（application_status='applied'）的全部职位——邮件拒信扫描用，
    告诉调用方该去邮箱里查哪些公司，不查已经有后续结果（面试中/已拒绝/offer等）的职位。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, company, title, applied_at, job_url FROM jobs "
        "WHERE application_status = 'applied' ORDER BY applied_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_email_scan_run(jobs_checked, rejections_found):
    """记一次邮件拒信扫描跑完了——由 email_rejection_scan.py 的 record-run 子命令调用，
    在 Claude Code 完成一整轮"查已投递清单→搜Gmail→确认→落库"之后才记一笔，只是列出
    候选、没走完确认流程的半截扫描不算数（避免"该提醒了"误判成"刚查过"）。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO email_scan_runs (run_at, jobs_checked, rejections_found) VALUES (?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), jobs_checked, rejections_found),
    )
    conn.commit()
    conn.close()


def last_email_scan_run():
    conn = get_conn()
    row = conn.execute("SELECT * FROM email_scan_runs ORDER BY run_at DESC, id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def add_pending_rejection(job_id, note):
    """无人值守扫描发现一条疑似拒信，先排进待确认队列（不直接改状态）。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO pending_rejections (job_id, note, detected_at) VALUES (?, ?, ?)",
        (job_id, note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    pending_id = cur.lastrowid
    conn.close()
    return pending_id


def list_pending_rejections():
    """待确认的疑似拒信，带上职位信息给网页展示用。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT pending_rejections.id, pending_rejections.job_id, pending_rejections.note, "
        "pending_rejections.detected_at, jobs.company, jobs.title "
        "FROM pending_rejections JOIN jobs ON jobs.id = pending_rejections.job_id "
        "ORDER BY pending_rejections.detected_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_pending_rejection(pending_id):
    conn = get_conn()
    conn.execute("DELETE FROM pending_rejections WHERE id = ?", (pending_id,))
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


# ---------------------------------------------------------------- 通知


def add_notification(category, title, message=None, level="info", link=None):
    """后台任务（同步/AI分析等）完成时落一条通知。不做行数上限/自动清理，参照
    search_runs 表的先例——本地单用户库，量级不大，不需要额外的清理逻辑。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO notifications (created_at, category, level, title, message, link) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), category, level, title, message, link),
    )
    conn.commit()
    notification_id = cur.lastrowid
    conn.close()
    return notification_id


def list_notifications(limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unread_notification_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL").fetchone()
    conn.close()
    return row["n"]


def mark_all_notifications_read():
    """打开通知下拉列表时调用——不做单条已读追踪，看过一次就整体清零未读数，
    交互复杂度对齐 /api/checklist 的"今天先别提醒"那一档，不过度设计。"""
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    conn.commit()
    conn.close()


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


# ---------------------------------------------------------------- 面试语音练习


def insert_interview_doc(filename, extracted_text):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO interview_docs (filename, uploaded_at, extracted_text, char_count) VALUES (?, ?, ?, ?)",
        (filename, datetime.now().isoformat(timespec="seconds"), extracted_text, len(extracted_text or "")),
    )
    conn.commit()
    doc_id = cur.lastrowid
    conn.close()
    return doc_id


def list_interview_docs():
    """全部已上传文档，最新的排最前面（不带 extracted_text——列表页只需要文件名/字数，
    正文可能有几万字，没必要每次都拉全文）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, filename, uploaded_at, char_count FROM interview_docs ORDER BY uploaded_at DESC, id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_interview_doc(doc_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM interview_docs WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_interview_doc(doc_id):
    """连带删掉这份文档名下的题目集和作答记录——文档没了，从它生成的练习题也失去了
    存在的意义，不留孤儿数据。跟职位那边"只存 job_id 不建外键"的一贯做法一样，级联
    删除在应用层手动做。"""
    conn = get_conn()
    set_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM interview_practice_sets WHERE doc_id = ?", (doc_id,)
    )]
    for set_id in set_ids:
        conn.execute("DELETE FROM interview_practice_answers WHERE set_id = ?", (set_id,))
    conn.execute("DELETE FROM interview_practice_sets WHERE doc_id = ?", (doc_id,))
    cur = conn.execute("DELETE FROM interview_docs WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return cur.rowcount


def insert_practice_set(doc_id, content_json=None, round_label=None, error=None, provider=None, model=None):
    """写入一套练习题。失败也写一行（同 insert_interview_prep 的理由）——不然用户点了
    「生成题目」之后页面永远是空态，看不出是还在跑还是失败了。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO interview_practice_sets (doc_id, created_at, round_label, content_json, error, llm_provider, llm_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, datetime.now().isoformat(timespec="seconds"), round_label, content_json, error, provider, model),
    )
    conn.commit()
    set_id = cur.lastrowid
    conn.close()
    return set_id


def list_practice_sets(doc_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interview_practice_sets WHERE doc_id = ? ORDER BY created_at DESC, id DESC", (doc_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_practice_set(set_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM interview_practice_sets WHERE id = ?", (set_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_latest_practice_set(doc_id, success_only=False):
    conn = get_conn()
    sql = "SELECT * FROM interview_practice_sets WHERE doc_id = ?"
    if success_only:
        sql += " AND error IS NULL AND content_json IS NOT NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
    row = conn.execute(sql, (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_practice_answer(set_id, question_id, transcript, score_json=None, error=None, provider=None, model=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO interview_practice_answers "
        "(set_id, question_id, created_at, transcript, score_json, error, llm_provider, llm_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            set_id, question_id, datetime.now().isoformat(timespec="seconds"),
            transcript, score_json, error, provider, model,
        ),
    )
    conn.commit()
    answer_id = cur.lastrowid
    conn.close()
    return answer_id


def list_latest_practice_answers(set_id):
    """这套题里每一题**最新**的一条作答记录（不是全部历史）——练习页只需要展示"当前
    进度"，历史重答记录暂时不做单独的回看入口（见 spec/roadmap.md 的范围取舍）。
    用窗口函数按 question_id 分组取最新一条，比 Python 里再筛一遍更直接。"""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY question_id ORDER BY created_at DESC, id DESC
            ) AS rn
            FROM interview_practice_answers WHERE set_id = ?
        ) WHERE rn = 1
        """,
        (set_id,),
    ).fetchall()
    conn.close()
    # rn 只是窗口函数计算过程中的辅助列，不是数据本身，不该跟着 API 响应泄漏出去。
    results = []
    for r in rows:
        d = dict(r)
        d.pop("rn", None)
        results.append(d)
    return results


# ---------------------------------------------------------------- LLM 调用流水


# llm.py 里 _CallRecord 收集到的字段名，跟表结构一一对应。显式列出来而不是直接把
# 调用方传来的 dict 拼进 SQL，避免 llm.py 那边加个字段就悄悄写出一条 SQL 语法错误。
_LLM_CALL_FIELDS = (
    "task", "provider", "model", "ok", "error_type", "error", "duration_ms",
    "input_tokens", "output_tokens", "cost_usd", "max_tokens", "prompt_chars", "usage_json",
)


def insert_llm_call(**fields):
    """写入一次 LLM 调用记录。由 llm.py 通过 set_recorder() 注册后调用——llm.py 不直接
    import 本模块，那样会让"第5层地基"反向依赖数据层（见 spec/architecture.md）。

    error 截断到 1000 字符：DeepSeek 的 HTTP 错误会把整个响应体带进来，个别情况下是
    一大段 HTML，全存下来对排查没有额外价值。
    """
    error = fields.get("error")
    if error and len(error) > 1000:
        error = error[:1000] + "…（已截断）"
    values = [datetime.now().isoformat(timespec="seconds")]
    for name in _LLM_CALL_FIELDS:
        values.append(error if name == "error" else fields.get(name))
    conn = get_conn()
    cur = conn.execute(
        f"INSERT INTO llm_calls (created_at, {', '.join(_LLM_CALL_FIELDS)}) "
        f"VALUES ({', '.join(['?'] * (len(_LLM_CALL_FIELDS) + 1))})",
        values,
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def update_llm_call_error(call_id, error_type=None, error=None):
    """把一条已经记成成功的调用改判为失败。

    用在 JSON 解析失败这种情况：API 本身返回 200（埋点已经记了 ok=1），但返回的文本
    根本不是 JSON。这恰恰是最该统计的失败模式，不回填的话"哪个任务最容易失败"就漏掉了
    最大的一类。
    """
    if not call_id:
        return 0
    if error and len(error) > 1000:
        error = error[:1000] + "…（已截断）"
    conn = get_conn()
    cur = conn.execute(
        "UPDATE llm_calls SET ok = 0, error_type = ?, error = ? WHERE id = ?",
        (error_type, error, call_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def llm_call_stats(since=None, group_by="task"):
    """按任务或模型聚合调用流水。since 是 ISO 日期字符串（含），不传就是全部历史。

    SQLite 没有百分位函数，所以只给 avg/max 耗时；真要看 p95 得把 duration 拉到 Python
    里算，目前的用量（每天几十次）还不值得为这个多写一层。
    """
    if group_by not in ("task", "model", "provider"):
        raise RuntimeError(f"未知的聚合维度：{group_by}（应为 task / model / provider）")
    sql = (
        f"SELECT {group_by} AS key, COUNT(*) AS calls, SUM(1 - ok) AS failures, "
        "SUM(cost_usd) AS cost_usd, AVG(duration_ms) AS avg_ms, MAX(duration_ms) AS max_ms, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens "
        "FROM llm_calls"
    )
    params = ()
    if since:
        sql += " WHERE created_at >= ?"
        params = (since,)
    sql += f" GROUP BY {group_by} ORDER BY calls DESC"
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def llm_call_totals(since=None):
    """总调用数 / 总失败数 / 总花费。跟 llm_call_stats 分开是因为按维度聚合再求和的话，
    task 为 NULL 的那些行（没走 resolve_task 的调用）在 GROUP BY 里会单独成组，
    调用方还得记得把它加回来，容易漏。"""
    sql = (
        "SELECT COUNT(*) AS calls, SUM(1 - ok) AS failures, SUM(cost_usd) AS cost_usd, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens FROM llm_calls"
    )
    params = ()
    if since:
        sql += " WHERE created_at >= ?"
        params = (since,)
    conn = get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------- 职位收集运行流水


# collect_runs 表里 finished_at/ok 之外的其余列，跟 insert_collect_run() 的调用方
# （scraper.py/linkedin_tracker.py/linkedin_how_you_fit.py/routes_search.py）逐个
# 对应。显式列出来而不是直接把调用方传来的 dict 拼进 SQL，避免加个字段时悄悄写出
# 一条 SQL 语法错误（跟 _LLM_CALL_FIELDS 同样的取舍）。
_COLLECT_RUN_FIELDS = (
    "source", "started_at", "duration_ms", "found", "added", "skipped_duplicate",
    "skipped_irrelevant", "failed", "ok", "error_kind", "error_detail", "retries",
    "agent_used", "suspicious",
)


def insert_collect_run(**fields):
    """写入一次职位收集运行记录，成败都写一行——见 spec/roadmap.md「职位收集链路的
    错误处理生产级加固」缺口7。error_kind 取值见 collect_errors.KINDS。

    error_detail 截断到 1000 字符，跟 insert_llm_call() 的 error 截断同样的取舍。
    """
    error_detail = fields.get("error_detail")
    if error_detail and len(error_detail) > 1000:
        error_detail = error_detail[:1000] + "…（已截断）"
    values = [datetime.now().isoformat(timespec="seconds")]
    for name in _COLLECT_RUN_FIELDS:
        values.append(error_detail if name == "error_detail" else fields.get(name))
    conn = get_conn()
    cur = conn.execute(
        f"INSERT INTO collect_runs (finished_at, {', '.join(_COLLECT_RUN_FIELDS)}) "
        f"VALUES ({', '.join(['?'] * (len(_COLLECT_RUN_FIELDS) + 1))})",
        values,
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def collect_run_stats(since=None, group_by="source"):
    """按来源或失败分类聚合收集运行流水。since 是 ISO 日期字符串（含），不传就是
    全部历史。按 error_kind 聚合时只看真的失败过的行（error_kind 非空），不然"没
    出错"会作为一个 NULL 分组挤在结果里，没有意义。"""
    if group_by not in ("source", "error_kind"):
        raise RuntimeError(f"未知的聚合维度：{group_by}（应为 source / error_kind）")
    sql = (
        f"SELECT {group_by} AS key, COUNT(*) AS runs, SUM(1 - ok) AS failures, "
        "SUM(found) AS found, SUM(added) AS added, SUM(failed) AS item_failed, "
        "AVG(duration_ms) AS avg_ms FROM collect_runs"
    )
    conditions = []
    params = []
    if since:
        conditions.append("started_at >= ?")
        params.append(since)
    if group_by == "error_kind":
        conditions.append("error_kind IS NOT NULL")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" GROUP BY {group_by} ORDER BY runs DESC"
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def collect_run_totals(since=None):
    """总运行次数 / 总失败次数 / 总找到 / 总入库。跟 collect_run_stats 分开的理由
    跟 llm_call_totals 一样：按维度聚合再求和的话容易漏掉聚合维度本身为 NULL 的行。"""
    sql = (
        "SELECT COUNT(*) AS runs, SUM(1 - ok) AS failures, SUM(found) AS found, "
        "SUM(added) AS added FROM collect_runs"
    )
    params = ()
    if since:
        sql += " WHERE started_at >= ?"
        params = (since,)
    conn = get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else {}


def recent_found_counts(source, limit=10):
    """某个 source 最近 limit 次成功运行（ok=1）的 found 值，按时间倒序。供
    collect_errors.is_suspicious_drop() 判断"这次数量是不是断崖式下跌"用——只看
    成功过的运行，失败的运行 found 本来就没有意义（要么是 0，要么根本没跑完）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT found FROM collect_runs WHERE source = ? AND ok = 1 "
        "ORDER BY started_at DESC LIMIT ?",
        (source, limit),
    ).fetchall()
    conn.close()
    return [r["found"] for r in rows if r["found"] is not None]


def last_successful_collect_run_at(source):
    """某个 source 最近一次成功（ok=1）运行的 started_at，没有则 None。供
    scheduler.py 判断"距上次成功抓取是否已经超过一个正常调度周期"，决定进程启动时
    要不要立刻补跑一次（比如笔记本合盖休眠错过了昨天的定时触发）。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT started_at FROM collect_runs WHERE source = ? AND ok = 1 "
        "ORDER BY started_at DESC LIMIT 1",
        (source,),
    ).fetchone()
    conn.close()
    return row["started_at"] if row else None
