"""职位收集链路（scraper.py / job_link.py / linkedin_tracker.py /
linkedin_how_you_fit.py）失败分类的统一入口，供 collect_runs 落库、以后要接的分层
重试/账号熔断复用（见 spec/roadmap.md「职位收集链路的错误处理生产级加固」缺口2）。

在这之前，每个模块的 except 分支各自决定"记个日志就算了"，限流/网络抖动/登录失效/
DOM 改版/代码 bug 混在一起——但它们的正确处置正好相反：限流该退避降速，登录失效该
立刻停手（继续撞只会加深账号风险），DOM 改版该告警取证，bug 该修，不分类就谈不上
任何自动处置。

kind 取值（分类给的是"该怎么处置"的方向，不是精确诊断——同一个
requests.exceptions.ConnectionError 既可能是真的网络抖动，也可能是被限流后拒绝
连接，试图分得完全准确不现实，这里只保证给一个默认合理的处置）：
    transient       网络抖动/超时，值得原样重试
    rate_limited    对方明确表示限流（HTTP 429/文案），该退避降速再重试
    auth            登录态确定已失效，不该重试，该走 job_state 的账号熔断
    locked          浏览器 profile 被别的窗口占用，等它结束就好，跟账号本身无关
    structure       页面结构大概率变了（选择器认不出/agent判定卡住/导航离开），该
                    告警取证，不该反复重试撞同一堵墙
    upstream_empty  抓到的数量跟历史比明显偏低但没有异常，可疑但不确定
    bug             以上都不像，保守当成代码问题——不重试、不熔断，原样暴露出来

classify() 只覆盖能从异常类型/文案里判断的前四类（transient/rate_limited/auth/
locked）。structure 和 upstream_empty 依赖对结果内容的判断（比如"确定性扫描收集到
的职位数量偏少"、"agent 判定卡住的理由"），这类判断只能由调用方在构造 collect_run
记录时直接指定 kind，不经过 classify()——本模块刻意不 import
linkedin_how_you_fit/linkedin_tracker/job_link 反过来解析它们的错误消息，那样会
造成循环 import（那几个模块反过来要 import 本模块记录 collect_run）。
"""
import contextvars
import logging
import os
import random
import shutil
import statistics
import time
from datetime import datetime

import job_state
from easy_apply import EasyApplyInProgress

try:
    import requests
except ImportError:  # pragma: no cover - requests 是项目硬依赖，这里只是防御
    requests = None

logger = logging.getLogger(__name__)


KINDS = ("transient", "rate_limited", "auth", "locked", "structure", "upstream_empty", "bug")

# 每个 kind 对应的默认处置：要不要自动重试、退避多少次/多久、通知给用户看的级别。
# 目前只有 /api/collect/stats 的展示会读这张表；以后要接的分层重试（roadmap 里的⑤）
# 也会查这里，不在各模块里各自重复判断一遍。
RETRY_POLICY = {
    "transient":      {"retryable": True,  "max_retries": 3, "base_delay_s": 2,  "notify_level": "error"},
    "rate_limited":   {"retryable": True,  "max_retries": 2, "base_delay_s": 60, "notify_level": "error"},
    "auth":           {"retryable": False, "max_retries": 0, "base_delay_s": 0,  "notify_level": "error"},
    "locked":         {"retryable": False, "max_retries": 0, "base_delay_s": 0,  "notify_level": "info"},
    "structure":      {"retryable": False, "max_retries": 0, "base_delay_s": 0,  "notify_level": "error"},
    "upstream_empty": {"retryable": False, "max_retries": 0, "base_delay_s": 0,  "notify_level": "warning"},
    "bug":            {"retryable": False, "max_retries": 0, "base_delay_s": 0,  "notify_level": "error"},
}

_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate-limited")


