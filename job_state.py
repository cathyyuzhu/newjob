import threading
import time

# 记录职位当前的分析排队状态——后台自动分析（搜索后/启动补跑）一次会提交一批职位，
# 但 pipeline.analyze_pending_jobs() 是逐条串行处理的（见该函数注释里"是否可以并发"
# 的取舍），排在后面的职位在轮到它之前跟"完全没开始"没法区分，容易让用户以为按钮没反应、
# 重复点击。这里分两个状态：queued（已提交批次、还没轮到）、analyzing（正在跑这一条）。
# 进程内内存状态即可：单进程 Flask app，重启后自动分析本来就会重新触发、状态重新建立。
_lock = threading.Lock()
_queued_ids = set()
_analyzing_ids = set()

# 顶部"AI分析"按钮的"停止分析"用——串行分析循环（pipeline.analyze_pending_jobs）每处理完
# 一条职位就检查一次这个标志，标志被设置就不再继续下一条。跟 _queued_ids/_analyzing_ids
# 一样是进程内内存状态。
_stop_event = threading.Event()

# 批量分析循环当前正在跑的那一条职位id（没有则为None）——LLM调用是同步阻塞的HTTP请求，
# 一次性等完整结果才返回（非流式），Python这边没法从另一个线程强行掐断这个正在进行的网络
# 请求，而且云端那次调用送出去就已经在计费了，掐不掐都省不下这次的钱。用户点"停止分析"时
# 就算没法真的中断这个网络请求，也希望页面立刻表现出"已经停了"，并且这条职位就算LLM结果
# 后台跑完了也不要用（不写库/不写追踪表），跟真的没跑过一样，下次点AI分析还会重新完整分析
# 一次——记录哪条职位需要这样"事后丢弃"（_discard_ids）。
# 单独记录"批量循环当前在跑哪条"（而不是直接对整个 _analyzing_ids 生效），是因为同一时刻
# 除了批量循环，用户也可能手动对另一条职位点了单条"AI 分析"，那条不受批量"停止"影响，
# 不应该被一起丢弃结果。
_batch_current_job_id = None
_discard_ids = set()


def request_stop():
    global _batch_current_job_id
    _stop_event.set()
    with _lock:
        if _batch_current_job_id is not None:
            _discard_ids.add(_batch_current_job_id)
            # 立刻让UI看起来"已经停了"，即使这条职位的LLM调用其实还在后台默默跑完。
            _analyzing_ids.discard(_batch_current_job_id)
        # 排在后面还没轮到的职位也要立刻清掉"排队中"状态，不能指望分析循环自己转回来清
        # ——它这会儿正卡在上面那条职位的LLM调用里出不来，可能还要一两分钟才能转回循环
        # 顶部再检查一次 stop_requested()。不立刻清的话，前端"是否还有职位在排队/分析"
        # 的判断（决定顶部按钮显示"AI分析"还是"停止分析"）在这一两分钟里会一直看到这些
        # 还没清掉的"排队中"，误以为还没真的停下来。
        _queued_ids.clear()


def discard_job(job_id):
    """把单条职位标成"结果作废"，用于用户在这条职位正在分析时把它标记成"已忽略"。

    跟 request_stop() 的区别就是这个函数刻意不做的两件事：不设 _stop_event、不清空
    _queued_ids。用户忽略的是这一条职位，不是整批——排在后面的职位应该照常轮到、照常
    分析。分析循环（pipeline.analyze_pending_jobs）跑完当前这条后会 continue 到下一条，
    而当前这条的LLM结果会在 analyze_and_record() 里的 should_discard() 检查处被丢掉
    （不写库/不写追踪表/不生成简历），跟没跑过一样。

    正在跑的那次LLM调用本身没法中断（同步阻塞的HTTP请求，钱也已经花出去了），这里只
    保证结果不落地；同时立刻把它从 _analyzing_ids/_queued_ids 里摘掉，前端下一次刷新
    就不会再看到这条职位挂着"AI分析中…"。"""
    with _lock:
        _discard_ids.add(job_id)
        _analyzing_ids.discard(job_id)
        _queued_ids.discard(job_id)


