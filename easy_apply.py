"""LinkedIn Easy Apply 半自动投递。

跟 scraper.py（纯 HTTP 抓取）分开——这个模块要开真实可见的浏览器窗口、驱动一个登录态的
LinkedIn 会话。核心设计：自动化只负责把 Easy Apply 表单尽力填好（简历上传、cover letter），
然后把浏览器窗口留在原地不动，由用户自己在 LinkedIn 真实页面上点它自带的提交按钮——程序
任何时候都不会调用提交按钮的 click，即使代码逻辑上应该在提交前就已经停下，也在下面显式
禁止点击任何文本匹配"提交/submit application"的元素，双重保险。

登录态用本地持久化 profile（.playwright_profile/linkedin/，已加入 .gitignore）保存，
不在 config/数据库里存明文账号密码；首次使用需要手动跑一次 ensure_logged_in()。
"""

import logging
import os
import re

from playwright.sync_api import sync_playwright

from config import BASE_DIR

PROFILE_DIR = os.path.join(BASE_DIR, ".playwright_profile", "linkedin")
# 本机没有装 Chrome，用 Edge（同样是真实 Chromium 内核，不是 Playwright 自带的 Chromium）
# ——反自动化检测更容易识别 Playwright 自带 Chromium 的指纹，真实浏览器二进制风险更低。
BROWSER_CHANNEL = "msedge"

# 显式禁止点击任何文本匹配这个模式的元素——这是"程序绝不代替用户点提交"这条设计原则
# 的代码兜底。下面 _click_next_or_review() 推进表单步骤时会用这个模式做排除检查，
# 即使前面的"下一步"按钮识别逻辑有 bug 误命中了提交类按钮，也不会真的点下去。
SUBMIT_DENY_PATTERN = re.compile(r"submit\s*application|提交申请|提交", re.I)

# 自动推进表单步骤的最大轮数，防止表单结构识别有问题时无限循环卡在这个函数里出不来
# ——达到上限就当作"填不动了"停下，跟遇到答不上来的问题是同一种收尾方式。
MAX_STEPS = 15


class EasyApplyError(Exception):
    pass


class EasyApplyInProgress(Exception):
    """同一时刻只能有一个 review 窗口开着（持久化 profile 目录是独占锁）。"""
    pass


def _launch_context(playwright, headless=False):
    """launch_persistent_context 对 user_data_dir 是独占锁（Chromium 自己的
    SingletonLock），同一时间只能有一个真实浏览器进程用这个 profile。不额外自己维护
    一个文件锁去判断"是否已有窗口开着"——那样的锁在"用户直接关掉浏览器窗口"这种正常
    退出路径下没有代码会去清理，容易卡成误报的"一直在用中"。直接尝试 launch，失败
    就说明真的有一个窗口正开着，把 Playwright 抛出的异常包成 EasyApplyInProgress，
    这个判断天然跟"浏览器进程是否真的还活着"同步，不会有状态不一致的问题。
    """
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        return playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel=BROWSER_CHANNEL,
            headless=headless,
        )
    except Exception as e:
        raise EasyApplyInProgress(
            "已有一个 Easy Apply 窗口正在处理中，请先在浏览器里处理完当前这条（或关闭那个窗口后重试）"
        ) from e


def ensure_logged_in(timeout_ms=300_000):
    """一次性手动登录流程：打开真实可见窗口到 LinkedIn 登录页，等用户手动登录完成
    （检测页面跳出 /login，跳到 feed 或其它已登录页面）后正常关闭，让 session cookie
    落盘到 PROFILE_DIR，之后 run_easy_apply() 复用这个登录态。不接 Flask 路由，只在
    终端手动跑一次：python -c "from easy_apply import ensure_logged_in; ensure_logged_in()"
    """
    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.linkedin.com/login")
        logging.info("请在弹出的浏览器窗口里手动登录 LinkedIn，登录完成后这个窗口会自动关闭")
        try:
            page.wait_for_url(lambda url: "/login" not in url and "linkedin.com" in url, timeout=timeout_ms)
            logging.info("检测到已登录，登录态已保存到 %s", PROFILE_DIR)
        except Exception:
            logging.warning(
                "等待登录超时（%s ms），如果你已经登录成功，登录态大概率已经保存，可以直接关闭窗口重试",
                timeout_ms,
            )
        context.close()


