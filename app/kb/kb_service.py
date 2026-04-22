"""知识库：上传、列表、删除、按智能体清空。"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.chat.database import SessionLocal
from app.chat.db_models import KbDocument
from app.kb.image_store import get_image_store
from app.kb.kb_scope import kb_scope_for
from app.kb.milvus_client import MilvusManager, milvus_escape
from app.kb.multimodal_document_loader import MultimodalDocumentLoader
from app.kb.multimodal_milvus_writer import MultimodalMilvusWriter
from app.kb.parent_chunk_store import ParentChunkStore
from app.settings import settings

_multimodal_loader = MultimodalDocumentLoader()
_milvus = MilvusManager()
_multimodal_writer = MultimodalMilvusWriter(milvus_manager=_milvus)
_parent = ParentChunkStore()
_image_store = get_image_store()


def agent_kb_directory(user_id: int, agent_id: int) -> Path:
    """
    每个智能体知识库文档存储的根目录
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :return: 智能体知识库文档根目录（data/user_agent_docs/user_{id}/{agent_id}/）
    """
    root = Path(settings.USER_AGENT_KB_DOCS_ROOT)
    return root / f"user_{user_id}" / str(agent_id)


def normalize_display_filename(raw: str) -> str:
    """
    规范化展示文件名
    :param raw: 原始文件名
    :return: 规范化后的文件名
    """
    name = os.path.basename((raw or "").strip()) or "unnamed"
    if len(name) > 500:
        name = name[:500]
    return name


def allowed_upload_extension(filename: str) -> bool:
    """
    允许上传的文件扩展名, 支持 PDF、Word、Excel 文档
    :param filename: 文件名
    :return: 是否允许上传
    """
    fl = filename.lower()
    return fl.endswith(".pdf") or fl.endswith((".docx", ".doc")) or fl.endswith((".xlsx", ".xls"))


def purge_kb_for_scope(kb_scope: str, user_id: int, agent_id: int) -> None:
    """
    删除单个智能体的知识库全部向量、父块、元数据与磁盘文件。
    :param kb_scope: 知识库范围（用户ID + 智能体ID）
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :return: None
    """
    _milvus.init_collection()
    esc = milvus_escape(kb_scope)
    # 1. 删除 Milvus 中（用户ID + 智能体ID）对应知识库的全部向量
    try:
        _milvus.delete(f'kb_scope == "{esc}"')
    except Exception:
        pass
    # 2. 删除 PostgreSQL 中（用户ID + 智能体ID）对应知识库的全部父块（L1/L2）
    _parent.delete_by_kb_scope(kb_scope)
    # 3. 删除数据库中（用户ID + 智能体ID）对应知识库的全部文档的元数据
    db = SessionLocal()
    try:
        db.query(KbDocument).filter(KbDocument.kb_scope == kb_scope).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    # 4. 删除图片
    _image_store.delete_images_by_kb_scope(kb_scope)
    # 5. 删除磁盘文件
    d = agent_kb_directory(user_id, agent_id)
    if d.is_dir():
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def delete_kb_document(kb_scope: str, user_id: int, agent_id: int, display_filename: str) -> bool:
    """
    删除单个智能体的知识库中的单个文档的向量、父块、元数据与磁盘文件。
    :param kb_scope: 知识库范围（用户ID + 智能体ID）
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param display_filename: 展示文件名
    :return: 是否删除成功
    """
    esc = milvus_escape(kb_scope) # 转义知识库范围
    fn_esc = milvus_escape(display_filename) # 转义展示文件名
    _milvus.init_collection() # 初始化 Milvus 集合
    # 1. 删除 Milvus 中（用户ID + 智能体ID）对应知识库的单个文档的向量
    try:
        _milvus.delete(f'kb_scope == "{esc}" && filename == "{fn_esc}"')
    except Exception:
        pass
    # 2. 删除 PostgreSQL 中（用户ID + 智能体ID）对应知识库的单个文档的元数据
    _parent.delete_by_kb_scope_and_filename(kb_scope, display_filename)
    # 3. 删除 PostgreSQL 中（用户ID + 智能体ID）对应知识库的单个文档的元数据
    db = SessionLocal() # 创建 PostgreSQL 会话
    try:
        row = (
            db.query(KbDocument)
            .filter(
                KbDocument.kb_scope == kb_scope,
                KbDocument.display_filename == display_filename,
            )
            .first()
        )
        stored = row.stored_filename if row else None
        db.query(KbDocument).filter(
            KbDocument.kb_scope == kb_scope,
            KbDocument.display_filename == display_filename,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    # 4. 删除图片
    _image_store.delete_images_by_document(kb_scope, display_filename)
    # 5. 删除对应的磁盘文件
    if stored:
        p = agent_kb_directory(user_id, agent_id) / stored
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass
    return True


def fetch_kb_document_list(kb_scope: str) -> list[dict]:
    """
    获取单个智能体的知识库中的全部文档的元数据
    :param kb_scope: 知识库范围（用户ID + 智能体ID）
    :return: 文档列表
    """
    db = SessionLocal() # 创建 PostgreSQL 会话
    try:
        # 获取（用户ID + 智能体ID）对应知识库的全部文档的元数据
        rows = (
            db.query(KbDocument)
            .filter(KbDocument.kb_scope == kb_scope)
            .order_by(KbDocument.updated_at.desc())
            .all()
        )
        # 返回文档列表
        return [
            {
                "display_filename": r.display_filename,
                "file_type": r.file_type,
                "chunk_count": r.chunk_count,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


async def ingest_upload(
    kb_scope: str,
    user_id: int,
    agent_id: int,
    display_filename: str,
    file: UploadFile,
) -> dict:
    """
    上传并入库单个智能体的知识库中的单个文档的向量、父块、元数据与磁盘文件。
    :param kb_scope: 知识库范围（用户ID + 智能体ID）
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param display_filename: 展示文件名
    :param file: 上传文件
    :return: 文档信息
    """
    # 1. 检查文件扩展名 和 向量嵌入 API 密钥配置
    if not allowed_upload_extension(display_filename):
        raise ValueError("仅支持 PDF、Word、Excel 文档")
    if not (settings.EMBEDDING_API_KEY or "").strip():
        raise ValueError("未配置 EMBEDDING_API_KEY，无法生成向量")

    ddir = agent_kb_directory(user_id, agent_id) # 获取智能体知识库文档根目录
    ddir.mkdir(parents=True, exist_ok=True) # 创建智能体知识库文档根目录

    stored = f"{uuid.uuid4().hex}_{normalize_display_filename(display_filename).replace('/', '_')}" # 生成存储文件名
    path = ddir / stored # 获取存储文件路径

    # 覆盖同展示名：先删旧数据
    delete_kb_document(kb_scope, user_id, agent_id, display_filename) # 删除旧数据

    # 2. 读取文件内容并写入磁盘目录
    content = await file.read() # 读取文件内容
    path.write_bytes(content) # 写入文件内容

    try:
        # 使用多模态文档加载器（支持图片提取）
        chunks = _multimodal_loader.load_document(str(path), display_filename, kb_scope, user_id, agent_id)
    except Exception as e:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise ValueError(f"文档处理失败: {e}") from e

    if not chunks:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise ValueError("文档处理失败，未能提取内容")

    # 3. 获取父块和叶子块（L1/L2/L3/L4）
    parent_docs = [c for c in chunks if int(c.get("chunk_level", 0) or 0) in (1, 2)]
    leaf_docs = [c for c in chunks if int(c.get("chunk_level", 0) or 0) in (3, 4)]  # L3文本块 + L4图片块
    
    if not leaf_docs:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise ValueError("未生成可检索叶子分块")

    # 统计图片和文本块数量
    text_count = len([c for c in leaf_docs if c.get("content_type") == "text"])
    image_count = len([c for c in leaf_docs if c.get("content_type") == "image"])

    # 4. 初始化 Milvus 集合、批量插入或更新父块、批量写入叶子块到 Milvus 集合
    _milvus.init_collection()
    _parent.upsert_documents(parent_docs) # 批量插入或更新父块  
    _multimodal_writer.write_documents(leaf_docs) # 批量写入叶子块（包括图片）到 Milvus 集合

    # 5. 插入或更新 PostgreSQL 中（用户ID + 智能体ID）对应知识库的单个文档的元数据
    ft = (leaf_docs[0].get("file_type") or "") if leaf_docs else ""
    db = SessionLocal() # 创建 PostgreSQL 会话
    try:
        rec = KbDocument(
            kb_scope=kb_scope,
            display_filename=display_filename,
            stored_filename=stored,
            file_type=ft,
            chunk_count=len(leaf_docs),
        )
        db.add(rec)
        db.commit()
    finally:
        db.close()

    return {
        "display_filename": display_filename,
        "chunk_count": len(leaf_docs),
        "parent_chunks": len(parent_docs),
        "text_chunks": text_count,
        "image_chunks": image_count,
    }
