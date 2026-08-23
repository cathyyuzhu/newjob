"""
第五步：专门演示过拟合 (overfitting)。

决策树 (Decision Tree) 很适合演示这件事：给它足够的深度，它可以对着训练数据
一条条切分规则，直到几乎能把每一条样本都记住——"记住答案"和"学到规律"
是两回事，前者在没见过的新数据上会崩，这正是过拟合的核心。

对照两个分数：
- 训练集分数：模型在它见过的数据上考得多少分（用全量数据 fit 完直接自测）
- 交叉验证分数：模型在没见过的数据上大概能考多少分（cross_val，更接近真实能力）
两者差距越大，过拟合越严重。
"""

import pandas as pd
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score, cross_val_predict
from sklearn.metrics import roc_auc_score, recall_score

from data import load_labeled_jobs

df = load_labeled_jobs()
y = df["label"].to_numpy()

onehot = pd.get_dummies(df[["company_origin", "site", "keyword"]], drop_first=True)
X = pd.concat([df[["overall_match"]], onehot], axis=1)

cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

print(f"{'max_depth':<10} {'训练集accuracy':<14} {'交叉验证accuracy':<16} {'训练集AUC':<10} {'交叉验证AUC':<10} {'交叉验证收藏recall':<12} {'叶子数':<6}")
depths = [1, 2, 3, 4, 5, 7, 10, None]
rows = []
for depth in depths:
    model = DecisionTreeClassifier(class_weight="balanced", max_depth=depth, random_state=42)

    # 训练集分数：直接在全量数据上 fit 再自测——模型见过这些答案，分数天然偏高
    model.fit(X, y)
    train_acc = model.score(X, y)
    train_auc = roc_auc_score(y, model.predict_proba(X)[:, 1])
    n_leaves = model.get_n_leaves()

    # 交叉验证分数：每次都在没见过的那一折上测，更接近真实泛化能力
    cv_acc = cross_val_score(model, X, y, cv=cv, scoring="accuracy").mean()
    cv_pred = cross_val_predict(model, X, y, cv=cv)
    cv_proba = cross_val_predict(model, X, y, cv=cv, method="predict_proba")[:, 1]
    cv_auc = roc_auc_score(y, cv_proba)
    cv_recall_pos = recall_score(y, cv_pred, pos_label=1)

    depth_label = "不限" if depth is None else str(depth)
    rows.append(dict(depth=depth_label, train_acc=train_acc, cv_acc=cv_acc, train_auc=train_auc, cv_auc=cv_auc, cv_recall=cv_recall_pos, n_leaves=n_leaves))
    print(f"{depth_label:<10} {train_acc:<14.3f} {cv_acc:<16.3f} {train_auc:<10.3f} {cv_auc:<10.3f} {cv_recall_pos:<12.3f} {n_leaves:<6d}")

best_auc_row = max(rows, key=lambda r: r["cv_auc"])
best_acc_row = max(rows, key=lambda r: r["cv_acc"])
n_samples = len(df)

print(
    f"\n解读表格——两列『交叉验证』指标常常讲不一样的故事，这个分歧本身就是重点："
    f"\n1. 训练集 accuracy/AUC 几乎单调上升，深度不限时逼近 1.0——"
    f"\n   树长得够深，几乎能把 {n_samples} 条样本的答案背下来（叶子数也接近样本数）。这是标准的过拟合信号。"
    f"\n2. 交叉验证 AUC 在 max_depth={best_auc_row['depth']} 见顶（{best_auc_row['cv_auc']:.3f}），"
    f"深度继续增加基本是在下降，跟训练集的走势相反——这才是『过拟合导致真实能力下降』的证据。"
    f"\n3. 但交叉验证 accuracy 反而在 max_depth={best_acc_row['depth']} 最高（{best_acc_row['cv_acc']:.3f}），"
    f"跟 AUC 峰值出现的深度对不上——这不是过拟合消失了，是 accuracy 在不均衡数据上又骗人了一次："
    f"\n   对比两行的『交叉验证收藏recall』：max_depth={best_auc_row['depth']} 时 recall={best_auc_row['cv_recall']:.2f}，"
    f"max_depth={best_acc_row['depth']} 时 recall={best_acc_row['cv_recall']:.2f}——"
    f"树越深，越倾向在没见过的数据上退回预测『忽略』（多数类），蒙对的次数变多、accuracy 被抬高，"
    f"\n   代价是真正关心的『收藏』类识别能力反而更差。"
    f"\n\n结论：『accuracy 最高』的模型不等于『真正有用』的模型，往往恰恰是过拟合更严重的那个——"
    f"\n这是 step2 就强调过的『不能只看 accuracy』在过拟合场景下的翻版，AUC/recall 才是这里能信的指标。"
    f"\n对照 step3/step4：那两步用的逻辑回归本质上只有几个参数（相当于一棵很浅的树），"
    f"\n『模型能力强』和『数据够不够撑得住这个能力』是两件事，后者才是这份数据的硬约束。"
)
