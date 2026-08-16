# tech-solution.md — 技术方案

记录 `newjob` 的技术选型和主要设计决策的"为什么"。配合 [mission.md](mission.md)（为什么做）、[roadmap.md](roadmap.md)（功能状态）、[README.md](../README.md)（怎么用）一起看，这四份文档各自的定位见 `CLAUDE.md`。

## 技术栈总览

| 层 | 选型 | 说明 |
|---|---|---|
| 后端框架 | Flask (>=3.0.0) | 轻量、无需额外基础设施 |
| 抓取 | python-jobspy (>=1.1.79) | 统一封装 Indeed 公开接口 + LinkedIn 非官方抓取 |
| 定时任务 | APScheduler `BackgroundScheduler` (>=3.10.4) | 进程内 cron，随 Flask 进程启停 |
| 待审核队列存储 | SQLite（标准库 `sqlite3`，`jobs.db`） | 本机单进程小数据量，零配置 |
| 匹配结果追踪表 | openpyxl 读写 xlsx (>=3.1.2) | 复用已有的 `JD匹配追踪表.xlsx` 格式 |
| 简历生成 | python-docx (>=1.1.0) | 按段落索引改写定制简历 |
| LLM 调用层 | `llm.py` 统一封装两家 provider（`chat`/`chat_json`/`ask`/`ask_json`/`resolve`），支持多轮 `messages` + system prompt + 可调 `max_tokens` | Anthropic Claude API（`anthropic` SDK）或 DeepSeek API（`urllib` 直调，OpenAI 兼容接口），按 `llm_provider` 配置二选一 |
| AI 匹配分析 | `analyzer.py` | 复用 `jd-resume-matcher` 技能的 prompt/双因子模型 |
| 面试准备 | `interview.py`（prompt + LLM 调用）+ `pipeline.py`（编排）+ `interview_preps` / `interview_bank` 表 | 单职位准备材料复用已有匹配分析结论；通用题库跨职位复用、用户可编辑 |
| 前端 | 原生 HTML / CSS / JS | 无框架、无构建步骤 |
| LinkedIn Easy Apply 自动化 | Playwright (>=1.45.0)，`launch_persistent_context` + `channel="msedge"`（本机无 Chrome） | 驱动真实浏览器、真实登录会话，非 headless；见下方决策 |

## 架构 / 数据流

```
定时器 (scheduler.py, APScheduler cron)
        │
        ▼
run_search_once() (scraper.py)
        │  按 关键词 × 城市 循环调用 jobspy.scrape_jobs()
        │  去重: dedupe_key(company+title) 对照
        │        SQLite 已有记录 + tracker.xlsx 已有记录 (tracker_xlsx.py)
        ▼
SQLite jobs 表 (models.py, jobs.db) —— 待审核队列
        │
        │  搜索结束后自动触发（后台线程），或用户点某条职位的"AI 分析"手动触发
        ▼
analyze_pending_jobs() 批量 / analyze_and_record() 单条 (pipeline.py)
        │  1. 读基础简历文本 (resume_docx.py)
        │  2. 调 LLM 双因子匹配分析 (analyzer.py)
        │  3. 匹配度 >=70% 且需要定制 → 生成定制简历 docx (resume_docx.py)
        │  4. 写入 JD匹配追踪表.xlsx (tracker_utils.py，复用自 jd-resume-matcher 技能)
        │  5. 回写 SQLite: 匹配度 / 简历路径 / 报错（不动审核状态 new/reviewed/dismissed）
        ▼
网页 /api/jobs、/api/tracker 读出最新状态展示
        │
        │  用户把投递状态改成"面试中"时自动触发（后台线程），
        │  或在职位详情弹窗的「面试准备」tab 里手动点"重新生成"
        ▼
generate_interview_prep() (pipeline.py)
        │  1. 读基础简历文本 (resume_docx.py)
        │  2. 从追踪表取回这条职位已有的匹配分析结论 (find_tracker_entry)
        │  3. 调 LLM 生成面试准备材料 (interview.py → llm.py)
        │  4. 写入 SQLite interview_preps 表（成功/失败都写一行）
        ▼
网页详情弹窗「面试准备」tab 读 /api/jobs/<id>/interview_prep 展示
```

`app.py` 是唯一的 Flask 入口，暴露 JSON API（`/api/config`、`/api/search/run`、`/api/jobs`、`/api/jobs/<id>/status`、`/api/jobs/<id>/starred`、`/api/jobs/<id>/analyze`、`/api/runs`、`/api/tracker`），`static/app.js` 用原生 `fetch` 调用，没有 SPA 框架、没有前端状态管理库。`app.run()` 开了 `threaded=True`（否则单次分析耗时 1~2 分钟期间会卡住整个开发服务器）。

## 关键决策

**前端为什么不用 React/Vue**
页面规模小（几个区块、几十个交互点），Flask 直接 `render_template` + 静态 JS 已经够用。省去构建工具链和 node 依赖，本地单人工具不需要为可维护性预先买框架的复杂度。

