# architecture.md — 分层架构

记录 `newjob` 代码从上到下的分层（UI → API → 编排 → 数据/LLM），谁能调谁、每层实际有哪些能力。跟 [tech-solution.md](tech-solution.md) 的关系：那份文档讲"为什么这么选、为什么这么设计"，这份文档讲"现在代码实际分成几层、每层是什么"，前者是决策记录，后者是结构快照——两者都要跟着代码走，模块加入/挪层/废弃时都要回来更新这份文档，不需要用户明确说才做。

从上到下五层，每层只跟相邻层直接打交道，不跨层调用。

```
┌─────────────────────────────────────────────┐
│ 1. UI 层                                      │
│    templates/*.html + static/*.js/css        │
│    渲染页面、处理交互、fetch 调 API             │
└───────────────────┬───────────────────────────┘
                     │ fetch (JSON)
┌───────────────────▼───────────────────────────┐
│ 2. 控制器 / API 层                             │
│    app.py（组装根）+ routes_*.py（按业务域分）  │
│    路由分发、请求解析、调编排层、序列化响应       │
└───────────────────┬───────────────────────────┘
                     │ 函数调用
┌───────────────────▼───────────────────────────┐
│ 3. 业务流程编排层                              │
│    pipeline.py                                │
│    覆盖分析/材料生成/面试准备等几乎所有业务功能，  │
│    每次调用都固定「读第4层 → 调第5层 → 写回第4层」│
└─────────┬───────────────────────┬─────────────┘
          │                       │
┌─────────▼─────────────┐   ┌─────────▼─────────────┐
│ 4. 数据与调度层          │   │ 5. LLM 功能层           │
│ 零 LLM 依赖             │   │ 只依赖 llm.py           │
│ ・抓取：scraper.py /     │   │                        │
│   job_link.py           │   │                        │
│ ・落库：models.py /      │   │                        │
│   tracker_utils.py      │   │                        │
│ ・调度：scheduler.py /   │   │                        │
│   job_state.py          │   │                        │
│ ・LinkedIn自动化模块群    │   │                        │
│（linkedin_*.py/easy_apply）│  │                        │
│ 完整清单见下方第4层表格    │   │                        │
└─────────┬──────────────┘   └────────────────────────┘
          │
┌─────────▼──────────┐
│ 存储介质              │
│ jobs.db (SQLite)     │
│ JD匹配追踪表.xlsx      │
└─────────────────────┘
```

`pipeline.py`（第3层）跟第4、5层的关系是"常规，每次调用都固定跨两层"。但第4层里还藏着一个特例，单独画出来看：

```
linkedin_how_you_fit.py（第4层，LinkedIn 自动化模块之一）
│
├─ 日常路径：确定性扫描
│  调 linkedin_list_scan.py 硬编码滚动/解析页面
│  → 不是 agent，就是普通脚本，全程留在第4层
│
└─ 兜底路径：仅当扫描失败 / 结果明显偏少时触发
   调 llm.py 的 chat_tool_step()（全项目唯一的工具调用循环）
   → ★这一段才是 agent★：LLM 自己观察页面上有哪些可交互
     元素、自己决定点哪个/滚哪个，而不是继续猜硬编码选择器
   → 这一条路径才真正触碰第5层，日常路径完全用不到
```

`linkedin_how_you_fit.py` 这一个文件里其实是两条路径：日常那条只是确定性脚本，跟 agent 没关系；只有兜底那条才是真正意义上的 agent（LLM 自主决策+工具调用），而且只在扫描失败或结果偏少时才会被触发。之前的图把整个文件画成一个框、笼统标"agent 兜底"，容易让人以为整个文件都是 agent——实际上 agent 只是文件里的一小段兜底逻辑。跟 `pipeline.py` 那种"常规、每次都走"第4/5层的关系完全不是一回事，权重上也不该跟 `pipeline.py` 平起平坐。细节见第3节末尾的特例说明。

---

## 1. UI 层

**组成**：`templates/*.html`、`static/app.js`、`static/common.js`、`static/style.css`