def _raise_if_login_wall(page):
    """登录态失效/未登录时 LinkedIn 会跳到几种不同的墙页面：验证码/二次验证走
    checkpoint，直接跳回登录表单走 /login，未登录用户点交互式元素（比如职位页上的
    按钮）常会跳到 authwall——公开的职位详情页本身不登录也能打开、看起来一切正常，
    只有真正触发交互才会暴露出来，容易被误判成"页面上没有这个按钮"。"""
    url = page.url
    if "checkpoint" in url or "/login" in url or "authwall" in url:
        raise EasyApplyError("登录态可能已失效或未登录（跳转到了验证/登录/authwall 页面），请重新运行 ensure_logged_in() 登录")


def _match_answer(question_text, profile):
    """拿问题文字（fieldset legend 或 label 文字）去匹配 profile 里配置的答案。
    三个高频字段（work_authorization/expected_salary/notice_period）用固定关键词组
    识别，覆盖不到的问题走 extra_answers 的关键词子串匹配。都没匹配上返回 None——
    调用方遇到 None 会停在当前步骤，不猜、不调用LLM临时判断（详见 spec/mission.md）。"""
    q = (question_text or "").lower()
    fixed_fields = (
        (profile.get("work_authorization"), ("签证", "工作授权", "sponsor", "visa", "authorized to work", "work authorization")),
        (profile.get("expected_salary"), ("薪资", "薪酬", "salary", "compensation", "期望薪资")),
        (profile.get("notice_period"), ("入职", "到岗", "notice period", "start date", "可入职")),
    )
    for answer, keywords in fixed_fields:
        if answer and any(kw.lower() in q for kw in keywords):
            return answer
    for qa in profile.get("extra_answers") or []:
        keyword = (qa.get("keyword") or "").lower()
        if keyword and keyword in q:
            return qa.get("answer")
    return None


def _fill_radio_group(fieldset, question_text, profile):
    """单选题组：fieldset + legend 是问题，组内每个 input[type=radio] 配一个
    label[for=id] 是选项文字。已经有选中项的跳过（大概率是LinkedIn自己带的默认值）；
    找不到选项文字包含答案（或反过来）的 radio 就算没填成功。"""
    radios = fieldset.locator('input[type="radio"]')
    count = radios.count()
    if count == 0:
        return True
    for j in range(count):
        if radios.nth(j).is_checked():
            return True  # 已经选过，不覆盖用户/LinkedIn已有的选择
    answer = _match_answer(question_text, profile)
    if not answer:
        return False
    for j in range(count):
        radio = radios.nth(j)
        radio_id = radio.get_attribute("id")
        if not radio_id:
            continue
        option_label = fieldset.locator(f'label[for="{radio_id}"]').first
        if option_label.count() == 0:
            continue
        option_text = option_label.inner_text().strip()
        if not option_text:
            continue
        if answer.lower() in option_text.lower() or option_text.lower() in answer.lower():
            try:
                radio.check(timeout=3_000)
                return True
            except Exception:
                try:
                    option_label.click(timeout=3_000)
                    return True
                except Exception:
                    return False
    return False


