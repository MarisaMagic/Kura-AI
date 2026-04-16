"""会话附件 BM25 关键词检索（按块打分并返回扩窗片段）。"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections import Counter

from app.chat.attachment_service import extract_attachment_plaintext

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(s: str) -> list[str]:
    s = (s or "").strip().lower()
    if not s:
        return []
    if _CJK_RE.search(s):
        try:
            import jieba

            return [t for t in jieba.lcut(s) if t and str(t).strip()]
        except ImportError:
            return [ch for ch in s if not ch.isspace()]
    return [t for t in re.split(r"\s+", s) if t]


def _bm25_scores(
    corpus_tokens: list[list[str]],
    query_tokens: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Okapi BM25，与常见实现一致；仅依赖标准库。"""
    n_docs = len(corpus_tokens)
    if n_docs == 0:
        return []
    doc_lens = [len(d) for d in corpus_tokens]
    avgdl = sum(doc_lens) / n_docs
    if avgdl <= 0:
        avgdl = 1.0
    df: dict[str, int] = {}
    for doc in corpus_tokens:
        for w in set(doc):
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log((n_docs - n + 0.5) / (n + 0.5)) for w, n in df.items()}
    scores: list[float] = []
    for idx, doc in enumerate(corpus_tokens):
        dl = doc_lens[idx]
        freq = Counter(doc)
        sc = 0.0
        for w in query_tokens:
            f = freq.get(w, 0)
            if f <= 0:
                continue
            denom = f + k1 * (1.0 - b + b * dl / avgdl)
            sc += idf.get(w, 0.0) * (f * (k1 + 1.0)) / denom
        scores.append(sc)
    return scores


def _chunk_spans(text: str, chunk_size: int, overlap: int) -> list[tuple[int, int]]:
    n = len(text)
    if n == 0:
        return []
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 4)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        spans.append((start, end))
        if end >= n:
            break
        start = max(0, end - overlap)
    return spans


def _page_for_offset(page_starts: tuple[int, ...], pos: int) -> int | None:
    if not page_starts:
        return None
    i = bisect_right(page_starts, pos) - 1
    if i < 0:
        i = 0
    return i + 1


def _expand_snippet(
    text: str,
    core_start: int,
    core_end: int,
    margin: int,
    max_snippet_chars: int,
) -> tuple[str, int, int]:
    n = len(text)
    s = max(0, core_start - margin)
    e = min(n, core_end + margin)
    if e - s <= max_snippet_chars:
        return text[s:e], s, e
    mid = (core_start + core_end) // 2
    half = max_snippet_chars // 2
    s2 = max(0, mid - half)
    e2 = min(n, s2 + max_snippet_chars)
    if e2 - s2 < max_snippet_chars:
        s2 = max(0, e2 - max_snippet_chars)
    return text[s2:e2], s2, e2


def search_attachment_text_bm25(
    attachment_id: str,
    query: str,
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    top_k: int = 5,
    chunk_size: int = 800,
    chunk_overlap: int = 120,
    expand_margin: int = 200,
    max_snippet_chars: int = 800,
    max_index_chars: int = 500_000,
) -> str:
    """
    对单份会话附件全文做分块 BM25 检索，返回带字符范围与 PDF 页码（若有）的扩窗片段。
    """
    q = (query or "").strip()
    if not q:
        return "错误：检索 query 为空。"

    ext, err = extract_attachment_plaintext(
        attachment_id.strip(),
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    if err:
        return err
    assert ext is not None

    text = ext.text
    truncated_note = ""
    if len(text) > max_index_chars:
        text = text[:max_index_chars]
        truncated_note = f"\n（正文仅索引前 {max_index_chars} 字符，其后未参与检索。）"

    spans = _chunk_spans(text, chunk_size, chunk_overlap)
    if not spans:
        return "附件正文为空，无法检索。"

    corpus_tokens: list[list[str]] = []
    for s, e in spans:
        t = _tokenize(text[s:e])
        corpus_tokens.append(t if t else ["_"])

    q_tok = _tokenize(q)
    if not q_tok:
        return "错误：检索 query 分词后为空，请换一种关键词表述。"

    scores = _bm25_scores(corpus_tokens, q_tok)

    k = max(1, min(int(top_k), 20))
    idx_sorted = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]

    lines: list[str] = [
        f"BM25 检索命中（共取 top {k} 条），query={q!r}{truncated_note}",
        "",
    ]
    for rank, i in enumerate(idx_sorted, start=1):
        s, e = spans[i]
        score = float(scores[i])
        mid = (s + e) // 2
        snippet, sn_s, sn_e = _expand_snippet(text, s, e, expand_margin, max_snippet_chars)
        page = _page_for_offset(ext.page_starts, mid)
        if page is not None:
            loc = f"页码≈{page} char=[{sn_s},{sn_e}) chunk=[{s},{e}) score={score:.4f}"
        else:
            loc = f"char=[{sn_s},{sn_e}) chunk=[{s},{e}) score={score:.4f}"
        lines.append(f"--- 命中 #{rank} ({loc}) ---")
        lines.append(snippet)
        lines.append("")

    lines.append(
        "提示：若需更多连续正文，可调用 read_session_attachment；长文档建议先本检索再按需精读。"
    )
    return "\n".join(lines).rstrip()
