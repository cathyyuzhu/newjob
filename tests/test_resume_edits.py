"""resume_edits.py 的确定性核查。纯函数，不起 Flask、不碰 DB、不调 LLM。

背景：resume_docx.write_tailored_resume() 是**整段替换**（p.runs[0].text = text，
其余 run 清空）。所以一条改写建议只要索引指错段、或者模型只摘抄了段落里的一句话，
应用下去就会把该段其余内容静默删掉。这个模块就是在应用之前把这种情况抓出来。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resume_edits as re_mod

# read_resume_text() 的真实产出形状：索引是稀疏的（空段落被跳过，所以第 3 行是 [5]）
RESUME = "\n".join([
    "[0] 张三",
    "[2] 负责用户增长项目，带领 3 人团队。上线后次日留存提升 12%，月活从 40 万涨到 55 万。",
    "[5] 熟悉 A/B 测试、埋点体系设计。",
])
PARAS = re_mod.parse_indexed_paragraphs(RESUME)

# ---- 1. 稀疏索引要原样保留，不能按行号重编
assert PARAS == {
    0: "张三",
    2: "负责用户增长项目，带领 3 人团队。上线后次日留存提升 12%，月活从 40 万涨到 55 万。",
    5: "熟悉 A/B 测试、埋点体系设计。",
}, PARAS
assert re_mod.max_index(PARAS) == 5
assert re_mod.max_index({}) is None
assert re_mod.parse_indexed_paragraphs(None) == {}
print("sparse paragraph parsing ok")

# ---- 2. 归一化：只吸收誊写偏差，不吸收真实改动
n = re_mod.normalize_for_compare
assert n("[2] 熟悉 A/B 测试") == n("熟悉 A/B 测试")          # 模型把索引标记一起抄进来
assert n("熟悉：A/B　测试") == n("熟悉：A/B 测试")            # 全角冒号/全角空格
assert n("熟悉 A/B  测试") == n("熟悉 A/B 测试")        # 不间断空格 + 连续空格
assert n("  熟悉 A/B 测试  ") == "熟悉 A/B 测试"
# 大小写有意义（英文简历里 PM ≠ pm），标点被改掉本身就是改写，都不该被吸收
assert n("Senior PM") != n("senior pm")
assert n("提升 12%") != n("提升 12")
print("normalization absorbs transcription noise only ok")

# ---- 3. 五种 verdict
def verdict(edit):
    return re_mod.check_edit(edit, PARAS)[0]


# ok：一字不差
assert verdict({"index": 5, "original": "熟悉 A/B 测试、埋点体系设计。", "text": "x"}) == re_mod.VERDICT_OK
# ok：模型带上了索引标记 + 全角标点，属于合理誊写偏差
assert verdict({"index": 5, "original": "[5] 熟悉　A/B 测试、埋点体系设计。", "text": "x"}) == re_mod.VERDICT_OK
# ok：少了个句号，相似度仍在 0.90 以上
assert verdict({"index": 5, "original": "熟悉 A/B 测试、埋点体系设计", "text": "x"}) == re_mod.VERDICT_OK

# missing：没给原文，没法核对（但不是已知有害，仍允许应用）
assert verdict({"index": 5, "original": "", "text": "x"}) == re_mod.VERDICT_MISSING
assert verdict({"index": 5, "text": "x"}) == re_mod.VERDICT_MISSING

# ★partial★：只摘抄了段落里的一句。整段替换会把"月活从40万涨到55万"那句删掉
partial = {
    "index": 2,
    "original": "负责用户增长项目，带领 3 人团队。",
    "text": "主导用户增长项目，带领 3 人团队。",
}
assert verdict(partial) == re_mod.VERDICT_PARTIAL, verdict(partial)

# mismatch：索引指向第 5 段，原文却是第 2 段的内容 —— 应用会覆写掉本不该改的段落
assert verdict({"index": 5, "original": "负责用户增长项目，带领 3 人团队。", "text": "x"}) \
    == re_mod.VERDICT_MISMATCH

# 越界 / 非法索引 / 指向被跳过的空段落
assert verdict({"index": 99, "original": "x", "text": "x"}) == re_mod.VERDICT_OUT_OF_RANGE
assert verdict({"index": 3, "original": "x", "text": "x"}) == re_mod.VERDICT_OUT_OF_RANGE  # 空段落
assert verdict({"index": -1, "original": "x", "text": "x"}) == re_mod.VERDICT_OUT_OF_RANGE
assert verdict({"index": "2", "original": "x", "text": "x"}) == re_mod.VERDICT_OUT_OF_RANGE
assert verdict({"index": True, "original": "x", "text": "x"}) == re_mod.VERDICT_OUT_OF_RANGE
assert verdict("not a dict") == re_mod.VERDICT_MISMATCH
print("five verdicts ok")

# ---- 4. 相似度阈值两侧：改一个字算誊写误差，改半句就不算了
base = "熟悉 A/B 测试、埋点体系设计。"
assert re_mod.check_edit({"index": 5, "original": base, "text": "x"}, PARAS)[2] == 1.0
near = re_mod.check_edit({"index": 5, "original": "熟悉 A/B 测试、埋点体系设计！", "text": "x"}, PARAS)
assert near[0] == re_mod.VERDICT_OK and near[2] >= re_mod.SIMILARITY_FLOOR, near
far = re_mod.check_edit({"index": 5, "original": "完全不相干的一句话内容", "text": "x"}, PARAS)
assert far[0] == re_mod.VERDICT_MISMATCH and far[2] < re_mod.SIMILARITY_FLOOR, far
print("similarity floor ok")

# ---- 5. annotate_edits 的分级策略
raw = [
    {"index": 5, "original": "熟悉 A/B 测试、埋点体系设计。", "text": "改写5", "reason": "r"},
    partial,
    {"index": 5, "original": "负责用户增长项目，带领 3 人团队。", "text": "改写mismatch"},
    {"index": 5, "original": "", "text": "改写missing"},
    {"index": 99, "original": "x", "text": "改写越界"},       # 丢
    {"index": 2, "original": "x", "text": "   "},              # 丢：没有改写内容
]
kept, dropped = re_mod.annotate_edits(raw, PARAS)
assert len(kept) == 4, [k["text"] for k in kept]
assert len(dropped) == 2, dropped
assert {d["verdict"] for d in dropped} == {re_mod.VERDICT_OUT_OF_RANGE, "empty_text"}

ok_item, partial_item, mismatch_item, missing_item = kept
assert ok_item["applicable"] is True and "warning" not in ok_item
# 会损坏内容的两种：保留但不可应用，且带上说明
assert partial_item["applicable"] is False and partial_item["verdict"] == re_mod.VERDICT_PARTIAL
assert "整段" in partial_item["warning"], partial_item["warning"]
assert mismatch_item["applicable"] is False
# 缺原文只是没法核对，不是已知有害，仍然允许应用
assert missing_item["applicable"] is True and missing_item["verdict"] == re_mod.VERDICT_MISSING

# ★original 永远被覆写成真实段落文本★，模型给的那份挪到 original_claimed
assert partial_item["original"] == PARAS[2], partial_item["original"]
assert partial_item["original_claimed"] == "负责用户增长项目，带领 3 人团队。"
assert missing_item["original"] == PARAS[5]        # 空 original 用真文本回填
print("annotate_edits grading ok")

# DROP_HARMFUL：给没有 UI 展示警告、下游直接落盘的调用方（analyzer 的定制简历）。
# 会损坏内容的（partial/mismatch/越界）丢掉，但"无法核实"的 missing 要留——模型漏个
# 字段是格式疏忽不是内容错误，把它也丢掉会让整个定制简历功能静默失效。
kept2, dropped2 = re_mod.annotate_edits(raw, PARAS, drop=re_mod.DROP_HARMFUL)
assert [k["text"] for k in kept2] == ["改写5", "改写missing"], [k["text"] for k in kept2]
assert len(dropped2) == 4
assert re_mod.VERDICT_MISSING not in re_mod.DROP_HARMFUL
print("DROP_HARMFUL mode keeps unverifiable, drops harmful ok")

# ---- 6. filter_applicable：服务端落盘前的复核
ok_edits, rejected = re_mod.filter_applicable(kept, PARAS)
assert [e["index"] for e in ok_edits] == [5, 5], ok_edits      # ok + missing 放行
assert len(rejected) == 2 and {r["verdict"] for r in rejected} == {
    re_mod.VERDICT_PARTIAL, re_mod.VERDICT_MISMATCH,
}
# 只带 index/text 传下去（write_tailored_resume 只认这两个字段）
assert set(ok_edits[0]) == {"index", "text"}

# ★换了一份简历，旧建议的索引全部错位 → 全部拒绝★
# 这是一条正常路径，不是攻击：用户重新上传简历后回头应用旧体检的建议。
NEW_PARAS = re_mod.parse_indexed_paragraphs("[0] 李四\n[1] 完全不同的一段经历描述。")
ok_edits2, rejected2 = re_mod.filter_applicable(kept, NEW_PARAS)
assert ok_edits2 == [], ok_edits2
assert len(rejected2) == 4
print("filter_applicable rejects stale indexes ok")

print("\nALL PASS")
