"""models.reconcile_application_status_from_linkedin() 的冒烟测试（2026-08-26）。

背景：用户反馈"同步 LinkedIn 收藏"进来的职位很多其实已经在 LinkedIn 上投递过了，
职达里却一直卡在"待投"——根因是"收藏"同步一直假设"从收藏列表来的职位=还没投"，
但 LinkedIn 的"收藏"和"投递"是两个独立维度，不是互斥的。修法是新增这个核对函数，
拿 LinkedIn"已投递"/"面试"两个 jobs-tracker 列表当权威数据源，把库里落后的投递
状态推进（只升不降）。

第一版只改了 application_status，上线后用户又反馈"待审核"列表里还是能看到这几条
职位、状态不对——根因是职位卡片（`static/app.js` 的 `jobCardHtml()`）只在
`status==='reviewed'` 时才渲染投递状态相关的交互，`status` 还停在 `'new'`（待审核）
的话，改对了的 application_status 根本没地方显示、也没法继续操作。补了"顺带推进
status"这一半：已经确认投递了，逻辑上不可能还没审核过（见 `_promote_reviewed_for_applied_jobs()`
的说明，这一步范围是全库的，不局限于本次核对推进的这几条，见测试 4/5/8/9）。
返回值是被更新职位的明细列表（按"这条职位"去重，不按"改了几个字段"计数——见测试
4/5 的更新），2026-09-08 起从纯计数改成明细，好让同步完成通知能报"具体是哪几条、
从什么状态变成什么状态"，不再只给一个数字；测试里用 len(updated) 取代原来的
updated == N 比较。

纯本地 sqlite 临时库，不碰真实 jobs.db，不需要 mock 网络/LLM/Playwright——传给
reconcile_application_status_from_linkedin() 的两个 id 集合是纯 Python 输入，
跟"怎么扫出来的"完全解耦（见 linkedin_tracker.sync_tracker_stage() 的说明）。

覆盖：
1. "待投" -> "已投递"：job_url 的 id 出现在 applied_ids 里就推进，status 跟着从
   待审核推进到已收藏
2. "待投" -> "面试中"：id 出现在 interview_ids 里，直接跳过"已投递"这一档
3. "已投递" -> "面试中"：已经在投递流程里的职位继续推进，不会原地不动
4. application_status 不降级：Offer 阶段的职位即使 id 出现在 applied_ids 里也不受
   影响（rank 已经最高）；但 status 该推进的还是推进，这条职位仍算被更新
5. 终态保护："已拒绝"/"已婉拒"的 application_status 不会被两个集合"复活"回
   "已投递"，即使 LinkedIn 列表里还挂着这条（终态排除逻辑，不是靠 rank 比较）；
   status 一样该推进
6. 不匹配的 id、非 linkedin 来源的职位不受影响（两个字段都不该动）
7. 两个集合都为空、且库里没有需要补 status 的历史积压时，返回 0，不报错
8. status 推进：已经是"已收藏"的职位不受影响（本来就是目标状态）
9. status 不越权：已经被用户手动"已忽略"（'dismissed'）的职位，即使投递状态被
   推进，也不把 status 悄悄改回"已收藏"——那是用户的显式决定，核对逻辑不该覆盖
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


def make_job(title, application_status, job_id="1111111111", site="linkedin", status=None):
    conn = models.get_conn()
    jid = models.insert_job(conn, {
        "title": title,
        "company": "Reconcile Co",
        "location": "Beijing",
        "site": site,
        "job_url": f"https://www.linkedin.com/jobs/view/{job_id}" if site == "linkedin" else "https://www.indeed.com/viewjob?jk=abc",
        "date_posted": "",
        "keyword": title,
        "jd_text": "some jd text long enough to not be flagged as missing content for this test case.",
    })
    conn.commit()
    if application_status != "not_applied":
        models.set_application_status(jid, application_status)
    if status:
        models.set_job_status(jid, status)
    conn.close()
    return jid


# ---- 1. "待投" -> "已投递"，顺带 status 从"待审核"推进到"已收藏" ----
j1 = make_job("Job Not Applied To Applied", "not_applied", job_id="1000000001")
assert models.get_job(j1)["status"] == "new", "新建职位默认待审核，测试前提"
updated = models.reconcile_application_status_from_linkedin({"1000000001"}, set())
assert len(updated) == 1, updated
detail = updated[0]
assert detail["id"] == j1 and detail["title"] == "Job Not Applied To Applied" and detail["company"] == "Reconcile Co"
assert detail["application_status_before"] == "not_applied" and detail["application_status_after"] == "applied", (
    "明细要带上'从什么状态变成什么状态'，同步完成通知靠这个拼文案"
)
assert models.get_job(j1)["application_status"] == "applied"
assert models.get_job(j1)["applied_at"], "推进成已投递要顺带记 applied_at"
assert models.get_job(j1)["status"] == "reviewed", "已经投递了，status 不能还停在待审核"
print("not_applied -> applied when id is in applied_ids (and status advances new -> reviewed) ok")


# ---- 2. "待投" -> "面试中"，直接跳过"已投递"这一档 ----
j2 = make_job("Job Not Applied To Interviewing", "not_applied", job_id="1000000002")
updated = models.reconcile_application_status_from_linkedin(set(), {"1000000002"})
assert len(updated) == 1, updated
assert models.get_job(j2)["application_status"] == "interviewing"
print("not_applied -> interviewing when id is in interview_ids (skips applied) ok")


# ---- 3. "已投递" -> "面试中" ----
j3 = make_job("Job Applied To Interviewing", "applied", job_id="1000000003")
updated = models.reconcile_application_status_from_linkedin(set(), {"1000000003"})
assert len(updated) == 1, updated
assert models.get_job(j3)["application_status"] == "interviewing"
print("applied -> interviewing ok")


# ---- 4. Offer 的 application_status 不降级（即使 id 出现在 applied_ids 里，
# 已经是最高进度，不该被"已投递"覆盖）；但 status 该推进的还是会推进——offer 明显
# 也意味着"已经审核过"，同样不该停在"待审核"，所以这条职位本身还是算"被更新了 1 条" ----
j4 = make_job("Job Offer Untouched", "offer", job_id="1000000004")
updated = models.reconcile_application_status_from_linkedin({"1000000004"}, set())
assert len(updated) == 1, updated
assert models.get_job(j4)["application_status"] == "offer", "offer 是最高进度，不该被'已投递'覆盖"
assert models.get_job(j4)["status"] == "reviewed", "offer 也意味着已经审核过，status 不该停在待审核"
print("offer application_status stays untouched but status still advances new -> reviewed ok")


# ---- 5. 终态保护："已拒绝"/"已婉拒"的 application_status 不会被复活回"已投递"；
# status 一样该推进——被拒/婉拒同样意味着已经审核过、投过了，两条职位算被更新 ----
j5 = make_job("Job Rejected Protected", "rejected", job_id="1000000005")
j6 = make_job("Job Declined Protected", "declined", job_id="1000000006")
updated = models.reconcile_application_status_from_linkedin({"1000000005", "1000000006"}, set())
assert len(updated) == 2, updated
assert models.get_job(j5)["application_status"] == "rejected", "已拒绝是终态，不该被核对逻辑复活"
assert models.get_job(j6)["application_status"] == "declined", "已婉拒是终态，不该被核对逻辑复活"
assert models.get_job(j5)["status"] == "reviewed", "已拒绝也意味着已经审核过，status 不该停在待审核"
assert models.get_job(j6)["status"] == "reviewed", "已婉拒也意味着已经审核过，status 不该停在待审核"
print("rejected/declined application_status stays protected, but status still advances ok")


# ---- 6. 不匹配的 id 不受影响；非 linkedin 来源的职位不受影响 ----
j7 = make_job("Job Unmatched Id", "not_applied", job_id="1000000007")
j8 = make_job("Job Indeed Source", "not_applied", job_id="1000000008", site="indeed")
updated = models.reconcile_application_status_from_linkedin({"9999999999"}, {"8888888888"})
assert len(updated) == 0, updated
assert models.get_job(j7)["application_status"] == "not_applied"
assert models.get_job(j8)["application_status"] == "not_applied"
print("unmatched ids and non-linkedin jobs are left alone ok")


# ---- 7. 两个集合都为空：直接返回 0，不报错 ----
assert models.reconcile_application_status_from_linkedin(set(), set()) == []
print("empty applied_ids/interview_ids short-circuits to 0 ok")


# ---- 8. status 推进：已经是"已收藏"的职位不受影响（本来就是目标状态，不用改） ----
j9 = make_job("Job Already Reviewed", "not_applied", job_id="1000000009", status="reviewed")
updated = models.reconcile_application_status_from_linkedin({"1000000009"}, set())
assert len(updated) == 1, updated
assert models.get_job(j9)["status"] == "reviewed"
print("status already 'reviewed' is left as-is when application_status advances ok")


# ---- 9. status 不越权："已忽略"的职位即使投递状态被推进，也不把 status 悄悄改回
# "已收藏"——那是用户显式做过的决定，核对逻辑不该覆盖 ----
j10 = make_job("Job Dismissed Protected", "not_applied", job_id="1000000010", status="dismissed")
updated = models.reconcile_application_status_from_linkedin({"1000000010"}, set())
assert len(updated) == 1, updated
dismissed_job = models.get_job(j10)
assert dismissed_job["application_status"] == "applied", "投递状态本身还是要推进的"
assert dismissed_job["status"] == "dismissed", "已忽略是用户的显式决定，不该被核对逻辑撤销"
print("dismissed status is not overridden even though application_status still advances ok")

print("\nALL PASS")
