"""从上传的面试准备 PDF 里抽取正文文字。单一职责，跟 resume_docx.py（读 .docx）并列——
不合并进那个模块，因为解析库和文件格式完全不同，混在一起只会让两边的异常处理互相绕。

用 PyMuPDF 而不是 pypdf：实测拿这个项目真实要处理的中文 PDF（Amazon_Interview_Prep.pdf，
中文正文用了内嵌字体）试过 pypdf 的默认解析和 layout 模式，中文全部被解成乱码（CJK 编码
表没解出来，不是偶发个例，两种模式结果一样烂）；换 PyMuPDF 抽同一份文件完全正常。
没有先入为主选更"轻"的库，是真的拿手头这份文件测过才定的。
"""
from pymupdf import open as _open_pdf


def _extract(doc):
    pages = []
    for page in doc:
        text = (page.get_text() or "").strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


def extract_text(path):
    """逐页抽取正文并拼接，页与页之间空一行分隔。抽不出任何文字（比如整份是扫描图片、
    没有文字层）时返回空字符串，调用方据此判断上传是否有效——不在这里抛异常，
    是否算失败由调用方按自己的校验流程决定（同 resume_docx.read_resume_text 的分工）。
    """
    doc = _open_pdf(path)
    try:
        return _extract(doc)
    finally:
        doc.close()


def extract_text_from_bytes(data):
    """同 extract_text，但直接从内存里的文件内容解析，不用先落一份临时文件到磁盘再删——
    面试练习的上传文档不需要保留原始 PDF（见 models.py interview_docs 表上的注释），
    没必要为了解析多绕一次磁盘 I/O。"""
    doc = _open_pdf(stream=data, filetype="pdf")
    try:
        return _extract(doc)
    finally:
        doc.close()
