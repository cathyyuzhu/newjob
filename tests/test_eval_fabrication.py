"""evals/checks_fabrication.py 的编造数字检测，以及 evals/run_analyzer_eval.py 的
evaluate_materials() 判定逻辑。全程手写假数据，不调 LLM、不碰 DB、不发网络请求。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evals"))

import checks_fabrication as cf
import verdicts as v
from run_analyzer_eval import evaluate_materials

RESUME_SENIOR_TEXT = (
    "[0] 张三\n"
    "[1] 产品经理 | 8年工作经验\n"
    "[2] 工作经历\n"
    "[3] ABC科技有限公司 高级产品经理 2019-至今\n"
    "[4] 主导企业级SaaS平台从0到1建设，服务超过200家企业客户，年营收贡献增长35%\n"
    "[5] 负责产品路线图规划、跨部门协作（研发/销售/客户成功团队20+人）、季度OKR制定\n"
    "[6] 主导3次产品重大改版，通过A/B测试等用户增长实验将核心功能留存率提升18%\n"
    "[7] XYZ互联网公司 产品经理 2016-2019\n"
    "[8] 负责toB企业协作工具的需求分析、竞品分析、原型设计\n"
    "[9] 与研发团队紧密协作完成20+个功能迭代，累计服务用户超过50万\n"
)

# ---- 1. extract_figures 在真实 ground truth 上应该全部命中
figs = cf.extract_figures(RESUME_SENIOR_TEXT)
for expect in ("200", "200家", "35", "35%", "18", "18%", "20+", "20+人", "50万", "3", "8"):
    assert expect in figs, f"应该抓到 {expect}，实际集合：{figs}"
print("extract_figures finds all ground-truth figures ok")

# ---- 2. allowed_figures 不会被 [N] 索引号污染（正则回归：曾经把 4/5/6/7 这些
# 索引数字误当成"简历真实提到过"，导致同样是小个位数的编造内容也能蒙混过关）
allowed = cf.allowed_figures(RESUME_SENIOR_TEXT)
assert cf.find_new_figures("负责4个核心项目，管理6人团队", allowed) == ["4", "4个", "6", "6人"], \
    "4/6 是段落索引号，不是简历真实数字，不该被当成许可集里的合法值"
print("allowed_figures not polluted by [N] index markers ok")

# ---- 2b. 回归：数字嵌在缩写/型号里不该被当成量化指标（2026-08-30 首次真实基线
# 跑出来的假阳性——cover letter 提到"B2B tools"，"2" 被当成新数字报了出来）
assert cf.extract_figures("with B2B tools") == set(), "B2B 里的 2 不是量化指标"
assert cf.extract_figures("GPT4 and iPhone15") == set(), "型号里的数字不是量化指标"
assert "4" in cf.extract_figures("提升了4%的转化率"), "紧跟单位（%）的数字仍要正常识别"
assert "20+" in cf.extract_figures("管理20+人团队"), "紧跟已承认单位（+）的数字仍要正常识别"
print("extract_figures ignores digits embedded in abbreviations/model names ok")

# ---- 3. 干净改写（数字原样保留或换单位形态）不报编造
clean = "主导企业级SaaS平台建设，服务超过200家企业客户，营收增长35%"
assert cf.find_new_figures(clean, allowed) == []
print("clean rewrite reports no new figures ok")

# ---- 4. 编造的数字被抓出来
fabricated = "服务超过1000家企业客户，年营收增长80%"
new = cf.find_new_figures(fabricated, allowed)
assert set(new) == {"1000", "1000家", "80", "80%"}, new
print("fabricated figures detected ok")

# ---- 5. evaluate_materials：干净改写 -> PASS
clean_reps = [{
    "ok": True,
    "result": {
        "needs_customization": True,
        "resume_paragraph_edits": [
            {"index": 4, "original": "...", "text": clean, "applicable": True},
        ],
        "resume_paragraph_edits_dropped": [],
        "cover_letter": "I have a strong background in product management.",
    },
}]
fixture = {"id": "materials_no_fabrication", "resume_text": RESUME_SENIOR_TEXT, "soft": True}
status, detail = evaluate_materials(fixture, clean_reps)
assert status == v.PASS, (status, detail)
print("evaluate_materials clean rewrite -> PASS ok")

# ---- 6. evaluate_materials：编造数字 -> FAIL（fixture 是 soft，main() 才做降级，
# evaluate_materials 本身返回未降级的 raw 判定）
fab_reps = [{
    "ok": True,
    "result": {
        "needs_customization": True,
        "resume_paragraph_edits": [
            {"index": 4, "original": "...", "text": fabricated, "applicable": True},
        ],
        "resume_paragraph_edits_dropped": [],
        "cover_letter": "I have a strong background in product management.",
    },
}]
status, detail = evaluate_materials(fixture, fab_reps)
assert status == v.FAIL, (status, detail)
assert any("1000" in d for d in detail), detail
print("evaluate_materials fabricated figures -> FAIL ok")

# ---- 7. evaluate_materials：模型什么都没改 -> INCONCLUSIVE（没东西可查，不是 PASS）
empty_reps = [{
    "ok": True,
    "result": {
        "needs_customization": False,
        "resume_paragraph_edits": [],
        "resume_paragraph_edits_dropped": [],
        "cover_letter": "A generic cover letter.",
    },
}]
status, detail = evaluate_materials(fixture, empty_reps)
assert status == v.INCONCLUSIVE, (status, detail)
print("evaluate_materials no edits produced -> INCONCLUSIVE ok")

# ---- 8. evaluate_materials：annotate_edits 静默丢弃的条目 -> 硬 FAIL，
# 即使 fixture 是 soft 也不能被降级掉（这个 check 是确定性的，不是启发式）
dropped_reps = [{
    "ok": True,
    "result": {
        "needs_customization": True,
        "resume_paragraph_edits": [
            {"index": 4, "original": "...", "text": clean, "applicable": True},
        ],
        "resume_paragraph_edits_dropped": [{"index": 5, "verdict": "original_mismatch"}],
        "cover_letter": "clean letter",
    },
}]
status, detail = evaluate_materials(fixture, dropped_reps)
assert status == v.FAIL, (status, detail)
assert any("dropped" in d.lower() or "丢弃" in d for d in detail), detail
print("evaluate_materials dropped edits -> hard FAIL ok")

# ---- 9. evaluate_materials：cover letter 编造跟简历编造是独立命名的 check
# （用不会在简历数字里出现的编号触发，同时简历改写保持干净）
letter_fab_reps = [{
    "ok": True,
    "result": {
        "needs_customization": True,
        "resume_paragraph_edits": [
            {"index": 4, "original": "...", "text": clean, "applicable": True},
        ],
        "resume_paragraph_edits_dropped": [],
        "cover_letter": "I closed 777 deals worth $999 million last year.",
    },
}]
status, detail = evaluate_materials(fixture, letter_fab_reps)
assert status == v.FAIL, (status, detail)
assert any("cover_letter" in d.lower() or "cover letter" in d.lower() for d in detail), detail
print("evaluate_materials cover-letter fabrication flagged separately ok")

print("\nALL PASS")