def stop_requested():
    return _stop_event.is_set()


def reset_stop():
    """每次有新一批职位要开始排队分析时调用，清掉上一次可能残留的停止标志，
    避免用户很久以前点过一次"停止"，导致这次全新的批次一上来就被误判成"该停了"。"""
    _stop_event.clear()


def set_batch_current(job_id):
    """批量分析循环轮到某条职位、真正调用LLM之前调用，记下"现在正在跑这条"，
    供 request_stop() 判断如果这时候用户点了停止，应该丢弃哪条职位的结果。"""
    global _batch_current_job_id
    with _lock:
        _batch_current_job_id = job_id


def clear_batch_current():
    """这条职位跑完（不管成功/失败/被丢弃）后调用，清掉"当前在跑"标记。"""
    global _batch_current_job_id
    with _lock:
        _batch_current_job_id = None


def should_discard(job_id):
    """LLM调用返回后检查一次：这条职位是不是在结果还没算完的时候被用户点了"停止分析"，
    如果是，调用方应该丢弃这次的结果，不写库/不写追踪表。"""
    with _lock:
        return job_id in _discard_ids


def clear_discard(job_id):
    with _lock:
        _discard_ids.discard(job_id)


def mark_queued(job_ids):
    with _lock:
        _queued_ids.update(job_ids)


def clear_queued(job_ids):
    """把还没轮到分析的职位从"排队中"状态里摘掉——用于用户点"停止分析"时，把当前批次
    里剩下没跑的职位状态清掉，不然会一直停在"排队中"，但实际上没有任何后台循环还会去处理它们。"""
    with _lock:
        _queued_ids.difference_update(job_ids)


def in_progress_ids():
    """当前处于排队中或正在分析的职位id集合，用于避免同一条职位被两个不同来源
    （比如"立即搜索一次"触发的自动分析和顶部"AI分析"按钮）同时排队、重复调用LLM。"""
    with _lock:
        return _queued_ids | _analyzing_ids


def start_analyzing(job_id):
    with _lock:
        _queued_ids.discard(job_id)
        _analyzing_ids.add(job_id)


def finish_analyzing(job_id):
    with _lock:
        _analyzing_ids.discard(job_id)
        _queued_ids.discard(job_id)


def get_states():
    """返回 {job_id: 'queued' | 'analyzing'}，供 /api/jobs 附带给前端。"""
    with _lock:
        states = {jid: "queued" for jid in _queued_ids}
        states.update({jid: "analyzing" for jid in _analyzing_ids})
        return states


# 定制简历 + Cover Letter 生成的进程内状态。跟上面的 queued/analyzing 完全同构（一个
# 排队集合 + 一个"当前正在跑哪条" + 一个停止标志），但刻意分开两套：材料生成从AI匹配
# 分析里拆出来之后（用户点按钮才生成，见 pipeline.generate_materials_for_job），两件事
# 可以同时在跑——一批职位在后台分析的同时，用户完全可能对另一条已分析完的职位点"生成
# 材料"。共用同一组状态会让前端分不清某条职位到底是在分析还是在生成材料，"停止"也会
# 互相误伤。
_materials_queued_ids = set()
_materials_current_id = None
_materials_stop_event = threading.Event()


def request_materials_stop():
    """批量生成材料的"停止"。跟 request_stop() 一样，正在跑的那一条没法中断，但不同的是
    这里不做"事后丢弃"——材料生成是用户明确点出来的，那条已经花掉的钱换回来的简历/cover
    letter 留着总比扔掉有用，用户不想要直接重新生成就行。"""
    _materials_stop_event.set()
    with _lock:
        _materials_queued_ids.clear()


def materials_stop_requested():
    return _materials_stop_event.is_set()


def reset_materials_stop():
    _materials_stop_event.clear()


