"""手动贴职位链接入库的冒烟测试（见 job_link.py）：链接解析、页面解析、去重、
访客页抓不到时走浏览器兜底、入库后自动排队分析。

临时库 + 临时 config + 临时简历目录，网络请求和 LLM 全程 mock——不访问 LinkedIn、
不开浏览器、不产生真实 API 费用。
"""
import json
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

tmpdir = tempfile.mkdtemp()
import config

config.DB_PATH = os.path.join(tmpdir, "test.db")
config.CONFIG_PATH = os.path.join(tmpdir, "config.json")

import models

models.DB_PATH = config.DB_PATH

import llm

FAKE_ANALYSIS = {
    "company_overview": "一家测试公司",
    "job_content_bullets": ["做产品"],
    "requirement_items": [{"text": "5年经验", "is_gap": False}],
    "skill_matched_bullets": ["产品全流程"],
    "skill_gap_bullets": [],
    "experience_years": "5年+",
    "industry_bullets": ["互联网"],
    "salary": "JD未公开，需进一步询问",
    "team_bullets": ["10人团队"],
    "location": "深圳",
    "company_origin": "domestic",
    "cognitive_match": 0.8,
    "content_match": 0.8,
}

llm.chat = lambda messages, provider="anthropic", model=None, system=None, max_tokens=None: json.dumps(
    FAKE_ANALYSIS, ensure_ascii=False
)

import resume_docx

resume_docx.read_resume_text = lambda path: "[0] Cathy Yang\n[1] 产品经理"

fake_resume = os.path.join(tmpdir, "base.docx")
open(fake_resume, "wb").close()
# 关键词/城市故意跟下面要贴的职位完全不沾边：手动贴进来的职位不该被这套粗筛挡掉
config.save_config({
    **config.DEFAULT_CONFIG,
    "base_resume_path": fake_resume,
    "keywords": ["Senior Product Manager"],
    "locations": ["Beijing"],
    "linkedin_request_delay": 0,
    "tracker_xlsx_path": "",
})

import job_link
import job_state
import app as flask_app

models.init_db()
flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()


# ---- 1. 链接解析：详情页链接、搜索页 currentJobId、带 slug 的、非 LinkedIn 的
cases = [
    ("https://www.linkedin.com/jobs/view/4123456789/", "4123456789"),
    ("https://www.linkedin.com/jobs/view/staff-product-manager-at-acme-4123456789?trk=xxx", "4123456789"),
    ("https://www.linkedin.com/jobs/search/?currentJobId=4123456789&keywords=pm", "4123456789"),
    ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=987654321", "987654321"),
    # 路径上的 id 优先于 currentJobId：用户点开的是路径上那条
    ("https://www.linkedin.com/jobs/view/4111111111/?currentJobId=4222222222", "4111111111"),
    ("www.linkedin.com/jobs/view/4123456789", "4123456789"),
    ("https://www.indeed.com/viewjob?jk=abc123", None),
    ("随便一段不是链接的文字", None),
    ("", None),
]
for url, expected in cases:
    got = job_link.parse_linkedin_job_id(url)
    assert got == expected, f"{url!r} → {got!r}，期望 {expected!r}"
print("parse_linkedin_job_id ok")


# ---- 2. 页面解析：访客页 DOM 和登录后 DOM 用同一个解析器
GUEST_HTML = """
<html><body>
  <h1 class="top-card-layout__title">Staff Product Manager</h1>
  <a class="topcard__org-name-link" href="/company/acme">Acme Robotics</a>
  <span class="topcard__flavor topcard__flavor--bullet">Shenzhen, China</span>
  <time datetime="2026-08-10">2 weeks ago</time>
  <div class="show-more-less-html__markup">
    <p>We are looking for a staff product manager to own our robotics platform.</p>
    <p>Requirements: 8 years of experience shipping hardware-adjacent software.</p>
  </div>
</body></html>
"""
LOGGED_IN_HTML = """
<html><body>
  <h1 class="job-details-jobs-unified-top-card__job-title">Principal PM, Autonomy</h1>
  <div class="job-details-jobs-unified-top-card__company-name"><a href="/company/beta">Beta Motors</a></div>
  <div id="job-details">
    <p>Own the autonomy roadmap end to end and partner with the research org.</p>
    <p>Requirements: 10 years in product, ideally in robotics or automotive.</p>
  </div>
</body></html>
"""
AUTHWALL_HTML = "<html><body><div class='authwall'>Sign in to view this job</div></body></html>"

fields = job_link._parse_job_page(GUEST_HTML)
assert fields["title"] == "Staff Product Manager", fields
assert fields["company"] == "Acme Robotics", fields
assert fields["location"] == "Shenzhen, China", fields
assert fields["date_posted"] == "2026-08-10", fields
assert "robotics platform" in fields["jd_text"], fields

