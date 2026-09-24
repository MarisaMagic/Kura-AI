"""实验运行后台任务：线程池执行，进度写 Redis，前端轮询；支持协作式取消。

流程：取题（库内前 N + 可选 OOD）→ 每题 embedding 只算一次 → 逐配置检索评测
→ 逐题落库 mg_exp_run_results → 终态更新 mg_exp_runs。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from loguru import logger

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import ExpQuestion, ExpRun, ExpRunResult
from app.experiment.runner import needs_dense_embedding, run_config_for_question
from app.experiment.service import exp_kb_scope
from app.kb.milvus_client import MilvusManager
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.settings import settings


def _meta_key(run_id: int) -> str:
    return f"exp_run_job:{run_id}:meta"


def _cancel_key(run_id: int) -> str:
    return f"exp_run_job:{run_id}:cancel"


def _ttl() -> int:
    return 86400


_worker_pool: ThreadPoolExecutor | None = None


def _get_pool() -> ThreadPoolExecutor:
    """实验运行专用线程池（同时最多 2 个运行，避免挤占 KB 上传与 DashScope 限额）。"""
    global _worker_pool
    if _worker_pool is None:
        _worker_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exp-run")
    return _worker_pool


async def create_exp_run_job(run_id: int) -> str | None:
    """调度实验运行到后台线程池，返回 task_id（即 run_id 字符串）；Redis 不可写时返回 None。"""
    meta = {
        "run_id": run_id,
        "status": "queued",
        "percent": 0,
        "done": 0,
        "total": None,
        "stage": "queued",
        "error": None,
    }
    written = await asyncio.to_thread(cache.set_json, _meta_key(run_id), meta, _ttl())
    if not written:
        logger.error("实验运行任务初始化失败（Redis 不可写）run_id={}", run_id)
        return None
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_get_pool(), _run_exp_thread, run_id)
    return str(run_id)


def get_exp_run_job_meta(run_id: int) -> dict[str, Any] | None:
    raw = cache.get_json(_meta_key(run_id))
    return raw if isinstance(raw, dict) else None


async def request_exp_run_cancel(run_id: int) -> None:
    await asyncio.to_thread(cache.set_json, _cancel_key(run_id), {"v": 1}, _ttl())


def is_cancel_requested(run_id: int) -> bool:
    return bool(cache.get_json(_cancel_key(run_id)))


def _update_meta(run_id: int, **fields: Any) -> None:
    meta = cache.get_json(_meta_key(run_id))
    if not isinstance(meta, dict):
        meta = {"run_id": run_id}
    meta.update(fields)
    cache.set_json(_meta_key(run_id), meta, _ttl())


def _set_run_status(run_id: int, status: str, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        run = db.query(ExpRun).filter(ExpRun.id == run_id).first()
        if run:
            run.status = status
            run.error = error
            if status in ("completed", "cancelled", "failed"):
                run.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def _load_questions(run_id: int) -> list[dict]:
    """按 question_limit 取库内题（按 id 升序，即「前 N 个」），include_ood 时追加全部 OOD 题。"""
    db = SessionLocal()
    try:
        run = db.query(ExpRun).filter(ExpRun.id == run_id).first()
        if not run:
            return []
        q = db.query(ExpQuestion).filter(ExpQuestion.dataset_id == run.dataset_id, ExpQuestion.is_ood == False)  # noqa: E712
        q = q.order_by(ExpQuestion.id.asc())
        if run.question_limit and run.question_limit > 0:
            q = q.limit(run.question_limit)
        items = q.all()
        if run.include_ood:
            ood = (
                db.query(ExpQuestion)
                .filter(ExpQuestion.dataset_id == run.dataset_id, ExpQuestion.is_ood == True)  # noqa: E712
                .order_by(ExpQuestion.id.asc())
                .all()
            )
            items = items + ood
        # 脱离会话前取出所需字段
        return [
            {
                "id": r.id,
                "question": r.question,
                "gold_file_keys": list(r.gold_file_keys or []),
                "is_ood": bool(r.is_ood),
            }
            for r in items
        ]
    finally:
        db.close()


def _run_exp_thread(run_id: int) -> None:
    """实验执行线程：逐题 × 逐配置评测，Redis 上报进度，协作式取消。"""
    db = SessionLocal()
    try:
        run = db.query(ExpRun).filter(ExpRun.id == run_id).first()
        if not run:
            return
        configs = list(run.configs or [])
        dataset_id = run.dataset_id
    finally:
        db.close()

    if not configs:
        _set_run_status(run_id, "failed", "配置为空")
        _update_meta(run_id, status="failed", error="配置为空")
        return

    try:
        questions = _load_questions(run_id)
    except Exception as e:  # noqa: BLE001
        _set_run_status(run_id, "failed", str(e))
        _update_meta(run_id, status="failed", error=str(e))
        return
    if not questions:
        _set_run_status(run_id, "failed", "没有可评测的问题")
        _update_meta(run_id, status="failed", error="没有可评测的问题")
        return

    total = len(questions)
    _set_run_status(run_id, "running")
    _update_meta(run_id, status="running", stage="running", percent=0, done=0, total=total)

    scope = exp_kb_scope(dataset_id)
    milvus = MilvusManager()
    embedding_service = get_multimodal_embedding_service()
    need_dense = needs_dense_embedding(configs)
    if need_dense and not (settings.EMBEDDING_API_KEY or "").strip():
        _set_run_status(run_id, "failed", "未配置 EMBEDDING_API_KEY，无法生成查询向量")
        _update_meta(run_id, status="failed", error="未配置 EMBEDDING_API_KEY")
        return

    try:
        milvus.init_collection()
    except Exception as e:  # noqa: BLE001
        _set_run_status(run_id, "failed", f"Milvus 不可用: {e}")
        _update_meta(run_id, status="failed", error=f"Milvus 不可用: {e}")
        return

    cancelled = False
    try:
        for done, q in enumerate(questions, 1):
            if is_cancel_requested(run_id):
                cancelled = True
                break
            dense_embedding: list[float] | None = None
            embed_error: str | None = None
            if need_dense:
                try:
                    dense_embedding = embedding_service.get_text_embeddings([q["question"]])[0]
                except Exception as e:  # noqa: BLE001
                    embed_error = f"embedding_failed: {e}"[:500]

            rows: list[ExpRunResult] = []
            for idx, cfg in enumerate(configs):
                if is_cancel_requested(run_id):
                    cancelled = True
                    break
                if embed_error:
                    rows.append(
                        ExpRunResult(
                            run_id=run_id, config_idx=idx, question_id=q["id"],
                            is_ood=q["is_ood"], error=embed_error,
                        )
                    )
                    continue
                try:
                    res = run_config_for_question(
                        query=q["question"],
                        gold_file_keys=q["gold_file_keys"],
                        config=cfg,
                        dense_embedding=dense_embedding,
                        milvus=milvus,
                        kb_scope=scope,
                    )
                except Exception as e:  # noqa: BLE001
                    res = {"error": str(e)[:500]}
                rows.append(
                    ExpRunResult(
                        run_id=run_id,
                        config_idx=idx,
                        question_id=q["id"],
                        is_ood=q["is_ood"],
                        retrieved=res.get("retrieved") or [],
                        hit=bool(res.get("hit")),
                        hit_rank=int(res.get("hit_rank") or 0),
                        reciprocal_rank=float(res.get("reciprocal_rank") or 0.0),
                        recall=float(res.get("recall") or 0.0),
                        top1_score=float(res.get("top1_score") or 0.0),
                        max_rerank_score=res.get("max_rerank_score"),
                        rerank_below_min=bool(res.get("rerank_below_min")),
                        latency_ms=int(res.get("latency_ms") or 0),
                        error=res.get("error"),
                    )
                )
            if rows:
                db = SessionLocal()
                try:
                    db.add_all(rows)
                    db.commit()
                finally:
                    db.close()
            _update_meta(
                run_id,
                status="running",
                stage="running",
                done=done,
                total=total,
                percent=int(done * 100 / max(1, total)),
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("实验运行失败 run_id=%s", run_id)
        _set_run_status(run_id, "failed", str(e)[:500])
        _update_meta(run_id, status="failed", error=str(e)[:500])
        return

    if cancelled:
        _set_run_status(run_id, "cancelled", "用户已取消")
        _update_meta(run_id, status="cancelled", error="用户已取消")
    else:
        _set_run_status(run_id, "completed")
        _update_meta(run_id, status="completed", stage="done", percent=100, done=total, total=total)
    cache.delete(_cancel_key(run_id))


def exp_run_is_active(run_id: int) -> bool:
    """运行是否处于活动状态（防止重复调度）。"""
    meta = get_exp_run_job_meta(run_id)
    return bool(meta and meta.get("status") in ("queued", "running"))
