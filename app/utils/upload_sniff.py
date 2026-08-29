"""上传文件魔数与扩展名一致性校验。"""

from __future__ import annotations

from pathlib import Path

_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_GIF87 = b"GIF87a"
_GIF89 = b"GIF89a"
_PDF = b"%PDF"
_ZIP = b"PK\x03\x04"
_ZIP_EMPTY = b"PK\x05\x06"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_TEXT_EXT = {".txt", ".md", ".csv"}
_PDF_EXT = {".pdf"}
_OOXML_EXT = {".docx", ".xlsx"}
_OLE_EXT = {".doc", ".xls"}


def _is_webp(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def _is_jpeg(raw: bytes) -> bool:
    return raw.startswith(_JPEG)


def _is_png(raw: bytes) -> bool:
    return raw.startswith(_PNG)


def _is_gif(raw: bytes) -> bool:
    return raw.startswith(_GIF87) or raw.startswith(_GIF89)


def _is_pdf(raw: bytes) -> bool:
    return raw.lstrip()[:4] == _PDF or raw.startswith(_PDF)


def _is_zip(raw: bytes) -> bool:
    return raw.startswith(_ZIP) or raw.startswith(_ZIP_EMPTY)


def _is_ole(raw: bytes) -> bool:
    return raw.startswith(_OLE)


def assert_upload_magic(filename: str, raw: bytes) -> None:
    """
    扩展名与文件头必须匹配；文本类不强制魔数。
    不匹配时抛 ValueError。
    """
    ext = Path(filename or "").suffix.lower()
    if not raw:
        raise ValueError("空文件")
    if ext in _TEXT_EXT:
        return
    if ext in {".jpg", ".jpeg"}:
        if not _is_jpeg(raw):
            raise ValueError("文件内容与扩展名不符（需要 JPEG）")
        return
    if ext == ".png":
        if not _is_png(raw):
            raise ValueError("文件内容与扩展名不符（需要 PNG）")
        return
    if ext == ".gif":
        if not _is_gif(raw):
            raise ValueError("文件内容与扩展名不符（需要 GIF）")
        return
    if ext == ".webp":
        if not _is_webp(raw):
            raise ValueError("文件内容与扩展名不符（需要 WEBP）")
        return
    if ext in _PDF_EXT:
        if not _is_pdf(raw):
            raise ValueError("文件内容与扩展名不符（需要 PDF）")
        return
    if ext in _OOXML_EXT:
        if not _is_zip(raw):
            raise ValueError("文件内容与扩展名不符（需要 Office Open XML）")
        return
    if ext in _OLE_EXT:
        if not _is_ole(raw):
            raise ValueError("文件内容与扩展名不符（需要 OLE 复合文档）")
        return
    raise ValueError(f"不支持的文件类型：{ext or '（无扩展名）'}")
