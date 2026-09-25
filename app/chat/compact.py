"""会话压缩：token 预算触发 + 9 段结构化分段摘要，原文窗口 append-only。

对齐 Claude Code 的三层设计，并按 B/S 形态做了改造：

- 触发口径：``window - 摘要输出预留 - 安全缓冲``（绝对缓冲，而非百分比/轮数），
  估算值再乘以上一轮真实 ``usage_metadata`` 反推的校准系数；
- 摘要形态：``<analysis>``（丢弃）+ ``<summary>``（9 段固定清单），
  其中「All user messages」是枚举而非概括，「Current Work」要求最细颗粒度；
- 存储形态：**分段**摘要落 mg_chat_compact_segments，各段保真度独立，
  段数/总 token 超限时由同一次 LLM 调用把旧链归并为 level+1 的粗粒度段（向量数收敛 O(log n)）；
- 执行时机：**请求内同步压缩**——达到触发点即当场摘要落段；压缩过程通过 emit_rag_step
  向前端流式提示「正在进行上下文压缩」，用户可见；
- 失败处理：连续失败 N 次熔断，降级为硬截断，不再反复烧钱。

原文消息行永不改写，压缩只影响送给模型的视图。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.chat import compact_store
from app.chat.context_budget import (
    ContextBudget,
    apply_factor,
    budget_for,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tokens_blind,
    extract_input_tokens,
    factor_of,
    resolve_window,
)
from app.chat.memory_turns import group_turns, split_system_prefix
from app.chat.message_codec import msg_content_to_str
from app.settings import settings

logger = logging.getLogger(__name__)

COMPACT_TITLE = "【会话压缩摘要】"
CONTINUATION_LEAD = (
    "本会话是从更早的对话延续过来的。以下是截至指定轮次的结构化摘要，"
    "请据此继续，不要重复询问已经明确过的信息。"
)

# --- 存量 v1 metadata 键（只读兼容，不再写入） ---
_META_SUMMARY = "compact_summary"
_META_UNTIL = "compact_until_turn_index"
_META_STATES = "compact_states"
# --- v2 metadata 键 ---
_META_CALIB = "ctx_calib"
_META_USAGE = "ctx_usage"
_META_FAILURES = "compact_failures"
_META_HISTORY = "compact_history"

_HISTORY_MAX = 20
_NO_TOOL_WARN = (
    "CRITICAL: 只输出纯文本。禁止调用任何工具（包括检索、读文件、执行命令）。\n"
    "- 任何工具调用都会被拒绝，并浪费你唯一的一次机会。\n"
    "- 你的完整回复必须是纯文本。\n"
)

_SUMMARY_SECTIONS = """1. Primary Request and Intent（用户请求与意图）：逐条列出用户明确提出的需求、目标与约束，包含后续轮次里的变更与追加。
2. Key Technical Concepts（关键概念）：涉及的技术/领域概念、方案选型与理由。
3. Files, Attachments and Sources（文件、附件与来源）：出现过的文件名、附件名、attachment_id、file_key、URL、知识库来源编号；每项一句话说明为何重要。
4. Errors and fixes（错误与修复）：遇到的错误、根因、修复方式、用户的相关反馈。
5. Problem Solving（已解决问题）：已解决的与正在排查的，含关键结论、数字与判定依据。
6. All user messages（全部用户消息）：**枚举**所有非工具结果的用户消息（可合并连续同一意图的短句），保留原意与语气，一条不能落。这是判断意图变化的唯一依据。
7. Pending Tasks（待办）：尚未完成的事项，逐条列出。
8. Current Work（当前工作）：用**最细颗粒度**描述中断前正在做什么。反例：「正在调试」；正例：「正在调试登录模块的 token 刷新逻辑，已定位到 cookie 过期判断有误，准备修改 auth.ts 的 refreshToken 函数」。
9. Optional Next Step（下一步）：与 Current Work 直接衔接的下一步。若任务已收尾则写「无」。"""

_FACTS_INSTRUCTION = """另外，在 </summary> 之后追加一个 <facts> 块，输出 JSON 数组，抽取值得跨会话长期记住的**用户稳定偏好与硬约束**：
- 类型只能是 preference（用户偏好）/ constraint（硬约束）之一；已定方案与关键实体不必输出（摘要正文已覆盖）；
- 每项形如 {"type":"preference","subject":"回答语言","content":"始终用中文","why":"用户明确要求","how_to_apply":"生成回答时默认中文"}；
- 相对时间必须转为绝对日期（「周四前」→「2026-03-05 前」）；
- 不要记录：能从当前对话/知识库/系统提示词推导出的内容、临时任务状态、一次性调试细节；
- 没有值得记的就输出 []。"""

_SUMMARIZE_PROMPT = """你是会话压缩助手。把「已有摘要」与「被移出上下文的旧对话」合并成一份供后续对话使用的稳定摘要。

{no_tool_warn}
输出格式（严格遵守，标签必须原样出现）：
<analysis>
（你的分析草稿：判断哪些信息重要、哪些可丢。此块不会进入后续上下文。）
</analysis>

<summary>
（按下面 9 个固定小节组织，每节以「N. 小节名」开头，内容用要点列表）
{sections}
</summary>
{facts_instruction}

硬性要求：
- 只保留可复用信息：用户目标与约束、已做决定、未完成事项、关键实体（文件名、file_key、attachment_id、URL、ID、数字与结论）；
- 不要写入本轮检索开关、选档范围或附件清单等仅单轮有效的内容；
- 不要抄写知识库/网页大段原文，只留结论与来源线索；
- 不要编造对话中没有出现过的事实；信息缺失就写「未提及」；
- 摘要正文控制在 {max_chars} 字以内。
{suppress_note}
已有摘要：
{old_summary}

