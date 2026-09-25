"""实验运行后台任务：线程池执行，细粒度进度写 Redis，前端轮询；支持协作式取消。

流程：取题（库内前 N + 可选 OOD）→ 每题 embedding 只算一次 → 逐配置检索评测
（QA 任务额外生成答案与判分）→ 逐题落库 mg_exp_run_results → 终态更新 mg_exp_runs。
进度以「单元」计：初始化 1、查询向量 1、每配置检索 1、生成 1、判分 1~2，
阶段回调同时写入文案与已用时长，避免长 LLM 调用期间进度停滞。
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from loguru import logger

from app.chat.cache import cache
from app.chat.database import SessionLocal
from app.chat.db_models import ExpQuestion, ExpRun, ExpRunResult
from app.experiment import answer_eval
from app.experiment.runner import needs_dense_embedding, run_config_for_question
from app.experiment.service import RUN_KIND_QA, exp_kb_scope
from app.kb.milvus_client import MilvusManager
from app.kb.multimodal_embedding import get_multimodal_embedding_service
from app.settings import settings


class _CancelledRun(Exception):
    """阶段回调内检测到取消请求时抛出，快速中断当前题。"""


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


def _qa_extra_units(is_ood: bool) -> int:
    """QA 题在检索之外消耗的单元：生成 1 + 判分（OOD 拒答 1 / 库内正确率+忠实度 2）。"""
    return 2 if is_ood else 3


def planned_units(questions: list[dict], config_count: int, kind: str, need_dense: bool) -> int:
    """进度单元总数：初始化 1 + （查询向量 1）+ 每题（每配置检索 1 + QA 生成/判分单元）。"""
    per_q = max(1, int(config_count))
    total = 1 + (1 if need_dense else 0)
    for q in questions:
        units = per_q
        if kind == RUN_KIND_QA:
            units += _qa_extra_units(bool(q.get("is_ood")))
        total += units
    return max(1, total)


async def create_exp_run_job(run_id: int) -> str | None:
    """调度实验运行到后台线程池，返回 task_id（即 run_id 字符串）；Redis 不可写时返回 None。"""
    meta = {
        "run_id": run_id,
        "status": "queued",
        "percent": 0,
        "done": 0,
        "total": None,
        "stage": "排队中",
        "stage_at": None,
        "started_at": None,
        "units_done": 0,
        "units_total": None,
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


def enrich_job_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """补充服务端计算的已用时长（活动任务），避免浏览器时钟偏差。"""
    if not isinstance(meta, dict):
        return {}
    out = dict(meta)
    started = out.get("started_at")
    if isinstance(started, (int, float)) and out.get("status") in ("queued", "running"):
        out["elapsed_seconds"] = max(0, int(time.time() - started))
    return out


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
                "answer": r.answer or "",
                "gold_file_keys": list(r.gold_file_keys or []),
                "stratum": r.stratum or "",
                "is_ood": bool(r.is_ood),
            }
            for r in items
        ]
    finally:
        db.close()


def _run_exp_thread(run_id: int) -> None:
    """实验执行线程：逐题 × 逐配置评测，Redis 上报阶段进度，协作式取消。"""
    db = SessionLocal()
    try:
        run = db.query(ExpRun).filter(ExpRun.id == run_id).first()
        if not run:
            return
        configs = list(run.configs or [])
        dataset_id = run.dataset_id
        kind = run.kind or "retrieval"
        eval_idx = run.eval_config_idx
    finally:
        db.close()

    if not configs:
        _set_run_status(run_id, "failed", "配置为空")
        _update_meta(run_id, status="failed", error="配置为空")
        return

    # 评测配置下标越界（如配置被改写）时静默降级为仅检索，避免整次运行失败
    if eval_idx is not None and not (0 <= int(eval_idx) < len(configs)):
        logger.warning("实验运行 eval_config_idx 越界，已忽略生成评测 run_id={} idx={}", run_id, eval_idx)
        eval_idx = None
    answer_cfg: dict | None = None
    judge_cfg: dict | None = None
    if eval_idx is not None:
        answer_cfg = answer_eval.exp_eval_llm_config("answer")
        judge_cfg = answer_eval.exp_eval_llm_config("judge")
        if not answer_cfg or not judge_cfg:
            _set_run_status(run_id, "failed", "未配置 EXP_EVAL_LLM_API_KEY / EMBEDDING_API_KEY，无法进行生成评测")
            _update_meta(run_id, status="failed", error="未配置 EXP_EVAL_LLM_API_KEY / EMBEDDING_API_KEY")
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
    milvus = MilvusManager()
    embedding_service = get_multimodal_embedding_service()
    need_dense = needs_dense_embedding(configs)
    if need_dense and not (settings.EMBEDDING_API_KEY or "").strip():
        _set_run_status(run_id, "failed", "未配置 EMBEDDING_API_KEY，无法生成查询向量")
        _update_meta(run_id, status="failed", error="未配置 EMBEDDING_API_KEY")
        return

    units_total = planned_units(questions, len(configs), kind, need_dense)
    state = {"units_done": 0, "cur_q": 0}

    def _stage(text: str, units: int = 1) -> None:
        """阶段回调：检测取消 → 累加单元 → 写 Redis；阶段边界即可中断。"""
        if is_cancel_requested(run_id):
            raise _CancelledRun()
        state["units_done"] = min(units_total, state["units_done"] + max(1, int(units)))
        cur = state["cur_q"]
        prefix = f"第 {cur}/{total} 题 · " if cur else ""
        _update_meta(
            run_id,
            status="running",
            stage=f"{prefix}{text}",
            stage_at=time.time(),
            units_done=state["units_done"],
            units_total=units_total,
            percent=min(99, int(state["units_done"] * 100 / units_total)),
            done=max(0, cur - 1),
            total=total,
        )

    _set_run_status(run_id, "running")
    _update_meta(
        run_id,
        status="running",
        stage="初始化检索服务",
        started_at=time.time(),
        stage_at=time.time(),
        done=0,
        total=total,
        units_done=0,
        units_total=units_total,
        percent=0,
    )
    try:
        milvus.init_collection()
    except Exception as e:  # noqa: BLE001
        _set_run_status(run_id, "failed", f"Milvus 不可用: {e}")
        _update_meta(run_id, status="failed", error=f"Milvus 不可用: {e}")
        return

    cancelled = False
    try:
        _stage("初始化检索服务", 1)
        scope = exp_kb_scope(dataset_id)
        for qi, q in enumerate(questions, 1):
            state["cur_q"] = qi
            if is_cancel_requested(run_id):
                cancelled = True
                break
            dense_embedding: list[float] | None = None
            embed_error: str | None = None
            if need_dense:
                _stage("生成查询向量", 1)
                try:
                    dense_embedding = embedding_service.get_text_embeddings([q["question"]])[0]
                except Exception as e:  # noqa: BLE001
                    embed_error = f"embedding_failed: {e}"[:500]

            rows: list[ExpRunResult] = []
            if embed_error:
                # 整题跳过：一次性消耗该题全部单元，避免进度停滞
                skip_units = len(configs) + (_qa_extra_units(q["is_ood"]) if kind == RUN_KIND_QA else 0)
                _stage("跳过（查询向量失败）", skip_units)
                for idx in range(len(configs)):
                    rows.append(
                        ExpRunResult(
                            run_id=run_id, config_idx=idx, question_id=q["id"],
                            is_ood=q["is_ood"], error=embed_error,
                        )
                    )
            else:
                try:
                    for idx, cfg in enumerate(configs):
                        if is_cancel_requested(run_id):
                            cancelled = True
                            break
                        try:
                            res = run_config_for_question(
                                query=q["question"],
                                gold_file_keys=q["gold_file_keys"],
                                config=cfg,
                                dense_embedding=dense_embedding,
                                milvus=milvus,
                                kb_scope=scope,
                                generate=(eval_idx is not None and idx == int(eval_idx)),
                                reference_answer=q.get("answer") or "",
                                is_ood=q["is_ood"],
                                answer_cfg=answer_cfg,
                                judge_cfg=judge_cfg,
                                on_stage=_stage,
                            )
                        except _CancelledRun:
                            raise
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
                                answer=res.get("answer") or "",
                                answer_latency_ms=int(res.get("answer_latency_ms") or 0),
                                answer_metrics=res.get("answer_metrics") or {},
                            )
                        )
                except _CancelledRun:
                    cancelled = True
            if rows:
                db = SessionLocal()
                try:
                    db.add_all(rows)
                    db.commit()
                finally:
                    db.close()
            state["units_done"] = min(
                units_total,
                max(state["units_done"], int(planned_units(questions[:qi], len(configs), kind, need_dense))),
            )
            _update_meta(
                run_id,
                status="running",
                done=qi,
                total=total,
                units_done=state["units_done"],
                units_total=units_total,
                percent=min(99, int(state["units_done"] * 100 / units_total)),
            )
            if cancelled:
                break
    except _CancelledRun:
        cancelled = True
    except Exception as e:  # noqa: BLE001
        logger.exception("实验运行失败 run_id=%s", run_id)
        _set_run_status(run_id, "failed", str(e)[:500])
        _update_meta(run_id, status="failed", error=str(e)[:500])
        return

    if cancelled:
        _set_run_status(run_id, "cancelled", "用户已取消")
        _update_meta(run_id, status="cancelled", stage="已取消", error="用户已取消")
    else:
        _set_run_status(run_id, "completed")
        _update_meta(
            run_id,
            status="completed",
            stage="已完成",
            percent=100,
            done=total,
            total=total,
            units_done=units_total,
            units_total=units_total,
        )
    cache.delete(_cancel_key(run_id))


def exp_run_is_active(run_id: int) -> bool:
    """运行是否处于活动状态（防止重复调度）。"""
    meta = get_exp_run_job_meta(run_id)
    return bool(meta and meta.get("status") in ("queued", "running"))
