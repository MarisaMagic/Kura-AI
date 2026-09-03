"""
按「轮」切分 LangChain 消息：一轮 = 一条用户 Human 及其后直到下一条 Human 前的所有消息。
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.settings import settings


def split_system_prefix(messages: list[BaseMessage]) -> tuple[list[BaseMessage], list[BaseMessage]]:
    """
    将消息体按系统消息分组, 返回系统消息列表和非系统消息列表。
    :param messages: 消息列表
    :return: 系统消息列表和非系统消息列表
    """
    i = 0
    while i < len(messages) and isinstance(messages[i], SystemMessage):
        i += 1
    return messages[:i], messages[i:]


def group_turns(body: list[BaseMessage]) -> list[list[BaseMessage]]:
    """
    将消息体按用户轮次分组（不含前缀 System）。
    一轮 = 一条用户 Human 及其后直到下一条 Human 前的所有消息。
    双指针算法, 时间复杂度 O(n)。
    :param body: 消息列表
    :return: 轮次列表
    """
    turns: list[list[BaseMessage]] = []
    i = 0
    while i < len(body):
        if isinstance(body[i], HumanMessage):
            turn: list[BaseMessage] = [body[i]]
            i += 1
            while i < len(body) and not isinstance(body[i], HumanMessage):
                turn.append(body[i])
                i += 1
            turns.append(turn)
        else:
            i += 1
    return turns


def group_turn_pairs(body_pairs: list[tuple[int, BaseMessage]]) -> list[list[tuple[int, BaseMessage]]]:
    """
    按「轮」切分带行 id 的消息对（不含前缀 System）。
    一轮 = 一条用户 Human 及其后直到下一条 Human 前的所有消息；轮首元素即该轮 turn_key 载体。
    """
    turns: list[list[tuple[int, BaseMessage]]] = []
    cur: list[tuple[int, BaseMessage]] | None = None
    for rid, m in body_pairs:
        if isinstance(m, HumanMessage):
            cur = []
            turns.append(cur)
        if cur is not None:
            cur.append((rid, m))
    return turns


def turn_keys_of(messages: list[BaseMessage], path_ids: list[int] | None) -> list[int]:
    """
    当前路径各轮的稳定身份（turn_key = 轮首用户消息的数据库行 id）。
    path_ids 与 messages 不等长时返回空列表（调用方回退旧逻辑）。
    """
    if not path_ids or len(path_ids) != len(messages):
        return []
    prefix, body = split_system_prefix(messages)
    pairs = list(zip(path_ids[len(prefix):], body))
    return [t[0][0] for t in group_turn_pairs(pairs) if t]


def apply_sliding_window_turns(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    仅保留最近 N 轮对话（不含前缀 System）；轮数不足 N 则原样返回。
    主路径已改为按字符预算压缩（CHAT_COMPACT_ENABLED）；本函数仅作关闭压缩时的回退。
    """
    if not getattr(settings, "CHAT_USE_SESSION_MEMORY", True):
        return list(messages)
    n = max(1, int(getattr(settings, "CHAT_MEMORY_WINDOW_TURNS", 10) or 10))
    prefix, body = split_system_prefix(messages)  # 将消息体按系统消息分组, 返回系统消息列表和非系统消息列表。
    turns = group_turns(body)  # 将消息体按用户轮次分组（不含前缀 System）。
    if len(turns) <= n:
        return list(messages)  # 如果轮次不足 N 则原样返回。
    kept = turns[-n:]  # 保留最近 N 轮对话。
    flat: list[BaseMessage] = []
    for t in kept:
        flat.extend(t)  # 将最近 N 轮对话展平。
    return prefix + flat  # 将系统消息列表和最近 N 轮对话展平。
