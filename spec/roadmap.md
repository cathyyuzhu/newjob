# 项目路线图 (Roadmap)

追踪 `newjob` 项目的功能列表。维护规则见 `CLAUDE.md`：每次讨论新功能都要同步更新本文件。

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

## 计划中 / 讨论中

### AI agent 化探索（讨论于 2026-08-18）
现状分析：项目里的 LLM 调用基本都是单轮"prompt 进、JSON/文本出"，编排逻辑（搜索→分析→生成简历→投递）由代码写死串联，不是模型自主决策的循环+工具调用，严格来说属于"LLM-powered 自动化流水线"而非"AI agent 项目"。讨论了哪些环节适合改成真正的 agent（模型自主决定下一步动作、可调用工具、可多步试探）：
- 适合：`company_origin`/`company_overview` 判断——现在纯靠模型参数化记忆猜，拿不准就填 unknown；可以让模型在不确定时主动调用网页搜索工具核实，是最贴近"agent"定义的候选，且判断错代价可控（只是标签不准）
- 适合：JD 重新获取失败后的恢复——现在是固定的"关键词+城市重搜一次，找不到就失败"，可以让模型面对失败结果自己尝试不同恢复策略
- 适合（小范围新功能）：投递跟进管理——目前完全没有的一环，可以做一个定期巡检 agent，扫 `application_status`/日期，对久未跟进的职位主动给建议，属于"自主巡检+生成建议"，不涉及实际投递动作
- 明确不适合：Easy Apply 筛选问题回答——mission.md 已经写死"没配置的不猜、停下交给人工"，这是刻意的正确决策，不应该为了"更像agent"而改动，事实性字段（签证/薪资）填错直接影响真实投递，不能让模型现场编
- 明确不适合：匹配度打分——单轮判断即可，没有"要不要采取下一步行动"的决策需求，套 agent 循环是过度设计
以上都还只是讨论，没有开始实现。

### 面试准备模块（讨论于 2026-08-16）
现有流水线到"投递 + 状态跟踪"就结束了，对方约面试之后又回到全手工准备。分三期补上这一段，每期独立可用。P1、P2 已完成（见上面"已完成"部分），剩余：
- [ ] **P3 模拟面试**：AI 扮演面试官多轮提问、用户打字作答，结束后给评价报告（维度评分 + 优势/改进 + 逐题改写参考）；支持"针对某个职位"和"通用"两种模式、中/英文两种语言。会话状态需要持久化（支持刷新页面恢复 + 轮询），`llm.chat()` 的多轮 `messages` 支持已在 P1 就位

### 其它
- [ ] **简历准备**（backlog，讨论于 2026-08-16）：用户提出但明确本期不做，先记着
- kpi中体现哪些是linkedin的哪些是indeed
- [ ] 右上角"原文 / 中文"语言切换按钮，支持界面双语切换（讨论于 2026-08-15，尚未实现）
- [ ] `llm_provider` / `deepseek_model` 目前只能编辑 `config.json`，还没做到设置页界面上（讨论于 2026-08-15，尚未实现）

---
最后更新：2026-08-18（新增"AI agent 化探索"讨论）
