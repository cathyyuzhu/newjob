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
    # 与 jd-resume-matcher 技能共用的追踪表路径，用于去重。留空则不跟已投递记录去重。
    "tracker_xlsx_path": "",
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
