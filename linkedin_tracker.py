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
import time
from datetime import datetime

import collect_errors
import job_state
from easy_apply import PROFILE_DIR, EasyApplyInProgress
from job_link import JOB_VIEW_URL
from linkedin_list_scan import STABLE_ROUNDS_TO_STOP, scan_job_list  # noqa: F401 (STABLE_ROUNDS_TO_STOP 供测试引用)

logger = logging.getLogger(__name__)

TRACKER_URL_TEMPLATE = "https://www.linkedin.com/jobs-tracker/?stage={stage}"

# 目前支持同步的 stage，跟 LinkedIn 页面上"已收藏"/"已投递"/"面试"三个 tab 对应；
# LinkedIn 那边其实还有 offer、archived 等其它 stage，用户没提出同步需求前不主动
# 支持（每加一个都要假设它的 DOM 结构方式跟这几个一样，没验证过的话宁可先不做）。
#
# "面试"（interview）单独补上是因为发现 LinkedIn 会把职位从"已投递"列表挪进"面试"
# 列表后就不再出现在"已投递"里了——只同步 applied 会漏掉这些已经进入面试阶段、
# 但明明也是"已投递过"的职位（用户反馈过一个真实案例：某职位在 LinkedIn 上显示
# 已进入面试，但从没同步进职达，因为它当时已经不在 applied 列表里了）。
SUPPORTED_STAGES = ("saved", "applied", "interview")


class TrackerSyncError(Exception):
    """整个同步流程没法继续时抛（没登录、profile 被占用、浏览器起不来、stage 不支持等）。"""


class TrackerAuthError(TrackerSyncError, job_state.LinkedInAuthRequired):
    """登录态确定已失效/未登录（区别于 profile 被占用）——多重继承job_state.
    LinkedInAuthRequired 让熔断器能认出这一类失败，同时不破坏现有 `except
    TrackerSyncError` 调用点。"""


def _scan_tracker_jobs(stage, headless):
    """开一次浏览器扫一遍某个 stage 的列表。返回职位id集合；撞上登录墙返回 None
    （不算异常，调用方决定是否要换个模式重试——无头模式撞上登录墙不代表真的没登录，
    见下面 fetch_tracker_job_ids 的说明）。

    对瞬时失败分层重试（2026-08-29，见 collect_errors.with_retry 说明）：只重试
    transient/rate_limited，EasyApplyInProgress 会被 classify() 判成 locked，
    第一次就直接向上抛，不会浪费重试次数在"等别的窗口"这种重试没用的场景上。
    """
    try:
        return collect_errors.with_retry(
            scan_job_list, TRACKER_URL_TEMPLATE.format(stage=stage), headless=headless
        )
    except EasyApplyInProgress as e:
        raise TrackerSyncError(
            "有另一个 LinkedIn 浏览器窗口正在用同一个登录 profile"
            "（Easy Apply、添加链接的浏览器兜底、或另一次同步），请等它结束后重试"
        ) from e