**为什么职位队列用 SQLite，匹配结果用 xlsx**
`JD匹配追踪表.xlsx` 是 `jd-resume-matcher` 技能已经在用的格式，`tracker_utils.py` 直接复用该技能的写入逻辑以保证列结构一致，不重新发明格式。SQLite 只管本项目独有的"待审核队列"这个临时状态。两者职责不重叠：SQLite = 工作队列，xlsx = 最终追踪记录。
（2026-08-16 补充：新增的"公司简介"列是本项目独有的扩展，`jd-resume-matcher` 技能原格式没有这一列。新列固定加在 `HEADERS` 末尾而不是插进中间，避免打乱已有文件的列位置；`_migrate_headers()` 让老文件首次写入时自动补上表头，不需要手动迁移。）

**LinkedIn 为什么用非官方抓取**
LinkedIn 没有公开的职位搜索 API。在"仅手动粘贴 JD"（零风险）和"jobspy 绕过登录墙抓取"（有 ToS 风险）之间，已明确选择接受后者的风险换自动化，见 [README.md](../README.md#重要说明--边界)。想改保守方案的话，把 `config.json` 的 `sites` 去掉 `linkedin` 即可，不用改代码。

**定时任务为什么用 APScheduler 而不是系统级 crontab / 任务计划程序**
需要跨 Windows/Mac 都能跑，且改了网页设置后 `reschedule()` 立即生效，不需要用户操作系统层面的定时任务。代价是必须保持 `python app.py` 进程常驻，关机/退出不会补跑——已知且接受的限制。

**为什么用 Claude/DeepSeek 这类云端 API 而不是本地模型做匹配分析**
匹配分析的 prompt 和双因子模型定义在 `jd-resume-matcher` 技能里（`analyzer.py` 的 `PROMPT_TEMPLATE` 两个供应商共用），保证和技能手动跑的结果口径一致；简历改写、cover letter 生成这类需要理解力的任务，本地模型效果不足以支撑。

**为什么 `cognitive_match` 打分要单独加"硬性门槛"规则，而不是只按"任职要求"列表本身反推总分（讨论于 2026-08-15）**
起因：用户审查一条分析结果（HSBC「Associate Director, Product Development」），发现"任职要求"11条全标红（无ITIL认证、无Product Owner title等），总体匹配度却有72.5%。排查后确认这是 prompt 设计的盲点——`overall_match = cognitive_match*0.5 + content_match*0.5`，这两个分数是LLM给的整体印象分，跟"任职要求"逐条是否达标（`is_gap`）是同一次LLM调用里两条独立判断逻辑，代码里从不读取 `requirement_items` 反过来算总分，所以会出现"硬要求全红但总分仍≥70%"的矛盾结果。没有改成代码里用 `is_gap` 数量做确定性扣分/封顶，是因为哪些要求算"硬性/强制"需要理解JD原文措辞（"required"/"must have"/"必须"等）和上下文，这类判断交给LLM在打分时一并考虑更省事，也不用改JSON输出结构；直接在 `analyzer.py` 的 `PROMPT_TEMPLATE` 里给 `cognitive_match` 打分规则加了一条：JD原文明确标注为强制性且 `is_gap=true` 的条目会把 `cognitive_match` 封顶在0.5，两条以上则封顶到0.3左右。

**为什么额外支持 DeepSeek，而不是只用 Claude**
DeepSeek API 价格比 Claude 低一个数量级（讨论于 2026-08-15），个人求职场景下分析成本从"几美元"降到"几毛钱"。`analyzer.py` 里两个供应商共用同一份 prompt 和 JSON 输出解析逻辑，只是发起请求的方式不同（Anthropic SDK vs. 直接调 DeepSeek 的 OpenAI 兼容 REST 接口），没有引入新的第三方 SDK 依赖。默认仍是 Anthropic——DeepSeek 分析质量（尤其是简历改写、cover letter 这类需要语言组织能力的部分）需要用户自己对比后再决定是否切换。

**为什么"待审核"职位改成自动分析，而不是保留手动逐条点击（讨论于 2026-08-15）**
用户明确要求不想逐条点"AI 分析"。改成每次搜索完自动把**这次搜索新增**的职位跑一遍分析（`scraper.py` 的 `run_search_once()` 返回 `new_job_ids`，传给 `pipeline.py` 的 `analyze_pending_jobs(job_ids=...)`）。代价是失去了"点了才花钱"的成本闸门——现在费用直接取决于每次搜索新增多少职位，见 README 的费用说明；为了不做无意义的调用，加了两层节流：(1) 职位标题跟搜索关键词完全不沾边的自动跳过、不调用LLM（`_title_looks_relevant()`），(2) 只分析本次新增，不会牵连数据库里历史积压的其它未分析职位（这个功能上线前积累的约300条历史"待审核"由启动时的补跑单独、限量处理，见下）。如果之后觉得费用不可控，退回手动模式只需要把 `scheduler.py`/`app.py` 里调用 `analyze_pending_jobs()` 的地方删掉，不用改其余代码。

**为什么启动时的历史积压补跑要限制条数（讨论于 2026-08-15）**
这个自动分析功能上线前，数据库里已经积压了约300条从未分析过的"待审核"职位——远超单次搜索通常新增的量。如果启动时不加限制地全部自动分析，会跑好几个小时（用户明确反对这种一次性长时间跑批，要求"先分析最近5条"）。所以启动时的补跑改成传 `limit=STARTUP_BACKLOG_LIMIT`（当前=5，`app.py`），且按 `first_seen` 倒序（最新的优先）——每次重启程序会再清理一批，想加快就多重启几次或者调大这个常量。日常搜索到的新职位不受此限制，因为量级天然小得多。

**为什么批量分析放独立后台线程而不是同步跑在请求里**
单条分析可能要 1~2 分钟，待审核里可能同时有几十条新职位。如果同步跑在 `/api/search/run` 请求里，"立即搜索一次"按钮会被卡住几分钟甚至更久；放 `threading.Thread(daemon=True)` 里跑，接口立刻返回找到/新增数，分析结果陆续写入 SQLite/xlsx，前端刷新即可看到——不需要引入 Celery/RQ 这类任务队列，本地单进程小工具用线程池外的裸线程已经够用。定时任务（APScheduler 的 cron）本身已经跑在后台线程里，不需要额外包一层。

**标题相关性粗筛怎么判断、为什么不更精确**
`pipeline.py` 的 `_title_looks_relevant()` 用职位标题和它入库时记录的搜索关键词（`job.keyword`）做词面 token 重合判断，完全不重合才跳过——纯字符串处理，不调用LLM，成本几乎为零。代价是不够精确：纯中英文混排的关键词（如"AI产品经理"）会被当成一个整体token，标题里如果只包含其中一部分（比如只有"产品经理"没有"AI"）可能被误判为不相关而跳过。这是刻意的取舍——粗筛的目标是"过滤掉明显不沾边的噪音"而不是"精确判断相关性"（精确判断就得调LLM，等于没省钱），漏判的边界情况可以用户手动点"AI 分析"强制跑一次找回来。

**地点相关性粗筛，以及为什么设置变更不触发旧数据清理（讨论于 2026-08-15）**
跟标题粗筛同一思路：`_location_looks_relevant()` 用职位地点字符串和配置的城市列表（`config.json` 的 `locations`）做双向子串包含判断，都不沾边才跳过。起因是用户反馈"Remote"作为搜索地点时经常混进地理位置完全不相关的结果（例：Richmond, VA 的坐班职位），这类噪音之前会被手动强制重试时意外分析掉（手动路径不走粗筛）。同时讨论了"改了设置后旧结果怎么处理"——没有做自动删除/批量忽略：设置变更只影响之后新一轮"哪些职位该自动分析"的判断，不会回头处理数据库里已有的记录，因为待审核队列里可能有用户还没来得及看但仍然感兴趣的条目，自动清理是不可逆操作，风险大于收益。旧的不再匹配当前设置的记录会一直留在"待审核"里，需要用户自己手动忽略。

**为什么 SQLite 连接要开 WAL 模式 + 长超时（讨论/发现于 2026-08-15）**
自动分析上线后，"搜索"和"后台批量分析"经常并发跑（比如刚点了"立即搜索一次"，同时程序刚启动的历史积压补跑还没跑完）。`run_search_once()` 原来一次搜索的所有写入攒到最后才 `commit()`，这段时间持有写锁，同时后台分析线程想写分析结果就会撞上 SQLite 默认 5 秒超时的 `database is locked`（实测触发过一次，一条职位的分析结果因此丢失）。修法是 `models.get_conn()` 里加 `PRAGMA journal_mode=WAL`（读不阻塞写，更适合这种多线程各自开连接的场景）+ `timeout=30`（超时时间从默认 5 秒放宽到 30 秒，给一点排队等待的余地），配合 `scraper.py` 里改成每个 关键词×城市 组合处理完就提交一次而不是攒到最后——没有引入应用层的锁或消息队列，本地单进程小工具这个组合已经够用。

**为什么职位的审核状态（new/reviewed/dismissed）和分析结果分开存**
早期实现里"AI 分析"直接把 `status` 字段改写成 `analyzed`/`analysis_failed`，覆盖掉了 new/reviewed/dismissed——这在手动点击模式下不明显，但一旦改成自动分析所有新职位，会导致职位一分析完就从"待审核"筛选里消失，等于自动分析功能直接把"待审核"列表清空。修复为 `overall_match`/`analysis_error` 只更新分析相关字段，不碰 `status`；审核状态只由用户手动点"已看过"/"忽略"改变。

**JD正文缺失时为什么跳过AI分析，"重新获取"为什么是重新搜索而不是直接抓URL（讨论/实现于 2026-08-15）**
JD正文为空时把它当真实内容发给LLM分析，打出来的分数没有意义（通常是很低分，因为LLM也看不出所以然），纯粹浪费调用成本——这跟修 `"nan"` 字面量那次是同一类问题的另一面，所以在 `pipeline.analyze_and_record()` 入口统一加了检查，JD为空直接跳过、不调LLM，手动/批量共用同一处逻辑。"重新获取"没有做成"直接请求 job_url 拿描述"，是因为 jobspy 本身不对外暴露"按URL取详情"的接口（每个站点的详情抓取都是搜索流程内部的私有实现，尤其 LinkedIn 需要带着搜索上下文的会话请求）；退而求其次，用职位入库时记录的 关键词+城市+来源站点 重新跑一遍 `scrape_jobs()`，在结果里按 `job_url` 或 company+title 去重键找回同一条取新描述（`scraper.refetch_job_jd()`）。代价：如果这条职位现在排名掉出了该关键词/城市组合的最新一批结果（时间久了、下架又重新上架换了ID等），会找不到匹配、重新获取失败——这是接受的限制，用户可以再点一次重试，或者判断这条职位大概率已经过期。

**单条"重新获取"为什么改成后台线程+前端轮询，而不是同步等请求返回（讨论/发现于 2026-08-15）**
最早的实现是同步的：请求进来直接跑完"重新搜索+抓LinkedIn详情页+自动分析"全流程再返回，跟单条"AI 分析"一个模式。上线后实测偶发浏览器报 `TypeError: Failed to fetch`——这是网络层错误（不是HTTP错误状态），说明连接在请求完成前就被中断了；根因判断是这个流程可能要一两分钟甚至更久（LinkedIn尤其慢），长时间挂着不返回数据的连接容易被浏览器/IDE内置浏览器/网络中间层当成失活连接掐断。批量"全部重新获取JD"从一开始就是后台线程+立即返回（因为条数多、耗时是分钟级的，同步等待完全不可行），单条的手动版本这次也改成同一个模式（`app.py` 的 `/api/jobs/<id>/refetch_jd` 改成 `threading.Thread` 里跑）。代价是前端拿不到"这次到底抓没抓到"的即时返回值，用轮询补回来：`static/app.js` 的 `pollJobUntilSettled()` 每隔几秒查一次 `/api/jobs`，对比这条职位的 `jd_text`/`analysis_error` 有没有变化，变了就当作后台跑完了，自动刷新列表并弹 toast 告知结果；轮询最长4分钟，超时还没变化就提示"仍在后台处理中，可稍后点刷新"，不会无限等下去。

**"只看外企"为什么靠AI分析顺带判断，而不是搜索阶段的关键词黑名单（讨论/实现于 2026-08-15）**
公司国籍没有可靠的结构化数据源（jobspy/Indeed/LinkedIn 都不返回这个字段），能想到的办法只有两种：(1) 维护一份人工关键词黑名单在搜索/粗筛阶段过滤，成本低但覆盖不全、需要持续手动维护；(2) 让本来就要调用的 AI 匹配分析顺带判断，不增加额外 LLM 调用成本，但只能在分析完成后才知道结果，没法在搜索/粗筛阶段提前拦截省钱。用户选了方案(2)。实现上是在 `analyzer.py` 的 `PROMPT_TEMPLATE` 里加一步 `company_origin` 判断（`foreign`/`domestic`/`unknown`，模型基于自身知识+JD线索判断，不追求100%准确，拿不准就填 `unknown`），结果随 `overall_match` 一起写回 `jobs` 表（`models.py` 新增 `company_origin` 列）。

**"只看外企"的筛选入口为什么从设置页开关改成职位列表页面 chip（讨论于 2026-08-15）**
第一版实现是 `config.json` 全局开关 `hide_domestic_companies` + 设置页勾选框，服务端 `GET /api/jobs`（`app.py`）按开关过滤。用户体验后要求改成跟"状态筛选"（新/已看过/已忽略）同样形态的页面筛选控件，还要能反过来单独看"国内公司"，不只是开/关两态。改成职位列表工具栏新增"🌍 外企 / 🇨🇳 国内公司 / 全部"三态 chip 筛选（`templates/index.html` 的 `#originChips`，默认选中"外企"），过滤逻辑挪到前端 `renderJobs()`（`static/app.js` 的 `currentOrigin` 状态，写法跟已有的 `currentStatus` 状态筛选一致），撤掉了设置页开关和服务端过滤（`hide_domestic_companies` 相关代码从 `config.py`/`app.py`/前端设置表单里整个删掉，没有走废弃/兼容路径，因为这个字段刚加当天就被替换，没有依赖它的历史数据）。三态语义："外企"排除明确判定为 `domestic` 的职位（`unknown`/未分析的仍保留，避免误藏还没分析过的职位）；"国内公司"只显示 `domestic`；"全部"不过滤。跟状态筛选一样是纯前端状态，刷新页面会重置回默认值"外企"，不做持久化——保持跟 `currentStatus` 筛选同样的行为，不引入 localStorage 之类的额外状态存储。

**投递状态为什么新增独立列，不复用审核状态 `status`（2026-08-16）**
`status`（new/reviewed/dismissed）是审核工作流字段，被 `list_jobs_needing_analysis()`/`queue_pending_jobs()` 等分析资格判断依赖（`WHERE status='new'`），混用会连带破坏这些查询。新增 `application_status` 列（`not_applied`/`applied`/`interviewing`/`rejected`/`offer`/`declined`，默认 `not_applied`），完全独立于审核状态，手动维护，`models.py` 用现有 ALTER-column 回填模式（`NOT NULL DEFAULT`，SQLite 自动给约300条历史行回填默认值，不用写迁移脚本）。

**"重点关注"为什么也是独立列、且筛选做成独立开关而不是 origin chip 的第四项（2026-08-16）**
跟上一条同样的理由：审核状态 `status` 被分析资格查询依赖，不能塞进第四种取值，所以新增 `starred` 列（INTEGER 0/1，`NOT NULL DEFAULT 0` 沿用同一套 ALTER-column 回填模式）。前端筛选没有并进 `#originChips` 那组三选一——那组是"公司国籍"这一个维度的互斥取值，而"重点关注"是另一个维度的布尔开关，用户需要的是"外企里标了星的"这种叠加，而不是二选一；所以放在一条竖分割线之后，用独立的 `starredOnly` 状态跟其它筛选相与（`static/app.js` 的 `renderJobs()`）。跟其它筛选一致，只存在于页面状态、刷新即重置，不做持久化（星标本身在数据库里）。

**LinkedIn Easy Apply 为什么用 Playwright 持久化 context + 真实浏览器 channel，而不是网页内"确认"按钮（2026-08-16）**
需求是"材料自动备好，人工点最后一下确认"，讨论后确定的架构：自动化开一个真实可见的浏览器窗口，尽力填好 Easy Apply 表单后停在原地，用户自己在 LinkedIn 真实页面上点它自带的按钮完成剩余步骤——而不是把"确认"做成我们自己网页里的一个按钮再转发给已经在跑的自动化进程。原因：后者需要在 Flask 请求和一个独立、长期存活的浏览器自动化进程之间搭一层状态同步/转发机制，复杂度和出错面都明显更大；前者让"提交动作是否发生"完全由用户在真实 LinkedIn UI 里的动作决定，代码里从来不需要、也没有一行"点击提交"的代码，可逆性和安全性都更好。

`launch_persistent_context` 而不是普通 `launch`：需要保留登录 session（cookie）跨进程重启复用，避免每次都要重新登录，也避免在 config/数据库里存明文账号密码。

`channel="msedge"` 而不是 Playwright 默认自带的 Chromium：真实浏览器二进制被反自动化检测识别的概率比 Playwright 自带的 Chromium 低（后者的一些底层特征是已知会被部分网站针对性检测的）。本机没有装 Chrome，退而用同为 Chromium 内核、系统自带的 Edge；两者对 Playwright 而言是等价的 `channel` 选项，如果之后装了 Chrome 可以把 `easy_apply.py` 的 `BROWSER_CHANNEL` 改成 `"chrome"`。

考虑过 Firefox 系（用户曾问能否换成 Zen 浏览器）：Playwright 对 Firefox 系只能驱动它自己打了 Juggler 自动化协议补丁的 Firefox build，驱动不了普通 Firefox 或基于 Firefox 的第三方浏览器（如 Zen），技术上不可行，维持 Chromium 系方案。

并发控制没有自己维护一个文件锁/内存标记来判断"是否已有窗口开着"，而是直接尝试 `launch_persistent_context`、失败就说明真的有浏览器在用这个 profile：自建的锁在"用户直接关掉浏览器窗口"这种最常见的正常退出路径下没有代码会去清理，容易卡成误报的"一直在用中"；依赖 Chromium 自己的 SingletonLock 天然跟"浏览器进程是否真的还活着"同步，不会有状态不一致的问题。

**Easy Apply 按钮选择器为什么要同时匹配"申请"/"快速申请"/"easy apply"三种写法，且区分精确匹配和子串匹配（实测于 2026-08-16）**
LinkedIn 中文界面下 Easy Apply 按钮的可见文字不固定（实测见过"申请""快速申请"两种），且**可访问性名称（`aria-label`，Playwright `get_by_role` 匹配用的就是这个）优先于可见文字、内容也可能不完全一样**——"快速申请"按钮的 `aria-label` 实际是"快速申请职位"（多了"职位"二字），一开始按可见文字精确匹配一直匹配不上。同时踩过一个更隐蔽的坑：某条外部投递职位的按钮可见文字也是"申请"，但 `aria-label` 是"去公司网站申请"（点了会跳转到公司官网，不是站内 Easy Apply）。综合两个实测结果调整为：`快速申请`/`easy apply` 用子串匹配（覆盖"职位"后缀、"Easy Apply to Company"这类带额外文字的情况），纯 `申请` 用精确匹配 `^申请$`（避免误配到"去公司网站申请"这类语义不同的按钮，以及"已申请"这类字面包含"申请"但表示已投过的按钮）。

**为什么 `run_easy_apply()` 不用 `with sync_playwright() as p:`（实测踩坑于 2026-08-16）**
最初实现用了 `with sync_playwright() as p:` 包住整个函数体，结果发现浏览器窗口打开后立刻自动关闭——因为函数 `return` 时 Python 仍会执行 `with` 块的 `__exit__`，把 Playwright 驱动连接停掉，连带子进程浏览器一起关闭，这跟"成功路径要故意留着窗口不关"直接矛盾。改成手动管理生命周期（`p = sync_playwright().start()`），成功路径不调用 `p.stop()`，只在错误路径主动清理；因为 `run_easy_apply()` 实际运行在 Flask 常驻进程的后台线程里（不是一次性脚本），这样浏览器和驱动连接能正确存活到用户自己关闭窗口或整个 Flask 进程退出为止。

**Easy Apply 为什么扩大到自动答筛选问题+自动翻页，答案来源为什么是设置页固定表而不是 LLM 现场判断（讨论/实现于 2026-08-16）**
第一版（只填简历/cover letter，从第一步就停下等人工）上线后用户实测反馈"这样还不如自己点"——多数职位的 Easy Apply 都有好几步筛选问题，每步都要人工点"下一步"、逐题手动填，自动化几乎没省下操作。讨论后把自动化范围扩大到：尽力答完能识别的问题并自动翻页，只在真正遇到没配置过答案的问题、或者已经没有"下一步"按钮（到最终确认页）时才停下——但"停下等人工确认提交"这条底线完全不变，变的只是"自动化能替你走到哪一步"。
答案来源选了"设置页固定表 + 关键词匹配"而不是"每个问题交给 LLM 结合简历现场判断"：后者更智能、能覆盖任意措辞的问题，但每次调用都要花 LLM 调用成本和几秒延迟，而且 LLM 判断这类事实性问题（工作签证状态、期望薪资、入职时间）本身没有"标准答案"可推导，答错的风险（可能直接影响这条申请的审核结果）比"没配置就停下让人工填"更不可接受。固定表方案答案是用户自己填的、100%准确，代价是覆盖不到没预先想到的问题——但这正是设计上想要的：宁可覆盖率低一点、多停几次让人工接手，也不猜。

**Easy Apply 问题扫描为什么严格限定在弹窗（`role="dialog"`）范围内，不扫整个 page（2026-08-16）**
早期版本 `_answer_questions_on_step()`/`_click_next_or_review()` 直接在整个 `page` 上找 `fieldset`/`label[for]`/按钮，理论上可能误扫到 Easy Apply 弹窗背后背景页面上无关的表单元素（比如同一个 LinkedIn 页面上其它区块碰巧也有的 `label`），虽然实测阶段没有实锤复现出因此导致的错误行为，但风险本身是显而易见的——弹窗背后是真实的 LinkedIn 页面，混进去的元素性质完全不可控。改成先拿到 `page.get_by_role("dialog")` 这个弹窗 locator，后续所有扫描/点击都限定在这个 scope 内，从源头排除这类风险。

**为什么 `run_easy_apply()` 里显式等待可交互元素、而不是固定 `sleep`（实测踩坑于 2026-08-16）**
最初每步循环只是固定 `page.wait_for_timeout(800)` 再扫描，实测发现不稳定：LinkedIn Easy Apply 弹窗每一步的内容是异步渲染的，有时候弹窗 shell（`role="dialog"`）已经出现但里面的按钮/表单字段还没渲染完，这个时间点去扫描/找"下一步"按钮会全部扑空，误判成"已经到了没有下一步的最终确认页"而提前停下——实测中同一条职位、几乎一样的操作序列，有时候能正确走完好几步，有时候第一步就误判"到头了"，典型的时序竞态。修复：每步开始时先显式 `wait_for(state="visible", timeout=8_000)` 等到弹窗内至少出现一个可交互元素（button/input/select/textarea）再开始扫描，等不到也不报错（可能这一步真的没有交互元素），比固定睡眠更贴近"内容实际准备好了没有"这个真实条件。

**为什么 fieldset 没有 `<legend>` 时也要算"未答上"而不是跳过（实测踩坑于 2026-08-16）**
`_answer_questions_on_step()` 最初的写法：找到 `fieldset` 但拿不到 `<legend>` 文字（`legend.count()==0` 或文字为空）就直接 `continue`，既不尝试填也不标记成"没答上"。实测中这类 fieldset 确实存在——有交互控件（比如单选题的几个选项）但不用标准 `<legend>` 标签承载问题文字，可能是某类自定义题型的 LinkedIn 前端实现。原来的写法会**悄悄放过一个真实存在、且用户可能需要回答的问题**，比"明确停下来交给人工"更危险：自动化会认为这一步"答完了"、继续点"下一步"往前走，用户根本不知道中间跳过了什么。修复：判断 fieldset 内是否真的有交互控件（radio/input/select/textarea），如果有但拿不到问题文字，也算作"未答上"，触发跟"配置里没有匹配答案"完全一样的停止行为——这类问题反正也没法靠关键词匹配去配置答案（不知道问题文字，配置的关键词也无从谈起），只能每次遇到都停下人工填。

**为什么把 LLM provider 适配器从 `analyzer.py` 抽到独立的 `llm.py`（2026-08-16）**
原来这层只服务一个消费者（JD-简历匹配分析），所以直接住在 `analyzer.py` 里，接口也只支持"一条 prompt 字符串进、一段文本出"。面试准备模块引入了两个它接不住的需求：(1) 模拟面试是多轮对话，需要传 `messages` 数组 + 独立的 system prompt（Anthropic 的 `system` 是顶层参数，DeepSeek/OpenAI 兼容接口是 `messages[0]`，差异必须在适配层抹平）；(2) 面试准备一次要输出十几道题+答法+话术，硬编码的 `max_tokens=4096` 会把 JSON 截断成解析失败，需要可调。同时原来的写法里 provider 分派（`if deepseek / elif anthropic / else raise`）在每个公开函数里各抄了一遍、`pipeline.py` 里 `provider`/`model` 的解析也重复了两处，再加第三个消费者只会继续复制。抽出 `llm.py`（`chat`/`chat_json`/`ask`/`ask_json`/`resolve`）后，`analyzer.py` 只剩 prompt 和结果后处理，对外签名一字未改，`pipeline.py` 现有调用点不用动。没有顺手引入 LangChain 之类的抽象层——两家 provider、五个函数，标准库 `urllib` + Anthropic SDK 已经够用，多一层依赖不划算。

**面试准备为什么存 SQLite 新表，而不是像匹配分析那样追加到 `JD匹配追踪表.xlsx`（2026-08-16）**
两个原因。一是那张表跟 `jd-resume-matcher` 技能共享格式（见上面"为什么职位队列用 SQLite，匹配结果用 xlsx"），面试准备是本项目独有的东西，往共享格式里塞会让两边越走越远；"公司简介"那次加一列还能接受，面试准备是四个板块、十几道题的结构化内容，不是一列能装下的。二是内容形态本身不适合表格：每道题有"为什么会问 + 答题要点列表 + 简历依据"三层结构，塞进一个单元格只能靠换行符硬拼，既没法在 Excel 里读，读回来也没法可靠还原结构。新表 `interview_preps` 直接存 LLM 返回的 JSON 原文（`content_json`），前端 `JSON.parse` 后按结构渲染。

一条职位对应多行而不是 jobs 表上的一列：二面前想换个角度重新生成一份是真实需求，历史版本留着能对照（前端有版本下拉）。沿用本项目一贯做法只存 `job_id`、不建外键。

**面试准备为什么在"投递状态改成面试中"时自动触发，且失败不能连累状态更新（2026-08-16）**
用户选定的触发时机——那正是真正需要材料的那一刻，不用再多点一次按钮。三个配套约束：(1) 同一条职位只自动生成一次（`get_latest_interview_prep(success_only=True)` 判断），否则用户反复切换投递状态就会反复烧 LLM 调用；(2) 上次生成**失败**的不算"已有材料"，会自动再试一次，不然一次网络抖动就让这条职位永远拿不到材料；(3) 整段触发逻辑用 `try/except` 包住——"改投递状态"是纯本地 DB 写入、绝不该因为没配 API key / 追踪表被 Excel 占用 / LLM 报错而失败，那是两件独立的事。手动重新生成的入口只放在详情弹窗的面试准备 tab 内部，没有在职位卡片上再加按钮：卡片操作区已经有投递状态下拉、AI分析、Easy Apply、星标、收藏、忽略六个控件了。

**面试准备为什么复用已有的匹配分析结论，而不是让 LLM 重新解析一遍 JD（2026-08-16）**
省 token 只是次要原因，主要是**一致性**：用户会在同一个弹窗的两个 tab 里对着看「匹配分析」和「面试准备」，如果两次独立的 LLM 调用各自判断哪些要求算缺口，很容易出现"匹配分析把 ITIL 认证标红、面试准备的缺口话术里却没有这一条"这种自相矛盾，直接摧毁两边的可信度。实现上把追踪表里已有的 `requirement_items`（含 `is_gap`）/`skill_gap_bullets`/`skill_matched_bullets`/`company_overview`/`overall_match` 压成一段文本塞进 prompt（`interview.build_analysis_block()`），并明确要求"直接采信，不要推翻重算"、缺口话术要覆盖所有 `is_gap=true` 的条目。取数复用 `pipeline.find_tracker_entry()`——这段"按 `make_dedupe_key(company, title)` 在追踪表里找回记录"的逻辑原来只在 `app.py` 的 `_find_cover_letter()` 里给 Easy Apply 用，这次抽出来共用，避免两处各写一遍导致关联规则漂移。读表失败（文件不存在/被 Excel 独占打开）不抛异常，降级成"没有已有分析"让 LLM 自行判断，不因为一个可选输入就让整个生成失败。

**为什么面试准备的生成状态放内存、结果落库，而 Easy Apply 是两者都放内存（2026-08-16）**
`job_state.py` 里三组状态各自的取舍不同：面试准备的**结果**本来就落库（成功和失败都往 `interview_preps` 写一行），所以内存里只需要 `generating` 一个中间态，失败原因前端从库里那一行的 `error` 字段读，重启后也还在；Easy Apply 的失败是"浏览器没打开"这类不落库的瞬时结果，只能靠内存暂存一次给前端弹 toast，所以它多了个 `_easy_apply_errors`。跟分析状态一样，进程内内存足够——单进程 Flask，重启后重新触发即可。

**通用题库为什么用 `user_edited` 标志保护，而不是"每次起草都全量覆盖"或"起草后另存为新版本"（2026-08-16）**
题库跟面试准备材料的性质完全不同：面试准备是"针对这场面试一次性生成的参考资料"，重新生成一份新的就行；题库是"我的标准答案"，用户会反复打磨自我介绍、把 AI 的套话改成自己的说法，这些编辑是长期资产。所以 `replace_ai_bank_items()` 的合并规则是：同类别下问题文字相同的条目，只有 `user_edited=0` 才更新答案；改过的一律跳过；没有的新增；**从不删除**（AI 这轮没生成到的题可能是用户手动加的，不能当成过期数据清掉）。手动加的题默认 `user_edited=1`，本来就是用户自己的内容。

考虑过另外两种做法：(1) 每次起草全量覆盖 —— 直接摧毁用户的编辑，不可接受；(2) 每次起草存成新版本、让用户自己合并 —— 一份题库十几条，逐条人工合并的负担比"AI 只补空缺"大得多，而且用户真正想要的就是"补充我没想到的问题"这个语义。返回的统计里专门带上 `skipped` 条数并在 toast 里告诉用户，让"你改过的没被覆盖"这件事可见，而不是让人担心点了起草会丢东西。

**为什么只有自我介绍做中英双版，通用题不做（2026-08-16）**
自我介绍是外企面试的固定开场，中英文都需要，而且是同一段内容的两种语言表达，值得成对维护。其它通用题配英文版会让 AI 起草的输出量直接翻倍（token 成本和被截断的风险都上去），但实际使用中英文面试问到这些题时，用户看着中文答案现场组织英文表达完全够用——真正需要逐字准备的只有开场那 60-90 秒。这也是数据模型上 `answer_en` 允许为空的原因。

**题库起草为什么是全局单例，而面试准备是按职位区分（2026-08-16）**
`job_state.py` 里两者的并发保护粒度不同：面试准备挂在 `job_id` 上（不同职位可以同时生成，互不干扰），题库是跨职位的单例，同时跑两次除了浪费 LLM 调用，两次结果还会互相覆盖（后跑完的把先跑完的合并结果又合并一遍）。`start_bank_generation()` 把"检查是否在跑"和"置位"放在同一把锁里完成，避免连点两下时两个请求都通过了检查——这跟 Easy Apply 那边"靠 Chromium 自己的 SingletonLock 兜底"不同，题库没有这样一个天然的外部锁可以依赖，只能自己保证原子性。失败时在 `finally` 里清标志，否则按钮会永远灰着。

**密钥/本地路径不进版本库**
`ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` 都只通过环境变量传入；`config.json`、`jobs.db` 都在 `.gitignore` 里，避免密钥或个人本地文件路径被提交。

**设置/运行记录为什么从顶层平级标签改成"更多"弹窗入口，而不是保留标签、只做视觉降级（讨论/实现于 2026-08-16）**
原来"职位列表/设置/运行记录"是顶层三个平级标签，跟"只看外企"筛选之前从设置页开关挪到职位列表 chip 是同一类问题的反面：那次是把高频操作从设置页挪出来，这次是低频操作（设置偶尔改一次、运行记录偶尔查一次）占着跟职位列表同等的顶层导航位置，喧宾夺主。讨论过三种方案：(1) 顶部图标各开一个弹窗；(2) 合并成一个"更多"入口，弹窗内部再分两个子标签；(3) 保留三标签结构、只把设置/运行记录做小做视觉降级。选了方案(2)：只用一个入口点比两个图标更简洁，且没有像方案(3)那样在本质上仍是同级导航、没有解决"占满顶部一整行"的问题。实现上复用了已有的 `#jobDetailModalOverlay` 弹窗结构（`.modal-overlay`/`.modal`/`.modal-head`/`.modal-body`）和已有的 `.tab-btn`/`.tab-panel` 标签切换机制（`.modal-tabs` 只是去掉了原来顶层标签用的 `border-bottom`/`margin-bottom`），没有新增样式体系。

## 已知技术限制

- 定时任务依赖进程常驻，没有补跑机制。
- 去重基于"公司+职位名"归一化字符串精确匹配，措辞不同的同一职位可能漏判为不重复。
- xlsx 当"数据库"用没有并发写保护，目前单进程场景下不是问题，多进程同时写会有风险。
- 前端没有自动化测试，改动依赖手动在浏览器里验证。
- Easy Apply 的按钮/字段选择器基于实测调整，不是官方稳定接口，LinkedIn 前端改版可能导致选择器失效，需要重新实测调整。
- Easy Apply 同一时间只支持处理一条职位（Chromium 对 profile 目录的独占锁天然限制），批量场景没有做队列。
- Easy Apply 自动答题只能覆盖设置页配置过的问题，没配置的一律停下人工填；`fieldset` 没有 `<legend>` 的问题即使想配置也做不到（拿不到问题文字），每次都需要人工介入。

---
最后更新：2026-08-16（面试准备 P1/P2 的技术决策：抽出 llm.py、interview_preps 存 SQLite、复用匹配分析结论、自动触发的边界、题库 user_edited 保护与全局单例起草）