def _fill_labelled_field(scope, label, profile):
    """文本框/下拉框：靠 label[for] 关联到 input/textarea/select。已经有值的跳过
    （不覆盖 LinkedIn 自动带出的资料，比如姓名/邮箱这类本来就该用账号信息的字段）。
    `scope` 是 Easy Apply 弹窗的 locator，不是整个 page——避免 id 选择器意外扫到
    弹窗背后背景页面上同名 id 的元素。"""
    for_id = label.get_attribute("for")
    if not for_id:
        return True
    field = scope.locator(f'[id="{for_id}"]')
    if field.count() == 0:
        return True
    tag = field.evaluate("el => el.tagName").lower()
    if tag == "input":
        input_type = (field.get_attribute("type") or "text").lower()
        if input_type in ("radio", "checkbox", "file", "hidden"):
            return True  # 单选/复选走 _fill_radio_group；文件已经在简历上传步骤处理过
    elif tag not in ("textarea", "select"):
        return True

    try:
        current_value = field.input_value()
    except Exception:
        current_value = ""
    if current_value:
        return True

    question_text = label.inner_text().strip()
    if not question_text:
        return True
    answer = _match_answer(question_text, profile)
    if not answer:
        return False

    try:
        if tag == "select":
            try:
                field.select_option(label=answer, timeout=3_000)
            except Exception:
                field.select_option(answer, timeout=3_000)
        else:
            field.fill(answer, timeout=3_000)
        return True
    except Exception:
        return False


def _fill_fieldset_text_field(fieldset, question_text, profile):
    """fieldset 包的不是单选题组，是文本/下拉类问题（LinkedIn 有些非单选问题也用
    fieldset+legend 包，不是靠 label[for] 关联）。逻辑跟 _fill_labelled_field 的
    尾段一样，只是字段定位方式不同（在 fieldset 内直接找，不经过 label）。"""
    field = fieldset.locator('input:not([type="radio"]):not([type="checkbox"]):not([type="hidden"]), textarea, select').first
    try:
        current_value = field.input_value()
    except Exception:
        current_value = ""
    if current_value:
        return True
    answer = _match_answer(question_text, profile)
    if not answer:
        return False
    try:
        tag = field.evaluate("el => el.tagName").lower()
        if tag == "select":
            try:
                field.select_option(label=answer, timeout=3_000)
            except Exception:
                field.select_option(answer, timeout=3_000)
        else:
            field.fill(answer, timeout=3_000)
        return True
    except Exception:
        return False


def _answer_questions_on_step(scope, profile):
    """扫描当前 Easy Apply 步骤里能识别到的问题，逐个尝试用 profile 匹配答案填写。
    返回 True 表示这一步的问题都填成功了（或者本来就没有问题），False 表示遇到了
    没匹配上答案的问题——调用方应该停在这里，把窗口留给用户手动填这一题。
    `scope` 是 Easy Apply 弹窗的 locator，不扫整个 page——避免误扫到弹窗背后
    背景页面上无关的表单元素（比如设置页/其它职位卡片里凑巧也有的 label/fieldset）。"""
    all_answered = True

    fieldsets = scope.locator("fieldset")
    fs_count = fieldsets.count()
    logging.info("[easy_apply debug] fieldsets found: %s", fs_count)
    for i in range(fs_count):
        fs = fieldsets.nth(i)
        legend = fs.locator("legend").first
        question_text = legend.inner_text().strip() if legend.count() else ""
        has_radio = fs.locator('input[type="radio"]').count() > 0
        has_text_field = fs.locator(
            'input:not([type="radio"]):not([type="checkbox"]):not([type="hidden"]), textarea, select'
        ).count() > 0
        if not has_radio and not has_text_field:
            continue  # 这个 fieldset 不是问题（比如纯展示用的分组），跳过
        if not question_text:
            # 实测踩过的坑：有些问题（比如"学历要求"）确实有交互控件，但没有标准
            # <legend> 标签（或者是空的），之前的写法在这里直接 continue、既不填
            # 也不算"没答上"，等于悄悄放过一个真实存在的问题——现在没问题文字就
            # 没法匹配答案，直接算未答上，跟真正扫到问题但答案没配置是一个结果。
            logging.info("[easy_apply debug] fieldset 有交互控件但找不到问题文字，判定为未答上")
            all_answered = False
            continue
        ok = _fill_radio_group(fs, question_text, profile) if has_radio else _fill_fieldset_text_field(fs, question_text, profile)
        logging.info("[easy_apply debug] fieldset legend=%r matched=%s", question_text, ok)
        if not ok:
            all_answered = False

    labels = scope.locator("label[for]")
    label_count = labels.count()
    logging.info("[easy_apply debug] labels[for] found: %s", label_count)
    for i in range(label_count):
        label = labels.nth(i)
        try:
            label_text = label.inner_text().strip()
        except Exception:
            label_text = "<err>"
        ok = _fill_labelled_field(scope, label, profile)
        logging.info("[easy_apply debug] label=%r matched=%s", label_text, ok)
        if not ok:
            all_answered = False

    return all_answered


