"""段落改写建议的确定性核查：拿模型声称"照抄"的原文，回简历里比对真实段落。

为什么需要这一层：`resume_docx.write_tailored_resume()` 是**整段替换**
（`p.runs[0].text = text` 之后把该段其余 run 清空）。所以一条改写建议只要索引指错段、
或者模型只摘抄了段落里的一句话就只改那一句，应用下去就会把该段其余内容**静默删掉**——
用户拿到一份缺内容的简历，而且没有任何提示。

做法上刻意不用另一个模型去核查：模型声称"这段原文是 X"，我们手上就有真正的第 N 段，
`normalize_for_compare` 一比就知道它有没有说谎。零 LLM 成本、零延迟、结果确定。
这也是本项目防幻觉的主要路线——用确定性代码核查模型的事实性断言，而不是用模型查模型。

三个消费者共用这里：`resume_review.normalize_result`（体检的 paragraph_edits）、
`analyzer.generate_materials`（定制简历的 resume_paragraph_edits）、
`pipeline.build_optimized_resume`（落盘前的服务端复核）。原来这三处对同一个形状的数据
各写各的校验，宽严还不一致（体检拦越界、材料连越界都不拦），收敛到这里。
"""
import difflib
import re
import unicodedata

_INDEX_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s*")
_WS_RE = re.compile(r"\s+")

# 归一化之后仍然要求的相似度下限。0.90 是留给"照抄"的合理偏差：模型经常少个句号、
# 把中文引号写成英文引号、把两个空格并成一个。低于这个值就不是誊写误差了。
SIMILARITY_FLOOR = 0.90

# original 是真实段落的子串时，长度占比低于这个数才算"只摘抄了一部分"。
PARTIAL_RATIO = 0.9

VERDICT_OK = "ok"
VERDICT_OUT_OF_RANGE = "index_out_of_range"
VERDICT_MISSING = "original_missing"
VERDICT_PARTIAL = "original_partial"
VERDICT_MISMATCH = "original_mismatch"

# 每种问题给用户看的说明。措辞刻意说清楚"应用了会怎样"，而不是只说"校验没过"。
VERDICT_WARNINGS = {
    VERDICT_MISSING: "AI 没有给出这一段的原文，无法核对改写位置是否正确。",
    VERDICT_PARTIAL: (
        "AI 只引用了这一段的一部分内容。应用后会用改写文本替换**整段**，"
        "该段其余内容会丢失。"
    ),
    VERDICT_MISMATCH: "AI 引用的原文跟这一段对不上，应用后会覆盖掉本不该改的内容。",
    VERDICT_OUT_OF_RANGE: "段落索引超出了简历实际的段落范围。",
}


def parse_indexed_paragraphs(resume_text):
    """把 read_resume_text() 产出的 "[7] 文本" 逐行还原成 {7: "文本"}。

    索引是稀疏的：read_resume_text() 按 docx 全部段落 enumerate 但跳过空段落，
    所以第 3 行完全可能是 [7]，不能拿行号当索引。
    """
    paragraphs = {}
    for line in (resume_text or "").splitlines():
        m = re.match(r"^\[(\d+)\]\s?(.*)$", line)
        if m:
            paragraphs[int(m.group(1))] = m.group(2)
    return paragraphs


def max_index(paragraphs):
    """最大的段落索引；简历为空时返回 None（表示"不知道，别按索引筛"）。"""
    return max(paragraphs) if paragraphs else None


