"""检查前端资源和模板确实把面试准备的入口接上了（不启动真实浏览器，做静态断言）。"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

html = open(os.path.join(BASE, "templates", "index.html"), encoding="utf-8").read()
appjs = open(os.path.join(BASE, "static", "app.js"), encoding="utf-8").read()
ivjs = open(os.path.join(BASE, "static", "interview.js"), encoding="utf-8").read()
css = open(os.path.join(BASE, "static", "style.css"), encoding="utf-8").read()

# 模板：两个 tab、两个 panel、脚本引入顺序
assert 'data-detailtab="analysis"' in html and 'data-detailtab="interview"' in html
assert 'id="detailSubpanel-analysis"' in html and 'id="detailSubpanel-interview"' in html
assert html.index("/static/interview.js") < html.index("/static/app.js"), "interview.js 必须在 app.js 之前"
assert 'id="jobDetailModalBody"' not in html, "旧的单块 body 容器应该已经被两个 panel 替换"
print("template ok")

# app.js 不再往已删除的 jobDetailModalBody 里写内容
assert "jobDetailModalBody" not in appjs, "app.js 仍在引用已删除的 jobDetailModalBody"
assert "detailSubpanel-analysis" in appjs and "detailSubpanel-interview" in appjs
print("app.js targets new panels ok")

# 跨文件调用的函数都要存在
defined = set(re.findall(r"function\s+(\w+)", appjs + ivjs))
for fn in [
    "interviewPrepBadgeHtml",
    "loadInterviewPrep",
    "stopInterviewPrepPoll",
    "pollInterviewPrepUntilDone",
    "switchDetailTab",
    "regenerateInterviewPrep",
    "deleteInterviewPrep",
    "switchPrepVersion",
    "renderInterviewPrep",
    "refreshJobPrepState",
]:
    assert fn in defined, f"缺少函数定义：{fn}"

# interview.js 用到的 app.js 工具函数都存在
for fn in ["escapeHtml", "showToast", "setBtnLoading", "restoreBtn", "bulletListHtml", "loadJobs", "openJobDetailModal"]:
    assert fn in defined, f"interview.js 依赖的 {fn} 未定义"
print("cross-file function refs ok")

# interview.js 里 onclick 引用的函数（内联字符串里的）也要存在
inline = set(re.findall(r'onclick="[^"]*?(\w+)\(', ivjs))
missing = {f for f in inline if f not in defined and f not in {"event", "stopPropagation"}}
assert not missing, f"内联 onclick 引用了不存在的函数：{missing}"
print("inline onclick refs ok")

# 所有外部数据都过了 escapeHtml（抽查渲染函数里不该出现裸插值的字段）
for bad in ["${q.question}", "${g.gap}", "${r.business}", "${prep.error}"]:
    assert bad not in ivjs, f"未转义的插值：{bad}"
print("xss escaping ok")

# CSS 类都定义了
for cls in [".prep-pill", ".prep-toolbar", ".prep-item", ".prep-item-body", ".prep-cat-name", ".prep-round-input", ".prep-sub"]:
    assert cls in css, f"缺少样式：{cls}"
assert "#jobDetailModalOverlay .modal-tabs" in css
print("css ok")

# 页面能正常渲染出来（模板没有语法错误）
os.environ.setdefault("FLASK_ENV", "testing")
import tempfile
import config

tmp = tempfile.mkdtemp()
config.DB_PATH = os.path.join(tmp, "t.db")
config.CONFIG_PATH = os.path.join(tmp, "c.json")
import models

models.DB_PATH = config.DB_PATH
import app as flask_app

flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()
r = c.get("/")
assert r.status_code == 200
body = r.data.decode("utf-8")
assert "面试准备" in body and "/static/interview.js" in body
print("page renders ok")

print("\nALL PASS")
