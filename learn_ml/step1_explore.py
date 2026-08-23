"""
第一步：认识数据。

机器学习的第一步永远不是建模，是先老老实实看一眼数据长什么样——
有多少条、标签分布均不均衡、有没有缺失值。这一步决定了后面能做什么、
不能做什么，跳过它直接建模是新手最容易踩的坑。

我们要预测的问题：给一条职位的各种信息，能不能猜出用户会"收藏"还是"忽略"它？
这个问题在 jobs.db 里已经有天然的标签——status 字段，
'reviewed' = 已收藏，'dismissed' = 已忽略（这就是"监督学习"里的"监督"：
真实世界里已经发生过的人工决定，就是模型要学习模仿的标准答案）。
"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "jobs.db"

conn = sqlite3.connect(DB_PATH)
df = pd.read_sql_query("SELECT * FROM jobs", conn)
conn.close()

print(f"数据库里一共有 {len(df)} 条职位记录")
print(f"列名: {list(df.columns)}\n")

# 只保留用户已经做过决定的记录（reviewed=收藏 / dismissed=忽略）。
# 'new' 状态的职位还没被人工审核，没有标签，没法用来训练或验证——
# 这类"暂时用不上、但以后可能有用"的数据先放一边。
labeled = df[df["status"].isin(["reviewed", "dismissed"])].copy()
print(f"其中有标签（已收藏/已忽略）的记录: {len(labeled)} 条\n")

print("=== 标签分布 ===")
counts = labeled["status"].value_counts()
print(counts)
print(f"忽略占比: {counts['dismissed'] / len(labeled):.1%}")
print(f"收藏占比: {counts['reviewed'] / len(labeled):.1%}")
print(
    "\n注意这个比例——85% vs 15%，这叫『类别不均衡』(class imbalance)。"
    "\n记住这个数字，下一步会用到：如果一个模型『不管什么职位一律猜忽略』，"
    "\n它的准确率(accuracy)天然就有 ~85%，这个数字后面会用来戳穿"
    "\n『准确率高不代表模型学到了东西』这件事。"
)

print("\n=== 有没有缺失值？（关键候选特征） ===")
candidate_cols = ["overall_match", "company_origin", "site", "location", "keyword"]
print(labeled[candidate_cols].isna().sum())

print("\n=== overall_match（LLM打的匹配度）按标签分组统计 ===")
print(labeled.groupby("status")["overall_match"].describe())
print(
    "\n看这两组的均值/分布差多少——如果收藏组普遍比忽略组分数高很多，"
    "\n说明 LLM 打的分本身就已经是个很强的信号了，这对后面『我们的模型能不能"
    "\n比现成的 overall_match 更聪明』是个重要参照。"
)

print("\n=== company_origin（外企/国内）按标签交叉统计 ===")
print(pd.crosstab(labeled["company_origin"], labeled["status"]))

print("\n=== site（Indeed/LinkedIn）按标签交叉统计 ===")
print(pd.crosstab(labeled["site"], labeled["status"]))
