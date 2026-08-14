"""Read-only helper to dedupe against the existing jd-resume-matcher xlsx tracker
(JD匹配追踪表.xlsx). We never write to that file from this app — the Claude skill
owns writes to it. This just avoids re-surfacing jobs already analyzed there.
"""
import os

from models import normalize


def existing_keys_from_tracker(path):
    """Returns a set of "company::title" dedupe keys already recorded in the
    tracker xlsx (columns A=title, B=company), or an empty set if the path
    is blank / the file doesn't exist / can't be read.
    """
    if not path or not os.path.exists(path):
        return set()
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        keys = set()
        for row in ws.iter_rows(min_row=2, max_col=2, values_only=True):
            title, company = row[0], row[1]
            if not title and not company:
                continue
            keys.add(f"{normalize(company)}::{normalize(title)}")
        wb.close()
        return keys
    except Exception:
        return set()
