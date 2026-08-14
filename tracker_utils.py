"""
JD匹配追踪表 - 可复用工具函数

用途：每次分析完一个新JD后，调用 add_entry() 把结果作为新的一行
插入到追踪表的最上方（row 2，紧跟表头下面），已有记录自动下移。

固定保存路径：此电脑/下载/JD匹配追踪表.xlsx
（Windows下的实际路径形如 C:\\Users\\<用户名>\\Downloads\\JD匹配追踪表.xlsx，
Mac下形如 /Users/<用户名>/Downloads/JD匹配追踪表.xlsx，
调用前请确认当前系统下"下载"文件夹的真实路径）

用法示例：
    from tracker_utils import add_entry

    add_entry(
        path="/path/to/JD匹配追踪表.xlsx",
        job_title="Product Lead, AI Neobank App",
        job_url="https://www.linkedin.com/jobs/view/4454342546/",  # 优先用投递/申请链接(apply link)；如果只有职位展示页链接也可以，但场景三(Indeed)必须用get_job_details返回的apply链接
        company="BJAK",
        overall_match=0.65,   # 0~1之间的小数
        job_content_bullets=[
            "统筹AI Neobank App核心产品方向",
            "覆盖保险、支付、储蓄、投资、旅行等多个金融服务模块",
        ],
        requirement_items=[
            ("资深数字产品负责人经验，能带领多产品方向/squad", False),  # False=已匹配
            ("Fintech/保险/支付/消费应用行业经验", True),               # True=未达标->红色
        ],
        skill_matched_bullets=["领导DemandTec四条产品线，多workstream统筹经验"],
        skill_gap_bullets=["缺少金融类消费者产品简化的直接案例"],
        experience_years="12+年，Director级别，满足资深要求",
        industry_bullets=["无fintech/保险/支付直接经验", "核心背景为零售定价SaaS（最大差距点）"],
        salary="JD未公开，需询问",
        team_bullets=["现任Director，管理多条产品线", "JD未说明具体团队规模"],
        location="远程（需常驻中国）",
        status="低于70%阈值，待用户决定是否生成尝试性简历",
        apply_date=None,  # 默认today
        resume_optimization_bullets=None,  # 情况B/未生成简历时不传，留空即可
        resume_path=None,
        cover_letter=None,
    )

    # 情况A（匹配度达标且生成了定制简历+cover letter）示例：
    add_entry(
        ...,
        resume_optimization_bullets=[
            "Professional Summary第一条改写为突出AI Agent 0-to-1经验",
            "DemandTec经历拆分强调跨职能leadership与交付质量",
        ],
        resume_path="C:\\Users\\Cathy\\Downloads\\Cathy_Yang_Resume_EN_BJAK.docx",
        cover_letter="Dear Hiring Team,\n\nI'm writing to express my interest in...\n\nBest regards,\nCathy Yang",
    )
"""

import os
from datetime import date
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

FONT_NAME = "Arial"

HEADERS = [
    "职位名称", "公司", "总体匹配度", "投递日期",
    "职位内容", "任职要求",
    "技能匹配度-匹配的", "技能匹配度-未达标的",
    "相关经验年限", "行业背景", "薪资范围",
    "团队规模/汇报线", "地理位置/远程要求", "状态/下一步",
    "简历优化内容", "简历存储路径", "Cover Letter",
]

COL_WIDTHS = {
    "A": 26, "B": 12, "C": 12, "D": 12, "E": 34, "F": 42,
    "G": 30, "H": 28, "I": 18, "J": 26, "K": 16, "L": 26, "M": 18, "N": 26,
    "O": 34, "P": 30, "Q": 50,
}

# 重要：颜色必须用8位ARGB且alpha=FF（不透明）。
# 只传6位RGB（如"000000"）会被openpyxl解读为alpha=00，也就是完全透明，
# 文字会实际上不可见——这是个真实踩过的坑，务必保留FF前缀。
BLACK_RUN = InlineFont(rFont=FONT_NAME, sz=10, color="FF000000")
RED_RUN = InlineFont(rFont=FONT_NAME, sz=10, color="FFFF0000", b=True)