def mark_materials_queued(job_ids):
    with _lock:
        _materials_queued_ids.update(job_ids)


def clear_materials_queued(job_ids):
    with _lock:
        _materials_queued_ids.difference_update(job_ids)


def start_materials(job_id):
    global _materials_current_id
    with _lock:
        _materials_queued_ids.discard(job_id)
        _materials_current_id = job_id


def finish_materials(job_id):
    global _materials_current_id
    with _lock:
        if _materials_current_id == job_id:
            _materials_current_id = None
        _materials_queued_ids.discard(job_id)


def materials_in_progress(job_id):
    """这条职位是不是已经有一次材料生成在跑/在排队了——用于挡住重复点击（单条按钮
    和批量按钮可能撞在一起，重复跑除了浪费钱还会两次写同一个docx文件）。"""
    with _lock:
        return job_id == _materials_current_id or job_id in _materials_queued_ids


def get_materials_states():
    """返回 {job_id: 'queued' | 'generating'}，供 /api/jobs 附带给前端。"""
    with _lock:
        states = {jid: "queued" for jid in _materials_queued_ids}
        if _materials_current_id is not None:
            states[_materials_current_id] = "generating"
        return states


# LinkedIn Easy Apply 半自动投递的进程内状态——跟上面 queued/analyzing 是同一类不持久化
# 的进程内状态（重启后重新触发即可），不新建单独的模块。
#
# 只有 'opening'（正在启动浏览器/自动填表）是需要暂存的中间态；成功打开等待用户确认之后
# 不额外保留一个"opened_for_review"常驻态——那样的状态没有天然的清除时机（用户直接把
# 浏览器窗口关掉是最常见的收尾方式，没有代码会去感知这个动作、把状态清回空闲），容易
# 卡成按钮永远灰着点不动。成功与否都直接回到"空闲"，用一次性 toast 告诉用户结果；
# 真正的并发保护交给 easy_apply.py 里 launch_persistent_context 自己的进程锁——
# 重复点击时它会自然抛错（说明真的还有一个窗口开着），而不是靠这里的内存状态兜底。
_easy_apply_job_id = None  # 当前正在"opening"阶段（还没轮到判断成功/失败）的职位id
_easy_apply_errors = {}  # {job_id: 最近一次的错误信息}，成功一次会清掉对应职位的记录


def easy_apply_opening():
    """当前是否有一次 Easy Apply 请求正处于"启动浏览器/自动填表"这个短暂的中间阶段
    （几秒到十几秒）——只用来防止同一个按钮被连续点两下开两个后台线程，不是完整的
    并发保护（那部分交给 easy_apply.py 的浏览器进程锁，见上面模块说明）。"""
    with _lock:
        return _easy_apply_job_id is not None


def start_easy_apply(job_id):
    global _easy_apply_job_id
    with _lock:
        _easy_apply_job_id = job_id
        _easy_apply_errors.pop(job_id, None)


def finish_easy_apply(job_id, ok, error=None):
    global _easy_apply_job_id
    with _lock:
        if _easy_apply_job_id == job_id:
            _easy_apply_job_id = None
        if ok:
            _easy_apply_errors.pop(job_id, None)
        else:
            _easy_apply_errors[job_id] = error


def get_easy_apply_states():
    """返回 {job_id: 'opening' | 'error'}，供 /api/jobs 附带给前端；没有 'opened_for_review'
    这个常驻态，成功之后直接不在这个字典里出现，按钮恢复空闲可点。"""
    with _lock:
        states = {}
        if _easy_apply_job_id is not None:
            states[_easy_apply_job_id] = "opening"
        for jid in _easy_apply_errors:
            if jid != _easy_apply_job_id:
                states[jid] = "error"
        return states


def get_easy_apply_error(job_id):
    with _lock:
        return _easy_apply_errors.get(job_id)


