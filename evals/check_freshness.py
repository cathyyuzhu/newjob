"""免费、无 LLM、无网络：检查 evals/results/latest.json 记录的 prompt 指纹是不是
跟当前代码一致。用于"我改完 prompt 了，上次那份全绿的 eval 结果还作数吗"这个问题
——改一个字都会让指纹变化，是判断"该不该重新花钱跑一次"的最快方式。

用法（项目根目录下）：
    .venv/Scripts/python.exe evals/check_freshness.py

exit 0 = 指纹一致（或者压根没有历史记录）；exit 1 = 指纹不一致，上次结果针对的
是旧版 prompt。

刻意不接入 tests/run_all.py：那套测试必须保持免费常绿，而这里只要动一个字的
PROMPT_TEMPLATE 就会报"不一致"——不能让"正在改 prompt、还没跑 eval"这个完全
正常的中间状态把日常的免费测试套件搞红。留成手动运行，或者以后接进 pre-commit
钩子（这次没做，只是留了这个可能性）。
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
sys.path.insert(0, BASE)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from run_analyzer_eval import RESULTS_PATH, _prompt_fingerprints, load_results_json  # noqa: E402


def main():
    previous = load_results_json(RESULTS_PATH)
    if not previous:
        print(f"没有找到 {RESULTS_PATH}，还没跑过一次带结果记录的 eval，无从比较。")
        return 0

    current_fp = _prompt_fingerprints()
    prev_fp = (previous.get("run") or {}).get("prompt_fingerprints") or {}
    changed = [name for name, fp in current_fp.items() if prev_fp.get(name) != fp]

    finished_at = (previous.get("run") or {}).get("finished_at", "未知时间")
    if not changed:
        print(f"指纹一致：{RESULTS_PATH} 是针对当前 prompt 跑的（上次运行于 {finished_at}）。")
        return 0

    print(f"⚠ 指纹不一致：{RESULTS_PATH}（跑于 {finished_at}）是针对旧版 prompt 跑的：")
    for name in changed:
        print(f"    {name}: {prev_fp.get(name)} -> {current_fp.get(name)}")
    print("需要重新跑一次 evals/run_analyzer_eval.py 才能知道当前 prompt 的真实状态。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
