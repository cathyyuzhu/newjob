"""打分器质量 eval：用库里已经真实发生过的人工决策（收藏/忽略、投递/忽略）核对
`overall_match` 到底有没有区分度。

**这个脚本默认不调 LLM、不花钱**——跟同目录下的 evals/run_analyzer_eval.py 是
完全不同的性质，那个测的是"模型有没有遵守 prompt 里写死的规则"、每次都要真实
调用；这个直接读库里已经存在的 overall_match，跟人工决策做统计对比。唯一花钱
的路径是 --replay N（重跑 analyzer.analyze_job() 拿回没有落库的子分数），显式
opt-in，同样要求 --yes 或交互确认。

## 这个指标的三个先天限制（每次运行都会打印在最前面，不是甩在文档里不提）

1. **混淆**：overall_match 在用户做决定时就显示在界面上，它部分导致了标签本身。
   高 AUC 可能测的是"用户信任这个分"，不是"分打得对"。这个 eval 没法完全剥离
   这层混淆，能给的部分对照是下面的分歧清单（具体到哪几条职位分歧最大，人可以
   自己判断）和 in-remit 切片（限定在打分器自己声称负责的两个维度上）。
2. **选择偏差**：只统计有 overall_match 的职位——那些是花钱做过分析的，本身就是
   "认真考虑过"的子集。真实库里 338 条 dismissed 只有 184 条有分数，154 条被
   丢弃，且这个丢弃对负类的影响远大于正类（reviewed 47 条里 46 条都有分数）。
3. **样本小、数字会晃**：46 个正类，bootstrap CI 宽度通常在 ±0.07 量级，两次
   相隔几天的运行 AUC 差 0.03 大概率只是新增了几条数据，不代表 prompt 变好/
   变坏了——判断趋势要看多次运行的走势，不要盯着单次数字。

用法（项目根目录下）：
    .venv/Scripts/python.exe evals/run_scoring_eval.py
    .venv/Scripts/python.exe evals/run_scoring_eval.py --scheme applied
    .venv/Scripts/python.exe evals/run_scoring_eval.py --replay 40 --yes   # 花钱
"""
import argparse
import json
import os
import random
import sys
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

import labels  # noqa: E402
import metrics  # noqa: E402

RESULTS_PATH = os.path.join(BASE, "results", "scoring_latest.json")
THRESHOLD = 0.70  # analyzer.py 生成材料的门槛，也是首页原来"越过投递线"的口径


def print_confound_warning():
    print("=" * 70)
    print("⚠ 混淆警告：overall_match 在用户做决定时就显示在界面上，它部分导致了")
    print("  标签本身——高 AUC 可能测的是「用户信任这个分」，不是「分打得对」。")
    print("  下面的分歧清单和 in-remit 切片是部分对照，不是完整解药。")
    print("=" * 70)


def print_hygiene(report):
    print("\n数据卫生：")
    print(f"  dismissed 总数 {report['n_dismissed_total']} 条，其中 "
          f"{report['n_dropped_no_score']} 条没有 overall_match（从没花钱分析过），已丢弃")
    print(f"  job_dismiss_reasons 一共只有 {report['n_reasons_total']} 条记录（覆盖不到全部 "
          f"dismissed），命中「记录问题」关键词丢弃 {report['n_dropped_hygiene']} 条")
    print(f"  最终标签集（scheme={report['scheme']}）：正类 {report['n_positive']} 条，"
          f"负类 {report['n_negative']} 条")


def print_disagreements(rows, threshold, limit=20):
    high_dismissed = sorted(
        (r for r in rows if r["overall_match"] >= threshold and r["label"] == 0),
        key=lambda r: -r["overall_match"],
    )
    low_kept = sorted(
        (r for r in rows if r["overall_match"] < threshold and r["label"] == 1),
        key=lambda r: r["overall_match"],
    )
    print(f"\n分歧清单（首要产物——比下面任何一个数字都更值得读）：")
    print(f"  分数≥{threshold} 却被忽略：{len(high_dismissed)} 条")
    for r in high_dismissed[:limit]:
        print(f"    {r['overall_match']:.2f}  {r['company']} / {r['title']}")
    print(f"  分数<{threshold} 却被收藏/投递：{len(low_kept)} 条")
    for r in low_kept[:limit]:
        print(f"    {r['overall_match']:.2f}  {r['company']} / {r['title']}")
    return high_dismissed, low_kept


