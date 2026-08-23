"""把 LinkedIn 的"职位跟踪"列表（jobs-tracker/?stage=<stage>）同步进职达。

LinkedIn 自己的 jobs-tracker 页面按 `stage` 参数分好几个列表：`saved`（已收藏）、
`applied`（已投递）等，页面结构和抓取方式都一样，只是要找的链接来自不同的筛选结果，
以及入库之后要不要顺带改一下投递状态——这个模块只负责前半件事（找齐一个 stage 下
出现过的所有职位链接），入库交给 job_link.add_jobs_from_urls()，"入库之后要不要
顺带改投递状态"这种业务语义留给调用方（app.py）决定，本模块不关心。

跟 job_link.py 的关系：那边负责"给一条职位链接，抓详情+去重+入库"；这边只多做一件
它做不到的事——jobs-tracker 列表页本身要登录才能看，得先找齐上面有哪些职位链接，
找齐之后转手交给 job_link.add_jobs_from_urls() 做剩下的全部工作，不重复写抓取/
入库逻辑。

实际"开浏览器扫列表页"的机制（滚动/点加载更多、识别登录墙、收集职位链接）搬到了
linkedin_list_scan.py——linkedin_how_you_fit.py 同步"How You Fit"匹配推荐搜索
要用同一套机制，两边共用，不重复写一份（详见 linkedin_list_scan.py 顶部说明）。

登录态复用 easy_apply.py 的持久化 profile（.playwright_profile/linkedin/），
首次使用前必须先手动跑一次 easy_apply.ensure_logged_in()，这里不会、也不该替用户
处理登录（跟 job_link.py 的浏览器兜底一样，是同一个 profile、同一条登录路径）。
"""

import logging
import os

from easy_apply import PROFILE_DIR, EasyApplyInProgress
from job_link import JOB_VIEW_URL
from linkedin_list_scan import STABLE_ROUNDS_TO_STOP, scan_job_list  # noqa: F401 (STABLE_ROUNDS_TO_STOP 供测试引用)

logger = logging.getLogger(__name__)

TRACKER_URL_TEMPLATE = "https://www.linkedin.com/jobs-tracker/?stage={stage}"

# 目前支持同步的 stage，跟 LinkedIn 页面上"已收藏"/"已投递"两个 tab 对应；LinkedIn
# 那边其实还有 archived 等其它 stage，用户没提出同步需求前不主动支持（每加一个都要
# 假设它的 DOM 结构方式跟这两个一样，没验证过的话宁可先不做）。
SUPPORTED_STAGES = ("saved", "applied")


class TrackerSyncError(Exception):
    """整个同步流程没法继续时抛（没登录、profile 被占用、浏览器起不来、stage 不支持等）。"""


def _scan_tracker_jobs(stage, headless):
    """开一次浏览器扫一遍某个 stage 的列表。返回职位id集合；撞上登录墙返回 None
    （不算异常，调用方决定是否要换个模式重试——无头模式撞上登录墙不代表真的没登录，
    见下面 fetch_tracker_job_ids 的说明）。"""
    try:
        return scan_job_list(TRACKER_URL_TEMPLATE.format(stage=stage), headless=headless)
    except EasyApplyInProgress as e:
        raise TrackerSyncError(
            "有另一个 LinkedIn 浏览器窗口正在用同一个登录 profile"
            "（Easy Apply、添加链接的浏览器兜底、或另一次同步），请等它结束后重试"
        ) from e


def fetch_tracker_job_ids(stage):
    """某个 stage 列表里出现过的全部职位 id。没有登录态/登录已失效会抛
    TrackerSyncError，提示先跑一次 ensure_logged_in()。

    先试无头模式，如果撞上登录墙再带界面重试一次——跟 job_link.fetch_via_browser()
    同样的取舍：LinkedIn 对无头浏览器的识别比带界面严，偶尔会对无头会话直接甩登录墙，
    即使 cookie 其实还有效；带界面重试一次通常就好了，不代表真的没登录。
    """
    if stage not in SUPPORTED_STAGES:
        raise TrackerSyncError(f"暂不支持同步这个列表：{stage}")
    if not os.path.isdir(PROFILE_DIR):
        raise TrackerSyncError(
            "还没有保存过 LinkedIn 登录态，请先在终端手动运行一次 "
            "python -c \"from easy_apply import ensure_logged_in; ensure_logged_in()\" 登录"
        )

    ids = _scan_tracker_jobs(stage, headless=True)
    if ids is None:
        logger.info("无头模式扫 %s 列表撞上登录墙，带界面重试一次", stage)
        ids = _scan_tracker_jobs(stage, headless=False)
    if ids is None:
        raise TrackerSyncError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")
    return ids


def sync_tracker_stage(stage):
    """完整同步一次某个 stage：抓列表的职位链接，交给 job_link.add_jobs_from_urls()
    逐条抓详情+去重+入库。分批调用（每批 job_link.MAX_URLS 条）是跟"添加链接"手动
    入口同一条节流逻辑——列表可能有几百条，不该一次性全砸给访客页+浏览器兜底。

    只负责"链接找齐+入库"，不处理入库后要不要顺带改投递状态之类的业务语义——那是
    调用方（app.py）该做的事，这里对 stage 具体含义（"已收藏"还是"已投递"）不敏感，
    只是拿它去拼 URL、过滤 SUPPORTED_STAGES。

    返回 {"results": [...], "added_ids": [...], "total_found": N}，results/added_ids
    的格式跟 add_jobs_from_urls() 完全一样（前端已有渲染逐条结果的代码，直接复用）。
    """
    from job_link import MAX_URLS, add_jobs_from_urls

    job_ids = fetch_tracker_job_ids(stage)
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
