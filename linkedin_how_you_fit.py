"""同步 LinkedIn "How You Fit" 求职资格匹配搜索结果页——登录态下 LinkedIn 基于用户
档案算出的"你可能符合条件"的搜索结果（带 keywords=/geoId= 等参数的
`/jobs/search-results/?showHowYouFit=HOW_YOU_FIT&...` 页面）。

跟 linkedin_tracker.py 同步"已收藏"/"已投递"的关系：两边都是"开登录态浏览器扫一个
列表页、把找到的职位链接转手给 job_link.add_jobs_from_urls() 入库"，共用
linkedin_list_scan.py 的扫描机制。区别在于 How You Fit 用的
`/jobs/search-results/` 页面结构没有被验证过（不像 jobs-tracker 那样已知管用），
所以这里比 tracker 多一层兜底：linkedin_list_scan 的确定性扫描如果一无所获（大概率
是没认出这个页面的实际交互方式，而不是真的零结果），会升级给一个 LLM 驱动的导航
agent 接管——让它自己观察页面上有哪些可交互元素、自己决定点哪个/滚哪个，而不是
我们继续猜一套新的硬编码选择器。这是项目里第一个真正的工具调用循环，只在确定性
路径失败时触发，日常成本接近零。

风险提醒：这个功能覆盖了 2026-08-18 记录在案的"不做 LinkedIn 个性化推荐流自动化
抓取"决策（见 spec/roadmap.md、spec/product-review.md），是用户知情后主动要求的
例外，配套了数量上限（MAX_HOW_YOU_FIT_SEARCHES）和同步间隔节流
（config 的 linkedin_how_you_fit_delay）来缓释账号风险。
"""
import logging
import os
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

import llm
import linkedin_list_scan
from config import load_config
from easy_apply import PROFILE_DIR, EasyApplyInProgress, _launch_context
from job_link import JOB_VIEW_URL, parse_linkedin_job_id

logger = logging.getLogger(__name__)

_URL_PREFIX = "https://www.linkedin.com/jobs/search-results"

MAX_HOW_YOU_FIT_SEARCHES = 8   # 配置最多允许几条搜索——2026-08-18 决策的具体补偿措施之一
MAX_AGENT_STEPS = 12           # agent 兜底最多循环几轮才强制判定"卡住"，防止无限调LLM


class HowYouFitSyncError(Exception):
    """整个同步流程没法继续时抛（没登录、profile被占用、浏览器起不来、search_id不存在、
    URL格式不对、agent判定卡住或超步数上限等）。"""


def _validate_search_url(url):
    """只要求开头是 https://www.linkedin.com/jobs/search-results，不强校验
    showHowYouFit 参数本身——那是 LinkedIn 前端实现细节，改名/去掉参数的可能性
    比域名+路径结构大得多，校验太严会导致以后 LinkedIn 一次前端调整就让所有已保存
    的搜索全部失效。"""
    if not (url or "").strip().startswith(_URL_PREFIX):
        raise HowYouFitSyncError(f"链接格式不对，应该是 {_URL_PREFIX} 开头的 LinkedIn 搜索结果页链接")


# ---------------------------------------------------------------- 确定性优先路径


def fetch_search_job_ids(url):
    """跟 linkedin_tracker.fetch_tracker_job_ids 同样的"先无头、撞墙带界面重试一次"
    逻辑，内部调 linkedin_list_scan.scan_job_list(url, headless)。撞登录墙两次都
    失败抛 HowYouFitSyncError 提示先登录。返回职位id集合（可能是空集——这是
    sync_search() 判断要不要升级给 agent 兜底的输入，本函数自己不做这个判断）。
    """
    if not os.path.isdir(PROFILE_DIR):
        raise HowYouFitSyncError(
            "还没有保存过 LinkedIn 登录态，请先在终端手动运行一次 "
            "python -c \"from easy_apply import ensure_logged_in; ensure_logged_in()\" 登录"
        )
    try:
        ids = linkedin_list_scan.scan_job_list(url, headless=True)
        if ids is None:
            logger.info("无头模式扫 How You Fit 搜索撞上登录墙，带界面重试一次")
            ids = linkedin_list_scan.scan_job_list(url, headless=False)
    except EasyApplyInProgress as e:
        raise HowYouFitSyncError(
            "有另一个 LinkedIn 浏览器窗口正在用同一个登录 profile"
            "（Easy Apply、添加链接的浏览器兜底、或另一次 tracker/How You Fit 同步），"
            "请等它结束后重试"
        ) from e
    if ids is None:
        raise HowYouFitSyncError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")
    return ids