def normalize_for_compare(s):
    """把文本归一化到"能公平比较誊写差异"的形状。

    做四件事，每一件都对应模型照抄时的一种常见偏差：
    1. 去掉行首的 [N] 索引标记——prompt 说了不要保留，但模型经常连着抄进来
    2. NFKC：全角标点/字母 → 半角（中文输入法下的"，"和","）
    3. 各种空白（含 \\u00a0 不间断空格、制表符）压成单个空格
    4. 首尾空白

    刻意**不做**的两件事：不做 casefold（英文简历里 "PM" 和 "pm" 是两回事）、
    不删标点（标点被改掉本身就是改写，不该当成誊写误差放过）。
    """
    s = _INDEX_PREFIX_RE.sub("", s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace(" ", " ")
    return _WS_RE.sub(" ", s).strip()


def check_edit(edit, paragraphs):
    """核查一条改写建议。返回 (verdict, real_text, similarity)。

    real_text 是索引对应的**真实**段落文本（拿不到就是 None）；similarity 只在做过
    模糊比对时有意义，其余情况是 None。
    """
    if not isinstance(edit, dict):
        return VERDICT_MISMATCH, None, None
    index = edit.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return VERDICT_OUT_OF_RANGE, None, None
    if index not in paragraphs:
        # 索引在简历范围内、但那一行是空段落（read_resume_text 跳过了）也算这一类：
        # write_tailored_resume 会往一个空段落里写内容，位置多半是错的。
        return VERDICT_OUT_OF_RANGE, None, None

    real_text = paragraphs[index]
    claimed = normalize_for_compare(edit.get("original"))
    if not claimed:
        return VERDICT_MISSING, real_text, None

    actual = normalize_for_compare(real_text)
    if claimed == actual:
        return VERDICT_OK, real_text, 1.0

    similarity = difflib.SequenceMatcher(None, claimed, actual).ratio()
    if similarity >= SIMILARITY_FLOOR:
        return VERDICT_OK, real_text, similarity
    # 子串要单独识别：这是最危险的一种。模型只摘抄了它想改的那一句，但整段替换会把
    # 该段其余内容删掉，而相似度比对看起来"只是差得多一点"，不区分就会跟指错段混为一谈。
    if claimed and claimed in actual and len(claimed) < len(actual) * PARTIAL_RATIO:
        return VERDICT_PARTIAL, real_text, similarity
    return VERDICT_MISMATCH, real_text, similarity


# 默认丢弃的 verdict：只有越界。write_tailored_resume 本来就静默跳过越界索引，留在
# 界面上只是一个勾了不生效的框，而且连原文都拿不到，没什么可展示的。
DROP_DEFAULT = (VERDICT_OUT_OF_RANGE,)

# 没有人工复核、结果直接落盘的调用方用这一档：把**已知会损坏内容**的也丢掉。
#
# 注意 VERDICT_MISSING 不在里面。它是"无法核实"而不是"已知有害"——模型漏了 original
# 字段是格式疏忽，不是内容错误，把它也丢掉的话，模型某次少写个字段就会让整个定制简历
# 功能静默失效（表现成"AI 说要定制却没生成文件"），代价远大于收益。
DROP_HARMFUL = (VERDICT_OUT_OF_RANGE, VERDICT_PARTIAL, VERDICT_MISMATCH)


def annotate_edits(edits, paragraphs, drop=DROP_DEFAULT):
    """核查一组改写建议，返回 (保留下来的条目, 被丢弃的报告)。

    drop 是要丢弃的 verdict 集合，其余的**保留但标记** applicable=False + warning。
    默认 DROP_DEFAULT（只丢越界）给有 UI 的简历体检用：选标记不是舍不得条数，而是
    丢掉之后用户永远不知道模型指错过段——把校验结果显性化正是这一层存在的意义。
    DROP_HARMFUL 给 analyzer 的定制简历用（直接落盘，没有 UI 展示警告的机会）。

    无论哪种情况，`original` 都会被覆写成**真实**段落文本，模型给的那份存进
    original_claimed——这样界面上"原文"面板永远是真的原文，不是模型的复述。
    """
    kept, dropped = [], []
    for edit in edits or []:
        if not isinstance(edit, dict):
            continue
        text = (edit.get("text") or "").strip()
        verdict, real_text, similarity = check_edit(edit, paragraphs)
        if not text:
            # 没有改写内容的条目没有任何用处，跟越界一样直接丢。
            dropped.append({"index": edit.get("index"), "verdict": "empty_text"})
            continue
        if verdict in drop:
            dropped.append({"index": edit.get("index"), "verdict": verdict})
            continue

        item = {
            "index": edit["index"],
            "original": real_text if real_text is not None else (edit.get("original") or ""),
            "text": text,
            "reason": edit.get("reason") or "",
        }
        if verdict != VERDICT_OK:
            item["original_claimed"] = edit.get("original") or ""
            item["verdict"] = verdict
            item["warning"] = VERDICT_WARNINGS.get(verdict, "校验未通过。")
            # missing 只是没法核对，不是已知有害，仍然允许应用；另外两种会真的损坏内容。
            item["applicable"] = verdict == VERDICT_MISSING
            if similarity is not None:
                item["similarity"] = round(similarity, 3)
        else:
            item["applicable"] = True
        kept.append(item)
    return kept, dropped


def filter_applicable(edits, paragraphs):
    """服务端落盘前的复核：只放行核查通过的条目，返回 (可应用的, 被拒绝的报告)。

    不能只靠前端的 disabled checkbox——前端传回来的是用户可编辑的 JSON，而且还有一种
    完全正常的路径会绕过它：用户重新上传了简历，再回头应用旧体检的建议，索引全部错位。
    routes_resume.py 已经在算 stale 标志发给前端了，但落盘这一步从来没检查过它。
    这里对着**当前**简历重跑一次核查，那条路径一并堵上。
    """
    ok, rejected = [], []
    for edit in edits or []:
        # 两道关，缺一不可：
        # (1) annotate_edits 已经判过"应用了会损坏内容"的，直接拒——注意不能只靠下面
        #     那道重跑，因为 annotate_edits 会把 original 覆写成真实段落文本，对它的
        #     输出再比一次必然通过，标记是这一类唯一的痕迹。
        if isinstance(edit, dict) and edit.get("applicable") is False:
            rejected.append({"index": edit.get("index"), "verdict": edit.get("verdict") or "not_applicable"})
            continue
        # (2) 对着**当前**简历重跑核查，抓"体检之后简历被换掉了"这条正常路径。
        verdict, _real, _sim = check_edit(edit, paragraphs)
        if verdict in (VERDICT_OK, VERDICT_MISSING):
            ok.append({"index": edit["index"], "text": edit["text"]})
        else:
            rejected.append({"index": (edit or {}).get("index"), "verdict": verdict})
    return ok, rejected