**能力**
- 渲染页面骨架（职位列表、详情弹窗、设置页、面试准备页等）
- 前端状态管理（筛选条件、排序、深色模式，纯内存/localStorage，不落库）
- 用原生 `fetch` 调 routes_*.py 暴露的 JSON API（对前端而言仍然是同一个 Flask 服务、同一套 URL，感知不到后端按业务域拆了文件），拿到数据后自己拼 DOM
- 对外部抓取来的文本（职位标题/公司名）做 HTML 转义防 XSS

**不做的事**：不直接碰数据库、不知道 LLM 是 Claude 还是 DeepSeek。

---

## 2. 控制器 / API 层

**组成**：`app.py`（组装根：建 Flask app、注册各 blueprint、启停时的一次性后台任务，本身不定义任何 `@app.route`）+ 按业务域拆分的 `routes_*.py`（2026-08-28 从 app.py 拆出，原因见下）

```
app.py（组装根，只做 register_blueprint × 7 + 启停时的一次性后台任务）
  │
  ├─ routes_core.py          首页 + 全局配置
  ├─ routes_search.py        职位入库与批量分析
  ├─ routes_jobs.py ─────┐   职位列表/详情、单条操作
  ├─ routes_job_actions.py   材料生成 / Easy Apply / 重新获取JD
  ├─ routes_interview.py ◄──┘ 面试准备/题库/语音练习
  │      ↑ 唯一一处跨文件调用：routes_jobs.py 的 update_application_status()
  │        用模块级引用调 routes_interview._maybe_start_interview_prep()
  ├─ routes_resume.py        我的简历模块
  └─ routes_misc.py          运行记录/清单/通知/追踪表只读
```

七个 `routes_*.py` 之间原则上是互不依赖的兄弟文件（都只单向依赖第3层 `pipeline.py`），上图那条 `routes_jobs.py → routes_interview.py` 是目前唯一的例外，跟第4层 `linkedin_how_you_fit.py` 偶尔借用第5层是同一种"记录下来、别当成常态"的特例处理。

| 文件 | 业务域 | 路由数 |
|---|---|---|
| `routes_core.py` | 首页 + 全局配置（`/api/config`、`/api/models`） | 4 |
| `routes_search.py` | 职位入库与批量分析：搜索、贴链接、LinkedIn 各类列表同步、批量AI分析、公司国籍分类 | 11 |
| `routes_jobs.py` | 职位列表/详情、状态/标签/忽略原因/偏好档案、单条AI分析、AI对话、备注 | 17 |
| `routes_job_actions.py` | 分析完成后对单条职位的动作：材料生成、Easy Apply、重新获取JD | 6 |
| `routes_interview.py` | 面试三个子模块：单职位面试准备、通用题库、语音练习 | 20 |
| `routes_resume.py` | 「我的简历」模块：上传/下载/体检/优化版/定制简历列表 | 10 |
| `routes_misc.py` | 运行记录、每日任务清单、通知、追踪表只读接口、LLM 调用流水统计（`/api/llm/stats`）、职位收集运行流水统计（`/api/collect/stats`，2026-08-29）、历史数据回填 | 11 |

共 79 个路由（+ Flask 自动加的 1 个静态文件路由），其中 6 个 `render_template` 返回页面骨架，其余是 JSON API。`app.py` 还多做一件事：启动时 `llm.set_recorder(...)` 把第4层的 `insert_llm_call` 注册给第5层的 `llm.py`（理由见第5层说明）。`web_helpers.py` 放跨这些文件共用的小工具：`need_resume_response`（还没上传简历时的统一响应）、`usage_notification_message`（把第5层 `llm.py` 算出的单次操作用量拼进后台任务完成通知的文案，2026-09-08）。

**能力**
- 请求参数校验、错误码封装（400/404/500）
- 把请求转发给编排层（pipeline.py）对应函数，或直接读数据与调度层数据做简单查询
- `threaded=True`，避免单条 LLM 分析卡住 1~2 分钟时整个服务器无响应
- 唯一的例外：直接 import `llm.py` 的 `MODELS`/`LLM_TASKS`，给设置页展示可选模型清单——这个不算业务逻辑，只是读一份静态注册表

**不做的事**：不写业务逻辑（怎么判断相关性、怎么打分），那些都在下面两层。

