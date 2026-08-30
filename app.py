"""Flask 入口：创建 app、注册各业务域的 blueprint、启动时的一次性后台任务。

路由本身按业务域拆在 routes_*.py 里（见 spec/architecture.md 的"控制器/API 层"一节），
这个文件只负责组装，不再直接定义任何 @app.route。
"""
import logging
import threading

from flask import Flask

import llm
import resume_store
from models import init_db, insert_llm_call, update_llm_call_error
from pipeline import MAX_DOC_BYTES
from routes_core import core_bp
from routes_interview import interview_bp
from routes_job_actions import job_actions_bp
from routes_jobs import jobs_bp
from routes_misc import _backfill_materials_from_tracker, misc_bp
from routes_resume import resume_bp
from routes_search import STARTUP_BACKLOG_LIMIT, _analyze_pending_jobs_background, _classify_company_origins_background, search_bp
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
# 上传接口的大小上限。超过这个数 Flask 会在读请求体之前就返回 413，不会先把几百MB
# 读进内存再让我们自己判断。resume_store/pipeline 里还各有一道同样数值的校验，因为
# 那边是"文件已经拿到手了"的最后一关（比如以后有别的入口不走 HTTP）。取两个上传口
# 里较大的那个——这是 Flask 全局唯一一个大小上限，不能按路由分别设置。
app.config["MAX_CONTENT_LENGTH"] = max(resume_store.MAX_RESUME_BYTES, MAX_DOC_BYTES)

for bp in (core_bp, search_bp, jobs_bp, job_actions_bp, interview_bp, resume_bp, misc_bp):
    app.register_blueprint(bp)

init_db()

# LLM 调用流水的写入回调。在这里注册而不是让 llm.py 直接 import models，是为了保住
# "llm.py 不依赖任何项目内模块"这条（见 spec/architecture.md 第5层）。代价是忘了注册
# 就悄悄没数据——tests/test_llm_logging.py 里有一条断言盯着这个。
llm.set_recorder(insert_llm_call, update_llm_call_error)


if __name__ == "__main__":
    start_scheduler()
    # 启动时顺带把历史积压里最新的 STARTUP_BACKLOG_LIMIT 条自动分析一遍（后台跑，不卡启动）。
    threading.Thread(
        target=_analyze_pending_jobs_background, kwargs={"limit": STARTUP_BACKLOG_LIMIT}, daemon=True
    ).start()
    # 公司国籍分类很便宜，不像完整分析那样需要限量，启动时直接把所有历史积压里还没
    # 判断过的一次性处理完，让"外企/国内公司"筛选马上就有历史数据可看。
    threading.Thread(target=_classify_company_origins_background, daemon=True).start()
    threading.Thread(target=_backfill_materials_from_tracker, daemon=True).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
