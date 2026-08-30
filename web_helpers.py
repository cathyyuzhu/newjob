"""路由层共用的小工具，被多个 routes_*.py 一起用，不属于任何一个业务域。"""
from flask import jsonify


def need_resume_response(e):
    """把"还没上传简历"翻译成一个前端能识别的响应。

    用 409 而不是 400：这不是请求本身写错了，是服务端当前状态不满足前置条件，重试也没用，
    得先去做另一件事（上传简历）。need_resume 这个标记让前端能弹"去上传"而不是一句
    干巴巴的错误 toast——参见 static/common.js 的 handleNeedResume()。
    """
    return jsonify({"error": str(e), "need_resume": True}), 409
