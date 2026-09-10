"""路由层共用的小工具，被多个 routes_*.py 一起用，不属于任何一个业务域。"""
from flask import jsonify

import llm


def usage_notification_message(base_message):
    """把 llm.pop_usage_summary() 取到的用量拼进通知文案。给"后台线程生成 + 通知铃"
    这类异步流程用（材料生成/面试准备/题库起草/语音练习出题/简历体检）——这些操作不是
    同步返回 HTTP 响应，没法像 analyze_job_route 那样直接把 llm_usage_text 塞进
    jsonify()，只能借道通知文案。

    调用时机：必须在对应的 llm.start_usage_tracking() 之后、这条通知落库之前调用
    （通常就是 add_notification 的 message 参数位置上直接嵌套调用本函数）。
    """
    usage_text = llm.usage_text(llm.pop_usage_summary())
    if not usage_text:
        return base_message
    return f"{base_message} · {usage_text}" if base_message else usage_text


def need_resume_response(e):
    """把"还没上传简历"翻译成一个前端能识别的响应。

    用 409 而不是 400：这不是请求本身写错了，是服务端当前状态不满足前置条件，重试也没用，
    得先去做另一件事（上传简历）。need_resume 这个标记让前端能弹"去上传"而不是一句
    干巴巴的错误 toast——参见 static/common.js 的 handleNeedResume()。
    """
    return jsonify({"error": str(e), "need_resume": True}), 409
