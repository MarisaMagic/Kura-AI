"""结构感知分块：Markdown / 纯文本 / 源码 / 表格。

产出带 block_type（text/code/table）与 language 的叶子片段（Leaf）。
- 代码块/表格在阈值内整块保留，超出按结构边界切分；
- 阈值同时约束字符数与 UTF-8 字节数（Milvus text 字段 max_length 按字节计）；
- 源码优先用 tree-sitter AST（懒加载，不可用时逐层降级到 langchain 语言分隔符 / 硬切）。

本模块为纯函数实现（除懒加载解析器外无 IO），便于单测。
"""

from __future__ import annotations

import bisect
import csv
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from app.utils.document_types import doc_kind, language_for_filename

BLOCK_TEXT = "text"
BLOCK_CODE = "code"
BLOCK_TABLE = "table"

DEFAULT_MAX_CHARS = 1200
DEFAULT_MAX_BYTES = 1800
DEFAULT_TABLE_MAX_ROWS = 200
CODE_LANGUAGE_MAX_CHARS = 40

PARENT_L1_MAX_CHARS = 1800
PARENT_L2_MAX_CHARS = 900


@dataclass
class Leaf:
    """一个可独立检索的叶子片段。"""

    text: str
    block_type: str = BLOCK_TEXT
    language: str = ""
    parent_titles: tuple[str, ...] = ()
    heading_path: tuple[str, ...] = ()
    start: int = 0
    end: int = 0
    line_start: int = 0
    line_end: int = 0
    embed_text: str = ""
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------- 阈值与硬切


def _fits(text: str, max_chars: int, max_bytes: int) -> bool:
    return len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes


