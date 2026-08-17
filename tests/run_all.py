"""一次性跑完所有回归测试。

用法（项目根目录下）：
    .venv/Scripts/python.exe tests/run_all.py

每个测试脚本都在独立子进程里跑——它们都会 monkeypatch llm.chat、改写
config.DB_PATH 并 import app，放在同一个进程里会互相污染。
"""
import os
import subprocess
import sys

# 本进程的 stdout 在 Windows 上默认是 GBK，转发子进程里的中文会变成乱码
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = ["test_llm.py", "test_prep.py", "test_frontend.py", "test_bank.py", "test_bank_chat.py",
         "test_resume.py", "test_job_detail.py", "test_dismiss_abort.py", "test_add_by_url.py"]
JS_FILES = ["static/common.js", "static/interview.js", "static/bank.js", "static/app.js",
            "static/resume.js", "static/job_detail.js"]


def run(cmd, cwd=BASE):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main():
    failed = []

    for name in TESTS:
        path = os.path.join(BASE, "tests", name)
        print(f"{'=' * 60}\n== {name}\n{'=' * 60}")
        # -X utf8：Windows 上默认按 GBK 输出，中文断言信息会变成乱码
        r = run([sys.executable, "-X", "utf8", path])
        out = (r.stdout or "") + (r.stderr or "")
        print(out.rstrip())
        if r.returncode != 0 or "ALL PASS" not in out:
            failed.append(name)
            print(f"-- FAILED (exit {r.returncode})")
        print()

    # JS 语法检查——node 没装就跳过，不算失败
    print(f"{'=' * 60}\n== javascript syntax\n{'=' * 60}")
    for js in JS_FILES:
        r = run(["node", "--check", js])
        if r.returncode == 0:
            print(f"  ok  {js}")
        else:
            print(f"  FAIL {js}\n{(r.stderr or r.stdout).rstrip()}")
            failed.append(js)
    print()

    print("=" * 60)
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print(f"ALL PASS ({len(TESTS)} suites + {len(JS_FILES)} js files)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError as e:
        # node 不在 PATH 上时给个明确提示，而不是丢一个裸 traceback
        print(f"缺少可执行文件：{e}")
        sys.exit(1)