**为什么按业务域拆、而不是继续放一个文件（2026-08-28）**：`app.py` 长到 1808 行、77 个路由后，找一个路由要先滚很久、改一处不清楚影响面有多大——这是层内没有再切一刀导致的，不是分层方式本身的问题（见 [tech-solution.md](tech-solution.md) 的评估）。拆分严格是物理搬家：路由路径、函数体、注释一字不改，只是挪文件+按 Flask Blueprint 注册；跨文件调用时注意两点——(1) 一个 blueprint 需要调用另一个 blueprint 里定义的函数（目前只有 `routes_jobs.py` 调 `routes_interview.py` 的 `_maybe_start_interview_prep`）时用模块级引用（`import routes_interview` 再 `routes_interview.func(...)`），不用 `from routes_interview import func`——后者会在导入时把函数对象绑死在本模块命名空间里，测试套件对这类内部函数的 monkeypatch（`routes_interview._maybe_start_interview_prep = fake`）会因此打不到调用点；(2) 测试文件里原来 `import app as flask_app` 后直接 `flask_app.X = fake` 的 monkeypatch，如果 `X` 是被拆到某个 routes_*.py 里的函数，要相应改成 `import routes_xxx` 后 patch `routes_xxx.X`，这次拆分同步改了 `tests/test_linkedin_company.py`、`tests/test_linkedin_how_you_fit_sync.py`、`tests/test_notifications.py`、`tests/test_prep.py` 这四个文件里的 patch 目标。

---

## 3. 业务流程编排层

**组成**：`pipeline.py`

**能力**（按业务场景分组，每组内部都是"读数据与调度层数据 → 调LLM层处理 → 写回数据与调度层"的模式）

| 场景 | 代表函数 |
|---|---|
| 职位分析 | `analyze_and_record`、`analyze_pending_jobs`、`queue_pending_jobs`、`classify_company_origins` |
| JD 补抓 | `refetch_jd`、`refetch_missing_jd_jobs` |
| 材料生成 | `generate_materials_for_job`、`generate_materials_batch` |
| 面试准备 | `generate_interview_prep`、`generate_bank_draft`、`chat_bank_answer` |
| 语音练习 | `save_uploaded_interview_doc`、`generate_practice_set_for_doc`、`score_practice_answer` |
| 职位问答 | `chat_about_job` |
| 简历体检 | `run_resume_review`、`build_optimized_resume` |

**特点**
- 函数名是"动词+名词"的业务操作（分析/生成/体检/打分），不关心底层是 SQLite 还是 xlsx、Claude 还是 DeepSeek
- 是全项目**主要**同时 import 数据与调度层模块和 LLM 层模块的地方——第4层里的 `linkedin_how_you_fit.py` 偶尔也会触碰第5层，但那是从属于第4层的一个特例（见下方），不是跟 `pipeline.py` 平级的第二个编排器
- 不是正规 workflow 引擎（无 DAG 定义、无持久化重试、无跨进程调度）——就是普通函数按顺序调用 + 裸线程（`threading.Thread(daemon=True)`）跑后台任务，配合 `job_state.py` 维护"排队中/分析中"这类进程内内存状态

### 特例：`linkedin_how_you_fit.py` 偶尔触碰第5层

这个文件本质上属于第4层——它是 LinkedIn 自动化那组模块（`easy_apply.py`/`job_link.py`/`linkedin_list_scan.py`）里的一个，日常工作方式跟它们一样：`linkedin_list_scan.py` 的确定性脚本硬编码扫一遍 "How You Fit" 求职资格匹配搜索结果页，抠出职位链接。

跟同组其它模块不同的是，它多留了一条**兜底逃生舱**：扫不到、或收集到的数量明显偏少（判断是没认出页面的真实交互方式，比如没找到可滚动容器，而不是真的没那么多结果），才会临时升级，调一次第5层 `llm.py` 的 `chat_tool_step()`（全项目唯一的工具调用循环），让模型自己观察页面上有哪些可交互元素、自己决定点哪个/滚哪个，而不是继续猜一套新的硬编码选择器。日常绝大多数情况走确定性路径根本不会碰到 `llm.py`，升级只是偶发兜底，成本接近零；配套 `MAX_HOW_YOU_FIT_SEARCHES`（搜索条数上限）和 `MAX_AGENT_STEPS`（单次最多循环几轮）两层节流。

