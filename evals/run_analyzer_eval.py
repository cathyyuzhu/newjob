"""对 analyzer.py 打分器做真实 LLM 调用的质量回归套件。

跟 tests/ 下的回归测试完全分开、故意不接入 tests/run_all.py：这里每一次运行
都是真实调用 LLM API（不 mock），会产生真实费用，跑起来也慢（几条 fixture x
repeats 次调用，每次几十秒到一两分钟）。只测 analyzer.PROMPT_TEMPLATE 里写死的
具体规则有没有被模型遵守（职级错配拖累、硬性门槛拖累、偏好档案软信号、公司归属
分类、公司简介不编造），不评判"这条职位到底该打几分"——那没有客观答案。

用法（项目根目录下）：
    .venv/Scripts/python.exe evals/run_analyzer_eval.py
    .venv/Scripts/python.exe evals/run_analyzer_eval.py --provider deepseek --repeats 3
    .venv/Scripts/python.exe evals/run_analyzer_eval.py --only hard_gap_single_mandatory --repeats 1
    .venv/Scripts/python.exe evals/run_analyzer_eval.py --yes   # 跳过运行前的确认提示

fixture 数据在 evals/fixtures_analyzer.py。每条 fixture 完整的原始 LLM 输出会
写进 evals/reports/<timestamp>.md（已加入 .gitignore），方便像
spec/product-review.md 那样人工抽查分数背后的理由是否合理——脚本只能验证"有没
有遵守写死的规则"，打分本身"准不准"仍然需要人看。
"""
import argparse
import os
import statistics
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
import config  # noqa: E402
import llm  # noqa: E402
from fixtures_analyzer import FIXTURES, FIXTURES_BY_ID  # noqa: E402

def run_fixture_analyze(fixture, provider, model, repeats):
    # 结构校验（字段齐全、分数范围、is_gap 类型）现在是 analyzer.analyze_job() 生产路径自己
    # 的一部分（见 analyzer.validate_analysis_result），不合规会在 analyze_job() 内部直接
    # 抛错——这里不用再单独跑一遍，抛出的错误会落进下面的 except 分支，跟其它调用失败一样
    # 处理。
    reps = []
    for _ in range(repeats):
        try:
            result = analyzer.analyze_job(
                company=fixture["company"],
                title=fixture["title"],
                jd_text=fixture["jd_text"],
                resume_text=fixture["resume_text"],
                model=model,
                provider=provider,
                preference_profile_text=fixture.get("preference_profile_text"),
            )
            reps.append({"ok": True, "result": result})
        except Exception as e:
            reps.append({"ok": False, "error": str(e)})
    return reps


def run_fixture_classify(fixture, provider, model, repeats):
    reps = []
    companies = list(fixture["companies"].keys())
    for _ in range(repeats):
        try:
            result = analyzer.classify_companies(companies, model=model, provider=provider)
            reps.append({"ok": True, "result": result})
        except Exception as e:
            reps.append({"ok": False, "error": str(e)})
    return reps


def mean_metric(reps, metric):
    values = [float(r["result"][metric]) for r in reps if r["ok"]]
    return statistics.mean(values) if values else None


def evaluate_analyze_fixture(reps, fixture):
    if any(not r["ok"] for r in reps):
        errs = [r["error"] for r in reps if not r["ok"]]
        return "FAIL", [f"{len(errs)}/{len(reps)} 次调用报错（含结构校验失败）：" + "; ".join(errs)]

    values = [r["result"] for r in reps]
    detail = [
        "cognitive_match=" + ", ".join(f"{float(v['cognitive_match']):.2f}" for v in values)
        + " | content_match=" + ", ".join(f"{float(v['content_match']):.2f}" for v in values)
        + " | overall_match=" + ", ".join(f"{float(v['overall_match']):.2f}" for v in values)
    ]

    status = "PASS"
    cap = fixture.get("max_cognitive_match")
    if cap is not None:
        over = [round(float(v["cognitive_match"]), 3) for v in values if float(v["cognitive_match"]) > cap]
        if over:
            status = "FAIL"
            detail.append(f"cognitive_match 超过上限 {cap}：{over}")

    return status, detail


def evaluate_pair(fixture, this_reps, anchor_reps):
    metric = fixture["compare_metric"]
    this_mean = mean_metric(this_reps, metric)
    anchor_mean = mean_metric(anchor_reps, metric)
    if this_mean is None or anchor_mean is None:
        return "FAIL", [f"成对对比缺数据（this={this_mean}, anchor={anchor_mean}），可能是调用失败或结构校验没过"]

    detail = [f"{metric}: 本条均值={this_mean:.3f}，锚点({fixture['pair_with']})均值={anchor_mean:.3f}"]
    status = "PASS"

    margin = fixture.get("min_margin_below")
    if margin is not None and (anchor_mean - this_mean) < margin:
        status = "FAIL"
        detail.append(f"差值 {anchor_mean - this_mean:.3f} 小于要求的最小差距 {margin}")

    floor = fixture.get("min_value_floor")
    if floor is not None and this_mean < floor:
        status = "FAIL"
        detail.append(f"均值 {this_mean:.3f} 低于下限 {floor}（疑似被一票否决）")

    cap = fixture.get("max_overall_match")
    if cap is not None:
        overall_mean = mean_metric(this_reps, "overall_match")
        if overall_mean is not None and overall_mean >= cap:
            status = "FAIL"
            detail.append(f"overall_match 均值 {overall_mean:.3f} 未低于 {cap}")

    return status, detail


