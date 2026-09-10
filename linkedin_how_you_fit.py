"""同步 LinkedIn "How You Fit" 求职资格匹配搜索结果页——登录态下 LinkedIn 基于用户
档案算出的"你可能符合条件"的搜索结果（带 keywords=/geoId= 等参数的
`/jobs/search-results/?showHowYouFit=HOW_YOU_FIT&...` 页面）。

跟 linkedin_tracker.py 同步"已收藏"/"已投递"的关系：两边都是"开登录态浏览器扫一个
列表页、把找到的职位链接转手给 job_link.add_jobs_from_urls() 入库"，共用
linkedin_list_scan.py 的扫描机制。区别在于 How You Fit 用的
`/jobs/search-results/` 页面结构没有被验证过（不像 jobs-tracker 那样已知管用），
所以这里比 tracker 多一层兜底：linkedin_list_scan 的确定性扫描如果一无所获、或者
收集到的数量明显偏少（低于 MIN_DETERMINISTIC_JOB_IDS——大概率是没认出这个页面的
实际交互方式，比如没找到真正的可滚动容器，而不是真的没那么多结果），会升级给一个
LLM 驱动的导航 agent 接管——让它自己观察页面上有哪些可交互元素、自己决定点哪个/
滚哪个，而不是我们继续猜一套新的硬编码选择器。这是项目里第一个真正的工具调用循环，
只在确定性路径看起来不够时触发，日常成本接近零。

风险提醒：这个功能覆盖了 2026-08-18 记录在案的"不做 LinkedIn 个性化推荐流自动化
抓取"决策（见 spec/roadmap.md、spec/product-review.md），是用户知情后主动要求的
例外，配套了数量上限（MAX_HOW_YOU_FIT_SEARCHES）和同步间隔节流
（config 的 linkedin_how_you_fit_delay）来缓释账号风险。
"""
import logging
import os
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

import collect_errors
import job_state
import llm
import linkedin_list_scan
from config import load_config
from easy_apply import PROFILE_DIR, EasyApplyInProgress, _launch_context
from job_link import JOB_VIEW_URL, parse_linkedin_job_id

logger = logging.getLogger(__name__)

_URL_PREFIX = "https://www.linkedin.com/jobs/search-results"

MAX_HOW_YOU_FIT_SEARCHES = 12  # 配置最多允许几条搜索——2026-08-18 决策的具体补偿措施之一，2026-08-29 从8上调
MAX_AGENT_STEPS = 3            # agent 兜底最多循环几轮才强制判定"卡住"，防止无限调LLM

# 确定性扫描收集到的职位数低于这个阈值时，也升级给 agent 兜底复核（不再要求"完全
# 抱空"才升级）。起因：How You Fit 页面左侧职位列表很可能是独立的可滚动容器，
# linkedin_list_scan 的 page.mouse.wheel 兜底滚的是整个窗口，滚不到那个容器——
# 于是收集到最初渲染的寥寥几个职位后，连续 3 轮没有新增就被判定"到底"提前停手
# （不是真的抱空，所以旧版 `if not job_ids` 条件不会触发升级）。agent 兜底自己会
# 扫描页面找真正的可滚动容器（见下方 _PAGE_SNAPSHOT_JS），所以数量偏少时也该给它
# 一次机会。代价是这类情况会多花一次 LLM 调用，但 MAX_HOW_YOU_FIT_SEARCHES 已经
# 把每日总次数上限卡住了，可接受。
MIN_DETERMINISTIC_JOB_IDS = 5


class HowYouFitSyncError(Exception):
    """整个同步流程没法继续时抛（没登录、profile被占用、浏览器起不来、search_id不存在、
    URL格式不对、agent判定卡住或超步数上限等）。"""


class HowYouFitAuthError(HowYouFitSyncError, job_state.LinkedInAuthRequired):
    """登录态确定已失效/未登录（区别于 profile 被占用）——多重继承 job_state.
    LinkedInAuthRequired 让熔断器能认出这一类失败，同时不破坏现有 `except
    HowYouFitSyncError` 调用点。"""