**跟 `pipeline.py` 的关键区别**：`pipeline.py` 每一次调用都固定要跨第4、5层，这是它存在的意义；`linkedin_how_you_fit.py` 绝大多数时候完全不需要第5层，只在自己那条路走不通时才临时借用一次。前者是常规路径，后者是例外路径，不应该在架构图上被画成同一种东西。

---

## 4. 数据与调度层（零 LLM 依赖）

| 文件 | 能力 |
|---|---|
| `scheduler.py` | APScheduler 定时任务，触发搜索和历史积压补跑；2026-08-29 起加固：`misfire_grace_time`/`coalesce`/`max_instances` 三件套 + 启动时检查距上次成功抓取是否超过一个调度周期，超过就补跑一次（`_maybe_catch_up()`） |
| `scraper.py` | 按关键词 × 城市抓取 Indeed/LinkedIn，去重、标题/地点粗筛后入库 |
| `models.py` | SQLite 数据层（`jobs.db`）：jobs 表、search_runs 表、面试相关表、`llm_calls` 调用流水表（2026-08-29）、`collect_runs` 职位收集运行流水表（2026-08-29） |
| `collect_errors.py` | 职位收集链路错误处理生产级加固的地基（2026-08-29）：`classify(exc, http_status)` 判断 transient/rate_limited/auth/locked/bug（structure/upstream_empty 两类依赖内容判断，由调用方直接指定）；`with_retry()` 分层重试；`start_run_deadline()`/`check_deadline()` 墙钟上限；`is_suspicious_drop()` 空结果健康检查；`save_debug_snapshot()` 故障现场取证。重试计数/deadline 用 ContextVar 透传（跟 `llm.py` 的 `_current_task` 同一手法），不逐层加形参 |
| `tracker_utils.py` | 读写 `JD匹配追踪表.xlsx`（基于 openpyxl），跟 `models.py` 是两套并行的存储封装 |
| `job_state.py` | 进程内内存状态机（排队中/分析中/停止请求），零外部依赖；2026-08-29 起还带一个 LinkedIn 登录态熔断器（`LinkedInAuthRequired` + `record_linkedin_auth_failure/success`），供 tracker/how_you_fit/job_link 共用 |
| `config.py` | 读写 `config.json`，被以上模块共用 |
| `resume_docx.py` | 读简历 docx 成带 `[N]` 索引的纯文本；按段落索引写出定制版（**整段替换**，见 `resume_edits.py` 为什么要核查） |
| `resume_edits.py` | 段落改写建议的确定性核查（2026-08-29）：把模型声称"照抄"的原文回简历里比对。纯函数、零 LLM 依赖，所以在这一层不在第5层；三个消费者共用——`resume_review`、`analyzer`、`pipeline.build_optimized_resume` |
| `resume_store.py` | 简历文件的存取（上传/基础简历/优化版/定制版路径），不解析内容 |
| `job_link.py` | 手动贴 LinkedIn 链接入库：`requests` 抓访客页，抓不到降级到已登录的 Playwright profile |
| `easy_apply.py` | Playwright 驱动真实 Edge 浏览器，自动填 Easy Apply 表单，停在确认页等人工点提交 |
| `linkedin_list_scan.py` | 确定性脚本：滚动/解析 LinkedIn 职位列表页，抠出职位链接 |
| `linkedin_tracker.py` | 同步"已收藏"/"已投递"列表页，复用 `linkedin_list_scan.py` 的扫描机制 |
| `linkedin_how_you_fit.py` | 同步 "How You Fit" 搜索结果页，日常跟上面几个一样是确定性扫描；**唯一的例外**——扫描失败/结果偏少时会偶尔借用第5层 `llm.py` 做一次 agent 兜底导航，见第3节末尾的特例说明 |

即使不配置任何 LLM API key，这一层单独也能完整跑通"抓取 → 去重 → 待审核队列"这条主线。

**审计能力现状**：不均匀，但 2026-08-29 起有了一条统一的调用流水。

