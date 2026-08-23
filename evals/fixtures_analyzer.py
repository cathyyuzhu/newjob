"""analyzer.py 打分器的评估 fixture 数据。

不测"这条职位到底该打几分"（没有客观答案），只测 `analyzer.PROMPT_TEMPLATE`
里写死的几条具体规则有没有被 LLM 真的遵守：

- 职级错配拖累（重点，用户核实过的真实问题）：JD 明显是初级岗、要求年限远低于
  简历实际经验时，content_match 该往下调，overall_match 不该轻易越过 70% 投递线。
  用「同一份简历 + 技能条目相同、只改 title/年限档位」的成对 JD 做对比，而不是
  断言绝对分数（prompt 里这条规则本来就没给出具体数值）。
- 硬性门槛拖累：JD 明确标注 mandatory 且简历未覆盖时，prompt 给了具体数字
  （单条 ≤0.5，两条以上"约0.3"），可以直接断言绝对上限。
- 偏好档案软信号：命中排斥点该往下调，但不能一票否决——同样用成对 JD（带/不带
  偏好档案）比较，并给一个"不能被压到接近0"的下限。
- 公司归属分类：对知名公司有客观正确答案，直接断言。
- 公司简介不编造：对虚构公司应如实说"未找到"，弱检查（soft=True），不影响
  整体 exit code，只在报告里提示人工看一眼。

resume_text 按 `resume_docx.read_resume_text()` 的真实格式构造（每个非空段落
一行，前面带 `[序号]`），保证 fixture 跟生产环境喂给 LLM 的格式一致。
"""

RESUME_SENIOR_TEXT = (
    "[0] 张三\n"
    "[1] 产品经理 | 8年工作经验\n"
    "[2] 工作经历\n"
    "[3] ABC科技有限公司 高级产品经理 2019-至今\n"
    "[4] 主导企业级SaaS平台从0到1建设，服务超过200家企业客户，年营收贡献增长35%\n"
    "[5] 负责产品路线图规划、跨部门协作（研发/销售/客户成功团队20+人）、季度OKR制定\n"
    "[6] 主导3次产品重大改版，通过A/B测试等用户增长实验将核心功能留存率提升18%\n"
    "[7] XYZ互联网公司 产品经理 2016-2019\n"
    "[8] 负责toB企业协作工具的需求分析、竞品分析、原型设计\n"
    "[9] 与研发团队紧密协作完成20+个功能迭代，累计服务用户超过50万\n"
    "[10] 技能\n"
    "[11] 精通数据分析（SQL、Excel、Tableau）、用户研究方法、敏捷开发流程（Scrum）\n"
    "[12] 英语流利，可独立完成英文商务沟通与文档撰写\n"
    "[13] 教育背景\n"
    "[14] 某大学 工商管理硕士 2014-2016\n"
    "[15] 某大学 计算机科学学士 2010-2014"
)

