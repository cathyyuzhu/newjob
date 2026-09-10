"""免费、无 LLM、无网络：检查 evals/results/latest.json 记录的 prompt 指纹是不是
跟当前代码一致。用于"我改完 prompt 了，上次那份全绿的 eval 结果还作数吗"这个问题
——改一个字都会让指纹变化，是判断"该不该重新花钱跑一次"的最快方式。

用法（项目根目录下）：
    .venv/Scripts/python.exe evals/check_freshness.py

exit 0 = 指纹一致（或者压根没有历史记录）；exit 1 = 指纹不一致，上次结果针对的
是旧版 prompt。

接入 tests/run_all.py 的方式（2026-08-30）：默认 **advisory**——每次跑测试套件都会
附带执行本脚本，指纹不一致时打印醒目提醒但不失败。刻意不做硬失败：那套测试必须
保持免费常绿，而只要动一个字的 PROMPT_TEMPLATE 就会报"不一致"，不能让"正在改
prompt、还没跑 eval"这个完全正常的中间状态把日常测试搞红。需要强制时用
`tests/run_all.py --check-fresh`——发布/提交前把过期从"提醒"升级为"失败"。
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


def stale_prompts(previous=None):
    """比对上次 eval 结果记录的 prompt 指纹和当前代码，返回已变化的 prompt 名列表。

    没有历史记录（还没跑过一次带结果记录的 eval）返回空列表——"从没跑过"不是
    "过期"，不该在这里报。previous 可传入已加载的 latest.json 内容，供测试复用，
    免得每个用例都要写临时文件。"""
    if previous is None:
        previous = load_results_json(RESULTS_PATH)
    if not previous:
        return []
    current_fp = _prompt_fingerprints()
    prev_fp = (previous.get("run") or {}).get("prompt_fingerprints") or {}
    return [name for name, fp in current_fp.items() if prev_fp.get(name) != fp]


def main():
    previous = load_results_json(RESULTS_PATH)
    if not previous:
        print(f"没有找到 {RESULTS_PATH}，还没跑过一次带结果记录的 eval，无从比较。")
        return 0

    changed = stale_prompts(previous)
    finished_at = (previous.get("run") or {}).get("finished_at", "未知时间")
    if not changed:
        print(f"指纹一致：{RESULTS_PATH} 是针对当前 prompt 跑的（上次运行于 {finished_at}）。")
        return 0

    current_fp = _prompt_fingerprints()
    prev_fp = (previous.get("run") or {}).get("prompt_fingerprints") or {}
    print(f"⚠ 指纹不一致：{RESULTS_PATH}（跑于 {finished_at}）是针对旧版 prompt 跑的：")
    for name in changed:
        print(f"    {name}: {prev_fp.get(name)} -> {current_fp.get(name)}")
    print("需要重新跑一次 evals/run_analyzer_eval.py 才能知道当前 prompt 的真实状态。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
