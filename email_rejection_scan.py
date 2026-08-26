"""邮件拒信扫描——本地数据库读写这一半。

Gmail 搜索/读信/判断"是不是拒信"这一半不在这个脚本里，是 Claude Code 在对话里用
Gmail MCP 连接器完成的（这个项目本身没有独立的 Gmail API 凭证，见 spec/roadmap.md
"邮件拒信自动识别"条目的方案取舍）。这个脚本只负责三头本地操作：

    list-applied   列出当前"已投递"的职位（company/title/job_id），给 Claude 知道
                   该去邮箱里搜哪些公司
    apply          确认后批量落库：把选中的职位改成 application_status='rejected'，
                   并把邮件证据（主题/摘要）记成一条备注，跟其它状态变更一样留痕。
                   用于人在 Claude Code 对话里当场确认的交互式扫描。
    queue          把疑似拒信排进"待确认"队列，不直接改状态——用于本机计划任务每天
                   跑的无人值守扫描（见 README"邮件拒信自动识别"），没有人在对话里
                   当场确认，所以先落 pending_rejections 表，人工上网页确认/忽略后
                   才会真的改 application_status。
    record-run     一整轮扫描跑完后记一笔时间戳，供网页"每日任务清单"算"该不该提醒
                   你去查一次邮箱"（间隔天数见设置页 email_scan_interval_days）

用法：
    python email_rejection_scan.py list-applied
    python email_rejection_scan.py apply --file confirmed.json
    python email_rejection_scan.py queue --file candidates.json
    python email_rejection_scan.py record-run --checked 18 --rejections 0

confirmed.json / candidates.json 格式相同：
    [{"job_id": 123, "note": "Gmail 2026-08-20 来信主题《...》：..."}, ...]
"""
import argparse
import json
import sys

import models


def cmd_list_applied(args):
    jobs = models.list_applied_jobs()
    print(json.dumps(jobs, ensure_ascii=False, indent=2))


def cmd_apply(args):
    with open(args.file, "r", encoding="utf-8") as f:
        items = json.load(f)
    updated = []
    for item in items:
        job_id = item["job_id"]
        models.set_application_status(job_id, "rejected")
        note = item.get("note") or "邮件扫描识别为拒信"
        models.add_job_note(job_id, note, source="email_scan")
        updated.append(job_id)
    print(json.dumps({"updated": updated}, ensure_ascii=False))


def cmd_queue(args):
    with open(args.file, "r", encoding="utf-8") as f:
        items = json.load(f)
    queued = []
    for item in items:
        pending_id = models.add_pending_rejection(item["job_id"], item.get("note") or "邮件扫描识别为拒信")
        queued.append(pending_id)
    print(json.dumps({"queued": queued}, ensure_ascii=False))


def cmd_record_run(args):
    models.record_email_scan_run(args.checked, args.rejections)
    print(json.dumps({"ok": True}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-applied", help="列出当前已投递的职位")
    p_list.set_defaults(func=cmd_list_applied)

    p_apply = sub.add_parser("apply", help="确认后批量标记为已拒绝")
    p_apply.add_argument("--file", required=True, help="确认列表 JSON 文件路径")
    p_apply.set_defaults(func=cmd_apply)

    p_queue = sub.add_parser("queue", help="把疑似拒信排进待确认队列（无人值守扫描用）")
    p_queue.add_argument("--file", required=True, help="候选列表 JSON 文件路径")
    p_queue.set_defaults(func=cmd_queue)

    p_record = sub.add_parser("record-run", help="记一次扫描已完成（用于每日任务清单提醒）")
    p_record.add_argument("--checked", type=int, required=True, help="这次扫描检查了多少条已投递职位")
    p_record.add_argument("--rejections", type=int, required=True, help="确认为拒信、已落库的条数")
    p_record.set_defaults(func=cmd_record_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