def _click_next_or_review(scope):
    """尝试点击"下一步"类按钮推进到 Easy Apply 表单的下一步。返回 True 表示点了、
    成功推进；False 表示没找到这类按钮——大概率已经走到了最后的确认/review步骤，
    调用方据此判断"该停下来交给用户了"。`scope` 同样是弹窗 locator，不是整个 page。

    实测确认：精确匹配（^…$）对可访问性名称太严格，会漏掉——跟 Easy Apply 按钮那次
    踩的是同一类坑（可访问性名称可能比看到的文字多点别的，或者取名方式跟 textContent
    对不上）。改成子串匹配，SUBMIT_DENY_PATTERN 检查兜底避免误点提交类按钮。"""
    next_btn = scope.get_by_role("button", name=re.compile(r"next|下一步|继续|review|查看申请", re.I))
    count = next_btn.count()
    logging.info("[easy_apply debug] next-like buttons found: %s", count)
    if count == 0:
        return False
    btn_text = next_btn.first.inner_text().strip()
    logging.info("[easy_apply debug] next button text: %r", btn_text)
    if SUBMIT_DENY_PATTERN.search(btn_text):
        # 双重保险：即使上面的正则理论上不该匹配到提交类按钮，这里再显式挡一次。
        logging.info("[easy_apply debug] next button matched SUBMIT_DENY_PATTERN, refusing to click")
        return False
    try:
        next_btn.first.click(timeout=5_000)
        return True
    except Exception:
        return False