def evaluate_classify_fixture(fixture, reps):
    if any(not r["ok"] for r in reps):
        errs = [r["error"] for r in reps if not r["ok"]]
        return "FAIL", [f"{len(errs)}/{len(reps)} 次调用报错：" + "; ".join(errs)]

    detail = []
    status = "PASS"
    for company, expected in fixture["companies"].items():
        got = [r["result"].get(company) for r in reps]
        if any(g != expected for g in got):
            status = "FAIL"
            detail.append(f"{company}：期望 {expected}，实际 {got}")
    if status == "PASS":
        detail.append(f"{len(fixture['companies'])} 家公司在 {len(reps)} 次调用里全部判断正确")
    return status, detail


def evaluate_overview_honesty(fixture, reps):
    phrases = fixture["expect_phrases"]
    detail = []
    any_fabricated = False
    any_ok = False
    for i, r in enumerate(reps):
        if not r["ok"]:
            detail.append(f"第{i+1}次：调用报错 —— {r['error']}")
            continue
        any_ok = True
        overview = r["result"].get("company_overview") or ""
        hit = any(p in overview for p in phrases)
        if not hit:
            any_fabricated = True
        detail.append(f"第{i+1}次：{'如实说明信息不足' if hit else '⚠️ 疑似编造'} —— {overview[:100]}")
    if not any_ok:
        return "WARN", detail
    return ("WARN" if any_fabricated else "PASS"), detail


def write_report(path, provider, model, repeats, rows, raw_by_fixture):
    lines = [
        "# analyzer.py 打分器 LLM 质量回归报告",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- provider={provider} model={model or '(默认)'} repeats={repeats}",
        "",
        "## 汇总",
        "",
        "| fixture | 结果 | 规则 |",
        "|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['id']} | {row['status']} | {row['rule']} |")
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
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


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
    args = parser.parse_args()

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

    call_count = len(fixtures) * args.repeats
    print(f"即将真实调用 LLM（provider={provider}, model={model or '默认'}），"
          f"共 {len(fixtures)} 条 fixture × {args.repeats} 次 = 约 {call_count} 次调用，会产生真实 API 费用。")
    if not args.yes:
        answer = input("确认继续吗？输入 yes 继续，其它任意键取消：")
        if answer.strip().lower() != "yes":
            print("已取消。")
            return 1

    reps_by_id = {}
    raw_by_fixture = {}
    for f in fixtures:
        print(f"跑 {f['id']} ...")
        if f["kind"] == "classify_companies":
            reps = run_fixture_classify(f, provider, model, args.repeats)
        else:
            reps = run_fixture_analyze(f, provider, model, args.repeats)
        reps_by_id[f["id"]] = reps
        raw_by_fixture[f["id"]] = [r.get("result") if r["ok"] else r.get("error") for r in reps]

    rows = []
    for f in fixtures:
        reps = reps_by_id[f["id"]]
        if f["kind"] == "classify_companies":
            status, detail = evaluate_classify_fixture(f, reps)
        elif f["kind"] == "overview_honesty":
            status, detail = evaluate_overview_honesty(f, reps)
        elif f.get("pair_with"):
            anchor_reps = reps_by_id.get(f["pair_with"])
            if anchor_reps is None:
                status, detail = "FAIL", [f"缺少锚点 fixture {f['pair_with']} 的结果（用 --only 时忘了带上？）"]
            else:
                status, detail = evaluate_pair(f, reps, anchor_reps)
        else:
            status, detail = evaluate_analyze_fixture(reps, f)
        rows.append({"id": f["id"], "status": status, "rule": f["rule"], "detail": detail})

    print("\n" + "=" * 70)
    print(f"{'fixture':<32} {'结果':<6} 规则")
    print("=" * 70)
    for row in rows:
        print(f"{row['id']:<32} {row['status']:<6} {row['rule']}")
        for d in row["detail"]:
            print(f"    - {d}")
    print("=" * 70)

    reports_dir = os.path.join(BASE, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, datetime.now().strftime("%Y%m%d-%H%M%S") + ".md")
    write_report(report_path, provider, model, args.repeats, rows, raw_by_fixture)
    print(f"完整报告（含原始 LLM 输出）：{report_path}")

    hard_fail = any(row["status"] == "FAIL" for row in rows)
    if hard_fail:
        print("\n存在 FAIL：以上规则至少有一条没被 LLM 遵守，详见上表。")
        return 1
    warns = [row for row in rows if row["status"] == "WARN"]
    if warns:
        print(f"\n{len(warns)} 条 WARN（弱检查，不影响 exit code，建议人工看一眼报告）。")
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
