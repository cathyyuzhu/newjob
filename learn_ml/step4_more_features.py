"""
第四步：加入更多特征，看能不能比"只用 overall_match"（step3 AUC 0.797/0.799）更好。

新加的类别特征（company_origin / site / keyword）要先"one-hot 编码"成数字列——
比如 site 只有 indeed/linkedin 两种取值，编码成一列 site_linkedin（1=linkedin，0=indeed）。
用 drop_first=True 是标准做法：n 个类别只需要 n-1 列就能表达完整信息
（比如 site 只需要 1 列，如果它是 0 就一定是 indeed，不用再加一列 site_indeed，
两列会互相冗余，这叫"哑变量陷阱" dummy variable trap）。

同时做一次关键的对照实验：**去掉 company_origin 前后对比**。
如果 AUC 主要靠它撑起来，说明模型学到的很可能是"复现选择偏差"
（国内公司职位几乎没被认真看过就忽略了），而不是真的更懂你的偏好。
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score

from data import load_labeled_jobs

df = load_labeled_jobs()
y = df["label"].to_numpy()

onehot = pd.get_dummies(df[["company_origin", "site", "keyword"]], drop_first=True)
X_full = pd.concat([df[["overall_match"]], onehot], axis=1)
X_no_origin = X_full.drop(columns=[c for c in X_full.columns if c.startswith("company_origin_")])

print(f"完整特征集列名: {list(X_full.columns)}")
print(f"去掉 company_origin 后列名: {list(X_no_origin.columns)}\n")

cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)


def evaluate(X, name):
    model = LogisticRegression(class_weight="balanced", max_iter=1000)
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
    print(f"ROC-AUC: {auc:.3f}")
    return auc


# 现算而不是抄 step3 的数字——data.py 的清洗规则以后可能再变，硬编码的数字会悄悄过期
auc_single = evaluate(df[["overall_match"]], "只用 overall_match（对照组，等价于 step3 版本B）")
auc_full = evaluate(X_full, "完整特征集（overall_match + company_origin + site + keyword）")
auc_no_origin = evaluate(X_no_origin, "去掉 company_origin（overall_match + site + keyword）")

print(f"\n=== 三者 AUC 对比 ===")
print(f"只用 overall_match:          {auc_single:.3f}")
print(f"完整特征集:                  {auc_full:.3f}")
print(f"完整特征集去掉company_origin: {auc_no_origin:.3f}")
print(
    "\n判断依据：如果『完整特征集』比『去掉company_origin』明显更高，"
    "\n且这个差距接近『完整特征集』比『只用overall_match』的提升幅度，"
    "\n说明这次提升主要是 company_origin 在起作用——"
    "\n结合它『国内公司=0条收藏』的选择偏差，这个提升要打问号，不能直接当『模型更懂你了』。"
)

# 在全量数据上（不做交叉验证）重新训练一次完整特征集模型，专门看系数——
# 这里目的不是评估泛化能力（那是上面 cross_val 的任务），只是想读一读
# 模型到底认为『哪个特征方向上，收藏概率更高』。小样本上的系数解读要谨慎，
# 当参考、别当结论。
final_model = LogisticRegression(class_weight="balanced", max_iter=1000)
final_model.fit(X_full, y)

print("\n=== 模型系数（正=推高收藏概率，负=推低；绝对值越大影响越强）===")
coef_table = pd.Series(final_model.coef_[0], index=X_full.columns).sort_values(key=abs, ascending=False)
for name, value in coef_table.items():
    print(f"  {name:<28s} {value:+.3f}")
