"""
从 step2 开始，每个脚本都要"读数据 -> 清洗 -> 拿到 X/y"，与其在 5 个脚本里各抄一遍，
不如像正经项目一样抽成一个共享模块——这本身也是标准做法的一部分：
数据加载/清洗逻辑只写一处，后面哪一步改了清洗规则，所有脚本同步生效，不会走岔。
"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "jobs.db"

# 这些忽略原因反映的是"记录本身有问题"（重复入库、职位已下架），
# 不是"看过之后判断不匹配/不感兴趣"——用它们当负样本(label=0)训练，
# 等于教模型一个跟真实偏好无关的模式，所以从训练数据里整行剔除，而不是当负例。
# 局限：只能剔除『填过忽略原因』的记录里能识别出来的这几条，
# 没填原因的忽略记录里可能还混着同类情况，没法排查干净。
NON_PREFERENCE_REASON_KEYWORDS = ("重复", "停止招聘")


def load_labeled_jobs() -> pd.DataFrame:
    """读出有标签（已收藏/已忽略）的职位，做过两轮清洗：
    1. 丢弃 overall_match 缺失的行（都是 dismissed，丢了不影响正类）。
    2. 丢弃忽略原因是『记录问题』而非『偏好判断』的行（重复入库/职位已下架）。
    新增 label 列：1=收藏(reviewed)，0=忽略(dismissed)，这是后面所有模型的 y。
    """
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM jobs", conn)
    reasons = pd.read_sql_query("SELECT job_id, tags, note FROM job_dismiss_reasons", conn)
    conn.close()

    df = df[df["status"].isin(["reviewed", "dismissed"])].copy()
    df = df.dropna(subset=["overall_match"])

    reasons["text"] = (reasons["tags"].fillna("") + " " + reasons["note"].fillna(""))
    non_preference_ids = set(
        reasons.loc[
            reasons["text"].apply(lambda t: any(k in t for k in NON_PREFERENCE_REASON_KEYWORDS)),
            "job_id",
        ]
    )
    excluded = df[df["id"].isin(non_preference_ids)]
    if len(excluded):
        print(
            f"[data.py] 剔除 {len(excluded)} 条『忽略原因是记录问题而非偏好判断』的记录: "
            f"{list(excluded['company'] + ' - ' + excluded['title'])}"
        )
    df = df[~df["id"].isin(non_preference_ids)]

    df["label"] = (df["status"] == "reviewed").astype(int)
    return df.reset_index(drop=True)