# ---------------------------------------------------------------- agent 兜底路径
#
# 感知设计：不靠"更多"/"加载"/"下一页"这类关键词预筛候选元素——那等于我们又替 agent
# 猜了一遍它该找什么，猜错了跟硬编码选择器是同一个坑。改成结构性提取：抓页面主内容
# 区域（用 el.closest('nav, header, footer, ...') 排除站点通用"chrome"区域——这一刀
# 按 DOM landmark 结构切，不按文案关键词猜）内当前可见的所有可交互元素，加上检测到的
# 可滚动容器，统一编号成一份候选列表，每轮重新生成（用 data-hyf-idx 属性现场标记，
# 不跨轮持有 Playwright element handle——避免 DOM 变化后 handle 失效的问题）。
#
# 两条安全网：
# 1. 候选列表**排除**能被 job_link.parse_linkedin_job_id() 识别成职位链接的元素
#    （/jobs/view/、currentJobId=/jobId=）——这些点了会导航去职位详情页，把整个扫描
#    带偏；collect_job_ids() 已经单独收集这些链接，agent 的候选列表不需要、也不该
#    包含它们。这个判断刻意放在 Python 侧、用生产入库同一份 parse_linkedin_job_id()
#    做，不在 JS 里再手写一份正则重复判断同一件事——JS 只负责把每个候选元素的原始
#    href 带出来，两阶段设计（先在 JS 里给全部候选打临时编号，Python 筛完幸存者后
#    再回填正式编号）也让这条安全网能被单测直接覆盖，而不必真的起浏览器验证 JS 逻辑。
# 2. 每次执行完 agent 选的动作后，检查页面的 (scheme, host, path) 是否还跟原始 url
#    一致（忽略 query string——同一页面内的状态更新常见）；一旦不一致，判定被导航
#    离开，立刻中止，不再往下继续在错误的页面上瞎扫。
#
# 候选列表设上限 80 项，按元素在页面上的垂直位置排序截断，避免 token 成本随页面
# 复杂度失控——这是相对"关键词预筛"版本的明确代价，但只有确定性扫描失败时才会触发。

AGENT_TOOLS = [
    {
        "name": "click_element",
        "description": "点击候选列表里编号为 index 的元素",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "候选列表里的编号"}},
            "required": ["index"],
        },
    },
    {
        "name": "scroll",
        "description": (
            "滚动页面，让浏览器有机会懒加载更多职位卡片。不传 index 表示滚整个页面；"
            "传候选列表里某个可滚动容器的编号表示只滚那个容器"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "可选，候选列表里某个可滚动容器的编号"}},
            "required": [],
        },
    },
    {
        "name": "finish",
        "description": (
            "结束这次导航：已经收集完所有能看到的职位（reached_end），或者遇到没法处理的"
            "情况比如验证码/意外弹层/页面被导航离开搜索结果（stuck）"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["reached_end", "stuck"]},
                "reason": {"type": "string", "description": "简短说明判断依据"},
            },
            "required": ["status", "reason"],
        },
    },
]

AGENT_SYSTEM_PROMPT = """你在帮忙浏览一个 LinkedIn 登录态职位搜索结果页，唯一目标是让
页面尽可能多地加载出职位卡片（这个页面用懒加载/无限滚动或"加载更多"按钮展示结果，
具体机制未知，需要你自己观察判断）。

规则：
- 每一轮会告诉你当前收集到几个职位、比上一轮新增了几个，以及页面上有哪些候选可交互
  项（编号 + 类型 + 文字）。
- 只能从候选列表里选：用 click_element 点一个编号，或用 scroll 滚动（可以不传编号
  滚整个页面，也可以传一个"可滚动容器"编号只滚那个容器）。
- 连续几轮新增职位数量都是0、且候选列表里也没有看起来能加载更多的选项时，用
  finish(status="reached_end") 结束——这大概率代表已经到底了。
- 如果碰到验证码、意外弹窗、或者完全看不出该怎么继续，用 finish(status="stuck")
  并说明原因，不要瞎猜乱点。
- 不要点击任何看起来会离开当前搜索结果页的内容（比如职位标题本身、导航栏链接）。"""

