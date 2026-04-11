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
        }

    @staticmethod
    def _cache_key(chunk_id: str) -> str:
        """生成知识库父级分块的缓存键"""
        return f"kb_parent_chunk:{chunk_id}"

    def upsert_documents(self, docs: List[dict]) -> int:
        """批量插入或更新知识库父级分块"""
        if not docs:
            return 0
        # 创建 PostgreSQL 会话
        db = SessionLocal()
        upserted = 0
        try:
            for doc in docs:
                chunk_id = (doc.get("chunk_id") or "").strip()
                if not chunk_id:
                    continue
                kb_scope = (doc.get("kb_scope") or "").strip()
                # 查询知识库父级分块是否存在
                record = db.query(KbParentChunk).filter(KbParentChunk.chunk_id == chunk_id).first()
                # 构建知识库父级分块的负载
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
                    "updated_at": datetime.utcnow(),
                }
                cache_payload = {**payload, "chunk_id": chunk_id}
                # 如果知识库父级分块存在，则更新知识库父级分块
                if record:
                    for k, v in payload.items():
                        setattr(record, k, v)
                # 如果知识库父级分块不存在，则在 PostgreSQL 中创建知识库父级分块
                else:
                    db.add(KbParentChunk(chunk_id=chunk_id, **payload))
                # 缓存知识库父级分块
                cache.set_json(self._cache_key(chunk_id), cache_payload)
                # 更新插入数量
                upserted += 1
            # 提交事务
            db.commit()
        finally:
            db.close()
        return upserted

    def get_documents_by_ids(self, chunk_ids: List[str]) -> List[dict]:
        """根据 chunk_ids 获取知识库父级分块"""
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
        """根据 kb_scope 删除知识库父级分块"""
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
        """根据 kb_scope 和 filename 删除知识库父级分块"""
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
