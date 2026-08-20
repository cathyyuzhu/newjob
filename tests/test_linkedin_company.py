"""重点关注公司定向搜索的冒烟测试（2026-08-18，见 linkedin_company.py + scraper.py 的
`target_companies` 那段 + app.py 的 /api/config 里 linkedin_target_companies 处理）。

覆盖：① 公司名解析（访客页命中 / 访客页失败退化到浏览器兜底 / 两级都失败）；
② 保存设置时只重新解析新增或上次失败的名字，已解析成功的直接复用缓存；
③ run_search_once() 对每个已解析的目标公司额外跑一次 linkedin_company_ids 定向搜索，
   沿用现有关键词、结果照常走现有去重。

网络请求和浏览器全程 mock，不访问 LinkedIn、不开真实浏览器、不产生真实 API 费用。
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

import linkedin_company


# ---- 1. resolve_company_id_guest()：从访客页 HTML 里提取数字公司 ID
GUEST_HTML_HIT = '<html>...urn:li:fs_normalized_company:1586...</html>'
GUEST_HTML_MISS = '<html>没有公司ID的普通页面</html>'


class FakeResponse:
    def __init__(self, text, url, status_code=200):
        self.text, self.url, self.status_code = text, url, status_code


class FakeSession:
    def __init__(self, table):
        self.table = table

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        for frag, resp in self.table.items():
            if frag in url:
                return resp
        return FakeResponse("", url, status_code=404)


sess = FakeSession({
    "amazon": FakeResponse(GUEST_HTML_HIT, "https://www.linkedin.com/company/amazon/"),
    "authwall-co": FakeResponse("authwall", "https://www.linkedin.com/authwall?x=1"),
})
assert linkedin_company.resolve_company_id_guest("Amazon", session=sess) == "1586"
assert linkedin_company.resolve_company_id_guest("Authwall Co", session=sess) is None
print("resolve_company_id_guest() ok")


# ---- 2. resolve_company_ids()：访客页命中的直接返回；访客页失败的批量走一次浏览器兜底
linkedin_company.resolve_company_id_guest = (
    lambda name, session=None: "1586" if name == "Amazon" else None
)
browser_calls = []


def fake_browser_resolve(name):
    browser_calls.append(name)
    return "60870" if name == "Riot Games" else None


linkedin_company.resolve_company_id_browser = fake_browser_resolve

out = linkedin_company.resolve_company_ids(["Amazon", "Riot Games", "Nonexistent Corp XYZ"], delay=0)
assert out["Amazon"] == {"company_id": "1586", "status": "resolved"}
assert out["Riot Games"] == {"company_id": "60870", "status": "resolved"}
assert out["Nonexistent Corp XYZ"] == {"company_id": None, "status": "failed"}
# 访客页已经命中的（Amazon）不该再进浏览器兜底那一批
assert browser_calls == ["Riot Games", "Nonexistent Corp XYZ"], browser_calls
print("resolve_company_ids() guest-first + batched browser fallback ok")


# ---- 3. /api/config 保存：只重新解析新增/失败的名字，已解析成功的直接复用缓存
import app as flask_app

flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()

resolve_calls = []


def fake_resolve_company_ids(names, delay=1.0):
    resolve_calls.append(list(names))
    result = {}
    for n in names:
        result[n] = (
            {"company_id": "9999", "status": "resolved"} if n != "Bad Co" else {"company_id": None, "status": "failed"}
        )
    return result


flask_app.resolve_company_ids = fake_resolve_company_ids

r = c.post("/api/config", json={"linkedin_target_companies": ["Amazon", "Bad Co"]})
assert r.status_code == 200, r.get_json()
cfg = r.get_json()
saved = {t["name"]: t for t in cfg["linkedin_target_companies"]}
assert saved["Amazon"]["status"] == "resolved" and saved["Amazon"]["company_id"] == "9999"
assert saved["Bad Co"]["status"] == "failed"
assert resolve_calls == [["Amazon", "Bad Co"]], resolve_calls
print("first save resolves both new names ok")

# 第二次保存：Amazon 已经是 resolved，Bad Co 上次失败——只有 Bad Co 应该被重新解析
r = c.post("/api/config", json={"linkedin_target_companies": ["Amazon", "Bad Co"]})
assert r.status_code == 200, r.get_json()
assert resolve_calls[-1] == ["Bad Co"], resolve_calls
print("second save only re-resolves the previously-failed name ok")

# 第三次保存：去掉 Bad Co，只留 Amazon——不该有任何解析请求
r = c.post("/api/config", json={"linkedin_target_companies": ["Amazon"]})
assert r.status_code == 200, r.get_json()
assert len(resolve_calls) == 2, "已解析成功的名字不该触发重新解析"
saved3 = {t["name"]: t for t in r.get_json()["linkedin_target_companies"]}
assert list(saved3.keys()) == ["Amazon"]
print("third save with no new/failed names triggers no resolve calls ok")


# ---- 4. scraper.run_search_once()：对已解析的目标公司额外跑一次 linkedin_company_ids
#         定向搜索，沿用现有关键词；结果照常走现有去重（重复的不会重复入库）
config.save_config({
    **config.DEFAULT_CONFIG,
    "keywords": ["Product Manager"],
    "locations": ["Beijing"],
    "sites": ["linkedin"],
    "linkedin_request_delay": 0,
    "linkedin_target_companies": [
        {"name": "Amazon", "company_id": "1586", "status": "resolved"},
        {"name": "Unresolved Co", "company_id": None, "status": "failed"},
    ],
    "tracker_xlsx_path": "",
})

import pandas as pd

import scraper

scrape_calls = []


def fake_scrape_jobs(**kwargs):
    scrape_calls.append(kwargs)
    if kwargs.get("linkedin_company_ids"):
        return pd.DataFrame([{
            "title": "Senior Product Manager, AI",
            "company": "Amazon",
            "location": "Beijing",
            "site": "linkedin",
            "job_url": "https://www.linkedin.com/jobs/view/9990001",
            "date_posted": "",
            "description": "招聘产品经理",
        }])
    # 常规 keyword×location 搜索这次不返回任何结果，只验证定向搜索这条路径本身
    return pd.DataFrame([])


import jobspy

# run_search_once() 是函数内部 `from jobspy import scrape_jobs`（每次调用都重新查一遍
# jobspy 模块上的这个名字），所以只需要在模块级别替换掉它，不需要碰 scraper 模块本身。
jobspy.scrape_jobs = fake_scrape_jobs

result = scraper.run_search_once()
targeted_calls = [k for k in scrape_calls if k.get("linkedin_company_ids")]
assert len(targeted_calls) == 1, f"应该只对已解析成功的 Amazon 跑一次定向搜索，实际 {len(targeted_calls)} 次"
assert targeted_calls[0]["linkedin_company_ids"] == [1586]
assert targeted_calls[0]["search_term"] == "Product Manager", "定向搜索应该沿用配置里的关键词"
assert result["added"] == 1
added_job = models.list_jobs()[0]
assert added_job["company"] == "Amazon"
assert added_job["title"] == "Senior Product Manager, AI"
print("run_search_once() targets resolved companies with linkedin_company_ids, skips unresolved ones ok")

# 再跑一次：同一条职位应该被去重挡掉，不重复入库
scrape_calls.clear()
result2 = scraper.run_search_once()
assert result2["added"] == 0
assert result2["skipped_duplicate"] >= 1
print("company-targeted results dedupe against existing rows ok")


print("\nALL PASS")