# 面试准备生成的进程内状态——跟上面两组一样不持久化。只需要 'generating' 一个中间态：
# 生成结果本身是落库的（interview_preps 表，成功和失败都写一行），所以这里不需要记录
# 错误信息，前端从库里读那一行的 error 字段就能看到失败原因，重启后也还在。
# 这跟 easy_apply 的 _easy_apply_errors 不同——那边的失败是"浏览器没打开"这类不落库的
# 瞬时结果，只能靠内存暂存一次给前端弹 toast。
_interview_prep_ids = set()


def start_interview_prep(job_id):
    with _lock:
        _interview_prep_ids.add(job_id)


def finish_interview_prep(job_id):
    with _lock:
        _interview_prep_ids.discard(job_id)


def interview_prep_in_progress(job_id):
    """这条职位是不是已经有一次生成在跑了——用于挡住重复触发（用户手动点"重新生成"、
    以及投递状态改成"面试中"的自动触发，可能撞在一起）。"""
    with _lock:
        return job_id in _interview_prep_ids


def get_interview_prep_states():
    """返回 {job_id: 'generating'}，供 /api/jobs 附带给前端显示"准备中…"并安排轮询。"""
    with _lock:
        return {jid: "generating" for jid in _interview_prep_ids}


# 通用题库的 AI 起草状态。跟上面几组不同，它不挂在某条职位上——题库是跨职位的单例，
# 全局同一时刻最多只该有一次起草在跑（重复跑除了浪费钱，两次结果还会互相覆盖）。
_bank_generating = False

# 上一次起草失败的原因。面试准备失败时会往 interview_preps 落一行 error，题库没有这么
# 一张"每次生成一行"的表，失败了除了服务端日志没有任何出口——前端只看得到 generating
# 从 true 翻成 false，会把失败当成"跑完了"，弹一个绿色的"起草完成"配一个空题库
# （真实发生过：起草连炸三次，用户以为是自己等得不够久）。放在内存里够用，这条错误只在
# "刚点完起草"的上下文里有意义，重启丢掉不影响什么。
_bank_error = None


def start_bank_generation():
    """抢占式地标记"开始起草"。已经在跑则返回 False，调用方据此拒绝这次请求——
    检查和置位在同一个锁里完成，避免连点两下时两个请求都通过了检查。"""
    global _bank_generating, _bank_error
    with _lock:
        if _bank_generating:
            return False
        _bank_generating = True
        # 新一轮开始就清掉上一轮的错误，不然它会一直挂在界面上，让人分不清这条报错
        # 说的是刚点的这次还是上次。
        _bank_error = None
        return True


def finish_bank_generation(error=None):
    """标记起草结束。error 有值表示这一轮失败了，原因留着给前端显示。"""
    global _bank_generating, _bank_error
    with _lock:
        _bank_generating = False
        _bank_error = error


def bank_generating():
    with _lock:
        return _bank_generating


def bank_error():
    """上一次起草的失败原因（成功或没跑过则为 None）。"""
    with _lock:
        return _bank_error


# 偏好档案生成状态——跟题库起草同一类"全局单例，同一时刻最多跑一次"，理由也一样：
# 重复跑除了浪费钱，两次结果还会互相覆盖（后写入的覆盖先写入的，没有实际意义）。
_profile_generating = False


def start_profile_generation():
    global _profile_generating
    with _lock:
        if _profile_generating:
            return False
        _profile_generating = True
        return True


def finish_profile_generation():
    global _profile_generating
    with _lock:
        _profile_generating = False


def profile_generating():
    with _lock:
        return _profile_generating


# 简历体检状态——跟题库起草（_bank_generating/_bank_error）同一个模式：全局单例、
# 同一时刻最多跑一次，改成后台线程跑之后需要这组状态让前端知道"还在跑"还是"跑挂了"，
# 不然刷新页面/跳转回来会看不出体检是否还在进行，误以为被打断了。
_resume_review_generating = False
_resume_review_error = None


