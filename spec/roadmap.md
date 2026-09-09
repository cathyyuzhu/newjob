# 项目路线图 (Roadmap)

追踪 `newjob` 项目的功能列表。维护规则见 `CLAUDE.md`：每次讨论新功能都要同步更新本文件。

> 想知道"这些功能加起来到底有没有竞争力、市面上有什么竞品、能不能商业化"，见 [product-review.md](product-review.md)（2026-08-16 的产品评估快照，含竞品对照和商业化判断；它是时间点快照，不随本文件同步更新）。

## 已完成

### 核心：自动搜索与待审核列表
- 按关键词 × 城市定时自动抓取 Indeed / LinkedIn 新职位（`scraper.py`、`scheduler.py`）
- 基于公司+职位名去重，新增职位存入 SQLite 待审核列表（`models.py`，`jobs.db`）
- 网页手动触发"立即搜索一次"
- 待审核职位列表：按状态（新 / 已收藏 / 已忽略）筛选
- 搜索运行记录查看（最近历史，含找到数/新增数/去重跳过数/报错信息）
- 配置页：关键词、城市、Indeed 国家代码、每组合返回条数、发布时间范围（天数）、每日定时时间、追踪表路径

### AI 自动分析
- 调用 LLM（Claude 或 DeepSeek，见下）按 `jd-resume-matcher` 技能的双因子模型（认知要求匹配度50% + 工作内容匹配度50%）计算总体匹配度（`analyzer.py`）
- 分析结果写入 `JD匹配追踪表.xlsx`（`tracker_utils.py`）
- 匹配度 ≥70% 时自动生成定制简历 docx + cover letter（`resume_docx.py`、`pipeline.py`）
- 需要用户自行配置 `ANTHROPIC_API_KEY`（或 `DEEPSEEK_API_KEY`）环境变量，未配置时其余功能不受影响
- 也保留单条职位"AI 分析"按钮，可手动立刻重新触发（比如重试失败的）

### 多 LLM 供应商支持（2026-08-15）
- `config.json` 新增 `llm_provider`（"anthropic" 或 "deepseek"）、`deepseek_model` 字段，可切换成本更低的 DeepSeek API 做分析（`analyzer.py`、`config.py`、`pipeline.py`、`app.py`）
- DeepSeek 走 OpenAI 兼容的 `/chat/completions` 接口（标准库 `urllib` 直接调用，未引入新依赖），需要用户自行配置 `DEEPSEEK_API_KEY` 环境变量

### UI 重做（2026-08-15）
- 整体布局改为顶部品牌栏 + 统计卡片（待审核/已收藏/已忽略/最近运行时间）+ 标签页导航（职位列表 / 设置 / 运行记录）
- 职位列表改为卡片式：Indeed/LinkedIn 来源徽章、匹配度彩色药丸标签（高/中/低配色）、状态筛选 chip、职位搜索框
- 交互反馈：Toast 通知替代原来的单行文字状态、按钮 loading 态防重复点击、列表骨架屏、空状态提示
- 深色模式：跟随系统 `prefers-color-scheme` 自动切换，同时提供手动切换按钮（记忆于 localStorage）
- 前端对外部抓取数据（职位标题/公司名等）做 HTML 转义，避免潜在 XSS

### 项目文档体系（2026-08-15）
- 新增 `spec/mission.md`（项目愿景与目标）、`spec/tech-solution.md`（技术选型与关键决策）
- `CLAUDE.md` 加入文档同步规则和"项目文档一览"，明确各文档定位避免内容重复

### Flask 服务改单线程为多线程（2026-08-15）
- `app.run()` 加 `threaded=True`（`app.py`）：修复"点 AI 分析后页面卡住/其他操作没反应"——根因是开发服务器默认单线程，DeepSeek/Claude 单次分析耗时可达 1~2 分钟，期间会阻塞其他所有请求

### 追踪记录标签页（2026-08-15）
- 新增"追踪记录"标签页，卡片列表展示 `JD匹配追踪表.xlsx` 里的记录（最新在最上面），点卡片弹窗看完整分析详情（职位内容、任职要求红色标未达标项、技能匹配、经验/薪资/团队/地点、简历优化建议、简历路径、cover letter 全文）
- 新增 `tracker_utils.list_entries()`：读取 xlsx 并把富文本（红色未达标标注）还原为结构化 `is_gap` 字段（`tracker_utils.py`），新增只读接口 `GET /api/tracker`（`app.py`）

### 追踪记录支持跳转原职位页面（2026-08-15）
- 追踪记录卡片的职位名称改为可点击链接，新标签页打开原职位发布页（`job_url`，已由 `tracker_utils.list_entries()` 从 xlsx 超链接读出）；点详情弹窗顶部也加"查看原职位页面"链接；无 `job_url` 时退化为纯文本，不影响原有点卡片弹窗看详情的交互（`static/app.js`）

### 待审核职位自动分析，取消手动逐条点击（2026-08-15）
- 每次搜索（定时 cron 或手动"立即搜索一次"）结束后，自动分析**这次搜索新增的职位**（`scraper.py` 的 `run_search_once()` 返回 `new_job_ids`，透传给 `pipeline.py` 新增的 `analyze_pending_jobs(job_ids=...)`），不再需要用户逐条点"AI 分析"（接入 `scheduler.py` 的 `_run_job()` 和 `app.py` 的 `/api/search/run`）
- 批量分析放独立后台线程跑（`threading.Thread(daemon=True)`），不阻塞搜索接口的返回、也不阻塞定时任务
- 标题粗筛：批量自动分析前，跳过职位标题跟搜索关键词完全没有词面重合的条目（大概率是 LinkedIn/Indeed 返回的不相关噪音结果），不调用LLM省钱省时间（`pipeline.py` 的 `_title_looks_relevant()`）；手动点单条"AI 分析"不受此限制
- 程序上线前数据库里积压了一大批从未分析过的历史"待审核"职位（远超单次搜索的量，一次性全跑要好几个小时）；启动时的补跑（`analyze_pending_jobs()` 不传 `job_ids`，走全量"待审核"查询）限制只跑最新的 `STARTUP_BACKLOG_LIMIT`（当前=5，`app.py`）条，避免一次性跑很久很久；新搜索到的职位不受这个上限影响
- 顺带修了一个问题：之前"AI 分析"会把职位的审核状态字段（new/reviewed/dismissed）直接覆盖成"analyzed"/"analysis_failed"，导致分析完的职位从"待审核"筛选里消失；改成只更新 `overall_match`/`analysis_error` 等分析结果字段，不再动审核状态（`models.py` 的 `update_job_analysis()`）；同时把历史遗留的、被错误覆盖过状态的记录一次性改回了 `new`
- 单条职位的"AI 分析"按钮保留，用于手动立刻重试

### 修复抓取时 JD 正文被写成字面量 "nan"（2026-08-15）
- `scraper.py` 原来用 `str(row.get(...) or "")` 处理 jobspy 返回的字段，但 pandas 缺失值 `NaN` 是 truthy 的 float，接不住 `or ""` 这层兜底，`str(NaN)` 结果是字面量三个字符 `"nan"`，会被当成真实 JD 正文存进数据库、发给 LLM 分析——导致这些职位在没有任何真实职位描述的情况下被打了分（通常是 0% 或很低的分，因为 LLM 也看不出个所以然）。改成统一的 `_clean()` 帮助函数识别并转成真正的空字符串；数据库里累计清理了两批共 153 条受影响记录（`jd_text` 改回空字符串，之后能走"未获取到JD正文"的提示，也能被正常重新分析；其中已经拿着假 `"nan"` 跑出分析结果的 30 条，连带清空了 `overall_match`/`resume_path`/`analysis_error`，避免虚假匹配度误导审核）
- 根因：这批 `"nan"` 主要出在 LinkedIn 抓的职位上——jobspy 默认不带完整职位描述（每条要多发一次请求，比较慢），`description` 字段本身就是空的。`scrape_jobs()` 调用加上 `linkedin_fetch_description=True`（`scraper.py`），之后新抓的 LinkedIn 职位才能真正拿到 JD 正文，而不只是"nan"变"未获取到JD正文"的文案修复

### 合并"待审核职位"与"追踪记录"为单个标签页（2026-08-15）
- 用户反馈两个列表来回切换不方便，改成一个"职位列表"标签页：职位卡片沿用原来的筛选/搜索/AI分析/标记已看过/忽略等交互不变；卡片一旦有 `overall_match`（已分析完成）就变为可点击，点击后弹窗展示追踪表里的完整分析详情（原"追踪记录"弹窗的全部内容，含查看原职位页面链接），未分析的卡片保持不可点击
- 数据关联靠公司名+职位名归一化字符串（小写去空格）在前端做匹配（`dedupeKey()`），对应 `jobs.db` 的去重键逻辑；`analyze_and_record()` 里 `update_job_analysis()` 和 `add_entry()` 总是同一次调用一起写，两边数据不会不同步，抽样验证过线上 13 条已分析记录全部匹配成功
- 移除"追踪记录"独立标签页/面板及对应的 `renderTracker()`/`loadTracker()` 等前端代码，`GET /api/tracker` 接口保留（前端仍用它取详情数据做匹配）（`templates/index.html`、`static/app.js`、`static/style.css`）

### 地点粗筛（2026-08-15）
- 起因：用"Remote"当搜索地点时，LinkedIn/Indeed 经常返回地理位置完全对不上的噪音结果（例：搜"AI产品经理"+Remote 混进来一条 Richmond, VA 的坐班职位）。用户把设置里的 Remote 去掉、换成深圳后，要求同类噪音以后不要再自动花钱分析
- 跟标题粗筛并列的第二层粗筛：职位地点跟当前配置的城市列表（`config.json` 的 `locations`）双向子串包含判断都对不上就跳过、不调用LLM（`pipeline.py` 的 `_location_looks_relevant()`）；城市列表留空（不限地点）则不过滤，跟设置页文案语义一致。同样只影响批量自动分析，手动点单条"AI 分析"不受影响
- 讨论过是否要把不再符合新设置的历史"待审核"记录批量删除/忽略，决定不做：设置变更不应该销毁用户还没审核过的数据，只影响以后新分析该分析什么，旧记录留给用户自己手动忽略

### 修复"搜索时后台自动分析报 database is locked"（2026-08-15）
- 触发场景：手动点"立即搜索一次"期间，`run_search_once()` 原来一次性攒着所有 关键词×城市 组合的写入，跑完整个搜索才 `commit()`，如果这段时间里恰好有后台批量分析（比如程序刚启动的历史积压补跑）也要写库，SQLite 默认 5 秒就报 `database is locked`，导致那条职位分析结果丢失、白花了一次LLM调用的钱
- 修复两处：`models.py` 的 `get_conn()` 开 WAL 模式 + 30 秒超时（原来是 sqlite3 默认的 5 秒，且没开 WAL）；`scraper.py` 的 `run_search_once()` 改成每处理完一个 关键词×城市 组合就 `commit()` 一次，不再攒到整个搜索跑完才提交，缩短持有写锁的时间窗口

### 标题粗筛改用信号词子串匹配 + 中英文别名，并批量删除待审核里标题不相关的记录（2026-08-15）
- 用户要求把"待审核"里标题明显不是产品经理的记录直接删除。执行前预览发现原来的标题粗筛（token集合精确相交）有严重漏洞：中文没有空格分词，"AI产品经理"这个关键词是一整块token，但很多标题里"AI"和"产品经理"中间有符号/空格会拆成两个token，导致**标题就叫"Product Manager"/"产品经理"/"AI Product Manager"的职位**都被误判成不相关——如果直接删会连真实产品经理职位一起删掉
- 改成：先用 `_STOPWORDS` 过滤掉职级通用词，剩下的信号词（目前所有配置关键词过滤后都归约成"product"或"ai产品经理"）通过 `_TERM_ALIASES`（`product`↔`产品`）展开出中英文对应写法，再用子串包含（而不是token集合精确匹配）判断标题里有没有出现——覆盖"高级产品经理-AI"、"AI应用数据产品经理/AI Application Data Product Manager"这类关键词和标题分词边界对不上的情况
- 用修复后的粗筛重新预览、逐条人工过了一遍确认没有明显误伤，删除了 137 条标题跟"产品经理"完全不沾边的待审核记录（`models.py` 新增 `delete_jobs()`），剩余 225 条
- 第二轮：字符串子串匹配"产品/product"这个信号词本身还是不够精确，会把"产品运营""产品实习生""Product Designer"这类含"产品"但明显不是"产品经理"职能的岗位也误判成相关而保留下来（例：268号"广告产品运营-【生活服务】"）。用户确认后又删了一批"明显不是"的 8 条（工程师/实习生/设计师/运营岗），"专家/分析师/负责人"这类模糊的（Investment Product Expert、产品专家、Product Strategy Analyst、Head of Product 等）按用户要求先保留，不自动判断。剩余 217 条
- 第三轮：用同一套已修好的地点粗筛（`_location_looks_relevant()`，已支持中英文城市名+Indeed代码兜底）预览"待审核"里地点不在 Beijing/Shanghai/Shenzhen 的记录——这次没再踩坑，逐条扫过确认全是真实的海外/其它中国城市职位（美加澳新欧+广州/杭州/厦门/天津/苏州/香港等未配置城市），删除 124 条。三轮清理后"待审核"从最初约 362 条降到 93 条

### 抓取按发布时间过滤（2026-08-15）
- 新增 `config.json` 的 `days_old` 字段（默认30天），传给 jobspy 的 `hours_old` 参数（天数×24），只抓取最近这么多天内发布的职位，Indeed/LinkedIn 均支持；设为0或留空表示不限（`scraper.py`、`config.py`、`app.py`、设置页新增输入框）

### JD正文缺失时跳过AI分析，新增"重新获取"按钮（2026-08-15）
- JD正文为空的职位（抓取时来源站点没返回描述、被限流等）之前会被当成真实内容发给LLM分析，白花钱还打不出有意义的分数。改成 `pipeline.analyze_and_record()` 统一在入口处检查，JD为空直接跳过AI调用（手动点单条"AI 分析"和批量自动分析共用同一处逻辑，都会跳过）；批量自动分析（`analyze_pending_jobs()`）额外做了预过滤，避免每次都空跑一次报错
- 职位卡片：JD正文为空时"AI 分析"按钮换成"重新获取"按钮，点击后重新抓一次JD正文，抓到了会自动接着跑一次AI分析（`pipeline.refetch_jd()`）；顶部工具栏新增"全部重新获取JD"按钮，批量重新抓取"待审核"里所有JD缺失的职位，放后台线程跑（`pipeline.refetch_missing_jd_jobs()`、`app.py` 新增 `/api/jobs/<id>/refetch_jd`、`/api/jobs/refetch_jd`）
- 重新抓取的实现方式：jobspy 没有"按 job_url 直接取详情"的接口，只能用职位入库时记录的 关键词+城市+来源站点 重新跑一遍搜索，在结果里按 job_url（或退化到 company+title 去重键）找回同一条职位取新的描述（`scraper.refetch_job_jd()`）。局限：如果这条职位现在已经不在该关键词/城市组合的最新一批搜索结果里了，会找不到匹配、重新获取失败，可以再点一次重试
- 未分析（`overall_match` 为空）且JD缺失的职位，卡片上的匹配度徽章从"未分析"改成更明确的"JD未获取"（`static/app.js` 的 `matchBadge()`）
- 单条"重新获取"改成后台线程跑之后（避免长耗时请求被判定连接失活报"Failed to fetch"，见下），前端改用轮询而不是干等：点击后每隔几秒查一次 `/api/jobs`，发现这条职位的 `jd_text`/`analysis_error` 变化了（说明后台抓取+自动分析跑完了）就自动刷新列表、弹 toast 告知结果，不需要用户自己再点"刷新"；最长轮询4分钟，超时则提示"仍在后台处理中"（`static/app.js` 的 `pollJobUntilSettled()`）
- 修复单条"重新获取"点击后偶发报错"Failed to fetch"：原来是同步等待整个请求跑完（重新搜索+抓LinkedIn详情页可能要一两分钟），长时间挂着的连接被浏览器/网络中间层判定失活后中断连接。改成跟批量"全部重新获取JD"一样放后台线程跑、接口立刻返回（`app.py` 的 `/api/jobs/<id>/refetch_jd`），配合上面的前端轮询找补回"抓完自动看到结果"的体验

### "只看外企"过滤（2026-08-15）
- 起因：没有可靠的公司国籍数据源，讨论后放弃搜索阶段关键词黑名单方案（覆盖不全、需持续维护），改成让本来就要跑的AI匹配分析顺带判断，不增加额外LLM调用成本，取舍详见 [tech-solution.md](tech-solution.md)
- `analyzer.py` 的 `PROMPT_TEMPLATE` 新增 `company_origin` 判断（foreign/domestic/unknown，模型基于自身知识+JD线索判断，拿不准就填unknown），结果随匹配度一起写入 `jobs` 表新增列（`models.py`）
- 第一版做成 `config.json` 全局开关 + 设置页勾选框（服务端 `/api/jobs` 过滤），用户体验后要求改成页面上的筛选 chip：职位列表工具栏新增"🌍 外企 / 🇨🇳 国内公司 / 全部"三态筛选（`templates/index.html` 的 `#originChips`），默认选中"外企"；筛选逻辑挪到前端 `renderJobs()`（`static/app.js`），撤掉了原来的设置开关和服务端过滤（`config.py`/`app.py` 里 `hide_domestic_companies` 相关代码已删除）
- "外企"筛选态排除的是明确判定为 `domestic` 的职位，`unknown`/未分析的职位仍会保留显示，避免"判断不出就藏起来"导致漏看；"国内公司"态只显示 `domestic`；"全部"态不过滤
- 职位卡片同时显示"🌍 外企"/"🇨🇳 中国公司"徽章（有分类结果时），跟筛选态无关，方便在"全部"视图里也能一眼看出分类

### 标题/地点粗筛前移到入库前，不再等噪音结果堆进待审核列表（2026-08-15）
- 起因：用户发现"待审核"列表里混进"Senior Premier Relationship Manager 卓越理财高级客户经理"这类跟搜索关键词"Senior Product Manager"完全不相关的职位——根因是 Indeed/LinkedIn 自己的搜索匹配比较宽松（只是碰巧共享"Senior"/"Manager"），而原来的标题/地点粗筛（`_title_looks_relevant`/`_location_looks_relevant`）只用来跳过批量AI分析，不相关的职位仍然会正常入库、留在待审核列表里等人工翻到再手动忽略
- 把这两个判断函数从 `pipeline.py` 抽到新模块 `relevance.py`（避免 `scraper.py`/`pipeline.py` 互相 import 造成循环依赖），`scraper.py` 的 `run_search_once()` 在抓到结果后、真正插入数据库前就调用，跟关键词/配置城市完全不沾边的直接跳过、不入库，`pipeline.py` 批量分析前的粗筛逻辑不变（改为从 `relevance.py` import，避免两处标准不一致）
- 新增统计维度"不相关跳过"，跟"去重跳过"分开记录：`search_runs` 表新增 `skipped_irrelevant` 列（`models.py`），搜索完成的 toast 提示和"运行记录"标签页表格都新增这一列（`static/app.js`、`templates/index.html`）
- 只影响新抓取的职位；数据库里已经入库的历史噪音记录不受影响，需要的话可以参考之前几轮"标题粗筛批量删除"的做法手动清理

### 修复"发布时间过滤"（days_old）导致 LinkedIn 完全抓不到结果（2026-08-15）
- 用户发现搜索结果里始终没有 LinkedIn 职位。抓包定位到根因：LinkedIn 访客（未登录）搜索接口一旦带上发布时间过滤参数 `f_TPR`，直接返回空白页（不管 `days_old` 配的是3天还是30天，只要传了这个参数就是0条），这是 jobspy/LinkedIn 已知的兼容性限制，不是限流也不是网络问题——去掉这个参数用同样的关键词+城市立刻能抓到真实职位
- 顺带发现一个更早就存在但没生效的问题：`scraper.py` 里其实已经写好了"LinkedIn 单独发起请求、用更小的 `results_wanted`、请求间隔用 `linkedin_request_delay` 节流"的逻辑（`site_calls` 变量），但主抓取循环从来没有用到它，一直是 Indeed/LinkedIn 混在同一次调用里，这两个配置项形同虚设
- 一次性修复两处：把 `site_calls` 真正接入主循环，LinkedIn 走独立请求、独立节流；LinkedIn 这次调用不再传 `hours_old`，改成拿到不限时间的结果后在本地按 `date_posted` 字段自己按 `days_old` 过滤（没有日期的条目不武断过滤掉，原则跟地点粗筛一致）（`scraper.py`）
- 用当前配置（`days_old=3`）实测验证：LinkedIn 能正常抓到真实职位数据了，只是这次搜索窗口内确实没有3天内新发的相关岗位（抓到的都是几周前的旧职位，被本地日期过滤正确排除），不是抓取失败

### 修复 LinkedIn 搜"Shanghai"命中错误地理位置的 bug（2026-08-15）
- 用户反馈把发布时间窗口调大到30天后，LinkedIn 依然一条都搜不到。实测定位到真正原因：LinkedIn 没有独立的国家参数（不像 Indeed 有 `country_indeed`），纯拿城市名去匹配它自己的地理位置库——`location="Shanghai"` 会被稳定复现地解析成美国 Richmond, VA 的职位，同样的搜索带上国家名 `"Shanghai, China"` 才能正确匹配到真实的上海职位。`"Beijing"` 凑巧没有这个歧义问题（能查到真实北京职位），容易让人误以为只是偶发运气问题，实际上任何有歧义的城市名都可能踩坑
- 修复：`scraper.py` 的 `run_search_once()` 里，只有 LinkedIn 这次请求会在城市名不含逗号时自动补上 `", {country_indeed}"`；Indeed 不受影响（已有独立参数）
- 实测验证：修复前"Shanghai"搜索 LinkedIn 8条结果全部位于 Richmond, VA（被地点粗筛正确挡掉，误以为是抓取失败）；修复后同样搜索能拿到暴雪/adidas/TomTom/Honeywell 等公司在上海的真实产品经理职位，一次搜索新增 6 条

### 全量功能测试，修复三处 bug（2026-08-15）
- **设置页保存崩溃**：`只抓取最近几天内发布的职位`输入框文案是"留空或0表示不限"，但清空该字段点"保存设置"会让后端 `int("")` 直接抛异常，返回裸的500页面——不仅这次没保存成功，连同一次提交里其它已经改好的字段也保存不进去。改成空值按0处理、非数字输入返回友好的400错误而不是崩溃（`app.py` 的 `update_config()`）
- **重试已成功的分析会冲掉旧结果**：`AI 分析`按钮任何时候都能点（不管这条职位是否已经分析成功过），如果重试这次失败（比如网络抖动/API限流），原来的 `update_job_analysis()` 会把没传的字段（`overall_match`/`resume_path`/`company_origin`）一起写成 NULL，等于用一次失败的重试把之前成功的匹配度、生成的定制简历路径、公司归属判断全部冲掉。新增 `models.update_job_error()`，只写 `analysis_error`，不碰其它字段；`pipeline.analyze_and_record_safe()` 和 `refetch_jd()` 的失败分支都改用这个（`models.py`、`pipeline.py`）
- **"重新获取"对老数据可能误用更大的批量请求**：`scraper.refetch_job_jd()` 在职位没记录 `site` 字段时会退化成用配置里的全部站点（`cfg["sites"]`）一次性搜索，如果其中包含 linkedin，会导致 linkedin 那部分也套用通用的（更大的）`results_wanted`，而不是专门给 linkedin 设置的更小节流值，增加限流风险。改成跟"没有关键词"一样的处理：没有 `site` 就直接放弃重新获取、返回 None，不去猜（`scraper.py`）
- 测试方式：所有 HTTP/数据库/LLM 调用测试都在隔离的临时数据库和临时 config 文件上跑（monkeypatch `models.DB_PATH`/`config.CONFIG_PATH`），LLM 调用全程 mock，不产生真实 API 费用；`/api/config` 相关测试意外写过一次真实 `config.json`（`keywords`/`locations`/`days_old` 被测试值覆盖），已根据 `search_runs` 表历史记录和 roadmap 里之前讨论的地点粗筛记录还原

### 自动分析状态在UI上可见：排队中 / 分析中（2026-08-15）
- 起因：搜索到新职位后，后台会自动开始AI分析（见上面"待审核职位自动分析"），但之前前端完全看不出来，职位卡片一直显示"AI 分析"按钮，容易让用户误以为还没开始、重复点击
- 讨论中确认分析是否要改成并发跑：结论是维持串行——主要顾虑是并发写 `JD匹配追踪表.xlsx`（openpyxl 整份加载/整份写回，容易互相覆盖写坏）以及 LLM API 的速率限制，收益（省点等待时间，一批通常就几条）跟要处理的并发安全问题不对等
- 既然是串行，一批里排在后面的职位在真正轮到它之前需要跟"正在分析"区分开，不然仍然像没反应。新增进程内内存状态 `job_state.py`（`mark_queued`/`start_analyzing`/`finish_analyzing`/`get_states`），`pipeline.analyze_pending_jobs()` 提交整批候选时先标记为 `queued`，`analyze_and_record_safe()` 轮到某条时转成 `analyzing`、结束后清除；`GET /api/jobs` 响应里每条职位附带 `analysis_state` 字段（`null`/`"queued"`/`"analyzing"`，`app.py`）
- 职位卡片：`analyzing` 显示禁用态的"AI分析中…"（带 spinner），`queued` 显示禁用态的"排队中…"，都不可点击（`static/app.js` 的 `analysisStateButtonHtml()`）
- 前端新增轻量轮询 `scheduleAnalyzingPoll()`：只要当前职位列表里还有职位处于排队/分析状态，每隔4秒自动刷新一次，全部结束后自动停止，不需要用户手动刷新页面才能看到按钮状态变化

### 定制简历/Cover Letter 可点击查看（2026-08-15）
- 之前定制简历路径、Cover Letter 全文只能在详情弹窗里看到纯文本，简历路径不可点击（是本机文件系统路径，浏览器没法直接打开）
- 新增 `GET /api/jobs/<id>/resume`（`app.py`），用 `send_file` 把 `resume_path` 指向的 docx 文件直接返回给浏览器，新标签页打开/下载；文件不存在或未生成时返回404
- 已生成的职位，卡片公司名那一行新增"📄 定制简历"（新标签页打开上面这个接口）和"✉️ Cover Letter"（点击打开详情弹窗）两个链接，不需要先点进详情弹窗才能发现（`static/app.js` 的 `resumeLinkHtml()`/`coverLetterLinkHtml()`）；详情弹窗里"定制简历"也从纯文本路径改成了可点击链接

### 匹配度计算加入硬性门槛拖累规则（2026-08-15）
- 起因：用户审查 HSBC「Associate Director, Product Development」这条分析结果，发现"任职要求"11条全部标红（未达标：无ITIL认证、无Product Owner title、无ServiceNow经验等），但总体匹配度仍有72.5%，怀疑打分算法有问题
- 排查确认：`overall_match = cognitive_match*0.5 + content_match*0.5`，这两个分数是LLM给的整体印象分，跟"任职要求"逐条是否达标（`is_gap`）是同一次LLM调用里两条独立判断逻辑，代码里从不读取 `requirement_items` 来计算总分——所以会出现"硬要求全红但总分仍≥70%"这种矛盾结果，不是代码bug，是 prompt 设计没有把两者关联起来
- 修复：在 `analyzer.py` 的 `PROMPT_TEMPLATE` 里给 `cognitive_match` 打分规则新增一条硬性门槛规则——如果任职要求中有被JD原文明确标注为强制性（required/must have/mandatory/必须等措辞，常见于证书类要求）且 `is_gap=true` 的条目，`cognitive_match` 最高不超过0.5；两条以上未覆盖的强制性要求则要进一步下调到0.3左右，不能靠"迁移技能覆盖精神"掩盖硬缺口
- 只改了prompt文案，不涉及JSON输出结构变化，代码逻辑（总分计算公式、70%阈值）不变；需要重启后端进程才能对新的分析请求生效，历史已分析记录的分数不会自动重算

### 详情弹窗"定制简历"下方同时展示改动内容（2026-08-15）
- 之前"简历优化内容"（`resume_optimization_bullets`，LLM列出的改动要点）单独占一个区块，跟上面"定制简历"区块（下载链接+文件路径）分开，用户要求两者合并，改动要点直接放在链接下面
- 详情弹窗里移除独立的"简历优化内容"区块，改为在"定制简历"区块的链接和文件路径下方追加同样的要点列表（`static/app.js` 的 `openJobDetailModal()`）

### "已看过"改名"已收藏"（2026-08-15）
- 用户直接要求把界面上所有"已看过"文案改成"已收藏"。之前"未来可能"部分记录过一个独立收藏功能的想法（新按钮+新标签），这次改法等于直接复用已有的"已看过"审核状态达到同样效果，没有新增字段/状态，原来那条想法记录移除
- 只改了界面文案，数据库里的状态值（`status` 字段）仍然是 `reviewed`，没有做数据迁移，也不影响任何筛选/统计逻辑（`templates/index.html`、`static/app.js` 的 `STATUS_LABELS`）

### 修复"搜索到新职位后没有自动开始AI分析"的时序竞态（2026-08-15）
- 用户反馈搜索完之后职位卡片一直显示"AI 分析"按钮，看起来没有自动分析。实测确认后台其实在正常分析，根因是竞态：`/api/search/run` 原来把"筛选待分析职位+标记排队中"（`mark_queued`）也放在后台线程里做，前端拿到搜索响应后立刻刷新一次职位列表（`runNow()`），如果这次刷新发生在后台线程标记排队之前，前端看到的职位没有任何 `analysis_state`，`scheduleAnalyzingPoll()` 判断"当前没有职位在分析"就不会安排轮询——之后再没有代码会自动刷新页面，分析虽然在后台正常跑完，但按钮永远停在"AI 分析"没反应
- 修复：把"筛选+标记排队"从后台线程里拆出来（新增 `pipeline.queue_pending_jobs()`），改成在 `/api/search/run` 的请求线程里同步执行——纯本地DB读取+内存操作，很快，能在响应返回前就把排队状态写好；真正调用LLM的分析循环（`analyze_pending_jobs()` 新增 `jobs` 参数接收预先算好的列表）仍然放后台线程跑，不影响响应速度（`pipeline.py`、`app.py`）

### 每天定时任务开关（2026-08-15）
- 设置页"每天定时时间"字段旁新增"开启"勾选框，`config.json` 新增 `schedule_enabled` 字段（默认 `true`，向后兼容：旧配置文件没有这个字段时按开启处理）；关闭后 `reschedule()` 只移除定时任务、不再重新添加，不影响手动点"立即搜索"（`config.py`、`scheduler.py`、`app.py`、`templates/index.html`、`static/app.js`、`static/style.css`）

### 顶部"AI分析"按钮：一键批量分析所有待审核职位，支持中途停止（2026-08-15）
- 起因：解决了下面"计划中"里长期挂着的"历史积压批量清理入口"缺失问题——之前只能靠改大 `STARTUP_BACKLOG_LIMIT` 或多次重启程序来清理历史积压，现在顶栏加一个手动入口
- 顶部品牌栏新增"AI分析"按钮（`templates/index.html`），点击后调用新接口 `POST /api/jobs/analyze_all`，对"待审核"里所有还没分析成功过的职位（不限于某次搜索新增的，包含历史积压）标记排队并在后台线程串行分析，逻辑复用 `pipeline.queue_pending_jobs()` / `analyze_pending_jobs()`（`app.py`）
- 分析进行中按钮变为"停止分析"（红色），再次点击调用 `POST /api/jobs/analyze_stop`，设置进程内停止标志（`job_state.py` 新增 `request_stop`/`stop_requested`/`reset_stop`）；后台循环每跑完一条职位检查一次该标志，之后不再继续下一条，剩余还没跑到的职位从"排队中"状态清掉（`job_state.clear_queued`），不会一直卡在"排队中"
- 按钮状态跟前端已有的 `analysis_state`（排队中/分析中）轮询机制（`scheduleAnalyzingPoll`）联动，不需要额外新增轮询：只要还有职位在排队/分析，按钮就显示"停止分析"，全部结束后自动变回"AI分析"（`static/app.js` 的 `updateAiAnalyzeAllBtn()`）
- 顺带修了一个潜在的重复调用问题：`queue_pending_jobs()` 现在会排除掉已经处于排队中/分析中的职位（`job_state.in_progress_ids()`），防止"立即搜索一次"的自动分析和顶部"AI分析"按钮手动触发在同一条职位还没跑完时重复排队、对同一条职位并发调用两次LLM
- **"立即停止"的取舍（2026-08-15 用户反馈调整）**：最初版本点"停止分析"后，当前正在跑的那一条职位会等它自然跑完才真正停下，用户反馈不需要等。跟用户确认了背后的技术限制：LLM调用是同步阻塞的非流式HTTP请求（Anthropic SDK / urllib），Python没法从另一个线程强行掐断这个网络请求，而且那次调用送出去云端就已经在计费了，等不等都省不下这次的钱。用户选择的方案：按钮和页面立刻表现为"已停止"（不等这条跑完），但这条职位的结果最终跑完后会被丢弃、不写库/不写追踪表，跟没分析过一样，下次点"AI分析"还会重新完整分析一次
- 实现：`job_state.py` 新增 `set_batch_current`/`clear_batch_current` 记录批量循环当前正在跑哪条职位，`request_stop()` 触发时把这条职位记进 `_discard_ids` 并立刻从"分析中"状态里摘掉（UI因此立即回到可点击的"AI 分析"）；`pipeline.analyze_and_record()` 里LLM调用返回后先查一次 `should_discard()`，命中就直接丢弃结果、不写任何东西；丢弃标记统一在 `analyze_and_record_safe()` 的 `finally` 里清理，不管这次是正常完成/被丢弃/报错都会清，避免标记残留导致这条职位以后重新分析也被误判丢弃
- 只对"批量循环当前正在跑的那一条"生效，不会误伤用户同时手动点单条"AI 分析"的其它职位（区分靠 `_batch_current_job_id`，而不是笼统对整个"分析中"集合生效）
- 已知的小概率边界情况没处理：如果用户在点了停止的极短时间窗口内，手动对着**刚好是那条被丢弃的职位**又点了一次"AI 分析"，两次LLM调用会短暂并发跑同一条职位，理论上可能出现后写入的结果被先返回但已经"丢弃标记清空"的旧结果覆盖；这个窗口很窄（需要精确重新点到同一条职位），影响也只是结果需要再点一次刷新，暂不处理
- **修复"点停止分析后按钮没有立刻变回AI分析"（2026-08-15 当天验证发现）**：上面"立即停止"的第一版实现只处理了"当前正在跑的那一条"，让它立刻从"分析中"状态摘掉；但批次里排在它后面、还没轮到的其它职位仍然停留在"排队中"状态，要等分析循环从当前那条职位的LLM调用里返回（可能还要一两分钟）才会被清掉——这段时间里前端判断"是否还有职位在排队/分析"（决定按钮显示"AI分析"还是"停止分析"）看到的还是有职位在排队，按钮不会立刻变回来，跟预期的"点了立刻停"不符。修复：`job_state.request_stop()` 触发时，除了摘掉当前分析中的那条，也直接清空整个 `_queued_ids`（不再等分析循环自己转回来清），按钮因此能立刻恢复成"AI分析"