def fetch_tracker_job_ids(stage):
    """某个 stage 列表里出现过的全部职位 id。没有登录态/登录已失效会抛
    TrackerAuthError（TrackerSyncError 的子类），提示先跑一次 ensure_logged_in()。

    先试无头模式，如果撞上登录墙再带界面重试一次——跟 job_link.fetch_via_browser()
    同样的取舍：LinkedIn 对无头浏览器的识别比带界面严，偶尔会对无头会话直接甩登录墙，
    即使 cookie 其实还有效；带界面重试一次通常就好了，不代表真的没登录。

    熔断（2026-08-29，见 job_state.py 顶部说明）：如果最近连续判定过几次登录态失效，
    这里会直接快速失败，不再真的去发请求撞墙——避免在账号已经被风控盯上的时候继续
    重试加深风险。只有"无头+带界面都确认撞墙"这个真实发生过网络请求的分支才会计入
    熔断计数；"从没保存过登录态"是本地配置检查，没发出任何请求，不计数。
    """
    if stage not in SUPPORTED_STAGES:
        raise TrackerSyncError(f"暂不支持同步这个列表：{stage}")
    if job_state.linkedin_auth_breaker_open():
        raise TrackerAuthError(
            "LinkedIn 登录态最近连续判定失效，已暂停自动化 "
            f"{job_state.linkedin_auth_breaker_remaining_seconds() // 60} 分钟，避免继续撞墙"
            "增加账号风险；如果已经手动确认登录态没问题，重新运行一次 ensure_logged_in() "
            "登录即可让下一次尝试重新计入成功"
        )
    if not os.path.isdir(PROFILE_DIR):
        raise TrackerAuthError(
            "还没有保存过 LinkedIn 登录态，请先在终端手动运行一次 "
            "python -c \"from easy_apply import ensure_logged_in; ensure_logged_in()\" 登录"
        )

    ids = _scan_tracker_jobs(stage, headless=True)
    if ids is None:
        logger.info("无头模式扫 %s 列表撞上登录墙，带界面重试一次", stage)
        ids = _scan_tracker_jobs(stage, headless=False)
    if ids is None:
        job_state.record_linkedin_auth_failure()
        raise TrackerAuthError("登录态已失效或未登录，请重新运行 ensure_logged_in() 登录后再试")
    job_state.record_linkedin_auth_success()
    return ids