def start_resume_review():
    global _resume_review_generating, _resume_review_error
    with _lock:
        if _resume_review_generating:
            return False
        _resume_review_generating = True
        _resume_review_error = None
        return True


def finish_resume_review(error=None):
    global _resume_review_generating, _resume_review_error
    with _lock:
        _resume_review_generating = False
        _resume_review_error = error


def resume_review_generating():
    with _lock:
        return _resume_review_generating


def resume_review_error():
    with _lock:
        return _resume_review_error


# 面试语音练习：出题生成状态——跟 _interview_prep_ids 同一个模式（只需要一个
# 'generating' 中间态，结果本身落库在 interview_practice_sets，失败原因前端从那一行
# 的 error 字段读）。按 doc_id 区分而不是全局单例：不同文档的出题互不相干，可以同时跑。
_practice_generating_ids = set()


def start_practice_generation(doc_id):
    with _lock:
        _practice_generating_ids.add(doc_id)


def finish_practice_generation(doc_id):
    with _lock:
        _practice_generating_ids.discard(doc_id)


def practice_generation_in_progress(doc_id):
    with _lock:
        return doc_id in _practice_generating_ids


# LinkedIn jobs-tracker 列表同步状态（"已收藏"/"已投递"/"面试"，见 linkedin_tracker.py）
# ——跟题库起草（_bank_generating/_bank_error）同一个模式：全局单例、同一时刻最多跑
# 一次（要开一次真实浏览器扫列表，重复跑除了浪费时间，两次浏览器还会抢同一个登录
# profile 的独占锁，后一次必然报错）。多存一个 result 是因为这个操作没有像题库/体检
# 那样落库的地方，前端只能靠这里拿到"抓到几条/入库几条"的汇总数字，不像 add_by_url
# 那样能在同一次HTTP请求里同步拿到结果。
#
# 按 stage（"saved"/"applied"/"interview"）分开存一份，而不是全局共用一份：这几个
# 列表是用户会分别独立触发的事，共用一份状态会导致"正在同步已收藏"时误报"已投递也
# 在同步中"、或者一个的结果覆盖另一个还没被前端看到的结果。这里的 key 要跟
# linkedin_tracker.SUPPORTED_STAGES 保持一致，加新 stage 时两边都要改。
#
# 同一个 stage 内部重复点击靠这里的 syncing 标记挡（409）；不同 stage 之间、以及
# 跟 How You Fit 单条/批量同步之间的互斥，2026-09-08 之前完全没有协调，全靠 Chromium
# 对登录 profile 目录的独占锁"谁后 launch 谁失败"（`EasyApplyInProgress`）——用户前后
# 脚点两个不同类型的同步按钮时，后一个会莫名其妙报错，还得自己发现失败再手动重新点。
# 改成显式排队（见下面「LinkedIn 登录态浏览器排队」）：`queued_behind` 记录这条同步
# 正在等谁用完浏览器，为 None 就是"正常同步中或还没开始"。
_tracker_sync = {
    "saved": {"syncing": False, "result": None, "error": None, "queued_behind": None},
    "applied": {"syncing": False, "result": None, "error": None, "queued_behind": None},
    "interview": {"syncing": False, "result": None, "error": None, "queued_behind": None},
}


def start_tracker_sync(stage):
    with _lock:
        state = _tracker_sync[stage]
        if state["syncing"]:
            return False
        state["syncing"] = True
        state["error"] = None
        state["queued_behind"] = None
        return True


def finish_tracker_sync(stage, result=None, error=None):
    with _lock:
        state = _tracker_sync[stage]
        state["syncing"] = False
        state["queued_behind"] = None
        if error is None:
            state["result"] = result
        else:
            state["error"] = error


def tracker_syncing(stage):
    with _lock:
        return _tracker_sync[stage]["syncing"]


def tracker_sync_result(stage):
    with _lock:
        return _tracker_sync[stage]["result"]


def tracker_sync_error(stage):
    with _lock:
        return _tracker_sync[stage]["error"]


