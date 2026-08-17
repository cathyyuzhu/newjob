"""从一条 LinkedIn 职位链接直接入库（手动补自动搜索漏掉的职位）。

跟 scraper.py 的关系：那边是"按关键词×城市搜一轮，把搜到的都入库"，这边是"我自己在
LinkedIn 上看到一条，把这个链接收进待审核"。两者最终都走 models.insert_job()、都落成
status='new'，区别只在职位内容是从哪来的——所以刻意不塞进 scraper.py：那个模块整个是
围绕 jobspy 的批量搜索接口组织的，而 jobspy 没有"按 job_url 取单条详情"的入口
（见 scraper.refetch_job_jd 的说明），这边只能自己发请求、自己解析 HTML。

抓取分两级，够用就不往下走：
1. 访客页（requests）：`/jobs/view/<id>` 不登录也能看到大部分公开职位，快、不占浏览器。
2. 已登录浏览器（Playwright）：访客页被限流、或职位本身要登录才可见时的兜底，复用
   Easy Apply 那个持久化登录 profile（easy_apply.PROFILE_DIR）。代价是要开浏览器进程、
   跟 Easy Apply 窗口互斥（profile 目录是独占锁），所以只在第一级抓不到时才用。
"""

import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from config import load_config
from models import get_conn, init_db, insert_job, job_exists, make_dedupe_key
from tracker_xlsx import existing_keys_from_tracker

logger = logging.getLogger(__name__)

# 一次最多处理多少条链接。这个接口是同步的（前端要等抓取结果才好逐条报告成功/失败），
# 而每条都要发一次真实网络请求、可能还要走浏览器兜底，条数不设上限的话一次提交能把
# 请求挂在那里好几分钟。
MAX_URLS = 20

# 跟 jobspy 用的是同一套请求头（jobspy/linkedin/constant.py）：LinkedIn 对没有正常
# UA / accept 头的请求会直接返回登录墙。
GUEST_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}"

# LinkedIn 的 job id 是一串纯数字，在链接里有两种出现方式：
#   .../jobs/view/senior-product-manager-at-acme-4123456789?xxx  → 路径末段的数字
#   .../jobs/search/?currentJobId=4123456789&xxx                 → 查询参数
# 从搜索页/推荐页复制到的链接基本都是后一种，从职位详情页复制到的是前一种。
_TRAILING_DIGITS_RE = re.compile(r"(\d{6,})$")


class JobLinkError(Exception):
    """整批抓取都没法继续时抛（比如浏览器兜底根本起不来），单条失败不用这个——
    单条失败要留在结果列表里逐条告诉用户原因。"""


def parse_linkedin_job_id(url):
    """从各种形态的 LinkedIn 链接里取出 job id，认不出来返回 None。"""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if "linkedin.com" not in (parsed.netloc or "").lower():
        return None

    # 路径优先于 currentJobId：`/jobs/view/<id>?currentJobId=<别的id>` 这种链接是真实
    # 存在的（在搜索结果里点开某条职位后又滑动了列表），此时用户要的是路径上那条。
    segments = [s for s in (parsed.path or "").split("/") if s]
    if "view" in segments:
        tail = segments[-1]
        m = _TRAILING_DIGITS_RE.search(tail)
        if m:
            return m.group(1)

    query = parse_qs(parsed.query or "")
    for key in ("currentJobId", "jobId"):
        for value in query.get(key, []):
            m = _TRAILING_DIGITS_RE.search(value.strip())
            if m:
                return m.group(1)
    return None


# ---------------------------------------------------------------- 解析

# 访客页和登录后的职位页是两套完全不同的 DOM，但要取的东西一样，所以用一张"候选选择器"
# 表覆盖两边，谁先命中用谁——这样一个解析函数同时服务 requests 和 Playwright 两条抓取
# 路径，不用维护两份几乎一样的解析代码。LinkedIn 改版时也只需要往列表里补一条。
_TITLE_SELECTORS = (
    "h1.top-card-layout__title",
    "h1.topcard__title",
    "h1.job-details-jobs-unified-top-card__job-title",
    ".job-details-jobs-unified-top-card__job-title",
    "h1",
)
_COMPANY_SELECTORS = (
    "a.topcard__org-name-link",
    ".topcard__flavor a",
    ".top-card-layout__card .topcard__flavor",
    ".job-details-jobs-unified-top-card__company-name a",
    ".job-details-jobs-unified-top-card__company-name",
)
_LOCATION_SELECTORS = (
    ".topcard__flavor--bullet",
    ".job-details-jobs-unified-top-card__primary-description-container span.tvm__text",
)
_JD_SELECTORS = (
    ".show-more-less-html__markup",
    ".description__text",
    "#job-details",
    ".jobs-description__content",
    ".jobs-box__html-content",
)
# 页面跳到登录墙时的特征：URL 里带这些片段，或者页面上有登录表单。抓到登录墙不能当成
# "这条职位没有正文"——两者的处置完全不同（前者该换浏览器兜底，后者该告诉用户抓不到）。
_AUTHWALL_MARKERS = ("/authwall", "/login", "/signup", "/uas/login", "/checkpoint")


