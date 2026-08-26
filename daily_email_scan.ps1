# 无人值守邮件拒信扫描——Windows 计划任务每天调用这个脚本，跑一次 headless Claude Code
# 会话完成"查已投递清单 -> 搜 Gmail -> 排进待确认队列"。见 README.md「无人值守每天自动
# 扫描」和 spec/roadmap.md「邮件拒信自动识别——无人值守每日扫描（2026-08-24）」。
#
# 关键约束：这个 prompt 明确只允许调用 email_rejection_scan.py 的 list-applied / queue /
# record-run 三个子命令，绝不能调用 apply——无人值守场景没有人当场确认，疑似拒信必须先进
# pending_rejections 待确认队列，等用户上网页点"确认"/"忽略"才会真的改状态。
# --dangerously-skip-permissions 是因为计划任务没人盯着、没法逐条允许 Gmail 连接器和 Bash
# 调用，所以这条"不能 apply"的约束只能靠 prompt 本身、不是权限系统挡住。

Set-Location $PSScriptRoot

$prompt = @'
你现在是无人值守的每日邮件拒信扫描任务，没有人在旁边确认，请严格按下面步骤执行，不要跳步、不要做步骤之外的事（不要提交代码、不要改动这几个命令之外的任何文件）：

1. 执行 `.venv\Scripts\python.exe email_rejection_scan.py list-applied`，拿到当前"已投递"状态的职位清单（company/title/job_id）。
2. 如果清单为空，直接执行 `.venv\Scripts\python.exe email_rejection_scan.py record-run --checked 0 --rejections 0`，然后结束，不用往下做。
3. 如果清单不为空，用 Gmail 连接器按每个职位的公司名搜索最近 14 天的邮件，读正文判断是不是拒信。判断要保守：措辞含糊、不确定是不是拒信的（比如"我们还在看其他候选人""感谢你的耐心"这类，没有明确说"不再推进"/"另择他人"/"职位已招满"的），一律不算，宁可漏判也不要误判。
4. 把你判断为拒信的职位整理成 JSON 数组，格式 `[{"job_id": 123, "note": "Gmail <日期> 来信主题《...》：<一两句摘要>"}, ...]`，写到一个临时文件（比如 `%TEMP%\pending_rejections_<日期>.json`）。
5. 执行 `.venv\Scripts\python.exe email_rejection_scan.py queue --file <上一步的文件路径>`，把这些候选排进待确认队列——**绝对不要执行 `apply` 子命令**，queue 不会改职位状态，只有用户之后上网页手动确认才会真的改。
6. 执行 `.venv\Scripts\python.exe email_rejection_scan.py record-run --checked <第1步清单条数> --rejections <第4步识别出的候选条数>`，记一笔本次扫描时间戳。
'@

claude -p $prompt --dangerously-skip-permissions