# 第一遍：收集页面上所有候选元素（不做职位链接排除、不做最终编号），给每个候选打一个
# 临时的 data-hyf-raw-idx，附带 href（用于 Python 侧按 parse_linkedin_job_id 过滤）。
_PAGE_SNAPSHOT_JS = """
() => {
    document.querySelectorAll('[data-hyf-raw-idx]').forEach(el => el.removeAttribute('data-hyf-raw-idx'));
    document.querySelectorAll('[data-hyf-idx]').forEach(el => el.removeAttribute('data-hyf-idx'));

    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        return true;
    };
    const inChrome = (el) => !!el.closest(
        'nav, header, footer, [role="navigation"], [role="banner"], [role="contentinfo"]'
    );

    const raw = [];
    document.querySelectorAll(
        'button, a, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="menuitem"], [role="combobox"]'
    ).forEach(el => {
        if (inChrome(el) || !isVisible(el)) return;
        const href = el.tagName === 'A' ? el.getAttribute('href') : null;
        const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '')
            .trim().slice(0, 60);
        const rect = el.getBoundingClientRect();
        raw.push({el, kind: 'click', tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
                  text, href, top: rect.top});
    });

    document.querySelectorAll('div, section, ul, ol, main').forEach(el => {
        if (inChrome(el) || !isVisible(el)) return;
        const style = getComputedStyle(el);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 10) {
            const rect = el.getBoundingClientRect();
            if (rect.height > 100) {
                raw.push({el, kind: 'scroll', tag: el.tagName.toLowerCase(), role: '',
                          text: '(可滚动区域)', href: null, top: rect.top});
            }
        }
    });

    raw.sort((a, b) => a.top - b.top);
    // 这里的上限只是防止候选生成阶段本身失控（远大于最终展示给 agent 的 80 项），
    // 真正的展示上限和职位链接过滤都在 Python 侧做。
    const capped = raw.slice(0, 200);
    const result = capped.map((item, i) => {
        item.el.setAttribute('data-hyf-raw-idx', String(i));
        return {raw_index: i, kind: item.kind, tag: item.tag, role: item.role, text: item.text, href: item.href};
    });
    return {items: result};
}
"""

# 第二遍：把 Python 侧筛出的幸存候选（按最终展示顺序）正式编号成 data-hyf-idx，供
# _apply_agent_action 用选择器定位。
_TAG_SURVIVORS_JS = """
(rawIndices) => {
    rawIndices.forEach((rawIdx, finalIdx) => {
        const el = document.querySelector('[data-hyf-raw-idx="' + rawIdx + '"]');
        if (el) el.setAttribute('data-hyf-idx', String(finalIdx));
    });
}
"""

MAX_AGENT_CANDIDATES = 80