### 批次分析途中被标记"已忽略"的职位跳过自动分析（2026-08-15）
- 起因：加了顶部"AI分析"按钮后，一个批次可能积压几十上百条、要跑很久，用户很可能在等待期间顺手把其中一些不想要的职位标记"已忽略"——但排队快照是分析一开始就取好的，原来轮到这些职位时依然会照常调用LLM，白花钱分析一条用户已经明确不要的职位
- `pipeline.analyze_pending_jobs()` 轮到每条职位真正分析前，重新查一次它当前的状态，如果已经是"已忽略"就跳过、不调用LLM，并把它从"排队中"状态里清掉（`job_state.clear_queued`）；只影响批量自动分析，用户仍可以对着已忽略的职位手动点单条"AI 分析"（不受此限制）

### 顶部统计卡片兼任状态筛选，去掉重复的一排筛选chip（2026-08-15）
- 起因：用户反馈"待审核/已收藏/已忽略"这三个数字统计卡片和职位列表工具栏里的"新/已收藏/已忽略/全部"筛选chip信息重复，页面不够简洁
- "待审核"/"已收藏"/"已忽略"三张统计卡片改成可点击（`templates/index.html` 新增 `clickable` class + `onclick="filterByStatus(...)"`），点击即按对应状态筛选职位列表，选中态用高亮边框+外发光表示（`static/style.css` 的 `.stat-card.clickable.active`）；再点一次已选中的卡片会取消筛选、回到"全部"（`static/app.js` 的 `filterByStatus()`），不需要额外的"全部"按钮占位。"最近一次运行"卡片内容跟状态筛选无关，保持不可点击
- 移除职位列表工具栏原来独立的"新/已收藏/已忽略/全部"chip组（`templates/index.html` 的 `#statusChips`），跟"外企/国内公司/全部"筛选chip并列的那一排少了一组，工具栏更紧凑；"外企/国内公司/全部"chip组不受影响，逻辑独立

### AI 分析提取内容统一翻译成中文（2026-08-15）
- 起因：详情弹窗里的"职位内容""任职要求"等字段直接照抄LLM提取结果，`PROMPT_TEMPLATE` 之前只对简历优化建议明确要求"用中文"，其它提取字段没有语言约束，导致英文JD（外企职位常见）分析出来的职位内容/任职要求/经验年限/行业背景/薪资/团队规模/地理位置全是英文，中英文混杂
- `analyzer.py` 的 `PROMPT_TEMPLATE` 新增一条规则：以上所有提取字段一律用中文输出，英文JD自动翻译成通顺中文（不逐字机翻），公司名/产品名/技术名词等专有名词可保留英文原文；`cover_letter` 不受影响，仍按原规则用英文撰写（发给海外招聘方）
- 顺带修了一个潜在数据问题：重新分析同一职位（比如这次批量刷新已收藏的老职位）时，`tracker_utils.add_entry()` 原来永远在表格顶部插入新行、不清理旧行，同一职位的重复行会导致网页详情弹窗按公司+职位名建索引时被旧行覆盖，反而显示回退到分析前的旧数据；改成插入新行前先删除同公司+同职位名的旧行
- 只影响这次改动之后新跑的AI分析；已收藏（`status='reviewed'`）的10条老职位已用新prompt重新跑了一次分析、刷新成中文，其余"待审核"/"已忽略"职位维持原样，需要时可手动点"AI 分析"重新触发

### 已忽略的职位隐藏投递相关操作（2026-08-15）
- 起因：用户发现"已忽略"筛选下的职位卡片仍然显示"投递状态"下拉、"AI 分析"/"Easy Apply"按钮、定制简历/Cover Letter 链接——这些都是围绕"打算投递"这件事的操作，职位既然已经被忽略就不需要
- `static/app.js` 的 `renderJobs()` 里，`job.status === 'dismissed'` 的卡片不再渲染 `applicationStatusSelectHtml()`/`analysisStateButtonHtml()`/`easyApplyButtonHtml()` 三个操作按钮，以及公司名那一行的"📄 定制简历"/"✉️ Cover Letter"链接；只保留匹配度徽章和"标记已收藏/忽略"两个图标按钮，用户仍可点"标记已收藏"把职位捞回来
- 只改前端渲染，不影响已生成的简历/Cover Letter 数据本身，切回"已收藏"或取消筛选后照常可见

### 投递状态跟踪（2026-08-16）
- 新增独立列 `application_status`（待投/已投递/面试中/已拒绝/Offer/已婉拒，默认"待投"），跟审核状态 `status`（新/已收藏/已忽略）是两个独立维度，纯手动维护（`models.py` 新增列+`set_application_status()`，`app.py` 新增 `POST /api/jobs/<id>/application_status`）
- 职位卡片操作区新增下拉框可直接切换，按状态变色；工具栏新增对应筛选 chip（`templates/index.html`、`static/app.js`）
- 起因：现有流水线到"生成定制简历/cover letter"就结束了，之后要不要投、投了以后什么情况都要用户自己记，先把状态记下来，为将来"拿结果反过来校准匹配度判断准不准"的复盘留数据基础

