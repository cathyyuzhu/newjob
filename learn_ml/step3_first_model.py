"""
第三步：第一个真正的模型——只用一个特征 (overall_match) 的逻辑回归。

为什么先只用一个特征？因为 overall_match 是我们已知最强的现成信号
（step1 里看到收藏组均值 0.71 明显高于忽略组的 0.53）。如果连这一个特征
都训练不出比"瞎猜"更好的模型，那说明思路有问题，没必要急着堆更多特征。

逻辑回归 (Logistic Regression) 在做什么：本质是找一条"分界线"，
把 overall_match 转换成"属于收藏类的概率"——概率越高越像会收藏。
比线性回归多一步"压缩到 0~1 之间"的处理（sigmoid 函数），
这正好符合我们要的"分数/概率"语义。
"""

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score

from data import load_labeled_jobs

df = load_labeled_jobs()
y = df["label"].to_numpy()
X = df[["overall_match"]].to_numpy()  # sklearn 要求 X 是二维的，哪怕只有一列

cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)  # 跟 step2 用同一个切法，结果才能公平对比


def evaluate(model, name):
    print(f"\n========== {name} ==========")
    y_pred = cross_val_predict(model, X, y, cv=cv)
    y_proba = cross_val_predict(model, X, y, cv=cv, method="predict_proba")[:, 1]

    print(classification_report(y, y_pred, target_names=["忽略(0)", "收藏(1)"], zero_division=0))

    cm = confusion_matrix(y, y_pred)
    print("混淆矩阵（行=真实标签，列=预测标签）：")
    print(f"              预测忽略  预测收藏")
    print(f"  真实忽略      {cm[0][0]:>4d}      {cm[0][1]:>4d}")
    print(f"  真实收藏      {cm[1][0]:>4d}      {cm[1][1]:>4d}")

    auc = roc_auc_score(y, y_proba)
    print(f"\nROC-AUC: {auc:.3f}  (0.5=瞎猜，1.0=完美排序；衡量『给收藏的职位打分是否总比忽略的高』，不依赖0.5这个判定阈值)")


# 版本一：默认逻辑回归，把两类同等看待
evaluate(LogisticRegression(), "版本A：默认权重")

# 版本二：class_weight="balanced"——按类别出现频率反向加权，
# 相当于告诉模型"漏掉一个收藏样本，代价要按比例放大"。
# 这对应我们讨论过的业务判断：漏掉一个真正想收藏的职位（false negative）
# 比多推荐一个不感兴趣的（false positive）代价更高。
evaluate(LogisticRegression(class_weight="balanced"), "版本B：class_weight='balanced'")

print(
    "\n对照 step2 基线：accuracy 83.2%，但『收藏』类 recall = 0.00、AUC 等价于 0.5（瞎猜）。"
    "\n看上面两个版本的『收藏(1)』recall 和 AUC 有没有真的超过这个基准——"
    "\n这是本轮判断『这个模型有没有学到东西』的唯一标准，accuracy 数字本身不用太在意。"
)
