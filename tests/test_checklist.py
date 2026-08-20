"""每日任务清单冒烟测试（P0-1，2026-08-18）：applied_at 时间戳、超7天待跟进查询、
用户自建待办条目的增删改查接口。不涉及 LLM 调用。

临时库 + 临时 config。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH

import resume_store

# 上传的文件落到临时目录，别往真实的项目 resumes/ 里写（跟 test_resume.py 同样的理由）
resume_store.RESUME_DIR = os.path.join(tmpdir, "resumes")

import app as flask_app

models.init_db()
flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()


def insert_job(title, url):
    conn = models.get_conn()
    job_id = models.insert_job(conn, {
        "title": title, "company": "TestCo", "location": "Beijing", "site": "linkedin",
        "job_url": url, "date_posted": "2026-08-01", "keyword": title, "jd_text": "JD",
    })
    conn.commit()
    conn.close()
    return job_id


# ---- 1. applied_at：只在变成 applied 那一刻记一次
job_id = insert_job("PM", "https://example.com/1")
assert models.get_job(job_id)["applied_at"] is None

r = c.post(f"/api/jobs/{job_id}/application_status", json={"application_status": "applied"})
assert r.status_code == 200, r.get_json()
first_applied_at = models.get_job(job_id)["applied_at"]
assert first_applied_at, "变成 applied 应该记一次时间戳"

# 改成别的状态再改回 applied：applied_at 不该被清空，也不该被覆盖成一个新时间
# （改错又改回来的场景，不需要精确记录"第几次投的"）
c.post(f"/api/jobs/{job_id}/application_status", json={"application_status": "interviewing"})
assert models.get_job(job_id)["applied_at"] == first_applied_at, "改成其它状态不该清空 applied_at"
c.post(f"/api/jobs/{job_id}/application_status", json={"application_status": "applied"})
assert models.get_job(job_id)["applied_at"] == first_applied_at, "已经是 applied 过的，不该覆盖成新时间戳"
print("applied_at bookkeeping ok")

# ---- 2. list_stale_applications：只看真正超过 N 天、且有时间戳的
job_recent = insert_job("PM Recent", "https://example.com/2")
c.post(f"/api/jobs/{job_recent}/application_status", json={"application_status": "applied"})
assert job_recent not in [j["id"] for j in models.list_stale_applications(days=7)], "刚投的不该算超7天"

job_stale = insert_job("PM Stale", "https://example.com/3")
c.post(f"/api/jobs/{job_stale}/application_status", json={"application_status": "applied"})
# 手动把时间戳往回拨 8 天，模拟"投了超过7天"
old_ts = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
conn = models.get_conn()
conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (old_ts, job_stale))
conn.commit()
conn.close()

stale_ids = [j["id"] for j in models.list_stale_applications(days=7)]
assert job_stale in stale_ids and job_recent not in stale_ids, stale_ids

job_no_timestamp = insert_job("PM No TS", "https://example.com/4")
conn = models.get_conn()
conn.execute("UPDATE jobs SET application_status = 'applied' WHERE id = ?", (job_no_timestamp,))
conn.commit()
conn.close()
assert job_no_timestamp not in [j["id"] for j in models.list_stale_applications(days=7)], \
    "没有时间戳的历史数据不该被武断地当成超7天"
print("list_stale_applications ok")

# ---- 3. GET /api/checklist：结构 + 数据都对
data = c.get("/api/checklist").get_json()
assert set(data.keys()) == {"followups", "custom_items", "resume_review_done", "resume_review_ready", "resume_review_id"}, data
followup_ids = [f["job_id"] for f in data["followups"]]
assert job_stale in followup_ids and job_recent not in followup_ids
assert data["custom_items"] == []
assert data["resume_review_done"] is False
assert data["resume_review_ready"] is False, "还没体检过，不该冒出「体检已完成」这条"
assert data["resume_review_id"] is None
print("GET /api/checklist ok")

# ---- 3b. 体检完成提醒：留到真的处理完（生成过优化版）才消失，不是按"今天"这种日期收敛
# （用户反馈：点了提醒跳去简历页，还没做优化这条就自己没了——不该按日期消失，只应该在
# 「已经生成过比这次体检更晚的优化版」时才收起）
review_id = models.insert_resume_review(content_json='{"overall_score": 0.5}')
data = c.get("/api/checklist").get_json()
assert data["resume_review_done"] is True
assert data["resume_review_ready"] is True, "体检给出建议、还没优化过，应该提醒去优化简历"
assert data["resume_review_id"] == review_id, data

# 生成一份"晚于这次体检"的优化版（用 os.utime 显式钉死 mtime，不依赖真实时间流逝，
# 避免同一秒内写文件导致的 mtime 精度问题让断言偶发失败）
review_created = datetime.fromisoformat(models.get_latest_resume_review()["created_at"])
optimized_path = resume_store.optimized_path()
os.makedirs(os.path.dirname(optimized_path), exist_ok=True)
with open(optimized_path, "wb") as f:
    f.write(b"fake docx bytes")
later_ts = (review_created + timedelta(hours=1)).timestamp()
os.utime(optimized_path, (later_ts, later_ts))
assert c.get("/api/checklist").get_json()["resume_review_ready"] is False, \
    "已经生成过更晚的优化版，这条提醒该收起"

# 体检失败（只落了 error）不该被当成"给出了建议"
models.insert_resume_review(error="模拟失败")
assert c.get("/api/checklist").get_json()["resume_review_ready"] is False, "体检失败不该提醒去优化简历"
print("resume_review_ready ok")

# ---- 4. 自定义待办：增删 + 校验
r = c.post("/api/checklist", json={"content": ""})
assert r.status_code == 400
r = c.post("/api/checklist", json={"content": "x" * 300})
assert r.status_code == 400

r = c.post("/api/checklist", json={"content": "整理一下投递优先级"})
assert r.status_code == 200
item_id = r.get_json()["id"]

data = c.get("/api/checklist").get_json()
assert [i["content"] for i in data["custom_items"]] == ["整理一下投递优先级"]

r = c.delete(f"/api/checklist/{item_id}")
assert r.status_code == 200
assert c.get("/api/checklist").get_json()["custom_items"] == []

assert c.delete(f"/api/checklist/{item_id}").status_code == 404, "删一条不存在的记录该报404"
print("custom checklist items ok")

print("\nALL PASS")
