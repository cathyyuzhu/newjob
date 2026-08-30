"""打分质量 eval 用的几个统计量，仅标准库（不引入 numpy/sklearn——这个项目现有的
所有测试都是手写 assert 脚本，没有任何数值计算依赖，AUC~15行手算比新增一个重量级
依赖更符合这个仓库一贯的取舍）。

`overall_match` 在 0.70/0.75/0.80 这几个值上重度打结（同一堆职位常常被打出完全
相同的分数），朴素的"两两比较 pos>neg 计数"会因为打结而低估真实的区分度，所以
auc() 用带平局处理（tied ranks 取平均秩）的 Mann-Whitney U 公式，不是最简单的
那种。
"""
import random


def auc(scores, labels):
    """scores/labels 等长；labels 里 1=正类，0=负类。返回 [0,1] 之间的 AUC，
    0.5 是随机瞎猜的基线。用平均秩处理 tie（并列的分数会被赋予并列名次的平均值，
    而不是任意打破平局），这是标准 Mann-Whitney U 统计量的做法。

    正类或负类为空时返回 None（AUC 在只有一个类别时没有定义）。
    """
    n = len(scores)
    if n == 0 or n != len(labels):
        return None
    pairs = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    rank = 1
    while i < n:
        j = i
        while j + 1 < n and scores[pairs[j + 1]] == scores[pairs[i]]:
            j += 1
        avg_rank = (rank + (rank + (j - i))) / 2.0
        for k in range(i, j + 1):
            ranks[pairs[k]] = avg_rank
        rank += (j - i + 1)
        i = j + 1

    n_pos = sum(1 for x in labels if x == 1)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    rank_sum_pos = sum(ranks[i] for i in range(n) if labels[i] == 1)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def precision_at(scores, labels, threshold):
    """分数 >= threshold 的那批里，正类占比是多少（"打分器说值得看的，实际有多少
    真的被收藏/投递了"）。没有任何职位达到阈值时返回 (None, 0)——分不清"精度是0"
    还是"压根没有样本"，调用方要看第二个返回值区分。"""
    selected = [(s, l) for s, l in zip(scores, labels) if s >= threshold]
    if not selected:
        return None, 0
    hit = sum(1 for _, l in selected if l == 1)
    return hit / len(selected), len(selected)


def permutation_auc_p95(scores, labels, n=1000, seed=20260830):
    """打乱标签 n 次重算 AUC，取第 95 百分位——回答"以这个样本量，AUC 要高于
    多少才算跟随机瞎标不一样"。真实分数不参与打乱，只打乱哪些行是正类/负类，
    这样能看出"分数本身有没有信息量"，而不是"这批分数长什么样"。"""
    rng = random.Random(seed)
    labels = list(labels)
    n_pos = sum(1 for x in labels if x == 1)
    results = []
    for _ in range(n):
        shuffled = labels[:]
        rng.shuffle(shuffled)
        a = auc(scores, shuffled)
        if a is not None:
            results.append(a)
    if not results:
        return None
    results.sort()
    idx = min(len(results) - 1, int(0.95 * len(results)))
    return results[idx]


def bootstrap_auc_ci(scores, labels, n=1000, seed=20260830):
    """对 (score,label) 行有放回重采样 n 次，返回 (p5, p95) 的 AUC 区间——衡量
    "这批样本本身的抽样噪声有多大"，区别于上面 permutation 衡量的"信号有多强"。
    重采样偶尔会抽出只含单一类别的样本（AUC 未定义），跳过、不计入分母。"""
    rng = random.Random(seed)
    n_rows = len(scores)
    results = []
    for _ in range(n):
        idx = [rng.randrange(n_rows) for _ in range(n_rows)]
        a = auc([scores[i] for i in idx], [labels[i] for i in idx])
        if a is not None:
            results.append(a)
    if not results:
        return None, None
    results.sort()
    lo = results[min(len(results) - 1, int(0.05 * len(results)))]
    hi = results[min(len(results) - 1, int(0.95 * len(results)))]
    return lo, hi
