import threading

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
