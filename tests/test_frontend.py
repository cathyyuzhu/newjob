"""检查前端资源和模板确实把三个页面接上了（不启动真实浏览器，做静态断言）。

页面切分：
  /                       职位列表 + 匹配分析弹窗   common.js + app.js
  /interview              通用面试题库               common.js + bank.js
  /jobs/<id>/interview    某条职位的面试准备         common.js + interview.js
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def read(*parts):
    return open(os.path.join(BASE, *parts), encoding="utf-8").read()


index_html = read("templates", "index.html")
bank_html = read("templates", "interview.html")
prep_html = read("templates", "job_interview.html")
commonjs = read("static", "common.js")
appjs = read("static", "app.js")
bankjs = read("static", "bank.js")
ivjs = read("static", "interview.js")
css = read("static", "style.css")

# ---- 模板：每个页面引对了脚本，且 common.js 在前（里面是大家都要用的 escapeHtml/toast/主题）
for name, html, page_js in [
    ("index.html", index_html, "/static/app.js"),
    ("interview.html", bank_html, "/static/bank.js"),
    ("job_interview.html", prep_html, "/static/interview.js"),
]:
    assert "/static/common.js" in html, f"{name} 没引 common.js"
    assert page_js in html, f"{name} 没引 {page_js}"
    assert html.index("/static/common.js") < html.index(page_js), f"{name}：common.js 必须在前"
    assert 'id="toastStack"' in html and 'id="themeIcon"' in html, f"{name} 缺 toast 容器或主题图标"
print("template script wiring ok")

# 面试准备页要拿到职位 id（后端注入，前端不从 URL 里解析）
assert "window.PREP_JOB_ID" in prep_html and 'id="prepRoot"' in prep_html
assert 'id="bankRoot"' in bank_html and 'id="bankAssistant"' in bank_html
print("page containers ok")

# ---- 详情弹窗只剩「匹配分析」，面试准备改成跳转
assert "data-detailtab" not in index_html, "详情弹窗的 tab 栏应该已经拆掉"
assert "detailSubpanel-interview" not in index_html, "面试准备面板应该已经从弹窗里删掉"
assert 'id="detailSubpanel-analysis"' in index_html
assert 'id="jobDetailPrepLink"' in index_html, "弹窗里要有跳去面试准备页的入口"
assert 'href="/interview"' in index_html, "顶栏的「面试题库」要跳到独立页面"
assert "interviewModalOverlay" not in index_html, "旧的题库弹窗应该已经删掉"
for gone in ["switchDetailTab", "currentDetailJobId", "detailSubpanel-interview"]:
    assert gone not in appjs, f"app.js 里还留着已经拆掉的 {gone}"
assert "/jobs/${job.id}/interview" in appjs, "职位卡片上的 🎤 标记要跳到面试准备页"
print("detail modal is analysis-only ok")

# ---- 每个页面的函数都能在"它自己加载的那几个文件"里找齐（跨页面串用会在浏览器里报 undefined）
common_defined = set(re.findall(r"function\s+(\w+)", commonjs))
for fn in ["escapeHtml", "showToast", "setBtnLoading", "restoreBtn", "bulletListHtml", "initTheme", "toggleTheme"]:
    assert fn in common_defined, f"common.js 里缺 {fn}"
# 搬走之后不能在原地留一份，否则同名函数会被后加载的那个覆盖，改一处不生效
for fn in ["escapeHtml", "showToast", "setBtnLoading", "restoreBtn", "bulletListHtml", "toggleTheme"]:
    assert f"function {fn}(" not in appjs, f"app.js 里还留着 {fn} 的重复定义"

PAGES = {
    "index": (appjs, index_html),
    "interview(bank)": (bankjs, bank_html),
    "job_interview(prep)": (ivjs, prep_html),
}
for page, (js, html) in PAGES.items():
    defined = common_defined | set(re.findall(r"function\s+(\w+)", js))
    inline = set(re.findall(r'onclick="[^"]*?(\w+)\(', js)) | set(re.findall(r'onclick="[^"]*?(\w+)\(', html))
    inline |= set(re.findall(r'onchange="[^"]*?(\w+)\(', js))
    # if/event/stopPropagation 不是函数名，是内联表达式里的关键字（比如
    # onclick="if(event.target===this) closeMoreModal()"）
    missing = {f for f in inline if f not in defined and f not in {"if", "event", "stopPropagation"}}
    assert not missing, f"{page}：内联事件引用了本页面加载不到的函数：{missing}"
print("per-page function refs ok")

# ---- 所有外部数据都过了 escapeHtml（抽查渲染函数里不该出现裸插值的字段）
for bad in ["${q.question}", "${g.gap}", "${r.business}", "${prep.error}"]:
    assert bad not in ivjs, f"interview.js 未转义的插值：{bad}"
for bad in ["${item.answer}", "${item.question}", "${msg.content}", "${msg.answer}"]:
    assert bad not in bankjs, f"bank.js 未转义的插值：{bad}"
print("xss escaping ok")

# ---- CSS 类都定义了
for cls in [".prep-pill", ".prep-toolbar", ".prep-item", ".prep-item-body", ".prep-cat-name",
            ".prep-round-input", ".prep-sub",
            ".bank-item", ".bank-item-head", ".bank-answer", ".bank-actions", ".bank-label",
            ".bank-chat", ".bank-chat-msgs", ".bank-chat-draft", ".bank-assistant"]:
    assert cls in css, f"缺少样式：{cls}"
print("css ok")

# ---- 三个页面都能正常渲染出来（模板没有语法错误）
os.environ.setdefault("FLASK_ENV", "testing")
import tempfile
import config

tmp = tempfile.mkdtemp()
config.DB_PATH = os.path.join(tmp, "t.db")
config.CONFIG_PATH = os.path.join(tmp, "c.json")
import models

models.DB_PATH = config.DB_PATH
models.init_db()
import app as flask_app

flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()

r = c.get("/")
assert r.status_code == 200 and "/static/app.js" in r.data.decode("utf-8")
r = c.get("/interview")
assert r.status_code == 200 and "通用面试题库" in r.data.decode("utf-8")
# 面试准备页要求职位存在，不存在给 404 而不是渲染一个空壳
# （真实职位的 200 那条在 test_prep.py 里，那边有建好的测试数据）
assert c.get("/jobs/99999/interview").status_code == 404
print("pages render ok")

print("\nALL PASS")