def classify(exc, http_status=None):
    """给一次失败判定一个 kind。exc 可以是 None（配合 http_status 单独判断，比如
    HTTP 层面的失败没有抛异常、只是状态码不对）。

    也检查 exc.__cause__（`raise Xxx(...) from original_error` 留下的原始异常）——
    tracker/how_you_fit 里 EasyApplyInProgress 被捕获后会包成各自的 XxxSyncError
    再 `from e` 重新抛出，包完的类型对 classify() 就不再是 EasyApplyInProgress 了，
    不看 __cause__ 会把"profile 被占用"错判成 bug。只看一层，不递归整条链——这几个
    模块目前也只包一层。"""
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if isinstance(candidate, job_state.LinkedInAuthRequired):
            return "auth"
        if isinstance(candidate, EasyApplyInProgress):
            return "locked"
    if http_status == 429:
        return "rate_limited"
    if http_status is not None and http_status >= 500:
        return "transient"
    text = str(exc or "").lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if requests is not None and isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return "transient"
    if isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text:
        return "transient"
    if requests is not None and isinstance(exc, requests.exceptions.RequestException):
        return "transient"
    return "bug"


# ---------------------------------------------------------------- 分层重试（缺口⑤）

# 统计"这次运行触发了几次重试"，供 collect_runs 的 retries 字段用。用 ContextVar
# 而不是层层传参/返回值——scrape_jobs()/scan_job_list() 的调用链路很深（run_search_once
# → _ingest_df 之外的循环体、sync_tracker_stage → fetch_tracker_job_ids →
# _scan_tracker_jobs），逐层加个"重试计数器"形参会改动一大片函数签名，还会让已有
# 测试里替身掉中间某一层的 fake 函数因为签名对不上而报错；ContextVar 对调用链路
# 不可见，运行时自然透传，跟 llm.py 的 `_current_task` 是同一个手法。
_retry_count_var = contextvars.ContextVar("collect_retry_count", default=None)


def reset_retry_count():
    """在一次"运行"（run_search_once/sync_tracker_stage/sync_search 等）开始时调用
    一次，开始统计这次运行里 with_retry() 触发了几次重试。"""
    _retry_count_var.set(0)


def get_retry_count():
    """读当前这次运行累计触发了几次重试，写 collect_runs 的 retries 字段用。"""
    return _retry_count_var.get() or 0


def note_retry():
    """手动记一次重试，供 with_retry() 覆盖不到的场景用——比如 job_link.
    fetch_via_guest() 是按 HTTP 状态码（429/5xx）而不是异常判断要不要重试，走的是
    自己手写的退避循环，不经过 with_retry()，但同一次运行的重试计数应该看得到这里
    也发生过重试。"""
    current = _retry_count_var.get()
    if current is not None:
        _retry_count_var.set(current + 1)


def with_retry(fn, *args, **kwargs):
    """按 RETRY_POLICY 对 fn(*args, **kwargs) 做分层重试。

    只应该包一次"最小可安全重放的操作"——一次 scrape_jobs() 调用、一次
    scan_job_list() 会话——不要用来包整条同步流程，那样等于把可重放的粒度做粗了：
    一次同步里途中已经入库的部分不该因为后面某一步失败就被重放一遍。

    只对 transient/rate_limited 重试；auth/locked/structure/bug 第一次失败直接
    向上抛——这几类重试没有意义甚至有害（比如 auth 重试只会在已经出问题的账号上
    继续撞，账号风险不会因为多试几次而消失）。

    敢重试的前提是调用方那一层入库天然幂等：scraper/job_link 都是靠 dedupe_key +
    job_exists 去重，同一条数据抓两次不会重复入库；调用方自己保证这一点，本函数
    不检查、也不知道"重放"具体做了什么。

    退避 = base_delay_s * 2**已重试次数 + 0~1秒随机抖动——抖动是为了避免同一次限流
    事件命中的多个并发调用（比如同一批 keyword×location 循环里连续几个组合都被
    限流）卡在完全相同的时间点上再次一起醒来撞墙。sleep 发生在 fn() 本身已经返回/
    抛出之后，如果 fn() 内部自己管理浏览器/连接生命周期（比如 scan_job_list 用
    `with sync_playwright()` 且在 finally 里关 context），退避时浏览器已经关掉了，
    不会占用资源空等。
    """
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            kind = classify(e)
            policy = RETRY_POLICY[kind]
            if not policy["retryable"] or attempt >= policy["max_retries"]:
                raise
            current = _retry_count_var.get()
            if current is not None:
                _retry_count_var.set(current + 1)
            delay = policy["base_delay_s"] * (2 ** attempt) + random.uniform(0, 1)
            logger.info(
                "%s failed (%s), retrying in %.1fs (attempt %d/%d): %s",
                getattr(fn, "__name__", fn), kind, delay, attempt + 1, policy["max_retries"], e,
            )
            time.sleep(delay)
            attempt += 1


