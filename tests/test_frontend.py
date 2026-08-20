"""检查前端资源和模板确实把五个页面接上了（不启动真实浏览器，做静态断言）。

页面切分：
  /                       职位列表                   common.js + app.js
  /jobs/<id>              职位详情（分析+AI对话+备注） common.js + job_detail.js
  /resume                 我的简历（上传 + 体检）    common.js + resume.js
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


def strip_comments(js):
    """去掉 // 行注释。下面几条"某个函数体里不许出现某个标识符"的断言必须先去注释，
    否则一句解释「为什么这里刻意不提交 base_resume_path」的注释就能把断言弄挂——
    那正是我们希望留在代码里的东西。"""
    return "\n".join(re.sub(r"//.*$", "", line) for line in js.splitlines())


def fn_body(js, header):
    """从 `header` 开始到第一个顶格 `}` 为止的函数体（已去注释）。"""
    return strip_comments(js).split(header)[1].split("\n}")[0]


index_html = read("templates", "index.html")
detail_html = read("templates", "job_detail.html")
bank_html = read("templates", "interview.html")
prep_html = read("templates", "job_interview.html")
resume_html = read("templates", "resume.html")
commonjs = read("static", "common.js")
appjs = read("static", "app.js")
detailjs = read("static", "job_detail.js")
bankjs = read("static", "bank.js")
ivjs = read("static", "interview.js")
resumejs = read("static", "resume.js")
css = read("static", "style.css")

# ---- 模板：每个页面引对了脚本，且 common.js 在前（里面是大家都要用的 escapeHtml/toast/主题）
for name, html, page_js in [
    ("index.html", index_html, "/static/app.js"),
    ("job_detail.html", detail_html, "/static/job_detail.js"),
    ("interview.html", bank_html, "/static/bank.js"),
    ("job_interview.html", prep_html, "/static/interview.js"),
    ("resume.html", resume_html, "/static/resume.js"),
]:
    assert "/static/common.js" in html, f"{name} 没引 common.js"
    assert page_js in html, f"{name} 没引 {page_js}"
    assert html.index("/static/common.js") < html.index(page_js), f"{name}：common.js 必须在前"
    assert 'id="toastStack"' in html and 'id="themeIcon"' in html, f"{name} 缺 toast 容器或主题图标"
print("template script wiring ok")

# 面试准备页要拿到职位 id（后端注入，前端不从 URL 里解析）；职位详情页同理
assert "window.PREP_JOB_ID" in prep_html and 'id="prepRoot"' in prep_html
assert "window.DETAIL_JOB_ID" in detail_html
assert 'id="bankRoot"' in bank_html and 'id="bankAssistant"' in bank_html
print("page containers ok")

# ---- 职位详情从弹窗改成独立页面（2026-08-17）：AI对话/备注需要长时间挂着交互，
# 弹窗当年就是因为同一个理由把面试准备搬出去的，见 spec/tech-solution.md
for gone in ["jobDetailModalOverlay", "detailSubpanel-analysis", "openJobDetailModal(",
             "closeJobDetailModal(", "dismissFromDetail(", "jobDetailDismissBtn",
             "trackerIndex", "trackerEntries", "switchDetailTab", "currentDetailJobId"]:
    assert gone not in index_html, f"index.html 里还留着已经拆掉的弹窗痕迹：{gone}"
    assert gone not in appjs, f"app.js 里还留着已经拆掉的弹窗痕迹：{gone}"
assert 'href="/interview"' in index_html, "顶栏的「面试题库」要跳到独立页面"
assert "interviewModalOverlay" not in index_html, "旧的题库弹窗应该已经删掉"
assert "location.href='/jobs/${j.id}'" in appjs, "职位卡片要点进去跳转到 /jobs/<id> 而不是开弹窗"
assert "/jobs/${job.id}/interview" in appjs, "职位卡片上的 🎤 标记要跳到面试准备页"
# 详情页要有：分析内容容器、AI对话面板、备注面板、跳回面试准备的入口
for needle in ['id="detailMain"', 'id="jobChatBody"', 'id="jobNotesBody"', 'id="detailPrepLink"']:
    assert needle in detail_html, f"job_detail.html 缺 {needle}"
for fn in ["renderMain", "sendJobChat", "renderNotes", "generateMaterials", "editDetailTags", "dismissFromDetailPage"]:
    assert f"function {fn}(" in detailjs, f"job_detail.js 缺 {fn}"
assert 'id="detailDismissBtn"' in detail_html, "职位详情页缺少「忽略」按钮"
print("job detail is now a standalone page ok")

# ---- 首屏收敛（2026-08-17）：重点关注提到统计卡、去掉「最近一次运行」卡、默认筛选改「全部」
assert 'id="statCardStarred"' in index_html and 'id="statStarred"' in index_html
assert 'data-filter="starred"' in index_html, "重点关注不是 status 维度，要用 data-filter"
for gone in ['id="statCardLastRun"', 'id="statLastRun"', 'id="starredChip"']:
    assert gone not in index_html, f"index.html 里还留着已经去掉的 {gone}"
assert "statLastRun" not in appjs and "starredChip" not in appjs, "app.js 里还在写已经删掉的元素"
assert "let currentOrigin = '';" in appjs, "默认筛选要是「全部」而不是「外企」"
assert "origin: ''" in appjs, "FILTER_DEFAULTS.origin 要跟 currentOrigin 一致，否则 URL 同步会错"
# 「已拒绝/已婉拒」只从筛选栏拿掉，职位卡片上的投递状态下拉仍然要能标记它们
for gone in ['data-appstatus="rejected"', 'data-appstatus="declined"']:
    assert gone not in index_html, f"筛选栏里还留着 {gone}"
for kept in ["rejected", "declined"]:
    assert kept in appjs, f"投递状态下拉不该丢掉 {kept}（历史数据还在用）"
# 主题切换搬进了设置面板，但按钮本体和 id 没变
assert 'id="themeToggle"' in index_html and "moreSubpanel-settings" in index_html
theme_pos = index_html.index('id="themeToggle"')
assert theme_pos > index_html.index('id="moreSubpanel-settings"'), "主题按钮应该在设置面板里，不在顶栏"
print("home layout revamp ok")

# ---- 添加职位链接（2026-08-18）：按钮 → 弹窗 → 提交，三处 id 要接得上
assert 'id="addLinkBtn"' in index_html and "openAddLinkModal()" in index_html
assert 'id="addLinkModalOverlay"' in index_html, "添加链接弹窗要在主页模板里"
for el in ['id="addLinkUrls"', 'id="addLinkResults"', 'id="addLinkSubmitBtn"']:
    assert el in index_html, f"添加链接弹窗缺少 {el}"
for fn in ["openAddLinkModal", "closeAddLinkModal", "submitJobLinks", "renderLinkResults"]:
    assert f"function {fn}(" in appjs, f"app.js 里缺 {fn}"
assert "/api/jobs/add_by_url" in appjs, "提交要打 /api/jobs/add_by_url"
assert ".link-result" in css, "style.css 缺逐条结果的样式"
print("add job link entry ok")

# ---- 每个页面的函数都能在"它自己加载的那几个文件"里找齐（跨页面串用会在浏览器里报 undefined）
common_defined = set(re.findall(r"function\s+(\w+)", commonjs))
for fn in ["escapeHtml", "showToast", "setBtnLoading", "restoreBtn", "bulletListHtml", "initTheme", "toggleTheme",
           "reqListHtml", "safeUrl", "onChatKeydown", "openTagEditor"]:
    assert fn in common_defined, f"common.js 里缺 {fn}"
# 搬走之后不能在原地留一份，否则同名函数会被后加载的那个覆盖，改一处不生效
for fn in ["escapeHtml", "showToast", "setBtnLoading", "restoreBtn", "bulletListHtml", "toggleTheme", "reqListHtml", "safeUrl"]:
    assert f"function {fn}(" not in appjs, f"app.js 里还留着 {fn} 的重复定义"
assert "function onChatKeydown(" not in bankjs, "bank.js 里还留着 onChatKeydown 的重复定义（应该只在 common.js）"

PAGES = {
    "index": (appjs, index_html),
    "job_detail": (detailjs, detail_html),
    "interview(bank)": (bankjs, bank_html),
    "job_interview(prep)": (ivjs, prep_html),
    "resume": (resumejs, resume_html),
}
for page, (js, html) in PAGES.items():
    defined = common_defined | set(re.findall(r"function\s+(\w+)", js))
    inline = set(re.findall(r'onclick="[^"]*?(\w+)\(', js)) | set(re.findall(r'onclick="[^"]*?(\w+)\(', html))
    inline |= set(re.findall(r'onchange="[^"]*?(\w+)\(', js)) | set(re.findall(r'onchange="[^"]*?(\w+)\(', html))
    # if/event/stopPropagation/preventDefault 不是函数名，是内联表达式里的关键字（比如
    # onclick="if(event.target===this) closeMoreModal()"，或者
    # onclick="event.preventDefault(); xxx()"——checklist 那条为了不让点文字连带勾选框）
    missing = {f for f in inline if f not in defined and f not in {"if", "event", "stopPropagation", "preventDefault"}}
    assert not missing, f"{page}：内联事件引用了本页面加载不到的函数：{missing}"
print("per-page function refs ok")

# ---- 所有外部数据都过了 escapeHtml（抽查渲染函数里不该出现裸插值的字段）
for bad in ["${q.question}", "${g.gap}", "${r.business}", "${prep.error}"]:
    assert bad not in ivjs, f"interview.js 未转义的插值：{bad}"
for bad in ["${item.answer}", "${item.question}", "${msg.content}", "${msg.answer}"]:
    assert bad not in bankjs, f"bank.js 未转义的插值：{bad}"
# 简历页的外部数据有两个来源：LLM 生成的体检文本，和用户自己的文件名
for bad in ["${it.title}", "${it.detail}", "${e.text}", "${e.original}", "${e.reason}",
            "${resumeMeta.filename}", "${r.company}", "${r.title}"]:
    assert bad not in resumejs, f"resume.js 未转义的插值：{bad}"
# 职位详情页：AI回复文本、备注内容都是要转义的外部数据
for bad in ["${msg.content}", "${n.content}", "${e.company_overview}", "${job.cover_letter}"]:
    assert bad not in detailjs, f"job_detail.js 未转义的插值：{bad}"
print("xss escaping ok")

# ---- 我的简历页（2026-08-17）
assert 'id="resumeFileCard"' in resume_html and 'id="resumeReviewCard"' in resume_html
assert 'id="resumeEditsCard"' in resume_html and 'id="tailoredCard"' in resume_html
assert 'accept=".docx"' in resumejs, "上传只收 docx（定制简历/优化版靠段落索引改写，PDF 做不到）"
assert 'href="/resume"' in index_html, "主页顶栏要有「我的简历」入口"
# 没上传简历时后端回 409 + need_resume，三个入口都要走同一个引导，而不是各弹各的错误
assert "function handleNeedResume(" in commonjs
for fn in ["analyzeJob", "startAnalyzeAll"]:
    body = appjs.split(f"function {fn}(")[1][:900]
    assert "handleNeedResume" in body, f"{fn} 没接上「请先上传简历」的引导"
# base_resume_path 现在只由上传流程写。设置页可以读它来展示"当前用的哪份"，但绝不能跟着
# 「保存设置」再提交一遍——那个只读框是空的，提交上去就把刚传的简历覆盖没了。
assert "base_resume_path" not in fn_body(appjs, "async function saveConfig")
assert "getElementById('base_resume_path')" not in appjs, "设置页不该再有可编辑的简历路径输入框"
assert 'id="base_resume_path"' not in index_html
print("resume page ok")

# ---- 按功能位切换模型：各处下拉都接上了，且各管各的功能位
assert 'id="prepModelSelect"' in prep_html and "'interview_prep'" in prep_html
assert 'id="bankModelSelect"' in bank_html and "'interview_bank'" in bank_html
assert 'id="reviewModelSelect"' in resume_html and "'resume_review'" in resume_html
assert 'id="detailChatModelSelect"' in detail_html and "'job_chat'" in detail_html
for task in ["analysis", "materials", "interview_prep", "interview_bank", "resume_review", "job_chat", "preference_profile"]:
    assert f'id="modelTask-{task}"' in index_html, f"设置页缺少 {task} 的模型下拉"
# 下拉是"选了就存"的，不跟着设置页的「保存设置」走——saveConfig 再提交一遍 llm_tasks 的话，
# 会把另外两页刚改的模型覆盖回设置页打开那一刻的值
assert "llm_tasks" not in fn_body(appjs, "async function saveConfig")
print("per-task model selects wired ok")

# ---- P0-1 每日任务清单 + P0-3 忽略原因/偏好档案：模板和JS都接上了
assert 'id="checklistItems"' in index_html and 'id="checklistInput"' in index_html
assert "function renderChecklist" in appjs and "function loadChecklist" in appjs
assert "jobsNeedingMaterials" in appjs
assert 'id="preferenceProfileCard"' in index_html
assert "function openDismissReasonPrompt" in commonjs
assert "openDismissReasonPrompt" in appjs, "忽略之后应该接上原因弹窗"
assert "dismissReasonButtonHtml" in appjs, "已忽略卡片应该有补录原因的入口"
print("daily checklist + dismiss reason wiring ok")

# ---- 题库从职位列表 / 面试准备页都用新标签页打开（这一页要挂着背题，不该被顶掉）
for name, html in [("index.html", index_html), ("job_interview.html", prep_html)]:
    link = html.split('href="/interview"')[1][:120]
    assert 'target="_blank"' in link, f"{name} 的题库入口没开新标签页"

# ---- CSS 类都定义了
for cls in [".prep-pill", ".prep-toolbar", ".prep-item", ".prep-item-body", ".prep-cat-name",
            ".prep-round-input", ".prep-sub",
            ".bank-item", ".bank-item-head", ".bank-answer", ".bank-actions", ".bank-label",
            ".bank-chat", ".bank-chat-msgs", ".bank-chat-draft", ".bank-assistant",
            ".bank-layout", ".bank-nav", ".bank-nav-item", ".bank-add-form", ".bank-caret",
            ".model-select",
            ".upload-drop", ".upload-icon", ".upload-title", ".upload-desc", ".upload-note",
            ".resume-file-actions", ".resume-file-meta", ".resume-file-name", ".resume-file-sub",
            ".review-stale", ".review-error", ".review-summary",
            ".score-row", ".score-overall", ".score-dims", ".score-dim", ".score-bar", ".score-bar-fill",
            ".issue-list", ".issue-item", ".issue-sev", ".issue-title", ".issue-detail",
            ".tag-row", ".coverage-label", ".edit-list", ".edit-item", ".edit-check", ".edit-body",
            ".edit-before", ".edit-after", ".edit-tag", ".edits-selectall",
            ".stat-card.accent-star", ".appearance-row", ".settings-resume-row",
            # 职位详情页（2026-08-17）：左右两栏布局 + 对话/备注侧栏 + 标签编辑弹窗
            ".detail-layout", ".detail-sidebar", ".side-panel", ".side-panel-head",
            ".job-tags-row", ".tag-badge", ".note-list", ".note-item", ".note-meta",
            ".note-source", ".note-time", ".note-remove", ".note-content", ".note-input-row",
            ".tag-editor-modal", ".tag-current-row", ".tag-remove", ".tag-preset-row", ".tag-input-row"]:
    assert cls in css, f"缺少样式：{cls}"
print("css ok")

# ---- 页面都能正常渲染出来（模板没有语法错误）
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
r = c.get("/resume")
assert r.status_code == 200 and "/static/resume.js" in r.data.decode("utf-8")
# 面试准备页/职位详情页都要求职位存在，不存在给 404 而不是渲染一个空壳
# （真实职位的 200 那条在 test_prep.py / test_job_detail.py 里，那边有建好的测试数据）
assert c.get("/jobs/99999/interview").status_code == 404
assert c.get("/jobs/99999").status_code == 404
print("pages render ok")

# ---- /api/models：下拉的数据源，以及 llm_tasks 按 key 合并（不能整体替换）
data = c.get("/api/models").get_json()
assert {m["id"] for m in data["models"]} >= {"claude-sonnet-5", "claude-haiku-4-5", "deepseek-v4-pro"}
assert all(m["provider"] in ("anthropic", "deepseek") for m in data["models"])
assert set(data["llm_tasks"]) == {
    "analysis", "materials", "interview_prep", "interview_bank", "resume_review", "job_chat",
    "preference_profile",
}
assert data["fallback"], "留空的功能位要能告诉前端它实际会用哪个模型"

assert c.post("/api/config", json={"llm_tasks": {"interview_bank": "claude-haiku-4-5"}}).status_code == 200
assert c.post("/api/config", json={"llm_tasks": {"interview_prep": "claude-sonnet-5"}}).status_code == 200
tasks = c.get("/api/models").get_json()["llm_tasks"]
# 第二次提交只带了 interview_prep，第一次存的 interview_bank 不能被清掉
assert tasks == {
    "analysis": "",
    "materials": "",
    "interview_prep": "claude-sonnet-5",
    "interview_bank": "claude-haiku-4-5",
    "resume_review": "",
    "job_chat": "",
    "preference_profile": "",
}, tasks
# 界面上选不到的东西存不进去，免得存进一个打不通的模型名
assert c.post("/api/config", json={"llm_tasks": {"analysis": "gpt-9"}}).status_code == 400
assert c.post("/api/config", json={"llm_tasks": {"bogus_task": "claude-sonnet-5"}}).status_code == 400
# 清空 = 回退到全局默认
assert c.post("/api/config", json={"llm_tasks": {"interview_prep": ""}}).status_code == 200
assert c.get("/api/models").get_json()["llm_tasks"]["interview_prep"] == ""
print("model registry api + per-task config merge ok")

print("\nALL PASS")
