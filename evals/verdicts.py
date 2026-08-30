"""evals/run_analyzer_eval.py 用的状态词汇表和几个纯函数。抽成独立模块（仅标准库，
不 import 项目任何其它模块），好让 tests/test_eval_verdicts.py 能免费、快速地测这里
的判定逻辑，不用为了 import 它连带拖进 analyzer -> llm -> config，Step 1 之后还会
拖进 collect_errors -> playwright。

五个状态，一条严重度序：

    PASS         断言评估过，成立
    FAIL         断言评估过，被模型违反
    ERROR        断言没法评估：调用重试后仍然失败
    INCONCLUSIVE 断言没法评估：本次运行的噪声大于要断言的效应，或者压根没东西可查
    WARN         soft:True 的 fixture 上的 FAIL

exit code 刻意不是"只对 FAIL 敏感"：一次全员 ERROR 的运行如果 exit 0，整套 eval 会
在真正坏掉的时候悄悄一直"通过"。所以 1（有 FAIL）和 2（没 FAIL 但有 ERROR）分开——
"模型违反了规则"和"这次没测成"永远不能被混成同一个信号，这正是 2026-08-21 那次
`IncompleteRead` 网络抖动被记成规则违反的教训。
"""

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
INCONCLUSIVE = "INCONCLUSIVE"
WARN = "WARN"

STATUSES = (PASS, FAIL, ERROR, INCONCLUSIVE, WARN)

# 从最严重到最不严重。组合同一条 fixture 里的多个 check 时，取最严重的那个。
SEVERITY_ORDER = (ERROR, FAIL, INCONCLUSIVE, WARN, PASS)
_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}


def worse_of(a, b):
    """两个状态里更严重的那个（ERROR 最严重，PASS 最不严重）。"""
    return a if _RANK[a] <= _RANK[b] else b


def worst_of(statuses):
    """一组状态里最严重的那个；空输入返回 PASS（没有任何 check 就是没有任何问题）。"""
    statuses = list(statuses)
    if not statuses:
        return PASS
    out = statuses[0]
    for s in statuses[1:]:
        out = worse_of(out, s)
    return out


def apply_soft(status):
    """soft:True 的 fixture 只做一件事：FAIL 降级成 WARN。ERROR 不降级——一个软
    fixture 全部调用都失败，仍然是"这次没测成"，不是"弱信号"。INCONCLUSIVE/PASS
    原样返回。"""
    return WARN if status == FAIL else status


def summarize(values):
    """values 一组数字 -> {"n","mean","min","max","range"}，None 值先过滤掉。
    空输入返回 None（"这次没有数据"要能跟"summarize({0})"这种真实存在但是0的
    情况区分开，所以不用空 dict）。"""
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    lo, hi = min(values), max(values)
    return {
        "n": len(values),
        "mean": sum(values) / len(values),
        "min": lo,
        "max": hi,
        "range": hi - lo,
    }


def format_summary(s):
    """给控制台表格和 markdown 报告共用的人类可读格式。"""
    if s is None:
        return "（无数据）"
    return f"mean={s['mean']:.3f} ({s['min']:.2f}~{s['max']:.2f}, range={s['range']:.2f}, n={s['n']})"


EXIT_CLEAN = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def exit_code_for(rows):
    """rows 是一组 {"status": ...} 字典。有 FAIL 就是 1；没 FAIL 但有 ERROR 就是 2；
    否则（全是 PASS/INCONCLUSIVE/WARN）是 0。"""
    statuses = {r["status"] for r in rows}
    if FAIL in statuses:
        return EXIT_FAIL
    if ERROR in statuses:
        return EXIT_ERROR
    return EXIT_CLEAN
