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

## 计划中 / 讨论中

> 优先级说明（2026-08-18 产品 review 后确立）：使用者处于**已离职、求职紧迫**的时期，本节按"这件事能不能在两周内增加真实面试机会"排序。下面「求职决策闭环」是 P0/P1，其余批次（UI/UX P1+P2、P3 模拟面试、界面双语切换）**明确冻结**，等求职告一段落再动。完整论证见 [product-review.md](product-review.md)（2026-08-18 快照）。

### 求职决策闭环（讨论于 2026-08-18）
来自使用者提出的 7 条一手痛点。review 把它们归因到三个根因：**评分器没有记忆**（痛点③⑦）、**没有工作流状态机**（痛点④⑥）、**抓取层**（痛点①②）。数据佐证：37 条 ≥0.7 的职位里被人工忽略 11 条、只投了 5 条；6 条已投递中 5 条是星标——星标比 AI 分数更能预测投递。

**P0 批次（本周）**
- [ ] **每日任务清单**（痛点④）：首页顶部按库状态自动生成可勾选清单（今日抓取 / N 条待审核 / M 条待生成材料 / K 条待投递 / J 条投了超 7 天该跟进），支持自定义条目。顺带把零使用率的备注/简历体检/标签带到用户面前
- [ ] **投递状态自动化**（痛点⑥）：Easy Apply 走完后自动置 `applied` 并记录投递时间；列表页加一键「我投了」；投递时间用于上面的跟进提醒。这是效果闭环的入口
- [ ] **忽略原因收集 → 偏好档案**（痛点③⑦的地基）：忽略时弹一行原因（预设标签 + 自由文本）存库；累计到阈值后一次 LLM 调用总结成「偏好档案」，注入 `analyzer.py` 的 prompt。可用现有 11 条"高分被忽略"补录冷启动。review 判断这是整个项目**唯一有结构性差异化**的能力——竞品的反馈学习全在雇主端，to-C 侧因拿不到足量单用户信号而做不了，本地单用户工具反而做得到

**P1 批次（两周内）**
- [ ] **每日/每周复盘报告**（痛点⑦）：今天审核 N 条 / 投递 M 条 / 生成材料 K 份 + 偏好总结 + 下一步综合建议。数据源是上面两条 P0
- [x] **LinkedIn 推荐职位手动导入**（痛点①）：**不做**个性化推荐流的抓取（需登录态高频请求，封号风险直接命中求职主通道）；改做「粘贴 URL / 批量粘贴」导入通道，走完整分析链路 —— 2026-08-18 完成，见上面「手动粘贴 LinkedIn 职位链接入库」
- [ ] **跨源去重加固**（痛点②）：`make_dedupe_key()` 增加归一化——剥离 `Senior/Sr./资深`、括号后缀 `(Shanghai)`、公司后缀 `Inc./Ltd./有限公司`、全半角统一。最近一次运行 98/281 判重，真实重复率更高
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
- [ ] **P3 模拟面试**：AI 扮演面试官多轮提问、用户打字作答，结束后给评价报告（维度评分 + 优势/改进 + 逐题改写参考）；支持"针对某个职位"和"通用"两种模式、中/英文两种语言。会话状态需要持久化（支持刷新页面恢复 + 轮询），`llm.chat()` 的多轮 `messages` 支持已在 P1 就位

### 其它
- [ ] kpi 中体现哪些是 linkedin 的哪些是 indeed（当前 LinkedIn 50 / Indeed 35）
- [ ] 右上角"原文 / 中文"语言切换按钮，支持界面双语切换（讨论于 2026-08-15，尚未实现；**2026-08-18 起冻结**）

---
最后更新：2026-08-18（新增「手动粘贴 LinkedIn 职位链接入库」：首页「添加链接」按钮 → 弹窗批量贴链接 → 访客页抓取、抓不到时用已登录浏览器兜底 → 入库待审核并自动排队分析，对应 P1 批次里的「LinkedIn 推荐职位手动导入」痛点①，该条已勾掉）

2026-08-18（完成一次资深产品总监视角的全面 review，含联网竞品调研，整体覆盖重写 [product-review.md](product-review.md) 为 2026-08-18 快照。核心结论：降噪这一层已做成，产品该从「降噪」升级到「决策」；评分器已被真实数据证伪——37 条 ≥0.7 里人工忽略 11 条、只投 5 条，星标比 AI 分数更能预测投递；7 条一手痛点收敛到三个根因。本节新增「求职决策闭环」P0/P1/P2 批次与「待决策：自动投递红线」，并把 UI/UX P1+P2、P3 模拟面试、界面双语切换明确冻结。未改 mission.md，等自动投递红线决策后再动）

2026-08-17（继续对比真实首页和设计稿 v2：把 11 处组件的字号/字重（含徽标的字体族/大小写/圆角形状）对齐设计稿数值，红/橙/绿语义色本轮不动。上一版：对比真实首页和设计稿 v2 后再调整：智能搜索/AI分析/面试题库三个高频入口从顶栏挪到导语行右侧，新增"今日抓取"漏斗统计，删掉失效的 `loadBankSummary()` 死代码。再上一版：首页视觉清理完成：emoji 图标统一成 SVG、颜色收敛到群青+黑白两色、面试题库入口挪回顶栏、修复 4 个次级页面顶栏 `.brand-icon` 断裂 bug；产品改名"职达 Landed"。再上一版：首页视觉改版已完成并接入真实代码：`static/style.css` 设计 token 整体从浅紫渐变换成冷调纸白/近黑+群青单色，状态卡片重新蒙皮成大卡片，面试题库入口从顶栏小按钮提成独立横幅，重点关注置顶+全部按匹配度排序取代原来"最新在前"。两版独立 mockup `design_preview.html`/`design_preview_v2.html` 仅作为过程产物留在根目录）

2026-08-17（职位详情页从弹窗改成独立页面 `/jobs/<id>`，加上 AI 对话 + 备注；新增职位标签；定制简历/Cover Letter 从匹配分析里拆成按需生成，单条按钮 + 批量按钮；标记忽略会中断当前正在分析的这一条但不再停掉整个批次。另外「我的简历」模块 + 首屏收敛已完成：简历改成上传、AI 体检 + 一键生成优化版、四个入口的 need_resume 引导、改名 Signal、重点关注提到统计卡、默认筛选改「全部」。原"其它"里的**简历准备** backlog 条目随之落地移除）

2026-08-16（面试准备 P1 + P2 已完成，P3 模拟面试待做；题库和面试准备都已改成独立页面，题库新增「讲述过往工作」区块、支持目录导航和逐题折叠，起草拆成 4 次调用；AI 模型可按功能位分别在界面上切换；完成一次 UI/UX 评审并落地 P0 修复——toast 层级、失败误报成功、刷新按钮、筛选进 URL、空状态区分、忽略可撤销、排版与对比度、焦点样式、轮询与防抖）；新增 [product-review.md](product-review.md) 产品评估快照（竞争力/不足/竞品/商业化），本文件顶部加了链接）
