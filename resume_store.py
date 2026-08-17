"""用户上传的基础简历的唯一入口：存哪、怎么校验、当前有没有、指纹是什么。

以前简历是"用户在设置页填一个本机绝对路径"，留空还会在 pipeline.py 里三处各自硬回退到
~/Downloads/Cathy_Yang_Resume_EN_AI.docx——换台机器、换个人用就直接分析报错，而且三处
回退早晚会改漏一处。现在统一走这里：用户上传，文件落在项目内的 resumes/，config 里的
base_resume_path 由上传流程回写。

只收 .docx 是刻意的：定制简历（analyzer 的 resume_paragraph_edits）和简历优化版都靠
resume_docx.write_tailored_resume() 按**段落索引**改写原文件、保留原排版，PDF 没有这个
结构，收了也只能降级成"给你一段文字建议"，反而让用户以为功能坏了。
"""
import os
import time
from datetime import datetime

from config import BASE_DIR, load_config, save_config
from resume_docx import read_resume_text

# 模块级变量而不是常量，测试里可以整体指到临时目录（跟 models.DB_PATH 一个套路）
RESUME_DIR = os.path.join(BASE_DIR, "resumes")

MAX_RESUME_BYTES = 10 * 1024 * 1024
ALLOWED_EXT = ".docx"

# 生成的"优化版"固定覆盖同一个文件，不按时间戳堆一堆：用户每次生成都是拿最新那份去投，
# 留着历史版本只会在 resumes/ 里越积越多，而真要回溯的话原件一直都在。
OPTIMIZED_NAME = "optimized.docx"

MISSING_MESSAGE = "还没有上传简历，请先在「我的简历」页上传一份 .docx 简历。"


class ResumeMissingError(RuntimeError):
    """没有可用的基础简历。单独一个类型，好让 app.py 把它翻译成 409 + need_resume，
    而不是跟其它 RuntimeError 一起变成笼统的 500/400。"""

    def __init__(self, message=MISSING_MESSAGE):
        super().__init__(message)


class ResumeUploadError(ValueError):
    """上传的文件不合格（扩展名/大小/解析不出内容），对应 400。"""


def _ensure_dir():
    os.makedirs(RESUME_DIR, exist_ok=True)


def get_base_resume_path():
    """当前基础简历的绝对路径；没配过或文件已经不在了都返回 None。

    仍然读 config["base_resume_path"]（而不是直接扫 resumes/ 目录）：一是老用户手填的
    本机路径不用迁移就继续有效，二是"当前用哪份简历"始终有一个明确的单一事实来源。
    """
    path = (load_config().get("base_resume_path") or "").strip()
    if path and os.path.exists(path):
        return path
    return None


def require_base_resume():
    path = get_base_resume_path()
    if not path:
        raise ResumeMissingError()
    return path


def has_base_resume():
    return get_base_resume_path() is not None


def fingerprint(path=None):
    """简历文件的"版本号"：换了简历，之前那份 AI 体检结果就该标记成过期。
    用 mtime+size 而不是算 md5——简历文件不大但每次开页面都要比一次，没必要读全文。"""
    path = path or get_base_resume_path()
    if not path or not os.path.exists(path):
        return ""
    st = os.stat(path)
    return f"{int(st.st_mtime)}:{st.st_size}"


def get_meta():
    """给前端展示用的一份元信息。没上传时 exists=False，其余字段留空。"""
    path = get_base_resume_path()
    if not path:
        return {"exists": False}

    cfg = load_config()
    meta = cfg.get("base_resume_meta") or {}
    text = read_resume_text(path)
    return {
        "exists": True,
        "filename": meta.get("original_filename") or os.path.basename(path),
        "uploaded_at": meta.get("uploaded_at") or "",
        "size": os.path.getsize(path),
        "paragraph_count": len(text.splitlines()) if text else 0,
        "fingerprint": fingerprint(path),
        "path": path,
    }