- *业务结果的历史版本*：`interview_preps`/`resume_reviews`/`interview_practice_sets` 三张表是 INSERT 新行、不覆盖旧行，留了历史版本（含 `content_json` 原始结果 + `llm_provider`/`llm_model` + 时间戳，失败也记一行）；但匹配分析这条主线不是这样——`jobs` 表直接覆盖更新分析结果（`update_job_analysis`），xlsx 追踪表也是删旧行插新行，重新分析一次就查不到上一次的结果。这一条没变。
- *LLM 调用流水*：新增 `llm_calls` 表，每次真实打到 API 的调用记一行——task / provider / model / 成败 / 错误类型 / 耗时 / input+output token / 估算成本 / 本次 max_tokens / prompt 字符数 / 原始 usage。埋点在第5层 `llm.py`（见下节），写入回调由 `app.py` 注册。能直接回答"这个月花了多少钱""哪个任务最容易失败""哪个模型慢"，`GET /api/llm/stats` 是它的聚合视图，顶栏花费小组件（`static/common.js`）和各处 toast/通知里的单次用量文案是这份数据在网页端的展示（2026-09-08）。
- *仍然刻意不记的*：**prompt 和响应原文**。一次匹配分析的 prompt 是简历全文 + JD 全文（10-20KB），每天几十次调用一年就是几百 MB，而这份数据 99% 的时间没人看；`prompt_chars` 只记长度，足够回答"是不是 prompt 变长导致变贵/被截断"。所以这是"记了 token 和成本、刻意没记原文"，不是完整审计。

---

## 5. LLM 功能层（只依赖 llm.py，互不调用）

```
config.json（第4层）
  │ llm_provider / anthropic_model / deepseek_model / llm_tasks{任务:模型}
  ▼
llm.py（第5层地基，全项目唯一直连 API 的适配器）
  │
  ├─ 模型路由：resolve_task(cfg, task)
  │    按任务位查 llm_tasks，查不到就退到全局默认 resolve(cfg)
  │    ★副作用★ 顺手把 task 写进 _current_task ContextVar，供下面两项复用
  │
  ├─ provider 抽象：chat() / chat_json() / ask() / ask_json()
  │    按 provider 分派到 _call_anthropic() 或 _call_deepseek()
  │    两家接口差异（system 参数位置、max_tokens 是否强制）在这里抹平
  │
  ├─ 参数配置：MODELS 注册表
  │    每个模型的 max_tokens / no_thinking / price_in / price_out /
  │    supports_sampling 硬编码在这里，不开放给 config.json
  │
  ├─ 采样控制：TASK_TEMPERATURE + temperature_for()（2026-08-29 加入）
  │    按功能位配温度，但只对 supports_sampling=True 的模型生效——
  │    claude-sonnet-5 已从 API 移除采样参数，传了 400，而它是默认模型
  │
  ├─ 调用流水埋点：_CallRecord（2026-08-29 加入）
  │    包住 _call_anthropic / _call_deepseek / _call_anthropic_tools
  │    三个收口函数，成功失败都往第4层 llm_calls 表落一行
  │    （task/耗时/token/成本）。写库回调由 app.py 用 set_recorder() 注册，
  │    llm.py 不 import models——保住"地基不依赖项目内模块"
  │    埋点自身 try/except 兜底：观测绝不能成为新的故障源
  │
  ├─ LLMJsonError：解析失败带上模型实际返回的前 500 字符（2026-08-29 加入）
  │    继承 ValueError，上层 except Exception 照旧接得住；
  │    同时把该次调用的流水行从 ok=1 回填成 ok=0
  │
  ├─ chat_tool_step()：工具调用循环
  │    唯一消费者是第4层 linkedin_how_you_fit.py 的 agent 兜底，
  │    不是 interview.py 在用（虽然定义在同一个文件里）
  │
  ├─ ANTI_FABRICATION_NOTE：反幻觉标准文案（2026-08-29 加入）
  │    通用场景（job_chat.py 等）直接引用；interview.py/resume_review.py
  │    里已经调好的、针对具体子任务的专用提示不动，见下方"幻觉防护现状"
  │
  ├─ clamp() / require_dict()：输出校验小工具（2026-08-29 加入）
  │    interview.py 和 resume_review.py 原来各自定义的 _clamp/_clamp01
  │    已改成调这个；analyzer.py 的 validate_analysis_result() 更完整、
  │    有 eval 覆盖，没有强行套进来，见下方"输出校验现状"
  │
  ├─ truncate()：按字符数截断（2026-08-29 加入）
  │    job_chat.py 原来自己发明的 _truncate() 已改成调这个；按对话轮数/
  │    候选数量截断的仍留在各自模块（形状不一样，不强行统一）
  │
  └─ ✕ 没有：prompt 版本化 / 回滚（讨论后确认不需要，见下方说明）

  │ 四个功能模块都只调 chat() / chat_json()，彼此互不调用
  ▼
┌─────────────┬─────────────┬─────────────┬───────────────┐
│ analyzer.py │interview.py │ job_chat.py │resume_review.py│
└─────────────┴─────────────┴─────────────┴───────────────┘
  每个模块仍各自拥有独立的 PROMPT_TEMPLATE（这部分不变——场景本来就该
  各自定义），三步里两步半开始复用 llm.py 的共享工具：
  ① 输入构造（job_chat.py 的字符数截断复用 truncate()；数据裁剪/格式化
     仍是各自的事，analyzer.py 仍没有长度保护，见下方）
  ② 调 llm.py 的 chat() / chat_json()
  ③ 输出校验（interview.py/resume_review.py 复用 clamp()；analyzer.py
     保留自己更完整的 validate_analysis_result()，没有改）
  幻觉防护：job_chat.py 改成引用 ANTI_FABRICATION_NOTE；analyzer.py/
  interview.py/resume_review.py 里已经调好的专用提示原样保留，没有替换
```

