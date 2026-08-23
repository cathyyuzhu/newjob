"""扫一个登录态 LinkedIn 列表页（找齐所有出现过的职位链接）的通用逻辑，从
linkedin_tracker.py 抽出来——tracker 同步"已收藏"/"已投递"和 linkedin_how_you_fit.py
同步"How You Fit"匹配推荐搜索都要用到同一套"开浏览器、滚动/点加载更多、收集职位
链接、识别登录墙"的机制，区别只是目标 URL 从哪来（stage 模板 vs 用户直接贴的完整
URL），没道理各写一份——以后 LinkedIn 改一次 DOM/加载方式，只用改这一处。

DOM 提取方式刻意不认卡片的具体 class（那些多是自动生成的哈希类名，改版就失效），只认
页面上所有指向 `/jobs/view/<id>` 或带 `currentJobId=` 的链接，这跟
job_link.parse_linkedin_job_id() 本来要处理的两种链接形态完全一致，直接复用同一个
解析函数。
"""
from playwright.sync_api import sync_playwright

from easy_apply import _launch_context
from job_link import parse_linkedin_job_id

# 列表滚动加载的轮数上限——正常情况下几十条用不了几轮就能看到"已经到底、没有新链接
# 出现"，设这个上限只是防止页面结构识别有问题时无限滚下去出不来。
MAX_SCROLLS = 60
# 连续这么多轮滚动/点"显示更多"后，页面上出现过的职位id集合都没再变大，判定已经到底。
STABLE_ROUNDS_TO_STOP = 3
# "加载更多"按钮可能出现的文案，中英文界面都覆盖；用子串匹配（不要求精确文字），
# 跟 job_link.py/easy_apply.py 里处理 LinkedIn 按钮文案时的取舍一致——可访问性名称
# 经常跟可见文字不完全一样，或者中英文界面用词不同。
LOAD_MORE_TEXTS = ("显示更多结果", "加载更多", "Show more results", "Show more")


def collect_job_ids(page):
    hrefs = page.eval_on_selector_all(
        "a[href*='/jobs/view/'], a[href*='currentJobId=']",
        "els => els.map(el => el.getAttribute('href'))",
    )
    ids = set()
    for href in hrefs:
        job_id = parse_linkedin_job_id(href or "")
        if job_id:
            ids.add(job_id)
    return ids


def click_load_more(page):
    for text in LOAD_MORE_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=2000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def scan_job_list(url, headless, max_scrolls=MAX_SCROLLS, stable_rounds_to_stop=STABLE_ROUNDS_TO_STOP):
    """开一次浏览器扫一遍某个列表页。返回职位id集合；撞上登录墙返回 None（不算异常，
    调用方决定是否要换个模式重试）。EasyApplyInProgress 原样向上抛，由调用方包成
    自己的异常类型、给出贴合自己业务语境的提示文案。"""
    with sync_playwright() as p:
        context = _launch_context(p, headless=headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            if any(marker in page.url for marker in ("/login", "authwall", "checkpoint")):
                return None

            all_ids = set()
            stable_rounds = 0
            for _ in range(max_scrolls):
                current = collect_job_ids(page)
                if current - all_ids:
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                all_ids |= current
                if stable_rounds >= stable_rounds_to_stop:
                    break
                if not click_load_more(page):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(1000)
            all_ids |= collect_job_ids(page)
            return all_ids
        finally:
            context.close()
