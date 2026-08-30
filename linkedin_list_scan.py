"""扫一个登录态 LinkedIn 列表页（找齐所有出现过的职位链接）的通用逻辑，从
linkedin_tracker.py 抽出来——tracker 同步"已收藏"/"已投递"和 linkedin_how_you_fit.py
同步"How You Fit"匹配推荐搜索都要用到同一套"开浏览器、滚动/点加载更多、收集职位
链接、识别登录墙"的机制，区别只是目标 URL 从哪来（stage 模板 vs 用户直接贴的完整
URL），没道理各写一份——以后 LinkedIn 改一次 DOM/加载方式，只用改这一处。

DOM 提取方式刻意不认卡片的具体 class（那些多是自动生成的哈希类名，改版就失效），只认
页面上所有指向 `/jobs/view/<id>` 或带 `currentJobId=` 的链接，这跟
job_link.parse_linkedin_job_id() 本来要处理的两种链接形态完全一致，直接复用同一个
解析函数。

componentkey 兜底（2026-08-30）：LinkedIn 把"根据您的偏好推荐职位"（How You Fit）
这个页面的卡片从 `<a href="/jobs/view/ID">` 改成了 `<div role="button"
componentkey="job-card-component-ref-ID">`——纯客户端路由，没有真实 href，上面那条
"只认链接"的假设在这个页面上完全失效，导致确定性扫描和 agent 兜底（复用同一个函数）
双双一直收集到 0 个职位，agent 因为"看不到任何新增"在原地滚了 12 轮也没用（排查详见
2026-08-30 的 debug_snapshots）。加一条兜底：认 `componentkey` 里带
`job-card-component-ref-<数字id>` 这个模式的元素。这依然不是"认哈希类名"那种脆弱
写法——`componentkey` 是有语义的内部埋点属性，但同样不保证长期稳定，以后这个页面再
改版大概率还会破，到时候还是同一个思路：抓一份 debug_snapshots 的 page.html，搜有
职位标题文字附近的 DOM 结构，找新的稳定标记物。
"""
import logging
import re

from playwright.sync_api import sync_playwright

import collect_errors
from easy_apply import _launch_context
from job_link import parse_linkedin_job_id

logger = logging.getLogger(__name__)

_COMPONENT_KEY_JOB_ID_RE = re.compile(r"job-card-component-ref-(\d+)")

# 列表滚动加载的轮数上限——正常情况下几十条用不了几轮就能看到"已经到底、没有新链接
# 出现"，设这个上限只是防止页面结构识别有问题时无限滚下去出不来。
MAX_SCROLLS = 60
# 连续这么多轮滚动/点"显示更多"后，页面上出现过的职位id集合都没再变大，判定已经到底。
STABLE_ROUNDS_TO_STOP = 3
# "加载更多"按钮可能出现的文案，中英文界面都覆盖；用子串匹配（不要求精确文字），
# 跟 job_link.py/easy_apply.py 里处理 LinkedIn 按钮文案时的取舍一致——可访问性名称
# 经常跟可见文字不完全一样，或者中英文界面用词不同。
#
# "下一步"是 jobs-tracker 列表页（已投递/面试等）用的翻页按钮文案——这个页面实测是
# 数字翻页（1/2/3/下一步），不是无限滚动/加载更多，之前漏了这个文案导致只能扫到第
# 一页（发现于用户反馈"LinkedIn 已申请 42 条，同步只入库了 10 条"）；点到最后一页
# 后按钮会被禁用，click() 抛异常会被下面 try/except 吞掉，回退到 stable_rounds 计数
# 自然停止，不会死循环。
LOAD_MORE_TEXTS = ("显示更多结果", "加载更多", "Show more results", "Show more", "下一步", "下一页", "Next")


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

    # componentkey 兜底，见模块顶部说明——How You Fit 页面的卡片没有真实 href。
    component_keys = page.eval_on_selector_all(
        "[componentkey*='job-card-component-ref-']",
        "els => els.map(el => el.getAttribute('componentkey'))",
    )
    for key in component_keys:
        m = _COMPONENT_KEY_JOB_ID_RE.search(key or "")
        if m:
            ids.add(m.group(1))

    # 诊断日志（2026-08-30，见 spec/roadmap.md 排查记录）：href 提取一直工作正常，
    # componentkey 提取在离线重放 debug_snapshots 里验证有效，但曾观察到真实活页面
    # 运行时两条路径都收集到 0 个、而失败瞬间落的 page.html 里其实有卡片——怀疑是
    # 真实渲染时序/懒加载导致提取时机不对，而不是选择器本身的问题。这两行分开记
    # href 和 componentkey 各自的原始匹配数，不合并成一个总数，方便后续复现时一眼
    # 看出到底是完全没匹配到任何东西，还是两条路径其中一条工作、一条没工作。用
    # info 级别，不用改日志级别配置就能在下次真实失败时看到（这个函数本身调用
    # 频率不高——一次同步几轮到几十轮，不会刷屏）。
    logger.info(
        "collect_job_ids: href 匹配 %d 个、componentkey 匹配 %d 个，合并去重后 %d 个",
        len(hrefs), len(component_keys), len(ids),
    )
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
    自己的异常类型、给出贴合自己业务语境的提示文案。

    墙钟超时（2026-08-29，见 collect_errors.start_run_deadline() 说明）：滚动循环
    每轮检查一次调用方有没有设过整体运行的截止时间，超过就抛 TimeoutError（会在
    finally 里正常关掉浏览器 context，不会泄漏）。没设过 deadline 时
    check_deadline() 是空操作，不影响单独调试这个函数。
    """
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
                collect_errors.check_deadline("扫描列表页超过本次运行的时间上限")
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