def run_easy_apply(job):
    """核心入口：打开 job["job_url"]、点 Easy Apply、尽力填好能填的字段，停在提交前，
    留窗口开着不关，正常返回。返回描述"填到哪一步"的结果 dict，不代表投递是否已完成
    ——投递与否完全取决于用户之后在浏览器里自己的操作。

    best-effort：LinkedIn 每个职位的自定义筛选问题不一样，大概率只能填到第一个答不上来
    的必填项就停下，这是预期的正常结果，不是 bug；每个子步骤单独 try/except，
    一步失败不影响已经填好的部分。

    出错时（登录态失效、找不到 Easy Apply 按钮等）会关闭窗口并抛出 EasyApplyError，
    调用方（app.py 的后台线程）负责捕获并把结果记录到 job_state。
    """
    steps_done = []

    # 不用 `with sync_playwright() as p:`——那个写法会在函数 return 时自动触发
    # __exit__，把 Playwright 驱动连接停掉，连带浏览器一起被关闭（实测踩过：窗口刚打开
    # 一闪就消失）。这里手动管理生命周期：成功路径故意不调用 p.stop()，让驱动连接和
    # 浏览器窗口都保持存活，直到用户自己关掉那个窗口或整个 Flask 进程退出；只有出错
    # 路径才需要主动清理。
    p = sync_playwright().start()
    try:
        context = _launch_context(p, headless=False)
    except EasyApplyInProgress:
        p.stop()
        raise
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(job["job_url"], wait_until="domcontentloaded")
        _raise_if_login_wall(page)

        # 实测确认：LinkedIn 无障碍名称（aria-label）优先于按钮可见文字，且不完全一样——
        # "快速申请"按钮的 aria-label 实际是"快速申请职位"（多了"职位"二字），精确匹配
        # 会漏掉；外部投递链接按钮虽然显示文字也是"申请"，但 aria-label 是"去公司网站
        # 申请"，本来就不该被点，也不受这里影响。"快速申请"/"easy apply" 用子串匹配
        # （覆盖"职位"后缀、"Easy Apply to Company"这类带额外文字的情况）；纯"申请"
        # 保留精确匹配，避免连带匹配到"已申请"（已投过）这类字面包含"申请"但语义相反的按钮。
        easy_apply_btn = page.get_by_role("button", name=re.compile(r"easy apply|快速申请|^申请$", re.I))
        try:
            easy_apply_btn.first.click(timeout=10_000)
            steps_done.append("clicked_easy_apply")
        except Exception as e:
            # 实测踩过的坑：未登录状态下职位页面本身能正常加载（url/标题都正常，
            # 一眼看不出问题），但点击交互会触发 LinkedIn 客户端跳转到 authwall
            # 登录墙——这个跳转发生在点击等待期间，不是 goto() 那一刻就能测出来，
            # 所以这里点击失败后要再检查一次，给出准确的"未登录"提示，而不是让人
            # 误以为是"页面上真的没有这个按钮"。
            _raise_if_login_wall(page)
            raise EasyApplyError(f"没找到或点不了 Easy Apply 按钮：{e}")

        # 简历上传：尽力而为，找不到 file input 就跳过，不影响后面步骤。
        try:
            resume_path = job.get("resume_path")
            if resume_path and os.path.isfile(resume_path):
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(resume_path, timeout=5_000)
                steps_done.append("uploaded_resume")
        except Exception as e:
            logging.info("简历上传步骤跳过（不影响其它步骤）：%s", e)

        # cover letter：按标签关键词尽力匹配文本域，找不到就跳过。
        try:
            cover_letter = job.get("cover_letter")
            if cover_letter:
                cl_field = page.get_by_label(re.compile(r"cover letter", re.I))
                cl_field.first.fill(cover_letter, timeout=5_000)
                steps_done.append("filled_cover_letter")
        except Exception as e:
            logging.info("cover letter 填写步骤跳过（不影响其它步骤）：%s", e)

        # 自动答筛选问题 + 推进"下一步"，直到遇到答不上来的问题、或者走到没有"下一步"
        # 按钮的最后确认页（说明到 review 步骤了）。两种情况都停在原地，从不点击提交类
        # 按钮——这条底线不因为自动化能走到哪一步而改变，详见 spec/mission.md。
        profile = job.get("easy_apply_profile") or {}
        modal = page.get_by_role("dialog")  # Easy Apply 弹窗，后续问题扫描/翻页都限定在这个范围内
        stop_reason = "reached_max_steps"
        for _ in range(MAX_STEPS):
            # 实测踩过的坑：固定 sleep 不够稳定——弹窗每一步的内容是异步渲染的，
            # 有时候 shell（role=dialog）已经出现但按钮/表单还没渲染出来，扫描/找
            # "下一步"按钮时全部扑空，误判成"已经到最后一步"。改成显式等到弹窗内
            # 出现至少一个可交互元素（button/input/select/textarea）再开始扫描；
            # 等不到也不报错，按"这一步可能真的没有交互元素"处理，继续往下走。
            try:
                modal.locator("button, input, select, textarea").first.wait_for(state="visible", timeout=8_000)
            except Exception:
                pass
            if not _answer_questions_on_step(modal, profile):
                stop_reason = "unanswered_question"
                break
            if not _click_next_or_review(modal):
                stop_reason = "reached_final_step"
                break
        steps_done.append(stop_reason)

        # 故意不调用 context.close()/p.stop()：窗口需要留给用户接着操作、自己点提交。
        return {
            "ok": True,
            "steps_done": steps_done,
            "message": "已停在当前步骤，浏览器窗口保持打开，请切换到该窗口手动完成剩余步骤并自己点击提交",
        }
    except EasyApplyError:
        context.close()
        p.stop()
        raise
    except Exception as e:
        context.close()
        p.stop()
        raise EasyApplyError(f"自动化过程出错：{e}") from e
