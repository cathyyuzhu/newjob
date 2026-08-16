"""标题/地点粗筛：判断一条抓取到的职位跟当前搜索关键词、配置城市列表是否沾边。

跟 pipeline.py 共用（批量自动分析前跳过不相关职位）、scraper.py 也用（入库前直接
过滤掉噪音结果，见 run_search_once()），单独拆出来避免两边互相 import 造成循环依赖。
"""
import re

_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "in", "to", "or",
    # 职级/头衔类通用词——单独出现不能说明"是不是产品经理"，只有"product"这类真正
    # 有区分度的词才算数，否则"Senior Account Manager"会因为共享senior/manager被
    # 误判成跟"Senior Product Manager"相关（2026-08-15 实测踩过的坑）。
    "senior", "sr", "manager", "director", "management", "lead", "head",
}

# 配置里的英文城市名 -> 职位地点字段里可能出现的写法（含中文）。Indeed 对中国的职位
# 经常直接返回中文地名（"北京市"）甚至内部代码（"C30, CN"），不会出现英文城市名，
# 纯英文子串比较会把这些全部误判为不相关而漏掉分析——2026-08-15 实测：加了地点粗筛后
# 186条Indeed职位里只有3条还在被分析，几乎全军覆没，才发现这个问题。
_CITY_ALIASES = {
    "beijing": ("北京",),
    "shanghai": ("上海",),
    "shenzhen": ("深圳",),
    "guangzhou": ("广州",),
    "hangzhou": ("杭州",),
    "remote": ("远程",),
}

# 关键词英文词 -> 中文标题里对应的写法。职位标题是中文、搜索关键词是英文（比如关键词
# "Senior Product Manager" 配中文标题"产品经理（搜索方向）"）时，两边分词后完全没有
# 交集，会被误判成不相关——中文不像英文有空格分词，"产品经理"整段会被识别成一个token，
# 光靠token集合交集找不出"product"和"产品经理"里其实包含着同一个意思。跟地点粗筛的
# _CITY_ALIASES 是同一类问题，这里改用子串匹配兜底（2026-08-15 差点因为这个问题批量
# 删掉一批真实的中文产品经理职位，删除前预览时才发现）。
_TERM_ALIASES = {
    "product": ("产品",),
}


def _keyword_tokens(text):
    return {w for w in re.findall(r"[A-Za-z一-鿿]+", (text or "").lower()) if len(w) > 1 and w not in _STOPWORDS}


def _signal_substrings(token):
    """把一个关键词token（纯英文/纯中文/中英混合blob）展开成一组"标题里出现任意一个
    就算相关"的信号子串，覆盖中英文对应写法。用子串包含而不是token集合精确相等——
    中文没有空格分词，"AI产品经理"这种关键词是一整块，但很多标题里"AI"和"产品经理"
    中间会插入符号/空格变成两个独立token，永远凑不出交集（2026-08-15 实测：这个问题
    导致标题就叫"Product Manager"/"产品经理"/"AI Product Manager"的职位全部被误判成
    不相关，删除前预览才发现，逃过一劫）。"""
    substrings = {token}
    substrings.update(_TERM_ALIASES.get(token, ()))
    for en, zh_aliases in _TERM_ALIASES.items():
        if en in token:
            substrings.update(zh_aliases)
        for zh in zh_aliases:
            if zh in token:
                substrings.add(en)
                substrings.add(zh)
    return substrings


def title_looks_relevant(job):
    """粗筛：职位标题跟搜索到它时用的关键词（job['keyword']）完全没有信号词重合，
    大概率是LinkedIn/Indeed返回的不相关噪音结果（比如搜"AI产品经理"混进来的"Clinical
    Research Lead"）。没记录关键词的老数据不过滤（无法判断）。"""
    kw_tokens = _keyword_tokens(job.get("keyword"))
    if not kw_tokens:
        return True
    title = (job.get("title") or "").lower()
    if not title:
        return True
    signals = set()
    for t in kw_tokens:
        signals |= _signal_substrings(t)
    return any(s and s in title for s in signals)


def location_looks_relevant(job, configured_locations):
    """粗筛：职位地点跟当前配置的城市列表完全对不上，可能是LinkedIn/Indeed返回的地理
    位置噪音结果（比如用"Remote"搜出来的某个不相关国家的坐班职位）。城市列表为空表示
    "不限地点/远程"（跟设置页文案一致），不过滤。没记录地点的老数据不过滤（无法判断）。
    双向子串包含判断（不区分大小写，配合 _CITY_ALIASES 兼容中文地名），不追求精确。"""
    configured = [(loc or "").strip().lower() for loc in configured_locations if (loc or "").strip()]
    if not configured:
        return True
    job_location = (job.get("location") or "").strip().lower()
    if not job_location:
        return True

    candidates = set(configured)
    for loc in configured:
        candidates.update(_CITY_ALIASES.get(loc, ()))
    if any(c in job_location or job_location in c for c in candidates):
        return True

    # Indeed 对国内非直辖市地区经常只返回内部代码（比如"c30, cn"），没有可读的城市名，
    # 既证实不了也证伪不了跟配置城市是否相关。保守起见不过滤——宁可多花一次分析成本，
    # 也不要把可能相关的职位悄悄漏掉（这正是 2026-08-15 那次踩坑的根源）。
    residual = re.sub(r"\bc\d+\b", "", job_location)
    residual = re.sub(r"[,\s]+", "", residual.replace("china", "").replace("cn", ""))
    return not residual