def set_tracker_queued_behind(stage, label):
    with _lock:
        _tracker_sync[stage]["queued_behind"] = label


def tracker_queued_behind(stage):
    with _lock:
        return _tracker_sync[stage]["queued_behind"]


# LinkedIn "How You Fit" 搜索同步状态——跟 _tracker_sync 同一个"syncing/result/error
# 三件套+全局锁"模式，区别是 key（search_id）是用户自定义、数量不固定的，没法像
# _tracker_sync 那样在模块加载时预先列出来，改成第一次访问某个 search_id 时才用
# setdefault 现建一份默认状态。`queued_behind` 含义跟 `_tracker_sync` 里的同名字段
# 一致，见那边的说明。
_how_you_fit_sync = {}


def _how_you_fit_state(search_id):
    return _how_you_fit_sync.setdefault(
        search_id, {"syncing": False, "result": None, "error": None, "queued_behind": None}
    )


def start_how_you_fit_sync(search_id):
    with _lock:
        state = _how_you_fit_state(search_id)
        if state["syncing"]:
            return False
        state["syncing"] = True
        state["error"] = None
        state["queued_behind"] = None
        return True


def finish_how_you_fit_sync(search_id, result=None, error=None):
    with _lock:
        state = _how_you_fit_state(search_id)
        state["syncing"] = False
        state["queued_behind"] = None
        if error is None:
            state["result"] = result
        else:
            state["error"] = error


def how_you_fit_syncing(search_id):
    with _lock:
        return _how_you_fit_state(search_id)["syncing"]


def how_you_fit_sync_result(search_id):
    with _lock:
        return _how_you_fit_state(search_id)["result"]


def how_you_fit_sync_error(search_id):
    with _lock:
        return _how_you_fit_state(search_id)["error"]


def set_how_you_fit_queued_behind(search_id, label):
    with _lock:
        _how_you_fit_state(search_id)["queued_behind"] = label


def how_you_fit_queued_behind(search_id):
    with _lock:
        return _how_you_fit_state(search_id)["queued_behind"]


def discard_how_you_fit_state(search_id):
    """设置页删掉某条搜索配置时调用，清掉对应的状态条目，避免这个内存字典随着
    "新增又删除"的操作无限增长（实际量级不大，属于卫生性清理，不是必须）。"""
    with _lock:
        _how_you_fit_sync.pop(search_id, None)


# LinkedIn "How You Fit" 批量同步（"立即同步全部"按钮 + 每日定时任务共用同一把锁）
# ——防止另一次批量同步撞车。跟单条同步、tracker 同步之间的互斥现在交给下面「LinkedIn
# 登录态浏览器排队」，不再是"完全不管、指望不要撞上"。
_how_you_fit_batch = {"syncing": False, "result": None, "error": None, "queued_behind": None}


def start_how_you_fit_batch():
    with _lock:
        if _how_you_fit_batch["syncing"]:
            return False
        _how_you_fit_batch["syncing"] = True
        _how_you_fit_batch["error"] = None
        _how_you_fit_batch["queued_behind"] = None
        return True


def finish_how_you_fit_batch(result=None, error=None):
    with _lock:
        _how_you_fit_batch["syncing"] = False
        _how_you_fit_batch["queued_behind"] = None
        if error is None:
            _how_you_fit_batch["result"] = result
        else:
            _how_you_fit_batch["error"] = error


def how_you_fit_batch_syncing():
    with _lock:
        return _how_you_fit_batch["syncing"]


def set_how_you_fit_batch_queued_behind(label):
    with _lock:
        _how_you_fit_batch["queued_behind"] = label


def how_you_fit_batch_queued_behind():
    with _lock:
        return _how_you_fit_batch["queued_behind"]


def how_you_fit_batch_result():
    with _lock:
        return _how_you_fit_batch["result"]


def how_you_fit_batch_error():
    with _lock:
        return _how_you_fit_batch["error"]