def save_uploaded(file_storage):
    """校验并保存上传的简历，回写 config。返回 get_meta()。

    校验顺序不能反：扩展名 → 大小 → 真的能被 python-docx 解析出正文。最后这步是关键——
    把 a.pdf 改名成 a.docx 传上来，前两关都过得了，但等到跑 AI 分析时才炸，那时候用户
    已经不知道问题出在哪了。所以先落到临时文件试解析，解析不出来就删掉、当场报错。
    """
    filename = (getattr(file_storage, "filename", "") or "").strip()
    if not filename:
        raise ResumeUploadError("没有收到文件。")
    if os.path.splitext(filename)[1].lower() != ALLOWED_EXT:
        raise ResumeUploadError(
            "只支持 .docx 格式的简历。定制简历和优化版都要按段落改写原文件、保留你的排版，"
            "PDF 做不到这件事。可以先用 Word 另存为 .docx 再上传。"
        )

    _ensure_dir()
    tmp_path = os.path.join(RESUME_DIR, f".upload_{int(time.time() * 1000)}.docx")
    try:
        file_storage.save(tmp_path)

        size = os.path.getsize(tmp_path)
        if size > MAX_RESUME_BYTES:
            raise ResumeUploadError(f"文件太大（{size / 1024 / 1024:.1f}MB），上限 10MB。")

        try:
            text = read_resume_text(tmp_path)
        except Exception:
            text = None
        if not text:
            raise ResumeUploadError(
                "这个文件解析不出任何文字，可能不是真正的 .docx（比如把 PDF 改了个后缀），"
                "或者内容全在文本框/图片里。请用 Word 打开确认后另存为 .docx 再传。"
            )

        # 磁盘名统一用时间戳：原始文件名常带中文和空格，secure_filename 会把中文清成空串，
        # 直接拿来当文件名反而更容易出问题。真正要展示的原名存进 config 的 base_resume_meta。
        final_path = os.path.join(RESUME_DIR, f"base_{datetime.now():%Y%m%d_%H%M%S}.docx")
        _replace(tmp_path, final_path)
        tmp_path = None

        old_path = get_base_resume_path()
        cfg = load_config()
        cfg["base_resume_path"] = final_path
        cfg["base_resume_meta"] = {
            "original_filename": os.path.basename(filename),
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        save_config(cfg)

        # 换简历时把上一份我们自己存的删掉（只删 resumes/ 里的，用户自己填过的本机路径
        # 不碰——那是他自己的文件，不该被这个应用删）
        if old_path and old_path != final_path and _is_ours(old_path):
            _silent_remove(old_path)

        return get_meta()
    finally:
        if tmp_path:
            _silent_remove(tmp_path)


def delete_base_resume():
    """清掉当前简历。同样只删 resumes/ 里的文件，外部路径只是取消引用。"""
    path = get_base_resume_path()
    cfg = load_config()
    cfg["base_resume_path"] = ""
    cfg["base_resume_meta"] = {"original_filename": "", "uploaded_at": ""}
    save_config(cfg)
    if path and _is_ours(path):
        _silent_remove(path)
    _silent_remove(optimized_path())
    return {"exists": False}


def optimized_path():
    return os.path.join(RESUME_DIR, OPTIMIZED_NAME)


def optimized_download_name():
    """下载时给用户看到的文件名：在他自己的文件名后面挂个"_优化版"，比 optimized.docx 好认。"""
    meta = load_config().get("base_resume_meta") or {}
    stem = os.path.splitext(meta.get("original_filename") or "简历")[0]
    return f"{stem}_优化版.docx"


def _is_ours(path):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(RESUME_DIR)]) == os.path.abspath(RESUME_DIR)
    except ValueError:
        # commonpath 在两个路径不同盘符时会抛 ValueError（Windows），那就肯定不是我们的
        return False


def _replace(src, dst):
    os.replace(src, dst)


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
