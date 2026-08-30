"""定制简历改写编造数字的确定性检查（仅标准库，不调 LLM、不碰 DB）。

背景：`analyzer.MATERIALS_PROMPT` 明说"改动要基于简历里真实存在的经历和数据，不能
编造未发生的经历或夸大数据"，但 `resume_edits.annotate_edits()` 只核对改写建议的
`original` 字段有没有对上原文段落——它从来不检查改写后的 `text` 里写了什么。一条
`original` 逐字照抄、但 `text` 编出"服务超过1000家企业客户"的改写建议会一路通过
核验、被 `write_tailored_resume()` 写进发给真实雇主的 .docx。这个模块补的就是这个洞。

做法上跟 `analyzer.verify_mandatory_items` 是同一个形状：`resume_edits.
normalize_for_compare` 归一化两边，朴素子串包含判断，fail-open（抓不到就不算数，
不误伤）。区别是那边核的是"引用"（一段话要么原样出现要么没有），这里核的是
"集合差"（改写文本里的数字，有没有一个不在整份简历的数字集合里）——是启发式，
不是精确匹配，见下面 extract_figures 的说明。
"""
import re

import resume_edits

# 数字后面如果紧跟着这些单位/符号，连着单位一起也算一种"figure"形态：
# "200家" 跟裸数字 "200" 分开记，编造检查两种形态都比对，任一种命中都算发现编造。
_FIGURE_UNITS = ("%", "万", "千", "亿", "家", "人", "次", "年", "个", "k", "K", "+")

# 数字核心：允许千分位逗号（500,000）和小数点，去掉逗号后再比较。刻意不在这条
# 正则里用 (?<![A-Za-z]) 排除紧贴字母的数字——试过，会有一个隐蔽的坑：
# finditer 对 "iPhone15" 这种数字前面是字母的情况，用负向断言会导致正则在"1"这个
# 位置匹配失败后，从下一个位置"5"重新尝试并匹配成功，等于把"15"拆成"1"（正确排除）
# 和"5"（错误漏网），只排除了一半。改成正则只管识别数字本身，前后是不是贴着字母
# 放到下面循环里对着完整匹配片段的边界字符判断，一次性接受/拒绝整个数字串，
# 不会被正则的重试机制拆开。
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def extract_figures(text):
    """从文本里抠出"看起来是量化指标"的数字，返回 set[str]。

    每个命中的数字生成两种形态放进集合：裸数字（去掉千分位逗号）、以及数字后面
    紧跟单位时的"数字+单位"形态（比如 "200家企业客户" 产出 {"200", "200家"}）。
    两种形态都参与后续的包含判断，只要有一种在允许集合里就不算编造。

    数字前后紧贴英文字母（且不是 k/K 这两个被承认的单位）时整条跳过，不计入任何
    集合——大概率是嵌在缩写/型号里的字符（"B2B"、"GPT4"、"iPhone15"），不是量化
    指标。前缀靠 `_FIGURE_RE` 的负向断言过滤，后缀在这里手动查一次紧跟的字符。

    局限（写在这里而不是假装没有）：
    1. 只能抓"数字没变但换了位置"，抓不到"数字对但换了含义"——把简历里真实的
       "35% 营收增长"改述成"35% 用户留存提升"，两边数字都是 35，会被判定为
       没有编造。这是"取整份简历当许可集，而不是逐段比对"这个设计选择的代价：
       段落级比对能抓到重新归因，但会把"把数字从A段搬到B段"这类合法整合动作
       高频误判成编造。
    2. 年份（"2019"、"2016-2019"）也会被当成 figure 提取——这是刻意的：简历里
       真实存在的年份是合法许可集的一部分，模型编一个不存在的年份同样该被抓到。
    3. **不识别否定语境**——2026-08-30 首次真实基线跑出来的假阳性：模型在
       cover letter 里如实说"JD 要求 1000+ 客户/80% 增长，我实际做到的是 200 家/
       35%，这是差距而非虚构"，逐字引用了 JD 的目标数字来**承认自己没有**，
       这本身是诚实行为，但集合差检测认不出"提到=承认没有"和"提到=声称拥有"
       的区别，两者都会被标成"新数字"。这是启发式的天花板，不是好修的 bug——
       真要分辨需要理解句子语义，超出了"确定性代码核查"这条路线的能力范围。
       fixture 保持 soft:True 正是为了兜住这类假阳性：结果仍然可见（供人读），
       但不会让 exit code 变红。
    """
    normalized = resume_edits.normalize_for_compare(text)
    out = set()
    for m in _FIGURE_RE.finditer(normalized):
        core = m.group(0).replace(",", "")
        if not core:
            continue
        before = normalized[m.start() - 1:m.start()]
        tail = normalized[m.end():m.end() + 1]
        if before.isascii() and before.isalpha():
            continue  # 前面紧贴英文字母（"B2B" 的 "2"），判定为嵌在缩写/型号里，整条跳过
        if tail.isascii() and tail.isalpha() and tail not in ("k", "K"):
            continue  # 后面紧贴非单位的英文字母（"iPhone15" 的 "15"），同上
        out.add(core)
        if tail in _FIGURE_UNITS:
            out.add(core + tail)
        # "20+人" 这种：+ 和单位都紧跟着数字，各自也要记一种形态
        if tail == "+":
            out.add(core + "+")
            after_plus = normalized[m.end() + 1:m.end() + 2]
            if after_plus in _FIGURE_UNITS:
                out.add(core + "+" + after_plus)
    return out


def allowed_figures(resume_text):
    """整份简历里出现过的全部数字，作为改写文本的许可集。取整份简历而不是被改的
    那一段——见 extract_figures 文档字符串里的取舍说明。

    先用 resume_edits.parse_indexed_paragraphs 把 "[N] 正文" 格式拆开、丢掉索引号，
    再拼回去抽数字——不能直接把整份带 [N] 标记的原文丢给 extract_figures：
    `resume_edits.normalize_for_compare` 的 `_INDEX_PREFIX_RE` 只在整个字符串开头
    生效一次，对多行文本里散落的 "[3]" "[4]"... 不起作用，那些索引号本身是 0-9 的
    小数字，会污染许可集，导致简历里真实不存在的个位数被误判成"允许"。"""
    paragraphs = resume_edits.parse_indexed_paragraphs(resume_text)
    text = "\n".join(paragraphs.values()) if paragraphs else (resume_text or "")
    return extract_figures(text)


def find_new_figures(rewritten_text, allowed):
    """rewritten_text 里出现、但不在 allowed 许可集里的数字，排序后返回 list[str]。

    比较方式是"这条改写文本抠出来的每个 figure 形态，有没有在许可集里"——不要求
    形态完全一致，只要任一形态（裸数字或数字+单位）命中就不算新增，跟
    analyzer.verify_mandatory_items 的 fail-open 精神一致：宁可漏判也不要因为
    单位写法不同就把合法数字误判成编造。
    """
    found = extract_figures(rewritten_text)
    # 对每个"新数字候选"，只有它的裸数字形态也不在许可集的任何形态里，才真的判定为新增；
    # 例如许可集有 "35%"，改写文本里出现裸 "35" 不该被判成新增（同一个数字换了呈现）。
    allowed_bare = {f.rstrip("%万千亿家人次年个kK+") for f in allowed}
    new = set()
    for figure in found:
        bare = figure.rstrip("%万千亿家人次年个kK+")
        if figure in allowed or bare in allowed_bare:
            continue
        new.add(figure)
    return sorted(new)