FIXTURES = [
    # ---- 职级错配拖累（重点）：1 个资深锚点 + 2 个不同幅度的初级错配对比 ----
    {
        "id": "seniority_senior_anchor",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "纵横云科技",
        "title": "高级产品经理",
        "jd_text": (
            "岗位职责：\n"
            "1. 主导企业级 toB SaaS 产品路线图规划，制定并跟进季度 OKR\n"
            "2. 跨部门协作（研发/销售/客户成功团队），推动核心产品从0到1建设与迭代\n"
            "3. 通过数据分析（A/B测试等）驱动用户增长与核心功能留存率提升\n\n"
            "任职要求：\n"
            "1. 5年以上产品经理相关经验，有 toB SaaS 产品经验优先\n"
            "2. 精通数据分析工具（SQL/Tableau等），有较强的跨部门协作与项目推动能力\n"
            "3. 英语流利，能与海外团队顺畅沟通"
        ),
        "rule": "锚点（非独立断言）：技能高度匹配的资深岗位，供下面两条初级错配 fixture 做对比基准",
    },
    {
        "id": "seniority_junior_mismatch",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "晴岚科技",
        "title": "产品经理（初级）",
        "jd_text": (
            "岗位职责：\n"
            "1. 协助高级产品经理完成日常产品需求文档的撰写与维护\n"
            "2. 收集、整理用户反馈，配合完成产品原型设计\n"
            "3. 跟进研发团队的需求排期与验收，做好基础的项目沟通\n\n"
            "任职要求：\n"
            "1. 1-2年产品经理相关经验，应届生有相关实习经历也可考虑\n"
            "2. 良好的沟通能力和文档撰写能力\n"
            "3. 了解基本的产品设计工具（Axure/Figma等）"
        ),
        "pair_with": "seniority_senior_anchor",
        "compare_metric": "content_match",
        "min_margin_below": 0.10,
        "max_overall_match": 0.70,
        "rule": "职级错配拖累（轻度）：1-2年经验的协助型初级岗，content_match 应明显低于资深锚点，overall_match 不应轻易越过 70% 投递线",
    },
    {
        "id": "seniority_entry_level_mismatch",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "启明星科技",
        "title": "产品助理（应届）",
        "jd_text": (
            "岗位职责：\n"
            "1. 在导师带领下学习产品需求文档撰写、原型设计基础技能\n"
            "2. 协助收集用户反馈、整理竞品资料\n"
            "3. 参与团队日常会议记录与执行跟进\n\n"
            "任职要求：\n"
            "1. 应届毕业生或0-1年相关经验，欢迎实习经历丰富的应届生投递\n"
            "2. 有较强的学习能力和责任心\n"
            "3. 沟通表达能力良好"
        ),
        "pair_with": "seniority_senior_anchor",
        "compare_metric": "content_match",
        "min_margin_below": 0.20,
        "max_overall_match": 0.60,
        "rule": "职级错配拖累（重度）：应届/0-1年经验的岗位跟8年经验的候选人差距更极端，content_match 应比锚点低得更多、overall_match 门槛更低",
    },

    # ---- 硬性门槛拖累：prompt 给了具体数值，可以直接断言绝对上限 ----
    {
        "id": "hard_gap_single_mandatory",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "领航国际咨询",
        "title": "高级产品经理（企业数字化）",
        "jd_text": (
            "岗位职责：\n"
            "1. 负责企业数字化转型咨询项目的产品方案设计与落地跟进\n"
            "2. 与客户高层对接，输出产品路线图与实施计划\n\n"
            "任职要求：\n"
            "1. 必须持有 PMP（Project Management Professional）认证，这是强制要求，无认证简历不予考虑\n"
            "2. 5年以上产品经理相关经验\n"
            "3. 熟悉数据分析工具，具备较强的项目管理与跨部门协作能力"
        ),
        "max_cognitive_match": 0.5,
        "rule": "硬性门槛拖累（1条未覆盖的强制性要求：简历没有PMP认证）：cognitive_match 不应超过 0.5",
    },
    {
        "id": "hard_gap_double_mandatory",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "樱花国际科技",
        "title": "高级产品经理（日本市场）",
        "jd_text": (
            "岗位职责：\n"
            "1. 负责面向日本市场的产品规划与需求管理\n"
            "2. 与日本总部团队高频对接，参与产品方向决策\n\n"
            "任职要求：\n"
            "1. 必须持有 PMP 认证（强制要求，无认证不予考虑）\n"
            "2. 日语达到母语水平或 JLPT N1，需支持与日本团队全日语日常沟通（强制要求，硬性门槛）\n"
            "3. 5年以上产品经理经验"
        ),
        "max_cognitive_match": 0.4,
        "rule": "硬性门槛拖累（2条以上未覆盖的强制性要求：无PMP认证 + 不会日语）：cognitive_match 应进一步下调，不应超过 0.4",
    },

    # ---- 偏好档案软信号：成对对比 + 下限（不能被一票否决） ----
    {
        "id": "pref_profile_none",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "速跑创业科技",
        "title": "产品经理",
        "jd_text": (
            "岗位职责：\n"
            "1. 负责核心产品的需求分析、原型设计与版本迭代\n"
            "2. 配合团队保持业务快速推进的工作节奏，弘扬\"狼性文化\"，鼓励996/大小周投入\n\n"
            "任职要求：\n"
            "1. 3年以上产品经理经验\n"
            "2. 具备较强的数据分析能力和跨部门协作经验\n"
            "3. 认同高强度、快节奏的创业公司工作氛围"
        ),
        "preference_profile_text": None,
        "rule": "锚点（非独立断言）：不带偏好档案时的基准分数，供下面带档案的版本对比",
    },
    {
        "id": "pref_profile_repel",
        "kind": "analyze",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "速跑创业科技",
        "title": "产品经理",
        "jd_text": (
            "岗位职责：\n"
            "1. 负责核心产品的需求分析、原型设计与版本迭代\n"
            "2. 配合团队保持业务快速推进的工作节奏，弘扬\"狼性文化\"，鼓励996/大小周投入\n\n"
            "任职要求：\n"
            "1. 3年以上产品经理经验\n"
            "2. 具备较强的数据分析能力和跨部门协作经验\n"
            "3. 认同高强度、快节奏的创业公司工作氛围"
        ),
        "preference_profile_text": (
            "用户历史上标记忽略职位时反复给出的原因：不喜欢强调\"狼性文化\"、"
            "鼓励996/大小周的创业公司氛围，多次因为这个原因忽略同类职位，这是一个反复出现的排斥点。"
        ),
        "pair_with": "pref_profile_none",
        "compare_metric": "content_match",
        "min_margin_below": 0.0,
        "min_value_floor": 0.25,
        "rule": "偏好档案软信号：命中排斥点时 content_match 不应高于不带档案的版本，但不能被一票否决压到接近0（下限 0.25）",
    },

    # ---- 公司归属分类：有客观正确答案 ----
    {
        "id": "company_origin_known",
        "kind": "classify_companies",
        "companies": {
            "微软（中国）有限公司": "foreign",
            "字节跳动": "domestic",
            "沃尔玛": "foreign",
            "华为技术有限公司": "domestic",
            "亚马逊": "foreign",
            "腾讯科技（深圳）有限公司": "domestic",
        },
        "rule": "公司归属分类：知名公司的 foreign/domestic 判断应跟常识一致",
    },

    # ---- 公司简介不编造：弱检查，只提示不硬失败 ----
    {
        "id": "company_overview_honesty",
        "kind": "overview_honesty",
        "resume_text": RESUME_SENIOR_TEXT,
        "company": "赛博坦星际动力（上海）有限公司",
        "title": "产品经理",
        "jd_text": "负责产品规划与需求管理，3年以上产品经理经验，具备数据分析能力。",
        "expect_phrases": ["未找到", "没有找到", "无法确认", "没有可靠", "未收录", "不了解", "没有相关信息", "无法找到"],
        "rule": "公司简介不编造：对虚构公司应如实说明信息不足，而不是编出一段介绍",
        "soft": True,
    },
]

FIXTURES_BY_ID = {f["id"]: f for f in FIXTURES}