def _first_text(soup, selectors):
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _jd_text(soup):
    """JD 正文取纯文本：按块换行、去掉连续空行。库里其它职位的 jd_text 也是给 LLM 读的
    纯文本（jobspy 给的是 markdown），保持一致，不留 HTML 标签进 prompt。"""
    for sel in _JD_SELECTORS:
        el = soup.select_one(sel)
        if not el:
            continue
        text = el.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) >= 60:  # 太短的基本是"登录后查看"之类的占位，当作没抓到
            return text.strip()
    return ""


def _parse_job_page(html):
    """把职位页 HTML 解析成入库需要的字段。认不出职位名/公司就返回 None。"""
    soup = BeautifulSoup(html, "html.parser")
    title = _first_text(soup, _TITLE_SELECTORS)
    company = _first_text(soup, _COMPANY_SELECTORS)
    if not title or not company:
        return None

    date_posted = ""
    time_tag = soup.select_one("time[datetime]")
    if time_tag:
        date_posted = (time_tag.get("datetime") or "").strip()

    return {
        "title": title,
        "company": company,
        "location": _first_text(soup, _LOCATION_SELECTORS),
        "date_posted": date_posted,
        "jd_text": _jd_text(soup),
    }


def _looks_like_authwall(url, html):
    lowered = (url or "").lower()
    if any(marker in lowered for marker in _AUTHWALL_MARKERS):
        return True
    return "authwall" in (html or "")[:4000].lower()


# ---------------------------------------------------------------- 抓取：访客页


def fetch_via_guest(job_id, session=None):
    """不登录抓一条职位。抓到返回字段 dict，遇到登录墙/限流/解析不出来返回 None，
    由调用方决定要不要走浏览器兜底。"""
    sess = session or requests.Session()
    url = JOB_VIEW_URL.format(job_id=job_id)
    try:
        resp = sess.get(url, headers=GUEST_HEADERS, timeout=15, allow_redirects=True)
    except Exception as e:
        logger.info("guest fetch failed for %s: %s", job_id, e)
        return None
    if resp.status_code != 200:
        logger.info("guest fetch got HTTP %s for %s", resp.status_code, job_id)
        return None
    if _looks_like_authwall(resp.url, resp.text):
        logger.info("guest fetch hit authwall for %s", job_id)
        return None
    fields = _parse_job_page(resp.text)
    # 有职位名/公司但正文是空的，也算这一级没抓成：正文缺了这条职位进库也没法做匹配
    # 分析（analyze_and_record 会直接拒绝），不如让浏览器兜底再试一次。
    if not fields or not fields["jd_text"]:
        return None
    return fields


# ---------------------------------------------------------------- 抓取：已登录浏览器


def _expand_description(page):
    """点掉 JD 正文的"see more"折叠。点不到就算了——多数情况下完整正文本来就在 DOM 里，
    折叠只是 CSS 层面的裁剪。"""
    for sel in ("button.show-more-less-html__button", "button.jobs-description__footer-button"):
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(300)
                return
        except Exception:
            continue


