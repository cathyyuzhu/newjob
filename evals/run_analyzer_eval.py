"""对 analyzer.py 打分器做真实 LLM 调用的质量回归套件。

跟 tests/ 下的回归测试完全分开、故意不接入 tests/run_all.py：这里每一次运行
都是真实调用 LLM API（不 mock），会产生真实费用，跑起来也慢（几条 fixture x
repeats 次调用，每次几十秒到一两分钟）。只测 analyzer.PROMPT_TEMPLATE /
MATERIALS_PROMPT 里写死的具体规则有没有被模型遵守（职级错配拖累、硬性门槛拖累、
偏好档案软信号、公司归属分类、公司简介不编造、定制简历不编造数字），不评判"这条
职位到底该打几分"——那没有客观答案。

用法（项目根目录下）：
    .venv/Scripts/python.exe evals/run_analyzer_eval.py
    .venv/Scripts/python.exe evals/run_analyzer_eval.py --provider deepseek --repeats 3
    .venv/Scripts/python.exe evals/run_analyzer_eval.py --only hard_gap_single_mandatory --repeats 1
    .venv/Scripts/python.exe evals/run_analyzer_eval.py --yes   # 跳过运行前的确认提示

## 状态词汇（见 evals/verdicts.py）

    PASS          断言评估过，成立
    FAIL          断言评估过，被模型违反                       -> exit 1
    ERROR         断言没法评估：调用重试后仍然失败               -> exit 2（没有 FAIL 时）
    INCONCLUSIVE  断言没法评估：本次噪声大于要断言的效应，或没东西可查
    WARN          soft:True 的 fixture 上的 FAIL

exit code 1（有 FAIL）和 2（没 FAIL 但有 ERROR）刻意分开：**"模型违反了规则"和
"这次没测成"永远不能被混同**——2026-08-21 那次报告里，一次 IncompleteRead 网络抖动
被直接记成 FAIL，是这次重构要修的最大的坑。

## 花钱的事——每次运行前会先打印一条真实调用提示并要求确认（--yes 跳过）

## 质性评审（--judge，可选）

规则断言只能查"有没有遵守明文规则"，查不了"写得好不好"。--judge 会在规则断言
跑完后，把每条可评审 fixture（analyze/materials）的第 1 次成功输出连同 JD/简历
原文交给另一个模型按维度打分（1-5）并列关注点，机制见 evals/judge.py。judge 默认
**跨厂商**选模型（被评是 anthropic 就用 deepseek，反之亦然）以避免自评偏差，缺
另一家 key 时退回同厂商默认模型。结果是纯信息性的：评审失败/分数低都不影响
fixture 的规则判定和 exit code，只进报告和 latest.json 供人读。

## 产出两份东西

1. 每条 fixture 完整的原始 LLM 输出写进 evals/reports/<timestamp>.md（已加入
   .gitignore），供人工抽查分数背后的理由是否合理——脚本只能验证"有没有遵守
   写死的规则"，打分本身"准不准"仍然需要人看。
2. 结构化结果覆盖写入 evals/results/latest.json（**已提交进 git**，不在
   .gitignore 里）——只存状态/阈值/统计摘要，不存模型原文，所以 `git diff` 能
   直接看出某条 fixture 从上一次 prompt 改动到这一次状态有没有变化。指标统一
   round 到 3 位小数，减少纯采样噪声造成的 diff。

## 简化说明（相对最初设计的取舍）

JSON 里目前是"每条 fixture 一个 status + 一份 detail 文本 + 若干 metrics 摘要"，
没有做成"每个子断言一条 {name,status,threshold,observed}"这么细的结构化 schema——
这个项目里没有第二段代码会消费那份结构，先做出来只是为了好看，属于过度设计。
detail 里已经用"check: xxx"这种前缀标出了不同子断言，需要更细的机器可读结构时
再加，不提前做。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime

# 本进程的 stdout 在 Windows 上默认是 GBK，直接跑（不带 `-X utf8`）会把中文输出
# 成乱码——跟 tests/run_all.py 同一个坑，同一个修法。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import analyzer  # noqa: E402
import collect_errors  # noqa: E402
import config  # noqa: E402
import judge  # noqa: E402
import llm  # noqa: E402
import verdicts as v  # noqa: E402
from checks_fabrication import allowed_figures, find_new_figures  # noqa: E402
from fixtures_analyzer import FIXTURES, FIXTURES_BY_ID  # noqa: E402

REPORTS_DIR = os.path.join(BASE, "reports")
RESULTS_DIR = os.path.join(BASE, "results")
RESULTS_PATH = os.path.join(RESULTS_DIR, "latest.json")


# ---------------------------------------------------------------- 单次调用 + 重试分类

def classify_repeat_failure(exc):
    """一次 repeat 失败后，判定该记 FAIL 还是 ERROR，并带上更细的 kind 供报告展示。

    FAIL：模型确实给出了回复，但不遵守输出契约——不是合法 JSON（llm.LLMJsonError），
    或结构/取值范围不对（analyzer.AnalysisContractError）。这正是 eval 要测的东西。
    ERROR：调用本身没能拿到一个可判定的结果——网络抖动/欠费/限流/未知代码问题，
    复用 collect_errors.classify() 给个更细的 kind（transient/rate_limited/...）。"""
    if isinstance(exc, analyzer.AnalysisContractError):
        return v.FAIL, "contract"
    if isinstance(exc, llm.LLMJsonError):
        return v.FAIL, "structure"
    return v.ERROR, collect_errors.classify(exc)


def _call_with_retry(fn, *args, **kwargs):
    """跑一次 repeat。fn/args/kwargs 包在 collect_errors.with_retry 里——只对
    transient/rate_limited 自动退避重试，其它失败（包括上面两种 FAIL 类型）第一次
    就直接向上抛，不浪费重试次数在没有意义的地方。"""
    try:
        result = collect_errors.with_retry(fn, *args, **kwargs)
        return {"ok": True, "result": result}
    except Exception as e:
        status, kind = classify_repeat_failure(e)
        return {"ok": False, "error": str(e), "failure_status": status, "failure_kind": kind}


def _split_reps(reps):
    ok = [r for r in reps if r.get("ok")]
    bad = [r for r in reps if not r.get("ok")]
    return ok, bad


def _partial_fail_baseline(bad_reps):
    """只在"至少有一次调用成功"的前提下调用（0 成功的情况在各 evaluate_* 里已经
    单独 `return v.ERROR` 了，不会走到这里）。

    失败的 repeats 里只要有一条被判 FAIL（模型确实返回了东西、但违反了输出契约），
    就要把这个 FAIL 带进最终判定——即使别的 repeats 都成功也不能被稀释掉，模型
    在这次运行里确实违反过一次规则是事实。

    但纯 ERROR（网络抖动/限流之类）**不能**把"至少测出来一次"的结果拖成 ERROR
    ——那是 0 成功时才该给的判定。ERROR-only 的失败只在 detail 里留一笔说明，
    基线仍然是 PASS，让调用方基于成功的 repeats 正常评估规则。"""
    if any(r["failure_status"] == v.FAIL for r in bad_reps):
        return v.FAIL
    return v.PASS


def _failure_detail(bad_reps, n_total, label=""):
    kinds = "；".join(f"{r.get('failure_kind')}: {r['error'][:120]}" for r in bad_reps)
    prefix = f"{label}：" if label else ""
    return f"{prefix}{len(bad_reps)}/{n_total} 次调用未成功（{kinds}）"


# ---------------------------------------------------------------- 各 kind 的 runner

def run_fixture_analyze(fixture, provider, model, repeats):
    reps = []
    for i in range(repeats):
        print(f"    第{i + 1}/{repeats}次 ...")
        reps.append(_call_with_retry(
            analyzer.analyze_job,
            company=fixture["company"], title=fixture["title"], jd_text=fixture["jd_text"],
            resume_text=fixture["resume_text"], model=model, provider=provider,
            preference_profile_text=fixture.get("preference_profile_text"),
        ))
    return reps


def run_fixture_classify(fixture, provider, model, repeats):
    reps = []
    companies = list(fixture["companies"].keys())
    for i in range(repeats):
        print(f"    第{i + 1}/{repeats}次 ...")
        reps.append(_call_with_retry(analyzer.classify_companies, companies, model=model, provider=provider))
    return reps


def run_fixture_materials(fixture, provider, model, repeats):
    """跟 run_fixture_analyze 的区别：包一层 llm.task_context()，把这次调用标成
    "materials" 功能位——不然 main() 里 resolve_task(cfg,"analysis") 设过的
    ContextVar 会一直是 "analysis"，llm_calls 埋点和未来的 materials 采样温度都会
    被错误归因。"""
    reps = []
    task = fixture.get("task", "materials")
    for i in range(repeats):
        print(f"    第{i + 1}/{repeats}次 ...")
        with llm.task_context(task):
            reps.append(_call_with_retry(
                analyzer.generate_materials,
                company=fixture["company"], title=fixture["title"], jd_text=fixture["jd_text"],
                resume_text=fixture["resume_text"], analysis_context=fixture.get("analysis_context", ""),
                model=model, provider=provider,
            ))
    return reps


KIND_RUNNERS = {
    "analyze": run_fixture_analyze,
    "overview_honesty": run_fixture_analyze,
    "classify_companies": run_fixture_classify,
    "materials": run_fixture_materials,
}


# ---------------------------------------------------------------- 各 kind 的 evaluator

def evaluate_analyze_fixture(reps, fixture):
    ok_reps, bad_reps = _split_reps(reps)
    detail = []
    if bad_reps:
        detail.append(_failure_detail(bad_reps, len(reps)))
    if not ok_reps:
        return v.ERROR, detail
    status = _partial_fail_baseline(bad_reps)

    values = [r["result"] for r in ok_reps]
    for key in ("cognitive_match", "content_match", "overall_match", "raw_cognitive_match"):
        s = v.summarize([vv.get(key) for vv in values])
        if s:
            detail.append(f"{key}: {v.format_summary(s)}")

    cap = fixture.get("max_cognitive_match")
    if cap is not None:
        # 查 raw_cognitive_match（模型**原始**输出），不查 cognitive_match。
        # analyzer.apply_score_rules 会在 Python 侧强制封顶，再查封顶后的值必然
        # 通过，这条断言就从"检测 prompt 漂移"退化成了永远绿的摆设。
        raws = [float(vv.get("raw_cognitive_match", vv["cognitive_match"])) for vv in values]
        over = [round(x, 3) for x in raws if x > cap]
        if over:
            status = v.worse_of(status, v.FAIL)
            detail.append(f"模型自己给的 cognitive_match 超过上限 {cap}：{over}（代码侧已封顶，但说明 prompt 没被遵守）")

        # 封顶能不能生效，取决于 mandatory_evidence 回 JD 原文核验的通过率。通过率
        # 低就说明模型在转述而不是照抄，规则形同虚设——check 名单独标出来，跟真正的
        # 封顶突破（上面那条）区分开，都算 FAIL 但原因不同。
        gaps = [int(vv.get("mandatory_gap_count", 0)) for vv in values]
        detail.append(f"核验通过的硬缺口条数={gaps}")
        if not any(gaps):
            status = v.worse_of(status, v.FAIL)
            detail.append("check: mandatory_evidence_unverified —— 没有任何一条强制性要求通过 JD 原文核验，封顶规则完全没触发（模型在转述而非照抄）")

    return status, detail


def evaluate_pair(fixture, this_reps, anchor_reps):
    this_ok, this_bad = _split_reps(this_reps)
    anchor_ok, anchor_bad = _split_reps(anchor_reps)
    detail = []
    if this_bad:
        detail.append(_failure_detail(this_bad, len(this_reps), "本条"))
    if anchor_bad:
        detail.append(_failure_detail(anchor_bad, len(anchor_reps), f"锚点({fixture['pair_with']})"))
    if not this_ok or not anchor_ok:
        return v.ERROR, detail

    metric = fixture["compare_metric"]
    this_s = v.summarize([r["result"].get(metric) for r in this_ok])
    anchor_s = v.summarize([r["result"].get(metric) for r in anchor_ok])
    detail.append(f"{metric}: 本条 {v.format_summary(this_s)}；锚点 {v.format_summary(anchor_s)}")

    status = _partial_fail_baseline(this_bad + anchor_bad)

    margin = fixture.get("min_margin_below")
    if margin is not None:
        # INCONCLUSIVE 闸门只在断言"至少要差多少"（margin>0）时生效——margin=0
        # 是纯方向性断言（"不应该比锚点高"），套闸门会让这类断言永远测不出结果。
        if margin > 0:
            worst_range = max(this_s["range"], anchor_s["range"])
            # 浮点减法算出来的 range（比如 0.9-0.8）可能比数学上的 0.1 略小一丁点，
            # 卡在边界值上会被误判成"离散度够小"——加一个远小于任何真实 margin 的
            # 容差，只吸收浮点误差，不影响正常的判定。
            if worst_range >= margin - 1e-9:
                status = v.worse_of(status, v.INCONCLUSIVE)
                detail.append(
                    f"本次运行离散度(range={worst_range:.3f})不小于要断言的 margin({margin})，"
                    f"无法判断这次差异是真实效应还是抽样噪声 → INCONCLUSIVE"
                )
            else:
                if (anchor_s["mean"] - this_s["mean"]) < margin:
                    status = v.worse_of(status, v.FAIL)
                    detail.append(f"差值 {anchor_s['mean'] - this_s['mean']:.3f} 小于要求的最小差距 {margin}")
        else:
            if (anchor_s["mean"] - this_s["mean"]) < margin:
                status = v.worse_of(status, v.FAIL)
                detail.append(f"差值 {anchor_s['mean'] - this_s['mean']:.3f} 小于要求的最小差距 {margin}")

    # min_value_floor / max_overall_match 是绝对断言，不受上面 INCONCLUSIVE 闸门影响
    # ——闸门只管"能不能测出差异"，测不出差异不代表"分数没有被一票否决"这类硬底线
    # 也测不出来。
    floor = fixture.get("min_value_floor")
    if floor is not None and this_s["mean"] < floor:
        status = v.worse_of(status, v.FAIL)
        detail.append(f"均值 {this_s['mean']:.3f} 低于下限 {floor}（疑似被一票否决）")

    cap = fixture.get("max_overall_match")
    if cap is not None:
        overall_s = v.summarize([r["result"].get("overall_match") for r in this_ok])
        if overall_s and overall_s["mean"] >= cap:
            status = v.worse_of(status, v.FAIL)
            detail.append(f"overall_match 均值 {overall_s['mean']:.3f} 未低于 {cap}")

    return status, detail


def evaluate_classify_fixture(fixture, reps):
    ok_reps, bad_reps = _split_reps(reps)
    detail = []
    if bad_reps:
        detail.append(_failure_detail(bad_reps, len(reps)))
    if not ok_reps:
        return v.ERROR, detail
    status = _partial_fail_baseline(bad_reps)

    for company, expected in fixture["companies"].items():
        got = [r["result"].get(company) for r in ok_reps]
        if any(g != expected for g in got):
            status = v.worse_of(status, v.FAIL)
            detail.append(f"{company}：期望 {expected}，实际 {got}")
    if status == v.PASS:
        detail.append(f"{len(fixture['companies'])} 家公司在 {len(ok_reps)} 次调用里全部判断正确")
    return status, detail


def evaluate_overview_honesty(fixture, reps):
    ok_reps, bad_reps = _split_reps(reps)
    phrases = fixture["expect_phrases"]
    detail = []
    if bad_reps:
        detail.append(_failure_detail(bad_reps, len(reps)))
    if not ok_reps:
        return v.ERROR, detail
    status = _partial_fail_baseline(bad_reps)

    any_fabricated = False
    for i, r in enumerate(ok_reps):
        overview = r["result"].get("company_overview") or ""
        hit = any(p in overview for p in phrases)
        if not hit:
            any_fabricated = True
        detail.append(f"第{i + 1}次：{'如实说明信息不足' if hit else '疑似编造'} —— {overview[:100]}")
    if any_fabricated:
        status = v.worse_of(status, v.FAIL)
    return status, detail


def evaluate_materials(fixture, reps):
    """定制简历改写 + cover letter 是否编造简历里不存在的数字。见
    evals/checks_fabrication.py 的说明；许可集取整份简历，不是被改的那一段。

    三项检查，严重度不同：
    1. 空转守卫：所有成功调用都没产出任何改写内容 -> INCONCLUSIVE（没东西可查，
       不是 PASS——一个永远空转的 fixture 会被误读成"从没编造过"）。
    2/3. 简历改写、cover letter 各自的编造数字检查（分开判断——cover letter 是
       散文，误报率天然更高，且 MATERIALS_PROMPT 的诚实条款只约束简历改动，
       不该跟简历编造混成一条）：FAIL，但这条 fixture 通常是 soft:True，main()
       会把 FAIL 降级成 WARN。
    4. resume_paragraph_edits_dropped（annotate_edits 静默丢弃的条目）：硬 FAIL，
       不受 soft 降级影响——这是确定性检查，不是启发式，模型指错段落是真实的
       契约违反，不该因为编造检查还在校准误报率就被一起软化掉。
    """
    ok_reps, bad_reps = _split_reps(reps)
    detail = []
    if bad_reps:
        detail.append(_failure_detail(bad_reps, len(reps)))
    if not ok_reps:
        return v.ERROR, detail
    status = _partial_fail_baseline(bad_reps)

    any_dropped = False
    for r in ok_reps:
        dropped = r["result"].get("resume_paragraph_edits_dropped") or []
        if dropped:
            any_dropped = True
            detail.append(f"check: dropped_edits（硬性，不受 soft 影响）—— 1次调用有 {len(dropped)} 条改写建议被静默丢弃：{dropped}")
    if any_dropped:
        status = v.worse_of(status, v.FAIL)

    any_edits = any(r["result"].get("resume_paragraph_edits") for r in ok_reps)
    if not any_edits:
        detail.append("check: vacuity_guard —— 所有成功调用都没有产出任何段落改写（needs_customization=false 或 edits 为空），无法检验是否编造")
        return v.worse_of(status, v.INCONCLUSIVE), detail

    allowed = allowed_figures(fixture["resume_text"])
    resume_fabrications = []
    letter_fabrications = []
    for r in ok_reps:
        for edit in r["result"].get("resume_paragraph_edits") or []:
            new_figs = find_new_figures(edit.get("text") or "", allowed)
            if new_figs:
                resume_fabrications.append({"index": edit.get("index"), "figures": new_figs})
        letter_figs = find_new_figures(r["result"].get("cover_letter") or "", allowed)
        if letter_figs:
            letter_fabrications.append(letter_figs)

    if resume_fabrications:
        status = v.worse_of(status, v.FAIL)
        detail.append(f"check: resume_fabrication —— 简历改写引入了简历里不存在的数字：{resume_fabrications}")
    if letter_fabrications:
        status = v.worse_of(status, v.FAIL)
        detail.append(f"check: cover_letter_fabrication（跟简历改写分开判断）—— cover_letter 引入了简历里不存在的数字：{letter_fabrications}")
    if not resume_fabrications and not letter_fabrications and not any_dropped:
        detail.append("简历改写与 cover letter 均未引入简历外的数字")

    return status, detail


KIND_EVALUATORS = {
    "classify_companies": evaluate_classify_fixture,
    "overview_honesty": evaluate_overview_honesty,
    "materials": evaluate_materials,
    # "analyze" 处理起来要区分"有没有 pair_with"，在 main() 的循环里单独分派，
    # 不放进这张表——放进来反而要在这里重新判断一次 pair_with，两处判断容易漂移。
}


# ---------------------------------------------------------------- 质性评审（--judge，信息性）

def run_judge_phase(fixtures, reps_by_id, provider, model, cfg, judge_model_arg):
    """对可评审 kind 的 fixture 各取第 1 次成功输出，交给 judge 模型打质性分。

    只产信息性结果：judge 调用失败/返回不合规都记成 status=ERROR，不影响 fixture
    的规则判定和 exit code。评审单次成功输出而不是全部 repeats 是成本取舍——规则
    层面的噪声过滤已经由 repeats+离散度闸门做了，judge 只做人工报告的预读。"""
    judge_provider, judge_model = judge.resolve_judge_target(provider, judge_model_arg, cfg)
    if judge_provider == provider and not judge_model_arg:
        print(f"（judge 退回同厂商模型 {provider}，存在自评偏差的可能；"
              f"补另一家 API key 或用 --judge-model 可避免）")
    print(f"质性评审 judge：provider={judge_provider}, model={judge_model or '(默认)'}")
    judge_by_id = {}
    for f in fixtures:
        if f["kind"] not in judge.JUDGE_KINDS:
            continue
        ok_reps = [r for r in reps_by_id.get(f["id"], []) if r.get("ok")]
        if not ok_reps:
            continue
        print(f"  judge {f['id']} ...")
        try:
            verdict = collect_errors.with_retry(
                judge.judge_output, f["kind"],
                company=f["company"], title=f["title"], jd_text=f["jd_text"],
                resume_text=f["resume_text"], output=ok_reps[0]["result"],
                provider=judge_provider, model=judge_model,
            )
            judge_by_id[f["id"]] = {"provider": judge_provider, "model": judge_model, **verdict}
        except Exception as e:
            judge_by_id[f["id"]] = {
                "provider": judge_provider, "model": judge_model,
                "status": "ERROR", "error": str(e)[:200],
            }
    return judge_by_id


# ---------------------------------------------------------------- 报告输出

STATUS_MARK = {v.PASS: "  ", v.FAIL: "✗ ", v.ERROR: "⚠ ", v.INCONCLUSIVE: "? ", v.WARN: "~ "}


def write_report(path, provider, model, repeats, rows, raw_by_fixture, retries, judge_by_id=None):
    lines = [
        "# analyzer.py 打分器 LLM 质量回归报告",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- provider={provider} model={model or '(默认)'} repeats={repeats}",
        f"- 本次运行触发的自动重试次数：{retries}（transient/rate_limited 才会重试，见 collect_errors.py）",
        "",
        "## 状态图例",
        "",
        "PASS 测过且成立 / FAIL 测过且被违反 / **ERROR 没测成（调用失败，不代表模型有问题）** / "
        "**INCONCLUSIVE 没测成（本次噪声盖过了要断言的效应）** / WARN soft fixture 上的 FAIL",
        "",
        "## 汇总",
        "",
        "| fixture | 结果 | 规则 |",
        "|---|---|---|",
    ]
    for row in rows:
        raw_note = f"（raw={row['raw_status']}）" if row["raw_status"] != row["status"] else ""
        lines.append(f"| {row['id']} | {row['status']}{raw_note} | {row['rule']} |")
    lines.append("")
    lines.append("## 详情（含原始 LLM 输出，供人工抽查分数背后的理由是否合理）")
    for row in rows:
        lines.append("")
        lines.append(f"### {row['id']}（{row['status']}）")
        lines.append(f"规则：{row['rule']}")
        lines.append("")
        for d in row["detail"]:
            lines.append(f"- {d}")
        raw = raw_by_fixture.get(row["id"])
        if raw:
            lines.append("")
            lines.append("<details><summary>原始输出</summary>")
            lines.append("")
            lines.append("```")
            for i, rep in enumerate(raw):
                lines.append(f"-- 第{i + 1}次 --")
                lines.append(str(rep))
            lines.append("```")
            lines.append("</details>")

    judged = judge_by_id or {}
    if judged:
        lines.append("")
        lines.append("## 质性评审（LLM judge，信息性结果，不影响 exit code）")
        lines.append("")
        for rid, j in judged.items():
            if j.get("status") == "ERROR":
                lines.append(f"- {rid}：评审失败（{j.get('error')}）")
                continue
            dims = "；".join(f"{name}={d['score']}" for name, d in (j.get("dimensions") or {}).items())
            lines.append(f"- {rid}：overall={j.get('overall')}（{dims}）")
            for c in j.get("concerns") or []:
                lines.append(f"    - {c}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _round_metrics(obj):
    """把结果里所有 float 都 round 到 3 位小数再写 JSON——采样噪声本身在小数点
    后第 3~4 位晃，不 round 的话 latest.json 每次运行都会因为纯噪声产生 diff，
    真正的 status/threshold 变化反而淹没在里面。"""
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, dict):
        return {k: _round_metrics(x) for k, x in obj.items()}
    if isinstance(obj, list):
        return [_round_metrics(x) for x in obj]
    return obj


def _prompt_fingerprints():
    return {
        "PROMPT_TEMPLATE": hashlib.sha256(analyzer.PROMPT_TEMPLATE.encode("utf-8")).hexdigest()[:12],
        "MATERIALS_PROMPT": hashlib.sha256(analyzer.MATERIALS_PROMPT.encode("utf-8")).hexdigest()[:12],
        "COMPANY_ORIGIN_PROMPT": hashlib.sha256(analyzer.COMPANY_ORIGIN_PROMPT.encode("utf-8")).hexdigest()[:12],
    }


def _git_rev():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def load_results_json(path=RESULTS_PATH):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_results_json(path, run_meta, rows):
    """结构化结果，覆盖写单个文件（不带时间戳）——git log 就是历史，git diff 就是
    "这条 fixture 从上次改动到这次变了什么"，比翻散文 markdown 快得多。刻意不含
    模型原文，那份留在 evals/reports/（.gitignore 掉的）里。"""
    fixtures_json = {}
    for row in rows:
        entry = {
            "kind": row["kind"],
            "rule": row["rule"],
            "soft": row["soft"],
            "status": row["status"],
            "raw_status": row["raw_status"],
            "n_ok": row["n_ok"],
            "n_error": row["n_error"],
            "detail": row["detail"],
        }
        # judge 结果是附加字段：没开 --judge 的历史 latest.json 没有它，schema 兼容。
        if row.get("judge"):
            entry["judge"] = row["judge"]
        fixtures_json[row["id"]] = entry
    payload = {
        "schema": 1,
        "run": run_meta,
        "fixtures": fixtures_json,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(_round_metrics(payload), f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def print_freshness_banner(previous):
    """开始花钱之前，比对上一份 committed 结果的 prompt 指纹，跟当前代码是否一致。
    不一致就说明上一份结果是对着一个已经变了的 prompt 跑的，早点提醒，免得看着
    一份"全绿"的报告却不知道它测的是旧版本。"""
    if not previous:
        print("（没有找到上一次的 evals/results/latest.json，这是第一次运行）")
        return
    current_fp = _prompt_fingerprints()
    prev_fp = (previous.get("run") or {}).get("prompt_fingerprints") or {}
    changed = [name for name, fp in current_fp.items() if prev_fp.get(name) != fp]
    if changed:
        print("=" * 70)
        print(f"⚠ 上一次记录的结果（{(previous.get('run') or {}).get('finished_at', '未知时间')}）"
              f"是针对以下 prompt 跑的，当前代码已经不一样了，那份结果对当前 prompt 已经失效：")
        for name in changed:
            print(f"    {name}: {prev_fp.get(name)} -> {current_fp.get(name)}")
        print("=" * 70)
    else:
        print(f"（上一次结果的 prompt 指纹跟当前代码一致，跑于 {(previous.get('run') or {}).get('finished_at', '未知时间')}）")


def resolve_fixture_set(only_id):
    if only_id not in FIXTURES_BY_ID:
        return None
    needed = {only_id}
    fixture = FIXTURES_BY_ID[only_id]
    if fixture.get("pair_with"):
        needed.add(fixture["pair_with"])
    for other in FIXTURES:
        if other.get("pair_with") == only_id:
            needed.add(other["id"])
    return [f for f in FIXTURES if f["id"] in needed]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default=None, help="anthropic 或 deepseek，不传则用 config.json 里 analysis 功能位当前配置的 provider")
    parser.add_argument("--model", default=None, help="模型 id，不传则用 config.json 里 analysis 功能位当前配置的模型")
    parser.add_argument("--repeats", type=int, default=2, help="每条 fixture 重复调用几次，用来过滤单次抽样的随机噪声（默认2）")
    parser.add_argument("--only", default=None, help="只跑某一条 fixture 及其配对/锚点（调试用，配合 --repeats 1 少花钱）")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过运行前的真实调用确认提示")
    parser.add_argument("--judge", action="store_true",
                        help="规则断言跑完后，用另一个模型对质性质量（分析理由/cover letter/简历改写）"
                             "做 LLM 评审——额外花钱，只产信息性结果，不影响 exit code")
    parser.add_argument("--judge-model", default=None,
                        help="judge 用的模型 id；默认跨厂商（被评是 anthropic 就用 deepseek，反之亦然），"
                             "缺另一家 key 时退回同厂商默认模型")
    args = parser.parse_args()

    previous = load_results_json()
    print_freshness_banner(previous)

    cfg = config.load_config()
    default_provider, default_model = llm.resolve_task(cfg, "analysis")
    provider = args.provider or default_provider
    model = args.model or default_model

    fixtures = FIXTURES
    if args.only:
        fixtures = resolve_fixture_set(args.only)
        if fixtures is None:
            print(f"没有这条 fixture：{args.only}（可选：{', '.join(FIXTURES_BY_ID)}）")
            return 1

    spread_measurable = args.repeats >= 2
    if not spread_measurable:
        print(f"（--repeats={args.repeats} < 2：离散度算不出来，涉及 min_margin_below 的成对断言会跳过 INCONCLUSIVE 闸门直接判定，噪声风险自己承担）")

    call_count = len(fixtures) * args.repeats
    print(f"即将真实调用 LLM（provider={provider}, model={model or '默认'}），"
          f"共 {len(fixtures)} 条 fixture × {args.repeats} 次 = 约 {call_count} 次调用，会产生真实 API 费用。")
    if args.judge:
        n_judgeable = sum(1 for f in fixtures if f["kind"] in judge.JUDGE_KINDS)
        if n_judgeable:
            print(f"质性评审（--judge）：额外对 {n_judgeable} 条 fixture 的第 1 次成功输出各做 1 次 LLM 评审"
                  f"（约 {n_judgeable} 次调用）。")
    if not args.yes:
        answer = input("确认继续吗？输入 yes 继续，其它任意键取消：")
        if answer.strip().lower() != "yes":
            print("已取消。")
            return 1

    collect_errors.reset_retry_count()

    reps_by_id = {}
    raw_by_fixture = {}
    for f in fixtures:
        print(f"跑 {f['id']} ...")
        runner = KIND_RUNNERS.get(f["kind"], run_fixture_analyze)
        reps = runner(f, provider, model, args.repeats)
        reps_by_id[f["id"]] = reps
        raw_by_fixture[f["id"]] = [r.get("result") if r.get("ok") else r.get("error") for r in reps]

    rows = []
    for f in fixtures:
        reps = reps_by_id[f["id"]]
        if f["kind"] == "analyze" and f.get("pair_with"):
            anchor_reps = reps_by_id.get(f["pair_with"])
            if anchor_reps is None:
                raw_status, detail = v.ERROR, [f"缺少锚点 fixture {f['pair_with']} 的结果（用 --only 时忘了带上？）"]
            else:
                raw_status, detail = evaluate_pair(f, reps, anchor_reps)
        elif f["kind"] == "analyze":
            raw_status, detail = evaluate_analyze_fixture(reps, f)
        else:
            evaluator = KIND_EVALUATORS[f["kind"]]
            raw_status, detail = evaluator(f, reps)

        soft = bool(f.get("soft"))
        status = v.apply_soft(raw_status) if soft else raw_status
        ok_reps, bad_reps = _split_reps(reps)
        rows.append({
            "id": f["id"], "kind": f["kind"], "rule": f["rule"], "soft": soft,
            "status": status, "raw_status": raw_status, "detail": detail,
            "n_ok": len(ok_reps), "n_error": len(bad_reps),
        })

    print("\n" + "=" * 70)
    print(f"{'fixture':<32} {'结果':<14} 规则")
    print("=" * 70)
    for row in rows:
        mark = STATUS_MARK.get(row["status"], "")
        print(f"{mark}{row['id']:<30} {row['status']:<14} {row['rule']}")
        for d in row["detail"]:
            print(f"    - {d}")
    print("=" * 70)

    judge_by_id = {}
    if args.judge:
        judge_by_id = run_judge_phase(fixtures, reps_by_id, provider, model, cfg, args.judge_model)
        for row in rows:
            if row["id"] in judge_by_id:
                row["judge"] = judge_by_id[row["id"]]

    reports_dir = REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, datetime.now().strftime("%Y%m%d-%H%M%S") + ".md")
    retries = collect_errors.get_retry_count()
    write_report(report_path, provider, model, args.repeats, rows, raw_by_fixture, retries, judge_by_id=judge_by_id)
    print(f"完整报告（含原始 LLM 输出）：{report_path}")

    run_meta = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
        "repeats": args.repeats,
        "fixture_set": f"only:{args.only}" if args.only else "all",
        "retries": retries,
        "spread_measurable": spread_measurable,
        "prompt_fingerprints": _prompt_fingerprints(),
        "git_rev": _git_rev(),
    }
    if args.judge and judge_by_id:
        first = next(iter(judge_by_id.values()))
        run_meta["judge"] = {"provider": first.get("provider"), "model": first.get("model")}
    write_results_json(RESULTS_PATH, run_meta, rows)
    print(f"结构化结果（已提交进 git，可 diff）：{RESULTS_PATH}")

    exit_code = v.exit_code_for(rows)
    if exit_code == v.EXIT_FAIL:
        print("\n存在 FAIL：以上规则至少有一条没被 LLM 遵守，详见上表。")
    elif exit_code == v.EXIT_ERROR:
        print("\n没有 FAIL，但存在 ERROR：至少一条 fixture 没能跑成（调用失败/重试耗尽），不代表模型有问题，但这次没有真正测到它。")
    warns = [row for row in rows if row["status"] == v.WARN]
    inconclusive = [row for row in rows if row["status"] == v.INCONCLUSIVE]
    if warns:
        print(f"{len(warns)} 条 WARN（soft fixture 上的 FAIL，不影响 exit code，建议人工看一眼报告）。")
    if inconclusive:
        print(f"{len(inconclusive)} 条 INCONCLUSIVE（没测出结果，不影响 exit code，多是离散度太大或没东西可查）。")
    if exit_code == v.EXIT_CLEAN:
        print("\nALL PASS" if not (warns or inconclusive) else "\nALL PASS（含 WARN/INCONCLUSIVE，见上）")
    if judge_by_id:
        judged_ok = [j for j in judge_by_id.values() if j.get("status") != "ERROR"]
        judge_errs = [j for j in judge_by_id.values() if j.get("status") == "ERROR"]
        with_concerns = [j for j in judged_ok if j.get("concerns")]
        note = f"质性评审（不影响 exit code）：{len(judged_ok)} 条已评审"
        if judged_ok:
            note += f"，overall 均值 {sum(j['overall'] for j in judged_ok) / len(judged_ok):.1f}"
        if with_concerns:
            note += f"，{len(with_concerns)} 条有关注点（详见报告）"
        if judge_errs:
            note += f"，{len(judge_errs)} 条评审失败"
        print(note)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
