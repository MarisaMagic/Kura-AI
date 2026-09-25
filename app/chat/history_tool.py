"""read_session_history：从 PostgreSQL 原文精确翻牌本会话较早的轮次。

为什么需要这个工具（向量库的兜底通道）：
向量库只存**蒸馏后**的摘要与稳定事实，为了控制体积还会滚动淘汰最旧的段。
原文始终 append-only 留在 PG 里，所以「精确取证」不该走向量近似检索，
而应直接按轮次区间/关键词查库——这等价于 Claude Code 压缩后附上的 transcript 文件路径。

有了它，向量库才敢存得少、删得狠。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool

from app.chat.storage import storage
from app.chat.tools import (
    emit_rag_step,
    log_kb_tool_return_to_terminal,
    try_acquire_history_tool_slot,
)
from app.settings import settings

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 12000
_MAX_KEYWORD_HITS = 8


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def read_path_turns(user_id: int, agent_id: int, session_id: str) -> list[dict[str, Any]]:
    """把当前路径的消息记录切成轮次列表。

    轮次下标与记忆归档/压缩使用的 turn_index 同一套编号（按路径顺序从 0 起），
    因此 search_session_memory 报出的「轮次 N」可以直接拿来这里翻原文。
    :return: [{"turn_index", "turn_key", "user", "assistant", "error"}]
    """
    records = storage.get_session_messages(user_id, agent_id, session_id)
    turns: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for rec in records:
        rtype = str(rec.get("type") or "")
        content = str(rec.get("content") or "")
        if rtype == "human":
            cur = {
                "turn_index": len(turns),
                "turn_key": int(rec.get("message_id") or 0),
                "user": content,
                "assistant": "",
                "error": None,
            }
            turns.append(cur)
        elif rtype == "ai" and cur is not None:
            cur["assistant"] = content
            err = rec.get("error_text")
            if err:
                cur["error"] = str(err)
    return turns


def _clip(text: str, max_chars: int) -> str:
    """超长裁剪：保留头尾（结论常在尾部）。"""
    s = text or ""
    if len(s) <= max_chars:
        return s
    if max_chars <= 40:
        return s[:max_chars] + "…"
    head = int(max_chars * 0.25)
    tail = max_chars - head - 24
    return s[:head] + "\n…（中段已省略）…\n" + s[-tail:]


def render_history(
    turns: list[dict[str, Any]],
    *,
    from_turn: int | None = None,
    to_turn: int | None = None,
    keyword: str = "",
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """按轮次区间或关键词筛选并渲染原文。

    :param from_turn: 起始轮次下标（含），缺省为 0
    :param to_turn: 结束轮次下标（含），缺省为最后一轮
    :param keyword: 非空时改为关键词命中模式，最多返回 _MAX_KEYWORD_HITS 轮
    """
    if not turns:
        return "本会话暂无可翻阅的历史轮次。"
    budget = max(500, int(max_chars))
    kw = (keyword or "").strip().lower()

    if kw:
        hits = [
            t
            for t in turns
            if kw in str(t.get("user") or "").lower() or kw in str(t.get("assistant") or "").lower()
        ]
        if not hits:
            return f"未在本会话原文中命中关键词「{keyword.strip()}」（共 {len(turns)} 轮）。"
        picked = hits[-_MAX_KEYWORD_HITS:]
        head = (
            f"[会话原文] 关键词「{keyword.strip()}」命中 {len(hits)} 轮"
            + (f"，仅展示最近 {len(picked)} 轮" if len(hits) > len(picked) else "")
            + f"；全会话共 {len(turns)} 轮：\n"
        )
        selected = picked
    else:
        lo = 0 if from_turn is None else max(0, int(from_turn))
        hi = (len(turns) - 1) if to_turn is None else min(len(turns) - 1, int(to_turn))
        if lo > hi:
            return f"轮次区间无效：from_turn={lo} > to_turn={hi}（本会话共 {len(turns)} 轮，下标从 0 起）。"
        head = f"[会话原文] 轮次 {lo}~{hi}（全会话共 {len(turns)} 轮）：\n"
        selected = turns[lo : hi + 1]

    # 逐轮分配预算：每轮要渲染「用户 + 助手 + 轮次头」三份，故按 3 份均摊，
    # 保证区间内每一轮都出现，而不是把预算全喂给前几轮后被末尾硬截断。
    per_turn = max(120, budget // max(1, len(selected) * 3))
    lines: list[str] = [head]
    for t in selected:
        body = [f"--- 轮次 {int(t.get('turn_index', 0))} ---"]
        body.append(f"用户: {_clip(str(t.get('user') or ''), per_turn)}")
        if t.get("assistant"):
            body.append(f"助手: {_clip(str(t.get('assistant')), per_turn)}")
        if t.get("error"):
            body.append(f"（该轮生成失败：{_clip(str(t.get('error')), 200)}）")
        lines.append("\n".join(body))
    return "\n\n".join(lines)


def make_session_history_tools(user_id: int, agent_id: int, session_id: str) -> list:
    """构造 read_session_history 工具（每轮限用 2 次，防止模型反复翻库）。"""

    def _read_session_history(
        from_turn: int | None = None,
        to_turn: int | None = None,
        keyword: str = "",
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> str:
        limit_msg = (
            "TOOL_CALL_LIMIT_REACHED: read_session_history 本轮调用次数已用完，"
            "请基于已取得的原文直接作答。"
        )
        if not try_acquire_history_tool_slot(_int_setting("CHAT_HISTORY_TOOL_MAX_CALLS", 2)):
            log_kb_tool_return_to_terminal(limit_msg, tool_label="read_session_history")
            return limit_msg

        label = (keyword or "").strip() or f"轮次 {from_turn}~{to_turn}"
        emit_rag_step("📜", "翻阅会话原文", label[:120])
        try:
            turns = read_path_turns(user_id, agent_id, session_id)
            text = render_history(
                turns,
                from_turn=from_turn,
                to_turn=to_turn,
                keyword=keyword,
                max_chars=min(max(500, int(max_chars or _DEFAULT_MAX_CHARS)), 40000),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("read_session_history failed")
            emit_rag_step("⚠️", "翻阅会话原文失败", str(e)[:200])
            text = f"读取会话原文出错：{e}"

        log_kb_tool_return_to_terminal(text, tool_label="read_session_history")
        return text

    return [
        StructuredTool.from_function(
            name="read_session_history",
            description=(
                "按轮次区间或关键词，从数据库读取**本会话较早轮次的逐字原文**。"
                "与 search_session_memory 的分工：后者是向量召回的蒸馏摘要（模糊、可能不全），"
                "本工具是精确取证（逐字、可靠）。"
                "何时使用：（1）search_session_memory 返回的摘要不够具体，需要原文里的代码、数字、表格、原话；"
                "（2）摘要中标注了轮次编号，需要展开该轮完整内容；"
                "（3）用户要求「把我之前发的那段原文再贴一次」。"
                "轮次下标从 0 起，与 search_session_memory 报出的「轮次 N」同一套编号。"
                "参数：from_turn/to_turn 为闭区间轮次下标（都留空表示全部）；keyword 非空时改为关键词命中模式。"
                "约束：同一用户提问轮次内最多成功调用 2 次；取得原文后请直接作答，不要反复翻阅。"
            ),
            func=_read_session_history,
        )
    ]