def cap_text(text: str, max_chars: int) -> str:
    """父块等展示文本的字符上限截断。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…（内容过长已截断）"


def _hard_split(text: str, max_chars: int, max_bytes: int) -> list[str]:
    """按字符与字节双上限硬切；优先在换行处断开。"""
    max_chars = max(16, int(max_chars))
    max_bytes = max(16, int(max_bytes))
    n = len(text)
    out: list[str] = []
    start = 0
    while start < n:
        end = min(n, start + max_chars)
        piece = text[start:end]
        # 收缩到字节上限内（中文等按 3~4 字节/字符估算步长）
        while piece and len(piece.encode("utf-8")) > max_bytes:
            over = len(piece.encode("utf-8")) - max_bytes
            end -= max(1, over // 4 + 1)
            if end <= start:
                end = start + 1
            piece = text[start:end]
        if end <= start:
            end = start + 1
            piece = text[start:end]
        nl = piece.rfind("\n")
        if nl > 0 and nl >= int(len(piece) * 0.5):
            end = start + nl + 1
        out.append(text[start:end])
        start = end
    return [p for p in out if p]


# ----------------------------------------------------------- 表格式渲染与切分


def _escape_cell(value: object) -> str:
    s = "" if value is None else str(value)
    s = s.replace("|", "\\|")
    s = s.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
    return s.strip()


def _table_ncols(rows: list[list[str]]) -> int:
    return max((len(r) for r in rows), default=0)


def _render_table_row(row: list[str], ncols: int) -> str:
    cells = [_escape_cell(v) for v in row][:ncols]
    cells += [""] * (ncols - len(cells))
    return "| " + " | ".join(cells) + " |"


def rows_to_markdown_table(rows: list[list[str]]) -> str:
    """二维单元格 -> Markdown 表格（含表头与分隔行）。"""
    rows = [r for r in rows if r]
    if not rows:
        return ""
    ncols = _table_ncols(rows)
    if ncols == 0:
        return ""
    header = _render_table_row(rows[0], ncols)
    sep = "| " + " | ".join(["---"] * ncols) + " |"
    lines = [header, sep]
    lines += [_render_table_row(r, ncols) for r in rows[1:]]
    return "\n".join(lines)


def split_table_rows(
    rows: list[list[str]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_TABLE_MAX_ROWS,
) -> list[str]:
    """按行切分表格：每块重复表头，不跨块拆行；单行超限时硬切并重复表头。"""
    rows = [r for r in (rows or []) if any(str(c or "").strip() for c in r)]
    if not rows:
        return []
    ncols = _table_ncols(rows)
    header_line = _render_table_row(rows[0], ncols)
    sep_line = "| " + " | ".join(["---"] * ncols) + " |"
    head = header_line + "\n" + sep_line
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(head + "\n" + "\n".join(buf))
            buf.clear()

    for row in rows[1:]:
        line = _render_table_row(row, ncols)
        if buf and (
            len(buf) >= max(1, int(max_rows))
            or not _fits(head + "\n" + "\n".join(buf + [line]), max_chars, max_bytes)
        ):
            flush()
        if _fits(head + "\n" + line, max_chars, max_bytes):
            buf.append(line)
            continue
        # 单行自身超限：硬切该行并逐片重复表头
        flush()
        budget_chars = max(16, max_chars - len(head) - 1)
        budget_bytes = max(16, max_bytes - len(head.encode("utf-8")) - 1)
        for piece in _hard_split(line, budget_chars, budget_bytes):
            out.append(head + "\n" + piece)
    flush()
    return out


def parse_markdown_table(text: str) -> list[list[str]]:
    """解析 Markdown 管道表格为二维单元格（忽略分隔行与空行）。"""
    rows: list[list[str]] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or _TABLE_DELIM_RE.match(s):
            continue
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|") and not s.endswith("\\|"):
            s = s[:-1]
        cells = re.split(r"(?<!\\)\|", s)
        rows.append([c.replace("\\|", "|").strip() for c in cells])
    return rows


# --------------------------------------------------------------- Markdown 扫描

_FENCE_RE = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)$")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
_ATX_RE = re.compile(r"^[ \t]{0,3}(?P<hashes>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")

_LANG_ALIASES = {
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "yml": "yaml",
    "c++": "cpp",
    "cp": "cpp",
    "cs": "csharp",
    "c#": "csharp",
    "kt": "kotlin",
    "golang": "go",
    "dockerfile": "dockerfile",
    "console": "",
    "text": "",
    "plaintext": "",
}


def normalize_code_language(info: str) -> str:
    """归一化代码围栏 info string 的语言名（取首 token）。"""
    token = ""
    for part in re.split(r"[\s,;{}]+", (info or "").strip()):
        if part:
            token = part
            break
    token = token.strip("`~.").lower()
    if not token:
        return ""
    token = _LANG_ALIASES.get(token, token)
    return token[:CODE_LANGUAGE_MAX_CHARS]


def _embed_prefix(filename: str, block_type: str, language: str, heading_path: tuple[str, ...]) -> str:
    if block_type == BLOCK_CODE:
        label = f"[代码 {language}]" if language else "[代码]"
    elif block_type == BLOCK_TABLE:
        label = "[表格]"
    else:
        label = "[文档]"
    parts = [label, filename or ""]
    if heading_path:
        parts.append(" > ".join(str(t) for t in heading_path if t))
    return " ".join(p for p in parts if p) + "\n"


def build_embed_text(
    filename: str,
    text: str,
    *,
    block_type: str = BLOCK_TEXT,
    language: str = "",
    heading_path: tuple[str, ...] = (),
) -> str:
    """给叶子正文拼上检索用上下文前缀（标题路径/语言），仅用于嵌入。"""
    return _embed_prefix(filename, block_type, language, tuple(heading_path)) + text


def _make_leaves(
    text: str,
    *,
    block_type: str,
    language: str,
    filename: str,
    heading_path: tuple[str, ...],
    parent_titles: tuple[str, ...],
    start: int,
    end: int,
    max_chars: int,
    max_bytes: int,
    max_table_rows: int,
) -> list[Leaf]:
    """按 block_type 选择切分策略并产出带上下文的叶子。"""
    prefix = _embed_prefix(filename, block_type, language, heading_path)
    pieces: list[str]
    if block_type == BLOCK_TABLE:
        rows = parse_markdown_table(text)
        pieces = split_table_rows(
            rows, max_chars=max_chars, max_bytes=max_bytes, max_rows=max_table_rows
        )
    elif block_type == BLOCK_CODE:
        pieces = split_code_structural(text, language, max_chars=max_chars, max_bytes=max_bytes)
    else:
        pieces = _split_prose(text, max_chars=max_chars, max_bytes=max_bytes)

    leaves: list[Leaf] = []
    for piece in pieces:
        if not piece.strip():
            continue
        leaves.append(
            Leaf(
                text=piece,
                block_type=block_type,
                language=language,
                parent_titles=parent_titles,
                heading_path=heading_path,
                start=start,
                end=end,
                embed_text=prefix + piece,
            )
        )
    return leaves


def _is_table_start(line: str, next_line: str) -> bool:
    if "|" not in line or not line.strip():
        return False
    return bool(_TABLE_DELIM_RE.match(next_line.strip()))


def segment_markdown(
    text: str,
    filename: str = "",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_table_rows: int = DEFAULT_TABLE_MAX_ROWS,
) -> list[Leaf]:
    """扫描 Markdown：围栏代码 / 管道表格 / ATX 标题 / 段落，产出叶子。"""
    if not text or not text.strip():
        return []

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)

    leaves: list[Leaf] = []
    heading_stack: list[tuple[int, str]] = []
    i = 0
    n = len(lines)

    def current_path() -> tuple[str, ...]:
        return tuple(t for _, t in heading_stack if t)

    while i < n:
        raw_line = lines[i]
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            i += 1
            continue

        # 1) 围栏代码块
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group("fence")
            fchar, flen = fence[0], len(fence)
            info = m.group("info") or ""
            start_off = offsets[i]
            j = i + 1
            body: list[str] = []
            while j < n:
                cand = lines[j].rstrip("\r\n")
                cm = _FENCE_CLOSE_RE.match(cand)
                if cm and cm.group("fence")[0] == fchar and len(cm.group("fence")) >= flen:
                    break
                body.append(lines[j])
                j += 1
            code = "".join(body)
            end_off = offsets[j] + len(lines[j]) if j < n else len(text)
            if code.strip():
                lang = normalize_code_language(info)
                path = current_path()
                leaves.extend(
                    _make_leaves(
                        code,
                        block_type=BLOCK_CODE,
                        language=lang,
                        filename=filename,
                        heading_path=path,
                        parent_titles=_parent_titles(path, filename),
                        start=start_off,
                        end=end_off,
                        max_chars=max_chars,
                        max_bytes=max_bytes,
                        max_table_rows=max_table_rows,
                    )
                )
            i = j + 1 if j < n else n
            continue

        # 2) 管道表格
        if i + 1 < n and _is_table_start(line, lines[i + 1]):
            j = i + 1
            while j + 1 < n:
                nxt = lines[j + 1].rstrip("\r\n")
                if not nxt.strip() or ("|" not in nxt):
                    break
                j += 1
            table_text = "".join(lines[i : j + 1])
            path = current_path()
            leaves.extend(
                _make_leaves(
                    table_text,
                    block_type=BLOCK_TABLE,
                    language="",
                    filename=filename,
                    heading_path=path,
                    parent_titles=_parent_titles(path, filename),
                    start=offsets[i],
                    end=offsets[j] + len(lines[j]),
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                )
            )
            i = j + 1
            continue

        # 3) ATX 标题
        hm = _ATX_RE.match(line)
        if hm:
            level = len(hm.group("hashes"))
            title = hm.group("title").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            i += 1
            continue

        # 4) 段落/列表（连续非空且非结构行）
        j = i
        buf: list[str] = []
        while j < n:
            cur = lines[j]
            s = cur.rstrip("\r\n")
            if not s.strip():
                break
            if _FENCE_RE.match(s) or _ATX_RE.match(s):
                break
            if j + 1 < n and _is_table_start(s, lines[j + 1]):
                break
            buf.append(cur)
            j += 1
        para = "".join(buf).strip()
        if para:
            path = current_path()
            leaves.extend(
                _make_leaves(
                    para,
                    block_type=BLOCK_TEXT,
                    language="",
                    filename=filename,
                    heading_path=path,
                    parent_titles=_parent_titles(path, filename),
                    start=offsets[i],
                    end=offsets[j] if j < n else len(text),
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=max_table_rows,
                )
            )
        i = max(j, i + 1)

    return leaves


def _parent_titles(heading_path: tuple[str, ...], filename: str) -> tuple[str, ...]:
    """叶子归属的父级标题：(L1, L2)；无标题时用文件名兜底。"""
    titles = tuple(t for t in heading_path if t)[:2]
    if not titles:
        return (filename or "文档",)
    return (titles[0],) if len(titles) == 1 else titles


def looks_like_markdown(text: str, *, scan_lines: int = 500) -> bool:
    """纯文本是否具备 Markdown 结构特征（围栏/标题/表格）。"""
    if not text:
        return False
    lines = text.splitlines()
    for idx, line in enumerate(lines[:scan_lines]):
        if _FENCE_RE.match(line) or _ATX_RE.match(line):
            return True
        if _TABLE_DELIM_RE.match(line.strip()):
            return True
    return False


def segment_plain_text(
    text: str,
    filename: str = "",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Leaf]:
    """纯文本：按空行段落聚合，超长再按句子/字符切。"""
    if not text or not text.strip():
        return []
    leaves: list[Leaf] = []
    buf: list[str] = []
    for line in text.splitlines():
        if line.strip():
            buf.append(line)
            continue
        if buf:
            leaves.extend(
                _make_leaves(
                    "\n".join(buf),
                    block_type=BLOCK_TEXT,
                    language="",
                    filename=filename,
                    heading_path=(),
                    parent_titles=(filename or "文档",),
                    start=0,
                    end=0,
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    max_table_rows=1,
                )
            )
            buf = []
    if buf:
        leaves.extend(
            _make_leaves(
                "\n".join(buf),
                block_type=BLOCK_TEXT,
                language="",
                filename=filename,
                heading_path=(),
                parent_titles=(filename or "文档",),
                start=0,
                end=0,
                max_chars=max_chars,
                max_bytes=max_bytes,
                max_table_rows=1,
            )
        )
    return leaves


# ------------------------------------------------------------------- 表格叶子


def make_table_leaves(
    rows: list[list[str]],
    *,
    filename: str,
    title: str = "",
    block_type: str = BLOCK_TABLE,
    language: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_table_rows: int = DEFAULT_TABLE_MAX_ROWS,
    start: int = 0,
    end: int = 0,
) -> list[Leaf]:
    """由二维单元格直接构造表格叶子（Excel/CSV/PDF/Word 共用）。"""
    if not rows:
        return []
    heading_path = (title,) if title else ()
    parent_titles = (filename or "文档",) if not title else (filename or "文档", title)
    prefix = _embed_prefix(filename, block_type, language, heading_path)
    leaves: list[Leaf] = []
    for piece in split_table_rows(
        rows, max_chars=max_chars, max_bytes=max_bytes, max_rows=max_table_rows
    ):
        if not piece.strip():
            continue
        leaves.append(
            Leaf(
                text=piece,
                block_type=block_type,
                language=language,
                parent_titles=parent_titles,
                heading_path=heading_path,
                start=start,
                end=end,
                embed_text=prefix + piece,
            )
        )
    return leaves


# --------------------------------------------------------------------- 源码 AST

try:  # langchain 语言分隔符兜底（未安装时仅走硬切）
    from langchain_text_splitters import Language as _LC_LANGUAGE
    from langchain_text_splitters import RecursiveCharacterTextSplitter as _RecursiveSplitter
except Exception:  # pragma: no cover - 环境缺依赖时的降级
    _LC_LANGUAGE = None  # type: ignore[assignment]
    _RecursiveSplitter = None  # type: ignore[assignment]

_LC_LANG_MAP = {}
if _LC_LANGUAGE is not None:
    _LC_LANG_MAP = {
        "python": _LC_LANGUAGE.PYTHON,
        "javascript": _LC_LANGUAGE.JS,
        "typescript": _LC_LANGUAGE.TS,
        "java": _LC_LANGUAGE.JAVA,
        "go": _LC_LANGUAGE.GO,
        "rust": _LC_LANGUAGE.RUST,
        "ruby": _LC_LANGUAGE.RUBY,
        "php": _LC_LANGUAGE.PHP,
        "cpp": _LC_LANGUAGE.CPP,
        "c": _LC_LANGUAGE.C,
        "csharp": _LC_LANGUAGE.CSHARP,
        "scala": _LC_LANGUAGE.SCALA,
        "swift": _LC_LANGUAGE.SWIFT,
        "kotlin": _LC_LANGUAGE.KOTLIN,
        "lua": _LC_LANGUAGE.LUA,
        "perl": _LC_LANGUAGE.PERL,
        "r": _LC_LANGUAGE.R,
        "powershell": _LC_LANGUAGE.POWERSHELL,
        "html": _LC_LANGUAGE.HTML,
        "markdown": _LC_LANGUAGE.MARKDOWN,
        "proto": _LC_LANGUAGE.PROTO,
    }

CODE_SEPARATORS = [
    "\n\nclass ",
    "\n\nasync def ",
    "\n\ndef ",
    "\n\nfunc ",
    "\n\nfunction ",
    "\n\nexport ",
    "\n\npublic ",
    "\n\nprivate ",
    "\n\n# ",
    "\n\n// ",
    "\n\n",
    "\n    ",
    "\n\t",
    ";\n",
    "{\n",
    "}\n",
    "\n",
    " ",
    "",
]

_PROSE_SEPARATORS = [
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    ".",
    "!",
    "?",
    "，",
    ",",
    "、",
    ";",
    " ",
    "",
]


def _split_prose(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS, max_bytes: int = DEFAULT_MAX_BYTES
) -> list[str]:
    """散文切分：优先句读边界，再做字节上限兜底。"""
    pieces: list[str] = []
    if _RecursiveSplitter is not None:
        try:
            splitter = _RecursiveSplitter(
                separators=_PROSE_SEPARATORS, chunk_size=max_chars, chunk_overlap=0
            )
            pieces = splitter.split_text(text)
        except Exception:
            pieces = []
    if not pieces:
        pieces = _hard_split(text, max_chars, max_bytes)
    out: list[str] = []
    for p in pieces:
        if not p.strip():
            continue
        out.extend([p] if _fits(p, max_chars, max_bytes) else _hard_split(p, max_chars, max_bytes))
    return out or ([text] if text.strip() else [])


_SOURCE_UNIT_TYPES: dict[str, dict[str, set[str]]] = {
    "python": {"class": {"class_definition"}, "func": {"function_definition"}},
    "javascript": {
        "class": {"class_declaration", "class", "class_expression"},
        "func": {
            "function_declaration",
            "function_expression",
            "generator_function_declaration",
            "arrow_function",
            "method_definition",
        },
    },
    "typescript": {
        "class": {"class_declaration", "class", "class_expression", "interface_declaration", "type_alias_declaration", "enum_declaration"},
        "func": {
            "function_declaration",
            "function_expression",
            "generator_function_declaration",
            "arrow_function",
            "method_definition",
        },
    },
    "tsx": {
        "class": {"class_declaration", "interface_declaration", "type_alias_declaration"},
        "func": {"function_declaration", "arrow_function", "method_definition"},
    },
    "java": {
        "class": {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        },
        "func": {"method_declaration", "constructor_declaration"},
    },
    "go": {"class": {"type_declaration"}, "func": {"function_declaration", "method_declaration"}},
    "rust": {
        "class": {"struct_item", "enum_item", "trait_item", "impl_item", "mod_item", "type_item", "union_item"},
        "func": {"function_item"},
    },
    "csharp": {
        "class": {
            "class_declaration",
            "interface_declaration",
            "struct_declaration",
            "enum_declaration",
            "record_declaration",
        },
        "func": {"method_declaration", "constructor_declaration", "local_function_statement"},
    },
    "ruby": {"class": {"class", "module", "singleton_class"}, "func": {"method", "singleton_method"}},
    "php": {
        "class": {
            "class_declaration",
            "interface_declaration",
            "trait_declaration",
            "enum_declaration",
        },
        "func": {"function_definition", "method_declaration"},
    },
    "kotlin": {
        "class": {"class_declaration", "object_declaration", "interface_declaration"},
        "func": {"function_declaration"},
    },
    "swift": {
        "class": {
            "class_declaration",
            "protocol_declaration",
            "extension_declaration",
            "struct_declaration",
            "enum_declaration",
        },
        "func": {"function_declaration", "init_declaration"},
    },
    "scala": {
        "class": {"class_definition", "object_definition", "trait_definition"},
        "func": {"function_definition"},
    },
    "c": {"class": {"struct_specifier", "union_specifier", "enum_specifier", "type_definition"}, "func": {"function_definition"}},
    "cpp": {
        "class": {
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
            "namespace_definition",
        },
        "func": {"function_definition"},
    },
    "lua": {"class": set(), "func": {"function_declaration", "function_definition"}},
}

_CLASS_TOKENS = (
    "class",
    "interface",
    "struct",
    "enum",
    "trait",
    "impl",
    "namespace",
    "module",
    "object_declaration",
    "type_declaration",
    "type_alias",
    "record",
)
_FUNC_TOKENS = ("function", "method", "constructor", "func", "arrow_function", "lambda")
_COMMENT_TYPES = {"comment", "line_comment", "block_comment", "decorator", "attribute_item"}


@dataclass
class _Unit:
    kind: str  # class | func
    start: int
    end: int
    name: str = ""
    parent_name: str = ""  # 方法所属类名


def _get_ts_parser(language: str):
    """懒加载 tree-sitter parser；不可用/不支持时返回 None。"""
    if not language:
        return None
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore
    except Exception:
        return None
    try:
        return _cached_parser(get_parser, language)
    except Exception:
        return None


@lru_cache(maxsize=64)
def _cached_parser(get_parser, language: str):
    return get_parser(language)


def _unit_kind(node, language: str) -> Optional[str]:
    ntype = getattr(node, "type", "") or ""
    if ntype == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None:
            return _unit_kind(inner, language)
    table = _SOURCE_UNIT_TYPES.get(language) or {}
    if ntype in (table.get("class") or set()):
        return "class"
    if ntype in (table.get("func") or set()):
        return "func"
    if not table:
        lowered = ntype.lower()
        if any(tok in lowered for tok in _CLASS_TOKENS):
            return "class"
        if any(tok in lowered for tok in _FUNC_TOKENS):
            return "func"
    return None


def _node_name(node, data: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        try:
            return data[name_node.start_byte : name_node.end_byte].decode("utf-8", "replace").strip()
        except Exception:
            return ""
    return ""


def _is_comment_like(ntype: str) -> bool:
    return ntype in _COMMENT_TYPES or "comment" in (ntype or "")


def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    starts.extend(m.end() for m in re.finditer(b"\n", data))
    return starts


def _byte_to_line(line_starts: list[int], offset: int) -> int:
    return bisect.bisect_right(line_starts, offset)


def _byte_to_char_map(text: str) -> tuple[bytes, list[int]]:
    data = text.encode("utf-8")
    mapping = [0] * (len(data) + 1)
    bi = 0
    ci = 0
    for ch in text:
        blen = len(ch.encode("utf-8"))
        for k in range(blen):
            mapping[bi + k] = ci
        bi += blen
        ci += 1
    if mapping:
        mapping[len(data)] = ci
    return data, mapping


def _extract_units(root, data: bytes, language: str) -> tuple[list[_Unit], list[tuple[int, int]]]:
    """返回 (顶层定义单元, 未被定义覆盖的前导区间)。方法级单元展开到类单元内部。"""
    children = list(root.named_children)
    claimed: set[int] = set()
    units: list[_Unit] = []
    for child in children:
        if child.id in claimed:
            continue
        kind = _unit_kind(child, language)
        if kind is None:
            continue
        start = child.start_byte
        prev = child.prev_named_sibling
        while prev is not None and prev.id not in claimed and _is_comment_like(getattr(prev, "type", "")):
            start = prev.start_byte
            claimed.add(prev.id)
            prev = prev.prev_named_sibling
        claimed.add(child.id)
        name = _node_name(child, data)

        if kind == "class":
            class_name = name or "class"
            body = child.child_by_field_name("body")
            methods: list[_Unit] = []
            if body is not None:
                for m in body.named_children:
                    if _unit_kind(m, language) == "func":
                        m_start = m.start_byte
                        mp = m.prev_named_sibling
                        while mp is not None and _is_comment_like(getattr(mp, "type", "")):
                            m_start = mp.start_byte
                            mp = mp.prev_named_sibling
                        methods.append(
                            _Unit("func", m_start, m.end_byte, _node_name(m, data), parent_name=class_name)
                        )
            header_end = body.start_byte if body is not None else child.end_byte
            if methods:
                units.append(_Unit("class", start, max(header_end, start), class_name))
                units.extend(methods)
            else:
                units.append(_Unit("class", start, child.end_byte, class_name))
        else:
            units.append(_Unit("func", start, child.end_byte, name))

    units.sort(key=lambda u: (u.start, u.end))
    ranges = [(u.start, u.end) for u in units]
    preamble: list[tuple[int, int]] = []
    cursor = 0
    for s, e in ranges:
        if s > cursor:
            preamble.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < len(data):
        preamble.append((cursor, len(data)))
    return units, preamble


def _split_code_by_lang(text: str, language: str, max_chars: int, max_bytes: int) -> list[str]:
    pieces: list[str] = []
    if _RecursiveSplitter is not None:
        try:
            lang = _LC_LANG_MAP.get(language)
            if lang is not None:
                splitter = _RecursiveSplitter.from_language(
                    language=lang, chunk_size=max_chars, chunk_overlap=0
                )
            else:
                splitter = _RecursiveSplitter(
                    separators=CODE_SEPARATORS, chunk_size=max_chars, chunk_overlap=0
                )
            pieces = splitter.split_text(text)
        except Exception:
            pieces = []
    if not pieces:
        pieces = _hard_split(text, max_chars, max_bytes)
    out: list[str] = []
    for p in pieces:
        if not p.strip():
            continue
        out.extend([p] if _fits(p, max_chars, max_bytes) else _hard_split(p, max_chars, max_bytes))
    return out or ([text] if text.strip() else [])


def split_code_structural(
    text: str,
    language: str = "",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    depth: int = 0,
) -> list[str]:
    """长代码切分：AST 子节点边界 → langchain 语言分隔符 → 硬切。"""
    if not text or not text.strip():
        return []
    if _fits(text, max_chars, max_bytes):
        return [text]
    if depth < 2 and language:
        parser = _get_ts_parser(language)
        if parser is not None:
            try:
                data = text.encode("utf-8")
                root = parser.parse(data).root_node
                children = list(root.named_children)
                if len(children) >= 2:
                    groups: list[str] = []
                    cur_start: Optional[int] = None
                    cur_end: Optional[int] = None
                    for ch in children:
                        if cur_start is None:
                            cur_start, cur_end = ch.start_byte, ch.end_byte
                            continue
                        candidate = data[cur_start : ch.end_byte].decode("utf-8", "replace")
                        if _fits(candidate, max_chars, max_bytes):
                            cur_end = ch.end_byte
                        else:
                            groups.append(data[cur_start:cur_end].decode("utf-8", "replace"))
                            cur_start, cur_end = ch.start_byte, ch.end_byte
                    if cur_start is not None:
                        groups.append(data[cur_start:cur_end].decode("utf-8", "replace"))
                    if len(groups) > 1:
                        out: list[str] = []
                        for g in groups:
                            out.extend(
                                [g]
                                if _fits(g, max_chars, max_bytes)
                                else split_code_structural(
                                    g, language, max_chars=max_chars, max_bytes=max_bytes, depth=depth + 1
                                )
                            )
                        if out:
                            return out
            except Exception:
                pass
    return _split_code_by_lang(text, language, max_chars, max_bytes)


def segment_source(
    text: str,
    filename: str = "",
    *,
    language: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Leaf]:
    """源码切分：文件级父标题 + 类/函数叶子；AST 不可用时整体降级。"""
    if not text or not text.strip():
        return []
    lang = language or language_for_filename(filename)
    parent_l1 = filename or "源码"
    leaves: list[Leaf] = []

    parser = _get_ts_parser(lang) if lang else None
    units: list[_Unit] = []
    preamble: list[tuple[int, int]] = []
    data: bytes = b""
    mapping: list[int] = []
    line_starts: list[int] = []

    if parser is not None:
        try:
            data, mapping = _byte_to_char_map(text)
            root = parser.parse(data).root_node
            units, preamble = _extract_units(root, data, lang)
            line_starts = _line_starts(data)
        except Exception:
            units, preamble = [], []

    if not units and not preamble:
        for piece in _split_code_by_lang(text, lang, max_chars, max_bytes):
            leaves.append(
                Leaf(
                    text=piece,
                    block_type=BLOCK_CODE,
                    language=lang,
                    parent_titles=(parent_l1,),
                    heading_path=(parent_l1,),
                    embed_text=_embed_prefix(filename, BLOCK_CODE, lang, ()) + piece,
                )
            )
        return leaves

    def emit(raw: str, start_byte: int, end_byte: int, unit: Optional[_Unit] = None) -> None:
        if unit is not None and unit.kind == "class":
            parent = (parent_l1, unit.name or "class")
            prefix_ctx = (unit.name,) if unit.name else ()
        elif unit is not None and unit.parent_name:
            parent = (parent_l1, unit.parent_name)
            prefix_ctx = (unit.parent_name, unit.name) if unit.name else (unit.parent_name,)
        elif unit is not None and unit.name:
            parent = (parent_l1,)
            prefix_ctx = (unit.name,)
        else:
            parent = (parent_l1,)
            prefix_ctx = ()
        pieces = (
            [raw]
            if _fits(raw, max_chars, max_bytes)
            else split_code_structural(raw, lang, max_chars=max_chars, max_bytes=max_bytes)
        )
        for piece in pieces:
            if not piece.strip():
                continue
            leaves.append(
                Leaf(
                    text=piece,
                    block_type=BLOCK_CODE,
                    language=lang,
                    parent_titles=parent,
                    heading_path=prefix_ctx or (parent_l1,),
                    start=mapping[start_byte] if mapping else 0,
                    end=mapping[end_byte] if mapping else 0,
                    line_start=_byte_to_line(line_starts, start_byte) if line_starts else 0,
                    line_end=_byte_to_line(line_starts, end_byte) if line_starts else 0,
                    embed_text=_embed_prefix(filename, BLOCK_CODE, lang, prefix_ctx) + piece,
                )
            )

    # 前导（imports / 模块 docstring / 常量）：独立叶子，便于「这个文件是做什么的」类问题
    for s, e in preamble:
        raw = data[s:e].decode("utf-8", "replace")
        if len(raw.strip()) < 8 and not any(k in raw for k in ("import", "from ", "package ", "#!")):
            continue
        emit(raw, s, e, unit=None)

    for unit in units:
        raw = data[unit.start : unit.end].decode("utf-8", "replace")
        emit(raw, unit.start, unit.end, unit=unit)

    return leaves


# ------------------------------------------------------------------- 统一入口


def segment_document(
    filename: str,
    text: str,
    *,
    language: str = "",
    force_kind: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_table_rows: int = DEFAULT_TABLE_MAX_ROWS,
    code_enabled: bool = True,
) -> list[Leaf]:
    """按文档种类选择切分器。force_kind 可覆盖扩展名判定。"""
    kind = force_kind or doc_kind(filename)
    if kind == "code":
        if not code_enabled:
            return _plain_leaves(text, filename, max_chars=max_chars, max_bytes=max_bytes)
        return segment_source(text, filename, language=language, max_chars=max_chars, max_bytes=max_bytes)
    if kind == "markdown":
        return segment_markdown(
            text, filename, max_chars=max_chars, max_bytes=max_bytes, max_table_rows=max_table_rows
        )
    if kind in ("csv",):
        return []
    if kind == "text" and looks_like_markdown(text):
        return segment_markdown(
            text, filename, max_chars=max_chars, max_bytes=max_bytes, max_table_rows=max_table_rows
        )
    return segment_plain_text(text, filename, max_chars=max_chars, max_bytes=max_bytes)


def _plain_leaves(text: str, filename: str, *, max_chars: int, max_bytes: int) -> list[Leaf]:
    return segment_plain_text(text, filename, max_chars=max_chars, max_bytes=max_bytes)


# ------------------------------------------------------------------ CSV 解析


def parse_csv_rows(text: str) -> list[list[str]]:
    """解析 CSV/TSV 为二维单元格；分隔符自动探测。"""
    if not text or not text.strip():
        return []
    sample = text[:8192]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        cells = ["" if v is None else str(v) for v in row]
        while cells and cells[-1] == "":
            cells.pop()
        rows.append(cells)
    while rows and not any(rows[-1]):
        rows.pop()
    return rows
