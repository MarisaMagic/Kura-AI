"""
知识库文档上传任务：后台线程处理，进度写 Redis，前端轮询状态。
上传接口立即返回 task_id；处理在专用线程池中执行（完全并发，上限 KB_UPLOAD_MAX_PARALLEL），
绝不占用 FastAPI 事件循环 —— 阻塞问题的根治点。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger

from app.chat.cache import cache
from app.kb.kb_service import (
    KbUploadTaskCancelled,
    KbUploadTaskGuard,
    KbUploadTaskTimeout,
    run_ingest_pipeline_sync,
)
from app.settings import settings


def _meta_key(task_id: str) -> str:
    """任务元数据/进度的 Redis key（单 key 快照，轮询场景只需最新状态）。"""
    return f"kb_upload_job:{task_id}:meta"


def _cancel_key(task_id: str) -> str:
    """用户请求取消时写入的标记 key。"""
    return f"kb_upload_job:{task_id}:cancel"


def _ttl() -> int:
    """任务元数据的 Redis TTL（秒）。"""
    return int(getattr(settings, "KB_UPLOAD_JOB_TTL_SECONDS", 86400))


# stage -> (percent 区间下限, 上限)，由 done/total 在区间内线性插值
_STAGE_BANDS: dict[str, tuple[int, int]] = {
    "queued": (0, 0),
    "parsing": (0, 10),
    "chunking": (10, 12),
    "embedding": (12, 85),
    "writing": (85, 100),
    "done": (100, 100),
}


_worker_pool: ThreadPoolExecutor | None = None


def _get_pool() -> ThreadPoolExecutor:
    """知识库上传专用线程池（懒加载）；worker 线程不占用事件循环。"""
    global _worker_pool
    if _worker_pool is None:
        max_workers = max(1, int(getattr(settings, "KB_UPLOAD_MAX_PARALLEL", 8) or 8))
        _worker_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kb-upload")
    return _worker_pool


async def create_kb_upload_job(
    *,
    user_id: int,
    agent_id: int,
    kb_scope: str,
    display_filename: str,
    content: bytes,
) -> str | None:
    """
    创建上传任务并调度到后台线程池，立即返回 task_id。
    初始 meta 写入失败（Redis 不可用）时返回 None，调用方应拒绝受理，
    避免产生「无法查询进度的幽灵任务」。
    :return: task_id，或 None（任务状态初始化失败）
    """
    task_id = uuid.uuid4().hex
    meta = {
        "task_id": task_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "kb_scope": kb_scope,
        "display_filename": display_filename,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "done": None,
        "total": None,
        "error": None,
        "error_type": None,
        "result": None,
    }
    written = False
    for _attempt in range(2):
        written = bool(await asyncio.to_thread(cache.set_json, _meta_key(task_id), meta, _ttl()))
        if written:
            break
        await asyncio.sleep(0.1)
    if not written:
        logger.error(
            "知识库上传任务初始化失败（Redis 不可写）task_id={} filename={!r}",
            task_id,
            display_filename,
        )
        return None
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        _get_pool(),
        _run_upload_thread,
        task_id,
        user_id,
        agent_id,
        kb_scope,
        display_filename,
        content,
    )
    return task_id


def _update_meta(task_id: str, identity: dict[str, Any], **fields: Any) -> None:
    """
    合并更新任务元数据快照（worker 线程内直接同步调 Redis）。
    快照缺失时用创建时的身份字段完整重建——绝不生成没有 user_id 的
    半截快照（否则状态接口会因归属校验失败对该任务永久 404）。
    """
    meta = cache.get_json(_meta_key(task_id))
    if not isinstance(meta, dict):
        logger.warning("知识库上传任务 meta 快照缺失，按身份字段重建 task_id={}", task_id)
        meta = dict(identity)
    meta.update(fields)
    cache.set_json(_meta_key(task_id), meta, _ttl())


def _run_upload_thread(
    task_id: str,
    user_id: int,
    agent_id: int,
    kb_scope: str,
    display_filename: str,
    content: bytes,
) -> None:
    """流水线执行线程：进度上报 + 协作式取消/超时 + 终态落定。"""
    timeout_secs = max(1, int(getattr(settings, "KB_UPLOAD_TASK_TIMEOUT_SECONDS", 900) or 900))
    guard = KbUploadTaskGuard(
        is_cancelled=lambda: bool(cache.get_json(_cancel_key(task_id))),
        deadline=time.monotonic() + timeout_secs,
    )

    identity = {
        "task_id": task_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "kb_scope": kb_scope,
        "display_filename": display_filename,
    }

    last_percent = 0

    def progress_cb(stage: str, done: int, total: int) -> None:
        nonlocal last_percent
        lo, hi = _STAGE_BANDS.get(stage, (0, 100))
        if total and total > 0 and done >= 0:
            ratio = min(1.0, max(0.0, done / max(1, total)))
            percent = int(lo + (hi - lo) * ratio)
        else:
            percent = lo
        # 进度只前进不回退
        percent = max(percent, last_percent)
        last_percent = percent
        _update_meta(
            task_id,
            identity,
            stage=stage,
            percent=percent,
            done=done if total and total > 0 else None,
            total=total if total and total > 0 else None,
        )

    try:
        _update_meta(task_id, identity, status="running", stage="parsing", percent=0)
        result = run_ingest_pipeline_sync(
            kb_scope=kb_scope,
            user_id=user_id,
            agent_id=agent_id,
            display_filename=display_filename,
            content=content,
            progress_cb=progress_cb,
            guard=guard,
        )
        _update_meta(
            task_id,
            identity,
            status="completed",
            stage="done",
            percent=100,
            done=1,
            total=1,
            result=result,
        )
    except KbUploadTaskCancelled:
        _update_meta(task_id, identity, status="cancelled", error="用户已取消", error_type="cancelled")
    except KbUploadTaskTimeout:
        _update_meta(
            task_id,
            identity,
            status="timeout",
            error=f"处理超过 {timeout_secs} 秒已中止，请减小文件后重试",
            error_type="timeout",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("知识库上传任务失败 task_id=%s filename=%r", task_id, display_filename)
        _update_meta(task_id, identity, status="failed", error=str(e), error_type="failed")
    finally:
        cache.delete(_cancel_key(task_id))


def get_kb_upload_job_meta(task_id: str) -> dict[str, Any] | None:
    """获取任务元数据/进度快照（无则返回 None）。"""
    raw = cache.get_json(_meta_key(task_id))
    return raw if isinstance(raw, dict) else None


def is_job_cancel_requested(task_id: str) -> bool:
    """是否已请求取消该任务。"""
    return bool(cache.get_json(_cancel_key(task_id)))


async def request_kb_upload_cancel(task_id: str) -> None:
    """标记任务为「用户请求取消」，worker 在批处理边界协作式退出。"""
    await asyncio.to_thread(cache.set_json, _cancel_key(task_id), {"v": 1}, _ttl())