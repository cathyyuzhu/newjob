"""把公司名解析成 LinkedIn 数字公司 ID，供"重点关注公司"定向搜索使用。

背景：jobspy 的 LinkedIn 抓取原生支持 `linkedin_company_ids` 参数（对应 LinkedIn 搜索页
的 f_C 过滤器），但它要数字 ID，不是公司名——普通关键词搜索排名/结果条数上限可能漏掉某些
大公司的新职位，用这个参数能强制"这几家公司下所有匹配关键词的职位都要看到"，不受排名影响。

跟 job_link.py 的关系：解析思路是同一套（访客页优先，抓不到再退化到已登录浏览器），但
job_link.py 是"给一条职位链接、抓这条职位的详情"，这边是"给一个公司名、找它的数字 ID"，
目标页面结构完全不同，所以没有直接复用 job_link.py 的解析函数，只复用它的两级抓取骨架
（guest headers、authwall 识别、浏览器兜底走 easy_apply 的持久化登录 profile）。
"""
import logging
import re
import time
from urllib.parse import quote

import requests

from job_link import GUEST_HEADERS, _looks_like_authwall

logger = logging.getLogger(__name__)

COMPANY_PAGE_URL = "https://www.linkedin.com/company/{slug}/"
COMPANY_SEARCH_URL = "https://www.linkedin.com/search/results/companies/?keywords={q}"

# LinkedIn 页面里公司的数字 ID 以这几种 urn 形式出现，不同页面模板用的字段不一样，
# 按命中率从高到低排，第一个匹配上就用。
_ORG_ID_PATTERNS = (
    re.compile(r"urn:li:fs_normalized_company:(\d+)"),
    re.compile(r"urn:li:organization:(\d+)"),
    re.compile(r'"companyId"\s*:\s*"?(\d+)'),
)

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name):
    """公司名转 LinkedIn 主页可能用的 slug，只是"猜测"——猜不中会在访客页拿到 404/
    跳转到别的页面，不影响正确性，因为下一步会校验解析出的公司名跟输入是否对得上。"""
    s = _SLUG_STRIP_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s


def _extract_org_id(html):
    for pat in _ORG_ID_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


def _fetch_company_page(url, session):
    try:
        resp = session.get(url, headers=GUEST_HEADERS, timeout=15, allow_redirects=True)
    except Exception as e:
        logger.info("company page fetch failed for %s: %s", url, e)
        return None
    if resp.status_code != 200:
        logger.info("company page fetch got HTTP %s for %s", resp.status_code, url)
        return None
    if _looks_like_authwall(resp.url, resp.text):
        logger.info("company page fetch hit authwall for %s", url)
        return None
    return resp.text


def resolve_company_id_guest(name, session=None):
    """不登录解析一个公司名。解析到返回数字 ID 字符串，解析不出来返回 None。"""
    sess = session or requests.Session()
    slug = _slugify(name)
    if slug:
        html = _fetch_company_page(COMPANY_PAGE_URL.format(slug=slug), sess)
        if html:
            org_id = _extract_org_id(html)
            if org_id:
                return org_id
    return None


def resolve_company_id_browser(name):
    """访客页猜不中 slug、或页面结构变化拿不到 ID 时的兜底：用已登录的浏览器打开
    LinkedIn 的公司搜索页，点第一条结果，再从跳转后的公司主页里提取 ID。
    复用 Easy Apply 的持久化登录 profile（跟 job_link.py 的浏览器兜底同一个 profile，
    两者互斥——profile 目录是独占锁，不能同时开两个浏览器进程）。
    """
    from playwright.sync_api import sync_playwright

    from easy_apply import _launch_context

    with sync_playwright() as p:
        context = _launch_context(p, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                COMPANY_SEARCH_URL.format(q=quote(name)),
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(1500)
            link = page.query_selector("a[href*='/company/']")
            if not link:
                return None
            href = link.get_attribute("href") or ""
            if not href:
                return None
            page.goto(href, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1000)
            html = page.content()
            if _looks_like_authwall(page.url, html):
                return None
            return _extract_org_id(html)
        except Exception as e:
            logger.info("browser company resolve failed for %s: %s", name, e)
            return None
        finally:
            context.close()


def resolve_company_ids(names, delay=1.0):
    """批量解析一批公司名，返回 {name: {"company_id": str 或 None, "status": "resolved"|"failed"}}。

    访客页逐个先跑一轮（便宜、不占浏览器）；全部失败的名字再统一走一次浏览器兜底
    （复用同一个浏览器进程，不是每个失败的名字各开一次——跟 job_link.py 的批量抓取
    同一个理由：profile 独占锁，开销也更低）。
    """
    out = {}
    session = requests.Session()
    pending = []
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        company_id = resolve_company_id_guest(name, session=session)
        if company_id:
            out[name] = {"company_id": company_id, "status": "resolved"}
        else:
            pending.append(name)
        if delay:
            time.sleep(delay)

    for name in pending:
        try:
            company_id = resolve_company_id_browser(name)
        except Exception as e:
            logger.info("browser fallback unavailable while resolving %s: %s", name, e)
            company_id = None
        out[name] = (
            {"company_id": company_id, "status": "resolved"}
            if company_id
            else {"company_id": None, "status": "failed"}
        )

    return out