# ---------------------------------------------------------------------------
# LinkedIn 登录态熔断器（2026-08-29，见 spec/roadmap.md「职位收集链路的错误处理生产级
# 加固」缺口5）
#
# 无人值守路径（tracker/how_you_fit 同步，尤其是每日定时任务）撞上"登录态确定已失效"
# 时，原来的行为是继续尝试下一条搜索——8 条搜索 × 2 次会话（无头 + 撞墙后带界面重试）
# = 在账号已经被风控盯上的时候再撞 16 次。这里加一层熔断：连续 N 次判定为真正的登录态
# 失效就打开熔断，M 分钟内所有登录态路径直接快速失败，不再继续尝试——账号风险不会因为
# 多试几次就消失，只会因为多试几次而变大。
#
# 只认 LinkedInAuthRequired 这一种确定性信号（"无头+带界面都撞上登录墙"/"从没保存过
# 登录态"），不认 EasyApplyInProgress（profile 被别的窗口占用，跟账号本身没关系，
# 继续撞不会有额外风险）——两者是完全不同的失败原因，不能共用同一个计数器。
class LinkedInAuthRequired(Exception):
    """登录态已失效或未登录。是熔断器唯一认的信号——各模块自己的 XxxSyncError/
    EasyApplyError 在判定为这一类失败时应该额外继承这个类（多重继承），而不是让熔断器
    反过来解析错误消息字符串去猜是不是登录问题。"""


_linkedin_auth_lock = threading.Lock()
_linkedin_auth = {"consecutive_failures": 0, "opened_until": None}

LINKEDIN_AUTH_BREAKER_THRESHOLD = 2                # 连续几次判定登录态失效就熔断
LINKEDIN_AUTH_BREAKER_COOLDOWN_SECONDS = 30 * 60   # 熔断期多长


def record_linkedin_auth_failure():
    """登录态路径判定为真正的登录态失效（发出过真实请求、确认撞上登录墙）时调用一次。
    "从没保存过登录态"这种没发出任何请求的配置缺失不算，不要在那种分支调用这个。"""
    with _linkedin_auth_lock:
        _linkedin_auth["consecutive_failures"] += 1
        if _linkedin_auth["consecutive_failures"] >= LINKEDIN_AUTH_BREAKER_THRESHOLD:
            _linkedin_auth["opened_until"] = time.time() + LINKEDIN_AUTH_BREAKER_COOLDOWN_SECONDS


def record_linkedin_auth_success():
    """登录态路径成功跑完（不管有没有抓到数据，只要没撞上登录墙）时调用一次，清零计数
    ——避免偶发的一两次真实抖动被当成"账号已经出问题"长期误伤后面的同步。"""
    with _linkedin_auth_lock:
        _linkedin_auth["consecutive_failures"] = 0
        _linkedin_auth["opened_until"] = None


def linkedin_auth_breaker_open():
    """当前是否处于熔断期。熔断期内各登录态路径入口直接快速失败、不会真的发请求，
    所以只能靠熔断期自然过期恢复，不会因为"其实已经重新登录了"提前自愈——这是刻意的
    取舍：换成"仍然放一次请求去探测"会失去熔断本身要避免的那次撞墙。真正等不及的话，
    用户可以重启一次 Flask 进程（进程内状态，重启即清零）。"""
    with _linkedin_auth_lock:
        opened_until = _linkedin_auth["opened_until"]
        return opened_until is not None and time.time() < opened_until


def linkedin_auth_breaker_remaining_seconds():
    with _linkedin_auth_lock:
        opened_until = _linkedin_auth["opened_until"]
        if opened_until is None:
            return 0
        return max(0, int(opened_until - time.time()))