def _describe_page_for_agent(page, current_count, previous_count):
    """生成给 LLM 看的一段文字状态，见模块顶部"感知设计"说明。返回
    (描述文字, candidates)，candidates 是 {index: {"selector","kind"}}，供
    _apply_agent_action 用；描述文字只暴露编号+可读文案，不把选择器暴露给模型。"""
    snapshot = page.evaluate(_PAGE_SNAPSHOT_JS)
    raw_items = snapshot["items"]

    # 安全网1：排除能被 parse_linkedin_job_id() 识别成职位链接的候选（见模块顶部说明）。
    survivors = [item for item in raw_items if not parse_linkedin_job_id(item.get("href") or "")]
    truncated = max(0, len(survivors) - MAX_AGENT_CANDIDATES)
    survivors = survivors[:MAX_AGENT_CANDIDATES]

    page.evaluate(_TAG_SURVIVORS_JS, [item["raw_index"] for item in survivors])

    delta = current_count - previous_count
    lines = [
        f"当前已收集到 {current_count} 个职位（上一轮 {previous_count} 个，"
        + (f"新增了 {delta} 个" if delta > 0 else "这一轮没有新增") + "）。",
        "页面上可交互的候选项（点击用 click_element，滚动用 scroll）：",
    ]
    candidates = {}
    if not survivors:
        lines.append("（没有识别到任何候选项）")
    for final_idx, item in enumerate(survivors):
        candidates[final_idx] = {"selector": f'[data-hyf-idx="{final_idx}"]', "kind": item["kind"]}
        kind_label = "可点击" if item["kind"] == "click" else "可滚动容器"
        role_part = f"[{item['role']}]" if item["role"] else ""
        lines.append(f"  [{final_idx}] {kind_label} <{item['tag']}>{role_part} {item['text']!r}")
    if truncated:
        lines.append(f"（还有 {truncated} 项候选未列出，已按页面位置截断）")
    return "\n".join(lines), candidates


def _apply_agent_action(page, tool_name, tool_input, candidates):
    """把 agent 选的动作真正执行成 Playwright 操作。index 不在 candidates 里（模型
    编造了一个不存在的编号）当成一次无效动作处理：记日志、这一轮不做任何页面操作，
    正常进入下一轮——不因为模型偶尔选错编号就直接判 stuck，保留一次容错。"""
    if tool_name == "click_element":
        idx = tool_input.get("index")
        cand = candidates.get(idx)
        if not cand or cand["kind"] != "click":
            logger.warning("agent 选了一个无效的可点击候选编号：%r", idx)
            return
        try:
            page.locator(cand["selector"]).first.click(timeout=3000)
        except Exception:
            logger.warning("agent 点击候选 %s 失败", idx, exc_info=True)
    elif tool_name == "scroll":
        idx = tool_input.get("index")
        if idx is None:
            page.mouse.wheel(0, 3000)
            return
        cand = candidates.get(idx)
        if not cand or cand["kind"] != "scroll":
            logger.warning("agent 选了一个无效的可滚动容器编号：%r", idx)
            return
        try:
            page.locator(cand["selector"]).first.evaluate("el => { el.scrollTop += 2000; }")
        except Exception:
            logger.warning("agent 滚动候选容器 %s 失败", idx, exc_info=True)
    # finish 不需要页面操作，由调用方处理循环终止。


def _origin_and_path(url):
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc, parts.path)


