# newjob — 每日自动职位搜索

本地运行的小型 Web 程序，按你设置的关键字每天定时自动搜索 Indeed 和 LinkedIn 上的新职位，去重后存入一个"待审核"列表，供你后续拿去用 `jd-resume-matcher` 技能做完整的匹配度分析。

## 重要说明 / 边界

- **Indeed**：通过 [python-jobspy](https://github.com/speedyapply/JobSpy) 调用其公开搜索接口抓取。
- **LinkedIn**：LinkedIn 没有公开的职位搜索 API，这里用 python-jobspy 做**非官方抓取**（绕过登录墙）。这可能违反 LinkedIn 的服务条款，存在账号/IP 被限流或封禁的风险。这是你在需求确认时明确选择接受的方案，如果之后想改成"仅手动粘贴 LinkedIn JD"（更保守、零风险），告诉我随时可以关掉 LinkedIn 这个搜索源（设置页把 `sites` 里的 `linkedin` 去掉即可，或直接编辑 `config.json`）。
- 本程序**不会**自动写入 `JD匹配追踪表.xlsx`，也**不会**调用任何 AI 自动打分——只做「搜索 → 去重 → 存入待审核列表」。追踪表的写入和匹配度分析仍然通过 Claude + `jd-resume-matcher` 技能完成，两者用同一份追踪表路径做去重，不会重复收录已经分析过的职位。
- 只能本地运行（你自己电脑开着的时候才会定时执行）。关机/程序退出后不会补跑错过的当天任务。

## 安装

```bash
cd newjob
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 运行

```bash
python app.py
```

打开浏览器访问 `http://127.0.0.1:5050`。

首次打开后，在页面里：

1. 填写关键词（每行一个，如 `Senior Product Manager` / `AI产品经理`）。
2. 填写想搜索的城市（每行一个；留空一行表示不限地点/远程）。
3. 可选：填写现有 `JD匹配追踪表.xlsx` 的完整本地路径，用于跟已分析过的职位去重（不会写这个文件，只读）。
4. 设置每天自动搜索的时间。
5. 点击"保存设置"。
6. 想立刻测试效果，点"立即搜索一次"。

只要这个程序开着（`python app.py` 进程存活），就会按设置的时间每天自动跑一次，把新职位加进"待审核新职位"列表。

## 后续流程

在"待审核新职位"列表里看到感兴趣的职位后，把职位链接发给 Claude（配合 `jd-resume-matcher` 技能），照常做匹配度分析、生成简历/cover letter、写入追踪表。分析完可以在列表里点"已看过"或"忽略"标记状态。

## 文件说明

- `app.py`：Flask 服务 + API。
- `scraper.py`：单次搜索逻辑（调用 python-jobspy，按关键词×城市循环，去重后入库）。
- `scheduler.py`：APScheduler 定时任务。
- `models.py`：SQLite 存储（`jobs.db`，运行后自动生成，已加入 .gitignore）。
- `tracker_xlsx.py`：只读追踪表，用于去重。
- `config.py` / `config.json`：搜索配置（`config.json` 首次运行自动生成，已加入 .gitignore，因为里面可能含本地文件路径）。
- `templates/`, `static/`：前端页面。
