"""evals/run_analyzer_eval.py 的部分成功语义 + INCONCLUSIVE 闸门。全程手写假的
rep 字典（不走 collect_errors.with_retry，不发调用），只测 evaluate_pair /
evaluate_analyze_fixture / classify_repeat_failure 这几个纯判定函数。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evals"))

import analyzer
import llm
import verdicts as v
from run_analyzer_eval import classify_repeat_failure, evaluate_analyze_fixture, evaluate_pair


def ok(result):
    return {"ok": True, "result": result}


def bad(status, kind, msg="boom"):
    return {"ok": False, "error": msg, "failure_status": status, "failure_kind": kind}


def analyze_result(cognitive=0.3, content=0.5, overall=None, raw_cognitive=None, gap_count=1):
    return {
        "cognitive_match": cognitive, "content_match": content,
        "overall_match": overall if overall is not None else round((cognitive + content) / 2, 3),
        "raw_cognitive_match": raw_cognitive if raw_cognitive is not None else cognitive,
        "mandatory_gap_count": gap_count,
    }


# ---- 1. classify_repeat_failure：区分 FAIL（契约/JSON违反）和 ERROR（其它）
status, kind = classify_repeat_failure(analyzer.AnalysisContractError("缺字段 x"))
assert status == v.FAIL and kind == "contract", (status, kind)
status, kind = classify_repeat_failure(llm.LLMJsonError("bad json"))
assert status == v.FAIL and kind == "structure", (status, kind)
status, kind = classify_repeat_failure(TimeoutError("timed out"))
assert status == v.ERROR and kind == "transient", (status, kind)
status, kind = classify_repeat_failure(ValueError("随便什么"))
assert status == v.ERROR and kind == "bug", (status, kind)
print("classify_repeat_failure ok")

# ---- 2. evaluate_analyze_fixture：2次里1次网络抖动 -> 用幸存的1次判定，不是ERROR
fixture = {"max_cognitive_match": 0.5}
reps = [bad(v.ERROR, "transient", "IncompleteRead"), ok(analyze_result(cognitive=0.4, gap_count=1))]
status, detail = evaluate_analyze_fixture(reps, fixture)
assert status == v.PASS, (status, detail)
assert any("1/2" in d for d in detail), detail
print("evaluate_analyze_fixture: 1-of-2 transient survives on the other rep ok")

# ---- 3. 2次全挂（都是网络问题）-> ERROR，不是 FAIL
reps = [bad(v.ERROR, "transient"), bad(v.ERROR, "rate_limited")]
status, detail = evaluate_analyze_fixture(reps, fixture)
assert status == v.ERROR, (status, detail)
print("evaluate_analyze_fixture: 0-of-2 success -> ERROR ok")

# ---- 4. 一次契约错误（FAIL）+ 一次成功 -> 整体至少 FAIL，即使成功的那次本身没问题
reps = [bad(v.FAIL, "contract"), ok(analyze_result(cognitive=0.4, gap_count=1))]
status, detail = evaluate_analyze_fixture(reps, fixture)
assert status == v.FAIL, (status, detail)
print("evaluate_analyze_fixture: any FAIL rep drags the whole fixture to FAIL ok")

# ---- 5. evaluate_pair：任一臂 0 成功 -> ERROR，不是 FAIL（复现 2026-08-21 那次事故：
# 之前 IncompleteRead 网络抖动会被判成 FAIL）
pair_fixture = {"pair_with": "anchor", "compare_metric": "content_match", "min_margin_below": 0.10}
this_reps = [ok(analyze_result(content=0.6)), ok(analyze_result(content=0.55))]
anchor_reps_all_dead = [bad(v.ERROR, "transient"), bad(v.ERROR, "transient")]
status, detail = evaluate_pair(pair_fixture, this_reps, anchor_reps_all_dead)
assert status == v.ERROR, (status, detail)
print("evaluate_pair: dead anchor -> ERROR not FAIL (2026-08-21 regression) ok")

# ---- 6. evaluate_pair INCONCLUSIVE 闸门：锚点自身离散度(range) >= margin 时不能 PASS
anchor_reps_noisy = [ok(analyze_result(content=0.90)), ok(analyze_result(content=0.80))]  # range 0.10
this_reps_clean = [ok(analyze_result(content=0.60)), ok(analyze_result(content=0.60))]
status, detail = evaluate_pair(pair_fixture, this_reps_clean, anchor_reps_noisy)
assert status == v.INCONCLUSIVE, (status, detail)
print("evaluate_pair: anchor range >= margin -> INCONCLUSIVE not PASS ok")

# ---- 7. min_margin_below=0.0（纯方向性断言）永远不会被判 INCONCLUSIVE，即使离散度很大
direction_fixture = {"pair_with": "anchor", "compare_metric": "content_match", "min_margin_below": 0.0}
noisy_this = [ok(analyze_result(content=0.60)), ok(analyze_result(content=0.10))]
status, detail = evaluate_pair(direction_fixture, noisy_this, anchor_reps_noisy)
assert status != v.INCONCLUSIVE, (status, detail)
print("evaluate_pair: min_margin_below=0.0 never gated to INCONCLUSIVE ok")

# ---- 8. min_value_floor 是绝对断言，闸门触发之后（本该 INCONCLUSIVE）如果同时踩了
# floor，还是要 FAIL——floor 不受"测不出差异"这件事影响
floor_fixture = {
    "pair_with": "anchor", "compare_metric": "content_match",
    "min_margin_below": 0.10, "min_value_floor": 0.5,
}
this_below_floor = [ok(analyze_result(content=0.2)), ok(analyze_result(content=0.2))]
status, detail = evaluate_pair(floor_fixture, this_below_floor, anchor_reps_noisy)
assert status == v.FAIL, (status, detail)
assert any("下限" in d for d in detail), detail
print("evaluate_pair: min_value_floor still FAILs even when margin check is gated INCONCLUSIVE ok")

# ---- 9. --repeats 1 时 range 恒为 0，闸门天然不生效（不能让 --repeats 1 全员 INCONCLUSIVE）
single_this = [ok(analyze_result(content=0.60))]
single_anchor = [ok(analyze_result(content=0.90))]
status, detail = evaluate_pair(pair_fixture, single_this, single_anchor)
assert status == v.PASS, (status, detail)
print("evaluate_pair: repeats=1 (range=0) does not trigger the gate ok")

print("\nALL PASS")
