"""父级分块：PostgreSQL + Redis 缓存。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import KbParentChunk


class ParentChunkStore:
    @staticmethod
    def _to_dict(item: KbParentChunk) -> dict[str, Any]:
        """将 KbParentChunk 对象转换为字典"""
        return {
            "kb_scope": item.kb_scope,
            "text": item.text,
            "filename": item.filename,
            "file_type": item.file_type,
            "file_path": item.file_path,
            "page_number": item.page_number,
            "chunk_id": item.chunk_id,
            "parent_chunk_id": item.parent_chunk_id,
            "root_chunk_id": item.root_chunk_id,
            "chunk_level": item.chunk_level,
            "chunk_idx": item.chunk_idx,
            "block_type": item.block_type or "text",
            "code_language": item.code_language or "",
        }

    @staticmethod
    def _cache_key(chunk_id: str) -> str:
        """生成知识库父级分块的缓存键"""
        return f"kb_parent_chunk:{chunk_id}"

    def upsert_documents(self, docs: List[dict]) -> int:
        """
        批量插入或更新知识库父级分块。

        优化（阶段 3）：一次 IN 查询定位已有行（替代逐条 SELECT）、pipeline 批量写缓存
        （替代逐条 set_json）、单事务单 commit。

        :param docs: 知识库父级分块列表
        :return: 插入或更新的父块数量
        """
        if not docs:
            return 0
        # 归一化并去重：同 chunk_id 后者覆盖前者（与逐条 upsert 的最终态一致）
        normalized: dict[str, dict] = {}
        for doc in docs:
            chunk_id = (doc.get("chunk_id") or "").strip()
            if not chunk_id:
                continue
            normalized[chunk_id] = doc
        if not normalized:
            return 0

        db = SessionLocal()
        try:
            existing = {
                row.chunk_id: row
                for row in db.query(KbParentChunk)
                .filter(KbParentChunk.chunk_id.in_(list(normalized.keys())))
                .all()
            }
            cache_items: dict[str, Any] = {}
            upserted = 0
            for chunk_id, doc in normalized.items():
                kb_scope = (doc.get("kb_scope") or "").strip()
                payload = {
                    "kb_scope": kb_scope,
                    "text": doc.get("text", ""),
                    "filename": doc.get("filename", ""),
                    "file_type": doc.get("file_type", ""),
                    "file_path": doc.get("file_path", ""),
                    "page_number": int(doc.get("page_number", 0) or 0),
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    "chunk_level": int(doc.get("chunk_level", 0) or 0),
                    "chunk_idx": int(doc.get("chunk_idx", 0) or 0),
                    "block_type": str(doc.get("block_type") or "text")[:20],
                    "code_language": str(doc.get("code_language") or "")[:40],
                    "updated_at": datetime.utcnow(),
                }
                cache_payload = {**payload, "chunk_id": chunk_id}
                record = existing.get(chunk_id)
                if record:
                    for k, v in payload.items():
                        setattr(record, k, v)
                else:
                    db.add(KbParentChunk(chunk_id=chunk_id, **payload))
                # 缓存晚于 commit 批量写（set_json_many 一次 pipeline 往返）
                cache_payload.pop("updated_at", None)
                cache_items[self._cache_key(chunk_id)] = cache_payload
                upserted += 1
            # 提交事务
            db.commit()
            cache.set_json_many(cache_items)
            return upserted
        finally:
            db.close()

    def get_documents_by_ids(self, chunk_ids: List[str]) -> List[dict]:
        """
        根据 chunk_ids 获取知识库父级分块
        :param chunk_ids: 分块ID列表
        :return: 知识库父级分块列表
        """
        if not chunk_ids:
            return []
        ordered: dict[str, dict] = {}
        missing: list[str] = []
        for cid in chunk_ids:
            # 获取知识库父级分块的缓存键
            key = (cid or "").strip()
            if not key:
                continue
            # 获取知识库父级分块的缓存
            cached = cache.get_json(self._cache_key(key))
            if cached:
                ordered[key] = cached
            # 如果知识库父级分块不存在，则从 PostgreSQL 中获取知识库父级分块
            else:
                missing.append(key)
        if missing:
            # 创建 PostgreSQL 会话
            db = SessionLocal()
            try:
                rows = db.query(KbParentChunk).filter(KbParentChunk.chunk_id.in_(missing)).all()
                # 将知识库父级分块转换为字典
                for row in rows:
                    payload = self._to_dict(row)
                    ordered[row.chunk_id] = payload
                    # 缓存知识库父级分块
                    cache.set_json(self._cache_key(row.chunk_id), payload)
            finally:
                db.close()
        return [ordered[i] for i in chunk_ids if i in ordered]

    def delete_by_kb_scope(self, kb_scope: str) -> int:
        """
        根据 kb_scope 删除知识库父级分块
        :param kb_scope: 知识库范围
        :return: 删除的父块数量
        """
        if not kb_scope:
            return 0
        db = SessionLocal()
        # 创建 PostgreSQL 会话
        try:
            rows = db.query(KbParentChunk).filter(KbParentChunk.kb_scope == kb_scope).all()
            # 获取知识库父级分块的 ID
            ids = [r.chunk_id for r in rows]
            # 如果知识库父级分块不存在，则返回 0
            if not ids:
                return 0
            # 删除知识库父级分块
            n = db.query(KbParentChunk).filter(KbParentChunk.kb_scope == kb_scope).delete(synchronize_session=False)
            # 提交事务
            db.commit()
            # 删除缓存
            for cid in ids:
                cache.delete(self._cache_key(cid))
            return int(n)
        finally:
            db.close()

    def delete_by_kb_scope_and_filename(self, kb_scope: str, filename: str) -> int:
        """
        根据 kb_scope 和 filename 删除知识库父级分块
        :param kb_scope: 知识库范围
        :param filename: 文件名
        :return: 删除的父块数量
        """
        if not kb_scope or not filename:
            return 0
        db = SessionLocal()
        # 创建 PostgreSQL 会话
        try:
            rows = (
                db.query(KbParentChunk)
                .filter(KbParentChunk.kb_scope == kb_scope, KbParentChunk.filename == filename)
                .all()
            )
            # 获取知识库父级分块的 ID
            ids = [r.chunk_id for r in rows]
            # 如果知识库父级分块不存在，则返回 0
            if not ids:
                return 0
            # 删除知识库父级分块
            db.query(KbParentChunk).filter(
                KbParentChunk.kb_scope == kb_scope,
                KbParentChunk.filename == filename,
            ).delete(synchronize_session=False)
            # 提交事务
            db.commit()
            # 删除缓存
            for cid in ids:
                cache.delete(self._cache_key(cid))
            return len(ids)
        finally:
            db.close()