def print_in_remit_slice(rows):
    dismissed_ids = [r["id"] for r in rows if r["label"] == 0]
    tag_map = labels.dismiss_tags_for(dismissed_ids)
    in_remit = [
        r for r in rows if r["label"] == 0
        and any(t in labels.IN_REMIT_DISMISS_TAGS for t in tag_map.get(r["id"], []))
    ]
    print(f"\nin-remit 切片（忽略原因只标了「{'」「'.join(labels.IN_REMIT_DISMISS_TAGS)}」——"
          f"打分器 cognitive_match/content_match 自己声称要负责的两个维度，其它原因"
          f"如地点/薪资明确不在打分器 remit 内）：{len(in_remit)} 条")
    if in_remit:
        for r in sorted(in_remit, key=lambda r: -r["overall_match"]):
            print(f"    {r['overall_match']:.2f}  {r['company']} / {r['title']}")
        avg = sum(r["overall_match"] for r in in_remit) / len(in_remit)
        print(f"  均分 {avg:.3f}（仅供参考，n={len(in_remit)} 太小，不算精度/AUC/置信区间）")
    return in_remit


def run_replay(rows, n, provider, model, yes):
    """重跑 analyzer.analyze_job() 拿回没有落库的子分数（cognitive_match/
    content_match/raw_cognitive_match/mandatory_gap_count）。花钱，opt-in。"""
    import analyzer
    import collect_errors
    import config
    import llm
    import resume_store
    from models import get_latest_preference_profile
    from resume_docx import read_resume_text

    positives = [r for r in rows if r["label"] == 1]
    negatives = [r for r in rows if r["label"] == 0]
    half = n // 2
    rng = random.Random(20260830)
    sample = (
        rng.sample(positives, min(half, len(positives)))
        + rng.sample(negatives, min(n - half, len(negatives)))
    )
    rng.shuffle(sample)

    print(f"\n即将重跑 analyzer.analyze_job() 采样 {len(sample)} 条"
          f"（正类 {sum(1 for r in sample if r['label'] == 1)} / "
          f"负类 {sum(1 for r in sample if r['label'] == 0)}），会产生真实 API 费用。")
    print("⚠ 重跑用的是**今天**的简历和**今天**的 prompt，不是当初人工决策时的条件——"
          "子分数反映的是「如果今天重新打分」，不是「当初为什么被这样归类」。")
    if not yes:
        answer = input("确认继续吗？输入 yes 继续，其它任意键取消：")
        if answer.strip().lower() != "yes":
            print("已取消 replay。")
            return None

    cfg = config.load_config()
    default_provider, default_model = llm.resolve_task(cfg, "analysis")
    provider = provider or default_provider
    model = model or default_model
    base_resume_path = resume_store.require_base_resume()
    resume_text = read_resume_text(base_resume_path)
    profile = get_latest_preference_profile(success_only=True)
    jd_by_id = labels.jd_text_for([r["id"] for r in sample])

    collect_errors.reset_retry_count()
    replayed = []
    for i, r in enumerate(sample):
        jd_text = jd_by_id.get(r["id"]) or ""
        if not jd_text.strip():
            print(f"  [{i + 1}/{len(sample)}] 跳过 {r['company']}/{r['title']}（jd_text 为空）")
            continue
        print(f"  [{i + 1}/{len(sample)}] {r['company']} / {r['title']} ...")
        try:
            result = collect_errors.with_retry(
                analyzer.analyze_job,
                company=r["company"], title=r["title"], jd_text=jd_text,
                resume_text=resume_text, model=model, provider=provider,
                preference_profile_text=(profile or {}).get("content_text"),
            )
            replayed.append({
                "id": r["id"], "label": r["label"], "old_overall_match": r["overall_match"],
                "new_overall_match": result["overall_match"],
                "cognitive_match": result["cognitive_match"], "content_match": result["content_match"],
                "raw_cognitive_match": result.get("raw_cognitive_match"),
                "mandatory_gap_count": result.get("mandatory_gap_count", 0),
            })
        except Exception as e:
            print(f"    重跑失败：{e}")

    if not replayed:
        print("replay 没有产出任何成功结果。")
        return {"n_sampled": len(sample), "n_ok": 0, "rows": []}

    cog_scores = [r["cognitive_match"] for r in replayed]
    content_scores = [r["content_match"] for r in replayed]
    lbls = [r["label"] for r in replayed]
    print(f"\nreplay 完成：{len(replayed)}/{len(sample)} 条成功，"
          f"重试次数 {collect_errors.get_retry_count()}")
    print(f"  cognitive_match AUC: {metrics.auc(cog_scores, lbls)}")
    print(f"  content_match AUC: {metrics.auc(content_scores, lbls)}")
    old_new_diff = [abs(r["old_overall_match"] - r["new_overall_match"]) for r in replayed]
    print(f"  新旧 overall_match 平均差异: {sum(old_new_diff) / len(old_new_diff):.3f}"
          f"（差异大说明 prompt/简历/偏好档案自当初分析以来变化明显，两次分数不是同一回事）")

    return {
        "n_sampled": len(sample), "n_ok": len(replayed),
        "cognitive_auc": metrics.auc(cog_scores, lbls),
        "content_auc": metrics.auc(content_scores, lbls),
        "rows": replayed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scheme", default="reviewed", choices=("reviewed", "applied"),
                         help="标签口径，默认 reviewed（收藏 vs 忽略）；applied 是更严格的子集（真的投了 vs 忽略），见 labels.load_labeled_jobs 的说明")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    parser.add_argument("--replay", type=int, default=0, metavar="N",
                         help="重跑 N 条（正负各半）拿回子分数，真实调用 LLM，花钱，需要确认")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--yes", "-y", action="store_true")
    args = parser.parse_args()

    print_confound_warning()
    rows, report = labels.load_labeled_jobs(scheme=args.scheme)
    print_hygiene(report)

    if not rows:
        print("标签集为空，没法算任何指标。")
        return 1

    scores = [r["overall_match"] for r in rows]
    lbls = [r["label"] for r in rows]

    high_dismissed, low_kept = print_disagreements(rows, args.threshold)
    in_remit = print_in_remit_slice(rows)

    p, n_selected = metrics.precision_at(scores, lbls, args.threshold)
    a = metrics.auc(scores, lbls)
    p95 = metrics.permutation_auc_p95(scores, lbls)
    lo, hi = metrics.bootstrap_auc_ci(scores, lbls)

    print(f"\nprecision@{args.threshold}: {f'{p:.3f}' if p is not None else 'n/a（没有职位达到阈值）'}（n={n_selected}）")
    print(f"AUC: {a:.3f}" if a is not None else "AUC: n/a")
    print(f"置换检验 p95（随机瞎标能到多高，供参照——AUC 明显高于这个值才算有信号）: "
          f"{p95:.3f}" if p95 is not None else "n/a")
    print(f"bootstrap 95% CI: [{lo:.3f}, {hi:.3f}]" if lo is not None else "bootstrap 95% CI: n/a")

    replay_result = None
    if args.replay:
        replay_result = run_replay(rows, args.replay, args.provider, args.model, args.yes)

    payload = {
        "schema": 1,
        "run": {
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "scheme": args.scheme,
            "threshold": args.threshold,
            **report,
            "auc": round(a, 3) if a is not None else None,
            "precision_at_threshold": round(p, 3) if p is not None else None,
            "n_selected_at_threshold": n_selected,
            "permutation_p95": round(p95, 3) if p95 is not None else None,
            "bootstrap_ci": [round(lo, 3), round(hi, 3)] if lo is not None else None,
        },
        "disagreements": {
            "high_scored_dismissed": [
                {"id": r["id"], "company": r["company"], "title": r["title"], "overall_match": round(r["overall_match"], 3)}
                for r in high_dismissed
            ],
            "low_scored_kept": [
                {"id": r["id"], "company": r["company"], "title": r["title"], "overall_match": round(r["overall_match"], 3)}
                for r in low_kept
            ],
        },
        "in_remit_slice": [
            {"id": r["id"], "company": r["company"], "title": r["title"], "overall_match": round(r["overall_match"], 3)}
            for r in in_remit
        ],
        "replay": replay_result,
    }
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    print(f"\n结构化结果（已提交进 git，可 diff）：{RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
