import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "jobs.db")

DEFAULT_CONFIG = {
    "keywords": ["Senior Product Manager", "AI产品经理"],
    "locations": ["Beijing", "Shanghai", "Remote"],
    "country_indeed": "china",
    "results_wanted": 20,
    "sites": ["indeed", "linkedin"],
    "schedule_hour": 8,
    "schedule_minute": 0,
    # 与 jd-resume-matcher 技能共用的追踪表路径，用于去重、以及自动分析结果的写入目标。
    # 留空则去重时不跟已投递记录比对；自动分析写入时留空会用 ~/Downloads/JD匹配追踪表.xlsx。
    "tracker_xlsx_path": "",
    # 自动分析开关：由用户在页面上对每条待审核职位手动点"自动分析"触发，这里只是路径配置。
    "base_resume_path": "",  # 留空则用 ~/Downloads/Cathy_Yang_Resume_EN_AI.docx
    "resume_output_dir": "",  # 留空则跟 base_resume_path 同目录
    "anthropic_model": "claude-sonnet-5",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