被移出上下文的对话（越靠后越新）：
{dropped}

{no_tool_warn}"""

_SUPPRESS_FOLLOWUP_NOTE = (
    "\n注意：本次为系统自动压缩，用户不在场。不要在 Current Work 或 Next Step 里写「需要向用户确认」"
    "之类会卡住任务的话；确实存在待确认项时，写入 Pending Tasks 并给出你打算采用的默认假设。\n"
)


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 存量 v1 状态读取（只读兼容）
# ---------------------------------------------------------------------------


def load_compact_state(metadata: dict | None) -> tuple[str, int]:
    """读取存量单状态压缩信息，返回 (摘要, 已压缩到的轮次下标)。"""
    meta = metadata if isinstance(metadata, dict) else {}
    summary = str(meta.get(_META_SUMMARY) or "").strip()
    raw = meta.get(_META_UNTIL, -1)
    try:
        until = int(raw)
    except (TypeError, ValueError):
        until = -1
    if until < -1:
        until = -1
    return summary, until


def verbatim_keep_from_index(until_turn_index: int, turn_count: int) -> int:
    """存量单状态下的原文窗口起始轮次。"""
    if turn_count <= 0:
        return 0
    if until_turn_index < 0:
        return 0
    return min(until_turn_index + 1, turn_count)


def load_compact_states(metadata: dict | None, path_turn_keys: list[int] | None = None) -> list[dict]:
    """
    读取存量压缩状态列表（每条 = 一段已摘要的路径前缀）。
    存量单状态（compact_summary + compact_until_turn_index）在给出当前路径 turn_key 序列时惰性映射。
    """
    meta = metadata if isinstance(metadata, dict) else {}
    states = meta.get(_META_STATES)
    if isinstance(states, list):
        return [
            {
                "covered_turn_keys": [int(k) for k in s.get("covered_turn_keys") or []],
                "summary": str(s.get("summary") or ""),
            }
            for s in states
            if isinstance(s, dict) and isinstance(s.get("covered_turn_keys"), list)
        ]
    summary, until = load_compact_state(meta)
    if summary and until >= 0 and path_turn_keys and len(path_turn_keys) > until:
        return [{"covered_turn_keys": [int(k) for k in path_turn_keys[: until + 1]], "summary": summary}]
    return []


def match_compact_state(states: list[dict], path_turn_keys: list[int]) -> tuple[str, int]:
    """
    在存量压缩状态列表中选取 covered_turn_keys 为当前路径最长前缀的那条。
    返回 (摘要文本, 已压缩轮数)；无匹配返回 ("", 0)。
    """
    best_summary, best_n = "", 0
    for s in states:
        covered = s.get("covered_turn_keys") or []
        n = len(covered)
        if n <= best_n or n > len(path_turn_keys):
            continue
        if list(covered) == [int(k) for k in path_turn_keys[:n]]:
            best_summary, best_n = str(s.get("summary") or ""), n
    return best_summary, best_n


def legacy_summary_and_keep(
    meta: dict | None, turn_keys: list[int] | None, turn_count: int
) -> tuple[str, int]:
    """无分段摘要时回退存量状态，返回 (摘要文本, 原文窗口起始轮次)。"""
    if turn_keys:
        summary, covered = match_compact_state(load_compact_states(meta, turn_keys), turn_keys)
        return summary, min(covered, turn_count)
    summary, until = load_compact_state(meta)
    return summary, verbatim_keep_from_index(until, turn_count)


def verbatim_keep_from_for_session(
    user_id: int,
    agent_id: int,
    session_id: str,
    turn_count: int | None = None,
    path_turn_keys: list[int] | None = None,
    meta: dict | None = None,
    session_ref_id: int | None = None,
) -> int:
    """当前原文窗口起始轮次（不含已压缩轮）。

    优先用分段摘要链；无段时回退存量 metadata 状态。
    :param meta: 调用方已读取的会话元数据；缺省则自行查库（同一轮内可透传避免重复查询）。
    :param session_ref_id: 调用方已取得的会话行主键；缺省则自行查库。
    """
    from app.chat.storage import storage

    if meta is None or session_ref_id is None:
        loaded_meta, loaded_ref = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
        meta = loaded_meta if meta is None else meta
        session_ref_id = loaded_ref if session_ref_id is None else session_ref_id

    if path_turn_keys:
        chain = compact_store.chain_for_path(compact_store.load_segments(session_ref_id), path_turn_keys)
        covered = compact_store.covered_turn_count(chain)
        if covered > 0:
            return min(covered, turn_count) if turn_count is not None else covered
        # turn_count 缺省时以路径轮数为准，否则 legacy 分支会被 min(covered, 0) 压成 0
        n_turns = len(path_turn_keys) if turn_count is None else turn_count
        _, legacy_keep = legacy_summary_and_keep(meta, path_turn_keys, n_turns)
        return legacy_keep

    _, until = load_compact_state(meta)
    if turn_count is None:
        return until + 1 if until >= 0 else 0
    return verbatim_keep_from_index(until, turn_count)


# ---------------------------------------------------------------------------
# 摘要渲染 / 提示词 / 解析
# ---------------------------------------------------------------------------


def render_summary_block(chain: list[dict[str, Any]], *, legacy_summary: str = "") -> str:
    """把段链渲染为注入模型的摘要正文（旧→新，带轮次区间标注）。"""
    texts = [str(s.get("summary") or "").strip() for s in (chain or [])]
    texts = [t for t in texts if t]
    if not texts and legacy_summary:
        texts = [legacy_summary.strip()]
    if not texts:
        return ""
    if len(texts) == 1 and not chain:
        return texts[0]
    parts: list[str] = [CONTINUATION_LEAD]
    for seg, text in zip(chain or [{}], texts):
        if seg:
            parts.append(
                f"--- 覆盖轮次 {int(seg.get('from_index', 0))}~{max(0, int(seg.get('to_index', 1)) - 1)} ---\n{text}"
            )
        else:
            parts.append(text)
    return "\n\n".join(parts)


def build_summary_prompt(
    *,
    old_summary: str,
    dropped_text: str,
    max_chars: int,
    suppress_follow_up: bool = True,
    with_facts: bool = False,
) -> str:
    """构造压缩摘要提示词（供测试与复用）。"""
    return _SUMMARIZE_PROMPT.format(
        no_tool_warn=_NO_TOOL_WARN,
        sections=_SUMMARY_SECTIONS,
        facts_instruction=_FACTS_INSTRUCTION if with_facts else "",
        max_chars=max(500, int(max_chars)),
        suppress_note=_SUPPRESS_FOLLOWUP_NOTE if suppress_follow_up else "",
        old_summary=(old_summary or "").strip() or "（无）",
        dropped=(dropped_text or "").strip() or "（无）",
    )


def parse_summary_output(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """解析摘要器输出：剥离 <analysis>，取 <summary> 正文；附带解析可选 <facts>。

    无标签时回退为「全文即摘要」（并去掉可能的代码块围栏）。
    :return: (摘要正文, facts 列表)
    """
    text = (raw or "").strip()
    if not text:
        return "", []

    summary = ""
    low = text.lower()
    if "<summary>" in low:
        start = low.index("<summary>") + len("<summary>")
        end = low.find("</summary>", start)
        summary = text[start:end] if end > 0 else text[start:]
    else:
        summary = text
    summary = summary.strip()
    if summary.startswith("```"):
        summary = summary.strip("`")
        if summary.lower().startswith("markdown"):
            summary = summary[len("markdown") :]
        summary = summary.strip()

    facts: list[dict[str, Any]] = []
    if "<facts>" in low:
        fstart = low.index("<facts>") + len("<facts>")
        fend = low.find("</facts>", fstart)
        blob = (text[fstart:fend] if fend > 0 else text[fstart:]).strip()
        facts = _parse_facts_json(blob)
    return summary, facts


def _parse_facts_json(blob: str) -> list[dict[str, Any]]:
    """容错解析 facts JSON 数组。"""
    import json

    if not blob:
        return []
    if blob.startswith("```"):
        blob = blob.strip("`")
        if blob.lower().startswith("json"):
            blob = blob[4:]
        blob = blob.strip()
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        s, e = blob.find("["), blob.rfind("]")
        if s < 0 or e <= s:
            return []
        try:
            data = json.loads(blob[s : e + 1])
        except (ValueError, TypeError):
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and str(item.get("content") or item.get("text") or "").strip():
            out.append(item)
    return out


def _turn_texts(turns: list[list[BaseMessage]]) -> list[str]:
    """逐轮转为纯文本（与向量归档共用同一实现，保证口径一致）。"""
    from app.chat.memory_turns_text import turn_to_text

    return [turn_to_text(t) for t in turns]


def prepare_dropped_text(
    turns: list[list[BaseMessage]], *, max_tokens: int, turn_offset: int = 0
) -> str:
    """把被挤出的轮次拼成摘要器输入，超预算时**丢最旧、保最新**。

    旧实现是 ``dropped[:60000]``，恰好把最有价值的最新轮次截掉，此处方向反转。
    :param turn_offset: 首轮在会话中的轮次下标，仅用于标注
    """
    texts = _turn_texts(turns)
    if not texts:
        return ""
    budget = max(512, int(max_tokens))
    kept: list[str] = []
    acc = 0
    dropped_old = 0
    for i in range(len(texts) - 1, -1, -1):
        body = (texts[i] or "").strip()
        if not body:
            continue
        cost = estimate_tokens(body)
        if kept and acc + cost > budget:
            dropped_old = i + 1
            break
        if not kept and cost > budget:
            # 单轮就超预算：保留其尾部（最新信息密度更高）
            head_chars = max(200, int(len(body) * 0.2))
            tail_chars = max(800, len(body) - head_chars)
            body = body[:head_chars] + "\n…（本轮中段已省略）…\n" + body[-tail_chars:]
            cost = estimate_tokens(body)
        kept.append(f"--- 轮次 {turn_offset + i} ---\n{body}")
        acc += cost
    kept.reverse()
    note = ""
    if dropped_old:
        note = f"（提示：更早的 {dropped_old} 轮因超出摘要器输入上限未纳入，其中信息可能缺失）\n\n"
    return note + "\n\n".join(kept)


# ---------------------------------------------------------------------------
# 摘要器调用
# ---------------------------------------------------------------------------


def _build_summarizer_model(llm_config: dict[str, Any]) -> Any:
    from langchain.chat_models import init_chat_model

    from app.utils.egress import pinned_llm_client_kwargs

    base_url = (llm_config.get("base_url") or "").strip() or None
    return init_chat_model(
        model=(llm_config.get("model_name") or "gpt-4"),
        model_provider="openai",
        api_key=(llm_config.get("api_key") or "").strip(),
        base_url=base_url,
        temperature=0,
        stream_usage=False,
        **pinned_llm_client_kwargs(base_url),
    )


def run_summarizer(
    *,
    old_summary: str,
    dropped_text: str,
    llm_config: dict[str, Any],
    max_chars: int,
    suppress_follow_up: bool = True,
    with_facts: bool = False,
) -> tuple[str, list[dict[str, Any]]] | None:
    """调用副模型生成结构化摘要；失败返回 None（由调用方计熔断）。"""
    if not (llm_config.get("api_key") or "").strip():
        return None
    prompt = build_summary_prompt(
        old_summary=old_summary,
        dropped_text=dropped_text,
        max_chars=max_chars,
        suppress_follow_up=suppress_follow_up,
        with_facts=with_facts,
    )
    try:
        from langchain_core.messages import HumanMessage as HM

        model = _build_summarizer_model(llm_config)
        out = model.invoke([HM(content=prompt)])
        raw = (getattr(out, "content", None) or str(out)).strip()
        if isinstance(raw, list):
            raw = msg_content_to_str(raw)
        if not raw:
            return None
        summary, facts = parse_summary_output(raw)
        if not summary:
            return None
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "…"
        return summary, facts
    except Exception as e:  # noqa: BLE001
        logger.warning("会话压缩摘要失败: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 熔断与历史记录
# ---------------------------------------------------------------------------


def is_breaker_tripped(meta: dict | None) -> bool:
    """连续失败是否已达熔断阈值。"""
    limit = max(1, _int_setting("CHAT_COMPACT_MAX_CONSECUTIVE_FAILURES", 3))
    f = (meta or {}).get(_META_FAILURES) if isinstance(meta, dict) else None
    if not isinstance(f, dict):
        return False
    try:
        return int(f.get("count") or 0) >= limit
    except (TypeError, ValueError):
        return False


def _failures_patch(meta: dict | None, *, failed: bool, error: str = "") -> dict:
    """生成熔断计数 patch（成功清零，失败累加）。"""
    cur = (meta or {}).get(_META_FAILURES) if isinstance(meta, dict) else None
    cur = dict(cur) if isinstance(cur, dict) else {}
    if not failed:
        return {_META_FAILURES: {"count": 0, "tripped_at": None, "last_error": None}}
    n = int(cur.get("count") or 0) + 1
    limit = max(1, _int_setting("CHAT_COMPACT_MAX_CONSECUTIVE_FAILURES", 3))
    return {
        _META_FAILURES: {
            "count": n,
            "tripped_at": cur.get("tripped_at") if n < limit else _now_iso(),
            "last_error": (error or "")[:300],
        }
    }


def _history_patch(meta: dict | None, record: dict[str, Any]) -> dict:
    """追加一条压缩记录（上限 20 条，最新在后）。"""
    cur = (meta or {}).get(_META_HISTORY) if isinstance(meta, dict) else None
    hist = list(cur) if isinstance(cur, list) else []
    hist.append(record)
    return {_META_HISTORY: hist[-_HISTORY_MAX:]}


# ---------------------------------------------------------------------------
# 预算估算
# ---------------------------------------------------------------------------


def _choose_keep_from(turns: list[list[BaseMessage]], keep_tokens: int, factor: float) -> int:
    """从尾部往前选出总 token 不超预算的原文窗口起点（至少保留最后一轮）。"""
    if not turns:
        return 0
    budget = max(256, int(keep_tokens))
    acc = 0
    keep_from = len(turns) - 1
    for i in range(len(turns) - 1, -1, -1):
        c = estimate_messages_tokens(turns[i], factor=factor)
        if i < len(turns) - 1 and acc + c > budget:
            break
        acc += c
        keep_from = i
    return keep_from


def estimate_prompt_tokens(
    *,
    system_prompt: str,
    system_chars: int,
    tools_tokens: int,
    summary_text: str,
    verbatim: list[BaseMessage],
    factor: float = 1.0,
) -> int:
    """估算一次请求的输入 token：system + tools + 摘要 + 原文（统一乘校准系数）。"""
    if system_prompt:
        sys_tokens = estimate_tokens(system_prompt)
    else:
        sys_tokens = estimate_tokens_blind(max(0, int(system_chars or 0)))
    raw = (
        sys_tokens
        + max(0, int(tools_tokens or 0))
        + estimate_tokens(summary_text or "")
        + estimate_messages_tokens(verbatim, factor=1.0)
    )
    return apply_factor(raw, factor)


def _budget_of(context_window: Any) -> ContextBudget:
    return budget_for(resolve_window(context_window))


def _summary_max_chars() -> int:
    return max(1000, _int_setting("CHAT_COMPACT_SUMMARY_MAX_CHARS", 8000))


def _summarizer_input_max_tokens() -> int:
    return max(2000, _int_setting("CHAT_COMPACT_SUMMARIZER_INPUT_TOKENS", 60000))


def _facts_enabled() -> bool:
    """是否让摘要器顺带抽取跨会话用户偏好/约束（与摘要共用同一次调用，几乎零额外成本）。"""
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return False
    v = getattr(settings, "CHAT_USER_MEMORY_ENABLED", True)
    return True if v is None else bool(v)


# ---------------------------------------------------------------------------
# 压缩主流程
# ---------------------------------------------------------------------------


def _resolve_state(
    *, meta: dict, session_ref_id: int | None, turn_keys: list[int], turn_count: int
) -> tuple[list[dict[str, Any]], str, int]:
    """返回 (段链, 存量摘要, 原文窗口起始轮次)。"""
    if session_ref_id and turn_keys:
        chain = compact_store.chain_for_path(compact_store.load_segments(session_ref_id), turn_keys)
        covered = compact_store.covered_turn_count(chain)
        if covered > 0:
            return chain, "", min(covered, turn_count)
    legacy_summary, legacy_keep = legacy_summary_and_keep(meta, turn_keys or None, turn_count)
    return [], legacy_summary, legacy_keep


def _persist_segment(
    *,
    session_ref_id: int | None,
    chain: list[dict[str, Any]],
    turn_keys: list[int],
    keep_from: int,
    new_keep: int,
    summary: str,
    llm_config: dict[str, Any],
    merge_chain: bool,
) -> tuple[list[dict[str, Any]], str]:
    """落库新段；merge_chain 为真时把起点锚定到 0 并删除被合并的旧段。

    merge_chain 且 chain 为空是合法组合：存量 v1 单条摘要迁到 v2 时，
    legacy 摘要被并入本次摘要输入，新段的起点必须是 0，
    否则段链无法从 0 连续匹配、下一轮会被整体忽略。
    """
    if not session_ref_id:
        return chain, ""
    if merge_chain:
        from_index, from_key = 0, int(turn_keys[0])
        levels = [int(s.get("level", 0)) for s in chain]
        level = (max(levels) + 1) if levels else 0
        merged_from = [str(s.get("seg_id") or "") for s in chain if s.get("seg_id")]
    else:
        if new_keep <= keep_from:
            return chain, ""
        from_index, from_key = keep_from, int(turn_keys[keep_from])
        level, merged_from = 0, []
    to_index = new_keep
    to_key = int(turn_keys[to_index - 1])
    seg_tokens = estimate_tokens(summary)
    seg_id = compact_store.upsert_segment(
        session_ref_id,
        from_index=from_index,
        to_index=to_index,
        from_turn_key=from_key,
        to_turn_key=to_key,
        summary=summary,
        tokens=seg_tokens,
        level=level,
        merged_from=merged_from,
        model=str(llm_config.get("model_name") or "")[:100] or None,
    )
    if not seg_id:
        return chain, ""
    new_seg = {
        "seg_id": seg_id,
        "from_index": from_index,
        "to_index": to_index,
        "from_turn_key": from_key,
        "to_turn_key": to_key,
        "level": level,
        "merged_from": merged_from,
        "summary": summary,
        "tokens": seg_tokens,
    }
    if merge_chain and merged_from:
        compact_store.delete_segments(session_ref_id, merged_from)
        return [new_seg], seg_id
    return [*chain, new_seg], seg_id


def _should_merge_chain(chain: list[dict[str, Any]]) -> bool:
    """段数或段摘要总 token 超限时，本次压缩顺带把旧链归并成一段。"""
    if not chain:
        return False
    if len(chain) + 1 > max(2, _int_setting("CHAT_COMPACT_MAX_SEGMENTS", 8)):
        return True
    return compact_store.chain_tokens(chain) > max(
        512, _int_setting("CHAT_COMPACT_SEGMENTS_TOKEN_BUDGET", 4000)
    )


def attempt_compaction(
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    turns: list[list[BaseMessage]],
    turn_keys: list[int],
    chain: list[dict[str, Any]],
    legacy_summary: str,
    keep_from: int,
    budget: ContextBudget,
    factor: float,
    llm_config: dict[str, Any],
    meta: dict,
    session_ref_id: int | None,
    system_prompt: str = "",
    system_chars: int = 0,
    tools_tokens: int = 0,
    estimated: int = 0,
    auto: bool = True,
    instructions: str | None = None,
) -> dict[str, Any]:
    """执行一次压缩：选窗口 → 摘要 → 落段 → 写熔断/历史。

    :return: {"ok": bool, "keep_from": int, "chain": list, "summary": str, "seg_id": str,
              "pre_tokens": int, "post_tokens": int, "degraded": bool, "reason": str}
    """
    from app.chat.storage import storage
    from app.chat.tools import emit_rag_step

    new_keep = _choose_keep_from(turns, budget.keep_tokens, factor)
    new_keep = max(new_keep, keep_from)
    result: dict[str, Any] = {
        "ok": False,
        "keep_from": keep_from,
        "chain": chain,
        "summary": render_summary_block(chain, legacy_summary=legacy_summary),
        "seg_id": "",
        "pre_tokens": int(estimated),
        "post_tokens": int(estimated),
        "degraded": False,
        "reason": "",
    }
    if new_keep <= keep_from:
        result["reason"] = "no_turns_to_drop"
        return result

    dropped = turns[keep_from:new_keep]

    # 没有可用的打杂模型 Key：这是配置问题不是压缩失败，不能计入熔断
    # （否则后台预压缩每轮都会「失败」，三次后把真正需要的压缩永久熔断掉）
    if not (llm_config.get("api_key") or "").strip():
        result.update(keep_from=new_keep, degraded=True, reason="no_api_key")
        emit_rag_step("⚠️", "会话压缩未启用", "未配置子智能体 API Key，本轮仅截断较早原文")
        return result

    # 熔断：不再调 LLM，仅本轮硬截断（不写 metadata，下一轮同样降级）
    if auto and is_breaker_tripped(meta) and not instructions:
        result.update(keep_from=new_keep, degraded=True, reason="breaker_tripped")
        emit_rag_step(
            "⚠️",
            "会话压缩已熔断",
            f"连续失败 {int(((meta or {}).get(_META_FAILURES) or {}).get('count') or 0)} 次，本轮仅截断较早原文",
        )
        return result

    # 存量 v1 只有一条单摘要（无分段）：把它并入本次摘要输入，并把新段锚定到 0，
    # 否则新段 from_index>0 无法与段链的「必须从 0 连续」约束匹配，下一轮会被整体忽略。
    merge_chain = _should_merge_chain(chain) or (not chain and bool((legacy_summary or "").strip()))
    old_summary = render_summary_block(chain, legacy_summary=legacy_summary) if merge_chain else ""
    dropped_text = prepare_dropped_text(
        dropped, max_tokens=_summarizer_input_max_tokens(), turn_offset=keep_from
    )
    if instructions:
        dropped_text = f"【用户对本次压缩的额外要求】{instructions.strip()}\n\n{dropped_text}"

    # 同步压缩会阻塞本轮首 token，先向前端发一条「进行中」状态，让用户知道在等什么。
    emit_rag_step(
        "⏳",
        "正在进行上下文压缩",
        f"正在摘要较早的 {new_keep - keep_from} 轮对话，请稍候…",
    )

    out = run_summarizer(
        old_summary=old_summary,
        dropped_text=dropped_text,
        llm_config=llm_config,
        max_chars=_summary_max_chars(),
        suppress_follow_up=auto,
        with_facts=_facts_enabled(),
    )
    if not out:
        # 计数必须在行锁内基于**最新** metadata 递增：用请求开始时读到的快照算，
        # 并发的两次失败会互相覆盖，熔断永远攒不满。
        try:
            storage.mutate_session_metadata(
                user_id,
                agent_id,
                session_id,
                lambda m: _failures_patch(m, failed=True, error="summarizer returned nothing"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("写入压缩熔断计数失败")
        result.update(keep_from=new_keep, degraded=True, reason="summarizer_failed")
        emit_rag_step("⚠️", "会话压缩失败", "本轮仅截断较早原文，稍后自动重试")
        return result

    summary, facts = out
    if facts:
        # 与摘要共用同一次 LLM 调用，抽取用户偏好/约束写入 PG 长期记忆，几乎零额外成本
        try:
            from app.chat.user_memory import store_user_facts

            stored = store_user_facts(user_id, agent_id, facts, session_id=session_id)
            changed = int(stored.get("inserted") or 0) + int(stored.get("updated") or 0)
            if changed:
                emit_rag_step("🧠", "长期记忆更新", f"已记住 {changed} 条用户偏好/约束")
        except Exception:  # noqa: BLE001
            logger.exception("store_user_facts failed")
    new_chain, seg_id = _persist_segment(
        session_ref_id=session_ref_id,
        chain=chain,
        turn_keys=turn_keys,
        keep_from=keep_from,
        new_keep=new_keep,
        summary=summary,
        llm_config=llm_config,
        merge_chain=merge_chain,
    )
    if not seg_id:
        # 落库失败：本轮仍按新窗口截断，但不改状态（下次重试）
        result.update(keep_from=new_keep, degraded=True, reason="persist_failed")
        emit_rag_step("⚠️", "会话压缩未落库", "本轮仅截断较早原文，下次再试")
        return result

    summary_text = render_summary_block(new_chain)
    post = estimate_prompt_tokens(
        system_prompt=system_prompt,
        system_chars=system_chars,
        tools_tokens=tools_tokens,
        summary_text=summary_text,
        verbatim=[m for t in turns[new_keep:] for m in t],
        factor=factor,
    )
    record = {
        "at": _now_iso(),
        "auto": bool(auto),
        "pre_tokens": int(estimated),
        "post_tokens": int(post),
        "covered_from": int(new_chain[-1].get("from_index", 0)),
        "covered_to": int(new_keep),
        "level": int(new_chain[-1].get("level", 0)),
        "seg_id": seg_id,
        "merged": bool(merge_chain),
        "model": str(llm_config.get("model_name") or "")[:100],
    }

    def _patch(m: dict) -> dict:
        p = _failures_patch(m, failed=False)
        p.update(_history_patch(m, record))
        # 不清理存量 v1 键：v2 段链在匹配时优先，v1 仅作为「分叉到尚未 v2 化的分支」时的兜底。
        # 无条件清 _META_STATES 会连带抹掉其它分支的摘要，导致切回该分支时窗口回退到 0。
        return p

    try:
        storage.mutate_session_metadata(user_id, agent_id, session_id, _patch)
    except Exception:  # noqa: BLE001
        logger.exception("写入压缩历史失败")

    result.update(
        ok=True,
        keep_from=new_keep,
        chain=new_chain,
        summary=summary_text,
        seg_id=seg_id,
        post_tokens=int(post),
        reason="compacted",
    )
    emit_rag_step(
        "📦",
        "会话压缩",
        f"{new_keep - int(new_chain[0].get('from_index', 0))} 轮已摘要，"
        f"约 {_fmt_k(record['pre_tokens'])} → {_fmt_k(post)} tokens，原文自轮次 {new_keep} 起",
    )
    return result


def _fmt_k(n: int) -> str:
    """token 数的紧凑展示。"""
    n = max(0, int(n or 0))
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _summary_message(text: str) -> HumanMessage:
    return HumanMessage(content=f"{COMPACT_TITLE}\n\n{text.strip()}")


def build_compacted_model_messages(
    messages: list[BaseMessage],
    *,
    user_id: int,
    agent_id: int,
    session_id: str,
    llm_config: dict[str, Any],
    system_chars: int = 0,
    system_prompt: str = "",
    tools_tokens: int = 0,
    context_window: Any = None,
    calib: dict | None = None,
    path_ids: list[int] | None = None,
    budget_out: dict | None = None,
    allow_sync_compact: bool = True,
) -> list[BaseMessage]:
    """构建送给主模型的压缩视图（不修改 storage 中的原文）。

    流程：先用 micro-compact 零成本投影清理旧工具结果，再据此重新估算；
    仍超过触发点才做同步摘要（摘要器只看到占位符，输入更小、质量更高）。
    :param budget_out: 可选出参字典，回写本轮预算/估算/校准，供落库后做 usage 校准。
    """
    from app.chat.storage import storage
    from app.chat.tool_result_compact import project_micro_compact
    from app.chat.tools import emit_rag_step

    prefix, body = split_system_prefix(messages)
    turns = group_turns(body)
    if not turns:
        if isinstance(budget_out, dict):
            budget_out.update({"turn_count": 0, "estimated": 0})
        return list(messages)

    turn_keys = _turn_keys_of(messages, path_ids)
    budget = _budget_of(context_window)
    model_name = str(llm_config.get("model_name") or "")
    meta, session_ref_id = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
    if calib is None:
        calib = meta.get(_META_CALIB)
    factor = factor_of(calib, model_name)

    chain, legacy_summary, keep_from = _resolve_state(
        meta=meta, session_ref_id=session_ref_id, turn_keys=turn_keys, turn_count=len(turns)
    )
    summary_text = render_summary_block(chain, legacy_summary=legacy_summary)

    # micro-compact 前置：超轮内阈值先用零成本投影清理旧工具结果，再据此估算。
    # 否则「清一下就够了」的场景也会触发一次 3-8s 的同步 LLM 摘要。
    projected, mc_cleared, mc_clipped = project_micro_compact(
        prefix + body, context_window=context_window
    )
    if mc_cleared or mc_clipped:
        prefix, body = split_system_prefix(projected)
        turns = group_turns(body)
        emit_rag_step(
            "🧹",
            "轮内上下文清理",
            f"压缩预处理：已清理 {mc_cleared} 条旧工具结果，裁剪 {mc_clipped} 条超长结果",
        )

    verbatim = [m for t in turns[keep_from:] for m in t]
    estimated = estimate_prompt_tokens(
        system_prompt=system_prompt,
        system_chars=system_chars,
        tools_tokens=tools_tokens,
        summary_text=summary_text,
        verbatim=prefix + verbatim,
        factor=factor,
    )

    if isinstance(budget_out, dict):
        budget_out.update(
            {
                "window": budget.window,
                "effective": budget.effective,
                "trigger": budget.trigger,
                "estimated": estimated,
                "factor": factor,
                "model_name": model_name,
                "keep_from": keep_from,
                "turn_count": len(turns),
                "segments": len(chain),
                "session_ref_id": session_ref_id,
                "system_prompt": system_prompt,
                "system_chars": system_chars,
                "tools_tokens": tools_tokens,
                "turn_keys": turn_keys,
                "microcompacted": {"cleared": mc_cleared, "clipped": mc_clipped},
                "degraded": is_breaker_tripped(meta),
            }
        )

    need = estimated >= budget.trigger and keep_from < len(turns) - 1
    if need and allow_sync_compact and turn_keys and session_ref_id:
        res = attempt_compaction(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            turns=turns,
            turn_keys=turn_keys,
            chain=chain,
            legacy_summary=legacy_summary,
            keep_from=keep_from,
            budget=budget,
            factor=factor,
            llm_config=llm_config,
            meta=meta,
            session_ref_id=session_ref_id,
            system_prompt=system_prompt,
            system_chars=system_chars,
            tools_tokens=tools_tokens,
            estimated=estimated,
            auto=True,
        )
        chain = res.get("chain") or chain
        summary_text = res.get("summary") or summary_text
        keep_from = int(res.get("keep_from") or keep_from)
        estimated = int(res.get("post_tokens") or estimated)
        if isinstance(budget_out, dict):
            budget_out.update(
                {
                    "estimated": estimated,
                    "keep_from": keep_from,
                    "segments": len(chain),
                    "sync_compacted": bool(res.get("ok")),
                    "degraded": bool(res.get("degraded")),
                }
            )

    out = list(prefix)
    if summary_text:
        out.append(_summary_message(summary_text))
    for t in turns[keep_from:]:
        out.extend(t)
    return out


def _turn_keys_of(messages: list[BaseMessage], path_ids: list[int] | None) -> list[int]:
    from app.chat.memory_turns import turn_keys_of

    return turn_keys_of(messages, path_ids)


# ---------------------------------------------------------------------------
# 观测
# ---------------------------------------------------------------------------


def record_usage_calibration(
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    usage: Any,
    estimated: int,
    model_name: str,
) -> None:
    """用本轮真实 usage_metadata 更新 token 校准系数（写会话 metadata）。"""
    from app.chat.context_budget import calibrate_factor
    from app.chat.storage import storage

    input_tokens = extract_input_tokens(usage)
    if input_tokens <= 0 or estimated <= 0:
        return

    def _patch(m: dict) -> dict:
        new_calib = calibrate_factor(
            m.get(_META_CALIB), estimated=estimated, input_tokens=input_tokens, model=model_name
        )
        if not new_calib:
            return {}
        return {
            _META_CALIB: new_calib,
            _META_USAGE: {"input_tokens": input_tokens, "estimated": int(estimated), "at": _now_iso()},
        }

    try:
        storage.mutate_session_metadata(user_id, agent_id, session_id, _patch)
    except Exception:  # noqa: BLE001
        logger.exception("record_usage_calibration failed")


def manual_compact(
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    llm_config: dict[str, Any],
    context_window: Any = None,
    system_prompt: str = "",
    system_chars: int = 0,
    tools_tokens: int = 0,
    instructions: str | None = None,
) -> dict[str, Any]:
    """手动压缩（对齐 /compact）：无视阈值与熔断，立即把原文窗口外的轮次摘要落段。

    :param instructions: 用户对本次压缩的额外要求，会拼进摘要器输入
    """
    from app.chat.storage import storage

    pairs = storage.load_path_with_ids(user_id, agent_id, session_id)
    if not pairs:
        return {"ok": False, "reason": "empty"}
    msgs = [m for _, m in pairs]
    ids = [i for i, _ in pairs]
    prefix, body = split_system_prefix(msgs)
    turns = group_turns(body)
    turn_keys = _turn_keys_of(msgs, ids)
    if not turns or not turn_keys:
        return {"ok": False, "reason": "no_turn_keys"}

    budget = _budget_of(context_window)
    model_name = str(llm_config.get("model_name") or "")
    meta, session_ref_id = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
    factor = factor_of(meta.get(_META_CALIB), model_name)
    chain, legacy_summary, keep_from = _resolve_state(
        meta=meta, session_ref_id=session_ref_id, turn_keys=turn_keys, turn_count=len(turns)
    )
    if keep_from >= len(turns) - 1:
        return {"ok": False, "reason": "nothing_to_compact", "keep_from": keep_from}

    estimated = estimate_prompt_tokens(
        system_prompt=system_prompt,
        system_chars=system_chars,
        tools_tokens=tools_tokens,
        summary_text=render_summary_block(chain, legacy_summary=legacy_summary),
        verbatim=prefix + [m for t in turns[keep_from:] for m in t],
        factor=factor,
    )
    return attempt_compaction(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        turns=turns,
        turn_keys=turn_keys,
        chain=chain,
        legacy_summary=legacy_summary,
        keep_from=keep_from,
        budget=budget,
        factor=factor,
        llm_config=llm_config,
        meta=meta,
        session_ref_id=session_ref_id,
        system_prompt=system_prompt,
        system_chars=system_chars,
        tools_tokens=tools_tokens,
        estimated=estimated,
        auto=False,
        instructions=instructions,
    )


def _cached_fixed_overhead(agent_id: int) -> dict[str, Any]:
    """读取上一次构建 agent 时缓存的固定开销（system + tools schema）。"""
    from app.chat.cache import cache

    val = cache.get_json(f"chat_ctx_fixed:{int(agent_id)}")
    return val if isinstance(val, dict) else {}


def context_usage_snapshot(
    user_id: int,
    agent_id: int,
    session_id: str,
    *,
    context_window: Any = None,
    system_prompt: str = "",
    system_chars: int = 0,
    tools_tokens: int = 0,
) -> dict[str, Any]:
    """当前会话的上下文占用快照（供前端上下文占用条 / 运维排查）。

    未显式传入 system/tools 开销时，回退到上次构建 agent 时缓存的实测值。
    """
    from app.chat.storage import storage

    if not system_prompt and not system_chars and not tools_tokens:
        cached = _cached_fixed_overhead(agent_id)
        system_chars = int(cached.get("system_chars") or 0)
        tools_tokens = int(cached.get("tools_tokens") or 0)

    budget = _budget_of(context_window)
    pairs = storage.load_path_with_ids(user_id, agent_id, session_id)
    msgs = [m for _, m in pairs]
    ids = [i for i, _ in pairs]
    prefix, body = split_system_prefix(msgs)
    turns = group_turns(body)
    turn_keys = _turn_keys_of(msgs, ids)
    meta, session_ref_id = storage.get_session_meta_and_ref(user_id, agent_id, session_id)
    calib = meta.get(_META_CALIB) if isinstance(meta.get(_META_CALIB), dict) else {}
    factor = factor_of(calib, str(calib.get("model") or ""))
    chain, legacy_summary, keep_from = _resolve_state(
        meta=meta, session_ref_id=session_ref_id, turn_keys=turn_keys, turn_count=len(turns)
    )
    summary_text = render_summary_block(chain, legacy_summary=legacy_summary)
    estimated = estimate_prompt_tokens(
        system_prompt=system_prompt,
        system_chars=system_chars,
        tools_tokens=tools_tokens,
        summary_text=summary_text,
        verbatim=prefix + [m for t in turns[keep_from:] for m in t],
        factor=factor,
    )
    failures = meta.get(_META_FAILURES) if isinstance(meta.get(_META_FAILURES), dict) else {}
    return {
        "window": budget.window,
        "effective": budget.effective,
        "trigger": budget.trigger,
        "keep_tokens": budget.keep_tokens,
        "used": estimated,
        "ratio": round(min(1.5, estimated / budget.window), 4) if budget.window else 0.0,
        "turn_count": len(turns),
        "keep_from": keep_from,
        "verbatim_turns": max(0, len(turns) - keep_from),
        "segments": [
            {
                "seg_id": s.get("seg_id"),
                "from_index": s.get("from_index"),
                "to_index": s.get("to_index"),
                "level": s.get("level"),
                "tokens": s.get("tokens"),
            }
            for s in chain
        ],
        "summary_tokens": estimate_tokens(summary_text),
        "calibrated": bool(factor != 1.0),
        "factor": round(float(factor), 4),
        "degraded": is_breaker_tripped(meta),
        "failures": int(failures.get("count") or 0),
        "last_compactions": list(meta.get(_META_HISTORY) or [])[-5:],
    }
