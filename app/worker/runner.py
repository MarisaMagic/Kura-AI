"""
知识库/实验文档上传 worker（独立进程）。

与 API 进程解耦后：
- 解析/向量化不再抢占 API 事件循环与 GIL，接口在高负载下仍可响应；
- 队列（Redis List，BRPOPLPUSH + processing 列表）提供背压与崩溃恢复；
- 页面刷新/崩溃不影响已受理任务，处理进度照样写入 Redis 供前端查询。

用法：python worker.py（或 docker compose 的 kb-worker 服务）。
"""

from __future__ import annotations

import signal
import threading
import time

from app.log import logger
from app.settings import settings

_stop = threading.Event()


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
        from app.kb.milvus_client import MilvusManager

        MilvusManager().init_collection()
    except Exception as e:  # noqa: BLE001
        logger.warning("worker Milvus 预热失败（首次任务时自动重试）: {}", e)
    try:
        from app.kb import kb_job

        stale = max(60, int(getattr(settings, "KB_UPLOAD_STALE_SECONDS", 900) or 900))
        kb_job.recover_stale_processing(stale)
    except Exception as e:  # noqa: BLE001
        logger.warning("worker stale 任务回收失败: {}", e)


def _run_one(worker_name: str) -> bool:
    """出队并执行一个任务；返回 False 表示队列空闲（用于退避）。"""
    from app.kb import kb_job

    payload = kb_job.dequeue_task(timeout=3)
    if not payload:
        return False
    task_id = str(payload.get("task_id") or "")
    kind = str(payload.get("kind") or "kb_upload")
    if not task_id:
        kb_job.ack_task(payload)
        return True
    started = time.monotonic()
    try:
        if kind == "kb_upload":
            kb_job.run_upload_task_from_source(task_id)
        else:
            logger.warning("未知任务类型 kind={} task_id={}（已跳过）", kind, task_id)
    except Exception:  # noqa: BLE001
        logger.exception("{} 任务执行异常 task_id={}", worker_name, task_id)
    finally:
        kb_job.ack_task(payload)
    logger.info(
        "{} 任务完成 task_id={} 耗时={:.1f}s",
        worker_name,
        task_id,
        time.monotonic() - started,
    )
    return True


def _worker_loop(index: int) -> None:
    name = f"kb-worker-{index}"
    logger.info("{} 启动", name)
    idle = 0
    while not _stop.is_set():
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