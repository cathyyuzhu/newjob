"""去重键归一化 + 同公司相似职位分组的冒烟测试（2026-08-18，见 models.py 里
normalize/normalize_company/_migrate_dedupe_keys/annotate_similar_groups 的说明）。

背景：2026-08-18 产品 review 发现库里 11 条"高分却被忽略"的职位，实际大多不是打分不准，
而是同一家公司连续发的近似岗位（如 Amazon 一家占 10 条）——真正该修的是去重/展示层，
不是打分逻辑。这里锁住三件事：① 标点/大小写只差一点的两条职位要判成同一条；
② "Amazon.com" 和 "Amazon" 这种公司名后缀差异要归一到同一家公司；③ 历史数据要能
安全重算 dedupe_key（幂等、不因为撞车报错、不丢数据）。

纯本地 sqlite 临时库，不碰真实 jobs.db，不需要 mock 网络/LLM。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH
models.init_db()


# ---- 1. normalize()：标点/空白/大小写差异要折成同一个值
assert models.normalize("Senior Product Manager") == models.normalize("senior   product  manager")
assert models.normalize("Associate Director, Product Development") == models.normalize(
    "Associate Director Product Development"
)
# 但不做词根收敛：级别不同的两个头衔仍然是两个不同的值（这是刻意的边界，见 models.py 注释）
assert models.normalize("Product Manager") != models.normalize("Senior Product Manager")
print("normalize() punctuation/whitespace folding ok")


# ---- 2. normalize_company()：常见法律实体后缀要归一到同一家公司
assert models.normalize_company("Amazon.com") == models.normalize_company("Amazon")
assert models.normalize_company("ABC Inc.") == models.normalize_company("ABC")
assert models.normalize_company("字节跳动有限公司") == models.normalize_company("字节跳动")
# 不同公司不能被误伤合并到一起
assert models.normalize_company("Amazon") != models.normalize_company("Amazonia")
print("normalize_company() suffix stripping ok")


# ---- 3. make_dedupe_key() 综合两者
assert models.make_dedupe_key("HSBC", "Associate Director, Product Development") == models.make_dedupe_key(
    "HSBC", "Associate Director Product Development"
)
print("make_dedupe_key() ok")


# ---- 4. _migrate_dedupe_keys()：历史行按旧规则算的 key，重算后要能正确合并判重逻辑，
#         且幂等、不因为两行撞车而报错、不删除任何一行
conn = models.get_conn()
old_normalize_key = lambda company, title: f"{(company or '').strip().lower()}::{(title or '').strip().lower()}"

rows = [
    # (company, title) —— 后两条模拟"标点不同的同一条职位被拆成两行"的历史脏数据
    ("Amazon.com", "Senior AI Product Manager"),
    ("Amazon", "Principal Product Manager - AI"),
    ("HSBC", "Associate Director, Product Development"),
    ("HSBC", "Associate Director Product Development"),
]
for i, (company, title) in enumerate(rows):
    conn.execute(
        "INSERT INTO jobs (title, company, first_seen, dedupe_key) VALUES (?, ?, ?, ?)",
        (title, company, f"2026-08-1{i}T00:00:00", old_normalize_key(company, title)),
    )
conn.commit()

models._migrate_dedupe_keys(conn)
after1 = {r["id"]: r["dedupe_key"] for r in conn.execute("SELECT id, dedupe_key FROM jobs ORDER BY id")}

# 幂等：再跑一次结果不变
models._migrate_dedupe_keys(conn)
after2 = {r["id"]: r["dedupe_key"] for r in conn.execute("SELECT id, dedupe_key FROM jobs ORDER BY id")}
assert after1 == after2, "迁移不是幂等的"

# 唯一约束没被破坏
keys = [r["dedupe_key"] for r in conn.execute("SELECT dedupe_key FROM jobs")]
assert len(keys) == len(set(keys)), "迁移后出现了重复的 dedupe_key"

# Amazon.com / Amazon 两行的 key 现在应该共享同一个公司前缀
amazon_keys = [after1[1], after1[2]]
assert all(k.startswith("amazon::") for k in amazon_keys), amazon_keys

# HSBC 两行里，first_seen 更早的那行（id=3）应该被更新成新格式；较晚那行保留原样，
# 不因为撞车被静默丢弃或报错
hsbc_row3 = conn.execute("SELECT dedupe_key FROM jobs WHERE id=3").fetchone()["dedupe_key"]
hsbc_row4 = conn.execute("SELECT dedupe_key FROM jobs WHERE id=4").fetchone()["dedupe_key"]
assert hsbc_row3 == models.make_dedupe_key("HSBC", "Associate Director, Product Development")
assert hsbc_row4 != hsbc_row3  # 没有被更新成同一个值，避免撞车
print("_migrate_dedupe_keys() idempotent + collision-safe ok")

# 一条新抓到的"Amazon"（无 .com 后缀）职位，标题跟已入库的 Amazon.com 那条完全相同时
# 应该被判定为重复
new_key = models.make_dedupe_key("Amazon", "Senior AI Product Manager")
assert models.job_exists(conn, new_key), "迁移后，公司名只差 .com 后缀的新职位应该被判重"
print("post-migration dedupe catches company-suffix variants ok")


# ---- 5. annotate_similar_groups()：同公司相似标题分组，跨公司/低相似度不误合并
jobs = [
    {"id": 1, "company": "Amazon.com", "title": "Senior Product Manager - AI, Global Selling", "overall_match": 0.8,
     "application_status": "not_applied", "starred": 0},
    {"id": 2, "company": "Amazon", "title": "Senior AI Product Manager, Global Selling", "overall_match": 0.85,
     "application_status": "applied", "starred": 1},
    {"id": 3, "company": "Amazon.com", "title": "Product Marketing Manager, Device Solutions", "overall_match": 0.4,
     "application_status": "not_applied", "starred": 0},
    {"id": 4, "company": "Blizzard Entertainment", "title": "Senior Manager, WoW Product Management", "overall_match": 0.7,
     "application_status": "not_applied", "starred": 0},
    {"id": 5, "company": "Blizzard Entertainment", "title": "Senior Manager, Hearthstone Product Management", "overall_match": 0.73,
     "application_status": "not_applied", "starred": 0},
    {"id": 6, "company": "Riot Games", "title": "Senior Game Product Manager, Events - Wild Rift", "overall_match": 0.73,
     "application_status": "not_applied", "starred": 0},
]
out = models.annotate_similar_groups([dict(j) for j in jobs])
by_id = {j["id"]: j for j in out}

# 1、2 标题高度相似（同公司归一后也是同一家），应该分到一组
assert by_id[1]["similar_group_id"] is not None
assert by_id[1]["similar_group_id"] == by_id[2]["similar_group_id"]
# 3 跟 1/2 标题差异很大，不该被拉进同一组
assert by_id[3]["similar_group_id"] != by_id[1]["similar_group_id"]
# 4、5 是同公司但产品线完全不同（Hearthstone vs WoW），相似度不够阈值，不该被误合并
# ——2026-08-18 用真实库数据验证过这两条的相似度是 0.33，低于 0.5 的分组阈值。两条各自
# 落单（无法组成"至少2条"的簇）时都应该是 None——不能简单判断"两个 id 不相等"，
# 因为 None != None 是 False，会把"两条都没分组"误判成"分组失败"。
assert by_id[4]["similar_group_id"] is None and by_id[5]["similar_group_id"] is None, (
    by_id[4]["similar_group_id"], by_id[5]["similar_group_id"]
)
# 单独一家公司只有一条职位，不分组
assert by_id[6]["similar_group_id"] is None
# 每条职位都必须有这个 key（哪怕是 None），前端渲染逻辑依赖它总是存在
assert all("similar_group_id" in j for j in out)
print("annotate_similar_groups() clusters near-duplicates, not different roles ok")


# ---- 6. duplicate_of_applied：组里已经有一条投递过/面试中的，其余几条要被打上提示
# ——哪怕那几条自己标了星（标星故意不参与上面的折叠展示，但这个提示不折叠）。
# 背景：2026-08-18 使用者发现一条已标星、84% 匹配的 Amazon 职位其实是已投递的
# 「Principal Product Manager - AI, Amazon Global Selling - PMO」换了个标题重新挂出来的，
# 库里当时已经有 similar_group_id 把两条连在一起，只是没有任何提示露出来。
assert by_id[1]["duplicate_of_applied"] is not None, "同组里 id 2 已投递，id 1 应该被标记疑似重复"
assert by_id[1]["duplicate_of_applied"]["id"] == 2
assert by_id[1]["duplicate_of_applied"]["application_status"] == "applied"
# 已投递的那条本身不用提示"你重复了你自己"
assert by_id[2]["duplicate_of_applied"] is None
# 组外/没分组的职位没有这个字段该是 None，不因为没进 if 分支就直接缺 key
assert by_id[3]["duplicate_of_applied"] is None
assert by_id[6]["duplicate_of_applied"] is None
print("duplicate_of_applied flags near-duplicates of an already-applied job, even when starred ok")


# ---- 7. _merge_cross_source_duplicates() / upgrade_to_linkedin_if_needed()：
# 2026-08-18 使用者要求"LinkedIn 和 Indeed 重复的职位也要去重，只留 LinkedIn"。库里
# 已经有 2 对是标题一字不差、显然同一条被两个源都抓到的严格重复（真实案例：Senior
# Product Manager - AI, NBS AI and OPS / AI Product Manager, MKT AI & Seller Exp）。
def _insert_job_row(title, company, first_seen, dedupe_key, site, application_status="not_applied",
                     starred=0, job_url="", jd_text=""):
    conn.execute(
        "INSERT INTO jobs (title, company, first_seen, dedupe_key, site, application_status, "
        "starred, job_url, jd_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, company, first_seen, dedupe_key, site, application_status, starred, job_url, jd_text),
    )


# A 组：LinkedIn 那行进度更靠前（面试中 + 标星），应该留它，Indeed 那行删掉，
# 它名下的备注要过户给留下来的那行，不能丢
_insert_job_row("AI PM, Merge Test A", "Amazon", "2026-08-18T00:00:00", "test-a-li", "linkedin",
                 "interviewing", 1, "https://linkedin.example/a", "li jd")
_insert_job_row("AI PM, Merge Test A", "Amazon.com", "2026-08-18T00:01:00", "test-a-in", "indeed")
# B 组：两行进度打平（都 not_applied、都标星），应该优先留 LinkedIn 那行
_insert_job_row("AI PM, Merge Test B", "Amazon", "2026-08-18T00:02:00", "test-b-li", "linkedin", starred=1)
_insert_job_row("AI PM, Merge Test B", "Amazon.com", "2026-08-18T00:03:00", "test-b-in", "indeed", starred=1)
# C 组：Indeed 那行进度更靠前（已投递），LinkedIn 那行只是标星——留 Indeed 行的状态
# （已投递不能丢），但链接要换成 LinkedIn 的版本
_insert_job_row("AI PM, Merge Test C", "Amazon", "2026-08-18T00:04:00", "test-c-in", "indeed", "applied")
_insert_job_row("AI PM, Merge Test C", "Amazon.com", "2026-08-18T00:05:00", "test-c-li", "linkedin",
                 starred=1, job_url="https://linkedin.example/c", jd_text="li jd c")
conn.commit()

a_indeed_id = conn.execute("SELECT id FROM jobs WHERE dedupe_key='test-a-in'").fetchone()["id"]
conn.execute(
    "INSERT INTO job_notes (job_id, content, source, created_at) VALUES (?, ?, 'manual', ?)",
    (a_indeed_id, "备注过户测试", "2026-08-18T00:06:00"),
)
conn.commit()

models._merge_cross_source_duplicates(conn)


def _rows_by_title(title):
    return conn.execute("SELECT * FROM jobs WHERE title = ?", (title,)).fetchall()


rows_a = _rows_by_title("AI PM, Merge Test A")
assert len(rows_a) == 1, "A 组两行应该合并成一行"
assert rows_a[0]["site"] == "linkedin" and rows_a[0]["application_status"] == "interviewing"
notes = conn.execute("SELECT * FROM job_notes WHERE job_id = ?", (rows_a[0]["id"],)).fetchall()
assert len(notes) == 1, "被合并掉那行名下的备注应该过户给留下来的那行，不能丢"

rows_b = _rows_by_title("AI PM, Merge Test B")
assert len(rows_b) == 1, "B 组两行应该合并成一行"
assert rows_b[0]["site"] == "linkedin", "进度打平时应该优先留 LinkedIn 那行"

rows_c = _rows_by_title("AI PM, Merge Test C")
assert len(rows_c) == 1, "C 组两行应该合并成一行"
assert rows_c[0]["application_status"] == "applied", "该留进度更靠前的那行（已投递），不能因为它是 Indeed 就被牺牲掉"
assert rows_c[0]["site"] == "linkedin" and rows_c[0]["job_url"] == "https://linkedin.example/c", (
    "留下来的是 Indeed 来源的行时，链接要换成 LinkedIn 的版本——「留哪行状态」和"
    "「链接指向哪个源」是两件事"
)
print("_merge_cross_source_duplicates() keeps the more-advanced row, ties favor linkedin, "
      "link re-points to linkedin ok")

# upgrade_to_linkedin_if_needed()：以后新抓到的场景——库里已有一行是 Indeed 来源，这次
# 抓到同一条的 LinkedIn 版本，判重命中后应该原地把这行升级成 LinkedIn，而不是新插入一行
_insert_job_row("Upgrade Test Role", "Amazon", "2026-08-18T00:07:00", "test-upgrade", "indeed",
                 job_url="https://indeed.example/upgrade", jd_text="old jd")
conn.commit()
models.upgrade_to_linkedin_if_needed(conn, "test-upgrade", {
    "site": "linkedin", "job_url": "https://linkedin.example/upgrade", "jd_text": "new jd",
})
rows_upgrade = _rows_by_title("Upgrade Test Role")
assert len(rows_upgrade) == 1, "应该原地升级，不能新插入一行"
assert rows_upgrade[0]["site"] == "linkedin"
assert rows_upgrade[0]["job_url"] == "https://linkedin.example/upgrade"
assert rows_upgrade[0]["jd_text"] == "new jd"
print("upgrade_to_linkedin_if_needed() upgrades an existing indeed row in place instead of duplicating it ok")


print("\nALL PASS")
