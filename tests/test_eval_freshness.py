"""evals/check_freshness.py 的 stale_prompts()：免费、无 LLM、无网络。

用真实 analyzer prompts 算指纹，不写临时文件——previous 直接传 dict（stale_prompts
的 previous 参数就是为测试留的口子）。"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evals"))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from check_freshness import stale_prompts  # noqa: E402
from run_analyzer_eval import _prompt_fingerprints  # noqa: E402

# 没有历史记录：不算过期（"从没跑过"不是"过期"，不该报）
assert stale_prompts(None) == []
assert stale_prompts({}) == []

# 指纹一致：不过期
assert stale_prompts({"run": {"prompt_fingerprints": _prompt_fingerprints()}}) == []

# 指纹不一致：报出具体哪个 prompt 过期
fp = _prompt_fingerprints()
fp["PROMPT_TEMPLATE"] = "deadbeef0000"
assert stale_prompts({"run": {"prompt_fingerprints": fp}}) == ["PROMPT_TEMPLATE"]

# 历史记录里缺指纹字段（schema 漂移/老版本）：三个 prompt 全部算不一致
changed = stale_prompts({"run": {"prompt_fingerprints": {}}})
assert set(changed) == {"PROMPT_TEMPLATE", "MATERIALS_PROMPT", "COMPANY_ORIGIN_PROMPT"}, changed

print("ALL PASS")
