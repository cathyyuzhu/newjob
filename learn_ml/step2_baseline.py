"""
第二步：基线模型（baseline）。

任何模型训练之前，都先问一句："什么都不学，能考多少分？"
这个"什么都不学"的答案就是基线——后面训练的每个模型都要打得过它，
打不过就说明模型其实没学到东西，白费功夫。

这里用 DummyClassifier(strategy="most_frequent")：不管输入什么职位信息，
一律预测"忽略"（因为忽略是训练集里最多的类别）。
"""

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_predict, cross_val_score
from sklearn.metrics import classification_report

from data import load_labeled_jobs

df = load_labeled_jobs()
y = df["label"].to_numpy()

# DummyClassifier 根本不看特征，X 传什么都不影响结果——
# 但 sklearn 的 API 统一要求 fit(X, y)，所以随便给一列占位（全零）。
X_placeholder = np.zeros((len(y), 1))

print(f"样本数: {len(y)}（收藏 {y.sum()} / 忽略 {(y == 0).sum()}）\n")

# StratifiedKFold：把数据切成 5 份，"Stratified"保证每一份里
# 收藏/忽略的比例都跟整体一致（不然运气不好某一份可能一个收藏样本都没有）。
# shuffle+random_state：打乱顺序但固定随机种子，保证这次和下次跑出来的切法一样，
# 方便你我对着同一份结果讨论，不会出现"我这边跑出来的数字和你不一样"。
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

dummy = DummyClassifier(strategy="most_frequent")

acc_scores = cross_val_score(dummy, X_placeholder, y, cv=cv, scoring="accuracy")
print("=== 基线模型：5折交叉验证的 accuracy ===")
print(f"每折: {np.round(acc_scores, 3)}")
print(f"平均: {acc_scores.mean():.1%}\n")

# cross_val_predict：让每条样本都在"没见过它的那一折"上被预测一次，
# 拼起来就是一份完整、公平的预测结果，可以拿去算更细的指标。
y_pred = cross_val_predict(dummy, X_placeholder, y, cv=cv)

print("=== 基线模型：完整分类报告 ===")
print(classification_report(y, y_pred, target_names=["忽略(0)", "收藏(1)"], zero_division=0))

print(
    "解读：accuracy 看着有 84%+，很唬人。但看『收藏(1)』这一行的 recall——"
    "\n是 0.00。因为这个模型压根没预测过一次『收藏』，17 条真正想收藏的职位，"
    "\n一条都没被它找出来。这就是准确率会撒谎的地方：数据不均衡时，"
    "\n『整体对多少』和『真正关心的那一类找出来了多少』是两件完全不同的事。"
    "\n\n后面训练的每个模型，第一件要打过的事，就是让『收藏』这一类的 recall > 0。"
)
