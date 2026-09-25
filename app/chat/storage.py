"""
对话持久化: PostgreSQL + Redis (按 user_id + agent_id + session_id)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import ChatAttachment as ChatAttachmentRow
from app.chat.db_models import ChatMessage as ChatMessageRow
from app.chat.db_models import ChatSession as ChatSessionRow
from app.chat.errors import ChatQuotaExceeded
from app.chat.message_codec import envelope_to_langchain_message, msg_content_to_str, serialize_message_envelope
from app.chat.preview_session import (
    EDITOR_PREVIEW_SESSION_PREFIX,
    is_editor_preview_session,
)
from app.settings import settings

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _cap_preview(text: str) -> str:
    """预览列字符上限（完整正文存 content_json，预览仅用于列表/展示）。"""
    cap = _int_setting("CHAT_MESSAGE_PREVIEW_CHARS", 2000)
    s = text or ""
    if cap > 0 and len(s) > cap:
        return s[:cap] + "…"
    return s


def _cap_extra_fields(fields: dict) -> dict:
    """限制助手消息附加 JSON 体积：列表长度上限 + 整体字符兜底（超出丢调试字段）。"""

    def _cap_list(value, name: str, default: int):
        limit = _int_setting(name, default)
        if isinstance(value, list) and limit > 0 and len(value) > limit:
            return value[-limit:]
        return value

    fields["rag_steps"] = _cap_list(fields.get("rag_steps"), "CHAT_RAG_STEPS_MAX", 50)
    fields["thinking_items"] = _cap_list(fields.get("thinking_items"), "CHAT_THINKING_ITEMS_MAX", 200)
    fields["sources"] = _cap_list(fields.get("sources"), "CHAT_SOURCES_MAX", 50)
    thinking_text = fields.get("thinking_text")
    if isinstance(thinking_text, str):
        cap = _int_setting("CHAT_MESSAGE_PREVIEW_CHARS", 2000)
        if cap > 0 and len(thinking_text) > cap:
            fields["thinking_text"] = thinking_text[:cap] + "…"

    # 整体字符兜底：先丢纯调试的 rag_trace，再对列表逐步对半裁，直到低于预算
    budget = _int_setting("CHAT_EXTRA_JSON_MAX_CHARS", 8000)
    if budget > 0:
        for _ in range(8):
            total = 0
            for key in ("rag_trace", "rag_steps", "thinking_items", "sources", "image_references"):
                value = fields.get(key)
                if not value:
                    continue
                try:
                    total += len(json.dumps(value, ensure_ascii=False, default=str))
                except Exception:  # noqa: BLE001
                    continue
            if total <= budget:
                break
            if fields.get("rag_trace") is not None:
                fields["rag_trace"] = None
                continue
            for key in ("thinking_items", "rag_steps", "sources", "image_references"):
                value = fields.get(key)
                if isinstance(value, list) and value:
                    fields[key] = value[: max(1, len(value) // 2)]
                    break
    return fields


class ConversationStorage:
    def _preview_from_path_rows(self, path: list) -> str:
        preview = ""
        for r in reversed(path):
            if r.message_type != "human":
                continue
            if r.content_json and isinstance(r.content_json, dict):
                preview = msg_content_to_str(r.content_json.get("lc")).strip()
            elif (r.content or "").strip():
                preview = (r.content or "").strip()
            preview = preview.replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:160]
            break
        return preview

    def _build_session_info_dict(self, db, s: ChatSessionRow) -> dict:
        """
        由会话 ORM 行生成列表项（预览与计数），供全量列表与分页列表复用。
        优先读会话表上的 last_user_preview / path_message_count，避免 N+1 加载 content_json。
        """
        if s.path_message_count is not None:
            return {
                "session_id": s.session_id,
                "agent_id": s.agent_id,
                "updated_at": s.updated_at.isoformat(),
                "message_count": int(s.path_message_count),
                "last_user_preview": (s.last_user_preview or ""),
            }
        rows = (
            db.query(
                ChatMessageRow.id,
                ChatMessageRow.parent_id,
                ChatMessageRow.selected_child_id,
                ChatMessageRow.message_type,
                ChatMessageRow.content,
                ChatMessageRow.content_json,
            )
            .filter(ChatMessageRow.session_ref_id == s.id)
            .order_by(ChatMessageRow.id.asc())
            .all()
        )
        path = self._walk_path(rows)
        preview = self._preview_from_path_rows(path)
        return {
            "session_id": s.session_id,
            "agent_id": s.agent_id,
            "updated_at": s.updated_at.isoformat(),
            "message_count": len(path),
            "last_user_preview": preview,
        }

    @staticmethod
    def _messages_cache_key(user_id: int, agent_id: int, session_id: str) -> str:
        """
        生成消息缓存的 Redis 键
        """
        return f"chat_messages:{user_id}:{agent_id}:{session_id}"

    @staticmethod
    def _sessions_cache_key(user_id: int, agent_id: int) -> str:
        """
        生成会话缓存的 Redis 键
        """
        return f"chat_sessions:{user_id}:{agent_id}"

    @staticmethod
    def _sessions_all_cache_key(user_id: int) -> str:
        """
        当前用户跨全部智能体的会话列表（有序，与侧栏「最近对话」分页一致）
        """
        return f"chat_sessions_all:{user_id}"

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
        """
        将数据库记录转换为 LangChain 消息列表
        (HumanMessage, AIMessage, SystemMessage)
        """
        messages = []
        for msg_data in records:
            msg_type = msg_data.get("type")
            cj = msg_data.get("content_json")
            if cj and isinstance(cj, dict) and cj.get("v"):
                messages.append(envelope_to_langchain_message(cj))
                continue
            content = msg_data.get("content", "")
            if msg_type == "human":
                messages.append(HumanMessage(content=content))
            elif msg_type == "ai":
                messages.append(AIMessage(content=content))
            elif msg_type == "system":
                messages.append(SystemMessage(content=content))
        return messages

    @staticmethod
    def _row_to_record(row: ChatMessageRow) -> dict:
        """将消息 ORM 行转为 API / Redis 使用的字典。"""
        env = row.content_json if isinstance(row.content_json, dict) else None
        item = {
            "message_id": row.id,
            "parent_id": row.parent_id,
            "type": row.message_type,
            "content": row.content,
            "timestamp": row.timestamp.isoformat(),
            "rag_trace": row.rag_trace,
            "rag_steps": row.rag_steps,
            "error_text": row.error_text,
            "image_references": row.image_references,
            "sources": row.sources,
            "thinking_text": row.thinking_text,
            "thinking_items": row.thinking_items,
        }
        if env:
            item["content_json"] = env
        return item

    @staticmethod
    def _walk_path(rows: list) -> list:
        """
        从根（parent_id 为空的首条消息）沿 selected_child_id 解析当前路径。
        指针缺失或悬空时回退到 id 最小的子节点；无根时退化为按 id 全量线性序列（数据异常兜底）。
        """
        if not rows:
            return []
        by_id = {r.id: r for r in rows}
        children: dict[int, list] = {}
        roots = []
        for r in rows:
            if r.parent_id is None:
                roots.append(r)
            else:
                children.setdefault(r.parent_id, []).append(r)
        if not roots:
            return sorted(rows, key=lambda r: r.id)
        node = min(roots, key=lambda r: r.id)
        path = []
        seen = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            path.append(node)
            nxt = by_id.get(node.selected_child_id) if node.selected_child_id else None
            if nxt is None:
                kids = children.get(node.id, [])
                nxt = min(kids, key=lambda r: r.id) if kids else None
            node = nxt
        return path

    @staticmethod
    def _version_meta(rows: list, path: list) -> dict[int, tuple[int, int, list[int]]]:
        """路径上每个 AI 节点在同父兄弟中的 (version_index, version_count, sibling_ids)。"""
        ai_by_parent: dict[int, list] = {}
        for r in rows:
            if r.message_type == "ai" and r.parent_id is not None:
                ai_by_parent.setdefault(r.parent_id, []).append(r)
        meta: dict[int, tuple[int, int, list[int]]] = {}
        for node in path:
            if node.message_type != "ai" or node.parent_id is None:
                continue
            sibs = sorted(ai_by_parent.get(node.parent_id, []), key=lambda r: r.id)
            ids = [s.id for s in sibs]
            idx = ids.index(node.id) + 1 if node.id in ids else len(ids)
            meta[node.id] = (idx, len(ids), ids)
        return meta

    @staticmethod
    def _parse_extra(extra: dict | None) -> dict:
        """从 extra_message_data 项提取可落库字段（kb_preselect 无对应列，与旧 save 一样不入库）。"""
        extra = extra or {}
        error_text = None
        raw_err = extra.get("error_text")
        if raw_err is not None:
            error_text = str(raw_err).strip() or None
        return _cap_extra_fields(
            {
                "rag_trace": extra.get("rag_trace"),
                "rag_steps": extra.get("rag_steps"),
                "error_text": error_text,
                "image_references": extra.get("image_references"),
                "sources": extra.get("sources"),
                "thinking_text": extra.get("thinking_text"),
                "thinking_items": extra.get("thinking_items"),
            }
        )

    def _session_query(self, db, user_id: int, agent_id: int, session_id: str):
        return db.query(ChatSessionRow).filter(
            ChatSessionRow.user_id == user_id,
            ChatSessionRow.agent_id == agent_id,
            ChatSessionRow.session_id == session_id,
        )

    def _get_or_create_session(
        self,
        db,
        user_id: int,
        agent_id: int,
        session_id: str,
        metadata: dict | None = None,
    ) -> ChatSessionRow:
        """
        取会话行并加行锁；不存在则创建。
        并发首条消息撞唯一约束时回滚本事务起点后重查（此时尚未插入消息行）。
        """
        session = self._session_query(db, user_id, agent_id, session_id).with_for_update().first()
        if session:
            if metadata is not None:
                session.metadata_json = metadata
            return session
        session = ChatSessionRow(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            metadata_json=metadata if metadata is not None else {},
        )
        db.add(session)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            session = self._session_query(db, user_id, agent_id, session_id).with_for_update().first()
            if session is None:
                raise
            if metadata is not None:
                session.metadata_json = metadata
        return session

    def _enforce_session_quota(self, db, user_id: int, agent_id: int, session_id: str) -> None:
        """新建会话前检查每用户每智能体会话数上限（已存在则放行）。"""
        limit = _int_setting("CHAT_MAX_SESSIONS_PER_USER_AGENT", 200)
        if limit <= 0:
            return
        exists = (
            self._session_query(db, user_id, agent_id, session_id)
            .with_entities(ChatSessionRow.id)
            .first()
        )
        if exists:
            return
        count = int(
            db.query(func.count(ChatSessionRow.id))
            .filter(
                ChatSessionRow.user_id == int(user_id),
                ChatSessionRow.agent_id == int(agent_id),
            )
            .scalar()
            or 0
        )
        if count >= limit:
            raise ChatQuotaExceeded("session_limit", limit=limit)

    @staticmethod
    def _enforce_message_quota(current_rows: int, adding: int) -> None:
        """写入前检查每会话消息行上限（adding 可为负，表示本次净减少）。"""
        limit = _int_setting("CHAT_MAX_MESSAGES_PER_SESSION", 4000)
        if limit <= 0:
            return
        if int(current_rows) + int(adding) > limit:
            raise ChatQuotaExceeded("message_limit", limit=limit)

    def check_chat_quota(
        self, user_id: int, agent_id: int, session_id: str, *, reserve: int = 2
    ) -> None:
        """对话入口预检：会话数 / 每会话消息数超限时抛 ChatQuotaExceeded。

        :param reserve: 预计本轮新增的消息行数（普通一轮 = human + ai = 2；重新生成 = 1）
        """
        db = SessionLocal()
        try:
            session = (
                self._session_query(db, user_id, agent_id, session_id)
                .with_entities(ChatSessionRow.id)
                .first()
            )
            if session is None:
                limit = _int_setting("CHAT_MAX_SESSIONS_PER_USER_AGENT", 200)
                if limit > 0:
                    count = int(
                        db.query(func.count(ChatSessionRow.id))
                        .filter(
                            ChatSessionRow.user_id == int(user_id),
                            ChatSessionRow.agent_id == int(agent_id),
                        )
                        .scalar()
                        or 0
                    )
                    if count >= limit:
                        raise ChatQuotaExceeded("session_limit", limit=limit)
                return
            total = int(
                db.query(func.count(ChatMessageRow.id))
                .filter(ChatMessageRow.session_ref_id == int(session[0]))
                .scalar()
                or 0
            )
            self._enforce_message_quota(total, max(0, int(reserve)))
        finally:
            db.close()

    def _insert_message_row(
        self,
        db,
        session: ChatSessionRow,
        msg,
        extra: dict | None,
        now: datetime,
        parent_id: int | None = None,
    ) -> ChatMessageRow:
        fields = self._parse_extra(extra)
        envelope = serialize_message_envelope(msg)
        preview = _cap_preview(msg_content_to_str(getattr(msg, "content", "")))
        row = ChatMessageRow(
            session_ref_id=session.id,
            parent_id=parent_id,
            message_type=msg.type,
            content=preview,
            content_json=envelope,
            timestamp=now,
            rag_trace=fields["rag_trace"],
            rag_steps=fields["rag_steps"],
            error_text=fields["error_text"],
            image_references=fields["image_references"],
            sources=fields["sources"],
            thinking_text=fields["thinking_text"],
            thinking_items=fields["thinking_items"],
        )
        db.add(row)
        return row

    def _records_from_rows(self, rows: list, path: list) -> list[dict]:
        """由（全量行, 当前路径行）生成带版本信息的记录列表，不再查库。"""
        vmeta = self._version_meta(rows, path)
        records = []
        for row in path:
            rec = self._row_to_record(row)
            vidx, vcnt, sib = vmeta.get(row.id, (1, 1, [row.id]))
            rec["version_index"] = vidx
            rec["version_count"] = vcnt
            rec["sibling_ids"] = sib
            records.append(rec)
        return records

    def _load_message_records(self, db, session: ChatSessionRow) -> list[dict]:
        """加载当前选中路径上的消息记录（附带版本信息），而非全树按 id 排序。"""
        rows = (
            db.query(ChatMessageRow)
            .filter(ChatMessageRow.session_ref_id == session.id)
            .order_by(ChatMessageRow.id.asc())
            .all()
        )
        return self._records_from_rows(rows, self._walk_path(rows))

    def _commit_and_refresh_caches(
        self,
        db,
        session: ChatSessionRow,
        user_id: int,
        agent_id: int,
        session_id: str,
        rows: list | None = None,
    ) -> None:
        """提交消息变更后按库回填消息缓存，并失效会话列表缓存。

        :param rows: 传入「已有行 + 本次新增行」的全量内存行时直接复用，
            跳过重复全量 SELECT（append 热路径）；缺省则照旧查库（其余入口）。
        """
        session.updated_at = datetime.utcnow()
        db.flush()
        if rows is None:
            rows = (
                db.query(ChatMessageRow)
                .filter(ChatMessageRow.session_ref_id == session.id)
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
        path = self._walk_path(rows)
        session.last_user_preview = self._preview_from_path_rows(path)
        session.path_message_count = len(path)
        db.commit()
        # expire_on_commit=False：提交后内存行属性仍有效，直接据此构建记录，无需重查
        records = self._records_from_rows(rows, path)
        cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), records)
        cache.delete(self._sessions_cache_key(user_id, agent_id))
        cache.delete(self._sessions_all_cache_key(user_id))

    def append_messages(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        new_messages: list,
        extra_message_data: list | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        增量追加消息行，不改写已有行（含 rag_steps / thinking_text 等 extras）。
        :param new_messages: 本轮新增的 LangChain 消息（不是全量历史）
        :param extra_message_data: 与 new_messages 等长的 extras 列表，缺省视为空
        """
        if not new_messages:
            return
        db = SessionLocal()
        try:
            self._enforce_session_quota(db, user_id, agent_id, session_id)
            session = self._get_or_create_session(db, user_id, agent_id, session_id, metadata)
            now = datetime.utcnow()
            # 单次加载全量行：叶子定位与后续缓存回填复用同一份内存数据（原为 3 次全量 SELECT）
            rows = (
                db.query(ChatMessageRow)
                .filter(ChatMessageRow.session_ref_id == session.id)
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
            self._enforce_message_quota(len(rows), len(new_messages))
            path = self._walk_path(rows)
            parent = path[-1] if path else None
            extras = extra_message_data or []
            for idx, msg in enumerate(new_messages):
                extra = extras[idx] if idx < len(extras) else None
                row = self._insert_message_row(
                    db, session, msg, extra, now, parent_id=parent.id if parent else None
                )
                db.flush()
                if parent is not None:
                    parent.selected_child_id = row.id
                parent = row
                rows.append(row)
            self._commit_and_refresh_caches(db, session, user_id, agent_id, session_id, rows=rows)
        finally:
            db.close()

    def insert_assistant_version(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        target_ai_id: int,
        ai_message,
        extra: dict | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """
        重新生成：在目标助手消息的父用户消息下插入兄弟版本并选中新版本，旧分支完整保留。
        :return: 目标非法（不属于本会话 / 非 ai / 无父用户消息）时返回 False
        """
        db = SessionLocal()
        try:
            session = self._get_or_create_session(db, user_id, agent_id, session_id, metadata)
            target = (
                db.query(ChatMessageRow).filter(ChatMessageRow.id == int(target_ai_id)).first()
            )
            if (
                not target
                or target.session_ref_id != session.id
                or target.message_type != "ai"
                or target.parent_id is None
            ):
                return False
            parent = (
                db.query(ChatMessageRow).filter(ChatMessageRow.id == target.parent_id).first()
            )
            if not parent or parent.message_type != "human":
                return False
            # 版本上限：同一 human 下只保留最近 N 个助手版本，超出淘汰最旧
            version_cap = _int_setting("CHAT_MAX_ASSISTANT_VERSIONS", 5)
            sibs = (
                db.query(ChatMessageRow)
                .filter(
                    ChatMessageRow.session_ref_id == session.id,
                    ChatMessageRow.parent_id == parent.id,
                    ChatMessageRow.message_type == "ai",
                )
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
            evict = 0
            if version_cap > 0 and len(sibs) >= version_cap:
                evict = len(sibs) - (version_cap - 1)
            total = int(
                db.query(func.count(ChatMessageRow.id))
                .filter(ChatMessageRow.session_ref_id == session.id)
                .scalar()
                or 0
            )
            self._enforce_message_quota(total, 1 - evict)
            row = self._insert_message_row(
                db, session, ai_message, extra, datetime.utcnow(), parent_id=parent.id
            )
            db.flush()
            parent.selected_child_id = row.id
            for old in sibs[:evict]:
                db.delete(old)
            self._commit_and_refresh_caches(db, session, user_id, agent_id, session_id)
            return True
        finally:
            db.close()

    def update_assistant_in_place(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        target_ai_id: int,
        ai_message,
        extra: dict | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """
        原地覆盖目标助手消息内容与 extras（MCP 高危确认续跑用，不产生新版本）。
        :return: 目标非法时返回 False
        """
        db = SessionLocal()
        try:
            session = self._get_or_create_session(db, user_id, agent_id, session_id, metadata)
            row = (
                db.query(ChatMessageRow).filter(ChatMessageRow.id == int(target_ai_id)).first()
            )
            if not row or row.session_ref_id != session.id or row.message_type != "ai":
                return False
            fields = self._parse_extra(extra)
            preview = _cap_preview(msg_content_to_str(getattr(ai_message, "content", "")))
            row.content = preview
            row.content_json = serialize_message_envelope(ai_message)
            row.timestamp = datetime.utcnow()
            row.rag_trace = fields["rag_trace"]
            row.rag_steps = fields["rag_steps"]
            row.error_text = fields["error_text"]
            row.image_references = fields["image_references"]
            row.sources = fields["sources"]
            row.thinking_text = fields["thinking_text"]
            row.thinking_items = fields["thinking_items"]
            self._commit_and_refresh_caches(db, session, user_id, agent_id, session_id)
            return True
        finally:
            db.close()

    def select_branch(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        assistant_id: int,
    ) -> list[dict] | None:
        """
        切换助手消息版本：把目标 AI 设为其父用户消息的选中分支。
        :return: 切换后的当前路径记录；目标非法或会话不存在时返回 None
        """
        db = SessionLocal()
        try:
            session = self._session_query(db, user_id, agent_id, session_id).with_for_update().first()
            if not session:
                return None
            row = (
                db.query(ChatMessageRow).filter(ChatMessageRow.id == int(assistant_id)).first()
            )
            if (
                not row
                or row.session_ref_id != session.id
                or row.message_type != "ai"
                or row.parent_id is None
            ):
                return None
            parent = (
                db.query(ChatMessageRow).filter(ChatMessageRow.id == row.parent_id).first()
            )
            if not parent or parent.message_type != "human":
                return None
            parent.selected_child_id = row.id
            self._commit_and_refresh_caches(db, session, user_id, agent_id, session_id)
            return self.get_session_messages(user_id, agent_id, session_id)
        finally:
            db.close()

    def get_regenerate_context(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        target_ai_id: int,
    ) -> tuple[list, str, list[int]] | None:
        """
        重新生成上下文：(根到目标 AI 的父用户消息为止的 LangChain 消息列表, 该用户消息纯文本, 对应行 id 列表)。
        目标非法时返回 None。
        """
        db = SessionLocal()
        try:
            session = self._session_query(db, user_id, agent_id, session_id).first()
            if not session:
                return None
            rows = (
                db.query(ChatMessageRow)
                .filter(ChatMessageRow.session_ref_id == session.id)
                .order_by(ChatMessageRow.id.asc())
                .all()
            )
            by_id = {r.id: r for r in rows}
            target = by_id.get(int(target_ai_id))
            if not target or target.message_type != "ai" or target.parent_id is None:
                return None
            human = by_id.get(target.parent_id)
            if not human or human.message_type != "human":
                return None
            chain = []
            node = human
            seen = set()
            while node is not None and node.id not in seen:
                seen.add(node.id)
                chain.append(node)
                node = by_id.get(node.parent_id) if node.parent_id else None
            chain.reverse()
            records = [self._row_to_record(r) for r in chain]
            messages = self._to_langchain_messages(records)
            if not messages:
                return None
            return (
                messages,
                msg_content_to_str(getattr(messages[-1], "content", "")),
                [int(r["message_id"]) for r in records],
            )
        finally:
            db.close()

    def load(self, user_id: int, agent_id: int, session_id: str) -> list:
        """
        从缓存或数据库加载会话消息
        用于用户打开历史会话时，加载之前的会话消息到对话框中显示
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :return: 消息列表
        """
        cached = cache.get_json(self._messages_cache_key(user_id, agent_id, session_id))
        # 1. 检查 Redis 缓存, 如果存在则直接返回, 转换为 LangChain 消息列表
        if cached is not None:
            return self._to_langchain_messages(cached)

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话消息
        records = self.get_session_messages(user_id, agent_id, session_id)
        # 将消息列表添加到 Redis 缓存
        cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), records)
        # 将消息列表转换为 LangChain 消息列表并返回
        return self._to_langchain_messages(records)

    def load_path_with_ids(self, user_id: int, agent_id: int, session_id: str) -> list[tuple[int, object]]:
        """
        当前路径的 (消息行 id, LangChain 消息) 列表。
        行 id 即消息的稳定身份（turn_key 取用户消息行 id），供记忆归档与压缩跨分支定位。
        """
        records = self.get_session_messages(user_id, agent_id, session_id)
        out: list[tuple[int, object]] = []
        for rec in records:
            msgs = self._to_langchain_messages([rec])
            if msgs:
                out.append((int(rec.get("message_id") or 0), msgs[0]))
        return out

    def list_session_infos(self, user_id: int, agent_id: int) -> list[dict]:
        """
        获取用户与特定 agent 的所有会话信息列表
        用于用户打开智能体弹窗时，显示对话历史列表
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :return: 会话列表
        """
        cached = cache.get_json(self._sessions_cache_key(user_id, agent_id))
        # 1. 检查 Redis 缓存, 如果存在则直接返回（剔除编辑器试聊会话）
        if cached is not None:
            return [x for x in cached if not is_editor_preview_session(x.get("session_id"))]

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话信息列表
        db = SessionLocal() # 创建 PostgreSQL 数据库连接
        try:
            # 查询符合 user_id 和 agent_id 的所有会话, 并按更新时间降序排序
            sessions = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ~ChatSessionRow.session_id.like(EDITOR_PREVIEW_SESSION_PREFIX + "%"),
                )
                .order_by(desc(ChatSessionRow.updated_at), desc(ChatSessionRow.id))
                .all()
            )
            result = [self._build_session_info_dict(db, s) for s in sessions]
            # 3. 更新 Redis 缓存 (使用 _sessions_cache_key)
            # 将会话信息列表添加到 Redis 缓存
            cache.set_json(self._sessions_cache_key(user_id, agent_id), result)
            # 返回会话信息列表
            return result
        finally:
            db.close()

    def list_session_infos_paginated(
        self, user_id: int, agent_id: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        """
        分页查询会话列表（直接查库，不走全量 Redis 缓存）。
        按 updated_at 降序，同时间以 id 降序。
        不返回编辑器试聊会话（__editor_preview_ 前缀）。
        """
        db = SessionLocal()
        try:
            q = db.query(ChatSessionRow).filter(
                ChatSessionRow.user_id == user_id,
                ChatSessionRow.agent_id == agent_id,
                ~ChatSessionRow.session_id.like(EDITOR_PREVIEW_SESSION_PREFIX + "%"),
            )
            total = q.count()
            rows = (
                q.order_by(desc(ChatSessionRow.updated_at), desc(ChatSessionRow.id))
                .offset(offset)
                .limit(limit)
                .all()
            )
            items = [self._build_session_info_dict(db, s) for s in rows]
            return items, total
        finally:
            db.close()

    def list_session_infos_all_paginated(
        self, user_id: int, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        """
        分页查询当前用户全部智能体下的会话（不按 agent 过滤），按 updated_at 降序。
        优先读 Redis 全量列表缓存，未命中则查库并回填。
        用于用户打开侧栏「最近对话」时，显示全部智能体下的会话列表
        :param user_id: 用户 ID
        :param limit: 分页条数
        :param offset: 分页偏移
        :return: 会话列表
        """
        # 1. 检查 Redis 全量列表缓存, 如果存在则直接返回（剔除编辑器试聊会话）
        key = self._sessions_all_cache_key(user_id)
        cached = cache.get_json(key)
        if cached is not None:
            filtered = [x for x in cached if not is_editor_preview_session(x.get("session_id"))]
            # 返回分页后的会话列表
            total = len(filtered)
            return filtered[offset : offset + limit], total

        # 2. 如果 Redis 全量列表缓存不存在, 则从数据库加载会话列表
        db = SessionLocal()
        try:
            q = db.query(ChatSessionRow).filter(
                ChatSessionRow.user_id == user_id,
                ~ChatSessionRow.session_id.like(EDITOR_PREVIEW_SESSION_PREFIX + "%"),
            )
            rows = (
                q.order_by(desc(ChatSessionRow.updated_at), desc(ChatSessionRow.id)).all()
            )
            items = [self._build_session_info_dict(db, s) for s in rows]
            # 3. 更新 Redis 全量列表缓存 (使用 _sessions_all_cache_key)
            # 将会话列表添加到 Redis 全量列表缓存
            cache.set_json(key, items)
            total = len(items)
            # 返回分页后的会话列表
            return items[offset : offset + limit], total
        finally:
            db.close()

    def get_session_messages(self, user_id: int, agent_id: int, session_id: str) -> list[dict]:
        """
        从数据库获取指定会话的所有消息记录
        用于在加载会话消息时, 如果 Redis 缓存不存在, 则从数据库加载会话消息
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :return: 消息列表
        """
        # 1. 检查 Redis 缓存, 如果存在则直接返回
        cached = cache.get_json(self._messages_cache_key(user_id, agent_id, session_id))
        if cached is not None:
            return cached 

        # 2. 如果 Redis 缓存不存在, 则从数据库加载会话消息
        db = SessionLocal() # 创建 PostgreSQL 数据库连接
        try:
            # 查询符合 user_id 和 agent_id 和 session_id 的会话
            session = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ChatSessionRow.session_id == session_id,
                )
                .first()
            )
            # 如果会话不存在, 则返回空列表
            if not session: 
                return []

            result = self._load_message_records(db, session)
            cache.set_json(self._messages_cache_key(user_id, agent_id, session_id), result)
            # 返回消息列表
            return result
        finally:
            db.close()

    def delete_session(self, user_id: int, agent_id: int, session_id: str) -> bool:
        """
        删除指定会话
        用于用户删除历史会话时，删除数据库中的会话记录并更新 Redis 缓存
        :param user_id: 用户 ID
        :param agent_id: 智能体 ID
        :param session_id: 会话 ID
        :return: 是否删除成功
        """
        db = SessionLocal()
        try:
            # 查询符合 user_id 和 agent_id 和 session_id 的会话
            session = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                    ChatSessionRow.session_id == session_id,
                )
                .first()
            )
            # 如果会话不存在, 则返回 False
            if not session:
                return False

            try:
                from app.chat.attachment_service import purge_attachments_for_session

                purge_attachments_for_session(user_id, agent_id, session_id)
            except Exception:
                pass

            # 删除会话（级联删除消息与分段摘要）
            db.delete(session)
            # 提交事务到数据库
            db.commit()
            # 删除 Redis 缓存 (使用 _messages_cache_key)
            cache.delete(self._messages_cache_key(user_id, agent_id, session_id))
            # 删除 Redis 缓存 (使用 _sessions_cache_key)
            cache.delete(self._sessions_cache_key(user_id, agent_id))
            cache.delete(self._sessions_all_cache_key(user_id))
            return True
        finally:
            db.close()

    def purge_chat_data_for_agent(self, user_id: int, agent_id: int) -> int:
        """
        删除某用户某智能体下全部会话、消息、附件与相关 Redis 缓存。
        :return: 删除的会话条数
        """
        from app.chat.attachment_service import purge_attachments_for_agent

        db = SessionLocal()
        session_ids: list[str] = []
        try:
            sessions = (
                db.query(ChatSessionRow)
                .filter(
                    ChatSessionRow.user_id == user_id,
                    ChatSessionRow.agent_id == agent_id,
                )
                .all()
            )
            session_ids = [s.session_id for s in sessions]
            for s in sessions:
                db.delete(s)
            db.commit()
        finally:
            db.close()

        try:
            purge_attachments_for_agent(user_id, agent_id)
        except Exception:
            pass

        for sid in session_ids:
            cache.delete(self._messages_cache_key(user_id, agent_id, sid))
        cache.delete(self._sessions_cache_key(user_id, agent_id))
        cache.delete(self._sessions_all_cache_key(user_id))
        return len(session_ids)

    def purge_idle_sessions(
        self, before: datetime, *, batch: int = 200, dry_run: bool = False
    ) -> dict:
        """清理 updated_at < before 的闲置会话（硬删：级联消息/分段摘要，并清理附件与缓存）。

        :param dry_run: 只统计不删除
        :return: {"scanned": n, "deleted": n, "sessions": ["user:agent:session", ...]}
        """
        out: dict = {"scanned": 0, "deleted": 0, "sessions": []}
        db = SessionLocal()
        try:
            rows = (
                db.query(
                    ChatSessionRow.user_id,
                    ChatSessionRow.agent_id,
                    ChatSessionRow.session_id,
                )
                .filter(ChatSessionRow.updated_at < before)
                .order_by(ChatSessionRow.updated_at.asc())
                .limit(max(1, int(batch)))
                .all()
            )
        finally:
            db.close()
        items = [(int(r[0]), int(r[1]), str(r[2])) for r in rows]
        out["scanned"] = len(items)
        out["sessions"] = [f"{u}:{a}:{s}" for u, a, s in items]
        if dry_run:
            return out
        for uid, aid, sid in items:
            try:
                if self.delete_session(uid, aid, sid):
                    out["deleted"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("purge_idle_sessions failed session=%s", sid)
        return out

    def purge_orphan_chat_data(self, existing_agent_ids: set[int]) -> int:
        """
        清理 agent_id 已不在智能体表中的会话与附件。
        :param existing_agent_ids: 仍存在的智能体 ID
        :return: 清理涉及的 (user_id, agent_id) 组数
        """
        db = SessionLocal()
        try:
            sq = db.query(ChatSessionRow.user_id, ChatSessionRow.agent_id)
            aq = db.query(ChatAttachmentRow.user_id, ChatAttachmentRow.agent_id)
            if existing_agent_ids:
                ids = list(existing_agent_ids)
                sq = sq.filter(~ChatSessionRow.agent_id.in_(ids))
                aq = aq.filter(~ChatAttachmentRow.agent_id.in_(ids))
            pairs = {(int(r.user_id), int(r.agent_id)) for r in sq.distinct().all()}
            pairs |= {(int(r.user_id), int(r.agent_id)) for r in aq.distinct().all()}
        finally:
            db.close()
        for uid, aid in pairs:
            self.purge_chat_data_for_agent(uid, aid)
        return len(pairs)


    def get_session_metadata(self, user_id: int, agent_id: int, session_id: str) -> dict:
        """读取会话 metadata_json；会话不存在时返回空 dict。"""
        db = SessionLocal()
        try:
            session = self._session_query(db, user_id, agent_id, session_id).first()
            if not session:
                return {}
            meta = session.metadata_json
            return dict(meta) if isinstance(meta, dict) else {}
        finally:
            db.close()

    def get_session_ref_id(self, user_id: int, agent_id: int, session_id: str) -> int | None:
        """会话行主键（分段摘要表的外键）；会话不存在时返回 None，不创建。"""
        db = SessionLocal()
        try:
            row = (
                self._session_query(db, user_id, agent_id, session_id)
                .with_entities(ChatSessionRow.id)
                .first()
            )
            return int(row[0]) if row else None
        finally:
            db.close()

    def get_session_meta_and_ref(
        self, user_id: int, agent_id: int, session_id: str
    ) -> tuple[dict, int | None]:
        """一次查询同时取 metadata_json 与会话行主键（压缩/归档链路每轮都要两者）。"""
        db = SessionLocal()
        try:
            row = (
                self._session_query(db, user_id, agent_id, session_id)
                .with_entities(ChatSessionRow.id, ChatSessionRow.metadata_json)
                .first()
            )
            if not row:
                return {}, None
            meta = row[1]
            return (dict(meta) if isinstance(meta, dict) else {}), int(row[0])
        finally:
            db.close()

    def mutate_session_metadata(self, user_id: int, agent_id: int, session_id: str, mutate) -> dict:
        """在会话行锁内做 read-modify-write，杜绝「先读后写」的并发丢更新。

        请求线程与其它写入方可能同时改同一个键（如压缩熔断计数、校准系数）；
        patch_session_metadata 只在键级别原子，值仍需调用方自己合并，故提供本方法。

        :param mutate: callable(meta: dict) -> dict | None，返回要合并的 patch；返回 None/空则不写
        :return: 写入后的完整 metadata（未写时返回锁内读到的快照）
        """
        from sqlalchemy.orm.attributes import flag_modified

        db = SessionLocal()
        try:
            session = self._get_or_create_session(db, user_id, agent_id, session_id)
            meta = dict(session.metadata_json) if isinstance(session.metadata_json, dict) else {}
            patch = mutate(meta) if callable(mutate) else None
            if patch:
                meta.update(patch)
                session.metadata_json = meta
                flag_modified(session, "metadata_json")
                db.commit()
            return meta
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def patch_session_metadata(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        patch: dict,
    ) -> None:
        """合并写入会话 metadata_json（不覆盖未出现在 patch 中的键）。

        注意：本方法只在**键级别**原子（行锁内 update），patch 的值是调用方算好的。
        若值来自「先 get_session_metadata 读一遍再改」，两个并发写入者仍会互相覆盖
        （历史 bug：并发写入同一键导致丢更新）。
        凡是 read-modify-write 一律用 mutate_session_metadata。
        """
        from sqlalchemy.orm.attributes import flag_modified

        if not patch:
            return
        db = SessionLocal()
        try:
            session = self._get_or_create_session(db, user_id, agent_id, session_id)
            meta = dict(session.metadata_json) if isinstance(session.metadata_json, dict) else {}
            meta.update(patch)
            session.metadata_json = meta
            flag_modified(session, "metadata_json")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


storage = ConversationStorage()
