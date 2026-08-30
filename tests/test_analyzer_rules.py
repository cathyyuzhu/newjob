"""analyzer.apply_score_rules 的确定性打分规则。纯函数，不调 LLM、不碰 DB。

背景：PROMPT_TEMPLATE 里一直写着"未覆盖的强制性要求会把 cognitive_match 封顶到
0.5 / 0.3"，但 Python 侧一行实现都没有——模型返回 0.9 照样入库。唯一的执行力在离线
eval，那是抽样、事后、不阻断的。这个模块把那条规则落成代码。

关键设计：模型必须把 JD 里标注强制性的原话逐字照抄进 mandatory_evidence，程序拿它回
JD 原文核对。核不过就当不是强制要求（fail-open）——不这么做的话，模型只要把
"preferred" 也标成强制，大批职位会被误压到 0.3，比没有这条规则还糟。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer

JD = """Senior Product Manager, Payments

Requirements:
- 8+ years of product management experience
- PMP certification is required for this role
- Must have hands-on experience with PCI-DSS compliance
- Familiarity with SQL is preferred
"""


def item(text, is_gap, mandatory=False, evidence=""):
    return {"text": text, "is_gap": is_gap, "is_mandatory": mandatory, "mandatory_evidence": evidence}


def run(items, cognitive=0.85, jd=JD):
    result = {"requirement_items": items, "cognitive_match": cognitive, "content_match": 0.8}
    return analyzer.apply_score_rules(result, jd)


# ---- 1. 一条核验通过的硬缺口 → 封顶 0.5
r = run([item("PMP 认证", True, True, "PMP certification is required")])
assert r["mandatory_gap_count"] == 1
assert r["cognitive_match"] == 0.5, r["cognitive_match"]
assert r["raw_cognitive_match"] == 0.85, "模型原始输出必须保留，eval 靠它检测 prompt 漂移"
assert len(r["score_adjustments"]) == 1 and "0.5" in r["score_adjustments"][0]
print("single mandatory gap caps to 0.5 ok")

# ---- 2. 两条 → 进一步压到 0.3
r = run([
    item("PMP 认证", True, True, "PMP certification is required"),
    item("PCI-DSS 经验", True, True, "Must have hands-on experience with PCI-DSS"),
])
assert r["mandatory_gap_count"] == 2 and r["cognitive_match"] == 0.3, r
print("two mandatory gaps cap to 0.3 ok")

# 三条以上不再往下掉，但也不回升
r = run([
    item("a", True, True, "PMP certification is required"),
    item("b", True, True, "Must have hands-on experience with PCI-DSS"),
    item("c", True, True, "8+ years of product management experience"),
])
assert r["mandatory_gap_count"] == 3 and r["cognitive_match"] == analyzer.MANDATORY_GAP_CAP_FLOOR
print("three or more stays at floor ok")

# ---- 3. ★核心★ evidence 核验不过 → 不封顶（fail-open）
# 模型声称这是强制要求，但给的"原话"在 JD 里根本不存在
r = run([item("Kubernetes 经验", True, True, "Kubernetes experience is mandatory")])
assert r["mandatory_gap_count"] == 0, "JD 里没有的原话不能算数"
assert r["cognitive_match"] == 0.85, "核验不过就该退回原值，而不是误杀"
assert r["requirement_items"][0]["is_mandatory"] is False, "核验不过要把标注改回 False"
assert r["score_adjustments"] == []
print("unverifiable evidence fails open ok")

# 转述而不是照抄，也算核验不过——这正是要防的：模型把 preferred 说成 required
r = run([item("SQL", True, True, "SQL 是必须掌握的技能")])
assert r["mandatory_gap_count"] == 0 and r["cognitive_match"] == 0.85
print("paraphrased evidence rejected ok")

# 标了 is_mandatory 但没给 evidence
r = run([item("PMP 认证", True, True, "")])
assert r["mandatory_gap_count"] == 0 and r["cognitive_match"] == 0.85
print("missing evidence fails open ok")

# ---- 4. 归一化差异要吸收：模型照抄时的大小写以外的排版偏差不该导致核验失败
r = run([item("PMP 认证", True, True, "PMP  certification　is required")])  # 多空格 + 全角空格
assert r["mandatory_gap_count"] == 1 and r["cognitive_match"] == 0.5
print("evidence normalization ok")

# ---- 5. 强制要求但**没有缺口**（简历覆盖了）→ 不封顶
r = run([item("PMP 认证", False, True, "PMP certification is required")])
assert r["mandatory_gap_count"] == 0 and r["cognitive_match"] == 0.85
assert r["requirement_items"][0]["is_mandatory"] is True, "核验通过的标注要保留，只是没缺口"
print("covered mandatory requirement does not cap ok")

# ---- 6. 非强制的缺口不封顶（普通未达标要求很常见，封了就全员 0.3 了）
r = run([item("SQL", True, False, "")])
assert r["mandatory_gap_count"] == 0 and r["cognitive_match"] == 0.85
print("non-mandatory gap does not cap ok")

# ---- 7. 模型自己已经打得比上限还低 → 不要反向抬高
r = run([item("PMP 认证", True, True, "PMP certification is required")], cognitive=0.2)
assert r["cognitive_match"] == 0.2, "封顶是上限，不是赋值"
assert r["score_adjustments"] == []
print("cap never raises a lower score ok")

# ---- 8. 缺字段/脏数据不能把主流程搞挂（模型偶尔漏字段是常态）
r = analyzer.apply_score_rules(
    {"requirement_items": [{"text": "x", "is_gap": True}], "cognitive_match": 0.9}, JD
)
assert r["mandatory_gap_count"] == 0 and r["cognitive_match"] == 0.9
assert analyzer.apply_score_rules({"cognitive_match": 0.7}, JD)["cognitive_match"] == 0.7
assert analyzer.apply_score_rules({"requirement_items": "坏数据", "cognitive_match": 0.7}, JD)
assert analyzer.apply_score_rules(
    {"requirement_items": [None, "x"], "cognitive_match": 0.7}, JD
)["mandatory_gap_count"] == 0
# JD 拿不到时（refetch 失败的职位）：没有原文可核验，一律 fail-open
r = run([item("PMP 认证", True, True, "PMP certification is required")], jd=None)
assert r["mandatory_gap_count"] == 0 and r["cognitive_match"] == 0.85
print("malformed input never breaks the pipeline ok")

# ---- 9. overall_match 必须基于封顶**之后**的分数算（顺序反了封顶就白做）
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analyzer.py"),
           encoding="utf-8").read()
rules_pos = src.index("apply_score_rules(result, jd_text)")
overall_pos = src.index('result["overall_match"] = round(')
assert rules_pos < overall_pos, "apply_score_rules 必须在算 overall_match 之前调用"
# prompt 里那条规则文字要保留：代码只是兜底，仍然希望模型自己打对
assert "cognitive_match 最高不能超过0.5" in analyzer.PROMPT_TEMPLATE
assert "mandatory_evidence" in analyzer.PROMPT_TEMPLATE
print("rule application order ok")

print("\nALL PASS")