# ---------------------------------------------------------------- wall-clock deadline（缺口⑥）

# 一次同步/扫描的整体墙钟上限。tracker/how_you_fit 同步靠 job_state 的
# start_tracker_sync()/start_how_you_fit_sync() 占一个进程内的"正在同步"标记（前端
# 靠它防止重复点击、GET 轮询靠它知道有没有结果），标记只有同步函数正常返回/抛出时
# 才会被清掉——如果同步本身没有任何时间上限（比如 scan_job_list 卡在一个奇怪的
# 页面状态无限滚动、或者 add_jobs_from_urls 因为访客页限流每条都在重试上耗时间），
# 这个标记会一直占着，用户在这之前发起的任何新同步请求都会一直撞 409。
DEFAULT_RUN_DEADLINE_SECONDS = 20 * 60

# 跟重试计数同一个理由用 ContextVar：调用链路太深，不逐层加 deadline 形参。
_deadline_var = contextvars.ContextVar("collect_run_deadline", default=None)


def start_run_deadline(seconds=DEFAULT_RUN_DEADLINE_SECONDS):
    """标记"从现在起最多再跑 seconds 秒"。在 sync_tracker_stage()/sync_search() 等
    一次运行的最外层调用一次，跑完（不管成功失败）要在 finally 里调
    clear_run_deadline() 收尾，避免残留状态影响同一线程后续的其它调用——虽然当前
    这几个调用点都是后台线程跑完即销毁，不清也不会串到别的请求上，但清理是更安全
    的习惯，不依赖"这个线程反正会死"这个实现细节。"""
    _deadline_var.set(time.time() + seconds)


def clear_run_deadline():
    _deadline_var.set(None)


def check_deadline(message="超过本次运行的时间上限"):
    """检查当前是否已经超过 start_run_deadline() 设的截止时间，超过就抛
    TimeoutError（classify() 已经认得 TimeoutError，会分类成 transient，落库时
    跟其它瞬时失败走同一条处置路径）。没有设过 deadline（比如脚本里手动调试单个
    函数）时什么都不做，不强制所有调用方都要先设 deadline 才能跑。"""
    deadline = _deadline_var.get()
    if deadline is not None and time.time() > deadline:
        raise TimeoutError(message)


# ---------------------------------------------------------------- 空结果健康检查（缺口⑦）

# 断崖判定的两个参数：比历史中位数低于这个比例、且绝对值本身也低于这个数，才判可疑
# ——单独一个条件都不够：比例低但绝对值本身还有几十条，大概率只是正常波动；绝对值
# 低但历史本来也一直很低（比如小众关键词），也不该被判成"看起来出问题了"。5 这个
# 绝对值floor不是精算出来的，是复用 linkedin_how_you_fit.MIN_DETERMINISTIC_JOB_IDS
# 已经在用的同一个阈值，保持全项目对"数量少到值得怀疑"的判断口径一致；不同 source
# 的正常量级差异很大（jobspy 一次几十条 vs 手动配置的单条 how-you-fit 搜索个位数
# 也正常），如果以后发现某个 source 用这个 floor 噪音太大，再单独调，不提前设计
# 成每个 source 各一套参数。
SUSPICIOUS_RATIO = 0.3
SUSPICIOUS_ABSOLUTE_FLOOR = 5
MIN_HISTORY_FOR_SUSPICION = 3