fields2 = job_link._parse_job_page(LOGGED_IN_HTML)
assert fields2["title"] == "Principal PM, Autonomy", fields2
assert fields2["company"] == "Beta Motors", fields2
assert "autonomy roadmap" in fields2["jd_text"], fields2
assert job_link._parse_job_page(AUTHWALL_HTML) is None
print("_parse_job_page handles guest + logged-in DOM ok")


# ---- 3. mock 网络：4123456789 走访客页；4222222222 访客页登录墙、由浏览器兜底；
#         4333333333 两级都抓不到
class FakeResponse:
    def __init__(self, text, url, status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code


class FakeSession:
    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        if "4123456789" in url:
            return FakeResponse(GUEST_HTML, url)
        if "4222222222" in url:
            return FakeResponse(AUTHWALL_HTML, "https://www.linkedin.com/authwall?x=1")
        return FakeResponse("", url, status_code=429)


job_link.requests.Session = lambda: FakeSession()

browser_calls = []


def fake_browser_fetch(job_ids):
    browser_calls.append(list(job_ids))
    out = {}
    for jid in job_ids:
        out[jid] = job_link._parse_job_page(LOGGED_IN_HTML) if jid == "4222222222" else None
    return out


job_link.fetch_via_browser = fake_browser_fetch

r = c.post("/api/jobs/add_by_url", json={"urls": "\n".join([
    "https://www.linkedin.com/jobs/view/4123456789/",
    "https://www.linkedin.com/jobs/search/?currentJobId=4222222222",
    "https://www.linkedin.com/jobs/view/4333333333/",
    "https://www.indeed.com/viewjob?jk=abc123",
])})
assert r.status_code == 200, r.get_json()
data = r.get_json()
results = data["results"]
assert [x["status"] for x in results] == ["added", "added", "failed", "failed"], results
assert results[0]["title"] == "Staff Product Manager" and results[0]["via"] == "guest"
assert results[1]["title"] == "Principal PM, Autonomy" and results[1]["via"] == "browser"
assert "LinkedIn" in results[3]["message"]
# 访客页抓不到的那两条要一起交给浏览器兜底，而不是各开一次浏览器
assert browser_calls == [["4222222222", "4333333333"]], browser_calls
assert len(data["added_ids"]) == 2
print("add_by_url: guest + browser fallback + failures ok")

added = {j["title"]: j for j in models.list_jobs()}
assert len(added) == 2
guest_job = added["Staff Product Manager"]
assert guest_job["status"] == "new", "手动加的职位要落在待审核"
assert guest_job["site"] == "linkedin"
assert guest_job["job_url"] == "https://www.linkedin.com/jobs/view/4123456789"
# keyword 存职位名，好让这条职位以后还能走 scraper.refetch_job_jd（它没 keyword 就放弃）
assert guest_job["keyword"] == "Staff Product Manager"
assert "robotics platform" in guest_job["jd_text"]
print("inserted rows look right ok")


# ---- 4. 自动排队分析：标题/城市都跟配置里的关键词(Senior Product Manager)/城市(Beijing)
#         不沾边，但手动加的职位不该被粗筛挡掉（enforce_relevance=False）
for _ in range(300):
    if not job_state.in_progress_ids() and models.get_job(guest_job["id"])["overall_match"] is not None:
        break
    time.sleep(0.02)
assert models.get_job(guest_job["id"])["overall_match"] is not None, "手动加的职位应该自动分析，不该被相关性粗筛跳过"
print("manually added job gets analyzed despite irrelevant title/location ok")


# ---- 5. 重复：同一条链接再贴一次不该再插一行
r = c.post("/api/jobs/add_by_url", json={"urls": "https://www.linkedin.com/jobs/view/4123456789/"})
assert r.status_code == 200
dup = r.get_json()["results"][0]
assert dup["status"] == "duplicate" and dup["job_id"] == guest_job["id"], dup
assert len(models.list_jobs()) == 2, "重复的链接不该再入库一行"
print("duplicate link skipped ok")

# 同一次提交里贴两遍同一条也只算一条
r = c.post("/api/jobs/add_by_url", json={"urls": "\n".join([
    "https://www.linkedin.com/jobs/view/4123456789/",
    "https://www.linkedin.com/jobs/view/4123456789/?trk=copy",
])})
assert [x["status"] for x in r.get_json()["results"]] == ["duplicate", "duplicate"], r.get_json()
print("same link twice in one batch ok")


# ---- 6. 入参校验：空、超量
r = c.post("/api/jobs/add_by_url", json={"urls": "   "})
assert r.status_code == 400, r.get_json()
r = c.post("/api/jobs/add_by_url", json={
    "urls": "\n".join(f"https://www.linkedin.com/jobs/view/{4100000000 + i}/" for i in range(job_link.MAX_URLS + 1))
})
assert r.status_code == 400 and str(job_link.MAX_URLS) in r.get_json()["error"], r.get_json()
print("input validation ok")

print("\nALL PASS")
