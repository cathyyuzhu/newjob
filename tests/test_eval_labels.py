"""evals/labels.py + evals/metrics.py。两部分：
1. metrics.auc() 对手算样例的验证（含一组打结的分数）。
2. evals/labels.py 里 NON_PREFERENCE_REASON_KEYWORDS 是对 learn_ml/data.py:19 那条
   清洗规则刻意的文档化重复（原因见 labels.py 顶部说明：真实 harness 不该依赖
   pandas）。这条测试把 learn_ml/data.py **当纯文本读**，断言两边字面量还对得上
   ——不 import learn_ml（那会拖进 pandas，这个仓库没声明这个依赖），只是字符串
   比对，免费、快，且不会因为环境没装 pandas 而失败。这个手法有先例：
   tests/test_analyzer_rules.py 末尾也是把 analyzer.py 当文本读来断言调用顺序。

不碰真实 jobs.db——labels.load_labeled_jobs() 需要真实数据库文件的部分不在这里测，
那是 evals/run_scoring_eval.py 手动运行时才会用到真实（且是用户本机、已 gitignore
的）jobs.db，接入 tests/run_all.py 会导致新克隆的仓库直接失败。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evals"))

import labels
import metrics

# ---- 1. auc()：手算样例，含一对打结的分数（两个 0.8，一正一负）
# 排序：0.5(neg) 0.6(pos) 0.7(neg) 0.8(pos) 0.8(neg) 0.9(pos)
# 秩：   1        2        3        4.5      4.5      6
# 正类秩和 = 6+4.5+2 = 12.5；U = 12.5 - 3*4/2 = 6.5；AUC = 6.5/9 = 0.7222...
scores = [0.9, 0.8, 0.6, 0.8, 0.7, 0.5]
labels_ = [1, 1, 1, 0, 0, 0]
a = metrics.auc(scores, labels_)
assert abs(a - 6.5 / 9) < 1e-9, a
print("auc: hand-computed tied example ok")

# 完全区分（AUC 应该是 1.0）
assert metrics.auc([0.9, 0.8, 0.1, 0.2], [1, 1, 0, 0]) == 1.0
# 完全反着（AUC 应该是 0.0）
assert metrics.auc([0.1, 0.2, 0.9, 0.8], [1, 1, 0, 0]) == 0.0
# 只有一个类别 -> None
assert metrics.auc([0.5, 0.6], [1, 1]) is None
assert metrics.auc([], []) is None
print("auc: perfect separation / reversed / single-class edge cases ok")

# ---- 2. precision_at
p, n = metrics.precision_at([0.9, 0.8, 0.3, 0.2], [1, 0, 1, 0], threshold=0.7)
assert n == 2 and abs(p - 0.5) < 1e-9, (p, n)
p, n = metrics.precision_at([0.1, 0.2], [1, 0], threshold=0.9)
assert p is None and n == 0, "没有任何职位达到阈值，应该返回 (None, 0) 而不是除零"
print("precision_at ok")

# ---- 3. permutation_auc_p95：强信号数据打乱标签后的 p95 应该明显低于真实 AUC
# 用 5正5负（不是3正3负）——n 太小时随机打乱本身有不可忽略的概率凑出完美分离
# （3正3负下 C(6,3)=20 种排列，纯随机命中"分数最高的3个恰好是正类"的概率就有
# 1/20=5%，会跟 p95 的定义撞在一起，不是这条断言想测的东西）。5正5负下这个概率
# 降到 1/252，才能干净地把"真实信号"和"小样本运气"分开。
strong_scores = [0.95, 0.9, 0.85, 0.8, 0.75, 0.25, 0.2, 0.15, 0.1, 0.05]
strong_labels = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
real_auc = metrics.auc(strong_scores, strong_labels)
p95 = metrics.permutation_auc_p95(strong_scores, strong_labels, n=200)
assert p95 < real_auc, (p95, real_auc)
assert 0.0 <= p95 <= 1.0
print("permutation_auc_p95 ok")

# 固定种子应该可复现
p95_a = metrics.permutation_auc_p95(strong_scores, strong_labels, n=200, seed=1)
p95_b = metrics.permutation_auc_p95(strong_scores, strong_labels, n=200, seed=1)
assert p95_a == p95_b, "固定种子必须能复现，不然 committed JSON 会因为纯随机性 diff"
print("permutation_auc_p95 seeded reproducibility ok")

# ---- 4. bootstrap_auc_ci
lo, hi = metrics.bootstrap_auc_ci(strong_scores, strong_labels, n=200)
assert lo is not None and hi is not None and lo <= real_auc + 1e-9 <= hi + 1e-9, (lo, real_auc, hi)
lo_a, hi_a = metrics.bootstrap_auc_ci(strong_scores, strong_labels, n=200, seed=1)
lo_b, hi_b = metrics.bootstrap_auc_ci(strong_scores, strong_labels, n=200, seed=1)
assert (lo_a, hi_a) == (lo_b, hi_b)
print("bootstrap_auc_ci ok")

# ---- 5. NON_PREFERENCE_REASON_KEYWORDS 是 learn_ml/data.py:19 的文档化重复，
# 不能悄悄漂移——当纯文本读，不 import（learn_ml 依赖 pandas，这个仓库没声明）
learn_ml_src = open(os.path.join(ROOT, "learn_ml", "data.py"), encoding="utf-8").read()
for keyword in labels.NON_PREFERENCE_REASON_KEYWORDS:
    assert keyword in learn_ml_src, (
        f"evals/labels.py 的 NON_PREFERENCE_REASON_KEYWORDS 里有 {keyword!r}，"
        f"但 learn_ml/data.py 源码里找不到——两边的清洗规则已经不一致了"
    )
print("NON_PREFERENCE_REASON_KEYWORDS matches learn_ml/data.py (text-level drift guard) ok")

# ---- 6. IN_REMIT_DISMISS_TAGS 是 routes_jobs.DISMISS_REASON_TAGS 的真子集，
# 且必须只包含打分器 remit 内的两个标签——这条断言用 import（routes_jobs 是
# 项目自己的模块，没有 pandas 那层依赖问题）
sys.path.insert(0, ROOT)
import routes_jobs

assert set(labels.IN_REMIT_DISMISS_TAGS).issubset(set(routes_jobs.DISMISS_REASON_TAGS))
assert set(labels.IN_REMIT_DISMISS_TAGS) == {"职能不对", "层级不匹配"}
print("IN_REMIT_DISMISS_TAGS is a subset of the real tag set ok")

print("\nALL PASS")