def _browser_pass(job_ids, headless):
    """开一次浏览器，把这一批 job_id 挨个抓一遍。返回 {job_id: 字段dict 或 None}。

    整批共用一个浏览器上下文：登录 profile 是独占锁，每条各开一次不仅慢（每次启动几秒），
    还会在批量提交时把锁反复抢来抢去。
    """
    from playwright.sync_api import sync_playwright

    from easy_apply import _launch_context

    out = {}
    with sync_playwright() as p:
        context = _launch_context(p, headless=headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for job_id in job_ids:
                try:
                    page.goto(
                        JOB_VIEW_URL.format(job_id=job_id),
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    page.wait_for_timeout(1200)
                    _expand_description(page)
                    html = page.content()
                    if _looks_like_authwall(page.url, html):
                        out[job_id] = None
                        continue
                    # 这已经是最后一级了，正文没抓到也照样返回：职位名+公司够入库，
                    # 用户至少能在待审核里看到这条，之后点"重新获取JD"再补正文。
                    out[job_id] = _parse_job_page(html)
                except Exception as e:
                    logger.info("browser fetch failed for %s: %s", job_id, e)
                    out[job_id] = None
        finally:
            context.close()
    return out


def fetch_via_browser(job_ids):
    """用 Easy Apply 那个已登录的 profile 抓一批职位。

    先试无头，整批都没抓到才带界面重试一次：LinkedIn 对无头浏览器的识别比带界面严，
    偶尔会对无头会话直接甩登录墙——这种情况下带界面开一次通常就好了。反过来，如果无头
    已经抓到了内容，就没必要弹一个窗口打扰用户，所以不是无条件走带界面那条路。
    """
    job_ids = list(dict.fromkeys(job_ids))
    if not job_ids:
        return {}
    result = _browser_pass(job_ids, headless=True)
    if any(result.get(jid) for jid in job_ids):
        return result
    logger.info("headless browser fetch got nothing, retrying with a visible window")
    return _browser_pass(job_ids, headless=False)


# ---------------------------------------------------------------- 入库


def add_jobs_from_urls(urls):
    """把一批 LinkedIn 职位链接抓取并入库到待审核。

    返回 {"results": [逐条结果], "added_ids": [新入库的职位id]}。逐条结果的 status：
      added / duplicate（库里或追踪表里已经有了）/ failed（链接认不出、抓不到）。
    单条失败不影响其它条目——用户一次贴十条，不该因为其中一条下架了就整批白跑。
    """
    init_db()
    cfg = load_config()
    tracker_keys = existing_keys_from_tracker(cfg.get("tracker_xlsx_path", ""))
    delay = cfg.get("linkedin_request_delay") or 0

    results = []
    pending = []  # [(结果索引, job_id)]，第一级没抓到、要走浏览器兜底的
    fetched = {}  # {结果索引: 字段dict}
    seen_ids = set()

    session = requests.Session()
    for raw_url in urls:
        url = (raw_url or "").strip()
        idx = len(results)
        job_id = parse_linkedin_job_id(url)
        if not job_id:
            results.append({
                "url": url,
                "status": "failed",
                "message": "认不出这是哪条 LinkedIn 职位（目前只支持 LinkedIn 职位链接）",
            })
            continue
        if job_id in seen_ids:
            results.append({"url": url, "status": "duplicate", "message": "这一批里重复贴了同一条职位"})
            continue
        seen_ids.add(job_id)

        results.append({"url": url, "status": "pending", "job_link_id": job_id})
        fields = fetch_via_guest(job_id, session=session)
        if fields:
            fetched[idx] = dict(fields, via="guest")
        else:
            pending.append((idx, job_id))
        if delay:
            time.sleep(delay)

    if pending:
        try:
            browser_result = fetch_via_browser([jid for _, jid in pending])
        except Exception as e:
            # 浏览器兜底整个起不来（没装 Playwright 浏览器、Easy Apply 窗口占着 profile 等）：
            # 不抛给调用方，写进每条待兜底职位的失败原因里——同一批里访客页已经抓到的那些
            # 应该照常入库，不该被这个连带废掉。
            logger.exception("browser fallback unavailable")
            browser_result = {}
            for idx, _jid in pending:
                results[idx].update(status="failed", message=f"访客页抓不到，已登录浏览器兜底也没起来：{e}")
            pending = []
        for idx, job_id in pending:
            fields = browser_result.get(job_id)
            if fields:
                fetched[idx] = dict(fields, via="browser")
            else:
                results[idx].update(
                    status="failed",
                    message="抓不到这条职位（可能已下架、或需要登录后才可见——可确认一下浏览器里的 LinkedIn 还是登录状态）",
                )

    added_ids = []
    conn = get_conn()
    for idx, fields in sorted(fetched.items()):
        item = results[idx]
        job_id = item["job_link_id"]
        dedupe_key = make_dedupe_key(fields["company"], fields["title"])
        item.update(title=fields["title"], company=fields["company"], via=fields["via"])
        if dedupe_key in tracker_keys:
            item.update(status="duplicate", message="追踪表里已经有这家公司的这个职位了")
            continue
        if job_exists(conn, dedupe_key):
            existing = conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            item.update(status="duplicate", message="库里已经有这条职位了", job_id=existing["id"] if existing else None)
            continue
        new_id = insert_job(conn, {
            "title": fields["title"],
            "company": fields["company"],
            "location": fields["location"],
            "site": "linkedin",
            "job_url": JOB_VIEW_URL.format(job_id=job_id),
            "date_posted": fields["date_posted"],
            # keyword 记职位名而不是"手动添加"：这一列不展示给用户，它的实际用途是
            # scraper.refetch_job_jd() 重新定位这条职位时要拿它当搜索词（那个函数没有
            # keyword 就直接放弃）。填职位名，这条手动加的职位以后照样能用"重新获取JD"。
            "keyword": fields["title"],
            "jd_text": fields["jd_text"],
        })
        if new_id is None:  # 理论上上面已经查过，这里兜住并发插入的极端情况
            item.update(status="duplicate", message="库里已经有这条职位了")
            continue
        added_ids.append(new_id)
        item.update(status="added", job_id=new_id, jd_missing=not fields["jd_text"])
    conn.commit()
    conn.close()

    for item in results:
        item.pop("job_link_id", None)
        if item["status"] == "pending":  # 兜底，正常不会出现
            item.update(status="failed", message="抓取没有返回结果")
    return {"results": results, "added_ids": added_ids}