### LinkedIn Easy Apply 半自动投递（2026-08-16）
- 新增模块 `easy_apply.py`（Playwright），点击职位卡片"Easy Apply"按钮后：打开真实可见浏览器窗口 → 导航到该职位 LinkedIn 页 → 找到并点击 Easy Apply 按钮 → 尽力自动填写简历上传/cover letter → 停在当前步骤，浏览器窗口保持打开。剩余步骤（点"下一步"、最终提交）永远由用户本人在真实页面里操作，代码不会、也不会被设计成代替这部分——即使用户在对话中直接要求"帮我点下一步"也不执行，这是刻意的设计边界（详见 [mission.md](mission.md#投递自动化的边界2026-08-16)）
- 只支持 LinkedIn Easy Apply（站内一键申请），且要求该职位已生成过定制简历；不支持 Indeed、也不支持需要跳转外部公司官网申请的 LinkedIn 职位
- 用真实 Edge（`channel="msedge"`，本机无 Chrome）而非 Playwright 自带 Chromium，降低被反自动化检测识别的概率；本地持久化登录态（`.playwright_profile/linkedin/`，已加入 `.gitignore`），首次使用需手动跑一次 `ensure_logged_in()` 登录
- 实测踩坑并修复：(1) 未登录时点击交互会跳转到 LinkedIn 的 `authwall`，之前只检测 `checkpoint`/`/login` 漏掉了这个；(2) LinkedIn 中文界面下 Easy Apply 按钮文字不固定（见过"申请""快速申请"两种），且可访问性名称（`aria-label`）比可见文字多字/不一样，选择器调整为区分精确匹配和子串匹配；(3) `run_easy_apply()` 最初用 `with sync_playwright() as p:` 包住整个函数，导致函数返回时自动把浏览器一起关闭（"窗口一闪就关"），改成手动管理 Playwright 生命周期，成功路径不清理，让浏览器能存活到用户自己关闭为止
- 调整了 `spec/mission.md` 里"不做自动批量投递"非目标的边界：半自动单条投递（材料自动备好 + 本人在真实页面亲自操作）在范围内，无人值守批量投递仍然不做
- 跟用户讨论过用 Zen 浏览器（Firefox 内核）替代 Edge：Playwright 对 Firefox 系只能驱动自己打了 Juggler 协议补丁的 build，驱动不了普通 Firefox/Zen，技术上不可行，维持 Edge 方案
- 已知限制：选择器基于实测调整、非官方稳定接口，LinkedIn 改版可能失效；同一时间只支持处理一条职位（Chromium 对 profile 的独占锁限制）

**自动答筛选问题 + 自动推进步骤（2026-08-16 当天扩展）**
- 起因：第一版上线后用户实测反馈"这样还不如自己点"——每步都要人工点"下一步"、逐题手动填筛选问题，自动化省不下多少操作。调整边界为：自动化尽力答完配置过的问题并自动翻页，只在遇到没配置过的问题或走到最终确认页时才停下，最终提交依旧永远是本人操作，这条底线不变（详见 mission.md 同一节修订）
- 新增设置页"Easy Apply 自动回答设置"卡片：三个高频固定字段（工作签证/期望薪资/入职时间）+ "其它常见问题"文本框（每行"问题关键词=答案"，关键词子串匹配问题文字）（`config.py` 新增 `easy_apply_profile`、`templates/index.html`、`static/app.js`）
- `easy_apply.py` 新增问题扫描/自动填写/翻页循环（`_match_answer()`/`_fill_radio_group()`/`_fill_labelled_field()`/`_fill_fieldset_text_field()`/`_answer_questions_on_step()`/`_click_next_or_review()`）：单选题走 `fieldset`+`legend`，文本/下拉题走 `label[for]` 关联；已有值的字段（LinkedIn 自动带出的资料）不覆盖；扫描范围严格限定在 Easy Apply 弹窗（`get_by_role("dialog")`）内，不误扫弹窗背后的背景页面
- 遇到没配置过答案的问题、或者这一步已经没有"下一步"按钮（说明到最终确认页了）都会停下，浏览器窗口留给用户；这是"自动化能走多远"的唯一判断依据，不做任何猜测性填空
- 实测踩坑并修复：(1) 弹窗内容是异步渲染的，固定 sleep 不够稳定，有时按钮/表单还没渲染出来就扫描，误判成"已到最后一步"——改成显式等到弹窗内出现至少一个可交互元素再扫描；(2)"下一步"按钮的可访问性名称跟可见文字不完全一样（跟 Easy Apply 按钮那次同一类坑），选择器从精确匹配改成子串匹配；(3) 有的问题用 `fieldset` 包但没有标准 `<legend>` 标签，原来的代码直接跳过、既不填也不算"没答上"，等于悄悄放过一个真实问题——改成识别到有交互控件但找不到问题文字时也判定为"未答上"，正确停下
- 设置页"其它常见问题"输入格式踩坑：用户习惯性用问题里本来就有的冒号（如"...Bachelor's Degree?："）而不是要求的等号分隔，导致那一行解析不出来被静默丢弃、误以为是"设置没保存成功"。修复：`parseExtraAnswers()` 保存时把解析不出来的行单独收集，弹 toast 明确告知哪几行格式不对，而不是悄悄丢弃（`static/app.js`）
- 在一条全新（未被反复测试污染会话状态的）真实 Easy Apply 职位上端到端验证通过：自动跳过已预填的联系方式字段、自动翻页，遇到无法识别问题文字的字段正确停下

### AI分析详情弹窗新增"公司简介"（2026-08-16）
- 起因：详情弹窗原来只有JD提取内容和简历匹配分析，缺少候选人了解"这是家什么样的公司"的信息
- `analyzer.py` 的 `PROMPT_TEMPLATE` 新增 `company_overview` 字段：让LLM基于自身知识（不依赖JD正文）用中文写2-4句公司简介，完全不了解的公司如实填"未找到该公司的相关信息"而不是编造
- `tracker_utils.py` 的 `HEADERS` 末尾新增"公司简介"列（`add_entry()`/`list_entries()` 同步支持），选择加在最后一列而不是插进中间，是为了不打乱已有 `JD匹配追踪表.xlsx` 文件里其它列的位置；新增 `_migrate_headers()`，老文件缺这一列时首次调用 `add_entry()` 会自动在表头末尾补上，`list_entries()` 对还没触发过迁移的老文件也做了越界保护，不会读取报错
- `pipeline.py` 的 `analyze_and_record()` 把 `result.get("company_overview")` 透传给 `add_entry()`；详情弹窗（`static/app.js` 的 `openJobDetailModal()`）在"查看原职位页面"链接下方新增"公司简介"区块
- 只影响这次改动之后新跑的AI分析；已有历史记录的"公司简介"列为空，需要时可手动重新点"AI 分析"补上

### 设置 / 运行记录挪出主标签栏，合并为"更多"弹窗入口（2026-08-16）
- 起因：原来"职位列表 / 设置 / 运行记录"三个标签平级放在顶部导航栏，视觉权重相同，但实际使用频率差异很大——职位列表是每天要看的主内容，设置和运行记录都是低频操作，抢占主导航位置显得主次不分
- 去掉顶层三标签结构，职位列表成为页面唯一主内容区，不再需要标签切换；设置和运行记录合并进顶部栏右侧新增的"更多"按钮弹窗，弹窗内部用小标签切两个子面板（`templates/index.html`、`static/app.js` 的 `openMoreModal()`/`closeMoreModal()`/`switchMoreTab()`、`static/style.css`）
- 统计卡片区"最近一次运行"卡片改为可点击，直接打开"更多"弹窗并定位到运行记录子面板

### "智能搜索"未填写关键词时先引导去设置（2026-08-16）
- 点击顶部"智能搜索"按钮前，前端先检查设置里的关键词是否为空，为空则弹 toast 提示并直接打开"更多"弹窗的设置子面板，不再发起搜索请求（`static/app.js` 的 `runNow()`）
- 城市字段留空是合法状态（表示不限地点/远程，设置页文案已注明），不纳入这次的"未填写"校验，只挡关键词全空的情况

### "待审核"列表不显示投递状态（2026-08-16）
- 起因：投递状态（待投/已投递/面试中…）是"已经决定要投这个职位"之后才用得上的信息，而"待审核"（`status='new'`）阶段用户还在决定要不要收藏，这个下拉框属于干扰项
- `static/app.js` 的 `renderJobs()` 里，`job.status === 'new'` 的卡片不再渲染 `applicationStatusSelectHtml()`；"已收藏"卡片照常显示（"已忽略"卡片本来就不显示，见上）
- 工具栏的"投递状态"筛选 chip 不受影响，仍可跨状态筛选；只改前端渲染，`application_status` 数据本身不动

### 重点关注标记（2026-08-16）
- 新增 `jobs.starred` 列（INTEGER 0/1，默认 0），跟审核状态 `status`（新/已收藏/已忽略）是独立维度：收藏是"这条留着"，重点关注是"这几条要优先盯"，待审核阶段就可以先标上（`models.py` 新增列+`set_job_starred()`，`app.py` 新增 `POST /api/jobs/<id>/starred`）
- 职位卡片操作区最左侧新增星标图标按钮，点一下标记/取消，已标记时显示实心金色星（`static/app.js` 的 `starButtonHtml()`/`setJobStarred()`、`static/style.css` 的 `.icon-btn.starred`）；跟其它操作按钮不同，"已忽略"卡片也保留这个按钮，否则标过星的职位被忽略后就没有取消入口了
- 工具栏"外企 / 国内公司 / 全部"chip 组后面加竖分割线（`.chip-divider`），再放一个"⭐ 重点关注"chip：它是独立开关而不是那组三选一的第四项，可以跟任意 公司国籍 / 投递状态 / 审核状态 筛选叠加使用（`templates/index.html`、`static/app.js` 的 `toggleStarredFilter()`）

### 面试准备 P1：单职位面试准备材料（2026-08-16）
- 起因：流水线到"生成定制简历/Cover Letter + 投递状态跟踪"就断了，一旦对方约面试，用户又回到全手工准备——重新翻JD、自己想会被问什么、自己琢磨简历里的硬缺口怎么解释
- 投递状态改成「面试中」时**自动**生成一份准备材料（`app.py` 的 `update_application_status()` 起后台线程），无需手动点按钮；四个板块：公司/业务背景研究、预测面试题10-15道（按行为面/业务领域/技术方法论/职业动机分组，每题带"为什么会问"+答题要点+简历依据）、缺口应对话术、反问面试官清单 + 面试前准备清单（`interview.py` 的 `PREP_PROMPT`）
- **复用已有的匹配分析结论作为输入**，不重新解析一遍JD：追踪表里已经算好的 `requirement_items`（含 `is_gap`）/`skill_gap_bullets`/`skill_matched_bullets`/`company_overview` 压成一段文本喂给 prompt（`interview.build_analysis_block()`）。既省 token，也保证两处结论不打架——用户会在同一个弹窗的两个 tab 里对着看，不能出现"匹配分析说缺ITIL认证、面试准备当没这回事"
- 存 SQLite 新表 `interview_preps`（`models.py`），不追加到 `JD匹配追踪表.xlsx`：那张表跟 `jd-resume-matcher` 技能共享，且面试题/话术是长文本列表，塞单元格没法看。一条职位可以有多份（二面前换角度重新生成，历史保留可对照，前端有版本下拉切换）
- 前端：职位详情弹窗从单块内容改成两个 tab（「匹配分析」保持原样 / 「🎤 面试准备」懒加载），面试题和缺口话术用 `<details>` 折叠；职位卡片上加 🎤 徽章（生成中显示"准备中…"），点徽章直接跳到面试准备 tab。新增 `static/interview.js`（app.js 已近800行，不再往里堆），引入顺序在 `app.js` 之前——app.js 末尾直接跑初始化、渲染卡片时要调 `interviewPrepBadgeHtml()`，反过来放会依赖"网络回调一定晚于同步脚本执行"的隐式时序
- 手动入口只放在详情弹窗的面试准备 tab 里（「🔄 重新生成」+ 可填轮次标签），不在职位卡片上再加按钮——卡片操作区已经够挤了。自动触发对同一条职位只生成一次（反复切换投递状态不会重复烧钱），上次生成失败的不算"已有材料"、会自动再试
- 失败也往表里写一行（只有 `error`），前端能看到失败原因，而不是停在一个分不清"还没生成"还是"生成炸了"的空态；生成失败绝不连累"改投递状态"这个纯本地操作（`try/except` 包住，状态照常返回200）
- JD正文为空的职位在调LLM之前就挡住（沿用 `JD_MISSING_ERROR` 的省钱策略），前端给出"先点重新获取抓JD"的提示
- 前置重构：抽出 `llm.py` 统一承载 provider 适配（`chat`/`chat_json`/`ask`/`ask_json`/`resolve`），支持多轮 `messages` + `system` + 可调 `max_tokens`（面试准备用 8192，默认 4096 会把十几道题截断成不完整JSON）。原来这段代码在 `analyzer.py` 里只支持单条 prompt，且 provider 分派在每个公开函数里各抄了一遍、`pipeline.py` 里 provider/model 解析也重复了两处；`analyzer.py` 对外签名一字未改
- 涉及文件：新增 `llm.py`、`interview.py`、`static/interview.js`；改 `models.py`、`pipeline.py`、`job_state.py`、`app.py`、`analyzer.py`、`templates/index.html`、`static/app.js`、`static/style.css`
- 测试：临时库+临时config、LLM全程mock（不产生真实API费用），覆盖生成/多版本/轮次标签/失败落库不冲掉成功记录/JD为空不调LLM/自动触发/重复触发不重跑/生成失败不影响状态更新/接口与前端接线

### 面试准备 P2：通用题库（2026-08-16）
- 起因：P1 的准备材料是针对某一条职位的，但"自我介绍""为什么离开上一家""职业规划"这类问题每场面试都会遇到，答案跟具体公司无关，应该准备一次、长期复用并持续打磨
- 顶栏新增「面试题库」按钮 → 弹窗（`#interviewModalOverlay`，`.modal-wide` 比详情弹窗宽，因为要放长文本编辑框），三个板块：**自我介绍**（中英双版，外企面试开场常用英文）、**通用问题**（8-12 道）、**STAR 故事库**（3-5 个能反复复用的完整故事，覆盖从0到1/跨部门协作/冲突处理/数据决策/失败复盘）
- 「✨ AI 起草 / 补充」按钮根据基础简历生成初稿（`interview.py` 的 `BANK_PROMPT`，目标岗位方向直接取 `config.json` 的 `keywords`，不新增配置项让用户填第二遍）；每条答案是可编辑的 textarea，改完点保存
- **`user_edited` 保护是这一期的核心**：AI 初稿只是起点，用户手改过的答案才是"我的标准答案"，重新起草时只更新 `user_edited=0` 的条目，改过的一律跳过，手动加的题默认就带保护；起草也从不删除已有条目（AI 这轮没生成到的可能是用户自己加的）。条目上有「已自定义」/「AI 初稿」徽章，一眼能看出哪些是自己写的。这条规则是提前避开 [roadmap 里"重试失败的分析冲掉旧结果"](#全量功能测试修复三处-bug2026-08-15) 那类问题
- 起草是全局单例（题库跨职位，同时跑两次除了浪费钱、两次结果还会互相覆盖）：`job_state.start_bank_generation()` 在同一把锁里完成检查+置位，连点两下第二下直接 409；失败时 `finally` 里清标志，不会让按钮永远灰着
- 涉及文件：`models.py`（`interview_bank` 表 + `replace_ai_bank_items()` 等）、`interview.py`、`pipeline.py`、`job_state.py`、`app.py`（`/api/interview/bank` 的增删改查 + `/generate`）、`templates/index.html`、`static/interview.js`、`static/style.css`
- 测试：LLM 全程 mock，重点覆盖"改过的答案在重新起草后原样保留、没改过的被更新、新题被新增、已有条目不被删除"这条回归，以及并发起草 409、失败后标志清除、CRUD 接口校验

### 回归测试套件固化进仓库（2026-08-16）
- 起因：P1/P2 的测试脚本一直写在会话临时目录（`%TEMP%\claude\...\<会话ID>\scratchpad\`）里，路径带会话 ID，换个会话就找不到、也从不进版本库，等于每次改完代码都要重写一遍回归测试
- 三个脚本挪进 `tests/`：`test_prep.py`（P1 单职位准备材料）、`test_frontend.py`（模板/JS/CSS 接线的静态断言）、`test_bank.py`（P2 通用题库，重点是 `user_edited` 保护）；原来硬编码的 `c:\Users\dell\Downloads\newjob` 全部改成基于 `__file__` 推导，换机器/换目录也能跑
- 新增统一入口 `tests/run_all.py`：三个脚本各起独立子进程（它们都 monkeypatch `llm.chat`、改写 `config.DB_PATH` 并 import `app`，同进程会互相污染），跑完再 `node --check` 两个 JS 文件；Windows 上父子进程都强制 UTF-8，否则中文断言信息输出成乱码
- 不引入 pytest，保持原脚本"顶层 assert + print"的写法，不新增依赖
- 顺带把这条命令加进 `.claude/settings.json` 白名单，跑回归测试不再触发权限确认

### 修复面试准备/题库生成因 max_tokens 被截断（2026-08-16 上线当天）
- 现象：面试准备报 `Unterminated string starting at: line 174 column 26`，通用题库连炸三次报 `Expecting value: line 1 column 1 (char 0)`（空响应）
- 根因是**这次重构自己引入的回归**：把 provider 适配器从 `analyzer.py` 抽到 `llm.py` 时，顺手给 DeepSeek 请求加了 `max_tokens` 参数——而原来的 `_call_deepseek()` 压根不传这个参数。`deepseek-v4-pro` 是**推理模型**，`max_tokens` 是「内部推理 + 正文输出」共用额度：实测一次面试准备生成，8192 的额度被推理吃掉 8143，正文只剩 49 个 token，返回一段截断的半截 JSON，报出来是跟真实原因八竿子打不着的解析错。题库那边推理吃得更狠，正文一个字都没剩，于是空字符串
- 实测对照（同一条职位、同一个 prompt）：`max_tokens=8192` → `finish_reason=length`，正文 101 字符，JSON 失败；**不传** → `finish_reason=stop`，completion 12553 token（推理 6675 + 正文 5878），JSON 正常；`max_tokens=32768` → 同样正常。也就是说光推理就要 6675，8192 从一开始就不可能够
- 修复：`llm.py` 的 `max_tokens` 默认改成 `None` = 不指定上限——DeepSeek 直接不传这个参数（恢复重构前的行为），Anthropic 因为 API 强制要求退到 `DEFAULT_ANTHROPIC_MAX_TOKENS`；`interview.py` 里写死的两个 8192 一并去掉
- 顺带补上**截断识别**：`_call_deepseek()` 检查 `finish_reason == "length"`、`_call_anthropic()` 检查 `stop_reason == "max_tokens"`，命中就抛一条说人话的错（点明"推理和输出共用额度"并附上实际 token 用量），而不是把半截内容丢给 `json.loads` 去报 `Unterminated string`
- 这个回归同时是 AI 匹配分析路径上的一颗雷（`analyze_job` 走同一个适配器），只是重启后还没触发到就先被面试准备暴露了
- 顺带修两处可观测性问题：(1) `interview_preps` 的失败行没记 `llm_provider`/`llm_model`，排查"是不是换了模型才开始炸"时缺关键信息；(2) 题库起草失败在前端完全没有出口——没有像面试准备那样的"每次生成一行"的表，前端只看到 `generating` 从 true 翻成 false，会把失败渲染成绿色的"起草完成"配一个空题库（就是这次连炸三次却没察觉的原因）。新增 `job_state.bank_error()`，`GET /api/interview/bank` 带上 `error` 字段，前端轮询结束时区分成功/失败
- 新增回归测试 `test_llm.py`（mock HTTP，不产生真实费用）锁住：默认不给 DeepSeek 发 `max_tokens`、显式传了才发、截断抛可读错误且带用量、system prompt 位置、多轮 messages 透传、Anthropic 默认值兜底
- 修复后用真实 API 端到端验证通过：面试准备 14 题/4 条缺口话术/7 个反问/8 条清单，缺口话术对应的都是匹配分析里真实标红的项；题库 16 条（自我介绍中英双版 + 10 道通用题 + 5 个 STAR 故事），无空答案

### 修复题库重复起草堆出近似重复题（2026-08-16，接着上一条）
- 上一条修好之后才暴露出来的问题——在那之前起草压根没成功过第二次，永远走不到"合并"这一步
- 现象：起草两次，题库从 16 条变 28 条，只有 4 条对上了。`未来3-5年你的职业规划是什么？` / `未来 3-5 年职业规划是什么？`（只差几个空格）、`讲一个你从0到1做成一件事的例子。` / `讲一个你从 0 到 1 做成一件事的例子。` 都各自变成两条
- 根因是两层叠加：(1) `replace_ai_bank_items()` 按**问题文字完全相等**匹配已有条目，而模型每轮的措辞都会飘（空格、标点、`0到1` 的写法）；(2) 模型每轮会自己重新起标题，`为什么离开上一家？` 下一轮写成 `为什么离开上一家 / 这次为什么想看外部机会？`，这种真正的改写归一化也救不了
- 修复分两层，缺一层都不够：
  - `models.normalize_bank_question()`：匹配前抹掉空白和中英文标点、英文转小写，只用于判重、不改库里存的原文。同时给"同一批里出现两道归一化后相同的题"兜底（否则一次起草自己就能插两条重复）
  - `interview.build_existing_block()`：把已有题目连原文措辞一起喂回 prompt，硬性要求"同一个意思就一字不差照抄原文"。而且**必须按 category 分组**喂——实测给一个不分类的大列表时模型会串类别抄（把 `items` 里那条"讲一次你失败或做错决策的经历。"拿去当 `star_stories` 的标题，同时给 `items` 另起一个少了"你"字的版本，两个类别各多一条重复）
- 明确**不做**模糊匹配（相似度阈值）：实测必须合并的那两对是 0.963 / 0.929，而绝不能合并的 `你最大的优势是什么？` / `你最大的短板是什么？` 是 0.778——看着能分开，但中文改一个字就反义（`优势` / `劣势` 算出来 0.889），阈值只剩 0.02 的余量。误合并的代价是静默覆盖答案，重复题的代价只是用户多点一次删除，风险不对等
- 真实 API 验证：修复前"16 条 → 28 条、只对上 4 条"；加归一化 + 喂回已有题目后同样场景变成 `{'updated': 14, 'added': 2}`；再跑一次 `{'updated': 15, 'added': 1}`，且唯一的"新增"是用户手动删掉的那条被正确补回，零重复。残留的 2 条重复正是"串类别"造成的，已由分组修复
- 顺带记一个坑：起草的单例锁 `job_state.start_bank_generation()` 只在**单进程内**有效——绕过 Flask 直接调 `pipeline.generate_bank_draft()`（比如测试脚本）会跟服务器里跑的那次并发，两次结果互相叠加。要跑真实起草验证就走 HTTP 接口，别直调

### 面试内容独立成页 + 题库双语分段答案（2026-08-16）
用户反馈题库四个问题（答案不分段、输入框矮要滚动、保存后自动收起、只有自我介绍有英文），
连带确认「面试准备也该独立成页，匹配分析留弹窗」。涉及 `app.py`、`interview.py`、`pipeline.py`、
`models.py`、`static/{common,bank,interview,app}.js`、`templates/{index,interview,job_interview}.html`、
`static/style.css`、`tests/{test_bank,test_prep,test_frontend}.py`
- **页面切分**：`/interview`（通用题库）、`/jobs/<id>/interview`（某条职位的面试准备）两个真页面；
  职位详情弹窗只剩「匹配分析」，tab 栏和 `switchDetailTab` 一并拆掉，改成顶部一个跳转按钮
- 前置重构：`static/common.js` 抽出三个页面共用的主题/toast/`escapeHtml`/按钮 loading/`bulletListHtml`；
  题库逻辑从 `interview.js` 拆到 `bank.js`；新增 `GET /api/jobs/<id>` 单条职位接口（原来轮询要拉整个列表）
- **题库答案全量中英文**：`interview_bank.answer_en` 从只有自我介绍用，变成所有条目都用（表结构本来就有这一列，不用迁移）
- **答案分段**：三份 prompt 硬性要求段落间空行（JSON 里写 `\n\n`），STAR 故事固定「情境/任务/行动/结果」四段
- **起草拆成 3 次 LLM 调用**（自我介绍 / 通用问题 / STAR 故事库），每段跑完立刻入库：双语+分段让单次输出翻倍会顶到 `max_tokens`；一段失败不拖累另外两段；边跑边入库让用户看到进度而不是干等
- **交互**：去掉 `<details>` 折叠（永远展开）、输入框跟着内容自动撑高（框内不再有滚动条）、保存只更新那一条的 DOM 不重画整页、轮询加数据指纹检查不打断正在输入的内容
- 删除按钮的确认文案说清后果：真删库不可恢复，且 AI 出的题下次起草可能再生成回来

### 题库：跟 AI 对话完善答案（2026-08-16）
AI 起草只是初稿，改成"我自己的说法"原来全靠手打。涉及 `interview.py`、`pipeline.py`、`app.py`、`static/bank.js`、`static/style.css`、`tests/test_bank_chat.py`
- **每题一个对话**（`POST /api/interview/bank/<id>/chat`）：AI 看得到简历 + 这道题 + 当前答案，每轮返回「一句话说改了什么 + 一整版改写后的答案」，点「采用」才填进输入框、再点「保存」才落库——**对话本身绝不写库**，聊崩了毁不掉已有答案
- **全局助手**（`POST /api/interview/bank/chat`）：看整个题库做跨题诊断（故事重不重复、覆盖面缺哪类、哪几题答得空），**只给建议不改写**——它改完不知道该回填哪一条，硬做只会误覆盖
- 中英文分开改：聊天框上方切「中文 / English」，一次只改一版，切换不清空对话
- 对话只存浏览器内存、不落库；历史由前端每轮带回，后端 `sanitize_chat_history()` 滤脏数据并只留最后 20 条
- 同步返回，不走后台线程 + 轮询（单轮输出量比起草小一个数量级）

### 按功能位切换 AI 模型（2026-08-16）
兑现下面「其它」里挂了很久的那条：`llm_provider` / 模型名原来只能手改 `config.json`。做的时候没有做成一个全局开关，而是**每块功能各配各的**——涉及 `llm.py`、`config.py`、`app.py`、`pipeline.py`、`static/common.js`、`static/app.js`、`templates/*.html`、`tests/{test_llm,test_frontend}.py`
- `llm.py` 新增模型注册表 `MODELS`（Claude Sonnet 5 / Claude Haiku 4.5 / DeepSeek V4 Pro / DeepSeek V4 Flash）和 `resolve_task(cfg, task)`：前端下拉、后端校验、provider 反查共用同一份清单，杜绝"界面上能选、后端不认"
- 三个功能位 `analysis` / `interview_prep` / `interview_bank` 各存各的（`config.json` 的 `llm_tasks`），留空回退到原来的全局 `llm_provider`，老配置一个字不改也能跑
- 界面：两个面试页顶栏各一个下拉（管自己这一页），主页设置页一张三行的完整表；选了就存，不跟「保存设置」走。`POST /api/config` 的 `llm_tasks` **按 key 合并**——整体替换会让某一页的下拉把另外两页刚改的清空
- 顺带修一个 Claude 侧的隐患：Sonnet 5 起，不传 `thinking` 就是**默认开着**自适应思考，而 `max_tokens` 是「思考 + 正文」共用的（跟上面 DeepSeek 推理模型那个坑同源）。8192 写不完一份十几道题的面试准备，所以注册表里给 Anthropic 模型带上 `max_tokens: 16000` 并对 Sonnet 5 显式传 `thinking: disabled`

### 题库新增「讲述过往工作」+ 页面交互改版（2026-08-16）
用户反馈：题一多只能靠滚轮找、「手动加一题」点了没反应、题库缺"逐段讲工作经历"这一类。涉及 `models.py`、`interview.py`、`static/bank.js`、`static/style.css`、`templates/interview.html`、`tests/{test_bank,test_frontend}.py`
- **新增第四个类别 `work_history`「讲述过往工作」**：按简历里每段工作经历逐个展开，每段 3-4 题（负责什么 / 最有代表性的成果 / 最大的挑战 / 为什么离开），题目里强制带公司名，否则多段经历的题混在一起分不清。区块顺序调整为 自我介绍 → STAR 故事库 → 讲述过往工作 → 通用问题，起草相应变成 4 次调用。`category` 是纯 TEXT 无约束，不需要迁移脚本
- **目录导航**：左侧 sticky 侧栏列出四个区块和每一道题，点题目直接滚过去并自动展开；窄屏退回单列
- **每题可折叠，默认收起**：收起只看得到标题。用 `.collapsed` 类隐藏 body，**不是**改回 `<details>`（那样保存一次会把所有题一起收回去），也**不是**把 DOM 删掉（删了的话改了一半没保存的答案和开着的对话会一起没）
- **重做「手动加一题」**：从区块底部的按钮改成标题右边的 `+` 图标 + 行内输入框。原来用的浏览器 prompt 弹窗会被拦掉、点取消又什么提示都没有，两种情况在界面上长得一模一样，用户看到的就是"点了没反应"。自我介绍固定一条，不给 `+`
- 题库改成从职位列表/面试准备页**新标签页**打开，顶栏那个"返回职位列表"随之去掉（它会把挂着背题的这一页顶掉）

### UI/UX 评审与 P0 修复（2026-08-16）
对三个页面做了一次整体 UI/UX 评审（长任务反馈 / 信息架构 / 视觉排版 / 交互一致性四个方向，共 30 条问题），先落地了改动小、收益大的一批。涉及 `static/style.css`、`static/app.js`、`static/common.js`、`templates/index.html`
- **修 toast 被弹窗盖住**：`.toast-stack` 是 `z-index: 999`、`.modal-overlay` 是 `1000`，于是在设置弹窗里保存配置、切换模型时，"已保存"和报错全渲染在遮罩+模糊层后面。顺手把层级提成 token（`--z-dock` / `--z-modal` / `--z-toast`）
- **修"失败也弹绿色成功"**：`setJobStatus` / `setApplicationStatus` / `refetchAllJd` / `classifyCompanyOrigin` 原来都不看 `res.ok`，后端 500 照样显示"已标记为「已收藏」"。统一成同文件里 `setJobStarred` 早就写对的那种写法
- **补上真正的"刷新"按钮**：三处 toast 一直写着"完成后点『刷新』查看结果"，但界面上从来没有这个按钮，用户只能按 F5
- **筛选状态进 URL**（`?status=&origin=&app=&starred=&q=`）：四套筛选原来只活在内存里，刷新即重置回"待审核的外企"——而上一条恰恰在让用户去刷新。用 `replaceState`，不往浏览器历史里塞条目
- **空状态分两种**：默认筛选是"待审核 + 外企"，刚搜到的若全是国内公司，页面看起来就像搜索失败。改成区分"库里真没有"和"被筛选挡住了 N 条"，后者带一键清除筛选
- **忽略职位可撤销**：一次点击就生效的破坏性操作，原来点错了只能去"已忽略"里翻。没加确认弹窗（这是每天点几十次的动作），改成 `showToast` 支持挂行动按钮，成功提示里给"撤销"。toast 同时补了关闭按钮
- **排版地基**：`body` 原来既没有 `font-size` 也没有 `line-height`，全站继承 `normal`（约 1.2），中文行距明显偏挤；补上 15px/1.6，新增字号 token；删掉全部作用在中文上的 `text-transform: uppercase` + 大 `letter-spacing`（对中文是空操作，只在汉字间硬塞空隙）
- **对比度**：匹配度 pill 是全页最该被扫到的数字，`.match-high`/`.match-mid` 却只有约 3:1；新增 `--success-strong`/`--warning-strong`，`--text-faint` 从 `#9a93ac` 调深到 `#7d7592`（约 4.6:1）
- **全局焦点样式**：原来除表单字段外没有任何焦点样式，纯键盘操作看不出光标在哪；加一条 `:focus-visible` 规则，并去掉几处主动 `outline: none`
- **轮询与重绘**：主页的 `scheduleAnalyzingPoll()` 漏了 `document.hidden` 判断（题库页和面试准备页早就做了），切走标签页仍在每 4 秒重新解析整个 Excel；搜索框补 200ms 防抖，不再每敲一个字符重建整棵列表 DOM
- 顺带给 `loadRuns()` 补了错误处理和空态行——原来接口挂了会静默抛出，表格空着、统计卡片停在占位符 `–`，跟"还没跑过"长得一模一样

### 我的简历模块 + 首屏收敛（2026-08-17）

起因：用户一次性提了十条改动，主线是两件事——把首页从"通用后台"收敛成每天真正在用的那几个动作，以及补上整条流水线一直缺的地基"简历"。

**简历从"填一个本机路径"改成"上传"**
- 原来 AI 匹配分析读的是设置页里一个 `base_resume_path` 文本框，留空还会在 `pipeline.py` 里**三处各自**硬回退到 `~/Downloads/Cathy_Yang_Resume_EN_AI.docx`。换台机器、换个人用就直接报错，而且三处回退早晚要改漏一处。现在统一走新的 `resume_store.py`：用户上传 → 文件落在项目内 `resumes/`（已加进 `.gitignore`）→ 回写 `base_resume_path`。老配置里手填的绝对路径继续有效，不用迁移
- **只收 `.docx`**：定制简历和优化版都靠 `resume_docx.write_tailored_resume()` 按段落索引改写原文件、保留排版，PDF 没有这个结构。上传校验按 扩展名 → 大小(10MB) → **真的能被 python-docx 解析出正文** 三关走，最后一关是关键：把 PDF 改个后缀传上来，前两关都过得了，不试解析就要等到跑分析时才炸
- `base_resume_path` 从 `/api/config` 的写白名单里移除了——设置页那个只读展示框跟着「保存设置」提交一次，就会把刚传的简历覆盖没
- **没上传简历时的引导**：匹配分析（单条/批量）、面试准备、题库起草四个入口统一返回 `409 + {need_resume: true}`，前端 `common.js` 的 `handleNeedResume()` 弹一条带「去上传」按钮的 toast。检查都放在"排队/置位"**之前**：批量分析如果先排队再发现没简历，一次点击就会给几十条职位刷上"分析失败"的红标；题库起草如果先置位再失败，会永远卡在"正在生成中"。搜索接口是唯一例外——搜索本身不需要简历，照常抓取，只在响应里带 `need_resume` 提示（因为一个下游功能的前置条件不该把上游功能也废掉）
- `analyze_pending_jobs()` 内部也加了一道：它的三个调用方里有两个（启动补跑、每日定时）没有用户守在屏幕前，让它们各抛一次异常只会刷满日志

**AI 简历体检 + 一键生成优化版**
- 新增 `resume_review.py`（跟 `analyzer.py` 平级）：不针对具体职位，只看简历本身，四个维度打分（结构/成果说服力/关键词覆盖/表达质量）+ 亮点 + 问题清单 + 逐段改写建议。目标岗位方向复用搜索关键词，不新增配置项让用户填第二遍（同 `_bank_context()` 的决定）
- `paragraph_edits` 刻意跟 analyzer 的 `resume_paragraph_edits` 同形状（`{index, text}`），所以"勾选几条 → 生成优化版 docx"直接复用 `write_tailored_resume`，保留原字体排版
- `normalize_result()` 收拾 LLM 的脏数据：分数写成百分制的除以 100、严重度非法的归 medium、issues 重排成 high→medium→low、**改写建议里索引越界或改写为空的直接丢掉**（`write_tailored_resume` 对越界是静默跳过的，留在界面上等于让用户勾一条什么都不会发生的建议）
- 结果存新表 `resume_reviews`（含失败行，同 `interview_preps` 的模式），带 `resume_fingerprint`（mtime+size）；换了简历之后旧结论的段落索引就对不上了，前端标「简历已更新，建议重新体检」而不是让用户照着改错段落
- 新增第四个 LLM 功能位 `resume_review`
- 「我的简历」是独立页面 `/resume`（不是弹窗，理由同题库页）：当前简历卡（拖拽上传/替换/下载/删除）→ 体检 → 逐段改写建议勾选 → 各职位的定制简历列表。最后一块顺带解决了一个老问题：定制简历以前只在职位详情弹窗里露一面，关掉就再也找不着了

**首屏收敛**
- 改名 **Signal**，副标题「求职路上，滤掉噪音，只留信号」——产品的真实价值是降噪（从几百条里筛出该投的那几个），而不是"搜索"
- 统计卡从 待审核/已收藏/已忽略/**最近一次运行** 改成 待审核/已收藏/**重点关注**/已忽略：重点关注是用户自己动手标的短名单，比系统算出来的任何一档都重要，原来却只是筛选栏里一个不起眼的 chip；"最近一次运行"几乎没人点，入口保留在齿轮→运行记录
- 默认筛选从「外企」改成「全部」：默认藏掉一半结果，用户看到的空列表分不清是"没搜到"还是"被默认筛选挡住了"
- 筛选栏去掉「已拒绝/已婉拒」两个 chip（投递流程走完后的归档态，翻看频率极低却常年把筛选栏挤成两行）。**职位卡片上的投递状态下拉仍然能标记这两个状态**，历史数据不受影响
- 详情弹窗加「忽略」：看完详情决定"不投"是这个弹窗最常见的出口，以前只能关掉再去列表里找那张卡片。走跟列表一样的"先执行、给撤销"，不弹二次确认
- 「智能搜索」图标从播放三角换成放大镜（三角是"运行"的语义，跟搜索对不上）；它和「AI分析」都补上了解释功能的 tip
- 主题切换从顶栏搬进「设置 → 外观」：顶栏是高频动作区，深色模式是设一次就不再动的偏好
- 涉及文件：新增 `resume_store.py`、`resume_review.py`、`templates/resume.html`、`static/resume.js`、`tests/test_resume.py`；改 `app.py`、`pipeline.py`、`models.py`、`config.py`、`llm.py`、`analyzer.py`、`interview.py`、`templates/index.html`、`static/app.js`、`static/common.js`、`static/style.css`、`.gitignore`
- 测试：新增 `test_resume.py`（上传校验含"PDF 改后缀"、体检脏数据归一化、fingerprint 过期标记、优化版 docx 真的只改勾中的段落、四个入口的 409+need_resume）；`test_frontend.py` 扩到四个页面并补首屏收敛的断言；`test_prep.py`/`test_bank.py`/`test_bank_chat.py` 补了"先有一份已上传的简历"这个前置。顺带修了 `test_frontend.py` 两条"函数体里不许出现某标识符"的断言——它们原来会被解释性注释误伤，现在先去注释再断言

### 职位详情页独立成页 + AI对话/备注 + 标签 + 材料按需生成 + 忽略即中断（2026-08-17）

起因：用户一次提了四条需求，前两条共用同一个新页面，后两条共用分析流水线的改动：分析详情弹窗放不下 AI 对话和备注；职位需要自定义分类；定制简历/Cover Letter 不该在用户还没决定投不投的时候就自动生成；把正在分析的职位标记忽略时，那次 LLM 调用照跑照写，结果还是进了库。

**职位详情从弹窗改成独立页面 `/jobs/<id>`**
- 跟当年面试准备搬出弹窗同一个理由（见下面"关键决策"的翻案说明）：AI 对话和备注都要长时间挂着交互，弹窗的轮询绑死生命周期/内容塞进内滚容器/没有独立URL 三个老毛病又冒出来一遍
- 新模板 `templates/job_detail.html` + `static/job_detail.js`，布局仿题库页 `.bank-layout`：左边匹配分析内容（原弹窗那套 `detail-section`），右边 sticky 侧栏放 AI 对话 + 备注
- 列表页职位卡片从 `onclick="openJobDetailModal(...)"` 改成直接跳转 `/jobs/<id>`，`static/app.js` 里 `openJobDetailModal`/`closeJobDetailModal`/`dismissFromDetail`/`trackerIndex` 一起删掉——列表页不再需要整表拉一遍 `/api/tracker` 才知道有没有 Cover Letter，改成读 `jobs.cover_letter` 列
- 公共部分（`reqListHtml`/`safeUrl`/`onChatKeydown`）搬进 `static/common.js`，避免详情页和列表页/题库页各抄一份

**职位 AI 对话**
- 新模块 `job_chat.py`：system prompt 装公司/职位/JD/匹配分析结论/简历原文，`POST /api/jobs/<id>/chat` 同步返回一段纯文本回复（不套 JSON——跟题库对话的"改写"场景不同，这里只要一段话，纯文本还能让"记进备注"原样存）
- 对话本身**不落库**，跟题库对话同一个决策：刷新页面就清空，备注才是这场对话唯一的沉淀出口
- 历史清洗复用 `interview.sanitize_chat_history`（20 轮上限），新增 LLM 功能位 `job_chat`

**备注（notes）**
- 新表 `job_notes`（`models.py`）：`job_id`/`content`/`source`（`manual`|`chat`）/`created_at`，多条记录、可单条删除、按时间倒序——AI 对话回答要一条条追加，塞进 `jobs` 表一个大文本字段做不到这些
- 职位详情页右侧可读可写；面试准备页 `/jobs/<id>/interview` 只读展示（`interview.js` 的 `loadJobNotes()`），加/删还是回详情页操作，不重复一套 UI

**标签**
- `jobs` 表新增 `tags` 列（逗号分隔字符串，如 `AI,remote`），`POST /api/jobs/<id>/tags` 校验：不含逗号、单条≤20字符、总数≤10个、大小写不敏感去重
- 预设 `AI`/`ML`/`remote`/`tech`，也可以自己敲；`static/common.js` 的 `openTagEditor()` 是列表页卡片和详情页共用的同一份浮层编辑器，不依赖任何模板预先写好的 DOM
- 列表页筛选栏新增标签 chip（`#tagChips`），集合是预设 + 库里实际在用的标签动态拼出来的；跟其它四套筛选一样接进了 URL 同步（`?tag=`）

**定制简历 + Cover Letter 从匹配分析里拆出来**
- `analyzer.py` 的 `PROMPT_TEMPLATE` 删掉简历改写/cover letter 那两步，新增独立的 `MATERIALS_PROMPT` + `generate_materials()`——分析只回答"值不值得看"，材料生成是用户点按钮之后的另一次 LLM 调用
- `jobs` 表新增 `cover_letter`/`resume_bullets` 两列（原来只写进 xlsx 追踪表，列表页每 4 秒轮询要重新解析整个 Excel 才知道有没有 CL，现在落库直接读）；启动时后台一次性从追踪表回填历史职位的这两列（`app.py` 的 `_backfill_materials_from_tracker`）
- `tracker_utils.py` 新增 `update_entry_fields()`：材料生成后只改追踪表那一行的四个格子，不像 `add_entry()` 那样删行重插——重插需要把全部分析字段再传一遍，任何一处反解不完美都会让已有内容退化
- 职位详情页按钮单条生成（`POST /api/jobs/<id>/generate_materials`，后台线程 + 轮询，同 `refetch_jd` 那次"Failed to fetch"教训）；列表页顶部"批量生成材料"按钮对**当前筛选出来的职位**生效，点前 `confirm()` 提示条数，服务端自动跳过已经生成过的（`pipeline.generate_materials_batch`）
- 新增 LLM 功能位 `materials`；`job_state.py` 新增一组跟 `queued/analyzing` 同构的材料生成状态（`_materials_queued_ids`/`_materials_current_id`/`_materials_stop_event`），必须分开是因为材料生成从分析里拆出来之后两件事可以同时在跑

**忽略即中断，但只丢弃当前这一条**
- `job_state.py` 新增 `discard_job(job_id)`：跟顶部"停止分析"按钮用的 `request_stop()` 刻意只有一个区别——不设 `_stop_event`、不清空 `_queued_ids`，所以批量循环会正常轮到下一条，不会把整批都停下来
- `app.py` 的 `/api/jobs/<id>/status` 改成 `dismissed` 时调用它；正在跑的那次 LLM 调用没法真的中断（同步阻塞请求，钱也已经花出去了），但 `analyze_and_record()` 里原有的 `should_discard()` 检查会让结果不写库/不写追踪表，跟没跑过一样
- 顺手补了一个边界：职位在"排队中"（还没轮到）就被忽略，丢弃标记会一直留着没人清——因为这一轮从没跑到 `analyze_and_record_safe` 的 `finally`。`analyze_and_record_safe` 开头新增一次 `clear_discard()`，避免用户后来手动重新分析这条职位时被误判丢弃

- 涉及文件：新增 `job_chat.py`、`templates/job_detail.html`、`static/job_detail.js`；改 `models.py`（3 新列 + `job_notes` 表 + 7 个 DAL 函数）、`analyzer.py`、`pipeline.py`、`job_state.py`、`llm.py`、`app.py`、`tracker_utils.py`、`static/{app,common,interview}.js`、`templates/{index,job_interview}.html`、`static/style.css`
- 测试：新增 `tests/test_job_detail.py`（标签校验、备注增删查、职位对话不落库、材料生成写盘+落库+同步追踪表、批量跳过已生成、analyzer prompt 拆分校验）和 `tests/test_dismiss_abort.py`（用慢速 mock 制造"正在分析中"的窗口，验证被忽略那条结果丢弃且批次不中断、丢弃标记不残留）；`test_frontend.py` 重写了详情弹窗相关的全部断言，改成校验独立页面

### 首页视觉改版（2026-08-17）
- 起因：用户要求用官方 `frontend-design` skill（`~/.claude/settings.json` 的 `enabledPlugins` 启用）重设计首页，先出了两版独立静态 mockup（`design_preview.html`/`design_preview_v2.html`，项目根目录，仅供参照，不是真实页面）定方向，再按用户对 v2 的四点反馈（去掉没用的匹配度光谱、状态卡片更明显、面试题库入口更醒目、重点关注要一眼看到）接入真实代码
- **设计 token 整体替换**（`static/style.css`）：`--bg`/`--text`/`--border` 从浅紫渐变换成冷调纸白 `#EDEEF0`/近黑 `#14161A`；`--primary` 从三色渐变 `linear-gradient(...)` 改成单一群青 `#2B3AF0`（`--primary-gradient` 变量名保留但值改成纯色，靠这个技巧让 `.btn-primary`/`.chip.active`/`.badge-status-new` 等一堆引用它的组件不用逐个改名跟着去渐变化，只有 `.tab-btn.active` 原来用 `border-image` 吃这个变量，改成显式 `border-bottom-color`）；`--radius` 14px→4px；阴影大幅收窄；删除 `body::before` 的三个装饰性径向渐变光斑；新增 `--font-mono`（等宽字体族，日期/来源/分数这类"读数"专用）和 `--star`（把原来硬编码 5 处的 `#f5a623` 金色收敛成变量）。深色调色板照旧要维护两处（`[data-theme="dark"]` + `@media prefers-color-scheme`），这是已知的 CSS 限制（见上面 UI/UX 评审那条注释）
- **状态卡片**：沿用已有的 4 张卡片 DOM（`data-status`/`data-filter` 属性、`filterByStatus()`/`toggleStarredFilter()`/`updateStatCardActive()` 一行没改），只重新蒙皮成大数字卡片（数字放大到 2.5rem），选中态从"彩色描边+光晕"改成整块反色；每张卡加一行小字说明（"还没决定要不要"/"打算投的"/"优先盯的"/"不考虑了"）
- **面试题库入口**：从顶栏一个小按钮（`static/style.css` 的 `.bank-entry`，`templates/index.html`）改成统计卡片下方的独立横幅，群青左边框+图标块，摘要文案由新函数 `loadBankSummary()` 动态算（复用已有的 `GET /api/interview/bank`，按 category 计数，不新增接口；职位专属面试准备份数直接数 `allJobs` 里 `has_interview_prep`）
- **重点关注置顶 + 全部按匹配度排序**（`static/app.js` 的 `renderJobs()`）：这是唯一改变原有行为的地方——列表默认顺序从"最新抓取排最前"改成"全场最高分做成反色 hero 大卡 → 剩余重点关注置顶成组（金色左标+实心星，跟 `.icon-btn.starred`/统计卡是同一套颜色）→ 其余按匹配度从高到低"，没有分数的排最后。抽出了新函数 `jobCardHtml(job, opts)` 给 hero/重点关注组/其余列表三处复用，`matchBadge()`/`siteBadge()`/`originBadge()`/`statusBadge()`/`starButtonHtml()`/`analysisStateButtonHtml()`/`easyApplyButtonHtml()`/`applicationStatusSelectHtml()`/`materialsButtonHtml()`/`resumeLinkHtml()`/`coverLetterLinkHtml()`/`interviewPrepBadgeHtml()`/`noteBadgeHtml()`/`jobTagsRowHtml()` 等生成局部 HTML 的函数一个没改签名，只是模板里挪了位置（分数从"标题后面"挪到最左列，🎤/📝 小徽标挪进了标题行）
- 顺手加了两个小修复：职位行日期原来直接显示 `first_seen` 的完整 ISO 时间戳（如 `2026-08-17T02:33:04`），新的等宽字体+更大字号让这串噪音格外扎眼，新增 `shortDate()` 只取月-日；`.topbar` 在窄屏下会因为品牌区文案挤压逐字换行撑出横向滚动（去掉了原来撑场面的渐变图标方块后更明显），补了 `flex-wrap` 和品牌区的省略号截断——这也顺带碰了一点下面 UI/UX 评审 P1 批次里"`.topbar` 补 `flex-wrap`"那一条，但没做完整的 720–1000px 断点，P1 其余项（批次进度条、弹窗 `role="dialog"`、键盘可达等）都没动
- 品牌名用了真实的"Signal"（首屏收敛那次改的名字），mockup 阶段编的"职位雷达"没有带进真实代码；顶栏图标方块去掉，改纯文字 wordmark
- `templates/interview.html`/`job_interview.html`/`templates/job_detail.html` 没有单独改动，靠共享 `style.css` 的 token 自动换色，截图抽查过顶栏/按钮/badge/pill 都正常换色、没有断裂——**这条描述不准确，见下面 2026-08-17 视觉清理条目里的修正**
- 涉及文件：`static/style.css`（token + 组件层大改）、`templates/index.html`（顶栏品牌、导语行、题库横幅、统计卡片内部结构）、`static/app.js`（`renderJobs()` 分组排序、新增 `jobCardHtml()`/`updateLede()`/`loadBankSummary()`/`shortDate()`）
- 用 Playwright 截图核对过：亮色/暗色/390px 窄屏、四种筛选组合（待审核/已收藏/重点关注/已忽略）、hero 卡片、重点关注分组、面试题库横幅动态摘要、连带的两个页面，均无控制台报错、无横向溢出

### 首页视觉清理 + 改名"职达 Landed"（2026-08-17）
- 起因：上一轮视觉改版接入真实代码后，用户看实际页面截图反馈"颜色偏多、部分图标效果不好、整体风格有点乱、面试题库入口位置不合适"；先用 `frontend-design` skill 出了一版静态 mockup `design_preview_v3.html`（项目根目录，仅供参照）反复对比调整（包括用户直接对比 `design_preview_v2.html` 后要求把配色收得比 v3 初版更克制），定下方向后再接入真实代码。同一轮顺带把产品名从"Signal"改成"职达"，英文名定为"Landed"
- **图标统一成 SVG**：`static/common.js` 新增共享图标常量（`SPARK_ICON`/`TAG_ICON`/`RESUME_ICON`/`MAIL_ICON`/`MIC_ICON`/`NOTE_ICON`/`GLOBE_ICON`/`BUILDING_ICON`/`INBOX_ICON`/`CHAT_ICON`），把 `static/{app,job_detail,bank,resume}.js` 和 `templates/{index,interview,job_interview,job_detail}.html` 里所有功能性 emoji（🏷️📄✉️🎤📝🌍🇨🇳📭💬✨）换成同一套 24×24 线性描边图标；职位卡片操作行的标签按钮从"文字按钮包一个 emoji"（`.btn.btn-secondary`）改成方形图标按钮（`.icon-btn`），跟星标/勾选/X 归成一组，不再是两套按钮形状混排
- **颜色收敛**：`--accent` 青蓝并入 `--primary`（原来只服务"面试准备"pill 和"已投递"状态下拉两处，没必要单独存在）；`--star` 金色整个去掉，"重点关注"星标/卡片左标/分组标题/统计卡改用 `var(--text)`（近黑），靠"实心填充 vs 描边"这个形状差异表达"已标记"，不再靠颜色；Indeed/LinkedIn 来源徽标、职位状态徽标（待审核/已收藏）、投递状态下拉框（待投/已投递/面试中/已拒绝）都去掉了颜色编码，改中性样式，只有"已收藏"徽章和"Offer"状态保留墨色描边+加粗的强调；成功/警示/危险三个语义色 token 本身不动（toast、简历体检等其它功能还在用），只是不再用在职位卡片这一处
- **面试题库入口挪回顶栏**：删掉统计卡片和筛选栏之间的 `.bank-entry` 横幅，改成顶栏"我的简历"和设置齿轮之间的图标+文字按钮；`loadBankSummary()` 摘要文案改填进按钮的 `title` 悬浮提示，不再占正文一整行
- **修复遗留 bug**：`interview.html`/`job_interview.html`/`job_detail.html`/`resume.html` 这 4 个次级页面顶栏原来还留着旧版 `.brand-icon` 图标块的 HTML，但对应 CSS 在上一轮改版里已经删掉了，导致这几个页面顶栏图标位置一直是空的——上一条目"截图抽查过没有断裂"的说法不准确，这次一并删掉这段 markup，跟首页对齐成纯文字 wordmark
- 涉及文件：`static/style.css`（token 精简、`.bank-entry`/badge 颜色/`.app-status-select` 颜色等规则改写）、`static/common.js`（新增共享图标常量）、`static/{app,job_detail,bank,resume}.js`（emoji→SVG、标签按钮改 `icon-btn`）、`templates/{index,interview,job_interview,job_detail,resume}.html`（品牌改名、题库入口挪位、`.brand-icon` 修复、chip/空状态图标）、`README.md`（标题改名）
- `tests/run_all.py` 全量跑过，8 个套件 + 6 个 JS 语法检查全部通过

### 首页高频入口挪到导语行 + 补「今日抓取」漏斗（2026-08-17）
- 起因：用户拿真实首页跟设计稿 `design_preview_v2.html` 逐项对比，指出好几处结构性不一致；确认后按用户明确选择的方案接入：智能搜索/AI分析/面试题库这三个高频入口跟"面试题库入口"归一层级，且按 v2 的位置放在导语文案右侧（不放在最上方顶栏）；顶部"最近运行"时间戳和独立深色模式图标按钮维持现状（不加，仍在"更多"弹窗里）；"今日抓取"漏斗统计要加上；状态卡片 A/B/C 版式切换器不需要做成正式功能
- 顶栏（`.topbar-actions`）现在只留「我的简历」和「更多」齿轮，智能搜索/AI分析/面试题库三个按钮挪进新增的 `.lede-row`/`.lede-actions`（`templates/index.html`、`static/style.css`），跟导语段落同一行、贴右侧
- 新增「今日抓取」漏斗行（今日抓取 → 不相关跳过 → 重复跳过 → 新增），取当天（本机日期）内 `search_runs` 记录求和展示；`static/app.js` 新增 `renderFunnel()`，挂在已有的 `loadRuns()` 里（首页初始化时就会拉一次 `/api/runs`，不额外发请求），没有当天记录时整行隐藏
- 顺手删掉了死代码：`loadBankSummary()` 一直在往一个模板里根本不存在的 `#bankSummary` 元素写内容（上一轮"面试题库入口挪回顶栏"改动时忘了同步删），函数体和初始化调用一并移除
- 涉及文件：`templates/index.html`、`static/app.js`、`static/style.css`
- 用 Edge 无头模式截图核对过新布局（智能搜索/AI分析/面试题库三个按钮渲染在导语右侧、今日抓取漏斗显示真实数据 273→170→59→43）；`tests/run_all.py` 全量跑过，8 个套件 + 6 个 JS 语法检查全部通过

### 首页字号/字重对齐设计稿 v2（2026-08-17）
- 起因：继续拿真实首页跟 `design_preview_v2.html` 逐项比对，这次是颜色 token 之外的问题——十几处组件的字号/字重跟设计稿不一致：展示型数字（导语大数字、今日最高分、状态卡片数字）线上比设计稿小且更粗，普通 UI 文字（按钮、chip、徽标、标题）线上比设计稿更粗更规整；徽标在设计稿里是"等宽小写·大写字母·尖角矩形"，线上是"无衬线加粗·圆角药丸"。用户确认要完整按设计稿还原（不只改数值，字体族/大小写/字间距/圆角形状等强绑定属性一起改），红/橙/绿语义色系统本轮不动
- `static/style.css` 改了 11 处规则：`.lede-figure`/`.job-card.hero > .match-pill`（今日最高分数）改成 `clamp()` 响应式字号 + 250 字重，删掉两条被 clamp 取代后冗余、会打架的旧移动端固定覆盖值；`.job-card.hero .job-title`（今日最高标题）补齐 400 字重；`.stat-card .value`/`.label`、`.job-title`、`.match-pill`（普通职位行）、`.brand h1`（logo）、`.btn`、`.chip` 的字号/字重逐一对齐设计稿数值
- `.badge` 改动最大：从"无衬线加粗 700、圆角药丸 999px"整条换成设计稿的"等宽字体、.56rem、400 字重、大写、字间距 .12em、尖角矩形 2px"；`.badge-status-reviewed`/`.badge-status-dismissed` 等派生样式随基类自动继承新形状，没有单独改
- 明确排除：`body` 基础字号 15px/1.6（这其实是更新一版设计 `design_preview_v3.html` 的数值，线上已经是这个，不算跟 v2 不一致）；红/橙/绿语义色系统；简历页、面试题库页专属的字号
- 涉及文件：`static/style.css`

### 手动粘贴 LinkedIn 职位链接入库（2026-08-18，对应下面 P1 批次的「LinkedIn 推荐职位手动导入」痛点①）
- 起因：按关键词 × 城市的自动搜索总会漏——标题措辞对不上关键词、城市没配、或者是自己在 LinkedIn 推荐流/朋友转发里看到的一条。此前这类职位没有任何入库通道，等于整条 AI 分析/材料/面试准备的流水线都用不上。明确**不做**推荐流抓取（需登录态高频请求，封号风险直接命中求职主通道），只做粘贴导入
- 首页导语行加「添加链接」按钮 → 弹窗贴链接（每行一条，一次最多 20 条）→ 逐条报告 已入库 / 已存在 / 失败原因，成功的给一个直达 `/jobs/<id>` 的链接（`templates/index.html`、`static/app.js`、`static/style.css`）
- 抓取两级，够用就不往下走：① 访客页 `requests` 抓 `/jobs/view/<id>`；② 抓不到（限流/登录墙/职位要登录才可见）时自动改用 Easy Apply 那个已登录的 Playwright profile 兜底，整批共用一个浏览器上下文，先无头、整批都没抓到才带界面重试一次。访客页和登录页 DOM 完全不同，用一张"候选选择器表"让同一个解析函数服务两条路径（`job_link.py`）
- 链接解析支持从详情页复制的 `/jobs/view/<slug-带-id>` 和从搜索/推荐页复制的 `?currentJobId=`；两者同时出现时以路径上的为准。非 LinkedIn 链接明确报错（本轮只做 LinkedIn）
- 入库后接上跟"智能搜索"完全一样的后续：落成 `status='new'`（待审核）、后台排队 AI 匹配分析 + 公司国籍分类。差别是**跳过标题/地点粗筛**（`pipeline.queue_pending_jobs(enforce_relevance=False)`）——手动贴的是用户自己挑的，用当前搜索关键词去质疑它只会让这条职位永远拿不到匹配度
- 去重沿用公司+职位名的 `make_dedupe_key`，库里和追踪表里已有的都跳过；`keyword` 列存职位名（不展示给用户，用途是让 `scraper.refetch_job_jd()` 以后还能重新定位这条职位）
- 涉及文件：`job_link.py`（新增）、`app.py`（`POST /api/jobs/add_by_url`）、`pipeline.py`、`templates/index.html`、`static/app.js`、`static/style.css`、`tests/test_add_by_url.py`（新增，网络与 LLM 全 mock）

### 每日任务清单 + 忽略原因收集→偏好档案（2026-08-18）
「求职决策闭环」P0 批次里的两条（[product-review.md](product-review.md#p0-本周) 的 P0-1、P0-3），一起做是因为两者共用同一批 UI 基建（首页新增区块、`checklist_custom_items`/`job_dismiss_reasons`/`preference_profiles` 三张新表都走 `models.py` 现成的迁移模式）。P0-2（投递状态自动化）本轮不做，只顺带补了它需要的最小前提。

**每日任务清单**
- 首页统计卡片下方新增可勾选清单：今日抓取（原有漏斗）/ N 条待审核 / M 条待生成材料 / K 条已收藏未投递 / J 条投递超7天该跟进 / 简历从没体检过的提醒，点文案直接跳转/筛选到对应视图（`static/app.js` 的 `renderChecklist()`）
- 自动生成的几项**不落库**：真实来源永远是当前数据库状态，勾掉只是"今天已经看过、先别提醒"，存 `localStorage`（按日期分 key，换一天自动失效）；用户自己加的待办才真正持久化（新表 `checklist_custom_items`），勾掉即删除
- 待审核/待生成材料/已收藏未投递三项直接复用前端已有的 `allJobs`（`jobsNeedingMaterials()` 从 `batchGenerateMaterials()` 里抽出来给两处共用，不重复一份判断标准）；后端只多算它算不出来的部分（超7天未跟进、自定义待办、简历体检状态），新增 `GET/POST /api/checklist`、`DELETE /api/checklist/<id>`
- **顺带补上 `jobs.applied_at` 时间戳**（P0-2 的最小前提，不是完整投递自动化）：`models.set_application_status()` 只在状态**变成** `applied` 的那一刻记一次，改成其它状态不清空、已经是 `applied` 不覆盖；`models.list_stale_applications(days=7)` 只看有时间戳的行，历史上早就是 `applied` 但没有时间戳的数据不会被武断地当成"超7天"误报

**忽略原因收集 → 偏好档案**
- 预设原因（来自 product-review 的痛点③⑦分析）：薪资不符 / 职能不对 / 公司不感兴趣 / 地点 / 行业 / 层级不匹配，+ 自由文本，都是选填
- **不阻塞"忽略"本身**：忽略仍然是点一下立刻生效、给撤销的低摩擦操作（`static/app.js:897-899` 那条设计取舍不变），原因弹窗在忽略成功**之后**才弹出，跳过/关闭都不算错误（`static/common.js` 的 `openDismissReasonPrompt()`，结构仿标签编辑器 `openTagEditor()`）
- 已忽略但还没补过原因的历史职位（含 product-review 提到的"11 条高分被忽略"冷启动样本），卡片上有单独的"记录忽略原因"图标入口，走同一个弹窗组件（`dismissReasonButtonHtml()`）
- 新表 `job_dismiss_reasons`（一条职位可以有多行，忽略/收藏/再忽略的历史都是信号，不覆盖式存储）；新增 `preference_profile.py`（仿 `resume_review.py` 的模式：一次 LLM 调用把原因记录总结成一段偏好档案，失败也落一行，见新表 `preference_profiles`）
- **攒够 5 条新原因才自动重新生成一次**（`pipeline.PREFERENCE_PROFILE_THRESHOLD`），避免样本太少还没稳定就跟着零星波动；`job_state.py` 新增单例并发锁（同 `_bank_generating` 的模式），防止并发触发互相覆盖。设置面板里也留了"立即重新生成"手动入口，跳过阈值检查
- 生成好的档案通过 `analyzer.analyze_job()` 新增的可选参数 `preference_profile_text` 注入 `PROMPT_TEMPLATE`（没有档案时这个区块整段不出现，行为跟以前完全一样）；prompt 里明确要求"酌情参考、不能一票否决"，跟 `company_origin` 判断"不确定就填 unknown 不要瞎猜"同一个审慎标准
- 偏好档案本身展示在"更多 → 设置"面板的只读卡片里（不单独开页面，内容就一段话），让"被塞进 prompt 的内容"可见可信任；新增第7个 LLM 功能位 `preference_profile`
- 涉及文件：新增 `preference_profile.py`；改 `models.py`（3 新表 + `applied_at` 列 + 一堆 DAL 函数）、`app.py`、`pipeline.py`、`job_state.py`、`analyzer.py`、`llm.py`、`static/{common,app}.js`、`templates/index.html`、`static/style.css`
- 测试：新增 `tests/test_preference_profile.py`（校验、阈值触发/不触发、强制重新生成、失败落库、prompt 注入、pipeline 接线）、`tests/test_checklist.py`（`applied_at` 记账、超7天查询、清单接口增删查）；`tests/test_llm.py`/`tests/test_frontend.py` 补了第7个功能位的断言；`tests/run_all.py` 全量跑过，11 个套件 + 6 个 JS 语法检查全部通过

### 顶部统计卡片新增「已投递」「面试中」（2026-08-18）
- 起因：用户希望统计卡片区在「已收藏」后面能直接看到「已投递」「面试中」各多少条，不用去翻底部「投递状态」筛选chip
- `templates/index.html` 在「已收藏」卡片后新增两张卡（`data-appstatus="applied"`/`"interviewing"`），复用现有 `.stat-card.clickable` 样式，不引入新配色（沿用「重点关注」卡片定下的克制配色原则）
- 这两张卡按的是 `application_status` 维度，跟前两张按 `status`（新/收藏/忽略）不是同一个维度，也不是同一套互斥关系；`static/app.js` 新增 `filterByAppStatus()`，并把 `updateStatCardActive()` 扩展成同时识别 `data-appstatus`，让顶部新卡片和底部「投递状态」筛选chip共享同一个 `currentAppStatus`、双向同步高亮
- `static/style.css` 统计卡片栅格从 4 列改成 3 列（6 张卡两行更整齐，避免 4+2 的半空行）

### 「已收藏」卡片数字改为只算"待投递"（2026-08-20）
- 起因：新增「已投递」「面试中」两张卡后，「已收藏」的数字仍然是不分投递状态的全部 `status==='reviewed'`，跟旁边两张卡的口径重叠、容易看混
- `static/app.js` 的 `updateStats()` 新增 `reviewedPending` 统计（`status==='reviewed' && application_status==='not_applied'`），`statReviewed` 改显示这个数字；卡片小字同步改成「已收藏待投递，已经投出的不在这个下面展示」，说清楚这条数字不包含已投递/面试中等
- 小字文案变长后原来单行截断（`white-space: nowrap` + 省略号）会把话切掉，`static/style.css` 新增 `.hint.wrap` 允许换行，只用在这张卡上，其它卡片小字维持原来的单行截断
- 点击卡片跟着数字口径改：`filterByStatus('reviewed')` 打开筛选时顺带把 `currentAppStatus` 设成 `'not_applied'`（再点一次取消筛选时两个一起清空），列表跟卡片数字保持一致，不会出现"卡片写 5 条、点开列表却有 8 条"；跟已有的 `focusChecklistStatus('reviewed', 'not_applied')`（清单里"M 条已收藏还没投递"那一行）是同一个筛选组合，只是触发入口不同
- 用户反馈"点已收藏、再点已投递，不该两张卡同时选中"：`status`（新/收藏/忽略）和 `application_status`（已投递/面试中）原来是两个可以叠加的独立维度，现在把顶部这五张卡改成同一个单选组——`filterByStatus()`/`filterByAppStatus()` 互相清空对方那个维度，底部「投递状态」筛选chip（选中具体状态、非"全部"时）也跟着清空审核状态筛选，保持两处入口行为一致
- 用户还反馈每日待办里"M 条已收藏、还没生成材料"这条数字不对：`jobsNeedingMaterials(allJobs)` 当时是对全量 `allJobs` 算的，没有按 `status==='reviewed'` 过滤，待审核/已忽略里符合条件的也被算了进去，跟文案对不上；用户明确表示不需要这条清单项，直接从 `renderChecklist()` 删掉（`static/app.js`），`jobsNeedingMaterials()` 函数本身保留（"批量生成材料"按钮 `batchGenerateMaterials()` 还在用），只是不再单独出现在待办清单里

### 简历体检改成后台任务 + 完成提醒（2026-08-20）
- 起因：用户点了「AI 简历体检」按钮后跳回首页，再跳回来发现体检被打断了——`POST /api/resume/review` 当时是同步阻塞到 LLM 调用完成才返回（跟单条职位 `/analyze` 一样的设计取舍，见 `spec/tech-solution.md` 旧版决策记录），跳页会让浏览器取消这个还没返回的请求
- 讨论后确定方案：照搬题库起草（`generate_bank_draft`）已经在用的"后台线程 + 前端轮询"模式，不新引入架构；完成后除了体检那一页自己的 toast，还要在首页每日待办清单里加一条提醒——用户明确要求文案是"体检已给出建议，去优化简历"
- `job_state.py` 新增 `start_resume_review()`/`finish_resume_review()`/`resume_review_generating()`/`resume_review_error()`，单例锁同一时刻最多跑一次，跟 `_bank_generating`/`_bank_error` 同构
- `app.py`：`POST /api/resume/review` 改成起后台线程立刻返回 `{"started": true}`，重复点击拿 409；`GET /api/resume/review` 加 `generating`/`background_error` 两个字段；`GET /api/checklist` 新增 `resume_review_ready`——最新一条体检记录的 `created_at` 是今天、且有 `content_json` 没 `error` 才为真，只覆盖"今天完成"这个窗口，不需要额外一个"已读"状态
- `static/resume.js`：`startReview()` 改成"发请求就返回"，轮询 `GET /api/resume/review` 的 `generating` 直到跑完再弹 toast；页面刚加载（含从别的页面跳转回来）时如果发现体检还在后台跑，自动显示"体检中"并接着轮询，不会因为离开过页面就跟体检失去联系
- `static/app.js`：`renderChecklist()` 新增一行"体检已给出建议，去优化简历"（`resume_review_ready` 为真且当天没点掉时出现），跟已有的"还没体检过"提醒共用同一套按天重置的勾掉逻辑，两条互斥不会同时出现
- `pipeline.run_resume_review()` 本身不用改，管并发/线程从来是调用方的事；`tests/test_resume.py` 改成起后台线程后轮询 `job_state.resume_review_generating()` 等它跑完，`tests/test_checklist.py` 新增 `resume_review_ready` 的今天/昨天/失败三种场景断言

### 「已收藏」卡片小字精简 + 体检待办的消失 bug 修复（2026-08-20）
- 「已收藏」卡片小字从"已收藏待投递，已经投出的不在这个下面展示"精简成"已收藏待投递"，`static/style.css` 里为长文案加的 `.hint.wrap` 换行样式随之移除（`templates/index.html`）
- 用户反馈：点了"体检已给出建议，去优化简历"这条待办跳去简历页，还没做优化这条自己就没了。排查出两个问题，都在这次修掉：
  - **bug**：`checklistRowHtml()`（`static/app.js`）里 `<span onclick="...">` 套在关联着 checkbox 的 `<label>` 内，点文字触发跳转的同时，浏览器会顺带执行这个 `<label>` 的默认动作——连带勾选那个 checkbox，等价于用户自己顺手把这条待办勾掉了。所有走这个函数生成的待办条目（待审核/待投递/该跟进/该体检）都受影响，不止体检这一条。修法：`onclick` 表达式前面加 `event.preventDefault()`，取消 label 的默认联动
  - **语义**：上一版"体检完成提醒"是按日期收敛的（`resume_review_ready` 只在"今天完成"当天为真），过了今天就自动消失，不符合用户这次明确要求的"保留到我完成优化、或者主动点忽略"。改成 `app.py` 的 `get_checklist()` 判断"优化版文件的 mtime 有没有晚于这次体检的 `created_at`"——`optimized.docx` 不存在或者比这次体检更早，都算"建议还没被采纳"，继续提醒；同时新增 `resume_review_id` 字段。前端不再复用其它条目那套按天重置的 `dismissedToday`，改成 `ignoreResumeReviewReady()` 把"忽略"状态按 `resume_review_id` 存进 `localStorage`，跟着这一次体检这个具体对象走，不跟着日期走，换了新的体检结果会自动重新提醒
- `tests/test_checklist.py` 补上 `resume_store.RESUME_DIR` 隔离（之前会读写真实项目目录下的 `resumes/`），并把断言从"今天/昨天"改成"生成过更晚的优化版文件"；`tests/test_frontend.py` 的内联函数引用白名单里加了 `preventDefault`（`event.preventDefault()` 不是本项目定义的函数，属于跟 `stopPropagation` 同一类需要放行的浏览器内置方法）

### 每日待办卡片改为默认展开的可折叠卡片（2026-08-18）
- 起因：用户反馈待办模块位置不好（通栏卡片挤在统计卡和职位列表之间，占地方）。讨论过悬浮按钮（角标计数、点开弹面板）和可折叠卡片两个方案，用高保真效果图（复用真实配色/字体做的可交互 HTML mockup）对比后选定折叠卡片——不引入新的浮层交互模式，且折叠后的标题栏本身就带数字，可见性不比悬浮按钮差
- 卡片顶部新增标题栏（`checklist-head`）：chevron 图标 + "今日待办" + 数量角标（`checklist-count`，条数为 0 时隐藏），点击整行触发展开/收起（`toggleChecklist()`）
- 折叠动画用 `grid-template-rows: 0fr → 1fr` 技巧（`checklist-body`/`checklist-body-inner`），而不是固定 `max-height` 或 JS 量 `scrollHeight`——待办条数是动态的（followups/自定义待办数量不定），这样不会裁内容，也不用额外写测量逻辑
- 默认展开（`checklist-card` 标签上直接写死 `open` class）：每次刷新页面都想让用户先看一眼今天有什么事，看完自己点标题栏收起；折叠状态不持久化（不读/不写 localStorage），不需要记住上次开关状态
- 涉及文件：`templates/index.html`、`static/style.css`、`static/app.js`（`renderChecklist()` 顺带更新数量角标，新增 `toggleChecklist()`）

### LinkedIn/Indeed 跨源重复只留 LinkedIn（2026-08-18）
- 起因：用户要求"LinkedIn 和 Indeed 重复的职位也要去重，只留 LinkedIn"。排查发现库里当时有 4 对标题（归一化后）一字不差、显然是同一条被两个源都抓到的严格重复，都在 Amazon 名下：Senior Product Manager - AI, NBS AI and OPS；Senior Product Manager - AI, Amazon Global Selling - PMO；Senior AI Product Manager, Amazon Global Selling - PMO；AI Product Manager, MKT AI & Seller Exp。用户确认只处理"标题完全相同"这个确定性场景，不动亚马逊那一整簇标题不同但相似的近似职位（那是模糊相似度判断，风险不一样，已经有「疑似重复」角标处理，见上面一条）
- 分两层实现：① `models._merge_cross_source_duplicates()`，`init_db()` 里每次启动都跑一次（幂等）：按新版 `make_dedupe_key()` 重新分组，同组里出现 linkedin+indeed 两个来源时，留进度更靠前的一行（投递状态 > 是否标星，打平时优先留 LinkedIn），被合并掉那行名下的备注/面试准备材料先过户再删除；如果留下来的那行恰好是 Indeed 来源、但另一行是 LinkedIn，把 site/job_url/jd_text 换成 LinkedIn 版本——"留哪行状态"和"链接指向哪个源"分开处理。② `models.upgrade_to_linkedin_if_needed()` + `scraper.py` 的 `_ingest_df()`：以后新抓到的场景，判重命中已有 Indeed 行时，如果这次抓到的是 LinkedIn 版本，原地把已有那行升级成 LinkedIn，不新插入一行
- 在真实库跑过一次：89 条→85 条，4 对严格重复各留了 1 行；已投递的那条（id 58，Indeed 来源）因为标题跟邻居们不完全一致，不在这次合并范围内，保持不动，仍靠「疑似重复」角标提示。执行前对 `jobs.db` 做了一次快照备份
- 跟 `_migrate_dedupe_keys()`"绝不删除、绝不合并"的原则刻意不同——那个函数处理任意撞车（包括同源、还没验证是不是真的同一条），这个函数只处理"新规则算出来 company+title 完全相同、且一边 linkedin 一边 indeed"这个更窄更确定的场景，见 [tech-solution.md](tech-solution.md#关键决策)
- 涉及文件：`models.py`、`scraper.py`、`tests/test_dedupe_normalize.py`

### 匹配分析补一条"职级错配"降分规则（讨论于 2026-08-18）
- 起因：用户反馈如果 JD 只要求 4-6 年经验、职位 title 不带 Senior 等资深字样，大概率是初级 PM 岗位，这种情况下匹配度应该降低——现有 prompt 里只有"硬性门槛未达标拖累 cognitive_match"这一条降分规则，没有覆盖"技能都对得上，但职级/职责范围明显低于候选人现在的资历"这种错配
- `analyzer.py` 的 `PROMPT_TEMPLATE` 打分规则（步骤3）里新增一条，跟"用户偏好档案"那条紧挨着：JD 要求经验年限明显低于候选人简历体现的实际经验、且 title 不带 Senior/Staff/Principal/Director/Head/VP 等资深字样时，在 `content_match` 上体现降级——候选人有没有这个资历不是靠数据库字段判断，是 LLM 本来就在同时读简历全文和 JD，让它在打分这一步顺带比较
- 这条只改了 prompt 文案，不改评分公式（仍是 `0.5*cognitive_match + 0.5*content_match`）、不改输出 JSON 结构，只对**之后新跑的分析**生效，库里已经打好分的职位不会补跑
- 涉及文件：`analyzer.py`

### 「疑似重复」提示角标 + 三处视觉/文案小修（2026-08-18）
- 起因：用户反馈一条已标星、84% 匹配的 Amazon 职位其实是已投递岗位换标题重新挂出来的，追问"为什么没有去重"。核实后发现 `annotate_similar_groups()`（见上面「跨源去重加固」）其实已经把两条关联到同一个 `similar_group_id`，只是因为这条自己也标了星，被"重点关注不参与折叠"的规则挡住，提示没有露出来——不是去重逻辑漏判，是折叠展示的设计没覆盖到"标星 + 组内有一条已投递"这个组合。详细取舍见 [tech-solution.md](tech-solution.md#关键决策)
- 修法：`models.annotate_similar_groups()` 新增 `duplicate_of_applied` 字段——同组里有成员 `application_status` 是 applied/interviewing（优先取 interviewing），其余成员打上这个字段（指向那条的 id/标题/状态），不受是否标星、是否被折叠影响；前端 `static/app.js` 新增 `duplicateOfAppliedBadgeHtml()`，跟面试准备角标同形状、warning 语义色，点击跳到那条已投递/面试中的职位。`tests/test_dedupe_normalize.py` 新增断言覆盖
- 顺手改了三处：①「智能搜索」按钮改名「智能抓取」（更准确描述这是抓取动作而非查询），涉及 `templates/index.html`、`static/app.js`、`README.md` 里所有面向用户的引用；②顶栏「面试题库」「我的简历」两个链接按钮去掉图标、修掉浏览器默认下划线（`.btn` 基类补 `text-decoration: none`，是所有 `<a class="btn">` 的通用问题，不止这两个按钮）；③「今日抓取」漏斗行原来整行强制等宽字体（`--font-mono` 没有中文字形，中文标签被迫用系统兜底字体渲染，跟页面其它中文不一致），改成不强制字体、用 `tabular-nums` 对齐数字，跟导语行 `.lede-num` 的处理方式一致
- 追问了漏斗数据来源：确认「566条→不相关356→重复205→新增4」是真实数据，汇总当天所有「智能抓取」运行记录（`renderFunnel()`，见 `static/app.js`），每次抓取都更新。用户提议的"功能性陈述"文案其实已经是漏斗上方导语行（lede）在做的事，两者不重复（导语=库存状态，漏斗=当天抓取过程），讨论后决定两行都保留现状，包括导语里「N 条越过 70% 投递线」这句本已被「求职决策闭环」P1 批次标记为待改的措辞——**这句话仍待改，不算这次的范围**
- 涉及文件：`models.py`、`static/app.js`、`static/style.css`、`templates/index.html`、`README.md`、`tests/test_dedupe_normalize.py`

### 首页顶部视觉调整：统计卡片改一排 + 导航入口重排 + 智能搜索下拉（2026-08-18）
- 起因：上一条加了「已投递」「面试中」两张统计卡后变成 3 列 2 排，用户反馈太长；同时想把「我的简历」「面试题库」这两个低频入口从中间操作区挪到顶栏并去掉边框、「添加链接」合并进「智能搜索」做成下拉、副标题文案也想换一版
- 统计卡片改回一排：`static/style.css` 的 `.stat-grid` 从 `repeat(3,1fr)` 改回 `repeat(6,1fr)`，`.stat-card` 整体等比缩小（padding/数字字号/标签字号都调小，小字提示保留不砍），960px 以下（笔记本变窄但还没到手机断点）单独加了一条媒体查询退回 `repeat(3,1fr)` 两排，避免六列在中等宽度挤得看不清；原有 720px 手机断点不变
- 「我的简历」「面试题库」从 `.lede-actions` 挪到顶栏 `.topbar-actions`，顺序为 面试题库→我的简历→更多⚙️；去边框没有改共享的 `.btn-secondary`（12 个文件复用 50 次，改了影响面太大），新增 `.btn-plain` 修饰类叠加使用，平时无边框、hover 才露出一条浅边；「更多⚙️」同理没改共享的 `.icon-btn`（10 个文件在用），单独给 `#moreBtn` 去边框
- 「添加链接」从独立按钮合并进「智能搜索」的下拉菜单：新增 `.split-btn` 分裂按钮组件（主体按钮照常点击直接搜索，右侧箭头按钮点开下拉），下拉本身没有遮罩层，靠新增的 `document` 级点击监听判断点击是否落在 `.split-btn` 外部来自动收起（`static/app.js` 新增 `toggleRunNowMenu()`/`closeRunNowMenu()`）；下拉里的「添加链接」项复用原有的 `openAddLinkModal()`，弹窗本身没改，`id="addLinkBtn"` 也保留在新位置（`tests/test_frontend.py` 有断言依赖这个 id）
- 顶栏副标题文案从「求职路上，滤掉噪音，只留信号」改成「智能领航，过滤噪音，保留信号」（用户自定）
- 「面试题库」按钮**没有改名**：讨论时想过改叫「面试准备」，但跟职位详情页里已有的「面试准备」材料概念（`job_interview.html`）撞名，用户确认保留原名
- 涉及文件：`static/style.css`、`templates/index.html`、`static/app.js`；`tests/test_frontend.py`、`tests/run_all.py` 全量跑过（13 个套件 + 6 个 JS 语法检查）确认没有破坏现有断言

### 投递状态自动化：一键「我投了」（2026-08-20，P0-2）
- 「求职决策闭环」P0 批次最后一条（痛点⑥），补上 `applied_at` 时间戳记录（2026-08-18 已顺带做完）之后的自动化本体
- **职位卡片新增「我投了」按钮**：只在"已收藏但还没记录投递状态"（`status==='reviewed' && application_status==='not_applied'`）的卡片上出现，点击直接调用现成的 `setApplicationStatus(id, 'applied')`，`applied_at` 由已有的 `models.set_application_status()` 记录，没引入新接口（`static/app.js` 新增 `appliedButtonHtml()`）
- **Easy Apply 完成时特意不自动置 `applied`**：讨论后确认 `run_easy_apply()` 的"成功"只代表"浏览器打开、材料填好、停在提交按钮前"，不代表用户真的点了 LinkedIn 的提交——如果拿这个当信号自动置状态，会把"打开看了一眼但没投"也算成已投递，污染"超7天该跟进"提醒和以后的复盘统计，跟 `mission.md` "绝不代替用户提交"的原则在语义上也擦边。改成 Easy Apply 完成的成功提示里加一句引导，请用户提交完成后自己回来点「我投了」确认（`static/app.js` 的 `pollEasyApplyUntilSettled()`）
- 涉及文件：`static/app.js`（新增 `appliedButtonHtml()`，`jobCardHtml()` 接入，`pollEasyApplyUntilSettled()` 文案调整）；后端 `set_application_status`/`applied_at` 逻辑复用已有代码，未改动

### 职位详情页"忽略"后自动跳到列表原顺序的下一条（2026-08-21）
- 起因：用户反馈在详情页把职位标记「忽略」之后停在原地（只是隐藏了忽略按钮、弹个可撤销的 toast），逐条审核时还要自己手动返回列表再点下一条，要求改成直接跳到列表里原本排在它后面的那条
- 整个应用是服务端渲染的多页应用（Flask + Jinja，列表页/详情页之间是整页跳转，没有 SPA 路由或内存状态能跨页存活），列表页点卡片进详情页时原来只传了单个职位 id，不带顺序信息。改用 `sessionStorage` 跨页传递：列表页 `renderJobs()` 每次渲染完把当前显示顺序（跟展示顺序一致：置顶 → 重点关注 → 其余职位，只算可点进详情的）存进 `sessionStorage['jobListOrder']`（`static/app.js` 新增 `saveJobListOrder()`）；详情页 `dismissFromDetailPage()` 忽略成功后读这个数组找到当前职位的下一个 id，能找到就直接跳转过去，找不到（比如直接敲 URL 进来、或者已经是列表最后一条）就保留原来的"停在原地 + 可撤销 toast"逻辑作为兜底
- 忽略后自动跳走会让原来那条 toast 来不及看到，改成把撤销所需信息（`jobId`+`previousStatus`）存进 `sessionStorage['pendingUndo']` 带到下一页，新页面加载时读到就接着弹同样的"已标记为「已忽略」/撤销"提示（`static/job_detail.js` 新增 `getNextJobId()`/`savePendingUndo()`/`showPendingUndoToast()`/`undoDismissForJob()`），撤销时操作的是上一条职位的 id，不是当前页的 `DETAIL_JOB_ID`
- 涉及文件：`static/app.js`、`static/job_detail.js`

### 每日待办新增"面试中职位"提醒（2026-08-21）
- 起因：用户反馈有职位处于"面试中"时，希望每日待办清单里每天都能看到"准备《职位》的面试"这类提醒，点文字应该跳转去准备、不该连带把这条待办勾掉，只有主动勾选前面的 checkbox 才算"今天已处理"、从列表隐藏
- 跟"待审核"/"待投递"两项一样直接用前端已有的 `allJobs` 现算（不需要新增后端字段）：`renderChecklist()` 新增一段，遍历 `application_status === 'interviewing'` 的职位生成条目，复用已有的 `checklistRowHtml()`（点文字 `event.preventDefault()` 阻止连带勾选 checkbox 这个坑已经被之前那次 bug 修复挡住了，见上面"AI分析详情弹窗"之前的历史记录），文字点击跳转到 `/jobs/<id>/interview`（面试准备页）
- 勾选框走的是已有的 `dismissChecklistItemToday()`——按日期存 `localStorage`，今天勾掉明天会重新出现，跟"待审核""待投递"这两条提醒是同一套"每天重新提醒"的语义，直到投递状态改掉才会彻底消失
- 涉及文件：`static/app.js`（`renderChecklist()`）

### 修复自定义待办条目点文字会连带勾掉的 bug（2026-08-21）
- 起因：用户加了一条自定义待办，点击后（不是点勾选框）这条待办就从列表消失了。排查发现自定义待办这块 `<span class="checklist-text">` 没有 `onclick="event.preventDefault()"`，点文字时浏览器会顺带触发外层 `<label>` 关联的 checkbox（等价于自动点了勾选框），触发 `deleteChecklistItem()` 直接删除——这正是 2026-08-18 那次给 `checklistRowHtml()` 修过的同一类 bug，但自定义待办这块是单独手写的 HTML，没走 `checklistRowHtml()`，那次修复没覆盖到
- 修法：给自定义待办的 `<span>` 补上 `onclick="event.preventDefault();"`（没有点击跳转动作，只需要单纯拦下 label 联动），逻辑跟另外两处（`checklistRowHtml()`、"体检已给出建议"）保持一致；顺带确认了当天新加的"面试中"提醒复用的是 `checklistRowHtml()`，本来就没有这个问题
- 涉及文件：`static/app.js`（`renderChecklist()`）

### 打分器（analyzer.py）LLM 输出质量回归套件（2026-08-21）
- 起因：`product-review.md` 曾用真实使用行为得出"打分器已被证伪"的结论（37 条≥0.7 里 11 条被忽略）。跟用户核实后这个结论要打折扣——11 条里相当一部分是 LinkedIn/Indeed 同职位重复入库导致的误忽略，不是嫌分数打错；刨掉重复后，用户确认的真实问题收窄成一类：**职级错配**（初级岗因为技能条目表面对得上，`content_match` 没有被充分拖累）。`tests/` 下所有测试都是 `monkeypatch llm.chat`，只测周边逻辑，对 LLM 真实输出质量从来没有验证过，包括 prompt 里已经写好的"职级错配拖累""硬性门槛拖累"这些具体规则有没有被模型真的遵守
- 新增 `evals/` 目录，独立于 `tests/`（真实调用 LLM，不 mock，产生真实费用，不接入 `tests/run_all.py`）：`evals/fixtures_analyzer.py` 提供一批成对/规则化 fixture，`evals/run_analyzer_eval.py` 是命令行入口。覆盖：① 职级错配拖累（重点）——同一份简历配技能条目相同、只改职级/年限的成对 JD，断言初级版本的 `content_match` 显著低于资深锚点、`overall_match` 不该轻易越过 70%；② 硬性门槛拖累——prompt 给了具体数值，直接断言 `cognitive_match` 绝对上限；③ 偏好档案软信号——成对对比 + 下限（不能被一票否决）；④ 公司归属分类——对知名公司有客观正确答案；⑤ 公司简介不编造——弱检查，只提示不影响 exit code。设计取舍详见 [tech-solution.md](tech-solution.md#关键决策)
- 用法见 [README.md](../README.md#跑打分器的-llm-质量回归评估) 新增小节
- 涉及文件：`evals/fixtures_analyzer.py`（新增）、`evals/run_analyzer_eval.py`（新增）、`.gitignore`（新增 `evals/reports/`）

### 修复「已收藏」筛选下职位被折叠成空壳相似分组的 bug（2026-08-22）
- 起因：用户反馈把一条职位从待审核标记为已收藏后，在"已收藏"列表里找不到它。核实发现数据库本身正确（`status='reviewed'`），问题在前端 `groupJobsForRender()`：该职位与同公司另一条职位共享 `similar_group_id`（`annotate_similar_groups()` 判定标题相似度达标），但那条同伴职位 `application_status='applied'`，被"已收藏"卡片联动的待投筛选（`not_applied`）过滤掉了。`groupJobsForRender()` 原来只看 `similar_group_id` 是否存在就归为"分组"，没有检查筛选后同组还剩几条，导致这条职位被渲染成一个默认折叠的 `<details>`——摘要只显示"公司名 · 1 个相似职位"，看不到职位标题本身，视觉上跟"这条职位不在列表里"没有区别
- 修法：`groupJobsForRender()` 里按 `similar_group_id` 分组后，只有筛选后同组仍有 ≥2 条时才渲染成可折叠分组，否则按单条正常展示
- 涉及文件：`static/app.js`（`groupJobsForRender()`）

### 同步 LinkedIn「已收藏」/「已投递」职位列表（2026-08-22）
- 起因：用户在 LinkedIn 上用官方的"职位跟踪"功能攒了"收藏"（`jobs-tracker/?stage=saved`）和"已投递"（`jobs-tracker/?stage=applied`）两个列表，想批量同步进职达，而不是一条条手动复制链接贴进已有的「添加链接」入口。当天先做了"已收藏"，用户马上追加了"已投递"的同样需求，两者页面结构完全一样，做成一套通用逻辑按 stage 参数区分，不是两份独立实现
- 这两个列表页面本身要登录才能看，不是访客身份能抓到的公开页面。新增 `linkedin_tracker.py`（前身是只支持"已收藏"的 `linkedin_saved.py`，当天扩展时改的名）：复用 `easy_apply.py` 的持久化登录 profile（`.playwright_profile/linkedin/`）开一次真实浏览器，导航到 `jobs-tracker/?stage=<saved|applied>`，不认卡片的具体 class（LinkedIn 自动生成的哈希类名，改版就变），只认页面上所有指向 `/jobs/view/<id>` 或带 `currentJobId=` 的链接——跟 `job_link.parse_linkedin_job_id()` 本来要处理的两种链接形态一致，直接复用；配合滚动/点"加载更多"直到连续几轮都没有新链接出现才停止。收集齐链接后转手交给已有的 `job_link.add_jobs_from_urls()`（分批调用，每批 `MAX_URLS` 条）做抓详情/去重/入库，不重复实现这部分
- 无头模式撞上登录墙会自动带界面重试一次（跟 `job_link.fetch_via_browser()` 同样的取舍：LinkedIn 对无头浏览器识别更严，偶尔会对有效登录态的无头会话也甩登录墙）；本机从没跑过 `ensure_logged_in()`（`.playwright_profile/linkedin/` 目录不存在）时直接报错提示先登录，不会静默失败
- "已投递"这个 stage 独有的行为：新入库的职位自动调用 `models.set_application_status(job_id, "applied")`——跟职位卡片手动点"我投了"是同一个函数，会顺带记 `applied_at`。但这个时间戳只能是"同步这一刻"，不是真实投递日期（LinkedIn 页面上没有稳定可解析的投递日期字段），会影响"投递超过7天该跟进"提醒的判断基准，已在 README 里向用户说明这个局限。"已收藏"不做这个处理，新入库照旧是"待投"
- 整个同步流程（开浏览器扫列表+逐条入库）耗时不确定（列表长的话可能要一两分钟），放后台线程跑：`job_state.py` 新增按 stage 分开存状态的 `start_tracker_sync(stage)`/`finish_tracker_sync(stage, ...)` 等函数（跟题库起草 `bank_generating` 同一个"全局单例、同一时刻最多跑一次"模式，只是这里的"单例"是每个 stage 各一份，避免"已收藏"和"已投递"两个同步互相冲状态、或一个的结果覆盖另一个还没被前端看到的结果），`app.py` 合并成一组参数化路由 `POST /api/jobs/sync_tracker/<stage>`（启动）+ `GET /api/jobs/sync_tracker/<stage>`（轮询状态），不是给"已收藏""已投递"各写一套；前端轮询展示结果，复用「添加链接」弹窗已有的 `renderLinkResults()` 渲染逐条结果，不新建一套 UI
- 真正用真实 LinkedIn 账号跑通过一次"已收藏"同步验证：10 条收藏找到 6 条新职位、4 条判重跳过（其中几条是常规关键词搜索已经独立抓到过的，同一岗位偶尔会被两条路径都发现，属预期内的正常判重）
- 使用方式见 [README.md](../README.md#同步-linkedin-收藏已投递怎么用)；涉及文件：`linkedin_tracker.py`（新增，原 `linkedin_saved.py`）、`job_state.py`、`app.py`、`templates/index.html`、`static/app.js`；测试：`tests/test_linkedin_tracker_sync.py`（新增，原 `test_sync_saved.py`，fake Playwright page 模拟滚动收集/登录墙识别，mock 掉 `linkedin_tracker.sync_tracker_stage` 测 Flask 路由的并发保护/轮询/自动排队分析/不支持的 stage 返回 404/"已投递"自动标记投递状态）

### 修复测试套件曾经写坏用户真实追踪表文件的事故（2026-08-22）
- 起因：开发上面「同步 LinkedIn 收藏」功能时跑 `tests/run_all.py`，发现好几个测试文件在其临时 config 里把 `tracker_xlsx_path` 留成空字符串（或直接展开 `config.DEFAULT_CONFIG` 没有覆盖这个字段）——`pipeline.py` 对空值的处理是解析成真实的 `~/Downloads/JD匹配追踪表.xlsx`，而不是落在测试自己的隔离临时目录里。当时用户本机正好在跑真实的 `app.py`（后台批量分析线程也在写同一个文件），测试进程和真实进程同时对这个文件做非原子的整文件覆盖写（`openpyxl` 的 `wb.save()`），两次写穿插到一起，把用户真实的追踪表文件写坏成一个读不出来的损坏 zip（`BadZipFile: File is not a zip file`）
- 影响范围：`jobs.db` 里保留的 `overall_match`/`resume_path`/`cover_letter`/`resume_bullets`/`company_origin` 没有丢（这些字段本来就双写在数据库里），但追踪表 xlsx 独有、数据库不存第二份的字段——每条职位的任职要求达标/未达标逐条标注、技能匹配/缺口列表、薪资/团队/行业摘要、公司简介——对已经写进这份文件的记录来说没有第二个副本，找不到系统级备份（回收站为空，卷影副本因权限不足没能检查）。用户确认先不动这个文件、自己再找找有没有办法恢复，恢复与否是独立于本条修复的另一件事
- 修复：排查所有会解析 `tracker_xlsx_path` 默认值的测试文件（`test_add_by_url.py`、`test_bank.py`、`test_bank_chat.py`、`test_checklist.py`、`test_dismiss_abort.py`、`test_frontend.py`、`test_job_detail.py`、`test_linkedin_company.py`、`test_preference_profile.py`、`test_prep.py`、`test_resume.py`、当时新增的 `test_sync_saved.py`），统一显式指定成各自临时目录下的 `tracker.xlsx`，不再依赖默认值；这个改动顺带暴露了 `test_add_by_url.py` 里一条对判重结果字段过度假设的断言（`job_id` 只在命中"数据库已有"这条判重分支时才会有值，命中"追踪表已有"分支时没有——之前因为测试意外落在真实 Downloads 文件、凑巧没撞上这个分支才没暴露），一并修正
- 没有修的部分：`tracker_utils.py` 的 `wb.save()` 本身仍然不是原子写（没有"写临时文件再 rename"），如果以后又出现两个进程同时写同一个真实追踪表文件的场景，同样的损坏还会发生——这次只堵住了"测试进程意外写真实文件"这一种触发方式，不是把追踪表整体加固成并发安全
- 涉及文件：上面列出的 12 个测试文件（`test_sync_saved.py` 当天晚些时候随"已收藏"功能扩展成"已收藏/已投递"通用同步一起改名成了 `test_linkedin_tracker_sync.py`，见上面那条）、`tests/run_all.py`

### 职位详情页新增「重点关注」「已收藏」按钮（2026-08-22）
- 详情页头部新增星标按钮，跟列表页卡片上的星标是同一个开关（同一个 `jobs.starred` 字段、同一个 `POST /api/jobs/<id>/starred` 接口），看完详情再决定要不要标不用先返回列表（`templates/job_detail.html`、`static/job_detail.js`）
- 顺手把星标相关的图标常量（`STAR_ICON`/`STAR_ICON_FILLED`）和请求逻辑（`postJobStarred()`）从 `static/app.js` 挪到 `static/common.js`，详情页和列表页共用，不重复一份（`static/common.js`、`static/app.js`）
- 按钮文案用「重点关注」而不是「收藏」：项目里「收藏」已经是 `application_status=reviewed`（`status` 字段）的专用叫法（见统计卡片「已收藏」），两个概念不同，沿用已有命名避免歧义
- 补充：从"待审核"列表点进详情页时原来没有"标记已收藏"的入口，得先返回列表才能点。详情页头部再加一个「已收藏」按钮，跟「忽略」共用同一套"先执行、给撤销、自动跳到列表原顺序下一条"的逻辑（重构成 `setStatusFromDetailPage(status)`，`status` 传 `'reviewed'` 或 `'dismissed'`），已收藏之后自动隐藏按钮（跟列表卡片行为一致）

### 相似职位分组误把「(CN)」地区标记当成区分职位的关键词（修复于 2026-08-22）
- 起因：用户在真实数据里看到 AlphaLife Sciences 的「Technical Product Manager (CN)」和「Sr. Product Manager (CN)」被 `annotate_similar_groups()` 折叠成一组"相似职位"，但这两个明显是不同职位（技术向 PM vs 高级 PM）
- 排查：`_title_tokens()` 去停用词后，"Sr."/"Product"/"Manager" 都已经在停用词表里，「Sr. Product Manager (CN)」这条被去空之后只剩下"cn"；「Technical Product Manager (CN)」剩"technical"+"cn"。两者 Jaccard 相似度 = 1(交集"cn") / 2(并集) = 0.5，刚好压中 `_SIMILAR_GROUP_THRESHOLD`。"(CN)" 只是地区标记，不代表岗位方向，不该被当成有区分度的词，之前的停用词表漏了这个
- 修复：把 `"cn"` 加进 `_TITLE_STOPWORDS`（`models.py`）。加了之后「Sr. Product Manager (CN)」的 token 集合会变空，命中已有的"任一方为空集就跳过比较、不猜"逻辑，不再误判
- 用真实 `jobs.db`（147条）跑了修复前后的全量对比：只有这一对（job id 93/123）从"分组"变成"不分组"，其余所有分组结果完全不变，没有引入新的误判或漏判
- 涉及文件：`models.py`（`_TITLE_STOPWORDS`）

### 每周转化率（讨论/实现于 2026-08-22）
- 起因：用户投了20+职位只拿到1个面试+1个电话（薪资不匹配放弃），怀疑转化率偏低，想在产品里做转化率分析。核实 `jobs.db` 发现样本还太新（21条已投递记录里绝大多数是最近1-3天投的，真正过了回复窗口的只有2条），先如实告知不建议现在下"转化率低"的结论
- 最初设计是按关键词/公司类型/来源站点/匹配分/是否标星拆的5维度表格，用真实数据做了一版界面预览（Artifact）给用户看，反馈"没有太大价值"；改成精简版：一行式"最近一周"汇总 + 自投递开始的自然周趋势柱状图 + 一段规则式建议，第二版预览确认后定稿实现
- 数据全部来自首页已加载的 `allJobs`，不新增接口/数据库字段/依赖；建议文案是阈值判断，不接 LLM。第二版预览时发现按 `first_seen` 兜底缺失的 `applied_at` 会推算出一个比真实投递更早、只有1条数据的虚假周份，用户确认"没有投递可以认为从数据实际开始那周算起"，改成没有时间戳的记录并入已知最早的真实投递周。取舍详见 [tech-solution.md](tech-solution.md#关键决策)
- 位置：首页"今日待办"右侧并排（`.today-row`），不是最初设想的"更多"弹窗新标签页——内容精简后够轻，不用藏起来
- 用法见 [README.md](../README.md#每周转化率怎么看)；涉及文件：`templates/index.html`（`.today-row`/`#weeklyConvCard`）、`static/app.js`（`computeWeeklyConversion()`/`renderWeeklyConversion()`/`generateWeeklySuggestion()`）、`static/style.css`
- 用真实数据核对过：投递21条、面试中1（4.8%）、offer 0、已拒绝 0、被本人婉拒1，跟对话里手工核对的数字一致
- 补充：加了标题「投递分析」，跟「今日待办」一样做成可点击收起（原样照抄 `.checklist-body` 的 `grid-template-rows: 0fr→1fr` 折叠技巧，新写 `toggleWeeklyConv()`）；「最近一周投递」原来单独一行小标签，跟下面数字合并成一句话。数字字体原来套用了品牌英文小字那类 `var(--font-mono)`，反馈字体不对——那是给短标签用的，改成跟 `.funnel b` 一样走正文加粗，只用 `font-variant-numeric: tabular-nums` 保证对齐

### 匹配分析结果结构校验接入生产路径（2026-08-23）
- 起因：用户让评估这个项目的 agent 结构跟一张"上下文管理/规划编排/推理+工具调用/输出校验"四层架构图的对照关系，发现结构校验（字段齐全、分数在 [0,1]、`is_gap` 是否为 bool）之前只存在于离线评测脚本 `evals/run_analyzer_eval.py`，`analyzer.py`/`pipeline.py` 的生产调用链完全没接入——LLM 返回的 JSON 缺字段或分数越界时会被 `.get(...) or []` 之类的兜底悄悄糊过去，直接写进追踪表和数据库
- 把校验逻辑挪到 `analyzer.py` 新增的 `validate_analysis_result()`，`analyze_job()` 拿到 LLM 结果后立即调用，不合规直接抛 `RuntimeError`——走已有的错误处理路径（`pipeline.analyze_and_record_safe()` 捕获后写 `analysis_error`，批量循环 `analyze_pending_jobs()` 记日志继续下一条），不需要新增分支
- `evals/run_analyzer_eval.py` 原来自己重复维护了一份一模一样的 `check_structure()`，删掉改为直接依赖 `analyze_job()` 内置校验（校验失败会体现为调用报错，并入原有的"调用报错"分支，不再单独区分"结构校验失败"）
- 只做了结构校验，不是文首架构图里描述的完整"输出校验层"（业务规则校验、幻觉检测仍不存在），也没有引入 tool-calling 循环或 LLM 驱动的规划层——评估结论是这两层对这个项目（边界清晰的确定性流水线，且 `spec/mission.md` 明确不希望 AI 在 Easy Apply 环节自主决策）没必要，见对话记录
- 涉及文件：`analyzer.py`（新增 `validate_analysis_result()`，`analyze_job()` 里接入）、`evals/run_analyzer_eval.py`（删掉重复的 `check_structure()`）
- 全量测试套件（`tests/run_all.py`，14 个套件）复跑通过

### 同步 LinkedIn "How You Fit" 匹配推荐搜索（2026-08-23）
- 起因：用户想让每日自动抓取流程顺便同步 LinkedIn 上"与我的档案相匹配"的职位——登录态下才能看的 "How You Fit" 求职资格匹配搜索结果页（带 `keywords=`/`geoId=` 等参数）。**这条决策覆盖了 2026-08-18 记录在案的『不做 LinkedIn 个性化推荐流自动化抓取』决策**（见 [product-review.md](product-review.md)、上面"手动粘贴 LinkedIn 职位链接入库"条目）——不是遗忘，是用户 2026-08-23 知情后主动要求，理由是 How You Fit 是 LinkedIn 官方算出的高置信度匹配（可指定关键词/地点参数），跟当时要规避的"完全没法参数化的个性化信息流"性质不完全一样，但登录态自动化本身的账号风险依然存在。配套的具体节流/风险缓释措施：`config.json` 新增 `linkedin_how_you_fit_searches`（最多8条，超过拒绝保存）、`linkedin_how_you_fit_delay`（每条同步间隔默认30秒，比访客抓取用的 `linkedin_request_delay` 宽松得多）、默认空列表（没配就没有任何浏览器会话，纯 opt-in）
- **这是项目第一次引入真正的 LLM 工具调用循环**，直接呼应了本次讨论"什么时候真的需要agent"的结论：How You Fit 用的 `/jobs/search-results/` 页面结构从没被验证过（不像已验证过的 `jobs-tracker` 页面），硬编码选择器猜错的话会静默返回空结果不报错——这正是"分支太多、画不出固定流程图"的场景。方案是**混合模式**：`linkedin_list_scan.py`（从 `linkedin_tracker.py` 抽出来的共享确定性扫描逻辑，`linkedin_tracker.py` 相应改成薄封装）优先跑，只有它一无所获时才升级给 `linkedin_how_you_fit._scan_with_agent()` 接管——LLM 每轮看页面上的候选可交互元素，自主决定点哪个/滚哪个/判定结束，不是每次同步都要花 LLM 调用成本。候选元素感知走过一次弯路：最初按"更多"/"加载"关键词预筛，复审后改成按 DOM landmark 结构性提取（不猜文案），并加了两道安全网（候选排除职位链接本身、动作后检测页面是否被导航离开），详见 [tech-solution.md](tech-solution.md#关键决策)
- `llm.py` 新增 `chat_tool_step()`（当时只支持 anthropic；2026-08-29 补上了 DeepSeek 支持，见下面「LinkedIn agent 兜底支持 DeepSeek」）
- 涉及文件：`llm.py`（新增 `chat_tool_step`）、`linkedin_list_scan.py`（新增，从 `linkedin_tracker.py` 抽出共享扫描逻辑）、`linkedin_how_you_fit.py`（新增）、`linkedin_tracker.py`（改造为薄封装）、`config.py`、`job_state.py`、`app.py`、`scheduler.py`、`templates/index.html`、`static/app.js`、`static/style.css`、`tests/test_llm_tool_use.py`（新增）、`tests/test_linkedin_how_you_fit_sync.py`（新增）、`tests/test_linkedin_tracker_sync.py`（改动mock点，逻辑不变）
- 全量测试套件（`tests/run_all.py`，16 个套件）复跑通过；How You Fit 页面的真实 DOM 结构没有用真实登录态验证过（要登录才能看，没法开发时预先打开看），如果 agent 兜底路径实测频繁判定"卡住"，需要手动带界面模式看一眼真实页面调整候选提取逻辑，已记入 [tech-solution.md](tech-solution.md#已知技术限制)

### 邮件拒信自动识别（本地落库半段，2026-08-23）
- 起因：用户问"能不能自动查邮箱、把拒信对应的职位从已投递挪到已拒绝"。讨论后明确两个关键决策点：
  1. **架构取舍**：`app.py`/`scheduler.py` 这个独立跑着的本地服务本身没有 Gmail 访问能力（要接的话得单独配一套 Gmail API OAuth 凭证，一次性设置但有点繁琐）；Claude Code 当前会话已经有 Gmail MCP 连接器可以直接用、零配置。用户选了后者——不追求"完全无人值守的后台自动"，接受"靠 Claude Code 手动/`/loop`定时跑"这种介于全自动和全手动之间的形态，跟 mission.md"不追求7x24云端可用性"的既有边界一致
  2. **确认方式**：识别出疑似拒信后，不直接改库，先把候选列表（公司/职位/邮件主题摘要）列给用户看，用户确认后才批量落库——用户明确选择这条而不是全自动直接改，理由是拒信措辞模糊时容易误判（比如"我们还在看其他候选人"这类不算拒信的话术）
- 实现只做了"本地读写"这一半，Gmail 搜索/读信/判断拒信与否这一半没写进代码里、由 Claude Code 在对话中直接调用 Gmail 连接器完成（这类判断本质是语言理解，写死规则不如让当次对话里的 LLM 直接看邮件正文判断）：新增 `models.list_applied_jobs()`（列出当前"已投递"职位供 Claude 知道该查哪些公司）、新增脚本 `email_rejection_scan.py`（`list-applied` 输出待查清单；`apply --file confirmed.json` 把确认后的职位批量改成 `application_status='rejected'` 并用已有的 `add_job_note()` 记一条 `source='email_scan'` 的备注留痕，复用现成的 `set_application_status()`，没有新的状态副作用）
- 只支持 Gmail 一个邮箱，第二个邮箱（用户没细说是什么服务商）先不做，以后需要再接（非 Gmail 的话得走 IMAP 账号密码或对应 OAuth，目前完全没有这块代码）
- 新增 `tests/test_email_rejection_scan.py`（隔离临时库，覆盖 `list_applied_jobs()` 只返回 `applied` 状态、`apply` 子命令改状态+记备注两处），接入 `tests/run_all.py`；README 新增"邮件拒信自动识别怎么用"说明具体操作步骤和限制
- 已知限制：这不是"程序自动"，是"Claude Code 帮你跑"——不开 Claude Code 会话/没人触发 `/loop` 的时候不会自己检查
- **设置页可配置提醒间隔，默认每天（同一天当天追加）**：用户要求"让用户在设置里可配置，默认是每天"。新增 `config.json` 的 `email_scan_interval_days`（默认1，0=关闭提醒），设置页新增对应输入框（`templates/index.html`、`static/app.js`，复用 `days_old` 那套"留空/0"处理逻辑，接入 `app.py` 现成的数字字段校验循环，不用另写校验代码）。新增 `models.email_scan_runs` 表 + `record_email_scan_run()`/`last_email_scan_run()`：`email_rejection_scan.py` 新增 `record-run` 子命令，Claude 完成一整轮"查清单→搜Gmail→用户确认→落库"后记一笔时间戳（只列候选没走完确认的半截扫描不算数，避免误判"刚查过"）。每日任务清单（`GET /api/checklist`）据此算出 `email_scan_due`/`email_scan_days_since`，前端 `renderChecklist()` 到期时显示提醒文案，因为扫描动作本身不在网页里发生（在 Claude Code 对话里），这条没有可点击跳转的站内页面，只有"今天先别提醒"的勾选框
- 首次真实运行验证：对当时库里 18 条"已投递"职位按公司名过 Gmail，未发现任何拒信（多数是这两三天刚投的自动确认信）；顺带在 Amazon 邮件里发现一条活跃面试进展（招聘方已在约电话面试时间）在追踪表里还停留在"已投递"状态，job id 与库内记录对不上（大概率是绕过本工具直接在官网投的），已告知用户但没有自动改状态

### 邮件拒信自动识别——无人值守每日扫描（2026-08-24）
- 起因：用户问"能不能每天自动查，不需要再 Claude Code 里发指令"，是对上面 2026-08-23 那次"已知限制：不开会话/没人触发 `/loop` 不会自己检查"的直接回应。
- 先探路了 Anthropic 云端 Routine（`/schedule`），发现两个硬性障碍导致这条路暂时走不通：(1) 这个账号下云端 Routine 可用的连接器列表里没有 Gmail（只有 Indeed），Gmail MCP 目前只对本机 Claude Code 会话开放；(2) `jobs.db` 在 `.gitignore` 里、只存在本机，云端沙箱的隔离 git checkout 读不到。跟用户确认后选择本机方案：Windows 计划任务每天定时跑一次 headless（`claude -p ... --dangerously-skip-permissions`）命令，本机 Gmail 连接器和 `jobs.db` 都能直接用，不依赖云端。
- **无人值守场景比交互式扫描多一道确认环节**：交互式扫描（人在对话里当场确认）可以直接 `apply` 改库；headless 计划任务没有人当场确认，继续沿用 2026-08-23 那条"拒信措辞模糊容易误判，不能全自动直接改库"的决策，新增待确认队列——`models.py` 新增 `pending_rejections` 表 + `add_pending_rejection()`/`list_pending_rejections()`/`remove_pending_rejection()`；`email_rejection_scan.py` 新增 `queue` 子命令（跟 `apply` 参数一样，但排队不改状态）；`app.py` 每日任务清单接口 (`GET /api/checklist`) 新增 `pending_rejections` 字段，新增 `POST /api/pending-rejections/<id>/confirm`（改状态+记备注，复用 `apply` 一样的落库路径）和 `/dismiss`（直接出队不改状态）两个接口；首页每日任务清单新增对应展示行（`static/app.js` `renderChecklist()`、`static/style.css`），带"确认是拒信"/"不是，忽略"两个按钮。
- headless 会话用 `--dangerously-skip-permissions` 免确认地用 Gmail 连接器和 Bash（计划任务没人盯着没法逐条允许），因此"只能 queue、不能 apply"这条约束写在 prompt 里、由 prompt 而不是权限系统挡住"自动改库"——这是这个方案里唯一还需要人工留意的风险点：如果 headless 会话没听懂 prompt、跑了 `apply` 而不是 `queue`，权限层不会拦。
- 涉及文件：`models.py`（新表+3个函数）、`email_rejection_scan.py`（`queue` 子命令）、`app.py`（`pending_rejections` 字段+2个接口）、`static/app.js`/`static/style.css`（待确认队列展示）、`tests/test_email_rejection_scan.py`/`tests/test_checklist.py`（新增覆盖）、README 新增"无人值守每天自动扫描"小节（含 `schtasks` 命令示例）
- 全量测试套件复跑通过（`tests/test_email_rejection_scan.py`、`tests/test_checklist.py` 单独跑通过；`tests/run_all.py` 里 `test_resume.py` 偶发失败但确认是本次改动之前就存在的测试间状态污染，跟这次改动无关，未处理）
- 已知限制：`schtasks` 计划任务本身没有在这次对话里创建（本机计划任务的具体创建命令还需要用户自己执行或在下一步确认后执行）；电脑当天没开机/任务被禁用的话那天就不会扫，前端"该查一次邮箱了"提醒兼职当健康检查信号

### 面试语音练习模块（2026-08-23）
- 起因：用户 2026-08-25 有一场 Amazon 二面，手头有一份自己整理的复合准备文档（`Amazon_Interview_Prep.pdf`：自我介绍、10 个 STAR 故事、LP 对照、业务理解问答、已预判的追问、常见行为面问题等），想要"上传文档 → AI 出二面题 → 逐题语音回答 → AI 打分+建议"的练习入口
- 跟已有"面试准备"（`interview_preps`，针对具体职位生成参考问答）和"通用题库"（`interview_bank`，AI 起草答案供编辑）都不同：这两个都是"AI 帮你写答案"的形态，[product-review.md](product-review.md) 2026-08-18 快照恰好诊断过这一点让用户觉得"虚假、没有创造出新东西"，给出的方向是"少替你写，多向你提问，把真实经历问出来"。这次做的形态正好是提问式的——上传文档只用来出题，打分反馈刻意不返回"标准答案"，跟 AI 代笔的角色边界分开
- 跟下面"计划中"里冻结的 **P3 模拟面试**（AI 扮演面试官多轮打字问答）是两个不同的东西：P3 是通用的多轮对话模拟，这次做的是"针对一份具体文档、逐题语音作答、单题即时打分"，形态和触发的紧迫性都不一样（2 天后就要真实面试，不是"两周内能不能增加面试机会"这种一般性优先级判断），P3 本身仍然冻结
- 独立模块，不挂具体职位（上传的文档往往是复合材料，不是标准 JD）：新增 `interview_docs`/`interview_practice_sets`/`interview_practice_answers` 三张表（`models.py`）；新增 `pdf_extract.py`（PyMuPDF 抽取正文——实测 pypdf 对这份文档的中文内嵌字体解析出来是乱码，换 PyMuPDF 完全正常，见 [tech-solution.md](tech-solution.md#关键决策)）；`interview.py` 新增 `generate_practice_questions()`（优先从文档里已有的"追问预案"/"常见问题清单"改编出题，覆盖不到的才由 AI 补充，每题标 `source_hint`）和 `score_practice_answer()`（评分标准取自文档自己的"面试当天技巧"一类元指导，不返回改写后的标准答案）；`pipeline.py`/`app.py`/`job_state.py` 按面试准备的既有模式接了后台线程+轮询（出题）和同步请求（单题打分）
- 语音方案用浏览器 `SpeechRecognition`（Web Speech API），零后端依赖；识别文字提交打分前可编辑修正识别错误；新增页面 `/interview/practice`（`templates/interview_practice.html` + `static/interview_practice.js`），从首页顶栏和题库页各加一个入口
- 新增 LLM 功能位 `interview_practice`（质量优先档，同面试准备/题库），`llm.LLM_TASKS`/`config.py` 默认配置同步加了这一项
- 范围取舍：只收 `.pdf`；不做题目手动编辑/增删、不做 TTS 朗读、不做跨场次分数趋势统计——2 天时间线内先跑通"生成+练习"这条闭环
- 涉及文件：`pdf_extract.py`（新增）、`models.py`、`job_state.py`、`llm.py`、`config.py`、`interview.py`、`pipeline.py`、`app.py`、`templates/interview_practice.html`（新增）、`static/interview_practice.js`（新增）、`static/style.css`、`templates/index.html`、`templates/interview.html`、`requirements.txt`（新增 `pymupdf`）、`tests/test_llm.py`/`tests/test_frontend.py`（`LLM_TASKS` 断言同步更新）、`tests/run_all.py`（`JS_FILES` 加入新文件）

### 「智能抓取」顺带同步 How You Fit（2026-08-23）
- 起因：用户发现点首页「智能抓取」主按钮并不会带上 How You Fit 同步（此前只有专门按钮/下拉菜单项/每日定时任务三条触发路径，见上面「同步 LinkedIn "How You Fit" 匹配推荐搜索」），问是否需要补上，并要求同时在 UI 上提示这一点。确认要"两者都要"：既顺带触发，也要有 UI 提示。
- 实现：`app.py` 的 `trigger_search()`（`/api/search/run`）在关键词搜索跑完后，如果配置里有已启用的 How You Fit 搜索、且批量同步没有正在别处跑着，顺带起一个后台线程跑 `sync_all_enabled_searches()`——跟专门的「同步 How You Fit 全部搜索」按钮共用同一把并发锁（`start_how_you_fit_batch()`），谁先点谁占用，后来的顺带触发静默跳过（不像专门按钮那样报 409，因为这只是"顺带"，不是用户这次操作的主要目的）。两个风险源（访客身份抓取 vs 登录态浏览器）继续按 2026-08-23 早些时候记录的取舍各自独立 try 逻辑，互不牵连。
- UI 提示：`runNowBtn` 和设置页 How You Fit 区块的 `field-hint` 都补了说明文字；顺带触发时前端会把「同步 How You Fit 全部搜索」按钮切到"同步中…"态并复用已有的轮询逻辑，外加一条 toast 提示，而不是让用户毫无感知地在后台开一次登录态浏览器；下拉菜单按钮的 title 也提示了这颗按钮的 loading 态是共用的。同时补了页面刷新/重开标签页时的恢复态检查（原来这颗按钮没有这一层，只有「收藏」「已投递」两个 tracker 按钮有，见 `static/app.js` 的 `DOMContentLoaded`）。
- 涉及文件：`app.py`（`trigger_search()`、新增 `_has_enabled_how_you_fit_searches()`）、`static/app.js`（`runNow()`、`DOMContentLoaded` 里补批量同步恢复态检查）、`templates/index.html`（按钮/设置区 hint 文案）、`tests/test_linkedin_how_you_fit_sync.py`（新增 3 个用例：无启用搜索时不触发、正常顺带触发且与专门按钮共锁、已有同步在跑时静默跳过）、`README.md`、`spec/tech-solution.md`
- 全量测试套件（`tests/run_all.py`）复跑通过

### 修复「同步 LinkedIn 已投递」漏同步、新增「同步 LinkedIn 面试」（2026-08-26）
- 起因：用户反馈 LinkedIn 上一条明明显示"已进入面试"的职位（Otter · Product Manager - Guest Engagement），同步之后职达里完全没有。排查发现两个独立 bug：
  1. **翻页识别缺一种文案**：`jobs-tracker/?stage=applied` 页面实测是数字翻页（1/2/3/下一步），不是无限滚动/"加载更多"——`linkedin_list_scan.py` 的 `LOAD_MORE_TEXTS` 之前只认"显示更多结果"/"加载更多"/"Show more"这类无限滚动文案，认不出"下一步"这个翻页按钮，导致扫描只能停在第一页。实测验证：用户当时 LinkedIn 上「已申请」共 42 条，修复前只能扫到 10 条（第一页），修复后能扫全 42 条。
  2. **"面试"这个 stage 从没被同步过**：LinkedIn 把职位挪进「面试」列表后就不再出现在「已投递」列表里了，而 `linkedin_tracker.SUPPORTED_STAGES` 一直只有 `saved`/`applied` 两个，凡是已经进入面试阶段的职位，光同步"已投递"永远同步不到——这正是 Otter 那条职位的情况（它当时在 LinkedIn 的 stage=interview 列表里，从没出现在 stage=applied 里）。
- 修法：`linkedin_list_scan.py` 的 `LOAD_MORE_TEXTS` 加入"下一步"/"下一页"/"Next"（点到最后一页按钮被禁用时 click() 抛异常会被现有的 try/except 吞掉，回退到 `stable_rounds` 计数自然停止，不会死循环，不用额外加终止逻辑）；`linkedin_tracker.SUPPORTED_STAGES` 新增 `"interview"`；`job_state._tracker_sync` 补上对应的状态槽位（这个字典是模块加载时按 stage 预先列出来的，加新 stage 两边都要改，见文件内注释）；`app.py` 的 `_sync_tracker_background` 新增 `stage == "interview"` 分支，同步进来的职位标记 `application_status='interviewing'`（不是`'applied'`——遵循已有的进度模型 applied < interviewing < offer，见 `models._APPLICATION_PROGRESS_RANK`，不倒退）；新增按钮「同步 LinkedIn 面试」（`templates/index.html`、`static/app.js` 的 `TRACKER_STAGE_META`，前端这块本来就是按 `Object.keys(TRACKER_STAGE_META)` 泛化处理的，加一条 entry 自动接入刷新恢复态逻辑，不用额外改动）。
- 验证：`tests/test_linkedin_tracker_sync.py` 新增一段覆盖"面试" stage 同步后自动标记 `interviewing`（跟已有的"已投递"→`applied` 那段对称）；全量测试套件复跑通过。真实环境验证：重启本机服务后触发一次面试同步，Otter 那条职位成功入库（job id 203, `application_status='interviewing'`）；顺带触发一次已投递同步做翻页 bug 的回归验证，补入库 7 条此前一直卡在翻页问题后面、没扫到的职位。
- 涉及文件：`linkedin_list_scan.py`、`linkedin_tracker.py`、`job_state.py`、`app.py`、`templates/index.html`、`static/app.js`、`README.md`、`tests/test_linkedin_tracker_sync.py`

### 同步 LinkedIn 收藏后顺带核对投递状态（2026-08-26）
- 起因：上一条修完当天，用户接着反馈"从 LinkedIn 收藏同步进来的职位出现在待审核，而它们在 LinkedIn 的状态很多时已投递，应该直接放入职达的已投递列表"。排查确认：LinkedIn 的"收藏"和"投递"不是互斥状态——同一条职位可以同时出现在「已收藏」和「已投递」列表里（先收藏、后来又投了，职位不会自动从收藏夹移出），但 `linkedin_tracker.sync_tracker_stage()` 一直假设"从收藏列表来的职位=还没投"，同步"已收藏"时从不碰 `application_status`，这个假设本来就不成立。在真实库里核实：`application_status='not_applied'` 的 115 条 LinkedIn 职位里有 8 条的 LinkedIn id 其实已经出现在"已投递"列表里，不是孤例。
- 修法第一版：`models.py` 新增 `reconcile_application_status_from_linkedin(applied_ids, interview_ids)`——按 job_url 解析出的 LinkedIn 职位 id 匹配，把库里落后的投递状态推进（待投→已投递→面试中，只升不降，用已有的 `_APPLICATION_PROGRESS_RANK`），"已拒绝"/"已婉拒"两个终态显式排除在外（不能只靠 rank 比较——它俩跟"待投"同分，但不该被 LinkedIn 列表里还挂着的旧记录"复活"回已投递；"Offer"不需要显式排除，rank 本身已经最高）。`linkedin_tracker.sync_tracker_stage()` 收尾时调用：不管这次同步的是哪个 stage，都顺手把"已投递"+"面试"这两个集合核对一遍（如果当前 stage 正好是其中一个，直接复用已经扫到的，不重复开浏览器）；这一步包了 try/except，失败只记日志、不影响本次同步已经入库的结果。
- **第一版留了一半没修完**：用户紧接着又反馈"待审核里这 6 个不对，应该是已投"——查下来发现只改 `application_status` 不够：职位卡片（`static/app.js` 的 `jobCardHtml()`）只在 `status==='reviewed'`（已收藏）时才渲染投递状态相关的交互，`status` 还停在 `'new'`（待审核）的话，改对了的 `application_status` 根本没地方显示、也没法继续操作。而且这不是当天新引入的问题——`app.py` 的 `_sync_tracker_background()` 自"同步 LinkedIn 已投递/面试"这个功能（2026-08-22）上线起，同步新入库的职位后就一直只调用 `set_application_status()`，从没管过 `status` 要不要跟着挪，两个字段不一致的情况其实存在了好几天，只是没人注意到。
- 修法第二版：`models.py` 新增 `_promote_reviewed_for_applied_jobs()`——扫全库（不限定 LinkedIn、不限定这次核对推进了什么）里"`status='new'` 但 `application_status != 'not_applied'`"这种不该出现的组合，统一推进 `status` 到 `'reviewed'`；故意跳过 `'dismissed'`（已忽略）——那是用户显式做过的决定，不该被这里悄悄撤销。`reconcile_application_status_from_linkedin()` 收尾时调用它，返回值改成按"职位 id"去重合并两边的更新集合，不再按"改了几个字段"重复计数。
- 代价：三个同步按钮现在不管点哪个，后台都要多开 1~2 次浏览器扫另外两个列表，单次同步耗时从"一两分钟"变成"两三分钟"，已在 README 里向用户说明。
- 验证：`tests/test_reconcile_application_status.py` 覆盖 9 个场景（待投→已投递+status 推进、待投→面试中、已投递→面试中、Offer 的 application_status 不降级但 status 仍推进、已拒绝/已婉拒终态保护但 status 仍推进、不匹配 id/非 linkedin 来源两个字段都不受影响、空输入不报错、已经是"已收藏"的不受影响、"已忽略"的 status 不被越权覆盖）；全量测试套件（18 个套件）复跑通过。真实环境验证：重启服务后先后触发"已收藏"、两次"已投递"同步，库里 8 条历史 `application_status` 错配全部修好（5 条因为"已投递"列表翻页扫描有网络波动、分两次同步才补全，顺带验证了"翻页偶发漏页、再同步一次能补上"这条说明属实）；第二版上线后再触发一次同步，"待审核"里卡住的 6 条里有 5 条（真实已在 LinkedIn 投递过的）被正确推进到"已收藏"+对应投递状态，剩下 1 条（CoinTR，真实确认还没投）保持"待审核"不动，符合预期。
- 涉及文件：`models.py`（新增 `reconcile_application_status_from_linkedin()`、`_promote_reviewed_for_applied_jobs()`、`_RECONCILE_ADVANCEABLE_STATUSES`）、`linkedin_tracker.py`（`sync_tracker_stage()` 收尾新增核对步骤）、`static/app.js`、`README.md`、`tests/test_reconcile_application_status.py`（新增）、`tests/run_all.py`

### 「已收藏待投递」/「已投递」/「面试中」/「已忽略」改按时间倒序展示（2026-08-26，2026-08-27 补上「已忽略」）
- 起因：用户要求这几个筛选视图按时间排序，最近的排最前——2026-08-17 视觉改版把列表默认顺序从"最新在前"统一改成了"重点关注置顶 + 全场按匹配度从高到低"（服务"一堆还没决定的职位该优先看哪条"这个目标），但「已投递」「面试中」这类已经有明确进展的职位，用户想看的是"最近发生了什么"，不是"AI 觉得哪条分高"，硬套匹配度排序反而不直观。「已忽略」当时漏改，2026-08-27 用户反馈后补上，道理相同——已经决定不考虑的职位，翻看时更想看"最近忽略了什么"。
- 修法：`static/app.js` 的 `renderJobs()` 里的 `isChronologicalView` 分支——当前筛选是"已收藏待投递"（`currentStatus==='reviewed' && currentAppStatus==='not_applied'`，对应顶部「已收藏」统计卡）、「已投递」「面试中」（`currentAppStatus` 是 `applied`/`interviewing`）或「已忽略」（`currentStatus==='dismissed'`）时，跳过重点关注置顶+hero 大卡那套匹配度分组，改成整个筛选结果按时间倒序铺开：已投递/面试中用 `applied_at`（投递那一刻记的时间戳；面试中沿用同一个字段，因为 `set_application_status()` 只在变成"已投递"那次写入，后续推进到面试中不会再更新，够反映"最近有动静"），没有该字段（`applied_at` 为空的历史记录）退回 `first_seen`；「已收藏待投递」「已忽略」都没有各自的时间戳（库里没有"收藏时间"/"忽略时间"字段），直接用 `first_seen`（入库时间）。相似职位折叠（`groupJobsForRender()`）跟排序方式无关，照常保留。其它筛选状态（待审核、外企/国内等）不受影响，仍是原来的匹配度分组排序。
- 验证：无自动化测试（纯前端渲染顺序，项目里没有 DOM 断言的前端测试基建），2026-08-26 的三个视图曾用 Playwright 核对过（见下）；2026-08-27 的「已忽略」改动只改了一处布尔条件，未重新起 Playwright 验证，`tests/run_all.py` 全量套件复跑通过（含 JS 语法检查）。2026-08-26 验证记录：起真实浏览器加载三个筛选视图（`?status=&app=applied`、`?status=&app=interviewing`、`?status=reviewed&app=not_applied`），读取 `allJobs` 里对应字段核对渲染出的卡片顺序，三个视图都确认严格按时间倒序（含 `applied_at` 为空退回 `first_seen` 的记录正确排到对应位置）。
- 涉及文件：`static/app.js`（`renderJobs()`）

### 通知铃铛：同步/AI分析类后台任务完成后提醒（2026-08-26）
- 起因：项目里几乎所有"同步"（LinkedIn 收藏/已投递/面试列表同步、How You Fit 同步）和"AI 分析"（批量匹配分析、材料生成、简历体检、题库起草、面试准备、语音练习出题、偏好档案刷新、公司国籍分类、批量重新获取JD）都是后台线程跑的，耗时几十秒到几分钟不等，之前唯一的反馈通道是 `showToast()`——只有用户正好停留在发起操作的那个页面、且没跳走/关闭浏览器才能看到结果，代码里有两处注释已经承认这个缺口（`static/app.js` "批量重新获取JD、识别公司国籍这些后台任务跑完没有任何通知"）。用户要求补上，讨论后确认覆盖全部后台 AI/同步任务（不止最初提到的两类），呈现方式是顶栏铃铛图标，6 个页面都要有
- 新增 `notifications` 表（`models.py`，`created_at/category/level/title/message/link/read_at`）+ 四个 CRUD 函数（`add_notification`/`list_notifications`/`unread_notification_count`/`mark_all_notifications_read`）。不做单条已读追踪、不做行数清理——打开下拉列表即视为"看过"、统一清零未读数（参照 `/api/checklist` 的"今天先别提醒"那一档复杂度），行数清理参照 `search_runs` 表的先例（同类日志表从不清理）
- 跟现有的 `/api/checklist` 是两个不同的概念：checklist 聚合"当前有哪些条件成立"，条件消失提醒也消失；这里的通知是"发生过一件事"，落一条记录，看过之前一直在
- 在 `app.py` 约 13 个 `_*_background` 函数（批量匹配分析、公司国籍分类、偏好档案刷新、LinkedIn 三个 stage 同步、How You Fit 单条/批量同步、材料生成单条/批量、重新获取JD单条/批量、面试准备生成、题库AI起草、面试语音练习出题、简历体检）的成功/失败分支各加一行 `add_notification()`，复用它们已有的 try/except 结构；`scheduler.py` 的每日定时任务（`_run_job()`）额外在结尾发一条汇总通知（新增职位数+自动分析数）——这是唯一完全没人盯着屏幕的触发路径，最需要"回来看板"
- `pipeline.py` 的 `maybe_refresh_preference_profile()` 原来内部三个分支都不 return（隐式返回 None），没法区分"没触发生成"和"触发了但失败/成功"，改成显式返回 `None`/`{"error":...}`/`{"ok": True}`，供通知埋点判断要不要发、发成功还是失败
- `_easy_apply_background` 不接入：成功时会打开一个真实浏览器窗口等用户当场操作，没有"错过"的场景，失败信息已经通过 `job_state.get_easy_apply_error()` 常驻展示在职位卡片上
- 新增接口 `GET /api/notifications`（`{items, unread}`）、`POST /api/notifications/read_all`（`app.py`）
- 前端：`static/common.js` 新增 `initNotifications()`（页面加载拉一次未读数，之后每 25 秒轮询一次，`document.hidden` 时跳过、切回前台立刻补一次）、`toggleNotifDropdown()`（点开铃铛拉列表渲染，随即调 `read_all` 清零未读）；6 个模板（`index.html`/`job_detail.html`/`interview.html`/`job_interview.html`/`interview_practice.html`/`resume.html`）的 `topbar-actions` 各自加一段铃铛按钮+徽标+下拉容器标记（现状顶栏本来就是 6 处各自维护、没有共享 partial，见下面"三页共享 topbar"仍是冻结中的待办）；`static/style.css` 新增 `.notif-wrap`/`.notif-badge`/`.notif-dropdown` 等样式，深色模式沿用现有 token
- 新增 `tests/test_notifications.py`（models CRUD、两个新接口、`_refetch_missing_jd_background`/`_classify_company_origins_background` 两个后台函数成功/失败分支落对通知），接入 `tests/run_all.py`；全量测试套件（19 个套件 + 7 个 JS 语法检查）复跑通过
- 涉及文件：`models.py`、`app.py`、`scheduler.py`、`pipeline.py`、`static/common.js`、`static/style.css`、`templates/index.html`、`templates/job_detail.html`、`templates/interview.html`、`templates/job_interview.html`、`templates/interview_practice.html`、`templates/resume.html`、`tests/test_notifications.py`（新增）、`tests/run_all.py`、`README.md`

### How You Fit 同步数量偏少时也升级给 agent 兜底（2026-08-27）
- 起因：用户反馈"How You Fit 的三个链接每次只能同步很少几个职位，是不是因为不能滚动加载更多"。排查 `linkedin_list_scan.py` 确认：确定性扫描找不到"加载更多"按钮时会退化成 `page.mouse.wheel(0, 3000)` 滚整个窗口——但 How You Fit 页面（`/jobs/search-results/`）很可能是左侧职位列表单独一个 `overflow-y: auto` 的可滚动容器，滚窗口滚不到那个容器，导致最初渲染的几个职位收集完后连续 3 轮没有新增就被判定"到底"提前停手。`_scan_with_agent()` 兜底本身已经会扫描页面找真正的可滚动容器（见 `_PAGE_SNAPSHOT_JS`），但 `sync_search()` 原来只在确定性扫描**完全抱空**（`if not job_ids`）时才升级给它，"扫到一点但不全"这种情况永远走不到这条兜底路径
- 修法：`linkedin_how_you_fit.py` 新增 `MIN_DETERMINISTIC_JOB_IDS`（=5），`sync_search()` 判断条件从 `if not job_ids` 放宽成 `if len(job_ids) < MIN_DETERMINISTIC_JOB_IDS`，升级时把确定性扫描已收集到的结果和 agent 兜底的结果取并集（`job_ids | _scan_with_agent(...)`），不再互相替换丢数据。代价是数量本来就正常偏少的搜索也会多花一次 agent 调用，但 `MAX_HOW_YOU_FIT_SEARCHES` 已经把每日总次数卡住了，可接受
- `tests/test_linkedin_how_you_fit_sync.py` 相应更新：原"确定性扫描命中即不触发 agent"用例改用达到阈值的职位数；新增一条"数量偏少时升级、且结果与 agent 取并集去重"的用例
- 全量测试套件（19 个套件 + 7 个 JS 语法检查）复跑通过；这次修改只调整了升级判断条件，没有改动 agent 兜底本身识别可滚动容器的逻辑（那部分从上线起就没变过），实际效果需要用户下次真实同步时验证
- 涉及文件：`linkedin_how_you_fit.py`、`tests/test_linkedin_how_you_fit_sync.py`

### LLM 功能层收敛部分共享工具：反幻觉文案 / 输出校验 / 上下文截断（2026-08-29）
- 起因：整理 `spec/architecture.md` 第5层"LLM 功能层"时发现，四个调 LLM 的模块（`analyzer.py`/`interview.py`/`job_chat.py`/`resume_review.py`）各自独立重复发明了同一类东西——"别编造"这句提示四个 prompt 里各写了一遍；`interview.py` 的 `_clamp()` 和 `resume_review.py` 的 `_clamp01()` 是几乎一模一样的分数裁剪代码；`job_chat.py` 的字符数截断也是独立发明。跟 `llm.py` 当初从 `analyzer.py` 拆出来的判断标准一致（多个消费者各自造轮子该收敛成共享函数），决定把这几处收进 `llm.py`
- `llm.py` 新增三样东西：`ANTI_FABRICATION_NOTE`（反幻觉标准文案常量）、`clamp()`/`require_dict()`（输出校验小工具）、`truncate()`（按字符数截断）
- 只收了"形状完全一样、替换没有代价"的部分：`job_chat.py` 的字符数截断和"别编造"那句改成调 `llm.truncate()`/引用 `llm.ANTI_FABRICATION_NOTE`；`interview.py`/`resume_review.py` 里重复的分数裁剪函数删掉，改调 `llm.clamp()`
- **刻意没动**的部分：`analyzer.py` 的 `validate_analysis_result()`（必填字段清单 + 结构校验 + 越界直接抛错）比通用工具更完整，而且是 `evals/run_analyzer_eval.py` 明确测过的路径，强套通用工具只有回归风险没有收益；`interview.py`/`resume_review.py`/`analyzer.py` 里已经针对具体子任务调好措辞的"别编造"提示（比如"不要编造候选人没有的经历"）没有替换成通用文案——这些提示比通用版本更精确，换成通用文案是文字倒退不是改进
- 讨论中确认过 prompt 版本化/回滚**不需要做**：git 本身就是 prompt 的版本历史，`evals/run_analyzer_eval.py` 已经是"改 prompt 前后跑一遍回归"的质量闸门，单人开发场景不需要再建运行时可切换的版本系统
- 全量测试套件（19 个套件 + 7 个 JS 语法检查）复跑通过
- 涉及文件：`llm.py`、`job_chat.py`、`interview.py`、`resume_review.py`、`spec/architecture.md`

### 防幻觉三层加固：调用可观测性 / 确定性后验证 / 采样控制（2026-08-29）
- 起因：用户看到一份"五层防幻觉体系"分享（Prompt 层 / RAG 层 / 生成控制层 / 后验证闭环层 / 可观测性层），问要不要照着优化本项目。逐层评估后**只做真正有收益的三层**，跳过的部分和理由记在 [tech-solution.md](tech-solution.md) 的"防幻觉分层：做什么、不做什么"
- **第5层 可观测性**：新增 `llm_calls` 表（`models.py`）记录每次真实 API 调用的 task/provider/model/耗时/token/成本/成败。埋点用 `_CallRecord` 打在 `llm.py` 的 `_call_anthropic`/`_call_deepseek`/`_call_anthropic_tools` 三个收口函数上，一处全覆盖。`task` 用 ContextVar 从 `resolve_task()` 带下去（不改任何调用点签名，理由见 tech-solution）；`extract_json` 失败改抛 `LLMJsonError`（带模型实际返回的前 500 字符）并把对应流水行回填成失败。新增 `GET /api/llm/stats`。此前 Anthropic 的 `resp.usage` 全项目从未被读过
- **第4层 后验证**：新增 `resume_edits.py`——把模型声称"照抄"的段落原文回简历里比对（归一化 + 0.90 相似度容忍誊写偏差），区分"只摘抄了半段"和"索引指错段"。这修的是一个真实的数据损坏路径：`resume_docx.write_tailored_resume` 是**整段替换**，模型只改一句就会把该段其余内容静默删掉。体检页保留并标记不可应用（`static/resume.js` 禁用 checkbox + 警告），定制简历路径直接丢弃（无人工复核），`pipeline.build_optimized_resume` 落盘前服务端重新核查一遍——顺带堵上"重新上传简历后应用旧建议导致索引全部错位"这条正常路径
- **第4层 打分规则代码化**：`analyzer.apply_score_rules()` 把"未覆盖的强制性要求把 cognitive_match 封顶到 0.5/0.3"这条规则从 prompt 自然语言落成代码。`requirement_items` 新增 `is_mandatory` + `mandatory_evidence`（JD 原话逐字照抄），程序回 JD 原文核验，**核不过就不封顶**（fail-open，避免模型把 preferred 也标成强制导致误杀）。保留 `raw_cognitive_match`，`evals/run_analyzer_eval.py` 改判这个字段——否则封顶之后 eval 那条断言必然通过、失去检测 prompt 漂移的能力
- **第3层 采样控制**：`MODELS` 新增 `supports_sampling`，`llm.py` 新增 `TASK_TEMPERATURE` 按功能位配温度。**关键约束：`claude-sonnet-5` 已从 API 移除采样参数，传 `temperature` 直接 400**，而它是本项目的默认模型——所以采样只能按模型开关，不能无条件加。温度值尚未用 eval 量过，是保守初值
- 顺带修：`MODELS` 里的价格从 `note` 自然语言改成 `price_in`/`price_out` 结构化字段（流水算成本要用，只能有一处来源）；原来 sonnet-5 标的 "$3/$15" 是过期的 Sonnet 4.6 价格，实际 $2/$10
- **⚠ 分数断点**：封顶规则上线后新老 `overall_match` 不可比，同一职位重跑分析可能从 0.8 掉到 0.4。`jobs` 表是覆盖式更新（`update_job_analysis`），查不到上一次的值
- 新增测试 `test_llm_logging.py`（含一条"注入必然抛错的 recorder，主流程仍要正常返回"的回归锁）、`test_resume_edits.py`、`test_analyzer_rules.py`；`test_llm.py` 补"Sonnet 5 绝不能收到 temperature"的回归锁。全量 22 套件 + 7 个 JS 检查通过
- 待办：跑一轮真实批量分析验证 `mandatory_evidence` 核验通过率（低于 ~70% 就该只记录不封顶）；用 `--repeats` 量温度对分数离散度的影响（只能在 haiku/deepseek-flash 上做）
- 涉及文件：`llm.py`、`models.py`、`app.py`、`analyzer.py`、`resume_review.py`、`resume_edits.py`（新）、`pipeline.py`、`routes_misc.py`、`linkedin_how_you_fit.py`、`static/resume.js`、`static/common.js`、`static/style.css`、`evals/run_analyzer_eval.py`

### LinkedIn agent 兜底支持 DeepSeek（2026-08-29）
- 起因：用户当前没有配置 `ANTHROPIC_API_KEY`，而 `linkedin_how_you_fit.py` 的 agent 兜底（`_scan_with_agent()`）之前硬编码只走 Anthropic 的 tool-use 协议，没有 key 就直接抛错——即使 `DEEPSEEK_API_KEY` 已经配置了也用不上
- `llm.py` 新增 `_call_deepseek_tools()`（DeepSeek 的 OpenAI 兼容 function calling，`tool_choice="required"` 对应 Anthropic 的 `tool_choice={"type":"any"}`，同样强制模型每轮必须选一个工具）；`chat_tool_step()` 新增 `provider` 参数分派到对应实现；`tools` 统一仍用 Anthropic 风格的 `{name, description, input_schema}` 定义一份，`provider="deepseek"` 时内部转换成对方要的 `{type:function, function:{...,parameters}}` 形状，调用方不用为两家各写一份
- 两家的会话续接协议不一样（Anthropic 用 content block 里的 `tool_use`/`tool_result`，DeepSeek/OpenAI 兼容用 `tool_calls` + `role="tool"`），这层差异原来是摊在调用方 `linkedin_how_you_fit.py` 里手写的，这次收进 `llm.py`：`chat_tool_step()` 返回值把 `content_blocks` 改名 `assistant_message`（原样存回 messages 即可，内部形状按 provider 不同，调用方不用关心），新增 `tool_result_message(provider, tool_use_id, content)` 生成回填消息
- `linkedin_how_you_fit._scan_with_agent()` 的 provider 选择策略：优先 Claude（更擅长这类"看页面描述、自主判断该点哪"的任务），没有 `ANTHROPIC_API_KEY` 才退到 `DEEPSEEK_API_KEY`，两个都没有才抛错——不是"只用 DeepSeek"，是"两个都没配置才阻塞"，以后补上 Claude key 会自动切回去，不用再改代码
- **2026-08-29 追加**：优先级反过来改成优先 DeepSeek，没有 `DEEPSEEK_API_KEY` 才退到 `ANTHROPIC_API_KEY`——原因是成本更低，不是效果更好；两个都没配置才抛错这条兜底逻辑不变
- DeepSeek 侧用的仍是 `MODELS` 注册表里的默认模型（`deepseek-v4-pro`），已核实该模型支持 function calling（DeepSeek 官方文档 + 社区实测，`deepseek-reasoner` 上一代才不支持）
- `tests/test_llm_tool_use.py` 新增 `provider="deepseek"` 的 tools/tool_choice 转换、tool_calls 解析、`tool_use=None`、`tool_result_message()` 四条用例；`tests/test_linkedin_how_you_fit_sync.py` 的"没有 API key"用例改成同时 pop 两个 key，新增"只有 DEEPSEEK_API_KEY 时正常退到 deepseek 跑完"一条
- 全量测试套件（22 个套件 + 7 个 JS 语法检查）复跑通过
- 涉及文件：`llm.py`、`linkedin_how_you_fit.py`、`tests/test_llm_tool_use.py`、`tests/test_linkedin_how_you_fit_sync.py`

### LinkedIn 智能匹配推荐搜索条数上限从 8 调到 12（2026-08-29）
- 起因：用户问能不能把「LinkedIn 智能匹配推荐」每次能配置的搜索条数上限从 8 改到 20。上限本身是 2026-08-18 决策的配套风险缓释措施（`MAX_HOW_YOU_FIT_SEARCHES`，跟同步间隔节流一起把账号风险摁住），改成 20 相当于每日自动扫描次数涨到 2.5 倍，跟用户确认后选了更保守的折中值 12（约 1.5 倍）
- 只改了一个常量：`linkedin_how_you_fit.MAX_HOW_YOU_FIT_SEARCHES = 8` → `12`；同步更新了引用这个数字的三处提示文案（`config.py` 注释、`templates/index.html` 设置区块提示、`README.md` 账号风险提醒），后端校验（`routes_core.py`）和测试（`tests/test_linkedin_how_you_fit_sync.py`）都是引用常量而非硬编码数字，不用改
- 涉及文件：`linkedin_how_you_fit.py`、`config.py`、`templates/index.html`、`README.md`

> 优先级说明（2026-08-18 产品 review 后确立）：使用者处于**已离职、求职紧迫**的时期，本节按"这件事能不能在两周内增加真实面试机会"排序。下面「求职决策闭环」是 P0/P1，其余批次（UI/UX P1+P2、P3 模拟面试、界面双语切换）**明确冻结**，等求职告一段落再动。完整论证见 [product-review.md](product-review.md)（2026-08-18 快照）。

### 职位收集链路的错误处理生产级加固（讨论于 2026-08-29）
起因：用户问"职位收集 agent 出错了怎么处理，方案完善吗"。通读 `scraper.py` / `job_link.py` / `linkedin_tracker.py` / `linkedin_how_you_fit.py` / `scheduler.py` / `job_state.py` 后的结论——**故障隔离做得好，故障恢复几乎没有**：每个失败单元都被 try/except 圈住了（不会一条挂全批挂），但圈住之后一律是"记一条 error 就结束"，没有重试、没有分类、没有熔断、没有历史。以下按缺口列，实施顺序见末尾。

**已经做对、不要在加固时推翻的**
- 分区隔离：`scraper` 每个 `关键词×城市×站点` 组合、`scheduler._run_job` 四个子步骤、`sync_all_enabled_searches` 每条搜索、`add_jobs_from_urls` 每条链接，都是各自 try/except
- 错误文案面向用户可操作（"先跑 ensure_logged_in()"、"profile 被别的窗口占着"），不是裸堆栈
- 节流上限齐全：`MAX_SCROLLS` / `MAX_AGENT_STEPS` / `MAX_HOW_YOU_FIT_SEARCHES` / `linkedin_request_delay`
- agent 兜底的安全网（导航离开就中止、无效编号容错一轮）
- 浏览器 profile 独占靠 Chromium 自己的 SingletonLock，不自造会卡死的文件锁

**缺口 1：所有失败都是终局的，全项目零重试。** `grep retry` 只有"无头撞墙→带界面重试一次"这种模式切换，不是瞬时故障重试。网络抖动/超时/429 跟"职位已下架"走同一条路：记一行 error 结束，下次机会是 24 小时后的定时任务。对唯一无人值守的路径来说，一次抖动 = 丢一整天的收集窗口。

**缺口 2：错误没有分类。** `errors` 是 f-string 拼的字符串列表，塞进 `search_runs.error` 一个 TEXT 字段。限流 / 网络 / 登录失效 / DOM 改版 / 代码 bug 五类混在一起，但它们的正确处置正好相反——限流该退避降速，登录失效该**立刻停手**（继续撞只会加深账号风险），DOM 改版该告警取证，bug 该修。没有分类就谈不上任何自动处置。

**缺口 3：静默的"成功但是空"没被当成故障（最阴的一类）。** `scan_job_list` 返回空集时，tracker 同步照常返回 `total_found=0`、通知 `level=success` 写"同步完成 · 共找到 0 条"——DOM 改版导致选择器全失效，和"列表确实空的"在 UI 上长得一模一样。`scraper` 同理：某个组合返回 0 行不计入 `errors`、`found` 也不涨，而 LinkedIn 访客接口被限流时正是返回近乎空的页面（roadmap 里 `f_TPR` 那条空结果 bug 就是这个形态，当初靠人肉抓包才发现）。`linkedin_how_you_fit` 的 `MIN_DETERMINISTIC_JOB_IDS` 是全项目唯一一处"数量偏少=可疑"的判断，思路对，但只长在这一条路径上。

**缺口 4：`sync_search()` 里 agent 兜底失败会连累确定性结果（设计级 bug）。** `job_ids = job_ids | _scan_with_agent(url, ...)`——确定性扫描拿到 4 条（低于阈值 5）触发升级，agent 判 stuck 抛 `HowYouFitSyncError`，整个 `sync_search` 挂掉，那 4 条也一起丢了。兜底本该是增强，现在变成了额外的失败源：不升级还能入库 4 条，升级了反而 0 条。

**缺口 5：账号风险没有刹车。** 这是本项目比一般服务更该关注的一层——失败代价不是"这次没数据"，是账号被封（2026-08-18 那条决策的顾虑）。现在登录态失效时的实际行为是：`fetch_search_job_ids` 无头撞墙→带界面再撞一次，`sync_all_enabled_searches` 捕获异常后**继续下一条搜索**，8 条搜索 × 2 次会话 = 在已经被风控盯上的时候再撞 16 次。

**缺口 6：进程内状态 + 无 wall-clock 上限。** `job_state` 全是内存 dict、后台线程 `daemon=True`。进程重启后前端轮询拿到 `syncing=False / result=None / error=None`，用户看到的是"什么都没发生"，实际是同步被腰斩（部分入库、reconcile 没跑）。同时整条同步没有任何时间上限：`scan_job_list` 最多 60 轮滚动 + `add_jobs_from_urls` 逐条抓几百条 × delay，可以跑几十分钟，期间 `start_tracker_sync` 的 409 一直挡着所有重试。

**缺口 7：收集侧没有可观测基线。** `llm_calls` 表（2026-08-29）把 LLM 侧的成本/失败率解决得很好，收集侧没有对应物：`search_runs` 只有一行汇总 + 一个 error 字符串，tracker / how_you_fit 同步**压根不落库**，只有 notifications 里一句人话。查不到"最近 7 次 saved 同步分别找到几条"——而趋势断崖正是发现"DOM 悄悄改版"唯一可靠的信号。

**缺口 8：agent 失败不留证据。** `_scan_with_agent` 抛异常即结束，页面快照和 messages 全丢。复现只能再跑一次（再花一次 LLM 钱、再冒一次账号风险）。而 DOM 改版是这个项目**必然反复发生**的故障类型（roadmap 里已有两次这类排查记录：`f_TPR` 空结果、tracker 翻页按钮文案）。

**计划的加固方案（8 项，按依赖分层）**
- [x] **① 错误分类地基**（2026-08-29 完成）：新建 `collect_errors.py`（第4层，零 LLM 依赖，只依赖 `job_state` + `easy_apply`）。`classify(exc, http_status=None) -> kind`，能从异常类型/HTTP状态码/文案判断出 `transient`/`rate_limited`/`auth`/`locked`，认不出的一律保守归 `bug`（不盲目当成"值得重试"）；额外会看一层 `exc.__cause__`，这样 tracker/how_you_fit 把 `EasyApplyInProgress` 包成自己的 `XxxSyncError` 再 `from e` 抛出后，classify() 仍能顺着链路认出原始类型。`structure`/`upstream_empty` 两类依赖对结果内容的判断（agent 判定卡住的理由、数量是否偏少），没法从异常类型推断，刻意留给调用方直接指定，避免 `collect_errors.py` 反过来 import `linkedin_how_you_fit.py` 等模块造成循环依赖。`RETRY_POLICY` 表把每个 kind 的处置（要不要重试、重试几次、退避多久、通知级别）集中写好，供以后的⑤读取。涉及文件：`collect_errors.py`（新）、`tests/test_collect_runs.py`（新）。
- [x] **② `collect_runs` 表 + `/api/collect/stats`**（2026-08-29 完成，对标 `llm_calls`）：新增 `collect_runs` 表（`models.py`），字段跟规划一致：`source`（`jobspy`/`tracker_<stage>`/`hyf_<search_id>`/`manual_urls`）、`started_at`/`finished_at`/`duration_ms`、`found`/`added`/`skipped_duplicate`/`skipped_irrelevant`/`failed`、`ok`、`error_kind`/`error_detail`、`retries`（占位，⑤还没做，目前恒为0）、`agent_used`、`suspicious`（占位，⑦还没做，目前恒为0）。**成败都写一行**——`scraper.run_search_once()`、`linkedin_tracker.sync_tracker_stage()`、`linkedin_how_you_fit.sync_search()`、`routes_search.add_jobs_by_url_route()` 四个入口各自在收尾处（含异常分支）记一行，之前 tracker/hyf 完全不落库的缺口补上了。`GET /api/collect/stats`（`routes_misc.py`）按 `source`/`error_kind` 聚合 + 总计，默认最近 30 天。刻意不存页面 HTML 原文，跟 `llm_calls` 不存 prompt 原文同一个取舍。降级但没丢数据的情况（③ 里 agent 兜底失败）记 `ok=1, error_kind="structure"`，不算失败，这个判断由 `linkedin_how_you_fit.py` 直接指定，不经过 `classify()`（原因见①）。涉及文件：`models.py`、`routes_misc.py`、`scraper.py`、`linkedin_tracker.py`、`linkedin_how_you_fit.py`、`routes_search.py`、`tests/test_collect_runs.py`（新）。全量测试套件（23 个套件 + 7 个 JS 语法检查）复跑通过。
- [x] **③ 修 `sync_search()` 的降级路径**（2026-08-29 完成）：agent 兜底失败改成 `try/except HowYouFitSyncError`，保留确定性扫描已拿到的 `job_ids` 照常入库，返回结果里加一个 `degraded` 字段说明原因，不再整条抛错连累已收集到的结果。落地时先记日志+返回字段，`collect_runs`（②）落库是后来补上的，见②的说明——降级会记成 `ok=1, error_kind="structure"`。涉及文件：`linkedin_how_you_fit.py`、`tests/test_linkedin_how_you_fit_sync.py`（新增用例"sync_search keeps deterministic results and reports degraded when agent fallback fails"）。
- [x] **④ LinkedIn 账号风险熔断**（2026-08-29 完成）：新增 `job_state.LinkedInAuthRequired` 异常 + 进程内 breaker（`record_linkedin_auth_failure/success`、`linkedin_auth_breaker_open`，连续 2 次真实撞墙就熔断 30 分钟）。`linkedin_tracker.TrackerAuthError`/`linkedin_how_you_fit.HowYouFitAuthError` 多重继承各自的 SyncError + `LinkedInAuthRequired`，让熔断器认出这一类失败又不破坏已有的 `except TrackerSyncError`/`except HowYouFitSyncError` 调用点；`job_link.fetch_via_browser()` 只读取（不写入）熔断状态，共用同一个登录 profile 的账号风险。**只在"无头+带界面都确认撞墙"这个真实发出过请求的分支计入熔断计数**，"从没保存过登录态"这种本地配置检查不算（没有发出请求，没有账号风险）。`sync_all_enabled_searches` 遇到 `LinkedInAuthRequired` 立即 `break` 整批，不再继续尝试剩下的搜索。**范围内明确没做**：`easy_apply.run_easy_apply()` 没接入熔断——那是用户直接点按钮、盯着浏览器看的场景，风险特征跟无人值守的定时同步不同，读/写熔断的收益不明确，留到真的需要时再做，不是漏掉。涉及文件：`job_state.py`、`linkedin_tracker.py`、`linkedin_how_you_fit.py`、`job_link.py`、`tests/test_linkedin_tracker_sync.py`、`tests/test_linkedin_how_you_fit_sync.py`。全量测试套件（22 个套件 + 7 个 JS 语法检查）复跑通过。
- [x] **⑤ 分层重试**（2026-08-29 完成）：新增 `collect_errors.with_retry(fn, *args, **kwargs)`，按 `RETRY_POLICY` 只重试 `transient`/`rate_limited`，指数退避+0~1s抖动，用 ContextVar（`reset_retry_count`/`get_retry_count`，跟 llm.py 的 `_current_task` 同一手法，避免逐层传参改一大片函数签名）统计这次运行触发了几次重试供 `collect_runs.retries` 用。包住的是**最小可安全重放的操作**：`scraper.py` 两处 `scrape_jobs()` 调用、`linkedin_tracker._scan_tracker_jobs`/`linkedin_how_you_fit.fetch_search_job_ids` 里的 `scan_job_list()` 调用；`EasyApplyInProgress` 会被 `classify()` 判成 `locked`（不可重试），第一次失败就直接向上抛，不浪费重试次数在"等别的窗口"上。`job_link.fetch_via_guest()` 的 HTTP 429/5xx 是"请求成功但状态码不对"，`with_retry()` 只认异常抓不到，单独按同一张 `RETRY_POLICY` 表手写了一个退避循环（`collect_errors.note_retry()` 手动计数）。**踩过一个坑**：初版测试直接 `job_link.time.sleep = lambda s: None` 想跳过退避等待，忘了 `job_link.time` 就是进程里唯一那份 `time` 模块——这样改的是全局 `time.sleep`，把 `test_add_by_url.py` 自己等后台分析线程完成的轮询循环也一起废了（循环瞬间跑完 300 次、不给后台线程真实的墙钟时间），改成只在触发重试的那次 POST 期间临时替换、`finally` 里立刻复原才对。
- [x] **⑥ wall-clock deadline**（2026-08-29 完成）：`collect_errors.start_run_deadline()`/`check_deadline()`/`clear_run_deadline()`，同样用 ContextVar（原因同上，调用链路太深不逐层加形参，也不会让已有测试里替身掉中间某层的 fake 函数因签名对不上而报错）。`linkedin_tracker.sync_tracker_stage()`/`linkedin_how_you_fit.sync_search()` 整个函数体在 `try/finally` 里设一个 20 分钟的统一 deadline；`linkedin_list_scan.scan_job_list()` 的滚动循环每轮检查，超时抛 `TimeoutError`（`classify()` 已经认得，落库按 `transient` 处理）；两处按 `MAX_URLS` 分批入库的循环也在批次之间检查。`_scan_with_agent()` 的每轮循环额外检查一次，超时转成 `HowYouFitSyncError` 而不是让 `TimeoutError` 直接向上抛——`sync_search()` 的降级路径只捕获 `HowYouFitSyncError`（见③），保持"agent 兜底只抛这一种异常"的约定，让超时也能走正常降级流程。
- [x] **⑦ 空结果健康检查**（2026-08-29 完成）：新增 `models.recent_found_counts(source, limit)` + `collect_errors.is_suspicious_drop(current, history)`——历史数据不够（<3 次）保守判不可疑，够的话用中位数×0.3 且绝对值 <5（复用 `MIN_DETERMINISTIC_JOB_IDS` 已经在用的同一个阈值，保持全项目"数量少到值得怀疑"口径一致）。命中时 `ok` 仍是 1（真的跑完了）、`error_kind` 记 `upstream_empty`、`suspicious` 记 1、返回结果带 `suspicious` 字段，文案写"可能是页面结构变了，不是真的没有新职位"。`hyf_<id>` 只在没触发 agent 兜底的路径上比——一旦触发过 agent，数量已经是"确定性扫描+agent修正"的结果，跟历史高位数字不是同一种情况的对照组。`scraper` 侧按计划文本用了不同的判法：没有任何组合报错但整个搜索周期一条职位都没找到就算可疑（单个组合 0 行仍正常），不跟历史中位数比——一次 `run_search_once()` 汇总几十个组合，总数波动天然比单条 tracker/hyf 同步大得多，中位数比较容易大量误报。降级到通知没有新增"warning"视觉级别（图标/CSS），沿用已有的 `info`——为这个低频场景加一整套新视觉级别代价不成比例，文案本身已经说清楚是什么情况。
- [x] **⑧ 故障现场取证 + 调度器加固**（2026-08-29 完成）：新增 `collect_errors.save_debug_snapshot(label, page)`，只在 `_scan_with_agent()` 三处判定为 `structure` 的失败点（判定卡住/被导航离开/超步数上限）调用，写 `debug_snapshots/<label>_<微秒级时间戳>/page.html`+`screenshot.png`，按 label 滚动保留最近 5 份；全程 try/except，取证本身失败不能变成新故障源（跟 `llm.py` 埋点"观测绝不能成为新的故障源"同一条原则），`.gitignore` 加了这个目录。调度器：`reschedule()` 的 `add_job()` 补上 `misfire_grace_time=3小时`/`coalesce=True`/`max_instances=1`；新增 `models.last_successful_collect_run_at(source)` + `scheduler._maybe_catch_up()`，进程启动时检查 `jobspy` 最近一次成功记录是否超过 25 小时（比 24 小时略宽松，日常调度抖动不该被误判成"错过了"），超过且用户没关掉每日定时任务，就立刻在后台补跑一次——处理的是 `misfire_grace_time` 处理不了的量级："进程/系统整个睡了一晚上"，不是"卡了几分钟"。


**明确不做（避免过度工程，跟 [tech-solution.md](tech-solution.md) 既有取舍一致）**
- 不引入 Celery/RQ/Redis 或持久化任务队列/死信队列：单用户本地应用，重试次数是个位数、时间窗口是分钟级，进程内够用；真正需要"跨重启不丢"的是**结果**，那已经在 SQLite 里
- 不接 Sentry/外部 APM：`collect_runs` + notifications 覆盖单用户场景
- 不做自动重新登录：登录必须人工，这是安全边界，也是 `easy_apply.py` 既有的明确决策
- 不做"让 agent 自动改选择器并写回代码"：诱人但风险极高

实施顺序按性价比：① ② 先行（地基，无行为变化，先能看见）→ ③ ④（正确性和账号风险，最急）→ ⑤ ⑥ → ⑦（等 ② 攒够历史）→ ⑧。

**8 项全部完成（2026-08-29）**。涉及文件汇总：`collect_errors.py`（新，分类/重试/deadline/健康检查/取证五件套）、`models.py`（`collect_runs` 表 + `insert_collect_run`/`collect_run_stats`/`collect_run_totals`/`recent_found_counts`/`last_successful_collect_run_at`）、`routes_misc.py`（`/api/collect/stats`）、`scraper.py`、`job_link.py`、`linkedin_tracker.py`、`linkedin_how_you_fit.py`、`linkedin_list_scan.py`、`job_state.py`（④ 熔断）、`routes_search.py`、`scheduler.py`、`.gitignore`（`debug_snapshots/`）；新增测试 `tests/test_collect_runs.py`、`tests/test_scheduler.py`，修改 `tests/test_linkedin_tracker_sync.py`/`tests/test_linkedin_how_you_fit_sync.py`/`tests/test_add_by_url.py`（含一处真实踩坑修复，见⑤的说明）。全量测试套件（24 个套件 + 7 个 JS 语法检查）复跑通过。

### 职位列表新增「批量选择」，忽略原因弹窗暂时隐藏（2026-08-30）
用户要求：职位列表能多选、全选，一次性把选中的移入「已忽略」；同时把忽略时弹出的"为什么不考虑这个职位"原因小弹窗暂时隐藏，以后再挪进设置页做成开关。

- **批量选择**：筛选栏新增「批量选择」按钮，点开后职位卡片（含 hero 大卡、相似分组折叠里的成员）左侧出现复选框，工具条上有「全选当前筛选下的职位」+ 已选计数 + 「移入已忽略」+ 「取消」。全选只作用于当前筛选结果（`renderJobs()` 新增的 `currentRenderedJobIds`），不是全库；切换筛选后已选中的职位不会被清掉。「移入已忽略」并发调用现成的 `/api/jobs/<id>/status` 接口逐条置为 `dismissed`（量级是用户手动选出来的一批，几十条封顶，没必要为此新开批量后端接口），成功后弹一次汇总 toast、自动退出选择模式。
- **忽略原因弹窗暂时隐藏**：`setJobStatus()` 里"忽略后自动弹一次原因小弹窗"的调用被注释掉（不是删除，方便以后按设置项恢复）——批量忽略几十条时逐条弹窗完全不可用。已忽略卡片上"记录忽略原因"的手动补录入口（`dismissReasonButtonHtml`）没有跟着关，想留原因的人仍可以手动点。这个自动弹窗以后计划挪进设置页做成一个开关，暂未实现。
- 用真实 Playwright 端到端验证过：勾选/全选/计数/按钮禁用态、批量忽略后 toast 提示、自动退出选择模式、两条测试职位都正确进入"已忽略"且过程中没有弹出忽略原因弹窗，控制台无报错。全量测试套件（24 个套件 + 7 个 JS 语法检查）复跑通过。
- 涉及文件：`templates/index.html`（「批量选择」按钮 + `batchSelectBar` 工具条）、`static/app.js`（`toggleBatchSelectMode`/`toggleJobSelected`/`toggleSelectAllJobs`/`updateBatchSelectBar`/`batchDismissSelected`，`jobCardHtml` 加复选框，`renderJobs` 记录 `currentRenderedJobIds`，`setJobStatus` 注释掉忽略原因弹窗调用）、`static/style.css`（`.batch-select-bar`/`.job-select-checkbox`/`#batchSelectToggleBtn.active` 样式）。

### 求职决策闭环（讨论于 2026-08-18）
来自使用者提出的 7 条一手痛点。review 把它们归因到三个根因：**评分器没有记忆**（痛点③⑦）、**没有工作流状态机**（痛点④⑥）、**抓取层**（痛点①②）。数据佐证：37 条 ≥0.7 的职位里被人工忽略 11 条、只投了 5 条；6 条已投递中 5 条是星标——星标比 AI 分数更能预测投递。

**P0 批次（本周）**
- [x] **每日任务清单**（痛点④）：首页顶部按库状态自动生成可勾选清单（今日抓取 / N 条待审核 / M 条待生成材料 / K 条待投递 / J 条投了超 7 天该跟进），支持自定义条目。顺带把零使用率的备注/简历体检/标签带到用户面前 —— 2026-08-18 完成，见下面「每日任务清单 + 忽略原因收集→偏好档案」。"投了超N天该跟进"这条的阈值天数、以及是否显示，2026-08-29 起可在设置页「自动化与提醒」调（`stale_application_reminder_days`，0=关闭），用户反馈这条提醒太烦后加的开关
- [x] **投递状态自动化**（痛点⑥）：列表页加一键「我投了」，投递时间用于上面的跟进提醒 —— 2026-08-20 完成，见上面「投递状态自动化：一键「我投了」」。范围有调整：Easy Apply 走完**没有**自动置 `applied`（讨论后确认那一刻只代表浏览器打开、材料填好，不代表已提交，自动置状态会有假阳性），改成引导用户提交后自己点「我投了」
- [x] **忽略原因收集 → 偏好档案**（痛点③⑦的地基）：忽略时弹一行原因（预设标签 + 自由文本）存库；累计到阈值后一次 LLM 调用总结成「偏好档案」，注入 `analyzer.py` 的 prompt。可用现有 11 条"高分被忽略"补录冷启动。review 判断这是整个项目**唯一有结构性差异化**的能力——竞品的反馈学习全在雇主端，to-C 侧因拿不到足量单用户信号而做不了，本地单用户工具反而做得到 —— 2026-08-18 完成

**P1 批次（两周内）**
- [ ] **每日/每周复盘报告**（痛点⑦）：今天审核 N 条 / 投递 M 条 / 生成材料 K 份 + 偏好总结 + 下一步综合建议。数据源是上面两条 P0
- [x] **LinkedIn 推荐职位手动导入**（痛点①）：**不做**个性化推荐流的抓取（需登录态高频请求，封号风险直接命中求职主通道）；改做「粘贴 URL / 批量粘贴」导入通道，走完整分析链路 —— 2026-08-18 完成，见上面「手动粘贴 LinkedIn 职位链接入库」
- [x] **重点关注公司定向搜索**（讨论于 2026-08-18，痛点①的补充方案）：使用者反馈普通关键词搜索不保证每次都能覆盖到重点公司的新职位。设置页新增「重点关注公司」（每行一个名字），保存时用 `linkedin_company.resolve_company_ids()` 解析成 LinkedIn 数字公司 ID（访客页优先，抓不到再退化到已登录浏览器兜底，复用 `job_link.py` 的两级抓取骨架）并缓存，避免每次保存都重新解析。`scraper.run_search_once()` 对每个已解析的目标公司额外跑一次 `linkedin_company_ids` 定向搜索（jobspy/LinkedIn 官方过滤器，不是自建爬虫逻辑）——沿用现有关键词，不收窄范围；不按城市循环，交给过滤器返回全量结果；跟常规搜索共用同一套去重/粗筛（`_ingest_df()` 从原来内联在循环里的逻辑抽出来，两处调用同一份代码，不会有"两处标准不一致"的风险）—— 2026-08-18 完成，涉及文件：`linkedin_company.py`（新增）、`config.py`（`linkedin_target_companies`）、`scraper.py`、`app.py`、`templates/index.html`、`static/app.js`、`tests/test_linkedin_company.py`（新增，网络与浏览器全 mock）
- [x] **跨源去重加固**（痛点②，讨论于 2026-08-16）：核实后发现痛点描述有偏差——11 条"高分被忽略"里跨源（Indeed↔LinkedIn）重复其实只有 1 对，真正的大头是**同一家公司在同一个源上连续发近似岗位**（Amazon 一家占 10 条，全在 Indeed），外加公司名后缀不统一（库里 "Amazon.com" 和 "Amazon" 被当成两家公司）。修了两层：① `models.normalize()` 加标点/空白清理 + `normalize_company()` 剥离常见法律实体后缀（`.com`/`Inc`/`Ltd`/`有限公司`等），`make_dedupe_key()` 用新规则；一次性迁移历史行的 `dedupe_key`（幂等、两行撞车时只更新更早那行，不删除/不合并任何数据）。② `annotate_similar_groups()`：同公司标题词汇 Jaccard 相似度 ≥0.5（用真实库数据验证的阈值——能分开 Amazon 那批近似 AI PM 岗位，同时不误合并 Blizzard 的 Hearthstone/WoW 两个产品线，相似度 0.33）打 `similar_group_id`，首页「其余职位」区块里折叠展示成一组，展开后每条仍完整可操作，不删除/不合并数据（重点关注和今日最高不参与折叠，避免弱化已经标星的决定）—— 2026-08-18 完成，涉及文件：`models.py`、`app.py`、`static/app.js`、`static/style.css`、`tests/test_dedupe_normalize.py`（新增）
- [ ] **材料生成触发点后移**：从"详情页随时可点"改成"标记准备投递时才生成"。现状是 24 条已生成里 20 条没投、7 条所在职位后来被忽略，约 80% 打水漂
- [ ] **首页主数字改口径**：从「N 条越过 70% 投递线」改成诚实口径（如「N 条待你决定」）。评分器已被数据证伪，不该把它的输出放在全页最大字号上

**冻结期间仍要做的两个例外**（各是几行，且挡住日常路径）
- [ ] `/jobs/<id>` 补移动端断点：`style.css:908` 的 `.detail-layout` 没被 720px 块覆盖，手机上是压缩而不是堆叠
- [ ] 未分析职位允许点进详情页：`app.js:691` 的 `clickable` 判定挡住了它，而 `job_detail.js:98` 的空态早就写好了，现在只能手敲 URL 才看得到

**P2（求职告一段落后）**
- [ ] **求职策略 / 职位定位模块**：接住 `解决问题方面的思考` 里那条因果链——定位不清 → 投了不想去的岗 → 面试没热情 → 不愿准备。与偏好档案联动，回答"我到底想要什么样的工作"
- [ ] **面试准备形态转向**：从"替你写标准答案"改成"向你提问、把真实经历问出来"。使用者笔记直言"觉得虚假、需要包装自己"，数据佐证 `work_history` 分类 0 条、`self_intro` 仅 1 条——最需要个人真实素材的两类恰恰是空的。这比再加一个 P3 模拟面试更能解决问题

### 待决策：自动投递红线（痛点⑤，讨论于 2026-08-18）
使用者提出"对不那么重点关注的职位自动操作浏览器完成投递"，直接冲突 [mission.md](mission.md) 里"代码里没有、也不会有点击提交申请的逻辑"这条承诺。已在 [product-review.md](product-review.md#九待决策项自动投递红线痛点) 第九节算清两条路的工作量/风险/收益，**等使用者决策**，在此之前不动 mission.md。要点：自动投递回复率 1–3%，直接联系 hiring manager 40–60%；且痛点⑤的字面需求（自动投更多）与使用者自己笔记里的诊断（投了太多不想去的）方向相反。

### UI/UX 后续批次（讨论于 2026-08-16，**2026-08-18 起冻结**）
上面那次评审里改动更大的部分，按优先级排着：
- [ ] **P1 批次**：批量分析的批次进度（`3/57`，需后端在 `job_state.py` 记 `{total, done}`）+ 全局"后台在跑什么"指示器；把"全部重新获取JD""识别公司国籍"两个后台任务从筛选栏移走（它们不是筛选器）；筛选汇总条；弹窗补 `role="dialog"`/焦点陷阱/背景锁滚动/设置脏检查；职位卡片键盘可达；统一三种并存的确认惯例；题库页补返回链接 + 三页共享 topbar；16 个设置项补 `<label for>`；`.topbar` 补 `flex-wrap` 和 720–1000px 断点
- [ ] **P2 批次**：列表分页/虚拟滚动；`renderJobs()` 改增量更新（现在每次轮询整表 `innerHTML` 重绘，滚动位置和 hover 全丢）；题库对话流式输出；`prefers-reduced-motion`；约 60 处内联 `onclick` 改事件委托（emoji 图标统一成 SVG 已在 2026-08-17 视觉清理里完成，从这里移除）
- 注：评审时提过"深色 token 写了两遍、合并掉"，实际做的时候确认**纯 CSS 做不到**——`@media` 包不住选择器列表的一半，没有预处理器就只能保留两份。已在 `style.css` 里加注释说明改色时两处都要改

### 面试准备模块（讨论于 2026-08-16，剩余 P3 **2026-08-18 起冻结**）
现有流水线到"投递 + 状态跟踪"就结束了，对方约面试之后又回到全手工准备。分三期补上这一段，每期独立可用。P1、P2 已完成（见上面"已完成"部分），剩余：
- [ ] **P3 模拟面试**：AI 扮演面试官多轮提问、用户打字作答，结束后给评价报告（维度评分 + 优势/改进 + 逐题改写参考）；支持"针对某个职位"和"通用"两种模式、中/英文两种语言。会话状态需要持久化（支持刷新页面恢复 + 轮询），`llm.chat()` 的多轮 `messages` 支持已在 P1 就位。仍然冻结——2026-08-23 因为一场 2 天后就要到的真实面试，做了一个形态不同的"面试语音练习模块"（见上面「已完成」），单题语音作答+即时打分，不是这里说的多轮打字对话模拟，两者不是同一件事，这条本身没有解冻
- [ ] **面试准备内容并入面试题库**（讨论于 2026-08-20，冻结）：职位详情页"面试准备"的内容整合进首页"面试题库"页面，职位详情页快捷入口保留、跳转目标改为题库页里按 job_id 展示的对应分区。两者数据模型不同（面试准备按职位分版本、不双语、只读；题库跨职位复用、双语、可编辑），暂定"轻整合"——不合并 `interview_preps`/`interview_bank` 两张表和各自的生成逻辑，只在展示层加个分区入口。因处于求职优先级冻结期不直接产生面试机会，用户确认先记录、不实现

### 对外开放 + 收费（商业化，讨论于 2026-09-08）
用户提出想把项目开放给其他用户并收取费用（倾向一次性付费），问可行性、操作步骤、触达渠道。完整计划见新增的 [monetization-plan.md](monetization-plan.md)，这里只记状态和结论要点。

- **结论**：一次性收费的直觉是对的（求职需求生命周期 1–3 个月，订阅在第 2 个月就流失），但"开放"不能理解成托管 SaaS。可行形态是**本地自托管 + 一次性买断激活码 + 用户自带 API Key（BYOK）**，人群收窄到"在中国找外企/英文岗、能自己配 API key 的技术型求职者"。
- **托管 SaaS 明确否掉**，三条独立理由：① 单用户假设长在骨头里（`config.json` 全局单例 / `jobs.db` 无租户列 / `app.py` 零鉴权 / APScheduler 在 Web 进程内 / xlsx 并发写会互相覆盖），改造粗估 4–6 周；② `.playwright_profile/` 承载用户真实 LinkedIn 登录态，托管等于替陌生人保管求职主账号 cookie，是红线；③ 托管要替用户付 LLM 费用（约 ¥30–100/月/人），跟一次性收费在数学上直接矛盾——**这就是 BYOK 必须成立的原因**。
- **下一步只有阶段 0**（0 行代码，3–5 小时）：写 3 篇基于自己真实求职数据的内容 + 一份问卷，7 天内收不到「30 份问卷 + 5 人愿付费」就停止，不投入任何商业化开发。product-review 第十节"先把自己的求职跑完"那句仍然成立，阶段 0 被设计成不跟求职抢时间、且产出物对求职本身有正外部性。
- **发现的硬阻塞项（开源/分发前必须处理）**：`JD匹配追踪表.xlsx`（636KB，真实职位记录）**当前正被 git 跟踪**，git 历史里还添加过 `Amazon_Interview_Prep.pdf`，`documents/` 下也是个人面试笔记——`.gitignore` 挡不住已被跟踪的文件。另：仓库**没有 LICENSE 文件**，商业分发前必须补。`python-jobspy` 已核实是 MIT，可商业分发。
- **暂不动 mission.md**：那里现在写着"仅本人自用的本地工具，不是面向多用户的产品"，跟这条计划冲突。只有阶段 0 达标、真的决定往下走时才回去改那一句。
- [ ] 阶段 0：需求验证（3 篇内容 + 问卷，门槛见上）。**素材包已备好**（2026-09-08）：`documents/阶段0-验证素材.md` 里有三篇内容的完整草稿（按小红书/即刻/V2EX 各自改写）、问卷 5 道题、招募内测的私信话术、7 天节奏表，以及把草稿里占位数字换成今天真实值的只读 SQL（查 `jobs` / `collect_runs` / `job_dismiss_reasons` / `llm_calls`）。这份素材是个人营销草稿，**不进分发包**（跟 `documents/` 下其它个人笔记一样，见 6.1 脱敏清单）
- [ ] 阶段 0.5：**付费内测**（2026-09-08 补入，讨论"要不要先开内测"的结论）——要开，但① **收费不免费**（免费内测测不出付费意愿，还会把本来就窄的人群烧掉一遍），向问卷里选"会买"的人收早鸟 ¥69 + 无条件退款；② **3–5 人**，每人你陪装 30 分钟、在旁边看不替他操作；③ **排在打包（阶段 1）之前而不是之后**——初稿把阶段 1 排在接触真实用户之前是排序错误，等于闭眼做安装器。内测只开 Indeed、LinkedIn 抓取关、Easy Apply 不进内测；绝不加埋点（会拆掉"数据不出本机"这个卖点）。**"退了款还继续用"明确不查**（2026-09-08 讨论）：自托管+离线激活决定了技术上就不知道，最大敞口 5×¥69=¥345，为它加遥测或在线校验是负收益且会毁掉核心卖点；退款在这个阶段是产出不是损失，退款后仍在用反而是"有用但定价/形态错了"的强信号。**收到"不好用"的处理办法**（2026-09-08 讨论）：这是本阶段的产出不是失败，最坏的结果反而是五个人都说"挺好用的"然后不再打开。当场退钱、不辩解、约 20 分钟复盘，只问行为不问意见（上次打开是什么时候/卡在哪/现在实际用回了什么/它消失你会少做什么）；六种"不好用"指向相反动作，其中"匹配度不准"是已知问题（评分器已被证伪+新用户冷启动，要提前打预防针）、"不想配 API Key"三个人说就要重估路 A 的 BYOK 前提、"以为能自动投递"是文案的错不是产品的错。判据：1 人=噪音、3 人=信号，且唯一裁判是第二周还开不开，不是嘴上说什么。规模化后（阶段 2）靠三件不需要 DRM 的事：更新绑 license（产品会随平台改版失效，退款用户的拷贝自己会烂）、license 嵌邮箱并显示在界面上（社交水印）、退款邮箱记名单不再发更新。门槛：5 人里 ≥2 人第二周仍在用、≥3 人简历能跑通匹配分析。**招募来源**（2026-09-08 补）：漏斗是 30 份问卷→5–8 人说会买→3–5 人真付钱，所以阶段 0 的内容+问卷本身就是招募通道；按信号质量排序是 问卷举手的人 > 求职路上认识的人（这是本项目独有的池子）> 即刻/V2EX > 脉脉/一亩三分地 > 小红书评论区私信，**朋友是最差来源**（会给面子说好话、也不好意思收钱，一次破掉两条硬规则）；求职群/内推群不要发硬广，那是你自己的求职资源。凑不齐 5 个人 = 阶段 0 的止损信号在响，不要拉人情硬凑
- [ ] 阶段 1：可分发化（首启向导 / 打包 / 剥离个人数据 / LinkedIn 默认关闭 / 补 LICENSE / README 拆成上手版+手册版）——**阶段 0 + 0.5 都达标才做，且清单顺序按内测观察到的真实卡点重排**
- [ ] 阶段 2：收款与发货（MoR 或 爱发电；激活码做成离线可验证的签名 license，不自建校验服务器）
- [ ] 阶段 3：触达（小红书 / 即刻 / V2EX / 脉脉 / GitHub）
- [ ] 阶段 4：跑出 ≥50 个付费用户后，才重新评估托管版

### 其它
- [ ] kpi 中体现哪些是 linkedin 的哪些是 indeed（当前 LinkedIn 50 / Indeed 35）
- [ ] 右上角"原文 / 中文"语言切换按钮，支持界面双语切换（讨论于 2026-08-15，尚未实现；**2026-08-18 起冻结**）

---
最后更新：2026-09-09（记录电脑故障时阶段 0 的应对：草稿数字已预填 8-18 真实值，标清时间就能直接照发，7 天倒计时不必等电脑修好，详见 [monetization-plan.md](monetization-plan.md) 和 `documents/阶段0-验证素材.md`。上一版：2026-09-08（补入内测用户说"不好用"的处理办法，详见 [monetization-plan.md](monetization-plan.md) 阶段 0.5。上一版：2026-09-08（补入"退款后继续使用"的处理判断：不查、不加 DRM，理由和规模化后的替代做法见 [monetization-plan.md](monetization-plan.md) 阶段 0.5 与阶段 2。上一版：2026-09-08（阶段 0 的执行素材包已备好：`documents/阶段0-验证素材.md`。上一版：2026-09-08（商业化计划的阶段 0.5 补入内测用户招募来源，详见 [monetization-plan.md](monetization-plan.md) 第四节。上一版：2026-09-08（商业化计划补入阶段 0.5「付费内测」：详见上面「对外开放 + 收费」条目和 [monetization-plan.md](monetization-plan.md) 第四节。上一版：2026-09-08（对外开放 + 收费的商业化计划：讨论结论和阶段划分见上面「对外开放 + 收费（商业化，讨论于 2026-09-08）」，完整方案见新增的 [monetization-plan.md](monetization-plan.md)；`CLAUDE.md` 的文档一览同步登记了这份新文档。上一版：2026-08-30（职位列表新增「批量选择」，忽略原因弹窗暂时隐藏：详见上面「职位列表新增「批量选择」，忽略原因弹窗暂时隐藏」。上一版：2026-08-30（真正定位并修复"用 agent 测试"报"共找到 0 条"：加了诊断日志后用户再测一次，日志显示真相跟之前两次猜测都不一样——`collect_job_ids()` 在全部 12 轮里其实**每轮都正确收集到了完整的 25 个职位**（`href 匹配 9 个、componentkey 匹配 50 个，合并去重后 25 个`），页面提取逻辑本身完全没问题。真正的 bug 在 `_scan_with_agent()` 自己：这条搜索的页面第一轮就已经加载完、没有更多可加载，但模型没有按提示词预期在"连续几轮没有新增"时调用 `finish(reached_end)`，而是继续无意义地滚动/点击直到耗尽 12 轮步数上限——命中步数上限时代码只是 `raise HowYouFitSyncError(...)`，没有把循环里已经收集到的 `job_ids` 一起带出来，异常一抛，这 25 个真实、正确的职位 id 全部被扔掉，`sync_search()` 的 except 块只能报告 agent 接管前的数量（force_agent 场景下就是 0）。这是"数据在，代码没把它交出来"，不是提取失败。修法：`_scan_with_agent()` 命中步数上限时把已收集到的 `job_ids` 挂在异常的 `partial_job_ids` 属性上再抛出，`sync_search()` 的 except 块读出来并入最终结果、仍标 `degraded`（说明"步数耗尽但数据可能仍完整"）——跟"判定卡住"那种模型主动声明不可信的失败区分开，不再被同等对待直接清零。也顺带纠正了之前给用户的说法：不是"agent 不知道该怎么找职位"，是"数据已经找到了，只是失败路径把它弄丢了"。涉及文件：`linkedin_how_you_fit.py`、`tests/test_linkedin_how_you_fit_sync.py`（新增 2 条用例：`_scan_with_agent` 步数上限保留 job_ids、`sync_search` 合并 `partial_job_ids`）。上一版：2026-08-30（排查中：修完"候选误把职位卡片当可点击项"之后，用户换了另一条搜索（"与职业档案匹配"）再测，"用 agent 测试"仍然报"共找到 0 条"——说明还有别的问题没解决。核实发现：这次失败落的 `debug_snapshots/hyf_agent_step_limit_20260830_031713_510748/page.html` 里其实有 25 个 `componentkey` 格式的职位卡片，把这份快照当本地文件重放、直接调现在的 `collect_job_ids()` 也确实能提取出全部 25 个——但服务器日志显示**真实运行期间**这个函数在全部 12 轮里都收集到 0 个，且没有任何异常堆栈（不是报错崩溃，是正常返回了空结果）。这说明之前两次「已经用真实 Playwright 验证过」的说法有漏洞：验证的都是保存下来的静态 HTML 快照（没有真实 CSP、没有真实渲染时序），不能代表真实活页面执行时的行为，这次的落差正好卡在这个验证方法的盲区里。真正根因还没定位到（时序/懒加载/这条搜索本身是数字翻页而非无限滚动，都只是未经证实的猜测），先加了细粒度诊断日志（`collect_job_ids()` 分别记 href/componentkey 各自的原始匹配数；`_describe_page_for_agent()` 记原始候选总数、判定为职位卡片排除的数量、最终幸存数），用 info 级别不需要额外改日志配置，等用户下一次真实测试后对照日志再定位。涉及文件：`linkedin_list_scan.py`、`linkedin_how_you_fit.py`。上一版：2026-08-30（修复 agent 兜底把职位卡片本身当成可点击候选：用户实际观察 agent 打开的浏览器窗口后反馈"左侧每个职位要一个个点开才能拿到链接"，截图显示 agent 在逐个点开职位卡片——这不是预期行为，`collect_job_ids()` 已经能直接从页面 DOM 的 `componentkey` 属性批量拿到全部职位 id（上一条 0 条 bug 修复时验证过），完全不需要点开任何一个职位。排查发现是"安全网1"（候选列表排除职位卡片，见 `linkedin_how_you_fit.py` 模块顶部说明）只认 `parse_linkedin_job_id()` 能识别的 href，改版后的 `<div role="button" componentkey="job-card-component-ref-ID">` 卡片没有 href，这道安全网完全拦不住，职位卡片被当成普通候选混进 agent 的可点击列表——点开只是切到右侧详情面板，不算"导航离开"，第二道安全网也拦不住，纯粹浪费步数预算。给 `_PAGE_SNAPSHOT_JS` 补上 `componentkey` 属性采集，`_describe_page_for_agent()` 的排除逻辑复用 `linkedin_list_scan._COMPONENT_KEY_JOB_ID_RE` 一并拦掉这类候选。用真实捕获到的失败快照复验：89 个原始候选里 25 个是职位卡片，修复前会漏进候选列表，修复后全部正确排除、0 个泄漏。同时借用户这个问题的机会说明白：本项目的自动化本来就不走"打开职位→点分享→复制链接"这条人工路径，是直接从列表页 DOM 里批量读所有职位 id、拼出 `https://www.linkedin.com/jobs/view/{id}` 规范链接，跟分享链接指向同一个页面；用户观察到的"一个个点开"是这个候选过滤的 bug，不是设计如此。涉及文件：`linkedin_how_you_fit.py`、`tests/test_linkedin_how_you_fit_sync.py`。上一版：2026-08-30（修复 LinkedIn 智能匹配推荐"共找到 0 条"：用户用新增的"用 agent 测试"按钮实测发现页面明明有 99+ 条结果，同步结果却是"共找到 0 条"，排查过程踩了两个坑才见到真根因——① 先误以为是 DeepSeek 工具调用报 400（"Thinking mode does not support this tool_choice"），加了 `DEFAULT_DEEPSEEK_TOOL_MODEL="deepseek-v4-flash"` 想换模型解决，用户复测后仍报同样的错，直接用真实 API key 探测三个模型才发现 v4-pro/v4-flash/deepseek-reasoner 全部一样报错——是"带 tools 就默认开 thinking mode"这个 v4 系列的通病，不是某个模型的问题，真正修法是显式传 `"thinking": {"type": "disabled"}`（`_call_deepseek_tools()`，跟 Anthropic 那边的 `no_thinking` 同一个思路），这次直接用真实 API 端到端验证过再报给用户，不再只跑 mock 测试就下结论。② 修完 DeepSeek 400 之后同步不报错了，但结果仍是"0 条"——从失败时自动落的 `debug_snapshots/`（`hyf_agent_step_limit_*`）里读真实截图和 `page.html` 才找到根因：LinkedIn 把这个"根据您的偏好推荐职位"页面的卡片从 `<a href="/jobs/view/ID">` 改成了 `<div role="button" componentkey="job-card-component-ref-ID">`，`linkedin_list_scan.collect_job_ids()` 只认 href 的旧假设彻底失效，确定性扫描和复用同一个函数的 agent 兜底因此双双一直收集到 0 个、agent 因为"看不到任何新增"在原地滚了 12 轮也没用。加了一条 `componentkey` 兜底提取，用真实 Playwright 加载那份失败快照的 `page.html` 复验，从 0 条变成正确收集到 26 个职位 id。详见上面「同步 LinkedIn "How You Fit" 匹配推荐搜索」小节的追加说明，涉及文件：`llm.py`、`linkedin_list_scan.py`、`tests/test_llm_tool_use.py`、`tests/test_linkedin_tracker_sync.py`。上一版：2026-08-30（LinkedIn 智能匹配推荐设置页新增"用 agent 测试"按钮：用户想直接观察 agent 兜底导航的实际运行情况，不想每次都靠确定性扫描先跑一遍、凑巧数量不够才触发降级。给 `linkedin_how_you_fit.sync_search()` 加 `force_agent` 参数，为真时跳过 `fetch_search_job_ids()` 直接强制走 `_scan_with_agent()`——仍是一次真实同步，收集到的职位照常入库、照常记 `collect_runs`，跟正常触发相比只是"怎么进入 agent 分支"不同。路由层 `POST /api/jobs/sync_how_you_fit/<id>?force_agent=1` 把这个参数转发下去，不新增独立的状态/端点/收尾逻辑；前端在每条搜索行加一个图标按钮，直接复用已有的 `syncHowYouFitRow()`/`pollHowYouFitSync()` 轮询流程。涉及文件：`linkedin_how_you_fit.py`、`routes_search.py`、`static/app.js`、`static/style.css`、`tests/test_linkedin_how_you_fit_sync.py`（新增 force_agent 相关用例）。上一版：2026-08-29（职位收集链路错误处理生产级加固——完成剩余 ⑤⑥⑦⑧ 四项，8 项全部做完：⑤ 分层重试（`collect_errors.with_retry()`，只重试 transient/rate_limited，包住 `scrape_jobs()`/`scan_job_list()` 这类"最小可安全重放的操作"，`fetch_via_guest()` 的 HTTP 429/5xx 单独手写退避循环）；⑥ 墙钟 deadline（`start_run_deadline`/`check_deadline`，tracker/how_you_fit 同步整体 20 分钟上限，超时按 transient 落库，不再无上限占着 409 锁）；⑦ 空结果健康检查（跟历史中位数比，断崖记 `error_kind="upstream_empty"`+`suspicious=1` 但 `ok` 仍是 1；scraper 侧用"全部组合零结果"这个更粗的判法）；⑧ 故障现场取证（agent 判定 structure 类失败时存 HTML+截图到 `debug_snapshots/`，滚动保留 5 份）+ 调度器加固（`misfire_grace_time`/`coalesce`/`max_instances` 三件套 + 启动时检查距上次成功抓取是否超过 25 小时、超过就补跑一次，处理笔记本合盖休眠这个本项目最常见的错过场景）。⑤ 里踩了一个真实的坑：写测试时直接 `job_link.time.sleep = lambda s: None` 想跳过退避等待，忘了这改的是全局 `time` 模块，把测试自己等后台线程完成的轮询循环也一起废了，改成只在触发重试期间临时替换、`finally` 里立刻复原。新增 `tests/test_collect_runs.py`（扩充）、`tests/test_scheduler.py`，全量测试套件（24 个套件 + 7 个 JS 语法检查）复跑通过。详见上面「职位收集链路的错误处理生产级加固」。上一版：2026-08-29（"投递超7天该跟进"提醒加开关：用户反馈这条提醒太烦、不想再看到，新增 `config.py` 的 `stale_application_reminder_days` 字段（默认 7，0=关闭），设置页「自动化与提醒」新增对应输入框，`routes_misc.get_checklist()` 按这个值算 `list_stale_applications()` 的天数、为 0 时直接不返回 followups；用户本机 `config.json` 已顺手设成 0。详见上面「每日任务清单」小节的追加说明。上一版：2026-08-29（职位收集链路错误处理生产级加固——继续实现①②两项地基：新建 `collect_errors.py`（第4层，零 LLM 依赖）统一分类失败——`classify(exc, http_status)` 能从异常类型/HTTP状态码/文案判断出 transient/rate_limited/auth/locked，还会顺着 `exc.__cause__` 找一层（tracker/how_you_fit 把 EasyApplyInProgress 包成自己的 SyncError 再 `from e` 抛出后仍能认出来），认不出的一律保守归 bug；structure/upstream_empty 两类依赖内容判断，留给调用方直接指定，避免反向 import 造成循环依赖。新增 `collect_runs` 表（对标 `llm_calls`）+ `GET /api/collect/stats`，`scraper.run_search_once()`/`linkedin_tracker.sync_tracker_stage()`/`linkedin_how_you_fit.sync_search()`/`routes_search.add_jobs_by_url_route()` 四个入口成败都记一行，之前 tracker/how_you_fit 同步完全不落库、查不到历史趋势的缺口补上了；③ 的降级路径现在也会落库成 `ok=1, error_kind="structure"`。剩下 ⑤～⑧（分层重试、wall-clock deadline、空结果健康检查、故障取证快照）仍未做。新增 `tests/test_collect_runs.py` 并入 `tests/run_all.py`，全量测试套件（23 个套件 + 7 个 JS 语法检查）复跑通过。详见上面「职位收集链路的错误处理生产级加固」里 ①② 两条的完成说明。上一版：2026-08-29（UI 显示名「LinkedIn 个人匹配搜索（How You Fit）」改成「LinkedIn 智能匹配推荐」：用户要求把这个功能的显示名从技术黑话（How You Fit 是 LinkedIn 官方对这个视图的内部叫法）换成更好懂的中文说法。选名字时发现不能直接叫「个性化推荐」——那个词在 2026-08-18 的决策记录（[tech-solution.md](tech-solution.md#关键决策)）里专指被否决、不做自动化的 LinkedIn 首页推荐信息流，跟这里（职位资格匹配搜索页）是刻意区分开的两件事，用同一个词会让 UI 文案和决策记录自相矛盾；跟用户确认后改用「LinkedIn 智能匹配推荐」，不跟已有的决策术语冲突。只改了面向用户的显示文案（HTML 标签/按钮/tooltip/toast/通知标题/JSON 错误信息），内部模块名 `linkedin_how_you_fit.py`、函数名、配置字段 `linkedin_how_you_fit_searches` 等代码标识符原样不动，全量测试套件复跑通过。涉及文件：`templates/index.html`、`static/app.js`、`routes_search.py`、`routes_core.py`、`linkedin_how_you_fit.py`、`README.md`。上一版：2026-08-29（LinkedIn 智能匹配推荐搜索条数上限从 8 调到 12：用户问能不能改成 20，考虑到这个上限是 2026-08-18 决策专门控制账号风险的补偿措施，跟用户确认后选了更保守的 12。只改一个常量 + 三处引用的提示文案，详见上面「LinkedIn 智能匹配推荐搜索条数上限从 8 调到 12」。上一版：2026-08-29（职位收集链路错误处理生产级加固——实现 ③④ 两项最急的：修了 `sync_search()` 里 agent 兜底失败连累确定性扫描结果的设计级 bug（改成 try/except，保留已收集到的 job_ids，返回结果加 `degraded` 字段）；新增 `job_state.LinkedInAuthRequired` + 进程内熔断器，连续 2 次真实撞上登录墙就熔断 30 分钟，`tracker`/`how_you_fit`/`job_link` 浏览器兜底三条登录态路径共用同一个熔断状态（因为共用同一个账号），`sync_all_enabled_searches` 遇到登录态失效立刻中止整批不再继续撞后面的搜索。① ② ⑤～⑧ 五项仍未做（`collect_errors.py` 错误分类、`collect_runs` 表、分层重试、wall-clock deadline、空结果健康检查、故障取证快照）。全量测试套件（22 个套件 + 7 个 JS 语法检查）复跑通过，新增 5 条针对熔断器和降级路径的用例。详见上面「职位收集链路的错误处理生产级加固」里 ③④ 两条的完成说明。上一版：2026-08-29（LinkedIn agent 兜底 provider 优先级反过来：`linkedin_how_you_fit._scan_with_agent()` 原来优先 Claude、没有 `ANTHROPIC_API_KEY` 才退 DeepSeek，这次改成优先 DeepSeek、没有 `DEEPSEEK_API_KEY` 才退 Claude——原因是成本更低，不是 DeepSeek 在这类导航任务上效果更好；两个都没配置才抛错的兜底逻辑不变。详见上面「LinkedIn agent 兜底支持 DeepSeek」小节的追加说明。上一版：2026-08-29（职位收集链路错误处理生产级加固方案：用户问"职位收集 agent 出错了怎么处理、方案完善吗"。通读四条收集路径（jobspy 搜索 / 贴链接 / tracker 同步 / How You Fit agent）后的结论是"故障隔离做得好、故障恢复几乎没有"——每个失败单元都有 try/except，但一律"记一行 error 就结束"，全项目零重试、错误不分类、"成功但是空"不算故障、账号风险没有熔断、收集侧没有 `collect_runs` 这样的历史基线。另外发现一个设计级 bug：`sync_search()` 里 agent 兜底抛异常会连带丢掉确定性扫描已拿到的职位，等于兜底反而成了额外的失败源。规划了 8 项加固（错误分类地基 → collect_runs 表 → 修降级路径 → 账号熔断 → 分层重试 → wall-clock deadline → 空结果健康检查 → 取证快照+调度器加固），并明确排除 Celery/Redis/持久化队列/Sentry/自动重新登录。尚未实现。详见上面「职位收集链路的错误处理生产级加固」。上一版：2026-08-29（LinkedIn agent 兜底支持 DeepSeek：用户当前没有 `ANTHROPIC_API_KEY`，而 `linkedin_how_you_fit.py` 的 agent 兜底之前硬编码只走 Anthropic，没有 key 就直接阻塞。给 `llm.py` 的 `chat_tool_step()` 加了 `provider` 参数和 `_call_deepseek_tools()`（DeepSeek 的 OpenAI 兼容 function calling），`tools` 定义仍只写一份、内部按 provider 转换字段名；两家会话续接协议不同这层差异也收进了 `llm.py`（`assistant_message` + 新增的 `tool_result_message()`），不再摊在调用方手写。`_scan_with_agent()` 改成优先 Claude、没有才退到 DeepSeek，两个都没配置才抛错，以后补上 Claude key 会自动切回去。详见上面「LinkedIn agent 兜底支持 DeepSeek」。上一版：2026-08-29（防幻觉三层加固：用户拿一份"五层防幻觉体系"的分享来问要不要照做，逐层评估后只做真正有收益的三层——第5层可观测性（`llm_calls` 流水表 + `llm.py` 三个收口函数埋点 + task ContextVar + `GET /api/llm/stats`，此前 `resp.usage` 全项目从未被读过）、第4层确定性后验证（新增 `resume_edits.py` 把模型声称"照抄"的段落原文回简历比对，修掉 `write_tailored_resume` 整段替换导致的静默删内容路径；`analyzer.apply_score_rules` 把硬门槛封顶规则从 prompt 落成代码，靠 `mandatory_evidence` 回 JD 原文核验、核不过 fail-open）、第3层采样控制（`supports_sampling` + `TASK_TEMPERATURE`，关键是 `claude-sonnet-5` 传 temperature 会 400 而它正是默认模型）。明确跳过第2层 RAG（项目没有检索场景）和验证器模型二次核查/置信度评分（成本翻倍、收益存疑），理由记进 tech-solution。注意封顶上线后新老分数不可比。详见上面「防幻觉三层加固」。上一版：2026-08-29（LLM 功能层收敛部分共享工具：整理 `spec/architecture.md` 时发现 `analyzer.py`/`interview.py`/`job_chat.py`/`resume_review.py` 四个调 LLM 的模块各自独立重复发明了同一类东西——"别编造"提示各写一遍、`interview.py`/`resume_review.py` 里两份几乎一模一样的分数裁剪代码、`job_chat.py` 自己发明的字符数截断。跟 `llm.py` 当初从 `analyzer.py` 拆出来的判断标准一致，把形状完全一样、替换没有代价的部分收进 `llm.py`（`ANTI_FABRICATION_NOTE`/`clamp()`/`require_dict()`/`truncate()`）；刻意没动 `analyzer.py` 的 `validate_analysis_result()`（更完整、有 eval 覆盖）和几个模块里已经调好措辞的专用"别编造"提示（比通用文案更精确）。同时确认 prompt 版本化/回滚不需要做，git+eval套件已经够用。详见上面「LLM 功能层收敛部分共享工具：反幻觉文案 / 输出校验 / 上下文截断」。上一版：2026-08-27（How You Fit 同步数量偏少时也升级给 agent 兜底：用户反馈"三个 How You Fit 链接每次只能同步很少几个职位，是不是不能滚动加载更多"。排查确认根因大概率是页面左侧职位列表是独立可滚动容器，确定性扫描滚窗口滚不到，很快因连续无新增而提前停手；但原逻辑只在完全抱空时才升级给能自己找到正确容器的 agent 兜底，"扫到一点但不全"用不上这条路。把升级阈值从"抱空"放宽成"数量低于 5"，并把确定性结果和 agent 结果取并集而不是互相替换。详见上面「How You Fit 同步数量偏少时也升级给 agent 兜底」。上一版：2026-08-27（「已忽略」筛选视图漏改成按时间倒序：2026-08-26 那次改动只覆盖了「已收藏待投递」/「已投递」/「面试中」三个视图，用户回来反馈首页「已忽略」卡片也要按时间排序、新的在前，`static/app.js` 的 `isChronologicalView` 判断条件里补上 `currentStatus === 'dismissed'` 即可，复用同一套排序逻辑（没有专门的"忽略时间"字段，退回 `first_seen`）。详见上面「「已收藏待投递」/「已投递」/「面试中」/「已忽略」改按时间倒序展示」小节。上一版：2026-08-26（新增通知铃铛：同步/AI分析类后台任务（LinkedIn同步、批量匹配分析、材料生成、简历体检、题库起草、面试准备、语音练习出题、偏好档案刷新、公司国籍分类、批量重新获取JD、每日定时抓取）完成后落一条持久化通知，顶栏铃铛图标显示未读数，6 个页面都有——之前唯一的反馈通道是 toast，只有正好停留在发起操作的那个页面才能看到，代码里已有两处注释承认"跑完没有任何通知"这个缺口。新增 `notifications` 表，不做单条已读追踪，打开下拉列表即清零未读数。详见上面「通知铃铛：同步/AI分析类后台任务完成后提醒」。上一版：2026-08-26（「已收藏待投递」/「已投递」/「面试中」改按时间倒序展示：用户要求这三个筛选视图最近的排最前，不再套用 2026-08-17 定下的"重点关注置顶+按匹配度排序"那套（那套服务的是"待审核里该优先看哪条"，跟"已投递/面试中最近发生了什么"不是同一个目标）。`static/app.js` 的 `renderJobs()` 新增分支，这三个筛选下改成整体按时间（已投递/面试中用 `applied_at`，退回 `first_seen`；已收藏待投递没有 `applied_at`，直接用 `first_seen`）倒序铺开，跳过 hero 大卡和重点关注分组。用 Playwright 起真实浏览器加载三个视图核对渲染顺序，全部确认严格按时间倒序。详见上面「「已收藏待投递」/「已投递」/「面试中」改按时间倒序展示」。上一版：2026-08-26（新增「同步 LinkedIn 收藏后顺带核对投递状态」：用户反馈从"已收藏"同步进来的职位很多其实已经在 LinkedIn 上投过了，职达里却一直卡在"待投"——根因是 LinkedIn 的"收藏"和"投递"不是互斥状态，`sync_tracker_stage()` 一直假设"从收藏来的=还没投"这个假设本来就不成立。新增 `models.reconcile_application_status_from_linkedin()`，不管点哪个同步按钮都会顺手核对库里全部 LinkedIn 职位的投递状态（不止这次新入库的），真实库里 8 条历史 `application_status` 错配全部修好；上线后用户紧接着又反馈"待审核里还有 6 个不对，应该是已投"——查出只改 `application_status` 不够，`status`（审核状态）没跟着推进的话职位卡片根本不显示这些交互，而且这个不一致其实从"同步已投递/面试"功能 2026-08-22 上线起就存在，不是当天新引入的。补了 `_promote_reviewed_for_applied_jobs()` 把"待审核但已经投递"这种不该出现的组合统一推进到"已收藏"（跳过用户手动"已忽略"的，不越权覆盖）。两版加起来详见上面「同步 LinkedIn 收藏后顺带核对投递状态」。上一版：2026-08-26（修复「同步 LinkedIn 已投递」漏同步、新增「同步 LinkedIn 面试」：用户反馈一条已进入面试的职位同步后完全不在职达里，排查出两个独立 bug——`jobs-tracker/?stage=applied` 页面是数字翻页不是无限滚动，`LOAD_MORE_TEXTS` 认不出"下一步"翻页按钮导致只能扫到第一页（42 条只扫到 10 条）；以及"面试"这个 stage 从没被同步过，职位一旦从"已投递"列表挪进"面试"列表就再也同步不到。两个都修了，并新增「同步 LinkedIn 面试」按钮。详见上面「修复「同步 LinkedIn 已投递」漏同步、新增「同步 LinkedIn 面试」」。上一版：2026-08-24（新增「邮件拒信自动识别——无人值守每日扫描」：用户要求每天自动查、不用再在 Claude Code 里发指令。云端 Routine 探路后发现两个硬性障碍（Gmail 连接器不开放给云端、`jobs.db` 是本机文件云端读不到）暂时走不通，改为本机 Windows 计划任务每天跑 headless Claude Code；无人值守场景比交互式扫描多一道确认——新增 `pending_rejections` 待确认队列（`models.py`/`email_rejection_scan.py queue` 子命令/`app.py` 两个确认接口）+ 首页展示，疑似拒信先排队等网页端人工点"确认"/"忽略"，不会自动改库。详见上面「邮件拒信自动识别——无人值守每日扫描」。上一版：2026-08-23（「智能抓取」主按钮现在如果配置了已启用的 How You Fit 搜索会顺带在后台同步全部，起因是用户发现原来点这个按钮不会带上 How You Fit、要求补上并在 UI 提示；跟专门的「同步 How You Fit 全部搜索」按钮共用同一把并发锁和轮询逻辑，前端复用那颗按钮的 loading 态 + toast 提示用户，不再是悄悄在后台跑。详见上面「「智能抓取」顺带同步 How You Fit」。上一版：2026-08-23（新增「面试语音练习模块」：上传准备文档 → AI 优先从文档已有内容改编出题 → 逐题语音作答（浏览器 Web Speech API）→ AI 打分反馈（不代笔标准答案）。独立于具体职位，起因是 2 天后的真实面试；跟冻结中的"P3 模拟面试"（多轮打字对话）不是同一个东西，那条仍冻结。详见上面「面试语音练习模块」。上一版：2026-08-23（新增「邮件拒信自动识别」本地落库半段：`models.list_applied_jobs()` + `email_rejection_scan.py`（`list-applied`/`apply` 两个子命令）+ 对应测试，Gmail 搜索/读信/拒信判断由 Claude Code 会话用 Gmail 连接器现场完成、不写进代码。用户在两个关键分叉点上都选了更保守的一支：架构上选"Claude Code 手动/`/loop`跑"而不是给 `app.py` 单独接一套 Gmail API 凭证做后台全自动；确认方式上选"先列候选给我确认、我点头再批量改"而不是全自动直接改库。详见上面「邮件拒信自动识别（本地落库半段，2026-08-23）」。上一版：2026-08-23（新增「同步 LinkedIn "How You Fit" 匹配推荐搜索」：每日自动抓取流程顺便同步登录态下 LinkedIn 基于用户档案算出的"你可能符合条件"搜索结果页，主动覆盖了 2026-08-18「不做个性化推荐流自动化抓取」的决策（配套数量上限+间隔节流缓释账号风险），也是项目第一次引入真正的 LLM 工具调用循环——`linkedin_list_scan.py` 确定性扫描优先，只有一无所获时才升级给 `linkedin_how_you_fit._scan_with_agent()` 接管，让 LLM 自主观察页面候选元素决定下一步点哪/滚哪，而不是继续猜一套新的硬编码选择器；候选感知走过一次"关键词预筛=换个形式又猜一遍"的弯路，改成按 DOM landmark 结构性提取并加了两道安全网（排除职位链接候选、检测页面被导航离开）。详见上面「同步 LinkedIn "How You Fit" 匹配推荐搜索」，技术取舍详见 [tech-solution.md](tech-solution.md#关键决策)。上一版：2026-08-23（评估 agent 是否符合"上下文管理/规划编排/推理+工具调用/输出校验"四层架构图，评估结论是 tool-calling 循环和 LLM 规划层对这个确定性流水线项目没必要，但发现"输出校验层"有一处真实缺口：`evals/run_analyzer_eval.py` 里写好的结构校验（字段齐全、分数范围、`is_gap` 类型）只在离线评测里跑，没接入 `analyzer.py`/`pipeline.py` 的生产调用链，LLM 返回缺字段/越界分数会被兜底悄悄糊过去直接写库。把校验挪成 `analyzer.validate_analysis_result()`，`analyze_job()` 内部直接调用、不合规就抛错走已有的错误处理路径，eval 脚本删掉了自己重复维护的一份，详见上面「匹配分析结果结构校验接入生产路径」。上一版：2026-08-22（修复相似职位误分组：AlphaLife Sciences 的「Technical Product Manager (CN)」和「Sr. Product Manager (CN)」被误判成相似职位，根因是「(CN)」地区标记在其它有区分度的词都被停用词表滤掉后，成了唯一的"共同词"，Jaccard 相似度刚好压线；把 `"cn"` 加进 `_TITLE_STOPWORDS` 修复，全量数据对比确认只影响这一对、没有引入新的误判，详见上面「相似职位分组误把「(CN)」地区标记当成区分职位的关键词」。上一版：新增「每周转化率」：首页"今日待办"右侧新卡片，一行式"最近一周"投递/面试/offer/拒绝/婉拒汇总 + 自投递开始的自然周趋势柱状图 + 规则式建议（不接LLM），后续反馈又加了可收起的「投递分析」标题、把汇总行合并成一句话、数字字体从等宽改回跟 `.funnel b` 一致的正文加粗。起因是用户怀疑投递转化率偏低，先用真实数据核实是样本太新（21条里大部分投递不到3天）；最初设计的5维度拆分表用真实数据做界面预览后被判断"没有太大价值"，改成这版精简的，第二版预览发现按 `first_seen` 兜底缺失时间戳会推算出一个虚假的更早周份，改成没时间戳的记录并入已知最早的真实投递周，详见上面「每周转化率」。上一版：职位详情页头部再加一个「已收藏」按钮——从"待审核"列表点进详情页原来没法直接标记已收藏，得先返回列表；跟「忽略」共用同一套"先执行、给撤销、自动跳到列表原顺序下一条"的逻辑，重构成 `setStatusFromDetailPage(status)`。上一版：同一天早些时候新增了「重点关注」星标按钮，跟列表页卡片共用同一个开关和接口，星标相关的图标/请求逻辑顺带从 `static/app.js` 挪到 `static/common.js` 共用。两条都详见上面「职位详情页新增「重点关注」「已收藏」按钮」。上一版：2026-08-22（新增「同步 LinkedIn 已收藏/已投递职位列表」：复用 Easy Apply 的登录态开一次浏览器扫 `jobs-tracker` 列表页、收集职位链接后转手交给已有的「添加链接」入口批量入库；"已投递"列表同步进来的职位会自动标记投递状态，"已收藏"当天先做、用户马上追加了"已投递"的同样需求，两者合并成一套按 stage 参数区分的通用实现（`linkedin_saved.py` 当天改名成了 `linkedin_tracker.py`），见上面「同步 LinkedIn「已收藏」/「已投递」职位列表」。开发过程中顺带发现并修复了一个测试套件的既有 bug——好几个测试文件把追踪表路径留空，被 `pipeline.py` 解析成真实的 `~/Downloads/JD匹配追踪表.xlsx`，跟当时正在跑的真实 `app.py` 撞车写坏了用户的真实追踪表文件，已改成全部指向隔离的临时文件，详见上面「修复测试套件曾经写坏用户真实追踪表文件的事故」。上一版：2026-08-22（修复「已收藏」筛选下职位被折叠成"1 个相似职位"空壳分组、看起来像消失了的 bug——`groupJobsForRender()` 原来没检查筛选后同组还剩几条，见上面「修复「已收藏」筛选下职位被折叠成空壳相似分组的 bug」。上一版：2026-08-21（新增打分器（analyzer.py）LLM 输出质量回归套件 `evals/`：跟用户核实"打分器已被证伪"这个旧结论后发现主要是重复入库误判，真实问题收窄成"职级错配"一类，做了一套真实调用 LLM（不 mock、不接入 `tests/run_all.py`）的规则化回归检查，详见上面「打分器（analyzer.py）LLM 输出质量回归套件」。上一版：三处小改动：① 职位详情页「忽略」后不再停在原地，直接跳到列表原顺序里的下一条（用 sessionStorage 跨页传列表顺序 + 撤销信息，纯前端改动，见上面「职位详情页"忽略"后自动跳到列表原顺序的下一条」）；② 每日待办新增「面试中」职位的每日提醒，点文字跳去面试准备页、只有勾选框才算今天处理掉，复用已有的 `checklistRowHtml()`/`dismissChecklistItemToday()`（见上面「每日待办新增"面试中职位"提醒」）；③ 修复自定义待办条目点文字会连带勾掉删除的 bug——这块手写 HTML 没走 `checklistRowHtml()`，漏了 2026-08-18 那次修过的 `event.preventDefault()`（见上面「修复自定义待办条目点文字会连带勾掉的 bug」）。上一版：2026-08-20（P0 批次收尾：投递状态自动化，职位卡片新增「我投了」一键按钮，Easy Apply 完成故意不自动置状态（讨论后确认那一刻不代表已提交），改成引导用户提交后自己点。上一版：新增讨论：职位详情页"面试准备"内容整合进首页"面试题库"入口的方案，倾向"轻整合"不合并数据层，冻结期内先记录不实现，见上面「面试准备模块」小节新增条目。上一版：修了一个待办条目通用 bug：点文字跳转会顺带把 checkbox 也勾上，等于自动把这条待办点掉了，`checklistRowHtml()` 加 `event.preventDefault()` 堵住；顺带把"体检已给出建议"这条提醒的语义从"按天重置"改成"留到真的生成过优化版、或者用户主动忽略"，不再因为跨天就凭空消失。「已收藏」卡片小字精简回「已收藏待投递」。上一版：简历体检从同步阻塞请求改成后台线程跑，跳去别的页面不再等于打断体检；首页每日待办清单新增"今天完成"提醒（体检已给出建议，去优化简历），跟已有的"还没体检过"提醒互斥、按天自动重置。上一版：每日待办清单删掉"M 条已收藏、还没生成材料"这条——数字算法有 bug（没按已收藏过滤），用户也确认不需要这条提醒；顶部统计卡片改成单选组，点「已收藏」再点「已投递」会取消前一个而不是同时选中两张。上一版：「已收藏」统计卡片数字改成只算"收藏且还没投递"，不再把已投递/面试中的也混进这个数字；卡片小字同步改成「已收藏待投递，已经投出的不在这个下面展示」，小字换行不再单行截断。上一版：2026-08-18（LinkedIn/Indeed 跨源重复只留 LinkedIn：库里 4 对标题完全相同的严格重复（都在 Amazon 名下）合并成 1 行，优先保留投递进度更靠前的那行，打平时留 LinkedIn；`scraper.py` 的入库判重也顺手加了"以后抓到 LinkedIn 版本就升级已有 Indeed 行"的逻辑，不用等下次迁移。删除前对 `jobs.db` 做了快照备份。顺手在匹配分析 prompt 里加了一条"职级错配"降分规则：JD 只要求 4-6 年经验、title 不带 Senior 等资深字样时，`content_match` 要体现"职责范围可能低于候选人现在资历"这层错配，只影响之后新跑的分析，不补跑历史职位。上一版：新增「疑似重复」提示角标：`annotate_similar_groups()` 补上 `duplicate_of_applied` 字段，同组里有一条已投递/面试中时，其余近似岗位（包括标星的）都会打上提示角标，不再因为"标星不参与折叠"而被完全遮住；顺手改了三处小问题：「智能搜索」改名「智能抓取」、顶栏「面试题库」「我的简历」去图标去下划线、「今日抓取」漏斗行的中文字体 bug（不该强制等宽字体）。上一版：首页顶部再调一版视觉：统计卡片从两排改回一排、整体缩小，960px 以下退回两排；「我的简历」「面试题库」挪进顶栏并去边框；「添加链接」合并进「智能搜索」做成下拉菜单；副标题文案改成「智能领航，过滤噪音，保留信号」。上一版：每日待办卡片改成默认展开、可折叠：标题栏加 chevron + 数量角标，点击展开/收起，折叠动画用 `grid-template-rows` 技巧兼容任意条数不裁内容；每次刷新页面都默认展开，折叠状态不持久化，看完自己收起。上一版：顶部统计卡片在「已收藏」后新增「已投递」「面试中」两张卡，跟底部「投递状态」筛选chip共享同一个状态、点哪个都双向同步高亮，栅格从4列改3列。上一版：使用者反馈「高分被忽略的职位」跨源重复、且想按公司定向抓取，两件事一起处理，都完成：① 核实后发现真正原因不是跨源重复（Indeed↔LinkedIn 实际只有 1 对），是同源近似岗位堆积 + 公司名后缀不统一（"Amazon.com"/"Amazon" 被当成两家），修了 `normalize()`/`normalize_company()`/`make_dedupe_key()` 三处 + 历史数据迁移，并加了 `annotate_similar_groups()` 在首页折叠展示相似职位；②「重点关注公司」定向搜索：设置页填公司名，自动解析成 LinkedIn 数字 ID 并对每家跑一次 `linkedin_company_ids` 定向搜索，不受关键词排名/条数上限影响。详见上面「求职决策闭环」P1 批次的两条新完成条目。上一版：完成「求职决策闭环」P0 批次里的两条：每日任务清单（首页新增可勾选清单，把待审核/待生成材料/待投递/超7天待跟进/简历体检提醒收进一处）、忽略原因收集→偏好档案（忽略时选填原因，攒够阈值自动总结成偏好档案并注入匹配分析 prompt）。P0 批次第三条「投递状态自动化」仍未做，只顺带补了它需要的 `applied_at` 时间戳。上一版：新增「手动粘贴 LinkedIn 职位链接入库」：首页「添加链接」按钮 → 弹窗批量贴链接 → 访客页抓取、抓不到时用已登录浏览器兜底 → 入库待审核并自动排队分析，对应 P1 批次里的「LinkedIn 推荐职位手动导入」痛点①，该条已勾掉）

2026-08-18（完成一次资深产品总监视角的全面 review，含联网竞品调研，整体覆盖重写 [product-review.md](product-review.md) 为 2026-08-18 快照。核心结论：降噪这一层已做成，产品该从「降噪」升级到「决策」；评分器已被真实数据证伪——37 条 ≥0.7 里人工忽略 11 条、只投 5 条，星标比 AI 分数更能预测投递；7 条一手痛点收敛到三个根因。本节新增「求职决策闭环」P0/P1/P2 批次与「待决策：自动投递红线」，并把 UI/UX P1+P2、P3 模拟面试、界面双语切换明确冻结。未改 mission.md，等自动投递红线决策后再动）

2026-08-17（继续对比真实首页和设计稿 v2：把 11 处组件的字号/字重（含徽标的字体族/大小写/圆角形状）对齐设计稿数值，红/橙/绿语义色本轮不动。上一版：对比真实首页和设计稿 v2 后再调整：智能搜索/AI分析/面试题库三个高频入口从顶栏挪到导语行右侧，新增"今日抓取"漏斗统计，删掉失效的 `loadBankSummary()` 死代码。再上一版：首页视觉清理完成：emoji 图标统一成 SVG、颜色收敛到群青+黑白两色、面试题库入口挪回顶栏、修复 4 个次级页面顶栏 `.brand-icon` 断裂 bug；产品改名"职达 Landed"。再上一版：首页视觉改版已完成并接入真实代码：`static/style.css` 设计 token 整体从浅紫渐变换成冷调纸白/近黑+群青单色，状态卡片重新蒙皮成大卡片，面试题库入口从顶栏小按钮提成独立横幅，重点关注置顶+全部按匹配度排序取代原来"最新在前"。两版独立 mockup `design_preview.html`/`design_preview_v2.html` 仅作为过程产物留在根目录）

2026-08-17（职位详情页从弹窗改成独立页面 `/jobs/<id>`，加上 AI 对话 + 备注；新增职位标签；定制简历/Cover Letter 从匹配分析里拆成按需生成，单条按钮 + 批量按钮；标记忽略会中断当前正在分析的这一条但不再停掉整个批次。另外「我的简历」模块 + 首屏收敛已完成：简历改成上传、AI 体检 + 一键生成优化版、四个入口的 need_resume 引导、改名 Signal、重点关注提到统计卡、默认筛选改「全部」。原"其它"里的**简历准备** backlog 条目随之落地移除）

2026-08-16（面试准备 P1 + P2 已完成，P3 模拟面试待做；题库和面试准备都已改成独立页面，题库新增「讲述过往工作」区块、支持目录导航和逐题折叠，起草拆成 4 次调用；AI 模型可按功能位分别在界面上切换；完成一次 UI/UX 评审并落地 P0 修复——toast 层级、失败误报成功、刷新按钮、筛选进 URL、空状态区分、忽略可撤销、排版与对比度、焦点样式、轮询与防抖）；新增 [product-review.md](product-review.md) 产品评估快照（竞争力/不足/竞品/商业化），本文件顶部加了链接）））））
