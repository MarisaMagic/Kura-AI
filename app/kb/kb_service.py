"""知识库：上传、列表、删除、按智能体清空。"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import KbDocument, KbParentChunk
from app.kb.image_store import get_image_store
from app.kb.kb_scope import kb_scope_for
from app.kb.milvus_client import MilvusManager, milvus_escape
from app.kb.multimodal_document_loader import MultimodalDocumentLoader, _filename_fingerprint
from app.kb.multimodal_milvus_writer import MultimodalMilvusWriter
from app.kb.parent_chunk_store import ParentChunkStore
from app.settings import settings

_multimodal_loader = MultimodalDocumentLoader()
_milvus = MilvusManager()
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


def delete_kb_document(
    kb_scope: str,
    user_id: int,
    agent_id: int,
    display_filename: str,
    milvus_manager: MilvusManager | None = None,
) -> bool:
    """
    删除单个智能体的知识库中的单个文档的向量、父块、元数据与磁盘文件。
    :param kb_scope: 知识库范围（用户ID + 智能体ID）
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param display_filename: 展示文件名
    :param milvus_manager: 指定 Milvus 管理器（上传任务线程传入专用实例，避免跨线程共用单例）；None 用模块级单例
    :return: 是否删除成功
    """
    mv = milvus_manager or _milvus
    esc = milvus_escape(kb_scope) # 转义知识库范围
    fn_esc = milvus_escape(display_filename) # 转义展示文件名
    mv.init_collection() # 初始化 Milvus 集合
    # 1. 删除 Milvus 中（用户ID + 智能体ID）对应知识库的单个文档的向量
    try:
        mv.delete(f'kb_scope == "{esc}" && filename == "{fn_esc}"')
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


class KbUploadTaskCancelled(Exception):
    """上传任务被用户主动取消（协作式，在批处理边界抛出）。"""


class KbUploadTaskTimeout(Exception):
    """上传任务超过整体时长上限被中止。"""


class KbUploadTaskGuard:
    """
    协作式中止检查点：在解析/嵌入/写入的批处理边界调用 checkpoint()。
    进行中的单次嵌入 HTTP 调用无法打断，由 KB_UPLOAD_EMBEDDING_HTTP_TIMEOUT_SECONDS 短超时兜底。
    """

    def __init__(
        self,
        is_cancelled: Callable[[], bool] | None = None,
        deadline: float | None = None,  # time.monotonic() 刻度；None 不限时
    ) -> None:
        self._is_cancelled = is_cancelled or (lambda: False)
        self._deadline = deadline

    def checkpoint(self) -> None:
        """超时/取消时抛出对应异常中止处理。"""
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise KbUploadTaskTimeout()
        if self._is_cancelled():
            raise KbUploadTaskCancelled()


def _document_images_dir(user_id: int, agent_id: int, display_filename: str) -> Path:
    """某文档提取图片在磁盘上的目录（与 loader 的存储位置一致，用于失败时清理）。"""
    return (
        Path(settings.USER_AGENT_KB_IMAGES_ROOT)
        / f"user_{user_id}"
        / str(agent_id)
        / _filename_fingerprint(display_filename)
    )


def _swap_lock_key(kb_scope: str, display_filename: str) -> str:
    """同名文档「替换落库」阶段的互斥锁 key。"""
    return f"kb_upload_lock:{kb_scope}:{display_filename}"


def _cleanup_upload_assets(path: Path, images_dir: Path) -> None:
    """删除本次上传的临时文件与解析期提取的图片（失败/中止时调用）。"""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        shutil.rmtree(images_dir, ignore_errors=True)
    except Exception:
        pass


def run_ingest_pipeline_sync(
    *,
    kb_scope: str,
    user_id: int,
    agent_id: int,
    display_filename: str,
    content: bytes,
    progress_cb: Callable[[str, int, int], None] | None = None,
    guard: KbUploadTaskGuard | None = None,
) -> dict:
    """
    同步执行单文档入库流水线（由 kb_job 在后台工作线程调用，不阻塞事件循环）。

    「先处理后替换」语义：
    - 解析 + 全部向量生成成功之前不碰旧数据；失败/超时/取消时旧文档原样保留，仅清理本次临时产物。
    - 成功后进入「替换落库」临界区（同名文档 Redis NX 锁互斥，后完成者生效）：删旧 → 父块 → 叶子块纯插入 → 图片元数据 → PG 文档行。
    进度通过 progress_cb(stage, done, total) 上报，stage ∈ parsing/chunking/embedding/writing。

    :param kb_scope: 知识库范围
    :param user_id: 用户ID
    :param agent_id: 智能体ID
    :param display_filename: 展示文件名
    :param content: 文件内容字节
    :param progress_cb: 阶段进度回调
    :param guard: 协作式中止检查
    :return: 文档元数据字典（unchanged 表示内容未变化跳过重建）
    """
    check = guard or KbUploadTaskGuard()
    report = progress_cb or (lambda stage, done, total: None)

    if not allowed_upload_extension(display_filename):
        raise ValueError("仅支持 PDF、Word、Excel 文档")
    if not (settings.EMBEDDING_API_KEY or "").strip():
        raise ValueError("未配置 EMBEDDING_API_KEY，无法生成向量")

    check.checkpoint()
    content_hash = hashlib.sha256(content).hexdigest()

    # 同展示名且内容未变化：直接返回现有元数据，不解析不重建
    db = SessionLocal()
    try:
        existing = (
            db.query(KbDocument)
            .filter(
                KbDocument.kb_scope == kb_scope,
                KbDocument.display_filename == display_filename,
            )
            .first()
        )
        if existing and existing.content_hash and existing.content_hash == content_hash:
            parent_chunks = (
                db.query(KbParentChunk)
                .filter(
                    KbParentChunk.kb_scope == kb_scope,
                    KbParentChunk.filename == display_filename,
                )
                .count()
            )
            return {
                "display_filename": display_filename,
                "chunk_count": existing.chunk_count or 0,
                "parent_chunks": parent_chunks,
                "unchanged": True,
            }
    finally:
        db.close()

    # 写盘并解析；此阶段失败只清理本次临时产物，旧文档不动
    ddir = agent_kb_directory(user_id, agent_id)
    ddir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}_{normalize_display_filename(display_filename).replace('/', '_')}"
    path = ddir / stored
    images_dir = _document_images_dir(user_id, agent_id, display_filename)
    path.write_bytes(content)

    report("parsing", 0, 1)
    try:
        chunks = _multimodal_loader.load_document(str(path), display_filename, kb_scope, user_id, agent_id)
    except Exception as e:
        _cleanup_upload_assets(path, images_dir)
        raise ValueError(f"文档处理失败: {e}") from e
    check.checkpoint()
    if not chunks:
        _cleanup_upload_assets(path, images_dir)
        raise ValueError("文档处理失败，未能提取内容")
    report("parsing", 1, 1)

    parent_docs = [c for c in chunks if int(c.get("chunk_level", 0) or 0) in (1, 2)]
    leaf_docs = [c for c in chunks if int(c.get("chunk_level", 0) or 0) in (3, 4)]  # L3文本块 + L4图片块
    if not leaf_docs:
        _cleanup_upload_assets(path, images_dir)
        raise ValueError("未生成可检索叶子分块")
    report("chunking", 1, 1)
    check.checkpoint()

    text_docs = [c for c in leaf_docs if c.get("content_type") == "text"]
    image_docs = [c for c in leaf_docs if c.get("content_type") == "image"]
    text_count = len(text_docs)
    image_count = len(image_docs)

    # 生成全部向量（最易超时的阶段；任务线程使用专用 Milvus 实例，避免跨线程共用单例）
    bs = max(1, int(settings.EMBEDDING_BATCH_SIZE or 10))
    job_milvus = MilvusManager()
    job_milvus.init_collection()
    job_writer = MultimodalMilvusWriter(milvus_manager=job_milvus)

    text_batches = math.ceil(len(text_docs) / bs) if text_docs else 0
    calls_total = max(1, text_batches + image_count)
    text_batch_done = 0
    images_ticked = 0

    def embed_progress(stage: str, done: int, total: int) -> None:
        nonlocal text_batch_done
        check.checkpoint()
        if stage == "text_embedding":
            # writer 的 done 是已完成文本条数，折算为批数
            text_batch_done = math.ceil(int(done) / bs)
        current = text_batch_done + (int(done) if stage == "image_embedding" else 0)
        report("embedding", current, calls_total)

    def embed_tick() -> None:
        nonlocal images_ticked
        check.checkpoint()
        images_ticked += 1
        report("embedding", text_batch_done + images_ticked, calls_total)

    try:
        text_emb, image_emb = job_writer.embed_documents(
            leaf_docs, batch_size=bs, progress_cb=embed_progress, tick_cb=embed_tick
        )
    except Exception:
        # 嵌入阶段失败：旧数据原样保留（尚未进入替换），只清理本次产物
        _cleanup_upload_assets(path, images_dir)
        raise
    check.checkpoint()

    # 替换落库临界区（同名并发上传互斥，后完成者生效；纯写入、无嵌入调用，数秒内完成）
    lock_key = _swap_lock_key(kb_scope, display_filename)
    lock_ttl = max(60, int(settings.KB_UPLOAD_SWAP_LOCK_TTL_SECONDS or 600))
    lock_acquired = cache.set_nx(lock_key, {"agent_id": agent_id}, ttl=lock_ttl)
    while not lock_acquired:
        check.checkpoint()
        time.sleep(0.2)
        lock_acquired = cache.set_nx(lock_key, {"agent_id": agent_id}, ttl=lock_ttl)

    def write_progress(stage: str, done: int, total: int) -> None:
        check.checkpoint()
        report("writing", done, total)

    try:
        delete_kb_document(kb_scope, user_id, agent_id, display_filename, milvus_manager=job_milvus)
        _parent.upsert_documents(parent_docs)
        job_writer.write_documents(
            leaf_docs,
            batch_size=bs,
            text_embeddings=text_emb,
            image_embeddings=image_emb,
            progress_cb=write_progress,
        )

        # 插入或更新 PostgreSQL 中文档的元数据
        ft = (leaf_docs[0].get("file_type") or "") if leaf_docs else ""
        db = SessionLocal()
        try:
            rec = KbDocument(
                kb_scope=kb_scope,
                display_filename=display_filename,
                stored_filename=stored,
                file_type=ft,
                chunk_count=len(leaf_docs),
                content_hash=content_hash,
            )
            db.add(rec)
            db.commit()
        finally:
            db.close()
    except Exception:
        # 临界区内失败：旧数据此刻已删，尽力清理本次半成品（窄窗口残余风险，同名重传即自愈）
        _cleanup_upload_assets(path, images_dir)
        try:
            delete_kb_document(kb_scope, user_id, agent_id, display_filename, milvus_manager=job_milvus)
        except Exception:
            pass
        raise
    finally:
        cache.delete(lock_key)

    return {
        "display_filename": display_filename,
        "chunk_count": len(leaf_docs),
        "parent_chunks": len(parent_docs),
        "text_chunks": text_count,
        "image_chunks": image_count,
        "unchanged": False,
    }