def _validate_search_url(url):
    """只要求开头是 https://www.linkedin.com/jobs/search-results，不强校验
    showHowYouFit 参数本身——那是 LinkedIn 前端实现细节，改名/去掉参数的可能性
    比域名+路径结构大得多，校验太严会导致以后 LinkedIn 一次前端调整就让所有已保存
    的搜索全部失效。"""
    if not (url or "").strip().startswith(_URL_PREFIX):
        raise HowYouFitSyncError(f"链接格式不对，应该是 {_URL_PREFIX} 开头的 LinkedIn 搜索结果页链接")


def _extract_keyword(url):
    """从搜索链接的 keywords= 查询参数里取出用户当初在 LinkedIn 上填的搜索词，供
    sync_search() 入库前做标题粗筛用（见 job_link.add_jobs_from_urls 的 keyword
    参数）。没有这个参数（用户贴的链接本身就没带，或者 LinkedIn 以后改了参数名）
    就返回 None，退化成不筛——跟粗筛在 relevance.py 别处的取舍一致：判断不了就不
    拦，宁可多留几条可疑结果，也不要因为解析失败而错杀真正相关的职位。"""
    try:
        values = parse_qs(urlsplit(url).query).get("keywords")
    except Exception:
        return None
    return (values[0] or "").strip() if values else None


# ---------------------------------------------------------------- 确定性优先路径


