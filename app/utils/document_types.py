"""文档类型台账：扩展名集合、类型判定与源码语言映射。

上传受理（kb_service / upload_sniff）、加载器路由（multimodal_document_loader）
与前端格式说明共用本模块，避免多处硬编码漂移。
"""

from __future__ import annotations

from pathlib import Path

PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
LEGACY_DOC_EXTS = {".doc"}
EXCEL_EXTS = {".xlsx"}
LEGACY_EXCEL_EXTS = {".xls"}
MARKDOWN_EXTS = {".md", ".markdown"}
CSV_EXTS = {".csv"}
TEXT_EXTS = {".txt"} | MARKDOWN_EXTS | CSV_EXTS

# 常见代码/配置/标记文件（决定是否走 AST 或代码专用切分）
CODE_EXTS = {
    # Python
    ".py", ".pyi", ".pyw",
    # JS / TS
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    # JVM
    ".java", ".kt", ".kts", ".scala", ".groovy",
    # Go / Rust
    ".go", ".rs",
    # C / C++
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    # C# / Objective-C
    ".cs", ".m", ".mm",
    # 脚本
    ".rb", ".php", ".swift", ".lua", ".pl", ".pm", ".r", ".dart",
    # 前端
    ".vue", ".svelte",
    # 数据 / 查询
    ".sql", ".proto",
    # Shell
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    # 配置 / 标记（无魔数，按文本处理但要作为代码文件受理）
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".jsonc", ".xml", ".html", ".htm", ".css", ".scss", ".less",
    ".gradle", ".tf",
}

# 无扩展名但属于源码/构建文件：小写文件名 -> tree-sitter 语言名
SPECIAL_LANGUAGES = {
    "dockerfile": "dockerfile",
    "makefile": "make",
    "gnumakefile": "make",
    "cmakelists.txt": "cmake",
    "jenkinsfile": "groovy",
    "vagrantfile": "ruby",
    "rakefile": "ruby",
    "gemfile": "ruby",
    "procfile": "ruby",
}

# 扩展名 -> tree-sitter 语言名（tree-sitter-language-pack 的 get_parser 入参）
EXT_LANGUAGE: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".m": "objc", ".mm": "objc",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".lua": "lua",
    ".pl": "perl", ".pm": "perl", ".r": "r", ".dart": "dart",
    ".vue": "vue", ".svelte": "svelte",
    ".sql": "sql", ".proto": "proto",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".json": "json", ".jsonc": "json", ".xml": "xml",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".less": "less",
    ".gradle": "groovy", ".tf": "hcl",
}

# 允许上传的知识库文档扩展名（.doc/.xls 为旧版二进制格式，加载库不支持，显式拒绝）
ALLOWED_UPLOAD_EXTS = (
    PDF_EXTS | DOCX_EXTS | EXCEL_EXTS | TEXT_EXTS | CODE_EXTS
)

SUPPORTED_UPLOAD_HINT = "支持 PDF、Word（.docx）、Excel（.xlsx）、TXT、Markdown、CSV 与常见代码文件"


def file_basename(filename: str) -> str:
    """取文件名（去目录），小写化用于特殊文件名判定。"""
    return Path(str(filename or "").replace("\\", "/")).name.lower()


def file_ext(filename: str) -> str:
    """取小写扩展名；无扩展名返回空串。"""
    return Path(file_basename(filename)).suffix.lower()


def language_for_filename(filename: str) -> str:
    """文件名 -> tree-sitter 语言名；未知返回空串。"""
    name = file_basename(filename)
    if name in SPECIAL_LANGUAGES:
        return SPECIAL_LANGUAGES[name]
    return EXT_LANGUAGE.get(Path(name).suffix.lower(), "")


def doc_kind(filename: str) -> str:
    """文件名 -> 文档种类：pdf/word/excel/markdown/text/csv/code/legacy_doc/legacy_excel/unknown。"""
    name = file_basename(filename)
    if name in SPECIAL_LANGUAGES:
        return "code"
    ext = Path(name).suffix.lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOCX_EXTS:
        return "word"
    if ext in LEGACY_DOC_EXTS:
        return "legacy_doc"
    if ext in EXCEL_EXTS:
        return "excel"
    if ext in LEGACY_EXCEL_EXTS:
        return "legacy_excel"
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in CSV_EXTS:
        return "csv"
    if ext in TEXT_EXTS:
        return "text"
    if ext in CODE_EXTS:
        return "code"
    return "unknown"


def reject_reason(filename: str) -> str | None:
    """明确拒绝的旧格式给出可操作提示；否则 None。"""
    kind = doc_kind(filename)
    if kind == "legacy_doc":
        return "暂不支持旧版 .doc 格式，请另存为 .docx 后上传"
    if kind == "legacy_excel":
        return "暂不支持旧版 .xls 格式，请另存为 .xlsx 后上传"
    return None


def allowed_upload_extension(filename: str) -> bool:
    """是否允许作为知识库文档上传。"""
    return doc_kind(filename) not in ("legacy_doc", "legacy_excel", "unknown")