# ---------------------------------------------------------------------------
# LinkedIn 登录态浏览器排队（2026-09-08，修「同时点两个同步按钮，后一个莫名其妙失败」）
#
# tracker 同步（收藏/已投递/面试）、How You Fit 单条/批量同步共用同一个持久化登录
# profile（easy_apply.PROFILE_DIR），Chromium 对它是操作系统级独占锁，同一时刻只能有
# 一个真实浏览器进程用着。之前完全没有跨操作类型的协调：`_tracker_sync`/
# `_how_you_fit_sync`/`_how_you_fit_batch` 各自的 `syncing` 标记只挡"同一类型重复点"，
# 不同类型之间（比如「同步收藏」跟「同步全部搜索」）前后脚点，全靠这把 OS 锁"谁后
# launch 谁失败"，报的是含糊的 `EasyApplyInProgress`，用户还得自己发现失败、手动重新
# 点一次（2026-09-08 真实反馈的案例）。
#
# 改成显式排队：调用方在真正起后台线程跑同步之前，先 `acquire_linkedin_browser(label,
# start_fn)`。能立刻用就返回 `(True, None)`，调用方自己起线程跑 `start_fn`；正被占用
# 就把 `(label, start_fn)` 存进队列、返回 `(False, 占用者的label)`，调用方拿这个占用者
# 标签给用户一个"XX 正在同步，已排队，会自动开始"的提示（写进各自的 `queued_behind`
# 字段，见 `_tracker_sync`/`_how_you_fit_sync`/`_how_you_fit_batch` 状态里的同名字段），
# 不需要用户自己想办法重试。占用者跑完调用 `release_linkedin_browser()`：队列里还有人
# 排着就直接把使用权交给队首那个、在新线程里跑它的 `start_fn`（不需要用户重新点按钮）；
# 队列空了就清空占用标记。
#
# 刻意不包括 Easy Apply：那是"打开浏览器窗口留给用户自己点提交"的半人工流程，没有一个
# 程序能感知到的"结束"时刻（不会自动 close，等用户自己关窗口），排在它后面的自动同步
# 会不知道要等多久、甚至可能永远等不到——所以 Easy Apply 仍然用它自己原有的独立锁
# （`start_easy_apply`/`easy_apply_opening`），不接入这套排队，跟它偶尔撞车的概率维持
# 2026-09-08 之前的现状，不是这次要解决的场景（用户反馈的是两个全自动同步按钮互撞）。
# 同理，`job_link.add_jobs_from_urls()` 内部的浏览器兜底（"贴链接"功能）是一次 HTTP
# 请求内同步等完的，接入排队会让这次请求不知道要挂多久，也不接入。
_linkedin_browser_lock = threading.Lock()
_linkedin_browser_occupant = None   # 当前占用者的人类可读标签，没人占用是 None
_linkedin_browser_queue = []        # [(label, start_fn), ...]，FIFO


def acquire_linkedin_browser(label, start_fn):
    """立刻能用就标记占用并返回 (True, None)——调用方负责真的起线程跑 start_fn（这里
    不代为起线程，因为"立刻能用"这条分支的调用方本来就要自己控制怎么起线程、传什么
    kwargs）。占用中就把 (label, start_fn) 存进队列，返回 (False, 当前占用者标签)。"""
    global _linkedin_browser_occupant
    with _linkedin_browser_lock:
        if _linkedin_browser_occupant is None:
            _linkedin_browser_occupant = label
            return True, None
        _linkedin_browser_queue.append((label, start_fn))
        return False, _linkedin_browser_occupant


def release_linkedin_browser():
    """当前占用者跑完（不管成功失败）后调用一次。队列里还有排队的，直接把占用权交给
    队首那个并在新线程里跑它的 start_fn——不需要用户重新点按钮；队列空了就清空占用
    标记。"""
    global _linkedin_browser_occupant
    with _linkedin_browser_lock:
        if _linkedin_browser_queue:
            label, start_fn = _linkedin_browser_queue.pop(0)
            _linkedin_browser_occupant = label
            next_fn = start_fn
        else:
            _linkedin_browser_occupant = None
            next_fn = None
    if next_fn is not None:
        threading.Thread(target=next_fn, daemon=True).start()