四个功能模块都是 `llm.py`的兄弟消费者，彼此之间没有 import 关系——这一点跟第2层 `routes_jobs.py → routes_interview.py` 那种"记录下来的例外"不同，这里是真的没有例外，四个互不调用。唯一容易看错的地方标在图里了：`chat_tool_step()` 这个函数物理定义在 `llm.py`，但它是给第4层 `linkedin_how_you_fit.py` 用的工具调用循环，不属于 `interview.py`——只是恰好都叫"面试/对话"相关的名字容易联想到一起，实际上没有关系。

| 文件 | 能力 |
|---|---|
| `llm.py` | Anthropic / DeepSeek 适配器，全项目唯一直连 LLM API 的模块；内含模型路由 / provider 抽象 / 参数配置 / 反幻觉文案 / 输出校验小工具 / 截断工具 / 单次操作用量汇总（`start_usage_tracking`/`pop_usage_summary`/`usage_text`，2026-09-08），见上图 |
| `analyzer.py` | JD-简历双因子匹配分析、生成定制简历+Cover Letter |
| `interview.py` | 面试准备材料、题库起草、语音练习出题打分 |
| `job_chat.py` | 针对单条职位的自由问答，不落库 |
| `resume_review.py` | 整份简历体检（不针对具体职位） |

下面三段是上图①③和"幻觉防护"这三块各自的现状（2026-08-29 更新：部分收进了 `llm.py`，但不是全盘统一，取舍见各段）。

**输入构造现状**：

- *数据裁剪*：`job_chat.py` 的 `_format_analysis_context()` 只从 `tracker_entry` 里摘 `company_overview`/`skill_gap_bullets`/未达标任职要求几个字段，不整份塞进去；`linkedin_how_you_fit.py` 的 `_describe_page_for_agent()` 会先过滤掉已被确定性脚本识别成职位链接的候选，只把模型真正要决策的部分递进去。这块不同场景该裁哪些字段天然不一样，没有抽共享函数，`analyzer.py` 的核心匹配分析仍然没有这层裁剪，JD/简历全文原样注入。
- *格式化*：都是 `## 标题` 分节的纯文本模板（`str.format()` 填空），不是 JSON schema/XML 这类机器可解析格式；简历文本会带 `[数字]` 段落索引方便模型引用改写位置；agent 侧更明显——DOM 快照不会把原始 CSS 选择器暴露给模型，重新编号成人类可读列表，模型只需要说"点第几项"。这块本来就该各场景自己定，没有调整。
- *上下文长度管理*：`job_chat.py` 的字符数截断已经改成调 `llm.truncate(text, MAX_CONTEXT_CHARS)`（原来自己写的 `_truncate()` 删掉了）；`interview.py` 的 `sanitize_chat_history()` 按对话轮数截断、`linkedin_how_you_fit.py` 按候选数量+总轮数截断，这两种形状跟字符数截断不一样，没有塞进 `truncate()`，留在原地。`analyzer.py`（调用量最大的功能）**仍然没有**这层保护，JD/简历原文无长度上限直接注入——讨论过要不要顺带补上，考虑到这是运行了很久、有 eval 套件覆盖的核心路径，没有在这次顺手改，留作单独决定。

