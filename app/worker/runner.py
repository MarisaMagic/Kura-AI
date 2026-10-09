"""
知识库/实验文档上传 worker（独立进程）。

与 API 进程解耦后：
- 解析/向量化不再抢占 API 事件循环与 GIL，接口在高负载下仍可响应；
- 队列提供背压与崩溃恢复。生产为 Kafka（手动提交位点，投递超限转死信）；
  KB_UPLOAD_QUEUE_BACKEND=stream 回滚到 Redis Stream（XREADGROUP / XACK+XDEL / XAUTOCLAIM），
  =list 回滚到旧 List + processing 可靠队列；
- 页面刷新/崩溃不影响已受理任务，处理进度照样写入 Redis 供前端查询。

用法：python worker.py（或 docker compose 的 kb-worker 服务）。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time

from app.log import logger
from app.settings import settings

_stop = threading.Event()


def _consumer_name(index: int) -> str:
    """消费者名：主机-进程-线程，Stream 消费者组内唯一标识各 worker 线程。"""
    return f"{socket.gethostname()}-{os.getpid()}-{index}"


def _reclaim_interval() -> float:
    """超时任务回收扫描间隔（秒）。"""
    return max(5.0, float(getattr(settings, "KB_UPLOAD_RECLAIM_INTERVAL_SECONDS", 60) or 60))


def _reclaim_once(consumer: str) -> int:
    """执行一次超时任务回收（按后端分派），返回处理条目数。"""
    from app.kb import kb_job

    backend = kb_job.queue_backend()
    if backend == "stream":
        return kb_job.reclaim_stale_tasks(consumer)
    # Kafka 由消费组在进程退出后重投未提交位点，不扫描 Redis List
    if backend == "kafka":
        return 0
    stale = max(60, int(getattr(settings, "KB_UPLOAD_STALE_SECONDS", 900) or 900))
    return kb_job.recover_stale_processing(stale)


def _bootstrap() -> None:
    """worker 启动初始化：数据库连接、对象存储、Milvus 预热、stale 任务回收。"""
    from app.chat.database import init_chat_db

    init_chat_db()
    try:
        from app.core.object_storage import ensure_bucket

        ensure_bucket()
    except Exception as e:  # noqa: BLE001
        logger.error("worker 对象存储初始化失败（上传处理将不可用）: {}", e)
    try:
        from app.kb.milvus_client import get_milvus_manager

        # 预热带硬超时（daemon 线程 + join timeout）：Milvus 半死/不可达时不得阻塞
        # worker 进入消费循环；超时线程随进程退出回收，首次任务时自动重试。
        _preheat_result: dict = {}

        def _preheat() -> None:
            try:
                get_milvus_manager().init_collection()
                _preheat_result["ok"] = True
            except Exception as e:  # noqa: BLE001
                _preheat_result["error"] = e

        t = threading.Thread(target=_preheat, name="milvus-preheat", daemon=True)
        t.start()
        t.join(timeout=20)
        if t.is_alive():
            logger.warning("worker Milvus 预热超时（20s），跳过；首次任务时自动重试")
        elif _preheat_result.get("error") is not None:
            logger.warning("worker Milvus 预热失败（首次任务时自动重试）: {}", _preheat_result["error"])
    except Exception as e:  # noqa: BLE001
        logger.warning("worker Milvus 预热初始化异常: {}", e)
    try:
        from app.kb import kb_job

        backend = kb_job.queue_backend()
        if backend == "stream":
            # 建组（幂等）→ 迁移旧 List 在途任务 → 首次回收
            kb_job.ensure_consumer_group()
            moved = kb_job.migrate_list_to_stream()
            if moved:
                logger.info("旧 List 队列在途任务迁移至 Stream：{} 条", moved)
            kb_job.reclaim_stale_tasks(_consumer_name(0))
        elif backend == "kafka":
            if not kb_job.ensure_kafka_topics():
                logger.error("Kafka 主题初始化失败（出队时会重试建主题）")
        else:
            stale = max(60, int(getattr(settings, "KB_UPLOAD_STALE_SECONDS", 900) or 900))
            kb_job.recover_stale_processing(stale)
    except Exception as e:  # noqa: BLE001
        logger.warning("worker 队列初始化/回收失败: {}", e)


def _run_one(worker_name: str) -> bool:
    """出队并执行一个任务；返回 False 表示队列空闲（用于退避）。"""
    from app.kb import kb_job

    entry = kb_job.dequeue_task(timeout=3, consumer=worker_name)
    if not entry:
        return False
    token, payload = entry
    task_id = str(payload.get("task_id") or "")
    kind = str(payload.get("kind") or "kb_upload")
    if not task_id:
        kb_job.ack_task(token)
        return True
    started = time.monotonic()
    try:
        if kind == "kb_upload":
            # 超限转死信或已终态：不再执行，finally 里仍提交位点
            if kb_job.queue_backend() != "kafka" or kb_job.accept_kafka_delivery(task_id):
                kb_job.run_upload_task_from_source(task_id)
        else:
            logger.warning("未知任务类型 kind={} task_id={}（已跳过）", kind, task_id)
    except Exception:  # noqa: BLE001
        logger.exception("{} 任务执行异常 task_id={}", worker_name, task_id)
    finally:
        kb_job.ack_task(token)
    logger.info(
        "{} 任务完成 task_id={} 耗时={:.1f}s",
        worker_name,
        task_id,
        time.monotonic() - started,
    )
    return True


def _worker_loop(index: int) -> None:
    name = _consumer_name(index)
    logger.info("{} 启动", name)
    idle = 0
    next_reclaim = time.monotonic() + _reclaim_interval()
    while not _stop.is_set():
        # 周期回收超时任务（stream: XAUTOCLAIM；list: processing 扫描；kafka: 跳过）
        now = time.monotonic()
        if now >= next_reclaim:
            next_reclaim = now + _reclaim_interval()
            try:
                recovered = _reclaim_once(name)
                if recovered:
                    logger.info("{} 回收超时任务 {} 条", name, recovered)
            except Exception:  # noqa: BLE001
                logger.exception("{} 超时任务回收异常", name)
        try:
            busy = _run_one(name)
        except Exception:  # noqa: BLE001
            logger.exception("{} 循环异常", name)
            busy = False
        if busy:
            idle = 0
            continue
        idle += 1
        # 队列空闲时逐级退避，避免空转
        _stop.wait(min(1.0 + idle * 0.5, 5.0))
    try:
        from app.kb import kb_job

        if kb_job.queue_backend() == "kafka":
            from app.kb import kafka_queue

            kafka_queue.close_consumer()
    except Exception:  # noqa: BLE001
        logger.exception("{} 关闭 Kafka 消费者失败", name)
    logger.info("{} 退出", name)


def _install_signal_handlers() -> None:
    def _handle(signum, _frame):  # noqa: ANN001
        logger.info("worker 收到信号 {}，停止取新任务，等待当前任务结束…", signum)
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            # Windows / 非主线程场景下可能无法注册，忽略
            pass


def main() -> int:
    mode = str(getattr(settings, "KB_UPLOAD_MODE", "inline") or "inline").lower()
    if mode != "queue":
        logger.warning(
            "KB_UPLOAD_MODE={}，API 不会写入队列；worker 将空转（生产请设为 queue）", mode
        )
    _install_signal_handlers()
    _bootstrap()
    threads = max(1, int(getattr(settings, "KB_WORKER_THREADS", 4) or 4))
    logger.info("上传 worker 启动：线程数={} 队列模式={}", threads, mode)
    workers = [
        threading.Thread(target=_worker_loop, args=(i + 1,), name=f"kb-worker-{i + 1}", daemon=False)
        for i in range(threads)
    ]
    for t in workers:
        t.start()
    while any(t.is_alive() for t in workers):
        for t in workers:
            t.join(timeout=0.5)
        if _stop.is_set():
            for t in workers:
                t.join(timeout=1.0)
            break
    _stop.set()
    logger.info("上传 worker 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())