def _scan_with_agent(url, headless=False):
    """确定性扫描怀疑卡住时的兜底，见模块顶部说明。开一次浏览器，导航到 url，循环
    最多 MAX_AGENT_STEPS 轮，每轮描述页面状态给 LLM、执行它选的动作、检查有没有被
    导航离开、重新收集职位id。模型判定 stuck、页面被导航离开、没有 ANTHROPIC_API_KEY、
    或超过步数上限仍未 finish：都抛 HowYouFitSyncError，不静默返回不完整结果。
    模型判定 reached_end：返回收集到的职位id集合（可以是空集，代表页面本身确实没有
    匹配结果）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HowYouFitSyncError(
            "AI 导航兜底需要 ANTHROPIC_API_KEY 环境变量，当前未设置，确定性扫描又没能"
            "收集到任何职位，本次同步中止"
        )

    expected_origin_path = _origin_and_path(url)

    with sync_playwright() as p:
        try:
            context = _launch_context(p, headless=headless)
        except EasyApplyInProgress as e:
            raise HowYouFitSyncError(
                "有另一个 LinkedIn 浏览器窗口正在用同一个登录 profile，请等它结束后重试"
            ) from e
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            if any(marker in page.url for marker in ("/login", "authwall", "checkpoint")):
                raise HowYouFitSyncError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")

            job_ids = linkedin_list_scan.collect_job_ids(page)
            description, candidates = _describe_page_for_agent(page, len(job_ids), 0)
            messages = [{"role": "user", "content": description}]

            for _ in range(MAX_AGENT_STEPS):
                step_result = llm.chat_tool_step(messages, AGENT_TOOLS, system=AGENT_SYSTEM_PROMPT)
                messages.append({"role": "assistant", "content": step_result["content_blocks"]})
                tool_use = step_result["tool_use"]
                if tool_use is None:
                    raise HowYouFitSyncError("AI 没有按预期调用工具，导航中止")

                if tool_use["name"] == "finish":
                    status = tool_use["input"].get("status")
                    reason = tool_use["input"].get("reason", "")
                    if status == "reached_end":
                        return job_ids
                    raise HowYouFitSyncError(f"AI 判定导航卡住：{reason}")

                _apply_agent_action(page, tool_use["name"], tool_use["input"], candidates)
                page.wait_for_timeout(1200)

                if _origin_and_path(page.url) != expected_origin_path:
                    raise HowYouFitSyncError(
                        f"页面被导航离开了搜索结果页（当前地址：{page.url}），可能是AI"
                        "误点了职位链接或其它导航元素，本次同步中止"
                    )

                previous_count = len(job_ids)
                job_ids = linkedin_list_scan.collect_job_ids(page)
                description, candidates = _describe_page_for_agent(page, len(job_ids), previous_count)
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use["id"], "content": description}],
                })

            raise HowYouFitSyncError(f"AI 导航超过步数上限（{MAX_AGENT_STEPS}轮）仍未确定结束，本次同步中止")
        finally:
            context.close()


# ---------------------------------------------------------------- 对外入口


def sync_search(search_id):
    """完整同步配置里 id=search_id 的这一条 How You Fit 搜索。不检查 enabled 字段
    ——那只管每日批量是否包含它（见 sync_all_enabled_searches），手动触发单条同步
    应该始终生效。不做标题/地点粗筛，理由跟 tracker 同步一致：LinkedIn 自己判定
    "符合资格"的结果，不该被关键词粗筛二次质疑。"""
    from job_link import MAX_URLS, add_jobs_from_urls

    cfg = load_config()
    searches = {s["id"]: s for s in (cfg.get("linkedin_how_you_fit_searches") or [])}
    search = searches.get(search_id)
    if not search:
        raise HowYouFitSyncError(f"配置里找不到这条 How You Fit 搜索：{search_id}")
    url = search["url"]

    job_ids = fetch_search_job_ids(url)
    if not job_ids:
        logger.info("How You Fit 确定性扫描抱空，升级给 agent 接管重新判断：%s", search.get("name"))
        job_ids = _scan_with_agent(url, headless=False)

    if not job_ids:
        return {"results": [], "added_ids": [], "total_found": 0}

    urls = [JOB_VIEW_URL.format(job_id=jid) for jid in sorted(job_ids)]
    all_results = []
    all_added = []
    for i in range(0, len(urls), MAX_URLS):
        batch_result = add_jobs_from_urls(urls[i:i + MAX_URLS])
        all_results.extend(batch_result["results"])
        all_added.extend(batch_result["added_ids"])
    return {"results": all_results, "added_ids": all_added, "total_found": len(urls)}


def sync_all_enabled_searches(delay_seconds=None):
    """遍历配置里 enabled=True 的每一条，依次 sync_search()，条间 sleep
    delay_seconds（默认读 config 的 linkedin_how_you_fit_delay）。单条异常（包括
    agent 判定 stuck 的情况）只记日志、继续下一条。返回
    {search_id: {"result": {...}|None, "error": str|None}, ...}。"""
    cfg = load_config()
    if delay_seconds is None:
        delay_seconds = cfg.get("linkedin_how_you_fit_delay", 30)
    searches = [s for s in (cfg.get("linkedin_how_you_fit_searches") or []) if s.get("enabled", True)]

    summary = {}
    for i, search in enumerate(searches):
        search_id = search["id"]
        try:
            result = sync_search(search_id)
            summary[search_id] = {"result": result, "error": None}
        except Exception as e:
            logger.exception("How You Fit 同步失败：%s", search.get("name"))
            summary[search_id] = {"result": None, "error": str(e) or e.__class__.__name__}
        if delay_seconds and i < len(searches) - 1:
            time.sleep(delay_seconds)
    return summary