def is_suspicious_drop(current_found, history_found):
    """本次成功运行找到的数量，跟同一 source 最近几次成功运行比是不是断崖式下跌。
    用于发现"页面结构悄悄变了，选择器认不出内容，但代码没有报任何错"这类最难发现
    的故障——正常的限流/登录失效已经在 classify() 里有专门分类，这里管的是"看起来
    一切正常、没有异常，但数量不对劲"。

    历史数据不够（新 source，或者刚开始跑）没法判断，保守返回 False——不能因为
    没有历史基线就把第一次运行判成可疑，宁可漏判、不要制造噪音。"""
    if len(history_found) < MIN_HISTORY_FOR_SUSPICION:
        return False
    baseline = statistics.median(history_found)
    if baseline <= 0:
        return False
    return current_found < baseline * SUSPICIOUS_RATIO and current_found < SUSPICIOUS_ABSOLUTE_FLOOR


# ---------------------------------------------------------------- 故障现场取证（缺口⑧）

# 只在 structure 类失败（DOM 改版/agent判定卡住/被导航离开这类）时才值得留证据——
# 限流/登录失效原因已经很明确，截图帮不上排查；而 structure 恰恰是最费时间排查的
# 一类（这个项目历史上已经出过两次要靠人肉抓包/试验才能定位的真实故障：LinkedIn
# f_TPR 参数导致的空结果、"下一步"翻页按钮文案没识别到），复现一次要重新触发同步、
# 再等它卡住，代价是几分钟到几十分钟；提前留一份现场，代价只是几百 KB 磁盘。
DEBUG_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_snapshots")
MAX_DEBUG_SNAPSHOTS_PER_LABEL = 5


def save_debug_snapshot(label, page):
    """把 Playwright page 的当前 HTML + 截图存到
    debug_snapshots/<label>_<时间戳>/，按 label 滚动保留最近
    MAX_DEBUG_SNAPSHOTS_PER_LABEL 份。

    这里存的是"人肉排查用的现场快照"，跟 llm_calls/collect_runs 刻意不存 prompt/
    页面原文是同一个"按需取证、不默认全量留档"的取舍，区别只是这里的"按需"判断
    标准是"failure kind == structure"，不是"从不存"。

    全程 try/except——截图/写文件本身失败（磁盘满、page 已经被关掉等）最多是排查
    更麻烦，不该反过来变成故障处理流程里的新故障源，这条原则跟 llm.py 的埋点
    "观测绝不能成为新的故障源"一致。
    """
    try:
        # 微秒精度：短时间内触发多次快照（比如连续几个 agent 步骤都判定卡住）用秒级
        # 时间戳会导致目录名撞车、后一份悄悄覆盖前一份，反而丢了证据。
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_dir = os.path.join(DEBUG_SNAPSHOT_DIR, f"{label}_{ts}")
        os.makedirs(snapshot_dir, exist_ok=True)
        try:
            with open(os.path.join(snapshot_dir, "page.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            logger.exception("保存故障现场 HTML 失败")
        try:
            page.screenshot(path=os.path.join(snapshot_dir, "screenshot.png"), full_page=True)
        except Exception:
            logger.exception("保存故障现场截图失败")
        _prune_old_snapshots(label)
    except Exception:
        logger.exception("保存故障现场快照失败（不影响原本的故障处理）")


def _prune_old_snapshots(label):
    if not os.path.isdir(DEBUG_SNAPSHOT_DIR):
        return
    matching = sorted(
        (d for d in os.listdir(DEBUG_SNAPSHOT_DIR) if d.startswith(f"{label}_")),
        reverse=True,
    )
    for old in matching[MAX_DEBUG_SNAPSHOTS_PER_LABEL:]:
        try:
            shutil.rmtree(os.path.join(DEBUG_SNAPSHOT_DIR, old))
        except Exception:
            logger.exception("清理旧故障现场快照失败：%s", old)