THIN = Side(style="thin", color="B7B7B7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)


def _bullets_plain(lines):
    """普通要点列表（黑色，无高亮需求时用）-> 多行字符串"""
    if isinstance(lines, str):
        return lines
    return "\n".join(f"• {l}" for l in lines)


def _bullets_rich(items):
    """items: list of (text, is_gap:bool) -> CellRichText
    每个要点独立一行；未达标(is_gap=True)的要点标红加粗。
    换行符直接拼在每个TextBlock文本末尾（而非单独的换行run）——
    如果用独立的"\\n"-only run，Excel按XML默认空白处理规则会把它丢弃，
    导致所有要点被合并显示成一行。这是实测踩过的坑，务必保持这个写法。
    """
    blocks = []
    n = len(items)
    for i, (text, is_gap) in enumerate(items):
        run = RED_RUN if is_gap else BLACK_RUN
        suffix = "\n" if i < n - 1 else ""
        blocks.append(TextBlock(run, f"• {text}{suffix}"))
    return CellRichText(*blocks)


def _new_workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = "JD匹配追踪表"
    for col_idx, h in enumerate(HEADERS, start=1):
        c = ws.cell(row=1, column=col_idx, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    for col, w in COL_WIDTHS.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30
    return wb


def _ensure_workbook(path):
    if os.path.exists(path):
        return load_workbook(path, rich_text=True)
    return _new_workbook()


def add_entry(
    path,
    job_title,
    job_url,
    company,
    overall_match,          # float 0~1
    job_content_bullets,    # list[str]
    requirement_items,      # list[(str, bool)]  bool=True表示未达标->红色
    skill_matched_bullets,  # list[str]
    skill_gap_bullets,      # list[str]
    experience_years,       # str（通常单条，不用bullet）
    industry_bullets,       # list[str] 或 str
    salary,                 # str
    team_bullets,           # list[str] 或 str
    location,               # str，如"远程（需常驻中国）"或具体城市名
    status,                 # str
    apply_date=None,
    resume_optimization_bullets=None,  # list[str]，简历改了哪些地方；情况B/未生成简历时传None
    resume_path=None,                  # str，定制简历的完整保存路径；未生成时传None
    cover_letter=None,                 # str，cover letter全文（含换行），未生成时传None
):
    wb = _ensure_workbook(path)
    ws = wb["JD匹配追踪表"] if "JD匹配追踪表" in wb.sheetnames else wb.active

    # 在表头下方插入一行新记录，已有记录自动下移（最新公司永远在最上面）
    ws.insert_rows(2)
    r = 2

    values = {
        "职位名称": job_title,
        "公司": company,
        "总体匹配度": overall_match,
        "投递日期": apply_date or date.today(),
        "职位内容": _bullets_plain(job_content_bullets),
        "任职要求": _bullets_rich(requirement_items),
        "技能匹配度-匹配的": _bullets_plain(skill_matched_bullets),
        "技能匹配度-未达标的": _bullets_plain(skill_gap_bullets),
        "相关经验年限": experience_years,
        "行业背景": _bullets_plain(industry_bullets),
        "薪资范围": salary,
        "团队规模/汇报线": _bullets_plain(team_bullets),
        "地理位置/远程要求": location,
        "状态/下一步": status,
        "简历优化内容": _bullets_plain(resume_optimization_bullets) if resume_optimization_bullets else "未生成定制简历",
        "简历存储路径": resume_path or "—",
        "Cover Letter": cover_letter or "未生成",
    }

    max_lines = 1
    for col_idx, h in enumerate(HEADERS, start=1):
        if h == "职位名称":
            cell = ws.cell(row=r, column=col_idx, value=job_title)
            if job_url:
                cell.hyperlink = job_url
                cell.font = Font(name=FONT_NAME, size=10, color="0563C1", underline="single")
            else:
                cell.font = Font(name=FONT_NAME, size=10)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            cell.border = BORDER
            continue

        val = values[h]
        cell = ws.cell(row=r, column=col_idx, value=val)
        cell.border = BORDER

        if h == "任职要求":
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            max_lines = max(max_lines, len(requirement_items))
            continue

        cell.font = Font(name=FONT_NAME, size=10)
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

        if h == "总体匹配度":
            cell.number_format = "0%"
            cell.alignment = Alignment(horizontal="center", vertical="center")
        elif h == "投递日期":
            cell.number_format = "yyyy-mm-dd"
            cell.alignment = Alignment(horizontal="center", vertical="center")
        elif isinstance(val, str) and "\n" in val:
            max_lines = max(max_lines, val.count("\n") + 1)

    ws.row_dimensions[r].height = min(500, max(60, 22 * max_lines))

    wb.save(path)
    return path


def list_existing_jobs(path):
    """返回追踪表里已有的 (公司, 职位名称) 元组列表，用于新搜索到的职位去重判断——
    避免同一个职位被反复分析、重复插入表格。如果文件不存在，返回空列表。"""
    if not os.path.exists(path):
        return []
    wb = load_workbook(path, rich_text=True)
    ws = wb["JD匹配追踪表"] if "JD匹配追踪表" in wb.sheetnames else wb.active
    company_col = HEADERS.index("公司") + 1
    title_col = HEADERS.index("职位名称") + 1
    result = []
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        company = row[company_col - 1].value
        title = row[title_col - 1].value
        if company and title:
            result.append((str(company).strip(), str(title).strip()))
    return result


def update_match_score(path, company, job_title, new_score, status_note=None):
    """更新已存在记录的总体匹配度（比如评分方法调整后需要重新计算历史记录时用），
    按公司+职位名称精确匹配对应行，只改C列（总体匹配度）和可选的N列（状态/下一步），
    不改动其他列、不新增行、不改变行的位置。"""
    wb = load_workbook(path, rich_text=True)
    ws = wb["JD匹配追踪表"] if "JD匹配追踪表" in wb.sheetnames else wb.active

    company_col = HEADERS.index("公司") + 1
    title_col = HEADERS.index("职位名称") + 1
    match_col = HEADERS.index("总体匹配度") + 1
    status_col = HEADERS.index("状态/下一步") + 1

    updated = False
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        if (
            row[company_col - 1].value == company
            and row[title_col - 1].value == job_title
        ):
            ws.cell(row=row[0].row, column=match_col, value=new_score).number_format = "0%"
            if status_note:
                ws.cell(row=row[0].row, column=status_col, value=status_note)
            updated = True
            break

    if not updated:
        raise ValueError(f"未找到匹配记录：公司={company}，职位名称={job_title}")

    wb.save(path)
    return path
