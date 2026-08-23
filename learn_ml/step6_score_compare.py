"""
第六步：产出"个人化匹配度分数"，跟现成的 overall_match 并排对比。

前面几步的结论是：单特征模型（只用 overall_match 训练的逻辑回归）
交叉验证表现最稳（AUC 0.799），比塞进 company_origin/site/keyword 的完整特征版本更可靠
——后者在小样本上有过拟合迹象，且 company_origin 的贡献混着未证实的选择偏差风险。
所以这里主打的分数用单特征模型产出；完整特征版本的分数也算出来放在旁边参考，
但明确标注它的风险，不作为主推荐。

注意：这里是在全量 101 条数据上训练后，直接对这 101 条数据自己打分——
对『它已经学过的这些职位』打分天然会比对全新职位更准，这里只是想看"模型的判断
跟 overall_match 差在哪"，不是在评估泛化能力（泛化能力已经在前面用交叉验证算过了，
单特征模型大概 AUC 0.80，别把这里的分数当成"新职位也能有这么准"）。
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from data import load_labeled_jobs

pd.set_option("display.width", 160)
pd.set_option("display.max_colwidth", 24)

df = load_labeled_jobs()
y = df["label"].to_numpy()

# 主推荐：单特征模型
X_single = df[["overall_match"]].to_numpy()
model_single = LogisticRegression(class_weight="balanced")
model_single.fit(X_single, y)
df["my_score"] = model_single.predict_proba(X_single)[:, 1]

# 对照参考：完整特征模型（含有风险标注的 company_origin）
onehot = pd.get_dummies(df[["company_origin", "site", "keyword"]], drop_first=True)
X_full = pd.concat([df[["overall_match"]], onehot], axis=1)
model_full = LogisticRegression(class_weight="balanced", max_iter=1000)
model_full.fit(X_full, y)
df["my_score_full"] = model_full.predict_proba(X_full)[:, 1]

df["diff"] = df["my_score"] - df["overall_match"]

corr = df["my_score"].corr(df["overall_match"])
print(f"my_score（单特征模型概率）跟 overall_match 的相关系数: {corr:.3f}")
print("（越接近1，说明这个分数基本就是 overall_match 的重新缩放，没提供多少新信息；")
print(" 明显小于1，说明模型确实从『你的历史收藏/忽略行为』里学到了 overall_match 没体现的东西）\n")

cols = ["company", "title", "status", "company_origin", "overall_match", "my_score", "my_score_full", "diff"]

print("=== 差异最大的15条：my_score 比 overall_match 高很多（模型比LLM更看好）===")
top_pos = df.sort_values("diff", ascending=False).head(15)
print(top_pos[cols].to_string(index=False))

print("\n=== 差异最大的15条：my_score 比 overall_match 低很多（模型比LLM更不看好）===")
top_neg = df.sort_values("diff", ascending=True).head(15)
print(top_neg[cols].to_string(index=False))

print(
    "\n看这两张表怎么读："
    "\n- status 是这条职位真实的处理结果（reviewed=收藏/dismissed=忽略）——"
    "\n  如果 my_score 比 overall_match 更贴近 status 的方向（比如真实收藏的职位 my_score 更高），"
    "\n  说明模型学到的行为信号是有效的。"
    "\n- 重点看这些差异大的条目里 company_origin 是不是集中在某一类——"
    "\n  如果差异大的条目全部是靠 company_origin 撑起来的（比如 my_score_full 和 my_score 差很多，"
    "\n  且都是 domestic），提醒自己这部分差异可能来自那个没验证清楚的选择偏差，别照单全收。"
)