def fetch_search_job_ids(url):
    """跟 linkedin_tracker.fetch_tracker_job_ids 同样的"先无头、撞墙带界面重试一次"
    逻辑，内部调 linkedin_list_scan.scan_job_list(url, headless)。撞登录墙两次都
    失败抛 HowYouFitAuthError 提示先登录。返回职位id集合（可能是空集——这是
    sync_search() 判断要不要升级给 agent 兜底的输入，本函数自己不做这个判断）。

    熔断（2026-08-29，见 job_state.py 顶部说明）：最近连续判定过几次登录态失效会直接
    快速失败，不再真的发请求撞墙。判断标准跟 linkedin_tracker.fetch_tracker_job_ids
    一致：只有真实发出过请求、无头+带界面都确认撞墙的分支才计入熔断计数。
    """
    if job_state.linkedin_auth_breaker_open():
        raise HowYouFitAuthError(
            "LinkedIn 登录态最近连续判定失效，已暂停自动化 "
            f"{job_state.linkedin_auth_breaker_remaining_seconds() // 60} 分钟，避免继续撞墙"
            "增加账号风险；如果已经手动确认登录态没问题，重新运行一次 ensure_logged_in() "
            "登录即可让下一次尝试重新计入成功"
        )
    if not os.path.isdir(PROFILE_DIR):
        raise HowYouFitAuthError(
            "还没有保存过 LinkedIn 登录态，请先在终端手动运行一次 "
            "python -c \"from easy_apply import ensure_logged_in; ensure_logged_in()\" 登录"
        )
    try:
        # 对瞬时失败分层重试（2026-08-29，见 collect_errors.with_retry 说明）：只重试
        # transient/rate_limited，EasyApplyInProgress 会被 classify() 判成 locked，
        # 第一次就直接向上抛，不浪费重试次数在"等别的窗口"这种重试没用的场景上。
        ids = collect_errors.with_retry(linkedin_list_scan.scan_job_list, url, headless=True)
        if ids is None:
            logger.info("无头模式扫 How You Fit 搜索撞上登录墙，带界面重试一次")
            ids = collect_errors.with_retry(linkedin_list_scan.scan_job_list, url, headless=False)
    except EasyApplyInProgress as e:
        raise HowYouFitSyncError(
            "有另一个 LinkedIn 浏览器窗口正在用同一个登录 profile"
            "（Easy Apply、添加链接的浏览器兜底、或另一次 tracker/LinkedIn 智能匹配推荐同步），"
            "请等它结束后重试"
        ) from e
    if ids is None:
        job_state.record_linkedin_auth_failure()
        raise HowYouFitAuthError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")
    job_state.record_linkedin_auth_success()
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
# 1. 候选列表**排除**能被识别成职位卡片本身的元素——两种识别方式并存：
#    a) job_link.parse_linkedin_job_id() 认得的职位链接（/jobs/view/、
#       currentJobId=/jobId=），对应旧版 `<a href="/jobs/view/ID">` 卡片；
#    b) componentkey 属性带 `job-card-component-ref-<id>` 模式的元素（2026-08-30
#       补的，见 linkedin_list_scan.py 顶部说明），对应 LinkedIn 改版后的
#       `<div role="button" componentkey="...">` 卡片——这种卡片没有 href，只排除
#       (a) 完全拦不住，实测会被 agent 当成"可点击候选"逐个点开（点开只是切到
#       右侧详情面板，不算真的导航离开，"检测被导航离开"这道安全网也拦不住），
#       白白浪费步数预算、一个新职位都收集不到。
#    这些点了要么导航去职位详情页、要么切到详情面板，都把整个扫描带偏；
#    collect_job_ids() 已经单独收集这些链接/id，agent 的候选列表不需要、也不该
#    包含它们。这两个判断刻意放在 Python 侧、跟生产入库同一份 parse_linkedin_job_id()
#    以及 linkedin_list_scan._COMPONENT_KEY_JOB_ID_RE 共用，不在 JS 里再手写一份
#    正则重复判断同一件事——JS 只负责把每个候选元素的原始 href/componentkey 带
#    出来，两阶段设计（先在 JS 里给全部候选打临时编号，Python 筛完幸存者后再回填
#    正式编号）也让这条安全网能被单测直接覆盖，而不必真的起浏览器验证 JS 逻辑。
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
        const componentKey = el.getAttribute('componentkey');
        const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '')
            .trim().slice(0, 60);
        const rect = el.getBoundingClientRect();
        raw.push({el, kind: 'click', tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
                  text, href, componentKey, top: rect.top});
    });

    document.querySelectorAll('div, section, ul, ol, main').forEach(el => {
        if (inChrome(el) || !isVisible(el)) return;
        const style = getComputedStyle(el);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 10) {
            const rect = el.getBoundingClientRect();
            if (rect.height > 100) {
                raw.push({el, kind: 'scroll', tag: el.tagName.toLowerCase(), role: '',
                          text: '(可滚动区域)', href: null, componentKey: null, top: rect.top});
            }
        }
    });

    raw.sort((a, b) => a.top - b.top);
    // 这里的上限只是防止候选生成阶段本身失控（远大于最终展示给 agent 的 80 项），
    // 真正的展示上限和职位链接过滤都在 Python 侧做。
    const capped = raw.slice(0, 200);
    const result = capped.map((item, i) => {
        item.el.setAttribute('data-hyf-raw-idx', String(i));
        return {raw_index: i, kind: item.kind, tag: item.tag, role: item.role, text: item.text,
                href: item.href, componentKey: item.componentKey};
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

    # 安全网1：排除职位卡片本身——href 版（旧版 <a> 卡片）和 componentkey 版（改版后
    # 的 <div role="button"> 卡片）两种都要拦，见模块顶部说明。
    def _is_job_card(item):
        if parse_linkedin_job_id(item.get("href") or ""):
            return True
        component_key = item.get("componentKey") or ""
        return bool(linkedin_list_scan._COMPONENT_KEY_JOB_ID_RE.search(component_key))

    job_card_count = sum(1 for item in raw_items if _is_job_card(item))
    survivors = [item for item in raw_items if not _is_job_card(item)]
    truncated = max(0, len(survivors) - MAX_AGENT_CANDIDATES)
    survivors = survivors[:MAX_AGENT_CANDIDATES]

    # 诊断日志（2026-08-30，见 spec/roadmap.md 排查记录，跟 linkedin_list_scan.
    # collect_job_ids() 里那条同一个目的）：曾观察到 collect_job_ids() 真实运行时
    # 一直收集到 0 个、但同一时刻这里的候选快照并不是空的。分开记"原始候选总数"
    # "其中判定为职位卡片被排除的数量""最终幸存数量"，方便对照 collect_job_ids()
    # 的日志判断：如果这里 raw_items 本身就是 0（说明整个页面这一刻可交互元素都
    # 没渲染出来，时序问题），还是 raw_items 不少但 job_card_count 一直是 0
    # （说明职位卡片这批元素本身就没进入这份快照，可能是选择器/懒加载问题）。
    # 用 info 级别而不是 debug——这条路径本来就只在 agent 兜底时才走，每轮一条，
    # 频率低，跟同一函数里其它 info 日志（"升级给 agent 接管复核"）一个量级，不
    # 需要额外去改日志级别配置才能在下次真实失败时看到。
    logger.info(
        "_describe_page_for_agent: 原始候选 %d 个（判定为职位卡片排除 %d 个），最终幸存 %d 个",
        len(raw_items), job_card_count, len(survivors),
    )

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
    导航离开、重新收集职位id。模型判定 stuck、页面被导航离开、两个 API key 都没有：
    都抛 HowYouFitSyncError，不返回任何数据——这几种情况要么还没开始扫（两个 key
    都没配）、要么模型自己声明"这次的结果不可信"（stuck/被导航离开），继续用已收集
    的 job_ids 没有意义。
    模型判定 reached_end：返回收集到的职位id集合（可以是空集，代表页面本身确实没有
    匹配结果）。

    超过步数上限（2026-08-30 修复的真实数据丢失 bug）：异常上挂 `partial_job_ids`
    属性，带上循环期间已经收集到的 job_ids，不再让调用方拿到空手而归——排查"用
    agent 测试"报"共找到 0 条"时发现，诊断日志显示 collect_job_ids() 每一轮都
    正确收集到了完整的 25 个职位（页面本身在第一轮就已经加载完，没有更多可加载），
    只是模型没有按提示词的预期在"连续几轮没有新增"时调用 finish(reached_end)，
    而是继续无意义地滚动/点击直到耗尽步数——这种情况下已收集到的 job_ids 是真实、
    可信的（跟 fetch_search_job_ids() 直接从同一个 DOM 读到的数据没有本质区别），
    只是模型没有明确"确认完成"，跟"判定卡住"那种模型主动声明不可信的情况性质不同，
    不该被同等对待、直接扔掉。调用方（sync_search()）读到这个属性后会把它并入
    确定性扫描结果、标 degraded 说明"步数耗尽但数据可能仍完整"，而不是静默漏掉。

    provider 优先用 DeepSeek（2026-08-29 起改的，成本更低），没有 DEEPSEEK_API_KEY 时
    退到 Anthropic——只要配了 ANTHROPIC_API_KEY 就不阻塞，两个都没配置才真的抛错。

    熔断检查（2026-08-29）：这里额外重复检查一次 job_state.linkedin_auth_breaker_open()
    ——fetch_search_job_ids() 已经在 sync_search() 里检查过一次，但批量同步一条搜索
    接一条跑，前一条搜索触发的熔断可能是在"这一条已经过了 fetch_search_job_ids 检查、
    正准备升级给 agent"之后才生效（同一批次内的时序问题），不在这里补一道会白白开一次
    浏览器、烧一次 LLM 调用才发现熔断已经打开。"""
    if job_state.linkedin_auth_breaker_open():
        raise HowYouFitAuthError(
            "LinkedIn 登录态最近连续判定失效，已暂停自动化 "
            f"{job_state.linkedin_auth_breaker_remaining_seconds() // 60} 分钟，避免继续撞墙"
            "增加账号风险，本次 agent 兜底跳过"
        )
    if os.environ.get("DEEPSEEK_API_KEY"):
        agent_provider = "deepseek"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        agent_provider = "anthropic"
    else:
        raise HowYouFitSyncError(
            "AI 导航兜底需要 ANTHROPIC_API_KEY 或 DEEPSEEK_API_KEY 环境变量，当前均未"
            "设置，确定性扫描又没能收集到任何职位，本次同步中止"
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
                job_state.record_linkedin_auth_failure()
                raise HowYouFitAuthError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")

            job_ids = linkedin_list_scan.collect_job_ids(page)
            description, candidates = _describe_page_for_agent(page, len(job_ids), 0)
            messages = [{"role": "user", "content": description}]

            for _ in range(MAX_AGENT_STEPS):
                # 墙钟超时（2026-08-29）：deadline 是 sync_search() 设的整条运行
                # 的时间上限，agent 每轮都要看一次 LLM+页面交互两步，任何一步偶尔
                # 卡住都可能拖很久，MAX_AGENT_STEPS 只保证轮数有上限，保证不了时间
                # 有上限。转成 HowYouFitSyncError 而不是让 TimeoutError 直接向上
                # 抛：sync_search() 的降级路径只捕获 HowYouFitSyncError（见③的
                # 说明），保持"agent 兜底只抛这一种异常"的既有约定，让超时也能走
                # 正常的降级流程而不是让整条同步硬失败。
                try:
                    collect_errors.check_deadline()
                except TimeoutError as e:
                    raise HowYouFitSyncError(f"AI 导航超过本次运行的时间上限：{e}") from e
                # 全项目唯一不走 llm.resolve_task() 的 LLM 调用（用默认模型、不查
                # llm_tasks），所以要显式标一下任务名，否则这条调用在流水里会顶着
                # 上一次调用留下的标签。顺带让这条一直隐形的 agent 兜底第一次进成本账。
                with llm.task_context("how_you_fit_agent"):
                    step_result = llm.chat_tool_step(
                        messages, AGENT_TOOLS, provider=agent_provider, system=AGENT_SYSTEM_PROMPT
                    )
                messages.append(step_result["assistant_message"])
                tool_use = step_result["tool_use"]
                if tool_use is None:
                    raise HowYouFitSyncError("AI 没有按预期调用工具，导航中止")

                if tool_use["name"] == "finish":
                    status = tool_use["input"].get("status")
                    reason = tool_use["input"].get("reason", "")
                    if status == "reached_end":
                        return job_ids
                    # "判定卡住"大概率是页面结构变了/出现了没见过的弹层，留一份现场
                    # （HTML+截图）——见 collect_errors.save_debug_snapshot() 说明，
                    # 复现这类故障平时要重新触发一次同步再等它卡住，有现场能省下这一趟。
                    collect_errors.save_debug_snapshot("hyf_agent_stuck", page)
                    raise HowYouFitSyncError(f"AI 判定导航卡住：{reason}")

                _apply_agent_action(page, tool_use["name"], tool_use["input"], candidates)
                page.wait_for_timeout(1200)

                if _origin_and_path(page.url) != expected_origin_path:
                    collect_errors.save_debug_snapshot("hyf_agent_navigated_away", page)
                    raise HowYouFitSyncError(
                        f"页面被导航离开了搜索结果页（当前地址：{page.url}），可能是AI"
                        "误点了职位链接或其它导航元素，本次同步中止"
                    )

                previous_count = len(job_ids)
                job_ids = linkedin_list_scan.collect_job_ids(page)
                description, candidates = _describe_page_for_agent(page, len(job_ids), previous_count)
                messages.append(llm.tool_result_message(agent_provider, tool_use["id"], description))

            collect_errors.save_debug_snapshot("hyf_agent_step_limit", page)
            step_limit_error = HowYouFitSyncError(
                f"AI 导航超过步数上限（{MAX_AGENT_STEPS}轮）仍未确定结束，本次同步中止"
            )
            step_limit_error.partial_job_ids = job_ids
            raise step_limit_error
        finally:
            context.close()


# ---------------------------------------------------------------- 对外入口


def sync_search(search_id, force_agent=False):
    """完整同步配置里 id=search_id 的这一条 How You Fit 搜索。不检查 enabled 字段
    ——那只管每日批量是否包含它（见 sync_all_enabled_searches），手动触发单条同步
    应该始终生效。

    标题粗筛（2026-09-04 恢复，修复原来"完全不筛"的设计缺陷）：原先这里跟 tracker
    同步一样不做标题/地点粗筛，理由是"LinkedIn 自己判定'符合资格'的结果，不该被
    关键词粗筛二次质疑"——但 tracker 同步的"已收藏/已投递/面试"列表是用户自己在
    LinkedIn 上一条条操作过的，How You Fit 是 LinkedIn 算法按 `url` 里的
    `keywords=` 参数算出来的"可能符合条件"候选，两者信任基础不一样。用户实测发现
    这个算法并不总是老实按标题匹配，会混进标题跟 keywords 参数完全不沾边的职位
    （2026-09-04反馈）。所以这里改成从 `url` 解析出 `keywords=` 的值（见
    `_extract_keyword`），传给 `add_jobs_from_urls` 的 `keyword` 参数，用
    `relevance.title_looks_relevant` 挡掉标题不沾边的结果（标成
    skipped_irrelevant，不入库）。地点仍然不筛——`geoId=` 是 LinkedIn 内部数字
    地理编码，没有现成的映射表可以还原成城市名，跟标题粗筛不是同一个量级的工作，
    这次先不做。

    force_agent=True（设置页"用 agent 测试"按钮，2026-08-29）：跳过
    fetch_search_job_ids 那条确定性扫描，直接强制升级给 _scan_with_agent()
    接管，用来肉眼观察 agent 打开的浏览器实际怎么操作——收集到的职位照常走下面
    同一条入库路径，跟正常触发（确定性扫描数量不够）相比只是"怎么触发 agent"
    不同，入库、记 collect_runs 都不变。

    agent 兜底失败不再连累确定性扫描的结果（2026-08-29 修复的设计级 bug）：升级前
    是 `job_ids = job_ids | _scan_with_agent(...)`——`_scan_with_agent` 判定 stuck
    或超步数上限时抛异常，会让整个 `sync_search` 失败，连确定性扫描已经拿到手的那
    几条也一起丢掉，等于"不升级还能入库几条，升级了反而 0 条"，agent 兜底从"增强"
    变成了额外的失败源。现在 agent 兜底失败只记日志、返回结果里带上 `degraded`
    字段说明原因，确定性扫描的 job_ids 照常拿去入库。

    成败都往 collect_runs 记一行（2026-08-29，见 spec/roadmap.md「职位收集链路的
    错误处理生产级加固」缺口7），source 是 `hyf_<search_id>`。降级（agent 兜底失败
    但确定性结果保住了）算 `ok=1`，`error_kind` 记成 `structure`——不用
    collect_errors.classify() 判：agent 失败的 HowYouFitSyncError 不是熔断/限流那
    类可以从异常类型直接判断的失败，"判定卡住"/"超步数上限"/"被导航离开"这几种绝
    大多数确实是页面结构问题，只有"两个 API key 都没配"这种配置缺失被一起归进
    structure 不够精确，但这种情况一旦配好环境变量就不会再复现，不值得为它单独
    分一类。"链接找齐"阶段（fetch_search_job_ids）失败会让整条同步没法继续，记一行
    失败后原样重新抛出。

    墙钟超时（缺口⑥，2026-08-29）：整个函数体设一个统一的 deadline
    （`collect_errors.start_run_deadline()`），确定性扫描的滚动循环、agent 兜底
    循环、按 MAX_URLS 分批入库的循环都会检查这个 deadline——不再让一次同步没有
    任何时间上限地占着 `start_how_you_fit_sync()` 的 409 锁。

    空结果健康检查（缺口⑦，2026-08-29）：只在"没有走 agent 兜底"的路径上跟历史
    比——一旦触发过 agent 兜底（不管成功还是降级），数量本身已经是"确定性扫描 +
    agent 修正"的结果，用同一个历史基线去比较意义不大（历史里那些"没触发 agent"
    的高位数字不是同一种情况的对照组）。
    """
    from job_link import MAX_URLS, add_jobs_from_urls
    from models import insert_collect_run, recent_found_counts

    cfg = load_config()
    searches = {s["id"]: s for s in (cfg.get("linkedin_how_you_fit_searches") or [])}
    search = searches.get(search_id)
    if not search:
        raise HowYouFitSyncError(f"配置里找不到这条 LinkedIn 智能匹配推荐搜索：{search_id}")
    url = search["url"]
    keyword = _extract_keyword(url)
    source = f"hyf_{search_id}"

    run_started_iso = datetime.now().isoformat(timespec="seconds")
    run_started_at = time.time()
    collect_errors.reset_retry_count()
    collect_errors.start_run_deadline()
    try:
        if force_agent:
            job_ids = set()
        else:
            try:
                job_ids = fetch_search_job_ids(url)
            except Exception as e:
                insert_collect_run(
                    source=source, started_at=run_started_iso,
                    duration_ms=int((time.time() - run_started_at) * 1000),
                    found=0, added=0, skipped_duplicate=0, skipped_irrelevant=0, failed=0,
                    ok=0, error_kind=collect_errors.classify(e), error_detail=str(e) or e.__class__.__name__,
                    retries=collect_errors.get_retry_count(), agent_used=0, suspicious=0,
                )
                raise

        degraded = None
        agent_used = False
        if force_agent or len(job_ids) < MIN_DETERMINISTIC_JOB_IDS:
            agent_used = True
            logger.info(
                "How You Fit %s，升级给 agent 接管复核：%s",
                "被强制要求走 agent 兜底" if force_agent
                else f"确定性扫描只收集到 {len(job_ids)} 个职位（低于阈值 {MIN_DETERMINISTIC_JOB_IDS}）",
                search.get("name"),
            )
            try:
                job_ids = job_ids | _scan_with_agent(url, headless=False)
            except HowYouFitSyncError as e:
                degraded = str(e) or e.__class__.__name__
                # 步数耗尽时 agent 会把已收集到的 job_ids 挂在异常上（见
                # _scan_with_agent() 步数上限那段说明），并进来一起入库，不能因为
                # 模型没显式确认 reached_end 就把它已经读到的真实数据也一起扔掉。
                partial_job_ids = getattr(e, "partial_job_ids", None)
                if partial_job_ids:
                    job_ids = job_ids | partial_job_ids
                logger.warning(
                    "How You Fit agent 兜底失败，保留已收集到的 %d 个职位继续入库：%s：%s",
                    len(job_ids), search.get("name"), degraded,
                )

        if not job_ids:
            result = {"results": [], "added_ids": [], "total_found": 0}
        else:
            urls = [JOB_VIEW_URL.format(job_id=jid) for jid in sorted(job_ids)]
            all_results = []
            all_added = []
            for i in range(0, len(urls), MAX_URLS):
                collect_errors.check_deadline(f"同步 LinkedIn 智能匹配推荐搜索超过本次运行的时间上限：{search.get('name')}")
                batch_result = add_jobs_from_urls(urls[i:i + MAX_URLS], keyword=keyword)
                all_results.extend(batch_result["results"])
                all_added.extend(batch_result["added_ids"])
            result = {"results": all_results, "added_ids": all_added, "total_found": len(urls)}

        suspicious = (
            not agent_used
            and collect_errors.is_suspicious_drop(result["total_found"], recent_found_counts(source))
        )
        error_kind = "structure" if degraded else ("upstream_empty" if suspicious else None)
        error_detail = degraded or (
            f"本次只找到 {result['total_found']} 条，明显低于近期水平，可能是页面结构变了，不是真的没有新职位"
            if suspicious else None
        )
        insert_collect_run(
            source=source, started_at=run_started_iso,
            duration_ms=int((time.time() - run_started_at) * 1000),
            found=result["total_found"], added=len(result["added_ids"]),
            skipped_duplicate=len([r for r in result["results"] if r.get("status") == "duplicate"]),
            skipped_irrelevant=len([r for r in result["results"] if r.get("status") == "skipped_irrelevant"]),
            failed=len([r for r in result["results"] if r.get("status") == "failed"]),
            ok=1, error_kind=error_kind, error_detail=error_detail,
            retries=collect_errors.get_retry_count(), agent_used=int(agent_used), suspicious=int(suspicious),
        )
        result["degraded"] = degraded
        result["suspicious"] = suspicious
        return result
    finally:
        collect_errors.clear_run_deadline()


def sync_all_enabled_searches(delay_seconds=None):
    """遍历配置里 enabled=True 的每一条，依次 sync_search()，条间 sleep
    delay_seconds（默认读 config 的 linkedin_how_you_fit_delay）。单条异常（包括
    agent 判定 stuck 的情况）只记日志、继续下一条。返回
    {search_id: {"result": {...}|None, "error": str|None}, ...}。

    登录态熔断（2026-08-29，见 job_state.py 顶部说明）：如果某一条判定为真正的
    "登录态已失效"（job_state.LinkedInAuthRequired），立刻中止整批、不再尝试剩下的
    搜索——继续跑只是在同一个已经出问题的账号上反复撞墙，不是"这一条搜索恰好有
    问题"，跟其它类型的异常（agent 判定 stuck、单条 URL 格式不对等，只影响这一条）
    性质不同。剩下没跑到的搜索不计入 summary，不跟"跑过但失败"的搜索混在一起。
    """
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
        except job_state.LinkedInAuthRequired as e:
            logger.warning(
                "How You Fit 批量同步因登录态失效中止，剩余 %d 条搜索本次不再尝试",
                len(searches) - i - 1,
            )
            summary[search_id] = {"result": None, "error": str(e) or e.__class__.__name__}
            break
        except Exception as e:
            logger.exception("How You Fit 同步失败：%s", search.get("name"))
            summary[search_id] = {"result": None, "error": str(e) or e.__class__.__name__}
        if delay_seconds and i < len(searches) - 1:
            time.sleep(delay_seconds)
    return summary
