"""
知识库文档上传任务：受理与处理解耦（进度写 Redis，前端批量轮询状态）。

- 队列模式（KB_UPLOAD_MODE=queue，生产默认）：上传接口把文件流式落对象存储 pending 区，
  写任务 meta 后入队立即返回 task_id；worker.py（独立进程）消费队列执行解析/向量化/入库。
  页面崩溃、API 重启都不影响已受理任务。队列后端由 KB_UPLOAD_QUEUE_BACKEND 选择：
  - kafka（生产 compose）：Kafka 主题 + 手动提交位点；深度用 Redis 计数器限流，
    投递次数写入 meta，超限转死信主题。进度/取消/处理锁仍在 Redis。
  - stream（代码默认，回滚）：Redis Stream + 消费者组，按消息 ack（XACK+XDEL）、XAUTOCLAIM 超时回收、
    投递计数超限转死信；XLEN 即未完成深度。
  - list（回滚通道）：旧 List + processing 可靠队列（BRPOPLPUSH + LREM）。
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
from app.utils.upstream_quota import QuotaBreakerOpenError, QuotaTimeoutError

TERMINAL_STATUSES = ("completed", "failed", "timeout", "cancelled")

# 旧 List 后端 key（回滚通道 / 一次性迁移源）
_QUEUE_KEY = "kb_upload_job:queue"
_PROCESSING_KEY = "kb_upload_job:processing"


def _stream_key() -> str:
    """Stream key（读配置，便于按需改名）。"""
    return str(getattr(settings, "KB_UPLOAD_STREAM_KEY", "kb_upload_job:stream") or "kb_upload_job:stream")


def _consumer_group() -> str:
    """消费者组名（多副本共用同一组实现负载均衡）。"""
    return str(getattr(settings, "KB_UPLOAD_CONSUMER_GROUP", "kb_upload_workers") or "kb_upload_workers")


def _dead_stream_key() -> str:
    """死信 stream key（超过最大投递次数仍失败的任务转存审计）。"""
    return str(getattr(settings, "KB_UPLOAD_DEAD_STREAM", "kb_upload_job:dead") or "kb_upload_job:dead")


# 兼容测试/调用方直接引用常量（读取当前配置值）
_STREAM_KEY = _stream_key()
_CONSUMER_GROUP = _consumer_group()
_DEAD_STREAM_KEY = _dead_stream_key()


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


def queue_backend() -> str:
    """队列后端：kafka | stream（默认，回滚）| list（旧 List 可靠队列）。"""
    backend = str(getattr(settings, "KB_UPLOAD_QUEUE_BACKEND", "stream") or "stream").strip().lower()
    return backend if backend in ("kafka", "stream", "list") else "stream"


# Kafka 模式的排队深度（与 produce 分离，用 Redis 计数做原子限深）
_KAFKA_DEPTH_KEY = "kb_upload_job:kafka_depth"
_KAFKA_DEPTH_TTL = 7 * 24 * 3600


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
    """排队 + 处理中（未 ack）的任务总数。stream 用 XLEN；kafka 用 Redis 计数器。"""
    backend = queue_backend()
    if backend == "stream":
        return cache.xlen(_stream_key())
    if backend == "kafka":
        return cache.get_int(_KAFKA_DEPTH_KEY)
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


def _enqueue_kafka(payload: dict[str, Any], queue_max: int) -> bool:
    """先占深度名额再 produce；写入失败则退还名额，避免计数泄漏把队列撑满。"""
    from app.kb import kafka_queue

    reserved = cache.incr_if_below(_KAFKA_DEPTH_KEY, queue_max, ttl=_KAFKA_DEPTH_TTL)
    if not reserved:
        return False
    if kafka_queue.produce_json(payload):
        return True
    cache.decr_floor(_KAFKA_DEPTH_KEY, ttl=_KAFKA_DEPTH_TTL)
    return False


def enqueue_task(task_id: str) -> bool:
    """入队上传任务：原子检查「排队 + 处理中」深度上限（多副本并发下不超卖）。

    KB_UPLOAD_QUEUE_MAX<=0 时仅做普通入队；超限或后端不可用返回 False。
    kafka 用 Redis 计数器限深后再 produce；stream 用 XADD 限深；list 沿用 RPUSH 限深。
    """
    payload = {"kind": "kb_upload", "task_id": task_id}
    queue_max = max(0, int(getattr(settings, "KB_UPLOAD_QUEUE_MAX", 0) or 0))
    backend = queue_backend()
    if backend == "kafka":
        return _enqueue_kafka(payload, queue_max)
    if backend == "stream":
        return cache.xadd_limited_json(_stream_key(), payload, queue_max) is not None
    return cache.rpush_json_with_limit(_QUEUE_KEY, _PROCESSING_KEY, payload, queue_max)


def dequeue_task(timeout: int = 5, consumer: str = "") -> tuple[Any, dict[str, Any]] | None:
    """出队一个任务，返回 (ack_token, payload)；无任务返回 None。

    kafka：手动提交前的消息对象作为 ack_token（须由拉取它的线程提交）；
    stream：消费者组阻塞读取，ack_token 为消息 id；
    list：BRPOPLPUSH 移入 processing，ack_token 为 payload 本身。
    """
    backend = queue_backend()
    if backend == "kafka":
        from app.kb import kafka_queue

        block_ms = max(1, int(getattr(settings, "KB_UPLOAD_READ_BLOCK_MS", 3000) or 3000))
        return kafka_queue.poll_one(consumer or "kb-worker", block_ms)
    if backend == "stream":
        block_ms = max(1, int(getattr(settings, "KB_UPLOAD_READ_BLOCK_MS", 3000) or 3000))
        entries = cache.xreadgroup_json(
            _stream_key(), _consumer_group(), consumer or "kb-worker", block_ms, count=1
        )
        for msg_id, payload in entries:
            if isinstance(payload, dict):
                return msg_id, payload
        return None
    payload = cache.brpoplpush_json(_QUEUE_KEY, _PROCESSING_KEY, timeout)
    return (payload, payload) if isinstance(payload, dict) else None


def ack_task(token: Any) -> None:
    """任务完成确认。

    kafka：提交位点成功后才减少深度计数（提交失败则消息会重投，计数保持）；
    stream：按消息 id 执行 XACK+XDEL（精确、无需值匹配）；
    list：仅移除自己消费的那一条（count=1），避免误删其它副本仍在处理的条目。
    """
    backend = queue_backend()
    if backend == "kafka":
        from app.kb import kafka_queue

        if kafka_queue.commit(token):
            cache.decr_floor(_KAFKA_DEPTH_KEY, ttl=_KAFKA_DEPTH_TTL)
        return
    if backend == "stream":
        cache.xack_del(_stream_key(), _consumer_group(), str(token))
        return
    cache.lrem_json(_PROCESSING_KEY, token, count=1)


def requeue_task(payload: dict[str, Any]) -> None:
    """list 后端重投（保留兼容）；stream 后端用 XAUTOCLAIM 回收，不需要重投。"""
    cache.lrem_json(_PROCESSING_KEY, payload, count=1)
    cache.rpush_json(_QUEUE_KEY, payload)


def ensure_consumer_group() -> bool:
    """确保 Stream 消费者组存在（worker 启动时调用；幂等）。"""
    return cache.xgroup_create(_stream_key(), _consumer_group(), "0")


def ensure_kafka_topics() -> bool:
    """确保上传主题与死信主题存在（worker 启动时调用；幂等）。"""
    from app.kb import kafka_queue

    return kafka_queue.ensure_topics()


def accept_kafka_delivery(task_id: str) -> bool:
    """记录一次 Kafka 投递。返回 True 表示应执行流水线。

    已终态或 meta 缺失时返回 False（调用方仍提交位点）。
    累计次数超过 KB_UPLOAD_MAX_DELIVERIES 时标失败、写入死信并返回 False，
    避免毒消息在提交前被无限重投。
    """
    meta = cache.get_json(_meta_key(task_id))
    if not isinstance(meta, dict) or is_terminal_status(meta.get("status")):
        return False
    max_deliveries = max(1, int(getattr(settings, "KB_UPLOAD_MAX_DELIVERIES", 3) or 3))
    deliveries = int(meta.get("deliveries") or 0) + 1
    identity = _identity_from_meta(meta)
    now = time.time()
    if deliveries > max_deliveries:
        user_id = int(meta.get("user_id") or 0)
        logger.error(
            "知识库上传任务超过最大投递次数，判失败并转死信 task_id={} 次数={}",
            task_id,
            deliveries,
        )
        _update_meta(
            task_id,
            identity,
            status="failed",
            error="处理进程反复中断/超时，已超过最大重试次数，请重新上传",
            error_type="failed",
            deliveries=deliveries,
            updated_at=now,
        )
        from app.kb import kafka_queue

        kafka_queue.produce_json(
            {"kind": "kb_upload", "task_id": task_id, "user_id": user_id, "deliveries": deliveries},
            topic_name=kafka_queue.dead_topic(),
        )
        _remove_user_active(user_id, task_id)
        return False
    _update_meta(task_id, identity, deliveries=deliveries, updated_at=now)
    return True


def _processing_lock_key(task_id: str) -> str:
    """任务级处理锁：同一 task_id 在任一时刻只允许一个副本执行（重复投递幂等）。"""
    return f"kb_upload_job:{task_id}:processing"


def _acquire_processing_lock(task_id: str) -> bool:
    ttl = max(600, int(getattr(settings, "KB_UPLOAD_TASK_TIMEOUT_SECONDS", 900) or 900) + 180)
    return bool(cache.set_nx(_processing_lock_key(task_id), {"ts": time.time()}, ttl))


def _release_processing_lock(task_id: str) -> None:
    cache.delete(_processing_lock_key(task_id))


def recover_stale_processing(stale_seconds: int) -> int:
    """
    worker 启动时回收 processing 列表（多副本安全）：
    - 分布式锁保证同一时刻仅一个副本执行回收；
    - 每条目用 count=1 原子抢占（LREM 成功者才负责后续处理），避免重复回收/误删；
    - 任务已终态/ meta 丢失：直接清理；
    - 心跳超过 stale_seconds：重投一次（meta.recovered=True），再次超时则判失败，避免死循环。
    :return: 处理（清理/重投/判死）的条目数
    """
    lock_key = f"{_QUEUE_KEY}:recover_lock"
    if not cache.set_nx(lock_key, {"ts": time.time()}, 60):
        logger.info("另一 worker 正在回收 stale 任务，跳过本次回收")
        return 0
    try:
        now = time.time()
        recovered = 0
        for raw in cache.lrange_str(_PROCESSING_KEY, 0, -1):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                if cache.lrem_raw(_PROCESSING_KEY, raw, count=1) > 0:
                    recovered += 1
                continue
            task_id = str(payload.get("task_id") or "")
            meta = cache.get_json(_meta_key(task_id)) if task_id else None
            if not isinstance(meta, dict) or is_terminal_status(meta.get("status")):
                if cache.lrem_raw(_PROCESSING_KEY, raw, count=1) > 0:
                    recovered += 1
                continue
            updated_at = float(meta.get("updated_at") or meta.get("created_at") or 0)
            if now - updated_at < stale_seconds:
                continue
            # 原子抢占：仅成功移除该条目的副本负责后续处理（其它副本 LREM 返回 0 跳过）
            if cache.lrem_raw(_PROCESSING_KEY, raw, count=1) <= 0:
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
            else:
                # 重投前清除旧处理锁：保证新执行者能获取锁（旧处理者已判定死亡）
                _release_processing_lock(task_id)
                _update_meta(
                    task_id,
                    identity,
                    status="queued",
                    stage="queued",
                    percent=0,
                    recovered=True,
                    updated_at=now,
                )
                cache.rpush_json(_QUEUE_KEY, payload)
            recovered += 1
        if recovered:
            logger.info("知识库上传任务回收完成：{} 条", recovered)
        return recovered
    finally:
        cache.delete(lock_key)


def reclaim_stale_tasks(consumer: str) -> int:
    """stream 后端超时回收：XAUTOCLAIM 认领其它消费者空闲超过阈值的待确认消息。

    与 recover_stale_processing 的差异：
    - 只遍历待确认列表（PEL），非全表 LRANGE 扫描；
    - 按累计投递次数判死（超过 KB_UPLOAD_MAX_DELIVERIES 判失败并转死信）；
    - 可周期调用，不依赖 worker 重启。
    :return: 处理（回收执行/判死/清理）的条目数
    """
    stale = max(60, int(getattr(settings, "KB_UPLOAD_STALE_SECONDS", 900) or 900))
    min_idle_ms = stale * 1000
    max_deliveries = max(1, int(getattr(settings, "KB_UPLOAD_MAX_DELIVERIES", 3) or 3))
    stream = _stream_key()
    group = _consumer_group()
    claimed = cache.xautoclaim_json(stream, group, consumer, min_idle_ms)
    handled = 0
    for msg_id, payload in claimed:
        task_id = str(payload.get("task_id") or "") if isinstance(payload, dict) else ""
        if not task_id:
            cache.xack_del(stream, group, msg_id)
            handled += 1
            continue
        meta = cache.get_json(_meta_key(task_id))
        if not isinstance(meta, dict) or is_terminal_status(meta.get("status")):
            # 已终态 / meta 丢失：清理待确认条目
            cache.xack_del(stream, group, msg_id)
            handled += 1
            continue
        delivery = cache.xpending_delivery_count(stream, group, msg_id)
        user_id = int(meta.get("user_id") or 0)
        if delivery > max_deliveries:
            identity = _identity_from_meta(meta)
            logger.error(
                "知识库上传任务超过最大投递次数，判失败并转死信 task_id={} 次数={}", task_id, delivery
            )
            _update_meta(
                task_id,
                identity,
                status="failed",
                error="处理进程反复中断/超时，已超过最大重试次数，请重新上传",
                error_type="failed",
                updated_at=time.time(),
            )
            cache.xadd_json(
                _dead_stream_key(),
                {"kind": "kb_upload", "task_id": task_id, "user_id": user_id, "deliveries": delivery},
            )
            _remove_user_active(user_id, task_id)
            cache.xack_del(stream, group, msg_id)
            handled += 1
            continue
        # 有效但超时：清旧锁 → 重置 meta → 立即处理（消息已认领到本消费者），完成后 ack
        identity = _identity_from_meta(meta)
        _release_processing_lock(task_id)
        _update_meta(
            task_id,
            identity,
            status="queued",
            stage="queued",
            percent=0,
            recovered=True,
            updated_at=time.time(),
        )
        try:
            run_upload_task_from_source(task_id)
        except Exception:  # noqa: BLE001
            logger.exception("知识库上传任务回收执行异常 task_id={}", task_id)
        finally:
            cache.xack_del(stream, group, msg_id)
        handled += 1
    if handled:
        logger.info("知识库上传任务 Stream 回收完成：{} 条", handled)
    return handled


def migrate_list_to_stream() -> int:
    """一次性把旧 List 后端的在途任务迁入 Stream（零停机）。

    分布式锁保证多副本下仅一个执行；迁一条删一条，避免重复处理。
    :return: 迁移条目数
    """
    lock_key = "kb_upload_job:migrate_to_stream"
    if not cache.set_nx(lock_key, {"ts": time.time()}, 300):
        return 0
    moved = 0
    try:
        for key in (_QUEUE_KEY, _PROCESSING_KEY):
            for raw in cache.lrange_str(key, 0, -1):
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    cache.lrem_raw(key, raw, count=1)
                    continue
                if not isinstance(payload, dict):
                    cache.lrem_raw(key, raw, count=1)
                    continue
                cache.xadd_json(_stream_key(), payload)
                cache.lrem_raw(key, raw, count=1)
                moved += 1
        if moved:
            logger.info("旧 List 队列任务迁移至 Stream 完成：{} 条", moved)
    finally:
        cache.delete(lock_key)
    return moved


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
        written = await cache.aset_json(_meta_key(tid), meta, _ttl())
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
            await cache.adelete(_meta_key(tid))
            await asyncio.to_thread(_remove_user_active, user_id, tid)
            return None

    if content is None and upload_mode() == "queue":
        enqueued = await asyncio.to_thread(enqueue_task, tid)
        if not enqueued:
            logger.error("知识库上传任务入队失败（队列不可用或已满）task_id={}", tid)
            await cache.adelete(_meta_key(tid))
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
    """队列模式：从 pending 对象取回源文件（流式落临时文件）后执行。

    任务级处理锁保证多副本下同一 task_id 的重复投递（崩溃恢复/队列重复）只被一个副本执行。
    """
    if not _acquire_processing_lock(task_id):
        logger.warning("知识库上传任务已在其它 worker 处理中，跳过重复投递 task_id={}", task_id)
        return
    try:
        _run_upload_task_from_source_locked(task_id)
    finally:
        _release_processing_lock(task_id)


def _run_upload_task_from_source_locked(task_id: str) -> None:
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
    last_stage = ""
    last_progress_at = 0.0
    min_interval = max(0.0, float(getattr(settings, "KB_PROGRESS_MIN_INTERVAL_SECONDS", 0.5) or 0.0))
    min_step = max(0, int(getattr(settings, "KB_PROGRESS_MIN_PERCENT_STEP", 2) or 0))

    def progress_cb(stage: str, done: int, total: int) -> None:
        nonlocal last_percent, last_stage, last_progress_at
        lo, hi = _STAGE_BANDS.get(stage, (0, 100))
        if total and total > 0 and done >= 0:
            ratio = min(1.0, max(0.0, done / max(1, total)))
            percent = int(lo + (hi - lo) * ratio)
        else:
            percent = lo
        # 进度只前进不回退
        percent = max(percent, last_percent)
        # 写节流：阶段未变且时间/百分比增量均未达阈值时跳过本次 Redis 往返。
        # （图片逐张 tick 与文本逐批回调会产生大量小幅写；终态写入不走本回调，不受影响）
        now = time.monotonic()
        if (
            stage == last_stage
            and percent < 100
            and (now - last_progress_at) < min_interval
            and percent < last_percent + min_step
        ):
            return
        last_percent = percent
        last_stage = stage
        last_progress_at = now
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
    except (QuotaBreakerOpenError, QuotaTimeoutError) as e:
        # 配额保护触发（熔断/等待超时）：归类限流，提示稍后重传而非查找本地问题
        _update_meta(
            task_id,
            identity,
            status="failed",
            error=f"嵌入服务配额受限（熔断中），请稍后重传：{e}",
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
    await cache.aset_json(_cancel_key(task_id), {"v": 1}, _ttl())