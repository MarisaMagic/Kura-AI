"""
对话持久化: PostgreSQL + Redis (按 user_id + agent_id + session_id)
"""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import ChatAttachment as ChatAttachmentRow
from app.chat.db_models import ChatMessage as ChatMessageRow
from app.chat.db_models import ChatSession as ChatSessionRow
from app.chat.message_codec import envelope_to_langchain_message, msg_content_to_str, serialize_message_envelope
from app.chat.preview_session import (
    EDITOR_PREVIEW_SESSION_PREFIX,
    is_editor_preview_session,
)


class ConversationStorage:
    def _build_session_info_dict(self, db, s: ChatSessionRow) -> dict:
        """
        由会话 ORM 行生成列表项（预览与计数），供全量列表与分页列表复用。
        """
        count = db.query(ChatMessageRow).filter(ChatMessageRow.session_ref_id == s.id).count()
        last_human = (
            db.query(ChatMessageRow)
            .filter(
                ChatMessageRow.session_ref_id == s.id,
                ChatMessageRow.message_type == "human",
            )
            .order_by(desc(ChatMessageRow.id))
            .first()
        )
        preview = ""
        if last_human:
            if last_human.content_json and isinstance(last_human.content_json, dict):
                env = last_human.content_json
                lc = env.get("lc")
                preview = msg_content_to_str(lc).strip()
            elif (last_human.content or "").strip():
                preview = (last_human.content or "").strip()
            preview = preview.replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "…"
        return {
            "session_id": s.session_id,
            "agent_id": s.agent_id,
            "updated_at": s.updated_at.isoformat(),
            "message_count": count,
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
    def _parse_extra(extra: dict | None) -> dict:
        """从 extra_message_data 项提取可落库字段（kb_preselect 无对应列，与旧 save 一样不入库）。"""
        extra = extra or {}
        error_text = None
        raw_err = extra.get("error_text")
        if raw_err is not None:
            error_text = str(raw_err).strip() or None
        return {
            "rag_trace": extra.get("rag_trace"),
            "rag_steps": extra.get("rag_steps"),
            "error_text": error_text,
            "image_references": extra.get("image_references"),
            "sources": extra.get("sources"),
            "thinking_text": extra.get("thinking_text"),
            "thinking_items": extra.get("thinking_items"),
        }

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

    def _insert_message_row(self, db, session: ChatSessionRow, msg, extra: dict | None, now: datetime) -> None:
        fields = self._parse_extra(extra)
        envelope = serialize_message_envelope(msg)
        preview = msg_content_to_str(getattr(msg, "content", ""))
        if len(preview) > 65500:
            preview = preview[:65500] + "…"
        db.add(
            ChatMessageRow(
                session_ref_id=session.id,
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
        )

    def _load_message_records(self, db, session: ChatSessionRow) -> list[dict]:
        rows = (
            db.query(ChatMessageRow)
            .filter(ChatMessageRow.session_ref_id == session.id)
            .order_by(ChatMessageRow.id.asc())
            .all()
        )
        return [self._row_to_record(row) for row in rows]

    def _commit_and_refresh_caches(
        self,
        db,
        session: ChatSessionRow,
        user_id: int,
        agent_id: int,
        session_id: str,
    ) -> None:
        """提交消息变更后按库回填消息缓存，并失效会话列表缓存。"""
        session.updated_at = datetime.utcnow()
        session_pk = session.id
        db.commit()
        db.expire_all()
        session = db.query(ChatSessionRow).filter(ChatSessionRow.id == session_pk).first()
        records = self._load_message_records(db, session) if session else []
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
            session = self._get_or_create_session(db, user_id, agent_id, session_id, metadata)
            now = datetime.utcnow()
            extras = extra_message_data or []
            for idx, msg in enumerate(new_messages):
                extra = extras[idx] if idx < len(extras) else None
                self._insert_message_row(db, session, msg, extra, now)
            self._commit_and_refresh_caches(db, session, user_id, agent_id, session_id)
        finally:
            db.close()

    def replace_trailing_assistant(
        self,
        user_id: int,
        agent_id: int,
        session_id: str,
        ai_message,
        extra: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        重新生成：删除末尾连续的 ai 行后插入新的助手消息（带新 extras），更早轮次不动。
        """
        db = SessionLocal()
        try:
            session = self._get_or_create_session(db, user_id, agent_id, session_id, metadata)
            rows = (
                db.query(ChatMessageRow)
                .filter(ChatMessageRow.session_ref_id == session.id)
                .order_by(ChatMessageRow.id.desc())
                .all()
            )
            trailing: list[ChatMessageRow] = []
            for row in rows:
                if row.message_type == "ai":
                    trailing.append(row)
                else:
                    break
            for row in trailing:
                db.delete(row)
            self._insert_message_row(db, session, ai_message, extra, datetime.utcnow())
            self._commit_and_refresh_caches(db, session, user_id, agent_id, session_id)
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

            # 删除 Milvus 中本会话的记忆向量（PG 会话行删除后无法再查 session_id）
            try:
                from app.chat.memory_archive import purge_session_memory_vectors

                purge_session_memory_vectors(user_id, agent_id, session_id)
            except Exception:
                pass

            try:
                from app.chat.attachment_service import purge_attachments_for_session

                purge_attachments_for_session(user_id, agent_id, session_id)
            except Exception:
                pass

            # 删除会话（级联删除消息与 mg_chat_memory_cursor）
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
        删除某用户某智能体下全部会话、消息、记忆向量、附件与相关 Redis 缓存。
        :return: 删除的会话条数
        """
        from app.chat.attachment_service import purge_attachments_for_agent
        from app.chat.memory_archive import purge_session_memory_vectors

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
                try:
                    purge_session_memory_vectors(user_id, agent_id, s.session_id)
                except Exception:
                    pass
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


storage = ConversationStorage()
