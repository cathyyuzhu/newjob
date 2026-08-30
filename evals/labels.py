"""从库里已有的人工决策（收藏/忽略、投递/忽略）里取出打分质量 eval 用的标签集。
仅标准库，只读打开 jobs.db，不 import analyzer/llm，不发任何网络请求，免费。

跟 `learn_ml/data.py` 的关系：那个模块已经实现了几乎一样的标注逻辑（`learn_ml/
step1_explore.py` 等已经报过 overall_match AUC≈0.797 的单特征基线），但它 import
pandas——pandas 不在 requirements.txt 里，是那个学习沙盒自己引入的依赖。这里的
`NON_PREFERENCE_REASON_KEYWORDS` 是对 `learn_ml/data.py:19` 那条清洗规则**刻意的
文档化重复**，不是重构成共享模块：
1. 真实 eval harness 不该仅仅为了复用几行清洗逻辑就获得对 pandas/numpy/sklearn
   的硬依赖，在一个干净的 checkout 上会直接 ImportError。
2. `learn_ml/` 是已经冻结的学习产物，`documents/ML学习实践总结.md` 引用了它当时
   跑出来的具体行数/指标，回头重构 `learn_ml/data.py` 会让那份记录跟代码脱节。

这份重复靠 tests/test_eval_labels.py 里的一条源码文本断言守住：那条关键词元组的
字面量如果只改了这一处、没有同步改 learn_ml/data.py（或反过来），测试会红。
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)

# 文档化重复自 learn_ml/data.py:19——这些忽略原因反映的是"记录本身有问题"
# （重复入库、职位已下架），不是"看过之后判断不匹配/不感兴趣"，当负样本会教出
# 一个跟真实偏好无关的模式，所以整条从标签集里剔除，不是当负例。
NON_PREFERENCE_REASON_KEYWORDS = ("重复", "停止招聘")


def _db_path():
    return os.path.join(ROOT, "jobs.db")


def _connect_readonly():
    """只读打开，不触发 WAL 之类的写操作——models.get_conn() 会执行 PRAGMA
    journal_mode=WAL，那本身是对文件头的一次写，这里刻意绕开 models.py，
    用 URI 只读模式连接。"""
    path = _db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_labeled_jobs(db_path=None, scheme="reviewed"):
    """读出有标签的职位，做两轮清洗，返回 (rows, report)。

    scheme:
      "reviewed" —— 正类=status=='reviewed'（收藏/保留），负类=status=='dismissed'
                     （忽略）。这是打分器自己声称要回答的问题："这条职位值不值得看"
                     （见 analyzer.PROMPT_TEMPLATE 和 70% 投递线），也是默认口径。
      "applied"  —— 正类=application_status 不是 'not_applied'（真的投了/在面试/
                     有结果），负类同样是 dismissed。这个切子集更小、更严格，但
                     `models._promote_reviewed_for_applied_jobs()` 会把每条投递
                     状态非 not_applied 的职位都推成 status='reviewed'，两个口径
                     是嵌套关系（applied ⊂ reviewed），不是相互独立的两组数据，
                     不要在同一份报告里把两者的 AUC 当成互相印证。

    两轮清洗：
    1. 丢弃 overall_match 缺失的行——这些职位从没花钱做过匹配分析，没有分数可比。
       report 里记 n_dropped_no_score，这个数字对负类（dismissed）的影响远大于
       正类：真实库里 338 条 dismissed 只有 184 条有分数（154 条被丢弃），而
       reviewed 是 47 条里 46 条有分数——负类被砍掉的比例高得多，是"被分析过的
       职位更可能是认真考虑过的"这层选择偏差，report 里必须带出这个数字，不能
       假装丢弃是无痛的。
    2. 丢弃忽略原因是"记录问题"而非"偏好判断"的行（重复入库/职位已下架）——
       只能覆盖『填过忽略原因』的那一小部分记录，job_dismiss_reasons 目前只有
       32 行对应 338 条 dismissed，report 里记 n_reasons_total 说明这层清洗的
       覆盖率有多低。
    """
    if scheme not in ("reviewed", "applied"):
        raise ValueError(f"scheme 必须是 'reviewed' 或 'applied'，实际：{scheme}")

    conn = sqlite3.connect(f"file:{db_path or _db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
        reasons = [dict(r) for r in conn.execute(
            "SELECT job_id, tags, note FROM job_dismiss_reasons"
        ).fetchall()]
    finally:
        conn.close()

    n_dismissed_total = sum(1 for j in jobs if j["status"] == "dismissed")

    if scheme == "reviewed":
        pool = [j for j in jobs if j["status"] in ("reviewed", "dismissed")]
        label_of = lambda j: 1 if j["status"] == "reviewed" else 0  # noqa: E731
    else:
        pool = [
            j for j in jobs
            if j["status"] == "dismissed" or (j.get("application_status") or "not_applied") != "not_applied"
        ]
        label_of = lambda j: 1 if (j.get("application_status") or "not_applied") != "not_applied" else 0  # noqa: E731

    n_before_score_drop = len(pool)
    pool = [j for j in pool if j.get("overall_match") is not None]
    n_dropped_no_score = n_before_score_drop - len(pool)

    # 命中 NON_PREFERENCE_REASON_KEYWORDS 的 job_id 集合。
    hygiene_ids = set()
    for r in reasons:
        text = f"{r.get('tags') or ''} {r.get('note') or ''}"
        if any(k in text for k in NON_PREFERENCE_REASON_KEYWORDS):
            hygiene_ids.add(r["job_id"])

    n_before_hygiene_drop = len(pool)
    pool = [j for j in pool if j["id"] not in hygiene_ids]
    n_dropped_hygiene = n_before_hygiene_drop - len(pool)

    rows = [
        {
            "id": j["id"], "company": j["company"], "title": j["title"],
            "status": j["status"], "application_status": j.get("application_status"),
            "overall_match": j["overall_match"], "label": label_of(j),
        }
        for j in pool
    ]

    report = {
        "scheme": scheme,
        "n_dismissed_total": n_dismissed_total,
        "n_dropped_no_score": n_dropped_no_score,
        "n_dropped_hygiene": n_dropped_hygiene,
        "n_reasons_total": len(reasons),
        "n_positive": sum(1 for r in rows if r["label"] == 1),
        "n_negative": sum(1 for r in rows if r["label"] == 0),
    }
    return rows, report


# 打分器自己声称负责的两个维度（analyzer.PROMPT_TEMPLATE 明确说"不要把经验年限、
# 薪资、团队规模、地理位置这些因素混入这两个分数"），routes_jobs.DISMISS_REASON_TAGS
# 六个标签里只有这两个落在打分器的remit内，其余四个（薪资不符/公司不感兴趣/地点/
# 行业）是打分器明确不该管的因素，混进来算 in-remit 精度会冤枉模型。
IN_REMIT_DISMISS_TAGS = ("职能不对", "层级不匹配")


def dismiss_tags_for(job_ids):
    """job_id 集合 -> {job_id: [tags]}，只取最新一次忽略原因记录。给"in-remit
    切片"（职能不对/层级不匹配）用，样本量很小（真实库里目前只有 18 条），只做
    诊断展示，不计算置信区间。"""
    if not job_ids:
        return {}
    conn = _connect_readonly()
    try:
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT job_id, tags, created_at FROM job_dismiss_reasons "
            f"WHERE job_id IN ({placeholders}) ORDER BY created_at DESC",
            list(job_ids),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        if r["job_id"] in out:
            continue  # 已经取过这个 job 最新的一条了
        tags = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
        out[r["job_id"]] = tags
    return out


def jd_text_for(job_ids):
    """job_id 集合 -> {job_id: jd_text}。给 --replay 采样重跑用——labels.load_
    labeled_jobs() 返回的行不带 jd_text（那份数据主要用于统计聚合，没必要把
    可能几十KB一条的 JD 正文也塞进每一行），需要的时候单独按 id 查这几条。"""
    if not job_ids:
        return {}
    conn = _connect_readonly()
    try:
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT id, jd_text FROM jobs WHERE id IN ({placeholders})", list(job_ids)
        ).fetchall()
    finally:
        conn.close()
    return {r["id"]: r["jd_text"] for r in rows}
