"""evals/verdicts.py 的状态判定逻辑。纯函数，不调 LLM、不碰 DB、不 import evals 里
任何其它模块。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals"))

import verdicts as v

# ---- 1. worse_of 覆盖所有两两组合：ERROR > FAIL > INCONCLUSIVE > WARN > PASS
order = [v.ERROR, v.FAIL, v.INCONCLUSIVE, v.WARN, v.PASS]
for i, worse in enumerate(order):
    for better in order[i:]:
        assert v.worse_of(worse, better) == worse, (worse, better)
        assert v.worse_of(better, worse) == worse, (better, worse)
print("worse_of covers all pairs ok")

# 自反
for s in order:
    assert v.worse_of(s, s) == s
print("worse_of reflexive ok")

# ---- 2. worst_of
assert v.worst_of([v.PASS, v.PASS]) == v.PASS
assert v.worst_of([v.PASS, v.WARN, v.FAIL]) == v.FAIL
assert v.worst_of([v.FAIL, v.ERROR, v.PASS]) == v.ERROR
assert v.worst_of([]) == v.PASS, "空输入（没有任何 check）应该是 PASS，不是报错"
print("worst_of ok")

# ---- 3. soft 降级表：只有 FAIL 会变成 WARN，ERROR 绝不降级
assert v.apply_soft(v.FAIL) == v.WARN
assert v.apply_soft(v.ERROR) == v.ERROR, "软 fixture 全挂了仍是没测成，不能降级成弱信号"
assert v.apply_soft(v.PASS) == v.PASS
assert v.apply_soft(v.INCONCLUSIVE) == v.INCONCLUSIVE
assert v.apply_soft(v.WARN) == v.WARN, "已经是 WARN 的不该再变"
print("apply_soft downgrade table ok")

# ---- 4. summarize
assert v.summarize([]) is None
assert v.summarize([None, None]) is None, "全是 None 等价于没数据"
s = v.summarize([0.9])
assert s == {"n": 1, "mean": 0.9, "min": 0.9, "max": 0.9, "range": 0.0}
s = v.summarize([0.9, 0.85, None])  # None 要被过滤掉，不能拉低 n
assert s["n"] == 2
assert abs(s["mean"] - 0.875) < 1e-9
assert s["min"] == 0.85 and s["max"] == 0.9
assert abs(s["range"] - 0.05) < 1e-9
print("summarize ok")

# format_summary 不炸、None 有专门文案
assert "无数据" in v.format_summary(None)
assert "n=2" in v.format_summary(v.summarize([0.9, 0.85]))
print("format_summary ok")

# ---- 5. exit_code_for
assert v.exit_code_for([{"status": v.PASS}, {"status": v.WARN}]) == v.EXIT_CLEAN
assert v.exit_code_for([{"status": v.PASS}, {"status": v.INCONCLUSIVE}]) == v.EXIT_CLEAN
assert v.exit_code_for([{"status": v.PASS}, {"status": v.ERROR}]) == v.EXIT_ERROR
assert v.exit_code_for([{"status": v.ERROR}, {"status": v.FAIL}]) == v.EXIT_FAIL, "FAIL 优先于 ERROR"
assert v.exit_code_for([]) == v.EXIT_CLEAN
print("exit_code_for ok")

print("\nALL PASS")