def sync_tracker_stage(stage):
    """完整同步一次某个 stage：抓列表的职位链接，交给 job_link.add_jobs_from_urls()
    逐条抓详情+去重+入库。分批调用（每批 job_link.MAX_URLS 条）是跟"添加链接"手动
    入口同一条节流逻辑——列表可能有几百条，不该一次性全砸给访客页+浏览器兜底。

    只负责"链接找齐+入库"，不处理入库后要不要顺带改投递状态之类的业务语义——那是
    调用方（app.py）该做的事，这里对 stage 具体含义（"已收藏"还是"已投递"）不敏感，
    只是拿它去拼 URL、过滤 SUPPORTED_STAGES。

    返回 {"results": [...], "added_ids": [...], "total_found": N, "reconciled": R,
    "reconciled_details": [...]}，results/added_ids 的格式跟 add_jobs_from_urls()
    完全一样（前端已有渲染逐条结果的代码，直接复用）。reconciled_details 是
    models.reconcile_application_status_from_linkedin() 原样返回的明细列表，
    reconciled 是它的条数——调用方（app.py 的通知文案）要报"具体更新了哪几条"就从
    reconciled_details 里取，只要数量就用 reconciled，两个字段不用各自重新计算。

    额外做一步"投递状态核对"（2026-08-26 新增，起因见 models.
    reconcile_application_status_from_linkedin() 的说明）：不管这次同步的是哪个
    stage，都顺手拿"已投递"+"面试"这两个权威集合核对一遍库里全部 LinkedIn 职位的
    投递状态——不止改这次同步新入库的这批，历史上通过任何渠道入库的也一并核对。
    这一步失败（比如刚好撞上并发锁、登录态临时抖动）不该连累这次同步已经拿到手
    的入库结果，所以包了 try/except，失败只记日志、"reconciled" 计 0，不向上抛。

    成败都往 collect_runs 记一行（2026-08-29，见 spec/roadmap.md「职位收集链路的
    错误处理生产级加固」缺口7）：source 是 `tracker_<stage>`，之前这条路径完全不
    落库，查不到"最近几次 saved 同步分别找到几条"这类趋势。"链接找齐"阶段
    （fetch_tracker_job_ids）失败会让整条同步没法继续，记一行失败后原样重新抛出，
    不改变调用方原有的异常处理；"链接找齐"之后的入库阶段本身不太会抛异常
    （add_jobs_from_urls 内部单条失败已经落在 results 里），所以只在最后统一记一行
    成功。

    墙钟超时 + 空结果健康检查（缺口⑥⑦，2026-08-29）：整个函数体设一个统一的
    deadline（`collect_errors.start_run_deadline()`），`fetch_tracker_job_ids`→
    `_scan_tracker_jobs`→`scan_job_list` 的滚动循环、以及下面按 MAX_URLS 分批入库
    的循环，都会在每轮检查这个 deadline，超时按 transient 处理（`TimeoutError` 会
    被 `classify()` 认出来）——不再让一次同步没有任何时间上限地占着
    `start_tracker_sync()` 的 409 锁。成功但找到的数量跟同一 stage 最近几次比明显
    偏低时，`ok` 仍然是 1（真的跑完了，不是失败），但 `error_kind` 记成
    `upstream_empty`、`suspicious` 记 1，返回结果里也带上 `suspicious` 字段——
    页面结构悄悄变了导致选择器抓不到内容，往往就是这种"没报错但数量不对劲"的形态。
    """
    from job_link import MAX_URLS, add_jobs_from_urls
    from models import insert_collect_run, recent_found_counts, reconcile_application_status_from_linkedin

    source = f"tracker_{stage}"
    run_started_iso = datetime.now().isoformat(timespec="seconds")
    run_started_at = time.time()
    collect_errors.reset_retry_count()
    collect_errors.start_run_deadline()
    try:
        try:
            job_ids = fetch_tracker_job_ids(stage)

            urls = [JOB_VIEW_URL.format(job_id=jid) for jid in sorted(job_ids)]
            all_results = []
            all_added = []
            for i in range(0, len(urls), MAX_URLS):
                collect_errors.check_deadline(f"同步 {stage} 超过本次运行的时间上限")
                batch_result = add_jobs_from_urls(urls[i:i + MAX_URLS])
                all_results.extend(batch_result["results"])
                all_added.extend(batch_result["added_ids"])
        except Exception as e:
            insert_collect_run(
                source=source, started_at=run_started_iso,
                duration_ms=int((time.time() - run_started_at) * 1000),
                found=0, added=0, skipped_duplicate=0, skipped_irrelevant=0, failed=0,
                ok=0, error_kind=collect_errors.classify(e), error_detail=str(e) or e.__class__.__name__,
                retries=collect_errors.get_retry_count(), agent_used=0, suspicious=0,
            )
            raise

        reconciled_details = []
        try:
            applied_ids = job_ids if stage == "applied" else fetch_tracker_job_ids("applied")
            interview_ids = job_ids if stage == "interview" else fetch_tracker_job_ids("interview")
            reconciled_details = reconcile_application_status_from_linkedin(applied_ids, interview_ids)
        except Exception:
            logger.exception("同步 %s 后的投递状态核对失败，不影响本次同步已入库的结果", stage)

        suspicious = collect_errors.is_suspicious_drop(len(urls), recent_found_counts(source))
        insert_collect_run(
            source=source, started_at=run_started_iso,
            duration_ms=int((time.time() - run_started_at) * 1000),
            found=len(urls), added=len(all_added),
            skipped_duplicate=len([r for r in all_results if r.get("status") == "duplicate"]),
            skipped_irrelevant=0, failed=len([r for r in all_results if r.get("status") == "failed"]),
            ok=1, error_kind=("upstream_empty" if suspicious else None),
            error_detail=(f"本次只找到 {len(urls)} 条，明显低于近期水平，可能是页面结构变了，不是真的没有新职位") if suspicious else None,
            retries=collect_errors.get_retry_count(), agent_used=0, suspicious=int(suspicious),
        )
        return {
            "results": all_results, "added_ids": all_added, "total_found": len(urls),
            "reconciled": len(reconciled_details), "reconciled_details": reconciled_details,
            "suspicious": suspicious,
        }
    finally:
        collect_errors.clear_run_deadline()