**输出校验现状**：没有独立的 schema 校验库（没用 pydantic/jsonschema）。`interview.py` 原来局部定义的 `_clamp()`、`resume_review.py` 原来的 `_clamp01()`（两份几乎一模一样的分数裁剪代码）已经删掉，改成调 `llm.clamp()`。`analyzer.py` 的 `validate_analysis_result()`**没有改**——它比一个通用 `clamp`/`require_dict` 覆盖得更完整（必填字段清单、`requirement_items` 结构校验、越界直接抛错而不是静默裁剪），而且是 `evals/run_analyzer_eval.py` 明确测过的路径，强行套进通用工具只有回归风险、没有收益。`llm.py` 新增的 `require_dict()` 目前只提供最基础的"顶层是不是 dict"这一层，字段级校验业务差异太大没有强行统一。

2026-08-29 起在"格式校验"之外多了一类**内容核查**，收在第4层的 `resume_edits.py`（零 LLM 依赖，所以放第4层不是第5层）：段落改写建议的 `original` 字段会被拿回简历原文比对，`resume_review.normalize_result` 和 `analyzer.generate_materials` 共用同一套。两者的宽严策略不同但都由 `annotate_edits(drop=...)` 表达——体检有 UI，核查不过的保留下来标记成不可应用；定制简历直接落盘没有人工复核，会损坏内容的直接丢。这是本项目第一处"不只看形状、还回原文核对内容"的校验。

**幻觉防护现状**：`llm.py` 新增了 `ANTI_FABRICATION_NOTE` 共享文案，`job_chat.py` 的 system prompt 已经改成引用它（原来是自己写的一句"如实说不知道，不要编造"）。`analyzer.py`/`interview.py`/`resume_review.py` 里原有的"别编造"提示**没有替换**——这些都是针对具体子任务调好的措辞（比如"不要编造公司近况、融资、财报之类的具体事实"、"绝对不能编造未发生的经历、不能凭空添加数字"），比通用文案更精确，`interview.py` 还有多处、各自对应不同子任务，强行换成一句通用文案是文字倒退，不是改进；`analyzer.py` 的措辞还在 eval 覆盖范围内，改了要重新跑一遍 `run_analyzer_eval.py` 才能确认没有回归，这次没有做。`analyzer.py` 额外的两招具体手法（`company_origin` 拿不准填 `unknown`、硬性门槛拖累 `cognitive_match` 封顶）跟它自己的输出结构绑定，本来就不通用，维持原样。

**2026-08-29 起多了一层"回原文核对"的确定性防护**，这是目前项目防幻觉的主要路线——用确定性代码核查模型的事实性断言，而不是用另一个模型核查。两个落地点：

- `resume_edits.check_edit()`（第4层）：模型声称"这一段原文是 X"，程序拿真实的第 N 段来比（归一化后精确匹配 → 0.90 相似度容忍誊写偏差 → 子串判定）。区分出"只摘抄了半段"这一类特别重要：`resume_docx.write_tailored_resume` 是整段替换，只改一句会把该段其余内容静默删掉。
- `analyzer.verify_mandatory_items()`（第5层）：模型声称"JD 里把这条标成了 required"，程序拿它照抄的 `mandatory_evidence` 回 `jd_text` 里找。核不过就不当强制要求（fail-open）。这条决定了 `cognitive_match` 封顶规则会不会误杀，理由见 [tech-solution.md](tech-solution.md)。

**仍然没有**：独立验证器模型二次核查、置信度分数。这两项是**刻意跳过**的（成本翻倍 / LLM 自报置信度校准差），不是待办，理由记在 [tech-solution.md](tech-solution.md) 的"防幻觉分层：做什么、不做什么"。同样刻意跳过的还有 RAG——本项目所有调用都是"单文档进单文档出"，没有检索问题。
