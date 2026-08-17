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
    "days_old": 30,  # 只抓取最近这么多天内发布的职位；0/留空表示不限
    "sites": ["indeed", "linkedin"],
    # LinkedIn 每条职位的 JD 正文要单独多发一次详情页请求（jobspy 的 linkedin_fetch_description），
    # 没有代理的情况下量一大很容易被限流/拦截导致 JD 全部拿不到，所以单独给它设一个更小的条数上限。
    "linkedin_results_wanted": 8,
    "linkedin_request_delay": 4,  # 每个 关键词×城市 组合抓完 LinkedIn 详情页后，停顿几秒再抓下一组，降低触发限流概率
    "schedule_enabled": True,  # 关闭后每天定时任务不会自动运行，需要手动点"立即搜索"
    "schedule_hour": 8,
    "schedule_minute": 0,
    # 与 jd-resume-matcher 技能共用的追踪表路径，用于去重、以及自动分析结果的写入目标。
    # 留空则去重时不跟已投递记录比对；自动分析写入时留空会用 ~/Downloads/JD匹配追踪表.xlsx。
    "tracker_xlsx_path": "",
    # 基础简历：由「我的简历」页的上传流程回写，一般不用手改（上传的文件落在项目内的
    # resumes/ 目录）。老配置里手填的本机绝对路径继续有效，不用迁移。留空 = 还没上传简历，
    # 匹配分析/面试准备/题库都会返回"请先上传简历"而不是去猜一个默认路径。
    "base_resume_path": "",
    "base_resume_meta": {"original_filename": "", "uploaded_at": ""},  # 只为展示，真身看 base_resume_path
    "resume_output_dir": "",  # 定制简历的输出目录，留空则跟 base_resume_path 同目录
    # 全局默认的 provider/模型。下面的 llm_tasks 没单独配某个功能位时回退到这里。
    "llm_provider": "anthropic",  # "anthropic" 或 "deepseek"
    "anthropic_model": "claude-sonnet-5",
    "deepseek_model": "deepseek-v4-pro",  # 便宜档位可用 "deepseek-v4-flash"
    # 按功能位分别指定模型（2026-08-16）。值是 llm.MODELS 里的 id，留空表示用上面的全局默认。
    # 分开配是因为三者的成本/质量诉求差很多：匹配分析每条职位都要跑一次（量大、便宜优先），
    # 面试准备和题库是一次生成看很久（质量优先）。
    "llm_tasks": {
        "analysis": "",  # JD-简历匹配分析
        "interview_prep": "",  # 单条职位的面试准备
        "interview_bank": "",  # 通用题库：AI 起草 + 跟 AI 对话
        "resume_review": "",  # 简历体检：整份简历的诊断 + 逐段改写建议
    },
    # LinkedIn Easy Apply 半自动投递：自动回答筛选问题用的个人资料表（2026-08-16）。
    # 三个高频字段固定命名，覆盖不到的问题走 extra_answers 关键词匹配；都匹配不上就停在
    # 那一步等本人手动填，不猜、不调用LLM临时判断——详见 spec/mission.md 的边界说明。
    "easy_apply_profile": {
        "work_authorization": "",  # 回答"是否需要工作签证/是否有工作授权"类问题
        "expected_salary": "",  # 回答"期望薪资"类问题
        "notice_period": "",  # 回答"入职时间/notice period"类问题
        "extra_answers": [],  # [{"keyword": "问题里的关键词", "answer": "答案"}, ...]
    },
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
