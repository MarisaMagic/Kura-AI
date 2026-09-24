"""
知识库文档上传任务：受理与处理解耦（进度写 Redis，前端批量轮询状态）。

- 队列模式（KB_UPLOAD_MODE=queue，生产默认）：上传接口把文件流式落对象存储 pending 区，
  写任务 meta 后 RPUSH 到 Redis 队列立即返回 task_id；worker.py（独立进程）消费队列执行
  解析/向量化/入库。页面崩溃、API 重启都不影响已受理任务。
- 内联模式（KB_UPLOAD_MODE=inline，本地开发默认）：API 进程内线程池执行（保留旧行为）。

取消标记、用户活动计数均走 Redis，跨进程生效。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger

from app.chat.cache import cache
from app.core import object_storage as obs
from app.kb.kb_service import (
    KbUploadTaskCancelled,
    KbUploadTaskGuard,
    KbUploadTaskTimeout,
    run_ingest_pipeline_sync,
)
from app.kb.multimodal_embedding import EmbeddingConcurrencyTimeoutError, EmbeddingThrottledError
from app.settings import settings

TERMINAL_STATUSES = ("completed", "failed", "timeout", "cancelled")

_QUEUE_KEY = "kb_upload_job:queue"
_PROCESSING_KEY = "kb_upload_job:processing"


def _meta_key(task_id: str) -> str:
    """任务元数据/进度的 Redis key（单 key 快照，轮询场景只需最新状态）。"""
    return f"kb_upload_job:{task_id}:meta"


def _cancel_key(task_id: str) -> str:
    """用户请求取消时写入的标记 key。"""
    return f"kb_upload_job:{task_id}:cancel"


def _source_key(task_id: str) -> str:
    """任务源文件引用 key（队列模式：对象存储 pending 对象 key）。"""
    return f"kb_upload_job:{task_id}:source"


def _user_active_key(user_id: int) -> str:
    """用户活动任务集合 key（排队 + 处理中，用于按用户限流）。"""
    return f"kb_upload_job:user_active:{int(user_id)}"


def _ttl() -> int:
    """任务元数据的 Redis TTL（秒）。"""
    return int(getattr(settings, "KB_UPLOAD_JOB_TTL_SECONDS", 86400))


def upload_mode() -> str:
    """上传受理模式：queue（独立 worker）| inline（API 进程线程池）。"""
    mode = str(getattr(settings, "KB_UPLOAD_MODE", "inline") or "inline").strip().lower()
    return mode if mode in ("queue", "inline") else "inline"


def is_terminal_status(status: Any) -> bool:
    return str(status or "") in TERMINAL_STATUSES


def new_task_id() -> str:
    """预生成任务 ID（受理方需先落 pending 对象、再写 meta 时使用）。"""
    return uuid.uuid4().hex


def pending_source_key(task_id: str, display_filename: str) -> str:
    """pending 区对象 key（worker 消费后删除）；按原扩展名保存便于解析库识别。"""
    prefix = str(getattr(settings, "KB_UPLOAD_PENDING_PREFIX", "pending-uploads") or "").strip("/")
    suffix = Path(str(display_filename or "")).suffix.lower() or ".bin"
    return obs.join_key(prefix, task_id, f"source{suffix}")


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
    """内联模式线程池（懒加载）；队列模式不使用。"""
    global _worker_pool
    if _worker_pool is None:
        max_workers = max(1, int(getattr(settings, "KB_UPLOAD_MAX_PARALLEL", 8) or 8))
        _worker_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kb-upload")
    return _worker_pool


# ---------------------------------------------------------------- 准入控制


def queue_depth() -> int:
    """排队 + 处理中的任务总数。"""
    return cache.llen(_QUEUE_KEY) + cache.llen(_PROCESSING_KEY)


def admission_reason(user_id: int) -> str | None:
    """
    受理前的背压检查；超限返回可展示的中文原因，允许则返回 None。
    队列/用户上限为 0 表示不限制（便于本地开发）。
    """
    queue_max = max(0, int(getattr(settings, "KB_UPLOAD_QUEUE_MAX", 0) or 0))
    if queue_max > 0:
        depth = queue_depth()
        if depth >= queue_max:
            return f"上传任务排队已满（{depth}/{queue_max}），请稍后再试"
    user_max = max(0, int(getattr(settings, "KB_UPLOAD_USER_MAX_ACTIVE", 0) or 0))
    if user_max > 0:
        active = cache.scard(_user_active_key(user_id))
        if active >= user_max:
            return f"您有 {active} 个文档正在排队或处理，请等待完成后再上传"
    return None


def _mark_user_active(user_id: int, task_id: str) -> None:
    ttl = max(600, int(getattr(settings, "KB_UPLOAD_USER_ACTIVE_TTL_SECONDS", 7200) or 7200))
    cache.sadd_json(_user_active_key(user_id), task_id, ttl=ttl)


def _remove_user_active(user_id: int, task_id: str) -> None:
    cache.srem_json(_user_active_key(user_id), task_id)


# ---------------------------------------------------------------- 队列原语


def enqueue_task(task_id: str) -> bool:
    payload = {"kind": "kb_upload", "task_id": task_id}
    return cache.rpush_json(_QUEUE_KEY, payload) > 0


def dequeue_task(timeout: int = 5) -> dict[str, Any] | None:
    """可靠出队：原子移入 processing 列表，任务完成后 ack；崩溃可回收重投。"""
    payload = cache.brpoplpush_json(_QUEUE_KEY, _PROCESSING_KEY, timeout)
    return payload if isinstance(payload, dict) else None


def ack_task(payload: dict[str, Any]) -> None:
    cache.lrem_json(_PROCESSING_KEY, payload)


def requeue_task(payload: dict[str, Any]) -> None:
    cache.lrem_json(_PROCESSING_KEY, payload)
    cache.rpush_json(_QUEUE_KEY, payload)


def recover_stale_processing(stale_seconds: int) -> int:
    """
    worker 启动时回收 processing 列表：
    - 任务已终态/ meta 丢失：直接清理；
    - 心跳超过 stale_seconds：重投一次（meta.recovered=True），再次超时则判失败，避免死循环。
    :return: 处理（清理/重投/判死）的条目数
    """
    now = time.time()
    recovered = 0
    for raw in cache.lrange_str(_PROCESSING_KEY, 0, -1):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            cache.lrem_raw(_PROCESSING_KEY, raw)
            recovered += 1
            continue
        task_id = str(payload.get("task_id") or "")
        meta = cache.get_json(_meta_key(task_id)) if task_id else None
        if not isinstance(meta, dict) or is_terminal_status(meta.get("status")):
            cache.lrem_raw(_PROCESSING_KEY, raw)
            recovered += 1
            continue
        updated_at = float(meta.get("updated_at") or meta.get("created_at") or 0)
        if now - updated_at < stale_seconds:
            continue
        identity = _identity_from_meta(meta)
        if meta.get("recovered"):
            _update_meta(
                task_id,
                identity,
                status="failed",
                error="处理进程中断且自动恢复已尝试，请重新上传",
                error_type="failed",
                updated_at=now,
            )
            _remove_user_active(int(meta.get("user_id") or 0), task_id)
            cache.lrem_raw(_PROCESSING_KEY, raw)
        else:
            _update_meta(
                task_id,
                identity,
                status="queued",
                stage="queued",
                percent=0,
                recovered=True,
                updated_at=now,
            )
            cache.lrem_raw(_PROCESSING_KEY, raw)
            cache.rpush_json(_QUEUE_KEY, payload)
        recovered += 1
    if recovered:
        logger.info("知识库上传任务回收完成：{} 条", recovered)
    return recovered


# ---------------------------------------------------------------- 任务创建


def _identity_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": meta.get("task_id"),
        "user_id": int(meta.get("user_id") or 0),
        "agent_id": int(meta.get("agent_id") or 0),
        "kb_scope": str(meta.get("kb_scope") or ""),
        "display_filename": str(meta.get("display_filename") or ""),
    }


async def create_kb_upload_job(
    *,
    user_id: int,
    agent_id: int,
    kb_scope: str,
    display_filename: str,
    source_key: str | None = None,
    content: bytes | None = None,
    size: int | None = None,
    task_id: str | None = None,
) -> str | None:
    """
    创建上传任务：写 meta（提交可查询性），队列模式再入队，内联模式提交线程池。
    初始 meta 写入失败（Redis 不可用）时返回 None，调用方应拒绝受理，
    避免产生「无法查询进度的幽灵任务」。
    :param source_key: 队列模式：pending 对象 key（API 已流式上传）
    :param content: 内联模式：文件内容（API 已读入内存）
    :return: task_id，或 None（任务状态初始化失败/入队失败）
    """
    tid = task_id or uuid.uuid4().hex
    now = time.time()
    meta = {
        "task_id": tid,
        "user_id": user_id,
        "agent_id": agent_id,
        "kb_scope": kb_scope,
        "display_filename": display_filename,
        "size": size,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "done": None,
        "total": None,
        "error": None,
        "error_type": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    written = False
    for _attempt in range(2):
        written = bool(await asyncio.to_thread(cache.set_json, _meta_key(tid), meta, _ttl()))
        if written:
            break
        await asyncio.sleep(0.1)
    if not written:
        logger.error(
            "知识库上传任务初始化失败（Redis 不可写）task_id={} filename={!r}",
            tid,
            display_filename,
        )
        return None

    await asyncio.to_thread(_mark_user_active, user_id, tid)

    if source_key:
        stored = await asyncio.to_thread(
            cache.set_json,
            _source_key(tid),
            {"kind": "object", "key": source_key, "filename": display_filename, "size": size},
            _ttl(),
        )
        if not stored:
            await asyncio.to_thread(cache.delete, _meta_key(tid))
            await asyncio.to_thread(_remove_user_active, user_id, tid)
            return None

    if content is None and upload_mode() == "queue":
        enqueued = await asyncio.to_thread(enqueue_task, tid)
        if not enqueued:
            logger.error("知识库上传任务入队失败（Redis 不可用）task_id={}", tid)
            await asyncio.to_thread(cache.delete, _meta_key(tid))
            await asyncio.to_thread(_remove_user_active, user_id, tid)
            return None
        return tid

    # 内联模式：API 进程线程池执行
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_get_pool(), _run_upload_with_content, tid, content)
    return tid


# ---------------------------------------------------------------- 执行核心


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


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_upload_with_content(task_id: str, content: bytes | None) -> None:
    """内联模式：字节写入本地临时文件后走统一执行核心。"""
    meta = cache.get_json(_meta_key(task_id))
    filename = str((meta or {}).get("display_filename") or "source.bin")
    suffix = Path(filename).suffix.lower() or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="kura_kb_inline_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content or b"")
        run_upload_task(task_id, tmp_path)
    finally:
        _silent_unlink(tmp_path)


def run_upload_task_from_source(task_id: str) -> None:
    """队列模式：从 pending 对象取回源文件（流式落临时文件）后执行。"""
    source = cache.get_json(_source_key(task_id))
    source_key = str((source or {}).get("key") or "")
    suffix = Path(str((source or {}).get("filename") or "")).suffix.lower() or ".bin"
    if not source_key:
        meta = cache.get_json(_meta_key(task_id))
        if isinstance(meta, dict):
            _update_meta(
                task_id,
                _identity_from_meta(meta),
                status="failed",
                error="源文件引用丢失，请重新上传",
                error_type="failed",
                updated_at=time.time(),
            )
            _remove_user_active(int(meta.get("user_id") or 0), task_id)
        return
    try:
        with obs.download_temp(source_key, suffix=suffix) as path:
            run_upload_task(task_id, path)
    except Exception as e:  # noqa: BLE001
        logger.exception("知识库上传任务源文件获取失败 task_id={}", task_id)
        meta = cache.get_json(_meta_key(task_id))
        if isinstance(meta, dict) and not is_terminal_status(meta.get("status")):
            _update_meta(
                task_id,
                _identity_from_meta(meta),
                status="failed",
                error=f"源文件读取失败：{e}",
                error_type="failed",
                updated_at=time.time(),
            )
            _remove_user_active(int(meta.get("user_id") or 0), task_id)
    finally:
        try:
            obs.delete_key(source_key)
        except Exception:  # noqa: BLE001
            logger.warning("删除 pending 源文件失败 key={}", source_key)
        cache.delete(_source_key(task_id))


def run_upload_task(task_id: str, source_path: str) -> None:
    """流水线执行核心：进度上报 + 协作式取消/超时 + 终态落定（source_path 为本地文件）。"""
    meta = cache.get_json(_meta_key(task_id))
    if not isinstance(meta, dict):
        logger.warning("知识库上传任务 meta 缺失，跳过执行 task_id={}", task_id)
        return
    if is_terminal_status(meta.get("status")):
        return
    identity = _identity_from_meta(meta)
    user_id = identity["user_id"]
    timeout_secs = max(1, int(getattr(settings, "KB_UPLOAD_TASK_TIMEOUT_SECONDS", 900) or 900))
    guard = KbUploadTaskGuard(
        is_cancelled=lambda: bool(cache.get_json(_cancel_key(task_id))),
        deadline=time.monotonic() + timeout_secs,
    )

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
            updated_at=time.time(),
        )

    completed = False
    try:
        _update_meta(
            task_id, identity, status="running", stage="parsing", percent=0, updated_at=time.time()
        )
        result = run_ingest_pipeline_sync(
            kb_scope=identity["kb_scope"],
            user_id=user_id,
            agent_id=identity["agent_id"],
            display_filename=identity["display_filename"],
            source_path=source_path,
            progress_cb=progress_cb,
            guard=guard,
        )
        completed = True
        _update_meta(
            task_id,
            identity,
            status="completed",
            stage="done",
            percent=100,
            done=1,
            total=1,
            result=result,
            updated_at=time.time(),
        )
    except KbUploadTaskCancelled:
        _update_meta(
            task_id,
            identity,
            status="cancelled",
            error="用户已取消",
            error_type="cancelled",
            updated_at=time.time(),
        )
    except KbUploadTaskTimeout:
        _update_meta(
            task_id,
            identity,
            status="timeout",
            error=f"处理超过 {timeout_secs} 秒已中止，请减小文件后重试",
            error_type="timeout",
            updated_at=time.time(),
        )
    except EmbeddingThrottledError as e:
        # 限流已自动退避重试仍未成功：单独归类，前端可提示「稍后重试」而非查找本地问题
        logger.warning("知识库上传任务因嵌入限流失败 task_id=%s filename=%r: {}", task_id, identity["display_filename"], e)
        _update_meta(
            task_id,
            identity,
            status="failed",
            error=f"嵌入服务限流（已自动重试仍未成功），请稍后重传：{e}",
            error_type="throttled",
            updated_at=time.time(),
        )
    except EmbeddingConcurrencyTimeoutError as e:
        _update_meta(
            task_id,
            identity,
            status="failed",
            error=f"嵌入服务繁忙，请稍后重传：{e}",
            error_type="throttled",
            updated_at=time.time(),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("知识库上传任务失败 task_id=%s filename=%r", task_id, identity["display_filename"])
        _update_meta(
            task_id,
            identity,
            status="failed",
            error=str(e),
            error_type="failed",
            updated_at=time.time(),
        )
    finally:
        if completed:
            # 仅成功入库才刷新计数（失败/取消/超时未改变文档数，避免无谓 DB 查询）
            _on_task_finished(identity["kb_scope"], reindex=True)
        cache.delete(_cancel_key(task_id))
        _remove_user_active(user_id, task_id)


def _on_task_finished(kb_scope: str, *, reindex: bool = False) -> None:
    """任务终态回调：刷新实验数据集文档计数（KB 上传无副作用，直接返回）。

    在 worker/执行线程内完成数据库 COUNT，避免「上传状态」路由为刷新计数而阻塞事件循环。
    :param kb_scope: 知识库范围
    :param reindex: 是否重新 COUNT（仅在文档数确实变化时为 True）
    """
    if not reindex or not kb_scope.startswith("exp:d"):
        return
    try:
        from app.experiment import service as exp_service

        exp_service.refresh_document_count(int(kb_scope[len("exp:d") :]))
    except Exception:  # noqa: BLE001
        logger.warning("刷新实验数据集文档计数失败 kb_scope={}", kb_scope)


# ---------------------------------------------------------------- 状态查询


def get_kb_upload_job_meta(task_id: str) -> dict[str, Any] | None:
    """获取任务元数据/进度快照（无则返回 None）。"""
    raw = cache.get_json(_meta_key(task_id))
    return raw if isinstance(raw, dict) else None


def get_kb_upload_job_meta_many(task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """
    批量获取任务快照（一次 MGET，避免逐任务轮询打满事件循环）。
    :return: {task_id: meta}；缺失/损坏的条目不返回（key 必须回填为 task_id，
        cache.mget_json 返回的 key 是 Redis meta key，直接透传会导致前端查不到状态）
    """
    ids = [tid for tid in task_ids if tid]
    metas = cache.mget_json([_meta_key(tid) for tid in ids])
    out: dict[str, dict[str, Any]] = {}
    for tid in ids:
        meta = metas.get(_meta_key(tid))
        if isinstance(meta, dict):
            out[tid] = meta
    return out


def is_job_cancel_requested(task_id: str) -> bool:
    """是否已请求取消该任务。"""
    return bool(cache.get_json(_cancel_key(task_id)))


async def request_kb_upload_cancel(task_id: str) -> None:
    """标记任务为「用户请求取消」，worker 在批处理边界协作式退出。"""
    await asyncio.to_thread(cache.set_json, _cancel_key(task_id), {"v": 1}, _ttl())