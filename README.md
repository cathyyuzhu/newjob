# newjob — 每日自动职位搜索 + 自动匹配分析

本地运行的小型 Web 程序：

1. 按你设置的关键字每天定时自动搜索 Indeed 和 LinkedIn 上的新职位，去重后存入"待审核"列表。
2. 对待审核列表里的任意一条职位，点击"自动分析"，会调用 Claude API 完整走一遍 `jd-resume-matcher` 技能的双因子匹配度分析，写入 `JD匹配追踪表.xlsx`；匹配度≥70%时还会自动生成一份定制简历（docx）和 cover letter。

## 重要说明 / 边界

- **Indeed**：通过 [python-jobspy](https://github.com/speedyapply/JobSpy) 调用其公开搜索接口抓取。
- **LinkedIn**：LinkedIn 没有公开的职位搜索 API，这里用 python-jobspy 做**非官方抓取**（绕过登录墙）。这可能违反 LinkedIn 的服务条款，存在账号/IP 被限流或封禁的风险。这是你在需求确认时明确选择接受的方案，如果之后想改成"仅手动粘贴 LinkedIn JD"（更保守、零风险），把设置页里 `sites` 的 `linkedin` 去掉即可（或直接编辑 `config.json`）。
- **自动分析会产生 Anthropic API 调用费用**，且不是自动触发的——只有你在网页上点了某条职位的"自动分析"按钮才会调用，定时搜索本身不会自动分析。
- 只能本地运行（你自己电脑开着的时候才会定时执行）。关机/程序退出后不会补跑错过的当天任务。

## 安装

```bash
cd newjob
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 配置 Anthropic API Key（自动分析功能必需）

自动分析功能需要你自己的 Anthropic API key（在 https://console.anthropic.com 申请），通过环境变量传入，不写进 `config.json`：

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxx   # Windows: set ANTHROPIC_API_KEY=sk-ant-xxxxx
```

不配置这个环境变量的话，定时搜索、待审核列表这些功能照常可用，只是点"自动分析"会报错提示未设置。

## 运行

```bash
python app.py
```

打开浏览器访问 `http://127.0.0.1:5050`。

首次打开后，在页面里：

1. 填写关键词（每行一个，如 `Senior Product Manager` / `AI产品经理`）。
2. 填写想搜索的城市（每行一个；留空一行表示不限地点/远程）。
3. 填写 `JD匹配追踪表.xlsx` 的完整本地路径（留空则默认 `~/Downloads/JD匹配追踪表.xlsx`）——既用于去重，也是自动分析结果的写入目标，和 `jd-resume-matcher` 技能共用同一份文件。
4. 填写基础简历 docx 的完整路径（留空则默认 `~/Downloads/Cathy_Yang_Resume_EN_AI.docx`），以及定制简历想保存到哪个目录（留空则跟基础简历同目录）。
5. 设置每天自动搜索的时间。
6. 点击"保存设置"。
7. 想立刻测试搜索效果，点"立即搜索一次"。

只要这个程序开着（`python app.py` 进程存活），就会按设置的时间每天自动跑一次，把新职位加进"待审核新职位"列表。

## 自动分析怎么用

在"待审核新职位"列表里，看到感兴趣的职位，点它那一行的"自动分析"按钮：

- 程序会把该职位的 JD 正文（搜索时已经抓到）和你的基础简历一起发给 Claude API，按 `jd-resume-matcher` 技能里定义的双因子模型（认知要求匹配度50% + 工作内容匹配度50%）计算总体匹配度。
- 结果会写入追踪表 xlsx 新的一行（在最上面），包含对比表全部字段。
- 如果匹配度 ≥70% 且判定需要优化简历，会基于基础简历生成一份定制版 docx（改写措辞、突出相关经验，不编造未发生的经历），保存到设置的目录，并同时生成配套的 cover letter 写入追踪表。
- 分析完成后该职位在网页列表里会显示匹配度百分比；失败会显示"失败"，鼠标悬停能看到具体错误信息。

分析可能需要几十秒（LLM 调用+处理JD正文长度视情况而定），期间按钮不会重复触发，等页面状态更新即可。

## 文件说明

- `app.py`：Flask 服务 + API。
- `scraper.py`：单次搜索逻辑（调用 python-jobspy，按关键词×城市循环，抓取JD正文，去重后入库）。
- `scheduler.py`：APScheduler 定时任务。
- `models.py`：SQLite 存储（`jobs.db`，运行后自动生成，已加入 .gitignore）。
- `tracker_xlsx.py`：只读追踪表工具，用于搜索阶段的去重判断。
- `tracker_utils.py`：追踪表写入工具（复用自 `jd-resume-matcher` 技能的 `scripts/tracker_utils.py`，保证格式/列结构完全一致）。
- `resume_docx.py`：读取基础简历文本、按段落索引生成定制版简历 docx。
- `analyzer.py`：封装对 Claude API 的调用，构造 prompt、解析双因子匹配度分析结果。
- `pipeline.py`：把"取JD → 调用analyzer → 视匹配度生成简历/cover letter → 写入追踪表 → 更新任务状态"串起来的编排逻辑。
- `config.py` / `config.json`：搜索与分析配置（`config.json` 首次运行自动生成，已加入 .gitignore，因为里面可能含本地文件路径）。
- `templates/`, `static/`：前端页面。
